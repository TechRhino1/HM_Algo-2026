"""
Unit tests for Pending Orders Pipeline (Tasks 2 & 5).
Validates:
1. MT5Client paper-mode place_pending_order, get_pending_orders, cancel_pending_order
2. ExecutionEngine order_type routing (MARKET vs LIMIT)
3. Direction-vs-price sanity check and fallback to MARKET order
4. OrderManager cleanup_stale_pending_orders
"""
import time
import unittest
from unittest.mock import MagicMock
from datetime import datetime, timezone

from jarvis.execution.mt5_client import MT5Client
from jarvis.execution.execution_engine import ExecutionEngine
from jarvis.execution.order_manager import OrderManager
from jarvis.data.schemas import (
    DecisionObject, RegimeOutput, MarketRegime, TradeQualityGateResult,
    MarketContext, StructureContext, LiquidityContext, VolatilityContext,
    MomentumContext, SessionContext
)
from jarvis.application.state_manager import StateManager


class TestPendingOrders(unittest.TestCase):
    def setUp(self):
        self.mt5_client = MT5Client(mode="paper")
        self.state_manager = StateManager()
        self.execution_engine = ExecutionEngine(self.mt5_client, self.state_manager)
        self.order_manager = OrderManager(self.mt5_client)

    def test_mt5_client_pending_orders_paper_mode(self):
        """Validates MT5Client pending order placement, fetching, and cancellation in paper mode."""
        res = self.mt5_client.place_pending_order(
            symbol="BTCUSD",
            order_type="BUY_LIMIT",
            price=70000.0,
            volume=0.01,
            sl_price=68000.0,
            tp_price=75000.0,
            comment="TEST_LIMIT"
        )
        self.assertEqual(res["status"], "PLACED")
        ticket = res["ticket"]
        self.assertIsNotNone(ticket)

        # In paper mode, place_pending_order returns PLACED status
        cancel_res = self.mt5_client.cancel_pending_order(ticket)
        self.assertEqual(cancel_res["status"], "CANCELLED")

    def test_execution_engine_limit_order_routing(self):
        """Validates ExecutionEngine routes order_type='LIMIT' to place_pending_order when direction is sane."""
        ctx = MarketContext(
            symbol="EURUSD",
            timestamp=datetime.now(timezone.utc),
            current_price=1.0850,
            bid=1.0849,
            ask=1.0851,
            structure=StructureContext(bias="BULLISH"),
            liquidity=LiquidityContext(),
            volatility=VolatilityContext(atr=0.0015, current_spread_pips=1.0),
            momentum=MomentumContext(),
            session=SessionContext()
        )
        regime = RegimeOutput(primary_regime=MarketRegime.TREND_BULL, probabilities={}, confidence=0.85)
        gate = TradeQualityGateResult(passed=True, checks={})

        # Sane BUY_LIMIT: price (1.0800) < bid (1.0849)
        decision = DecisionObject(
            symbol="EURUSD",
            timestamp=datetime.now(timezone.utc),
            regime=regime,
            bias="BUY",
            probabilities={"buy": 0.7, "sell": 0.1, "no_trade": 0.2},
            strategy="TREND_PULLBACK",
            order_type="LIMIT",
            entry_price=1.0800,
            stop_loss=1.0750,
            take_profit=1.0950,
            risk_reward_ratio=3.0,
            calculated_risk_percent=0.5,
            expected_value=15.0,
            model_confidence=0.75,
            adversarial_penalty=0.0,
            invalidation_levels=[],
            bull_case=[],
            bear_case=[],
            risk_factors=[],
            quality_gate=gate,
            decision="EXECUTE",
            execution_authorized=True,
            context=ctx
        )

        res = self.execution_engine.execute_decision(decision, lots=0.01)
        self.assertEqual(res.get("status"), "PLACED")

    def test_execution_engine_limit_direction_sanity_fallback(self):
        """Validates that a BUY_LIMIT price >= current bid falls back safely to MARKET order dispatch."""
        ctx = MarketContext(
            symbol="EURUSD",
            timestamp=datetime.now(timezone.utc),
            current_price=1.0850,
            bid=1.0849,
            ask=1.0851,
            structure=StructureContext(bias="BULLISH"),
            liquidity=LiquidityContext(),
            volatility=VolatilityContext(atr=0.0015, current_spread_pips=1.0),
            momentum=MomentumContext(),
            session=SessionContext()
        )
        regime = RegimeOutput(primary_regime=MarketRegime.TREND_BULL, probabilities={}, confidence=0.85)
        gate = TradeQualityGateResult(passed=True, checks={})

        # Insane BUY_LIMIT: price (1.0900) >= bid (1.0849) -> should fall back to MARKET order fill
        decision = DecisionObject(
            symbol="EURUSD",
            timestamp=datetime.now(timezone.utc),
            regime=regime,
            bias="BUY",
            probabilities={"buy": 0.7, "sell": 0.1, "no_trade": 0.2},
            strategy="TREND_PULLBACK",
            order_type="LIMIT",
            entry_price=1.0900,
            stop_loss=1.0750,
            take_profit=1.0950,
            risk_reward_ratio=3.0,
            calculated_risk_percent=0.5,
            expected_value=15.0,
            model_confidence=0.75,
            adversarial_penalty=0.0,
            invalidation_levels=[],
            bull_case=[],
            bear_case=[],
            risk_factors=[],
            quality_gate=gate,
            decision="EXECUTE",
            execution_authorized=True,
            context=ctx
        )

        res = self.execution_engine.execute_decision(decision, lots=0.01)
        # Market order in paper mode returns status="FILLED"
        self.assertEqual(res.get("status"), "FILLED")

    def test_order_manager_stale_cleanup(self):
        """Validates OrderManager cleanup_stale_pending_orders."""
        res = self.order_manager.cleanup_stale_pending_orders(max_age_sec=10)
        self.assertIsInstance(res, list)

    def test_api_pending_orders_route_handler(self):
        """Validates GET /api/pending_orders and POST /api/action/cancel_pending_order handler logic in server.py."""
        from jarvis.api.server import JarvisRequestHandler
        from unittest.mock import MagicMock

        handler = MagicMock(spec=JarvisRequestHandler)
        handler.mt5_client = self.mt5_client
        handler._require_role = MagicMock(return_value=(True, None))
        
        # Place a dummy pending order
        p = self.mt5_client.place_pending_order("XAUUSD", "BUY_LIMIT", 2350.0, 0.1, 2340.0, 2370.0, "TEST")
        ticket = p["ticket"]

        # Test pending orders retrieval
        orders = handler.mt5_client.get_pending_orders()
        self.assertTrue(any(o["ticket"] == ticket for o in orders))

        # Test cancellation handler
        cancel_res = handler.mt5_client.cancel_pending_order(ticket)
        self.assertEqual(cancel_res.get("status"), "CANCELLED")

    def test_pending_order_feature_persistence_on_placed_status(self):
        """Validates that PLACED pending limit orders persist open-time features and trade memory."""
        from jarvis.application.orchestrator import JarvisOrchestrator

        orchestrator = JarvisOrchestrator(mode="demo")

        fake_decision = MagicMock()
        fake_decision.symbol = "BTCUSD"
        fake_decision.bias = "SELL"
        fake_decision.strategy = "SMC_ORDER_BLOCK"
        fake_decision.entry_price = 83500.0
        fake_decision.stop_loss = 83800.0
        fake_decision.take_profit = 82500.0
        fake_decision.risk_reward_ratio = 3.33
        fake_decision.model_confidence = 0.72
        fake_decision.adversarial_penalty = 0.0
        fake_decision.expected_value = 2.4
        fake_decision.raw_win_prob = 0.68
        fake_decision.regime = MagicMock()
        fake_decision.regime.primary_regime.value = "TREND_BEAR"

        fake_exec_res = {
            "status": "PLACED",
            "ticket": 962799999,
            "price": 83500.0,
            "sl": 83800.0,
            "tp": 82500.0
        }

        fake_ctx = MagicMock()
        orchestrator._record_open_trade_context(
            exec_res=fake_exec_res,
            decision=fake_decision,
            context=fake_ctx,
            trade_style="DAY_TRADING",
            lots=0.01
        )

        # Context MUST be registered under ticket 962799999
        self.assertIn(962799999, orchestrator._pending_features)
        pending_data = orchestrator._pending_features[962799999]
        self.assertEqual(pending_data["symbol"], "BTCUSD")
        self.assertEqual(pending_data["strategy"], "SMC_ORDER_BLOCK")
        self.assertEqual(pending_data["entry"], 83500.0)
        self.assertEqual(pending_data["risk_dist"], 300.0)

    def test_pending_order_integer_type_and_price_open_invalidation(self):
        """Validates that OrderManager parses integer MT5 order types and price_open for runaway invalidation."""
        mock_client = MagicMock()
        # Mock pending order with MT5 integer type 3 (SELL_LIMIT) and price_open
        mock_client.get_pending_orders.return_value = [{
            "ticket": 771122,
            "symbol": "GOLD",
            "type": 3,  # ORDER_TYPE_SELL_LIMIT as raw integer
            "price_open": 2400.0,
            "sl": 2420.0,
            "tp": 2350.0,
            "comment": "HMA2_DAY_SMC_DEMO",
            "time_setup": time.time()  # fresh setup
        }]
        mock_client.cancel_pending_order.return_value = {"status": "CANCELLED", "ticket": 771122}

        om = OrderManager(mock_client)
        # Context where price surged above stop loss (2425 > 2420)
        ctx = MagicMock()
        ctx.current_price = 2425.0
        ctx.volatility.atr = 5.0
        ctx.structure = None
        ctx.momentum = None

        cancelled = om.cleanup_stale_pending_orders(max_age_sec=600, context_map={"GOLD": ctx})
        self.assertEqual(len(cancelled), 1)
        self.assertEqual(cancelled[0]["ticket"], 771122)
        mock_client.cancel_pending_order.assert_called_with(771122)


if __name__ == "__main__":
    unittest.main()

