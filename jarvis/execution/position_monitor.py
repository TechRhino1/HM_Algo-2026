"""
HM Algo 2.0 — Continuous Position Monitor Engine.

A dedicated background thread that independently monitors and dynamically manages
EVERY open position (AI-opened and manually opened) at a 2-second resolution,
completely decoupled from the 15-60s orchestration scan cycle.

Management capabilities:
  - Emergency SL placement for manual trades with no SL or dangerously wide SL
  - 3-Stage micro breakeven + profit lock (Stages 1/2/3)
  - Structural S/R ratchet trailing (Higher-Low / Lower-High)
  - Regime-invalidation exit (regime flips against trade → tighten to 80% lock)
  - VWAP cross alert + optional tighten
  - Drawdown emergency brake (floating DD > 5% equity → all positions → breakeven)
  - Spread blowout protection (pauses modifications during extreme spread)
  - Momentum exhaustion exit (trend_score flip against trade)
"""
import time
import logging
import threading
from typing import Dict, List, Optional, Any, Set, Tuple
from datetime import datetime, timezone

from jarvis.data.schemas import PositionSnapshot, MarketContext, AccountSnapshot, MarketRegime
from jarvis.execution.mt5_client import MT5Client
from jarvis.execution.exit_policy import ExitPolicy, evaluate_exit
from jarvis.application.state_manager import StateManager, GLOBAL_STATE
from jarvis.application.event_bus import EventBus, GLOBAL_EVENT_BUS
from jarvis.market.market_context import MarketContextEngine
from jarvis.market.data_feed import DataFeedEngine

logger = logging.getLogger("JARVIS_PositionMonitor")

# ─── Constants ─────────────────────────────────────────────────────────────────
MONITOR_INTERVAL_SEC      = 2.0    # Monitor loop tick rate
CONTEXT_CACHE_TTL_SEC     = 10.0   # Re-fetch market context every 10s per symbol
EMERGENCY_SL_ATR_MULT     = 2.0    # Auto-SL for manual trades: 2× ATR from entry
DANGEROUS_SL_ATR_MULT     = 3.0    # SL wider than 3× ATR → tighten to 2× ATR

# Profit-lock / trailing thresholds are NO LONGER defined here.
# Every stop-ratchet decision is delegated to jarvis.execution.exit_policy, which
# is the single source of truth shared with the backtester. The module-level
# STAGE1/2/3 and STD_ATR_TRIGGER constants that used to live here drifted away
# from the backtest values, which is precisely what made live results
# irreproducible. Only the partial-exit defaults remain.
PARTIAL_TP_TRIGGER_R       = 1.0    # Default partial TP trigger fallback (1.0R)
PARTIAL_CLOSE_PCT          = 0.50   # 50% scale out at partial target

# Regime invalidation triggers
REGIME_INVALIDATION_CONFIDENCE = 0.70   # Regime confidence required to act
FLOAT_DD_EMERGENCY_PCT         = 5.0    # % of equity — triggers emergency brake

# Magic number used by HM Algo 2.0 orders (manual trades have different magic)
JARVIS_MAGIC_NUMBER        = 888999

# Spread blowout: pause modifications if spread > 2× typical
SPREAD_BLOWOUT_MULT        = 2.0

# 180s Post-entry discretionary grace period (Spec v2.1 Refinement 3)
DISCRETIONARY_GRACE_PERIOD_SEC = 180.0

# Manual trade isolation mode (Spec v2.1 Refinement 4)
MANUAL_MANAGEMENT_MODE         = "PROTECT_ONLY"


class PositionMonitorEngine:
    """
    Dedicated 2-second loop that manages all open positions independently of
    the main orchestration cycle. Covers both AI-placed and manual trades.
    """

    def __init__(
        self,
        mt5_client: MT5Client,
        data_feed: DataFeedEngine,
        context_engine: MarketContextEngine,
        state_manager: StateManager = GLOBAL_STATE,
        event_bus: EventBus = GLOBAL_EVENT_BUS,
        ml_predictor: Optional[Any] = None,
        manual_mode: Optional[str] = None,
    ):
        self.mt5_client     = mt5_client
        self.data_feed      = data_feed
        self.context_engine = context_engine
        self.state_manager  = state_manager
        self.event_bus      = event_bus
        self.ml_predictor   = ml_predictor
        import os
        self.manual_mode    = (manual_mode or os.environ.get("JARVIS_MANUAL_MANAGEMENT_MODE", MANUAL_MANAGEMENT_MODE)).upper()

        self._running       = False
        self._thread: Optional[threading.Thread] = None

        # Per-symbol context cache  {symbol: (context, fetched_at)}
        self._ctx_cache: Dict[str, tuple] = {}
        self._ctx_lock = threading.Lock()

        # Per-ticket tracking: last action taken (to avoid log spam)
        self._last_action: Dict[int, str] = {}
        # Per-ticket tracking for partial closes (§B-2 / §B-3)
        self._partially_closed_tickets: Set[int] = set()
        # Per-ticket live MFE high-water mark tracking (E2)
        self._peak_favorable_price: Dict[int, float] = {}
        # Autonomous dynamic trailing tracking
        self._initial_risk_dist: Dict[int, float] = {}
        self._highest_favorable_price: Dict[int, float] = {}

        # D19 — the trade's own path, kept as excursions in PRICE UNITS.
        #
        # Stored as distances rather than as prices because a price cannot be
        # interpreted without `open_price`, and by the time a close is handled the
        # position (and therefore its open price) is gone.
        self._peak_mfe: Dict[int, float] = {}
        self._peak_mae: Dict[int, float] = {}
        # Opposite extreme to `_highest_favorable_price`: for a BUY the running
        # low, for a SELL the running high. Adverse, so the sense is reversed.
        self._lowest_adverse_price: Dict[int, float] = {}
        # Excursions of tickets that closed recently, so the close handler can
        # still read them. Bounded: the monitor runs for the life of the process,
        # so an unbounded map grows with every trade the account ever takes.
        self._closed_excursions: Dict[int, tuple] = {}
        self._closed_excursions_order: list = []
        self._closed_excursions_cap = 512

        # ── Canonical exit-policy state (shared arithmetic with the backtest) ──
        # 1R must be frozen at position open, otherwise every stop-ratchet would
        # rescale R against the *current* stop and thresholds would silently drift.
        self._initial_sl: Dict[int, float] = {}
        self._initial_tp: Dict[int, float] = {}
        self._initial_risk_usd: Dict[int, float] = {}
        self._entry_ml_prob: Dict[int, float] = {}
        self._exit_policy: Dict[int, ExitPolicy] = {}
        self._be_locked: Set[int] = set()
        self._position_milestones: Dict[int, Dict[str, Any]] = {}

    # ─── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        if not self._running:
            self._running = True
            self._thread = threading.Thread(
                target=self._monitor_loop, daemon=True, name="position_monitor"
            )
            self._thread.start()
            logger.info("PositionMonitorEngine started (2s resolution).")

    def stop(self):
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        logger.info("PositionMonitorEngine stopped.")

    # ─── Helpers ───────────────────────────────────────────────────────────────

    def _remember_closed_excursions(self, ticket: int) -> None:
        """Retire a closed ticket's MFE/MAE into the bounded history map."""
        mfe = self._peak_mfe.get(ticket)
        mae = self._peak_mae.get(ticket)
        if mfe is None and mae is None:
            return
        self._closed_excursions[ticket] = (mfe, mae)
        self._closed_excursions_order.append(ticket)
        while len(self._closed_excursions_order) > self._closed_excursions_cap:
            oldest = self._closed_excursions_order.pop(0)
            self._closed_excursions.pop(oldest, None)

    def pop_excursions(self, ticket: int) -> Optional[tuple]:
        """The measured (mfe, mae) for a ticket that has just closed, in price units.

        Returns `None` when the monitor never sampled this ticket's path — the
        position was opened before this process started, or closed before the
        first scan. `None` must be written as NULL, never as 0.0: "not measured"
        and "measured, no excursion" are different facts, and a learner that
        cannot tell them apart is being fed a fabricated zero.

        The value is removed once read, so a ticket cannot be credited twice.
        """
        try:
            return self._closed_excursions.pop(int(ticket))
        except (KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def _coerce_positive_float(value: Any, default: Optional[float] = None) -> Optional[float]:
        """Return ``value`` as a positive float, or ``default`` if that is impossible.

        Guards the monitoring loop against malformed decision payloads: a mocked
        or partially populated decision object used to raise TypeError on
        comparison and abort management of the position entirely.
        """
        if value is None or isinstance(value, bool):
            return default
        try:
            f = float(value)
        except (TypeError, ValueError):
            return default
        if not (f > 0.0):
            return default
        if f != f or f in (float("inf"), float("-inf")):   # NaN / inf
            return default
        return f

    # ─── Main loop ─────────────────────────────────────────────────────────────

    def _monitor_loop(self):
        while self._running:
            start = time.monotonic()
            try:
                self._run_monitor_tick()
            except Exception as e:
                logger.error(f"PositionMonitor tick error: {e}", exc_info=True)
            elapsed = time.monotonic() - start
            sleep_time = max(0.0, MONITOR_INTERVAL_SEC - elapsed)
            time.sleep(sleep_time)

    def _run_monitor_tick(self):
        positions: List[PositionSnapshot] = self.state_manager.positions
        if not positions:
            return

        account: Optional[AccountSnapshot] = self.state_manager.account
        if account is None:
            return

        equity = account.equity
        balance = account.balance

        # ── Drawdown Emergency Brake ────────────────────────────────────────
        total_float_pnl = sum(p.profit for p in positions)
        float_dd_pct = abs(total_float_pnl / equity * 100) if (total_float_pnl < 0 and equity > 0) else 0.0
        emergency_brake = float_dd_pct >= FLOAT_DD_EMERGENCY_PCT

        if emergency_brake:
            logger.warning(
                f"🚨 EMERGENCY BRAKE: Floating DD={float_dd_pct:.1f}% exceeds {FLOAT_DD_EMERGENCY_PCT}% threshold. "
                f"Tightening ALL positions to breakeven."
            )

        # ── Prune memory for closed tickets ─────────────────────────────────
        active_tickets = {p.ticket for p in positions}
        self._partially_closed_tickets = {t for t in self._partially_closed_tickets if t in active_tickets}
        self._last_action = {t: act for t, act in self._last_action.items() if t in active_tickets}
        self._peak_favorable_price = {t: pr for t, pr in self._peak_favorable_price.items() if t in active_tickets}
        self._initial_risk_dist = {t: r for t, r in self._initial_risk_dist.items() if t in active_tickets}
        self._highest_favorable_price = {t: pr for t, pr in self._highest_favorable_price.items() if t in active_tickets}
        self._initial_sl = {t: v for t, v in self._initial_sl.items() if t in active_tickets}
        self._exit_policy = {t: v for t, v in self._exit_policy.items() if t in active_tickets}
        self._be_locked = {t for t in self._be_locked if t in active_tickets}
        self._lowest_adverse_price = {t: v for t, v in self._lowest_adverse_price.items() if t in active_tickets}
        self._position_milestones = {t: v for t, v in self._position_milestones.items() if t in active_tickets}

        # D19 — a closed ticket's excursions are the ONLY record of its path, so
        # they are retired into a bounded map instead of dropped here. Dropping
        # them is why `update_closed_trade` had nothing to write and hardcoded 0.0.
        for t in list(self._peak_mfe):
            if t in active_tickets:
                continue
            self._remember_closed_excursions(t)
            self._peak_mfe.pop(t, None)
            self._peak_mae.pop(t, None)

        # ── Per-position management ─────────────────────────────────────────
        for pos in positions:
            try:
                self._manage_single_position(pos, equity, balance, emergency_brake)
            except Exception as e:
                logger.error(f"Error managing position #{pos.ticket}: {e}", exc_info=True)

    def _execute_exit(self, pos: PositionSnapshot, reason: str) -> bool:
        """Central canonical exit dispatcher with grace period enforcement and audit logging."""
        if self._is_manual_trade(pos) and getattr(self, "manual_mode", MANUAL_MANAGEMENT_MODE) != "FULL":
            logger.debug(f"🛡️ Discretionary exit '{reason}' suppressed for manual trade #{pos.ticket}")
            return False

        open_dur_sec = self._get_position_duration_sec(pos)
        if open_dur_sec < DISCRETIONARY_GRACE_PERIOD_SEC:
            logger.info(
                f"🛡️ GRACE PERIOD: Discretionary exit '{reason}' suppressed for #{pos.ticket} "
                f"({pos.symbol}) — age {open_dur_sec:.1f}s < {DISCRETIONARY_GRACE_PERIOD_SEC:.0f}s."
            )
            return False

        logger.info(
            f"🚪 CANONICAL EXIT: Closing #{pos.ticket} {pos.symbol} {pos.type} @ {pos.current_price:.5f} | "
            f"Reason={reason} | Duration={open_dur_sec/60:.1f}m | Floating PnL=${pos.profit:.2f}"
        )
        res = self.mt5_client.close_position(pos.ticket)
        if res and res.get("status") in ("CLOSED", "PARTIALLY_CLOSED"):
            self._remember_closed_excursions(pos.ticket)
            return True
        return False

    # ─── Single-Position Logic ──────────────────────────────────────────────────

    def _manage_single_position(
        self,
        pos: PositionSnapshot,
        equity: float = 10000.0,
        balance: float = 10000.0,
        emergency_brake: bool = False,
    ):
        symbol  = pos.symbol
        is_manual = self._is_manual_trade(pos)
        regime  = self._get_cached_regime(symbol)

        # ── Fetch (or reuse cached) market context ──────────────────────────
        ctx = self._get_context(symbol)
        if ctx is None:
            logger.debug(f"No market context for {symbol} — skipping #{pos.ticket}")
            return

        c_price = ctx.current_price
        atr     = ctx.volatility.atr if ctx.volatility.atr > 0 else (c_price * 0.005)
        spread  = ctx.volatility.current_spread_pips

        # ── Spread blowout guard ────────────────────────────────────────────
        from jarvis.data.symbol_registry import resolve as _res
        try:
            spec = _res(symbol)
            typical_spread = spec.typical_spread_pips
            pip_size = spec.pip_size if spec.pip_size > 0 else 0.0001
            digits = spec.digits
        except Exception:
            typical_spread = 3.0
            pip_size = 0.0001
            digits = 5

        if spread > (typical_spread * SPREAD_BLOWOUT_MULT):
            logger.warning(f"⚠️ Spread blowout {spread:.1f} pips on #{pos.ticket} — skipping modification.")
            return

        new_sl  = pos.sl
        new_tp  = pos.tp
        actions = []

        # ── Manual Trade Isolation & Trailing (Spec v2.1 Refinement 4) ──────
        effective_manual_mode = getattr(self, "manual_mode", MANUAL_MANAGEMENT_MODE)
        if is_manual:
            if effective_manual_mode == "PROTECT_ONLY":
                # Emergency SL only if missing
                if pos.sl <= 0:
                    new_sl, act = self._handle_manual_sl(pos, c_price, atr, new_sl)
                    if new_sl != pos.sl and new_sl > 0:
                        logger.info(f"🛡️ MANUAL PROTECT_ONLY: Emergency SL set on #{pos.ticket}: {new_sl:.4f}")
                        self.mt5_client.modify_position(ticket=pos.ticket, sl=new_sl, tp=pos.tp)
                # NEVER trail, partial close, or discretionary close manual trades in PROTECT_ONLY mode!
                return
            elif pos.sl <= 0:
                # In TRAIL/FULL modes, place emergency SL if missing before trailing
                new_sl, act = self._handle_manual_sl(pos, c_price, atr, new_sl)
                if new_sl != pos.sl and new_sl > 0:
                    logger.info(f"🛡️ MANUAL {effective_manual_mode}: Emergency SL set on #{pos.ticket}: {new_sl:.4f}")
                    self.mt5_client.modify_position(ticket=pos.ticket, sl=new_sl, tp=pos.tp)

        # ── 180s Post-Entry Discretionary Grace Period (Spec v2.1 Refinement 3) ──
        open_dur_sec = self._get_position_duration_sec(pos)
        in_grace_period = (open_dur_sec < DISCRETIONARY_GRACE_PERIOD_SEC)

        # ─────────────────────────────────────────────────────────────────────
        # EMERGENCY BRAKE: tighten everything to breakeven
        # ─────────────────────────────────────────────────────────────────────
        if emergency_brake:
            new_sl = self._emergency_breakeven(pos, c_price, atr, new_sl)
            if new_sl != pos.sl:
                actions.append(f"EMERGENCY_BRAKE→BE@{new_sl:.4f}")

        else:
            # ── 0. Partial Profit-Taking (§B-2 / §B-3, Suppressed in Grace Period) ──
            if not in_grace_period and (not is_manual or effective_manual_mode == "FULL") and pos.ticket not in self._partially_closed_tickets and pos.volume >= 0.02:
                decision_obj = self.state_manager.latest_decisions.get(symbol)
                first_target = self._coerce_positive_float(
                    getattr(decision_obj, "first_target_price", None)
                )
                target_pct = self._coerce_positive_float(
                    getattr(decision_obj, "first_target_volume_pct", None),
                    default=PARTIAL_CLOSE_PCT,
                )
                if not (0.0 < target_pct < 1.0):
                    target_pct = PARTIAL_CLOSE_PCT

                risk_dist_init = abs(pos.open_price - pos.sl) if pos.sl > 0 else (atr * 1.5)
                if first_target is None:
                    first_target = (pos.open_price + (risk_dist_init * PARTIAL_TP_TRIGGER_R)) if pos.type == "BUY" else (pos.open_price - (risk_dist_init * PARTIAL_TP_TRIGGER_R))

                is_target_hit = (c_price >= first_target) if pos.type == "BUY" else (c_price <= first_target)
                if is_target_hit:
                    close_volume = round(pos.volume * target_pct, 2)
                    remaining_volume = round(pos.volume - close_volume, 2)
                    if close_volume >= 0.01 and remaining_volume >= 0.01:
                        p_res = self.mt5_client.close_position(pos.ticket, volume=close_volume)
                        if p_res and p_res.get("status") in ("PARTIALLY_CLOSED", "CLOSED"):
                            self._partially_closed_tickets.add(pos.ticket)
                            logger.info(f"🎯 PARTIAL TP HIT: #{pos.ticket} {symbol} closed {close_volume} lots @ {c_price:.4f}. Remaining: {remaining_volume}")
                            actions.append(f"PARTIAL_TP_{int(target_pct*100)}%@{c_price:.4f}")
                            _p = self._exit_policy.get(pos.ticket) or ExitPolicy.for_symbol(symbol, spec)
                            _be_buf = _p.buffer_distance(risk_dist_init)
                            be_candidate = round(
                                pos.open_price + _be_buf if pos.type == "BUY" else pos.open_price - _be_buf,
                                digits,
                            )
                            if pos.type == "BUY" and be_candidate > new_sl and be_candidate < c_price:
                                new_sl = be_candidate
                                actions.append(f"PARTIAL_BE@{new_sl:.4f}")
                            elif pos.type == "SELL" and (new_sl == 0 or be_candidate < new_sl) and be_candidate > c_price:
                                new_sl = be_candidate
                                actions.append(f"PARTIAL_BE@{new_sl:.4f}")

            # ── 1.5 Adversarial Order Flow Shield (Suppressed in Grace Period) ────
            if not in_grace_period:
                shield_triggered, shield_action = self._check_adversarial_order_flow_shield(pos, ctx, c_price, atr, digits)
                if shield_triggered:
                    if shield_action == "CLOSE":
                        logger.warning(
                            f"🛡️ ADVERSARIAL ORDER FLOW SHIELD: Closing underwater/flat #{pos.ticket} ({pos.symbol} {pos.type}) "
                            f"to prevent full stop-out."
                        )
                        self._execute_exit(pos, reason="ADVERSARIAL_ORDER_FLOW_SHIELD")
                        return
                    elif shield_action is not None and isinstance(shield_action, (int, float)):
                        shield_sl = float(shield_action)
                        if pos.type == "BUY" and shield_sl > new_sl and shield_sl < c_price:
                            new_sl = shield_sl
                            actions.append(f"ADVERSARIAL_SHIELD@{new_sl:.4f}")
                        elif pos.type == "SELL" and (new_sl == 0 or shield_sl < new_sl) and shield_sl > c_price:
                            new_sl = shield_sl
                            actions.append(f"ADVERSARIAL_SHIELD@{new_sl:.4f}")
            else:
                logger.debug(f"🛡️ [GRACE_PERIOD] #{pos.ticket} ({symbol}): Discretionary shield suppressed ({open_dur_sec:.1f}s / {DISCRETIONARY_GRACE_PERIOD_SEC:.0f}s).")

            # ── 2. Canonical R-multiple stop management ─────────────────────────
            # ALL stop-ratchet arithmetic is delegated to
            # jarvis.execution.exit_policy.evaluate_exit(), which the backtester
            # also calls. Previously this block held its own horizon-adaptive
            # Stage 0/1/2 table with values that disagreed with both the backtest
            # and order_manager, so live results could never reproduce a backtest.
            #
            # 1R is frozen here on first sight of the position and never
            # recomputed, otherwise every ratchet would rescale R against the
            # *current* stop and the thresholds would silently drift.
            if pos.ticket not in self._initial_risk_dist:
                self._initial_risk_dist[pos.ticket] = (
                    abs(pos.open_price - pos.sl) if pos.sl > 0 else (1.5 * atr)
                )
            risk_dist = max(self._initial_risk_dist[pos.ticket], pip_size * 5)

            # Freeze the original stop, which defines 1R for the policy.
            if pos.ticket not in self._initial_sl:
                self._initial_sl[pos.ticket] = (
                    pos.sl if pos.sl > 0
                    else (pos.open_price - risk_dist if pos.type == "BUY"
                          else pos.open_price + risk_dist)
                )

            # Freeze initial Take Profit and initial monetary risk ceiling
            if pos.ticket not in self._initial_tp:
                self._initial_tp[pos.ticket] = pos.tp
            if pos.ticket not in self._initial_risk_usd:
                _pip_val = float(getattr(spec, "pip_value_per_lot", 10.0) or 10.0) if spec else 10.0
                _p_sz = float(getattr(spec, "pip_size", pip_size) or pip_size)
                _d_unit = (_pip_val / max(_p_sz, 1e-9)) if _p_sz > 0 else 100_000.0
                self._initial_risk_usd[pos.ticket] = risk_dist * _d_unit * pos.volume

            if pos.ticket not in self._exit_policy:
                self._exit_policy[pos.ticket] = ExitPolicy.for_symbol(symbol, spec)
            policy = self._exit_policy[pos.ticket]

            if pos.ticket not in self._position_milestones:
                # 3-Tier AI Milestone initialization (TP1: Scale-Out / Fast Cash, TP2: Structure / Take Profit, TP3: Macro Runner)
                m_tp1 = (pos.open_price + (risk_dist * policy.fast_cash_r)) if pos.type == "BUY" else (pos.open_price - (risk_dist * policy.fast_cash_r))
                m_tp2 = pos.tp if pos.tp > 0 else ((pos.open_price + (risk_dist * 2.0)) if pos.type == "BUY" else (pos.open_price - (risk_dist * 2.0)))
                m_tp3 = (pos.open_price + (risk_dist * 3.5)) if pos.type == "BUY" else (pos.open_price - (risk_dist * 3.5))
                self._position_milestones[pos.ticket] = {
                    "tp1": round(m_tp1, digits),
                    "tp2": round(m_tp2, digits),
                    "tp3": round(m_tp3, digits),
                    "status": "OPEN"
                }

            # Track the running favourable excursion in price units.
            if pos.type == "BUY":
                prev_high = self._highest_favorable_price.get(pos.ticket, pos.open_price)
                high_price = max(prev_high, c_price)
                self._highest_favorable_price[pos.ticket] = high_price
                self._peak_favorable_price[pos.ticket] = high_price
                favorable_dist = max(0.0, high_price - pos.open_price)

                prev_low = self._lowest_adverse_price.get(pos.ticket, pos.open_price)
                low_price = min(prev_low, c_price)
                self._lowest_adverse_price[pos.ticket] = low_price
                adverse_dist = max(0.0, pos.open_price - low_price)
            else:
                prev_low = self._highest_favorable_price.get(pos.ticket, pos.open_price)
                low_price = min(prev_low, c_price)
                self._highest_favorable_price[pos.ticket] = low_price
                self._peak_favorable_price[pos.ticket] = low_price
                favorable_dist = max(0.0, pos.open_price - low_price)

                prev_high = self._lowest_adverse_price.get(pos.ticket, pos.open_price)
                high_price = max(prev_high, c_price)
                self._lowest_adverse_price[pos.ticket] = high_price
                adverse_dist = max(0.0, high_price - pos.open_price)

            # D19 — accumulate both excursions for the life of the position.
            #
            # These are SAMPLED at monitor cadence, not read from bar high/low, so
            # a spike between two scans is missed: the result is a LOWER BOUND on
            # the true intrabar extreme. That is still a measurement. The 0.0 the
            # close handler used to write was a fabricated one, and it read to any
            # downstream learner as "measured, no excursion".
            self._peak_mfe[pos.ticket] = max(self._peak_mfe.get(pos.ticket, 0.0), favorable_dist)
            self._peak_mae[pos.ticket] = max(self._peak_mae.get(pos.ticket, 0.0), adverse_dist)

            # Structural reference for the ratchet (higher-low support for a BUY,
            # lower-high resistance for a SELL). Falls back to None when the
            # structure context is unavailable, in which case the ATR trail and
            # the R-milestones still provide progressive locking.
            struct_level: Optional[float] = None
            try:
                st = getattr(ctx, "structure", None)
                if st is not None:
                    if pos.type == "BUY" and getattr(st, "higher_lows", False) and st.demand_zone[0] > 0:
                        struct_level = float(st.demand_zone[0])
                    elif pos.type == "SELL" and getattr(st, "lower_highs", False) and st.supply_zone[1] > 0:
                        struct_level = float(st.supply_zone[1])
            except Exception:
                struct_level = None

            exit_dec = evaluate_exit(
                side=pos.type,
                entry=pos.open_price,
                initial_sl=self._initial_sl[pos.ticket],
                current_sl=new_sl,
                tp=pos.tp,
                price=c_price,
                favorable_dist=favorable_dist,
                atr=atr,
                policy=policy,
                partial_already_taken=pos.ticket in self._partially_closed_tickets,
                be_already_locked=pos.ticket in self._be_locked,
                struct_level=struct_level,
            )

            if exit_dec.partial_close_pct > 0.0 and pos.ticket not in self._partially_closed_tickets:
                # evaluate_exit signals *that* a partial is due; the actual lot
                # split is executed in step 0 above, so only record the intent.
                actions.append(f"PARTIAL_DUE_{exit_dec.partial_close_pct:.2f}")

            if exit_dec.new_sl and exit_dec.new_sl != new_sl:
                # Ratchets are one-way; evaluate_exit already enforces this, but we
                # re-assert it because new_sl may have been tightened upstream by
                # the emergency-brake / manual-SL / adversarial-shield steps.
                if pos.type == "BUY":
                    new_sl = max(new_sl, exit_dec.new_sl) if new_sl > 0 else exit_dec.new_sl
                else:
                    new_sl = min(new_sl, exit_dec.new_sl) if new_sl > 0 else exit_dec.new_sl

            if exit_dec.be_locked:
                self._be_locked.add(pos.ticket)

            for act in exit_dec.actions:
                actions.append(f"{act}@{new_sl:.{digits}f}")

            # ── 3.5 Live MFE Scale-Out & Retracement Protection (E2) ───────────
            if pos.type == "BUY":
                peak_mfe = self._peak_favorable_price[pos.ticket] - pos.open_price
                if peak_mfe >= (atr * 1.5):
                    current_gain = c_price - pos.open_price
                    giveback_pct = (peak_mfe - current_gain) / (peak_mfe + 1e-9)
                    if giveback_pct >= 0.40:
                        mfe_locked_sl = round(pos.open_price + (peak_mfe * 0.50), digits)
                        if mfe_locked_sl > new_sl and mfe_locked_sl < c_price:
                            new_sl = mfe_locked_sl
                            actions.append(f"MFE_RETRACE_50%_LOCK@{new_sl:.4f}")
            elif pos.type == "SELL":
                peak_mfe = pos.open_price - self._peak_favorable_price[pos.ticket]
                if peak_mfe >= (atr * 1.5):
                    current_gain = pos.open_price - c_price
                    giveback_pct = (peak_mfe - current_gain) / (peak_mfe + 1e-9)
                    if giveback_pct >= 0.40:
                        mfe_locked_sl = round(pos.open_price - (peak_mfe * 0.50), digits)
                        if (new_sl == 0 or mfe_locked_sl < new_sl) and mfe_locked_sl > c_price:
                            new_sl = mfe_locked_sl
                            actions.append(f"MFE_RETRACE_50%_LOCK@{new_sl:.4f}")

            # ── 4. Regime, VWAP & Momentum discretionary checks (Suppressed in Grace Period) ──
            if not in_grace_period:
                new_sl, act = self._check_regime_invalidation(pos, ctx, regime, c_price, atr, new_sl)
                if act:
                    actions.append(act)

                new_sl, act = self._check_vwap_cross(pos, ctx, c_price, atr, new_sl)
                if act:
                    actions.append(act)

                new_sl, act = self._check_momentum_exhaustion(pos, ctx, c_price, atr, new_sl)
                if act:
                    actions.append(act)

            # ── 5. Live ML Prediction & Dynamic SL/TP Adaptation ────────────
            ml_prob: Optional[float] = None
            style = self._determine_position_style(pos, ctx)
            if self.ml_predictor is not None and ctx is not None:
                try:
                    ml_feats = self.ml_predictor.extract_features(
                        context=ctx,
                        regime=regime,
                        trade_style=style,
                        tentative_bias=pos.type,
                    )
                    ml_prob = float(self.ml_predictor.predict_probability(ml_feats))
                    if pos.ticket not in self._entry_ml_prob:
                        self._entry_ml_prob[pos.ticket] = ml_prob
                except Exception as e:
                    logger.debug(f"Live ML prediction failed for #{pos.ticket} ({symbol}): {e}")
                    ml_prob = None

            # ── 5.1 Dynamic Take Profit (TP) Adaptation ─────────────────────
            current_r = ((c_price - pos.open_price) if pos.type == "BUY" else (pos.open_price - c_price)) / max(risk_dist, 1e-6)
            if not in_grace_period and ml_prob is not None:
                init_tp = self._initial_tp.get(pos.ticket, pos.tp)
                init_tp_r = abs(init_tp - pos.open_price) / max(risk_dist, 1e-6) if init_tp > 0 else 2.5
                adx_val = float(getattr(ctx.momentum, "adx", 20.0) or 20.0)
                trend_score = float(getattr(ctx.momentum, "trend_score", 0.0) or 0.0)
                trend_aligned = (pos.type == "BUY" and trend_score > 25.0) or (pos.type == "SELL" and trend_score < -25.0)

                # High ML Confidence (>=0.68) + Strong Trend: Extend TP to let winners run
                if ml_prob >= 0.68 and adx_val >= 25.0 and trend_aligned and current_r >= 1.0:
                    extension_r = max(init_tp_r, min(4.5, current_r + 2.0))
                    extended_tp = round(
                        pos.open_price + (risk_dist * extension_r) if pos.type == "BUY"
                        else pos.open_price - (risk_dist * extension_r),
                        digits,
                    )
                    if pos.type == "BUY" and (new_tp == 0 or extended_tp > new_tp):
                        new_tp = extended_tp
                        actions.append(f"ML_TP_EXTEND_{extension_r:.1f}R(p={ml_prob:.2f})")
                    elif pos.type == "SELL" and (new_tp == 0 or extended_tp < new_tp):
                        new_tp = extended_tp
                        actions.append(f"ML_TP_EXTEND_{extension_r:.1f}R(p={ml_prob:.2f})")

                # Low ML Confidence (<=0.45) or Divergence Detected: Contract TP to bank profit early
                elif (ml_prob <= 0.45 or getattr(ctx.momentum, "divergence", "NONE") not in ("NONE", None, "")) and current_r >= 0.75:
                    contracted_r = max(current_r + 0.35, 1.0)
                    if contracted_r < init_tp_r:
                        contracted_tp = round(
                            pos.open_price + (risk_dist * contracted_r) if pos.type == "BUY"
                            else pos.open_price - (risk_dist * contracted_r),
                            digits,
                        )
                        if pos.type == "BUY" and (new_tp == 0 or (contracted_tp < new_tp and contracted_tp > c_price)):
                            new_tp = contracted_tp
                            actions.append(f"ML_TP_CONTRACT_{contracted_r:.1f}R(p={ml_prob:.2f})")
                        elif pos.type == "SELL" and (new_tp == 0 or (contracted_tp > new_tp and contracted_tp < c_price)):
                            new_tp = contracted_tp
                            actions.append(f"ML_TP_CONTRACT_{contracted_r:.1f}R(p={ml_prob:.2f})")

            # ── 5.2 Dynamic ML Stop Loss (SL) Tightening ───────────────────
            if not in_grace_period and ml_prob is not None:
                entry_prob = self._entry_ml_prob.get(pos.ticket, ml_prob)
                prob_deteriorated = (ml_prob < 0.40) or (entry_prob - ml_prob >= 0.18)

                if prob_deteriorated:
                    if current_r > 0:
                        be_buf = policy.buffer_distance(risk_dist)
                        be_level = round(
                            pos.open_price + be_buf if pos.type == "BUY" else pos.open_price - be_buf,
                            digits,
                        )
                        if pos.type == "BUY" and be_level > new_sl and be_level < c_price:
                            new_sl = be_level
                            actions.append(f"ML_DETERIORATION_BE@{new_sl:.4f}(p={ml_prob:.2f})")
                        elif pos.type == "SELL" and (new_sl == 0 or be_level < new_sl) and be_level > c_price:
                            new_sl = be_level
                            actions.append(f"ML_DETERIORATION_BE@{new_sl:.4f}(p={ml_prob:.2f})")
                    elif -0.70 <= current_r <= 0.0 and atr > 0:
                        reduced_sl_dist = risk_dist * 0.50
                        tightened_sl = round(
                            pos.open_price - reduced_sl_dist if pos.type == "BUY" else pos.open_price + reduced_sl_dist,
                            digits,
                        )
                        if pos.type == "BUY" and tightened_sl > new_sl and tightened_sl < c_price:
                            new_sl = tightened_sl
                            actions.append(f"ML_RISK_CAP_0.5R@{new_sl:.4f}(p={ml_prob:.2f})")
                        elif pos.type == "SELL" and (new_sl == 0 or tightened_sl < new_sl) and tightened_sl > c_price:
                            new_sl = tightened_sl
                            actions.append(f"ML_RISK_CAP_0.5R@{new_sl:.4f}(p={ml_prob:.2f})")

            # ── 6. 3-Tier Milestone Progression Tracking ────────────────────
            if pos.ticket in self._position_milestones:
                if current_r >= 3.0:
                    self._position_milestones[pos.ticket]["status"] = "TP3_RUNNER_TRAILING"
                elif current_r >= 2.0:
                    self._position_milestones[pos.ticket]["status"] = "TP2_HIT_PROFIT_LOCKED"
                elif current_r >= policy.fast_cash_r or pos.ticket in self._be_locked or pos.ticket in self._partially_closed_tickets:
                    self._position_milestones[pos.ticket]["status"] = "TP1_HIT_BE_LOCKED"
                else:
                    self._position_milestones[pos.ticket]["status"] = "OPEN"

                pos.tp1 = self._position_milestones[pos.ticket]["tp1"]
                pos.tp2 = self._position_milestones[pos.ticket]["tp2"]
                pos.tp3 = self._position_milestones[pos.ticket]["tp3"]
                pos.milestone_status = self._position_milestones[pos.ticket]["status"]

            # ── 7. Horizon-Adaptive Stagnation & Time-Decay Auto-Exit ────────
            open_dur_sec = self._get_position_duration_sec(pos)
            if open_dur_sec > 0:
                # Scalp: 45 min max hold without progress (R < 0.50R) -> Close position
                if style == "SCALP" and open_dur_sec >= 2700.0 and current_r < 0.50:
                    self._execute_exit(pos, reason="SCALP_STAGNATION")
                    return

                # Day: 6 hours max hold without progress (R < 0.75R) -> Close position
                elif style in ("DAY_TRADING", "DAY", "INTRADAY") and open_dur_sec >= 21600.0 and current_r < 0.75:
                    self._execute_exit(pos, reason="DAY_STAGNATION")
                    return

                # Swing: 36 hours max hold in compression -> Close position
                elif style == "SWING" and open_dur_sec >= 129600.0:
                    is_comp = (
                        getattr(ctx.volatility, "state", "").upper() == "COMPRESSION"
                        or (regime and hasattr(regime, "primary_regime") and regime.primary_regime in (
                            MarketRegime.COMPRESSION, MarketRegime.CONSOLIDATION, MarketRegime.LOW_VOLATILITY, MarketRegime.RANGE
                        ))
                        or getattr(ctx.momentum, "adx", 0.0) < 18.0
                    )
                    if is_comp:
                        self._execute_exit(pos, reason="SWING_COMPRESSION_STAGNATION")
                        return

                # Fallback Regime-Adaptive Time-Decay Stale Trade Exit
                try:
                    regime_str = getattr(regime, "primary_regime", None)
                    r_name = getattr(regime_str, "value", str(regime_str or "DEFAULT")).upper()
                    if "TREND" in r_name:
                        max_stall_sec = 43200.0
                    elif "RANGE" in r_name or "LOW_VOLATILITY" in r_name:
                        max_stall_sec = 28800.0
                    elif "BREAKOUT" in r_name:
                        max_stall_sec = 21600.0
                    else:
                        max_stall_sec = 86400.0

                    if open_dur_sec >= max_stall_sec:
                        trend_score = getattr(ctx.momentum, "trend_score", 0.0)
                        profit_ratio = abs(pos.profit / (balance + 1e-9))
                        if profit_ratio < 0.005 and abs(trend_score) < 20.0:
                            self._execute_exit(pos, reason=f"REGIME_TIME_DECAY_{r_name}")
                            return
                        elif profit_ratio >= 0.005:
                            # Protect a stale-but-profitable trade with a breakeven floor
                            be_buffer = policy.buffer_distance(risk_dist)
                            be_cand = round(
                                pos.open_price + be_buffer if pos.type == "BUY"
                                else pos.open_price - be_buffer,
                                digits,
                            )
                            if pos.type == "BUY" and be_cand > new_sl and be_cand < c_price:
                                new_sl = be_cand
                                actions.append(f"TIME_DECAY_BE@{new_sl:.4f}")
                            elif pos.type == "SELL" and (new_sl == 0 or be_cand < new_sl) and be_cand > c_price:
                                new_sl = be_cand
                                actions.append(f"TIME_DECAY_BE@{new_sl:.4f}")
                except Exception as e:
                    logger.debug(f"Regime time-decay evaluation failed for #{pos.ticket} ({symbol}): {e}")

        # Monotonic ratchet enforcement (SL can only move closer to price, NEVER loosen or move backward)
        if pos.type == "BUY":
            if pos.sl > 0 and new_sl < pos.sl:
                new_sl = pos.sl
        elif pos.type == "SELL":
            if pos.sl > 0 and new_sl > pos.sl:
                new_sl = pos.sl

        # ── 8. Broker Constraints, Clamping & Pre-Dispatch Risk Verification ─
        spec_info = {}
        if hasattr(self.mt5_client, "get_symbol_trading_spec"):
            try:
                res_spec = self.mt5_client.get_symbol_trading_spec(symbol)
                if isinstance(res_spec, dict):
                    spec_info = res_spec
            except Exception:
                spec_info = {}

        def _val_float(val, fallback: float) -> float:
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                return float(val)
            return fallback

        def _val_int(val, fallback: int) -> int:
            if isinstance(val, int) and not isinstance(val, bool):
                return int(val)
            return fallback

        point = _val_float(spec_info.get("point"), pip_size * 0.1 if pip_size > 0 else 0.00001)
        stops_level = _val_int(spec_info.get("trade_stops_level"), 0)
        freeze_level = _val_int(spec_info.get("trade_freeze_level"), 0)
        spread_pts = _val_int(spec_info.get("spread"), int(spread * 10) if spread > 0 else 0)
        min_stop_pts = max(stops_level, freeze_level, spread_pts * 2, 10)
        min_stop_dist = min_stop_pts * point

        # Ensure new_sl does not encroach inside broker stop distance
        if pos.type == "BUY":
            if new_sl >= c_price - min_stop_dist:
                new_sl = round(c_price - min_stop_dist, digits)
            if pos.sl > 0 and new_sl < pos.sl:
                new_sl = pos.sl
        else:
            if new_sl <= c_price + min_stop_dist and new_sl > 0:
                new_sl = round(c_price + min_stop_dist, digits)
            if pos.sl > 0 and new_sl > pos.sl:
                new_sl = pos.sl

        # Ensure new_tp does not encroach inside broker stop distance
        if new_tp > 0:
            if pos.type == "BUY" and new_tp <= c_price + min_stop_dist:
                new_tp = round(c_price + min_stop_dist, digits)
            elif pos.type == "SELL" and new_tp >= c_price - min_stop_dist:
                new_tp = round(c_price - min_stop_dist, digits)

        # Pre-dispatch monetary risk check: modification must never increase monetary risk!
        init_risk = self._initial_risk_usd.get(pos.ticket)
        if init_risk and init_risk > 0 and new_sl > 0:
            _pip_val = float(getattr(spec, "pip_value_per_lot", 10.0) or 10.0) if spec else 10.0
            _p_sz = float(getattr(spec, "pip_size", pip_size) or pip_size)
            dollar_per_unit = (_pip_val / max(_p_sz, 1e-9)) if _p_sz > 0 else 100_000.0
            if "trade_tick_value" in spec_info and "trade_tick_size" in spec_info:
                tv = _val_float(spec_info.get("trade_tick_value"), 0.0)
                ts = _val_float(spec_info.get("trade_tick_size"), 0.0)
                if tv > 0 and ts > 0:
                    dollar_per_unit = tv / ts
            downside_dist = (pos.open_price - new_sl) if pos.type == "BUY" else (new_sl - pos.open_price)
            current_projected_risk = max(0.0, downside_dist) * dollar_per_unit * pos.volume
            if current_projected_risk > init_risk * 1.01:
                logger.warning(
                    f"🛡️ RISK GATE BLOCKED MODIFICATION on #{pos.ticket}: "
                    f"Projected risk ${current_projected_risk:.2f} > initial risk ${init_risk:.2f}."
                )
                new_sl = pos.sl

        # ── Apply modifications if SL changes by >= 1.0 pip or TP changes by >= 2.0 pips ────
        sl_pip_diff = abs(new_sl - pos.sl) / (pip_size if pip_size > 0 else 1.0)
        sl_changed = (sl_pip_diff >= 1.0) or (pos.sl == 0 and new_sl > 0)
        tp_pip_diff = abs(new_tp - pos.tp) / (pip_size if pip_size > 0 else 1.0) if new_tp > 0 and pos.tp > 0 else 0.0
        tp_changed = tp_pip_diff >= 2.0 or (pos.tp == 0 and new_tp > 0)

        if sl_changed or tp_changed:
            new_sl = round(new_sl, digits)
            new_tp = round(new_tp, digits)
            action_key = f"{new_sl:.{digits}f}_{new_tp:.{digits}f}"
            if self._last_action.get(pos.ticket) != action_key:
                res = self.mt5_client.modify_position(pos.ticket, sl=new_sl, tp=new_tp)
                status = res.get("status") if res else "FAILED"
                if status == "MODIFIED" or (status == "FAILED" and "No changes" in str(res.get("reason", ""))):
                    self._last_action[pos.ticket] = action_key
                    log_tag = "[MANUAL]" if is_manual else "[AI-ML]"
                    ml_str = f"ML_p={ml_prob:.2f}" if ml_prob is not None else "ML_p=N/A"
                    r_now = ((c_price - pos.open_price) if pos.type == "BUY" else (pos.open_price - c_price)) / max(risk_dist, 1e-6)
                    r_str = f"R={r_now:.2f}"
                    logger.info(
                        f"✅ {log_tag} #{pos.ticket} {pos.symbol} {pos.type} | "
                        f"SL: {pos.sl:.{digits}f}→{new_sl:.{digits}f} | "
                        f"TP: {pos.tp:.{digits}f}→{new_tp:.{digits}f} | "
                        f"{ml_str} | {r_str} | Actions: {', '.join(actions)}"
                    )
                    self.event_bus.publish_sync("position_managed", {
                        "ticket": pos.ticket,
                        "symbol": symbol,
                        "is_manual": is_manual,
                        "old_sl": pos.sl,
                        "new_sl": new_sl,
                        "old_tp": pos.tp,
                        "new_tp": new_tp,
                        "ml_prob": ml_prob,
                        "current_r": round(r_now, 2),
                        "actions": actions,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })
                else:
                    logger.warning(f"⚠️ Modify failed for #{pos.ticket}: {res}")

    # ─── SL Trailing Core ──────────────────────────────────────────────────────

    def _trail_sl(
        self,
        pos: PositionSnapshot,
        ctx: MarketContext,
        c_price: float,
        atr: float,
        current_sl: float,
        equity: float,
    ):
        """Legacy adapter over the canonical exit policy.

        Retained because external callers/tests use its ``(new_sl, actions)``
        signature. It contains NO exit arithmetic of its own — it resolves the
        policy, computes the favourable excursion, and forwards to
        :func:`jarvis.execution.exit_policy.evaluate_exit`, so any caller of this
        method gets byte-identical behaviour to the backtester.
        """
        spec = None
        try:
            from jarvis.data.symbol_registry import resolve as _res
            spec = _res(pos.symbol)
        except Exception:
            spec = None

        policy = ExitPolicy.for_symbol(pos.symbol, spec)
        digits = policy.digits

        if pos.type == "BUY":
            favorable_dist = max(0.0, c_price - pos.open_price)
        else:
            favorable_dist = max(0.0, pos.open_price - c_price)

        # The caller passes the *live* stop; the immutable 1R reference comes from
        # the tracking dict when available, else we fall back to the caller's stop.
        initial_sl = self._initial_sl.get(pos.ticket, current_sl)

        st = getattr(ctx, "structure", None)
        struct_level = None
        if st is not None:
            if pos.type == "BUY" and getattr(st, "higher_lows", False) and getattr(st, "demand_zone", (0, 0))[0] > 0:
                struct_level = float(st.demand_zone[0])
            elif pos.type == "SELL" and getattr(st, "lower_highs", False) and getattr(st, "supply_zone", (0, 0))[1] > 0:
                struct_level = float(st.supply_zone[1])

        dec = evaluate_exit(
            side=pos.type,
            entry=pos.open_price,
            initial_sl=initial_sl,
            current_sl=current_sl,
            tp=pos.tp,
            price=c_price,
            favorable_dist=favorable_dist,
            atr=atr,
            policy=policy,
            partial_already_taken=pos.ticket in self._partially_closed_tickets,
            be_already_locked=pos.ticket in self._be_locked,
            struct_level=struct_level,
        )
        actions = [f"{a}@{dec.new_sl:.{digits}f}" for a in dec.actions]
        return dec.new_sl, actions

    # ─── Manual Trade Handler ──────────────────────────────────────────────────

    def _handle_manual_sl(
        self,
        pos: PositionSnapshot,
        c_price: float,
        atr: float,
        current_sl: float,
    ):
        """Auto-set emergency SL for manual trades without one, or with dangerously wide SL."""
        action = None

        if pos.type == "BUY":
            if pos.sl == 0 or pos.sl < 0.0001:
                # No SL at all — set emergency at 2× ATR below entry
                new_sl = pos.open_price - (atr * EMERGENCY_SL_ATR_MULT)
                action = f"MANUAL_EMERGENCY_SL@{new_sl:.4f}"
                logger.warning(f"🛑 Manual trade #{pos.ticket} has NO SL! Auto-setting emergency SL @ {new_sl:.4f}")
                return new_sl, action
            sl_distance = pos.open_price - pos.sl
            if sl_distance > (atr * DANGEROUS_SL_ATR_MULT):
                # SL too wide — tighten to 2× ATR
                new_sl = pos.open_price - (atr * EMERGENCY_SL_ATR_MULT)
                if new_sl > current_sl:
                    action = f"MANUAL_TIGHTEN_SL@{new_sl:.4f}"
                    logger.warning(f"⚠️ Manual trade #{pos.ticket} SL too wide ({sl_distance:.2f} > {atr*DANGEROUS_SL_ATR_MULT:.2f}). Tightening to {new_sl:.4f}")
                    return new_sl, action

        elif pos.type == "SELL":
            if pos.sl == 0 or pos.sl < 0.0001:
                new_sl = pos.open_price + (atr * EMERGENCY_SL_ATR_MULT)
                action = f"MANUAL_EMERGENCY_SL@{new_sl:.4f}"
                logger.warning(f"🛑 Manual trade #{pos.ticket} has NO SL! Auto-setting emergency SL @ {new_sl:.4f}")
                return new_sl, action
            sl_distance = pos.sl - pos.open_price
            if sl_distance > (atr * DANGEROUS_SL_ATR_MULT):
                new_sl = pos.open_price + (atr * EMERGENCY_SL_ATR_MULT)
                if new_sl == 0 or new_sl < current_sl:
                    action = f"MANUAL_TIGHTEN_SL@{new_sl:.4f}"
                    logger.warning(f"⚠️ Manual trade #{pos.ticket} SL too wide. Tightening to {new_sl:.4f}")
                    return new_sl, action

        return current_sl, None

    # ─── Regime Invalidation ────────────────────────────────────────────────────

    def _check_regime_invalidation(
        self,
        pos: PositionSnapshot,
        ctx: MarketContext,
        regime: Optional[Any],
        c_price: float,
        atr: float,
        current_sl: float,
    ):
        """If the market regime flips strongly against the trade, tighten SL to lock 80% profit."""
        if regime is None:
            return current_sl, None

        try:
            regime_str = regime.primary_regime.value if hasattr(regime, "primary_regime") else str(regime)
            confidence = getattr(regime, "confidence", 0.0)

            if confidence < REGIME_INVALIDATION_CONFIDENCE:
                return current_sl, None

            is_invalidated = False
            if pos.type == "BUY" and ("BEAR" in regime_str or "REVERSAL" in regime_str):
                is_invalidated = True
            elif pos.type == "SELL" and ("BULL" in regime_str or "REVERSAL" in regime_str):
                is_invalidated = True

            if is_invalidated:
                profit_pips = (c_price - pos.open_price) if pos.type == "BUY" else (pos.open_price - c_price)
                if profit_pips > 0:
                    # Lock 80% of floating profit
                    if pos.type == "BUY":
                        candidate = pos.open_price + (profit_pips * 0.80)
                        if candidate > current_sl:
                            logger.info(
                                f"🔄 Regime invalidation for #{pos.ticket} ({regime_str} conf={confidence:.2f}) → "
                                f"80% profit lock @ {candidate:.4f}"
                            )
                            return candidate, f"REGIME_INVALIDATION@{candidate:.4f}"
                    else:
                        candidate = pos.open_price - (profit_pips * 0.80)
                        if current_sl == 0 or candidate < current_sl:
                            logger.info(
                                f"🔄 Regime invalidation for #{pos.ticket} ({regime_str} conf={confidence:.2f}) → "
                                f"80% profit lock @ {candidate:.4f}"
                            )
                            return candidate, f"REGIME_INVALIDATION@{candidate:.4f}"
                else:
                    # In loss — move to breakeven if possible
                    if pos.type == "BUY" and current_sl < pos.open_price - (atr * 0.1):
                        be = pos.open_price - (atr * 0.1)
                        if be > current_sl:
                            logger.info(f"🔄 Regime invalidation (losing) for #{pos.ticket} → BE @ {be:.4f}")
                            return be, f"REGIME_INVALIDATION_BE@{be:.4f}"
                    elif pos.type == "SELL" and (current_sl == 0 or current_sl > pos.open_price + (atr * 0.1)):
                        be = pos.open_price + (atr * 0.1)
                        if current_sl == 0 or be < current_sl:
                            logger.info(f"🔄 Regime invalidation (losing) for #{pos.ticket} → BE @ {be:.4f}")
                            return be, f"REGIME_INVALIDATION_BE@{be:.4f}"
        except Exception as e:
            logger.debug(f"Regime invalidation check failed for #{pos.ticket}: {e}")

        return current_sl, None

    # ─── VWAP Cross Awareness ───────────────────────────────────────────────────

    def _check_vwap_cross(
        self,
        pos: PositionSnapshot,
        ctx: MarketContext,
        c_price: float,
        atr: float,
        current_sl: float,
    ):
        """If price crosses VWAP against the trade direction, warn and optionally tighten."""
        vwap = getattr(ctx, "vwap", 0.0)
        if not isinstance(vwap, (int, float)) or isinstance(vwap, bool) or vwap <= 0:
            return current_sl, None

        if pos.type == "BUY" and c_price < vwap:
            # Price dropped below VWAP — bearish signal for a BUY
            profit_pips = c_price - pos.open_price
            if profit_pips > 0:
                # Still profitable — tighten to 50% profit lock
                candidate = pos.open_price + (profit_pips * 0.50)
                if candidate > current_sl:
                    logger.info(f"📊 VWAP cross (below) on BUY #{pos.ticket} → 50% profit lock @ {candidate:.4f}")
                    return candidate, f"VWAP_CROSS_50%@{candidate:.4f}"

        elif pos.type == "SELL" and c_price > vwap:
            # Price rose above VWAP — bullish signal against a SELL
            profit_pips = pos.open_price - c_price
            if profit_pips > 0:
                candidate = pos.open_price - (profit_pips * 0.50)
                if current_sl == 0 or candidate < current_sl:
                    logger.info(f"📊 VWAP cross (above) on SELL #{pos.ticket} → 50% profit lock @ {candidate:.4f}")
                    return candidate, f"VWAP_CROSS_50%@{candidate:.4f}"

        return current_sl, None

    # ─── Momentum Exhaustion ───────────────────────────────────────────────────

    def _check_momentum_exhaustion(
        self,
        pos: PositionSnapshot,
        ctx: MarketContext,
        c_price: float,
        atr: float,
        current_sl: float,
    ):
        """If trend_score flips sign against trade, apply 80% profit lock."""
        raw_score = getattr(ctx.momentum, "trend_score", 0.0) if hasattr(ctx, "momentum") else 0.0
        trend_score = float(raw_score) if isinstance(raw_score, (int, float)) and not isinstance(raw_score, bool) else 0.0
        profit_pips = (
            (c_price - pos.open_price) if pos.type == "BUY"
            else (pos.open_price - c_price)
        )
        if profit_pips <= 0:
            return current_sl, None

        if pos.type == "BUY" and trend_score < -20:
            candidate = pos.open_price + (profit_pips * 0.80)
            if candidate > current_sl:
                logger.info(f"⚡ Momentum exhaustion (score={trend_score}) BUY #{pos.ticket} → 80% lock @ {candidate:.4f}")
                return candidate, f"MOMENTUM_EXHAUST@{candidate:.4f}"

        elif pos.type == "SELL" and trend_score > 20:
            candidate = pos.open_price - (profit_pips * 0.80)
            if current_sl == 0 or candidate < current_sl:
                logger.info(f"⚡ Momentum exhaustion (score={trend_score}) SELL #{pos.ticket} → 80% lock @ {candidate:.4f}")
                return candidate, f"MOMENTUM_EXHAUST@{candidate:.4f}"

        return current_sl, None

    # ─── Emergency Breakeven ───────────────────────────────────────────────────

    def _emergency_breakeven(
        self,
        pos: PositionSnapshot,
        c_price: float,
        atr: float,
        current_sl: float,
    ) -> float:
        """Tighten SL to breakeven (entry ± small buffer) as fast as possible."""
        if pos.type == "BUY":
            be = pos.open_price + (atr * 0.05)
            return max(be, current_sl)
        else:
            be = pos.open_price - (atr * 0.05)
            return be if (current_sl == 0 or be < current_sl) else current_sl

    # ─── Helpers ───────────────────────────────────────────────────────────────

    def _is_manual_trade(self, pos: PositionSnapshot) -> bool:
        """Identifies trades NOT placed by HM Algo 2.0 (manual dashboard or MT5 terminal)."""
        is_wrong_magic = (pos.magic != JARVIS_MAGIC_NUMBER)
        has_manual_comment = any(
            tag in (pos.comment or "").upper()
            for tag in ("MANUAL", "DESK")
        )
        return is_wrong_magic or (pos.magic == 0) or has_manual_comment

    def _determine_position_style(self, pos: PositionSnapshot, ctx: Optional[MarketContext] = None) -> str:
        """
        Determine position style (SCALP, DAY_TRADING, SWING) from comment, tag, or duration.
        """
        comment = (pos.comment or "").upper()
        tag = str(getattr(pos, "tag", "") or "").upper()
        trade_style = str(getattr(pos, "trade_style", "") or "").upper()

        for text in (comment, tag, trade_style):
            if "SCALP" in text:
                return "SCALP"
            if "SWING" in text:
                return "SWING"
            if "DAY" in text or "INTRADAY" in text:
                return "DAY_TRADING"

        # Check duration
        dur_sec = self._get_position_duration_sec(pos)
        if dur_sec > 24.0 * 3600:
            return "SWING"
        elif dur_sec > 2.0 * 3600:
            return "DAY_TRADING"

        return "LEGACY"

    def _get_position_duration_sec(self, pos: PositionSnapshot) -> float:
        """Returns elapsed open duration of a position in seconds."""
        if not hasattr(pos, "open_time") or not pos.open_time:
            return 0.0
        try:
            if isinstance(pos.open_time, str):
                if "T" in pos.open_time:
                    open_dt = datetime.fromisoformat(pos.open_time.replace("Z", "+00:00"))
                else:
                    open_dt = datetime.strptime(pos.open_time, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            elif isinstance(pos.open_time, datetime):
                open_dt = pos.open_time if pos.open_time.tzinfo else pos.open_time.replace(tzinfo=timezone.utc)
            else:
                return 0.0
            now = datetime.now(timezone.utc)
            if open_dt.tzinfo is None:
                open_dt = open_dt.replace(tzinfo=timezone.utc)
            elapsed = (now - open_dt).total_seconds()
            if elapsed < 0:
                # A negative holding time means open_time is on a different clock to
                # `now`. The old `max(0.0, ...)` turned that into a silent zero, and
                # because the stagnation exits below are gated on `open_dur_sec > 0`
                # it DISABLED them entirely - which is how a 2-3h broker-server-time
                # mislabel stayed invisible. Clamp, but say so.
                logger.warning(
                    f"Negative holding time for position #{getattr(pos, 'ticket', '?')}: "
                    f"open_time {open_dt.isoformat()} is in the future. Treating as 0. "
                    "open_time is probably on the broker's clock rather than UTC."
                )
                return 0.0
            return elapsed
        except Exception:
            return 0.0

    def _check_adversarial_order_flow_shield(
        self,
        pos: PositionSnapshot,
        ctx: MarketContext,
        c_price: float,
        atr: float,
        digits: int
    ) -> Tuple[bool, Optional[Any]]:
        """
        Adversarial Order Flow Shield:
        If counter volume delta > 35% or absorption trap detected while holding open trade:
          - If in profit: Immediately ratchet SL to Bid/Ask +/- 0.15x ATR.
          - If underwater / flat: Close position to prevent full stop-out.
        """
        of_data = getattr(ctx, "order_flow", {})
        if not of_data or not isinstance(of_data, dict):
            return False, None

        delta_score = float(of_data.get("delta_score", 0.0))
        delta_ratio = float(of_data.get("delta_ratio", 0.0))
        absorption_trap = of_data.get("absorption_trap")

        is_adversarial = False
        if pos.type == "BUY":
            counter_delta = (delta_score < -35.0) or (delta_ratio < -0.35)
            counter_trap = absorption_trap in ("SELLER_ABSORPTION_TRAP", "ABSORPTION_TRAP", "BEARISH_ABSORPTION_TRAP")
            if counter_delta or counter_trap:
                is_adversarial = True
        elif pos.type == "SELL":
            counter_delta = (delta_score > 35.0) or (delta_ratio > 0.35)
            counter_trap = absorption_trap in ("BUYER_ABSORPTION_TRAP", "ABSORPTION_TRAP", "BULLISH_ABSORPTION_TRAP")
            if counter_delta or counter_trap:
                is_adversarial = True

        if not is_adversarial:
            return False, None

        is_in_profit = (pos.profit > 0.0) or ((c_price > pos.open_price) if pos.type == "BUY" else (c_price < pos.open_price))

        if is_in_profit:
            # In profit -> ratchet SL to Bid/Ask +/- 0.15x ATR
            if pos.type == "BUY":
                bid_price = getattr(ctx, "bid", c_price)
                cand_sl = round(bid_price - (0.15 * atr), digits)
                return True, cand_sl
            else:
                ask_price = getattr(ctx, "ask", c_price)
                cand_sl = round(ask_price + (0.15 * atr), digits)
                return True, cand_sl
        else:
            # Underwater or flat -> close position
            return True, "CLOSE"

    def _get_context(self, symbol: str) -> Optional[MarketContext]:
        """Returns cached context or fetches fresh context if TTL expired."""
        now = time.monotonic()
        with self._ctx_lock:
            cached = self._ctx_cache.get(symbol)
            if cached:
                ctx, fetched_at = cached
                if (now - fetched_at) < CONTEXT_CACHE_TTL_SEC:
                    return ctx

        # Fetch fresh context
        try:
            mtf_data = self.data_feed.fetch_multi_timeframe(symbol)
            # Feed the symbol's own typical spread so the spread-blowout guard
            # in _manage_single_position compares like-for-like. build_context
            # defaults current_spread_pips to a global 2.0; that constant
            # exceeded 2x the typical spread of EURUSD/GBPUSD/USDJPY/AUDUSD,
            # so the guard fired on every call and silently disabled trailing
            # stops, breakeven moves and partial closes on those majors.
            from jarvis.data.symbol_registry import resolve as _resolve_symbol
            try:
                spread_pips = _resolve_symbol(symbol).typical_spread_pips
            except Exception:
                spread_pips = 3.0
            ctx = self.context_engine.build_context(
                symbol, mtf_data, current_spread_pips=spread_pips
            )
            with self._ctx_lock:
                self._ctx_cache[symbol] = (ctx, now)
            return ctx
        except Exception as e:
            logger.debug(f"Context fetch failed for {symbol}: {e}")
            # Return last cached value even if stale rather than None
            with self._ctx_lock:
                cached = self._ctx_cache.get(symbol)
                if cached:
                    return cached[0]
            return None

    def _get_cached_regime(self, symbol: str) -> Optional[Any]:
        """Get the last known regime from state manager decisions."""
        try:
            # `latest_decisions`, not `_decisions`. The typo raised
            # AttributeError on every call and the bare `except: pass` below
            # swallowed it, so this branch had never once executed -- the
            # caller silently fell through to a fresh classification.
            decisions = self.state_manager.latest_decisions  # type: ignore
            dec = decisions.get(symbol)
            if dec and hasattr(dec, "regime"):
                return dec.regime
        except Exception as e:
            # Never swallow silently: that is what hid the typo for so long.
            logger.debug(f"_get_cached_regime({symbol}) fell back to None: {e}")
        return None

    def get_position_milestones(self, ticket: int) -> Dict[str, Any]:
        """Returns the 3-tier milestone prices and progression status for an active position."""
        return self._position_milestones.get(ticket, {"status": "OPEN", "tp1": None, "tp2": None, "tp3": None})

    def get_status(self) -> Dict[str, Any]:
        return {
            "running": self._running,
            "monitored_symbols": list(self._ctx_cache.keys()),
            "last_actions": dict(self._last_action),
            "milestones": dict(self._position_milestones),
        }
