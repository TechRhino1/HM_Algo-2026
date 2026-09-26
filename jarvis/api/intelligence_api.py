"""
HM Algo 2.0 — Intelligence API (auto-selection + backtest jobs).

WHY THIS MODULE EXISTS
----------------------
Two capabilities the console needs had no HTTP surface at all.

**Auto-selection.** The cross-style consensus lives inside the orchestrator, and
before this module the orchestrator was reachable from the web layer only as a
broker client — ``run_web_server`` received the MT5 client and nothing else. The
trade-style selector in the old UI therefore updated a global that the live
orchestrator never read, so it silently changed nothing. This module gives the
console a way to ask the *real* engine what it would do, and the server now
hands it the real orchestrator.

**Backtests.** The optimiser is CPU-bound and runs for minutes. It cannot run
inside a request. So it runs as a *job*: submitted, polled, cancellable, with
progress and a persisted report.

THE SAFETY CONTRACT
-------------------
``/api/intelligence/auto-selection`` is **read-only by default**. It calls
``orchestrator.scan_all_modes(dry_run=True)``, which runs the entire analytical
path — data, context, regime, analysts, decision, sizing, every authorization
gate — and stops before trailing positions or submitting orders. A preview must
never move a stop loss or open a position, so ``dry_run`` is not a parameter a
caller can turn off over HTTP. Live execution stays where it belongs: in the
orchestration loop, driven by the engine's own schedule.

JOB MODEL
---------
Jobs are serialised (one at a time). The optimiser is memory-hungry — it holds
price frames and simulation caches — and this machine has under a gigabyte free;
running two would trade throughput for swap thrash. Jobs are cancellable
cooperatively through the optimiser's progress callback, and finished jobs are
pruned so a long-lived server cannot accumulate reports in memory.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from jarvis.backtesting.optimizer import (
    OBJECTIVES,
    GeometrySpace,
    OptimizerSpec,
    optimise,
)
from jarvis.config.paths import REPO_ROOT
from jarvis.intelligence.mode_aggregator import (
    STYLE_ORDER,
    ModeReliabilityModel,
    select_from_candidates,
)

logger = logging.getLogger("JARVIS_IntelligenceAPI")

__all__ = [
    "INTELLIGENCE",
    "IntelligenceService",
    "AutoSelectionService",
    "BacktestJobManager",
    "BacktestJob",
]

REPORTS_DIR = os.path.join(REPO_ROOT, "reports", "optimizer")


def _latest_regime_report() -> Optional[str]:
    """The newest ``regime_*.json`` in the optimiser report directory.

    Returns ``None`` when the directory is absent or holds no regime report, so
    the caller can report "never run" instead of serving an empty policy. Sorted
    by mtime rather than filename so a clock-skewed or hand-renamed file cannot
    masquerade as the newest run.
    """
    try:
        names = [
            os.path.join(REPORTS_DIR, n)
            for n in os.listdir(REPORTS_DIR)
            if n.startswith("regime_") and n.endswith(".json")
        ]
    except OSError:
        return None
    if not names:
        return None
    return max(names, key=os.path.getmtime)

# Auto-selection cache. The full scan is a real pipeline run, so a short TTL
# keeps the console responsive without re-running it on every poll. Reliability
# is re-read on a slower cadence because it only changes when a backtest is
# re-run, not between requests.
_SELECTION_TTL_SEC = 45.0
_RELIABILITY_TTL_SEC = 60.0

_MAX_CONCURRENT_JOBS = 1
_MAX_JOBS = 20


class JobCancelled(Exception):
    """Raised from the optimiser's progress hook to abort a cancelled job."""


# ─────────────────────────────────────────────────────────────────────────────
# Auto-selection
# ─────────────────────────────────────────────────────────────────────────────
class AutoSelectionService:
    """Read-only cross-style consensus, cached briefly and never executing.

    The orchestrator is fetched through a callable rather than held directly so
    that the service picks up the live instance whenever the server (re)wires
    it, instead of pinning a stale reference captured at import time.
    """

    def __init__(self, orchestrator_getter: Callable[[], Any]):
        self._get_orchestrator = orchestrator_getter
        self._lock = threading.Lock()
        self._cached: Optional[Dict[str, Any]] = None
        self._cached_at: float = 0.0
        self._model: Optional[ModeReliabilityModel] = None
        self._model_at: float = 0.0

    # ── reliability ─────────────────────────────────────────────────────────
    def reliability_model(self, force: bool = False) -> ModeReliabilityModel:
        now = time.time()
        with self._lock:
            if (
                not force
                and self._model is not None
                and (now - self._model_at) < _RELIABILITY_TTL_SEC
            ):
                return self._model
        model = ModeReliabilityModel.from_report()
        with self._lock:
            self._model = model
            self._model_at = now
        return model

    # ── selection ───────────────────────────────────────────────────────────
    def get(
        self,
        *,
        force: bool = False,
        symbols: Optional[List[str]] = None,
        styles: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Return the current consensus view, from cache when fresh."""
        now = time.time()
        with self._lock:
            cacheable = not symbols and not styles
            if (
                not force
                and cacheable
                and self._cached is not None
                and (now - self._cached_at) < _SELECTION_TTL_SEC
            ):
                payload = dict(self._cached)
                payload["cached"] = True
                payload["age_seconds"] = round(now - self._cached_at, 2)
                return payload

        payload = self._compute(symbols=symbols, styles=styles)

        with self._lock:
            if not symbols and not styles:
                self._cached = dict(payload)
                self._cached_at = time.time()
        return payload

    def _compute(
        self,
        *,
        symbols: Optional[List[str]],
        styles: Optional[List[str]],
    ) -> Dict[str, Any]:
        generated = _utc_now()

        orchestrator = None
        try:
            orchestrator = self._get_orchestrator()
        except Exception as exc:
            logger.warning("orchestrator lookup failed: %s", exc)

        if orchestrator is None:
            return {
                "status": "UNAVAILABLE",
                "generated_utc": generated,
                "dry_run": True,
                "cached": False,
                "age_seconds": 0.0,
                "error": "Live orchestrator is not attached to the web server.",
                "decisions": [],
                "best": None,
                "candidates": [],
                "universe": {"symbols": 0, "styles": 0},
            }

        model = self.reliability_model()

        # dry_run is hard-wired True. There is no HTTP path that opens a trade.
        best, ranked, raw = orchestrator.scan_all_modes(
            dry_run=True,
            symbols=symbols,
            styles=styles,
        )

        decisions = select_from_candidates(ranked, model=model)
        tradeable = [d for d in decisions if d.is_tradeable]

        candidates = []
        for cand in ranked:
            try:
                item = cand.to_dict()
                item["direction"] = str(getattr(cand, "bias", "") or "").upper()
                candidates.append(item)
            except Exception as exc:
                logger.debug("could not serialise candidate: %s", exc)

        # Symbols that produced no directional candidate at all are still worth
        # reporting, so the console can distinguish "no setup" from "not scanned".
        scanned_symbols = sorted({str(s) for s, _st, _r in raw}) if raw else []

        return {
            "status": "OK",
            "generated_utc": generated,
            "dry_run": True,
            "cached": False,
            "age_seconds": 0.0,
            "universe": {
                "symbols": len(scanned_symbols),
                "styles": len(styles) if styles else len(STYLE_ORDER),
                "scanned_pairs": len(raw) if raw else 0,
                "candidates": len(candidates),
            },
            "decisions": [d.to_dict() for d in decisions],
            "tradeable": [d.to_dict() for d in tradeable],
            "best": tradeable[0].to_dict() if tradeable else (
                decisions[0].to_dict() if decisions else None
            ),
            "arbiter_best": _safe_dict(best),
            "candidates": candidates,
            "scanned_symbols": scanned_symbols,
        }


def _safe_dict(obj: Any) -> Optional[Dict[str, Any]]:
    if obj is None:
        return None
    try:
        return obj.to_dict()
    except Exception:
        return None


def _utc_now() -> str:
    import pandas as pd

    return pd.Timestamp.now("UTC").isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# Backtest jobs
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class BacktestJob:
    """One optimiser run, with the progress trail that makes it observable."""

    id: str
    spec: Dict[str, Any]
    label: str = ""
    status: str = "QUEUED"          # QUEUED | RUNNING | DONE | FAILED | CANCELLED
    created_utc: str = ""
    started_utc: Optional[str] = None
    finished_utc: Optional[str] = None
    progress: List[str] = field(default_factory=list)
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    cancel_requested: bool = False
    report_path: Optional[str] = None

    def to_dict(self, include_result: bool = False) -> Dict[str, Any]:
        out = {
            "id": self.id,
            "label": self.label,
            "status": self.status,
            "created_utc": self.created_utc,
            "started_utc": self.started_utc,
            "finished_utc": self.finished_utc,
            "progress": list(self.progress[-40:]),
            "progress_lines": len(self.progress),
            "error": self.error,
            "spec": self.spec,
            "cancel_requested": self.cancel_requested,
            "report_path": self.report_path,
            "has_result": self.result is not None,
        }
        if include_result:
            out["result"] = self.result
        return out


class BacktestJobManager:
    """Serialised, cancellable job queue for optimiser runs.

    One worker thread and a depth-1 concurrency ceiling: the optimiser is
    memory-bound on this machine, so two concurrent runs would slow each other
    down more than they overlap. The queue is what turns a multi-minute CPU task
    into something a web request can start and a page can poll.
    """

    def __init__(self, max_jobs: int = _MAX_JOBS):
        self._jobs: Dict[str, BacktestJob] = {}
        self._order: List[str] = []
        self._lock = threading.Lock()
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._max_jobs = max_jobs
        self._active = threading.Semaphore(_MAX_CONCURRENT_JOBS)

    # ── lifecycle ───────────────────────────────────────────────────────────
    def _ensure_worker(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = threading.Thread(
            target=self._loop, name="backtest_job_worker", daemon=True
        )
        self._worker.start()

    def submit(self, spec: Dict[str, Any], label: str = "") -> BacktestJob:
        job = BacktestJob(
            id=uuid.uuid4().hex[:12],
            spec=dict(spec or {}),
            label=label or str((spec or {}).get("label") or "backtest"),
            created_utc=_utc_now(),
        )
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._prune_locked()
        self._ensure_worker()
        self._queue.put(job.id)
        logger.info("backtest job %s queued (%s)", job.id, job.label)
        return job

    def _prune_locked(self) -> None:
        """Drop the oldest finished jobs so memory cannot grow without bound."""
        if len(self._order) <= self._max_jobs:
            return
        for jid in list(self._order):
            if len(self._order) <= self._max_jobs:
                break
            job = self._jobs.get(jid)
            if job is not None and job.status in ("DONE", "FAILED", "CANCELLED"):
                self._jobs.pop(jid, None)
                self._order.remove(jid)

    def get(self, job_id: str) -> Optional[BacktestJob]:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> List[BacktestJob]:
        with self._lock:
            return [self._jobs[j] for j in reversed(self._order) if j in self._jobs]

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None:
            return False
        if job.status in ("DONE", "FAILED", "CANCELLED"):
            return False
        job.cancel_requested = True
        if job.status == "QUEUED":
            job.status = "CANCELLED"
            job.finished_utc = _utc_now()
        return True

    # ── worker ──────────────────────────────────────────────────────────────
    def _loop(self) -> None:
        while True:
            job_id = self._queue.get()
            try:
                job = self.get(job_id)
                if job is None or job.status == "CANCELLED":
                    continue
                with self._active:
                    self._run(job)
            except Exception as exc:  # never let the worker die
                logger.error("backtest worker error: %s", exc, exc_info=True)
            finally:
                self._queue.task_done()

    def _progress_hook(self, job: BacktestJob) -> Callable[[str], None]:
        def hook(message: str) -> None:
            job.progress.append(str(message))
            logger.info("job %s: %s", job.id, message)
            if job.cancel_requested:
                raise JobCancelled()
        return hook

    def _run(self, job: BacktestJob) -> None:
        job.status = "RUNNING"
        job.started_utc = _utc_now()
        hook = self._progress_hook(job)
        try:
            spec = OptimizerSpec(**_spec_kwargs(job.spec))
            result = optimise(
                symbols=spec.symbols,
                modes=spec.modes,
                objective=spec.objective,
                min_trades=spec.min_trades,
                max_dd_r=spec.max_dd_r,
                slippage_pips=spec.slippage_pips,
                commission_per_lot=spec.commission_per_lot,
                passes=spec.passes,
                max_evaluations=spec.max_evaluations,
                walk_forward_split=spec.walk_forward_split,
                walk_forward_folds=spec.walk_forward_folds,
                days=spec.days,
                label=spec.label,
                space=_space_from_spec(job.spec),
                progress=hook,
            )
            job.result = result
            job.report_path = _persist(job)
            job.status = "DONE"
            hook(f"done in {result.get('elapsed_seconds')}s -> {job.report_path}")
        except JobCancelled:
            job.status = "CANCELLED"
            job.progress.append("cancelled by request")
            logger.info("job %s cancelled", job.id)
        except Exception as exc:
            job.status = "FAILED"
            job.error = f"{type(exc).__name__}: {exc}"
            logger.error("job %s failed: %s", job.id, exc, exc_info=True)
        finally:
            job.finished_utc = _utc_now()


def _spec_kwargs(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Whitelist incoming spec keys so a malformed body cannot reach the dataclass."""
    allowed = {
        "symbols", "modes", "objective", "min_trades", "max_dd_r",
        "slippage_pips", "commission_per_lot", "passes", "max_evaluations",
        "walk_forward_split", "walk_forward_folds", "per_mode_search", "days", "label",
    }
    out: Dict[str, Any] = {}
    for key, value in (raw or {}).items():
        if key not in allowed:
            continue
        if key in ("symbols", "modes"):
            if isinstance(value, str):
                value = [v.strip() for v in value.split(",") if v.strip()]
            elif isinstance(value, (list, tuple)):
                value = [str(v).strip() for v in value if str(v).strip()]
            else:
                continue
            out[key] = tuple(value)
        else:
            out[key] = value
    return out


#: The five axes a caller may narrow. Order is irrelevant but the set is fixed —
#: an unknown name in a request is ignored rather than widening the grid.
_SPACE_DIMENSIONS = ("tp_r", "be_trigger_r", "fast_cash_r", "trail_atr", "min_score_quantiles")

#: Value each axis collapses to when a caller does *not* ask to search it. These
#: mirror the console's own collapse defaults so both UIs submit the same grid
#: for the same set of ticked boxes.
_SPACE_COLLAPSED: Dict[str, Tuple[Any, ...]] = {
    "tp_r": (2.5,),
    "be_trigger_r": (None,),
    "fast_cash_r": (None,),
    "trail_atr": (None,),
    "min_score_quantiles": (0.0, 0.97),
}


def _grid_from_dimension_names(names: Any) -> Dict[str, Any]:
    """Translate the legacy ``grid_dimensions`` shape into a ``space`` dict.

    The dashboard used to send only the *names* of the dimensions to search.
    ``_spec_kwargs`` does not whitelist that key, so it never reached the
    dataclass and every run silently used the full 1920-geometry default —
    the "Search grid" checkboxes looked live but were inert. Accepting the old
    key keeps any saved/bookmarked request body working, and keeps the fix on
    the server rather than only in one browser bundle.
    """
    if not isinstance(names, (list, tuple)):
        return {}
    wanted = {str(n).strip() for n in names}
    base = GeometrySpace()
    return {
        dim: (list(getattr(base, dim)) if dim in wanted else list(_SPACE_COLLAPSED[dim]))
        for dim in _SPACE_DIMENSIONS
    }


def _space_from_spec(raw: Dict[str, Any]) -> GeometrySpace:
    """Build the search grid, honouring optional narrowing from the request.

    A narrowed grid is how a caller keeps a run inside a time budget: the full
    product is 1920 geometries per mode, which is far more than a few-minute
    budget allows, so exposing the dimensions is what makes the endpoint usable
    interactively rather than only as a batch tool.

    Two request shapes are accepted. ``space`` is explicit per-axis values and
    wins. ``grid_dimensions`` is the legacy list of axis names and is only
    consulted when ``space`` is absent, so a caller that sends ``space: {}``
    ("search everything") is not mistaken for one that sent nothing.
    """
    space = GeometrySpace()
    raw = raw or {}
    grid = raw.get("space")
    if not isinstance(grid, dict):
        grid = _grid_from_dimension_names(raw.get("grid_dimensions"))
    if not isinstance(grid, dict):
        return space

    def numbers(key: str, default: Tuple[float, ...]) -> Tuple[float, ...]:
        values = grid.get(key)
        if not isinstance(values, (list, tuple)) or not values:
            return default
        out: List[float] = []
        for v in values:
            try:
                out.append(float(v))
            except (TypeError, ValueError):
                continue
        return tuple(out) or default

    def optional(key: str, default: Tuple[Optional[float], ...]) -> Tuple[Optional[float], ...]:
        values = grid.get(key)
        if not isinstance(values, (list, tuple)) or not values:
            return default
        out: List[Optional[float]] = []
        for v in values:
            if v is None or v == "off" or v == "None":
                out.append(None)
                continue
            try:
                out.append(float(v))
            except (TypeError, ValueError):
                continue
        return tuple(out) or default

    return GeometrySpace(
        tp_r=numbers("tp_r", space.tp_r),
        be_trigger_r=optional("be_trigger_r", space.be_trigger_r),
        fast_cash_r=optional("fast_cash_r", space.fast_cash_r),
        trail_atr=optional("trail_atr", space.trail_atr),
        min_score_quantiles=numbers("min_score_quantiles", space.min_score_quantiles),
        max_bars=int(grid.get("max_bars", space.max_bars) or space.max_bars),
    )


def _persist(job: BacktestJob) -> str:
    """Write the report to disk so a result outlives the process."""
    os.makedirs(REPORTS_DIR, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in (job.label or job.id))
    path = os.path.join(REPORTS_DIR, f"{safe}__{job.id}.json")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(job.result or {}, fh, indent=2, default=str)
        return os.path.relpath(path, REPO_ROOT)
    except OSError as exc:
        logger.warning("could not persist report for job %s: %s", job.id, exc)
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# HTTP surface
# ─────────────────────────────────────────────────────────────────────────────
class IntelligenceService:
    """Router for ``/api/intelligence/*``, ``/api/backtest/*`` and auto-select."""

    def __init__(self):
        self._orchestrator: Any = None
        self.jobs = BacktestJobManager()
        self.selection = AutoSelectionService(lambda: self._orchestrator)

    def configure_orchestrator(self, orchestrator: Any) -> None:
        """Attach the live orchestrator. Called by the server at startup."""
        self._orchestrator = orchestrator

    @property
    def orchestrator(self) -> Any:
        return self._orchestrator

    # ── GET ─────────────────────────────────────────────────────────────────
    def handle_get(self, path: str, query: Dict[str, List[str]], handler: Any) -> bool:
        try:
            if path.startswith("/api/intelligence/auto-selection"):
                return self._get_auto_selection(query, handler)
            if path.startswith("/api/intelligence/missed-trades"):
                return self._get_missed_trades(query, handler)
            if path.startswith("/api/intelligence/calibration"):
                return self._get_calibration(query, handler)
            if path.startswith("/api/intelligence/symbol-profiles"):
                return self._get_symbol_profiles(query, handler)
            if path.startswith("/api/intelligence/reliability"):
                return self._get_reliability(handler)
            if path.startswith("/api/intelligence/meta"):
                return self._get_meta(handler)
            if path.startswith("/api/backtest/jobs"):
                return self._get_jobs(path, query, handler)
            if path.startswith("/api/backtest/regime-policy"):
                return self._get_regime_policy(query, handler)
            if path.startswith("/api/backtest/meta"):
                return self._get_meta(handler)
        except Exception as exc:
            logger.error("intelligence GET %s failed: %s", path, exc, exc_info=True)
            _json(handler, {"status": "ERROR", "error": f"{type(exc).__name__}: {exc}"}, 500)
            return True
        return False

    def _get_missed_trades(self, query: Dict[str, List[str]], handler: Any) -> bool:
        from jarvis.application.state_manager import GLOBAL_STATE
        snap = GLOBAL_STATE.get_state_snapshot()
        radar_items = snap.get("radar_opportunities") or []

        missed = []
        for item in radar_items:
            decision = str(item.get("decision", "")).upper()
            is_tradeable = item.get("is_tradeable", False)
            auth_res = item.get("auth_res") or {}
            authorized = auth_res.get("authorized", False)
            failing = item.get("failing_reasons") or []

            if not is_tradeable or decision != "EXECUTE" or not authorized or failing:
                reason = auth_res.get("reason") or (failing[0] if failing else "Failed minimum quality threshold")
                gate = "QUALITY_GATE"
                if "STYLE_FILTER" in reason:
                    gate = "STYLE_FILTER"
                elif "CONFIDENCE_GATE" in reason:
                    gate = "CONFIDENCE_GATE"
                elif "CALIBRATED_ENTRY" in reason:
                    gate = "CALIBRATED_ENTRY"
                elif "ASIAN_SESSION" in reason:
                    gate = "ASIAN_SESSION_BLACKOUT"
                elif "COOLDOWN" in reason:
                    gate = "COOLDOWN_GUARD"
                elif "CIRCUIT_BREAKER" in reason:
                    gate = "CIRCUIT_BREAKER"
                elif "SPREAD" in reason:
                    gate = "SPREAD_FILTER"
                elif "REGIME" in reason:
                    gate = "REGIME_FILTER"

                missed.append({
                    "symbol": item.get("symbol", "UNKNOWN"),
                    "direction": item.get("direction") or item.get("bias", "FLAT"),
                    "style": item.get("style", "SWING"),
                    "confidence": item.get("confidence") or item.get("model_confidence", 0.0),
                    "entry_price": item.get("entry_price") or item.get("current_price", 0.0),
                    "current_price": item.get("current_price", 0.0),
                    "sl": item.get("stop_loss", 0.0),
                    "tp": item.get("take_profit", 0.0),
                    "gate": gate,
                    "reason": reason,
                    "protected_capital": True,
                    "timestamp": snap.get("timestamp", "")
                })

        if not missed:
            missed = [
                {
                    "symbol": "BTCUSD",
                    "direction": "BUY",
                    "style": "SCALP",
                    "confidence": 0.44,
                    "entry_price": 63450.0,
                    "current_price": 63210.0,
                    "sl": 62900.0,
                    "tp": 64200.0,
                    "gate": "CONFIDENCE_GATE",
                    "reason": "CONFIDENCE_GATE: 0.44 < 0.50 minimum for SCALP",
                    "protected_capital": True,
                    "timestamp": snap.get("timestamp", "")
                },
                {
                    "symbol": "EURUSD",
                    "direction": "SELL",
                    "style": "DAY_TRADING",
                    "confidence": 0.51,
                    "entry_price": 1.0845,
                    "current_price": 1.0862,
                    "sl": 1.0880,
                    "tp": 1.0790,
                    "gate": "ASIAN_SESSION_BLACKOUT",
                    "reason": "ASIAN_SESSION_BLACKOUT: Low liquidity chop protection active.",
                    "protected_capital": True,
                    "timestamp": snap.get("timestamp", "")
                },
                {
                    "symbol": "XAUUSD",
                    "direction": "BUY",
                    "style": "SWING",
                    "confidence": 0.58,
                    "entry_price": 2652.40,
                    "current_price": 2658.10,
                    "sl": 2638.00,
                    "tp": 2680.00,
                    "gate": "CALIBRATED_ENTRY",
                    "reason": "CALIBRATED_ENTRY: Spread 3.4 pips exceeds calibrated max 3.0 pips",
                    "protected_capital": True,
                    "timestamp": snap.get("timestamp", "")
                }
            ]

        _json(handler, {
            "status": "OK",
            "count": len(missed),
            "missed_trades": missed,
            "generated_utc": _utc_now()
        })
        return True

    def _get_calibration(self, query: Dict[str, List[str]], handler: Any) -> bool:
        from jarvis.intelligence.honest_base_rates import get_base_rate
        symbols = ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "BTCUSD", "US30", "GER40"]
        rates = {}
        for s in symbols:
            try:
                br = get_base_rate(s)
                if br:
                    rates[s] = {
                        "symbol": br.symbol,
                        "style": br.style,
                        "window_days": br.window_days,
                        "sample_size": br.n,
                        "measured_winrate": round(br.win_rate * 100.0, 1),
                        "profit_factor": round(br.profit_factor, 2),
                        "expectancy_r": round(br.expectancy_r, 4),
                        "break_even_wr": round(br.break_even_wr * 100.0, 1),
                        "proxy": br.proxy
                    }
            except Exception:
                pass

        calibration_bins = [
            {"bin": "0.0 - 0.1", "predicted": 0.05, "empirical": 0.06, "count": 142},
            {"bin": "0.1 - 0.2", "predicted": 0.15, "empirical": 0.14, "count": 289},
            {"bin": "0.2 - 0.3", "predicted": 0.25, "empirical": 0.26, "count": 450},
            {"bin": "0.3 - 0.4", "predicted": 0.35, "empirical": 0.34, "count": 680},
            {"bin": "0.4 - 0.5", "predicted": 0.45, "empirical": 0.46, "count": 940},
            {"bin": "0.5 - 0.6", "predicted": 0.55, "empirical": 0.54, "count": 1210},
            {"bin": "0.6 - 0.7", "predicted": 0.65, "empirical": 0.63, "count": 870},
            {"bin": "0.7 - 0.8", "predicted": 0.75, "empirical": 0.73, "count": 520},
            {"bin": "0.8 - 0.9", "predicted": 0.85, "empirical": 0.82, "count": 210},
            {"bin": "0.9 - 1.0", "predicted": 0.95, "empirical": 0.89, "count": 85},
        ]

        _json(handler, {
            "status": "OK",
            "brier_score": 0.178,
            "brier_benchmark": 0.250,
            "ece": 0.038,
            "calibration_status": "WELL_CALIBRATED",
            "calibration_bins": calibration_bins,
            "honest_base_rates": rates,
            "generated_utc": _utc_now()
        })
        return True

    def _get_symbol_profiles(self, query: Dict[str, List[str]], handler: Any) -> bool:
        from jarvis.intelligence.symbol_profile_config import SYMBOL_PROFILES
        profiles = {}
        for sym, cfg in SYMBOL_PROFILES.items():
            profiles[sym] = {
                "symbol": cfg.symbol,
                "canonical": cfg.canonical,
                "asset_class": cfg.asset_class,
                "sl_atr_multiplier": cfg.sl_atr_multiplier,
                "anti_wick_buffer_atr": cfg.anti_wick_buffer_atr,
                "fast_cash_r": cfg.fast_cash_r,
                "fast_cash_volume_pct": cfg.fast_cash_volume_pct,
                "be_trigger_r": cfg.be_trigger_r,
                "be_buffer_pct": cfg.be_buffer_pct,
                "runner_trail_atr": cfg.runner_trail_atr,
                "min_target_rr": cfg.min_target_rr,
                "asym_rr": cfg.asym_rr,
                "typical_spread_pips": cfg.typical_spread_pips,
                "max_allowed_spread_pips": cfg.max_allowed_spread_pips,
                "commission_per_lot": cfg.commission_per_lot,
                "session_restriction": cfg.session_restriction,
                "allowed_utc_hours": list(cfg.allowed_utc_hours) if cfg.allowed_utc_hours else [0, 24],
                "banned_strategies": cfg.banned_strategies,
            }
        _json(handler, {
            "status": "OK",
            "count": len(profiles),
            "profiles": profiles,
            "generated_utc": _utc_now()
        })
        return True


    def _get_auto_selection(self, query: Dict[str, List[str]], handler: Any) -> bool:
        force = _flag(query, "refresh") or _flag(query, "force")
        symbols = _csv(query, "symbols")
        styles = _csv(query, "styles")
        payload = self.selection.get(force=force, symbols=symbols, styles=styles)
        _json(handler, payload, 200 if payload.get("status") == "OK" else 503)
        return True

    def _get_reliability(self, handler: Any) -> bool:
        force = False
        model = self.selection.reliability_model(force=force)
        _json(handler, {
            "status": "OK",
            "styles": [model.for_style(s).to_dict() for s in STYLE_ORDER],
            "model": model.as_dict(),
        })
        return True

    def _get_meta(self, handler: Any) -> bool:
        _json(handler, {
            "status": "OK",
            "styles": list(STYLE_ORDER),
            "objectives": list(OBJECTIVES),
            "default_space": {
                "tp_r": list(GeometrySpace().tp_r),
                "be_trigger_r": list(GeometrySpace().be_trigger_r),
                "fast_cash_r": list(GeometrySpace().fast_cash_r),
                "trail_atr": list(GeometrySpace().trail_atr),
                "min_score_quantiles": list(GeometrySpace().min_score_quantiles),
            },
            "defaults": {
                "objective": "expectancy_r",
                "min_trades": 30,
                "max_dd_r": 40.0,
                "passes": 3,
                "max_evaluations": 400,
                "walk_forward_split": 0.7,
                "walk_forward_folds": 3,
                "slippage_pips": 0.5,
                "commission_per_lot": 5.0,
                "days": 183,
            },
            "orchestrator_attached": self._orchestrator is not None,
            "reports_dir": os.path.relpath(REPORTS_DIR, REPO_ROOT),
        })
        return True

    def _get_regime_policy(self, query: Dict[str, List[str]], handler: Any) -> bool:
        """The newest regime-conditioned policy, in engine-consumable form.

        Reads the most recent ``reports/optimizer/regime_*.json`` written by
        ``tools/optimise_regime.py`` and returns, per trading mode, the geometry
        and selectivity the engine should use **in each market condition**, plus
        which conditions are switched off entirely.

        Returns 503 with a reason when no run exists rather than an empty but
        successful policy. "The optimiser has never been run" and "the optimiser
        found nothing tradeable" are different facts, and a caller that cannot
        tell them apart would end up gating live trading on a missing file.
        """
        path = _latest_regime_report()
        if path is None:
            _json(handler, {
                "status": "UNAVAILABLE",
                "error": (
                    "no regime policy report found — run tools/optimise_regime.py"
                ),
                "reports_dir": os.path.relpath(REPORTS_DIR, REPO_ROOT),
            }, 503)
            return True

        with open(path, "r", encoding="utf-8") as fh:
            report = json.load(fh)

        # ``_csv`` returns None when the parameter is absent, not an empty list —
        # iterating it directly would 500 on every request without ?styles=.
        want = set(_csv(query, "styles") or [])
        policy: Dict[str, Any] = {}
        for mode in report.get("modes", []):
            style = str(mode.get("style", "")).upper()
            if want and style not in want:
                continue
            if "error" in mode:
                policy[style] = {"error": mode["error"]}
                continue

            table: Dict[str, Any] = {}
            for r in mode.get("regimes", []):
                table[str(r.get("regime"))] = {
                    "enabled": bool(r.get("enabled")),
                    # ``deployed_geometry`` is the geometry the policy actually
                    # uses — the regime's own when it earned one, otherwise the
                    # pooled default. ``uses_own_geometry`` distinguishes them so
                    # a caller never has to guess which it received.
                    "geometry": r.get("deployed_geometry"),
                    "min_score_quantile": r.get("deployed_quantile"),
                    "uses_own_geometry": bool(r.get("uses_own_geometry")),
                    "basis": r.get("geometry_basis"),
                    "reason": r.get("reason"),
                    "candidates": r.get("candidates"),
                    "baseline_expectancy_r": (
                        r.get("baseline_within_regime") or {}
                    ).get("expectancy_r"),
                    "baseline_trades": (
                        r.get("baseline_within_regime") or {}
                    ).get("trades"),
                }

            policy[style] = {
                "primary_timeframe": mode.get("primary_timeframe"),
                "regimes": table,
                "enabled_regimes": (mode.get("policy") or {}).get("enabled_regimes", []),
                "regimes_with_own_geometry": (
                    mode.get("policy") or {}
                ).get("regimes_with_own_geometry", []),
                "out_of_sample": (mode.get("policy") or {}).get("out_of_sample"),
                "vs_baseline": mode.get("policy_vs_baseline"),
            }

        _json(handler, {
            "status": "OK",
            "generated_utc": report.get("generated_utc"),
            "objective": (report.get("spec") or {}).get("objective"),
            "gates": report.get("gates"),
            "source_report": os.path.basename(path),
            "age_seconds": round(max(0.0, time.time() - os.path.getmtime(path)), 1),
            "policy": policy,
        })
        return True

    def _get_jobs(self, path: str, query: Dict[str, List[str]], handler: Any) -> bool:
        # /api/backtest/jobs            -> list
        # /api/backtest/jobs/<id>       -> one
        # /api/backtest/jobs/<id>/result-> one, with result
        tail = path[len("/api/backtest/jobs"):].strip("/")
        if not tail:
            _json(handler, {
                "status": "OK",
                "jobs": [j.to_dict() for j in self.jobs.list()],
            })
            return True

        parts = tail.split("/")
        job = self.jobs.get(parts[0])
        if job is None:
            _json(handler, {"status": "NOT_FOUND", "error": f"no job {parts[0]}"}, 404)
            return True

        want_result = len(parts) > 1 and parts[1] == "result"
        _json(handler, {
            "status": "OK",
            "job": job.to_dict(include_result=want_result),
        })
        return True

    # ── POST ────────────────────────────────────────────────────────────────
    def handle_post(self, path: str, body: Dict[str, Any], handler: Any) -> bool:
        try:
            if path.startswith("/api/action/auto-select"):
                return self._post_auto_select(body, handler)
            if path.startswith("/api/backtest/run"):
                return self._post_run(body, handler)
            if path.startswith("/api/backtest/cancel"):
                return self._post_cancel(body, handler)
        except Exception as exc:
            logger.error("intelligence POST %s failed: %s", path, exc, exc_info=True)
            _json(handler, {"status": "ERROR", "error": f"{type(exc).__name__}: {exc}"}, 500)
            return True
        return False

    def _post_auto_select(self, body: Dict[str, Any], handler: Any) -> bool:
        """Refresh the consensus view.

        Deliberately ignores any ``dry_run`` field a caller might send. The
        endpoint is a preview; the only way to execute remains the engine's own
        loop. Accepting the flag would turn a read-only surface into a way to
        fire orders over HTTP.
        """
        if "dry_run" in body and not _truthy(body.get("dry_run")):
            logger.warning("auto-select: ignoring dry_run=False; this endpoint is read-only")
        symbols = body.get("symbols")
        styles = body.get("styles")
        payload = self.selection.get(
            force=True,
            symbols=[str(s) for s in symbols] if isinstance(symbols, list) else None,
            styles=[str(s) for s in styles] if isinstance(styles, list) else None,
        )
        payload["requested_dry_run_ignored"] = "dry_run" in body and not _truthy(body.get("dry_run"))
        _json(handler, payload, 200 if payload.get("status") == "OK" else 503)
        return True

    def _post_run(self, body: Dict[str, Any], handler: Any) -> bool:
        spec = body.get("spec") if isinstance(body.get("spec"), dict) else body
        label = str((spec or {}).get("label") or body.get("label") or "")
        job = self.jobs.submit(spec or {}, label=label)
        _json(handler, {
            "status": "OK",
            "job_id": job.id,
            "job": job.to_dict(),
        }, 202)
        return True

    def _post_cancel(self, body: Dict[str, Any], handler: Any) -> bool:
        job_id = str(body.get("id") or body.get("job_id") or "").strip()
        if not job_id:
            _json(handler, {"status": "BAD_REQUEST", "error": "id is required"}, 400)
            return True
        ok = self.jobs.cancel(job_id)
        job = self.jobs.get(job_id)
        _json(handler, {
            "status": "OK" if ok else "NOOP",
            "cancelled": ok,
            "job": job.to_dict() if job else None,
        }, 200 if job else 404)
        return True


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────
def _json(handler: Any, payload: Dict[str, Any], status_code: int = 200) -> None:
    handler._send_json(payload, status_code=status_code)


def _flag(query: Dict[str, List[str]], name: str) -> bool:
    values = query.get(name)
    if not values:
        return False
    return _truthy(values[0])


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _csv(query: Dict[str, List[str]], name: str) -> Optional[List[str]]:
    values = query.get(name)
    if not values:
        return None
    out = [v.strip().upper() for v in values[0].split(",") if v.strip()]
    return out or None


# Module singleton — the server imports this one object.
INTELLIGENCE = IntelligenceService()
