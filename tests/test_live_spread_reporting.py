"""The real per-bar spread is carried, reported, and consumed by the gates.

The live path used to feed every caller the registry constant
(`_spec.typical_spread_pips`), so `spread_ratio` was structurally 1.0 and the
trade journal recorded the constant rather than the spread actually quoted.
The real spread is available — MT5's `copy_rates_from_pos` returns a per-bar
`spread` column (in **points**) that `data_feed` projected away.

This file pins the wiring of that real spread (2026-10-23 P0 audit):

* `data_feed.fetch_rates` keeps the `spread` column when the broker provides it.
* `MarketContextEngine.build_context` converts it to pips into
  `MarketContext.live_spread_pips` and uses it for `ask` and
  `volatility.current_spread_pips`, so spread gates see what the broker is
  actually quoting. It falls back to the registry constant when absent.
* `execution_engine` records the live spread in `TRADE_DB.log_trade`.
* a context that predates the field still works.

Hermetic: no MT5, no sockets, no real DB (the shared conftest swaps the trade
journal for a per-test temp file).
"""
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from jarvis.data.schemas import (
    MarketContext, StructureContext, LiquidityContext, VolatilityContext,
    MomentumContext, SessionContext, DecisionObject, RegimeOutput, MarketRegime,
    TradeQualityGateResult,
)
from jarvis.data.symbol_registry import resolve as resolve_symbol
from jarvis.execution.execution_engine import ExecutionEngine
from jarvis.execution.mt5_client import MT5Client
from jarvis.application.state_manager import StateManager
from jarvis.market import data_feed as data_feed_mod
from jarvis.market.data_feed import DataFeedEngine
from jarvis.market.market_context import MarketContextEngine

UTC = timezone.utc


# --------------------------------------------------------------------------
# fakes / helpers
# --------------------------------------------------------------------------

class _Structure:
    def analyze_structure(self, df):
        return StructureContext(bias="NEUTRAL")


class _Liquidity:
    def analyze_liquidity(self, df, **kwargs):
        return LiquidityContext()


class _Momentum:
    def analyze_momentum(self, df):
        return MomentumContext()


class _OrderFlow:
    def analyze_order_flow(self, df):
        return {}


class _EchoVolatility:
    """Records the kwargs it was handed and echoes `current_spread_pips` back.

    The echo matters: the regression guard compares the value the volatility
    engine *received* against a build with no spread column at all.
    """

    def __init__(self):
        self.kwargs = []

    def analyze_volatility(self, df, current_spread_pips=2.0,
                           max_allowed_spread_pips=35.0):
        self.kwargs.append({
            "current_spread_pips": current_spread_pips,
            "max_allowed_spread_pips": max_allowed_spread_pips,
        })
        return VolatilityContext(
            current_spread_pips=current_spread_pips,
            max_allowed_spread_pips=max_allowed_spread_pips,
        )


def make_engine():
    vol = _EchoVolatility()
    engine = MarketContextEngine(
        structure_engine=_Structure(),
        liquidity_engine=_Liquidity(),
        volatility_engine=vol,
        momentum_engine=_Momentum(),
        order_flow_engine=_OrderFlow(),
    )
    return engine, vol


def frame(spread=None, n=60, price=1.1000):
    idx = pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC")
    df = pd.DataFrame(index=idx)
    for col in ("open", "high", "low", "close"):
        df[col] = price
    df["volume"] = 1000.0
    df["time"] = idx
    if spread is not None:
        df["spread"] = spread
    return df


def build(symbol="EURUSD", spread=None, price=1.1000, spread_arg=0.7):
    engine, vol = make_engine()
    ctx = engine.build_context(
        symbol,
        {"primary": frame(spread=spread, price=price)},
        current_spread_pips=spread_arg,
        trade_style="SWING",
    )
    return ctx, vol


# --------------------------------------------------------------------------
# 1. points → pips conversion
# --------------------------------------------------------------------------

class TestConversion:

    def test_eurusd_19_points_is_1_9_pips(self):
        """5-digit FX: point = 1e-5, pip = 1e-4, so 19 points → 1.9 pips."""
        spec = resolve_symbol("EURUSD")
        assert spec.digits == 5
        assert spec.pip_size == pytest.approx(1e-4)
        ctx, _ = build("EURUSD", spread=19)
        assert ctx.live_spread_pips == pytest.approx(1.9)

    def test_a_symbol_where_pip_size_differs_from_the_point(self):
        """USDJPY: digits=3 (point 1e-3) but pip_size=0.01, so the conversion
        is `points * 1e-3 / 1e-2 = points / 10`. Using `points * pip_size`
        would be wrong by 100x here, which is the bug the formula avoids."""
        spec = resolve_symbol("USDJPY")
        assert spec.digits == 3
        assert spec.pip_size == pytest.approx(0.01)
        ctx, _ = build("USDJPY", spread=20, price=150.0)
        assert ctx.live_spread_pips == pytest.approx(2.0)

    def test_the_last_row_is_used_not_the_first(self):
        df = frame(spread=19)
        df["spread"] = np.arange(len(df))  # 0..59
        engine, _ = make_engine()
        ctx = engine.build_context("EURUSD", {"primary": df},
                                   current_spread_pips=0.7, trade_style="SWING")
        # last row is 59 points → 5.9 pips
        assert ctx.live_spread_pips == pytest.approx(5.9)

    def test_an_explicit_live_spread_argument_wins_over_the_column(self):
        engine, _ = make_engine()
        ctx = engine.build_context(
            "EURUSD", {"primary": frame(spread=19)},
            current_spread_pips=0.7, trade_style="SWING",
            live_spread_pips=4.2,
        )
        assert ctx.live_spread_pips == pytest.approx(4.2)


# --------------------------------------------------------------------------
# 2. absent / unusable column → None, no exception
# --------------------------------------------------------------------------

class TestUnusableSpread:

    def test_missing_column_is_none(self):
        ctx, _ = build("EURUSD", spread=None)
        assert ctx.live_spread_pips is None

    @pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf, 0, -5])
    def test_non_finite_or_non_positive_is_none(self, bad):
        ctx, _ = build("EURUSD", spread=bad)
        assert ctx.live_spread_pips is None

    def test_a_non_numeric_value_is_none(self):
        df = frame(spread=19)
        df["spread"] = df["spread"].astype(object)
        df.loc[df.index[-1], "spread"] = "n/a"
        engine, _ = make_engine()
        ctx = engine.build_context("EURUSD", {"primary": df},
                                   current_spread_pips=0.7, trade_style="SWING")
        assert ctx.live_spread_pips is None

    def test_an_empty_primary_is_none(self):
        engine, _ = make_engine()
        ctx = engine.build_context("EURUSD", {"primary": pd.DataFrame()},
                                   current_spread_pips=0.7, trade_style="SWING")
        assert ctx.live_spread_pips is None

    def test_a_zero_pip_size_falls_back_to_none(self):
        spec = SimpleNamespace(digits=5, pip_size=0.0)
        df = frame(spread=19)
        assert MarketContextEngine._live_spread_pips_from_frame(df, spec) is None

    def test_zero_digits_falls_back_to_none(self):
        spec = SimpleNamespace(digits=0, pip_size=1e-4)
        df = frame(spread=19)
        assert MarketContextEngine._live_spread_pips_from_frame(df, spec) is None


# --------------------------------------------------------------------------
# 3. the regression guard — live spread must reach every spread consumer
# --------------------------------------------------------------------------

class TestDecisionPathUsesLiveSpread:

    def test_bid_is_stable_but_ask_and_volatility_use_the_live_spread(self):
        """An extra `spread` column must move the quoted ask and the spread the
        volatility engine is handed, while the close-derived bid stays put."""
        spec = resolve_symbol("EURUSD")
        with_col, vol_with = build("EURUSD", spread=19, price=1.1234, spread_arg=0.7)
        without, vol_without = build("EURUSD", spread=None, price=1.1234, spread_arg=0.7)

        assert with_col.bid == without.bid
        assert with_col.current_price == without.current_price
        assert with_col.ask == pytest.approx(1.1234 + 1.9 * spec.pip_size)
        assert without.ask == pytest.approx(1.1234 + 0.7 * spec.pip_size)
        assert with_col.volatility.current_spread_pips == pytest.approx(1.9)
        assert without.volatility.current_spread_pips == pytest.approx(0.7)
        assert vol_with.kwargs[0]["current_spread_pips"] == pytest.approx(1.9)
        assert vol_without.kwargs[0]["current_spread_pips"] == pytest.approx(0.7)

        assert with_col.live_spread_pips == pytest.approx(1.9)
        assert without.live_spread_pips is None

    def test_the_volatility_engine_gets_the_live_spread(self):
        """The measured spread wins over the registry constant."""
        ctx, vol = build("EURUSD", spread=19, spread_arg=0.7)
        assert vol.kwargs[0]["current_spread_pips"] == pytest.approx(1.9)
        assert ctx.volatility.current_spread_pips == pytest.approx(1.9)

    def test_the_ask_uses_the_live_spread(self):
        spec = resolve_symbol("EURUSD")
        ctx, _ = build("EURUSD", spread=19, price=1.1000, spread_arg=0.7)
        assert ctx.ask == pytest.approx(1.1000 + 1.9 * spec.pip_size)


# --------------------------------------------------------------------------
# 4. execution_engine records the live spread (and falls back safely)
# --------------------------------------------------------------------------

def _decision(ctx):
    return DecisionObject(
        symbol="EURUSD",
        timestamp=datetime.now(timezone.utc),
        regime=RegimeOutput(primary_regime=MarketRegime.TREND_BULL,
                            probabilities={}, confidence=0.85),
        bias="BUY",
        probabilities={"buy": 0.7, "sell": 0.1, "no_trade": 0.2},
        strategy="TREND_PULLBACK",
        order_type="MARKET",
        entry_price=1.1000,
        stop_loss=1.0950,
        take_profit=1.1150,
        risk_reward_ratio=3.0,
        calculated_risk_percent=0.5,
        expected_value=15.0,
        model_confidence=0.75,
        adversarial_penalty=0.0,
        invalidation_levels=[],
        bull_case=[],
        bear_case=[],
        risk_factors=[],
        quality_gate=TradeQualityGateResult(passed=True, checks={}),
        decision="EXECUTE",
        execution_authorized=True,
        context=ctx,
    )


def _capture_log_trade(monkeypatch, journal):
    captured = {}

    def spy(**kwargs):
        captured.update(kwargs)
        return None

    monkeypatch.setattr(journal, "log_trade", spy)
    return captured


class TestExecutionRecording:

    def test_log_trade_prefers_the_live_spread(self, monkeypatch,
                                               _hermetic_trade_journal):
        ctx, _ = build("EURUSD", spread=19, spread_arg=0.7)
        assert ctx.live_spread_pips == pytest.approx(1.9)
        captured = _capture_log_trade(monkeypatch, _hermetic_trade_journal)

        engine = ExecutionEngine(MT5Client(mode="paper"), StateManager())
        res = engine.execute_decision(_decision(ctx), lots=0.01)
        assert res.get("status") == "FILLED"
        assert captured["spread_pips"] == pytest.approx(1.9)

    def test_log_trade_falls_back_to_the_volatility_spread(self, monkeypatch,
                                                           _hermetic_trade_journal):
        ctx, _ = build("EURUSD", spread=None, spread_arg=0.7)
        assert ctx.live_spread_pips is None
        captured = _capture_log_trade(monkeypatch, _hermetic_trade_journal)

        engine = ExecutionEngine(MT5Client(mode="paper"), StateManager())
        res = engine.execute_decision(_decision(ctx), lots=0.01)
        assert res.get("status") == "FILLED"
        assert captured["spread_pips"] == pytest.approx(0.7)

    def test_a_context_without_the_field_does_not_break_execution(self, monkeypatch,
                                                                  _hermetic_trade_journal):
        """An object built before `live_spread_pips` existed (or any foreign
        context) must still execute and record the fallback spread."""
        bare = SimpleNamespace(
            session=SessionContext(),
            momentum=MomentumContext(),
            volatility=VolatilityContext(current_spread_pips=0.7),
            mtf_alignment={},
        )
        assert not hasattr(bare, "live_spread_pips")
        captured = _capture_log_trade(monkeypatch, _hermetic_trade_journal)

        engine = ExecutionEngine(MT5Client(mode="paper"), StateManager())
        res = engine.execute_decision(_decision(bare), lots=0.01)
        assert res.get("status") == "FILLED"
        assert captured["spread_pips"] == pytest.approx(0.7)


# --------------------------------------------------------------------------
# 5. data_feed keeps the column (Step 0)
# --------------------------------------------------------------------------

def _rates(n=60, with_spread=True, spread=19):
    """A structured array shaped like MT5's `copy_rates_from_pos` output."""
    now = int(time.time())
    names = ["time", "open", "high", "low", "close", "tick_volume"]
    if with_spread:
        names.append("spread")
    names.append("real_volume")
    rows = []
    for i in range(n):
        row = [now - (n - i) * 60, 1.1, 1.11, 1.09, 1.1, 1000]
        if with_spread:
            row.append(spread)
        row.append(0)
        rows.append(tuple(row))
    dtype = [(name, "i8" if name in ("time", "tick_volume", "spread", "real_volume")
              else "f8") for name in names]
    return np.array(rows, dtype=dtype)


class _FakeMT5:
    def __init__(self, rates):
        self._rates = rates
        self.calls = []

    def copy_rates_from_pos(self, symbol, tf, pos, count):
        self.calls.append((symbol, tf, pos, count))
        return self._rates

    def symbol_select(self, symbol, enable):
        return True


class _FakeClient:
    mode = "paper"

    def resolve_symbol_name(self, symbol):
        return symbol


@pytest.fixture
def live_feed(monkeypatch):
    """Drive `fetch_rates` through its real MT5 branch with a fake terminal."""
    def _make(rates):
        fake_mt5 = _FakeMT5(rates)
        monkeypatch.setattr(data_feed_mod, "MT5_AVAILABLE", True)
        monkeypatch.setattr(data_feed_mod, "mt5", fake_mt5)
        monkeypatch.setattr(data_feed_mod, "ensure_mt5_terminal", lambda: True)
        monkeypatch.setattr(data_feed_mod, "broker_utc_offset", lambda **kw: 0)
        return DataFeedEngine(mt5_client=_FakeClient(), timeout_sec=5.0)
    return _make


class TestDataFeedKeepsSpread:

    def test_the_spread_column_is_retained_in_points(self, live_feed):
        feed = live_feed(_rates(with_spread=True, spread=19))
        df = feed.fetch_rates("EURUSD", timeframe="H1", num_bars=60)
        assert df.attrs["data_source"] == "LIVE_MT5"
        assert "spread" in df.columns
        # Kept in MT5 points, not converted here.
        assert int(df["spread"].iloc[-1]) == 19
        assert {"open", "high", "low", "close", "volume"} <= set(df.columns)

    def test_a_source_without_spread_still_builds(self, live_feed):
        feed = live_feed(_rates(with_spread=False))
        df = feed.fetch_rates("EURUSD", timeframe="H1", num_bars=60)
        assert df.attrs["data_source"] == "LIVE_MT5"
        assert "spread" not in df.columns
        assert {"open", "high", "low", "close", "volume"} <= set(df.columns)
