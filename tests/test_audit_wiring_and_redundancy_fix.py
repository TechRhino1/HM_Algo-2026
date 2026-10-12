"""
Unit tests validating codebase wiring, synchronization, and redundancy fixes.
Ensures:
1. DataFeedEngine uses MT5Client._shared_lock without circular dependencies.
2. DecisionEngine connects strategy_bandit with StrategySelector.
3. Dead attributes are removed from DecisionEngine.
4. MacroAnalyst enforces max_wait=1.5 on news fetches.
5. DynamicRiskAndLevelsEngine clamps TP expansion ratio and prevents unrealistic targets.
"""
import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone
import pandas as pd

from jarvis.execution.mt5_client import MT5Client
from jarvis.market.data_feed import DataFeedEngine
from jarvis.learning.strategy_bandit import StrategyBandit
from jarvis.intelligence.strategy_selector import StrategySelector
from jarvis.intelligence.decision_engine import DecisionEngine
from jarvis.intelligence.dynamic_levels import DynamicRiskAndLevelsEngine
from jarvis.analysts.macro_analyst import MacroAnalyst
from jarvis.data.schemas import (
    MarketContext, StructureContext, VolatilityContext, SessionContext,
    MomentumContext, LiquidityContext, RegimeOutput, MarketRegime
)


class TestAuditWiringAndRedundancyFix(unittest.TestCase):

    def test_shared_mt5_lock_unification(self):
        """Verify that DataFeedEngine uses MT5Client._shared_lock."""
        feed_lock = DataFeedEngine.get_mt5_fetch_lock()
        self.assertIs(feed_lock, MT5Client._shared_lock)

    def test_decision_engine_bandit_wiring(self):
        """Verify that DecisionEngine passes strategy_bandit to StrategySelector."""
        shared_bandit = StrategyBandit()
        engine = DecisionEngine(strategy_bandit=shared_bandit)
        self.assertIs(engine.strategy_selector.bandit, shared_bandit)
        
        # Verify dead attributes are removed
        self.assertFalse(hasattr(engine, "ensemble_bandit"))
        self.assertFalse(hasattr(engine, "frac_diff"))
        self.assertFalse(hasattr(engine, "mean_reversion_engine"))

    def test_macro_analyst_bounded_news_wait(self):
        """Verify that MacroAnalyst passes max_wait=1.5 to get_news_calendar."""
        analyst = MacroAnalyst()
        ctx = MarketContext(
            timestamp=datetime.now(timezone.utc),
            symbol="EURUSD",
            current_price=1.0850,
            bid=1.0849,
            ask=1.0851,
            session=SessionContext(current_session="LONDON", is_prime_session=True),
            structure=StructureContext(bias="BULLISH"),
            momentum=MomentumContext(),
            volatility=VolatilityContext(atr=0.0015, current_spread_pips=1.2),
            liquidity=LiquidityContext()
        )
        regime = RegimeOutput(primary_regime=MarketRegime.TREND_BULL, confidence=0.85, probabilities={})
        with patch("jarvis.market.news.GLOBAL_NEWS_ENGINE.get_news_calendar") as mock_news:
            mock_news.return_value = []
            rep = analyst.analyze(ctx, regime)
            mock_news.assert_called_once_with(max_wait=1.5)
            self.assertIsNotNone(rep)

    def test_tp_expansion_clamping_and_ceiling(self):
        """Verify that DynamicRiskAndLevelsEngine clamps TP expansion and caps TP distance."""
        engine = DynamicRiskAndLevelsEngine()
        ctx = MarketContext(
            timestamp=datetime.now(timezone.utc),
            symbol="ETHUSD",
            current_price=2500.0,
            bid=2499.8,
            ask=2500.2,
            session=SessionContext(),
            structure=StructureContext(bias="BULLISH"),
            momentum=MomentumContext(adx=26.0),
            volatility=VolatilityContext(atr=15.0, current_spread_pips=0.4),
            liquidity=LiquidityContext()
        )
        regime = RegimeOutput(primary_regime=MarketRegime.TREND_BULL, confidence=0.85, probabilities={})
        
        # Mock institutional engine to return a very tight risk (e.g. 0.50), while base floor is 20.0
        with patch.object(engine.institutional_engine, "calculate_entry_and_levels") as mock_inst:
            mock_inst.return_value = {
                "entry_price": 2500.0,
                "sl_price": 2499.50,
                "tp_price": 2501.50,
                "tp1_price": 2501.0,
                "tp2_price": 2502.0,
                "tp3_price": 2503.0,
                "risk_dist": 0.50,
                "rr_ratio": 3.0,
                "entry_type": "SNIPER_IMMEDIATE"
            }
            
            dummy_df = pd.DataFrame({"close": [2500.0]})
            mtf_data = {"H1": dummy_df}
            
            res = engine.calculate_levels(
                context=ctx,
                regime=regime,
                tentative_bias="BUY",
                trade_style="DAY_TRADING",
                mtf_data=mtf_data
            )
            
            self.assertEqual(res["bias"], "BUY")
            clamped_risk = res["risk_dist"]
            clamped_tp_dist = abs(res["tp_price"] - res["entry_price"])
            # Verify TP distance does not blow up to 40x (which would be 40 * 1.5 = 60.0 on 0.5 risk)
            # It must be bounded within 5R ceiling
            self.assertLessEqual(clamped_tp_dist, clamped_risk * 5.0)


if __name__ == "__main__":
    unittest.main()
