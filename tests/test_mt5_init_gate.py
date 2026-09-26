"""MT5 initialization must go through ONE process-wide gate.

Why this exists
---------------
`mt5.initialize()` blocks inside native code while HOLDING THE GIL when no
terminal answers (measured: 60-100s, "Pipe server didn't answer in 60 sec").
`broker_symbols.ensure_mt5_terminal` already serialises that behind one lock and
refuses to retry within a cooldown — but `MT5Client.init_connection` used to
call `mt5.initialize()` itself, five times over with backoff, once per thread.

The consequence was measured on a live `HM_start.py live` with MT5 down: six
worker threads cycling through that call starved the main thread so completely
that `run_web_server` never reached `ThreadingHTTPServer`. The process ran for
40+ minutes, started both tunnels, logged "Orchestrator and Watchdog started" —
and never listened on :8501. Alive and logging was not evidence of serving.

These tests assert the client dials the gate, not the terminal.
"""

from __future__ import annotations

import logging
import unittest
from unittest import mock

from jarvis.execution import mt5_client as mc


class _FakeMT5:
    """Stands in for the MetaTrader5 package. Any direct use is the defect."""

    def __init__(self):
        self.initialize_calls = 0

    def initialize(self, *a, **kw):
        self.initialize_calls += 1
        return False

    def last_error(self):
        return (-10003, "IPC initialize failed")

    def terminal_info(self):
        return None

    def account_info(self):
        return None


class InitGateTest(unittest.TestCase):
    def _client(self):
        return mc.MT5Client(mode="live", auto_init=False)

    def test_a_dead_terminal_never_reaches_initialize(self):
        """The gate decides; the client must not also dial the terminal."""
        fake = _FakeMT5()
        with mock.patch.object(mc, "MT5_AVAILABLE", True), \
                mock.patch.object(mc, "mt5", fake), \
                mock.patch("time.sleep", lambda *_a, **_k: None), \
                mock.patch("jarvis.data.broker_symbols.ensure_mt5_terminal",
                           return_value=False) as gate:
            client = self._client()
            connected = client.init_connection()

        self.assertFalse(connected)
        self.assertFalse(client.is_connected)
        self.assertGreater(gate.call_count, 0, "the shared gate was never consulted")
        self.assertEqual(
            fake.initialize_calls, 0,
            "MT5Client called mt5.initialize() directly and bypassed the gate — "
            "that is what starved the main thread and stopped the web server "
            "from ever binding",
        )

    def test_the_client_reports_connected_when_the_gate_opens(self):
        fake = _FakeMT5()
        with mock.patch.object(mc, "MT5_AVAILABLE", True), \
                mock.patch.object(mc, "mt5", fake), \
                mock.patch("time.sleep", lambda *_a, **_k: None), \
                mock.patch("jarvis.data.broker_symbols.ensure_mt5_terminal",
                           return_value=True):
            client = self._client()
            connected = client.init_connection()

        self.assertTrue(connected)
        self.assertTrue(client.is_connected)
        self.assertEqual(fake.initialize_calls, 0)

    def test_paper_mode_still_short_circuits_without_touching_the_gate(self):
        """Paper must not drag a terminal into a simulated run."""
        fake = _FakeMT5()
        with mock.patch.object(mc, "MT5_AVAILABLE", True), \
                mock.patch.object(mc, "mt5", fake), \
                mock.patch("jarvis.data.broker_symbols.ensure_mt5_terminal",
                           return_value=False) as gate:
            client = mc.MT5Client(mode="paper", auto_init=False)
            connected = client.init_connection()

        self.assertTrue(connected)
        self.assertEqual(gate.call_count, 0, "paper mode asked for the terminal")
        self.assertEqual(fake.initialize_calls, 0)

    def test_the_mode_is_not_rewritten_when_the_package_is_missing(self):
        """Under Spec v2.2 fail-closed matrix, live mode is strictly NOT rewritten to paper."""
        with mock.patch.object(mc, "MT5_AVAILABLE", False):
            client = self._client()
            connected = client.init_connection()
        self.assertEqual(client.mode, "live")
        self.assertFalse(connected)

    def test_a_dead_terminal_is_not_logged_once_per_attempt(self):
        """Measured: 544 ERROR lines in 30 minutes on the live platform.

        `_init` runs inside a six-attempt backoff loop that every
        broker-touching worker calls, so an unconditional log here fires ~18
        times a minute for a condition that is expected and already explained by
        the gate's own warning. That volume is how a real error gets missed.
        """
        fake = _FakeMT5()
        with mock.patch.object(mc, "MT5_AVAILABLE", True), \
                mock.patch.object(mc, "mt5", fake), \
                mock.patch("time.sleep", lambda *_a, **_k: None), \
                mock.patch("jarvis.data.broker_symbols.ensure_mt5_terminal",
                           return_value=False), \
                self.assertLogs(mc.logger, level="WARNING") as captured:
            client = self._client()
            client.init_connection()
            client.init_connection()

        records = captured.records
        not_available = [r for r in records if "MT5 not available" in r.getMessage()]
        self.assertEqual(len(not_available), 1,
                         [r.getMessage() for r in records])
        self.assertEqual(
            [r.levelname for r in records if r.levelno >= logging.ERROR], [],
            "an expected condition (no terminal) must not be logged at ERROR",
        )
        self.assertEqual(
            fake.initialize_calls, 0,
            "the retry loop reached the terminal instead of the gate",
        )


if __name__ == "__main__":
    unittest.main()
