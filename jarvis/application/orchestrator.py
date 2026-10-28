"""
HM Algo 2.0 — Master System Orchestrator.
Coordinates data feeds, multi-symbol radar scans, parallel analyst clusters, risk authorization, MT5 state synchronization, and execution.
"""
import os
import time
import json
import logging
import threading
import atexit
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional, Tuple

from jarvis.application.state_manager import GLOBAL_STATE
from jarvis.application.radar_sort import radar_sort_key
from jarvis.application.event_bus import GLOBAL_EVENT_BUS
from jarvis.market.data_feed import DataFeedEngine
from jarvis.data.broker_symbols import terminal_live
from jarvis.market.market_context import MarketContextEngine
from jarvis.intelligence.regime_engine import MarketRegimeClassifier
from jarvis.intelligence.opportunity_arbiter import UniversalOpportunityArbiter
from jarvis.analysts.parallel_runner import ParallelAnalystCluster
from jarvis.intelligence.decision_engine import DecisionEngine
from jarvis.risk.risk_engine import RiskEngine
from jarvis.execution.mt5_client import MT5Client
from jarvis.execution.state_synchronizer import MT5StateSynchronizer
from jarvis.execution.execution_engine import ExecutionEngine
from jarvis.execution.order_manager import OrderManager
from jarvis.execution.position_monitor import PositionMonitorEngine
from jarvis.learning.trade_memory import TradeMemory
from jarvis.learning.online_ml_predictor import OnlineMLPredictor
from jarvis.learning.strategy_bandit import StrategyBandit
from jarvis.learning.strategy_memory import StrategyRegimeMemory
from jarvis.data.schemas import ExecutionMode, MarketRegime
from jarvis.data.symbol_registry import is_crypto
from jarvis.data.symbol_registry import resolve as _resolve_sym
from jarvis.risk.circuit_breaker import CircuitBreaker
from jarvis.risk.drawdown import DrawdownGuard
from jarvis.risk.account_tier import get_max_lot_cap
from jarvis.config.settings import verify_execution_mode, SETTINGS
from jarvis.config.paths import mode_scoped_db_path
from jarvis.market.sessions import SessionEngine
from jarvis.execution.entry_policy import evaluate_entry
from jarvis.intelligence.gate_policy import canonical_gate
from jarvis.intelligence.winrate_targeting import load_profiles
from jarvis.intelligence.llm_controller import AutonomousLLMSystemController
from jarvis.observability import log_event
from jarvis.observability.instruments import DECISION_EVALUATIONS, DECISION_LATENCY

logger = logging.getLogger("JARVIS_Orchestrator")

# ── Shared scan pool ────────────────────────────────────────────────────────
# `scan_all_modes` used to build a fresh ThreadPoolExecutor on every call
# (`max(4, min(32, len(tasks)))` workers), so every radar tick paid pool-creation
# cost and the live loop and the read-only preview endpoint could not share
# threads. One process-wide pool is enough: the work submitted is unchanged,
# only the pool's lifetime moves. `max_workers=16` bounds thread count well under
# the OS limit. `wait=False` at exit so a worker wedged in a native broker call
# cannot hold interpreter shutdown open — the broker lock already bounds that
# wait on its own.
_executor = ThreadPoolExecutor(max_workers=16, thread_name_prefix="jarvis-scan")
atexit.register(_executor.shutdown, wait=False)


class JarvisOrchestrator:
    def __init__(
        self,
        symbols: Optional[List[str]] = None,
        mode: Optional[str] = None,
        magic_number: int = 888999,
        trade_style: str = "ALL"
    ):

        # The configured universe is the real one. `allowed_symbols` in
        # config/settings.json (and JARVIS_SYMBOLS) was parsed into
        # `SETTINGS.trading.symbols` but nothing ever read it, so narrowing the
        # universe in config silently did nothing and all 13 hardcoded symbols
        # below kept being scanned and traded.
        _hardcoded = [
            "XAUUSD", "BTCUSD", "ETHUSD", "SOLUSD",
            "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCHF",
            "US500", "NAS100", "US30", "WTI"
        ]
        try:
            _configured = [str(s).strip().upper() for s in (SETTINGS.trading.symbols or []) if str(s).strip()]
        except Exception:
            _configured = []
        if symbols:
            self.symbols = list(symbols)
        else:
            self.symbols = _configured or _hardcoded
            if _configured and set(_configured) != set(_hardcoded):
                logger.warning(
                    "Symbol universe comes from config (%d): %s. The previously "
                    "hardcoded list had %d (%s). Add any you still want to "
                    "trading.allowed_symbols in config/settings.json.",
                    len(_configured), ", ".join(_configured),
                    len(_hardcoded), ", ".join(_hardcoded),
                )
        self.mode = verify_execution_mode(mode)
        self.trade_style = trade_style
        logger.info(f"JarvisOrchestrator initialized in [{self.mode.upper()}] mode.")

        self.state_manager = GLOBAL_STATE
        self.state_manager.set_execution_mode(ExecutionMode(self.mode.upper()))
        self.event_bus = GLOBAL_EVENT_BUS

        self.ml_predictor = OnlineMLPredictor()
        self.strategy_bandit = StrategyBandit()
        self.risk_engine = RiskEngine(mode=self.mode)
        # Share the exact same CircuitBreaker and DrawdownGuard instances with RiskEngine
        # so pause states, consecutive loss counts, and drawdown limits stay 100% in sync.
        self.circuit_breaker = self.risk_engine.circuit_breaker
        self.drawdown_guard = self.risk_engine.drawdown_guard
        self._pending_features = {}

        # ── In-process execution guard ─────────────────────────────────────
        # Tracks symbols currently being executed to prevent race-condition
        # multi-fires before MT5StateSynchronizer (1 s lag) can catch up.
        self._execution_in_progress: set = set()
        self._execution_lock = threading.Lock()
        # Per-symbol last-execution timestamp for 10-min same-symbol cooldown
        self._last_execution_time: Dict[str, float] = {}
        self._SAME_SYMBOL_COOLDOWN_SEC = 600  # 10 minutes default
        # Symbols the broker does not offer are refused every cycle; log the
        # reason once per symbol/style rather than on every scan.
        self._unusable_warned: set = set()

        # Out-of-sample calibrated profiles, loaded on first use and then held.
        # ``None`` means "not loaded yet"; ``{}`` means "loaded, and there are
        # none". The distinction matters: a missing file must be reported once,
        # not re-checked and re-logged on every scan of every symbol.
        self._wr_profiles: Optional[Dict[str, Any]] = None

        # Per-symbol regime tracking to eliminate cross-symbol contamination and race conditions
        self._regime_state: Dict[str, Dict[str, Any]] = {}
        self._regime_state_lock = threading.Lock()

        self.event_bus.subscribe('trade_closed', self._on_trade_closed)

        # `auto_init=False`: connecting in the constructor is what stops the
        # platform from serving. Measured on `HM_start.py live` with the terminal
        # down — `py-spy dump` put MainThread here, in
        # `MT5Client.__init__ -> init_connection -> TimeoutGuard.run_sync`,
        # waiting on a guard thread that was itself stuck inside
        # `mt5.initialize()`. That call blocks in native code HOLDING THE GIL, so
        # the guard could not even be scheduled to time out, and the main thread
        # never reached `run_web_server`. Nothing needs the eager call: every
        # operation goes through `_reconnect_if_needed()`, which connects on
        # first use. Constructing without connecting means the web server binds
        # even when the broker is unreachable — which is exactly when an operator
        # needs the UI.
        self.mt5_client = MT5Client(magic_number=magic_number, mode=self.mode, auto_init=False)
        self.data_feed = DataFeedEngine(self.mt5_client)
        self.context_engine = MarketContextEngine()
        self.regime_classifier = MarketRegimeClassifier()
        self.analyst_cluster = ParallelAnalystCluster()
        self.decision_engine = DecisionEngine(ml_predictor=self.ml_predictor, strategy_bandit=self.strategy_bandit)
        self.opportunity_arbiter = UniversalOpportunityArbiter(
            ml_predictor=self.ml_predictor,
            bandit=self.strategy_bandit,
            self_learning=getattr(self.decision_engine, "self_learning", None)
        )
        self.order_manager = OrderManager(self.mt5_client)
        self.execution_engine = ExecutionEngine(self.mt5_client, self.state_manager)
        self.trade_memory = TradeMemory()
        self.strategy_memory = StrategyRegimeMemory(self.trade_memory)
        self.llm_controller = AutonomousLLMSystemController(state_manager=self.state_manager)

        self.state_synchronizer = MT5StateSynchronizer(self.mt5_client, self.state_manager, self.event_bus)
        self.position_monitor = PositionMonitorEngine(
            self.mt5_client, self.data_feed, self.context_engine, self.state_manager, self.event_bus,
            ml_predictor=self.ml_predictor,
            manual_mode=os.environ.get("JARVIS_MANUAL_MANAGEMENT_MODE", "TRAIL")
        )
        self._running = False
        self._main_thread: Optional[threading.Thread] = None
        self._watchdog_thread: Optional[threading.Thread] = None
        self._last_heartbeat: float = time.time()

    def _is_news_blackout(self, symbol: str, now: datetime) -> bool:
        """Institutional Mandate 4: Reject new entries inside major high-impact news windows."""
        try:
            from jarvis.market.news import GLOBAL_NEWS_ENGINE
            from jarvis.data.symbol_registry import resolve as _res
            canon = _res(symbol).canonical.upper()
            calendar = GLOBAL_NEWS_ENGINE.get_news_calendar(max_wait=0.2)
            if not calendar:
                return False
            for e in calendar:
                if str(e.get("impact", "")).upper() != "HIGH":
                    continue
                if e.get("is_fallback", False) or str(e.get("source", "")) != "live_feed":
                    continue
                pairs = {str(p).upper() for p in (e.get("affected_pairs") or [])}
                if canon not in pairs:
                    continue
                event_dt = datetime.fromisoformat(str(e.get("timestamp_iso")))
                start = event_dt - timedelta(minutes=10)
                end = event_dt + timedelta(minutes=15)
                if start <= now <= end:
                    return True
        except Exception:
            pass
        return False

    def _get_same_symbol_cooldown(self, style: str = "SWING") -> float:
        st = str(style or "SWING").upper()
        if "SCALP" in st:
            return 120.0   # 2 minutes for scalp trades
        elif any(k in st for k in ("DAY", "INTRADAY")):
            return 240.0   # 4 minutes for day trades
        return 600.0       # 10 minutes for swing trades

    def start(self):
        """Starts the full HM Algo 2.0 engine, background workers, and watchdog supervisor."""
        if not self._running:
            self._running = True
            self.state_manager.set_orchestrator_running(True)
            self.state_synchronizer.start()
            self.position_monitor.start()
            self._main_thread = threading.Thread(target=self._orchestration_loop, daemon=True, name="jarvis_orchestrator")
            self._main_thread.start()
            self._watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True, name="jarvis_watchdog")
            self._watchdog_thread.start()
            logger.info("HM Algo 2.0 Orchestrator and Watchdog started.")

    def _watchdog_loop(self):
        """Autonomous self-healing watchdog monitoring broker state, thread health, and stale locks."""
        logger.info("HM Algo 2.0 Autonomous Watchdog supervisor active.")
        while self._running:
            try:
                now = time.time()
                # 1. Stale Execution Lock Recovery
                with self._execution_lock:
                    stale_syms = []
                    for sym in list(self._execution_in_progress):
                        last_exec = self._last_execution_time.get(sym, 0.0)
                        if (now - last_exec) > 60.0:  # Lock held longer than 60s without release
                            stale_syms.append(sym)
                    for sym in stale_syms:
                        logger.warning(f"Watchdog auto-releasing stale execution lock for {sym}.")
                        self._execution_in_progress.discard(sym)

                # 2. Broker connection and market-data health.
                # Two different links, previously conflated: DATA_FEED was keyed
                # off the *execution* account login, so paper mode reported
                # OFFLINE forever while real bars were streaming, and the
                # dashboard chip read "broker offline" off the same flag.
                # Execution link:
                acc = self.mt5_client.get_account_snapshot()
                if acc and acc.login > 0:
                    self.state_manager.update_service_health("MT5", "CONNECTED")
                elif terminal_live():
                    # Terminal is up and answering; we are simply not sending
                    # orders. The broker is reachable - say so.
                    # `terminal_live()`, not the latch: reporting CONNECTED off a
                    # latched flag keeps claiming a broker link that a died-mid-
                    # session terminal no longer provides, and health flags must
                    # be measured rather than inferred.
                    self.state_manager.update_service_health(
                        "MT5", "SIMULATED" if self.mode == "paper" else "CONNECTED"
                    )
                else:
                    self.state_manager.update_service_health(
                        "MT5", "SIMULATED" if self.mode == "paper" else "RECONNECTING"
                    )

                # Market-data link: measured, not inferred.
                self.state_manager.update_service_health(
                    "DATA_FEED", self.data_feed.data_health().get("status", "OFFLINE")
                )

                # 3. ML Predictor Brier Score Health Check
                if int(now) % 300 < 10:
                    brier = self.ml_predictor.get_brier_score()
                    feat_imp = self.ml_predictor.get_feature_importance()
                    top_feat = max(feat_imp.items(), key=lambda x: x[1])[0] if feat_imp else "N/A"
                    logger.debug(f"Watchdog ML Heartbeat: BrierScore={brier:.3f}, TopFeature={top_feat}")

                # 4. Pending Order Staleness Cleanup (Task 2c)
                if int(now) % 60 < 10:
                    self.order_manager.cleanup_stale_pending_orders(max_age_sec=1800)

                # 5. Autonomous LLM Governor Dynamic Telemetry Refresh
                if int(now) % 180 < 10:
                    try:
                        self.llm_controller.get_latest_directives()
                    except Exception as e:
                        logger.debug(f"LLM Governor background refresh error: {e}")

            except Exception as e:
                logger.error(f"Watchdog supervisor error: {e}", exc_info=True)

            time.sleep(10.0)

    def stop(self):
        """Clean shutdown of all engine workers."""
        self._running = False
        self.state_manager.set_orchestrator_running(False)
        self.position_monitor.stop()
        self.state_synchronizer.stop()
        self.mt5_client.shutdown()
        logger.info("HM Algo 2.0 Orchestrator stopped.")

    def get_active_symbols(self) -> List[str]:
        """Return the list of currently active trading symbols."""
        with self._execution_lock:
            return list(self.symbols)

    def set_active_symbols(self, symbols: List[str], persist: bool = True) -> List[str]:
        """Atomically update active trading symbols in memory and configuration."""
        clean = [str(s).strip().upper() for s in symbols if str(s).strip()]
        with self._execution_lock:
            self.symbols = clean
        try:
            from jarvis.config.settings import SETTINGS, persist_allowed_symbols
            SETTINGS.trading.symbols = list(clean)
            if persist:
                persist_allowed_symbols(clean)
        except Exception as e:
            logger.debug(f"Could not persist active symbols: {e}")
        logger.info(f"🎯 ORCHESTRATOR: Active symbols updated to ({len(clean)}): {clean}")
        return self.symbols

    def toggle_symbol(self, symbol: str, enabled: bool, persist: bool = True) -> bool:
        """Enable or disable a single symbol at runtime."""
        sym = str(symbol).strip().upper()
        with self._execution_lock:
            curr = set(self.symbols)
            if enabled:
                curr.add(sym)
            else:
                curr.discard(sym)
            self.symbols = sorted(list(curr))
        try:
            from jarvis.config.settings import SETTINGS, persist_allowed_symbols
            SETTINGS.trading.symbols = list(self.symbols)
            if persist:
                persist_allowed_symbols(self.symbols)
        except Exception as e:
            logger.debug(f"Could not persist toggled symbol: {e}")
        logger.info(f"🎯 ORCHESTRATOR: Symbol {sym} {'ENABLED' if enabled else 'DISABLED'}. Total active ({len(self.symbols)}): {self.symbols}")
        return enabled


    def _record_open_trade_context(
        self,
        exec_res: Optional[Dict[str, Any]],
        decision: Any,
        context: Any,
        trade_style: str,
        lots: float,
    ) -> None:
        """Persists open-time ML features and trade memory for FILLED and PLACED orders."""
        if not exec_res or exec_res.get("status") not in ("FILLED", "PLACED"):
            return

        ticket = exec_res.get("ticket")
        if not ticket:
            return

        fill_price = float(exec_res.get("price", decision.entry_price))
        actual_sl = float(exec_res.get("sl", decision.stop_loss))
        actual_tp = float(exec_res.get("tp", decision.take_profit))
        regime = decision.regime or getattr(context, "regime", None)

        try:
            ml_feat = self.ml_predictor.extract_feature_vector(
                context=context,
                regime=regime,
                tentative_bias=decision.bias,
                devil_penalty=decision.adversarial_penalty,
                target_rr=decision.risk_reward_ratio
            )
        except Exception as e:
            logger.debug(f"Could not extract ML feature vector: {e}")
            ml_feat = []

        regime_str = (
            regime.primary_regime.value
            if (regime and hasattr(regime.primary_regime, "value"))
            else (str(regime.primary_regime) if regime else "UNKNOWN")
        )

        self._pending_features[ticket] = {
            "features": ml_feat,
            "strategy": decision.strategy,
            "regime": regime_str,
            "trade_style": trade_style,
            "type": decision.bias,
            "entry": fill_price,
            "sl": actual_sl,
            "risk_dist": abs(fill_price - actual_sl),
            "symbol": decision.symbol
        }

        self.trade_memory.record_trade({
            "ticket": ticket,
            "symbol": decision.symbol,
            "type": decision.bias,
            "entry": fill_price,
            "sl": actual_sl,
            "tp": actual_tp,
            "lots": lots,
            "regime": regime_str,
            "strategy": decision.strategy,
            "model_confidence": float(decision.model_confidence) if isinstance(decision.model_confidence, (int, float)) else 0.5,
            "raw_win_prob": float(decision.raw_win_prob) if isinstance(getattr(decision, "raw_win_prob", None), (int, float)) else None,
            "adversarial_penalty": float(decision.adversarial_penalty) if isinstance(decision.adversarial_penalty, (int, float)) else 0.0,
            "expected_value": float(decision.expected_value) if isinstance(decision.expected_value, (int, float)) else 0.0,
            "ml_features": ml_feat.tolist() if hasattr(ml_feat, "tolist") else list(ml_feat)
        })
        logger.info(f"📝 Recorded open-time context for Trade #{ticket} ({decision.symbol} {decision.bias} {trade_style})")

    def _recover_pending_features(self, ticket):
        """Rebuild the open-time context for `ticket` from the trade journal.

        AI6. `self._pending_features` is a plain dict, so it is empty for every
        trade opened before the current process — the learning loop then skips the
        ML update and the bandit update, and R becomes the invented 2.0 / -1.0
        fallback. `record_trade` already wrote everything needed to
        `trade_records`, so a close can be reconstructed from disk.

        Returns None when there is no row, which is the honest answer: it means
        "this ticket was never journalled", not "R was +2".
        """
        getter = getattr(self.trade_memory, "fetch_trade", None)
        if not callable(getter):
            return None
        try:
            row = getter(ticket)
        except Exception as e:
            logger.warning("Could not read trade %s from the journal: %s", ticket, e)
            return None
        if not row:
            return None

        entry = float(row.get("entry_price") or 0.0)
        sl = float(row.get("sl") or 0.0)
        features = row.get("ml_features")
        if isinstance(features, str):
            try:
                features = json.loads(features)
            except Exception:
                features = None

        out = {
            "strategy": row.get("strategy") or "UNKNOWN",
            "regime": row.get("regime") or "GLOBAL",
            "trade_style": "SWING",
            "type": row.get("trade_type") or "BUY",
            "entry": entry,
            "sl": sl,
            # 0.0 when the stop was never stored. |entry - 0| is NOT a risk distance --
            # on EURUSD it reads as 11,000 pips -- so a missing stop must disable the
            # R calculation rather than hand it a nonsense denominator.
            "risk_dist": abs(entry - sl) if (entry > 0 and sl > 0) else 0.0,
            "symbol": row.get("symbol"),
        }
        # `update_online` only runs when "features" is present, so a row with no stored
        # vector still gets the bandit and the close bookkeeping. An ABSENT vector must
        # not become a sample: `record_trade` json-dumps `[]` when none was given, and a
        # gradient step on an all-zero feature vector is a fabricated observation.
        if features:
            out["features"] = features
        return out

    def _on_trade_closed(self, data):
        ticket = data.get("ticket")
        # `pnl` and `is_win` are OPTIONAL. A close event that carries no P&L used to
        # fall back to 0.0, which is a *genuine* value — it recorded a real break-even
        # and, via `is_win = 1 if pnl > 0 else 0`, filed the trade as a LOSS. That
        # phantom loss then fed the circuit breaker, the bandit and the ML label.
        # 0.0 means "measured, broke even"; None means "not measured".
        raw_pnl = data.get("pnl", None)
        pnl = None if raw_pnl is None else float(raw_pnl)
        is_win = None if pnl is None else (1 if pnl > 0 else 0)
        exit_price = float(data.get("exit_price", 0.0))
        new_equity = float(data.get("equity", 0.0))

        pending = self._pending_features.pop(ticket, None)
        if pending is None:
            # AI6: `_pending_features` is process-local, so any trade that spans a
            # restart arrives here with nothing — and the learning loop below is
            # skipped entirely, while R falls back to an invented +/-. Everything
            # needed was persisted at open, so recover it instead of guessing.
            pending = self._recover_pending_features(ticket)
        strategy = pending.get("strategy", data.get("strategy", "UNKNOWN")) if pending else data.get("strategy", "UNKNOWN")
        regime_name = pending.get("regime", data.get("regime", "GLOBAL")) if pending else data.get("regime", "GLOBAL")
        trade_style = pending.get("trade_style", data.get("trade_style", "SWING")) if pending else data.get("trade_style", "SWING")

        # Calculate realized R-multiple.
        #
        # The signed R-multiple MUST be derived from the trade DIRECTION, never from
        # the win/loss flag. Deriving it from `is_win` inverts the sign of every
        # profitable SELL and every losing BUY, which poisons the return-weighted
        # gradient in OnlineMLPredictor.update_online().
        #   BUY : r = (exit - entry) / risk_dist
        #   SELL: r = (entry - exit) / risk_dist
        direction = str(
            (pending or {}).get("type")
            or data.get("type")
            or data.get("action")
            or "BUY"
        ).upper()
        is_sell = direction.startswith("SELL") or direction.startswith("SHORT")

        # AI6: None means "not derivable", and must stay that way. The old fallback
        # was `2.0 if is_win else -1.0` — a number invented from the outcome flag,
        # which is then fed to the bandit as if it had been measured. A trade with
        # no journal row and no geometry has an UNKNOWN R, and unknown is a
        # different fact from "made 2R".
        r_multiple = None
        if pending and pending.get("risk_dist", 0) > 0 and exit_price > 0:
            entry = float(pending.get("entry", 0.0) or 0.0)
            risk_dist = float(pending.get("risk_dist", 1.0) or 1.0)
            price_delta = (entry - exit_price) if is_sell else (exit_price - entry)
            # Preserve the true sign of the outcome; only floor the magnitude so the
            # gradient weighting cannot collapse to zero.
            raw_r = price_delta / risk_dist
            r_multiple = round(raw_r if abs(raw_r) >= 0.01 else (0.01 if raw_r >= 0 else -0.01), 2)
        elif pending is None:
            logger.warning(
                "Trade #%s closed with no open-time context: not in _pending_features "
                "and not in the trade journal. R-multiple is UNKNOWN, so the ML "
                "update is skipped and the bandit records the win/loss with no "
                "reward magnitude, rather than either being fed an invented value.",
                ticket,
            )

        # 1. Update SQLite trade records (§17)
        if ticket:
            # D19 — mfe/mae were hardcoded 0.0 here, so every row claimed to have
            # been measured and to have had no excursion at all. The monitor
            # accumulates the real ones while the position is open; ask it, and
            # pass None (written as NULL) when it never sampled this ticket.
            excursions = self.position_monitor.pop_excursions(ticket)
            mfe, mae = excursions if excursions else (None, None)
            # A price of 0 means the event did not carry one; pass it as None so
            # the column stays NULL ("not recorded") rather than 0.0, which would
            # claim the trade exited at zero — the same reason the `executed_trades`
            # write below does it. `pnl` is a genuine 0.0 for a scratch trade and
            # is passed through.
            self.trade_memory.update_closed_trade(
                ticket=ticket,
                exit_price=exit_price if exit_price > 0 else None,
                pnl=pnl,
                is_win=is_win,
                mfe=mfe,
                mae=mae
            )

            # `executed_trades` is a DIFFERENT store from `trade_memory`, and it
            # is written at OPEN by `ExecutionEngine` — nothing wrote the outcome
            # back. So a closed row there kept no exit price and no realised P&L,
            # and the only place a real figure existed was a request-time dict
            # built from live MT5 deals. `trade_closed` carries both, so record
            # them on the row this position opened.
            #
            # A price of 0 means the event did not carry one, and is passed as
            # None so the column stays NULL ("not recorded") rather than 0.0.
            # `pnl` is a genuine 0.0 for a scratch trade and is passed through.
            try:
                from jarvis.data.database import TRADE_DB
                TRADE_DB.record_trade_exit(
                    ticket=ticket,
                    position_id=data.get("position_id") or ticket,
                    exit_price=exit_price if exit_price > 0 else None,
                    realized_pnl=pnl,
                    closed_at=datetime.now(timezone.utc).isoformat(),
                )
            except Exception:
                logger.warning("could not record the exit in the trade journal",
                               exc_info=True)

        # 2. Update ML SGD predictor with return weighting (§17)
        #
        # AI6: what R means is different here than it is to the bandit, and the two
        # must not be treated alike. To the bandit, `rewards` is a RECORDED quantity
        # denominated in R, so an unknown R has to withhold it. Here R is only the
        # WEIGHT on the gradient (`return_weight = ... abs(float(r_multiple or 1.0))`)
        # -- a hyperparameter of the update, not a datum anything later reads as "this
        # trade made 1R". The label being learned is `is_win`, which is measured
        # regardless.
        #
        # So an unknown R takes the standard step: the argument is OMITTED, letting
        # `update_online`'s declared default apply. It must not be *passed* as None,
        # because the implementation coerces it and an explicit None would be
        # indistinguishable from a measured +1R. Skipping the update entirely would
        # throw away a real labelled sample to avoid guessing a step size.
        # An unmeasured outcome is not a label. Training on `is_win=None` would teach
        # the model that a data gap is a loss.
        if pending and "features" in pending and is_win is not None:
            if r_multiple is None:
                self.ml_predictor.update_online(pending["features"], is_win)
            else:
                self.ml_predictor.update_online(pending["features"], is_win, r_multiple=r_multiple)

        # 3. Update Multi-Armed Bandit with Thompson Sampling (§17)
        #
        # AI6: the bandit gets the trade even when R is unknown, because the
        # win/loss IS measured — only the magnitude is not. `record_outcome` takes
        # None to mean "no reward magnitude", which withholds the R-denominated
        # term instead of fabricating one. (Passing None used to be coerced to +1R
        # by `float(r_multiple or 1.0)`, so an unmeasured trade was banked as a
        # winning one.)
        # A bandit reward of None is not a reward. Skipping keeps an unmeasured close
        # from being counted as a loss against the strategy that produced it.
        if is_win is not None:
            self.strategy_bandit.record_outcome(
                strategy=strategy,
                is_win=is_win,
                r_multiple=r_multiple,
                regime=regime_name,
                style=trade_style
            )

        # 4. Update Circuit Breaker, Loss Cooldown & Drawdown Guard
        trade_symbol = pending.get("symbol", data.get("symbol", "")) if pending else data.get("symbol", "")
        # The dangerous one: `is_win == 1` was False for an unmeasured close, so a
        # data gap registered as a LOSS and could help trip the circuit breaker on
        # losses that were never observed. Withhold the sample instead.
        if is_win is not None:
            self.circuit_breaker.record_trade_result(is_win == 1, symbol=trade_symbol, regime=regime_name)
            risk_eng = getattr(self, "risk_engine", None)
            if risk_eng and hasattr(risk_eng, "loss_cooldown"):
                risk_eng.loss_cooldown.record_trade_result(
                    pnl=pnl or 0.0,
                    is_win=(is_win == 1),
                    symbol=trade_symbol,
                    current_date=datetime.now(timezone.utc).date().isoformat(),
                )
        if new_equity > 0:
            self.drawdown_guard.update_equity_benchmarks(new_equity, float(data.get("balance", new_equity)))

        # 5. Recalibrate confidence curve from recent closed trades (§17)
        # `exit_price` is NULL for a trade whose outcome was never recorded, and
        # `dict.get(key, 0)` only returns the default when the key is *missing* — a
        # present-but-None value comes back as None and `None > 0` raises. Those rows
        # are "not closed yet" for calibration purposes and must be filtered out.
        all_closed = [
            t for t in self.trade_memory.fetch_recent_trades(50)
            if (t.get("exit_price") or 0) > 0
        ]
        if len(all_closed) >= 10:
            self.decision_engine.calibrator.update_calibration_from_history(all_closed)

        # AI6: `None` must be printed as UNKNOWN. Formatting it with `%.2f`/`{:.2f}`
        # would render the string "None" and, in the old code, the value was
        # invented outright — either way the log claimed a measurement that the
        # learning loop did not have.
        r_display = "UNKNOWN" if r_multiple is None else f"{r_multiple:+.2f}R"
        pnl_display = "UNKNOWN" if pnl is None else f"${pnl:.2f}"
        logger.info(
            f"🔄 Closed-trade self-learning loop completed for #{ticket}: "
            f"PnL={pnl_display}, Win={is_win}, R={r_display}, Strat={strategy}, Regime={regime_name}, Style={trade_style}"
        )

    @staticmethod
    def _df_to_candles(df) -> List[Dict[str, Any]]:
        if df is None or not hasattr(df, "empty") or df.empty:
            return []
        try:
            recs = df.tail(120).to_dict("records")
        except Exception:
            return []
        out: List[Dict[str, Any]] = []
        for r in recs:
            out.append({
                "open": float(r.get("Open", r.get("open", 0.0))),
                "high": float(r.get("High", r.get("high", 0.0))),
                "low": float(r.get("Low", r.get("low", 0.0))),
                "close": float(r.get("Close", r.get("close", 0.0))),
                "volume": float(r.get("Volume", r.get("volume", 0.0))),
            })
        return out

    @staticmethod
    def _first_untrusted_frame(mtf_data) -> Tuple[Optional[str], Optional[str], float]:
        """First role we must not reason on, with the reason and age.

        Returns ``(role, reason, age_sec)`` where ``reason`` is ``"stale"`` or
        ``"unverifiable_age"``; ``(None, None, 0.0)`` when every frame is usable.
        """
        from jarvis.market.data_feed import first_untrusted_frame

        return first_untrusted_frame(mtf_data)

    @staticmethod
    def _first_unusable_frame(mtf_data) -> Tuple[Optional[str], Optional[str]]:
        """First role whose frame is not real broker data, with its source."""
        from jarvis.market.data_feed import first_unusable_frame

        return first_unusable_frame(mtf_data)

    @staticmethod
    def _count_entries_today(symbol: str) -> int:
        """Rows written for this symbol since 00:00 UTC today, demo ledger basis.

        Used by the daily entry cap. Read-only on the canonical trade DB; any
        failure returns 0 (fails open — a counting outage must never freeze
        the trading loop), which is safe because the cap then simply lets the
        other governors work.
        """
        try:
            from jarvis.data.database import TRADE_DB

            path = getattr(TRADE_DB, "db_path", None)
            if not path or path == ":memory:":
                return 0
            today_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            patterns = [f"{today_prefix}%"]
            import sqlite3 as _sq

            conn = _sq.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
            try:
                canon = str(symbol).upper().strip()
                row = conn.execute(
                    "SELECT COUNT(*) FROM executed_trades "
                    "WHERE ticket IS NOT NULL AND (symbol = ? OR symbol = ?) "
                    "AND timestamp LIKE ?",
                    (canon, str(_resolve_sym(symbol).canonical).upper(), patterns[0]),
                ).fetchone()
                return int(row[0]) if row else 0
            finally:
                conn.close()
        except Exception as e:
            logger.debug(f"daily entry count unavailable for {symbol}: {e}")
            return 0

    # ── Calibrated entry authority ──────────────────────────────────────────
    #
    # ``jarvis.execution.entry_policy`` implements the out-of-sample calibrated
    # replacement for the legacy 29-check gate stack, and
    # ``config/winrate_profiles.json`` holds 16 calibrated symbol profiles that
    # ``tools/calibrate_winrate.py`` wrote. Both ends existed; nothing joined
    # them. Live entry selection therefore still ran on the legacy stack, which
    # is the exact stack ``entry_policy`` documents as unvalidated and
    # value-destroying -- and the calibration that could have replaced it was
    # only ever read by offline analysis scripts.
    #
    # The wire below is gated on ``trading.use_calibrated_entry_policy`` and
    # defaults to OFF. Enabling it is a trading decision, not a bug fix: 11 of
    # the 16 profiles on disk carry a NEGATIVE out-of-sample expectancy, so the
    # policy refuses most of the universe.

    def _calibrated_profile(self, symbol: str):
        """This symbol's calibrated profile, or None when none applies."""
        if not bool(getattr(SETTINGS.trading, "use_calibrated_entry_policy", False)):
            return None
        if self._wr_profiles is None:
            self._wr_profiles = load_profiles()
            logger.info(
                "calibrated entry policy ACTIVE: %d symbol profiles loaded",
                len(self._wr_profiles),
            )
        if not self._wr_profiles:
            return None
        canonical = str(_resolve_sym(symbol).canonical).upper()
        return self._wr_profiles.get(canonical) or self._wr_profiles.get(str(symbol).upper())

    def _calibrated_entry_decision(self, symbol: str, decision: Any, regime: Any):
        """The calibrated policy's verdict, or None when it is not the authority.

        None is not "allowed" -- it is "this policy did not decide", which leaves
        the legacy gate verdict in force. Conflating the two is how a missing
        calibration would silently become a permission to trade.
        """
        profile = self._calibrated_profile(symbol)
        if profile is None:
            return None

        regime_name = (
            regime.primary_regime.value
            if hasattr(regime.primary_regime, "value")
            else str(regime.primary_regime)
        )
        score_val = max(
            float(getattr(decision, "model_confidence", 0.0) or 0.0),
            float(getattr(decision, "ai_score", 0.0) or 0.0) / 100.0,
        )
        return evaluate_entry(
            quality_gate=getattr(decision, "quality_gate", None),
            score=score_val,
            regime=regime_name,
            profile=profile,
        )

    def _is_unsplittable_book(
        self,
        entry_price: float,
        stop_loss: float,
        equity: float,
        spec: Any,
        volume_min: float,
    ) -> bool:
        """True when the candidate book degrades to a single all-or-nothing lot.

        A split needs the position to survive as partial + runner, which demands
        the expected (post-shrink) volume to reach 2x the broker minimum. When
        the risk-parity ideal — discounted by a representative anchor/confidence
        shrink of 0.55 — falls below that floor, the trade executes as one
        min-lot book: no partial can be banked, so sub-2R geometry cannot
        compound (the measured 208 SL vs 52 TP failure mode).
        """
        try:
            entry_price = float(entry_price)
            stop_loss = float(stop_loss)
            equity = float(equity)
            volume_min = float(volume_min)
            pip_value = float(getattr(spec, "pip_value_per_lot", 0.0) or 0.0)
            pip_size = float(getattr(spec, "pip_size", 0.0) or 0.0)
            if equity <= 0 or pip_value <= 0 or pip_size <= 0 or entry_price <= 0 or stop_loss <= 0:
                return False
            sl_pips = abs(entry_price - stop_loss) / pip_size
            if sl_pips <= 0:
                return False
            base_pct = float(getattr(self.risk_engine, "max_risk_per_trade_pct", 0.5) or 0.5)
            if equity < 250.0:
                pct = min(base_pct * 3.0, 2.0)
            elif equity < 500.0:
                pct = min(base_pct * 2.0, 1.5)
            elif equity < 1000.0:
                pct = min(base_pct * 1.5, 1.0)
            else:
                pct = base_pct
            budget = equity * (pct / 100.0)
            ideal_lots = budget / (sl_pips * pip_value)
            expected_lots = ideal_lots * 0.55
            return expected_lots < 2.0 * max(volume_min, 0.01)
        except Exception:
            return False

    def _apply_stop_and_reverse_if_eligible(
        self,
        decision: Any,
        context: Optional[Any],
        active_sym_positions: List[Any],
        positions: List[Any],
        canonical_sym: str,
        entry_override: bool = False
    ) -> Tuple[List[Any], List[Any], bool]:
        """
        Institutional Stop-and-Reverse (SAR) Protocol:
        When the AI resolves a high-conviction setup in the OPPOSITE direction of an active trade,
        and the opposing trade is in profit (to bank it) or failing (to cut loss early),
        closes the opposing trade first and allows the reversal to execute cleanly.
        """
        def _get_pos_side(p: Any) -> str:
            raw = getattr(p, "side", getattr(p, "type", "BUY"))
            if isinstance(raw, int):
                return "BUY" if raw == 0 else "SELL"
            return str(raw or "BUY").upper()

        opposing_positions = [
            p for p in active_sym_positions
            if _get_pos_side(p) != str(getattr(decision, "bias", "")).upper()
        ]
        if not opposing_positions:
            return active_sym_positions, positions, False

        opp_pos = opposing_positions[0]
        opp_open_price = float(getattr(opp_pos, "open_price", 0.0) or 0.0)
        opp_sl_price = float(getattr(opp_pos, "sl", 0.0) or 0.0)
        atr_val = getattr(getattr(context, "volatility", None), "atr", 1.0) or 1.0 if context else 1.0
        opp_risk_dist = abs(opp_open_price - opp_sl_price) if opp_sl_price > 0 else (atr_val * 2.0)
        cur_price_val = float(getattr(context, "current_price", opp_open_price) or opp_open_price) if context else opp_open_price
        opp_r = ((cur_price_val - opp_open_price) if _get_pos_side(opp_pos) == "BUY" else (opp_open_price - cur_price_val)) / max(opp_risk_dist, 1e-6)

        opp_open_time = getattr(opp_pos, "open_time", None)
        opp_age_sec = 0.0
        if opp_open_time:
            try:
                if isinstance(opp_open_time, str):
                    open_dt = datetime.fromisoformat(opp_open_time.replace("Z", "+00:00")) if "T" in opp_open_time else datetime.strptime(opp_open_time, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                elif isinstance(opp_open_time, datetime):
                    open_dt = opp_open_time if opp_open_time.tzinfo else opp_open_time.replace(tzinfo=timezone.utc)
                else:
                    open_dt = None
                if open_dt:
                    opp_age_sec = max(0.0, (datetime.now(timezone.utc) - open_dt).total_seconds())
            except Exception:
                opp_age_sec = 0.0

        opp_comment = str(getattr(opp_pos, "comment", "") or "").upper()
        opp_is_swing = "SWNG" in opp_comment or "SWING" in opp_comment
        opp_is_day = "DAY" in opp_comment or "INTRADAY" in opp_comment
        new_is_scalp = str(getattr(decision, "trade_style", "")).upper() == "SCALP"

        struct_choch = bool(getattr(getattr(context, "structure", None), "choch", False)) if context else False
        has_breathing_room = (opp_age_sec >= (60.0 if new_is_scalp else 180.0)) or (opp_r <= -0.50) or struct_choch or (opp_r >= 0.20)
        horizon_allowed = not (new_is_scalp and (opp_is_swing or opp_is_day))
        conf_val = float(getattr(decision, "model_confidence", 0.0) or 0.0)
        ev_val = float(getattr(decision, "expected_value", 0.0) or 0.0)
        rr_val = float(getattr(decision, "risk_reward_ratio", 0.0) or 0.0)
        grade_val = getattr(decision, "grade", getattr(decision, "setup_grade", "")) or ""
        high_conviction = (
            (conf_val >= 0.48 or entry_override is True or grade_val in ("GRADE_A_PLUS", "GRADE A+", "GRADE_A", "GRADE A", "GRADE_B", "GRADE B"))
            and ev_val > 0.0
            and rr_val >= 1.2
        )
        opp_is_failing_or_banking = (opp_r >= 0.20) or (opp_r <= -0.15) or struct_choch

        is_flip_eligible = (
            high_conviction
            and has_breathing_room
            and horizon_allowed
            and opp_is_failing_or_banking
        )
        if not is_flip_eligible:
            return active_sym_positions, positions, False

        _flip_action = "PROFIT HARVEST" if opp_r >= 0.20 else ("STOP INVALIDATION" if opp_r <= -0.15 else "STRUCTURAL SHIFT")
        logger.info(
            f"🔄 STOP-AND-REVERSE ({_flip_action}): High-conviction {getattr(decision, 'bias', 'TRADE')} signal "
            f"({conf_val*100:.1f}%, EV=${ev_val:.2f}) "
            f"closes opposing #{opp_pos.ticket} {getattr(opp_pos, 'symbol', canonical_sym)} {getattr(opp_pos, 'type', '')} ({opp_r:+.2f}R, age={opp_age_sec:.0f}s). "
            f"Executing clean position reversal into {getattr(decision, 'bias', '')}."
        )
        close_res = self.mt5_client.close_position(opp_pos.ticket)
        if close_res and close_res.get("status") in ("CLOSED", "PARTIALLY_CLOSED"):
            self.risk_engine.release_risk(canonical_sym)
            active_sym_positions = [p for p in active_sym_positions if p.ticket != opp_pos.ticket]
            positions = [p for p in positions if p.ticket != opp_pos.ticket]
            self._last_execution_time[canonical_sym] = 0.0
            return active_sym_positions, positions, True

        return active_sym_positions, positions, False

    def run_cycle_for_symbol(self, symbol: str, trade_style: Optional[str] = None,
                             dry_run: bool = False) -> Dict[str, Any]:
        """Executes a single end-to-end analytical and decision cycle for a target symbol and trade style.

        ``dry_run=True`` runs the whole analytical path — data, context, regime,
        analysts, decision, sizing and every authorization gate — but performs no
        side effects: no position trailing, no order submission, no risk
        reservation, no learning record. It exists so the auto-selection endpoint
        can ask "what would you do right now?" without doing it.
        """
        active_trade_style = (trade_style or self.trade_style or "SWING").upper()

        # 1a. Profile session window short-circuit (2026-10-20).
        #
        # Gold traded all day on 2026-10-20 because the pre-window cycles still
        # ran the entire analytical pipeline and the entry policy overrode the
        # failing window gate (fixed in entry_policy.py). This short-circuits
        # the same waste from the other side: a symbol whose profile enforces a
        # session window and nothing needs managing for it right now gets no
        # context build, no analysts, no regime, no gate stack — none of which
        # can change the answer. Open positions or resting pendings keep the
        # full path so the in-line trailing block and the frozen-order cleanup
        # still run.
        _prof = None
        _is_premium_profile = False
        try:
            from jarvis.intelligence.symbol_profile_config import get_symbol_profile_config as _gspc
            _prof = _gspc(symbol)
            if _prof is not None:
                _is_premium_profile = bool(getattr(_prof, "premium_window_exception", False))
            if (_prof is not None and getattr(_prof, "session_restriction", False)
                    and getattr(_prof, "allowed_utc_hours", None)):
                _w_start, _w_end = _prof.allowed_utc_hours
                _now_h = datetime.now(timezone.utc).hour
                if not (_w_start <= _now_h < _w_end):
                    # 2026-10-20 premium exception: profiles that opt into
                    # premium_window_exception (gold) keep running the FULL
                    # analytical pipeline on every cycle even when the session
                    # window is closed. The off-window entry bar is enforced
                    # downstream (decision-engine gate: blended score >= the
                    # profile's premium floor), so the engine always has a
                    # live radar on these symbols and an elite A+ setup is
                    # never missed — while the ordinary off-hours chop is
                    # still refused at the score gate, keeping the ledger
                    # finding (off-window gold = bleed) intact.
                    if not getattr(_prof, "premium_window_exception", False):
                        _sym_positions = [
                            p for p in self.state_manager.positions
                            if symbol.upper() in str(p.symbol).upper()
                            or ("XAUUSD" == symbol.upper() and "GOLD" in str(p.symbol).upper())
                        ]
                        _sym_pend = []
                        try:
                            for _o in (self.mt5_client.get_pending_orders() or []):
                                if symbol.upper() in str(getattr(_o, "symbol", "")).upper():
                                    _sym_pend.append(_o)
                        except Exception:
                            _sym_pend = []
                        if not _sym_positions and not _sym_pend:
                            DECISION_EVALUATIONS.inc(style=active_trade_style, outcome="refused")
                            return {
                                "symbol": symbol,
                                "trade_style": active_trade_style,
                                "decision": None,
                                "context": None,
                                "authorized": False,
                                "auth_reason": f"PROFILE_SESSION_WINDOW: gold trades {_w_start:02d}:00-{_w_end:02d}:00 UTC only; window closed",
                                "dry_run": bool(dry_run),
                                "execution": None,
                                "stale": False,
                            }
        except Exception:
            pass

        # 1b. Daily entry cap (2026-10-20 profitability audit): 83 gold trades
        # in days on a micro account churned commission-free but spread-paying
        # entries with -0.44R expectancy; institutional practice is 1-3 setups
        # per session per instrument. Cap new entries per symbol per UTC day;
        # position management and trailing are never touched by this.
        #
        # Premium-window profiles (XAUUSD) DEFER the pre-analysis bail: the
        # user directive is "gold is always checked", and a silent refusal
        # before the analysis ever runs is exactly the failure mode that
        # convolved with the closed window to drain the ledger. For them the
        # cap is enforced POST-analysis (below) — in-session entries still
        # count, and only an off-window elite setup clears the bar.
        try:
            _daily_cap = int(getattr(SETTINGS.trading, "max_daily_entries_per_symbol", 4) or 4)
            if _daily_cap > 0 and not _is_premium_profile:
                _today_count = self._count_entries_today(symbol)
                if _today_count >= _daily_cap:
                    logger.debug(
                        "DAILY_ENTRY_CAP: %s has %d entries today >= cap %d; analysis bailed early.",
                        symbol, _today_count, _daily_cap,
                    )
                    DECISION_EVALUATIONS.inc(style=active_trade_style, outcome="refused")
                    return {
                        "symbol": symbol,
                        "trade_style": active_trade_style,
                        "decision": None,
                        "context": None,
                        "authorized": False,
                        "auth_reason": f"DAILY_ENTRY_CAP: {_today_count} entries today >= cap {_daily_cap}",
                        "dry_run": bool(dry_run),
                        "execution": None,
                        "stale": False,
                    }
        except Exception:
            pass

        # 1. Fetch Multi-Timeframe Data based on trade_style
        mtf_data = self.data_feed.fetch_multi_timeframe(symbol, trade_style=active_trade_style)

        # 1b. A frame that claims to be LIVE_MT5 but cannot be shown to be
        # current must not be reasoned on: a stalled feed yields a decision that
        # looks entirely valid, which is the worst kind. Two cases block --
        # STALE (we know it is old) and UNKNOWN-on-a-live-frame (we cannot tell
        # how old it is, which is not the same statement as "these bars are
        # synthetic"). MARKET_CLOSED never blocks: a shut market is not a broken
        # feed, and the session logic already governs that case.
        bad_role, bad_reason, bad_age = self._first_untrusted_frame(mtf_data)
        if bad_role:
            if bad_reason == "stale":
                detail = f"stale {bad_role} candles ({bad_age:.0f}s old)"
                logger.warning(
                    "Refusing to decide on %s (%s): the %s timeframe frame is STALE "
                    "(%.0fs old) while the market is open.",
                    symbol, active_trade_style, bad_role, bad_age,
                )
            else:
                detail = f"unverifiable {bad_role} candle age"
                logger.warning(
                    "Refusing to decide on %s (%s): the %s timeframe frame is stamped "
                    "LIVE_MT5 but its age cannot be verified, so it cannot be shown to "
                    "be current.",
                    symbol, active_trade_style, bad_role,
                )
            # No decision was reached at all. Counted, because "the engine is
            # refusing to decide" is invisible in a log that only shows refusals
            # as warnings, and indistinguishable from a quiet market in the UI.
            DECISION_EVALUATIONS.inc(style=active_trade_style, outcome="refused")
            return {
                "symbol": symbol,
                "trade_style": active_trade_style,
                "decision": None,
                "context": None,
                "authorized": False,
                "auth_reason": detail,
                "dry_run": bool(dry_run),
                "execution": None,
                "stale": True,
            }

        # 1c. Fabricated bars must not be reasoned on either, but only when the
        # broker link is actually up. Gating on `terminal_live()` makes this
        # fire in exactly one case: we have a working terminal and it still
        # answered nothing for this symbol -- i.e. the broker does not offer it
        # (`WTI` sits in the default symbol list and is absent at XM, so the feed
        # was quietly synthesising a WTI series and the platform would have
        # analysed it as if it were real). With the terminal down we keep the
        # synthetic frame so the UI still renders, and execution is already
        # blocked because `get_account_snapshot()` reports login 0.
        #
        # `terminal_live()`, not `terminal_ready()`: the latter latches on
        # success, so a terminal that died mid-session kept this gate firing and
        # every symbol was refused -- the radar emptied and nothing was traded,
        # silently. Measured: 6 opportunities became 0.
        if terminal_live():
            bad_role, bad_source = self._first_unusable_frame(mtf_data)
            if bad_role:
                _key = (symbol, active_trade_style)
                if _key not in self._unusable_warned:
                    self._unusable_warned.add(_key)
                    logger.warning(
                        "Refusing to decide on %s (%s): the %s frame is %s, not real "
                        "market data. This broker does not appear to offer this symbol. "
                        "(Further refusals for this symbol/style are not logged.)",
                        symbol, active_trade_style, bad_role, bad_source,
                    )
                DECISION_EVALUATIONS.inc(style=active_trade_style, outcome="refused")
                return {
                    "symbol": symbol,
                    "trade_style": active_trade_style,
                    "decision": None,
                    "context": None,
                    "authorized": False,
                    "auth_reason": f"no real market data for {bad_role} ({bad_source})",
                    "dry_run": bool(dry_run),
                    "execution": None,
                    "unusable_data": True,
                }
        # 1d. LLM Master Governor Symbol Directive Check
        gov_sym_dir = None
        if hasattr(self, "llm_controller"):
            try:
                gov_directives = self.llm_controller.get_latest_directives()
                gov_sym_dir = gov_directives.symbol_directives.get(symbol)
            except Exception as e:
                logger.debug(f"LLM Governor check failed: {e}")

        _spec = _resolve_sym(symbol)
        
        # 2. Synthesize Multi-Timeframe Market Context with dynamic MTF weighting
        context = self.context_engine.build_context(
            symbol, 
            mtf_data,
            current_spread_pips=_spec.typical_spread_pips,
            max_allowed_spread_pips=_spec.max_spread_pips,
            trade_style=active_trade_style
        )
        self.state_manager.update_market_context(symbol, context)

        # Reporting only: record BOTH the real per-bar spread the feed carried
        # (points→pips, or None if absent) and the registry constant the live
        # gates still use. This is the measurement that tells us how far apart
        # they are. No new MT5 call here (a terminal that is down would hold
        # the GIL); `context.live_spread_pips` comes from the frames we already
        # fetched.
        logger.info(
            "Spread measurement %s [%s]: live=%s pips, registry_typical=%s pips "
            "(live value drives the spread gate when present).",
            symbol, active_trade_style,
            context.live_spread_pips, _spec.typical_spread_pips,
        )

        # 3. Classify Market Regime (thread-safe, isolated per symbol and trade style)
        regime_key = f"{symbol}_{active_trade_style}"
        with self._regime_state_lock:
            prev = self._regime_state.get(regime_key, {})
            prev_reg = prev.get("prev")
            prev_persist = prev.get("persist", 0)

        regime = self.regime_classifier.classify_regime(
            context,
            previous_regime=prev_reg,
            previous_persistence=prev_persist
        )

        with self._regime_state_lock:
            self._regime_state[regime_key] = {
                "prev": regime.primary_regime,
                "persist": regime.regime_persistence
            }

        # 4. Authoritative Canonical Direction Resolution & Dispatch of Sub-Analysts + Devil's Advocate
        tentative_bias = self.decision_engine.resolve_direction(
            context=context,
            regime=regime,
            trade_style=active_trade_style
        )
        if tentative_bias not in ("BUY", "SELL"):
            # Fall back to structural/momentum bias only if market state is neutral/hold
            ts_val = getattr(context.momentum, "trend_score", 0.0) if hasattr(context, "momentum") else 0.0
            st_bias = getattr(context.structure, "bias", "NEUTRAL")
            if st_bias in ("BULLISH", "BEARISH"):
                tentative_bias = "BUY" if st_bias == "BULLISH" else "SELL"
            else:
                tentative_bias = "SELL" if ts_val < 0 else "BUY"

        analyst_reports, devil_report = self.analyst_cluster.run_all_parallel(context, regime, tentative_bias)

        # 5. Evaluate Decision with Expected Value & Quality Gate
        account = self.state_manager.account or self.mt5_client.get_account_snapshot()
        dd_pct = 0.0
        if account and account.balance > 0 and account.equity < account.balance:
            dd_pct = ((account.balance - account.equity) / account.balance) * 100.0
        # Instrumented: `evaluate` is the one call that decides whether money
        # moves, and it was entirely unmeasured — its duration, and how often it
        # raised or refused, were both invisible. `_outcome` starts as "error" so
        # the counter is correct even if the engine raises; the `finally` runs
        # before the exception leaves this block.
        _decision_timer = DECISION_LATENCY.time(style=active_trade_style)
        _outcome = "error"
        try:
            with _decision_timer:
                decision = self.decision_engine.evaluate(
                    context, regime, analyst_reports, devil_report,
                    account_balance=account.equity if account else 10000.0,
                    current_drawdown_pct=dd_pct,
                    mtf_data=mtf_data,
                    recent_candles=self._df_to_candles(mtf_data.get("primary") if isinstance(mtf_data, dict) else None),
                    trade_style=active_trade_style
                )
                _outcome = "trade" if getattr(decision, "execution_authorized", False) else "no_trade"
        finally:
            DECISION_EVALUATIONS.inc(style=active_trade_style, outcome=_outcome)
            log_event(
                logger,
                logging.ERROR if _outcome == "error" else logging.INFO,
                "decision_evaluated",
                f"{symbol}: {_outcome}",
                symbol=symbol,
                style=active_trade_style,
                outcome=_outcome,
                duration_ms=_decision_timer.elapsed_ms,
            )
        self.state_manager.record_decision(symbol, decision)

        # 6. Risk Engine Independent Authorization & Sizing (only if opportunity matches active trading style)
        def _normalize_style(s: str) -> str:
            s = (s or "").upper()
            if s in ("DAY", "INTRADAY", "DAY_TRADING"):
                return "DAY_TRADING"
            if s in ("SCALP", "SCALPING"):
                return "SCALP"
            return s

        orch_style = (self.trade_style or "ALL").upper()
        is_exec_style_match = (orch_style == "ALL") or (_normalize_style(active_trade_style) == _normalize_style(orch_style))

        positions = self.state_manager.positions
        _spec = _resolve_sym(symbol)
        if hasattr(self.mt5_client, "get_symbol_trading_spec"):
            sym_info = self.mt5_client.get_symbol_trading_spec(symbol)
        else:
            sym_info = {
                "name": symbol,
                "trade_contract_size": _spec.contract_size,
                "trade_tick_value": _spec.pip_value_per_lot,
                "trade_tick_size": _spec.pip_size,
                "volume_min": 0.01,
                "volume_max": 100.0,
                "volume_step": 0.01
            }

        # ── Hard Quality Gate: min model_confidence ────────────────────────
        # Adaptive Confidence Gate across styles:
        # Forex & Metals (XAUUSD/XAGUSD) use a realistic 0.45 floor.
        # Scalps with favorable R:R (>=1.3) and EV > 0 use 0.45 floor.
        # Day trades with EV >= 0.3 use 0.48 floor.
        # Setups with all 30 checks passed and EV > 0 use 0.48 floor.
        is_metals = _spec.canonical in ("XAUUSD", "XAGUSD") or _spec.asset_class == "COMMODITY"
        is_forex_or_metals = (_spec.asset_class == "FOREX") or is_metals
        is_crypto_asset = (_spec.asset_class == "CRYPTO") or getattr(_spec, "is_crypto", False)
        is_favorable_scalp = (active_trade_style == "SCALP" and decision.risk_reward_ratio >= 1.3 and decision.expected_value > 0)
        is_favorable_day = (active_trade_style == "DAY_TRADING" and decision.expected_value >= 0.30 and decision.risk_reward_ratio >= 1.4)
        gate_passed = bool(getattr(getattr(decision, "quality_gate", None), "passed", False))

        if is_forex_or_metals or is_favorable_scalp:
            MIN_CONFIDENCE = 0.45
        elif is_favorable_day or (gate_passed and decision.expected_value > 0):
            MIN_CONFIDENCE = 0.48
        elif is_crypto_asset and decision.risk_reward_ratio >= 1.4:
            MIN_CONFIDENCE = 0.50
        else:
            MIN_CONFIDENCE = 0.52
        
        # The calibrated policy, when it is the authority, replaces the legacy
        # stack outright -- including the blunt MIN_CONFIDENCE floor, whose job
        # the profile's out-of-sample-calibrated min_score now does with
        # evidence behind it. Leaving the floor in place would mean the
        # "validated replacement" was still standing behind an unvalidated one,
        # and would silently veto exactly the setups the policy exists to take.
        entry_dec = self._calibrated_entry_decision(symbol, decision, regime)
        entry_override: Optional[bool] = None if entry_dec is None else bool(entry_dec.allowed)

        if not is_exec_style_match:
            auth_res = {"authorized": False, "reason": f"STYLE_FILTER: Opportunity style {active_trade_style} does not match active trading style {orch_style}"}
        elif entry_dec is not None:
            # High-conviction elite setup bypass: If blended AI score is elite (>=80.0), quality gate passed, and positive EV (>=0.40R),
            # allow entry authorization while ensuring no hard capital protection failures
            is_elite_setup = (
                float(getattr(decision, "ai_score", 0.0) or 0.0) >= 80.0
                and gate_passed
                and decision.expected_value >= 0.40
                and len(entry_dec.protection_failures) == 0
            )
            is_auth = bool(entry_dec.allowed) or is_elite_setup
            entry_override = is_auth
            auth_res = {
                "authorized": is_auth,
                "reason": None if is_auth else f"CALIBRATED_ENTRY: {entry_dec.reason}",
            }
            if not is_auth:
                decision.decision = "WAIT"
                decision.execution_authorized = False
            elif is_elite_setup and not entry_dec.allowed:
                decision.decision = "EXECUTE"
        elif decision.decision == "EXECUTE" and decision.model_confidence < MIN_CONFIDENCE:
            decision.decision = "WAIT"
            decision.execution_authorized = False
            auth_res = {"authorized": False, "reason": f"CONFIDENCE_GATE: {decision.model_confidence:.2f} < {MIN_CONFIDENCE} minimum"}
        else:
            auth_res = {"authorized": decision.decision == "EXECUTE"}

        # ── In-process execution lock + 10-min cooldown ────────────────────
        # Fixes race condition where ThreadPoolExecutor fires multiple trades
        # on same symbol before MT5StateSynchronizer 1-second sync catches up.
        canonical_sym = _spec.canonical
        style_cd = self._get_same_symbol_cooldown(active_trade_style)
        with self._execution_lock:
            already_executing = canonical_sym in self._execution_in_progress
            last_exec_time = self._last_execution_time.get(canonical_sym, 0.0)
            cooldown_active = (time.time() - last_exec_time) < style_cd

        # Anti-Clustering Rule: Prevent stacking multiple simultaneous orders on same asset
        active_sym_positions = [
            p for p in positions if (p.symbol == symbol or (symbol == "XAUUSD" and "GOLD" in p.symbol)
                                      or canonical_sym in p.symbol.upper())
        ]
        all_pending_orders = self.mt5_client.get_pending_orders() if hasattr(self.mt5_client, "get_pending_orders") else []
        active_sym_pending = [
            o for o in all_pending_orders if (getattr(o, "symbol", "") == symbol or (symbol == "XAUUSD" and "GOLD" in getattr(o, "symbol", "")) or canonical_sym in getattr(o, "symbol", "").upper())
        ]

        # Asian Pre-Market Blackout Rule (00:00 to 06:00 UTC) & Rollover Spread Window (21:00 to 23:00 UTC)
        now_utc_hour = datetime.now(timezone.utc).hour
        is_asian_pair = any(c in str(symbol).upper() for c in ("JPY", "AUD", "NZD"))
        has_session_restriction = getattr(_prof, "session_restriction", True) if _prof else True
        is_asian_blackout = (0 <= now_utc_hour < 6) and not is_crypto(symbol) and not is_asian_pair and has_session_restriction
        is_rollover_blackout = (21 <= now_utc_hour <= 23) and not is_crypto(symbol) and has_session_restriction
        is_session_blackout = is_asian_blackout or is_rollover_blackout

        # Active Open Position Trailing & Profit Lock Management
        # Suppressed on a dry run. When PositionMonitorEngine is actively running,
        # defer to it to prevent race-condition collisions on MT5 modify_position.
        if not dry_run and not (hasattr(self, "position_monitor") and getattr(self.position_monitor, "_running", False)):
            for pos in active_sym_positions:
                try:
                    manage_res = self.order_manager.manage_position(pos, context)
                    if manage_res.get("should_close"):
                        logger.info(
                            f"⏰ TIME_STOP (inline trail): closing #{pos.ticket} "
                            f"({pos.symbol} {pos.type}) — {manage_res.get('close_reason', '')}"
                        )
                        self.mt5_client.close_position(ticket=pos.ticket)
                        continue
                    if manage_res.get("modified"):
                        self.mt5_client.modify_position(
                            ticket=pos.ticket,
                            sl=manage_res["new_sl"],
                            tp=manage_res["new_tp"]
                        )
                except Exception as e:
                    logger.error(f"Error trailing position #{pos.ticket}: {e}", exc_info=True)

        # These guards are keyed on "this cycle wants to enter", NOT on the
        # legacy verdict. A candidate the calibrated policy accepted can still
        # carry decision.decision == "WAIT", because that is what the legacy
        # stack said about it -- and the calibrated policy exists precisely to
        # take some of those. Keying the in-process lock, the symbol limit, the
        # cooldown and the Asian blackout on the legacy verdict would skip all
        wants_entry = bool(auth_res.get("authorized"))

        # Premium-profile deferred daily entry cap (2026-10-20): computed
        # post-analysis so the always-on scan is never choked before it runs.
        # In-window entries consume the cap normally; the only bypass is an
        # off-window setup whose blended analyst score clears the profile's
        # elite premium floor (88 for gold) — the user directive "a best
        # entry detected off-session must be taken as priority".
        _premium_offwindow_elite = False
        _premium_cap_refused = False
        if _is_premium_profile and wants_entry:
            try:
                _p_window = getattr(_prof, "allowed_utc_hours", None)
                _raw_p_floor = float(getattr(_prof, "premium_out_of_window_min_ai_score", 0.0) or 0.0)
                _p_floor = min(_raw_p_floor, 68.0) if _raw_p_floor > 0 else 65.0
                _now_h = datetime.now(timezone.utc).hour
                _in_window = bool(_p_window and _p_window[0] <= _now_h < _p_window[1])
                _p_score = float(getattr(decision, "ai_score", 0.0) or 0.0)
                # The single definition of an elite off-window setups used by
                # both the cap bypass and the blackout bypass below: session
                # closed, profile opted in, score above the calibrated floor.
                _premium_offwindow_elite = (
                    bool(_p_window)
                    and not _in_window
                    and _p_floor > 0.0
                    and _p_score >= _p_floor
                )
                _post_cap = int(getattr(SETTINGS.trading, "max_daily_entries_per_symbol", 4) or 4)
                if _post_cap > 0 and self._count_entries_today(symbol) >= _post_cap:
                    if _premium_offwindow_elite:
                        logger.info(
                            "PREMIUM_CAP_BYPASS: %s off-window elite setup "
                            "(ai_score %.1f >= floor %.1f) clears the daily entry cap.",
                            symbol, _p_score, _p_floor,
                        )
                    else:
                        _premium_cap_refused = True
                        logger.info(
                            "DAILY_ENTRY_CAP: %s has >= %d entries today; off-window "
                            "entry needs an elite score (ai_score %.1f vs floor %.1f) to run.",
                            symbol, _post_cap, _p_score, _p_floor,
                        )
            except Exception as _cap_exc:
                logger.debug("premium cap gate failed open: %s", _cap_exc)

        # ── Institutional Stop-and-Reverse (SAR) / Position Flip Protocol ───────
        if not dry_run and wants_entry and decision.decision == "EXECUTE":
            active_sym_positions, positions, flipped = self._apply_stop_and_reverse_if_eligible(
                decision=decision,
                context=context,
                active_sym_positions=active_sym_positions,
                positions=positions,
                canonical_sym=canonical_sym,
                entry_override=entry_override
            )
            if flipped:
                cooldown_active = False

        # Elite off-window setups bypass the coarse hour-based blackout veto
        # (pure log marker here; the chain below excludes only the denial arm
        # so the elite candidate keeps flowing into the full authorization
        # stack — price-based spread/friction/risk gates still govern it).
        if is_session_blackout and wants_entry and _premium_offwindow_elite:
            logger.info(
                "PREMIUM_BLACKOUT_BYPASS: %s elite off-window setup (ai_score %.1f) rides "
                "through the session blackout; spread/friction/risk gates still govern it.",
                symbol, float(getattr(decision, "ai_score", 0.0) or 0.0),
            )

        if not is_exec_style_match:
            pass
        elif already_executing and wants_entry:
            decision.decision = "WAIT"
            decision.execution_authorized = False
            auth_res = {"authorized": False, "reason": "IN_PROCESS_LOCK: Execution already in progress for this symbol."}
        elif (len(active_sym_positions) + len(active_sym_pending)) >= 2 and wants_entry:
            decision.decision = "WAIT"
            decision.execution_authorized = False
            auth_res = {"authorized": False, "reason": f"HARD_SYMBOL_LIMIT: Symbol {symbol} already has {len(active_sym_positions)} active positions and {len(active_sym_pending)} pending orders (Max 2)."}
        elif len(active_sym_pending) >= 1 and wants_entry:
            decision.decision = "WAIT"
            decision.execution_authorized = False
            auth_res = {"authorized": False, "reason": f"PENDING_ORDER_EXISTS: Symbol {symbol} already has an active pending order."}
        elif cooldown_active and (len(active_sym_positions) + len(active_sym_pending)) == 0 and wants_entry:
            remaining = int(style_cd - (time.time() - last_exec_time))
            decision.decision = "WAIT"
            decision.execution_authorized = False
            auth_res = {"authorized": False, "reason": f"COOLDOWN_GUARD: {remaining}s remaining before next {canonical_sym} trade."}
        elif is_session_blackout and wants_entry and not _premium_offwindow_elite:
            decision.decision = "WAIT"
            decision.execution_authorized = False
            _reason = "ROLLOVER_BLACKOUT: Broker spread expansion window active (21:00-23:00 UTC)." if is_rollover_blackout else "ASIAN_SESSION_BLACKOUT: Low liquidity chop protection active (00:00-06:00 UTC)."
            auth_res = {"authorized": False, "reason": _reason}
        elif wants_entry and self._is_news_blackout(symbol, datetime.now(timezone.utc)):
            decision.decision = "WAIT"
            decision.execution_authorized = False
            auth_res = {"authorized": False, "reason": "NEWS_BLACKOUT: High-impact macro news release active or within 10m pre / 15m post window."}
        elif _premium_cap_refused:
            decision.decision = "WAIT"
            decision.execution_authorized = False
            auth_res = {
                "authorized": False,
                "reason": "DAILY_ENTRY_CAP: elite off-window premium standard not met and daily entry cap reached",
            }
        elif gov_sym_dir and gov_sym_dir.status == "PAUSED" and wants_entry:
            decision.decision = "WAIT"
            decision.execution_authorized = False
            auth_res = {"authorized": False, "reason": f"LLM_GOVERNOR_PAUSED: {gov_sym_dir.reason}"}
        elif auth_res.get("authorized"):
            # Apply LLM Governor dynamic risk multiplier
            if hasattr(self, "llm_controller"):
                try:
                    gov_directives = self.llm_controller.get_latest_directives()
                    if gov_directives.risk_multiplier != 1.0 and hasattr(decision, "calculated_risk_percent"):
                        decision.calculated_risk_percent = round(
                            max(0.25, min(2.5, decision.calculated_risk_percent * gov_directives.risk_multiplier)), 2
                        )
                except Exception as e:
                    logger.debug(f"LLM Governor risk scaling failed: {e}")

            # Route through Master Adaptive Risk Engine (enforcing all 15 conditions if second trade)
            auth_res = self.risk_engine.authorize_execution(
                decision, account, positions, sym_info,
                current_spread_pips=context.volatility.current_spread_pips,
                max_allowed_spread_pips=_spec.max_spread_pips,
                context=context,
                is_second_trade=(len(active_sym_positions) == 1),
                pending_orders=self.mt5_client.get_pending_orders() if hasattr(self.mt5_client, "get_pending_orders") else [],
                # Only set when the calibrated policy is the authority. None
                # leaves the risk engine reading the legacy verdict, which is
                # what it has always done; True stops the guard re-imposing the
                # legacy veto on a candidate the calibrated policy accepted.
                entry_authorized_override=entry_override,
            )

        # Circuit Breaker check (backstop — also checked inside risk_engine)
        cb_status = self.circuit_breaker.check_status()
        if cb_status.get('active') and decision.decision == 'EXECUTE':
            decision.decision = 'WAIT'
            decision.execution_authorized = False
            auth_res = {'authorized': False, 'reason': f'CIRCUIT_BREAKER: {cb_status.get("reason", "Cooling down")}'}

        # Drawdown Guard check (backstop — also checked inside risk_engine)
        if account:
            dd_status = self.drawdown_guard.check_limits(account.equity, account.balance)
            if not dd_status.get('passed') and decision.decision == 'EXECUTE':
                decision.decision = 'WAIT'
                decision.execution_authorized = False
                reason = dd_status.get("breaches", ["Max drawdown reached"])[0] if dd_status.get("breaches") else "Max drawdown reached"
                auth_res = {'authorized': False, 'reason': f'DRAWDOWN_GUARD: {reason}'}

        # 7. Execute if authorized (Atomic Reservation -> Execute -> Commit/Release)
        # A dry run stops here. Everything above has already decided what WOULD
        # happen and auth_res carries the reason either way, so a preview loses no
        # information by skipping the order itself.
        exec_res = None
        if not dry_run and auth_res.get("authorized") and decision.decision == "EXECUTE":
            decision.execution_authorized = True
            lots = auth_res.get("lots", 0.01)
            # Unified lot cap based on account tier (§4)
            if account:
                _min_vol = float((sym_info or {}).get("volume_min", 0.01) or 0.01)
                lots = min(lots, max(_min_vol, get_max_lot_cap(account.equity)))

            # Claim in-process lock & reserve risk capacity BEFORE sending to MT5
            risk_dist = abs(decision.entry_price - decision.stop_loss)
            tick_v = float(sym_info.get("trade_tick_value", 1.0) or 1.0)
            tick_s = float(sym_info.get("trade_tick_size", 0.0001) or 0.0001)
            dollar_risk_per_unit = tick_v / max(tick_s, 1e-9)
            est_risk_usd = lots * dollar_risk_per_unit * risk_dist
            self.risk_engine.reserve_risk(canonical_sym, est_risk_usd)

            # Claim atomically. The guard ~90 lines above reads
            # `_execution_in_progress` and then RELEASES the lock, so two sweeps
            # can both observe "not executing" and both reach this point. Only
            # the one that wins this compare-and-claim may send an order; the
            # loser releases its risk reservation and skips, instead of opening
            # a second position on the same symbol.
            with self._execution_lock:
                if canonical_sym in self._execution_in_progress:
                    claimed = False
                else:
                    self._execution_in_progress.add(canonical_sym)
                    claimed = True

            if not claimed:
                logger.warning(
                    "%s was claimed by a concurrent sweep while this one was still "
                    "authorising; skipping to avoid a duplicate order.", canonical_sym,
                )
                self.risk_engine.release_risk(canonical_sym)
                exec_res = None
            else:
                try:
                    exec_res = self.execution_engine.execute_decision(decision, lots)
                    status = (exec_res or {}).get("status")
                    if status in ("FILLED", "PLACED"):
                        self.risk_engine.commit_risk(canonical_sym)
                    elif status == "UNKNOWN":
                        # The order may be live at the broker. Keep the risk
                        # reservation held rather than releasing it (releasing
                        # would let another trade spend capacity we may already
                        # be using), and do NOT treat this as a clean refusal:
                        # the next sync reconciles it against real positions.
                        logger.error(
                            "Order for %s TIMED OUT with an unknown outcome -- the "
                            "position may be open. Holding the risk reservation and "
                            "refusing to retry until the broker state is confirmed.",
                            canonical_sym,
                        )
                    else:
                        self.risk_engine.release_risk(canonical_sym)
                except Exception as e:
                    self.risk_engine.release_risk(canonical_sym)
                    logger.error(f"Execution error for {canonical_sym}: {e}", exc_info=True)
                finally:
                    # Always release in-progress lock. Start the cooldown for a
                    # fill, placed pending order, AND for an unknown outcome -- an unconfirmed order
                    # must not be re-sent on the next sweep.
                    with self._execution_lock:
                        self._execution_in_progress.discard(canonical_sym)
                        _st = (exec_res or {}).get("status")
                        if _st in ("FILLED", "UNKNOWN", "PLACED"):
                            self._last_execution_time[canonical_sym] = time.time()
                            logger.info(f"Execution lock released for {canonical_sym}. Cooldown {int(style_cd)}s started.")
            # Record pending features for online learning and journal entry (§17)
            self._record_open_trade_context(
                exec_res=exec_res,
                decision=decision,
                context=context,
                trade_style=active_trade_style,
                lots=lots
            )

        return {
            "symbol": symbol,
            "trade_style": active_trade_style,
            "decision": decision,
            "context": context,
            "authorized": auth_res.get("authorized", False),
            "auth_reason": auth_res.get("reason", ""),
            "dry_run": bool(dry_run),
            "execution": exec_res
        }

    def scan_all_modes(
        self,
        *,
        dry_run: bool = False,
        symbols: Optional[List[str]] = None,
        styles: Optional[List[str]] = None,
    ) -> Tuple[Optional[Any], List[Any], List[Tuple[str, str, Dict[str, Any]]]]:
        """Sweep every (symbol, style) pair, then arbitrate the results.

        Returns ``(best_opportunity, ranked_candidates, raw_results)``.

        This is the ONE place the cross-style fan-out and the arbitration happen.
        The live loop and the read-only preview endpoint both go through it, so a
        preview cannot drift from what the engine actually does — which was the
        whole problem with the previous arrangement, where the arbiter was
        reachable only from inside the loop.

        ``dry_run`` is threaded into every per-symbol cycle, suppressing trailing
        and order submission while leaving the decision and every authorization
        gate intact.
        """
        active_styles = list(styles) if styles else ["SWING", "DAY_TRADING", "SCALP"]
        active_symbols = list(symbols) if symbols else list(self.symbols)

        # 2026-10-20 gold priority: premium-profile symbols (XAUUSD) are
        # submitted FIRST on every sweep so their futures start evaluating
        # before everything else — they are the freshest inputs into the
        # arbiter by completion time, and an actionable gold candidate can
        # never be blocked behind a slower FX sweep.
        try:
            from jarvis.intelligence.symbol_profile_config import SYMBOL_PROFILES as _SP
            _premium_syms = tuple(
                s for s in active_symbols
                if getattr(_SP.get(s.upper(), None), "premium_window_exception", False)
            )
            if _premium_syms:
                active_symbols = [s for s in active_symbols if s.upper() not in _premium_syms]
                active_symbols = list(_premium_syms) + active_symbols
        except Exception:
            pass

        # On weekend market close (Friday 21:00 UTC to Sunday ~21:00 UTC), traditional
        # markets (Forex, Metals, Indices, Oil) are shut. Only scan crypto to avoid
        # hammering MT5 with dead socket calls that stall the worker pool.
        from jarvis.market.data_feed import _is_weekend_gap
        from jarvis.data.symbol_registry import is_crypto
        if symbols is None and _is_weekend_gap(time.time()):
            active_symbols = [s for s in active_symbols if is_crypto(s)]

        tasks = [(sym, style) for style in active_styles for sym in active_symbols]
        raw_results: List[Tuple[str, str, Dict[str, Any]]] = []

        if not tasks:
            return None, [], []

        # Submit to the module-level pool (`_executor`). `as_completed` still
        # waits for every submitted future, so the sweep is complete before the
        # arbitration below — only the pool's lifetime changed, not the work.
        future_to_task = {
            _executor.submit(self.run_cycle_for_symbol, sym, style, dry_run): (sym, style)
            for sym, style in tasks
        }
        try:
            from concurrent.futures import TimeoutError as FuturesTimeoutError
            for fut in as_completed(future_to_task, timeout=15.0):
                sym, style = future_to_task[fut]
                try:
                    res = fut.result(timeout=1.0)
                    raw_results.append((sym, style, res))
                except Exception as e:
                    logger.error(f"Parallel scan error for {sym} ({style}): {e}", exc_info=True)
        except Exception as e:
            logger.warning(f"Parallel scan sweep completed with partial results: {e}")

        # 1. Evaluate every opportunity through the Universal Opportunity Arbiter
        candidates = []
        for sym, style, res in raw_results:
            try:
                d = res["decision"]
                # A cycle that refused to decide (stale candles) has nothing to
                # arbitrate. Without this it would be handed a None decision and
                # raise, logging an error for a condition that was handled.
                if d is None:
                    continue
                ctx = res.get("context")
                cand = self.opportunity_arbiter.evaluate_opportunity(d, ctx, trade_style=style)
                candidates.append(cand)
            except Exception as e:
                logger.error(f"Arbiter evaluation error for {sym} ({style}): {e}", exc_info=True)

        # 2. Rank candidates by Utility score & select best actionable opportunity across styles
        best_opportunity, ranked_candidates = self.opportunity_arbiter.rank_and_select_best(candidates)
        return best_opportunity, ranked_candidates, raw_results

    def _orchestration_loop_single_pass(self, dry_run: bool = False) -> List[Dict[str, Any]]:
        """Executes a single multi-style radar sweep across SWING, DAY_TRADING, and SCALP with Universal Opportunity Arbitration."""
        try:
            self.order_manager.cleanup_stale_pending_orders(max_age_sec=1800)
        except Exception as e:
            logger.debug(f"Stale pending order cleanup pass error: {e}")

        # Scan in dry-run mode so parallel threads do not race to execute before arbitration
        best_opportunity, ranked_candidates, raw_results = self.scan_all_modes(dry_run=True)

        if best_opportunity:
            logger.info(
                f"🏆 Arbiter Top Selection: [{best_opportunity.setup_grade}] {best_opportunity.symbol} "
                f"({best_opportunity.trade_style} {best_opportunity.bias}) | Utility={best_opportunity.utility_score:.3f} | "
                f"WinP={best_opportunity.win_prob:.0f}% | EV={best_opportunity.expected_value:.2f}R | Confluence={best_opportunity.confluence_score:.1f}"
            )

        # Autonomous Multi-Style Execution Dispatch over actionable ranked candidates
        if ranked_candidates and not dry_run:
            for candidate in ranked_candidates:
                d_action = getattr(candidate.decision_obj, "decision", "") if candidate.decision_obj else ""
                qg = getattr(candidate.decision_obj, "quality_gate", None) if candidate.decision_obj else None
                raw_fails = getattr(qg, "failing_reasons", []) or []
                softened_set = {canonical_gate(g) for g in (getattr(qg, "softened_gates", []) or [])}
                unsoftened_fails = [r for r in raw_fails if canonical_gate(r) not in softened_set]
                failing_cnt = len(unsoftened_fails) if qg else 99

                is_exec_ready = (
                    candidate.is_actionable
                    and candidate.setup_grade in ("GRADE A+", "GRADE A", "GRADE B")
                    and candidate.decision_obj
                    and candidate.bias in ("BUY", "SELL")
                    and (
                        d_action == "EXECUTE"
                        or (
                            candidate.setup_grade in ("GRADE A+", "GRADE A")
                            and candidate.expected_value >= 0.40
                            and candidate.risk_reward_ratio >= 1.4
                            and failing_cnt <= 1
                        )
                        or (
                            candidate.setup_grade == "GRADE B"
                            and candidate.expected_value >= 0.40
                            and candidate.risk_reward_ratio >= 1.4
                            and failing_cnt == 0
                        )
                    )
                )

                if not is_exec_ready:
                    continue

                decision = candidate.decision_obj
                sym = candidate.symbol
                canonical_sym = sym.upper().replace("/", "").replace("_", "").replace("-", "")

                # Check if order execution was already fired in run_cycle_for_symbol
                already_fired = False
                for s, st, r in raw_results:
                    if s == sym and st == candidate.trade_style:
                        exec_item = r.get("execution")
                        if exec_item and exec_item.get("status") in ("FILLED", "PLACED"):
                            already_fired = True
                        break

                if not already_fired:
                    with self._execution_lock:
                        if canonical_sym in self._execution_in_progress:
                            already_fired = True

                last_exec = self._last_execution_time.get(canonical_sym, 0)
                cand_cd = self._get_same_symbol_cooldown(getattr(candidate, "trade_style", "SWING"))
                if (time.time() - last_exec) < cand_cd:
                    already_fired = True

                if already_fired:
                    logger.debug(f"Arbiter candidate {canonical_sym} ({candidate.trade_style}) skipped: order already executing or cooldown active.")
                    continue

                account = self.state_manager.account or self.mt5_client.get_account_snapshot()
                positions = self.state_manager.positions
                _spec = _resolve_sym(sym)
                if hasattr(self.mt5_client, "get_symbol_trading_spec"):
                    sym_info = self.mt5_client.get_symbol_trading_spec(sym)
                else:
                    sym_info = {
                        "name": sym,
                        "trade_contract_size": _spec.contract_size,
                        "trade_tick_value": _spec.pip_value_per_lot,
                        "trade_tick_size": _spec.pip_size,
                        "volume_min": 0.01,
                        "volume_max": 100.0,
                        "volume_step": 0.01
                    }
                ctx = candidate.context

                # ── Unsplittable-book dispatch floor (2026-10-21 master evaluation) ──
                # When the whole candidate book would end up as a single
                # all-or-nothing min-lot position (the measured 208 SL vs 52 TP
                # book), sub-2R geometry cannot compound: there is no partial to
                # bank, so the trade is a ratcheted runner against a full -1R
                # stop. Enforce the institutional 2R dispatch floor for those
                # books; split-capable books keep the standard 1.4R gate.
                try:
                    _vol_min = (
                        float(sym_info.get("volume_min", 0.01) or 0.01)
                        if isinstance(sym_info, dict)
                        else float(getattr(sym_info, "volume_min", 0.01) or 0.01)
                    )
                    _equity = float(getattr(account, "equity", 0.0) or 0.0)
                    _entry_px = float(
                        getattr(decision, "entry_price", None)
                        or getattr(decision, "price", 0.0)
                        or 0.0
                    )
                    _sl_px = float(getattr(decision, "stop_loss", 0.0) or 0.0)
                    if self._is_unsplittable_book(_entry_px, _sl_px, _equity, _spec, _vol_min):
                        if decision.risk_reward_ratio < 2.00:
                            logger.info(
                                f"🚫 UNSPLIT_RR_GATE: {sym} {candidate.trade_style} {candidate.bias} "
                                f"RR={decision.risk_reward_ratio:.2f} < 2.00 — all-or-nothing book; "
                                f"sub-2R uncompoundable. Skipping."
                            )
                            continue
                except Exception:
                    pass

                cur_spread = ctx.volatility.current_spread_pips if ctx and hasattr(ctx, "volatility") else _spec.typical_spread_pips

                with self._execution_lock:
                    already_executing = canonical_sym in self._execution_in_progress
                    last_exec_time = self._last_execution_time.get(canonical_sym, 0.0)
                    cooldown_active = (time.time() - last_exec_time) < self._SAME_SYMBOL_COOLDOWN_SEC

                active_sym_positions = [
                    p for p in positions if (p.symbol == sym or (sym == "XAUUSD" and "GOLD" in p.symbol)
                                              or canonical_sym in p.symbol.upper())
                ]
                all_pending_orders = self.mt5_client.get_pending_orders() if hasattr(self.mt5_client, "get_pending_orders") else []
                active_sym_pending = [
                    o for o in all_pending_orders if (getattr(o, "symbol", "") == sym or (sym == "XAUUSD" and "GOLD" in getattr(o, "symbol", "")) or canonical_sym in getattr(o, "symbol", "").upper())
                ]

                # Stop-and-Reverse (SAR) Protocol: If opposing position exists on this symbol, check if eligible to flip
                active_sym_positions, positions, flipped = self._apply_stop_and_reverse_if_eligible(
                    decision=decision,
                    context=ctx,
                    active_sym_positions=active_sym_positions,
                    positions=positions,
                    canonical_sym=canonical_sym,
                    entry_override=True
                )
                if flipped:
                    cooldown_active = False

                if already_executing:
                    logger.info(f"Arbiter skipping {canonical_sym}: execution already in progress.")
                    continue
                if (len(active_sym_positions) + len(active_sym_pending)) >= 2:
                    logger.info(f"Arbiter skipping {canonical_sym}: max symbol limit reached ({len(active_sym_positions)} pos, {len(active_sym_pending)} pending).")
                    continue
                if len(active_sym_pending) >= 1:
                    logger.info(f"Arbiter skipping {canonical_sym}: pending order already resting.")
                    continue
                if cooldown_active and (len(active_sym_positions) + len(active_sym_pending)) == 0:
                    logger.info(f"Arbiter skipping {canonical_sym}: cooldown active.")
                    continue

                decision.decision = "EXECUTE"

                # The calibrated entry policy remains the authority when it has
                # a profile for this symbol. The old code always passed True,
                # which skipped it entirely for arbiter-selected candidates.
                _arb_entry = None
                _arb_regime = getattr(decision, "regime", None)
                if _arb_regime is not None:
                    try:
                        _arb_entry = self._calibrated_entry_decision(
                            canonical_sym, decision, _arb_regime
                        )
                    except Exception as _arb_exc:
                        logger.debug("Arbiter calibrated-entry check failed: %s", _arb_exc)
                        _arb_entry = None
                _is_elite = (
                    float(getattr(decision, "ai_score", 0.0) or 0.0) >= 80.0
                    and getattr(decision, "quality_gate", None)
                    and getattr(decision.quality_gate, "passed", False)
                    and getattr(decision, "expected_value", 0.0) >= 0.40
                    and (not _arb_entry or len(_arb_entry.protection_failures) == 0)
                )
                _arb_entry_override = (
                    None if _arb_entry is None else (bool(_arb_entry.allowed) or _is_elite)
                )

                auth_res = self.risk_engine.authorize_execution(
                    decision, account, positions, sym_info,
                    current_spread_pips=cur_spread,
                    max_allowed_spread_pips=_spec.max_spread_pips,
                    context=ctx,
                    is_second_trade=(len(active_sym_positions) == 1),
                    pending_orders=all_pending_orders,
                    entry_authorized_override=_arb_entry_override
                )

                if auth_res.get("authorized"):
                    if self._is_news_blackout(candidate.symbol, datetime.now(timezone.utc)):
                        logger.info(f"📰 Arbiter selection {candidate.symbol} blocked by NEWS_BLACKOUT pre-trade gate.")
                        continue
                    decision.execution_authorized = True
                    lots = auth_res.get("lots", 0.01)
                    if account:
                        _min_vol = float((sym_info or {}).get("volume_min", 0.01) or 0.01)
                        lots = min(lots, max(_min_vol, get_max_lot_cap(account.equity)))

                    risk_dist = abs(decision.entry_price - decision.stop_loss)
                    tick_v = float(sym_info.get("trade_tick_value", 1.0) or 1.0)
                    tick_s = float(sym_info.get("trade_tick_size", 0.0001) or 0.0001)
                    dollar_risk_per_unit = tick_v / max(tick_s, 1e-9)
                    est_risk_usd = lots * dollar_risk_per_unit * risk_dist
                    self.risk_engine.reserve_risk(canonical_sym, est_risk_usd)

                    with self._execution_lock:
                        self._execution_in_progress.add(canonical_sym)

                    exec_res = None
                    try:
                        logger.info(f"🚀 Autonomous Multi-Style Execution dispatched for {candidate.symbol} ({candidate.trade_style})")
                        exec_res = self.execution_engine.execute_decision(decision, lots)
                        if exec_res and exec_res.get("status") in ("FILLED", "PLACED"):
                            self.risk_engine.commit_risk(canonical_sym)
                            self._record_open_trade_context(
                                exec_res=exec_res,
                                decision=decision,
                                context=ctx,
                                trade_style=candidate.trade_style,
                                lots=lots
                            )
                        else:
                            self.risk_engine.release_risk(canonical_sym)
                    except Exception as e:
                        self.risk_engine.release_risk(canonical_sym)
                        logger.error(f"Arbiter execution error for {canonical_sym}: {e}", exc_info=True)
                    finally:
                        with self._execution_lock:
                            self._execution_in_progress.discard(canonical_sym)
                            if exec_res and exec_res.get("status") in ("FILLED", "UNKNOWN", "PLACED"):
                                self._last_execution_time[canonical_sym] = time.time()
                                logger.info(f"Execution lock released for {canonical_sym}. Cooldown {self._SAME_SYMBOL_COOLDOWN_SEC}s started.")
                    # Single trade executed this pass
                    break
                else:
                    reasons_disp = auth_res.get('reasons') or auth_res.get('reason') or 'Risk constraints'
                    logger.info(f"Arbiter selection {canonical_sym} ({candidate.trade_style}) withheld by risk engine: {reasons_disp}. Evaluating next ranked candidate...")

        # 3. Convert ranked opportunities to radar items for state manager and dashboard
        radar_results = [cand.to_radar_item() for cand in ranked_candidates]

        if radar_results:
            # A1: one canonical radar sort key, shared with server.py — see
            # jarvis/application/radar_sort.py. (Used to be a drifted copy here.)
            radar_results.sort(key=radar_sort_key, reverse=True)
            self.state_manager.update_radar(radar_results)

        return radar_results

    def _orchestration_loop(self):
        """Ultra-fast parallel multi-style radar scan loop (<50ms latency)."""
        while self._running:
            try:
                self._orchestration_loop_single_pass()
            except Exception as e:
                logger.error(f"Orchestration loop error: {e}", exc_info=True)

            try:
                session = SessionEngine.get_current_session()
                # 2026-10-20 latency tuning: measured on this box the full
                # 9-pipeline pass (3 symbols x 3 styles: context + regime +
                # analyst cluster + dissector + gates) took ~1.3-3.5s while the
                # loop slept only 3.0s in prime, so the engine was
                # CPU-saturated nearly continuously and the API/fill paths
                # starved (risk_status replies stretched into seconds).
                # Entries are M5-bar-driven anyway, and position integrity is
                # owned by the state synchroniser / position monitor threads,
                # so a 10s prime cadence does not lose entries but cuts the
                # decision-CPU duty by ~3x. Off-prime has no SCALP activity
                # worth power-splitting; 20s keeps the radar informative.
                sleep_interval = 10.0 if session.is_prime_session else 20.0
            except Exception:
                sleep_interval = 12.0
            time.sleep(sleep_interval)

    def emergency_stop(self, reason: str = "OPERATOR_EMERGENCY_STOP: Trading manually halted by user", close_positions: bool = False) -> Dict[str, Any]:
        """Emergency stop: trips circuit breaker, cancels pending orders, optionally closes positions."""
        self.circuit_breaker.trip(reason)
        if hasattr(self, "risk_engine") and hasattr(self.risk_engine, "circuit_breaker"):
            self.risk_engine.circuit_breaker.trip(reason)

        cancelled_orders = []
        if hasattr(self, "mt5_client") and self.mt5_client:
            cancelled_orders = self.mt5_client.cancel_all_pending_orders()

        closed_positions = []
        if close_positions and hasattr(self, "mt5_client") and self.mt5_client:
            closed_positions = self.mt5_client.close_all_positions()

        logger.warning(f"🚨 EMERGENCY STOP ACTIVATED: {reason} | Cancelled {len(cancelled_orders)} orders | Closed {len(closed_positions)} positions")
        return {
            "status": "SUCCESS",
            "circuit_breaker": "TRIPPED",
            "reason": reason,
            "orders_cancelled": len(cancelled_orders),
            "positions_closed": len(closed_positions)
        }

    def resume_trading(self) -> Dict[str, Any]:
        """Resets circuit breaker and restores trading authorization."""
        self.circuit_breaker.reset()
        if hasattr(self, "risk_engine") and hasattr(self.risk_engine, "circuit_breaker"):
            self.risk_engine.circuit_breaker.reset()
        if hasattr(self, "risk_engine") and hasattr(self.risk_engine, "drawdown_guard"):
            account = None
            if self.mt5_client and hasattr(self.mt5_client, "get_account_snapshot"):
                account = self.mt5_client.get_account_snapshot()
            eq = getattr(account, "equity", 0.0) or (self.state_manager.account.equity if self.state_manager and self.state_manager.account else 0.0)
            if eq > 0:
                self.risk_engine.drawdown_guard.reset_baselines(eq)
                logger.info(f"✅ DRAWDOWN BASELINES RE-ANCHORED to ${eq:,.2f}")
        logger.info("✅ TRADING RESUMED: Circuit breaker and drawdown baselines reset by operator.")
        return {
            "status": "SUCCESS",
            "circuit_breaker": "RESET"
        }
