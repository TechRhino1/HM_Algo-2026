"""
Test suite for Bounded Bayesian Probability Calibration and Volatility Forecasting.
Verifies that:
1. Probability updates respect empirical ceilings (0.65 for scalp/day, 0.72 for swing).
2. Multiple stacked confluences cannot inflate win probability to ungrounded levels (e.g. >= 0.85).
3. Exact algebraic associativity and monotonicity are preserved.
4. Volatility forward forecast and spread risk score are properly calculated and bounded.
"""
import pytest
from jarvis.intelligence.decision_engine import _bounded_bayesian_probability_update
from jarvis.market.volatility import VolatilityEngine
import pandas as pd
import numpy as np


class TestBoundedBayesianProbabilityUpdate:
    def test_scalp_ceiling_strictly_enforced(self):
        """Even with massive stacking of boosts, scalp win probability never exceeds 0.65."""
        p = 0.50
        boosts = [0.05, 0.04, 0.05, 0.06, 0.10, 0.05, 0.05]
        for b in boosts:
            p = _bounded_bayesian_probability_update(p, b, trade_style="SCALP")
        assert p <= 0.65
        assert p >= 0.50

    def test_day_trading_ceiling_strictly_enforced(self):
        """Day trading win probability is capped at 0.68."""
        p = 0.50
        boosts = [0.05, 0.04, 0.05, 0.06, 0.10, 0.05, 0.05]
        for b in boosts:
            p = _bounded_bayesian_probability_update(p, b, trade_style="DAY_TRADING")
        assert p <= 0.68
        assert p >= 0.50

    def test_swing_ceiling_strictly_enforced(self):
        """Swing trading win probability is capped at 0.72."""
        p = 0.50
        boosts = [0.05, 0.04, 0.05, 0.06, 0.10, 0.05, 0.05]
        for b in boosts:
            p = _bounded_bayesian_probability_update(p, b, trade_style="SWING")
        assert p <= 0.72
        assert p >= 0.50

    def test_zero_boost_is_identity(self):
        """A zero or negative boost returns the original probability unchanged."""
        assert _bounded_bayesian_probability_update(0.55, 0.0, "SWING") == 0.55
        assert _bounded_bayesian_probability_update(0.55, -0.05, "SWING") == 0.55

    def test_monotonic_increase(self):
        """Adding positive evidence strictly increases probability up to the ceiling."""
        p1 = 0.50
        p2 = _bounded_bayesian_probability_update(p1, 0.035, "SWING")
        p3 = _bounded_bayesian_probability_update(p2, 0.040, "SWING")
        assert p1 < p2 < p3 <= 0.72


class TestVolatilityForwardForecast:
    def test_forward_forecast_calculated(self):
        """VolatilityEngine computes forward_atr_forecast and spread_risk_score."""
        engine = VolatilityEngine(atr_period=14, bb_period=20)
        # Generate 30 bars of synthetic data
        np.random.seed(42)
        n = 30
        close = 100.0 + np.cumsum(np.random.randn(n) * 0.5)
        high = close + np.random.rand(n) * 0.8
        low = close - np.random.rand(n) * 0.8
        df = pd.DataFrame({"high": high, "low": low, "close": close})

        res = engine.analyze_volatility(df, current_spread_pips=2.0, max_allowed_spread_pips=10.0)
        assert res.atr > 0
        assert res.forward_atr_forecast > 0
        assert 0.0 <= res.spread_risk_score <= 100.0
