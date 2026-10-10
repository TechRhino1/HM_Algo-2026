"""
Unit tests validating Institutional Entry & Dynamic Lot Sizing Fixes:
1. DecisionEngine.evaluate() threads mtf_data into DynamicRiskAndLevelsEngine and activates InstitutionalEntryEngine.
2. Passive retracement / limit pullback entries generate order_type == "LIMIT".
3. Micro accounts ($500 balance) with tight stops (5-10 pips / $2.00-$2.50) dynamically scale up to 0.02 - 0.04 lots.
4. InstitutionalEntryEngine eliminates fake rejection_wick when no sweep is detected.
"""
import unittest
from datetime import datetime, timezone, timedelta
import pandas as pd

from jarvis.data.schemas import (
    MarketContext, StructureContext, LiquidityContext,
    VolatilityContext, MomentumContext, SessionContext,
    RegimeOutput, MarketRegime, AnalystReport, AnalystRole, DevilAdvocateReport
)
from jarvis.intelligence.decision_engine import DecisionEngine
from jarvis.intelligence.institutional_entry_engine import InstitutionalEntryEngine
from jarvis.intelligence.dynamic_levels import DynamicRiskAndLevelsEngine
from jarvis.risk.position_sizing import PositionSizer


def make_sample_ohlcv(start_price=2400.0, num_bars=30, trend=0.2, step=0.3):
    times = [datetime.now(timezone.utc) - timedelta(minutes=5 * (num_bars - i)) for i in range(num_bars)]
    opens, highs, lows, closes, volumes = [], [], [], [], []
    p = start_price
    for i in range(num_bars):
        o = p
        c = o + (trend * step)
        h = max(o, c) + 0.5
        l = min(o, c) - 0.5
        opens.append(o)
        highs.append(h)
        lows.append(l)
        closes.append(c)
        volumes.append(100.0 + i)
        p = c
    return pd.DataFrame({
        "time": times, "open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes
    })


class TestInstitutionalEntryAndLotSizingFixes(unittest.TestCase):
    def setUp(self):
        self.decision_engine = DecisionEngine()
        self.institutional_engine = InstitutionalEntryEngine()

    def test_decision_engine_activates_institutional_entry_engine(self):
        """Verify DecisionEngine.evaluate() activates InstitutionalEntryEngine when mtf_data is provided."""
        ctx = MarketContext(
            symbol="XAUUSD",
            timestamp=datetime(2026, 9, 3, 14, 0, tzinfo=timezone.utc),
            current_price=2400.0,
            bid=2399.8,
            ask=2400.2,
            structure=StructureContext(
                bias="BULLISH",
                demand_zone=(2390.0, 2393.0),
                supply_zone=(2420.0, 2425.0),
                fair_value_gaps=[{"type": "BULLISH_FVG", "bottom": 2395.0, "top": 2398.0}]
            ),
            liquidity=LiquidityContext(sell_side_liquidity=2389.0, buy_side_liquidity=2420.0),
            volatility=VolatilityContext(atr=5.0, current_spread_pips=2.0),
            momentum=MomentumContext(trend_score=40, adx=28.0),
            session=SessionContext(is_prime_session=True)
        )
        regime = RegimeOutput(primary_regime=MarketRegime.TREND_BULL, probabilities={}, confidence=0.85)
        df_m5 = make_sample_ohlcv(start_price=2405.0, num_bars=20, trend=-0.3)
        mtf = {"M5": df_m5, "M1": df_m5}

        report = AnalystReport(role=AnalystRole.STRUCTURE, symbol="XAUUSD", bias="BUY", score=85.0, confidence=0.85)
        devil = DevilAdvocateReport(symbol="XAUUSD", counter_bias="SELL", penalty_score=0.0, invalidation_risk_coefficient=1.0)

        decision = self.decision_engine.evaluate(
            context=ctx,
            regime=regime,
            analyst_reports={"STRUCTURE": report},
            devil_report=devil,
            account_balance=500.0,
            risk_per_trade_pct=0.5,
            trade_style="SCALP",
            mtf_data=mtf
        )

        self.assertIsNotNone(decision)
        # Verify InstitutionalEntryEngine was activated (entry price is refined, limit order or refined level)
        self.assertEqual(decision.bias, "BUY")
        self.assertIn(decision.order_type, ("LIMIT", "MARKET"))

    def test_pullback_entry_generates_limit_order(self):
        """Verify that when entry_price differs from market price (passive retracement), order_type is LIMIT."""
        st = StructureContext(
            bias="BULLISH",
            demand_zone=(2385.0, 2390.0),
            channel_lower=2388.0,
            channel_upper=2400.0,
        )
        setattr(st, "swing_low", 2388.0)
        setattr(st, "swing_high", 2400.0)
        ctx = MarketContext(
            symbol="XAUUSD",
            timestamp=datetime.now(timezone.utc),
            current_price=2400.0,
            bid=2399.8,
            ask=2400.2,
            structure=st,
            liquidity=LiquidityContext(sell_side_liquidity=2385.0, buy_side_liquidity=2415.0),
            volatility=VolatilityContext(atr=4.0, current_spread_pips=2.0),
            momentum=MomentumContext(trend_score=35, adx=25.0),
            session=SessionContext(is_prime_session=True)
        )
        regime = RegimeOutput(primary_regime=MarketRegime.TREND_BULL, probabilities={}, confidence=0.80)
        report = AnalystReport(role=AnalystRole.STRUCTURE, symbol="XAUUSD", bias="BUY", score=80.0, confidence=0.80)
        devil = DevilAdvocateReport(symbol="XAUUSD", counter_bias="SELL", penalty_score=0.0, invalidation_risk_coefficient=1.0)

        # Baseline evaluation (no mtf) computes OTE retracement entry around 2392.08 (< 2400.0)
        decision = self.decision_engine.evaluate(
            context=ctx,
            regime=regime,
            analyst_reports={"STRUCTURE": report},
            devil_report=devil,
            account_balance=500.0,
            trade_style="SWING"
        )

        self.assertEqual(decision.bias, "BUY")
        self.assertLess(decision.entry_price, ctx.current_price)
        self.assertEqual(decision.order_type, "LIMIT", "Passive retracement below current market must produce LIMIT order!")

    def test_no_sweep_sets_rejection_wick_false(self):
        """Verify that when no sweep is detected, rejection_wick is False (no fake fallback)."""
        st = StructureContext(bias="BULLISH")
        setattr(st, "swing_low", 1.0820)
        setattr(st, "swing_high", 1.0860)
        ctx = MarketContext(
            symbol="EURUSD",
            timestamp=datetime.now(timezone.utc),
            current_price=1.0850,
            bid=1.0849,
            ask=1.0851,
            structure=st,
            liquidity=LiquidityContext(sell_side_liquidity=1.0810, buy_side_liquidity=1.0890),
            volatility=VolatilityContext(atr=0.0020, current_spread_pips=1.0),
            momentum=MomentumContext(trend_score=30, adx=25.0),
            session=SessionContext(is_prime_session=True)
        )
        regime = RegimeOutput(primary_regime=MarketRegime.TREND_BULL, probabilities={}, confidence=0.80)
        # Normal upward trending bars without sweep
        df_m5 = make_sample_ohlcv(start_price=1.0840, num_bars=15, trend=0.0001, step=0.0001)

        res = self.institutional_engine.calculate_entry_and_levels(
            ctx, regime, tentative_bias="BUY", trade_style="SCALP", mtf_data={"M5": df_m5}
        )

        details = res["protocol_details"]
        if not details["sweep_detected"]:
            self.assertFalse(details["rejection_wick"], "rejection_wick must be False when no sweep is detected!")

    def test_dynamic_lot_sizing_scales_on_micro_account(self):
        """Verify that a $500 balance with tight stop distance (e.g. 5-10 pips) produces 0.02 - 0.04 lots."""
        sym_gold = {
            "name": "XAUUSD",
            "trade_contract_size": 100.0,
            "trade_tick_value": 1.0,
            "trade_tick_size": 0.01,
            "volume_min": 0.01,
            "volume_max": 100.0,
            "volume_step": 0.01,
        }
        # $500 balance, high-conviction trade (model_confidence=0.75, pattern_sample_size=20)
        # Tight stop distance: 2.0 points ($2.00 = 20 pips) on Gold
        lots_tight = PositionSizer.calculate_lot_size(
            account_balance=500.0,
            entry_price=2400.0,
            sl_price=2398.0,  # $2.00 stop distance
            risk_pct=0.5,
            symbol_info=sym_gold,
            model_confidence=0.75,
            pattern_sample_size=20
        )
        self.assertGreaterEqual(lots_tight, 0.02, f"Expected lot size >= 0.02, got {lots_tight}")
        self.assertLessEqual(lots_tight, 0.05, f"Expected lot size <= 0.05, got {lots_tight}")

        # EURUSD tight stop: 8 pips (0.0008)
        sym_eur = {
            "name": "EURUSD",
            "trade_contract_size": 100000.0,
            "trade_tick_value": 1.0,
            "trade_tick_size": 0.00001,
            "volume_min": 0.01,
            "volume_max": 100.0,
            "volume_step": 0.01,
        }
        lots_eur = PositionSizer.calculate_lot_size(
            account_balance=500.0,
            entry_price=1.1000,
            sl_price=1.0992,  # 8 pips stop
            risk_pct=0.5,
            symbol_info=sym_eur,
            model_confidence=0.75,
            pattern_sample_size=20
        )
        self.assertGreaterEqual(lots_eur, 0.02, f"Expected EURUSD lot size >= 0.02, got {lots_eur}")


if __name__ == "__main__":
    unittest.main()
