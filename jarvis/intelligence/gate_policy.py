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
from typing import List, Tuple, Optional, Dict


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



# Gates that must always block when they fail (capital-protection / validity).
HARD_GATES = {
    "Market Session Open",
    "Drawdown Safety Guard",
    "Margin Capacity Limit",
    "Regime Viability",
    "Directional Bias",
    "Strategy Viable",
    "Gold Session Window",
    "No Active Macro Shock",
    "Risk/Reward >= 1.5",
    "Valid Stop Loss Distance",
    "Positive Expected Value",
    "Volatility Spike Circuit Breaker",
}

# Granular confidence penalties for soft gates
SOFT_GATE_PENALTIES: Dict[str, float] = {
    "Macro MTF Alignment": 0.05,
    "Gold Trend Following Alignment": 0.04,
    "Trend Not Exhausted": 0.03,
    "Crypto Macro Trend Filter": 0.04,
    "Premium/Discount Alignment": 0.03,
    "AI Multi-Score Gate": 0.04,
    "Devil Adversarial Guard": 0.04,
    "Order Flow Alignment": 0.03,
    "Order Flow Momentum": 0.03,
    "No Order Flow Absorption Trap": 0.04,
    "Forex Prime Session": 0.02,
    "Forex Breakout Guard": 0.03,
    "Index Trend Alignment": 0.04,
    "Low-Beta FX Macro Alignment": 0.03,
    "JPY Momentum Guard": 0.03,
    "SOL Confluence Guard": 0.03,
    "US30 Confluence Guard": 0.03,
    "Risk/Reward >= 1.5": 0.03,
    "Positive Expected Value": 0.04,
    "Spread Protection": 0.03,
    "Valid Stop Loss Distance": 0.03,
    "Calibrated Win Prob >= 50%": 0.04,
    "Regime Trend Consistency": 0.04,
}


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
        hard = [g for g in failing_reasons if g in HARD_GATES]
        soft = [g for g in failing_reasons if g not in HARD_GATES]

        if hard:
            return ("BLOCK", hard)
        if not soft:
            return ("PASS", [])
        # If soft gates are failing, allow softening up to max_soft_fail only when performance
        # has acceptable historical evidence (recent_win_rate >= min_recent_win_rate).
        if len(soft) <= self.max_soft_fail:
            if recent_win_rate is not None and recent_win_rate >= self.min_recent_win_rate:
                return ("SOFTEN", soft)
        return ("BLOCK", soft)

    @staticmethod
    def confidence_penalty(softened_gates: List[str]) -> float:
        """Calculate aggregate confidence penalty based on specific softened gates (capped at 0.12)."""
        if not softened_gates:
            return 0.0
        total_pen = sum(SOFT_GATE_PENALTIES.get(g, 0.03) for g in softened_gates)
        return min(0.12, round(total_pen, 3))
