"""Unit and Contract Tests for Bugatti Cockpit UI & Extended Intelligence Endpoints."""
import unittest
import os
import json
from jarvis.api.intelligence_api import INTELLIGENCE
from jarvis.risk.risk_engine import RiskEngine
from jarvis.data.schemas import AccountSnapshot


class TestBugattiEndpoints(unittest.TestCase):
    def setUp(self):
        self.risk_engine = RiskEngine()

    def test_risk_status_includes_portfolio_heat_and_hrp(self):
        """Verify get_risk_status returns full cockpit heat instrument telemetry."""
        acc = AccountSnapshot(
            login=101059540,
            server="XMGlobal-MT5",
            balance=10000.0,
            equity=10000.0,
            margin=500.0,
            free_margin=9500.0,
            margin_level=2000.0,
            leverage=100
        )
        status = self.risk_engine.get_risk_status(10000.0, 10000.0, account=acc, positions=[])
        self.assertIn("portfolio_heat_score", status)
        self.assertIn("portfolio_heat_zone", status)
        self.assertIn("heat_risk_multiplier", status)
        self.assertIn("margin_utilization_pct", status)
        self.assertIn("hrp_weights", status)
        self.assertEqual(status["portfolio_heat_zone"], "NORMAL")
        self.assertGreaterEqual(status["portfolio_heat_score"], 0.0)
        self.assertLessEqual(status["portfolio_heat_score"], 100.0)
        self.assertIn("XAUUSD", status["hrp_weights"])

    def test_intelligence_missed_trades_endpoint(self):
        """Verify /api/intelligence/missed-trades returns structured vetoed opportunities."""
        class MockHandler:
            def __init__(self): self.data = None
        import jarvis.api.intelligence_api as mod
        old_json = mod._json
        try:
            mod._json = lambda h, p, c=200: setattr(h, 'data', p)
            handler = MockHandler()
            handled = INTELLIGENCE.handle_get('/api/intelligence/missed-trades', {}, handler)
            self.assertTrue(handled)
            self.assertIsNotNone(handler.data)
            self.assertEqual(handler.data["status"], "OK")
            self.assertIn("missed_trades", handler.data)
            self.assertGreater(handler.data["count"], 0)
            trade = handler.data["missed_trades"][0]
            self.assertIn("symbol", trade)
            self.assertIn("gate", trade)
            self.assertIn("reason", trade)
        finally:
            mod._json = old_json

    def test_intelligence_calibration_endpoint(self):
        """Verify /api/intelligence/calibration returns Brier score, ECE, and honest base rates."""
        class MockHandler:
            def __init__(self): self.data = None
        import jarvis.api.intelligence_api as mod
        old_json = mod._json
        try:
            mod._json = lambda h, p, c=200: setattr(h, 'data', p)
            handler = MockHandler()
            handled = INTELLIGENCE.handle_get('/api/intelligence/calibration', {}, handler)
            self.assertTrue(handled)
            self.assertIsNotNone(handler.data)
            self.assertEqual(handler.data["status"], "OK")
            self.assertIn("brier_score", handler.data)
            self.assertIn("ece", handler.data)
            self.assertIn("calibration_bins", handler.data)
            self.assertIn("honest_base_rates", handler.data)
            self.assertIn("XAUUSD", handler.data["honest_base_rates"])
        finally:
            mod._json = old_json

    def test_intelligence_symbol_profiles_endpoint(self):
        """Verify /api/intelligence/symbol-profiles returns all 20 symbol configs."""
        class MockHandler:
            def __init__(self): self.data = None
        import jarvis.api.intelligence_api as mod
        old_json = mod._json
        try:
            mod._json = lambda h, p, c=200: setattr(h, 'data', p)
            handler = MockHandler()
            handled = INTELLIGENCE.handle_get('/api/intelligence/symbol-profiles', {}, handler)
            self.assertTrue(handled)
            self.assertIsNotNone(handler.data)
            self.assertEqual(handler.data["status"], "OK")
            self.assertEqual(handler.data["count"], 21)
            profiles = handler.data["profiles"]
            self.assertIn("XAUUSD", profiles)
            self.assertIn("EURUSD", profiles)
            self.assertIn("BTCUSD", profiles)
            # Gold must have its calibrated 1.8R BE trigger
            self.assertEqual(profiles["XAUUSD"]["be_trigger_r"], 1.80)
            self.assertEqual(profiles["XAUUSD"]["fast_cash_r"], 1.50)
        finally:
            mod._json = old_json

    def test_theme_bugatti_css_exists_and_valid(self):
        """Verify theme_bugatti.css exists and defines core luxury palette tokens."""
        css_path = os.path.join(os.path.dirname(__file__), "..", "jarvis", "ui", "static", "css", "theme_bugatti.css")
        self.assertTrue(os.path.exists(css_path))
        with open(css_path, "r", encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn("--bg-onyx", content)
        self.assertIn("--bugatti-blue", content)
        self.assertIn("--electric-cyan", content)
        self.assertIn("--hyper-crimson", content)
        self.assertIn("cockpit-gauge", content)
        self.assertIn("lifecycle-stepper", content)

    def test_dashboard_html_components_present(self):
        """Verify dashboard.html contains the Trade Lifecycle Stepper, Gauge, and Modals."""
        html_path = os.path.join(os.path.dirname(__file__), "..", "jarvis", "ui", "templates", "dashboard.html")
        self.assertTrue(os.path.exists(html_path))
        with open(html_path, "r", encoding="utf-8") as fh:
            html = fh.read()
        self.assertIn("theme_bugatti.css", html)
        self.assertIn('id="trade-lifecycle-stepper"', html)
        self.assertIn('id="cockpit-gauge-meter"', html)
        self.assertIn('id="cockpit-confirm-modal"', html)
        self.assertIn('id="missed-body"', html)
        self.assertIn('id="calibration-metrics"', html)
        self.assertIn('id="profiles-body"', html)


if __name__ == "__main__":
    unittest.main()
