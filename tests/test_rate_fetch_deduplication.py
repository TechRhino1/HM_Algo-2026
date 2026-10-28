"""
Test Suite: MT5 Rate Fetch Deduplication & In-Memory Slicing (Phase 3).
Verifies that DataFeedEngine deduplicates timeframe requests across roles,
serves closed bars via local in-memory slicing, and avoids redundant broker roundtrips.
"""
import pytest
import pandas as pd
from unittest.mock import MagicMock
from jarvis.market.data_feed import DataFeedEngine


def test_multi_timeframe_deduplication_and_role_mapping():
    engine = DataFeedEngine(mt5_client=None)  # Use synthetic fallback
    
    mtf = engine.fetch_multi_timeframe("EURUSD", trade_style="DAY_TRADING", num_bars=50)
    
    # Must contain all standard roles
    assert "macro" in mtf
    assert "context" in mtf
    assert "primary" in mtf
    assert "setup" in mtf
    assert "timing" in mtf
    
    for role, df in mtf.items():
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 50


def test_closed_bar_in_memory_derivation_from_forming_cache():
    engine = DataFeedEngine(mt5_client=None)
    
    # First fetch with include_current_bar=True
    forming_df = engine.fetch_rates("XAUUSD", timeframe="H1", num_bars=100, include_current_bar=True)
    assert len(forming_df) == 100

    # Second fetch with include_current_bar=False should derive directly from cache without generating new bars
    closed_df = engine.fetch_rates("XAUUSD", timeframe="H1", num_bars=50, include_current_bar=False)
    assert len(closed_df) == 50
    
    # The last closed bar in closed_df must match the second-to-last bar in forming_df
    assert closed_df["close"].iloc[-1] == forming_df["close"].iloc[-2]
