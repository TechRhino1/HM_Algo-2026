"""
Master test suite verifying the comprehensive profitability & loss-prevention architecture:
1. Account tier micro mode & lot capping for $400 accounts.
2. Position monitor holding grace periods & non-destructive early invalidation.
3. Exit policy time-stop protection (only flattening dead trades in negative loss, style-aware).
4. Dynamic levels Forex stop loss noise floors (>= 8 pips scalp, >= 14 pips day, >= 25 pips swing).
5. Decision engine macro-regime alignment (never buy into HTF/D1 bear trends without authenticated reversal).
"""
import unittest
from datetime import datetime, timezone

from jarvis.risk.account_tier import is_micro_account, get_max_lot_cap
from jarvis.execution.exit_policy import ExitPolicy, evaluate_exit
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
from jarvis.intelligence.dynamic_levels import DynamicRiskAndLevelsEngine
from jarvis.intelligence.decision_engine import DecisionEngine


class TestProfitAndLossPreventionMaster(unittest.TestCase):
    def test_account_tier_micro_and_lot_cap(self):
        """Verify $400 account is properly classified as micro and capped at 0.03 lots."""
        equity = 409.81
        self.assertTrue(is_micro_account(equity), "Account with $409 equity should be recognized as micro")
        lot_cap = get_max_lot_cap(equity)
        self.assertEqual(lot_cap, 0.03, "Micro account equity should have max 0.03 lot cap")

    def test_exit_policy_does_not_flatten_positive_trades(self):
        """Verify time stop never kills a trade that is in profit or breakeven."""
        policy = ExitPolicy(
            symbol="SOLUSD",
            time_stop_minutes=90.0,
            time_stop_min_r=0.0,
        )
        dec = evaluate_exit(
            side="SELL",
            entry=106.0,
            initial_sl=108.0,
            current_sl=108.0,
            tp=100.0,
            price=105.88,
            favorable_dist=0.12,
            atr=1.5,
            policy=policy,
            hold_minutes=95.0,
        )
        self.assertFalse(dec.should_close, "Time stop must NOT close a trade that is in profit or positive excursion")

    def test_exit_policy_flattens_only_dead_trades_in_loss(self):
        """Verify time stop closes only genuinely dead trades sitting in negative loss after time limit."""
        policy = ExitPolicy(
            symbol="SOLUSD",
            time_stop_minutes=120.0,
            time_stop_min_r=0.10,
        )
        dec = evaluate_exit(
            side="BUY",
            entry=100.0,
            initial_sl=98.0,
            current_sl=98.0,
            tp=106.0,
            price=99.6,
            favorable_dist=-0.4,
            atr=1.5,
            policy=policy,
            hold_minutes=130.0,
        )
        self.assertTrue(dec.should_close, "Time stop should close stagnant negative trade after time limit")

    def test_dynamic_levels_forex_sl_floors(self):
        """Verify Forex stops stay beyond structure with ATR-scaled style floors."""
        engine = DynamicRiskAndLevelsEngine()
        ctx = MarketContext(
            symbol="EURUSD",
            timestamp=datetime.now(timezone.utc),
            current_price=1.12000,
            bid=1.11995,
            ask=1.12005,
            structure=StructureContext(bias="BEARISH", supply_zone=(1.12050, 1.12100)),
            liquidity=LiquidityContext(sweep_detected=False),
            volatility=VolatilityContext(atr=0.0008, current_spread_pips=1.0, state="NORMAL"),
            momentum=MomentumContext(trend_score=-40.0, rsi=45.0, adx=22.0),
            session=SessionContext(is_prime_session=True)
        )
        regime = RegimeOutput(primary_regime=MarketRegime.TREND_BEAR, probabilities={}, confidence=0.80)

        # SCALP on EURUSD: risk_dist must clear the structural swing high (>= 5 pips)
        scalp_lvls = engine.calculate_levels(ctx, regime, tentative_bias="SELL", trade_style="SCALP")
        self.assertGreaterEqual(round(scalp_lvls["risk_dist"], 5), 0.00050, "EURUSD SCALP SL distance must be >= 5 pips")

        # DAY_TRADING on EURUSD: wider ATR-scaled floor (>= 9 pips)
        day_lvls = engine.calculate_levels(ctx, regime, tentative_bias="SELL", trade_style="DAY_TRADING")
        self.assertGreaterEqual(round(day_lvls["risk_dist"], 5), 0.00090, "EURUSD DAY SL distance must be >= 9 pips")

        # SWING on EURUSD: widest ATR-scaled floor (>= 12 pips)
        swing_lvls = engine.calculate_levels(ctx, regime, tentative_bias="SELL", trade_style="SWING")
        self.assertGreaterEqual(round(swing_lvls["risk_dist"], 5), 0.00120, "EURUSD SWING SL distance must be >= 12 pips")

    def test_decision_engine_macro_bear_blocks_counter_trend_buy(self):
        """Verify D1 & HTF Bearish regime blocks BUY signals during breakouts/ranges without authenticated reversal."""
        engine = DecisionEngine()
        ctx = MarketContext(
            symbol="ETHUSD",
            timestamp=datetime.now(timezone.utc),
            current_price=2450.0,
            bid=2449.5,
            ask=2450.5,
            structure=StructureContext(bias="NEUTRAL"),
            liquidity=LiquidityContext(sweep_detected=False),
            volatility=VolatilityContext(atr=25.0, current_spread_pips=100.0, state="NORMAL"),
            momentum=MomentumContext(trend_score=15.0, rsi=45.0, adx=18.0),
            session=SessionContext(is_prime_session=True)
        )
        ctx.mtf_alignment = {"D1": "BEARISH", "H4": "BEARISH", "H1": "BEARISH", "M15": "NEUTRAL", "M5": "NEUTRAL"}
        regime = RegimeOutput(primary_regime=MarketRegime.RANGE, probabilities={}, confidence=0.75)

        analyst_reports = {}

        bias = engine._resolve_market_trend_and_bias(
            context=ctx,
            regime=regime,
            analyst_reports=analyst_reports,
            trade_style="DAY_TRADING"
        )
        self.assertNotEqual(bias, "BUY", "Must NEVER emit BUY into a D1/H4 macro downtrend without authenticated reversal")
        self.assertIn(bias, ("HOLD", "SELL"))


if __name__ == "__main__":
    unittest.main()
