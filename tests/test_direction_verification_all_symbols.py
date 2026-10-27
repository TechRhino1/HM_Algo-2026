"""
Universal Opposite-Direction Invalidation & Anti-Counter-Flush Verification across ALL Symbols.
Asset Classes: Commodities, Cryptos, Forex Majors, Equity Indices.
"""
import pytest
from datetime import datetime, timezone
from jarvis.data.schemas import (
    MarketContext,
    StructureContext,
    LiquidityContext,
    VolatilityContext,
    MomentumContext,
    SessionContext,
    RegimeOutput,
    MarketRegime,
    DecisionObject,
    TradeQualityGateResult
)
from jarvis.intelligence.decision_engine import DecisionEngine
from jarvis.intelligence.opportunity_arbiter import UniversalOpportunityArbiter

TEST_SYMBOLS = [
    # Commodities
    "XAUUSD", "XAGUSD", "WTI",
    # Cryptos
    "BTCUSD", "ETHUSD", "SOLUSD",
    # Forex Majors
    "EURUSD", "GBPUSD", "USDJPY",
    # Equity Indices
    "US500", "NAS100", "US30", "GER40"
]


def _make_macro_context(symbol: str, htf_bias: str, m15_bias: str, m5_bias: str, trend_score: float, zone: str):
    return MarketContext(
        symbol=symbol,
        timestamp=datetime.now(timezone.utc),
        current_price=100.0,
        bid=99.9,
        ask=100.1,
        structure=StructureContext(
            bias=m15_bias,
            discount_premium_zone=zone,
            bos=False,
            choch=False
        ),
        liquidity=LiquidityContext(
            sweep_detected=True,
            sweep_type="BULLISH_SWEEP" if zone == "DISCOUNT" else "BEARISH_SWEEP"
        ),
        volatility=VolatilityContext(atr=1.5, current_spread_pips=1.0),
        momentum=MomentumContext(trend_score=trend_score, adx=30.0),
        session=SessionContext(current_session="LONDON", is_prime_session=True),
        mtf_alignment={
            "D1": htf_bias,
            "H4": htf_bias,
            "H1": htf_bias,
            "M15": m15_bias,
            "M5": m5_bias
        },
        mtf_confluence_score=-50.0 if "BEAR" in htf_bias else 50.0
    )


@pytest.mark.parametrize("symbol", TEST_SYMBOLS)
def test_all_symbols_never_buy_in_bearish_macro_trend(symbol):
    """In an agreed bearish macro trend, system must NEVER return BUY across any symbol or style."""
    engine = DecisionEngine()
    ctx = _make_macro_context(symbol, htf_bias="BEARISH", m15_bias="BEARISH", m5_bias="BEARISH", trend_score=-45.0, zone="DISCOUNT")
    regime = RegimeOutput(primary_regime=MarketRegime.TREND_BEAR, probabilities={"TREND_BEAR": 0.9}, confidence=0.85)

    for style in ("SWING", "DAY_TRADING", "SCALP"):
        bias = engine._resolve_market_trend_and_bias(ctx, regime, {}, trade_style=style)
        assert bias != "BUY", f"{symbol} returned forbidden BUY in agreed BEARISH macro under {style}!"
        assert bias in ("SELL", "HOLD")


@pytest.mark.parametrize("symbol", TEST_SYMBOLS)
def test_all_symbols_never_sell_in_bullish_macro_trend(symbol):
    """In an agreed bullish macro trend, system must NEVER return SELL across any symbol or style."""
    engine = DecisionEngine()
    ctx = _make_macro_context(symbol, htf_bias="BULLISH", m15_bias="BULLISH", m5_bias="BULLISH", trend_score=45.0, zone="PREMIUM")
    regime = RegimeOutput(primary_regime=MarketRegime.TREND_BULL, probabilities={"TREND_BULL": 0.9}, confidence=0.85)

    for style in ("SWING", "DAY_TRADING", "SCALP"):
        bias = engine._resolve_market_trend_and_bias(ctx, regime, {}, trade_style=style)
        assert bias != "SELL", f"{symbol} returned forbidden SELL in agreed BULLISH macro under {style}!"
        assert bias in ("BUY", "HOLD")


@pytest.mark.parametrize("symbol", TEST_SYMBOLS)
def test_all_symbols_disqualify_counter_flush_in_arbiter(symbol):
    """If M15+M5 actively flush down and HTF agrees (BEARISH), any BUY candidate is 100% DISQUALIFIED in Arbiter."""
    arbiter = UniversalOpportunityArbiter()
    ctx = _make_macro_context(symbol, htf_bias="BEARISH", m15_bias="BEARISH", m5_bias="BEARISH", trend_score=-45.0, zone="DISCOUNT")

    decision = DecisionObject(
        symbol=symbol,
        timestamp=datetime.now(timezone.utc),
        regime=RegimeOutput(primary_regime=MarketRegime.TREND_BEAR, probabilities={}, confidence=0.85),
        bias="BUY",
        probabilities={"buy": 0.60, "sell": 0.40},
        strategy="LIQUIDITY_SWEEP_REVERSAL",
        entry_price=100.0,
        stop_loss=98.0,
        take_profit=106.0,
        risk_reward_ratio=3.0,
        calculated_risk_percent=1.0,
        expected_value=1.5,
        model_confidence=0.60,
        adversarial_penalty=0.0,
        invalidation_levels=[],
        bull_case=["Fading sweep"],
        bear_case=[],
        risk_factors=[],
        decision="EXECUTE",
        master_confluence_score=60.0,
        meta_label_prob=0.65,
        quality_gate=TradeQualityGateResult(passed=True, checks={})
    )

    cand = arbiter.evaluate_opportunity(decision, ctx, trade_style="DAY_TRADING")
    assert cand.is_actionable is False, f"Counter-flush BUY on {symbol} was NOT disqualified by Arbiter!"
