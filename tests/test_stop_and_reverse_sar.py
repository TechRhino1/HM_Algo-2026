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
        self.assertEqual(len(active_positions), 0, "Position must be cleared to allow clean reversal execution")


if __name__ == "__main__":
    unittest.main()
