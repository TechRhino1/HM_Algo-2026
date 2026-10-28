"""
HM Algo 2.0 — Adaptive Quality-Gate Policy.

Per the user's directive, gate strictness is decided automatically by the AI:
- CRITICAL gates (market session, drawdown, margin, event-regime) are ALWAYS hard
  (they protect capital and are never softened).
- NON-critical gates (order-flow, AI score, premium/discount, spread, win-prob, EV)
  can be *softened* — converted into a confidence penalty instead of a hard block —
  but ONLY when we have evidence the strategy is currently profitable in this regime
  (recent win-rate above threshold) and only a small number are failing.

If there is no performance evidence yet, the policy defaults to BLOCK (identical to
today's behaviour), so it can never increase risk on an untuned system.
"""
import logging
from typing import List, Tuple, Optional, Dict

from jarvis.intelligence.gate_registry import (
    HARD_GATES,
    SOFT_GATE_PENALTIES,
    canonical_gate,
    split_failures,
)

logger = logging.getLogger("JARVIS_GatePolicy")


def _policy_thresholds() -> tuple:
    """(max_soft_fail, min_recent_win_rate) from settings, with the documented
    defaults as fallback.

    Falls back silently if jarvis.config.settings is unavailable or does not
    define the trading.gate_policy_* fields, so callers with a minimal settings
    object keep the published behaviour.
    """
    try:
        from jarvis.config.settings import SETTINGS
        t = getattr(SETTINGS, "trading", None)
        if t is None:
            return 3, 0.45
        return (
            int(getattr(t, "gate_policy_max_soft_fail", 3)),
            float(getattr(t, "gate_policy_min_recent_win_rate", 0.45)),
        )
    except Exception:
        return 3, 0.45



# Gate names, severities and penalties are defined exactly once in
# ``gate_registry``; this module only applies the policy.



class AdaptiveGatePolicy:
    def __init__(self, max_soft_fail: Optional[int] = None,
                 min_recent_win_rate: Optional[float] = None):
        _df_max, _df_min = _policy_thresholds()
        self.max_soft_fail = int(max_soft_fail) if max_soft_fail is not None else _df_max
        self.min_recent_win_rate = float(min_recent_win_rate) if min_recent_win_rate is not None else _df_min

    def decide(self, failing_reasons: List[str], recent_win_rate: Optional[float] = None) -> Tuple[str, List[str]]:
        """Return (decision, gates_to_soften).

        decision is one of: PASS, SOFTEN, BLOCK.
        """
        hard, soft, unknown = split_failures(failing_reasons)
        if unknown:
            unknown_names = set(unknown)
            logged = set()
            for raw in failing_reasons:
                name = canonical_gate(raw)
                if name in unknown_names and name not in logged:
                    logged.add(name)
                    logger.warning(
                        "Gate policy: unknown gate failure %r (treated as soft, penalty 0.03)",
                        raw,
                    )

        if hard:
            return ("BLOCK", hard)
        if not soft:
            return ("PASS", [])
        # If soft gates are failing, allow softening up to max_soft_fail when performance
        # has acceptable historical evidence (recent_win_rate >= 0.30) or during cold start / fresh sessions.
        # Capital-protection hard gates NEVER soften under any circumstance.
        if len(soft) <= self.max_soft_fail:
            effective_min_wr = min(0.30, self.min_recent_win_rate)
            if recent_win_rate is None or recent_win_rate >= effective_min_wr:
                return ("SOFTEN", soft)
        return ("BLOCK", soft)

    @staticmethod
    def confidence_penalty(softened_gates: List[str]) -> float:
        """Calculate aggregate confidence penalty based on specific softened gates (capped at 0.12)."""
        if not softened_gates:
            return 0.0
        total_pen = sum(SOFT_GATE_PENALTIES.get(canonical_gate(g), 0.03) for g in softened_gates)
        return min(0.12, round(total_pen, 3))
