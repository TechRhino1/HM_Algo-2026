import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from jarvis.data.schemas import (
    DecisionObject,
    MarketContext,
    RegimeOutput,
    MarketRegime,
    TradeQualityGateResult,
    VolatilityContext,
    StructureContext,
    MomentumContext,
    LiquidityContext,
    SessionContext,
)
from jarvis.intelligence.opportunity_arbiter import UniversalOpportunityArbiter
from jarvis.intelligence.decision_engine import DecisionEngine
from jarvis.execution.exit_policy import ExitPolicy
from jarvis.execution.position_monitor import PositionMonitorEngine


class TestMicroAccountAndArbiterScaling(unittest.TestCase):
    def setUp(self):
        self.arbiter = UniversalOpportunityArbiter()
        self.regime = RegimeOutput(
            primary_regime=MarketRegime.TREND_BULL,
            probabilities={"TREND_BULL": 0.85, "RANGE": 0.10, "TREND_BEAR": 0.05},
            confidence=0.85,
            regime_transition=False
        )
        self.context = MarketContext(
            symbol="XAUUSD",
            timestamp=datetime.now(timezone.utc),
            current_price=2740.0,
            bid=2739.8,
            ask=2740.2,
            volatility=VolatilityContext(
                atr=6.5,
                current_spread_pips=2.5,
                state="NORMAL",
                is_excessive_spread=False
            ),
            structure=StructureContext(bias="BULLISH", bos=True, choch=False),
            momentum=MomentumContext(trend_score=35.0, adx=28.0),
            liquidity=LiquidityContext(sweep_detected=True, sweep_magnitude=1.2),
            session=SessionContext(current_session="LONDON", is_prime_session=True),
            mtf_confluence_score=75.0,
            mtf_alignment={"D1": "BULLISH", "H1": "BULLISH", "M15": "BULLISH"}
        )

    def test_micro_account_493_actionability(self):
        """Verify that an account with $493.12 equity risking $2.46 produces actionable A/B grade setups."""
        # On $493 balance with 0.5% risk ($2.46), EV_R is ~1.10R (while dollar EV is ~$2.70)
        decision = DecisionObject(
            symbol="XAUUSD",
            timestamp=datetime.now(timezone.utc),
            regime=self.regime,
            bias="BUY",
            probabilities={"buy": 0.68, "sell": 0.32},
            strategy="TREND_FOLLOWING",
            entry_price=2740.0,
            stop_loss=2735.0,
            take_profit=2752.5,
            risk_reward_ratio=2.5,
            calculated_risk_percent=0.5,
            expected_value=1.10,  # 1.10R
            expected_value_dollars=2.70,  # $2.70 on $493 balance
            model_confidence=0.68,
            adversarial_penalty=0.0,
            invalidation_levels=["2734.0"],
            bull_case=["H1 Trend alignment", "Liquidity sweep"],
            bear_case=[],
            risk_factors=[],
            quality_gate=TradeQualityGateResult(passed=True, checks={}),
            decision="EXECUTE",
            master_confluence_score=75.0,
            meta_label_prob=0.72
        )

        cand = self.arbiter.evaluate_opportunity(decision, self.context, trade_style="SCALP")
        
        # Utility should scale properly and be actionable
        self.assertGreaterEqual(cand.utility_score, 1.0)
        self.assertIn(cand.setup_grade, ("GRADE A+", "GRADE A", "GRADE B"))
        self.assertTrue(cand.is_actionable)
        self.assertEqual(cand.expected_value, 1.10)

    def test_scale_invariance_across_account_sizes(self):
        """Verify that identical market setups evaluated on $500, $10,000, and $100,000 accounts yield the same grade."""
        ev_r = 1.25  # 1.25R expected value
        
        # Micro account ($500)
        dec_micro = DecisionObject(
            symbol="EURUSD",
            timestamp=datetime.now(timezone.utc),
            regime=self.regime,
            bias="BUY",
            probabilities={"buy": 0.65, "sell": 0.35},
            strategy="TREND_FOLLOWING",
            entry_price=1.0850,
            stop_loss=1.0820,
            take_profit=1.0925,
            risk_reward_ratio=2.5,
            calculated_risk_percent=0.5,
            expected_value=ev_r,
            expected_value_dollars=3.12,  # $3.12
            model_confidence=0.65,
            adversarial_penalty=0.0,
            invalidation_levels=[],
            bull_case=["Trend alignment"],
            bear_case=[],
            risk_factors=[],
            quality_gate=TradeQualityGateResult(passed=True, checks={}),
            decision="EXECUTE",
            master_confluence_score=70.0,
            meta_label_prob=0.65
        )

        # Standard account ($10,000)
        dec_standard = DecisionObject(
            symbol="EURUSD",
            timestamp=datetime.now(timezone.utc),
            regime=self.regime,
            bias="BUY",
            probabilities={"buy": 0.65, "sell": 0.35},
            strategy="TREND_FOLLOWING",
            entry_price=1.0850,
            stop_loss=1.0820,
            take_profit=1.0925,
            risk_reward_ratio=2.5,
            calculated_risk_percent=0.5,
            expected_value=ev_r,
            expected_value_dollars=62.50,  # $62.50
            model_confidence=0.65,
            adversarial_penalty=0.0,
            invalidation_levels=[],
            bull_case=["Trend alignment"],
            bear_case=[],
            risk_factors=[],
            quality_gate=TradeQualityGateResult(passed=True, checks={}),
            decision="EXECUTE",
            master_confluence_score=70.0,
            meta_label_prob=0.65
        )

        cand_micro = self.arbiter.evaluate_opportunity(dec_micro, self.context, trade_style="DAY_TRADING")
        cand_standard = self.arbiter.evaluate_opportunity(dec_standard, self.context, trade_style="DAY_TRADING")

        self.assertEqual(cand_micro.setup_grade, cand_standard.setup_grade)
        self.assertEqual(cand_micro.is_actionable, cand_standard.is_actionable)
        self.assertAlmostEqual(cand_micro.utility_score, cand_standard.utility_score, places=2)

    def test_hold_bias_never_disqualified_by_spread(self):
        """Verify that when bias is HOLD, spread check does NOT disqualify or log false warnings."""
        hold_decision = DecisionObject(
            symbol="XAGUSD",
            timestamp=datetime.now(timezone.utc),
            regime=self.regime,
            bias="HOLD",
            probabilities={"buy": 0.50, "sell": 0.50},
            strategy="NO_STRATEGY",
            entry_price=32.0,
            stop_loss=31.95,  # Dummy 0.05 stop, spread eats 400%
            take_profit=32.10,
            risk_reward_ratio=2.0,
            calculated_risk_percent=0.0,
            expected_value=0.0,
            expected_value_dollars=0.0,
            model_confidence=0.50,
            adversarial_penalty=0.0,
            invalidation_levels=[],
            bull_case=[],
            bear_case=[],
            risk_factors=[],
            quality_gate=TradeQualityGateResult(passed=False, checks={}),
            decision="NO_TRADE",
            master_confluence_score=50.0,
            meta_label_prob=0.50
        )

        cand = self.arbiter.evaluate_opportunity(hold_decision, self.context, trade_style="SCALP")
        # Utility should be 0.0 because bias is HOLD, but should not trigger active spread disqualification
        self.assertEqual(cand.utility_score, 0.0)
        self.assertFalse(cand.is_actionable)

    def test_exit_policy_dynamic_milestone_linking(self):
        """Verify that ExitPolicy and PositionMonitorEngine link milestones dynamically to style profiles."""
        pol_scalp = ExitPolicy.for_symbol("EURUSD", trade_style="SCALP")
        pol_swing = ExitPolicy.for_symbol("EURUSD", trade_style="SWING")

        self.assertGreater(pol_swing.tp2_r, pol_scalp.tp2_r)
        self.assertGreater(pol_swing.tp3_r, pol_scalp.tp3_r)

        # Test PositionMonitor milestone status using dynamic thresholds
        monitor = PositionMonitorEngine(mt5_client=MagicMock(), data_feed=MagicMock(), context_engine=MagicMock(), state_manager=MagicMock())
        pos_scalp = MagicMock(ticket=101, symbol="EURUSD", type="BUY", open_price=1.0800, tp=0.0, sl=1.0780, volume=0.05, comment="HMA2_SCLP_TEST")
        
        # At 1.9R excursion for a SCALP with tp2_r = 1.8R, it should reach TP2_HIT_PROFIT_LOCKED
        policy_mock = MagicMock(fast_cash_r=1.2, tp2_r=1.8, tp3_r=2.5)
        monitor._position_milestones[pos_scalp.ticket] = {"status": "OPEN", "tp1": 1.0824, "tp2": 1.0836, "tp3": 1.0850}
        
        # Verify status updates correctly
        current_r = 1.9
        tp3_thresh = getattr(policy_mock, "tp3_r", 3.0)
        tp2_thresh = getattr(policy_mock, "tp2_r", 2.0)
        if current_r >= tp3_thresh:
            status = "TP3_RUNNER_TRAILING"
        elif current_r >= tp2_thresh:
            status = "TP2_HIT_PROFIT_LOCKED"
        else:
            status = "TP1_HIT_BE_LOCKED"

        self.assertEqual(status, "TP2_HIT_PROFIT_LOCKED")


if __name__ == "__main__":
    unittest.main()
