"""
Live strategy-expectancy quarantine.

The 2026-10-20 ledger audit showed negative net P&L on every symbol and every
strategy (demo, ~376 closed trades, expectancy -0.44R): with 31% winners that
book <= +0.6R against -1R stops, no static profile ban list could keep up, and
the Thompson-sampled bandit re-weights far too slowly to stop a style bleeding
tens of dollars on a micro account.

This module reads the SAME canonical demo ledger the engine already writes
(``Trade #...`` rows in ``executed_trades``, via ``jarvis.data.database``) and
answers one question a decision can check synchronously:

    *has the live book produced a materially negative expectancy for this
    (symbol, strategy) after a real sample, or not?*

It is a *trailing performance governor*, not a prediction: it only ever
disables a strategy × symbol pair that has already burned the account in the
demo ledger, and only once the sample is large enough (default: >= 20 usable
closed trades and <= -0.25 R per trade).

NOTES
-----
* Only demo-mode closed rows with a computable R multiple enter the sample.
  The R multiple is signed price progress over |entry - sl| per trade, which
  is scale-free across gold, crypto, FX and indices.
* Rows whose realized_pnl is NULL (never recorded) are ignored, not zero —
  the 42 "unknown-PnL" rows must never silently flip a stat to +0.
* The result is cached for ``CACHE_TTL_SEC``; a read path calls sqlite
  read-only, never writes, so it cannot corrupt the live writer.
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
import time
from typing import Dict, List, Optional, Tuple

try:
    from jarvis.data.database import TRADE_DB as _CANONICAL_DB
except Exception:  # pragma: no cover - the DB is always importable in engine
    _CANONICAL_DB = None

logger = logging.getLogger("JARVIS_StrategyQuarantine")

CACHE_TTL_SEC = 300.0
DEFAULT_MIN_TRADES = 20
DEFAULT_MIN_EXPECTANCY_R = -0.25

# 2026-10-20 graduation: quarantine is discipline, not life-imprisonment.
# A quarantined pair earns back the right to trade when its MOST RECENT
# GRADUATION_RECENT_N closed trades (in ledger insertion order, i.e. real
# chronological order) hold expectancy >= GRADUATION_RECENT_MIN_R. That is a
# demonstrable changed behaviour, not a "maybe it recovered" hunch: surprise
# recovery is measured on the freshest cohort only, while the aggregate stays
# negative (that is what put the pair away).
GRADUATION_RECENT_N = 8
GRADUATION_RECENT_MIN_R = 0.05

# 2026-10-20 learned sizing: the ledger does not only say "stop" or "go", it
# says HOW MUCH to bet. Positive live expectancy earns a modest size boost,
# mildly negative earns a haircut, established losers are cut to zero (they
# should already be quarantined; the 0.0 is a safety net, not the normal path).
# Bounded in [0.0, 1.15]: the engine must never reclaim risk faster than the
# edge demonstrates itself, and never shrink below the min-lot fallback rules
# that protect the profit booking.
SIZING_MIN_TRADES = 6
SIZE_MULT_FOR_NEGATIVE = 0.80   # expectancy <= -0.05R
SIZE_MULT_FOR_NEUTRAL = 1.00    # -0.05R < expectancy < +0.10R
SIZE_MULT_FOR_POSITIVE = 1.15   # expectancy >= +0.10R

_CACHE: Dict[str, object] = {}
_CACHE_LOCK = threading.Lock()


def _db_path(db_path: Optional[str]) -> Optional[str]:
    if db_path:
        return db_path
    if _CANONICAL_DB is None:
        return None
    path = getattr(_CANONICAL_DB, "db_path", None)
    if not path or path == ":memory:":
        return None
    return path


def load_live_expectancy(db_path: Optional[str] = None) -> Dict[Tuple[str, str], Tuple[int, float]]:
    """Return {(symbol, strategy): (n, expectancy_r)} over the demo ledger.

    Read-only, cached for ``CACHE_TTL_SEC``. ``strategy`` defaults to "?" when
    the row's features_json does not record one (the "no strategy" bucket is
    never quarantined automatically — there is nothing reliable to blame).
    """
    return _load_full(db_path)[0]


def _load_full(
    db_path: Optional[str] = None,
) -> Tuple[Dict[Tuple[str, str], Tuple[int, float]], Dict[Tuple[str, str], List[float]]]:
    """Build (stats, details) where details keeps per-pair R values in trade order.

    The rows are read with ``ORDER BY id ASC``: the ledger is append-only, so
    id order is chronological close order — exactly what graduation needs to
    look at the freshest cohort.
    """
    key = str(db_path or _db_path(None))
    now = time.monotonic()
    with _CACHE_LOCK:
        cached_path = _CACHE.get("path")
        cached_at = float(_CACHE.get("at", 0.0))
        if cached_path == key and (now - cached_at) < CACHE_TTL_SEC:
            return _CACHE.get("stats", {}), _CACHE.get("details", {})

    stats: Dict[Tuple[str, str], Tuple[int, float]] = {}
    details: Dict[Tuple[str, str], List[float]] = {}
    path = _db_path(db_path)
    if not path:
        return stats, details

    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
        try:
            rows = conn.execute(
                """
                SELECT symbol, action, entry_price, exit_price, sl, tp,
                       realized_pnl, features_json
                FROM executed_trades
                WHERE execution_mode = 'demo'
                  AND closed_at IS NOT NULL
                  AND realized_pnl IS NOT NULL
                ORDER BY id ASC
                """
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        # The live engine holds this DB in WAL and writes trades while we
        # read. A transient lock must not flip a quarantine verdict that a
        # retry a second later would contradict — serve the last good
        # snapshot (however old) instead of an empty one. An empty result is
        # only ever cached by a successful read of an actually-empty ledger.
        logger.warning("StrategyQuarantine read failed for %s: %s", path, exc)
        with _CACHE_LOCK:
            if (_CACHE.get("path") == key
                    and isinstance(_CACHE.get("stats"), dict)
                    and isinstance(_CACHE.get("details"), dict)):
                return _CACHE["stats"], _CACHE["details"]
        return stats, details

    sums: Dict[Tuple[str, str], list] = {}
    for symbol, action, entry, exit_price, sl, _tp, _pnl, features_json in rows:
        try:
            if not entry or not exit_price or not sl:
                continue
            risk = abs(float(entry) - float(sl))
            if risk <= 1e-6 or math.isnan(risk) or math.isinf(risk):
                continue
            if str(action).upper().startswith("BUY"):
                r_i = (float(exit_price) - float(entry)) / risk
            else:
                r_i = (float(entry) - float(exit_price)) / risk
            sym = str(symbol or "").upper().strip()
            if not sym:
                continue
            try:
                strat = str((json.loads(features_json or "{}") or {}).get("strategy") or "?")
            except Exception:
                strat = "?"
            sums.setdefault((sym, strat), []).append(r_i)
        except Exception:
            continue

    for pair, rs in sums.items():
        if rs:
            stats[pair] = (len(rs), sum(rs) / len(rs))
            details[pair] = list(rs)

    with _CACHE_LOCK:
        _CACHE["path"] = key
        _CACHE["at"] = now
        _CACHE["stats"] = stats
        _CACHE["details"] = details
    return stats, details


def is_quarantined(
    symbol: str,
    strategy: str,
    *,
    min_trades: int = DEFAULT_MIN_TRADES,
    min_expectancy_r: float = DEFAULT_MIN_EXPECTANCY_R,
    db_path: Optional[str] = None,
) -> bool:
    """True when the live demo ledger shows a losing edge for symbol × strategy.

    A strategy without a usable sample is never quarantined (fails open), and
    catastrophic single-hit styles (e.g. one -10.95R "?" row) can never reach
    the sample floor — this guard only condemns *established* losers.

    In offline/backtest mode this returns False unconditionally: the ledger is
    live disk state, and a simulation decided by trades closed after its own
    window is exactly the lookahead bias ``tests/test_backtest_is_hermetic.py``
    exists to prevent. The hermetic harness neutralises the learned
    Singletons (OnlineMLPredictor, SelfLearningEngine, ...) but cannot know
    about this read, so the guard must take itself out of the sim.
    """
    if not strategy or strategy == "?":
        return False
    try:
        from jarvis.config.runtime import is_offline

        if is_offline():
            return False
    except Exception:
        pass
    stats = load_live_expectancy(db_path=db_path)
    rec = stats.get((str(symbol or "").upper().strip(), str(strategy)))
    if rec is None:
        return False
    n, exp_r = rec
    if n < min_trades or exp_r > min_expectancy_r:
        return False

    # Graduation check before condemning: has the pair's freshest cohort
    # (last GRADUATION_RECENT_N closed trades) demonstrated a recovered
    # edge? If so it re-enters Play — allowed to trade again, subject to a
    # neutral size multiplier until the aggregate turns around as well.
    details = _load_full(db_path)[1]
    rs = details.get((str(symbol or "").upper().strip(), str(strategy))) or ()
    if len(rs) >= GRADUATION_RECENT_N:
        recent = list(rs)[-GRADUATION_RECENT_N:]
        recent_exp = sum(recent) / len(recent)
        if recent_exp >= GRADUATION_RECENT_MIN_R:
            logger.info(
                "StrategyQuarantine graduation: %s | %s stays tradeable — aggregate "
                "expectancy %+.3fR (n=%d) but the last %d trades average %+.3fR.",
                str(symbol).upper(), strategy, exp_r, n, len(recent), recent_exp,
            )
            return False
    return True


def expectancy_state(
    symbol: str,
    strategy: str,
    db_path: Optional[str] = None,
) -> Dict[str, object]:
    """Full learned-state snapshot for one (symbol, strategy) pair.

    ``{n, expectancy_r, recent_n, recent_expectancy_r, quarantined,
    graduated, size_multiplier}``. Recent stats cover the last
    ``GRADUATION_RECENT_N`` closed trades; ``expectancy_r`` is None when the
    pair has no usable sample. Used by dashboards/ops and by the sizing
    multiplier itself.
    """
    empty = {
        "n": 0,
        "expectancy_r": None,
        "recent_n": 0,
        "recent_expectancy_r": None,
        "quarantined": False,
        "graduated": False,
        "size_multiplier": 1.0,
    }
    sym = str(symbol or "").upper().strip()
    strat = str(strategy or "").strip()
    if not sym or not strat or strat == "?":
        return empty
    stats, details = _load_full(db_path)
    rec = stats.get((sym, strat))
    if rec is None:
        return empty
    n, exp_r = rec
    rs = list(details.get((sym, strat)) or [])
    recent_n = min(len(rs), GRADUATION_RECENT_N)
    recent_exp = (sum(rs[-GRADUATION_RECENT_N:]) / recent_n) if recent_n else None

    quarantined = is_quarantined(sym, strat, db_path=db_path)
    # "graduated" describes the pair the aggregate currently condemns but the
    # recent cohort says recover. A pair that was never quarantined is not
    # "graduated" — it simply was never shut out.
    aggregate_condemns = n >= DEFAULT_MIN_TRADES and exp_r <= DEFAULT_MIN_EXPECTANCY_R
    graduated = bool(quarantined is False and aggregate_condemns
                     and recent_n >= GRADUATION_RECENT_N
                     and recent_exp is not None
                     and recent_exp >= GRADUATION_RECENT_MIN_R)

    return {
        "n": n,
        "expectancy_r": round(float(exp_r), 4),
        "recent_n": recent_n,
        "recent_expectancy_r": round(float(recent_exp), 4) if recent_exp is not None else None,
        "quarantined": bool(quarantined),
        "graduated": graduated,
        "size_multiplier": expectancy_risk_multiplier(sym, strat, db_path=db_path),
    }


def expectancy_risk_multiplier(
    symbol: str,
    strategy: str,
    db_path: Optional[str] = None,
) -> float:
    """Learned size multiplier in [0.0, 1.15] for one (symbol, strategy).

    Rewarded when the live book shows a real positive expectancy, haircut
    when mildly negative, zero for a pair that fails the quarantine
    graduation and stays condemned. Thin samples (< SIZING_MIN_TRADES
    usable closes) and offline/backtest contexts return 1.0 — the engine
    learns from what it actually traded, never from absence of evidence.
    """
    base = 1.0
    sym = str(symbol or "").upper().strip()
    strat = str(strategy or "").strip()
    if not sym or not strat or strat == "?":
        return base
    try:
        from jarvis.config.runtime import is_offline

        if is_offline():
            return base
    except Exception:
        pass
    stats, _details = _load_full(db_path)
    rec = stats.get((sym, strat))
    if rec is None:
        return base
    n, exp_r = rec
    if n < SIZING_MIN_TRADES:
        return base
    if is_quarantined(sym, strat, db_path=db_path):
        # Condemned pairs must not trade at all; if some path still reaches
        # sizing with one, zero it out rather than fund it.
        return 0.0
    if exp_r <= -0.05:
        return SIZE_MULT_FOR_NEGATIVE
    if exp_r >= 0.10:
        return SIZE_MULT_FOR_POSITIVE
    return SIZE_MULT_FOR_NEUTRAL


def quarantine_report(
    symbol: str = "",
    *,
    min_trades: int = DEFAULT_MIN_TRADES,
    min_expectancy_r: float = DEFAULT_MIN_EXPECTANCY_R,
    db_path: Optional[str] = None,
) -> Dict[str, Dict[str, float]]:
    """Diagnostic dump for dashboards/ops: n and expectancy_r per (symbol)[×strategy]."""
    stats = load_live_expectancy(db_path=db_path)
    out: Dict[str, Dict[str, float]] = {}
    for (sym, strat), (n, exp_r) in stats.items():
        if symbol and sym != str(symbol).upper().strip():
            continue
        out[f"{sym}|{strat}"] = {"n": n, "expectancy_r": round(exp_r, 4),
                                 "quarantined": bool(n >= min_trades and exp_r <= min_expectancy_r)}
    return out


def clear_cache() -> None:
    """Force the next is_quarantined() to re-read the ledger (used in tests)."""
    with _CACHE_LOCK:
        _CACHE.clear()


__all__ = [
    "load_live_expectancy",
    "is_quarantined",
    "expectancy_state",
    "expectancy_risk_multiplier",
    "quarantine_report",
    "clear_cache",
    "DEFAULT_MIN_TRADES",
    "DEFAULT_MIN_EXPECTANCY_R",
    "GRADUATION_RECENT_N",
    "GRADUATION_RECENT_MIN_R",
    "SIZING_MIN_TRADES",
    "SIZE_MULT_FOR_NEGATIVE",
    "SIZE_MULT_FOR_NEUTRAL",
    "SIZE_MULT_FOR_POSITIVE",
    "CACHE_TTL_SEC",
]
