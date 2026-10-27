"""
Execution-mode safety gate.

`live` is only reachable with the explicit JARVIS_CONFIRM_LIVE=1
acknowledgement; without it the resolver falls back to paper. Demo remains the
default, and paper/backtest synonyms are unchanged.
"""
import os
import unittest
from unittest.mock import patch

from jarvis.config.settings import verify_execution_mode
from jarvis.application.orchestrator import JarvisOrchestrator


class TestLiveSafetyGate(unittest.TestCase):
    def test_live_without_confirmation_falls_back_to_paper(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("JARVIS_CONFIRM_LIVE", None)
            self.assertEqual(verify_execution_mode("live"), "paper")
        self.assertEqual(verify_execution_mode(None), "demo")
        self.assertEqual(verify_execution_mode(""), "demo")

    def test_live_with_confirmation_stays_live(self):
        with patch.dict(os.environ, {"JARVIS_CONFIRM_LIVE": "1"}):
            self.assertEqual(verify_execution_mode("live"), "live")

    def test_verify_execution_mode_paper_for_testing(self):
        """'paper' mode or simulation synonyms map to paper; demo is allowed."""
        self.assertEqual(verify_execution_mode("paper"), "paper")
        self.assertEqual(verify_execution_mode("backtest"), "paper")
        self.assertEqual(verify_execution_mode("simulated"), "paper")
        self.assertEqual(verify_execution_mode("demo"), "demo")

    def test_orchestrator_initialization_live_requires_confirmation(self):
        with patch.dict(os.environ, {"JARVIS_CONFIRM_LIVE": "1"}):
            orchestrator = JarvisOrchestrator(mode="live")
        self.assertEqual(orchestrator.mode, "live")

    def test_orchestrator_initialization_paper(self):
        """JarvisOrchestrator initialized with mode='paper' remains in paper mode for testing."""
        orchestrator = JarvisOrchestrator(mode="paper")
        self.assertEqual(orchestrator.mode, "paper")


if __name__ == "__main__":
    unittest.main()
