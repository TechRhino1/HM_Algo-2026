import unittest
from datetime import datetime, timezone
from jarvis.intelligence.decision_engine import DecisionEngine
from jarvis.data.schemas import (
    MarketContext, StructureContext, LiquidityContext, VolatilityContext,
    MomentumContext, SessionContext, RegimeOutput, MarketRegime,
    AnalystReport, AnalystRole, DevilAdvocateReport
)

class TestAIDecisionEngine(unittest.TestCase):
    def setUp(self):
        from jarvis.intelligence.self_learning import SelfLearningEngine
        self.engine = DecisionEngine(self_learning=SelfLearningEngine(db_path=":memory:"))

    def test_high_quality_setup(self):
        ctx = MarketContext(
            symbol="XAUUSD",
            timestamp=datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc),
            current_price=2400.0,
            bid=2399.8,
            ask=2400.2,
            structure=StructureContext(bias="BULLISH", bos=True, choch=True, choch_type="BULLISH", demand_zone=(2390.0, 2392.0)),
            liquidity=LiquidityContext(sweep_detected=True, sweep_type="BULLISH_SWEEP"),
            volatility=VolatilityContext(atr=10.0, current_spread_pips=1.5),
            momentum=MomentumContext(trend_score=80.0, adx=32.0),
            session=SessionContext(current_session="LONDON", is_prime_session=True)
        )
        regime = RegimeOutput(
            primary_regime=MarketRegime.TREND_BULL,
            probabilities={"TREND_BULL": 0.85},
            confidence=0.85
        )
        analyst_reports = {
            "STRUCTURE": AnalystReport(role=AnalystRole.STRUCTURE, symbol="XAUUSD", bias="BULLISH", confidence=0.85, score=85.0, evidence=["BOS confirmed"]),
            "MOMENTUM": AnalystReport(role=AnalystRole.MOMENTUM, symbol="XAUUSD", bias="BULLISH", confidence=0.80, score=80.0, evidence=["ADX 32"])
        }
        devil_report = DevilAdvocateReport(
            symbol="XAUUSD",
            counter_bias="BEARISH",
            penalty_score=10.0,
            invalidation_risk_coefficient=0.90,
            threats_detected=[]
        )

        res = self.engine.evaluate(ctx, regime, analyst_reports, devil_report, account_balance=10000.0)
        self.assertIn(res.decision, ["EXECUTE", "WAIT"])
        self.assertEqual(res.bias, "BUY")
        self.assertGreaterEqual(res.model_confidence, 0.50)

    def test_strict_quality_gate_no_soften_bypass(self):
        ctx = MarketContext(
            symbol="EURUSD",
            timestamp=datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc),
            current_price=1.0850,
            bid=1.0850,
            ask=1.0852,
            structure=StructureContext(bias="NEUTRAL"),
            liquidity=LiquidityContext(),
            volatility=VolatilityContext(atr=0.0010, current_spread_pips=5.0),
            momentum=MomentumContext(trend_score=0.0, rsi=50.0),
            session=SessionContext(current_session="ASIAN", is_prime_session=False)
        )
        regime = RegimeOutput(
            primary_regime=MarketRegime.LOW_VOLATILITY,
            probabilities={"LOW_VOLATILITY": 0.50},
            confidence=0.50
        )
        analyst_reports = {
            "STRUCTURE": AnalystReport(role=AnalystRole.STRUCTURE, symbol="EURUSD", bias="NEUTRAL", confidence=0.40, score=40.0, evidence=[])
        }
        devil = DevilAdvocateReport(
            symbol="EURUSD",
            counter_bias="BEARISH",
            penalty_score=25.0,
            invalidation_risk_coefficient=0.90,
            threats_detected=[]
        )

        res = self.engine.evaluate(ctx, regime, analyst_reports, devil, account_balance=10000.0)
        # Should NOT soften to EXECUTE
        self.assertNotEqual(res.decision, "EXECUTE")
        self.assertEqual(res.gate_policy_decision, "BLOCK")

    def test_gold_surging_prevents_sell(self):
        """When Gold is surging upward, the engine must NEVER resolve SELL or execute a SELL order."""
        ctx = MarketContext(
            symbol="XAUUSD",
            timestamp=datetime(2026, 10, 6, 6, 30, tzinfo=timezone.utc),
            current_price=4130.0,
            bid=4129.8,
            ask=4130.2,
            structure=StructureContext(bias="NEUTRAL", discount_premium_zone="PREMIUM"),
            liquidity=LiquidityContext(sweep_detected=True, sweep_type="BULLISH_SWEEP"),
            volatility=VolatilityContext(atr=15.0, current_spread_pips=2.0),
            momentum=MomentumContext(trend_score=45.0, adx=30.0),
            session=SessionContext(current_session="LONDON", is_prime_session=True),
            mtf_alignment={"D1": "BEARISH", "H4": "BEARISH", "H1": "NEUTRAL", "M15": "BULLISH", "M5": "BULLISH"},
            mtf_confluence_score=-40.0,
            trade_style="DAY_TRADING"
        )
        regime = RegimeOutput(
            primary_regime=MarketRegime.BREAKOUT,
            probabilities={"BREAKOUT": 0.70},
            confidence=0.70
        )
        analyst_reports = {
            "STRUCTURE": AnalystReport(role=AnalystRole.STRUCTURE, symbol="XAUUSD", bias="BULLISH", confidence=0.75, score=75.0, evidence=["Impulse expansion"]),
            "MOMENTUM": AnalystReport(role=AnalystRole.MOMENTUM, symbol="XAUUSD", bias="BULLISH", confidence=0.85, score=85.0, evidence=["Trend score +45"])
        }
        devil = DevilAdvocateReport(
            symbol="XAUUSD",
            counter_bias="BEARISH",
            penalty_score=15.0,
            invalidation_risk_coefficient=0.85,
            threats_detected=[]
        )

        # 1. Bias resolution must NOT be SELL
        resolved_bias = self.engine._resolve_market_trend_and_bias(ctx, regime, analyst_reports, trade_style="DAY_TRADING")
        self.assertNotEqual(resolved_bias, "SELL", "Surging Gold must NEVER resolve to SELL")

        # 2. Evaluation must NEVER execute a SELL
        res = self.engine.evaluate(ctx, regime, analyst_reports, devil, trade_style="DAY_TRADING", account_balance=10000.0)
        if res.decision == "EXECUTE":
            self.assertEqual(res.bias, "BUY")
        else:
            self.assertNotEqual(res.bias, "SELL")

    def test_momentum_immunity_case_b(self):
        """Case B (HTF Bearish) with strong bullish intraday momentum must resolve to BUY or HOLD, NEVER SELL."""
        ctx = MarketContext(
            symbol="EURUSD",
            timestamp=datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc),
            current_price=1.1250,
            bid=1.1249,
            ask=1.1251,
            structure=StructureContext(bias="BULLISH"),
            liquidity=LiquidityContext(),
            volatility=VolatilityContext(atr=0.0030, current_spread_pips=0.6),
            momentum=MomentumContext(trend_score=35.0, adx=26.0),
            session=SessionContext(current_session="LONDON", is_prime_session=True),
            mtf_alignment={"D1": "BEARISH", "H4": "BEARISH", "H1": "BULLISH", "M15": "BULLISH"},
            mtf_confluence_score=-50.0,
            trade_style="DAY_TRADING"
        )
        regime = RegimeOutput(
            primary_regime=MarketRegime.TREND_BEAR,
            probabilities={"TREND_BEAR": 0.60},
            confidence=0.60
        )
        analyst_reports = {
            "MOMENTUM": AnalystReport(role=AnalystRole.MOMENTUM, symbol="EURUSD", bias="BULLISH", confidence=0.80, score=80.0, evidence=["Strong bull push"])
        }
        bias = self.engine._resolve_market_trend_and_bias(ctx, regime, analyst_reports, trade_style="DAY_TRADING")
        self.assertIn(bias, ["BUY", "HOLD"])
        self.assertNotEqual(bias, "SELL")

    def test_reversal_strategy_suppression_in_strong_trend(self):
        """Liquidity sweep reversals must be suppressed to 0.0 prior weight during strong momentum."""
        from jarvis.intelligence.strategy_selector import StrategySelector
        selector = StrategySelector()
        ctx = MarketContext(
            symbol="XAUUSD",
            timestamp=datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc),
            current_price=4150.0,
            bid=4149.8,
            ask=4150.2,
            structure=StructureContext(bias="BULLISH"),
            liquidity=LiquidityContext(sweep_detected=True, sweep_magnitude=1.5),
            volatility=VolatilityContext(atr=15.0),
            momentum=MomentumContext(trend_score=50.0, adx=30.0),
            session=SessionContext(current_session="LONDON", is_prime_session=True),
        )
        regime = RegimeOutput(
            primary_regime=MarketRegime.BREAKOUT,
            probabilities={"BREAKOUT": 0.80},
            confidence=0.80
        )
        probs = selector.select_strategy_probabilities(regime, context=ctx, account_equity=1000.0)
        self.assertEqual(probs.get("LIQUIDITY_SWEEP_REVERSAL", 0.0), 0.0)

if __name__ == "__main__":
    unittest.main()
