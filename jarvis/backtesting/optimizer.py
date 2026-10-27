"""
HM Algo 2.0 — Backtest Optimiser (maximum-profit geometry search on real MT5 data).

WHY THIS MODULE EXISTS
----------------------
The calibrator answers "which geometry hits a target win rate on this symbol?".
That is a *constraint satisfaction* question, and it is the wrong question when
the goal is profit. A geometry can be the best win-rate match on a symbol and
still lose money, because win rate and expectancy are not the same objective and
the target itself was chosen by a heuristic.

This module asks the direct question instead: **over the real 6-month MT5
history, which entry-selectivity + exit-geometry configuration produces the most
money, subject to constraints that keep the answer honest?**

THE FOUR IDEAS THAT MAKE THE ANSWER TRUSTWORTHY
-----------------------------------------------
1. **Simulate once, threshold for free.**
   A trade's outcome depends on the geometry and the forward price path — never
   on which other trades were taken. ``min_score`` only decides which simulated
   trades are *kept*. So the expensive simulation is cached per geometry (keyed
   by everything except ``min_score``) and every selectivity threshold is then a
   cheap filtering pass over the same outcomes. A
   ``geometries x thresholds`` grid therefore costs ``geometries`` simulations,
   not ``geometries x thresholds``. This is the only reason a full-universe
   search is affordable on this machine.

2. **Costs are charged, not assumed away.**
   ``simulate_all_candidates`` defaults to zero commission and zero slippage. A
   search run with those defaults would select the geometry that best exploits
   the omission — typically the one with the most stop-outs — and report an edge
   the engine cannot realise. Round-turn commission is converted to a price
   distance (``commission_per_lot / money_per_unit``) and stop slippage to
   ``slippage_pips * pip_size``, both taken from the same conventions
   ``BacktestEngine`` uses.

3. **The search window and the validation window are disjoint.**
   The optimum is found on the first ``walk_forward_split`` of the history and
   then measured, untouched, on the remainder. Reporting the best in-sample
   number as if it were predictive is the classic way to ship a curve-fit; the
   held-out number is the only one that means anything, and
   ``walk_forward_verdict`` refuses to report a retention ratio for an edge that
   was never profitable in the first place (you cannot retain a fraction of
   nothing).

4. **The bar index means what the scanner thought it meant.**
   Candidates are cached against the frame the scanner actually saw, which for
   some timeframes is *trimmed* (``--since``) because a mode's usable window is
   the intersection over its timeframes — M5/M1 is capped by M1 history. Loading
   the untrimmed parquet would leave every ``bar_idx`` pointing one offset
   out — silently, with plausible-looking results, since the arrays are still
   long enough to index. ``load_series`` replays the trim from the scan manifest
   for exactly this reason.

WHAT THIS MODULE DOES NOT DO
----------------------------
It does not touch the live path. It reads the cached candidate tables and the
real price history, searches, and writes a report. Wiring the winner into the
engine is a separate, deliberate step — and it must go through
``jarvis.backtesting.exit_geometry.build_exit_geometry`` so that live and
backtest share one schedule.
"""
from __future__ import annotations

import glob
import json
import logging
import math
import os
import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from jarvis.backtesting.signal_scan import compute_atr
from jarvis.backtesting.trade_simulator import (
    Geometry,
    TradeOutcome,
    select_sequential,
    simulate_all_candidates,
    summarise,
)
from jarvis.config.paths import DATA_DIR
from jarvis.data.symbol_registry import get_dollar_risk_per_price_unit, resolve as resolve_symbol
from jarvis.market.data_feed import normalise_style

logger = logging.getLogger("JARVIS_BacktestOptimizer")

__all__ = [
    "PRIMARY_TIMEFRAME",
    "STYLES",
    "SeriesBundle",
    "GeometrySpace",
    "OptimizerSpec",
    "BacktestOptimizer",
    "objective_value",
    "load_series",
    "discover_symbols",
    "manifest_since",
    "optimise",
]

REAL_DIR = os.path.join(DATA_DIR, "market", "real")
SIGNALS_DIR = os.path.join(DATA_DIR, "signals")
DEFAULT_DAYS = 183

# Each style is scanned on its own primary timeframe (see tools/scan_signals.py
# and jarvis.market.data_feed.STYLE_TIMEFRAMES). These must agree, or the
# optimiser would read candidates scanned on one series and simulate them on
# another.
PRIMARY_TIMEFRAME: Dict[str, str] = {
    "SWING": "H1",
    "DAY_TRADING": "M15",
    "SCALP": "M5",
}
STYLES: Tuple[str, ...] = ("SWING", "DAY_TRADING", "SCALP")

OBJECTIVES: Tuple[str, ...] = ("expectancy_r", "total_r", "profit_factor", "calmar")


# ─────────────────────────────────────────────────────────────────────────────
# Data loading
# ─────────────────────────────────────────────────────────────────────────────
def manifest_since(timeframe: str, days: int = DEFAULT_DAYS) -> Optional[str]:
    """The ``--since`` trim the scanner applied when it cached this timeframe.

    Returns ``None`` when the scan used the full series (the H1 and M15 case),
    when no manifest exists, or when the manifest describes a different
    ``days`` window than the caller's data — in which case no trim is replayed.
    A 95-day manifest must never trim a 183-day candidate table.
    """
    tf = str(timeframe).upper()
    name = "scan_manifest.json" if tf == "H1" else f"scan_manifest_{tf}.json"
    path = os.path.join(SIGNALS_DIR, name)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError) as exc:
        logger.warning("could not read %s (%s) — assuming no trim", path, exc)
        return None

    try:
        manifest_days = int(payload.get("days"))
    except (TypeError, ValueError):
        manifest_days = None
    if manifest_days is not None and manifest_days != int(days):
        return None

    if payload.get("complete") is False:
        logger.warning(
            "%s belongs to a scan that is still running (complete=false). The "
            "`since` trim is published up front and IS trustworthy, but candidate "
            "tables for %s are being rewritten underneath this read — results will "
            "mix fresh and stale symbols.", path, tf,
        )
    since = payload.get("since")
    return str(since) if since else None


def _market_path(symbol: str, timeframe: str, days: int) -> str:
    return os.path.join(REAL_DIR, symbol, f"{symbol}_{timeframe}_{days}d.parquet")


def _candidates_path(symbol: str, timeframe: str, days: int) -> str:
    return os.path.join(SIGNALS_DIR, f"{symbol}_{timeframe}_{days}d_candidates.parquet")


def discover_symbols(timeframe: str, days: int = DEFAULT_DAYS) -> List[str]:
    """Symbols that have BOTH a price series and a candidate table for ``timeframe``.

    Both are required: a symbol with candidates but no bars cannot be simulated,
    and one with bars but no candidates has nothing to simulate.
    """
    tf = str(timeframe).upper()
    pattern = os.path.join(REAL_DIR, "*", f"*_{tf}_{days}d.parquet")
    out: List[str] = []
    for path in sorted(glob.glob(pattern)):
        symbol = os.path.basename(os.path.dirname(path))
        if os.path.exists(_candidates_path(symbol, tf, days)):
            out.append(symbol)
    return out


@dataclass
class SeriesBundle:
    """Everything needed to simulate and score one (symbol, style) pair.

    ``df`` is the frame the scanner saw — already trimmed by ``since`` and with a
    Wilder ATR column attached — so ``candidates['bar_idx']`` indexes it
    correctly. ``bars`` is the columnar view derived from it.
    """

    symbol: str
    style: str
    timeframe: str
    df: pd.DataFrame
    candidates: pd.DataFrame
    money_per_unit: float
    cost_price_equiv: float
    slippage_price_equiv: float
    spec: Any = None
    since: Optional[str] = None

    @property
    def key(self) -> str:
        return f"{self.symbol}__{self.style}"

    @property
    def n_bars(self) -> int:
        return int(len(self.df))

    def scores(self, regimes: Optional[Iterable[str]] = None) -> pd.Series:
        """The ``score`` column, optionally restricted to a set of regime labels.

        ``regimes=None`` means "no filter" (the pooled distribution). An empty or
        unmatched filter returns an empty series rather than falling back to the
        pooled column: silently widening back to everything would turn a regime
        filter that matched nothing into an unfiltered run, which is the one
        failure mode that would look like success.
        """
        if self.candidates is None or len(self.candidates) == 0:
            return pd.Series(dtype=float)
        col = self.candidates["score"]
        if regimes is None:
            return col
        wanted = {str(r) for r in regimes}
        if "regime" not in self.candidates.columns:
            return col.iloc[0:0]
        mask = self.candidates["regime"].astype(str).isin(wanted)
        return col[mask]

    def min_score_for(
        self, quantile: float, regimes: Optional[Iterable[str]] = None
    ) -> float:
        """The candidate-score threshold at ``quantile`` of THIS symbol's scores.

        Per-symbol rather than pooled on purpose: a "97th percentile signal" is a
        statement about a symbol's own score distribution, and the distributions
        differ enough between FX, gold and crypto that a pooled threshold would
        mean very different things on each.

        ``regimes`` narrows the distribution to those regimes first, and that is
        not cosmetic. The scores a symbol produces in COMPRESSION are
        systematically different from the ones it produces in TREND_BULL, so the
        90th percentile of the pooled distribution can sit near the median of the
        compression subset. Applying a pooled threshold inside a regime filter
        would then accept most of that regime's setups (or reject nearly all of
        them) while still reporting a "90th percentile" run.
        """
        if self.candidates is None or len(self.candidates) == 0:
            return 0.0
        q = float(max(0.0, min(1.0, quantile)))
        if q <= 0.0:
            return 0.0
        scores = self.scores(regimes)
        if len(scores) == 0:
            return 0.0
        try:
            return float(scores.quantile(q))
        except Exception:
            return 0.0

    def regime_counts(self) -> Dict[str, int]:
        """Candidate count per regime label, descending. Empty when unlabelled.

        Used to decide which regimes carry enough mass to be searched on their
        own, and to say so explicitly when one does not.
        """
        if self.candidates is None or len(self.candidates) == 0:
            return {}
        if "regime" not in self.candidates.columns:
            return {}
        vc = self.candidates["regime"].astype(str).value_counts()
        return {str(k): int(v) for k, v in vc.items()}

    def summary(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "style": self.style,
            "timeframe": self.timeframe,
            "bars": self.n_bars,
            "candidates": int(len(self.candidates)) if self.candidates is not None else 0,
            "since": self.since,
            "score_min": round(float(self.candidates["score"].min()), 4) if len(self.candidates) else None,
            "score_max": round(float(self.candidates["score"].max()), 4) if len(self.candidates) else None,
        }


def load_series(
    symbol: str,
    style: str,
    *,
    days: int = DEFAULT_DAYS,
    slippage_pips: float = 0.5,
    commission_per_lot: float = 5.0,
) -> Optional[SeriesBundle]:
    """Load one (symbol, style) series, replaying the scanner's trim exactly.

    Returns ``None`` when the data is missing or too short to be useful rather
    than raising: a universe sweep should degrade to "fewer symbols", not abort.
    """
    style = normalise_style(style)
    timeframe = PRIMARY_TIMEFRAME.get(style)
    if timeframe is None:
        logger.warning("unknown style %r", style)
        return None

    mpath = _market_path(symbol, timeframe, days)
    cpath = _candidates_path(symbol, timeframe, days)
    if not os.path.exists(mpath) or not os.path.exists(cpath):
        logger.debug("missing data for %s/%s (%s)", symbol, style, timeframe)
        return None

    df = pd.read_parquet(mpath)
    df["time"] = pd.to_datetime(df["time"], utc=True).dt.tz_localize(None)
    df = df.sort_values("time").reset_index(drop=True)

    # ── The alignment-critical step ─────────────────────────────────────────
    # Candidates are indexed against the TRIMMED frame. Replaying the same trim
    # is what makes bar_idx mean the same thing here as it did in the scanner.
    since = manifest_since(timeframe, days)
    if since:
        cutoff = pd.Timestamp(since)
        df = df[df["time"] >= cutoff].reset_index(drop=True)

    if len(df) < 150:
        logger.debug("%s/%s: only %d bars after trim", symbol, style, len(df))
        return None

    # The simulator trails off bars.atr; the stored parquet has no ATR column, so
    # it must be computed with the same Wilder definition the scanner used.
    if "atr" not in df.columns:
        df["atr"] = compute_atr(df, 14)

    candidates = pd.read_parquet(cpath)
    if len(candidates) == 0:
        return None

    # Guard the trap explicitly. If the trim were ever mis-replayed, every index
    # would be silently shifted; catching it here turns a wrong answer into a
    # loud error.
    max_idx = int(candidates["bar_idx"].max())
    if max_idx >= len(df):
        logger.error(
            "%s/%s: candidate bar_idx %d exceeds frame length %d — trim replay is wrong",
            symbol, style, max_idx, len(df),
        )
        return None

    spec = resolve_symbol(symbol)
    mpu = float(get_dollar_risk_per_price_unit(symbol) or 0.0)
    if mpu <= 0:
        logger.warning("%s: money-per-unit unavailable — skipping", symbol)
        return None

    return SeriesBundle(
        symbol=symbol,
        style=style,
        timeframe=timeframe,
        df=df,
        candidates=candidates,
        money_per_unit=mpu,
        cost_price_equiv=(float(commission_per_lot) / mpu) if mpu > 0 else 0.0,
        slippage_price_equiv=float(slippage_pips) * float(getattr(spec, "pip_size", 0.0001) or 0.0001),
        spec=spec,
        since=since,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Search space
# ─────────────────────────────────────────────────────────────────────────────
def _sim_key(geom: Geometry) -> str:
    """Cache key for a simulation — everything EXCEPT ``min_score``.

    ``min_score`` is deliberately excluded: it decides which simulated trades are
    taken, never what they did. Two geometries differing only in selectivity are
    therefore the same simulation, and the second one is free.
    """
    def f(v: Optional[float]) -> str:
        return "off" if v is None else f"{v:g}"

    return (
        f"tp{geom.tp_r:g}|be{f(geom.be_trigger_r)}|pc{f(geom.fast_cash_r)}"
        f"x{geom.fast_cash_pct:g}|tr{f(geom.trail_atr)}@{geom.trail_activation_r:g}"
        f"|mb{geom.max_bars}"
    )


@dataclass(frozen=True)
class GeometrySpace:
    """The discrete grid the coordinate descent moves over.

    ``min_score`` is searched as *quantiles of the symbol's own score
    distribution* rather than absolute values, because a raw score threshold is
    not comparable across symbols while "the top 3% of this symbol's setups" is.
    """

    tp_r: Tuple[float, ...] = (1.0, 1.5, 2.0, 2.5, 3.0)
    be_trigger_r: Tuple[Optional[float], ...] = (None, 0.5, 1.0, 1.5)
    fast_cash_r: Tuple[Optional[float], ...] = (None, 0.75, 1.0, 1.5)
    trail_atr: Tuple[Optional[float], ...] = (None, 1.0, 1.5, 2.0)
    min_score_quantiles: Tuple[float, ...] = (0.0, 0.5, 0.75, 0.9, 0.97, 0.99)
    fast_cash_pct: float = 0.5
    trail_activation_r: float = 2.0
    max_bars: int = 200

    def seed(self) -> Geometry:
        """Starting point for the descent — a plain fixed-target profile."""
        return Geometry(
            tp_r=1.5,
            be_trigger_r=None,
            fast_cash_r=None,
            fast_cash_pct=self.fast_cash_pct,
            trail_atr=None,
            trail_activation_r=self.trail_activation_r,
            max_bars=self.max_bars,
            min_score=0.0,
        )

    def options(self, dimension: str) -> Tuple[Any, ...]:
        # ``min_score`` is searched as quantiles, so the dimension name and the
        # field name differ. Every other dimension is its own field.
        attr = _DIMENSION_FIELD.get(dimension, dimension)
        return tuple(getattr(self, attr))

    def with_value(self, geom: Geometry, dimension: str, value: Any) -> Geometry:
        if dimension == "min_score":
            return replace(geom, min_score=float(value))
        return replace(geom, **{dimension: value})

    def all_geometries(self, limit: Optional[int] = None) -> List[Geometry]:
        """Full cartesian product, optionally truncated. Used for a coarse sweep."""
        out: List[Geometry] = []
        for tp in self.tp_r:
            for be in self.be_trigger_r:
                for pc in self.fast_cash_r:
                    for tr in self.trail_atr:
                        for q in self.min_score_quantiles:
                            out.append(
                                Geometry(
                                    tp_r=tp, be_trigger_r=be, fast_cash_r=pc,
                                    fast_cash_pct=self.fast_cash_pct, trail_atr=tr,
                                    trail_activation_r=self.trail_activation_r,
                                    max_bars=self.max_bars, min_score=q,
                                )
                            )
                            if limit and len(out) >= limit:
                                return out
        return out


@dataclass
class OptimizerSpec:
    """What to optimise, and the constraints that keep the answer honest."""

    symbols: Tuple[str, ...] = ()
    modes: Tuple[str, ...] = STYLES
    objective: str = "expectancy_r"
    # Constraints. A geometry that cannot clear these scores -inf regardless of
    # how good its raw number looks, which is what stops the search from
    # "winning" by taking three enormous trades.
    min_trades: int = 30
    max_dd_r: float = 40.0
    slippage_pips: float = 0.5
    commission_per_lot: float = 5.0
    # Descent control.
    passes: int = 3
    max_evaluations: int = 400
    # Out-of-sample discipline.
    walk_forward_split: float = 0.7
    walk_forward_folds: int = 3
    per_mode_search: bool = True
    days: int = DEFAULT_DAYS
    label: str = ""

    def normalised(self) -> "OptimizerSpec":
        objective = str(self.objective or "").strip().lower()
        if objective not in OBJECTIVES:
            objective = "expectancy_r"
        modes = tuple(normalise_style(m) for m in (self.modes or STYLES))
        modes = tuple(m for m in modes if m in STYLES) or STYLES
        split = float(self.walk_forward_split)
        split = 0.5 if not (0.3 <= split <= 0.9) else split
        return replace(
            self,
            objective=objective,
            modes=modes,
            walk_forward_split=split,
            min_trades=max(1, int(self.min_trades)),
            max_evaluations=max(1, int(self.max_evaluations)),
            passes=max(1, int(self.passes)),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Objective
# ─────────────────────────────────────────────────────────────────────────────
def objective_value(metrics: Dict[str, Any], spec: OptimizerSpec) -> float:
    """Score a metrics dict, returning ``-inf`` when a constraint is violated.

    ``-inf`` rather than a large negative number so that an infeasible geometry
    can never win a comparison, no matter how extreme the feasible scores are.
    """
    if not metrics:
        return float("-inf")

    trades = int(metrics.get("trades", 0) or 0)
    if trades < int(spec.min_trades):
        return float("-inf")

    dd = float(metrics.get("max_dd_r", 0.0) or 0.0)
    if spec.max_dd_r and dd > float(spec.max_dd_r):
        return float("-inf")

    exp = float(metrics.get("expectancy_r", 0.0) or 0.0)
    if not math.isfinite(exp) or exp <= 0.0:
        # A negative-expectancy geometry is not a candidate for "maximum profit",
        # whatever the other dimensions say.
        return float("-inf")

    obj = spec.objective
    if obj == "total_r":
        return float(metrics.get("total_r", 0.0) or 0.0)
    if obj == "profit_factor":
        return float(metrics.get("profit_factor", 0.0) or 0.0)
    if obj == "calmar":
        return exp / dd if dd > 1e-9 else exp * 10.0
    return exp


# ─────────────────────────────────────────────────────────────────────────────
# The optimiser
# ─────────────────────────────────────────────────────────────────────────────
# Descent order. ``tp_r`` is searched LAST on purpose: it is the dimension whose
# optimum moves most as the others change, so locking it early traps the descent
# in a corner. The stop-management dimensions are cheaper to reason about first.
_DESCENT_ORDER: Tuple[str, ...] = (
    "be_trigger_r", "fast_cash_r", "trail_atr", "min_score", "tp_r",
)

# The ``min_score`` search dimension is stored as quantiles, so its field name
# differs from its dimension name. All other dimensions are their own field.
_DIMENSION_FIELD: Dict[str, str] = {"min_score": "min_score_quantiles"}


@dataclass
class GeometryEvaluation:
    """One scored geometry, with the evidence behind the score."""

    geometry: Geometry
    metrics: Dict[str, Any]
    score: float
    evaluations: int = 0
    window: Tuple[Optional[int], Optional[int]] = (None, None)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "geometry": self.geometry.to_dict(),
            "metrics": self.metrics,
            "score": None if self.score == float("-inf") else round(self.score, 6),
            "feasible": self.score != float("-inf"),
            "window": list(self.window),
        }


class BacktestOptimizer:
    """Coordinate-descent search for the most profitable geometry.

    Coordinate descent rather than a full grid because the grid is
    ``5 tp x 4 be x 4 pc x 4 trail x 6 thresholds = 1920`` geometries per mode per
    universe, and each distinct geometry costs one simulation pass over every
    candidate. Descent reaches the same neighbourhood in a few hundred
    evaluations, and the simulation cache means repeat visits are free.
    """

    def __init__(
        self,
        spec: OptimizerSpec,
        *,
        space: Optional[GeometrySpace] = None,
        progress: Optional[Callable[[str], None]] = None,
    ):
        self.spec = spec.normalised()
        self.space = space or GeometrySpace()
        self._progress = progress or (lambda msg: logger.info(msg))
        self._sim_cache: Dict[str, List[TradeOutcome]] = {}
        self._eval_count = 0
        self._cache_hits = 0
        # ``max_evaluations`` is a budget per SEARCH, not per optimiser. The
        # regime-conditioned driver runs one descent per market condition, and
        # with an absolute counter the first descent would consume the budget and
        # every later regime would bail out on its first line search — silently
        # reporting "no feasible geometry" for regimes that were never searched.
        # Zero here keeps the single-search behaviour bit-identical.
        self._budget_start = 0

    # ── reporting ───────────────────────────────────────────────────────────
    def _say(self, msg: str) -> None:
        self._progress(msg)

    # ── search budget ───────────────────────────────────────────────────────
    def _budget_spent(self) -> int:
        """Evaluations consumed by the CURRENT search (see ``_budget_start``)."""
        return self._eval_count - self._budget_start

    def _reset_budget(self) -> None:
        """Start a fresh ``max_evaluations`` window for the next search."""
        self._budget_start = self._eval_count

    # ── simulation (cached) ─────────────────────────────────────────────────
    def _outcomes(self, bundle: SeriesBundle, geom: Geometry) -> List[TradeOutcome]:
        """Simulate every candidate once per distinct geometry."""
        key = f"{bundle.key}|{_sim_key(geom)}"
        cached = self._sim_cache.get(key)
        if cached is not None:
            self._cache_hits += 1
            return cached

        outcomes = simulate_all_candidates(
            df=bundle.df,
            candidates=bundle.candidates,
            geom=geom,
            money_per_unit=bundle.money_per_unit,
            cost_price_equiv=bundle.cost_price_equiv,
            slippage_price_equiv=bundle.slippage_price_equiv,
            spec=bundle.spec,
            symbol=bundle.symbol,
        )
        self._sim_cache[key] = outcomes
        return outcomes

    def _selected(
        self,
        bundle: SeriesBundle,
        geom: Geometry,
        *,
        entry_lo: Optional[int],
        entry_hi: Optional[int],
        quantile: float,
        regimes: Optional[Iterable[str]] = None,
    ) -> List[TradeOutcome]:
        """Simulate under ``geom`` then apply selectivity + one-position-at-a-time.

        The selectivity threshold is derived from ``quantile`` on this symbol's
        own score distribution, so the geometry's own ``min_score`` field is
        overwritten here — it is a carrier for the searched value, not an input.

        ``regimes`` is passed to BOTH the quantile lookup and the selection, so a
        regime-conditional run means "the top ``quantile`` of *this regime's*
        setups". Computing the threshold on the pooled scores and then filtering
        by regime would silently rescale the selectivity, which is the subtle way
        a regime-conditional search can end up describing a different system than
        the one it claims to.

        Regime filtering is free: ``_outcomes`` is cached on the geometry alone,
        so re-scoring one geometry under every regime costs one simulation plus
        cheap filtering passes over the same outcome list.
        """
        outcomes = self._outcomes(bundle, geom)
        min_score = bundle.min_score_for(quantile, regimes)
        return select_sequential(
            outcomes,
            min_score=min_score,
            entry_lo=entry_lo,
            entry_hi=entry_hi,
            regimes=set(regimes) if regimes is not None else None,
        )

    # ── scoring ─────────────────────────────────────────────────────────────
    def evaluate(
        self,
        geom: Geometry,
        bundles: Sequence[SeriesBundle],
        *,
        entry_lo: Optional[int] = None,
        entry_hi: Optional[int] = None,
        quantile: Optional[float] = None,
        count: bool = True,
        regimes: Optional[Iterable[str]] = None,
    ) -> Dict[str, Any]:
        """Pooled metrics for ``geom`` across every bundle in the window.

        Each bundle contributes its own selected trades; the pool is then ordered
        by entry time so the drawdown figure describes a portfolio equity curve
        rather than an arbitrary concatenation order.

        ``regimes`` restricts the pool to those market conditions, which is how
        the same cached simulation answers "what does this geometry do *in a
        trend*" as well as "what does it do overall".
        """
        if count:
            self._eval_count += 1
        q = geom.min_score if quantile is None else quantile

        pooled: List[TradeOutcome] = []
        for b in bundles:
            pooled.extend(
                self._selected(
                    b, geom, entry_lo=entry_lo, entry_hi=entry_hi,
                    quantile=q, regimes=regimes,
                )
            )

        if not pooled:
            return summarise([])

        pooled.sort(key=lambda o: str(o.entry_time))
        return summarise(pooled)

    def _score(
        self,
        geom: Geometry,
        bundles: Sequence[SeriesBundle],
        *,
        entry_lo: Optional[int],
        entry_hi: Optional[int],
        regimes: Optional[Iterable[str]] = None,
    ) -> GeometryEvaluation:
        metrics = self.evaluate(
            geom, bundles, entry_lo=entry_lo, entry_hi=entry_hi, regimes=regimes
        )
        return GeometryEvaluation(
            geometry=geom,
            metrics=metrics,
            score=objective_value(metrics, self.spec),
            evaluations=self._eval_count,
            window=(entry_lo, entry_hi),
        )

    # ── search ──────────────────────────────────────────────────────────────
    def _coordinate_descent(
        self,
        bundles: Sequence[SeriesBundle],
        *,
        entry_lo: Optional[int],
        entry_hi: Optional[int],
        regimes: Optional[Iterable[str]] = None,
    ) -> GeometryEvaluation:
        """Greedy line search over each dimension in turn, repeated ``passes`` times.

        ``regimes`` restricts the objective to those market conditions, so the
        same descent answers both "the best geometry overall" and "the best
        geometry when the market is in COMPRESSION".
        """
        current = self.space.seed()
        best = self._score(
            current, bundles, entry_lo=entry_lo, entry_hi=entry_hi, regimes=regimes
        )
        self._say(
            f"    seed: {_geom_str(best.geometry)} -> "
            f"exp={best.metrics.get('expectancy_r', 0):+.4f}R "
            f"trades={best.metrics.get('trades', 0)}"
        )

        for p in range(self.spec.passes):
            improved = False
            for dim in _DESCENT_ORDER:
                if self._budget_spent() >= self.spec.max_evaluations:
                    self._say(
                        f"    evaluation budget reached "
                        f"({self._budget_spent()}/{self.spec.max_evaluations})"
                    )
                    return best

                for value in self.space.options(dim):
                    if self._budget_spent() >= self.spec.max_evaluations:
                        break
                    # Skip a value that reproduces the current point.
                    if _same_value(getattr(best.geometry, dim, None), value):
                        continue

                    candidate = self.space.with_value(best.geometry, dim, value)
                    ev = self._score(
                        candidate, bundles,
                        entry_lo=entry_lo, entry_hi=entry_hi, regimes=regimes,
                    )
                    if ev.score > best.score:
                        best = ev
                        improved = True

            self._say(
                f"    pass {p + 1}: {_geom_str(best.geometry)} -> "
                f"exp={best.metrics.get('expectancy_r', 0):+.4f}R "
                f"trades={best.metrics.get('trades', 0)} "
                f"(evals={self._eval_count})"
            )
            if not improved:
                break

        return best

    def walk_forward_verdict(
        self,
        in_sample: Dict[str, Any],
        out_of_sample: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Decide whether an in-sample optimum survived on held-out data.

        The precondition matters. If the in-sample expectancy was not positive,
        there is no edge to retain, and reporting ``retention = 1.00`` because
        the out-of-sample number happened to match a negative number would be
        actively misleading. ``edge_retention`` is therefore ``None`` — "not
        defined" — rather than a fabricated ratio.
        """
        is_exp = float(in_sample.get("expectancy_r", 0.0) or 0.0)
        oos_exp = float(out_of_sample.get("expectancy_r", 0.0) or 0.0)
        oos_trades = int(out_of_sample.get("trades", 0) or 0)

        is_profitable = is_exp > 0.0
        oos_profitable = oos_exp > 0.0

        retention: Optional[float] = None
        if is_profitable:
            retention = round(oos_exp / is_exp, 4)

        generalises = bool(
            is_profitable
            and oos_profitable
            and oos_trades >= max(5, int(self.spec.min_trades * 0.2))
            and (retention is not None and retention >= 0.5)
        )

        if not is_profitable:
            note = "in-sample expectancy was not positive — no edge to retain"
        elif not oos_profitable:
            note = "edge did not survive: held-out expectancy is negative"
        elif retention is not None and retention < 0.5:
            note = f"edge decayed to {retention:.0%} of its in-sample value"
        elif not generalises:
            note = "held-out sample too small to confirm the edge"
        else:
            note = "edge survived out-of-sample"

        return {
            "in_sample_expectancy_r": round(is_exp, 4),
            "out_of_sample_expectancy_r": round(oos_exp, 4),
            "in_sample_trades": int(in_sample.get("trades", 0) or 0),
            "out_of_sample_trades": oos_trades,
            "edge_retention": retention,
            "is_profitable_in_sample": is_profitable,
            "is_profitable_out_of_sample": oos_profitable,
            "generalises": generalises,
            "note": note,
        }

    def _fold_metrics(
        self,
        bundles: Sequence[SeriesBundle],
        geom: Geometry,
        *,
        entry_lo: int,
        entry_hi: int,
        folds: int,
        regimes: Optional[Iterable[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Contiguous folds over the validation window, for a stability read."""
        folds = max(1, int(folds))
        span = max(1, entry_hi - entry_lo)
        width = max(1, span // folds)
        out: List[Dict[str, Any]] = []
        for i in range(folds):
            lo = entry_lo + i * width
            hi = entry_hi if i == folds - 1 else min(entry_hi, lo + width)
            m = self.evaluate(
                geom, bundles, entry_lo=lo, entry_hi=hi, count=False, regimes=regimes
            )
            out.append({
                "fold": i + 1,
                "entry_lo": lo,
                "entry_hi": hi,
                "trades": int(m.get("trades", 0) or 0),
                "expectancy_r": round(float(m.get("expectancy_r", 0.0) or 0.0), 4),
                "total_r": round(float(m.get("total_r", 0.0) or 0.0), 3),
                "win_rate": round(float(m.get("win_rate", 0.0) or 0.0), 4),
            })
        return out

    # ── driver ──────────────────────────────────────────────────────────────
    def run(self, bundles_by_style: Optional[Dict[str, List[SeriesBundle]]] = None) -> Dict[str, Any]:
        """Search per mode, validate out-of-sample, and return a report dict."""
        t0 = time.time()
        spec = self.spec

        if bundles_by_style is None:
            bundles_by_style = self.load_all()

        report: Dict[str, Any] = {
            "generated_utc": pd.Timestamp.now("UTC").isoformat(),
            "spec": {
                "symbols": list(spec.symbols),
                "modes": list(spec.modes),
                "objective": spec.objective,
                "min_trades": spec.min_trades,
                "max_dd_r": spec.max_dd_r,
                "slippage_pips": spec.slippage_pips,
                "commission_per_lot": spec.commission_per_lot,
                "passes": spec.passes,
                "max_evaluations": spec.max_evaluations,
                "walk_forward_split": spec.walk_forward_split,
                "walk_forward_folds": spec.walk_forward_folds,
                "days": spec.days,
                "label": spec.label,
            },
            "modes": [],
            "series": [],
        }

        for style in spec.modes:
            bundles = bundles_by_style.get(style) or []
            if not bundles:
                self._say(f"  {style}: no series available — skipped")
                report["modes"].append({"style": style, "error": "no series"})
                continue

            # Each mode gets its own ``max_evaluations`` window. With a single
            # absolute counter the first mode consumes the budget and every later
            # mode returns the unsearched seed geometry while still reporting a
            # full result — a silent failure, and one that a low budget (the live
            # verifier passes 12) triggers on every run.
            self._reset_budget()

            for b in bundles:
                report["series"].append(b.summary())

            n_bars = int(np.median([b.n_bars for b in bundles])) if bundles else 0
            split_idx = int(n_bars * spec.walk_forward_split)

            self._say(
                f"  {style}: {len(bundles)} series, ~{n_bars} bars, "
                f"search window [0,{split_idx}) / validate [{split_idx},{n_bars})"
            )

            # ── Search on the first split only ──────────────────────────────
            search = self._coordinate_descent(bundles, entry_lo=0, entry_hi=split_idx)

            # ── Validate on the held-out remainder ──────────────────────────
            oos = self._score(
                search.geometry, bundles, entry_lo=split_idx, entry_hi=None
            )
            verdict = self.walk_forward_verdict(search.metrics, oos.metrics)
            folds = self._fold_metrics(
                bundles, search.geometry,
                entry_lo=split_idx, entry_hi=n_bars, folds=spec.walk_forward_folds,
            )

            # ── Full-window numbers, clearly labelled as in-sample-inclusive ─
            full = self.evaluate(
                search.geometry, bundles, entry_lo=None, entry_hi=None, count=False
            )

            per_symbol: List[Dict[str, Any]] = []
            for b in bundles:
                m = self.evaluate(
                    search.geometry, [b], entry_lo=None, entry_hi=None, count=False
                )
                per_symbol.append({
                    "symbol": b.symbol,
                    "trades": int(m.get("trades", 0) or 0),
                    "expectancy_r": round(float(m.get("expectancy_r", 0.0) or 0.0), 4),
                    "total_r": round(float(m.get("total_r", 0.0) or 0.0), 3),
                    "win_rate": round(float(m.get("win_rate", 0.0) or 0.0), 4),
                    "profit_factor": round(float(m.get("profit_factor", 0.0) or 0.0), 3),
                    "max_dd_r": round(float(m.get("max_dd_r", 0.0) or 0.0), 3),
                })
            per_symbol.sort(key=lambda r: r["total_r"], reverse=True)

            positive = [r for r in per_symbol if r["total_r"] > 0]

            # ``Geometry.min_score`` carries the searched QUANTILE during the
            # descent, not a raw score threshold. Reporting it as "min_score"
            # would invite the reader to apply it directly, so the quantile is
            # stated explicitly alongside the per-symbol threshold it resolves
            # to — which is what a caller actually needs to reproduce the run.
            quantile = float(search.geometry.min_score)
            resolved = {
                b.symbol: round(b.min_score_for(quantile), 6) for b in bundles
            }

            report["modes"].append({
                "style": style,
                "primary_timeframe": PRIMARY_TIMEFRAME.get(style),
                "series_count": len(bundles),
                "bars_median": n_bars,
                "search_window": [0, split_idx],
                "validation_window": [split_idx, n_bars],
                "best_geometry": search.geometry.to_dict(),
                "best_geometry_key": search.geometry.key(),
                "best_min_score_quantile": quantile,
                "best_min_score_resolved": resolved,
                "feasible": search.score != float("-inf"),
                "in_sample": search.metrics,
                "out_of_sample": oos.metrics,
                "full_window": full,
                "walk_forward": verdict,
                "validation_folds": folds,
                "per_symbol": per_symbol,
                "symbols_positive": len(positive),
                "symbols_total": len(per_symbol),
                "evaluations": self._eval_count,
                "cache_hits": self._cache_hits,
            })

            self._say(
                f"    -> best {_geom_str(search.geometry)} | "
                f"IS exp={verdict['in_sample_expectancy_r']:+.4f}R "
                f"OOS exp={verdict['out_of_sample_expectancy_r']:+.4f}R "
                f"retention={verdict['edge_retention']} "
                f"generalises={verdict['generalises']}"
                + ("" if search.score != float("-inf") else "  [NO FEASIBLE GEOMETRY]")
            )

        report["elapsed_seconds"] = round(time.time() - t0, 1)
        report["evaluations"] = self._eval_count
        report["cache_hits"] = self._cache_hits
        report["cache_hit_rate"] = (
            round(self._cache_hits / (self._cache_hits + self._eval_count), 4)
            if (self._cache_hits + self._eval_count) else 0.0
        )
        return report

    # ── loading ─────────────────────────────────────────────────────────────
    def load_all(self) -> Dict[str, List[SeriesBundle]]:
        """Load every (symbol, style) series, one style at a time.

        Loaded per style rather than all at once because this machine has under a
        gigabyte of free RAM and the M5 series are the largest; holding all 60
        bundles simultaneously is the difference between finishing and swapping.
        """
        spec = self.spec
        out: Dict[str, List[SeriesBundle]] = {}

        for style in spec.modes:
            tf = PRIMARY_TIMEFRAME.get(style)
            if not tf:
                continue
            symbols = list(spec.symbols) or discover_symbols(tf, spec.days)
            bundles: List[SeriesBundle] = []
            for sym in symbols:
                b = load_series(
                    sym, style,
                    days=spec.days,
                    slippage_pips=spec.slippage_pips,
                    commission_per_lot=spec.commission_per_lot,
                )
                if b is not None:
                    bundles.append(b)
            out[style] = bundles
            self._say(f"  loaded {len(bundles)}/{len(symbols)} series for {style}")
        return out

    def release(self) -> None:
        """Drop cached simulations and loaded frames to reclaim memory."""
        self._sim_cache.clear()


def _same_value(a: Any, b: Any) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    try:
        return abs(float(a) - float(b)) < 1e-12
    except (TypeError, ValueError):
        return a == b


def _geom_str(geom: Geometry) -> str:
    def f(v: Optional[float]) -> str:
        return "off" if v is None else f"{v:g}"

    return (
        f"tp={geom.tp_r:g} be={f(geom.be_trigger_r)} pc={f(geom.fast_cash_r)} "
        f"trail={f(geom.trail_atr)} q={geom.min_score:g}"
    )


def optimise(
    *,
    symbols: Sequence[str] = (),
    modes: Sequence[str] = STYLES,
    objective: str = "expectancy_r",
    min_trades: int = 30,
    max_dd_r: float = 40.0,
    slippage_pips: float = 0.5,
    commission_per_lot: float = 5.0,
    passes: int = 3,
    max_evaluations: int = 400,
    walk_forward_split: float = 0.7,
    walk_forward_folds: int = 3,
    days: int = DEFAULT_DAYS,
    label: str = "",
    space: Optional[GeometrySpace] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Convenience entry point used by the CLI and the API job runner."""
    spec = OptimizerSpec(
        symbols=tuple(symbols or ()),
        modes=tuple(modes or STYLES),
        objective=objective,
        min_trades=min_trades,
        max_dd_r=max_dd_r,
        slippage_pips=slippage_pips,
        commission_per_lot=commission_per_lot,
        passes=passes,
        max_evaluations=max_evaluations,
        walk_forward_split=walk_forward_split,
        walk_forward_folds=walk_forward_folds,
        days=days,
        label=label,
    )
    opt = BacktestOptimizer(spec, space=space, progress=progress)
    return opt.run()
