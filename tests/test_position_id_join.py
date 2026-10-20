"""A journal row written at entry must be findable by the exit deal.

D2: `send_market_order` returns `result.order` — the ORDER ticket — and that is
what `log_trade` stored. But `sync_mt5_history` closes rows with
`WHERE ticket = deal.position_id`, and the position id is a **different number**.
Measured on `jarvis_history.db`: 155 rows with `closed_at` NULL and
`realized_pnl` 0.0, which is what a trade that can never be matched looks like.
Every realised-P&L statistic computed from that table is therefore wrong, and
the learning loop never sees the outcome.

The fix persists the position id alongside the order ticket and joins on the
position id, falling back to `ticket` for rows written before the column existed.
"""
import os
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from jarvis.data.database import SQLiteTradeDB

ORDER_TICKET = 555111000     # what order_send returned
POSITION_ID = 555222999      # what the exit deal reports
EXIT_EPOCH = int(time.time()) - 3600


def _deal(position_id, entry, deal_type, price, profit, symbol="EURUSD"):
    return SimpleNamespace(
        symbol=symbol,
        position_id=position_id,
        entry=entry,                 # 0 = DEAL_ENTRY_IN, 1 = DEAL_ENTRY_OUT
        type=deal_type,              # 0 = buy, 1 = sell
        price=price,
        volume=0.10,
        profit=profit,
        swap=0.0,
        commission=0.0,
        time=EXIT_EPOCH,
        magic=888999,
        comment="[sl1.0900][tp1.1100]",
    )


class _FakeMT5:
    """Just enough of MetaTrader5 for `sync_mt5_history` to run offline."""

    def __init__(self, deals):
        self._deals = deals

    def terminal_info(self):
        return SimpleNamespace(connected=True)

    def initialize(self, **kw):
        # Mirrors the real signature: the gate passes `timeout=` so that a
        # missing terminal cannot block for the package's 60s default.
        return True

    def history_deals_get(self, *args, **kwargs):
        return list(self._deals)


class TradeCloseJoinTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = SQLiteTradeDB(db_path=self.path)

    def tearDown(self):
        try:
            self.db._get_conn().close()
        except Exception:
            pass
        if os.path.exists(self.path):
            os.remove(self.path)

    def _log(self, ticket, position_id):
        self.db.log_trade(
            ticket=ticket,
            position_id=position_id,
            symbol="EURUSD",
            action="BUY",
            entry=1.1000,
            sl=1.0950,
            tp=1.1100,
            volume=0.10,
            score=85.0,
            regime="TREND_BULL",
            ev=25.0,
        )

    def _sync_exit(self, position_id, profit):
        deals = [_deal(position_id, entry=1, deal_type=1, price=1.1100, profit=profit)]
        fake = _FakeMT5(deals)
        with patch("jarvis.data.database.broker_utc_offset", return_value=0):
            self.db.sync_mt5_history(days=1, mt5_module=fake)

    def _rows(self):
        conn = self.db._get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT ticket, position_id, closed_at, realized_pnl "
                "FROM executed_trades ORDER BY id"
            )
            return cur.fetchall()
        finally:
            pass

    def test_the_two_identifiers_really_are_different_numbers(self):
        """The premise of the whole defect — if they were equal none of this matters."""
        self.assertNotEqual(ORDER_TICKET, POSITION_ID)

    def test_a_trade_logged_with_the_order_ticket_is_closed_by_its_position_id(self):
        """The row exists but holds the ORDER ticket; the deal reports the POSITION id.

        Before the fix the sync matched `ticket = position_id`, found nothing, and
        INSERTED a second row — leaving the original open with realized_pnl 0.0.
        """
        self._log(ticket=ORDER_TICKET, position_id=POSITION_ID)
        self._sync_exit(POSITION_ID, profit=42.0)

        rows = self._rows()
        self.assertEqual(len(rows), 1, "the sync must not insert a duplicate row")
        ticket, position_id, closed_at, realized_pnl = rows[0]
        self.assertEqual(ticket, ORDER_TICKET, "the order ticket must be preserved")
        self.assertEqual(position_id, POSITION_ID)
        self.assertIsNotNone(closed_at, "the trade must be closed")
        self.assertAlmostEqual(realized_pnl, 42.0, places=6)

    def test_a_legacy_row_keyed_only_by_its_ticket_still_closes(self):
        """Rows written before `position_id` existed must keep working."""
        self._log(ticket=POSITION_ID, position_id=None)
        self._sync_exit(POSITION_ID, profit=-18.0)

        rows = self._rows()
        self.assertEqual(len(rows), 1)
        _, _, closed_at, realized_pnl = rows[0]
        self.assertIsNotNone(closed_at)
        self.assertAlmostEqual(realized_pnl, -18.0, places=6)

    def test_an_unrelated_position_does_not_close_this_trade(self):
        """The join must not widen so far that any deal closes any row."""
        self._log(ticket=ORDER_TICKET, position_id=POSITION_ID)
        self._sync_exit(POSITION_ID + 7, profit=99.0)

        rows = self._rows()
        self.assertEqual(len(rows), 2, "an unknown position is a new row, not a match")
        self.assertIsNone(rows[0][2], "our trade must still be open")


if __name__ == "__main__":
    unittest.main()
