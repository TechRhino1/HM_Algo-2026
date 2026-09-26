"""The 7-Question Decision Battery.
One call, one latency, seven typed answers evaluated against the deterministic snapshot.
The model answers only qualitative conditions; composition and action execution stay in Python.
"""
from __future__ import annotations

import math
import os
import random
import time
from dataclasses import dataclass
from typing import Any

from .split_guard import assert_split_respected

REQUIRED_ANSWER_KEYS = {
    "noul": {"type", "noul"},
    "choice": {"type", "choice", "probabilities", "confidence"},
    "score": {"type", "score", "legend", "probabilities", "confidence"},
}


def build_battery_questions() -> dict[str, Any]:
    """Construct the 7 atomic questions and verify against the split guard."""
    questions = {
        "regime": {
            "type": "choice",
            "instructions": "What market regime does this state describe?",
            "criteria": {
                "trending": "Persistent directional price movement with low retracements",
                "mean_reverting": "Bounded oscillations between local liquidity extremes",
                "high_vol": "Erratic expansion with wide candles and elevated spread",
                "crisis": "Severe market dislocation or extreme gap/liquidity vacuum",
            },
        },
        "direction": {
            "type": "choice",
            "instructions": "What is the primary directional bias over the next 5-10 bars?",
            "criteria": {
                "up": "Strong bullish momentum or accumulation",
                "down": "Strong bearish distribution or sell pressure",
                "neutral": "No clear directional edge or balanced order book",
            },
        },
        "toxic_flow": {
            "type": "noul",
            "instructions": "Is aggressive order flow informed/adverse rather than harmless retail noise?",
        },
        "liquidity_stressed": {
            "type": "noul",
            "instructions": "Is liquidity deteriorating or book depth unusually thin compared to recent norms?",
        },
        "quote_environment": {
            "type": "score",
            "instructions": "How favourable is the current environment for providing resting limit orders?",
            "criteria": [
                "0: Do not quote / extreme adverse selection risk",
                "1: Marginal / quote wide with defense",
                "2: Standard / acceptable spread and stability",
                "3: Excellent / tight spread with stable two-way flow",
            ],
        },
        "inventory_pressure": {
            "type": "score",
            "instructions": "Given current inventory and drawdown, how urgent is it to cut exposure?",
            "criteria": [
                "0: None / zero or low inventory",
                "1: Mild / position within normal parameters",
                "2: Skew hard / actively reduce position",
                "3: Urgent / liquidate immediately to avoid catastrophic loss",
            ],
        },
        "execution_health": {
            "type": "score",
            "instructions": "Given fill ratio, slippage, and latency, is broker execution healthy?",
            "criteria": [
                "0: Broken / frequent rejects or severe slippage",
                "1: Degraded / latency spikes or elevated spread",
                "2: Normal / within expected execution parameters",
                "3: Optimal / instant fills with minimal slippage",
            ],
        },
    }
    assert_split_respected(questions)
    return questions


def validate_battery_answers(answers: dict[str, Any]) -> None:
    """Validate that every question was answered with its required type schema."""
    questions = build_battery_questions()
    for key, q in questions.items():
        if key not in answers:
            raise ValueError(f"Battery response is missing answer for '{key}'")
        ans = answers[key]
        expected_keys = REQUIRED_ANSWER_KEYS[q["type"]]
        missing = expected_keys - set(ans.keys())
        if missing:
            raise ValueError(f"Answer for '{key}' is missing required fields {missing}")


@dataclass
class DecisionBatteryClient:
    """Abstract client for evaluating the decision battery."""
    name: str
    model: str

    def ask(self, state: dict[str, Any], questions: dict[str, Any], timeout: float) -> tuple[dict[str, Any], dict[str, Any]]:
        raise NotImplementedError


class MockDecisionBatteryClient(DecisionBatteryClient):
    """High-speed coherent mock client for tests and zero-dependency offline runs."""
    def __init__(self, seed: int | None = 42):
        super().__init__(name="MOCK_BATTERY", model="mock-battery-v1")
        self._rng = random.Random(seed)

    def ask(self, state: dict[str, Any], questions: dict[str, Any], timeout: float) -> tuple[dict[str, Any], dict[str, Any]]:
        t0 = time.monotonic()
        imbalance = state.get("imbalance") or 0.0
        ret_5m = state.get("return_5m") or 0.0
        vol = state.get("realised_vol_short") or 0.001
        inv = abs(state.get("inventory") or 0.0)
        rejects = state.get("reject_count") or 0

        # Coherent mock evaluation
        toxic = max(0.0, min(1.0, 0.3 + abs(imbalance) * 0.4 + self._rng.uniform(-0.1, 0.1)))
        liquidity_stressed = max(0.0, min(1.0, (vol * 500.0) + self._rng.uniform(-0.1, 0.1)))

        # Direction based on return & imbalance
        if ret_5m > 0.0005 or imbalance > 0.3:
            dir_choice, dir_conf = "up", min(0.95, 0.6 + ret_5m * 100)
            dir_probs = {"up": round(dir_conf, 4), "down": round((1 - dir_conf) * 0.4, 4), "neutral": round((1 - dir_conf) * 0.6, 4)}
        elif ret_5m < -0.0005 or imbalance < -0.3:
            dir_choice, dir_conf = "down", min(0.95, 0.6 + abs(ret_5m) * 100)
            dir_probs = {"up": round((1 - dir_conf) * 0.4, 4), "down": round(dir_conf, 4), "neutral": round((1 - dir_conf) * 0.6, 4)}
        else:
            dir_choice, dir_conf = "neutral", 0.70
            dir_probs = {"up": 0.15, "down": 0.15, "neutral": 0.70}

        # Regime
        if vol > 0.005:
            reg_choice, reg_conf = "high_vol", 0.85
        elif abs(ret_5m) > 0.002:
            reg_choice, reg_conf = "trending", 0.80
        else:
            reg_choice, reg_conf = "mean_reverting", 0.75
        reg_probs = {"trending": 0.25, "mean_reverting": 0.25, "high_vol": 0.25, "crisis": 0.25}
        reg_probs[reg_choice] = round(reg_conf, 4)
        rem = round((1.0 - reg_conf) / 3.0, 4)
        for k in reg_probs:
            if k != reg_choice:
                reg_probs[k] = rem

        # Quote environment (0 to 3)
        quote_score = max(0.0, min(3.0, 2.5 - toxic * 1.5 - liquidity_stressed * 1.0))
        inv_score = max(0.0, min(3.0, inv * 5.0))
        exec_score = max(0.0, min(3.0, 2.8 - rejects * 0.5))

        answers = {
            "regime": {
                "type": "choice",
                "choice": reg_choice,
                "probabilities": reg_probs,
                "confidence": round(reg_conf, 4),
            },
            "direction": {
                "type": "choice",
                "choice": dir_choice,
                "probabilities": dir_probs,
                "confidence": round(dir_conf, 4),
            },
            "toxic_flow": {"type": "noul", "noul": round(toxic, 4)},
            "liquidity_stressed": {"type": "noul", "noul": round(liquidity_stressed, 4)},
            "quote_environment": {
                "type": "score",
                "score": round(quote_score, 2),
                "legend": {"0": "Do not quote", "1": "Marginal", "2": "Standard", "3": "Excellent"},
                "probabilities": {"0": 0.05, "1": 0.15, "2": 0.50, "3": 0.30},
                "confidence": 0.82,
            },
            "inventory_pressure": {
                "type": "score",
                "score": round(inv_score, 2),
                "legend": {"0": "None", "1": "Mild", "2": "Skew hard", "3": "Reduce now"},
                "probabilities": {"0": 0.7, "1": 0.2, "2": 0.08, "3": 0.02},
                "confidence": 0.88,
            },
            "execution_health": {
                "type": "score",
                "score": round(exec_score, 2),
                "legend": {"0": "Broken", "1": "Degraded", "2": "Normal", "3": "Optimal"},
                "probabilities": {"0": 0.02, "1": 0.08, "2": 0.30, "3": 0.60},
                "confidence": 0.90,
            },
        }

        latency_ms = (time.monotonic() - t0) * 1000.0
        meta = {
            "route": self.name,
            "model": self.model,
            "latency_ms": round(latency_ms, 2),
        }
        return answers, meta


def run_decision_battery(
    client: DecisionBatteryClient,
    state: dict[str, Any],
    timeout: float = 2.0,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run the 7-question battery against the deterministic snapshot."""
    questions = build_battery_questions()
    answers, meta = client.ask(state, questions, timeout=timeout)
    validate_battery_answers(answers)
    return answers, meta
