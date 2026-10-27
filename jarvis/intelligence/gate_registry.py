"""Single source of truth for every quality gate in the decision path.

Gate names used to be duplicated across:

* ``decision_engine`` (the 33-key check matrix),
* ``gate_policy`` (``HARD_GATES`` + ``SOFT_GATE_PENALTIES``),
* ``opportunity_arbiter`` (critical-failure substring matching),
* ``backtesting.signal_scan`` (hard-gate filtering).

Duplication produced real defects: ``Spread Protection`` was hard in the matrix
but soft in the policy, hard gates appeared in the soft-penalty table, and
metals/master-confluence failures were appended with free-text reasons that
never matched ``HARD_GATES`` and could therefore be softened.

This module is metadata only (no imports from the decision engine, so it is
cycle-free). Evaluators stay where the context is assembled; policy consumers
read the registry.  ``canonical_gate`` maps free-text failure reasons back to a
gate name, so membership tests are O(1) after the first call and cannot be
bypassed by a parenthetical suffix.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Dict, FrozenSet, Tuple


@dataclass(frozen=True)
class GateSpec:
    name: str
    severity: str  # "hard" | "soft"
    penalty: float = 0.0
    category: str = "quality"
    description: str = ""


# The order is the evaluation/report order.  Keep names stable: they are
# persisted in logs, the DB and the backtest signal scan.
GATE_SPECS: Tuple[GateSpec, ...] = (
    # ── Capital protection / validity (never softened) ──────────────────────
    GateSpec("Market Session Open", "hard", category="session"),
    GateSpec("Drawdown Safety Guard", "hard", category="risk"),
    GateSpec("Margin Capacity Limit", "hard", category="risk"),
    GateSpec("Regime Viability", "hard", category="regime"),
    GateSpec("Directional Bias", "hard", category="direction"),
    GateSpec("Strategy Viable", "hard", category="strategy"),
    GateSpec("Strategy Triggered", "hard", category="strategy"),
    GateSpec("Risk/Reward >= 1.5", "hard", category="geometry"),
    GateSpec("Valid Stop Loss Distance", "hard", category="geometry"),
    GateSpec("Positive Expected Value", "hard", category="edge"),
    GateSpec("Spread Protection", "hard", category="liquidity"),
    GateSpec("Volatility Spike Circuit Breaker", "hard", category="risk"),
    GateSpec("No Active Macro Shock", "hard", category="macro"),
    GateSpec("Gold Session Window", "hard", category="session"),
    GateSpec("Gold Trend Following Alignment", "hard", category="direction"),
    GateSpec("Metals Premium/Discount Discipline", "hard", category="structure"),
    GateSpec("Metals Pullback OTE Proximity", "hard", category="structure"),
    GateSpec("Master Confluence Minimum", "hard", category="confluence"),
    GateSpec("Commodity Macro Alignment", "hard", category="macro"),
    # ── Edge-quality gates (eligible for evidence-based softening) ──────────
    GateSpec("Regime Trend Consistency", "soft", 0.04, "regime"),
    GateSpec("AI Multi-Score Gate", "soft", 0.04, "score"),
    GateSpec("Devil Adversarial Guard", "soft", 0.04, "adversarial"),
    GateSpec("Calibrated Win Prob >= 50%", "soft", 0.04, "probability"),
    GateSpec("Premium/Discount Alignment", "soft", 0.03, "structure"),
    GateSpec("Order Flow Momentum", "soft", 0.03, "orderflow"),
    GateSpec("Order Flow Alignment", "soft", 0.03, "orderflow"),
    GateSpec("Macro MTF Alignment", "soft", 0.05, "macro"),
    GateSpec("Trend Not Exhausted", "soft", 0.03, "trend"),
    GateSpec("No Order Flow Absorption Trap", "soft", 0.04, "orderflow"),
    GateSpec("Forex Prime Session", "soft", 0.02, "session"),
    GateSpec("Crypto Macro Trend Filter", "soft", 0.04, "macro"),
    GateSpec("Forex Breakout Guard", "soft", 0.03, "trend"),
    GateSpec("Index Trend Alignment", "soft", 0.04, "direction"),
    GateSpec("Low-Beta FX Macro Alignment", "soft", 0.03, "macro"),
    GateSpec("JPY Momentum Guard", "soft", 0.03, "trend"),
    GateSpec("SOL Confluence Guard", "soft", 0.03, "confluence"),
    GateSpec("US30 Confluence Guard", "soft", 0.03, "confluence"),
)

GATE_NAMES: Tuple[str, ...] = tuple(spec.name for spec in GATE_SPECS)
_BY_NAME: Dict[str, GateSpec] = {spec.name: spec for spec in GATE_SPECS}

HARD_GATES: FrozenSet[str] = frozenset(
    spec.name for spec in GATE_SPECS if spec.severity == "hard"
)
SOFT_GATE_PENALTIES: Dict[str, float] = {
    spec.name: spec.penalty for spec in GATE_SPECS if spec.severity == "soft"
}

# Free-text failure reasons are still appended by a few specialised vetoes.
# Map them to their canonical gate so a hard veto can never be softened by a
# suffix mismatch.
_ALIASES: Tuple[Tuple[str, str], ...] = (
    ("Master Confluence Below Minimum", "Master Confluence Minimum"),
    ("Metals out-of-killzone", "Gold Session Window"),
    ("Metals BUY requires deep DISCOUNT", "Metals Premium/Discount Discipline"),
    ("Metals SELL requires deep PREMIUM", "Metals Premium/Discount Discipline"),
    ("Pullback entry too far from OTE", "Metals Pullback OTE Proximity"),
)


@lru_cache(maxsize=512)
def canonical_gate(reason: str) -> str:
    """Return the canonical gate name for a failure reason.

    Exact registry names are returned unchanged.  Known free-text vetoes are
    mapped to their gate.  Unknown reasons are returned as-is (and therefore
    remain soft, matching the historical default), but they are logged as
    unknown by ``is_known_gate`` so new gates cannot silently gain softening.
    """
    text = str(reason or "").strip()
    if text in _BY_NAME:
        return text
    for prefix, canonical in _ALIASES:
        if text.startswith(prefix):
            return canonical
    # Strip a trailing parenthetical and retry, covering future detail suffixes.
    base = text.split(" (", 1)[0].strip()
    if base in _BY_NAME:
        return base
    return text


def is_known_gate(reason: str) -> bool:
    return canonical_gate(reason) in _BY_NAME


def is_hard_gate(reason: str) -> bool:
    return canonical_gate(reason) in HARD_GATES


def penalty_for(reason: str) -> float:
    return SOFT_GATE_PENALTIES.get(canonical_gate(reason), 0.03)


def hard_failures(reasons) -> list:
    return [r for r in reasons if is_hard_gate(r)]


def soft_failures(reasons) -> list:
    return [r for r in reasons if not is_hard_gate(r)]


def split_failures(reasons):
    """Classify failure reasons in one canonicalisation pass.

    Returns ``(hard, soft, unknown)`` with duplicates removed while preserving
    order. Unknown names stay soft (the historical fail-open default) but are
    reported separately so the policy can log them once.
    """
    hard, soft, unknown = [], [], []
    seen = set()
    for raw in reasons or ():
        name = canonical_gate(raw)
        if name in seen:
            continue
        seen.add(name)
        if name in HARD_GATES:
            hard.append(name)
        elif name in SOFT_GATE_PENALTIES:
            soft.append(name)
        else:
            soft.append(name)
            unknown.append(name)
    return hard, soft, unknown
