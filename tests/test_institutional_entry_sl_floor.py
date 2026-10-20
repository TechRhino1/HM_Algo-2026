"""
Regression tests for the stop-distance floor in InstitutionalEntryEngine.

Bug: `risk_dist` was lifted to the `pip_size * 5` floor while `sl_price` kept the
tighter structural stop, so the two described different levels (sizing priced risk
off one number, the actual stop sat elsewhere, and R:R was derived from a stale
distance). The floor is now applied to the stop DISTANCE and `sl_price` is derived
from it, on every branch (structural + re-enforce), for BUY and SELL, across the
SCALP, DAY_TRADING and SWING protocols.

Invariant pinned here: `risk_dist == abs(entry_price - sl_price)` and
`risk_dist >= pip_size * 5` on every path.
"""
import unittest
from unittest.mock import patch
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from jarvis.data.schemas import (
    MarketContext, StructureContext, LiquidityContext,
    VolatilityContext, MomentumContext, SessionContext,
    RegimeOutput, MarketRegime,
)
from jarvis.data.symbol_registry import resolve as resolve_symbol
from jarvis.intelligence.institutional_entry_engine import InstitutionalEntryEngine


def _make_context(symbol, price, atr, spread_pips=0.0, bid=None, ask=None):
    """Minimal MarketContext with bid == ask (spread_dist == 0) unless overridden."""
    bid = price if bid is None else bid
    ask = price if ask is None else ask
    return MarketContext(
        symbol=symbol,
        timestamp=datetime(2026, 9, 3, 13, 0, tzinfo=timezone.utc),
        current_price=price,
        bid=bid,
        ask=ask,
        structure=StructureContext(
            bias="BULLISH",
            demand_zone=(price * 0.98, price * 0.99),
            supply_zone=(price * 1.01, price * 1.02),
        ),
        liquidity=LiquidityContext(
            sell_side_liquidity=price * 0.95,
            buy_side_liquidity=price * 1.05,
        ),
        volatility=VolatilityContext(atr=atr, current_spread_pips=spread_pips),
        momentum=MomentumContext(trend_score=30, adx=25.0),
        session=SessionContext(is_prime_session=True),
    )


def _regime():
    return RegimeOutput(
        primary_regime=MarketRegime.TREND_BULL, probabilities={}, confidence=0.80
    )


def _make_ohlcv(start_price=2400.0, num_bars=50, trend=1.0, step=0.5):
    times = [datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)] * num_bars
    opens, highs, lows, closes, vols = [], [], [], [], []
    p = start_price
    for i in range(num_bars):
        o = p
        c = o + (trend * step) + (0.2 if i % 2 == 0 else -0.2)
        opens.append(o)
        highs.append(max(o, c) + 0.8)
        lows.append(min(o, c) - 0.8)
        closes.append(c)
        vols.append(100.0 + i)
        p = c
    return pd.DataFrame(
        {"time": times, "open": opens, "high": highs, "low": lows,
         "close": closes, "volume": vols}
    )


class _InvariantMixin:
    def assert_stop_invariant(self, res, bias, floor, expect_floored=None):
        entry = res["entry_price"]
        sl = res["sl_price"]
        rd = res["risk_dist"]

        # 1. The two stop descriptors must describe the same level.
        self.assertAlmostEqual(rd, abs(entry - sl), places=9,
                               msg=f"risk_dist {rd} != |entry {entry} - sl {sl}|")

        # 2. Floor is always enforced.
        self.assertGreaterEqual(rd, floor - 1e-9,
                                msg=f"risk_dist {rd} below floor {floor}")

        # 3. Stop sits on the correct side of entry.
        if bias == "BUY":
            self.assertLess(sl, entry)
        else:
            self.assertGreater(sl, entry)

        if expect_floored:
            self.assertAlmostEqual(rd, floor, places=9)


class TestScalpStopFloor(_InvariantMixin, unittest.TestCase):
    """SCALP block: pip_size=1e-4 (EURUSD), floor=5e-4, m5_atr=2e-4."""

    PRICE = 1.10000
    ATR = 2e-4
    FLOOR = 5e-4

    def _run(self, bias, sweep_extreme):
        eng = InstitutionalEntryEngine()
        ctx = _make_context("EURUSD", self.PRICE, atr=0.001, spread_pips=0.0)
        with patch.object(eng, "_estimate_atr", return_value=self.ATR), \
             patch.object(eng, "_detect_liquidity_sweep",
                          return_value=(True, sweep_extreme, True)), \
             patch.object(eng, "_detect_mss_displacement",
                          return_value=(True, 0.60, self.PRICE - 1e-3, self.PRICE + 1e-3)), \
             patch.object(eng, "_find_micro_fvg_ce", return_value=self.PRICE):
            return eng.calculate_entry_and_levels(
                ctx, _regime(), tentative_bias=bias, trade_style="SCALP", mtf_data=None
            )

    def test_buy_tight_structural_stop_is_floored(self):
        # structural distance ~1.4e-4 < 5e-4 floor
        res = self._run("BUY", self.PRICE - 1e-4)
        self.assert_stop_invariant(res, "BUY", self.FLOOR, expect_floored=True)

    def test_buy_re_enforce_branch_is_floored(self):
        # sweep above entry -> re-enforce branch, re-enforce dist ~9e-5 < floor
        res = self._run("BUY", self.PRICE + 1e-3)
        self.assert_stop_invariant(res, "BUY", self.FLOOR, expect_floored=True)

    def test_buy_wide_structural_stop_not_widened(self):
        res = self._run("BUY", self.PRICE - 0.01)
        self.assert_stop_invariant(res, "BUY", self.FLOOR)
        self.assertGreater(res["risk_dist"], self.FLOOR)

    def test_sell_tight_structural_stop_is_floored(self):
        res = self._run("SELL", self.PRICE + 1e-4)
        self.assert_stop_invariant(res, "SELL", self.FLOOR, expect_floored=True)

    def test_sell_re_enforce_branch_is_floored(self):
        res = self._run("SELL", self.PRICE - 1e-3)
        self.assert_stop_invariant(res, "SELL", self.FLOOR, expect_floored=True)

    def test_sell_wide_structural_stop_not_widened(self):
        res = self._run("SELL", self.PRICE + 0.01)
        self.assert_stop_invariant(res, "SELL", self.FLOOR)
        self.assertGreater(res["risk_dist"], self.FLOOR)


class TestDayTradingStopFloor(_InvariantMixin, unittest.TestCase):
    """DAY_TRADING block: pip_size=1e-4 (EURUSD), floor=5e-4, h1_atr=2e-4."""

    PRICE = 1.10000
    ATR = 2e-4
    FLOOR = 5e-4

    def _run(self, bias, origin_swing):
        eng = InstitutionalEntryEngine()
        ctx = _make_context("EURUSD", self.PRICE, atr=0.001, spread_pips=0.0)
        with patch.object(eng, "_estimate_atr", return_value=self.ATR), \
             patch.object(eng, "_find_m15_fvg_midpoint", return_value=self.PRICE), \
             patch.object(eng, "_find_breaker_block_retest", return_value=None), \
             patch.object(eng, "_find_displacement_origin", return_value=origin_swing), \
             patch.object(eng, "_check_h1_alignment", return_value=True), \
             patch.object(eng, "_detect_choch", return_value=True):
            return eng.calculate_entry_and_levels(
                ctx, _regime(), tentative_bias=bias, trade_style="DAY_TRADING", mtf_data=None
            )

    def test_buy_tight_structural_stop_is_floored(self):
        res = self._run("BUY", self.PRICE - 1e-4)
        self.assert_stop_invariant(res, "BUY", self.FLOOR, expect_floored=True)

    def test_buy_re_enforce_branch_is_floored(self):
        # re-enforce dist 0.80*h1_atr = 1.6e-4 < floor
        res = self._run("BUY", self.PRICE + 1e-3)
        self.assert_stop_invariant(res, "BUY", self.FLOOR, expect_floored=True)

    def test_buy_wide_structural_stop_not_widened(self):
        res = self._run("BUY", self.PRICE - 0.01)
        self.assert_stop_invariant(res, "BUY", self.FLOOR)
        self.assertGreater(res["risk_dist"], self.FLOOR)

    def test_sell_tight_structural_stop_is_floored(self):
        res = self._run("SELL", self.PRICE + 1e-4)
        self.assert_stop_invariant(res, "SELL", self.FLOOR, expect_floored=True)

    def test_sell_re_enforce_branch_is_floored(self):
        res = self._run("SELL", self.PRICE - 1e-3)
        self.assert_stop_invariant(res, "SELL", self.FLOOR, expect_floored=True)

    def test_sell_wide_structural_stop_not_widened(self):
        res = self._run("SELL", self.PRICE + 0.01)
        self.assert_stop_invariant(res, "SELL", self.FLOOR)
        self.assertGreater(res["risk_dist"], self.FLOOR)


class TestSwingStopFloor(_InvariantMixin, unittest.TestCase):
    """SWING block on gold after the 2026-10 stop-widening.

    The published XAU standard puts the stop 0.3-0.5 ATR beyond the structural
    wick, and the old $6-$12 dollar clamps put it inside one H1 candle's normal
    range, so every anchor-zone retest grazed the stop out. The engine now uses:

      * `sl_buffer = spread + 0.45 * h1_atr` (gold-only; silver keeps 0.30),
      * mineral floor  `1.20 * h1_atr + spread` (the $8 floor applies above
        2.0 ATR, which the mocked context is below),
      * asset cap      `min(max(9.0, 1.20 * h1_atr), 18.0)`.

    With `h1_atr = 1.0` and no spread every tight structural stop comes out
    at exactly `1.20 * 1.0 = 1.2` - the mineral floor is what binds - and
    wide structural stops survive until they meet the 9.0 cap.
    """

    PRICE = 2400.00
    ATR = 1.0              # h1 ATR: the patched `_estimate_atr` value is d1 ATR
    MINERAL_FLOOR = 1.2    # 1.20 * h1_atr, spread_dist = 0
    WIDE_SPAN = 2.0        # structural distance 2.0: above the floor, below the cap

    def _run(self, bias, range_low, range_high):
        eng = InstitutionalEntryEngine()
        ctx = _make_context("XAUUSD", self.PRICE, atr=self.ATR, spread_pips=0.0)
        with patch.object(eng, "_estimate_atr", return_value=self.ATR), \
             patch.object(eng, "_get_htf_range", return_value=(range_low, range_high)), \
             patch.object(eng, "_find_htf_order_block", return_value=self.PRICE), \
             patch.object(eng, "_detect_choch", return_value=True):
            return eng.calculate_entry_and_levels(
                ctx, _regime(), tentative_bias=bias, trade_style="SWING", mtf_data=None
            )

    def test_buy_tight_structural_stop_is_floored(self):
        # outer swing 0.1 below entry: struct dist 0.1 + 0.45 buffer = 0.55, floor 1.2 binds
        res = self._run("BUY", self.PRICE - 0.1, self.PRICE + 0.1)
        self.assert_stop_invariant(res, "BUY", self.MINERAL_FLOOR, expect_floored=True)

    def test_buy_re_enforce_branch_is_floored(self):
        # outer swing above entry -> re-enforce dist 1.20*h1_atr = 1.2, floor binds
        res = self._run("BUY", self.PRICE + 1.0, self.PRICE + 2.0)
        self.assert_stop_invariant(res, "BUY", self.MINERAL_FLOOR, expect_floored=True)

    def test_buy_wide_structural_stop_not_widened_or_capped(self):
        # structural distance 2.0: above the 1.2 mineral floor, below the 9.0 cap
        res = self._run("BUY", self.PRICE - self.WIDE_SPAN, self.PRICE + 0.1)
        self.assert_stop_invariant(res, "BUY", self.MINERAL_FLOOR)
        self.assertGreater(res["risk_dist"], self.MINERAL_FLOOR)

    def test_sell_tight_structural_stop_is_floored(self):
        res = self._run("SELL", self.PRICE - 0.1, self.PRICE + 0.1)
        self.assert_stop_invariant(res, "SELL", self.MINERAL_FLOOR, expect_floored=True)

    def test_sell_re_enforce_branch_is_floored(self):
        res = self._run("SELL", self.PRICE - 2.0, self.PRICE - 1.0)
        self.assert_stop_invariant(res, "SELL", self.MINERAL_FLOOR, expect_floored=True)

    def test_sell_wide_structural_stop_not_widened_or_capped(self):
        res = self._run("SELL", self.PRICE - 0.1, self.PRICE + self.WIDE_SPAN)
        self.assert_stop_invariant(res, "SELL", self.MINERAL_FLOOR)
        self.assertGreater(res["risk_dist"], self.MINERAL_FLOOR)


class TestSwingCapCannotUndercutFloor(_InvariantMixin, unittest.TestCase):
    """The gold asset cap (`min(max(9.0, 1.20*h1_atr), 18.0)`) now floats a 9.0
    minimum, so unlike the pre-widening `2.80 * d1_atr` cap it can no longer be
    dragged below the `pip_size * 5` floor by a degenerate near-flat frame:
    9.0 is four orders of magnitude above that floor on XAUUSD. What CAN
    undercut the cap-bound is nothing — but the PC degeneracy check still
    matters for other buckets and the mineral floor still needs pinning here.

    These tests pin: with a degenerate d1 ATR the stop must still come out at
    the mineral floor (`1.20 * h1_atr` = 1.2 in the mocked context), and with a
    normal ATR the cap must still trim a runaway structural distance.
    """

    PRICE = 2400.00
    D1_ATR = 0.05          # degenerate daily ATR (the old 2.80*x cap would be 0.14)
    MINERAL_FLOOR = 1.2    # 1.20 * h1_atr, spread_dist = 0

    def _run(self, bias, range_low, range_high, d1_atr=None):
        eng = InstitutionalEntryEngine()
        ctx = _make_context("XAUUSD", self.PRICE, atr=1.0, spread_pips=0.0)
        with patch.object(eng, "_estimate_atr", return_value=(d1_atr if d1_atr is not None else self.D1_ATR)), \
             patch.object(eng, "_get_htf_range", return_value=(range_low, range_high)), \
             patch.object(eng, "_find_htf_order_block", return_value=self.PRICE), \
             patch.object(eng, "_detect_choch", return_value=True):
            return eng.calculate_entry_and_levels(
                ctx, _regime(), tentative_bias=bias, trade_style="SWING", mtf_data=None
            )

    def test_degenerate_d1_atr_keeps_mineral_floor_buy(self):
        res = self._run("BUY", self.PRICE - 0.1, self.PRICE + 0.1)
        self.assert_stop_invariant(res, "BUY", self.MINERAL_FLOOR, expect_floored=True)
        self.assertGreaterEqual(res["risk_dist"], self.MINERAL_FLOOR - 1e-9)

    def test_degenerate_d1_atr_keeps_mineral_floor_sell(self):
        res = self._run("SELL", self.PRICE - 0.1, self.PRICE + 0.1)
        self.assert_stop_invariant(res, "SELL", self.MINERAL_FLOOR, expect_floored=True)
        self.assertGreaterEqual(res["risk_dist"], self.MINERAL_FLOOR - 1e-9)

    def test_cap_still_binds_above_the_floor(self):
        """The clamp must not disable the cap: a normal H1 ATR still trims."""
        # Structural distance is ~15.45; the gold cap is min(max(9.0, 1.2*1.0), 18.0)
        # = 9.0 -> the cap applies.
        res = self._run("BUY", self.PRICE - 15.0, self.PRICE + 15.0, d1_atr=1.0)
        self.assertAlmostEqual(res["risk_dist"], 9.0, places=6)


class TestEndToEndStopInvariant(_InvariantMixin, unittest.TestCase):
    """End-to-end (no mocks): the invariant must hold for BUY and SELL, all styles."""

    def _run(self, style, bias):
        eng = InstitutionalEntryEngine()
        symbol = "XAUUSD" if style == "SWING" else "EURUSD"
        price = 2400.0 if symbol == "XAUUSD" else 1.0850
        ctx = _make_context(symbol, price, atr=5.0 if symbol == "XAUUSD" else 0.0030,
                            spread_pips=2.0, bid=price - 0.0002, ask=price + 0.0002)
        mtf = {
            "M5": _make_ohlcv(start_price=price, num_bars=30, trend=-0.3),
            "M1": _make_ohlcv(start_price=price, num_bars=30, trend=-0.3),
            "M15": _make_ohlcv(start_price=price, num_bars=30, trend=0.0002, step=0.0001),
            "H1": _make_ohlcv(start_price=price, num_bars=30, trend=0.0003, step=0.0001),
            "H4": _make_ohlcv(start_price=price, num_bars=30, trend=-0.8, step=1.8),
            "D1": _make_ohlcv(start_price=price, num_bars=30, trend=-1.0, step=2.5),
        }
        return eng.calculate_entry_and_levels(
            ctx, _regime(), tentative_bias=bias, trade_style=style, mtf_data=mtf
        )

    def test_invariant_all_styles_both_biases(self):
        for style in ("SCALP", "DAY_TRADING", "SWING"):
            for bias in ("BUY", "SELL"):
                with self.subTest(style=style, bias=bias):
                    res = self._run(style, bias)
                    entry = res["entry_price"]
                    sl = res["sl_price"]
                    rd = res["risk_dist"]
                    self.assertAlmostEqual(rd, abs(entry - sl), places=9)
                    if bias == "BUY":
                        self.assertLess(sl, entry)
                    else:
                        self.assertGreater(sl, entry)


if __name__ == "__main__":
    unittest.main()
