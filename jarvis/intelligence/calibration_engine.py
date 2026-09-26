"""Brier Score & Probability Calibration Engine.
Evaluates whether model confidence (e.g. 80%) translates to real empirical hit rate (80%).
Pairs predicted directional confidence with realized price movements N bars forward.
Computes Brier Score, Expected Calibration Error (ECE), and 10-bin reliability curves.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass
class PredictionRecord:
    timestamp: float
    symbol: str
    direction: str # "up" or "down"
    confidence: float # 0.0 to 1.0
    start_mid: float
    horizon_bars: int
    evaluated: bool = False
    outcome_mid: float | None = None
    was_correct: int | None = None # 1 or 0


class CalibrationEngine:
    """Manages forward outcome evaluation and calibration tables."""
    def __init__(self, horizon_bars: int = 5):
        self.horizon_bars = horizon_bars
        self.pending_predictions: list[PredictionRecord] = []
        self.completed_predictions: list[PredictionRecord] = []

    def record_prediction(
        self,
        timestamp: float,
        symbol: str,
        direction: str,
        confidence: float,
        start_mid: float,
    ) -> None:
        """Register a new direction prediction to be evaluated after horizon_bars."""
        if direction.lower() not in ("up", "down"):
            return
        if start_mid <= 0 or not (0.0 <= confidence <= 1.0):
            return

        rec = PredictionRecord(
            timestamp=timestamp,
            symbol=symbol,
            direction=direction.lower(),
            confidence=confidence,
            start_mid=start_mid,
            horizon_bars=self.horizon_bars,
        )
        self.pending_predictions.append(rec)

    def update_outcomes(self, current_mid: float, bars_elapsed: int = 1) -> None:
        """Evaluate pending predictions once their horizon has passed."""
        still_pending = []
        for pred in self.pending_predictions:
            pred.horizon_bars -= bars_elapsed
            if pred.horizon_bars <= 0:
                pred.evaluated = True
                pred.outcome_mid = current_mid
                moved_up = current_mid > pred.start_mid
                pred.was_correct = 1 if (moved_up == (pred.direction == "up")) else 0
                self.completed_predictions.append(pred)
            else:
                still_pending.append(pred)
        self.pending_predictions = still_pending

    def brier_score(self) -> float:
        """Calculates Brier score: 0 = perfect, 0.25 = random coin flip, 1 = always wrong."""
        if not self.completed_predictions:
            return float("nan")
        total = sum(
            (p.confidence - (p.was_correct or 0)) ** 2
            for p in self.completed_predictions
        )
        return round(total / len(self.completed_predictions), 4)

    def reliability_table(self, n_bins: int = 10) -> list[dict[str, Any]]:
        """Generates 10-bin reliability table: stated confidence vs empirical accuracy."""
        bins: list[list[PredictionRecord]] = [[] for _ in range(n_bins)]
        for p in self.completed_predictions:
            idx = min(n_bins - 1, int(p.confidence * n_bins))
            bins[idx].append(p)

        rows = []
        for i, b in enumerate(bins):
            lo, hi = i / n_bins, (i + 1) / n_bins
            if b:
                mean_conf = sum(p.confidence for p in b) / len(b)
                empirical_acc = sum((p.was_correct or 0) for p in b) / len(b)
            else:
                mean_conf = float("nan")
                empirical_acc = float("nan")

            rows.append({
                "bin": f"{lo:.1f}-{hi:.1f}",
                "count": len(b),
                "mean_confidence": round(mean_conf, 4) if not math.isnan(mean_conf) else None,
                "empirical_hit_rate": round(empirical_acc, 4) if not math.isnan(empirical_acc) else None,
            })
        return rows

    def expected_calibration_error(self, n_bins: int = 10) -> float:
        """Computes Expected Calibration Error (ECE)."""
        table = self.reliability_table(n_bins=n_bins)
        total_samples = len(self.completed_predictions)
        if total_samples == 0:
            return 0.0

        ece = 0.0
        for row in table:
            n = row["count"]
            if n > 0 and row["mean_confidence"] is not None and row["empirical_hit_rate"] is not None:
                diff = abs(row["mean_confidence"] - row["empirical_hit_rate"])
                ece += (n / total_samples) * diff
        return round(ece, 4)

    def to_dashboard_dict(self) -> dict[str, Any]:
        """Summary for UI dashboard rendering."""
        return {
            "total_decisions": len(self.completed_predictions),
            "pending_evaluations": len(self.pending_predictions),
            "brier_score": self.brier_score(),
            "ece": self.expected_calibration_error(),
            "reliability_table": self.reliability_table(),
        }
