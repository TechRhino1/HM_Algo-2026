"""
Unit tests for SMT Divergence fail-safe isolation, unmapped symbol handling, and desync protection.
"""
import pytest
import pandas as pd
from unittest.mock import patch, MagicMock
from jarvis.market.smt_divergence import MetalsSMTEngine, IndicesSMTEngine, ForexSMTEngine


def test_unmapped_secondary_symbol_returns_neutral():
    """If secondary asset (Silver) is unmapped at broker, detect_smt returns neutral without error."""
    engine = MetalsSMTEngine(cache_ttl_seconds=1.0)
    
    with patch("jarvis.data.broker_symbols.resolve_broker_symbol", side_effect=lambda s: "GOLD" if s == "XAUUSD" else None):
        res = engine.detect_smt("XAUUSD")
        assert res["smt_active"] is False
        assert res["smt_type"] == "NONE"
        assert "unavailable" in res["description"].lower()


def test_desynchronized_market_closed_returns_neutral():
    """If secondary asset bars are older than 2 hours (market closed/weekend), return neutral."""
    engine = MetalsSMTEngine(cache_ttl_seconds=1.0)
    
    now_ts = 1700000000
    # Gold bars up to now
    rates_g = [{"time": now_ts - (i * 900), "high": 2700 + i, "low": 2690 - i, "close": 2695} for i in range(25)][::-1]
    # Silver bars from Friday (4 hours ago = 14400s)
    rates_s = [{"time": (now_ts - 14400) - (i * 900), "high": 31 + (i*0.1), "low": 30 - (i*0.1), "close": 30.5} for i in range(25)][::-1]
    
    mock_mt5 = MagicMock()
    mock_mt5.copy_rates_from_pos.side_effect = lambda sym, tf, start, count: rates_g if ("GOLD" in sym or "XAU" in sym) else rates_s
    
    with patch("jarvis.data.broker_symbols.resolve_broker_symbol", side_effect=lambda s: s):
        with patch.dict("sys.modules", {"MetaTrader5": mock_mt5}):
            res = engine.detect_smt("XAUUSD")
            assert res["smt_active"] is False
            assert "out of sync" in res["description"].lower()


def test_indices_unmapped_secondary_failsafe():
    """If secondary index (NAS100) is unmapped, IndicesSMTEngine degrades to neutral."""
    engine = IndicesSMTEngine(cache_ttl_seconds=1.0)
    with patch("jarvis.data.broker_symbols.resolve_broker_symbol", side_effect=lambda s: "US500" if s == "US500" else None):
        res = engine.detect_smt("US500")
        assert res["smt_active"] is False
        assert res["smt_type"] == "NONE"


def test_forex_desync_failsafe():
    """If GBPUSD quotes are desynchronized (> 2h), ForexSMTEngine degrades to neutral."""
    engine = ForexSMTEngine(cache_ttl_seconds=1.0)
    now_ts = 1700000000
    rates_eur = [{"time": now_ts - (i * 900), "high": 1.08, "low": 1.07, "close": 1.075} for i in range(25)][::-1]
    rates_gbp = [{"time": (now_ts - 10000) - (i * 900), "high": 1.28, "low": 1.27, "close": 1.275} for i in range(25)][::-1]
    
    mock_mt5 = MagicMock()
    mock_mt5.copy_rates_from_pos.side_effect = lambda sym, tf, start, count: rates_eur if "EUR" in sym else rates_gbp
    
    with patch("jarvis.data.broker_symbols.resolve_broker_symbol", side_effect=lambda s: s):
        with patch.dict("sys.modules", {"MetaTrader5": mock_mt5}):
            res = engine.detect_smt("EURUSD")
            assert res["smt_active"] is False
            assert "out of sync" in res["description"].lower()
