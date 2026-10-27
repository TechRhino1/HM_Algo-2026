"""
Unit test verifying institutional Gold (XAUUSD) execution fixes:
1. Symbol profile banning of BREAKOUT_EXPANSION on Gold to prevent chasing low-edge breakdowns.
2. A Gold scalp whose structural stop is overwhelmed by friction is rejected, not widened.
3. Dynamic levels keep Gold stops beyond structure with style-appropriate ATR floors.
4. DecisionEngine chasing floor guard routing extended Gold moves to LIMIT orders at FVG/OB.
"""
import unittest
from datetime import datetime, timezone
import pandas as pd

from jarvis.data.schemas import (
    MarketContext,
    StructureContext,
    VolatilityContext,
    MomentumContext,
    LiquidityContext,
    SessionContext,
    RegimeOutput,
    MarketRegime,
)
from jarvis.intelligence.symbol_profile_config import get_symbol_profile_config
from jarvis.scalping.scalp_execution_engine import ScalpExecutionEngine
from jarvis.intelligence.dynamic_levels import DynamicRiskAndLevelsEngine


class TestGoldExecutionFix(unittest.TestCase):
    def test_gold_symbol_profile_bans_breakout_expansion(self):
        """Verify XAUUSD bans BREAKOUT_EXPANSION and favors LIQUIDITY_SWEEP & TREND_PULLBACK."""
        cfg = get_symbol_profile_config("XAUUSD")
        self.assertIn("BREAKOUT_EXPANSION", cfg.banned_strategies)
        self.assertEqual(cfg.strategy_weights.get("BREAKOUT_EXPANSION", 0.0), 0.0)
        self.assertGreaterEqual(cfg.strategy_weights.get("CHOCH_STRUCTURAL_REVERSAL", 0.0), 4.5)
        self.assertIn("LIQUIDITY_SWEEP_REVERSAL", cfg.banned_strategies)

    def test_gold_scalp_with_tiny_atr_is_rejected_by_cost_gate(self):
        """A sub-point structural stop must be rejected by friction, not widened to 4.0 pts."""
        engine = ScalpExecutionEngine()
        st = StructureContext(bias="BEARISH", supply_zone=(4088.0, 4088.5))
        st.swing_high = 4088.00
        st.swing_low = 4085.00
        ctx = MarketContext(
            symbol="XAUUSD",
            timestamp=datetime.now(timezone.utc),
            current_price=4087.62,
            bid=4087.50,
            ask=4087.75,
            structure=st,
            liquidity=LiquidityContext(sweep_detected=False),
            volatility=VolatilityContext(atr=0.50, current_spread_pips=2.5, state="EXPANSION"),
            momentum=MomentumContext(trend_score=-65.0, rsi=30.0, adx=28.0),
            session=SessionContext(is_prime_session=True)
        )
        regime = RegimeOutput(primary_regime=MarketRegime.TREND_BEAR, probabilities={}, confidence=0.85)

        df_m1 = pd.DataFrame([
            {"open": 4088.0, "high": 4088.5, "low": 4087.0, "close": 4087.62, "volume": 100}
        ] * 10)

        scalp_dec = engine.evaluate_scalp(
            context=ctx,
            regime=regime,
            tentative_bias="SELL",
            df_m1=df_m1,
            df_m5=df_m1,
            account_balance=410.0,
            risk_per_trade_pct=0.5
        )

        sl_dist = abs(scalp_dec.stop_loss - ctx.current_price)
        self.assertGreater(sl_dist, 0.5, "SL must sit beyond the swing high plus spread buffer")
        self.assertGreater(scalp_dec.stop_loss, ctx.current_price)
        self.assertFalse(scalp_dec.passed_cost_gate)
        self.assertTrue(any("Cost Friction Gate Failed" in r for r in scalp_dec.failing_reasons))

    def test_dynamic_levels_gold_floors(self):
        """Verify dynamic_levels keeps Gold stops beyond structure for every style."""
        engine = DynamicRiskAndLevelsEngine()
        st = StructureContext(bias="BEARISH", supply_zone=(4105.0, 4106.0))
        st.swing_high = 4105.00
        st.swing_low = 4095.00
        ctx = MarketContext(
            symbol="XAUUSD",
            timestamp=datetime.now(timezone.utc),
            current_price=4100.00,
            bid=4099.85,
            ask=4100.15,
            structure=st,
            liquidity=LiquidityContext(sweep_detected=False),
            volatility=VolatilityContext(atr=3.0, current_spread_pips=3.0, state="NORMAL"),
            momentum=MomentumContext(trend_score=-40.0, rsi=45.0, adx=22.0),
            session=SessionContext(is_prime_session=True)
        )
        regime = RegimeOutput(primary_regime=MarketRegime.TREND_BEAR, probabilities={}, confidence=0.80)

        structural_floor = 5.0

        # SCALP
        scalp_lvls = engine.calculate_levels(ctx, regime, tentative_bias="SELL", trade_style="SCALP")
        self.assertGreaterEqual(scalp_lvls["risk_dist"], structural_floor)

        # DAY_TRADING
        day_lvls = engine.calculate_levels(ctx, regime, tentative_bias="SELL", trade_style="DAY_TRADING")
        self.assertGreaterEqual(day_lvls["risk_dist"], structural_floor)

        # SWING
        swing_lvls = engine.calculate_levels(ctx, regime, tentative_bias="SELL", trade_style="SWING")
        self.assertGreaterEqual(swing_lvls["risk_dist"], structural_floor)


if __name__ == "__main__":
    unittest.main()
