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
EMERGENCY_SL_ATR_MULT     = 1.8    # Auto-SL for manual trades: 1.8× ATR from entry
DANGEROUS_SL_ATR_MULT     = 3.5    # SL wider than 3.5× ATR → tighten to 2.0× ATR

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
        self._last_modify_time: Dict[int, float] = {}

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
        # 1. Continuous Pending Order Staleness & Invalidation Management (every 2s)
        self._manage_pending_orders_tick()

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
        if self._is_manual_trade(pos):
            eff_mode = getattr(self, "manual_mode", MANUAL_MANAGEMENT_MODE)
            if reason == "PROXIMITY_TP_HARVEST" and eff_mode in ("TRAIL", "FULL"):
                pass  # Institutional profit banking near TP is allowed
            elif eff_mode != "FULL":
                logger.debug(f"🛡️ Discretionary exit '{reason}' suppressed for manual trade #{pos.ticket}")
                return False

        # Proximity TP harvest is an institutional target fulfillment; never suppress with grace period
        open_dur_sec = self._get_position_duration_sec(pos)
        if reason != "PROXIMITY_TP_HARVEST" and open_dur_sec < DISCRETIONARY_GRACE_PERIOD_SEC:
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

    def _manage_pending_orders_tick(self):
        """Scans resting pending orders every 2s for TTL expiration, runaway price, or structural invalidation."""
        if not hasattr(self.mt5_client, "get_pending_orders"):
            return
        try:
            pending = self.mt5_client.get_pending_orders()
            if not pending:
                return

            now = time.time()
            for o in pending:
                ticket = o.get("ticket")
                setup_time = o.get("time_setup", 0)
                age = (now - setup_time) if setup_time > 0 else 0
                sym = o.get("symbol", "UNKNOWN")
                otype = str(o.get("type", "")).upper()
                if otype in ("2", "0") or otype == "ORDER_TYPE_BUY_LIMIT":
                    otype = "BUY_LIMIT"
                elif otype in ("3", "1") or otype == "ORDER_TYPE_SELL_LIMIT":
                    otype = "SELL_LIMIT"
                open_price = float(o.get("price_open") or o.get("price", 0.0) or 0.0)
                sl_price = float(o.get("sl", 0.0) or 0.0)
                tp_price = float(o.get("tp", 0.0) or 0.0)
                comment = str(o.get("comment", "")).upper()

                # Dynamic TTL: Scalp orders expire in 5m (300s), Day Trading in 10m (600s), Swing in 20m (1200s)
                allowed_age = 600.0
                if "SCALP" in comment or "SCLP" in comment:
                    allowed_age = 300.0
                elif "DAY" in comment:
                    allowed_age = 600.0
                elif "SWING" in comment or "SWNG" in comment:
                    allowed_age = 1200.0

                # Defensively handle raw broker server timestamps (GMT+3 offset) if skew occurred
                if age < -3600:
                    age = max(0.0, age + 10800.0)
                elif age < 0:
                    age = 0.0

                cancel_reason = None
                if age > allowed_age:
                    cancel_reason = f"TTL expired ({age / 60.0:.1f}m > {allowed_age / 60.0:.1f}m)"

                ctx = self._get_context(sym)
                if ctx and not cancel_reason and open_price > 0:
                    c_price = ctx.current_price
                    atr = getattr(ctx.volatility, "atr", 0.0) or (c_price * 0.005)
                    st = getattr(ctx, "structure", None)
                    mom = getattr(ctx, "momentum", None)
                    trend_score = float(getattr(mom, "trend_score", 0.0) or 0.0) if mom else 0.0

                    if "BUY" in otype:
                        # Invalidation 1: Price has dumped below stop loss before order filled
                        if sl_price > 0 and c_price <= sl_price:
                            cancel_reason = f"Structural invalidation: price ({c_price:.4f}) dropped below SL ({sl_price:.4f})"
                        # Invalidation 2: Price ran away to target or > 1.5 ATR without filling limit
                        elif tp_price > 0 and c_price >= tp_price:
                            cancel_reason = f"Runaway target reached without fill ({c_price:.4f} >= TP {tp_price:.4f})"
                        elif (c_price - open_price) >= (1.5 * atr):
                            cancel_reason = f"Runaway excursion (+{c_price - open_price:.4f} >= 1.5 ATR)"
                        # Invalidation 3: Bearish structure shift or severe adverse momentum
                        elif (getattr(st, "choch", False) and getattr(st, "bias", "") == "BEARISH") or trend_score <= -30.0:
                            cancel_reason = f"Adverse market structure shift (choch_bear, trend_score={trend_score:.1f})"

                    elif "SELL" in otype:
                        # Invalidation 1: Price has spiked above stop loss before order filled
                        if sl_price > 0 and c_price >= sl_price:
                            cancel_reason = f"Structural invalidation: price ({c_price:.4f}) surged above SL ({sl_price:.4f})"
                        # Invalidation 2: Price ran away to target or > 1.5 ATR without filling limit
                        elif tp_price > 0 and c_price <= tp_price:
                            cancel_reason = f"Runaway target reached without fill ({c_price:.4f} <= TP {tp_price:.4f})"
                        elif (open_price - c_price) >= (1.5 * atr):
                            cancel_reason = f"Runaway excursion (-{open_price - c_price:.4f} >= 1.5 ATR)"
                        # Invalidation 3: Bullish structure shift or severe adverse momentum
                        elif (getattr(st, "choch", False) and getattr(st, "bias", "") == "BULLISH") or trend_score >= 30.0:
                            cancel_reason = f"Adverse market structure shift (choch_bull, trend_score={trend_score:.1f})"

                if cancel_reason:
                    logger.info(f"⏳ Auto-cancelling pending limit order #{ticket} ({sym} {otype}): {cancel_reason}")
                    self.mt5_client.cancel_pending_order(ticket)
        except Exception as e:
            logger.debug(f"_manage_pending_orders_tick error: {e}")

    def _ensure_valid_initial_sl_dist(
        self,
        pos: PositionSnapshot,
        assigned_sl: float = 0.0,
        fallback_atr: float = 0.0,
        pip_size: float = 0.0001,
    ) -> float:
        """
        Guarantees that _initial_risk_dist[ticket] is valid, positive, and non-degenerate.
        Prevents division-by-zero or infinite R-multiples if manual or algorithmic positions
        have pos.sl == 0 or sl placed too close to open price.
        """
        sl_cand = assigned_sl if assigned_sl > 0 else pos.sl
        dist = abs(pos.open_price - sl_cand) if sl_cand > 0 else 0.0
        min_floor = max(fallback_atr * 0.5, pip_size * 5, 1e-4) if fallback_atr > 0 else max(pip_size * 5, 1e-4)
        if dist < min_floor:
            dist = max(fallback_atr * 1.5, min_floor)
        self._initial_risk_dist[pos.ticket] = dist
        return dist

    # ─── Single-Position Logic ──────────────────────────────────────────────────

    def _manage_single_position(
        self,
        pos: PositionSnapshot,
        equity: float = 10000.0,
        balance: float = 10000.0,
        emergency_brake: bool = False,
    ):
        symbol  = pos.symbol
        from jarvis.data.symbol_registry import resolve as _res
        try:
            canon_sym = _res(symbol).canonical
        except Exception:
            canon_sym = symbol

        is_manual = self._is_manual_trade(pos)
        regime  = self._get_cached_regime(canon_sym) or self._get_cached_regime(symbol)

        # ── Fetch (or reuse cached) market context ──────────────────────────
        ctx = self._get_context(canon_sym) or self._get_context(symbol)
        if ctx is None:
            logger.debug(f"No market context for {symbol} ({canon_sym}) — skipping #{pos.ticket}")
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

        new_sl  = pos.sl
        new_tp  = pos.tp
        actions = []

        # ── Manual Trade Isolation & Trailing (Master Trader AI Engine) ──────
        effective_manual_mode = getattr(self, "manual_mode", MANUAL_MANAGEMENT_MODE)
        if is_manual:
            if effective_manual_mode == "PROTECT_ONLY" and pos.sl > 0:
                # In PROTECT_ONLY mode, do not modify manual trades that already have an SL
                return
            missing_sl = (pos.sl == 0 or pos.sl < 0.0001)
            missing_tp = (pos.tp == 0 or pos.tp < 0.0001)
            dangerously_wide_sl = False

            if not missing_sl:
                sl_dist = abs(pos.open_price - pos.sl)
                dangerously_wide_sl = (sl_dist > (atr * DANGEROUS_SL_ATR_MULT))

            if missing_sl or missing_tp or dangerously_wide_sl:
                ai_sl, ai_tp, ai_reason = self._calculate_master_trader_sl_tp(
                    pos=pos, ctx=ctx, c_price=c_price, atr=atr,
                    equity=equity, spec=spec, digits=digits, pip_size=pip_size
                )
                manual_modified = False
                target_sl = pos.sl
                target_tp = pos.tp

                if missing_sl or dangerously_wide_sl:
                    target_sl = ai_sl
                    new_sl = ai_sl
                    actions.append(f"AI_MANUAL_SL@{new_sl:.{digits}f}")
                    manual_modified = True

                if missing_tp:
                    target_tp = ai_tp
                    new_tp = ai_tp
                    actions.append(f"AI_MANUAL_TP@{new_tp:.{digits}f}")
                    manual_modified = True

                # Dispatch immediate protection if SL/TP changed
                if manual_modified and (target_sl != pos.sl or target_tp != pos.tp):
                    now_ts = time.time()
                    last_mod = self._last_modify_time.get(pos.ticket, 0.0)
                    if (now_ts - last_mod) >= 2.0 or missing_sl or missing_tp:
                        logger.info(
                            f"🎯 MASTER TRADER AI: Auto-placed SL/TP on Manual Trade #{pos.ticket} "
                            f"({pos.symbol} {pos.type}) -> SL: {pos.sl:.{digits}f}→{target_sl:.{digits}f} | "
                            f"TP: {pos.tp:.{digits}f}→{target_tp:.{digits}f} | Reason: {ai_reason}"
                        )
                        res = self.mt5_client.modify_position(ticket=pos.ticket, sl=target_sl, tp=target_tp)
                        if res and res.get("status") in ("MODIFIED", "SUCCESS", True):
                            pos.sl = target_sl
                            pos.tp = target_tp
                            self._last_action[pos.ticket] = f"{target_sl:.{digits}f}_{target_tp:.{digits}f}"
                            self._last_modify_time[pos.ticket] = now_ts
                            self._ensure_valid_initial_sl_dist(pos, target_sl, atr, pip_size)
                            self._initial_tp[pos.ticket] = target_tp

            if effective_manual_mode == "PROTECT_ONLY":
                # In PROTECT_ONLY mode, do not trail or apply discretionary algorithmic exits
                return

        if spread > (typical_spread * SPREAD_BLOWOUT_MULT):
            logger.warning(f"⚠️ Spread blowout {spread:.1f} pips on #{pos.ticket} — skipping modification.")
            return

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
                current_r = ((c_price - pos.open_price) if pos.type == "BUY" else (pos.open_price - c_price)) / max(risk_dist_init, 1e-6)
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
                            self._be_locked.add(pos.ticket)
                            # Lock breakeven buffer on remaining runner to secure risk-free trade
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
            if pos.ticket not in self._initial_risk_dist or self._initial_risk_dist[pos.ticket] <= 1e-6:
                self._ensure_valid_initial_sl_dist(pos, pos.sl, atr, pip_size)
            risk_dist = max(self._initial_risk_dist[pos.ticket], pip_size * 5)

            # Freeze the original stop, which defines 1R for the policy.
            if pos.ticket not in self._initial_sl or self._initial_sl[pos.ticket] == 0.0:
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
                    if pos.type == "BUY" and getattr(st, "higher_lows", False):
                        sw_low = float(getattr(st, "swing_low", 0.0) or 0.0)
                        dz_low = float(st.demand_zone[0]) if getattr(st, "demand_zone", None) else 0.0
                        candidates = [lvl for lvl in (sw_low, dz_low) if lvl > pos.open_price]
                        if candidates:
                            struct_level = max(candidates)
                    elif pos.type == "SELL" and getattr(st, "lower_highs", False):
                        sw_high = float(getattr(st, "swing_high", 0.0) or 0.0)
                        sz_high = float(st.supply_zone[1]) if getattr(st, "supply_zone", None) else 0.0
                        candidates = [lvl for lvl in (sw_high, sz_high) if 0 < lvl < pos.open_price]
                        if candidates:
                            struct_level = min(candidates)
            except Exception:
                struct_level = None

            regime_str = (regime.primary_regime.value if hasattr(regime, "primary_regime") else str(regime)) if regime else None
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
                regime=regime_str,
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

            # Compute current R-multiple for early defense and milestone evaluation
            current_r = ((c_price - pos.open_price) if pos.type == "BUY" else (pos.open_price - c_price)) / max(risk_dist, 1e-6)

            # ── 3.2 Near-TP Proximity Harvesting & Lock (Institutional TP Protection) ────
            # When price achieves >= 94% of the planned target move OR comes within proximity buffer
            # of TP (e.g. 1.5x spread or 0.10x ATR), prevent missed fills and sudden givebacks.
            if pos.tp > 0:
                tp_distance = abs(pos.tp - pos.open_price)
                if tp_distance > 1e-6:
                    dist_to_tp = (pos.tp - c_price) if pos.type == "BUY" else (c_price - pos.tp)
                    tp_prog = favorable_dist / tp_distance
                    spread_dist_val = spread * pip_size if pip_size > 0 else 0.0001
                    prox_thresh = max(pip_size * 2.5, spread_dist_val * 1.5, atr * 0.10)
                    if "XAU" in symbol or "GOLD" in symbol:
                        prox_thresh = max(prox_thresh, 0.40, atr * 0.12)

                    target_r = tp_distance / max(risk_dist, 1e-6)
                    # Active Proximity Harvest: if currently within micro proximity or >= 96% progress
                    # Must have captured meaningful profit (>= 1.5R) to avoid prematurely cutting winners for pennies
                    if ((dist_to_tp <= max(pip_size * 2.0, spread_dist_val * 1.2)) or (tp_prog >= 0.96 and dist_to_tp <= prox_thresh * 1.5)) and (current_r >= 1.50 or target_r <= 1.0):
                        logger.info(
                            f"🎯 PROXIMITY_TP_HARVEST: #{pos.ticket} ({pos.symbol} {pos.type}) "
                            f"within {dist_to_tp:.4f} of TP (progress={tp_prog*100:.1f}%, gain={current_r:.2f}R). "
                            f"Harvesting at market {c_price:.{digits}f} to bank profits."
                        )
                        if self._execute_exit(pos, reason="PROXIMITY_TP_HARVEST"):
                            return
                    elif (dist_to_tp <= prox_thresh) or (tp_prog >= 0.92):
                        # Tight Proximity Ratchet: Lock at least 82% of target move
                        raw_prox_sl = round(
                            pos.open_price + (tp_distance * 0.82) if pos.type == "BUY"
                            else pos.open_price - (tp_distance * 0.82),
                            digits,
                        )
                        _buf = policy.buffer_distance(risk_dist)
                        if pos.type == "BUY":
                            prox_locked_sl = min(raw_prox_sl, round(c_price - _buf, digits))
                            if prox_locked_sl > new_sl and prox_locked_sl > pos.open_price:
                                new_sl = prox_locked_sl
                                actions.append(f"PROX_LOCK_82%@{new_sl:.{digits}f}")
                        else:
                            prox_locked_sl = max(raw_prox_sl, round(c_price + _buf, digits))
                            if (new_sl == 0 or prox_locked_sl < new_sl) and prox_locked_sl < pos.open_price:
                                new_sl = prox_locked_sl
                                actions.append(f"PROX_LOCK_82%@{new_sl:.{digits}f}")

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

            # ── 3.8 Market Direction Reversal & Structural Invalidation Guard ────
            if not in_grace_period:
                rev_sl, rev_act, should_close, rev_reason = self._check_market_direction_reversal(
                    pos=pos,
                    ctx=ctx,
                    c_price=c_price,
                    atr=atr,
                    risk_dist=risk_dist,
                    current_r=current_r,
                    digits=digits,
                    current_sl=new_sl,
                    policy=policy,
                )
                if should_close:
                    logger.warning(
                        f"🚨 MARKET DIRECTION REVERSAL EXIT: Closing #{pos.ticket} {pos.symbol} {pos.type} "
                        f"at {c_price:.5f} | Profit: +{current_r:.2f}R | Reason: {rev_reason}"
                    )
                    if self._execute_exit(pos, reason=rev_reason):
                        return
                elif rev_sl != new_sl and rev_act:
                    new_sl = rev_sl
                    actions.append(rev_act)

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

                # (Micro runner TP extension removed: planned TP target must remain reachable)

                # Low ML Confidence (<=0.45) or Macro Divergence Check: Contract TP when in profit (>=1.0R) to bank gains before reversal
                elif (ml_prob <= 0.45 or (getattr(ctx.momentum, "divergence", "NONE") not in ("NONE", None, ""))) and current_r >= 1.0:
                    contracted_r = max(current_r + 0.40, 1.50)
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
                    # Require minimum +1.50R profit before allowing ML deterioration BE lock
                    if current_r >= 1.50:
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
                    # (Underwater SL halving removed: trades must be allowed structural breathing room)

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
                # Scalp: 45 min max hold without progress (R <= 0.0) -> Close position
                if style == "SCALP" and open_dur_sec >= 2700.0 and current_r <= 0.0:
                    self._execute_exit(pos, reason="SCALP_STAGNATION")
                    return

                # Day: 14 hours (full session) max hold without progress (R < 0.10R) -> Close position
                elif style in ("DAY_TRADING", "DAY", "INTRADAY") and open_dur_sec >= 50400.0 and current_r < 0.10:
                    self._execute_exit(pos, reason="DAY_STAGNATION")
                    return

                # Swing: 48 hours max hold in compression -> Close position
                elif style == "SWING" and open_dur_sec >= 172800.0:
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
            is_sl_tightened = (new_sl >= pos.sl - 1e-5) if pos.type == "BUY" else (new_sl <= pos.sl + 1e-5)
            if pos.sl > 0 and is_sl_tightened:
                pass  # Structural stop distance is tighter or identical to current SL; never block tightening
            elif current_projected_risk > init_risk * 1.05:
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

    # ─── Manual Trade Handler (Master Trader AI Engine) ─────────────────────────

    def _calculate_master_trader_sl_tp(
        self,
        pos: PositionSnapshot,
        ctx: Optional[MarketContext],
        c_price: float,
        atr: float,
        equity: float,
        spec: Any,
        digits: int,
        pip_size: float,
    ) -> Tuple[float, float, str]:
        """
        Institutional Master Trader Initial SL & TP Engine for Manual Trades.
        - Structural invalidation anchor (Swing Low for BUY, Swing High for SELL on M15/H1)
        - Anti-wick volatility buffer (max(atr * 1.2, pip_size * 10))
        - Max 2.0% account equity risk ceiling (clamps SL if dollar risk exceeds safe limit)
        - Institutional opposing liquidity target (BSL/SSL) or 1:2.5 Risk-Reward ratio
        - MT5 broker stop level & tick size quantization
        """
        is_buy = (pos.type == "BUY")
        entry = float(pos.open_price if pos.open_price > 0 else c_price)
        st = getattr(ctx, "structure", None) if ctx else None
        liq = getattr(ctx, "liquidity", None) if ctx else None

        # 1. Structural Anchor for SL
        anti_wick_buffer = max(atr * 1.2, pip_size * 10.0)
        struct_sl = 0.0

        if is_buy:
            cand_levels = []
            if st is not None:
                try:
                    sw_l = float(getattr(st, "swing_low", 0.0) or 0.0)
                    if 0 < sw_l < entry:
                        cand_levels.append(sw_l)
                except (TypeError, ValueError):
                    pass
                try:
                    dz = getattr(st, "demand_zone", None)
                    if dz and isinstance(dz, (list, tuple)) and len(dz) > 0:
                        dz_val = float(dz[0])
                        if 0 < dz_val < entry:
                            cand_levels.append(dz_val)
                except (TypeError, ValueError):
                    pass
                try:
                    obs = getattr(st, "order_blocks", None)
                    if isinstance(obs, list):
                        for ob in obs:
                            if isinstance(ob, dict) and ob.get("type") == "BULLISH_ORDER_BLOCK":
                                ob_low = float(ob.get("low", 0.0) or 0.0)
                                if 0 < ob_low < entry:
                                    cand_levels.append(ob_low)
                except (TypeError, ValueError):
                    pass
            if liq is not None:
                try:
                    ssl = float(getattr(liq, "sell_side_liquidity", 0.0) or 0.0)
                    if 0 < ssl < entry:
                        cand_levels.append(ssl)
                except (TypeError, ValueError):
                    pass

            levels_in_range = [l for l in cand_levels if (entry - l) <= 3.0 * atr]
            anchor = max(levels_in_range) if levels_in_range else (entry - (atr * 1.5))
            struct_sl = anchor - anti_wick_buffer
        else:
            cand_levels = []
            if st is not None:
                try:
                    sw_h = float(getattr(st, "swing_high", 0.0) or 0.0)
                    if sw_h > entry:
                        cand_levels.append(sw_h)
                except (TypeError, ValueError):
                    pass
                try:
                    sz = getattr(st, "supply_zone", None)
                    if sz and isinstance(sz, (list, tuple)) and len(sz) > 1:
                        sz_val = float(sz[1])
                        if sz_val > entry:
                            cand_levels.append(sz_val)
                except (TypeError, ValueError):
                    pass
                try:
                    obs = getattr(st, "order_blocks", None)
                    if isinstance(obs, list):
                        for ob in obs:
                            if isinstance(ob, dict) and ob.get("type") == "BEARISH_ORDER_BLOCK":
                                ob_high = float(ob.get("high", 0.0) or 0.0)
                                if ob_high > entry:
                                    cand_levels.append(ob_high)
                except (TypeError, ValueError):
                    pass
            if liq is not None:
                try:
                    bsl = float(getattr(liq, "buy_side_liquidity", 0.0) or 0.0)
                    if bsl > entry:
                        cand_levels.append(bsl)
                except (TypeError, ValueError):
                    pass

            levels_in_range = [l for l in cand_levels if (l - entry) <= 3.0 * atr]
            anchor = min(levels_in_range) if levels_in_range else (entry + (atr * 1.5))
            struct_sl = anchor + anti_wick_buffer

        # 2. Maximum Account Risk Cap (Max 2.0% Equity Loss)
        try:
            from jarvis.data.symbol_registry import get_dollar_risk_per_price_unit
            dollar_per_unit = get_dollar_risk_per_price_unit(pos.symbol)
        except Exception:
            dollar_per_unit = 100_000.0

        if equity < 250.0:
            max_risk_usd = max(equity * 0.05, 12.0)
        elif equity < 500.0:
            max_risk_usd = max(equity * 0.04, 18.0)
        elif equity < 1000.0:
            max_risk_usd = max(equity * 0.03, 25.0)
        else:
            max_risk_usd = max(equity * 0.02, 50.0)
        max_dist = max_risk_usd / max(dollar_per_unit * pos.volume, 1e-6)

        if is_buy:
            final_sl = max(struct_sl, entry - max_dist)
        else:
            final_sl = min(struct_sl, entry + max_dist)

        # 3. Take Profit Calculation (Opposing Liquidity or 1:2.5+ R:R)
        effective_sl = pos.sl if pos.sl > 0 else final_sl
        risk_dist = max(abs(entry - effective_sl), pip_size * 10.0)

        target_tp = 0.0
        if is_buy:
            min_tp = entry + (risk_dist * 2.5)
            cand_tp = []
            try:
                bsl = float(getattr(liq, "buy_side_liquidity", 0.0) or 0.0) if liq else 0.0
                if bsl >= min_tp:
                    cand_tp.append(bsl)
            except (TypeError, ValueError):
                pass
            try:
                sz = getattr(st, "supply_zone", None) if st else None
                if sz and isinstance(sz, (list, tuple)) and len(sz) > 1:
                    sz_top = float(sz[1])
                    if sz_top >= min_tp:
                        cand_tp.append(sz_top)
            except (TypeError, ValueError):
                pass
            target_tp = (min(cand_tp) - (atr * 0.2)) if cand_tp else min_tp
        else:
            min_tp = entry - (risk_dist * 2.5)
            cand_tp = []
            try:
                ssl = float(getattr(liq, "sell_side_liquidity", 0.0) or 0.0) if liq else 0.0
                if 0 < ssl <= min_tp:
                    cand_tp.append(ssl)
            except (TypeError, ValueError):
                pass
            try:
                dz = getattr(st, "demand_zone", None) if st else None
                if dz and isinstance(dz, (list, tuple)) and len(dz) > 0:
                    dz_bot = float(dz[0])
                    if 0 < dz_bot <= min_tp:
                        cand_tp.append(dz_bot)
            except (TypeError, ValueError):
                pass
            target_tp = (max(cand_tp) + (atr * 0.2)) if cand_tp else min_tp

        # 4. Broker Stops Level & Quantization
        sym_info = self.mt5_client.get_symbol_info(pos.symbol)
        if not isinstance(sym_info, dict):
            sym_info = {}
        try:
            point = float(sym_info.get("point") or (10 ** -digits))
        except (TypeError, ValueError):
            point = 10 ** -digits
        try:
            tick_sz = float(sym_info.get("trade_tick_size") or sym_info.get("point") or point)
        except (TypeError, ValueError):
            tick_sz = point
        try:
            stops_lvl = float(sym_info.get("trade_stops_level") or 0.0)
        except (TypeError, ValueError):
            stops_lvl = 0.0
        try:
            freeze_lvl = float(sym_info.get("freeze_level") or 0.0)
        except (TypeError, ValueError):
            freeze_lvl = 0.0
        min_dist = max(stops_lvl, freeze_lvl, 5.0) * point + (pip_size * 2.0)

        if is_buy:
            final_sl = min(final_sl, c_price - min_dist)
            target_tp = max(target_tp, c_price + min_dist)
        else:
            final_sl = max(final_sl, c_price + min_dist)
            target_tp = min(target_tp, c_price - min_dist)

        def _quantize(p: float) -> float:
            if tick_sz > 0:
                return round(round(p / tick_sz) * tick_sz, digits)
            return round(p, digits)

        final_sl = _quantize(final_sl)
        target_tp = _quantize(target_tp)
        rr = abs(target_tp - entry) / max(risk_dist, 1e-6)
        reason = f"MASTER_TRADER_AI(SL={final_sl}, TP={target_tp}, R:R={rr:.1f})"
        return final_sl, target_tp, reason

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
                risk_dist = self._initial_risk_dist.get(pos.ticket, atr * 1.5)
                # Ratchet on regime invalidation once secured >= 0.75R profit
                if profit_pips >= (risk_dist * 0.75):
                    if pos.type == "BUY":
                        candidate = pos.open_price + (profit_pips * 0.50)
                        if candidate > current_sl:
                            logger.info(
                                f"🔄 Regime invalidation for #{pos.ticket} ({regime_str} conf={confidence:.2f}) → "
                                f"50% profit lock @ {candidate:.4f}"
                            )
                            return candidate, f"REGIME_INVALIDATION@{candidate:.4f}"
                    else:
                        candidate = pos.open_price - (profit_pips * 0.50)
                        if current_sl == 0 or candidate < current_sl:
                            logger.info(
                                f"🔄 Regime invalidation for #{pos.ticket} ({regime_str} conf={confidence:.2f}) → "
                                f"50% profit lock @ {candidate:.4f}"
                            )
                            return candidate, f"REGIME_INVALIDATION@{candidate:.4f}"
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
        """If price crosses VWAP against the trade direction, warn and optionally tighten once in solid profit (>=1.5R)."""
        vwap = getattr(ctx, "vwap", 0.0)
        if not isinstance(vwap, (int, float)) or isinstance(vwap, bool) or vwap <= 0:
            return current_sl, None

        risk_dist = self._initial_risk_dist.get(pos.ticket, atr * 1.5)
        min_profit_dist = max(risk_dist * 0.80, atr * 0.60)

        if pos.type == "BUY" and c_price < vwap:
            profit_pips = c_price - pos.open_price
            if profit_pips >= min_profit_dist:
                candidate = pos.open_price + (profit_pips * 0.50)
                if candidate > current_sl:
                    logger.info(f"📊 VWAP cross (below) on BUY #{pos.ticket} → 50% profit lock @ {candidate:.4f}")
                    return candidate, f"VWAP_CROSS_50%@{candidate:.4f}"

        elif pos.type == "SELL" and c_price > vwap:
            profit_pips = pos.open_price - c_price
            if profit_pips >= min_profit_dist:
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
        """If trend_score flips sign against trade once in solid profit (>=1.5R), lock 50% profit safely."""
        raw_score = getattr(ctx.momentum, "trend_score", 0.0) if hasattr(ctx, "momentum") else 0.0
        trend_score = float(raw_score) if isinstance(raw_score, (int, float)) and not isinstance(raw_score, bool) else 0.0
        profit_pips = (
            (c_price - pos.open_price) if pos.type == "BUY"
            else (pos.open_price - c_price)
        )
        risk_dist = self._initial_risk_dist.get(pos.ticket, atr * 1.5)
        min_profit_dist = max(risk_dist * 0.65, atr * 0.50)

        if profit_pips < min_profit_dist:
            return current_sl, None

        if pos.type == "BUY" and trend_score < -20:
            candidate = pos.open_price + (profit_pips * 0.50)
            if candidate > current_sl:
                logger.info(f"⚡ Momentum exhaustion (score={trend_score}) BUY #{pos.ticket} → 50% lock @ {candidate:.4f}")
                return candidate, f"MOMENTUM_EXHAUST@{candidate:.4f}"

        elif pos.type == "SELL" and trend_score > 20:
            candidate = pos.open_price - (profit_pips * 0.50)
            if current_sl == 0 or candidate < current_sl:
                logger.info(f"⚡ Momentum exhaustion (score={trend_score}) SELL #{pos.ticket} → 50% lock @ {candidate:.4f}")
                return candidate, f"MOMENTUM_EXHAUST@{candidate:.4f}"

        return current_sl, None

    # ─── Market Direction Reversal & Structural Invalidation Guard ───────────

    def _check_market_direction_reversal(
        self,
        pos: PositionSnapshot,
        ctx: Optional[MarketContext],
        c_price: float,
        atr: float,
        risk_dist: float,
        current_r: float,
        digits: int,
        current_sl: float,
        policy: Any,
    ) -> Tuple[float, Optional[str], bool, Optional[str]]:
        """
        Institutional Profit Booking & Direction Reversal Defense:
        Detects Change of Character (CHoCH), Break of Structure (BOS),
        candlestick reversal, macro divergence, liquidity sweeps, and momentum shifts against an active trade.

        Guarantees:
        - Phase 0 (-0.75R <= current_r < 0.20R): Early Structural Thesis Invalidation.
          If an open trade suffers confirmed opposite CHoCH/BOS + sweep rejection/adverse momentum,
          executes early discretionary market exit to CUT LOSS EARLY (saving 60-75% stop loss capital).
        - Phase 1 (+0.50R <= current_r < 1.0R): On opposite CHoCH / structural shift or
          adverse momentum, immediately move SL to breakeven + buffer (locks zero-loss).
        - Phase 2 (+1.0R <= current_r < 1.80R): On multi-factor reversal (e.g. CHoCH +
          divergence/momentum), executes discretionary market exit to BANK solid profit.
          On single reversal factor, ratchets SL to lock >= +0.50R profit.
        - Phase 3 (current_r >= 1.80R): On opposite structural shift, executes discretionary
          market exit or locks >= 70% of peak floating profit.

        Returns: (candidate_sl, action_str, should_close, close_reason)
        """
        if ctx is None:
            return current_sl, None, False, None

        try:
            is_buy = (pos.type == "BUY")
            reversal_signals: List[str] = []

            # 1. Structure: Change of Character (CHoCH) or Break of Structure (BOS)
            struct = getattr(ctx, "structure", None)
            if struct is not None:
                choch = getattr(struct, "choch", False)
                choch_type = str(getattr(struct, "choch_type", "NONE") or "").upper()
                bos = getattr(struct, "bos", False)
                bos_type = str(getattr(struct, "bos_type", "NONE") or "").upper()
                struct_bias = str(getattr(struct, "bias", "NEUTRAL") or "").upper()

                if is_buy:
                    if choch and ("BEAR" in choch_type):
                        reversal_signals.append(f"BEARISH_CHOCH({choch_type})")
                    if bos and ("BEAR" in bos_type):
                        reversal_signals.append(f"BEARISH_BOS({bos_type})")
                    if struct_bias == "BEARISH":
                        reversal_signals.append("STRUCT_BIAS_BEARISH")
                else:
                    if choch and ("BULL" in choch_type):
                        reversal_signals.append(f"BULLISH_CHOCH({choch_type})")
                    if bos and ("BULL" in bos_type):
                        reversal_signals.append(f"BULLISH_BOS({bos_type})")
                    if struct_bias == "BULLISH":
                        reversal_signals.append("STRUCT_BIAS_BULLISH")

            # 2. Momentum: Strong Trend Score Inversion & Divergence
            mom = getattr(ctx, "momentum", None)
            if mom is not None:
                trend_score = float(getattr(mom, "trend_score", 0.0) or 0.0)
                divergence = str(getattr(mom, "divergence", "NONE") or "").upper()

                if is_buy:
                    if trend_score <= -25.0:
                        reversal_signals.append(f"ADVERSE_MOMENTUM({trend_score:.0f})")
                    if "BEAR" in divergence:
                        reversal_signals.append(f"DIVERGENCE({divergence})")
                else:
                    if trend_score >= 25.0:
                        reversal_signals.append(f"ADVERSE_MOMENTUM(+{trend_score:.0f})")
                    if "BULL" in divergence:
                        reversal_signals.append(f"DIVERGENCE({divergence})")

            # 3. Candlestick Price Action Reversal
            candle = getattr(ctx, "candlestick_pattern", {})
            if isinstance(candle, dict) and candle:
                c_dir = str(candle.get("direction", "") or candle.get("bias", "")).upper()
                c_pat = candle.get("pattern_name", candle.get("pattern", "UNKNOWN"))
                if is_buy and "BEAR" in c_dir:
                    reversal_signals.append(f"CANDLE_REVERSAL({c_pat})")
                elif (not is_buy) and "BULL" in c_dir:
                    reversal_signals.append(f"CANDLE_REVERSAL({c_pat})")

            # 4. Multi-Timeframe Alignment Flip
            mtf = getattr(ctx, "mtf_alignment", {})
            if isinstance(mtf, dict) and mtf:
                m5_bias = str(mtf.get("M5", "")).upper()
                m15_bias = str(mtf.get("M15", "")).upper()
                if is_buy and ("BEAR" in m5_bias and "BEAR" in m15_bias):
                    reversal_signals.append("MTF_M5_M15_BEARISH")
                elif (not is_buy) and ("BULL" in m5_bias and "BULL" in m15_bias):
                    reversal_signals.append("MTF_M5_M15_BULLISH")

            # 5. Liquidity Sweep Rejection (SMC/ICT §1)
            liq = getattr(ctx, "liquidity", None)
            if liq is not None and getattr(liq, "sweep_detected", False):
                sweep_type = str(getattr(liq, "sweep_type", "") or "").upper()
                if is_buy and "BEARISH_SWEEP" in sweep_type:
                    reversal_signals.append("BSL_SWEEP_REJECTION")
                elif (not is_buy) and "BULLISH_SWEEP" in sweep_type:
                    reversal_signals.append("SSL_SWEEP_REJECTION")

            # 6. Order Flow Counter-Delta & Absorption (Institutional Tape)
            of_data = getattr(ctx, "order_flow", {})
            if isinstance(of_data, dict) and of_data:
                delta_score = float(of_data.get("delta_score", 0.0) or 0.0)
                absorption_trap = str(of_data.get("absorption_trap", "") or "").upper()
                if is_buy and (delta_score <= -45.0 or "SELLER_ABSORPTION" in absorption_trap or "BEARISH_ABSORPTION" in absorption_trap):
                    reversal_signals.append(f"ORDER_FLOW_ABSORPTION({delta_score:.0f})")
                elif (not is_buy) and (delta_score >= 45.0 or "BUYER_ABSORPTION" in absorption_trap or "BULLISH_ABSORPTION" in absorption_trap):
                    reversal_signals.append(f"ORDER_FLOW_ABSORPTION(+{delta_score:.0f})")

            if not reversal_signals:
                return current_sl, None, False, None

            sig_summary = ", ".join(reversal_signals)
            has_structural_break = any("CHOCH" in s or "BOS" in s for s in reversal_signals)
            signal_count = len(reversal_signals)

            # ── PHASE 0: Early Structural Thesis Invalidation (current_r < 0.20R) ──
            if current_r < 0.20:
                # Decisive structural breakdown of the trade's premise:
                # Requires confirmed CHoCH/BOS against the trade, plus at least one of:
                # (1) Liquidity sweep in opposite direction (e.g. SSL swept & reclaimed for a short)
                # (2) Significant adverse momentum / trend score inversion (|score| >= 25)
                # (3) Multi-timeframe reversal (M5 & M15 flipped against trade)
                # (4) Strong candlestick reversal or order flow absorption trap
                if has_structural_break and signal_count >= 2:
                    return current_sl, None, True, f"STRUCTURAL_THESIS_INVALIDATION({current_r:.2f}R | {sig_summary})"
                return current_sl, None, False, None

            # Normal breathing room zone (+0.20R <= current_r < +0.50R)
            if current_r < 0.50:
                return current_sl, None, False, None

            # ── PHASE 3: High Profit Runner (current_r >= 1.80R) ─────────────────
            if current_r >= 1.80:
                if has_structural_break or signal_count >= 2:
                    return current_sl, None, True, f"RUNNER_REVERSAL_EXIT(+{current_r:.2f}R | {sig_summary})"
                profit_dist = (c_price - pos.open_price) if is_buy else (pos.open_price - c_price)
                lock_dist = profit_dist * 0.70
                candidate = round(pos.open_price + lock_dist if is_buy else pos.open_price - lock_dist, digits)
                if (is_buy and candidate > current_sl) or ((not is_buy) and (current_sl == 0 or candidate < current_sl)):
                    return candidate, f"RUNNER_REVERSAL_LOCK_70%@{candidate:.4f}", False, None

            # ── PHASE 2: Solid Profit (1.0R <= current_r < 1.80R) ────────────────
            elif current_r >= 1.0:
                if (has_structural_break and signal_count >= 2) or signal_count >= 3:
                    return current_sl, None, True, f"DIRECTION_REVERSAL_BANK(+{current_r:.2f}R | {sig_summary})"

                profit_dist = (c_price - pos.open_price) if is_buy else (pos.open_price - c_price)
                lock_dist = max(risk_dist * 0.50, profit_dist * 0.50)
                candidate = round(pos.open_price + lock_dist if is_buy else pos.open_price - lock_dist, digits)
                if (is_buy and candidate > current_sl) or ((not is_buy) and (current_sl == 0 or candidate < current_sl)):
                    return candidate, f"REVERSAL_LOCK_50%@{candidate:.4f}", False, None

            # ── PHASE 1: Early Profit (+0.50R <= current_r < 1.0R) ───────────────
            else:
                buf = policy.buffer_distance(risk_dist) if hasattr(policy, "buffer_distance") else min(atr * 0.10, risk_dist * 0.10)
                be_sl = round(pos.open_price + buf if is_buy else pos.open_price - buf, digits)
                if (is_buy and be_sl > current_sl) or ((not is_buy) and (current_sl == 0 or be_sl < current_sl)):
                    return be_sl, f"REVERSAL_DEFENSE_BE@{be_sl:.4f}({sig_summary})", False, None

        except Exception as e:
            logger.debug(f"_check_market_direction_reversal error for #{pos.ticket}: {e}")

        return current_sl, None, False, None

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
            # If trade is underwater, setting SL to entry+buffer places SL above current price!
            if c_price <= pos.open_price:
                return current_sl
            be = pos.open_price + (atr * 0.05)
            return min(c_price - (atr * 0.1), max(be, current_sl))
        else:
            # If trade is underwater, setting SL to entry-buffer places SL below current price!
            if c_price >= pos.open_price:
                return current_sl
            be = pos.open_price - (atr * 0.05)
            target = min(be, current_sl) if current_sl > 0 else be
            return max(c_price + (atr * 0.1), target)

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
        Determine position style (SCALP, DAY_TRADING, SWING) from comment, tag, duration, or structural geometry.
        """
        comment = (pos.comment or "").upper()
        tag = str(getattr(pos, "tag", "") or "").upper()
        trade_style = str(getattr(pos, "trade_style", "") or "").upper()

        for text in (comment, tag, trade_style):
            if "SCALP" in text or "SCLP" in text:
                return "SCALP"
            if "SWING" in text or "SWNG" in text:
                return "SWING"
            if "DAY" in text or "INTRADAY" in text:
                return "DAY_TRADING"

        # Check structural risk and target geometry (Swing orders have wide stops and targets)
        if pos.sl > 0 and pos.open_price > 0:
            risk_dist = abs(pos.open_price - pos.sl)
            if pos.tp > 0:
                tp_dist = abs(pos.tp - pos.open_price)
                if (tp_dist / max(risk_dist, 1e-6)) >= 2.5:
                    return "SWING"
            if ctx is not None:
                atr = getattr(ctx.volatility, "atr", 0.0) or 0.0
                if atr > 0 and risk_dist >= (2.5 * atr):
                    return "SWING"

        # Check duration
        dur_sec = self._get_position_duration_sec(pos)
        if dur_sec > 24.0 * 3600:
            return "SWING"
        elif dur_sec > 4.0 * 3600:
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

        # Require decisive confirmation: BOTH extreme counter delta AND confirmed trap
        is_adversarial = False
        if pos.type == "BUY":
            counter_delta = (delta_score < -60.0) or (delta_ratio < -0.60)
            counter_trap = absorption_trap in ("SELLER_ABSORPTION_TRAP", "BEARISH_ABSORPTION_TRAP")
            if counter_delta and counter_trap:
                is_adversarial = True
        elif pos.type == "SELL":
            counter_delta = (delta_score > 60.0) or (delta_ratio > 0.60)
            counter_trap = absorption_trap in ("BUYER_ABSORPTION_TRAP", "BULLISH_ABSORPTION_TRAP")
            if counter_delta and counter_trap:
                is_adversarial = True

        if not is_adversarial:
            return False, None

        profit_pips = (c_price - pos.open_price) if pos.type == "BUY" else (pos.open_price - c_price)
        # Only tighten to a sensible trailing distance (0.75*ATR) if already significantly in profit (>= +1.0R equivalent)
        # Never close underwater trades at market arbitrarily; the structural stop loss must govern.
        if profit_pips > (atr * 0.8):
            if pos.type == "BUY":
                bid_price = getattr(ctx, "bid", c_price)
                cand_sl = round(bid_price - (0.75 * atr), digits)
                return True, cand_sl
            else:
                ask_price = getattr(ctx, "ask", c_price)
                cand_sl = round(ask_price + (0.75 * atr), digits)
                return True, cand_sl
        else:
            return False, None

    def _get_context(self, symbol: str) -> Optional[MarketContext]:
        """Returns cached context or fetches fresh context if TTL expired."""
        now = time.monotonic()
        from jarvis.data.symbol_registry import resolve as _res
        try:
            canon = _res(symbol).canonical
        except Exception:
            canon = symbol

        with self._ctx_lock:
            cached = self._ctx_cache.get(canon) or self._ctx_cache.get(symbol)
            if cached:
                ctx, fetched_at = cached
                if (now - fetched_at) < CONTEXT_CACHE_TTL_SEC:
                    return ctx

        # Fetch fresh context
        try:
            mtf_data = self.data_feed.fetch_multi_timeframe(canon)
            try:
                spread_pips = _res(canon).typical_spread_pips
            except Exception:
                spread_pips = 3.0
            ctx = self.context_engine.build_context(
                canon, mtf_data, current_spread_pips=spread_pips
            )
            with self._ctx_lock:
                self._ctx_cache[canon] = (ctx, now)
                self._ctx_cache[symbol] = (ctx, now)
            return ctx
        except Exception as e:
            logger.debug(f"Context fetch failed for {symbol} ({canon}): {e}")
            with self._ctx_lock:
                cached = self._ctx_cache.get(canon) or self._ctx_cache.get(symbol)
                if cached:
                    return cached[0]
            return None

    def _get_cached_regime(self, symbol: str) -> Optional[Any]:
        """Get the last known regime from state manager decisions."""
        try:
            decisions = self.state_manager.latest_decisions  # type: ignore
            dec = decisions.get(symbol)
            if not dec:
                from jarvis.data.symbol_registry import resolve as _res
                try:
                    canon = _res(symbol).canonical
                    dec = decisions.get(canon)
                except Exception:
                    pass
            if dec and hasattr(dec, "regime"):
                return dec.regime
        except Exception as e:
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
