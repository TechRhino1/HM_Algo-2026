"""
Unit tests for Market Direction Reversal Guard and Institutional Profit Locking.
Verifies Phase 1 (BE defense), Phase 2 (50% lock / bank profit), and Phase 3 (runner lock / bank).
"""
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock

from jarvis.data.schemas import (
    PositionSnapshot,
    MarketContext,
    StructureContext,
    MomentumContext,
    LiquidityContext,
    VolatilityContext,
    SessionContext,
)
from jarvis.execution.position_monitor import PositionMonitorEngine, JARVIS_MAGIC_NUMBER
from jarvis.execution.exit_policy import ExitPolicy


class TestMarketDirectionReversalGuard(unittest.TestCase):
    def setUp(self):
        self.mock_mt5 = MagicMock()
        self.mock_feed = MagicMock()
        self.mock_ctx = MagicMock()
        self.mock_state = MagicMock()
        self.mock_bus = MagicMock()

        self.monitor = PositionMonitorEngine(
            mt5_client=self.mock_mt5,
            data_feed=self.mock_feed,
            context_engine=self.mock_ctx,
            state_manager=self.mock_state,
            event_bus=self.mock_bus,
        )

        self.policy = ExitPolicy.for_symbol("EURUSD")

    def test_phase1_opposite_choch_locks_breakeven(self):
        """Phase 1 (+0.70R): Bearish CHoCH against BUY trade moves SL to BE + buffer."""
        pos = PositionSnapshot(
            ticket=1001,
            symbol="EURUSD",
            type="BUY",
            volume=0.10,
            open_price=1.10000,
            current_price=1.10140,  # +14 pips (0.70R on 20-pip risk)
            sl=1.09800,
            tp=1.10500,
            profit=14.0,
            swap=0.0,
            commission=0.0,
            open_time=datetime.now(timezone.utc).isoformat(),
            magic=JARVIS_MAGIC_NUMBER,
        )

        ctx = MarketContext(
            symbol="EURUSD",
            timestamp=datetime.now(timezone.utc),
            current_price=1.10140,
            bid=1.10135,
            ask=1.10145,
            structure=StructureContext(bias="BEARISH", choch=True, choch_type="BEARISH_CHOCH"),
            liquidity=LiquidityContext(),
            volatility=VolatilityContext(atr=0.0020),
            momentum=MomentumContext(trend_score=-15.0),
            session=SessionContext(is_prime_session=True),
        )

        risk_dist = 0.00200
        current_r = 0.70

        new_sl, action, should_close, reason = self.monitor._check_market_direction_reversal(
            pos=pos,
            ctx=ctx,
            c_price=1.10140,
            atr=0.0020,
            risk_dist=risk_dist,
            current_r=current_r,
            digits=5,
            current_sl=1.09800,
            policy=self.policy,
        )

        self.assertFalse(should_close)
        self.assertIsNotNone(action)
        self.assertIn("REVERSAL_DEFENSE_BE", action)
        self.assertGreater(new_sl, pos.open_price, "BE stop must cover entry price + buffer")

    def test_phase2_confluent_reversal_banks_profit(self):
        """Phase 2 (+1.30R): Confluent reversal (CHoCH + adverse momentum) triggers market exit."""
        pos = PositionSnapshot(
            ticket=1002,
            symbol="EURUSD",
            type="BUY",
            volume=0.10,
            open_price=1.10000,
            current_price=1.10260,  # +26 pips (1.30R on 20-pip risk)
            sl=1.09800,
            tp=1.10500,
            profit=26.0,
            swap=0.0,
            commission=0.0,
            open_time=datetime.now(timezone.utc).isoformat(),
            magic=JARVIS_MAGIC_NUMBER,
        )

        ctx = MarketContext(
            symbol="EURUSD",
            timestamp=datetime.now(timezone.utc),
            current_price=1.10260,
            bid=1.10255,
            ask=1.10265,
            structure=StructureContext(bias="BEARISH", choch=True, choch_type="BEARISH_CHOCH"),
            liquidity=LiquidityContext(),
            volatility=VolatilityContext(atr=0.0020),
            momentum=MomentumContext(trend_score=-35.0, divergence="BEARISH_DIVERGENCE"),
            session=SessionContext(is_prime_session=True),
        )

        risk_dist = 0.00200
        current_r = 1.30

        new_sl, action, should_close, reason = self.monitor._check_market_direction_reversal(
            pos=pos,
            ctx=ctx,
            c_price=1.10260,
            atr=0.0020,
            risk_dist=risk_dist,
            current_r=current_r,
            digits=5,
            current_sl=1.09800,
            policy=self.policy,
        )

        self.assertTrue(should_close, "Confluent reversal at +1.30R must execute market close to bank profit")
        self.assertIn("DIRECTION_REVERSAL_BANK", reason)

    def test_phase3_runner_opposite_structural_break_banks_gain(self):
        """Phase 3 (+2.20R): Opposite BOS triggers full exit to lock peak runner profit."""
        pos = PositionSnapshot(
            ticket=1003,
            symbol="XAUUSD",
            type="BUY",
            volume=0.10,
            open_price=2400.0,
            current_price=2422.0,  # +22 points (2.20R on 10-point risk)
            sl=2390.0,
            tp=2440.0,
            profit=220.0,
            swap=0.0,
            commission=0.0,
            open_time=datetime.now(timezone.utc).isoformat(),
            magic=JARVIS_MAGIC_NUMBER,
        )

        ctx = MarketContext(
            symbol="XAUUSD",
            timestamp=datetime.now(timezone.utc),
            current_price=2422.0,
            bid=2421.8,
            ask=2422.2,
            structure=StructureContext(bias="BEARISH", bos=True, bos_type="BEARISH_BOS"),
            liquidity=LiquidityContext(),
            volatility=VolatilityContext(atr=5.0),
            momentum=MomentumContext(trend_score=-40.0),
            session=SessionContext(is_prime_session=True),
        )

        risk_dist = 10.0
        current_r = 2.20

        new_sl, action, should_close, reason = self.monitor._check_market_direction_reversal(
            pos=pos,
            ctx=ctx,
            c_price=2422.0,
            atr=5.0,
            risk_dist=risk_dist,
            current_r=current_r,
            digits=2,
            current_sl=2405.0,
            policy=ExitPolicy.for_symbol("XAUUSD"),
        )

        self.assertTrue(should_close, "Opposite structural break at +2.20R must bank runner profit")
        self.assertIn("RUNNER_REVERSAL_EXIT", reason)


if __name__ == "__main__":
    unittest.main()
