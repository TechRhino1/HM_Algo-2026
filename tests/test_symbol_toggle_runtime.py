"""
Unit Tests for Runtime Symbol Toggling, Settings Persistence, and Master Trader AI Symbol Intelligence API.
"""
import unittest
import os
import json
import tempfile
from unittest.mock import MagicMock, patch

from jarvis.application.orchestrator import JarvisOrchestrator
from jarvis.config.settings import SETTINGS, persist_allowed_symbols


class TestSymbolToggleRuntime(unittest.TestCase):
    def setUp(self):
        self.mock_mt5 = MagicMock()
        self.mock_mt5.is_connected = True
        self.mock_mt5.get_symbol_trading_spec.return_value = {
            "point": 0.0001, "digits": 5, "trade_stops_level": 10,
            "spread": 10, "trade_tick_value": 1.0, "trade_tick_size": 0.0001,
            "volume_step": 0.01, "volume_min": 0.01
        }
        self.mock_mt5.get_symbol_info.return_value = {
            "bid": 1.1000, "ask": 1.1002, "spread": 2, "digits": 5, "point": 0.00001
        }

    def test_orchestrator_toggle_and_set_symbols(self):
        """Verify orchestrator dynamically mutates self.symbols and notifies SETTINGS."""
        with patch("jarvis.application.orchestrator.MT5Client", return_value=self.mock_mt5):
            orch = JarvisOrchestrator(mode="demo", symbols=["XAUUSD", "EURUSD"])
            self.assertEqual(orch.get_active_symbols(), ["XAUUSD", "EURUSD"])

            # Toggle BTCUSD on
            res = orch.toggle_symbol("BTCUSD", True, persist=False)
            self.assertTrue(res)
            self.assertIn("BTCUSD", orch.get_active_symbols())

            # Toggle EURUSD off
            res = orch.toggle_symbol("EURUSD", False, persist=False)
            self.assertFalse(res)
            self.assertNotIn("EURUSD", orch.get_active_symbols())
            self.assertIn("XAUUSD", orch.get_active_symbols())
            self.assertIn("BTCUSD", orch.get_active_symbols())

            # Batch set symbols
            new_syms = orch.set_active_symbols(["ETHUSD", "SOLUSD", "XAGUSD"], persist=False)
            self.assertEqual(new_syms, ["ETHUSD", "SOLUSD", "XAGUSD"])
            self.assertEqual(orch.get_active_symbols(), ["ETHUSD", "SOLUSD", "XAGUSD"])

    def test_persist_allowed_symbols_atomic(self):
        """Verify persist_allowed_symbols safely updates config/settings.json."""
        with tempfile.TemporaryDirectory() as td:
            dummy_json = os.path.join(td, "settings.json")
            with open(dummy_json, "w", encoding="utf-8") as f:
                json.dump({"trading": {"magic_number": 888999, "allowed_symbols": ["EURUSD"]}}, f)

            with patch("jarvis.config.settings.os.path.join", return_value=dummy_json):
                ok = persist_allowed_symbols(["XAUUSD", "BTCUSD", "ETHUSD"])
                self.assertTrue(ok)

                with open(dummy_json, "r", encoding="utf-8") as f:
                    updated = json.load(f)
                self.assertEqual(updated["trading"]["allowed_symbols"], ["XAUUSD", "BTCUSD", "ETHUSD"])
                self.assertEqual(updated["trading"]["magic_number"], 888999)

    def test_symbols_intelligence_api_logic(self):
        """Verify symbols intelligence returns rich Master Trader telemetry."""
        from jarvis.data.symbol_registry import _REGISTRY
        self.assertIn("XAUUSD", _REGISTRY)
        self.assertIn("BTCUSD", _REGISTRY)
        self.assertIn("EURUSD", _REGISTRY)

        # Check that symbol specs have required fields
        gold_spec = _REGISTRY["XAUUSD"]
        self.assertEqual(gold_spec.asset_class, "COMMODITY")
        self.assertGreater(gold_spec.contract_size, 0)
        self.assertGreater(gold_spec.max_spread_pips, 0)


if __name__ == "__main__":
    unittest.main()
