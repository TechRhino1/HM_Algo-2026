"""
Tests for Advanced ML Models, Dynamic Volatility Risk Management, and Order Book / Flow Analysis.
Covers:
1. InstitutionalVolumeOrderFlowEngine: OBI, Micro-Price, CVD Divergence, and VPIN Toxicity.
2. VolatilityEngine: Parkinson & Garman-Klass Volatility, Volatility Ratio, and Circuit Breaker.
3. DynamicRiskAndLevelsEngine: Volatility-Adaptive Anti-Wick Buffer Scaling.
4. OnlineMLPredictor: 28-D Feature Extraction, AdaGrad Optimization, 24-D to 28-D Migration, and Calibrated Inference.
5. MT5Client: Market Depth API Interface.
"""
import pytest
import numpy as np
import pandas as pd
from datetime import datetime, timezone

from jarvis.data.schemas import (
    MarketContext,
    RegimeOutput,
    MarketRegime,
    StructureContext,
    MomentumContext,
    VolatilityContext,
    LiquidityContext,
    SessionContext,
)
from jarvis.intelligence.order_flow import InstitutionalVolumeOrderFlowEngine
from jarvis.market.volatility import VolatilityEngine
from jarvis.intelligence.dynamic_levels import DynamicRiskAndLevelsEngine
from jarvis.learning.online_ml_predictor import OnlineMLPredictor
from jarvis.execution.mt5_client import MT5Client


def test_order_book_imbalance_and_micro_price():
    """Verify Level 2 DOM Order Book Imbalance (OBI) and micro-price calculation."""
    engine = InstitutionalVolumeOrderFlowEngine()

    # Synthetic DOM: bids sum = 15.0, asks sum = 5.0 -> OBI = (15 - 5) / 20 = +0.50
    market_depth = [
        {"type": "BUY", "price": 2650.00, "volume": 10.0},
        {"type": "BUY", "price": 2649.50, "volume": 5.0},
        {"type": "SELL", "price": 2650.50, "volume": 3.0},
        {"type": "SELL", "price": 2651.00, "volume": 2.0},
    ]

    res = engine.compute_order_book_imbalance(market_depth, top_k=5)
    assert res["obi"] == pytest.approx(0.50, abs=0.01)
    assert res["bid_depth"] == 15.0
    assert res["ask_depth"] == 5.0
    # Micro-price = (2650.50 * 15 + 2650.00 * 5) / 20 = (39757.5 + 13250) / 20 = 2650.375
    assert res["micro_price"] == pytest.approx(2650.375, abs=0.01)

    # Empty DOM fallback
    empty_res = engine.compute_order_book_imbalance([])
    assert empty_res["obi"] == 0.0
    assert empty_res["micro_price"] == 0.0


def test_vpin_and_cvd_divergence():
    """Verify Volume-Synchronized Probability of Toxicity (VPIN) and CVD Divergence detection."""
    engine = InstitutionalVolumeOrderFlowEngine()

    # VPIN computation: balanced vs toxic
    buy_vols_balanced = np.array([10.0, 10.0, 10.0, 10.0])
    sell_vols_balanced = np.array([10.0, 10.0, 10.0, 10.0])
    vpin_low = engine.compute_vpin(buy_vols_balanced, sell_vols_balanced)
    assert vpin_low == pytest.approx(0.0, abs=0.01)

    buy_vols_toxic = np.array([100.0, 90.0, 95.0, 100.0])
    sell_vols_toxic = np.array([5.0, 5.0, 5.0, 5.0])
    vpin_high = engine.compute_vpin(buy_vols_toxic, sell_vols_toxic)
    assert vpin_high > 0.80

    # Bullish CVD Divergence (Price drops, but CVD rises - Passive Institutional Absorption)
    closes = np.array([100.0, 98.0, 96.0, 94.0, 92.0, 90.0, 89.0, 88.0, 87.0, 85.0])
    cum_deltas = np.array([-10.0, -15.0, -20.0, -15.0, -10.0, -5.0, 5.0, 15.0, 25.0, 40.0])
    div = engine.detect_cvd_divergence(closes, cum_deltas)
    assert div == "BULLISH_DIVERGENCE"

    # Bearish CVD Divergence (Price rises, but CVD falls - Exhaustion / Supply Wall)
    closes_bull = np.array([80.0, 82.0, 85.0, 88.0, 90.0, 92.0, 94.0, 96.0, 98.0, 100.0])
    cum_deltas_bear = np.array([50.0, 60.0, 70.0, 50.0, 40.0, 30.0, 20.0, 10.0, 0.0, -10.0])
    div_bear = engine.detect_cvd_divergence(closes_bull, cum_deltas_bear)
    assert div_bear == "BEARISH_DIVERGENCE"


def test_order_flow_full_pipeline_with_dom():
    """Verify analyze_order_flow output structure with Level 2 DOM and footprint delta."""
    engine = InstitutionalVolumeOrderFlowEngine(volume_ma_period=10)

    # Generate 15 synthetic bars
    data = {
        "open": [100.0 + i for i in range(15)],
        "high": [102.0 + i for i in range(15)],
        "low": [99.0 + i for i in range(15)],
        "close": [101.0 + i for i in range(15)],
        "volume": [1000.0 for _ in range(15)]
    }
    df = pd.DataFrame(data)
    market_depth = [
        {"type": "BUY", "price": 115.0, "volume": 500.0},
        {"type": "SELL", "price": 116.0, "volume": 100.0}
    ]

    res = engine.analyze_order_flow(df, market_depth=market_depth)
    assert "obi" in res
    assert "micro_price" in res
    assert "cvd_divergence" in res
    assert "vpin" in res
    assert "toxic_flow_detected" in res
    assert res["obi"] > 0.50  # 500 vs 100
    assert res["micro_price"] > 0.0


def test_parkinson_and_garman_klass_volatility():
    """Verify Parkinson and Garman-Klass volatility mathematical properties."""
    engine = VolatilityEngine(atr_period=14, bb_period=20)

    # 30 bars with known ranges
    np.random.seed(42)
    n = 30
    opens = np.full(n, 100.0)
    highs = np.full(n, 102.0)
    lows = np.full(n, 98.0)
    closes = np.full(n, 101.0)

    df = pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes})

    ctx = engine.analyze_volatility(df, current_spread_pips=1.5, max_allowed_spread_pips=10.0)
    assert ctx.parkinson_volatility > 0.0
    assert ctx.garman_klass_volatility > 0.0
    assert ctx.volatility_ratio >= 0.0
    assert ctx.volatility_spike_breaker is False
    assert ctx.state in ("COMPRESSION", "NORMAL", "EXPANSION", "EXTREME")

    # Extreme Volatility Spike (> 3.0x baseline)
    highs_spike = np.full(n, 102.0)
    lows_spike = np.full(n, 98.0)
    # Inject 4x expansion in the final bars
    highs_spike[-5:] = 120.0
    lows_spike[-5:] = 80.0
    df_spike = pd.DataFrame({"open": opens, "high": highs_spike, "low": lows_spike, "close": closes})

    ctx_spike = engine.analyze_volatility(df_spike, current_spread_pips=1.5, max_allowed_spread_pips=10.0)
    assert ctx_spike.volatility_spike_breaker is True
    assert ctx_spike.state == "EXTREME"


def test_dynamic_levels_anti_wick_volatility_scaling():
    """Verify dynamic anti-wick buffer expansion under volatility ratio > 1.3."""
    engine = DynamicRiskAndLevelsEngine()

    ctx = MarketContext(
        timestamp=datetime.now(timezone.utc),
        symbol="XAUUSD",
        current_price=2650.0,
        bid=2649.9,
        ask=2650.1,
        structure=StructureContext(bias="BULLISH", demand_zone=(2630.0, 2635.0)),
        volatility=VolatilityContext(
            atr=10.0,
            state="EXPANSION",
            volatility_ratio=1.60,  # 1.6x volatility expansion
            current_spread_pips=2.0
        ),
        momentum=MomentumContext(trend_score=65, adx=30.0),
        liquidity=LiquidityContext(),
        session=SessionContext(is_prime_session=True)
    )

    regime = RegimeOutput(
        primary_regime=MarketRegime.TREND_BULL,
        probabilities={"TREND_BULL": 0.85},
        confidence=0.85
    )

    levels = engine.calculate_levels(
        context=ctx,
        regime=regime,
        tentative_bias="BUY",
        account_balance=10000.0,
        risk_per_trade_pct=0.5
    )

    assert levels["bias"] == "BUY"
    assert levels["entry_price"] > 0
    assert levels["sl_price"] < levels["entry_price"]
    assert levels["tp_price"] > levels["entry_price"]
    # Stop distance must incorporate the widened anti-wick buffer under 1.6x volatility expansion
    risk_dist = levels["risk_dist"]
    assert risk_dist > 5.0  # Properly buffered beyond basic spread


def test_online_ml_predictor_28d_features_and_adagrad():
    """Verify 28-D feature extraction, AdaGrad parameter learning, and probability bounds."""
    predictor = OnlineMLPredictor()
    assert predictor.n_features == 28
    assert len(predictor.FEATURE_NAMES) == 28
    assert len(predictor.weights) == 28

    ctx = MarketContext(
        timestamp=datetime.now(timezone.utc),
        symbol="EURUSD",
        current_price=1.0850,
        bid=1.0849,
        ask=1.0851,
        structure=StructureContext(bias="BULLISH", bos=True),
        volatility=VolatilityContext(atr=0.0040, state="NORMAL", volatility_ratio=1.05),
        momentum=MomentumContext(trend_score=50, adx=25.0, rsi=55.0),
        liquidity=LiquidityContext(sweep_detected=True, sweep_type="BULLISH_SWEEP"),
        session=SessionContext(is_prime_session=True)
    )
    # Attach order flow dictionary with the new features
    ctx.order_flow = {
        "obi": 0.45,
        "vpin": 0.35,
        "cvd_divergence": "BULLISH_DIVERGENCE",
        "delta_score": 40.0
    }

    regime = RegimeOutput(
        primary_regime=MarketRegime.TREND_BULL,
        probabilities={"TREND_BULL": 0.80},
        confidence=0.80
    )

    # 1. Feature extraction
    feats = predictor.extract_feature_vector(context=ctx, regime=regime, tentative_bias="BUY")
    assert len(feats) == 28
    # Feature 25: OBI aligned (+0.45 for BUY)
    assert feats[24] == pytest.approx(0.45, abs=0.01)
    # Feature 26: VPIN (0.35)
    assert feats[25] == pytest.approx(0.35, abs=0.01)
    # Feature 27: Vol ratio normalized (1.05 - 1.0 = +0.05)
    assert feats[26] == pytest.approx(0.05, abs=0.01)
    # Feature 28: CVD Divergence signal (+1.0 for bullish aligned with BUY)
    assert feats[27] == 1.0

    # 2. Probability prediction
    prob = predictor.predict_probability(feats)
    assert 0.35 <= prob <= 0.88

    # 3. AdaGrad Online SGD update
    initial_weights = predictor.weights.copy()
    predictor.update_online(feats, target_win=1, r_multiple=2.0)
    predictor.update_online(feats, target_win=1, r_multiple=2.5)
    predictor.update_online(feats, target_win=1, r_multiple=1.8)
    # Weights should adapt smoothly
    assert predictor.weights.shape == (28,)
    assert not np.array_equal(predictor.weights, initial_weights)
    assert predictor._grad_accum_w.shape == (28,)
    assert np.all(predictor._grad_accum_w >= 0.0)


def test_online_ml_predictor_24d_to_28d_migration(tmp_path):
    """Verify seamless migration from older 24-D weights on disk to the new 28-D architecture."""
    import json

    test_model_file = str(tmp_path / "test_ml_weights.json")
    old_24_weights = [0.1 * i for i in range(24)]
    old_data = {
        "weights": old_24_weights,
        "bias": 0.35,
        "training_steps": 42,
        "n_features": 24
    }
    with open(test_model_file, "w") as f:
        json.dump(old_data, f)

    # Instantiate predictor with the 24-D file
    predictor = OnlineMLPredictor(model_file=test_model_file)
    assert predictor.n_features == 28
    assert len(predictor.weights) == 28
    # The first 24 weights must match the loaded weights
    assert np.allclose(predictor.weights[:24], old_24_weights)
    # The last 4 weights must be initialised with default prior weights
    assert np.allclose(predictor.weights[24:], predictor.DEFAULT_WEIGHTS[24:])
    assert predictor.bias == pytest.approx(0.35)
    assert predictor.training_steps == 42
    assert predictor.weights_source == "migrated"


def test_mt5_client_market_depth_interface():
    """Verify MT5Client get_market_depth and release_market_depth interface."""
    client = MT5Client(mode="paper")
    depth = client.get_market_depth("EURUSD")
    # Paper mode returns None gracefully without throwing exceptions
    assert depth is None

    released = client.release_market_depth("EURUSD")
    assert released is True
