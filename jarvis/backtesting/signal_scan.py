"""
HM Algo 2.0 — Signal Scanner.

The live decision pipeline is *extremely* selective: on three months of real H1
EURUSD it evaluated ~1,600 bars and executed 4 trades. Four samples cannot
support any statistical claim, let alone a per-symbol 75% win-rate target.

The scanner solves the sample-size problem without weakening the production
pipeline. It runs the SAME production path —

    MarketContextEngine -> MarketRegimeClassifier -> ParallelAnalystCluster
                        -> DecisionEngine.evaluate()

— but instead of only acting on ``EXECUTE``, it records *every bar on which the
pipeline formed a directional view*, together with the entry/stop/target it
would have used and the full gate outcome. That yields ~1,200 candidates per
symbol instead of 4 trades.

Nothing about the recorded candidates is synthetic: the bias, the levels, the
score, the regime and the failing gates all come from the real engine reading
real MT5 bars. The scanner only declines to *discard* the setups the gate stack
rejected, so that calibration can measure whether the rejections were correct.

Output is one row per candidate with a stable, documented schema so that
calibration, the simulator and the reporting layer share a single contract.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from jarvis.analysts.parallel_runner import ParallelAnalystCluster
from jarvis.backtesting.fills import entry_fill
from jarvis.data.symbol_registry import resolve as resolve_symbol
from jarvis.intelligence.decision_engine import DecisionEngine
from jarvis.intelligence.gate_policy import HARD_GATES
from jarvis.intelligence.gate_registry import canonical_gate
from jarvis.intelligence.regime_engine import MarketRegimeClassifier
from jarvis.market.market_context import MarketContextEngine

logger = logging.getLogger("JARVIS_SignalScan")


# Stable column contract shared by scanner -> simulator -> calibrator -> report.
#
# NOTE ON ``score``: DecisionObject exposes ``model_confidence``, which the
# decision engine assigns from ``calibrated_win_p``. There is therefore no
# separate "blended AI score" on the object, and inventing one here would be
# fabricating a signal. ``score`` is defined as the calibrated win probability
# (0-1) — the engine's own confidence estimate — and the two genuinely distinct
# alternates the engine does expose (``dissection_score``, ``master_score``) are
# carried alongside so calibration can choose between them on evidence.
CANDIDATE_COLUMNS: List[str] = [
    "symbol", "bar_idx", "time", "side",
    "entry", "fill", "sl", "tp", "risk_dist", "rr",
    "atr", "spread_pips",
    "score", "dissection_score", "master_score", "ev", "adversarial_penalty",
    "meta_label_prob", "mtf_confluence_score",
    "regime", "strategy", "zone", "trend_score", "confluence_count",
    "gate_passed", "hard_gate_failed", "n_failed_gates", "failing_gates",
    "decision", "order_type",
]

# Selectivity variables calibration may threshold on, in preference order.
SCORE_COLUMNS: List[str] = ["score", "master_score", "dissection_score"]


@dataclass
class ScanResult:
    symbol: str
    candidates: pd.DataFrame
    bars: int
    scanned: int
    executed: int
    gate_pass: int
    hard_gate_fail: int

    def summary(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "bars": self.bars,
            "scanned": self.scanned,
            "candidates": len(self.candidates),
            "executed_by_pipeline": self.executed,
            "gate_pass": self.gate_pass,
            "hard_gate_fail": self.hard_gate_fail,
        }


def _point_size(digits: int) -> float:
    return 10.0 ** (-int(digits))


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder ATR over H1 bars.

    Computed here (rather than relying on the context engine's per-bar value)
    so the scanner, the simulator and the backtest all trail off an identical
    series. ``min_periods`` is set so the first ``period`` bars are still usable.
    """
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=1).mean()


class SignalScanner:
    """Records every directional candidate the production pipeline forms."""

    def __init__(
        self,
        *,
        spread_pips: Optional[float] = None,
        start_bar_idx: int = 60,
        min_history: int = 60,
        offline: bool = True,
    ):
        # ``spread_pips=None`` means "use the real per-bar spread from the data".
        self.spread_pips = spread_pips
        self.start_bar_idx = start_bar_idx
        self.min_history = min_history
        # A backtest must be hermetic: no reading live trade history, no writing
        # model weights. Without this, repeated runs disagreed with each other.
        self.offline = offline

        self.context_engine = MarketContextEngine()
        self.regime_classifier = MarketRegimeClassifier()
        # The analyst fan-out runs in the cluster's production mode
        # (parallel=True) — the same path the live decision engine, the
        # orchestrator and the backtest engine use. `parallel_analysts` used to be
        # a toggle here, but it defaulted to False and no caller ever set it True,
        # so the ThreadPoolExecutor branch was dead code for the scan; and because
        # the cluster's two modes only differ in *failure* semantics (parallel
        # swallows an analyst exception into a score-50 fallback; sequential
        # propagates it), the flag was a latent landmine — flipping it would
        # silently change which scan results survive. The scan already wraps the
        # whole pipeline call in a try/except that skips a bad bar, so the cluster
        # mode is moot for robustness. Running parallel keeps the scan faithful to
        # production: its entire purpose is "the SAME production path", and a
        # sequential cluster is a path production never takes.
        self.analyst_cluster = ParallelAnalystCluster()
        self.decision_engine = DecisionEngine()

    # ── helpers ────────────────────────────────────────────────────────────
    @staticmethod
    def _resample(df: pd.DataFrame) -> tuple:
        indexed = df.copy()
        indexed["time"] = pd.to_datetime(indexed["time"])
        indexed = indexed.set_index("time")
        agg = {"open": "first", "high": "max", "low": "min", "close": "last"}
        for vol in ("volume", "tick_volume"):
            if vol in indexed.columns:
                agg[vol] = "sum"
        # label="right" stamps each bucket with its CLOSE time so the
        # `time <= bar_time` filter in _scan_impl can only admit a bucket that
        # has already finished. With pandas' default label="left" an in-progress
        # bucket - which here already aggregates the whole series, future bars
        # included - was visible to the decision and contaminated d1_bias/h4_bias.
        h4 = indexed.resample("4h", closed="left", label="right").agg(agg).dropna().reset_index()
        d1 = indexed.resample("1D", closed="left", label="right").agg(agg).dropna().reset_index()
        return h4, d1

    def _spread_for_bar(self, row: pd.Series, spec: Any, fallback: float) -> float:
        if self.spread_pips is not None:
            return float(self.spread_pips)
        raw = row.get("spread", None)
        if raw is None or not np.isfinite(float(raw)):
            return fallback
        pip = float(getattr(spec, "pip_size", 0.0001) or 0.0001)
        pt = _point_size(int(getattr(spec, "digits", 5) or 5))
        if pip <= 0 or pt <= 0:
            return fallback
        return float(raw) * pt / pip

    # ── main entry point ───────────────────────────────────────────────────
    def scan(self, df_h1: pd.DataFrame, symbol: str) -> ScanResult:
        """Run the production pipeline over ``df_h1`` and record candidates.

        Wraps the pass in :func:`jarvis.config.runtime.offline_mode` so the
        scan is hermetic and reproducible.
        """
        from jarvis.config.runtime import offline_mode

        if not self.offline:
            return self._scan_impl(df_h1, symbol)
        with offline_mode():
            return self._scan_impl(df_h1, symbol)

    def _scan_impl(self, df_h1: pd.DataFrame, symbol: str) -> ScanResult:
        """The actual pass. See :meth:`scan` for the public contract."""
        if df_h1 is None or len(df_h1) < self.min_history + 5:
            return ScanResult(symbol, pd.DataFrame(columns=CANDIDATE_COLUMNS),
                              len(df_h1) if df_h1 is not None else 0, 0, 0, 0, 0)

        df = df_h1.reset_index(drop=True).copy()
        df["time"] = pd.to_datetime(df["time"])
        df["atr"] = compute_atr(df, 14)

        spec = resolve_symbol(symbol)
        h4, d1 = self._resample(df)

        fallback_spread = float(getattr(spec, "typical_spread_pips", 2.0) or 2.0)
        total = len(df)
        start = max(self.start_bar_idx, self.min_history)

        rows: List[Dict[str, Any]] = []
        executed = 0
        gate_pass = 0
        hard_gate_fail = 0
        scanned = 0

        for i in range(start, total - 1):
            scanned += 1
            history = df.iloc[max(0, i - 300): i]
            current = df.iloc[i]
            nxt = df.iloc[i + 1]
            bar_time = current["time"]

            h4_slice = h4[h4["time"] <= bar_time].iloc[-100:]
            d1_slice = d1[d1["time"] <= bar_time].iloc[-50:]
            mtf = {"primary": history, "context": h4_slice, "macro": d1_slice}

            spread_pips = self._spread_for_bar(current, spec, fallback_spread)
            try:
                context = self.context_engine.build_context(
                    symbol, mtf,
                    current_spread_pips=spread_pips,
                    max_allowed_spread_pips=spec.max_spread_pips,
                )
                regime = self.regime_classifier.classify_regime(context)
                tentative = (
                    "BUY" if context.structure.bias == "BULLISH"
                    else ("SELL" if context.structure.bias == "BEARISH"
                          else ("SELL" if getattr(context.momentum, "trend_score", 0.0) < 0 else "BUY"))
                )
                reports, devil = self.analyst_cluster.run_all_parallel(context, regime, tentative)
                decision = self.decision_engine.evaluate(
                    context, regime, reports, devil,
                    account_balance=10_000.0, risk_per_trade_pct=0.5, mtf_data=mtf,
                )
            except Exception as exc:  # never let one bad bar kill a 40-minute scan
                logger.warning(f"[{symbol}] bar {i} pipeline error: {exc}")
                continue

            if decision.decision == "EXECUTE":
                executed += 1
            if getattr(decision.quality_gate, "passed", False):
                gate_pass += 1

            bias = str(decision.bias or "HOLD").upper()
            if bias not in ("BUY", "SELL"):
                continue

            failing = list(getattr(decision.quality_gate, "failing_reasons", []) or [])
            hard_failed = [g for g in failing if canonical_gate(g) in HARD_GATES]
            if hard_failed:
                hard_gate_fail += 1

            # Mirror the engine's fill convention exactly: enter at the NEXT
            # bar's open, paying the spread on the ask for a long AND on the bid
            # for a short.
            #
            # The short leg used to be free here. `entry_price = next_open` with
            # the spread added only for BUY meant every SELL candidate entered at
            # the mid, and the simulator applies no spread on exits either -- so
            # half of ~95k audited trades carried no spread cost at all. Measured
            # on the stored tables: (fill - next_open)/pip divided by the row's
            # own spread was +1.000 for BUY and 0.000 for SELL, on every symbol
            # checked. `engine.py` already had the fix and the comment explaining
            # it; the scanner was missed. Both now call `entry_fill` so there is
            # only one copy of the convention left to get wrong.
            entry_price = entry_fill(float(nxt["open"]), bias, spread_pips, spec.pip_size)
            price_shift = entry_price - float(decision.entry_price)
            sl_price = float(decision.stop_loss) + price_shift
            tp_price = float(decision.take_profit) + price_shift
            risk_dist = abs(entry_price - sl_price)
            if risk_dist <= 0:
                continue

            regime_val = getattr(regime.primary_regime, "value", str(regime.primary_regime))
            confluence_count = 0
            if bool(getattr(context.structure, "bos", False)):
                confluence_count += 1
            if bool(getattr(context.liquidity, "sweep_detected", False)):
                confluence_count += 1
            if abs(float(getattr(context.momentum, "trend_score", 0.0) or 0.0)) >= 20.0:
                confluence_count += 1
            if abs(float(getattr(context, "mtf_confluence_score", 0.0) or 0.0)) >= 30.0:
                confluence_count += 1

            rows.append({
                "symbol": symbol,
                "bar_idx": int(i),
                "time": bar_time,
                "side": bias,
                "entry": round(float(decision.entry_price), 8),
                "fill": round(entry_price, 8),
                "sl": round(sl_price, 8),
                "tp": round(tp_price, 8),
                "risk_dist": round(risk_dist, 8),
                "rr": round(float(getattr(decision, "risk_reward_ratio", 0.0) or 0.0), 4),
                "atr": round(float(current["atr"]), 8),
                "spread_pips": round(spread_pips, 4),
                "score": round(float(getattr(decision, "model_confidence", 0.0) or 0.0), 6),
                "dissection_score": round(float(getattr(decision, "dissection_score", 0.0) or 0.0), 4),
                "master_score": round(float(getattr(decision, "master_confluence_score", 0.0) or 0.0), 4),
                "ev": round(float(getattr(decision, "expected_value", 0.0) or 0.0), 4),
                "adversarial_penalty": round(float(getattr(decision, "adversarial_penalty", 0.0) or 0.0), 4),
                "meta_label_prob": (
                    float(decision.meta_label_prob)
                    if getattr(decision, "meta_label_prob", None) is not None else -1.0
                ),
                # The arbiter's confluence input is ``master_confluence_score``,
                # falling back to the context's ``mtf_confluence_score`` when the
                # former is zero. Storing the fallback explicitly is what makes
                # the arbiter's utility score exactly reconstructible offline
                # instead of approximated.
                "mtf_confluence_score": round(
                    float(getattr(context, "mtf_confluence_score", 0.0) or 0.0), 4
                ),
                "regime": str(regime_val),
                "strategy": str(getattr(decision, "strategy", "UNKNOWN")),
                "zone": str(getattr(getattr(context, "structure", None), "discount_premium_zone", "UNKNOWN")),
                "trend_score": float(getattr(context.momentum, "trend_score", 0.0) or 0.0),
                "confluence_count": int(confluence_count),
                "gate_passed": bool(getattr(decision.quality_gate, "passed", False)),
                "hard_gate_failed": bool(hard_failed),
                "n_failed_gates": len(failing),
                "failing_gates": "|".join(failing),
                "decision": str(getattr(decision, "decision", "NO_TRADE")),
                "order_type": str(getattr(decision, "order_type", "MARKET")),
            })

        frame = pd.DataFrame(rows, columns=CANDIDATE_COLUMNS) if rows else pd.DataFrame(columns=CANDIDATE_COLUMNS)
        return ScanResult(
            symbol=symbol,
            candidates=frame,
            bars=total,
            scanned=scanned,
            executed=executed,
            gate_pass=gate_pass,
            hard_gate_fail=hard_gate_fail,
        )


__all__ = ["SignalScanner", "ScanResult", "CANDIDATE_COLUMNS", "compute_atr"]
