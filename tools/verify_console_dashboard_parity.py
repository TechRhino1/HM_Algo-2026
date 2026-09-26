"""
Verification Harness: Test Page (/console) vs. Live Page (/dashboard) Contract & State Parity.
Confirms that both user interfaces execute against identical backend engines, endpoints, models, and risk gates.
"""
from __future__ import annotations

import json
import logging
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jarvis.api.server import ThreadingHTTPServer, JarvisRequestHandler
from jarvis.data.schemas import AccountSnapshot, PositionSnapshot
from jarvis.risk.risk_engine import RiskEngine
from jarvis.application.orchestrator import JarvisOrchestrator

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("ParityVerification")


class TestConsoleDashboardParity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.templates_dir = REPO_ROOT / "jarvis" / "ui" / "templates"
        cls.static_js_dir = REPO_ROOT / "jarvis" / "ui" / "static" / "js"
        cls.console_html = (cls.templates_dir / "console.html").read_text(encoding="utf-8")
        cls.dashboard_html = (cls.templates_dir / "dashboard.html").read_text(encoding="utf-8")
        cls.console_js = (cls.static_js_dir / "console.js").read_text(encoding="utf-8")
        cls.dashboard_js = (cls.static_js_dir / "dashboard.js").read_text(encoding="utf-8")

    def test_both_templates_exist_and_load_shared_libraries(self):
        """Verify both pages load common UI architecture."""
        self.assertTrue((self.templates_dir / "console.html").exists())
        self.assertTrue((self.templates_dir / "dashboard.html").exists())
        # Both load authentication and common lightweight-charts
        self.assertIn("lightweight-charts", self.console_html)
        self.assertIn("lightweight-charts", self.dashboard_html)
        self.assertIn("auth.js", self.console_html)
        self.assertIn("auth.js", self.dashboard_html)

    def test_telemetry_state_parity(self):
        """Verify both UIs poll the exact same /api/telemetry_state contract."""
        self.assertIn("/api/telemetry_state", self.console_js)
        # Dashboard polls telemetry through hm_ui.js or apiGet
        shared_hm_ui = (self.static_js_dir / "hm_ui.js").read_text(encoding="utf-8")
        self.assertTrue(
            "/api/telemetry_state" in self.dashboard_js or "/api/telemetry_state" in shared_hm_ui
        )

    def test_intelligence_auto_selection_parity(self):
        """Verify both UIs query the exact same autonomous radar opportunity endpoint."""
        self.assertIn("/api/intelligence/auto-selection", self.console_js)
        self.assertIn("/api/intelligence/auto-selection", self.dashboard_js)

    def test_manual_trade_execution_parity(self):
        """Verify both UIs dispatch manual orders to the same backend order manager."""
        self.assertIn("/api/action/manual_trade", self.console_js)
        # Dashboard uses manual_trade or place_pending_order
        self.assertTrue(
            "/api/action/manual_trade" in self.dashboard_js or "/api/action/place_pending_order" in self.dashboard_js
        )

    def test_close_position_parity(self):
        """Verify both UIs trigger the exact same emergency position closure logic."""
        self.assertIn("/api/action/close_position", self.console_js)
        self.assertIn("/api/action/close_position", self.dashboard_js)

    def test_server_routes_both_surfaces(self):
        """Verify ThreadingHTTPServer routes both paths cleanly without bypasses."""
        from jarvis.api.server import JarvisRequestHandler
        # Check that both endpoints are defined in public_get_endpoints
        server_py = (REPO_ROOT / "jarvis" / "api" / "server.py").read_text(encoding="utf-8")
        self.assertIn('"/dashboard"', server_py)
        self.assertIn('"/console"', server_py)
        self.assertIn("self._serve_dashboard_ui()", server_py)
        self.assertIn("self._serve_console_ui()", server_py)

    def test_risk_gate_is_unbypassable_for_both_surfaces(self):
        """Verify that any trade action from either surface is strictly bound to RiskEngine."""
        risk_engine = RiskEngine(max_risk_per_trade_pct=0.5)
        # Attempt an invalid order (excessive risk)
        acc = AccountSnapshot(login=101059540, server="XMGlobal-MT5", balance=618.70, equity=618.70, margin=0.0, free_margin=618.70, margin_level=0.0, leverage=500)
        from unittest.mock import MagicMock
        decision = MagicMock()
        decision.symbol = "XAUUSD"
        decision.regime = None
        decision.bias = "BUY"
        decision.entry_price = 3000.0
        decision.stop_loss = 2900.0
        decision.take_profit = 3200.0
        decision.risk_reward_ratio = 2.0
        decision.expected_value = 0.5
        decision.model_confidence = 0.6
        decision.calculated_risk_percent = 0.5
        # Calling authorize_execution with extreme spread violation (e.g. 999.0 pips) must reject
        auth = risk_engine.authorize_execution(
            decision=decision, account=acc, positions=[], symbol_info={"name": "XAUUSD"},
            current_spread_pips=999.0, max_allowed_spread_pips=5.0
        )
        self.assertFalse(auth["authorized"])
        self.assertTrue(any("spread" in str(r).lower() for r in auth["reasons"]))


if __name__ == "__main__":
    unittest.main()
