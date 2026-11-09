"""
HM Algo 2.0 — Fast Trade Simulator (calibration instrument).

WHY THIS MODULE EXISTS
----------------------
Calibrating a per-symbol win-rate target requires evaluating *many* exit
geometries against *many* candidate entries. Running the full decision pipeline
once per configuration is impossible: the pipeline costs ~78 ms per bar, so a
24-point grid x 16 symbols would take days.

The fix is an architectural split:

    signal generation  (expensive, once per symbol)  ->  SignalScanner
    trade simulation   (cheap, per configuration)    ->  this module

The scanner records every candidate entry the live pipeline considered. This
module then replays those candidates through a parameterised exit geometry using
the SAME ``jarvis.execution.exit_policy.evaluate_exit`` that the live position
monitor uses. Because both paths share one pure function, a calibrated geometry
cannot drift from live behaviour.

INTRABAR ORDERING (deliberate, conservative)
--------------------------------------------
A bar's OHLC does not reveal the order in which its high and low occurred. The
live engine previously resolved this optimistically: it advanced the stop to
breakeven using the bar's favourable excursion and *then* asked whether the stop
had been hit on the same bar — so a bar that ran up and then reversed into the
original stop was recorded as a breakeven win. That look-ahead inflates win rate
precisely in the region a 75% target cares about.

This module uses the standard conservative convention:

    1. the stop resting at the START of the bar is tested first
    2. only then is the target tested
    3. policy ratchets (partial / breakeven / trail) take effect from the NEXT bar

A bar that spans both the stop and the target is therefore booked as a LOSS.
This is the honest lower bound on win rate.

UNIT DISCIPLINE
---------------
All monetary results are expressed per 1.0 initial lot and additionally as an
R-multiple (net price move / initial risk distance). R is the only scale-free
unit comparable across gold, FX, indices and crypto, so calibration objectives
are stated in R and never in currency.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import numpy as np
import pandas as pd

from jarvis.execution.exit_policy import ExitPolicy, evaluate_exit


# ─────────────────────────────────────────────────────────────────────────────
# Geometry — the tunable search space
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Geometry:
    """A parameterised entry-selectivity + exit-geometry configuration.

    Every field is expressed so that it means the same thing on every symbol:

    tp_r                target distance as a multiple of the initial risk (1R).
                        tp_r < 1.0 is a "bank small, high hit-rate" profile;
                        tp_r > 1.0 is a "let it run" profile.
    be_trigger_r        favourable R at which the stop moves to breakeven.
                        ``None`` disables breakeven locking entirely, which is
                        usually REQUIRED for a high win rate: locking breakeven
                        early converts would-be winners into ~0R scratches.
    fast_cash_r         favourable R at which a partial is banked.
                        ``None`` disables partial banking.
    fast_cash_pct       fraction of the position banked at ``fast_cash_r``.
    trail_atr           ATR multiple for the runner trail. ``None`` disables.
    trail_activation_r  favourable R at which the ATR trail engages.
    max_bars            hard time stop in bars (stagnation guard).
    min_score           minimum AI blended score required to take the signal.
                        ``0.0`` accepts every candidate the pipeline produced.
    """

    tp_r: float = 1.5
    be_trigger_r: Optional[float] = None
    fast_cash_r: Optional[float] = None
    fast_cash_pct: float = 0.5
    trail_atr: Optional[float] = None
    trail_activation_r: float = 2.0
    max_bars: int = 200
    min_score: float = 0.0

    # ── construction helpers ────────────────────────────────────────────────
    def to_policy(self, symbol: str, spec: Any) -> ExitPolicy:
        """Build the canonical ExitPolicy this geometry represents.

        Disabled features are pushed to an unreachable threshold (1e9) rather
        than being special-cased in ``evaluate_exit``; that keeps the policy
        function a single, branch-free source of truth.
        """
        unreachable = 1e9
        return ExitPolicy(
            symbol=str(symbol),
            be_trigger_r=self.be_trigger_r if self.be_trigger_r is not None else unreachable,
            fast_cash_r=self.fast_cash_r if self.fast_cash_r is not None else unreachable,
            fast_cash_volume_pct=self.fast_cash_pct,
            runner_trail_atr=self.trail_atr if self.trail_atr is not None else 1.0,
            trail_activation_r=(
                self.trail_activation_r if self.trail_atr is not None else unreachable
            ),
            # Milestones are a third ratchet that would confound attribution of
            # win rate to tp_r / be / partial. They are deliberately excluded
            # from the calibration space and re-enabled by the production
            # profile only if a symbol's calibrated geometry wants them.
            milestones=[],
            digits=int(getattr(spec, "digits", 5) or 5),
            pip_size=float(getattr(spec, "pip_size", 0.0001) or 0.0001),
        )

    def key(self) -> str:
        def _f(v: Optional[float]) -> str:
            return "off" if v is None else f"{v:g}"

        return (
            f"tp{self.tp_r:g}_be{_f(self.be_trigger_r)}_"
            f"pc{_f(self.fast_cash_r)}x{self.fast_cash_pct:g}_"
            f"tr{_f(self.trail_atr)}@{self.trail_activation_r:g}_"
            f"mb{self.max_bars}_s{self.min_score:g}"
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Geometry":
        allowed = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in allowed})


# ─────────────────────────────────────────────────────────────────────────────
# Outcome
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class TradeOutcome:
    """Result of simulating one candidate entry under one geometry."""

    symbol: str
    side: str
    entry_idx: int
    entry_time: Any
    exit_idx: int
    exit_time: Any
    bars_held: int
    entry: float
    exit: float
    sl_initial: float
    sl_final: float
    tp: float
    risk_dist: float
    result: str
    mfe_r: float
    mae_r: float
    pnl_r: float
    pnl_money_per_lot: float
    is_win: bool
    partial_taken: bool
    be_locked: bool
    # Scale-out accounting (HM Algo 2.0). These were COMPUTED inside the
    # simulation loop but then thrown away -- only the ``partial_taken`` flag
    # survived into the outcome. Without the quantity, the price and the banked
    # R it is impossible to audit a scale-out: you cannot tell a 33%-at-1.5R
    # partial from a 75%-at-0.5R one, and you cannot attribute P&L between the
    # banked leg and the runner. Recorded so the exit-geometry engine can be
    # evaluated leg by leg.
    partial_pct: float = 0.0
    partial_price: float = 0.0
    partial_r: float = 0.0
    # Context carried through from the candidate so the AI layer can learn
    # conditional edge (symbol x regime x strategy x score) without a re-scan.
    ai_score: float = 0.0
    calibrated_win_p: float = 0.0
    regime: str = "UNKNOWN"
    strategy: str = "UNKNOWN"
    zone: str = "UNKNOWN"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────────────
# Columnar bar store
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class BarArrays:
    """Columnar OHLC/ATR view of a price series.

    The inner simulation loop touches up to ``max_bars`` bars per candidate and
    is executed tens of thousands of times per symbol during calibration.
    Indexing a DataFrame with ``df.iloc[j]`` builds a Series object every single
    time, which dominated the runtime. Extracting numpy columns once and reading
    scalars out of them is an order of magnitude faster and is the only reason a
    walk-forward calibration over a 16-symbol universe is affordable.
    """

    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    atr: np.ndarray
    time: np.ndarray
    n: int

    @classmethod
    def from_df(cls, df: pd.DataFrame) -> "BarArrays":
        high = df["high"].to_numpy(dtype=float)
        low = df["low"].to_numpy(dtype=float)
        close = df["close"].to_numpy(dtype=float)
        if "atr" in df.columns:
            atr = df["atr"].to_numpy(dtype=float)
        else:
            atr = np.zeros(len(df), dtype=float)
        time = df["time"].to_numpy() if "time" in df.columns else np.arange(len(df))
        return cls(high=high, low=low, close=close, atr=atr, time=time, n=len(df))


# ─────────────────────────────────────────────────────────────────────────────
# Core simulation
# ─────────────────────────────────────────────────────────────────────────────
def simulate_trade(
    *,
    symbol: str,
    side: str,
    entry_idx: int,
    fill: float,
    sl: float,
    geom: Geometry,
    money_per_unit: float,
    df: Optional[pd.DataFrame] = None,
    bars: Optional[BarArrays] = None,
    cost_price_equiv: float = 0.0,
    slippage_price_equiv: float = 0.0,
    ai_score: float = 0.0,
    calibrated_win_p: float = 0.0,
    regime: str = "UNKNOWN",
    strategy: str = "UNKNOWN",
    zone: str = "UNKNOWN",
    spec: Any = None,
) -> Optional[TradeOutcome]:
    """Replay one entry forward under ``geom``.

    ``fill`` is the realised entry price (next-bar open adjusted for spread).
    ``sl`` is the initial stop, which defines 1R = |fill - sl|.
    ``cost_price_equiv`` is the round-turn cost expressed as a PRICE DISTANCE
    (commission_per_lot / money_per_unit), so it can be netted off the price
    move directly without mixing units.

    Supply ``bars`` (preferred, and required in hot loops) or ``df``.
    Returns ``None`` if the trade cannot be represented (degenerate risk, no
    forward bars).
    """
    if bars is None:
        if df is None:
            return None
        bars = BarArrays.from_df(df)

    risk_dist = abs(fill - sl)
    if risk_dist <= 0 or money_per_unit <= 0:
        return None
    n = bars.n
    if entry_idx >= n - 1:
        return None

    high_arr = bars.high
    low_arr = bars.low
    close_arr = bars.close
    atr_arr = bars.atr

    is_buy = str(side).upper().startswith("BUY")
    direction = 1.0 if is_buy else -1.0
    tp_price = fill + direction * geom.tp_r * risk_dist
    policy = geom.to_policy(symbol, spec)
    inv_risk = 1.0 / risk_dist
    exit_side = "BUY" if is_buy else "SELL"

    current_sl = sl
    be_locked = False
    partial_taken = False
    partial_pct = 0.0
    partial_price = 0.0   # price at which the banked leg was closed (5.1 audit)
    partial_r_gross = 0.0  # banked R from the partial (gross of cost)
    mfe = 0.0
    mae = 0.0
    max_bars = int(geom.max_bars)

    # The entry fills at the NEXT bar's open (see signal_scan), so the trade
    # is not live until bar ``entry_idx + 1``. Testing bar ``entry_idx`` (the
    # signal bar) for SL/TP is a one-bar look-ahead that books phantom target
    # hits the live engine can never take -- it is the root cause of the
    # calibration's inflated win rates. Start at +1.
    entry_time = bars.time[entry_idx + 1]

    for j in range(entry_idx + 1, n):
        high = high_arr[j]
        low = low_arr[j]

        # ── 1. Stop resting at the START of this bar is tested FIRST ────────
        # (conservative intrabar convention — see module docstring)
        if (low <= current_sl) if is_buy else (high >= current_sl):
            # Protective-stop exits incur the same realised slippage the live
            # engine charges (BacktestEngine subtracts actual_slippage_delta =
            # slippage_pips * pip_size from every stop fill). Modelling it here
            # is what makes the calibrated OOS expectancy honest: without it the
            # simulator books breakeven scratches that the engine actually
            # closes as small losses, and the calibration over-promises an edge.
            sl_exit_price = current_sl - direction * slippage_price_equiv
            return _finalise(
                symbol=symbol, side=side, bars=bars, entry_idx=entry_idx,
                exit_idx=j, entry_time=entry_time, fill=fill, exit_price=sl_exit_price,
                sl_initial=sl, sl_final=current_sl, tp=tp_price, risk_dist=risk_dist,
                result=("BE/TRAIL_SL" if (be_locked or partial_taken) else "SL"),
                mfe=mfe, mae=mae, partial_pct=partial_pct, partial_price=partial_price,
                partial_r_gross=partial_r_gross,
                remaining_r_gross=(sl_exit_price - fill) * direction * inv_risk,
                cost_price_equiv=cost_price_equiv, money_per_unit=money_per_unit,
                ai_score=ai_score, calibrated_win_p=calibrated_win_p,
                regime=regime, strategy=strategy, zone=zone,
                partial_taken=partial_taken, be_locked=be_locked,
            )

        # ── 2. Target is tested second ──────────────────────────────────────
        if (high >= tp_price) if is_buy else (low <= tp_price):
            return _finalise(
                symbol=symbol, side=side, bars=bars, entry_idx=entry_idx,
                exit_idx=j, entry_time=entry_time, fill=fill, exit_price=tp_price,
                sl_initial=sl, sl_final=current_sl, tp=tp_price, risk_dist=risk_dist,
                result="TP", mfe=mfe, mae=mae, partial_pct=partial_pct, partial_price=partial_price,
                partial_r_gross=partial_r_gross,
                remaining_r_gross=(tp_price - fill) * direction * inv_risk,
                cost_price_equiv=cost_price_equiv, money_per_unit=money_per_unit,
                ai_score=ai_score, calibrated_win_p=calibrated_win_p,
                regime=regime, strategy=strategy, zone=zone,
                partial_taken=partial_taken, be_locked=be_locked,
            )

        # ── 3. Update excursions and ratchet the policy for the NEXT bar ────
        favourable = (high - fill) if is_buy else (fill - low)
        adverse = (fill - low) if is_buy else (high - fill)
        if favourable > mfe:
            mfe = favourable
        if adverse > mae:
            mae = adverse

        # Time stop: a position that has not produced meaningful progress is
        # capital tied up for no reason.
        # Match Engine behavior: require mfe < 0.25R AND charge slippage
        if (j - entry_idx) >= max_bars and mfe < (risk_dist * 0.25):
            exit_price = close_arr[j] - direction * slippage_price_equiv
            return _finalise(
                symbol=symbol, side=side, bars=bars, entry_idx=entry_idx,
                exit_idx=j, entry_time=entry_time, fill=fill, exit_price=exit_price,
                sl_initial=sl, sl_final=current_sl, tp=tp_price, risk_dist=risk_dist,
                result=f"STAGNATION_TIME_STOP_{max_bars}B", mfe=mfe, mae=mae,
                partial_pct=partial_pct, partial_price=partial_price, partial_r_gross=partial_r_gross,
                remaining_r_gross=(exit_price - fill) * direction * inv_risk,
                cost_price_equiv=cost_price_equiv, money_per_unit=money_per_unit,
                ai_score=ai_score, calibrated_win_p=calibrated_win_p,
                regime=regime, strategy=strategy, zone=zone,
                partial_taken=partial_taken, be_locked=be_locked,
            )

        # Evaluate exit policy (ratchets SL/TP)
        # Engine snapshots bar_start_sl BEFORE ratchet; we snapshot current_sl
        # before calling evaluate_exit, then test the NEW (ratcheted) stop
        # on the NEXT bar, matching Engine's close-based fallback logic.
        bar_start_sl = current_sl
        dec = evaluate_exit(
            side=exit_side,
            entry=fill,
            initial_sl=sl,
            current_sl=current_sl,
            tp=tp_price,
            price=close_arr[j],
            favorable_dist=mfe,
            atr=atr_arr[j],
            policy=policy,
            partial_already_taken=partial_taken,
            be_already_locked=be_locked,
            struct_level=None,
        )

        if dec.partial_close_pct > 0.0 and not partial_taken:
            # Bank the partial at the policy's stated level.
            partial_pct += dec.partial_close_pct
            partial_price = float(dec.partial_price)
            partial_r_gross += ((dec.partial_price - fill) * direction * inv_risk) * dec.partial_close_pct
            partial_taken = True

        if dec.new_sl and dec.new_sl != current_sl:
            if is_buy:
                if dec.new_sl > current_sl:
                    current_sl = dec.new_sl
            else:
                if dec.new_sl < current_sl:
                    current_sl = dec.new_sl

        if dec.be_locked:
            be_locked = True

    # ── Ran out of data: mark to market on the final close ──────────────────
    exit_price = close_arr[n - 1]
    return _finalise(
        symbol=symbol, side=side, bars=bars, entry_idx=entry_idx, exit_idx=n - 1,
        entry_time=entry_time, fill=fill, exit_price=exit_price, sl_initial=sl,
        sl_final=current_sl, tp=tp_price, risk_dist=risk_dist,
        result="CLOSE_AT_END", mfe=mfe, mae=mae, partial_pct=partial_pct, partial_price=partial_price,
        partial_r_gross=partial_r_gross,
        remaining_r_gross=(exit_price - fill) * direction * inv_risk,
        cost_price_equiv=cost_price_equiv, money_per_unit=money_per_unit,
        ai_score=ai_score, calibrated_win_p=calibrated_win_p, regime=regime,
        strategy=strategy, zone=zone, partial_taken=partial_taken, be_locked=be_locked,
    )


def _finalise(
    *,
    symbol: str, side: str, bars: BarArrays, entry_idx: int, exit_idx: int,
    entry_time: Any, fill: float, exit_price: float, sl_initial: float,
    sl_final: float, tp: float, risk_dist: float, result: str, mfe: float,
    mae: float, partial_pct: float, partial_price: float, partial_r_gross: float,
    remaining_r_gross: float, cost_price_equiv: float, money_per_unit: float,
    ai_score: float, calibrated_win_p: float, regime: str, strategy: str,
    zone: str, partial_taken: bool, be_locked: bool,
) -> TradeOutcome:
    """Net off costs and package the outcome.

    The position is split into a banked portion (``partial_pct``) and a runner
    (``1 - partial_pct``); each leg pays cost in proportion to its size. When no
    partial was taken, ``partial_pct`` is 0 and the whole position is the runner,
    so this reduces to a single cost charge.
    """
    runner_pct = 1.0 - partial_pct
    # Cost is charged per unit of position closed, so it scales with each leg.
    total_r_gross = partial_r_gross + remaining_r_gross * runner_pct
    cost_r = cost_price_equiv / risk_dist
    pnl_r = total_r_gross - cost_r

    exit_time = bars.time[exit_idx]

    return TradeOutcome(
        symbol=symbol,
        side=side,
        entry_idx=int(entry_idx),
        entry_time=entry_time,
        exit_idx=int(exit_idx),
        exit_time=exit_time,
        # The position goes live on the bar AFTER the signal (entry_idx + 1), so
        # the holding period is exit_idx - entry_idx, not +1. The old expression
        # overstated every hold by one bar.
        bars_held=int(exit_idx - entry_idx),
        entry=round(float(fill), 8),
        exit=round(float(exit_price), 8),
        sl_initial=round(float(sl_initial), 8),
        sl_final=round(float(sl_final), 8),
        tp=round(float(tp), 8),
        risk_dist=round(float(risk_dist), 8),
        result=result,
        mfe_r=round(mfe / risk_dist, 4),
        mae_r=round(mae / risk_dist, 4),
        pnl_r=round(float(pnl_r), 6),
        pnl_money_per_lot=round(float(pnl_r * risk_dist * money_per_unit), 4),
        is_win=bool(pnl_r > 0),
        partial_taken=bool(partial_taken),
        partial_pct=round(float(partial_pct), 6),
        partial_price=round(float(partial_price), 8),
        partial_r=round(float(partial_r_gross), 6),
        be_locked=bool(be_locked),
        ai_score=float(ai_score),
        calibrated_win_p=float(calibrated_win_p),
        regime=str(regime),
        strategy=str(strategy),
        zone=str(zone),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Portfolio-level replay
# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
# Two-phase replay: simulate once per geometry, then apply selectivity
# ─────────────────────────────────────────────────────────────────────────────
def simulate_all_candidates(
    *,
    df: pd.DataFrame,
    candidates: pd.DataFrame,
    geom: Geometry,
    money_per_unit: float,
    cost_price_equiv: float = 0.0,
    slippage_price_equiv: float = 0.0,
    spec: Any = None,
    symbol: Optional[str] = None,
    score_col: str = "score",
) -> List[TradeOutcome]:
    """Simulate EVERY candidate independently under ``geom`` (no blocking).

    This is the expensive step and it is deliberately separated from the
    selectivity filter. Because a trade's outcome depends only on the geometry
    and the forward path — never on which other trades were taken — the result
    of this call can be reused for every ``min_score`` threshold. That turns a
    ``geometries x thresholds`` search into ``geometries`` simulations plus a
    cheap filtering pass, which is what makes a walk-forward calibration over a
    full universe tractable.

    Simulation is strictly causal (each trade walks forward only), so subsetting
    these outcomes by entry index later introduces no look-ahead.

    ``slippage_price_equiv`` MUST reach ``simulate_trade``. This function
    previously accepted the argument and silently discarded it, so every
    protective-stop exit was booked at the exact stop price while
    ``BacktestEngine`` fills it ``slippage_pips * pip_size`` worse
    (``actual_slippage_delta``). The calibration therefore reported an
    expectancy the engine could not realise. Measured on EURUSD at the deployed
    geometry: 793 of 1113 exits are stop exits, the real 0.5-pip charge is
    0.0220 R per stop, i.e. **-0.0157 R per trade** of systematically omitted
    cost -- against a per-symbol expectancy of a few hundredths of an R. Target
    fills pay no slippage on either path, so the two remain symmetric.
    """
    return [
        o for _, o in simulate_all_candidates_with_rows(
            df=df, candidates=candidates, geom=geom, money_per_unit=money_per_unit,
            cost_price_equiv=cost_price_equiv,
            slippage_price_equiv=slippage_price_equiv,
            spec=spec, symbol=symbol, score_col=score_col,
        )
    ]


def simulate_all_candidates_with_rows(
    *,
    df: pd.DataFrame,
    candidates: pd.DataFrame,
    geom: Geometry,
    money_per_unit: float,
    cost_price_equiv: float = 0.0,
    slippage_price_equiv: float = 0.0,
    spec: Any = None,
    symbol: Optional[str] = None,
    score_col: str = "score",
) -> List[Tuple[int, TradeOutcome]]:
    """``simulate_all_candidates``, but each outcome keeps its candidate's index.

    Entry-edge attribution has to join every outcome back to the feature row
    that produced it -- score, regime, confluence count, and so on. Re-deriving
    that join afterwards by matching on ``(entry_idx, side, fill)`` is fragile:
    two candidates can share a bar, and ``simulate_all_candidates`` drops the
    degenerate ones, so position alone does not identify a row either. The
    pairing is therefore made here, inside the one loop that already exists, so
    the calibration and the diagnostic cannot drift apart.

    Returns ``(candidate_index, outcome)`` pairs in the same order as
    ``simulate_all_candidates``, i.e. ascending ``bar_idx``.
    """
    sym = symbol or (str(candidates["symbol"].iloc[0]) if len(candidates) else "UNKNOWN")
    if candidates is None or len(candidates) == 0:
        return []
    if score_col not in candidates.columns:
        score_col = "score"

    # Extract the price columns ONCE: the inner loop reads scalars out of these
    # arrays instead of building a pandas Series per bar.
    bars = BarArrays.from_df(df)

    paired: List[Tuple[int, TradeOutcome]] = []
    for row in candidates.sort_values("bar_idx").itertuples(index=True):
        outcome = simulate_trade(
            symbol=sym,
            side=str(getattr(row, "side")),
            entry_idx=int(getattr(row, "bar_idx")),
            fill=float(getattr(row, "fill")),
            sl=float(getattr(row, "sl")),
            geom=geom,
            money_per_unit=money_per_unit,
            bars=bars,
            cost_price_equiv=cost_price_equiv,
            slippage_price_equiv=slippage_price_equiv,
            ai_score=float(getattr(row, score_col, 0.0) or 0.0),
            calibrated_win_p=float(getattr(row, "score", 0.0) or 0.0),
            regime=str(getattr(row, "regime", "UNKNOWN")),
            strategy=str(getattr(row, "strategy", "UNKNOWN")),
            zone=str(getattr(row, "zone", "UNKNOWN")),
            spec=spec,
        )
        if outcome is not None:
            paired.append((int(row.Index), outcome))
    return paired


def select_sequential(
    outcomes: Sequence[TradeOutcome],
    *,
    min_score: float = 0.0,
    entry_lo: Optional[int] = None,
    entry_hi: Optional[int] = None,
    regimes: Optional[Set[str]] = None,
) -> List[TradeOutcome]:
    """Apply selectivity + one-position-at-a-time to precomputed outcomes.

    ``entry_lo``/``entry_hi`` restrict to a contiguous index window, which is how
    walk-forward folds are evaluated without re-simulating anything.

    ``regimes`` is the set of regimes the learned policy still permits. Filtering
    happens *before* the one-position-at-a-time walk, so a blocked trade does not
    occupy the slot and a later eligible trade can still be taken. Re-running the
    selection with this filter is how the calibrator reports the numbers the
    engine will actually produce (the engine applies the same policy).
    """
    chosen: List[TradeOutcome] = []
    blocked_until = -1
    for o in sorted(outcomes, key=lambda x: x.entry_idx):
        if entry_lo is not None and o.entry_idx < entry_lo:
            continue
        if entry_hi is not None and o.entry_idx > entry_hi:
            continue
        if regimes is not None and str(o.regime) not in regimes:
            continue
        if o.ai_score < min_score:
            continue
        if o.entry_idx <= blocked_until:
            continue
        chosen.append(o)
        blocked_until = o.exit_idx
    return chosen


def evaluate_geometry(
    *,
    df: pd.DataFrame,
    candidates: pd.DataFrame,
    geom: Geometry,
    money_per_unit: float,
    cost_price_equiv: float = 0.0,
    slippage_price_equiv: float = 0.0,
    spec: Any = None,
    symbol: Optional[str] = None,
    one_position_at_a_time: bool = True,
    score_col: str = "score",
) -> List[TradeOutcome]:
    """Convenience wrapper: simulate a candidate set and select sequentially.

    ``score_col`` selects which recorded confidence signal drives the
    ``min_score`` selectivity filter, so calibration can compare the engine's
    calibrated win probability against its dissection / confluence scores on
    evidence rather than assumption.
    """
    outcomes = simulate_all_candidates(
        df=df, candidates=candidates, geom=geom, money_per_unit=money_per_unit,
        cost_price_equiv=cost_price_equiv, slippage_price_equiv=slippage_price_equiv,
        spec=spec, symbol=symbol, score_col=score_col,
    )
    if not one_position_at_a_time:
        return [o for o in outcomes if o.ai_score >= geom.min_score]
    return select_sequential(outcomes, min_score=geom.min_score)


# ─────────────────────────────────────────────────────────────────────────────
# Statistics
# ─────────────────────────────────────────────────────────────────────────────
def summarise(outcomes: Sequence[TradeOutcome]) -> Dict[str, float]:
    """Win rate, expectancy and payoff for a set of outcomes (all in R)."""
    n = len(outcomes)
    if n == 0:
        return {
            "trades": 0, "win_rate": 0.0, "expectancy_r": 0.0,
            "avg_win_r": 0.0, "avg_loss_r": 0.0, "payoff": 0.0,
            "profit_factor": 0.0, "total_r": 0.0, "max_dd_r": 0.0,
        }
    rs = np.array([o.pnl_r for o in outcomes], dtype=float)
    wins = rs[rs > 0]
    losses = rs[rs < 0]
    win_rate = float(len(wins) / n)
    avg_win = float(wins.mean()) if len(wins) else 0.0
    avg_loss = float(losses.mean()) if len(losses) else 0.0
    gross_profit = float(wins.sum()) if len(wins) else 0.0
    gross_loss = float(-losses.sum()) if len(losses) else 0.0
    profit_factor = (gross_profit / gross_loss) if gross_loss > 1e-12 else (
        99.0 if gross_profit > 0 else 0.0
    )
    # Equity curve in R to obtain a drawdown comparable across symbols.
    curve = np.cumsum(rs)
    peak = np.maximum.accumulate(np.concatenate([[0.0], curve]))
    dd = peak[1:] - curve
    return {
        "trades": n,
        "win_rate": round(win_rate, 4),
        "expectancy_r": round(float(rs.mean()), 4),
        "avg_win_r": round(avg_win, 4),
        "avg_loss_r": round(avg_loss, 4),
        "payoff": round(abs(avg_win / avg_loss), 3) if avg_loss < 0 else 0.0,
        "profit_factor": round(profit_factor, 3),
        "total_r": round(float(rs.sum()), 3),
        "max_dd_r": round(float(dd.max()) if len(dd) else 0.0, 3),
    }


__all__ = [
    "Geometry",
    "TradeOutcome",
    "BarArrays",
    "simulate_trade",
    "simulate_all_candidates",
    "simulate_all_candidates_with_rows",
    "select_sequential",
    "evaluate_geometry",
    "summarise",
]
