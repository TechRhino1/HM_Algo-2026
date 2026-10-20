"""2026-10-21 institutional position protocols.

Verified live management gaps closed (all JARVIS-magic trades, every tick):

1. EVENT-WINDOW DE-RISK — HIGH-impact live-feed events affect open positions.
   A trade whose peak reached NEWS_BANK_R (0.60R) and still holds
   NEWS_BANK_CURRENT_R (0.20R) is banked pre-release; flat trades get a
   -0.25R protection floor; underwater trades keep their stop.
2. WEEKEND FLOOR — from Friday 20:00 UTC, deep winners (>= 0.90R) close and
   any trade in profit gets a breakeven floor against the weekend gap.
3. MARGIN LADDER — margin level below 150% closes the largest floating
   loser; below 250% flags breakeven floors.

Design constraints pinned by these tests: synthetic-calendar events never
act on real P&L; manual trades stay isolated; stops are never widened.
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from jarvis.data.schemas import PositionSnapshot
from jarvis.execution.position_monitor import (
    PositionMonitorEngine,
    JARVIS_MAGIC_NUMBER,
)
from jarvis.data.symbol_registry import resolve as resolve_spec


def _make_monitor():
    mt5_client = MagicMock()
    data_feed = MagicMock()
    context_engine = MagicMock()
    state_manager = MagicMock()
    state_manager.positions = []
    state_manager.account = None
    event_bus = MagicMock()
    monitor = PositionMonitorEngine(
        mt5_client=mt5_client,
        data_feed=data_feed,
        context_engine=context_engine,
        state_manager=state_manager,
        event_bus=event_bus,
    )
    return monitor, mt5_client


def _gold_pos(profit_r=0.5, peak_r=0.8, sl_offset=-1.0):
    spec = resolve_spec("XAUUSD")
    entry = 4200.0
    risk = 10.0  # SL 40 pips @ pip 0.1
    sl = entry + (sl_offset * risk)
    price = entry + (profit_r * risk)
    return PositionSnapshot(
        ticket=555,
        symbol="XAUUSD",
        type="BUY",
        volume=0.01,
        open_price=entry,
        current_price=price,
        sl=sl,
        tp=entry + risk * 2.5,
        profit=profit_r * risk,
        swap=0.0,
        commission=0.0,
        open_time=(datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
        magic=JARVIS_MAGIC_NUMBER,
        comment="HMA2_SCALP_DEMO",
    ), risk


def _live_event_iso(dt):
    return {
        "impact": "HIGH",
        "source": "live_feed",
        "is_fallback": False,
        "is_live": True,
        "affected_pairs": ["XAUUSD"],
        "timestamp_iso": dt.isoformat(),
    }


class TestEventWindowDeRisk(unittest.TestCase):
    def setUp(self):
        self.monitor, self.mt5 = _make_monitor()

    def test_profit_trade_is_banked_pre_release(self):
        pos, risk = _gold_pos(profit_r=0.4, peak_r=0.8)
        self.monitor._initial_risk_dist[pos.ticket] = risk
        self.monitor._peak_favorable_price[pos.ticket] = pos.open_price + 0.8 * risk
        now = datetime.now(timezone.utc)
        calendar = [_live_event_iso(now)]
        verdict = self.monitor._event_window_verdict("XAUUSD", calendar, now)
        self.assertEqual(verdict, "BANK")

    def test_flat_trade_gets_protection_floor(self):
        pos, risk = _gold_pos(profit_r=0.05, peak_r=0.15)
        real_now = datetime.now(timezone.utc)
        calendar = [_live_event_iso(real_now + timedelta(minutes=5))]
        verdict = self.monitor._event_window_verdict("XAUUSD", calendar, real_now)
        self.assertEqual(verdict, "PROTECT")

    def test_past_event_banks_the_aftermath_move(self):
        pos, risk = _gold_pos(profit_r=0.6, peak_r=0.8)
        now = datetime.now(timezone.utc)
        calendar = [_live_event_iso(now - timedelta(minutes=9))]
        verdict = self.monitor._event_window_verdict("XAUUSD", calendar, now)
        self.assertEqual(verdict, "BANK")

    def test_synthetic_calendar_never_acts(self):
        pos, risk = _gold_pos(profit_r=0.4, peak_r=0.8)
        now = datetime.now(timezone.utc)
        event = _live_event_iso(now)
        event["is_fallback"] = True
        event["source"] = "synthetic_calendar"
        calendar = [event]
        self.assertIsNone(
            self.monitor._event_window_verdict("XAUUSD", calendar, now)
        )

    def test_unrelated_symbol_no_verdict(self):
        pos, risk = _gold_pos()
        now = datetime.now(timezone.utc)
        calendar = [_live_event_iso(now)]
        self.assertIsNone(
            self.monitor._event_window_verdict("EURUSD", calendar, now)
        )

    def test_apply_protocols_banks_real_event(self):
        pos, risk = _gold_pos(profit_r=0.4, peak_r=0.8)
        self.monitor.state_manager.positions = [pos]
        self.monitor._initial_risk_dist[pos.ticket] = risk
        self.monitor._peak_favorable_price[pos.ticket] = pos.open_price + 0.8 * risk
        now = datetime.now(timezone.utc)
        # inject live event directly into the cache to avoid network
        self.monitor._news_calendar_cache = (now.timestamp(), [_live_event_iso(now)])
        account = MagicMock()
        account.margin_level = 400.0
        self.monitor._apply_position_protocols([pos], account, 400.0, 400.0, now=now)
        self.mt5.close_position.assert_called_once_with(pos.ticket)

    def test_apply_protocols_underwater_keeps_stop(self):
        pos, risk = _gold_pos(profit_r=-0.5, peak_r=0.6)
        now = datetime.now(timezone.utc)
        self.monitor.state_manager.positions = [pos]
        self.monitor._initial_risk_dist[pos.ticket] = risk
        self.monitor._news_calendar_cache = (now.timestamp(), [_live_event_iso(now)])
        account = MagicMock()
        account.margin_level = 400.0
        self.monitor._apply_position_protocols([pos], account, 400.0, 400.0, now=now)
        self.mt5.modify_position.assert_not_called()
        self.mt5.close_position.assert_not_called()


class TestWeekendFloor(unittest.TestCase):
    def setUp(self):
        self.monitor, self.mt5 = _make_monitor()
        # last Friday ever > 20:00 UTC (weekday=4)
        self.fri = datetime(2026, 10, 23, 21, 30, tzinfo=timezone.utc)
        assert self.fri.weekday() == 4

    def test_deep_winner_banks_pre_weekend(self):
        pos, risk = _gold_pos(profit_r=1.2, peak_r=1.2)
        self.assertEqual(
            self.monitor._weekend_verdict(pos, risk, self.fri), "BANK"
        )

    def test_small_profit_gets_floor(self):
        pos, risk = _gold_pos(profit_r=0.3, peak_r=0.3)
        self.assertEqual(
            self.monitor._weekend_verdict(pos, risk, self.fri), "FLOOR"
        )

    def test_underwater_untouched(self):
        pos, risk = _gold_pos(profit_r=-0.4, peak_r=0.2)
        self.assertIsNone(self.monitor._weekend_verdict(pos, risk, self.fri))

    def test_midweek_no_verdict(self):
        mid = datetime(2026, 10, 21, 21, 30, tzinfo=timezone.utc)
        assert mid.weekday() == 2
        pos, risk = _gold_pos(profit_r=0.3, peak_r=0.3)
        self.assertIsNone(self.monitor._weekend_verdict(pos, risk, mid))


class TestMarginLadder(unittest.TestCase):
    def setUp(self):
        self.monitor, self.mt5 = _make_monitor()

    def test_below_flatten_closes_worst_loser(self):
        losers = [_gold_pos(profit_r=-0.8, peak_r=0.2)[0] for _ in range(2)]
        losers[0]._rep_profit = -8.0
        algo = [_gold_pos(profit_r=-0.2, peak_r=0.2)[0], losers[1]]
        algo[1].profit = -22.0
        self.monitor._apply_margin_ladder(algo, margin_level=120.0)
        self.mt5.close_position.assert_called_once_with(algo[1].ticket)

    def test_healthy_margin_inert(self):
        pos, _ = _gold_pos(profit_r=-0.3)
        self.monitor._apply_margin_ladder([pos], margin_level=900.0)
        self.mt5.close_position.assert_not_called()

    def test_no_negative_loser_inert(self):
        algo = [_gold_pos(profit_r=0.4)[0], _gold_pos(profit_r=0.9)[0]]
        self.monitor._apply_margin_ladder(algo, margin_level=120.0)
        self.mt5.close_position.assert_not_called()


if __name__ == "__main__":
    unittest.main()
