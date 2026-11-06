import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

from jarvis.execution.execution_engine import ExecutionEngine
from jarvis.data.schemas import (
    DecisionObject, MarketContext, StructureContext, VolatilityContext,
    MomentumContext, SessionContext, LiquidityContext, MarketRegime,
    RegimeOutput, TradeQualityGateResult, DevilAdvocateReport
)


class TestGoldEntryFixes(unittest.TestCase):
    def test_pending_order_logs_pending_row_for_reconciliation(self):
        """Placing a pending limit must journal a PENDING row, not drop it.

        `sync_mt5_history` closes this row when the broker either fills or
        cancels the order. Suppressing the write at placement (the old
        ``if is_fill:`` change) made every pending fill invisible to
        reconciliation. The phantom-journal defect was in `trade_records`,
        where an intent was recorded as if it were a trade; that is fixed by
        deferring `TradeMemory.record_trade` until POSITION_OPENED.
        """
        mock_mt5 = MagicMock()
        mock_mt5.is_connected = True
        mock_mt5.get_pending_orders.return_value = []
        mock_mt5.place_pending_order.return_value = {
            "status": "PLACED",
            "ticket": 999123456,
            "price": 3984.94,
            "type": "SELL_LIMIT",
            "sl": 4002.94,
            "tp": 3913.14,
        }
        mock_state = MagicMock()
        mock_state.is_safe_mode = False
        mock_state.is_circuit_broken = False
        mock_state.is_drawdown_limit_reached = False
        mock_state.account.equity = 10000.0

        engine = ExecutionEngine(mt5_client=mock_mt5, state_manager=mock_state)

        st = StructureContext(bias="BEARISH")
        vol = VolatilityContext(atr=15.0, current_spread_pips=1.5)
        mom = MomentumContext(trend_score=-40.0, rsi=35.0, adx=30.0)
        sess = SessionContext()
        ctx = MarketContext(
            timestamp=datetime.now(timezone.utc),
            symbol="XAUUSD",
            current_price=3940.0,
            bid=3939.9,
            ask=3940.1,
            structure=st,
            liquidity=LiquidityContext(),
            volatility=vol,
            momentum=mom,
            session=sess,
            mtf_alignment={"D1": "BEARISH", "H4": "BEARISH", "H1": "BEARISH"}
        )
        mock_state.get_market_context.return_value = ctx

        gate = TradeQualityGateResult(passed=True, checks={})
        regime = RegimeOutput(primary_regime=MarketRegime.TREND_BEAR, probabilities={"TREND_BEAR": 0.85}, confidence=0.85)
        decision = DecisionObject(
            symbol="XAUUSD",
            timestamp=datetime.now(timezone.utc),
            regime=regime,
            bias="SELL",
            probabilities={"SELL": 0.85},
            strategy="TREND_PULLBACK",
            entry_price=3984.94,
            stop_loss=4002.94,
            take_profit=3913.14,
            risk_reward_ratio=3.99,
            calculated_risk_percent=0.5,
            expected_value=2.1,
            model_confidence=0.6,
            adversarial_penalty=0.0,
            invalidation_levels=[],
            bull_case=[],
            bear_case=[],
            risk_factors=[],
            quality_gate=gate,
            decision="EXECUTE",
            execution_authorized=True,
            order_type="LIMIT",
            ai_score=92.7,
            context=ctx,
        )

        with patch("jarvis.data.database.TRADE_DB.log_trade") as mock_log:
            res = engine.execute_decision(decision, lots=0.01)
            self.assertEqual(res.get("status"), "PLACED")
            # The pending row is what `sync_mt5_history` later closes or cancels.
            mock_log.assert_called_once()
            self.assertIsNone(mock_log.call_args.kwargs.get("position_id"))

    def test_market_fill_does_log_to_executed_trades(self):
        """Verify that a filled market order (status=FILLED) DOES log to executed_trades."""
        mock_mt5 = MagicMock()
        mock_mt5.is_connected = True
        mock_mt5.send_market_order.return_value = {
            "status": "FILLED",
            "ticket": 999123457,
            "price": 3940.00,
            "position_id": 999123457,
            "sl": 3955.00,
            "tp": 3910.00,
        }
        mock_state = MagicMock()
        mock_state.is_safe_mode = False
        mock_state.is_circuit_broken = False
        mock_state.is_drawdown_limit_reached = False
        mock_state.account.equity = 10000.0

        engine = ExecutionEngine(mt5_client=mock_mt5, state_manager=mock_state)

        st = StructureContext(bias="BEARISH")
        vol = VolatilityContext(atr=15.0, current_spread_pips=1.5)
        mom = MomentumContext(trend_score=-40.0, rsi=35.0, adx=30.0)
        sess = SessionContext()
        ctx = MarketContext(
            timestamp=datetime.now(timezone.utc),
            symbol="XAUUSD",
            current_price=3940.0,
            bid=3939.9,
            ask=3940.1,
            structure=st,
            liquidity=LiquidityContext(),
            volatility=vol,
            momentum=mom,
            session=sess,
            mtf_alignment={"D1": "BEARISH", "H4": "BEARISH", "H1": "BEARISH"}
        )
        mock_state.get_market_context.return_value = ctx

        gate = TradeQualityGateResult(passed=True, checks={})
        regime = RegimeOutput(primary_regime=MarketRegime.TREND_BEAR, probabilities={"TREND_BEAR": 0.85}, confidence=0.85)
        decision = DecisionObject(
            symbol="XAUUSD",
            timestamp=datetime.now(timezone.utc),
            regime=regime,
            bias="SELL",
            probabilities={"SELL": 0.85},
            strategy="BREAKDOWN",
            entry_price=3940.00,
            stop_loss=3955.00,
            take_profit=3910.00,
            risk_reward_ratio=2.0,
            calculated_risk_percent=0.5,
            expected_value=1.5,
            model_confidence=0.7,
            adversarial_penalty=0.0,
            invalidation_levels=[],
            bull_case=[],
            bear_case=[],
            risk_factors=[],
            quality_gate=gate,
            decision="EXECUTE",
            execution_authorized=True,
            order_type="MARKET",
            ai_score=85.0,
            context=ctx,
        )

        with patch("jarvis.data.database.TRADE_DB.log_trade") as mock_log:
            res = engine.execute_decision(decision, lots=0.01)
            self.assertEqual(res.get("status"), "FILLED")
            mock_log.assert_called_once()
            self.assertEqual(mock_log.call_args[1]["ticket"], 999123457)

    def test_oversold_sell_exhaustion_filter(self):
        """Verify that RSI < 25.0 triggers the anti-exhaustion gate for SELL entries."""
        from jarvis.intelligence.decision_engine import DecisionEngine
        de = DecisionEngine()

        st = StructureContext(bias="BEARISH", choch_type="BEARISH", discount_premium_zone="DISCOUNT")
        liq = LiquidityContext()
        vol = VolatilityContext(atr=15.0, current_spread_pips=1.5)
        mom = MomentumContext(trend_score=-40.0, rsi=22.5, adx=30.0)
        sess = SessionContext()
        ctx = MarketContext(
            timestamp=datetime.now(timezone.utc),
            symbol="XAUUSD",
            current_price=3940.0,
            bid=3939.9,
            ask=3940.1,
            structure=st,
            liquidity=liq,
            volatility=vol,
            momentum=mom,
            session=sess
        )
        regime = RegimeOutput(primary_regime=MarketRegime.TREND_BEAR, probabilities={"TREND_BEAR": 0.85}, confidence=0.85)
        devil = DevilAdvocateReport(symbol="XAUUSD", counter_bias="BULLISH", penalty_score=20.0, invalidation_risk_coefficient=1.0)

        decision = de.evaluate(ctx, regime, analyst_reports={}, devil_report=devil, account_balance=10000.0, risk_per_trade_pct=0.5)
        self.assertFalse(decision.quality_gate.passed)
        failing = decision.quality_gate.failing_reasons
        self.assertTrue(
            any("Exhausted" in r or "Discount" in r for r in failing),
            f"Expected exhaustion or discount failure in {failing}"
        )


if __name__ == "__main__":
    unittest.main()
