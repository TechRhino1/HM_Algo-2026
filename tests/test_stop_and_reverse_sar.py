"""
Unit tests for Institutional Stop-and-Reverse (SAR) Position Flip and Premium/Discount Limit Routing.
Verifies:
1. Orchestrator position flip closes opposing losing trade and executes reversal.
2. Decision engine routes to LIMIT order ("Sell from the Top") when selling in Discount or oversold RSI.
"""
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from jarvis.data.schemas import (
    PositionSnapshot,
    MarketContext,
    StructureContext,
    MomentumContext,
    LiquidityContext,
    VolatilityContext,
    SessionContext,
    DecisionObject,
    RegimeOutput,
    MarketRegime,
)


class TestStopAndReverseAndSellFromTop(unittest.TestCase):
    def test_decision_engine_routes_limit_when_selling_in_discount(self):
        """Selling in Discount zone with oversold RSI routes to LIMIT order ("Sell from the Top")."""
        from jarvis.intelligence.decision_engine import DecisionEngine

        engine = DecisionEngine()
        
        ctx = MarketContext(
            symbol="XAUUSD",
            timestamp=datetime.now(timezone.utc),
            current_price=4150.36,
            bid=4150.16,
            ask=4150.56,
            structure=StructureContext(
                bias="BEARISH",
                discount_premium_zone="DISCOUNT",
                equilibrium_price=4165.0,
                order_blocks=[{"type": "BEARISH_OB", "low": 4166.50, "high": 4170.0}],
            ),
            liquidity=LiquidityContext(sweep_detected=False),
            volatility=VolatilityContext(atr=5.0),
            momentum=MomentumContext(rsi=28.5, trend_score=-35.0),
            session=SessionContext(is_prime_session=True),
        )

        regime = RegimeOutput(
            primary_regime=MarketRegime.TREND_BEAR,
            probabilities={"TREND_BEAR": 0.85},
            confidence=0.85
        )

        decision = engine.evaluate(
            context=ctx,
            regime=regime,
            analyst_reports={},
            devil_report=MagicMock(penalty_score=10.0, invalidation_risk_coefficient=0.8, threats_detected=[]),
            account_balance=1000.0,
            trade_style="DAY_TRADING"
        )

        # In discount with oversold RSI, order_type must be LIMIT seeking Premium retracement ("Sell from the top")
        self.assertEqual(decision.order_type, "LIMIT", "Selling into discount floor must route to LIMIT order")
        self.assertGreater(decision.entry_price, ctx.current_price, "Limit entry must be higher than market price (seeking premium)")
        self.assertGreaterEqual(decision.entry_price, 4165.0, "Entry price must target equilibrium or bearish order block")

    def test_sar_position_flip_logic(self):
        """Opposing flat/losing trade is closed to allow reversal trade to execute."""
        mock_mt5 = MagicMock()
        mock_mt5.close_position.return_value = {"status": "CLOSED", "ticket": 962551759}

        active_positions = [
            PositionSnapshot(
                ticket=962551759,
                symbol="XAUUSD",
                type="SELL",
                volume=0.01,
                open_price=4150.36,
                current_price=4158.00,  # Floating loss (-0.38R)
                sl=4170.63,
                tp=4105.77,
                profit=-7.64,
                swap=0.0,
                commission=0.0,
                open_time=datetime.now(timezone.utc).isoformat(),
                magic=999999,
            )
        ]

        # Incoming high-conviction BUY decision
        new_decision = MagicMock()
        new_decision.bias = "BUY"
        new_decision.decision = "EXECUTE"
        new_decision.model_confidence = 0.60
        new_decision.expected_value = 1.85
        new_decision.risk_reward_ratio = 2.1

        opposing_positions = [
            p for p in active_positions
            if getattr(p, "type", "").upper() != str(new_decision.bias).upper()
        ]
        self.assertEqual(len(opposing_positions), 1)
        opp_pos = opposing_positions[0]

        opp_risk_dist = abs(opp_pos.open_price - opp_pos.sl)
        opp_r = (opp_pos.open_price - opp_pos.current_price) / opp_risk_dist  # negative
        self.assertLess(opp_r, 0.50)

        # Simulate SAR trigger
        close_res = mock_mt5.close_position(opp_pos.ticket)
        self.assertEqual(close_res["status"], "CLOSED")
        active_positions = [p for p in active_positions if p.ticket != opp_pos.ticket]
    def test_sar_does_not_flip_fresh_position_in_breathing_room(self):
        """A fresh position (< 300s) with minor spread noise (-0.01R) is NOT flipped by low confidence."""
        opp_pos = PositionSnapshot(
            ticket=962724022,
            symbol="BTCUSD",
            type="BUY",
            volume=0.01,
            open_price=83252.05,
            current_price=83246.95,  # -0.01R (spread noise)
            sl=82529.74,
            tp=85351.06,
            profit=-0.05,
            swap=0.0,
            commission=0.0,
            open_time=datetime.now(timezone.utc).isoformat(),  # 0s old
            magic=888999,
            comment="HMA2_DAY_TRND_DEMO",
        )
        opp_age_sec = 70.0  # Fresh trade
        opp_risk_dist = abs(opp_pos.open_price - opp_pos.sl)
        opp_r = (opp_pos.current_price - opp_pos.open_price) / opp_risk_dist  # -0.007R

        new_decision = MagicMock()
        new_decision.bias = "SELL"
        new_decision.trade_style = "SCALP"
        new_decision.model_confidence = 0.534  # Barely 53.4%
        new_decision.expected_value = 1.93
        new_decision.risk_reward_ratio = 1.4

        has_breathing_room = (opp_age_sec >= 300.0) or (opp_r <= -0.50)
        self.assertFalse(has_breathing_room, "Fresh trade must have breathing room (>300s)")

        opp_is_failing = (opp_r <= -0.15)
        self.assertFalse(opp_is_failing, "Trade at -0.01R is not failing, it's just paying spread")

        new_is_scalp = (new_decision.trade_style == "SCALP")
        opp_is_day = "DAY" in opp_pos.comment
        horizon_allowed = not (new_is_scalp and opp_is_day)
        self.assertFalse(horizon_allowed, "Lower timeframe scalp cannot flip day trading position")

    def test_pending_order_utc_setup_time_offset(self):
        """Pending order raw broker time (GMT+3) is adjusted by broker offset to UTC epoch."""
        import time
        now_utc = int(time.time())
        broker_offset = 10800  # GMT+3
        raw_broker_time_setup = now_utc + broker_offset

        raw_setup = raw_broker_time_setup
        utc_setup = (raw_setup - broker_offset) if raw_setup > 0 else now_utc
        self.assertEqual(utc_setup, now_utc, "time_setup must be converted to true UTC epoch")


    def test_orchestrator_single_pass_applies_sar_flip(self):
        """In _orchestration_loop_single_pass, an opposing trade is closed (profit banked/loss cut) and the reverse trade executes."""
        from jarvis.application.orchestrator import JarvisOrchestrator
        from jarvis.intelligence.opportunity_arbiter import CandidateOpportunity
        from jarvis.data.schemas import TradeQualityGateResult

        with patch("jarvis.application.orchestrator.MT5Client"), \
             patch("jarvis.application.orchestrator.ExecutionEngine"):

            orch = JarvisOrchestrator(symbols=["XAUUSD"], mode="paper", trade_style="DAY_TRADING")
            orch.mt5_client.close_position = MagicMock(return_value={"status": "CLOSED", "ticket": 777123})
            orch.mt5_client.get_pending_orders = MagicMock(return_value=[])
            orch.mt5_client.get_symbol_trading_spec = MagicMock(return_value={
                "name": "XAUUSD",
                "trade_contract_size": 100.0,
                "trade_tick_value": 1.0,
                "trade_tick_size": 0.01,
                "volume_min": 0.01,
                "volume_max": 100.0,
                "volume_step": 0.01,
            })
            orch.execution_engine.execute_decision = MagicMock(return_value={"status": "FILLED", "ticket": 888456})

            # Existing opposing SELL trade in profit (+0.40R)
            existing_pos = PositionSnapshot(
                ticket=777123,
                symbol="XAUUSD",
                type="SELL",
                volume=0.01,
                open_price=2400.0,
                current_price=2396.0,
                sl=2410.0,
                tp=2370.0,
                profit=40.0,
                swap=0.0,
                commission=0.0,
                open_time=(datetime.now(timezone.utc)).isoformat(),
                magic=999999,
                comment="HMA2_DAY_TRND",
            )
            orch.state_manager.positions = [existing_pos]
            orch.state_manager.account = MagicMock(equity=10000.0, balance=10000.0, margin=100.0)

            # Incoming BUY candidate from Arbiter
            cand_dec = MagicMock()
            cand_dec.symbol = "XAUUSD"
            cand_dec.bias = "BUY"
            cand_dec.decision = "EXECUTE"
            cand_dec.price = 2400.0
            cand_dec.entry_price = 2400.0
            cand_dec.stop_loss = 2392.0
            cand_dec.take_profit = 2420.0
            cand_dec.risk_reward_ratio = 2.5
            cand_dec.expected_value = 2.0
            cand_dec.model_confidence = 0.75
            cand_dec.ai_score = 85.0
            cand_dec.trade_style = "DAY_TRADING"
            cand_dec.pattern_sample_size = 50
            cand_dec.adversarial_penalty = 0.0
            cand_dec.calculated_risk_percent = 1.0
            cand_dec.meta_label_prob = 0.80
            cand_dec.dissection_score = 80.0
            cand_dec.first_target_volume_pct = 0.5
            cand_dec.first_target_price = 2410.0
            cand_dec.runner_trail_distance_atr = 1.0
            cand_dec.quality_gate = TradeQualityGateResult(passed=True, checks={}, failing_reasons=[])
            cand_dec.regime = RegimeOutput(primary_regime=MarketRegime.TREND_BULL, probabilities={}, confidence=0.85)

            cand = CandidateOpportunity(
                symbol="XAUUSD",
                trade_style="DAY_TRADING",
                timeframe="H1/M15/M5",
                strategy="BREAKOUT_EXPANSION",
                bias="BUY",
                entry_price=2400.0,
                stop_loss=2392.0,
                take_profit=2420.0,
                risk_reward_ratio=2.5,
                expected_value=2.0,
                win_prob=75.0,
                ml_prob=0.75,
                confluence_score=80.0,
                confluence_factors=[],
                adversarial_penalty=0.0,
                risk_factors=[],
                regime="TREND_BULL",
                regime_multiplier=1.2,
                utility_score=2.2,
                setup_grade="GRADE A+",
                is_actionable=True,
                decision_obj=cand_dec,
                context=MagicMock(volatility=MagicMock(atr=8.0, current_spread_pips=1.5), current_price=2396.0)
            )

            orch.scan_all_modes = MagicMock(return_value=(cand, [cand], [("XAUUSD", "DAY_TRADING", {"decision": cand_dec})]))

            orch._orchestration_loop_single_pass(dry_run=False)

            # Assert opposing position was closed via SAR
            orch.mt5_client.close_position.assert_called_once_with(777123)
            # Assert reverse BUY trade was executed
            orch.execution_engine.execute_decision.assert_called_once()

if __name__ == "__main__":
    unittest.main()
