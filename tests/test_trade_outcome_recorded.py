"""A closed trade must record HOW IT LEFT, not only how it entered.

`executed_trades` had a column for every part of the entry — `entry_price`, `sl`,
`tp`, `volume`, `expected_value` — and none for the outcome. So a closed row had
nowhere to put its exit price or its realised P&L, and the only place a real
figure existed was a request-time dict built from live MT5 deals in
`server.py:669`, never written back to the file. Measured consequences:

* every closed row reported `realized_pnl == expected_value` — the pre-trade
  estimate echoed back, so nothing was learned once a trade closed;
* `copilot.py` read `r.get("realized_pnl")` and silently got `0`, because the
  key was never stored;
* 42/93 rows in `data/jarvis_trade_memory.db` showed `exit_price=0, pnl=0`.

The hard part is not adding the columns, it is keeping **"unknown" distinct from
"zero"**. A `0.0` P&L is a real break-even trade; a `dict.get()` on a missing key
also yields `0.0`; and `realized_pnl` shipped with `DEFAULT 0.0`, so a row that
had never closed and a row that closed flat were the same value. NULL is the only
honest encoding of "not recorded", and these tests pin it.
"""

from __future__ import annotations

import sqlite3

import pytest

from jarvis.data.database import (
    MIGRATIONS,
    SCHEMA_VERSION,
    SQLiteTradeDB,
)
from jarvis.data.schema_version import migrate, read_version


# ── the fixture ─────────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path, monkeypatch):
    """A real file-backed journal, with the broker sync stubbed out.

    `fetch_recent_trades` calls `sync_mt5_history`, and on the dev box the MT5
    terminal is running — a fixture that left the sync in would receive real
    broker tickets next to its own, and every "returns one row" assert would be
    at the mercy of the broker. This file is about the persistence of outcomes,
    not about sync semantics, so the sync is stubbed here and exercised
    explicitly in `SyncRecordsTheExitPriceTest`.
    """
    d = SQLiteTradeDB(db_path=str(tmp_path / "hist.db"))
    monkeypatch.setattr(d, "sync_mt5_history", lambda *a, **kw: None)
    return d


def _open_trade(db, ticket=1001, position_id=2002):
    db.log_trade(
        ticket=ticket, position_id=position_id, symbol="EURUSD", action="BUY",
        entry=1.1000, sl=1.0950, tp=1.1100, volume=0.10,
        score=85.0, regime="TREND_BULL", ev=25.0,
    )


def _row(db, ticket):
    for r in db.fetch_recent_trades(limit=50):
        if r.get("ticket") == ticket:
            return r
    raise AssertionError(f"no row for ticket {ticket}")


# ── 1. the migration ────────────────────────────────────────────────────────

def _old_shape_db(path):
    """A journal as it looked BEFORE this change: no exit_price, and
    `realized_pnl` carrying `DEFAULT 0.0` from migration 1."""
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE executed_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket INTEGER, symbol TEXT, action TEXT, entry_price REAL,
            sl REAL, tp REAL, volume REAL, timestamp TEXT, ai_score REAL,
            regime TEXT, expected_value REAL
        )
    """)
    conn.commit()
    # Bring it to the shape the previous release left it in (version 4).
    migrate(conn, "executed_trades", 4, MIGRATIONS)
    assert read_version(conn) == 4
    return conn


class TestMigrationAddsTheOutcomeColumns:
    def test_the_migration_adds_both_columns_to_an_existing_database(self, tmp_path):
        path = str(tmp_path / "old.db")
        conn = _old_shape_db(path)
        try:
            before = {r[1] for r in conn.execute("PRAGMA table_info(executed_trades)")}
            assert "exit_price" not in before
            assert "realized_pnl" in before, (
                "the premise: migration 1 already added realized_pnl (with DEFAULT 0.0)")

            result = migrate(conn, "executed_trades", SCHEMA_VERSION, MIGRATIONS)
            conn.commit()

            after = {r[1] for r in conn.execute("PRAGMA table_info(executed_trades)")}
            assert result == SCHEMA_VERSION
            assert read_version(conn) == SCHEMA_VERSION
            assert "exit_price" in after, "the exit price column was not added"
            assert "realized_pnl" in after
            assert SCHEMA_VERSION >= 5
        finally:
            conn.close()

    def test_the_backfill_clears_the_sentinel_only_on_rows_that_never_closed(self, tmp_path):
        """`0.0` on an open row is the DEFAULT, not an outcome — but `0.0` on a
        closed row is a real scratch trade, and must survive."""
        path = str(tmp_path / "old.db")
        conn = _old_shape_db(path)
        try:
            # Both rows get realized_pnl = 0.0 from the column DEFAULT. Only the
            # second has a close time, i.e. only the second really closed.
            conn.execute("INSERT INTO executed_trades (ticket, closed_at) VALUES (1, NULL)")
            conn.execute("INSERT INTO executed_trades (ticket, closed_at) "
                         "VALUES (2, '2026-01-01T00:00:00+00:00')")
            conn.commit()

            migrate(conn, "executed_trades", SCHEMA_VERSION, MIGRATIONS)
            conn.commit()

            rows = dict(conn.execute(
                "SELECT ticket, realized_pnl FROM executed_trades ORDER BY ticket"))
            assert rows[1] is None, "an open row must read as unknown, not break-even"
            assert rows[2] == 0.0, "a closed flat trade must keep its real 0.0"
        finally:
            conn.close()


# ── 2. logging then closing ─────────────────────────────────────────────────

class TestAClosedTradeRecordsBothFields:
    def test_the_outcome_is_written_and_survives_a_fresh_read(self, db):
        _open_trade(db, ticket=1001, position_id=2002)

        recorded = db.record_trade_exit(
            position_id=2002, exit_price=1.1100, realized_pnl=42.5,
            closed_at="2026-01-02T00:00:00+00:00")
        assert recorded is True

        # A fresh handle on the same file, so the value is proven to be in the
        # database and not in the writer's connection cache.
        fresh = SQLiteTradeDB(db_path=db.db_path)
        try:
            fresh.sync_mt5_history = lambda *a, **kw: None
            row = _row(fresh, 1001)
        finally:
            fresh.close()

        assert row["exit_price"] == 1.1100
        assert row["realized_pnl"] == 42.5
        assert row["closed_at"] == "2026-01-02T00:00:00+00:00"

    def test_it_can_be_keyed_by_the_order_ticket_for_legacy_rows(self, db):
        _open_trade(db, ticket=555111000, position_id=None)
        assert db.record_trade_exit(ticket=555111000, exit_price=1.05,
                                    realized_pnl=-12.0) is True
        row = _row(db, 555111000)
        assert row["exit_price"] == 1.05
        assert row["realized_pnl"] == -12.0

    def test_an_unknown_exit_price_leaves_the_column_null_but_keeps_the_pnl(self, db):
        """The exit price is not always available; the P&L still is."""
        _open_trade(db, ticket=1001, position_id=2002)
        db.record_trade_exit(position_id=2002, exit_price=None, realized_pnl=-7.0)
        row = _row(db, 1001)
        assert row["exit_price"] is None
        assert row["realized_pnl"] == -7.0

    def test_an_unknown_outcome_does_not_erase_one_already_recorded(self, db):
        _open_trade(db, ticket=1001, position_id=2002)
        db.record_trade_exit(position_id=2002, exit_price=1.11, realized_pnl=9.0)
        # A later call that knows nothing must not wipe the known result.
        db.record_trade_exit(position_id=2002, exit_price=None, realized_pnl=None)
        row = _row(db, 1001)
        assert row["exit_price"] == 1.11
        assert row["realized_pnl"] == 9.0

    def test_an_unmatched_key_reports_that_nothing_was_written(self, db):
        _open_trade(db, ticket=1001, position_id=2002)
        assert db.record_trade_exit(position_id=999999, realized_pnl=1.0) is False


# ── 3 & 4. NULL is not 0.0 ──────────────────────────────────────────────────

class TestUnknownIsNotZero:
    """The regression that produced the original bad finding.

    The journal was read with `r.get("realized_pnl")`, which returns `0.0` for a
    missing key — so an open trade and a break-even trade were the same number.
    A stored NULL is the fix; these pin that a caller cannot get `0.0` back for
    an outcome that was never recorded.
    """

    def test_a_logged_trade_that_has_not_closed_reports_None_not_0_0(self, db):
        _open_trade(db, ticket=1001, position_id=2002)
        row = _row(db, 1001)

        assert "exit_price" in row, "the key must exist, or .get() invents 0.0"
        assert "realized_pnl" in row
        assert row["realized_pnl"] is None, "an open trade has no outcome"
        assert row["exit_price"] is None
        # The exact shape of the original bug: a caller coercing to float.
        assert row["realized_pnl"] != 0.0
        assert float(row["realized_pnl"] or 0.0) == 0.0  # only if you coerce badly

    def test_a_break_even_close_is_still_a_real_zero(self, db):
        """The other side of the same coin: a genuine 0.0 must survive as 0.0."""
        _open_trade(db, ticket=1001, position_id=2002)
        db.record_trade_exit(position_id=2002, exit_price=1.1000, realized_pnl=0.0)
        row = _row(db, 1001)
        assert row["realized_pnl"] == 0.0
        assert row["realized_pnl"] is not None

    def test_historic_rows_with_no_outcome_come_back_as_None(self, tmp_path):
        """A pre-existing row, migrated, must read as unknown — never 0.0.

        This is the shape the audit measured: rows on disk whose P&L is 0 because
        nothing ever wrote one.
        """
        path = str(tmp_path / "legacy.db")
        conn = _old_shape_db(path)
        try:
            conn.execute("INSERT INTO executed_trades (ticket) VALUES (7)")
            conn.commit()
            migrate(conn, "executed_trades", SCHEMA_VERSION, MIGRATIONS)
            conn.commit()
        finally:
            conn.close()

        db = SQLiteTradeDB(db_path=path)
        try:
            db.sync_mt5_history = lambda *a, **kw: None
            row = _row(db, 7)
        finally:
            db.close()

        assert row["realized_pnl"] is None
        assert row["exit_price"] is None
        assert row["realized_pnl"] != 0.0


# ── the broker sync path ────────────────────────────────────────────────────

class TestSyncRecordsTheExitPrice:
    """`sync_mt5_history` is the one writer that has the closing deal in hand."""

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

    def test_a_matched_close_stores_the_deal_price(self, tmp_path):
        import time
        from types import SimpleNamespace

        db = SQLiteTradeDB(db_path=str(tmp_path / "sync.db"))
        try:
            _open_trade(db, ticket=555111000, position_id=555222999)
            exit_deal = SimpleNamespace(
                symbol="EURUSD", position_id=555222999, entry=1, type=1,
                price=1.1088, volume=0.10, profit=42.0, swap=0.0, commission=0.0,
                time=int(time.time()) - 60, magic=888999, comment="",
            )
            self._run_sync(db, [exit_deal])

            db.sync_mt5_history = lambda *a, **kw: None
            row = _row(db, 555111000)
            assert row["realized_pnl"] == 42.0
            assert row["exit_price"] == 1.1088, "the closing deal's price was dropped"
            assert row["closed_at"] is not None
        finally:
            db.close()

    def test_an_open_position_is_not_given_a_zero_outcome(self, tmp_path):
        """Only an entry deal exists — the position has not closed, so there is
        no outcome, and 0.0 would be a lie."""
        import time
        from types import SimpleNamespace

        db = SQLiteTradeDB(db_path=str(tmp_path / "sync.db"))
        try:
            _open_trade(db, ticket=555111000, position_id=555222999)
            entry_deal = SimpleNamespace(
                symbol="EURUSD", position_id=555222999, entry=0, type=0,
                price=1.1000, volume=0.10, profit=0.0, swap=0.0, commission=0.0,
                time=int(time.time()) - 60, magic=888999, comment="",
            )
            self._run_sync(db, [entry_deal])

            db.sync_mt5_history = lambda *a, **kw: None
            row = _row(db, 555111000)
            assert row["closed_at"] is None
            assert row["realized_pnl"] is None, "an open trade has no realised P&L"
            assert row["exit_price"] is None
        finally:
            db.close()


class TestSyncDoesNotDestroyTheEntryTime:
    """A close must not rewrite WHEN THE TRADE OPENED.

    The close UPDATE carried `timestamp = ?` fed with the EXIT deal's time, so
    every sync moved a closed row's timestamp forward to its close second while
    the row kept its id. Measured on data/jarvis_history.db before the fix:
    144/144 closed rows had `closed_at == timestamp` — a zero-duration trade
    with a non-zero realised P&L — and 33 rows were out of chronological order.
    The close time belongs in `closed_at`; `timestamp` is the entry time.
    """

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

    def _deals(self):
        import time
        from types import SimpleNamespace

        # The entry is an hour ago, the exit a minute ago — far enough apart that
        # "the timestamp moved" cannot be confused with clock granularity.
        entry_time = int(time.time()) - 3600
        exit_time = int(time.time()) - 60
        entry_deal = SimpleNamespace(
            symbol="EURUSD", position_id=555222999, entry=0, type=0,
            price=1.1000, volume=0.10, profit=0.0, swap=0.0, commission=0.0,
            time=entry_time, magic=888999, comment="",
        )
        exit_deal = SimpleNamespace(
            symbol="EURUSD", position_id=555222999, entry=1, type=1,
            price=1.1088, volume=0.10, profit=42.0, swap=0.0, commission=0.0,
            time=exit_time, magic=888999, comment="",
        )
        return entry_deal, exit_deal

    def test_the_entry_timestamp_survives_a_close(self, tmp_path):
        import datetime as _dt
        import sqlite3

        # Read the row with a direct query rather than through
        # `fetch_recent_trades`. That read path calls `sync_mt5_history`, which
        # on this box would reach the real terminal, and it also stamps
        # `_last_mt5_sync` — so the explicit sync below would be swallowed by
        # the throttle and the test would pass while proving nothing.
        db_path = str(tmp_path / "sync.db")
        db = SQLiteTradeDB(db_path=db_path)

        def _stamp():
            conn = sqlite3.connect(db_path)
            try:
                return conn.execute(
                    "SELECT timestamp, closed_at, realized_pnl FROM executed_trades "
                    "WHERE ticket = ?", (555111000,)
                ).fetchone()
            finally:
                conn.close()

        try:
            _open_trade(db, ticket=555111000, position_id=555222999)
            before = _stamp()[0]

            entry_deal, exit_deal = self._deals()
            db._last_mt5_sync = 0.0  # defeat the sync throttle explicitly
            self._run_sync(db, [entry_deal, exit_deal])

            after_ts, closed_at, realized = _stamp()
            assert after_ts == before, (
                "the close rewrote the entry timestamp: "
                f"{before!r} -> {after_ts!r}"
            )
            # And it really did record the close, so this is not a no-op test.
            assert realized == 42.0, "the sync did not record the outcome"
            assert closed_at is not None, "the sync did not record a close time"
            assert closed_at != after_ts, (
                "closed_at and timestamp collapsed onto the same second again"
            )
            # `timestamp` is the moment the ENGINE logged the entry (wall
            # clock), not the broker's entry-deal time — so it need not equal
            # `entry_time`. The defect was the stamp being *moved forward* to the
            # exit second; adopting the broker's entry time for a row that
            # already has one is a separate behaviour change and is deliberately
            # not made here. What must hold is that the two stamps stay distinct.
            entry_stamp = _dt.datetime.fromisoformat(after_ts).timestamp()
            exit_stamp = _dt.datetime.fromisoformat(closed_at).timestamp()
            assert abs(entry_stamp - exit_stamp) > 1.0, (
                "the entry timestamp was overwritten with the exit time"
            )
        finally:
            db.close()
