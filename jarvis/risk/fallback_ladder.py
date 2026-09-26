"""The 5-Rung Operational Fallback Ladder.
Provides industrial-grade resilience:
healthy + high confidence -> RUN (normal operation)
healthy + low confidence or degraded execution -> REDUCE (smaller size / 50% scale)
decision late past deadline -> HOLD_LATE (never trade or quote on stale state)
probabilistic layer down -> RULES_ONLY (pure deterministic technical rules)
hard limit breached -> KILL (emergency flatten, cancel orders, abort)
"""
from __future__ import annotations

from enum import Enum
from typing import Any


class FallbackRung(str, Enum):
    RUN = "run"
    REDUCE = "reduce"
    HOLD_LATE = "hold_late"
    RULES_ONLY = "rules_only"
    KILL = "kill"


def select_fallback_rung(
    *,
    risk_kill: bool,
    decision_late: bool,
    ai_down: bool,
    decision_confidence: float | None = None,
    low_confidence_threshold: float = 0.50,
    execution_health_score: float | None = None,
    execution_health_floor: float = 1.0,
) -> FallbackRung:
    """Select the active operational rung based on safety priority."""
    # 1. Hard risk kill always wins unconditionally
    if risk_kill:
        return FallbackRung.KILL

    # 2. Late past deadline: never execute on stale market state
    if decision_late:
        return FallbackRung.HOLD_LATE

    # 3. AI provider unreachable: drop to deterministic rules only
    if ai_down:
        return FallbackRung.RULES_ONLY

    # 4. Low decision confidence -> REDUCE sizing
    if decision_confidence is not None and decision_confidence < low_confidence_threshold:
        return FallbackRung.REDUCE

    # 5. Degraded execution health (< 1.0 out of 3.0) -> REDUCE sizing
    if execution_health_score is not None and execution_health_score < execution_health_floor:
        return FallbackRung.REDUCE

    # 6. All systems green and confident
    return FallbackRung.RUN


class FallbackLadderState:
    """Tracks operational rung transitions and sizing multipliers."""
    def __init__(self, reduce_factor: float = 0.5):
        self.current_rung = FallbackRung.RUN
        self.reduce_factor = reduce_factor
        self.last_reason = "System initialized"

    def evaluate(
        self,
        risk_kill: bool,
        decision_late: bool,
        ai_down: bool,
        confidence: float | None = None,
        execution_health: float | None = None,
    ) -> FallbackRung:
        self.current_rung = select_fallback_rung(
            risk_kill=risk_kill,
            decision_late=decision_late,
            ai_down=ai_down,
            decision_confidence=confidence,
            execution_health_score=execution_health,
        )
        return self.current_rung

    @property
    def sizing_multiplier(self) -> float:
        """Returns position sizing multiplier for the current rung."""
        if self.current_rung == FallbackRung.RUN:
            return 1.0
        elif self.current_rung == FallbackRung.REDUCE:
            return self.reduce_factor
        elif self.current_rung == FallbackRung.RULES_ONLY:
            return self.reduce_factor * 0.8
        else:
            # HOLD_LATE or KILL
            return 0.0
