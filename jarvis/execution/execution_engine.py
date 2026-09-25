"""
HM Algo 2.0 — Master Execution Engine.
Orchestrates order dispatch, mode verification (LIVE/PAPER/DEMO), and execution logging.
"""
import logging
import uuid
from typing import Dict, Any, Optional
from jarvis.data.schemas import DecisionObject
from jarvis.execution.mt5_client import MT5Client
from jarvis.application.state_manager import StateManager, GLOBAL_STATE
from jarvis.observability import log_event
from jarvis.observability.instruments import ORDERS_SUBMITTED, ORDER_LATENCY

logger = logging.getLogger("JARVIS_ExecutionEngine")

class ExecutionEngine:
    def __init__(self, mt5_client: MT5Client, state_manager: StateManager = GLOBAL_STATE):
        self.mt5_client = mt5_client
        self.state_manager = state_manager

    def _classify_broker_response(self, res: Optional[Dict[str, Any]]) -> str:
        """Classify broker response according to Spec v2.2 taxonomy:
        - FILLED: Order executed fully with ticket and fill price.
        - ACCEPTED: Order placed as pending (e.g. BUY_LIMIT/SELL_LIMIT) or resting in broker queue.
        - PARTIAL: Order partially executed.
        - REJECTED: Broker explicitly rejected order (e.g. invalid volume, price, stops, off quotes).
        - FAILED: Internal failure before reaching broker or explicit broker failure code.
        - UNKNOWN: Timeout, disconnect, or ambiguous broker response requiring state reconciliation.
        - POSITION_CONFIRMED: Position confirmed active on terminal.
        """
        if not res:
            return "UNKNOWN"
        raw_status = str(res.get("status", "")).upper()
        if raw_status in ("FILLED", "POSITION_CONFIRMED", "PARTIAL"):
            return raw_status
        if raw_status in ("PLACED", "ACCEPTED", "PENDING"):
            return "ACCEPTED"
        if raw_status in ("REJECTED", "BLOCKED"):
            return "REJECTED"
        if raw_status in ("FAILED", "SAFETY_BLOCK", "TIMEOUT_FALLBACK_BLOCKED"):
            return "FAILED"
        if raw_status in ("UNKNOWN", "TIMEOUT", "DISCONNECTED"):
            return "UNKNOWN"
        retcode = res.get("retcode")
        if retcode is not None:
            if retcode in (10009,):
                return "FILLED" if res.get("ticket") else "ACCEPTED"
            if retcode in (10008,):
                return "ACCEPTED"
            if retcode in (10010,):
                return "PARTIAL"
            if retcode in (10004, 10006, 10013, 10014, 10015, 10016, 10017, 10018, 10019, 10020, 10021):
                return "REJECTED"
            if retcode in (10022, 10024):
                return "UNKNOWN"
        return "UNKNOWN"

    def _price_origin(self, res: Dict[str, Any]) -> str:
        """Where the fill price came from — `broker`, `paper` or `synthetic` (D1).

        Paper and live fills land in the same table and were indistinguishable,
        so every realised-P&L number read from it mixed simulated money with
        real. A fallback price is worse than paper: it means the quote was
        missing and a stand-in was used, and the row must not be counted as a
        market result at all.
        """
        if res.get("is_fallback"):
            return "synthetic"
        mode = str(getattr(self.mt5_client, "mode", "") or "").lower()
        if mode == "paper":
            return "paper"
        if mode in ("live", "demo"):
            # A live client that could not reach the terminal falls back too;
            # `not MT5_AVAILABLE` in send_market_order takes the same branch as
            # paper, so ask whether the client actually connected.
            return "broker" if getattr(self.mt5_client, "is_connected", True) else "synthetic"
        return "unknown"

    def _execution_mode(self, res: Dict[str, Any] = None) -> str:
        """The execution mode this fill was produced under (D1, completed).

        Deliberately NOT derived from :meth:`_price_origin`. A live client that
        has lost the terminal takes the same branch as paper inside
        `send_market_order`, so its price is a stand-in — but the order was
        still routed to a real account, and the row has to say so. Conflating
        "the price is a stand-in" with "this was simulated money" is what left
        paper and live fills indistinguishable in the first place.
        """
        mode = str(getattr(self.mt5_client, "mode", "") or "").lower()
        return mode if mode in ("live", "paper", "demo") else "unknown"

    def execute_decision(self, decision: DecisionObject, lots: float) -> Dict[str, Any]:
        """Dispatches authorized decision to MT5 or Paper Simulator."""
        if not decision.execution_authorized or lots <= 0:
            logger.warning(f"Execution rejected for {decision.symbol}: Not authorized or invalid lot size ({lots}).")
            ORDERS_SUBMITTED.inc(mode=self._execution_mode(), status="BLOCKED")
            return {"status": "BLOCKED", "reason": "Execution unauthorized"}

        if self.state_manager.is_safe_mode:
            logger.warning(f"Execution blocked for {decision.symbol}: SAFE MODE is ACTIVE.")
            ORDERS_SUBMITTED.inc(mode=self._execution_mode(), status="BLOCKED")
            return {"status": "BLOCKED", "reason": "Safe mode active"}

        setup_id_val = getattr(decision, "setup_id", None)
        cand_id_val = getattr(decision, "candidate_id", None)
        if not isinstance(setup_id_val, str) or not setup_id_val:
            if isinstance(cand_id_val, str) and cand_id_val:
                setup_id_val = cand_id_val
            else:
                setup_id_val = str(uuid.uuid4())
        decision.setup_id = setup_id_val
        setup_id = setup_id_val

        mode = self.state_manager.execution_mode
        comment = f"HMA2_{decision.strategy[:6]}_{mode.value}"

        # ── Pre-fill Broker-Safe Sizing and SL Distance Calculation (Spec v2.1 Refinement 1) ──
        sym_spec = {}
        if hasattr(self.mt5_client, "get_symbol_trading_spec"):
            sym_spec = self.mt5_client.get_symbol_trading_spec(decision.symbol)
        
        point = float(sym_spec.get("point", 0.0001) or 0.0001)
        digits = int(sym_spec.get("digits", 5) or 5)
        stops_level = int(sym_spec.get("trade_stops_level", 0) or 0)
        spread_points = int(sym_spec.get("spread", 0) or 0)
        spread_dist = spread_points * point
        stops_level_dist = stops_level * point
        min_broker_stop_dist = max(stops_level_dist, spread_dist * 2.0, 10.0 * point)

        planned_sl_dist = abs(decision.entry_price - decision.stop_loss)
        executable_sl_dist = max(planned_sl_dist, min_broker_stop_dist)

        # If planned stop is too close to entry for the broker, adjust SL before dispatch
        if executable_sl_dist > planned_sl_dist + 1e-9:
            logger.info(
                f"🔧 ADJUSTING SL TO BROKER MINIMUM for {decision.symbol}: Planned {planned_sl_dist:.5f} -> Executable {executable_sl_dist:.5f} "
                f"(stops_level={stops_level}, spread={spread_points} pts)"
            )
            if decision.bias == "BUY":
                decision.stop_loss = round(decision.entry_price - executable_sl_dist, digits)
            else:
                decision.stop_loss = round(decision.entry_price + executable_sl_dist, digits)
            decision.sl_distance = executable_sl_dist

            # Adjust lot size down if widening the SL would breach the risk budget
            tick_val = float(sym_spec.get("trade_tick_value", 1.0) or 1.0)
            tick_sz = float(sym_spec.get("trade_tick_size", point) or point)
            dollar_risk_per_price_unit = tick_val / max(tick_sz, 1e-9)
            
            account_equity = self.state_manager.account.equity if self.state_manager.account else 10000.0
            from jarvis.config.settings import SETTINGS
            max_risk_pct = SETTINGS.risk.max_risk_per_trade_pct
            target_risk_usd = account_equity * (max_risk_pct / 100.0)
            
            safe_lots = target_risk_usd / (executable_sl_dist * dollar_risk_per_price_unit)
            vol_step = float(sym_spec.get("volume_step", 0.01) or 0.01)
            vol_min = float(sym_spec.get("volume_min", 0.01) or 0.01)
            safe_lots = max(vol_min, round(safe_lots / vol_step) * vol_step)
            safe_lots = round(safe_lots, 2)
            if safe_lots < lots:
                logger.info(
                    f"🛡️ SIZING DOWN VOLUME to protect risk ceiling: {lots} lots -> {safe_lots} lots "
                    f"(target risk ${target_risk_usd:.2f} on {executable_sl_dist:.5f} stop distance)"
                )
                lots = safe_lots

        logger.info(f"DISPATCHING ORDER [{mode.value}]: {decision.bias} {lots} {decision.symbol} @ Entry={decision.entry_price} SL={decision.stop_loss} TP={decision.take_profit}")

        order_kind = getattr(decision, "order_type", "MARKET").upper()
        use_limit = "LIMIT" in order_kind or "PENDING" in order_kind

        _order_timer = ORDER_LATENCY.time(route="pending" if use_limit else "market")

        if use_limit:
            # Task 2b: Direction-vs-price sanity check for resting limit orders
            ctx = getattr(decision, "context", None)
            cur_price = getattr(ctx, "current_price", decision.entry_price) if ctx else decision.entry_price
            bid_price = getattr(ctx, "bid", cur_price) if ctx else cur_price
            ask_price = getattr(ctx, "ask", cur_price) if ctx else cur_price

            is_sane_limit = True
            if decision.bias == "BUY" and decision.entry_price >= bid_price:
                logger.warning(
                    f"⚠️ BUY_LIMIT price ({decision.entry_price}) is NOT below current bid ({bid_price}) for {decision.symbol}. "
                    f"Falling back safely to MARKET order dispatch."
                )
                is_sane_limit = False
            elif decision.bias == "SELL" and decision.entry_price <= ask_price:
                logger.warning(
                    f"⚠️ SELL_LIMIT price ({decision.entry_price}) is NOT above current ask ({ask_price}) for {decision.symbol}. "
                    f"Falling back safely to MARKET order dispatch."
                )
                is_sane_limit = False

            if is_sane_limit:
                limit_dir = "BUY_LIMIT" if decision.bias == "BUY" else "SELL_LIMIT"
                res = self.mt5_client.place_pending_order(
                    symbol=decision.symbol,
                    order_type=limit_dir,
                    price=decision.entry_price,
                    volume=lots,
                    sl_price=decision.stop_loss,
                    tp_price=decision.take_profit,
                    comment=comment
                )
            else:
                res = self.mt5_client.send_market_order(
                    symbol=decision.symbol,
                    order_type=decision.bias,
                    volume=lots,
                    sl_price=decision.stop_loss,
                    tp_price=decision.take_profit,
                    comment=comment,
                    reference_price=decision.entry_price
                )
        else:
            res = self.mt5_client.send_market_order(
                symbol=decision.symbol,
                order_type=decision.bias,
                volume=lots,
                sl_price=decision.stop_loss,
                tp_price=decision.take_profit,
                comment=comment,
                reference_price=decision.entry_price
            )

        _order_timer.stop()
        _order_status = self._classify_broker_response(res)
        if res is not None and isinstance(res, dict):
            res["classified_status"] = _order_status

        ORDERS_SUBMITTED.inc(mode=self._execution_mode(), status=_order_status)
        log_event(
            logger,
            logging.WARNING if _order_status not in ("FILLED", "ACCEPTED", "POSITION_CONFIRMED") else logging.INFO,
            "order_dispatched",
            f"{decision.symbol}: {_order_status} (setup_id={setup_id})",
            symbol=decision.symbol,
            action=decision.bias,
            status=_order_status,
            route="pending" if use_limit else "market",
            volume=lots,
            duration_ms=_order_timer.elapsed_ms,
            setup_id=setup_id,
        )

        if _order_status == "UNKNOWN":
            logger.error(
                f"🚨 UNKNOWN broker execution state for setup_id={setup_id} {decision.symbol}. "
                f"Broker response: {res}. Risk reservation held. Reconcile with MT5 positions/deals before any duplicate dispatch."
            )

        # §2: Re-anchor SL/TP to actual fill price if slippage occurred & validate post-fill risk
        if res and (res.get("status") == "FILLED" or _order_status == "FILLED"):
            ticket = res.get("ticket")
            fill_price = float(res.get("price", decision.entry_price))
            sl_dist = getattr(decision, "sl_distance", 0.0)
            tp_dist = getattr(decision, "tp_distance", 0.0)

            if ticket and sl_dist > 0 and abs(fill_price - decision.entry_price) > 1e-5:
                if decision.bias == "BUY":
                    actual_sl = fill_price - sl_dist
                    actual_tp = (fill_price + tp_dist) if tp_dist > 0 else decision.take_profit
                else:
                    actual_sl = fill_price + sl_dist
                    actual_tp = (fill_price - tp_dist) if tp_dist > 0 else decision.take_profit

                actual_rr = round(tp_dist / (sl_dist + 1e-9), 2)
                logger.info(
                    f"⚓ RE-ANCHORING SL/TP to real fill: Ticket=#{ticket} Fill={fill_price} "
                    f"(planned {decision.entry_price}) -> Real SL={actual_sl:.4f}, TP={actual_tp:.4f} (R:R={actual_rr})"
                )
                mod_res = self.mt5_client.modify_position(ticket=ticket, sl=actual_sl, tp=actual_tp)
                if mod_res and mod_res.get("status") == "MODIFIED":
                    res["sl"] = actual_sl
                    res["tp"] = actual_tp
                    res["real_fill_anchored"] = True

            # ── Post-Fill Monetary Risk Check (Spec v2.1 Refinement 2) ──
            if ticket:
                tick_val = float(sym_spec.get("trade_tick_value", 1.0) or 1.0)
                tick_sz = float(sym_spec.get("trade_tick_size", point) or point)
                dollar_per_unit = tick_val / max(tick_sz, 1e-9)
                actual_sl_final = float(res.get("sl", decision.stop_loss))
                realized_risk_usd = abs(fill_price - actual_sl_final) * dollar_per_unit * lots
                
                account_equity = self.state_manager.account.equity if self.state_manager.account else 10000.0
                from jarvis.config.settings import SETTINGS
                target_risk_usd = account_equity * (SETTINGS.risk.max_risk_per_trade_pct / 100.0)
                
                if realized_risk_usd > target_risk_usd * 1.10:
                    logger.warning(
                        f"⚠️ POST-FILL RISK EXCEEDED TARGET on #{ticket}: Realized risk ${realized_risk_usd:.2f} > target ${target_risk_usd:.2f} "
                        f"(Lots: {lots}, Stop Dist: {abs(fill_price - actual_sl_final):.5f})."
                    )
                    max_allowed_dist = target_risk_usd / (dollar_per_unit * lots)
                    if max_allowed_dist >= min_broker_stop_dist:
                        tighter_sl = round(fill_price - max_allowed_dist if decision.bias == "BUY" else fill_price + max_allowed_dist, digits)
                        logger.info(f"Tightening SL on #{ticket} to safe risk level: {actual_sl_final} -> {tighter_sl}")
                        mod_tight = self.mt5_client.modify_position(ticket=ticket, sl=tighter_sl, tp=res.get("tp", decision.take_profit))
                        if mod_tight and mod_tight.get("status") == "MODIFIED":
                            res["sl"] = tighter_sl
                            res["risk_tightened"] = True
                    
            try:
                import json
                from jarvis.data.database import TRADE_DB
                
                # Extract rich feature vector from MarketContext and DecisionObject
                ctx = getattr(decision, "context", None)
                if not ctx and hasattr(self.state_manager, "get_market_context"):
                    ctx = self.state_manager.get_market_context(decision.symbol)
                if not ctx and hasattr(self.state_manager, "market_contexts") and isinstance(self.state_manager.market_contexts, dict):
                    ctx = self.state_manager.market_contexts.get(decision.symbol)

                session_name = ctx.session.current_session if ctx and ctx.session else "UNKNOWN"
                is_prime = ctx.session.is_prime_session if ctx and ctx.session else True
                adx_val = float(ctx.momentum.adx) if ctx and ctx.momentum else 0.0
                plus_di = float(ctx.momentum.plus_di) if ctx and ctx.momentum else 0.0
                minus_di = float(ctx.momentum.minus_di) if ctx and ctx.momentum else 0.0
                # Reporting only: prefer the real per-bar spread the feed
                # measured (points→pips) over the registry constant, so trade
                # analytics record what was actually quoted. `getattr` keeps a
                # context built before this field existed working. No decision
                # reads this value.
                _live_spread = getattr(ctx, "live_spread_pips", None) if ctx else None
                spread_pips = (
                    float(_live_spread)
                    if _live_spread is not None
                    else (float(ctx.volatility.current_spread_pips) if ctx and ctx.volatility else 0.0)
                )
                mtf_str = json.dumps(ctx.mtf_alignment) if ctx and ctx.mtf_alignment else ""
                threats_json = json.dumps(decision.risk_factors or [])
                features_json = json.dumps({
                    "strategy": decision.strategy,
                    "adversarial_penalty": decision.adversarial_penalty,
                    "expected_value": decision.expected_value,
                    "rr_ratio": decision.risk_reward_ratio,
                    "model_confidence": decision.model_confidence,
                    "setup_id": setup_id
                })

                TRADE_DB.log_trade(
                    ticket=ticket,
                    symbol=decision.symbol,
                    action=decision.bias,
                    entry=fill_price,
                    sl=res.get("sl", decision.stop_loss),
                    tp=res.get("tp", decision.take_profit),
                    volume=lots,
                    score=decision.model_confidence * 100.0,
                    regime=decision.regime.primary_regime.value if decision.regime else "UNKNOWN",
                    ev=decision.expected_value,
                    executor="BOT (AI)",
                    session_name=session_name,
                    is_prime_session=is_prime,
                    adx=adx_val,
                    plus_di=plus_di,
                    minus_di=minus_di,
                    spread_pips=spread_pips,
                    mtf_alignment=mtf_str,
                    threats_json=threats_json,
                    features_json=features_json,
                    # D2: the position id, not the order ticket, is what the exit
                    # deal will be keyed on. Without it the row can never close.
                    position_id=res.get("position_id"),
                    # D1: say where the fill price came from, so a statistic over
                    # this table can separate real money from simulated.
                    origin=self._price_origin(res),
                    # D1 (completed): and say whether it WAS real money. These
                    # are different questions -- a live fill whose client had
                    # lost the terminal is origin='synthetic' but
                    # execution_mode='live', and that row used to be
                    # unclassifiable.
                    execution_mode=self._execution_mode(res),
                )
            except Exception as e:
                logger.error(f"Failed to log trade to DB: {e}")

        return res
