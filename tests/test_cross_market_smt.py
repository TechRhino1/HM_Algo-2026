"""
Unit tests for CrossMarketSMTEngine and SMT divergence logic.
"""
import pytest
import pandas as pd
from jarvis.market.smt_divergence import (
    _get_swings,
    _evaluate_smt_pair,
    MetalsSMTEngine,
    IndicesSMTEngine,
    ForexSMTEngine,
    CrossMarketSMTEngine,
    GLOBAL_SMT_ENGINE
)


def test_swing_detection():
    """Verify 3-bar fractal swing identification."""
    df = pd.DataFrame({
        "high": [10.0, 15.0, 12.0, 18.0, 14.0],
        "low":  [8.0,  11.0, 9.0,  13.0, 10.0]
    })
    highs, lows = _get_swings(df)
    # High at index 1 (15 > 10 and 15 >= 12), and index 3 (18 > 12 and 18 >= 14)
    assert len(highs) == 2
    assert highs[0] == (1, 15.0)
    assert highs[1] == (3, 18.0)
    # Low at index 2 (9 < 11 and 9 <= 13)
    assert len(lows) == 1
    assert lows[0] == (2, 9.0)


def test_bullish_smt_evaluation():
    """Verify accumulation SMT: Asset A Lower Low, Asset B Higher Low."""
    highs_a = [(1, 100.0), (3, 102.0)]
    lows_a  = [(2, 95.0),  (4, 92.0)]   # Lower Low (92 < 95)

    highs_b = [(1, 50.0),  (3, 51.0)]
    lows_b  = [(2, 45.0),  (4, 47.0)]   # Higher Low (47 > 45) -> SMT!

    smt_type, leader, bonus, desc = _evaluate_smt_pair(
        highs_a, lows_a, highs_b, lows_b, "Gold", "Silver"
    )
    assert smt_type == "BULLISH_SMT"
    assert leader == "Silver"
    assert bonus == 15.0
    assert "Accumulation" in desc


def test_bearish_smt_evaluation():
    """Verify distribution SMT: Asset A Higher High, Asset B Lower High."""
    highs_a = [(1, 100.0), (3, 105.0)]  # Higher High (105 > 100)
    lows_a  = [(2, 95.0),  (4, 96.0)]

    highs_b = [(1, 50.0),  (3, 48.0)]   # Lower High (48 < 50) -> SMT!
    lows_b  = [(2, 45.0),  (4, 46.0)]

    smt_type, leader, bonus, desc = _evaluate_smt_pair(
        highs_a, lows_a, highs_b, lows_b, "US500", "NAS100"
    )
    assert smt_type == "BEARISH_SMT"
    assert leader == "NAS100"
    assert bonus == 15.0
    assert "Distribution" in desc


def test_cross_market_dispatcher():
    """Verify asset-class routing."""
    assert MetalsSMTEngine.is_metal("XAUUSD")
    assert MetalsSMTEngine.is_metal("XAGUSD")
    assert not MetalsSMTEngine.is_metal("US500")

    assert IndicesSMTEngine.is_index("US500")
    assert IndicesSMTEngine.is_index("NAS100")
    assert not IndicesSMTEngine.is_index("EURUSD")

    assert ForexSMTEngine.is_fx_major("EURUSD")
    assert ForexSMTEngine.is_fx_major("GBPUSD")
    assert not ForexSMTEngine.is_fx_major("BTCUSD")

    engine = CrossMarketSMTEngine()
    res_btc = engine.detect_smt("BTCUSD")
    assert res_btc["smt_active"] is False
    assert "No SMT pair configured" in res_btc["description"]
