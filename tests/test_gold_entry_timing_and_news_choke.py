"""
Unit and integration tests for Gold entry timing, late entry prevention,
and news window protection stop-loss fixes.
"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from jarvis.data.schemas import (
    PositionSnapshot, MarketContext, StructureContext, LiquidityContext,
    VolatilityContext, MomentumContext, SessionContext, RegimeOutput, MarketRegime,
    DevilAdvocateReport, AnalystReport, AnalystRole
)
from jarvis.execution.position_monitor import PositionMonitorEngine
from jarvis.execution.execution_engine import ExecutionEngine
from jarvis.intelligence.decision_engine import DecisionEngine


class TestGoldEntryTimingAndNewsChoke(unittest.TestCase):

    def test_news_floor_sell_places_sl_above_entry_and_exempts_fresh_trade(self):
        """Verifies SELL position news floor stays above entry and young trades are not choked."""
        mt5_client = MagicMock()
        data_feed = MagicMock()
        context_engine = MagicMock()
        state_manager = MagicMock()
        event_bus = MagicMock()

        monitor = PositionMonitorEngine(
            mt5_client=mt5_client,
            data_feed=data_feed,
            context_engine=context_engine,
            state_manager=state_manager,
            event_bus=event_bus,
        )

        now = datetime.now(timezone.utc)
        # Fresh trade: opened 10 seconds ago
        fresh_pos = PositionSnapshot(
            ticket=9991,
            symbol="XAUUSD",
            type="SELL",
            volume=0.01,
            open_price=4042.89,
            current_price=4043.10,
            sl=4055.09,
            tp=3994.30,
            profit=-0.21,
            swap=0.0,
            commission=0.0,
            open_time=(now - timedelta(seconds=10)).isoformat(),
            magic=111,
            comment="FRESH_TRADE"
        )
        monitor.state_manager.positions = [fresh_pos]
        monitor._initial_risk_dist[fresh_pos.ticket] = 12.20
        monitor._peak_favorable_price[fresh_pos.ticket] = 4042.89

        # Inject active live event
        live_event = {
            "impact": "HIGH",
            "source": "live_feed",
            "is_fallback": False,
            "timestamp_iso": now.isoformat(),
            "affected_pairs": ["XAUUSD"]
        }
        monitor._news_calendar_cache = (now.timestamp(), [live_event])

        account = MagicMock()
        account.margin_level = 500.0

        # Run protocols on fresh trade
        monitor._apply_position_protocols([fresh_pos], account, 500.0, 500.0, now=now)
        # MUST NOT MODIFY SL of fresh trade
        mt5_client.modify_position.assert_not_called()

        # Now test mature trade (opened 30 minutes ago)
        mature_pos = PositionSnapshot(
            ticket=9992,
            symbol="XAUUSD",
            type="SELL",
            volume=0.01,
            open_price=4042.89,
            current_price=4040.00, # In profit by 2.89 pts (~0.24R)
            sl=4055.09,
            tp=3994.30,
            profit=2.89,
            swap=0.0,
            commission=0.0,
            open_time=(now - timedelta(minutes=30)).isoformat(),
            magic=111,
            comment="MATURE_TRADE"
        )
        monitor.state_manager.positions = [mature_pos]
        monitor._initial_risk_dist[mature_pos.ticket] = 12.20
        monitor._peak_favorable_price[mature_pos.ticket] = 4039.50

        monitor._apply_position_protocols([mature_pos], account, 500.0, 500.0, now=now)
        if mt5_client.modify_position.called:
            call_args = mt5_client.modify_position.call_args[1]
            new_sl = call_args.get("sl")
            # Invariant: SELL SL MUST be strictly above open_price
            self.assertGreater(new_sl, mature_pos.open_price, "SELL SL floor must be ABOVE open price!")

    def test_adversarial_threat_runway_gate_blocks_collision(self):
        """Verifies that an adversarial obstacle within minimum R:R blocks entry."""
        engine = DecisionEngine()
        context = MarketContext(
            symbol="XAUUSD",
            timestamp=datetime.now(timezone.utc),
            current_price=4042.89,
            ask=4043.10,
            bid=4042.89,
            structure=StructureContext(bias="BEARISH", discount_premium_zone="EQUILIBRIUM"),
            liquidity=LiquidityContext(),
            volatility=VolatilityContext(atr=3.5, current_spread_pips=2.0),
            momentum=MomentumContext(rsi=45.0, trend_score=-40.0),
            session=SessionContext(current_session="NEW_YORK", is_prime_session=True)
        )
        regime = RegimeOutput(
            primary_regime=MarketRegime.TREND_BEAR,
            confidence=0.80,
            probabilities={MarketRegime.TREND_BEAR.value: 0.80}
        )
        analyst_reports = {
            "STRUCTURE": AnalystReport(role=AnalystRole.STRUCTURE, symbol="XAUUSD", score=85.0, confidence=0.85, bias="SELL", evidence=["Bearish structure"]),
        }
        # Devil reports obstacle at 4042.17 (only 0.72 pts / ~0.07R away from entry!)
        devil_report = DevilAdvocateReport(
            symbol="XAUUSD",
            counter_bias="BUY",
            penalty_score=20.0,
            invalidation_risk_coefficient=1.0,
            threats_detected=["Immediate dynamic support at 4042.17 (0.07R runway)"],
            threat_price_level=4042.17
        )

        decision = engine.evaluate(
            context=context,
            regime=regime,
            analyst_reports=analyst_reports,
            devil_report=devil_report,
            trade_style="DAY_TRADING"
        )

        # Decision MUST be blocked due to lack of runway to obstacle
        self.assertFalse(decision.execution_authorized, "Trade with 0.07R obstacle must not be authorized!")
        self.assertEqual(decision.decision, "NO_TRADE")
        self.assertIn("Trade blocked to avoid collision", " ".join(decision.rejection_reasons))

    def test_limit_order_runaway_drift_rejected_no_chase(self):
        """Verifies ExecutionEngine rejects drifted limit orders instead of chasing at market."""
        mt5_client = MagicMock()
        exec_engine = ExecutionEngine(mt5_client=mt5_client)

        decision = MagicMock()
        decision.symbol = "XAUUSD"
        decision.bias = "SELL"
        decision.order_type = "LIMIT"
        decision.execution_authorized = True
        # Breached sell limit at 4040.00 (below market ask 4043.10)
        decision.entry_price = 4040.00
        decision.stop_loss = 4055.00 # risk = 15.0 pts
        decision.take_profit = 4010.00 # reward = 30.0 pts (R:R = 2.0)
        decision.risk_reward_ratio = 2.0
        
        ctx = MagicMock()
        ctx.current_price = 4042.89 # Market has fallen to 4042.89 (drift = 3.10 pts)
        ctx.bid = 4042.89
        ctx.ask = 4043.10
        ctx.volatility.atr = 3.50 # 0.20 ATR is 0.70 pts; 3.10 pts drift far exceeds 0.70
        decision.context = ctx

        sym_info = {
            "trade_tick_size": 0.01,
            "digits": 2,
            "trade_stops_level": 5,
            "point": 0.01
        }
        mt5_client.get_symbol_info.return_value = sym_info
        mt5_client.get_symbol_trading_spec.return_value = sym_info
        mt5_client.get_pending_orders.return_value = []

        res = exec_engine.execute_decision(decision, lots=0.01)
        self.assertEqual(res.get("status"), "BLOCKED")
        self.assertEqual(res.get("reason"), "LIMIT_DRIFT_EXCEEDED_NO_CHASE")
        # mt5_client MUST NOT have placed a market order or pending order
        mt5_client.place_market_order.assert_not_called()
        mt5_client.place_pending_order.assert_not_called()


if __name__ == "__main__":
    unittest.main()
