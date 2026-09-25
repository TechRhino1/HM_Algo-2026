"""
Unit tests for Hybrid Smart Order Routing (SOR) and Dynamic SL Trailing enhancements.
"""
import unittest
from unittest.mock import MagicMock
from datetime import datetime, timezone

from jarvis.data.schemas import DecisionObject, MarketContext, PositionSnapshot
from jarvis.execution.execution_engine import ExecutionEngine
from jarvis.execution.position_monitor import PositionMonitorEngine
from jarvis.execution.exit_policy import ExitPolicy, evaluate_exit


class TestHybridSmartOrderRouting(unittest.TestCase):
    def setUp(self):
        self.mock_mt5 = MagicMock()
        self.mock_mt5.mode = "demo"
        self.mock_state = MagicMock()
        self.mock_state.is_safe_mode = False
        self.mock_state.execution_mode = MagicMock(value="DEMO")
        self.mock_state.account = MagicMock(equity=10000.0)

        # Mock broker trading spec
        self.mock_mt5.get_symbol_trading_spec.return_value = {
            "point": 0.01,
            "digits": 2,
            "trade_stops_level": 10,
            "spread": 20,
            "trade_tick_value": 1.0,
            "trade_tick_size": 0.01,
            "volume_step": 0.01,
            "volume_min": 0.01
        }
        self.engine = ExecutionEngine(self.mock_mt5, self.mock_state)

    def test_limit_order_dispatched_when_outside_broker_stops(self):
        """Verify LIMIT order placed when entry price has sufficient distance from market price."""
        ctx = MagicMock()
        ctx.current_price = 2400.0
        ctx.bid = 2399.80
        ctx.ask = 2400.20

        # Planned pullback entry 5.0 points below market
        dec = DecisionObject(
            symbol="XAUUSD",
            timestamp=datetime.now(timezone.utc),
            regime="TREND_PULLBACK",
            bias="BUY",
            probabilities={"BUY": 0.75, "SELL": 0.25},
            strategy="TREND_PULLBACK",
            order_type="LIMIT",
            entry_price=2395.0,  # 4.8 points below bid (well outside 0.30 min stop dist)
            stop_loss=2385.0,
            take_profit=2420.0,
            risk_reward_ratio=2.5,
            calculated_risk_percent=1.0,
            expected_value=1.5,
            model_confidence=0.85,
            adversarial_penalty=0.0,
            invalidation_levels=[],
            bull_case=[],
            bear_case=[],
            risk_factors=[],
            quality_gate=MagicMock(failing_reasons=[]),
            decision="EXECUTE",
            execution_authorized=True,
            context=ctx
        )

        self.mock_mt5.place_pending_order.return_value = {"status": "PLACED", "ticket": 77701}
        res = self.engine.execute_decision(dec, lots=0.10)

        self.mock_mt5.place_pending_order.assert_called_once()
        self.mock_mt5.send_market_order.assert_not_called()
        self.assertEqual(res.get("status"), "PLACED")

    def test_limit_order_falls_back_to_market_when_within_broker_stops(self):
        """Verify seamless fallback to MARKET order when limit price is within broker minimum stop distance."""
        ctx = MagicMock()
        ctx.current_price = 2400.0
        ctx.bid = 2399.80
        ctx.ask = 2400.20

        # Entry price only 0.05 points below bid (encroaching broker minimum stop distance)
        dec = DecisionObject(
            symbol="XAUUSD",
            timestamp=datetime.now(timezone.utc),
            regime="TREND_PULLBACK",
            bias="BUY",
            probabilities={"BUY": 0.75, "SELL": 0.25},
            strategy="TREND_PULLBACK",
            order_type="LIMIT",
            entry_price=2399.75,  # Too close to bid (2399.80 - 0.05 < min_broker_stop_dist)
            stop_loss=2385.0,
            take_profit=2420.0,
            risk_reward_ratio=2.5,
            calculated_risk_percent=1.0,
            expected_value=1.5,
            model_confidence=0.85,
            adversarial_penalty=0.0,
            invalidation_levels=[],
            bull_case=[],
            bear_case=[],
            risk_factors=[],
            quality_gate=MagicMock(failing_reasons=[]),
            decision="EXECUTE",
            execution_authorized=True,
            context=ctx
        )

        self.mock_mt5.send_market_order.return_value = {"status": "FILLED", "ticket": 77702, "price": 2400.20}
        res = self.engine.execute_decision(dec, lots=0.10)

        # Fallback to market order triggered
        self.mock_mt5.send_market_order.assert_called_once()
        self.mock_mt5.place_pending_order.assert_not_called()
        self.assertEqual(res.get("status"), "FILLED")

    def test_manual_mode_trail_enables_trailing_on_manual_trade(self):
        """Verify that when manual_mode is 'TRAIL', manual trades (magic=0) are dynamically trailed."""
        mock_data = MagicMock()
        mock_ctx_eng = MagicMock()

        self.mock_mt5.get_symbol_trading_spec.return_value = {
            "point": 0.00001,
            "digits": 5,
            "trade_stops_level": 10,
            "spread": 10,
            "trade_tick_value": 1.0,
            "trade_tick_size": 0.00001,
            "volume_step": 0.01,
            "volume_min": 0.01
        }
        pm = PositionMonitorEngine(
            mt5_client=self.mock_mt5,
            data_feed=mock_data,
            context_engine=mock_ctx_eng,
            state_manager=self.mock_state,
            manual_mode="TRAIL"
        )

        # Manual position in profit (+2.5R)
        pos = PositionSnapshot(
            ticket=88801,
            symbol="EURUSD",
            type="BUY",
            volume=0.10,
            open_price=1.1000,
            current_price=1.1125,  # +2.5R profit (risk dist = 0.0050)
            sl=1.0950,
            tp=0.0,
            profit=125.0,
            swap=0.0,
            commission=0.0,
            open_time="2026-09-25 10:00:00",
            magic=0,  # manual trade
            comment="Desk trade"
        )

        ctx = MagicMock()
        ctx.current_price = 1.1125
        ctx.volatility.atr = 0.0020
        ctx.volatility.current_spread_pips = 1.0
        pm._get_context = MagicMock(return_value=ctx)
        pm._get_cached_regime = MagicMock(return_value=None)
        pm._get_position_duration_sec = MagicMock(return_value=600.0)

        self.mock_mt5.modify_position.return_value = {"status": "MODIFIED"}
        pm._manage_single_position(pos)

        # In TRAIL mode, modify_position MUST be called to lock profit / trail SL
        self.mock_mt5.modify_position.assert_called()
        call_sl = self.mock_mt5.modify_position.call_args.kwargs.get("sl")
        self.assertGreater(call_sl, 1.1000, "Stop loss must be ratcheted above entry for manual trade in TRAIL mode")


if __name__ == "__main__":
    unittest.main()
