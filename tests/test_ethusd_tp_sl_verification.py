import pytest
from datetime import datetime, timezone
from jarvis.intelligence.dynamic_levels import DynamicRiskAndLevelsEngine
from jarvis.data.schemas import (
    MarketContext, StructureContext, LiquidityContext,
    VolatilityContext, MomentumContext, SessionContext,
    RegimeOutput, MarketRegime
)
from jarvis.data.symbol_registry import resolve as resolve_symbol, get_dollar_risk_per_price_unit
from jarvis.risk.position_sizing import PositionSizer


def test_ethusd_symbol_specifications():
    spec = resolve_symbol("ETHUSD")
    dollar_unit = get_dollar_risk_per_price_unit("ETHUSD")
    
    assert spec is not None
    assert spec.digits == 2
    assert spec.contract_size == 1.0
    assert spec.pip_size == 0.01
    assert spec.is_crypto is True
    assert dollar_unit == 1.0


def test_ethusd_tp_sl_calculations_across_all_horizons():
    engine = DynamicRiskAndLevelsEngine()
    
    for style in ["SCALP", "DAY_TRADING", "SWING"]:
        for bias in ["BUY", "SELL"]:
            st = StructureContext(bias="BULLISH" if bias == "BUY" else "BEARISH")
            setattr(st, "swing_low", 2525.0)
            setattr(st, "swing_high", 2555.0)
            ctx = MarketContext(
                symbol="ETHUSD",
                timestamp=datetime.now(timezone.utc),
                current_price=2540.0,
                bid=2539.0,
                ask=2541.0,
                trade_style=style,
                volatility=VolatilityContext(atr=4.0, atr_percent=0.0016, current_spread_pips=200.0),
                structure=st,
                liquidity=LiquidityContext(sell_side_liquidity=2520.0, buy_side_liquidity=2560.0),
                momentum=MomentumContext(trend_score=25, adx=25.0),
                session=SessionContext(is_prime_session=True)
            )
            regime = RegimeOutput(primary_regime=MarketRegime.TREND_BULL if bias == "BUY" else MarketRegime.TREND_BEAR, probabilities={}, confidence=0.80)
            res = engine.calculate_levels(ctx, regime=regime, tentative_bias=bias, trade_style=style)
            
            entry = res["entry_price"]
            sl = res["sl_price"]
            tp = res["tp_price"]
            tp1 = res["tp1_price"]
            risk = res["risk_dist"]
            tp_dist = res["tp_dist"]
            rr = res["rr_ratio"]
            
            # 1. Structural directional sanity
            if bias == "BUY":
                assert entry > sl, f"{style} BUY entry ({entry}) must be > SL ({sl})"
                assert tp > entry, f"{style} BUY TP ({tp}) must be > entry ({entry})"
                assert tp1 > entry, f"{style} BUY TP1 ({tp1}) must be > entry ({entry})"
            else:
                assert entry < sl, f"{style} SELL entry ({entry}) must be < SL ({sl})"
                assert tp < entry, f"{style} SELL TP ({tp}) must be < entry ({entry})"
                assert tp1 < entry, f"{style} SELL TP1 ({tp1}) must be < entry ({entry})"
                
            # 2. Risk/Reward ratio must meet institutional threshold
            assert rr >= 1.50, f"{style} {bias} R:R ({rr}) is below institutional minimum 1.50"
            
            # 3. Horizon adaptive stop floor check
            if style == "SCALP":
                # SCALP floor is 10.0 ETH points
                assert risk >= 10.0, f"SCALP risk {risk} should respect min floor"
                # And should not be artificially blown up to 30.0 when ATR is small
                assert risk <= 20.0, f"SCALP risk {risk} should NOT be forced to 30.0"
            elif style == "DAY_TRADING":
                assert risk >= 20.0
            elif style == "SWING":
                assert risk >= 30.0


def test_ethusd_dollar_risk_and_lot_sizing():
    spec = resolve_symbol("ETHUSD")
    sym_eth = {
        "name": "ETHUSD#",
        "trade_contract_size": 1.0,
        "trade_tick_value": 0.01,
        "trade_tick_size": 0.01,
        "volume_min": 0.02,
        "volume_max": 100.0,
        "volume_step": 0.01,
    }
    balance = 468.51
    entry = 2540.0
    sl = 2528.0  # $12.00 stop distance on ETH
    risk_dist = abs(entry - sl)
    
    lot_size = PositionSizer.calculate_lot_size(
        account_balance=balance,
        entry_price=entry,
        sl_price=sl,
        risk_pct=0.5,
        symbol_info=sym_eth,
        model_confidence=0.75,
        pattern_sample_size=20
    )
    
    assert lot_size >= sym_eth["volume_min"], f"lot_size {lot_size} < min 0.02"
    
    # Verify dollar risk formula: lot_size * risk_dist * contract_size
    dollar_risk = lot_size * risk_dist * sym_eth["trade_contract_size"]
    assert dollar_risk > 0.0
    risk_pct = (dollar_risk / balance) * 100.0
    # On $468 balance with min lot 0.02, $12 SL risks $0.24 (0.05% of account), perfectly safe
    assert risk_pct <= 5.0
