"""Every journal row must be able to say where its price came from (D1).

Measured on `data/jarvis_history.db`: of 269 rows, 158 carry a microsecond
timestamp — the signature of `datetime.now()` in `log_trade`, i.e. written by
the engine — and **0 of those 158 have ever closed**. The other 111 have
whole-second timestamps (the signature of `sync_mt5_history`, which formats an
int epoch) and 110 of those ARE closed. Two populations, one table, no column
to tell them apart: every realised-P&L statistic read from it mixes simulated
money with real.

The `origin` column fixes the labelling. This file fixes the other half — the
labeller. Before this change `synthetic` was unreachable: `_price_origin`
branched on `res.get("is_fallback")`, and **no fill ever set that key**. The
only `is_fallback` in `mt5_client.py` was line 329, inside the quote *refusal*
path, which returns None and never produces a fill. So a live client that
silently simulated an order was booked as `broker`.

The subtle part is the downgrade. `init_connection()` rewrites `self.mode` to
"paper" when the terminal is missing, and `send_market_order` called
`_reconnect_if_needed()` BEFORE checking the mode — so by the time the branch
ran, a live client had already forgotten it was ever live. The requested mode
has to be captured first, or `is_fallback` is False in exactly the case it
exists to catch.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jarvis.data.database import SQLiteTradeDB, ORIGINS
from jarvis.execution.execution_engine import ExecutionEngine
from jarvis.execution import mt5_client as mt5_module
from jarvis.execution.mt5_client import MT5Client

BUY = dict(
    symbol="XAUUSD", order_type="BUY", volume=0.01,
    sl_price=4200.0, tp_price=4500.0, reference_price=4328.88,
)


def _origin(client, res):
    return ExecutionEngine(mt5_client=client)._price_origin(res)


class FillOriginTest(unittest.TestCase):
    """`_price_origin` must name the source, and `synthetic` must be reachable."""

    def test_a_paper_fill_is_not_a_fallback(self):
        """Paper mode asked for a simulated fill, so `paper` is the honest label."""
        paper = MT5Client(mode="paper")
        res = paper.send_market_order(**BUY)
        self.assertEqual(res["status"], "FILLED")
        self.assertIs(res["is_fallback"], False)
        self.assertEqual(_origin(paper, res), "paper")

    def test_a_live_client_without_a_terminal_reports_the_fallback(self):
        """Under Spec v2.2 INV-06, a live order without MT5 terminal strictly fails closed."""
        client = MT5Client(mode="live", auto_init=False)
        self.assertEqual(client.mode, "live", "the client is configured for real trading")
        with _no_terminal():
            res = client.send_market_order(**BUY)

        self.assertEqual(res["status"], "FAILED")
        self.assertIn("FAIL_CLOSED", res.get("reason", ""))
        self.assertEqual(client.mode, "live", "mode must not be downgraded to paper")
        self.assertEqual(_origin(client, {"status": "FILLED", "is_fallback": True}), "synthetic")

    def test_a_demo_client_without_a_terminal_reports_the_fallback(self):
        """Under Spec v2.2 INV-06, a demo order without MT5 terminal strictly fails closed."""
        client = MT5Client(mode="demo", auto_init=False)
        with _no_terminal():
            res = client.send_market_order(**BUY)
        self.assertEqual(res["status"], "FAILED")
        self.assertIn("FAIL_CLOSED", res.get("reason", ""))
        self.assertEqual(_origin(client, {"status": "FILLED", "is_fallback": True}), "synthetic")

    def test_a_live_client_that_reached_the_broker_reports_broker(self):
        client = MT5Client(mode="live", auto_init=False)
        client.is_connected = True
        # A real fill: no fallback flag on the result.
        self.assertEqual(_origin(client, {"status": "FILLED", "price": 4328.88}), "broker")

    def test_a_live_client_that_could_not_connect_is_not_counted_as_broker(self):
        client = MT5Client(mode="live", auto_init=False)
        client.is_connected = False
        self.assertEqual(_origin(client, {"status": "FILLED", "price": 4328.88}), "synthetic")

    def test_a_mode_we_do_not_recognise_is_unknown_not_guessed(self):
        client = MT5Client(mode="paper", auto_init=False)
        client.mode = "backtest"
        self.assertEqual(_origin(client, {"status": "FILLED"}), "unknown")


class _no_terminal:
    """Make `MT5_AVAILABLE` false for the duration, without touching the install."""

    def __enter__(self):
        self._before = mt5_module.MT5_AVAILABLE
        mt5_module.MT5_AVAILABLE = False
        return self

    def __exit__(self, *exc):
        mt5_module.MT5_AVAILABLE = self._before
        return False


class JournalOriginTest(unittest.TestCase):
    """The label has to survive the trip into SQLite."""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = SQLiteTradeDB(db_path=self.path)

    def tearDown(self):
        try:
            self.db._get_conn().close()
        except Exception:
            pass
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + suffix)
            except OSError:
                pass

    def _log(self, origin):
        self.db.log_trade(
            ticket=99001, position_id=99002, symbol="XAUUSD", action="BUY",
            entry=4328.88, sl=4200.0, tp=4500.0, volume=0.01,
            score=85.0, regime="TREND_BULL", ev=25.0, origin=origin,
        )

    def _origin(self):
        cur = self.db._get_conn().cursor()
        cur.execute("SELECT origin FROM executed_trades WHERE ticket = 99001")
        row = cur.fetchone()
        return row[0] if row else None

    def test_each_origin_is_stored_verbatim(self):
        for origin in ("broker", "paper", "synthetic"):
            with self.subTest(origin=origin):
                self.setUp()
                try:
                    self._log(origin)
                    self.assertEqual(self._origin(), origin)
                finally:
                    self.tearDown()

    def test_an_unrecognised_origin_is_recorded_as_unknown(self):
        """Guessing would let a filter silently include rows of unknown provenance."""
        self._log("definitely-not-a-source")
        self.assertEqual(self._origin(), "unknown")

    def test_no_origin_at_all_is_recorded_as_unknown(self):
        self._log(None)
        self.assertEqual(self._origin(), "unknown")

    def test_the_recognised_set_is_exactly_what_the_schema_documents(self):
        """`unknown` is a stored value, not one of the caller's choices."""
        self.assertEqual(
            set(ORIGINS), {"broker", "paper", "synthetic", "unknown"},
            "ORIGINS is a tuple the caller's value is tested against — a mapping "
            "here raised AttributeError inside log_trade and silently dropped "
            "every journalled trade.",
        )


class SyncStampsBrokerTest(unittest.TestCase):
    """A row the broker confirms must be promoted out of `unknown`."""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = SQLiteTradeDB(db_path=self.path)

    def tearDown(self):
        try:
            self.db._get_conn().close()
        except Exception:
            pass
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + suffix)
            except OSError:
                pass

    def test_a_row_of_unknown_origin_is_promoted_when_the_broker_confirms_it(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        self.db.log_trade(
            ticket=99001, position_id=99002, symbol="EURUSD", action="BUY",
            entry=1.1000, sl=1.0950, tp=1.1100, volume=0.10,
            score=85.0, regime="TREND_BULL", ev=25.0, origin="unknown",
        )
        deal = SimpleNamespace(
            symbol="EURUSD", position_id=99002, entry=1, type=1, price=1.1100,
            volume=0.10, profit=42.0, swap=0.0, commission=0.0,
            time=1700000000, magic=888999, comment="",
        )
        fake = SimpleNamespace(
            terminal_info=lambda: SimpleNamespace(connected=True),
            # `**kw` because the gate calls initialize(timeout=...): a fake that
            # refuses the kwarg makes the gate report "no terminal" and the sync
            # silently does nothing.
            initialize=lambda **kw: True,
            history_deals_get=lambda *a, **k: [deal],
        )
        with patch.dict(sys.modules, {"MetaTrader5": fake}), \
                patch("jarvis.data.database.broker_utc_offset", return_value=0):
            self.db.sync_mt5_history(days=1)

        cur = self.db._get_conn().cursor()
        cur.execute("SELECT origin, closed_at FROM executed_trades WHERE ticket = 99001")
        origin, closed_at = cur.fetchone()
        self.assertIsNotNone(closed_at, "the broker closed it")
        self.assertEqual(origin, "broker", "a confirmed deal outranks our guess")


if __name__ == "__main__":
    unittest.main()
