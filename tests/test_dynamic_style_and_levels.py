"""
Unit and integration tests for HM Algo 2.0 Advanced Dynamic Style, SL/TP and Trailing Engine.
Verifies:
1. Symbol-specific style profiles (SCALP, DAY_TRADING, SWING) for Crypto, Metals, Indices, Forex.
2. Dynamic ExitPolicy resolution and regime adaptation (RANGE, TREND_BULL, HIGH_VOLATILITY).
3. Position comment parsing for trade style extraction in PositionMonitorEngine.
4. Dynamic 3-tier milestone targets (TP1, TP2, TP3) across styles in DynamicRiskAndLevelsEngine.
5. Memoization cache O(1) performance benchmark.
"""
import time
import unittest
from datetime import datetime, timezone

from jarvis.intelligence.symbol_profile_config import (
    get_symbol_profile_config, get_style_profile, StyleProfile, SYMBOL_PROFILES
)
from jarvis.execution.exit_policy import ExitPolicy, evaluate_exit, _RESOLVED_POLICY_CACHE
from jarvis.intelligence.dynamic_levels import DynamicRiskAndLevelsEngine
from jarvis.data.schemas import (
    MarketContext, StructureContext, LiquidityContext,
    VolatilityContext, MomentumContext, SessionContext,
    RegimeOutput, MarketRegime, PositionSnapshot
)


class TestDynamicStyleAndLevels(unittest.TestCase):
    def setUp(self):
        self.levels_engine = DynamicRiskAndLevelsEngine()

    def test_get_style_profile_symbols_and_styles(self):
        """Verify get_style_profile returns calibrated profiles across asset classes."""
        # 1. Gold (XAUUSD)
        gold_scalp = get_style_profile("XAUUSD", "SCALP")
        self.assertEqual(gold_scalp.trade_style, "SCALP")
        self.assertAlmostEqual(gold_scalp.fast_cash_r, 1.00)
        self.assertAlmostEqual(gold_scalp.be_trigger_r, 1.10)
        self.assertAlmostEqual(gold_scalp.runner_trail_atr, 1.20)

        gold_day = get_style_profile("XAUUSD", "DAY_TRADING")
        self.assertEqual(gold_day.trade_style, "DAY_TRADING")
        self.assertAlmostEqual(gold_day.fast_cash_r, 1.10)
        self.assertAlmostEqual(gold_day.be_trigger_r, 1.20)

        gold_swing = get_style_profile("XAUUSD", "SWING")
        self.assertEqual(gold_swing.trade_style, "SWING")
        self.assertAlmostEqual(gold_swing.fast_cash_r, 1.80)
        self.assertAlmostEqual(gold_swing.be_trigger_r, 2.00)
        self.assertAlmostEqual(gold_swing.asym_rr, 3.80)

        # 2. Crypto (BTCUSD)
        btc_scalp = get_style_profile("BTCUSD", "SCALP")
        self.assertAlmostEqual(btc_scalp.fast_cash_r, 1.00)
        self.assertAlmostEqual(btc_scalp.be_trigger_r, 1.20)
        self.assertAlmostEqual(btc_scalp.anti_wick_buffer_atr, 0.40)

        btc_swing = get_style_profile("BTCUSD", "SWING")
        self.assertAlmostEqual(btc_swing.fast_cash_r, 1.80)
        self.assertAlmostEqual(btc_swing.be_trigger_r, 2.00)

        # 3. Indices (US500)
        us500_scalp = get_style_profile("US500", "SCALP")
        self.assertAlmostEqual(us500_scalp.be_trigger_r, 1.15)
        self.assertAlmostEqual(us500_scalp.runner_trail_atr, 1.00)

        us500_swing = get_style_profile("US500", "SWING")
        self.assertAlmostEqual(us500_swing.be_trigger_r, 2.20)
        self.assertAlmostEqual(us500_swing.runner_trail_atr, 2.00)

        # 4. Forex (EURUSD)
        eur_scalp = get_style_profile("EURUSD", "SCALP")
        self.assertAlmostEqual(eur_scalp.fast_cash_r, 1.00)
        self.assertAlmostEqual(eur_scalp.be_trigger_r, 1.10)

        eur_swing = get_style_profile("EURUSD", "SWING")
        self.assertAlmostEqual(eur_swing.fast_cash_r, 1.50)
        self.assertAlmostEqual(eur_swing.be_trigger_r, 1.80)

    def test_exit_policy_for_symbol_style_and_regime_dynamics(self):
        """Verify ExitPolicy.for_symbol dynamic modulation under ranging vs trending regimes."""
        # Baseline Scalp vs Swing on EURUSD
        pol_scalp = ExitPolicy.for_symbol("EURUSD", trade_style="SCALP", regime="NORMAL")
        pol_swing = ExitPolicy.for_symbol("EURUSD", trade_style="SWING", regime="NORMAL")
        self.assertEqual(pol_scalp.be_trigger_r, 1.10)
        self.assertEqual(pol_swing.be_trigger_r, 1.80)

        # Ranging Regime Modulation
        pol_range = ExitPolicy.for_symbol("EURUSD", trade_style="SWING", regime="RANGE")
        # In range, fast cash is clamped to 1.0R, BE moved forward to 1.15R, trail clamped to 1.00 ATR
        self.assertLessEqual(pol_range.fast_cash_r, 1.00)
        self.assertLessEqual(pol_range.be_trigger_r, 1.15)
        self.assertLessEqual(pol_range.runner_trail_atr, 1.00)

        # Trending Regime Modulation
        pol_trend = ExitPolicy.for_symbol("BTCUSD", trade_style="SWING", regime="TREND_BULL")
        # In trend, runner trail expands by 1.15x
        self.assertGreater(pol_trend.runner_trail_atr, 1.80)
        self.assertEqual(pol_trend.fast_cash_volume_pct, 0.25)

    def test_exit_policy_evaluate_exit_scalp_vs_swing(self):
        """Verify evaluate_exit ratchets stops according to style profile."""
        pol_scalp = ExitPolicy.for_symbol("XAUUSD", trade_style="SCALP", regime="NORMAL")
        pol_swing = ExitPolicy.for_symbol("XAUUSD", trade_style="SWING", regime="NORMAL")

        # Trade reaches +1.15R excursion
        # Entry=2400.0, SL=2390.0 -> Risk=10.0 pts. Price=2411.5 (+1.15R)
        entry = 2400.0
        initial_sl = 2390.0
        current_sl = 2390.0
        price = 2411.5
        fav_dist = 11.5
        atr = 5.0

        # Scalp trade MUST trigger BE lock and partial cash at +1.15R
        dec_scalp = evaluate_exit(
            side="BUY", entry=entry, initial_sl=initial_sl, current_sl=current_sl,
            tp=2425.0, price=price, favorable_dist=fav_dist, atr=atr, policy=pol_scalp
        )
        self.assertTrue(dec_scalp.be_locked or any("BE_LOCK" in a for a in dec_scalp.actions))
        self.assertGreaterEqual(dec_scalp.new_sl, entry)
        self.assertTrue(dec_scalp.partial_due or any("PARTIAL" in a for a in dec_scalp.actions))

        # Swing trade MUST NOT trigger BE lock at +1.15R (waiting for +2.0R to let macro trade breathe)
        dec_swing = evaluate_exit(
            side="BUY", entry=entry, initial_sl=initial_sl, current_sl=current_sl,
            tp=2440.0, price=price, favorable_dist=fav_dist, atr=atr, policy=pol_swing
        )
        self.assertFalse(dec_swing.be_locked)
        self.assertFalse(dec_swing.partial_due)
        self.assertLess(dec_swing.new_sl, entry)

    def test_position_monitor_comment_style_extraction(self):
        """Verify PositionMonitorEngine._extract_position_trade_style correctly parses comments."""
        from jarvis.execution.position_monitor import PositionMonitorEngine

        pm = PositionMonitorEngine(mt5_client=None, state_manager=None, data_feed=None, context_engine=None)

        from unittest.mock import MagicMock

        pos_scalp = MagicMock()
        pos_scalp.symbol = "XAUUSD"
        pos_scalp.comment = "HMA2_SCLP_TRND_DEMO"
        self.assertEqual(pm._extract_position_trade_style(pos_scalp), "SCALP")

        pos_day = MagicMock()
        pos_day.symbol = "BTCUSD"
        pos_day.comment = "HMA2_DAY_PULL_DEMO"
        self.assertEqual(pm._extract_position_trade_style(pos_day), "DAY_TRADING")

        pos_swing = MagicMock()
        pos_swing.symbol = "US500"
        pos_swing.comment = "HMA2_SWNG_CHOC_LIVE"
        self.assertEqual(pm._extract_position_trade_style(pos_swing), "SWING")

        pos_fallback = MagicMock()
        pos_fallback.symbol = "EURUSD"
        pos_fallback.comment = "MANUAL_ORDER"
        self.assertEqual(pm._extract_position_trade_style(pos_fallback), "SWING")

    def test_dynamic_levels_scalp_vs_swing_tp1_and_rr(self):
        """Verify DynamicRiskAndLevelsEngine sets distinct TP1 fast-cash and RR for SCALP vs SWING."""
        ctx = MarketContext(
            symbol="XAUUSD",
            timestamp=datetime.now(timezone.utc),
            current_price=2400.0,
            bid=2399.8,
            ask=2400.2,
            structure=StructureContext(
                bias="BULLISH",
                demand_zone=(2390.0, 2394.0),
                supply_zone=(2435.0, 2440.0),
                order_blocks=[{"type": "BULLISH_ORDER_BLOCK", "low": 2392.0, "high": 2395.0}]
            ),
            liquidity=LiquidityContext(buy_side_liquidity=2440.0, sell_side_liquidity=2388.0),
            volatility=VolatilityContext(atr=8.0, current_spread_pips=2.0, state="NORMAL"),
            momentum=MomentumContext(trend_score=50.0, adx=25.0),
            session=SessionContext(is_prime_session=True)
        )
        regime = RegimeOutput(primary_regime=MarketRegime.TREND_BULL, probabilities={}, confidence=0.80)

        # Calculate SCALP levels
        levels_scalp = self.levels_engine.calculate_levels(ctx, regime, tentative_bias="BUY", trade_style="SCALP")
        # Calculate SWING levels
        levels_swing = self.levels_engine.calculate_levels(ctx, regime, tentative_bias="BUY", trade_style="SWING")

        # Scalp stop distance must be tighter than Swing stop distance
        self.assertLess(levels_scalp["risk_dist"], levels_swing["risk_dist"])
        # Scalp TP1 price must be closer than Swing TP1 price
        self.assertLess(levels_scalp["tp1_price"], levels_swing["tp1_price"])
        # Both must offer >= 1.5 R:R
        self.assertGreaterEqual(levels_scalp["rr_ratio"], 1.5)
        self.assertGreaterEqual(levels_swing["rr_ratio"], 1.8)

    def test_policy_cache_performance(self):
        """Verify _RESOLVED_POLICY_CACHE delivers O(1) microsecond retrieval for 10,000 queries."""
        start_t = time.perf_counter()
        for _ in range(10000):
            _ = ExitPolicy.for_symbol("XAUUSD", trade_style="SCALP", regime="RANGE")
            _ = ExitPolicy.for_symbol("BTCUSD", trade_style="SWING", regime="TREND_BULL")
        elapsed_sec = time.perf_counter() - start_t

        # 20,000 lookups should easily finish in under 100 milliseconds
        self.assertLess(elapsed_sec, 0.10, f"Cache retrieval took too long: {elapsed_sec*1000:.2f} ms")


if __name__ == "__main__":
    unittest.main()
