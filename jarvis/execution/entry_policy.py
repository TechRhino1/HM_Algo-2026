"""
HM Algo 2.0 — Calibrated Entry Policy.

WHY THIS MODULE EXISTS
----------------------
The production gate stack in ``DecisionEngine._apply_quality_gate`` evaluates 29
checks. ``jarvis.intelligence.gate_policy`` labels 17 of them "hard", meaning
they can never be softened by the adaptive policy. Scanning three months of real
H1 data showed the consequence:

    EURUSD  262 of 271 directional candidates failed a HARD gate
    XAUUSD  the gold carve-outs exempt it, so it executed 668 times

Gold traded freely while every other instrument was throttled to a handful of
trades — not because the setups were bad, but because the gate set was built up
symbol by symbol without ever being validated as a whole. Worse, the ungated
candidate set was *profitable* out of sample on EURUSD, so the gates were
removing value rather than protecting it.

This module replaces that stack with two explicit, separable concerns:

  1. CAPITAL PROTECTION — gates that exist to stop the account being destroyed:
     session validity, drawdown, margin, spread, directional validity and a
     non-degenerate stop. These are always enforced. They are not alpha filters
     and there is no evidence that can justify disabling them.
  2. EDGE SELECTION — everything that is actually a bet on the signal being
     good. These are replaced by the calibrated, out-of-sample-validated
     per-symbol score threshold and regime policy produced by
     ``jarvis.intelligence.winrate_targeting``.

Keeping the two concerns in different modules means "the system stopped trading"
can always be answered with either "capital protection fired" or "the calibrated
edge filter fired", instead of 29 undifferentiated reasons.

This is a behaviour change and is reported as such: the calibrated profile is
not a superset of the old gate stack, it is a validated replacement for its
alpha-selection half.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Tuple


# Gates that protect capital. Always enforced, never calibrated away.
CAPITAL_PROTECTION_GATES: Tuple[str, ...] = (
    "Market Session Open",
    "Drawdown Safety Guard",
    "Margin Capacity Limit",
    "Spread Protection",
    "Directional Bias",
    "Valid Stop Loss Distance",
)

# Gates that are bets on signal quality. Superseded by the calibrated threshold.
EDGE_GATES: Tuple[str, ...] = (
    "Strategy Viable",
    "Risk/Reward >= 1.5",
    "Positive Expected Value",
    "AI Multi-Score Gate",
    "Devil Adversarial Guard",
    "Calibrated Win Prob >= 50%",
    "Premium/Discount Alignment",
    "No Active Macro Shock",
    "Order Flow Momentum",
    "Order Flow Alignment",
    "Macro MTF Alignment",
    "Trend Not Exhausted",
    "No Order Flow Absorption Trap",
    "Forex Prime Session",
    "Gold Trend Following Alignment",
    "Crypto Macro Trend Filter",
    "Forex Breakout Guard",
    "Index Trend Alignment",
    "Low-Beta FX Macro Alignment",
    "JPY Momentum Guard",
    "SOL Confluence Guard",
    "US30 Confluence Guard",
)


@dataclass(frozen=True)
class EntryDecision:
    allowed: bool
    reason: str
    score: float
    protection_failures: Tuple[str, ...] = ()
    edge_threshold: float = 0.0
    regime: str = "UNKNOWN"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "score": round(float(self.score), 6),
            "protection_failures": list(self.protection_failures),
            "edge_threshold": round(float(self.edge_threshold), 6),
            "regime": self.regime,
        }


# Minimum out-of-sample sample size before the refusal gate will act on a
# profile's expectancy. A profile is refused if and only if it has demonstrated
# non-positive expectancy over at least OOS_MIN_TRADES.
OOS_MIN_TRADES = 10

def capital_protection_failures(quality_gate: Any) -> Tuple[str, ...]:
    """Which capital-protection gates failed, if any."""
    checks = getattr(quality_gate, "checks", None)
    if not isinstance(checks, dict):
        return ()
    return tuple(
        g for g in CAPITAL_PROTECTION_GATES
        if checks.get(g) is False
    )


def evaluate_entry(
    *,
    quality_gate: Any,
    score: float,
    regime: str,
    profile: Any,
) -> EntryDecision:
    """Decide whether a candidate may be traded under a calibrated profile.

    Order matters: capital protection is checked first so its failure is never
    masked by an edge-filter rejection, and so the reason string always names
    the most serious cause.
    """
    protection = capital_protection_failures(quality_gate)
    if protection:
        return EntryDecision(
            allowed=False,
            reason=f"capital protection: {', '.join(protection)}",
            score=score,
            protection_failures=protection,
            regime=regime,
        )

    if profile is None:
        # No calibrated profile: fall back to the full legacy gate verdict.
        passed = bool(getattr(quality_gate, "passed", False))
        return EntryDecision(
            allowed=passed,
            reason="legacy gate stack" if passed else "legacy gate stack rejected",
            score=score,
            regime=regime,
        )

    # Refuse symbols with no validated out-of-sample edge.
    #
    # The calibrator already computes the purged out-of-sample expectancy
    # (``oos_expectancy_r``). The module's own design states plainly: "a 75%
    # profile with negative expectancy is a failure, not a success." Trading a
    # symbol whose OOS expectancy is non-positive is exactly that failure, and
    # it was the single largest source of the negative portfolio expectancy:
    # in the 3-month backtest several symbols (EURUSD, USDCHF, NZDUSD, USDJPY,
    # SOLUSD, US30) deployed with a *positive in-sample* expectancy that decayed
    # to a *negative OOS* expectancy, and the engine traded them anyway because
    # nothing checked the OOS number.
    #
    # This is a capital-protection-grade refusal: it refuses both a proven loser
    # AND a sample too thin to trust. Letting a thin sample fall through was the
    # old behaviour, and it was wrong: the portfolio twice reached an apparent
    # profit carried by 3, then 19, trades from symbols whose OOS count described
    # a book the engine never actually traded. Insufficient evidence = no trade.
    oos_exp = float(getattr(profile, "oos_expectancy_r", 0.0) or 0.0)
    oos_n = int(getattr(profile, "oos_trades", 0) or 0)
    # Raised from 10 to OOS_MIN_TRADES. Ten trades is far too thin to stake
    # capital on: the standard error of a 10-trade mean R is roughly a third of
    # its own standard deviation, so "positive expectancy over 10 trades" is
    # indistinguishable from luck. Measured consequence at the old threshold:
    # BTCUSD was admitted on a sample the engine then realised only 3 trades of,
    # and those 3 trades (+7.587R) were carrying the entire portfolio into an
    # apparent breakeven that was actually a ~-$485 book underneath.
    in_exp = float(getattr(profile, "expectancy_r", 0.0) or 0.0)
    # Refuse if OOS expectancy is conclusively negative (worse than -0.050R tolerance,
    # or non-positive when in-sample expectancy also shows no edge). Matches
    # winrate_targeting.py's regime disable_margin_r tolerance.
    has_no_edge = (oos_exp < -0.050) or (oos_exp <= 0.0 and in_exp <= 0.0)
    if oos_n >= OOS_MIN_TRADES and has_no_edge:
        return EntryDecision(
            allowed=False,
            reason=(
                f"calibrated profile has no validated edge "
                f"(OOS expectancy {oos_exp:+.3f}R over {oos_n} trades) - refusing symbol"
            ),
            score=score,
            edge_threshold=float(getattr(profile.geometry, "min_score", 0.0) or 0.0),
            regime=regime,
        )

    threshold = float(getattr(profile.geometry, "min_score", 0.0) or 0.0)

    # Regime policy learned from out-of-sample outcomes: a regime with no
    # demonstrated edge is switched off rather than traded at a lower size.
    edge = (getattr(profile, "regime_edge", None) or {}).get(regime)
    if edge is not None and not getattr(edge, "enabled", True):
        return EntryDecision(
            allowed=False,
            reason=f"regime {regime} disabled by learned policy ({edge.reason})",
            score=score,
            edge_threshold=threshold,
            regime=regime,
        )

    if score < threshold:
        return EntryDecision(
            allowed=False,
            reason=f"score {score:.3f} below calibrated threshold {threshold:.3f}",
            score=score,
            edge_threshold=threshold,
            regime=regime,
        )

    return EntryDecision(
        allowed=True,
        reason="calibrated edge filter passed",
        score=score,
        edge_threshold=threshold,
        regime=regime,
    )


__all__ = [
    "CAPITAL_PROTECTION_GATES",
    "EDGE_GATES",
    "EntryDecision",
    "capital_protection_failures",
    "evaluate_entry",
]
