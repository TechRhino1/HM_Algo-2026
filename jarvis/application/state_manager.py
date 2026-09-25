"""
HM Algo 2.0 — Central State Manager.
Thread-safe, atomic centralized state repository for live telemetry, account records, decisions, and system health.
"""
import os
import threading
from typing import Dict, List, Optional, Any
from datetime import datetime, timezone
from jarvis.data.schemas import (
    AccountSnapshot,
    PositionSnapshot,
    MarketContext,
    DecisionObject,
    ExecutionMode
)

# ── Stream digest (A10) ──────────────────────────────────────────────────────
# Fields dropped from the SSE payload because they are EXPLAINABILITY, not state:
# prose and check breakdowns a human reads once, on a detail view. Measured on the
# live snapshot, removing them takes 63,276 bytes to 18,888 (30%) while every
# price, level, score and grade survives. See `get_state_digest`.
#
# A denylist rather than an allowlist on purpose: a NEW small field passes through
# automatically, so the digest cannot silently fall behind the schema. What the
# denylist cannot do is catch a new *heavy* field — `tests/test_stream_digest.py`
# asserts a byte budget, so that shows up as a failing test instead of a surprise.
STREAM_EXPLAINABILITY_KEYS = frozenset({
    "checks",
    "rejection_reasons",
    "failing_reasons",
    "waiting_reasons",
    "risk_factors",
    "invalidation_levels",
    "mtf_alignment",
    "quality_gate",
    "honest_base_rate",
    "bull_case",
    "bear_case",
})

# Bounds on the stream only. `/api/telemetry_state` stays complete, so the UI is
# unaffected; these exist so the wire cost cannot scale with the scan universe.
STREAM_RADAR_LIMIT = 25
STREAM_DECISION_LIMIT = 25
STREAM_LOG_LIMIT = 20


def _slim_explainability(value: Any) -> Any:
    """Recursively drop the explainability keys, at any depth."""
    if isinstance(value, dict):
        return {k: _slim_explainability(v)
                for k, v in value.items() if k not in STREAM_EXPLAINABILITY_KEYS}
    if isinstance(value, list):
        return [_slim_explainability(v) for v in value]
    return value


class StateManager:
    """Central synchronized in-memory state repository."""
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(StateManager, cls).__new__(cls)
                cls._instance._init_state()
            return cls._instance

    def _init_state(self):
        self._rw_lock = threading.RLock()
        mode_env = os.environ.get("JARVIS_MODE")
        if mode_env:
            try:
                self.execution_mode = ExecutionMode(mode_env.lower())
            except Exception:
                self.execution_mode = ExecutionMode.DEMO
        else:
            try:
                from jarvis.config.settings import SETTINGS
                self.execution_mode = ExecutionMode(SETTINGS.trading.default_mode)
            except Exception:
                self.execution_mode = ExecutionMode.DEMO
        self.trade_style: str = "SWING"
        self.is_safe_mode: bool = False

        self.is_running: bool = True
        self.is_orchestrator_running: bool = False
        
        self.account: Optional[AccountSnapshot] = None
        self.positions: List[PositionSnapshot] = []
        self.market_contexts: Dict[str, MarketContext] = {}
        self.latest_decisions: Dict[str, DecisionObject] = {}
        self.radar_opportunities: List[Dict[str, Any]] = []
        self.services_health: Dict[str, str] = {
            "MT5": "DISCONNECTED",
            "DATA_FEED": "OFFLINE",
            "REGIME_ENGINE": "READY",
            "ANALYST_CLUSTER": "READY",
            "DEVIL_ADVOCATE": "READY",
            "RISK_ENGINE": "ACTIVE",
            "STATE_SYNC": "ONLINE",
            "TELEMETRY_API": "ONLINE"
        }
        self.logs: List[Dict[str, Any]] = []
        self._state_version: int = 0
        self.last_update = datetime.now(timezone.utc)

    def get_state_version(self) -> int:
        with self._rw_lock:
            return self._state_version

    def _bump_version(self):
        self._state_version += 1
        self.last_update = datetime.now(timezone.utc)

    def set_orchestrator_running(self, running: bool):
        with self._rw_lock:
            self.is_orchestrator_running = running

    def is_orchestrator_active(self) -> bool:
        with self._rw_lock:
            return self.is_orchestrator_running

    def set_execution_mode(self, mode: ExecutionMode):
        with self._rw_lock:
            self.execution_mode = mode

    def set_trade_style(self, style: str):
        with self._rw_lock:
            self.trade_style = (style or "SWING").upper()
            self._bump_version()

    def toggle_safe_mode(self) -> bool:
        with self._rw_lock:
            self.is_safe_mode = not self.is_safe_mode
            return self.is_safe_mode

    def update_account(self, account: AccountSnapshot):
        with self._rw_lock:
            self.account = account
            self._bump_version()

    def update_positions(self, positions: List[PositionSnapshot]):
        with self._rw_lock:
            self.positions = positions
            self._bump_version()

    def sync_broker_state(self, account: Optional[AccountSnapshot], positions: List[PositionSnapshot]):
        with self._rw_lock:
            if account:
                self.account = account
            self.positions = positions
            self._bump_version()

    def update_market_context(self, symbol: str, context: MarketContext):
        with self._rw_lock:
            self.market_contexts[symbol] = context
            self._bump_version()

    def get_market_context(self, symbol: str) -> Optional[MarketContext]:
        """Thread-safe retrieval of cached multi-timeframe market context for a symbol."""
        with self._rw_lock:
            return self.market_contexts.get(symbol)

    def record_decision(self, symbol: str, decision: DecisionObject):
        with self._rw_lock:
            self.latest_decisions[symbol] = decision
            self._bump_version()

    def update_radar(self, opportunities: List[Dict[str, Any]]):
        with self._rw_lock:
            self.radar_opportunities = opportunities
            self._bump_version()

    def update_service_health(self, service: str, status: str):
        with self._rw_lock:
            self.services_health[service] = status

    def append_log(self, level: str, message: str, source: str = "SYSTEM"):
        with self._rw_lock:
            entry = {
                "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "level": level,
                "source": source,
                "message": message
            }
            self.logs.append(entry)
            if len(self.logs) > 500:
                self.logs.pop(0)

    def get_state_digest(self) -> Dict[str, Any]:
        """The snapshot minus its explainability payload, for the SSE stream.

        A10: `/api/stream/telemetry` re-sent the FULL snapshot on every state
        change — measured at **63 KB** with 15 radar rows and 5 decisions, pushed
        up to once a second, so ~63 KB/s per client and ~144 KB/s with a couple
        of dashboards open. The bulk is prose: `checks`, `reasons`,
        `risk_factors`, `quality_gate` — fields a human reads once on a detail
        view, not sixty times a minute over an event stream.

        Every *actionable* field survives (prices, levels, scores, grades,
        regime), so a consumer can still rank and act. What is dropped is
        available in full from `/api/telemetry_state`, which the UI already
        polls; the digest says so rather than quietly omitting them.

        `latest_decisions` is capped in the DIGEST only, never in the store:
        `copilot.py` looks up arbitrary symbols in it, so evicting entries there
        would make a real query answer "no decision" for a symbol that has one.
        """
        snap = self.get_state_snapshot()
        radar = [_slim_explainability(v) for v in snap.get("radar_opportunities") or []]
        decisions = {k: _slim_explainability(v)
                     for k, v in list((snap.get("latest_decisions") or {}).items())}
        snap["radar_opportunities"] = radar[:STREAM_RADAR_LIMIT]
        snap["latest_decisions"] = dict(list(decisions.items())[:STREAM_DECISION_LIMIT])
        snap["recent_logs"] = (snap.get("recent_logs") or [])[-STREAM_LOG_LIMIT:]
        snap["digest"] = True
        snap["full_endpoint"] = "/api/telemetry_state"
        snap["omitted_fields"] = sorted(STREAM_EXPLAINABILITY_KEYS)
        snap["dropped"] = {
            "radar_items": max(0, len(radar) - STREAM_RADAR_LIMIT),
            "decisions": max(0, len(decisions) - STREAM_DECISION_LIMIT),
        }
        return snap

    def get_state_snapshot(self) -> Dict[str, Any]:
        """Returns an atomic serialization of current system state for API/UI dashboards."""
        with self._rw_lock:
            acc_dict = self.account.to_dict() if (self.account and hasattr(self.account, "to_dict")) else (self.account.__dict__ if self.account else None)
            pos_list = [p.to_dict() if hasattr(p, "to_dict") else p.__dict__ for p in self.positions]
            dec_dict = {}
            for k, v in self.latest_decisions.items():
                dec_dict[k] = v.to_dict() if hasattr(v, "to_dict") else v.__dict__

            return {
                "execution_mode": self.execution_mode.value,
                "trade_style": getattr(self, "trade_style", "SWING"),
                "safe_mode": self.is_safe_mode,
                "is_running": self.is_running,
                "account": acc_dict,
                "positions_count": len(self.positions),
                "positions": pos_list,
                "services": self.services_health,
                "radar_opportunities": self.radar_opportunities,
                "latest_decisions": dec_dict,
                "recent_logs": self.logs[-50:],
                "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            }

GLOBAL_STATE = StateManager()
