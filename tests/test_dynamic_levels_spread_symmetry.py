"""Spread symmetry between the BUY and SELL stop branches of DynamicRiskAndLevelsEngine.

`calculate_levels` prices the stop from the entry, and the entry is the side of the
book the order crosses: a BUY pays the ask, a SELL the bid, and either stop fills on
the far side. So ``spread_dist`` belongs in the stop distance in BOTH directions.

The SELL branch added ``spread_dist`` at every site (the structural distance, the
fallback structural distance, the SCALP/DAY caps and the SWING cap). The BUY branch
did not, which made every BUY stop systematically one spread tighter than the
mirrored SELL stop for identical structure — a directional asymmetry produced by an
inconsistency, not by any trading view.

These tests pin the symmetry and would fail on the pre-fix engine, where the BUY
risk distance came out exactly ``spread_dist`` short of the SELL risk distance.
"""
from datetime import datetime, timezone

import pytest

from jarvis.data.schemas import (
    MarketContext,
    StructureContext,
    LiquidityContext,
    VolatilityContext,
    MomentumContext,
    SessionContext,
    RegimeOutput,
    MarketRegime,
)
from jarvis.data.symbol_registry import resolve as resolve_symbol
from jarvis.intelligence.dynamic_levels import DynamicRiskAndLevelsEngine


ENGINE = DynamicRiskAndLevelsEngine()

# (symbol, mid price, spread in pips, ATR as a fraction of price, anchor gap in ATRs)
#
# One row per asset class the branch special-cases (forex / gold-commodity / index /
# crypto) so the symmetry is proven on every code path the BUY branch takes.
CASES = [
    ("EURUSD", 1.08500, 1.0, 0.0040, 1.2),
    ("USDJPY", 149.500, 1.0, 0.0040, 1.2),
    ("XAUUSD", 2400.00, 2.0, 0.0080, 1.2),
    ("US500", 5200.00, 0.6, 0.0080, 1.2),
    ("BTCUSD", 65000.0, 1500.0, 0.0250, 1.2),
]

STYLES = ["SWING", "DAY_TRADING", "SCALP"]


def _regime(bias: str) -> RegimeOutput:
    regime = MarketRegime.TREND_BULL if bias == "BUY" else MarketRegime.TREND_BEAR
    return RegimeOutput(primary_regime=regime, probabilities={}, confidence=0.85)


def _mirrored_context(symbol: str, mid: float, spread_pips: float, atr_frac: float,
                      gap_atrs: float, bias: str) -> MarketContext:
    """A BUY and a SELL setup whose entry-to-anchor distance is identical.

    ``anchor_buy = bid - gap`` and ``anchor_sell = ask + gap`` put the structural
    anchor exactly ``gap`` away from the price the order actually crosses, so the
    two directions describe the same structure and any difference in the resulting
    stop distance is the engine's, not the fixture's.
    """
    spec = resolve_symbol(symbol)
    pip = float(spec.pip_size)
    spread = spread_pips * pip
    bid = mid - spread / 2.0
    ask = mid + spread / 2.0
    atr = mid * atr_frac
    gap = gap_atrs * atr

    if bias == "BUY":
        structure = StructureContext(
            bias="BULLISH",
            demand_zone=(bid - gap, bid - gap + 0.2 * atr),
            order_blocks=[{"type": "BULLISH_ORDER_BLOCK", "low": bid - gap, "high": bid - gap + atr}],
        )
        momentum = MomentumContext(trend_score=60.0, adx=30.0)
    else:
        structure = StructureContext(
            bias="BEARISH",
            supply_zone=(ask + gap - 0.2 * atr, ask + gap),
            order_blocks=[{"type": "BEARISH_ORDER_BLOCK", "high": ask + gap, "low": ask + gap - atr}],
        )
        momentum = MomentumContext(trend_score=-60.0, adx=30.0)

    return MarketContext(
        symbol=symbol,
        timestamp=datetime.now(timezone.utc),
        current_price=mid,
        bid=bid,
        ask=ask,
        structure=structure,
        liquidity=LiquidityContext(),
        volatility=VolatilityContext(atr=atr, current_spread_pips=spread_pips, state="NORMAL"),
        momentum=momentum,
        session=SessionContext(is_prime_session=True),
    )


def _levels(symbol, mid, spread_pips, atr_frac, gap_atrs, bias, style):
    ctx = _mirrored_context(symbol, mid, spread_pips, atr_frac, gap_atrs, bias)
    return ENGINE.calculate_levels(ctx, _regime(bias), tentative_bias=bias, trade_style=style)


@pytest.mark.parametrize("symbol,mid,spread_pips,atr_frac,gap_atrs", CASES)
@pytest.mark.parametrize("style", STYLES)
def test_buy_and_sell_stops_are_symmetric_for_mirrored_structure(
    symbol, mid, spread_pips, atr_frac, gap_atrs, style
):
    """Identical structure must yield the same stop distance in both directions.

    The spread is a cost of crossing the book, not a directional view: BUY pays the
    ask and SELL the bid, and each stop fills on the opposite side, so a mirrored
    setup must produce a mirrored stop. Pre-fix this failed by exactly one spread
    for every case (BUY tighter than SELL).
    """
    digits = resolve_symbol(symbol).digits
    ulp = 10.0 ** (-int(digits))

    buy = _levels(symbol, mid, spread_pips, atr_frac, gap_atrs, "BUY", style)
    sell = _levels(symbol, mid, spread_pips, atr_frac, gap_atrs, "SELL", style)

    assert buy["bias"] == "BUY"
    assert sell["bias"] == "SELL"
    # Tolerance is a couple of price ticks: each risk distance is recovered from two
    # rounded prices, so a 1-ulp rounding difference is legitimate. The pre-fix
    # asymmetry is a full spread, i.e. orders of magnitude larger than this.
    assert abs(buy["risk_dist"] - sell["risk_dist"]) <= 2 * ulp, (
        f"{symbol} {style}: BUY stop {buy['risk_dist']} vs SELL stop {sell['risk_dist']} "
        f"for mirrored structure — the spread is being charged to one side only"
    )


@pytest.mark.parametrize("symbol,mid,spread_pips,atr_frac,gap_atrs", CASES)
@pytest.mark.parametrize("style", STYLES)
def test_buy_stop_distance_includes_the_spread(
    symbol, mid, spread_pips, atr_frac, gap_atrs, style
):
    """The BUY stop must be at least as wide as the mirrored SELL stop.

    This is the directional half of the symmetry claim: the fix widens BUY to match
    SELL, and must never leave BUY tighter (the pre-fix state) nor overshoot SELL.
    """
    digits = resolve_symbol(symbol).digits
    ulp = 10.0 ** (-int(digits))

    buy = _levels(symbol, mid, spread_pips, atr_frac, gap_atrs, "BUY", style)
    sell = _levels(symbol, mid, spread_pips, atr_frac, gap_atrs, "SELL", style)
    assert buy["risk_dist"] >= sell["risk_dist"] - 2 * ulp


@pytest.mark.parametrize("symbol,mid,spread_pips,atr_frac,gap_atrs", CASES)
def test_stop_price_is_on_the_correct_side_of_entry(symbol, mid, spread_pips, atr_frac, gap_atrs):
    """A BUY stop sits below entry, a SELL stop above it, and risk_dist matches."""
    for style in STYLES:
        buy = _levels(symbol, mid, spread_pips, atr_frac, gap_atrs, "BUY", style)
        sell = _levels(symbol, mid, spread_pips, atr_frac, gap_atrs, "SELL", style)

        assert buy["sl_price"] < buy["entry_price"]
        assert sell["sl_price"] > sell["entry_price"]
        assert buy["tp_price"] > buy["entry_price"]
        assert sell["tp_price"] < sell["entry_price"]

        assert buy["risk_dist"] == pytest.approx(
            abs(buy["entry_price"] - buy["sl_price"]), abs=1e-8
        )
        assert sell["risk_dist"] == pytest.approx(
            abs(sell["entry_price"] - sell["sl_price"]), abs=1e-8
        )


@pytest.mark.parametrize("bias", ["BUY", "SELL"])
@pytest.mark.parametrize("style", STYLES)
def test_stop_still_respects_the_floor(bias, style):
    """A near-zero ATR must not produce a stop inside the floor.

    The floor is ``max(3.5 * spread_dist, 0.10 * ATR)`` in both branches; the symmetry
    fix widens the structural term only and must leave that floor untouched.
    """
    symbol, mid, spread_pips, atr_frac, gap_atrs = "EURUSD", 1.08500, 1.0, 0.00005, 0.05
    spec = resolve_symbol(symbol)
    spread_dist = spread_pips * spec.pip_size
    atr = mid * atr_frac
    floor = max(3.0 * spread_dist, 0.10 * atr)

    levels = _levels(symbol, mid, spread_pips, atr_frac, gap_atrs, bias, style)
    assert levels["risk_dist"] >= floor - 1e-9, (
        f"{bias} {style}: stop distance {levels['risk_dist']} fell below the floor {floor}"
    )


@pytest.mark.parametrize("bias", ["BUY", "SELL"])
def test_floor_binds_identically_in_both_directions(bias):
    """When the floor is the binding term, BUY and SELL must both sit exactly on it.

    This is the degenerate-geometry end of the symmetry claim: the fix must not have
    moved either direction off the floor.
    """
    symbol, mid, spread_pips, atr_frac, gap_atrs = "EURUSD", 1.08500, 1.0, 0.00005, 0.05
    spec = resolve_symbol(symbol)
    spread_dist = spread_pips * spec.pip_size
    floor = max(3.5 * spread_dist, 0.10 * (mid * atr_frac))

    levels = _levels(symbol, mid, spread_pips, atr_frac, gap_atrs, bias, "SWING")
    assert levels["risk_dist"] == pytest.approx(floor, abs=2e-5)
