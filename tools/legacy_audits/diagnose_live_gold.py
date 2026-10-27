"""Manual live-gold diagnostic.

Runs the same chain the orchestrator runs -- context build, regime
classification, parallel analysts + devil's advocate, then the decision engine --
for every trade style and prints what the engine actually saw.

Import-safe: the import sweep imports every tool, so nothing runs until
``__main__`` (MT5 is not initialised at import time either).

Usage: python -m tools.diagnose_live_gold
"""
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from jarvis.analysts.parallel_runner import ParallelAnalystCluster  # noqa: E402
from jarvis.data.schemas import MarketRegime  # noqa: E402
from jarvis.intelligence.decision_engine import DecisionEngine  # noqa: E402
from jarvis.intelligence.regime_engine import MarketRegimeClassifier  # noqa: E402
from jarvis.market.data_feed import DataFeedEngine  # noqa: E402
from jarvis.market.market_context import MarketContextEngine  # noqa: E402

SYMBOL = "XAUUSD"
STYLES = ("DAY_TRADING", "SCALP", "SWING")


def _tentative_bias(context, regime):
    """Mirror the orchestrator's pre-analyst bias so the diagnostic matches live."""
    trend_score = getattr(context.momentum, "trend_score", 0.0)
    structure_bias = getattr(context.structure, "bias", "NEUTRAL")
    d1_bias = (getattr(context, "mtf_alignment", {}) or {}).get("D1", "NEUTRAL")

    if structure_bias in ("BULLISH", "BEARISH"):
        return "BUY" if structure_bias == "BULLISH" else "SELL"
    if d1_bias in ("BULLISH", "BEARISH") and abs(trend_score) < 40:
        return "BUY" if d1_bias == "BULLISH" else "SELL"
    if regime.primary_regime in (MarketRegime.TREND_BULL, MarketRegime.STRONG_TREND_BULL):
        return "BUY"
    if regime.primary_regime in (MarketRegime.TREND_BEAR, MarketRegime.STRONG_TREND_BEAR):
        return "SELL"
    return "SELL" if trend_score < 0 else "BUY"


def main():
    import MetaTrader5 as mt5

    if not mt5.initialize():
        print("MT5 init failed")
        return 1

    data_feed = DataFeedEngine()
    context_engine = MarketContextEngine()
    regime_classifier = MarketRegimeClassifier()
    analyst_cluster = ParallelAnalystCluster()
    decision_engine = DecisionEngine()

    for style in STYLES:
        print(f"\n================ Style: {style} ================")
        mtf_data = data_feed.fetch_multi_timeframe(SYMBOL, trade_style=style)
        context = context_engine.build_context(SYMBOL, mtf_data, trade_style=style)

        regime = regime_classifier.classify_regime(context)
        tentative_bias = _tentative_bias(context, regime)

        print(f"Current price: {context.current_price:.2f}")
        print(f"Regime: {regime.primary_regime.value} (conf {regime.confidence:.2f})")
        print(f"Trend score: {getattr(context.momentum, 'trend_score', 0.0):.1f}")
        print(f"MTF: {(getattr(context, 'mtf_alignment', {}) or {}).get('D1', 'NEUTRAL')}")
        print(f"DP Zone: {getattr(context.structure, 'discount_premium_zone', None)}")
        print(f"CHOCH: {getattr(context.structure, 'choch', None)}")
        print(f"BOS: {getattr(context.structure, 'bos', None)}")
        print(f"Pre-analyst bias: {tentative_bias}")

        analyst_reports, devil_report = analyst_cluster.run_all_parallel(
            context, regime, tentative_bias
        )
        decision = decision_engine.evaluate(
            context, regime, analyst_reports, devil_report,
            trade_style=style,
        )
        print(f"Decision Action: {decision.action}, Direction: {decision.direction}, "
              f"Conf: {decision.confidence:.2f}")
        print(f"Strategy: {getattr(decision, 'strategy', None)}")
        print(f"Gate passed: {decision.gate_passed}")
        print(f"Failing reasons: {decision.failing_reasons}")

    mt5.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
