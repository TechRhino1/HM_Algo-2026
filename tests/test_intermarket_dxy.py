"""
Unit tests for SyntheticDollarIndexEngine and IntermarketDXYReport.
"""
import pytest
import pandas as pd
import numpy as np
from jarvis.market.intermarket import (
    SyntheticDollarIndexEngine,
    IntermarketDXYReport,
    get_intermarket_dxy_report
)


def test_dxy_instant_calculation():
    """Verify standard formula yields reasonable DXY level (100 - 110 range)."""
    prices = {
        "EURUSD": 1.0820,
        "USDJPY": 152.50,
        "GBPUSD": 1.2950,
        "USDCAD": 1.3850,
        "USDCHF": 0.8650,
    }
    dxy = SyntheticDollarIndexEngine.calculate_dxy_instant(prices)
    assert dxy is not None
    assert 95.0 < dxy < 115.0, f"Expected realistic DXY, got {dxy}"


def test_dxy_instant_missing_data():
    """Verify graceful handling when key pairs are missing."""
    assert SyntheticDollarIndexEngine.calculate_dxy_instant({}) is None
    assert SyntheticDollarIndexEngine.calculate_dxy_instant({"EURUSD": 0.0}) is None
    assert SyntheticDollarIndexEngine.calculate_dxy_instant({"USDJPY": 150.0}) is None


def test_dxy_series_construction():
    """Verify series construction from synthetic price frames."""
    n = 30
    dates = pd.date_range("2026-01-01", periods=n, freq="1h")
    frames = {
        "EURUSD": pd.DataFrame({"time": dates, "close": np.linspace(1.10, 1.05, n)}),
        "USDJPY": pd.DataFrame({"time": dates, "close": np.linspace(150.0, 155.0, n)}),
        "GBPUSD": pd.DataFrame({"time": dates, "close": np.linspace(1.30, 1.25, n)}),
        "USDCAD": pd.DataFrame({"time": dates, "close": np.linspace(1.35, 1.39, n)}),
        "USDCHF": pd.DataFrame({"time": dates, "close": np.linspace(0.85, 0.88, n)}),
    }

    engine = SyntheticDollarIndexEngine(cache_ttl_seconds=5.0)
    dxy_df = engine.get_dxy_series_from_frames(frames)
    assert not dxy_df.empty
    assert len(dxy_df) == n
    assert "close" in dxy_df.columns
    # With EUR falling and USDJPY rising, DXY should rise
    assert dxy_df["close"].iloc[-1] > dxy_df["close"].iloc[0]


def test_intermarket_caching():
    """Verify that repeated calls within TTL return cached object."""
    engine = SyntheticDollarIndexEngine(cache_ttl_seconds=10.0)
    rep1 = engine.analyze()
    rep2 = engine.analyze()
    assert rep1 is rep2


def test_intermarket_dxy_report_to_dict():
    """Verify dictionary serialization for telemetry/dashboards."""
    rep = IntermarketDXYReport(
        dxy_value=104.5,
        trend="BULLISH",
        momentum_score=45.0,
        gold_impact="OPPOSES_BUY",
        gold_penalty=15.0,
        is_live=True
    )
    d = rep.to_dict()
    assert d["dxy_value"] == 104.5
    assert d["trend"] == "BULLISH"
    assert d["gold_impact"] == "OPPOSES_BUY"
    assert d["gold_penalty"] == 15.0
    assert d["is_live"] is True
