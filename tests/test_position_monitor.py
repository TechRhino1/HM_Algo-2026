import unittest
from unittest.mock import MagicMock
from datetime import datetime
from jarvis.data.schemas import PositionSnapshot
from jarvis.execution.position_monitor import PositionMonitorEngine, JARVIS_MAGIC_NUMBER

class TestPositionMonitorManualClassification(unittest.TestCase):
    def setUp(self):
        self.mt5_client = MagicMock()
        self.data_feed = MagicMock()
        self.context_engine = MagicMock()
        self.state_manager = MagicMock()
        self.event_bus = MagicMock()

        self.monitor = PositionMonitorEngine(
            mt5_client=self.mt5_client,
            data_feed=self.data_feed,
            context_engine=self.context_engine,
            state_manager=self.state_manager,
            event_bus=self.event_bus,
        )

    def test_jarvis_ai_trade_not_classified_as_manual(self):
        pos = PositionSnapshot(
            ticket=101,
            symbol="XAUUSD",
            type="BUY",
            volume=0.02,
            open_price=2400.0,
            current_price=2405.0,
            sl=2390.0,
            tp=2420.0,
            profit=10.0,
            swap=0.0,
            commission=0.0,
            open_time=datetime.now(),
            magic=JARVIS_MAGIC_NUMBER,
            comment=""
        )
        self.assertFalse(self.monitor._is_manual_trade(pos))

    def test_manual_trade_with_comment(self):
        pos = PositionSnapshot(
            ticket=102,
            symbol="XAUUSD",
            type="BUY",
            volume=0.02,
            open_price=2400.0,
            current_price=2405.0,
            sl=2390.0,
            tp=2420.0,
            profit=10.0,
            swap=0.0,
            commission=0.0,
            open_time=datetime.now(),
            magic=JARVIS_MAGIC_NUMBER,
            comment="Manual trade from mobile"
        )
        self.assertTrue(self.monitor._is_manual_trade(pos))

    def test_manual_trade_with_desk_comment(self):
        pos = PositionSnapshot(
            ticket=103,
            symbol="EURUSD",
            type="SELL",
            volume=0.05,
            open_price=1.0850,
            current_price=1.0840,
            sl=1.0880,
            tp=1.0800,
            profit=5.0,
            swap=0.0,
            commission=0.0,
            open_time=datetime.now(),
            magic=JARVIS_MAGIC_NUMBER,
            comment="DESK order"
        )
        self.assertTrue(self.monitor._is_manual_trade(pos))

    def test_manual_trade_wrong_magic(self):
        pos = PositionSnapshot(
            ticket=104,
            symbol="XAUUSD",
            type="BUY",
            volume=0.01,
            open_price=2400.0,
            current_price=2405.0,
            sl=2390.0,
            tp=2420.0,
            profit=10.0,
            swap=0.0,
            commission=0.0,
            open_time=datetime.now(),
            magic=0,
            comment=""
        )
        self.assertTrue(self.monitor._is_manual_trade(pos))

    def test_autonomous_dynamic_trailing_stages_buy(self):
        """Canonical BUY ratchet: partial at 0.75R, breakeven deferred to 2R,
        milestone at 2R, ATR trail beyond 2R, and monotonic (never loosens).

        Replaces the previous Stage 0/1/2 assertions, which pinned a DEFECT: a
        "zero-risk lock" that moved the stop to only +0.10R at +0.80R favourable
        movement. That premature lock is precisely what closed +20R winners near
        breakeven and produced the negative net profit. The stages no longer
        exist; jarvis.execution.exit_policy owns all stop arithmetic.
        """
        from jarvis.data.schemas import (
            MarketContext, StructureContext, LiquidityContext,
            VolatilityContext, MomentumContext, SessionContext
        )
        from datetime import timezone
        import time

        ctx = MarketContext(
            symbol="XAUUSD",
            timestamp=datetime.now(timezone.utc),
            current_price=2408.0,
            bid=2407.8,
            ask=2408.2,
            structure=StructureContext(bias="BULLISH"),
            liquidity=LiquidityContext(),
            volatility=VolatilityContext(atr=10.0, current_spread_pips=2.0),
            momentum=MomentumContext(trend_score=50.0, adx=25.0),
            session=SessionContext(is_prime_session=True)
        )
        self.monitor._ctx_cache["XAUUSD"] = (ctx, time.monotonic())

        # Entry 2400, initial SL 2390 -> 1R = 10.0 price units.
        pos = PositionSnapshot(
            ticket=201, symbol="XAUUSD", type="BUY", volume=1.00,
            open_price=2400.0, current_price=2408.0, sl=2390.0, tp=2440.0,
            profit=8.0, swap=0.0, commission=0.0,
            open_time=datetime.now(timezone.utc).isoformat(), magic=JARVIS_MAGIC_NUMBER
        )
        self.mt5_client.modify_position.return_value = {"status": "MODIFIED"}
        self.monitor._manage_single_position(pos)

        # 1. At +0.80R the stop must NOT have been tightened to a paltry +0.10R.
        #    The policy defers breakeven to +2R, so the stop stays at 2390.
        for call_args in self.mt5_client.modify_position.call_args_list:
            self.assertLessEqual(
                call_args.kwargs.get("sl", 0.0), 2390.0 + 1e-9,
                "stop must not ratchet before the 2R breakeven trigger",
            )

        # 2. +2.5R: breakeven must now be locked at or above entry (zero risk).
        ctx.current_price = 2425.0
        pos.current_price = 2425.0
        self.monitor._ctx_cache["XAUUSD"] = (ctx, time.monotonic())
        self.monitor._manage_single_position(pos)
        applied = [
            c.kwargs.get("sl") for c in self.mt5_client.modify_position.call_args_list
            if c.kwargs.get("sl") is not None
        ]
        self.assertTrue(applied, "a stop modification must occur at +2.5R")
        self.assertGreaterEqual(
            max(applied), 2400.0,
            "breakeven/profit lock must put the stop at or above entry",
        )

        # 3. Monotonic ratchet: a retracement must never loosen the stop.
        pos.sl = max(applied)
        ctx.current_price = 2405.0
        pos.current_price = 2405.0
        self.monitor._ctx_cache["XAUUSD"] = (ctx, time.monotonic())
        self.mt5_client.modify_position.reset_mock()
        self.monitor._manage_single_position(pos)
        for call_args in self.mt5_client.modify_position.call_args_list:
            self.assertGreaterEqual(
                call_args.kwargs.get("sl", 0.0), pos.sl - 1e-9,
                "ratchet must be one-way; the stop must never move backwards",
            )

    def test_autonomous_dynamic_trailing_stages_sell(self):
        """Canonical SELL ratchet: breakeven deferred to 2R, stop locks ABOVE
        entry as profit accrues, and never loosens. Mirror of the BUY case.
        """
        from jarvis.data.schemas import (
            MarketContext, StructureContext, LiquidityContext,
            VolatilityContext, MomentumContext, SessionContext
        )
        from datetime import timezone
        import time

        ctx = MarketContext(
            symbol="EURUSD",
            timestamp=datetime.now(timezone.utc),
            current_price=1.0960,
            bid=1.0959,
            ask=1.0961,
            structure=StructureContext(bias="BEARISH"),
            liquidity=LiquidityContext(),
            volatility=VolatilityContext(atr=0.0100, current_spread_pips=1.0),
            momentum=MomentumContext(trend_score=-50.0, adx=25.0),
            session=SessionContext(is_prime_session=True)
        )
        self.monitor._ctx_cache["EURUSD"] = (ctx, time.monotonic())

        # Entry 1.1000, initial SL 1.1050 -> 1R = 0.0050.
        pos = PositionSnapshot(
            ticket=301, symbol="EURUSD", type="SELL", volume=1.00,
            open_price=1.1000, current_price=1.0960, sl=1.1050, tp=1.0800,
            profit=40.0, swap=0.0, commission=0.0,
            open_time=datetime.now(timezone.utc).isoformat(), magic=JARVIS_MAGIC_NUMBER
        )
        self.mt5_client.modify_position.return_value = {"status": "MODIFIED"}
        self.monitor._manage_single_position(pos)

        # 1. +0.80R must NOT tighten the stop below entry yet.
        for call_args in self.mt5_client.modify_position.call_args_list:
            sl = call_args.kwargs.get("sl")
            if sl:
                self.assertGreaterEqual(
                    sl, 1.1000 - 1e-9,
                    "stop must not be ratcheted above entry before +2R",
                )

        # 2. +2.5R (price 1.0875): stop locks at/below entry, i.e. zero risk.
        ctx.current_price = 1.0875
        pos.current_price = 1.0875
        self.monitor._ctx_cache["EURUSD"] = (ctx, time.monotonic())
        self.monitor._manage_single_position(pos)
        applied = [
            c.kwargs.get("sl") for c in self.mt5_client.modify_position.call_args_list
            if c.kwargs.get("sl") is not None
        ]
        self.assertTrue(applied, "a stop modification must occur at +2.5R")
        self.assertLessEqual(
            min(applied), 1.1000 + 1e-9,
            "breakeven/profit lock must put the SELL stop at or below entry",
        )

        # 3. Monotonic: a retracement must not loosen the stop.
        pos.sl = min(applied)
        ctx.current_price = 1.0980
        pos.current_price = 1.0980
        self.monitor._ctx_cache["EURUSD"] = (ctx, time.monotonic())
        self.mt5_client.modify_position.reset_mock()
        self.monitor._manage_single_position(pos)
        for call_args in self.mt5_client.modify_position.call_args_list:
            self.assertLessEqual(
                call_args.kwargs.get("sl", 1e9), pos.sl + 1e-9,
                "ratchet must be one-way for SELL too",
            )

    def test_dynamic_ml_tp_extension_on_high_confidence(self):
        """Verify TP is dynamically extended when ML confidence is high (>=0.68) and trend is strong."""
        from jarvis.data.schemas import (
            MarketContext, StructureContext, LiquidityContext,
            VolatilityContext, MomentumContext, SessionContext
        )
        from datetime import timezone
        import time

        ctx = MarketContext(
            symbol="EURUSD",
            timestamp=datetime.now(timezone.utc),
            current_price=1.1070,  # in profit (+1.4R)
            bid=1.1069,
            ask=1.1071,
            structure=StructureContext(bias="BULLISH"),
            liquidity=LiquidityContext(),
            volatility=VolatilityContext(atr=0.0050, current_spread_pips=1.0),
            momentum=MomentumContext(trend_score=60.0, adx=30.0),
            session=SessionContext(is_prime_session=True)
        )
        self.monitor._ctx_cache["EURUSD"] = (ctx, time.monotonic())
        self.monitor._get_position_duration_sec = MagicMock(return_value=300.0)  # outside grace period

        # Mock ML predictor returning high confidence 0.75
        mock_ml = MagicMock()
        mock_ml.extract_features.return_value = [0.0] * 24
        mock_ml.predict_probability.return_value = 0.75
        self.monitor.ml_predictor = mock_ml

        # Entry 1.1000, SL 1.0950 (1R = 0.0050), initial TP 1.1100 (2R)
        pos = PositionSnapshot(
            ticket=401, symbol="EURUSD", type="BUY", volume=1.00,
            open_price=1.1000, current_price=1.1070, sl=1.0950, tp=1.1100,
            profit=70.0, swap=0.0, commission=0.0,
            open_time=datetime.now(timezone.utc).isoformat(), magic=JARVIS_MAGIC_NUMBER
        )
        self.mt5_client.modify_position.return_value = {"status": "MODIFIED"}
        self.monitor._manage_single_position(pos)

        applied_tp = [
            c.kwargs.get("tp") for c in self.mt5_client.modify_position.call_args_list
            if c.kwargs.get("tp") is not None
        ]
        self.assertTrue(applied_tp, "TP modification must occur on high ML confidence")
        # Extended TP should be > 1.1100 (initial TP was 1.1100)
        self.assertGreater(max(applied_tp), 1.1100)

    def test_dynamic_ml_tp_contraction_on_low_confidence(self):
        """Verify TP is dynamically contracted closer to price when ML confidence drops (<=0.45)."""
        from jarvis.data.schemas import (
            MarketContext, StructureContext, LiquidityContext,
            VolatilityContext, MomentumContext, SessionContext
        )
        from datetime import timezone
        import time

        ctx = MarketContext(
            symbol="EURUSD",
            timestamp=datetime.now(timezone.utc),
            current_price=1.1055,  # in profit (+1.1R)
            bid=1.1054,
            ask=1.1056,
            structure=StructureContext(bias="BULLISH"),
            liquidity=LiquidityContext(),
            volatility=VolatilityContext(atr=0.0050, current_spread_pips=1.0),
            momentum=MomentumContext(trend_score=10.0, adx=15.0, divergence="BEARISH_DIVERGENCE"),
            session=SessionContext(is_prime_session=True)
        )
        self.monitor._ctx_cache["EURUSD"] = (ctx, time.monotonic())
        self.monitor._get_position_duration_sec = MagicMock(return_value=300.0)

        # Mock ML predictor returning low confidence 0.38
        mock_ml = MagicMock()
        mock_ml.extract_features.return_value = [0.0] * 24
        mock_ml.predict_probability.return_value = 0.38
        self.monitor.ml_predictor = mock_ml

        # Entry 1.1000, initial SL 1.0950, initial TP 1.1150 (3R)
        pos = PositionSnapshot(
            ticket=402, symbol="EURUSD", type="BUY", volume=1.00,
            open_price=1.1000, current_price=1.1055, sl=1.0950, tp=1.1150,
            profit=55.0, swap=0.0, commission=0.0,
            open_time=datetime.now(timezone.utc).isoformat(), magic=JARVIS_MAGIC_NUMBER
        )
        self.mt5_client.modify_position.return_value = {"status": "MODIFIED"}
        self.monitor._manage_single_position(pos)

        applied_tp = [
            c.kwargs.get("tp") for c in self.mt5_client.modify_position.call_args_list
            if c.kwargs.get("tp") is not None
        ]
        self.assertTrue(applied_tp, "TP modification must occur on deteriorating ML confidence")
        # Contracted TP should be < initial TP (1.1150) and > current price (1.1055)
        self.assertLess(min(applied_tp), 1.1150)
        self.assertGreater(min(applied_tp), 1.1055)

    def test_sl_never_widens_to_save_losing_trade(self):
        """Invariant: SL must NEVER widen or move backwards to prevent a loss."""
        from jarvis.data.schemas import (
            MarketContext, StructureContext, LiquidityContext,
            VolatilityContext, MomentumContext, SessionContext
        )
        from datetime import timezone
        import time

        ctx = MarketContext(
            symbol="EURUSD",
            timestamp=datetime.now(timezone.utc),
            current_price=1.0970,  # underwater (-0.6R)
            bid=1.0969,
            ask=1.0971,
            structure=StructureContext(bias="BULLISH"),
            liquidity=LiquidityContext(),
            volatility=VolatilityContext(atr=0.0050, current_spread_pips=1.0),
            momentum=MomentumContext(trend_score=-10.0, adx=15.0),
            session=SessionContext(is_prime_session=True)
        )
        self.monitor._ctx_cache["EURUSD"] = (ctx, time.monotonic())
        self.monitor._get_position_duration_sec = MagicMock(return_value=300.0)

        # Existing SL is 1.0950
        pos = PositionSnapshot(
            ticket=403, symbol="EURUSD", type="BUY", volume=1.00,
            open_price=1.1000, current_price=1.0970, sl=1.0950, tp=1.1100,
            profit=-30.0, swap=0.0, commission=0.0,
            open_time=datetime.now(timezone.utc).isoformat(), magic=JARVIS_MAGIC_NUMBER
        )
        self.mt5_client.modify_position.reset_mock()
        self.monitor._manage_single_position(pos)

        for c in self.mt5_client.modify_position.call_args_list:
            sl = c.kwargs.get("sl")
            if sl is not None:
                self.assertGreaterEqual(sl, 1.0950 - 1e-9, "SL must NEVER be widened below current SL!")


if __name__ == "__main__":
    unittest.main()

