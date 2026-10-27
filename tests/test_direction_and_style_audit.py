import unittest
from datetime import datetime, timezone
import pandas as pd

from jarvis.data.schemas import (
    MarketContext, StructureContext, LiquidityContext, VolatilityContext,
    MomentumContext, SessionContext, RegimeOutput, MarketRegime, PositionSnapshot,
    DecisionObject, TradeQualityGateResult
)
from jarvis.intelligence.decision_engine import DecisionEngine
from jarvis.intelligence.opportunity_arbiter import UniversalOpportunityArbiter, CandidateOpportunity
from jarvis.market.market_context import MarketContextEngine


class TestDirectionResolutionAndStyle(unittest.TestCase):

    def setUp(self):
        self.decision_engine = DecisionEngine()
        self.arbiter = UniversalOpportunityArbiter()

    def _create_mock_context(self, symbol="XAUUSD", htf="BEARISH", d1="BEARISH", m15="BEARISH", m5="BEARISH", zone="DISCOUNT", trend_score=-40.0):
        ctx = MarketContext(
            symbol=symbol,
            timestamp=datetime.now(timezone.utc),
            current_price=2000.0,
            bid=1999.8,
            ask=2000.2,
            structure=StructureContext(
                bias=m15,
                discount_premium_zone=zone,
                bos=False,
                choch=False
            ),
            liquidity=LiquidityContext(
                sweep_detected=True,
                sweep_type="BULLISH_SWEEP" if zone == "DISCOUNT" else "BEARISH_SWEEP"
            ),
            volatility=VolatilityContext(atr=10.0, current_spread_pips=2.0),
            momentum=MomentumContext(trend_score=trend_score, adx=28.0),
            session=SessionContext(current_session="LONDON", is_prime_session=True),
            mtf_alignment={"D1": d1, "H4": htf, "H1": htf, "M15": m15, "M5": m5},
            mtf_confluence_score=-40.0 if "BEAR" in htf else 40.0
        )
        return ctx

    def test_no_buy_in_bearish_macro(self):
        """In a bearish macro trend, buying at discount or on low sweep is strictly forbidden."""
        ctx = self._create_mock_context(symbol="XAUUSD", htf="BEARISH", d1="BEARISH", zone="DISCOUNT", trend_score=-45.0)
        regime = RegimeOutput(primary_regime=MarketRegime.TREND_BEAR, probabilities={"TREND_BEAR": 0.9}, confidence=0.85)
        
        # Test DAY_TRADING
        bias = self.decision_engine._resolve_market_trend_and_bias(ctx, regime, {}, trade_style="DAY_TRADING")
        self.assertNotEqual(bias, "BUY", "System must NEVER return BUY in a bearish macro regime!")
        self.assertIn(bias, ("SELL", "HOLD"))

        # Test SWING
        bias_swing = self.decision_engine._resolve_market_trend_and_bias(ctx, regime, {}, trade_style="SWING")
        self.assertNotEqual(bias_swing, "BUY", "System must NEVER return BUY in a bearish swing regime!")

        # Test SCALP
        bias_scalp = self.decision_engine._resolve_market_trend_and_bias(ctx, regime, {}, trade_style="SCALP")
        self.assertNotEqual(bias_scalp, "BUY", "System must NEVER return BUY in a bearish scalp regime!")

    def test_no_sell_in_bullish_macro(self):
        """In a bullish macro trend, selling at premium or on high sweep is strictly forbidden."""
        ctx = self._create_mock_context(symbol="US30", htf="BULLISH", d1="BULLISH", m15="BULLISH", m5="BULLISH", zone="PREMIUM", trend_score=50.0)
        regime = RegimeOutput(primary_regime=MarketRegime.TREND_BULL, probabilities={"TREND_BULL": 0.9}, confidence=0.85)

        # Test DAY_TRADING
        bias = self.decision_engine._resolve_market_trend_and_bias(ctx, regime, {}, trade_style="DAY_TRADING")
        self.assertNotEqual(bias, "SELL", "System must NEVER return SELL in a bullish macro regime!")
        self.assertIn(bias, ("BUY", "HOLD"))

        # Test SWING
        bias_swing = self.decision_engine._resolve_market_trend_and_bias(ctx, regime, {}, trade_style="SWING")
        self.assertNotEqual(bias_swing, "SELL", "System must NEVER return SELL in a bullish swing regime!")

    def test_arbiter_disqualifies_counter_flush(self):
        """Arbiter must disqualify any candidate that goes against agreed HTF trend."""
        ctx = self._create_mock_context(symbol="ETHUSD", htf="BEARISH", d1="BEARISH", m15="BEARISH", m5="BEARISH")
        
        cand = CandidateOpportunity(
            symbol="ETHUSD",
            trade_style="DAY_TRADING",
            timeframe="M15",
            strategy="TREND_FOLLOWING",
            bias="BUY",
            entry_price=2500.0,
            stop_loss=2450.0,
            take_profit=2650.0,
            risk_reward_ratio=3.0,
            expected_value=1.5,
            win_prob=65.0,
            ml_prob=0.70,
            confluence_score=35.0,
            confluence_factors=["BOS"],
            adversarial_penalty=0.0,
            risk_factors=[],
            regime="TREND_BEAR",
            regime_multiplier=1.0,
            utility_score=1.5,
            setup_grade="GRADE A",
            is_actionable=True,
            decision_obj=DecisionObject(
                symbol="ETHUSD",
                timestamp=datetime.now(timezone.utc),
                decision="EXECUTE",
                bias="BUY",
                probabilities={"buy": 0.65, "sell": 0.35, "no_trade": 0.0},
                strategy="TREND_FOLLOWING",
                entry_price=2500.0,
                stop_loss=2450.0,
                take_profit=2650.0,
                risk_reward_ratio=3.0,
                calculated_risk_percent=0.5,
                expected_value=1.5,
                model_confidence=0.65,
                adversarial_penalty=0.0,
                invalidation_levels=[],
                bull_case=[],
                bear_case=[],
                risk_factors=[],
                quality_gate=TradeQualityGateResult(passed=True, checks={}, failing_reasons=[]),
                regime=MarketRegime.TREND_BEAR
            ),
            context=ctx
        )

        res_cand = self.arbiter.evaluate_opportunity(cand.decision_obj, context=ctx, trade_style="DAY_TRADING")
        self.assertFalse(res_cand.is_actionable, "Arbiter must disqualify counter-flush BUY into agreed BEARISH HTF trend!")

    def test_position_snapshot_style_property(self):
        """PositionSnapshot must correctly detect style from comment and serialize in to_dict."""
        p_scalp = PositionSnapshot(
            ticket=123, symbol="XAUUSD", type="BUY", volume=0.01,
            open_price=2000.0, current_price=2005.0, sl=1995.0, tp=2015.0,
            profit=50.0, swap=0.0, commission=-0.05, open_time="2026-10-22",
            magic=1001, comment="HMA2_SCLP_GOLD"
        )
        self.assertEqual(p_scalp.style, "SCALP")
        self.assertEqual(p_scalp.to_dict()["style"], "SCALP")

        p_day = PositionSnapshot(
            ticket=124, symbol="EURUSD", type="SELL", volume=0.01,
            open_price=1.10, current_price=1.09, sl=1.11, tp=1.07,
            profit=100.0, swap=0.0, commission=-0.05, open_time="2026-10-22",
            magic=1001, comment="HMA2_DAY_TREN_DEMO"
        )
        self.assertEqual(p_day.style, "DAY_TRADING")
        self.assertEqual(p_day.to_dict()["style"], "DAY_TRADING")

        p_swing = PositionSnapshot(
            ticket=125, symbol="BTCUSD", type="BUY", volume=0.01,
            open_price=65000.0, current_price=66000.0, sl=63000.0, tp=70000.0,
            profit=10.0, swap=0.0, commission=-0.05, open_time="2026-10-22",
            magic=1001, comment="HMA2_SWNG_BTC"
        )
        self.assertEqual(p_swing.style, "SWING")
        self.assertEqual(p_swing.to_dict()["style"], "SWING")


if __name__ == "__main__":
    unittest.main()
