"""
Comprehensive Test Suite for Market Direction AI, Hierarchical Trend Resolution,
Analyst Decoupling, Anti-Chase Guard, and Microstructure Scalp Engine.

Validates the Acceptance Criteria:
1. Market Direction AI correctly identifies BUY / SELL / NEUTRAL direction.
2. Bullish macro trends do NOT generate SELL signals during normal intraday pullbacks.
3. Bearish macro trends do NOT generate BUY signals during normal intraday bounces.
4. Reversal trades are strictly allowed ONLY when all required reversal confirmations are present (Zero UNVALIDATED Counter-Trend Trades).
5. Swing, Day Trading, and Scalp strategies correctly use their respective timeframe hierarchies.
6. Analyst votes are genuinely independent; neutral analysts do not create false consensus.
7. Entry signals do not chase tops (buying in Premium) or sell bottoms (selling in Discount).
8. Scalp Execution Engine vetoes CHOP and enforces cost-friction gates (< 25%).
"""
import unittest
from datetime import datetime, timezone
import pandas as pd
import numpy as np

from jarvis.intelligence.decision_engine import DecisionEngine
from jarvis.intelligence.self_learning import SelfLearningEngine
from jarvis.data.schemas import (
    MarketContext, StructureContext, LiquidityContext, VolatilityContext,
    MomentumContext, SessionContext, RegimeOutput, MarketRegime,
    AnalystReport, AnalystRole, DevilAdvocateReport
)
from jarvis.analysts.liquidity_analyst import LiquidityAnalyst
from jarvis.analysts.volatility_analyst import VolatilityAnalyst
from jarvis.analysts.risk_analyst import RiskAnalyst
from jarvis.analysts.macro_analyst import MacroAnalyst
from jarvis.scalping.microstructure_engine import MicrostructureEngine, MicrostructureState
from jarvis.scalping.scalp_execution_engine import ScalpExecutionEngine, ScalpDecision


class TestMarketDirectionAI(unittest.TestCase):
    def setUp(self):
        self.engine = DecisionEngine(self_learning=SelfLearningEngine(db_path=":memory:"))

    def test_bullish_macro_pullback_does_not_sell(self):
        """
        CRITICAL TEST: In a Bullish Macro Trend (D1/H4 Bullish, Regime TREND_BULL),
        a minor intraday structure break or pullback must NOT trigger a SELL signal.
        """
        ctx = MarketContext(
            symbol="EURUSD",
            timestamp=datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc),
            current_price=1.0850,
            bid=1.0849,
            ask=1.0851,
            # Intraday structure has minor bearish choch (pullback)
            structure=StructureContext(bias="BEARISH", choch=True, choch_type="BEARISH", discount_premium_zone="DISCOUNT"),
            liquidity=LiquidityContext(sweep_detected=False),
            volatility=VolatilityContext(atr=0.0015, current_spread_pips=1.0),
            momentum=MomentumContext(trend_score=35.0, adx=28.0),  # Macro momentum still positive
            session=SessionContext(current_session="LONDON", is_prime_session=True),
            mtf_alignment={"D1": "BULLISH", "H4": "BULLISH", "H1": "BULLISH", "M15": "BEARISH"},
            trade_style="SWING"
        )
        regime = RegimeOutput(
            primary_regime=MarketRegime.TREND_BULL,
            probabilities={"TREND_BULL": 0.85},
            confidence=0.85
        )
        analyst_reports = {
            "STRUCTURE": AnalystReport(role=AnalystRole.STRUCTURE, symbol="EURUSD", bias="BEARISH", confidence=0.60, score=60.0),
            "MOMENTUM": AnalystReport(role=AnalystRole.MOMENTUM, symbol="EURUSD", bias="BULLISH", confidence=0.80, score=80.0),
        }
        devil = DevilAdvocateReport(symbol="EURUSD", counter_bias="BEARISH", penalty_score=10.0, invalidation_risk_coefficient=0.80)

        resolved_bias = self.engine._resolve_market_trend_and_bias(ctx, regime, analyst_reports, trade_style="SWING")
        self.assertNotEqual(resolved_bias, "SELL", "Bullish macro trend MUST NOT resolve to SELL during an intraday pullback!")
        self.assertEqual(resolved_bias, "BUY")

        res = self.engine.evaluate(ctx, regime, analyst_reports, devil, trade_style="SWING")
        self.assertNotEqual(res.bias, "SELL")

    def test_bearish_macro_bounce_does_not_buy(self):
        """
        CRITICAL TEST: In a Bearish Macro Trend (D1/H4 Bearish, Regime TREND_BEAR),
        a minor intraday bounce must NOT trigger a BUY signal.
        """
        ctx = MarketContext(
            symbol="BTCUSD",
            timestamp=datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc),
            current_price=60000.0,
            bid=59998.0,
            ask=60002.0,
            # Intraday structure has minor bounce
            structure=StructureContext(bias="BULLISH", choch=True, choch_type="BULLISH", discount_premium_zone="PREMIUM"),
            liquidity=LiquidityContext(sweep_detected=False),
            volatility=VolatilityContext(atr=500.0, current_spread_pips=1.0),
            momentum=MomentumContext(trend_score=-40.0, adx=30.0),  # Macro momentum negative
            session=SessionContext(current_session="LONDON", is_prime_session=True),
            mtf_alignment={"D1": "BEARISH", "H4": "BEARISH", "H1": "BEARISH", "M15": "BULLISH"},
            trade_style="SWING"
        )
        regime = RegimeOutput(
            primary_regime=MarketRegime.TREND_BEAR,
            probabilities={"TREND_BEAR": 0.85},
            confidence=0.85
        )
        analyst_reports = {
            "STRUCTURE": AnalystReport(role=AnalystRole.STRUCTURE, symbol="BTCUSD", bias="BULLISH", confidence=0.60, score=60.0),
            "MOMENTUM": AnalystReport(role=AnalystRole.MOMENTUM, symbol="BTCUSD", bias="BEARISH", confidence=0.80, score=80.0),
        }
        devil = DevilAdvocateReport(symbol="BTCUSD", counter_bias="BULLISH", penalty_score=10.0, invalidation_risk_coefficient=0.80)

        resolved_bias = self.engine._resolve_market_trend_and_bias(ctx, regime, analyst_reports, trade_style="SWING")
        self.assertNotEqual(resolved_bias, "BUY", "Bearish macro trend MUST NOT resolve to BUY during an intraday bounce!")
        self.assertEqual(resolved_bias, "SELL")

    def test_zero_unvalidated_counter_trend_trades(self):
        """
        MANDATE: Zero unvalidated counter-trend trades.
        If a trade opposes the regime (e.g. SELL in TREND_BULL), it MUST be blocked
        unless full reversal confirmation (liquidity sweep + displacement + choch) is present.
        """
        ctx = MarketContext(
            symbol="XAUUSD",
            timestamp=datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc),
            current_price=2400.0,
            bid=2399.8,
            ask=2400.2,
            structure=StructureContext(bias="BEARISH", choch=False),
            liquidity=LiquidityContext(sweep_detected=False),  # NO SWEEP!
            volatility=VolatilityContext(atr=12.0, current_spread_pips=1.5),
            momentum=MomentumContext(trend_score=-10.0, adx=22.0),
            session=SessionContext(current_session="LONDON", is_prime_session=True),
            mtf_alignment={"D1": "BULLISH", "H4": "BULLISH"}
        )
        regime = RegimeOutput(
            primary_regime=MarketRegime.TREND_BULL,
            probabilities={"TREND_BULL": 0.80},
            confidence=0.80
        )
        devil = DevilAdvocateReport(symbol="XAUUSD", counter_bias="BULLISH", penalty_score=20.0, invalidation_risk_coefficient=0.85)

        gate_res = self.engine._apply_quality_gate(
            context=ctx,
            regime=regime,
            devil_report=devil,
            ai_score=75.0,
            rr_ratio=1.8,
            ev=10.0,
            final_win_p=0.60,
            spread=1.5,
            premium_discount_valid=True,
            account_balance=10000.0,
            tentative_bias="SELL"  # Counter-trend SELL in TREND_BULL!
        )
        self.assertFalse(gate_res.passed, "Counter-trend SELL in TREND_BULL without validated reversal MUST fail quality gate!")
        self.assertIn("Regime Trend Consistency", gate_res.failing_reasons)

    def test_timeframe_hierarchy_adaptation(self):
        """
        Verifies that Scalp uses H1/M15, Day Trading uses H4/H1, and Swing uses D1/H4.
        """
        mtf_data = {
            "D1": "BEARISH",
            "H4": "BEARISH",
            "H1": "BULLISH",
            "M15": "BULLISH",
            "M5": "BULLISH"
        }
        ctx = MarketContext(
            symbol="EURUSD",
            timestamp=datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc),
            current_price=1.0850,
            bid=1.0849,
            ask=1.0851,
            structure=StructureContext(bias="BULLISH"),
            liquidity=LiquidityContext(sweep_detected=True),
            volatility=VolatilityContext(atr=0.0015, current_spread_pips=1.0),
            momentum=MomentumContext(trend_score=25.0, adx=25.0),
            session=SessionContext(current_session="LONDON", is_prime_session=True),
            mtf_alignment=mtf_data
        )
        regime = RegimeOutput(primary_regime=MarketRegime.RANGE, probabilities={"RANGE": 0.70}, confidence=0.70)
        reports = {}

        # Swing should resolve to SELL because D1/H4 are BEARISH
        swing_bias = self.engine._resolve_market_trend_and_bias(ctx, regime, reports, trade_style="SWING")
        self.assertEqual(swing_bias, "SELL")

        # Scalp should resolve to BUY because H1/M15 are BULLISH
        scalp_bias = self.engine._resolve_market_trend_and_bias(ctx, regime, reports, trade_style="SCALP")
        self.assertEqual(scalp_bias, "BUY")

    def test_analyst_independence_no_echo_chamber(self):
        """
        Verifies that neutral analysts emit NEUTRAL and do not blindly echo st.bias.
        """
        ctx = MarketContext(
            symbol="EURUSD",
            timestamp=datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc),
            current_price=1.0850,
            bid=1.0849,
            ask=1.0851,
            structure=StructureContext(bias="BULLISH", bos=False, choch=False),
            liquidity=LiquidityContext(sweep_detected=False),  # No sweep!
            volatility=VolatilityContext(atr=0.0015, current_spread_pips=1.0, state="NORMAL"),
            momentum=MomentumContext(trend_score=0.0, adx=15.0),
            session=SessionContext(current_session="LONDON", is_prime_session=True)
        )
        regime = RegimeOutput(primary_regime=MarketRegime.RANGE, probabilities={"RANGE": 0.60}, confidence=0.60)

        liq_analyst = LiquidityAnalyst()
        vol_analyst = VolatilityAnalyst()
        risk_analyst = RiskAnalyst()
        macro_analyst = MacroAnalyst()

        liq_rep = liq_analyst.analyze(ctx, regime)
        vol_rep = vol_analyst.analyze(ctx, regime)
        risk_rep = risk_analyst.analyze(ctx, regime)
        macro_rep = macro_analyst.analyze(ctx, regime)

        self.assertEqual(liq_rep.bias, "NEUTRAL", "Liquidity analyst must return NEUTRAL when no sweep detected!")
        self.assertEqual(vol_rep.bias, "NEUTRAL", "Volatility analyst must return NEUTRAL when volatility is normal!")
        self.assertEqual(risk_rep.bias, "NEUTRAL", "Risk analyst must evaluate risk independently as NEUTRAL!")
        self.assertEqual(macro_rep.bias, "NEUTRAL", "Macro analyst must return NEUTRAL when no macro shock exists!")

    def test_anti_chase_protection(self):
        """
        Verifies that buying in PREMIUM without a deep sweep or extreme momentum is invalidated.
        """
        ctx = MarketContext(
            symbol="EURUSD",
            timestamp=datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc),
            current_price=1.0950,
            bid=1.0949,
            ask=1.0951,
            structure=StructureContext(bias="BULLISH", discount_premium_zone="PREMIUM", bos=True),
            liquidity=LiquidityContext(sweep_detected=False),
            volatility=VolatilityContext(atr=0.0015, current_spread_pips=1.0),
            momentum=MomentumContext(trend_score=30.0, adx=22.0),  # Not extreme momentum (< 50)
            session=SessionContext(current_session="LONDON", is_prime_session=True)
        )
        regime = RegimeOutput(primary_regime=MarketRegime.TREND_BULL, probabilities={"TREND_BULL": 0.70}, confidence=0.70)
        devil = DevilAdvocateReport(symbol="EURUSD", counter_bias="BEARISH", penalty_score=10.0, invalidation_risk_coefficient=0.80)

        gate_res = self.engine._apply_quality_gate(
            context=ctx,
            regime=regime,
            devil_report=devil,
            ai_score=80.0,
            rr_ratio=2.0,
            ev=12.0,
            final_win_p=0.65,
            spread=1.0,
            premium_discount_valid=False,  # Blocked by anti-chase!
            account_balance=10000.0,
            tentative_bias="BUY"
        )
        self.assertFalse(gate_res.passed)
        self.assertTrue(any("Premium/Discount" in r for r in gate_res.failing_reasons))

    def test_scalp_chop_veto(self):
        """
        Verifies that the dedicated Scalp Execution Engine vetoes trading when microstructure is in CHOP.
        """
        engine = ScalpExecutionEngine()
        # Create true choppy alternating dataframe with small bodies and large wicks (no displacement)
        opens = [100.0, 100.05, 100.0, 100.04, 100.01, 100.06, 100.02, 100.05, 100.01, 100.04, 100.0]
        closes = [100.05, 100.0, 100.04, 100.01, 100.06, 100.02, 100.05, 100.01, 100.04, 100.0, 100.03]
        highs = [max(o, c) + 0.3 for o, c in zip(opens, closes)]
        lows = [min(o, c) - 0.3 for o, c in zip(opens, closes)]
        df_m1 = pd.DataFrame({
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [100] * len(opens)
        })
        ctx = MarketContext(
            symbol="EURUSD",
            timestamp=datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc),
            current_price=100.0,
            bid=99.99,
            ask=100.01,
            structure=StructureContext(bias="BULLISH"),
            liquidity=LiquidityContext(),
            volatility=VolatilityContext(atr=0.5, current_spread_pips=1.0),
            momentum=MomentumContext(trend_score=10.0, adx=15.0),
            session=SessionContext(current_session="LONDON", is_prime_session=True)
        )
        regime = RegimeOutput(primary_regime=MarketRegime.RANGE, probabilities={"RANGE": 0.80}, confidence=0.80)

        scalp_dec = engine.evaluate_scalp(
            context=ctx,
            regime=regime,
            tentative_bias="BUY",
            df_m1=df_m1
        )
        self.assertEqual(scalp_dec.action, "NO_TRADE")
        self.assertTrue(any("CHOP" in r for r in scalp_dec.failing_reasons))

    def test_scalp_cost_friction_gate(self):
        """
        Verifies that scalps where spread + slippage > 25% of expected gross profit are rejected.
        """
        engine = ScalpExecutionEngine()
        # Scenario where spread is normal for symbol (1.0 pip vs 0.7 typical -> not expanded, no chop),
        # but friction (spread 1.0 + slippage 0.35 + commission 0.4 = 1.75 pips) exceeds 25% of small expected scalp profit (4.1 pips).
        ctx = MarketContext(
            symbol="EURUSD",
            timestamp=datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc),
            current_price=1.0850,
            bid=1.08495,
            ask=1.08505,
            structure=StructureContext(bias="BULLISH", discount_premium_zone="DISCOUNT"),
            liquidity=LiquidityContext(sweep_detected=True, sweep_type="BULLISH_SWEEP"),
            volatility=VolatilityContext(atr=0.0003, current_spread_pips=1.0),
            momentum=MomentumContext(trend_score=40.0, adx=26.0),
            session=SessionContext(current_session="LONDON", is_prime_session=True),
            mtf_alignment={"H1": "BULLISH", "M15": "BULLISH"}
        )
        regime = RegimeOutput(primary_regime=MarketRegime.TREND_BULL, probabilities={"TREND_BULL": 0.80}, confidence=0.80)

        # Microstructure with displacement
        df_m1 = pd.DataFrame({
            "open": [1.0848, 1.0849, 1.0850] * 4,
            "high": [1.0852] * 12,
            "low": [1.0845] * 12,
            "close": [1.0850] * 12,
            "volume": [100] * 12
        })

        scalp_dec = engine.evaluate_scalp(
            context=ctx,
            regime=regime,
            tentative_bias="BUY",
            df_m1=df_m1
        )
        self.assertFalse(scalp_dec.passed_cost_gate)
        self.assertTrue(any("Cost Friction Gate Failed" in r for r in scalp_dec.failing_reasons))


if __name__ == "__main__":
    unittest.main()
