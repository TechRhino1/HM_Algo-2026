"""
Test Suite: ICT FVG & OTE Geometry Engine (Phase 1).
Verifies:
1. True ICT OTE Fibonacci Golden Pocket calculation ([0.618, 0.786]) with sweet spot (0.705).
2. Directional assignment for Bullish pullback OTE and Bearish retracement OTE.
3. Consequent Encroachment (50% CE midpoint) on Fair Value Gaps.
4. Directional separation: price_in_bullish_fvg vs price_in_bearish_fvg.
"""
import pytest
import pandas as pd
import numpy as np
from jarvis.market.fair_value_gap import FairValueGapEngine


def test_ote_geometry_bullish_pullback():
    engine = FairValueGapEngine()
    
    # Upward impulse leg: prices trend up from 100 to 200
    n = 60
    closes = np.linspace(110, 190, n)
    highs = closes + 2.0
    lows = closes - 2.0
    opens = closes.copy()

    # Clear swing low at index 15
    lows[15] = 95.0
    highs[15] = 100.0

    # Clear swing high at index 45 (more recent than swing low)
    highs[45] = 205.0
    lows[45] = 200.0

    # Swing Low = 95.0, Swing High = 205.0. Range = 110.0
    # OTE 61.8% = 205 - 0.618 * 110 = 137.02
    # OTE 70.5% = 205 - 0.705 * 110 = 127.45
    # OTE 78.6% = 205 - 0.786 * 110 = 118.54
    # Current price at bar 59 is 135.0 (inside OTE zone [118.54, 137.02])
    closes[-1] = 135.0
    highs[-1] = 137.0
    lows[-1] = 134.0
    opens[-1] = 135.0

    df = pd.DataFrame({'open': opens, 'high': highs, 'low': lows, 'close': closes})
    res = engine.analyze(df)

    ote = res.get('ote_zone', {})
    assert ote.get('direction') == 'BULLISH', f"Expected BULLISH OTE, got {ote.get('direction')}"
    assert pytest.approx(ote.get('ote_62'), 0.1) == 137.02
    assert pytest.approx(ote.get('ote_705'), 0.1) == 127.45
    assert pytest.approx(ote.get('ote_79'), 0.1) == 118.54
    assert res.get('is_price_in_ote') is True


def test_ote_geometry_bearish_retracement():
    engine = FairValueGapEngine()
    
    # Downward impulse leg: prices trend down from 200 to 100
    n = 60
    closes = np.linspace(190, 110, n)
    highs = closes + 2.0
    lows = closes - 2.0
    opens = closes.copy()

    # Clear swing high at index 15
    highs[15] = 205.0
    lows[15] = 200.0

    # Clear swing low at index 45 (more recent than swing high)
    lows[45] = 95.0
    highs[45] = 100.0

    # Swing High = 205.0, Swing Low = 95.0. Range = 110.0
    # OTE 61.8% = 95 + 0.618 * 110 = 162.98
    # OTE 70.5% = 95 + 0.705 * 110 = 172.55
    # OTE 78.6% = 95 + 0.786 * 110 = 181.46
    # Current price at bar 59 is 165.0 (inside OTE zone [162.98, 181.46])
    closes[-1] = 165.0
    highs[-1] = 167.0
    lows[-1] = 164.0
    opens[-1] = 165.0

    df = pd.DataFrame({'open': opens, 'high': highs, 'low': lows, 'close': closes})
    res = engine.analyze(df)

    ote = res.get('ote_zone', {})
    assert ote.get('direction') == 'BEARISH', f"Expected BEARISH OTE, got {ote.get('direction')}"
    assert pytest.approx(ote.get('ote_62'), 0.1) == 162.98
    assert pytest.approx(ote.get('ote_705'), 0.1) == 172.55
    assert pytest.approx(ote.get('ote_79'), 0.1) == 181.46
    assert res.get('is_price_in_ote') is True


def test_fvg_displacement_and_consequent_encroachment():
    engine = FairValueGapEngine()
    
    n = 20
    closes = np.full(n, 105.0)
    highs = np.full(n, 106.0)
    lows = np.full(n, 104.0)
    opens = np.full(n, 105.0)

    # Candle 10: high = 102.0
    highs[10] = 102.0
    lows[10] = 98.0
    opens[10] = 99.0
    closes[10] = 101.0

    # Candle 11 (Displacement candle): huge bullish impulse
    opens[11] = 102.0
    closes[11] = 120.0
    highs[11] = 121.0
    lows[11] = 101.5

    # Candle 12: low = 108.0 (Gap between c10 high 102.0 and c12 low 108.0 = 6.0 pts)
    opens[12] = 115.0
    closes[12] = 118.0
    highs[12] = 122.0
    lows[12] = 108.0

    # Current price at bar 19 is 105.0 (inside unmitigated FVG [102.0, 108.0], CE midpoint is 105.0)
    closes[-1] = 105.0
    highs[-1] = 106.0
    lows[-1] = 104.0
    opens[-1] = 105.0

    df = pd.DataFrame({'open': opens, 'high': highs, 'low': lows, 'close': closes})
    res = engine.analyze(df)

    assert len(res['bullish_fvgs']) > 0
    fvg = res['bullish_fvgs'][-1]
    assert fvg['top'] == 108.0
    assert fvg['bottom'] == 102.0
    assert fvg['ce'] == 105.0  # 50% midpoint
    assert res['price_in_bullish_fvg'] is True
    assert res['price_in_bearish_fvg'] is False
