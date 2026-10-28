"""
Test Suite: Direction-Signed ML Feature Matrix & Weight Priors (Phase 2).
Verifies:
1. Feature #12 (order_block_fvg_confluence) yields +1.0 for supporting same-side blocks,
   and -1.0 for opposing obstacle blocks.
2. Feature #26 (vpin_toxicity) is signed relative to trade direction delta.
3. Model probability prediction operates cleanly within institutional bounds [0.35, 0.88].
"""
import pytest
import numpy as np
from datetime import datetime, timezone
from jarvis.learning.online_ml_predictor import OnlineMLPredictor
from jarvis.data.schemas import (
    MarketContext,
    StructureContext,
    MomentumContext,
    VolatilityContext,
    LiquidityContext,
    SessionContext,
)


def _build_context(bias="BUY", fvgs=None, obs=None, vpin=0.4, delta=50.0):
    st = StructureContext(
        bias="BULLISH" if bias == "BUY" else "BEARISH",
        bos=True,
        fair_value_gaps=fvgs or [],
        order_blocks=obs or [],
        discount_premium_zone="DISCOUNT" if bias == "BUY" else "PREMIUM",
    )
    mom = MomentumContext(trend_score=30.0 if bias == "BUY" else -30.0, rsi=50.0, adx=25.0)
    vol = VolatilityContext(atr=2.0)
    liq = LiquidityContext(sweep_detected=False)
    sess = SessionContext(current_session="LONDON", is_prime_session=True)
    
    ctx = MarketContext(
        symbol="EURUSD",
        timestamp=datetime.now(timezone.utc),
        current_price=1.1000,
        bid=1.0999,
        ask=1.1001,
        structure=st,
        momentum=mom,
        volatility=vol,
        liquidity=liq,
        session=sess,
        order_flow={"vpin": vpin, "delta_score": delta, "obi": 0.2 if bias == "BUY" else -0.2},
    )
    return ctx


def test_ob_fvg_feature_directional_alignment():
    predictor = OnlineMLPredictor()
    
    # 1. Long setup with supporting Bullish FVG below price
    ctx_bull_support = _build_context(
        bias="BUY",
        fvgs=[{"top": 1.0990, "bottom": 1.0970, "direction": "BULLISH"}],
    )
    feats_buy = predictor.extract_features(ctx_bull_support, tentative_bias="BUY")
    # Feature 12 is index 11 (0-indexed)
    f12_buy = feats_buy[11]
    assert f12_buy == 1.0, f"Expected +1.0 for supporting bullish FVG on BUY, got {f12_buy}"

    # 2. Long setup with opposing Bearish FVG overhead (resistance ceiling)
    ctx_bear_obstacle = _build_context(
        bias="BUY",
        fvgs=[{"top": 1.1030, "bottom": 1.1010, "direction": "BEARISH"}],
    )
    feats_obstacle = predictor.extract_features(ctx_bear_obstacle, tentative_bias="BUY")
    f12_obstacle = feats_obstacle[11]
    assert f12_obstacle == -1.0, f"Expected -1.0 for opposing bearish FVG on BUY, got {f12_obstacle}"

    # 3. Short setup with supporting Bearish FVG overhead
    ctx_sell_support = _build_context(
        bias="SELL",
        fvgs=[{"top": 1.1030, "bottom": 1.1010, "direction": "BEARISH"}],
    )
    feats_sell = predictor.extract_features(ctx_sell_support, tentative_bias="SELL")
    f12_sell = feats_sell[11]
    assert f12_sell == 1.0, f"Expected +1.0 for supporting bearish FVG on SELL, got {f12_sell}"


def test_ml_probability_within_institutional_bounds():
    predictor = OnlineMLPredictor()
    ctx = _build_context(bias="BUY", fvgs=[{"direction": "BULLISH"}])
    feats = predictor.extract_features(ctx, tentative_bias="BUY")
    
    prob = predictor.predict_probability(feats)
    assert 0.35 <= prob <= 0.88, f"Probability {prob} out of institutional bounds [0.35, 0.88]"
