"""The split, enforced.
Deterministic layer: exact arithmetic, hard metrics, safety and policy.
Probabilistic layer: fuzzy conditions, order-quality judgments, execution-health judgments.
Everything in the hybrid trading architecture is built around that split;
this module is the guard that keeps the probabilistic layer honest.
It ensures that the model is NEVER asked to calculate arithmetic or perform accounting.
"""
from __future__ import annotations

# question id -> (question type, description of judgment)
ALLOWED_QUESTIONS: dict[str, tuple[str, str]] = {
    "regime": (
        "choice",
        "market regime classification: trending, mean_reverting, high_vol, or crisis",
    ),
    "direction": (
        "choice",
        "price bias over the next few ticks/bars: up, down, or neutral",
    ),
    "toxic_flow": (
        "noul",
        "is aggressive order flow informed rather than noise (0.0 to 1.0)",
    ),
    "liquidity_stressed": (
        "noul",
        "is the book or spread stressed beyond recent norms (0.0 to 1.0)",
    ),
    "quote_environment": (
        "score",
        "favourability for providing liquidity or taking limit entries (0 to 3)",
    ),
    "inventory_pressure": (
        "score",
        "urgency to cut or skew the current position (0 to 3)",
    ),
    "execution_health": (
        "score",
        "whether broker execution quality is optimal or degrading (0 to 3)",
    ),
}

# Substrings that mean "this is asking the AI to do arithmetic", banned in instructions.
_ARITHMETIC_MARKERS = (
    "calculate",
    "compute the",
    "what is the exact",
    "sum of",
    "average of",
    "mean of",
    "add up",
    "multiply",
    "divide by",
    "vwap",
    "moving average",
    "standard deviation",
    "variance of",
    "exact value",
    "precise value",
    "spread in bps",
    "mid price of",
)


class SplitViolation(Exception):
    """Raised when a question sent to the probabilistic model is off the allow-list,
    or its instructions look like a request for a computable quantity instead of a judgment."""
    pass


def _instructions_text(instructions) -> str:
    if isinstance(instructions, str):
        return instructions
    if isinstance(instructions, dict):
        return " ".join(_instructions_text(v) for v in instructions.values())
    if isinstance(instructions, list):
        return " ".join(_instructions_text(v) for v in instructions)
    return str(instructions)


def assert_split_respected(questions: dict) -> None:
    """Raise SplitViolation if any question is off the allow-list, or its
    instructions contain an arithmetic marker. Checked before any battery is fired."""
    for qid, question in questions.items():
        if qid not in ALLOWED_QUESTIONS:
            raise SplitViolation(
                f"Question '{qid}' is not on the allow-list in split_guard.py. "
                "Only qualitative judgments are permitted in the probabilistic layer."
            )
        text = _instructions_text(question.get("instructions", "")).lower()
        for marker in _ARITHMETIC_MARKERS:
            if marker in text:
                raise SplitViolation(
                    f"Question '{qid}' looks like it asks the model to do arithmetic "
                    f"(found '{marker}'). Compute it deterministically in code instead."
                )


def deterministic_responsibilities() -> list[tuple[str, str]]:
    """(what code owns, module name)."""
    return [
        (
            "Exact arithmetic: mid-price, microprice, bid-ask spread, order book imbalance",
            "state_snapshot.py",
        ),
        (
            "Technical indicators: AQDC channels, EMA 5/13, ATR 14, Session VWAP",
            "dynamic_levels.py, order_flow.py",
        ),
        (
            "Hard metrics: live inventory, account drawdown vs peak, realized/unrealized P&L",
            "risk_engine.py, drawdown.py",
        ),
        (
            "Safety and policy: hard stop-losses, 9-limit risk vetoes, order routing",
            "fallback_ladder.py, risk_engine.py, mt5_client.py",
        ),
    ]


def probabilistic_responsibilities() -> list[tuple[str, str]]:
    """(what AI owns, battery questions)."""
    return [
        (
            "Fuzzy conditions: trending, mean reverting, high vol, crisis",
            "decision_battery.py: regime, direction",
        ),
        (
            "Order flow quality: is flow toxic/informed or noise, liquidity stress",
            "decision_battery.py: toxic_flow, liquidity_stressed",
        ),
        (
            "Execution & inventory health: quote environment, inventory pressure, execution health",
            "decision_battery.py: quote_environment, inventory_pressure, execution_health",
        ),
    ]
