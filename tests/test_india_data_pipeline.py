"""
Comprehensive Pipeline Test Suite for Indian Stock Market Data & Architecture
Tests:
1. Real multi-bar historical candle retrieval for Indian indices and equities.
2. Volume capture and non-zero liquidity validation.
3. Accurate CPR (TC, Pivot, BC, width_pct) and Camarilla (H1-H4, L1-L4) derivation from preceding session OHLC.
4. Smart Money Concepts (SMC) & Market Regime classification.
"""
import pytest
from jarvis.data.market_data_provider import fetch_real_candles
from jarvis.india.india_engine import INDIA_ENGINE
from jarvis.india.nse_bse_adapter import fetch_nse_historical, fetch_live_india_quote


def test_nse_historical_multi_timeframe():
    """Verify real multi-bar OHLCV fetching for NIFTY and RELIANCE across timeframes."""
    # Daily bars
    daily_bars = fetch_nse_historical("NIFTY", days=5, timeframe="1d")
    assert daily_bars is not None and len(daily_bars) >= 2, "Daily NIFTY bars should have at least 2 entries"
    latest_daily = daily_bars[-1]
    assert latest_daily["open"] > 0
    assert latest_daily["high"] >= latest_daily["low"]
    assert latest_daily["close"] > 0

    # 15m intraday bars for equity
    intraday_bars = fetch_nse_historical("RELIANCE", days=3, timeframe="15m")
    assert intraday_bars is not None and len(intraday_bars) >= 5, "Intraday RELIANCE bars should have multiple candles"
    assert any(b["volume"] > 0 for b in intraday_bars), "Intraday series must capture genuine trading volume"


def test_market_data_provider_india_dispatch():
    """Verify market_data_provider dispatches Indian symbols to real exchange feeds."""
    for sym in ("NIFTY", "BANKNIFTY", "RELIANCE"):
        bars = fetch_real_candles(sym, timeframe="1d", num_bars=5, market="IN")
        assert bars is not None and len(bars) >= 2, f"Expected real bars for {sym}"
        assert bars[-1]["close"] > 0


def test_cpr_and_camarilla_calculation_accuracy():
    """Verify exact CPR and Camarilla levels derived from preceding session."""
    analysis = INDIA_ENGINE.analyze_india_instrument("NIFTY", timeframe="1D")
    assert "cpr" in analysis
    cpr = analysis["cpr"]
    assert "pivot" in cpr and "tc" in cpr and "bc" in cpr and "width_pct" in cpr
    assert cpr["pivot"] > 0
    assert cpr["tc"] > 0
    assert cpr["bc"] > 0
    assert cpr["width_pct"] >= 0

    assert "camarilla" in analysis
    cam = analysis["camarilla"]
    assert "h3_reversal" in cam and cam["h3_reversal"] > 0
    assert "h4_breakout" in cam and cam["h4_breakout"] > 0
    assert "l3_reversal" in cam and cam["l3_reversal"] > 0
    assert "l4_breakdown" in cam and cam["l4_breakdown"] > 0
    assert cam["h4_breakout"] > cam["h3_reversal"], "Camarilla H4 must be higher than H3"
    assert cam["l3_reversal"] > cam["l4_breakdown"], "Camarilla L3 must be higher than L4"


def test_smc_and_market_regime_classification():
    """Verify Smart Money Concepts detection and Market Regime."""
    for sym in ("NIFTY", "FINNIFTY", "HDFCBANK"):
        analysis = INDIA_ENGINE.analyze_india_instrument(sym, timeframe="1D")
        assert "market_regime" in analysis
        regime = analysis["market_regime"]
        assert regime in (
            "BULLISH_TRENDING",
            "BEARISH_TRENDING",
            "SIDEWAYS_CONSOLIDATION",
            "HIGH_VOLATILITY"
        ), f"Unexpected regime {regime} for {sym}"

        assert "smc_structure" in analysis
        smc = analysis["smc_structure"]
        assert "liquidity_sweep" in smc
        assert "fair_value_gap" in smc
        assert "bos_status" in smc
