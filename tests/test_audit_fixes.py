# tests/test_audit_fixes.py
import unittest
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta

from jarvis.data.schemas import (
    StructureContext,
    MarketContext,
    VolatilityContext,
    LiquidityContext,
    MomentumContext,
    SessionContext,
    RegimeOutput,
    MarketRegime
)
from jarvis.market.market_structure import MarketStructureEngine
from jarvis.analysts.devil_advocate import DevilAdvocateAnalyst
from jarvis.intelligence.symbol_profile_config import get_symbol_profile_config


class TestAuditFixes(unittest.TestCase):
    def test_symbol_profile_config_gold_and_btc(self):
        gold_cfg = get_symbol_profile_config("XAUUSD")
        self.assertNotIn("TREND_FOLLOWING", gold_cfg.banned_strategies)
        self.assertEqual(gold_cfg.strategy_weights["TREND_FOLLOWING"], 3.5)
        self.assertEqual(gold_cfg.strategy_weights["CHOCH_STRUCTURAL_REVERSAL"], 1.5)
        self.assertEqual(gold_cfg.allowed_utc_hours, (6, 21))
        self.assertEqual(gold_cfg.fast_cash_r, 1.10)
        self.assertEqual(gold_cfg.fast_cash_volume_pct, 0.50)
        self.assertEqual(gold_cfg.be_trigger_r, 1.20)

        btc_cfg = get_symbol_profile_config("BTCUSD")
        self.assertIn("BREAKOUT_EXPANSION", btc_cfg.banned_strategies)
        self.assertEqual(btc_cfg.strategy_weights["TREND_PULLBACK"], 4.0)
        self.assertEqual(btc_cfg.sl_atr_multiplier, 2.20)
        self.assertEqual(btc_cfg.fast_cash_r, 1.20)
        self.assertEqual(btc_cfg.fast_cash_volume_pct, 0.50)

    def test_dynamic_trendline_in_market_structure(self):
        # Create synthetic bars with higher lows and higher highs (ascending trend)
        n = 50
        dates = [datetime.now(timezone.utc) - timedelta(minutes=(n - i) * 15) for i in range(n)]
        # Ascending trend: low starts at 4000 and rises to 4030
        base_prices = np.linspace(4000, 4030, n)
        # Add a wave to create swing pivots
        waves = np.sin(np.linspace(0, 4 * np.pi, n)) * 5.0
        closes = base_prices + waves
        highs = closes + 2.0
        lows = closes - 2.0
        df = pd.DataFrame({
            "open": closes - 0.5,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [1000] * n,
            "time": dates
        })

        analyzer = MarketStructureEngine(pivot_window=4)
        st = analyzer.analyze_structure(df)

        self.assertIsInstance(st, StructureContext)
        self.assertTrue(hasattr(st, "dynamic_support_trendline"))
        self.assertTrue(hasattr(st, "dynamic_resistance_trendline"))
        self.assertGreater(st.dynamic_support_trendline, 0.0)
        self.assertGreater(st.dynamic_resistance_trendline, 0.0)

    def test_devil_advocate_obstacle_collision(self):
        # Test that selling directly above an ascending trendline (<0.5 ATR) triggers collision penalty
        st = StructureContext(
            bias="BULLISH",
            higher_highs=True,
            higher_lows=True,
            demand_zone=(4000.0, 4005.0),
            supply_zone=(4035.0, 4040.0),
            dynamic_support_trendline=4012.77,   # Trendline at 4012.77
            dynamic_resistance_trendline=4038.00
        )
        vol = VolatilityContext(
            atr=10.0,
            current_spread_pips=2.0,
            is_excessive_spread=False,
            state="NORMAL"
        )
        regime = RegimeOutput(
            primary_regime=MarketRegime.TREND_BULL,
            probabilities={"trend_bull": 0.85, "range": 0.15},
            confidence=0.85,
            regime_transition=False
        )

        ctx = MarketContext(
            symbol="XAUUSD",
            timestamp=datetime.now(timezone.utc),
            current_price=4015.00,  # 2.23 pts away from 4012.77 (< 0.25 ATR = 2.5 pts)
            bid=4014.85,
            ask=4015.15,
            structure=st,
            liquidity=LiquidityContext(),
            volatility=vol,
            momentum=MomentumContext(),
            session=SessionContext(current_session="LONDON", is_prime_session=True),
            context_quality=95.0
        )

        da = DevilAdvocateAnalyst()
        report = da.critique_opportunity(context=ctx, regime=regime, proposed_bias="SELL")

        # Must detect immediate collision and apply strong penalty
        self.assertGreaterEqual(report.penalty_score, 20.0)
        self.assertTrue(any("collision" in t.lower() or "dynamic support/trendline" in t.lower() for t in report.threats_detected))


if __name__ == "__main__":
    unittest.main()
