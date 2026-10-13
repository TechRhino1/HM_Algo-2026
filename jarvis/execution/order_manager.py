"""
HM Algo 2.0 — Active Order & Position Manager.
Features:
- Canonical stop management (delegated to jarvis.execution.exit_policy)
- Execution Deterioration & Spread Expansion Protection

NOTE ON ARCHITECTURE
--------------------
This class used to carry its own MICRO 3-Stage breakeven/profit-lock table with
constants (STAGE1 1.0 / STAGE2 1.6 / STAGE3 2.0 ATR) that disagreed with both the
backtester and PositionMonitorEngine. Three divergent implementations meant live
behaviour could never reproduce a backtest. Stop arithmetic is now delegated to
the single shared policy, exactly like the monitor and the backtester.
"""
import time
import logging
import threading
from typing import Dict, List, Any, Optional
from jarvis.data.schemas import PositionSnapshot, MarketContext
from jarvis.execution.mt5_client import MT5Client
from jarvis.execution.exit_policy import ExitPolicy, evaluate_exit
from jarvis.risk.account_tier import is_micro_account

logger = logging.getLogger("JARVIS_OrderManager")

class OrderManager:
    MICRO_VOLUME_THRESHOLD = 0.03
    SPREAD_ALERT_THRESHOLD = 4.0

    def __init__(self, mt5_client: MT5Client):
        self.mt5_client = mt5_client
        # Per-ticket immutable 1R reference and policy (frozen at first sight).
        # Guarded by `_state_lock`: the sweep calls in from a 32-thread pool.
        self._state_lock = threading.RLock()
        self._initial_sl: Dict[int, float] = {}
        self._initial_volume: Dict[int, float] = {}
        self._exit_policy: Dict[int, ExitPolicy] = {}
        self._be_locked: set = set()

    def cleanup_stale_pending_orders(
        self,
        max_age_sec: int = 600,
        symbol: Optional[str] = None,
        context_map: Optional[Dict[str, MarketContext]] = None
    ) -> List[Dict[str, Any]]:
        """Scans pending orders and auto-cancels stale, structurally invalidated, or runaway orders."""
        pending = self.mt5_client.get_pending_orders(symbol=symbol)
        if not pending:
            return []
        
        now = time.time()
        cancelled = []
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

            # Dynamic TTL: Scalp orders expire in 5 mins, Day Trading in 10 mins, Swing in 20 mins
            allowed_age = max_age_sec
            if "SCALP" in comment:
                allowed_age = min(allowed_age, 300)
            elif "DAY" in comment:
                allowed_age = min(allowed_age, 600)
            elif "SWING" in comment:
                allowed_age = min(allowed_age, 1200)

            cancel_reason = None
            if age > allowed_age:
                cancel_reason = f"TTL expired ({age / 60.0:.1f}m > {allowed_age / 60.0:.1f}m)"

            # Structural Invalidation & Runaway Protection using live context
            ctx = context_map.get(sym) if context_map else None
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
                        cancel_reason = f"Runaway target reached ({c_price:.4f} >= TP {tp_price:.4f})"
                    elif (c_price - open_price) >= (1.5 * atr):
                        cancel_reason = f"Runaway excursion without fill (+{c_price - open_price:.4f} >= 1.5 ATR)"
                    # Invalidation 3: Bearish structure shift or severe adverse momentum
                    elif (getattr(st, "choch", False) and getattr(st, "bias", "") == "BEARISH") or trend_score <= -35.0:
                        cancel_reason = f"Adverse market structure shift (choch_bear={getattr(st, 'choch', False)}, trend_score={trend_score:.1f})"

                elif "SELL" in otype:
                    # Invalidation 1: Price has spiked above stop loss before order filled
                    if sl_price > 0 and c_price >= sl_price:
                        cancel_reason = f"Structural invalidation: price ({c_price:.4f}) surged above SL ({sl_price:.4f})"
                    # Invalidation 2: Price ran away to target or > 1.5 ATR without filling limit
                    elif tp_price > 0 and c_price <= tp_price:
                        cancel_reason = f"Runaway target reached ({c_price:.4f} <= TP {tp_price:.4f})"
                    elif (open_price - c_price) >= (1.5 * atr):
                        cancel_reason = f"Runaway excursion without fill (-{open_price - c_price:.4f} >= 1.5 ATR)"
                    # Invalidation 3: Bullish structure shift or severe adverse momentum
                    elif (getattr(st, "choch", False) and getattr(st, "bias", "") == "BULLISH") or trend_score >= 35.0:
                        cancel_reason = f"Adverse market structure shift (choch_bull={getattr(st, 'choch', False)}, trend_score={trend_score:.1f})"

            if cancel_reason:
                logger.info(f"⏳ Auto-cancelling pending order #{ticket} ({sym} {otype}): {cancel_reason}")
                res = self.mt5_client.cancel_pending_order(ticket)
                if res and res.get("status") == "CANCELLED":
                    cancelled.append(o)
        return cancelled

    def manage_position(
        self,
        position: PositionSnapshot,
        context: MarketContext,
        account_equity: Optional[float] = None
    ) -> Dict[str, Any]:
        """Dynamically manages trailing stop loss and profit protection.

        Delegates every stop-ratchet decision to the canonical exit policy so the
        backtester, the position monitor and this manager cannot drift apart.
        """
        c_price = context.current_price
        atr = context.volatility.atr if context.volatility.atr > 0 else (c_price * 0.005)
        st = getattr(context, "structure", None)
        vol = context.volatility

        # Resolve symbol spec defensively — the policy needs digits/pip_size and a
        # registry miss must not abort position management.
        spec = None
        try:
            from jarvis.data.symbol_registry import resolve as _res
            spec = _res(position.symbol)
        except Exception:
            spec = None

        # Freeze 1R and the policy on first sight of the ticket. Recomputing
        # against the *current* stop would make R drift as the stop ratchets.
        #
        # This runs on the sweep's worker pool (up to 32 threads), so the
        # check-then-set below must be atomic: two workers seeing a new ticket
        # at once would both compute a baseline and the second write would
        # silently move 1R, corrupting every R-multiple for that position.
        with self._state_lock:
            if position.ticket not in self._initial_sl:
                fallback_risk = atr * 1.5
                self._initial_sl[position.ticket] = (
                    position.sl if position.sl > 0
                    else (position.open_price - fallback_risk if position.type == "BUY"
                          else position.open_price + fallback_risk)
                )
            if position.ticket not in self._exit_policy:
                self._exit_policy[position.ticket] = ExitPolicy.for_symbol(position.symbol, spec)
            policy = self._exit_policy[position.ticket]

            initial_sl = self._initial_sl[position.ticket]
        risk_dist = abs(position.open_price - initial_sl)
        if risk_dist <= 0:
            logger.debug(
                f"#{position.ticket}: degenerate 1R (entry={position.open_price}, "
                f"initial_sl={initial_sl}) — leaving stop unchanged."
            )
            return {
                "ticket": position.ticket,
                "modified": False,
                "new_sl": round(position.sl, policy.digits),
                "new_tp": round(position.tp, policy.digits),
            }

        is_micro_pos = (
            position.volume <= self.MICRO_VOLUME_THRESHOLD
            or (account_equity is not None and is_micro_account(account_equity))
        )
        if is_micro_pos:
            logger.debug(f"#{position.ticket}: micro account/volume profile detected.")

        # Favourable excursion measured against the running high/low water mark
        # kept by the monitor when present; otherwise against the current price.
        if position.type == "BUY":
            favorable_dist = max(0.0, c_price - position.open_price)
        else:
            favorable_dist = max(0.0, position.open_price - c_price)

        struct_level = None
        if st is not None:
            if position.type == "BUY" and getattr(st, "higher_lows", False) and getattr(st, "demand_zone", (0, 0))[0] > 0:
                struct_level = float(st.demand_zone[0])
            elif position.type == "SELL" and getattr(st, "lower_highs", False) and getattr(st, "supply_zone", (0, 0))[1] > 0:
                struct_level = float(st.supply_zone[1])

        dec = evaluate_exit(
            side=position.type,
            entry=position.open_price,
            initial_sl=initial_sl,
            current_sl=position.sl,
            tp=position.tp,
            price=c_price,
            favorable_dist=favorable_dist,
            atr=atr,
            policy=policy,
            partial_already_taken=False,
            be_already_locked=position.ticket in self._be_locked,
            struct_level=struct_level,
        )

        old_sl = position.sl
        new_sl = dec.new_sl
        # Monotonic ratchet: never loosen a resting stop.
        if position.type == "BUY":
            if old_sl > 0 and new_sl < old_sl:
                new_sl = old_sl
        else:
            if old_sl > 0 and new_sl > old_sl:
                new_sl = old_sl
        if dec.be_locked:
            self._be_locked.add(position.ticket)

        modified = abs(new_sl - old_sl) > 0.0

        if dec.actions:
            logger.info(
                f"⚡ Position #{position.ticket} {position.type}: "
                f"SL {old_sl:.{policy.digits}f}→{new_sl:.{policy.digits}f} | "
                f"{', '.join(dec.actions)}"
            )

        # Spread blowout alert
        if vol.current_spread_pips > self.SPREAD_ALERT_THRESHOLD:
            logger.warning(f"⚠️ High Spread Detected on #{position.ticket} ({vol.current_spread_pips:.1f} pips).")

        # Take profit remains anchored at planned level (no moving goalposts)
        new_tp = position.tp

        return {
            "ticket": position.ticket,
            "modified": modified,
            "new_sl": round(new_sl, policy.digits),
            "new_tp": round(new_tp, policy.digits),
        }

    def forget_ticket(self, ticket: int) -> None:
        """Drop per-ticket state once a position closes (prevents unbounded growth)."""
        with self._state_lock:
            self._initial_sl.pop(ticket, None)
            self._initial_volume.pop(ticket, None)
            self._exit_policy.pop(ticket, None)
            self._be_locked.discard(ticket)
