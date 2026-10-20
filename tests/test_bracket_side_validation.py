"""A recovered bracket must not be stored on the wrong side of entry.

WHY THIS EXISTS
---------------
`sync_mt5_history` does not read `sl`/`tp` from the broker — it recovers them by
string-parsing the order comment (`[sl...]` / `[tp...]`). That parse had no
direction check, so a comment carrying the wrong number was written straight
into the journal. Measured on data/jarvis_history.db: **24 rows had a stop on
the WRONG side of entry** — 14 BUY with `sl >= entry`, 10 SELL with `sl <= entry`
— and the 20 closed ones among them were **20 winners totalling +37.38**, which
a genuine stop cannot produce. That is the signature of the TAKE-PROFIT level
having been parsed into the `sl` field.

This is not a duplicate of `trade_guard.validate_pre_execution`, which refuses an
inverted bracket at ORDER BUILD time. The point here is the other end: a value
that arrives through the comment parse cannot have come from a valid order, so it
must not be persisted either.

The positive control matters as much as the rejection: a guard that refuses
everything would also make this test pass while destroying the data.
"""

from __future__ import annotations

import sqlite3

import pytest

from jarvis.data.database import SQLiteTradeDB, _bracket_side_ok


class TestThePredicate:
    @pytest.mark.parametrize("action,entry,value,is_stop,expected", [
        # BUY: stop below entry, target above it.
        ("BUY", 100.0, 99.0, True, True),
        ("BUY", 100.0, 101.0, True, False),   # stop above entry -> wrong side
        ("BUY", 100.0, 101.0, False, True),   # target above entry -> right side
        ("BUY", 100.0, 99.0, False, False),   # target below entry -> wrong side
        # SELL: stop above entry, target below it.
        ("SELL", 100.0, 101.0, True, True),
        ("SELL", 100.0, 99.0, True, False),
        ("SELL", 100.0, 99.0, False, True),
        ("SELL", 100.0, 101.0, False, False),
    ])
    def test_direction_is_respected(self, action, entry, value, is_stop, expected):
        assert _bracket_side_ok(action, entry, value, is_stop=is_stop) is expected

    @pytest.mark.parametrize("value", [0.0, -1.0, -100.0])
    def test_an_absent_bracket_is_left_alone(self, value):
        """`0.0` means "no tag was parsed" — not a claim about the market."""
        assert _bracket_side_ok("BUY", 100.0, value, is_stop=True) is True
        assert _bracket_side_ok("SELL", 100.0, value, is_stop=True) is True

    def test_an_unknown_direction_is_not_trusted(self):
        assert _bracket_side_ok("", 100.0, 99.0, is_stop=True) is False
        assert _bracket_side_ok("buy", 100.0, 99.0, is_stop=True) is False


class TestTheSyncRefusesIt:
    """Drive the real `sync_mt5_history` with a comment that carries the wrong
    number, and assert it does not reach the journal."""

    def _run_sync(self, db, deals):
        from types import SimpleNamespace
        from unittest.mock import patch

        fake = SimpleNamespace(
            terminal_info=lambda: SimpleNamespace(connected=True),
            initialize=lambda **kw: True,
            history_deals_get=lambda *a, **k: list(deals),
        )
        with patch("jarvis.data.database.broker_utc_offset", return_value=0):
            db.sync_mt5_history(days=1, mt5_module=fake)

    def _deals(self, comment: str, action_type: int = 0):
        import time
        from types import SimpleNamespace

        now = int(time.time())
        entry_deal = SimpleNamespace(
            symbol="EURUSD", position_id=555222999, entry=0, type=action_type,
            price=1.1000, volume=0.10, profit=0.0, swap=0.0, commission=0.0,
            time=now - 3600, magic=888999, comment="",
        )
        exit_deal = SimpleNamespace(
            symbol="EURUSD", position_id=555222999, entry=1,
            type=1 if action_type == 0 else 0,
            price=1.1088, volume=0.10, profit=42.0, swap=0.0, commission=0.0,
            time=now - 60, magic=888999, comment=comment,
        )
        return [entry_deal, exit_deal]

    def _setup(self, tmp_path):
        db_path = str(tmp_path / "sync.db")
        db = SQLiteTradeDB(db_path=db_path)
        db.log_trade(
            ticket=555111000, position_id=555222999, symbol="EURUSD", action="BUY",
            entry=1.1000, sl=1.0950, tp=1.1100, volume=0.10,
            score=85.0, regime="TREND_BULL", ev=25.0,
        )
        db._last_mt5_sync = 0.0

        def _row():
            conn = sqlite3.connect(db_path)
            try:
                return conn.execute(
                    "SELECT sl, tp FROM executed_trades WHERE ticket = ?",
                    (555111000,),
                ).fetchone()
            finally:
                conn.close()

        return db, _row

    def test_a_stop_parsed_from_the_wrong_side_is_refused(self, tmp_path):
        """A BUY whose comment carries a stop ABOVE entry. The tag cannot have
        come from a valid order, so the stored stop must survive untouched."""
        db, _row = self._setup(tmp_path)
        try:
            # 1.1150 > entry 1.1000: wrong side for a BUY stop. This is the shape
            # of the 14 live rows -- the target parsed into the sl field.
            self._run_sync(db, self._deals("[sl1.1150]"))
            sl, tp = _row()
            assert sl == pytest.approx(1.0950), (
                f"a wrong-side stop was persisted: sl={sl} (expected the original 1.0950)"
            )
            assert tp == pytest.approx(1.1100), "tp should be untouched"
        finally:
            db.close()

    def test_a_correctly_sided_stop_is_still_recorded(self, tmp_path):
        """Positive control: the guard must not reject valid brackets, or the
        test above would pass while destroying the data."""
        db, _row = self._setup(tmp_path)
        try:
            # 1.0900 < entry 1.1000: correct side for a BUY stop.
            self._run_sync(db, self._deals("[sl1.0900]"))
            sl, _tp = _row()
            assert sl == pytest.approx(1.0900), (
                f"a valid stop was refused: sl={sl} (expected 1.0900)"
            )
        finally:
            db.close()
