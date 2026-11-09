"""The historical/backtest path must initialize the terminal and resolve the
symbol before it asks MT5 for bars.

Why this exists
---------------
A backtest run never places an order, so nothing in that process ever called
`mt5.initialize()`. Two defects followed, both measured against a live terminal:

1. Every `mt5.*` call returned `None` with `(-10004, 'No IPC connection')`, so
   `copy_rates_range` "found no history" and the run fell through to the refusal
   path. `MT5_AVAILABLE` only means the package *imported* — it is not evidence
   the terminal is up in this process. Measured: before `initialize()` -> `None`
   / `(-10004, 'No IPC connection')`; immediately after -> 5 bars.

2. `MT5Client.resolve_symbol_name` returns the input UNCHANGED whenever the
   client is in paper mode or simply not connected — i.e. in every historical
   process. The broker calls gold `GOLD.i#`, so asking for `XAUUSD` returned 0
   bars, and 0 bars is indistinguishable from "the broker has no history for
   this range".

Both made `tests/test_backtest_with_historical_engine.py` fail. These tests pin
the two fixes: the gate runs before any terminal call, and an unresolved symbol
is resolved against the terminal rather than trusted.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest import mock

import pandas as pd

from jarvis.data.mt5_history import SyntheticDataError
from jarvis.historical import acquisition as acq


def _bar(ts: int) -> dict:
    return {
        "time": ts,
        "open": 1800.0,
        "high": 1801.0,
        "low": 1799.0,
        "close": 1800.5,
        "tick_volume": 100,
        "spread": 0,
        "real_volume": 0,
    }


class _FakeMT5:
    """Stands in for the MetaTrader5 package and records every call made."""

    def __init__(self, bars=None):
        self.bars = [] if bars is None else bars
        self.initialize_calls = 0
        self.selects = []
        self.ranges = []
        self.from_pos = []

    def initialize(self, *a, **kw):
        self.initialize_calls += 1
        return False

    def last_error(self):
        return (-10004, "No IPC connection")

    def terminal_info(self):
        return None

    def account_info(self):
        return None

    def symbol_select(self, symbol, on):  # noqa: D102 - fake
        self.selects.append(symbol)
        return True

    def copy_rates_range(self, symbol, tf, start, end):
        self.ranges.append(symbol)
        return self.bars

    def copy_rates_from_pos(self, symbol, tf, pos, count):
        self.from_pos.append(symbol)
        return self.bars


class _StubClient:
    """Mimics an MT5Client that cannot resolve: paper/disconnected returns the
    input unchanged, which is exactly what every historical process has."""

    def __init__(self, resolved=None):
        self._resolved = resolved
        self.mode = "live"

    def resolve_symbol_name(self, symbol):
        return symbol if self._resolved is None else self._resolved


class AcquisitionTerminalGateTest(unittest.TestCase):
    START = datetime(2024, 1, 1, tzinfo=timezone.utc)
    END = datetime(2024, 1, 2, tzinfo=timezone.utc)

    def _engine(self, client=None):
        return acq.AcquisitionEngine(
            storage=None,
            metadata_db=None,
            quality_engine=None,
            mt5_client=client or _StubClient(),
        )

    def _download(self, engine, gate_open=True, bars=None, broker_symbol=None):
        """Runs the download and returns `(fake, gate, resolve, df, error)`.

        The refusal path raises, so the error is captured rather than allowed to
        escape — the call recording on `fake` is the evidence under test, and it
        is lost if the assertion has to live inside the `assertRaises` block.
        """
        fake = _FakeMT5(bars=bars)
        error = None
        df = None
        with mock.patch.object(acq, "MT5_AVAILABLE", True), \
                mock.patch.object(acq, "mt5", fake, create=True), \
                mock.patch("jarvis.data.broker_symbols.ensure_mt5_terminal",
                           return_value=gate_open) as gate, \
                mock.patch("jarvis.data.broker_symbols.resolve_broker_symbol",
                           return_value=broker_symbol) as resolve:
            try:
                df = engine.download_range_from_mt5(
                    "XAUUSD", "H1", self.START, self.END)
            except SyntheticDataError as exc:
                error = exc
        return fake, gate, resolve, df, error

    def test_a_closed_gate_refuses_instead_of_fabricating(self):
        """No terminal in this process => loud failure, never invented bars."""
        engine = self._engine()

        _fake, _gate, _resolve, df, error = self._download(
            engine, gate_open=False, bars=[_bar(1704067200)])

        self.assertIsNone(df, "returned bars with no terminal up")
        self.assertIsInstance(error, SyntheticDataError)
        self.assertIn("Refusing to fabricate", str(error))

    def test_a_closed_gate_never_reaches_the_terminal(self):
        """The point of the gate: no `mt5.*` call before it reports ready."""
        engine = self._engine()
        fake, gate, _resolve, _df, _error = self._download(
            engine, gate_open=False, bars=[_bar(1704067200)])

        self.assertGreater(gate.call_count, 0, "the shared gate was never consulted")
        self.assertEqual(fake.ranges, [], "asked for bars with no terminal up")
        self.assertEqual(fake.from_pos, [], "asked for bars with no terminal up")
        self.assertEqual(fake.selects, [], "selected a symbol with no terminal up")
        self.assertEqual(fake.initialize_calls, 0,
                         "the acquisition path called mt5.initialize() itself "
                         "instead of dialling the shared gate")

    def test_an_open_gate_returns_real_bars(self):
        engine = self._engine()
        fake, gate, _resolve, df, _error = self._download(
            engine, gate_open=True, bars=[_bar(1704067200)])

        self.assertGreater(gate.call_count, 0)
        self.assertIsInstance(df, pd.DataFrame)
        self.assertFalse(df.empty)
        self.assertIn("close", df.columns)

    def test_an_unconnected_client_does_not_strand_the_canonical_symbol(self):
        """`resolve_symbol_name` returns the input unchanged when the client is
        not connected, so the broker name has to be resolved explicitly — this
        broker calls gold `GOLD.i#` and `XAUUSD` returns 0 bars."""
        engine = self._engine(_StubClient(resolved=None))  # returns "XAUUSD"
        fake, _gate, resolve, _df, _error = self._download(
            engine, gate_open=True, bars=[_bar(1704067200)], broker_symbol="GOLD.i#")

        self.assertGreater(resolve.call_count, 0,
                           "the symbol was never resolved against the terminal")
        self.assertEqual(fake.ranges, ["GOLD.i#"],
                         "asked MT5 for XAUUSD, which this broker does not list")
        self.assertEqual(fake.selects, ["GOLD.i#"])

    def test_a_client_that_already_resolved_is_left_alone(self):
        """Do not second-guess a real resolution — only fill in a missing one."""
        engine = self._engine(_StubClient(resolved="EURUSD.i#"))
        fake, _gate, resolve, _df, _error = self._download(
            engine, gate_open=True, bars=[_bar(1704067200)], broker_symbol="GOLD.i#")

        self.assertEqual(fake.ranges, ["EURUSD.i#"])
        self.assertEqual(resolve.call_count, 0,
                         "overrode a symbol the client had already resolved")

    def test_zero_bars_refuses_rather_than_fabricating(self):
        """An empty result is a failure, not a licence to invent a market."""
        engine = self._engine()
        fake, _gate, _resolve, df, error = self._download(
            engine, gate_open=True, bars=[], broker_symbol="GOLD.i#")

        self.assertIsNone(df)
        self.assertIsInstance(error, SyntheticDataError)
        self.assertGreater(
            len(fake.from_pos), 0,
            "gave up after one empty read instead of retrying from position",
        )

    def test_resolution_failure_does_not_crash_the_download(self):
        """A resolver that raises must not take the whole run down."""
        engine = self._engine()
        fake = _FakeMT5(bars=[_bar(1704067200)])
        with mock.patch.object(acq, "MT5_AVAILABLE", True), \
                mock.patch.object(acq, "mt5", fake, create=True), \
                mock.patch("jarvis.data.broker_symbols.ensure_mt5_terminal",
                           return_value=True), \
                mock.patch("jarvis.data.broker_symbols.resolve_broker_symbol",
                           side_effect=RuntimeError("no terminal")):
            df = engine.download_range_from_mt5("XAUUSD", "H1", self.START, self.END)

        self.assertFalse(df.empty)
        self.assertEqual(fake.ranges, ["XAUUSD"], "fell back to the unresolved name")


if __name__ == "__main__":
    unittest.main()
