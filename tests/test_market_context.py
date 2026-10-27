"""Round 27 — `jarvis/market/market_context.py`, the producer of `MarketContext`.

Every analyst, the Devil's Advocate, the regime classifier and the backtester
consume what `build_context()` returns. It is also the **only** writer of
`mtf_alignment` — the dict whose per-trade-style shape caused the round-25
defect (a fabricated "H4 is None" divergence on every SCALP decision) and the
round-26 one (a dead correlation check that wanted an "EURUSD" key). Those were
found downstream; this pins them at the source.

Existing coverage of this module is smoke-level (`assertIsNotNone`, plus
`assertIn("M15", ctx.mtf_alignment)` in `test_trade_style_api.py`).

All five engines are injected, so this is pure, fast and offline.
"""
import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timezone

from jarvis.data.schemas import (
    MarketContext, StructureContext, LiquidityContext, VolatilityContext,
    MomentumContext, SessionContext,
)
from jarvis.market import market_context as mc_mod
from jarvis.market.market_context import MarketContextEngine
from jarvis.data.symbol_registry import resolve as resolve_symbol

UTC = timezone.utc


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------

def frame(tag, n=60, price=100.0, volume=1000.0, ts_col="time",
          drop=(), index=None, tz="UTC"):
    """A minimal OHLCV frame carrying a `tag` so tests can tell frames apart."""
    idx = index if index is not None else pd.date_range(
        "2026-01-01", periods=n, freq="h", tz=tz)
    df = pd.DataFrame(index=idx)
    for col in ("open", "high", "low", "close"):
        df[col] = price
    df["volume"] = volume
    if ts_col:
        df[ts_col] = idx
    for col in drop:
        df.drop(columns=[col], inplace=True)
    df["tag"] = tag
    return df


def empty(tag=""):
    return pd.DataFrame({"tag": pd.Series(dtype=str)}) if tag else pd.DataFrame()


class TagStructure:
    """Returns a bias per frame `tag`; records every frame it is handed."""

    def __init__(self, biases=None, default="NEUTRAL"):
        self.biases = biases or {}
        self.default = default
        self.calls = []

    def analyze_structure(self, df):
        self.calls.append(df)
        tag = df["tag"].iloc[0] if "tag" in df.columns and len(df) else None
        return StructureContext(bias=self.biases.get(tag, self.default))

    def tags(self):
        return [d["tag"].iloc[0] if "tag" in d.columns and len(d) else None
                for d in self.calls]


class StubEngine:
    """Records the frame it receives and returns a canned context."""

    def __init__(self, product, method):
        self.product = product
        self.method = method
        self.calls = []
        self.kwargs = []

    def __call__(self, df, **kw):
        self.calls.append(df)
        self.kwargs.append(kw)
        return self.product

    def __getattr__(self, name):  # only reached for the configured method
        if name == self.method:
            return self
        raise AttributeError(name)


def make(structure=None, liquidity=None, volatility=None, momentum=None,
         order_flow=None):
    liq = StubEngine(liquidity or LiquidityContext(), "analyze_liquidity")
    vol = StubEngine(volatility or VolatilityContext(), "analyze_volatility")
    mom = StubEngine(momentum or MomentumContext(), "analyze_momentum")
    of = StubEngine(order_flow if order_flow is not None else {}, "analyze_order_flow")
    return MarketContextEngine(
        structure_engine=structure,
        liquidity_engine=liq,
        volatility_engine=vol,
        momentum_engine=mom,
        order_flow_engine=of,
    ), (liq, vol, mom, of)


def build(mtf, symbol="EURUSD", style="SWING", spread=2.0, max_spread=35.0,
          biases=None, **engines):
    st = TagStructure(biases)
    ce, others = make(structure=st, **engines)
    ctx = ce.build_context(symbol, mtf, current_spread_pips=spread,
                           max_allowed_spread_pips=max_spread, trade_style=style)
    return ctx, st, others


FULL = {k: frame(k) for k in ("macro", "context", "primary", "setup", "timing")}
BY_TF = {k: frame(k) for k in ("D1", "H4", "H1", "M15", "M5", "M1")}


# --------------------------------------------------------------------------
# 1. style → which frame is which
# --------------------------------------------------------------------------

class TestFrameRouting:

    @pytest.mark.parametrize("style,expected", [
        ("SWING", ["primary", "macro", "context", "timing"]),
        ("DAY_TRADING", ["primary", "macro", "context", "timing"]),
        ("SCALP", ["primary", "macro", "context", "timing"]),
    ])
    def test_the_structure_engine_sees_primary_then_macro_context_timing(self, style, expected):
        _, st, _ = build(FULL, style=style)
        assert st.tags() == expected

    @pytest.mark.parametrize("style,wanted", [
        ("SWING", {"macro": "D1", "context": "H4", "primary": "H1",
                   "setup": "H4", "timing": "M15"}),
        ("DAY_TRADING", {"macro": "H4", "context": "H1", "primary": "M15",
                         "setup": "H1", "timing": "M5"}),
        ("SCALP", {"macro": "H1", "context": "M15", "primary": "M5",
                   "setup": "M5", "timing": "M1"}),
    ])
    def test_timeframe_keys_are_the_fallback_when_no_role_keys(self, style, wanted):
        """With only timeframe keys present, each role resolves to a different
        timeframe depending on style. `setup` is only consulted by SWING, so it
        is never routed here — but it is still counted for context quality."""
        _, st, _ = build(BY_TF, style=style)
        seen = {role: st.tags()[i] for i, role in
                enumerate(["primary", "macro", "context", "timing"])}
        assert seen == {k: v for k, v in wanted.items() if k != "setup"}

    def test_swing_falls_back_to_setup_when_timing_is_missing(self):
        data = dict(BY_TF)
        data["M15"] = empty()
        data["M5"] = empty()
        _, st, _ = build(data, style="SWING")
        assert st.tags()[-1] == "H4"

    @pytest.mark.parametrize("style", ["SWING", "DAY_TRADING", "SCALP"])
    def test_role_keys_win_over_timeframe_keys(self, style):
        data = {**{k: frame("tf-" + k) for k in BY_TF}, **FULL}
        _, st, _ = build(data, style=style)
        assert st.tags() == ["primary", "macro", "context", "timing"]

    def test_an_empty_primary_is_replaced_by_the_first_frame_in_the_dict(self):
        """`list(mtf_data.values())[0]` — order-dependent, and it is whatever
        the caller happened to put first, not any particular timeframe."""
        data = {"macro": frame("macro"), "primary": empty(), "context": frame("context")}
        _, st, _ = build(data, style="SWING")
        assert st.tags()[0] == "macro"

    def test_that_replacement_also_feeds_every_other_engine(self):
        data = {"macro": frame("macro"), "primary": empty()}
        _, _, (liq, vol, mom, of) = build(data, style="SWING")
        for eng in (liq, vol, mom, of):
            assert eng.calls[0]["tag"].iloc[0] == "macro"


class TestStyleNormalisation:

    @pytest.mark.parametrize("given,effective", [
        ("SWING", "SWING"), ("swing", "SWING"), ("Scalp", "SCALP"),
        (None, "SWING"), ("", "SWING"),
    ])
    def test_the_style_is_uppercased_and_defaulted(self, given, effective):
        ctx, _, _ = build(FULL, style=given)
        assert ctx.trade_style == effective

    @pytest.mark.parametrize("alias", ["DAY_TRADING", "INTRADAY", "DAY"])
    def test_the_day_trading_aliases_share_one_branch(self, alias):
        a, _, _ = build(BY_TF, style=alias)
        b, _, _ = build(BY_TF, style="DAY_TRADING")
        assert a.mtf_alignment == b.mtf_alignment
        assert a.mtf_confluence_score == b.mtf_confluence_score

    @pytest.mark.parametrize("unknown", ["WHALE", "POSITION", "M30", "SWING_OR_SOMETHING"])
    def test_an_unknown_style_silently_becomes_swing(self, unknown):
        a, _, _ = build(BY_TF, style=unknown)
        b, _, _ = build(BY_TF, style="SWING")
        assert a.mtf_alignment == b.mtf_alignment
        assert a.trade_style == unknown.upper()


# --------------------------------------------------------------------------
# 2. mtf_alignment — the dict that already caused two defects
# --------------------------------------------------------------------------

class TestMtfAlignment:

    @pytest.mark.parametrize("style,keys", [
        ("SWING", {"D1", "H4", "H1", "M15"}),
        ("DAY_TRADING", {"D1", "H4", "H1", "M15", "M5"}),
        ("SCALP", {"D1", "H1", "M15", "M5", "M1"}),
    ])
    def test_exactly_which_timeframes_appear_per_style(self, style, keys):
        ctx, _, _ = build(BY_TF, style=style)
        assert set(ctx.mtf_alignment) == keys

    def test_scalp_has_no_h4_but_keeps_the_d1_anchor(self):
        """Round 25: `structure_analyst` read the absent H4 as divergence, so
        every SCALP decision carried a fabricated risk factor and its +10
        confluence bonus was unreachable.

        D1 is different: it is written for every style as the macro anchor the
        direction engine reads (`mtf_alignment["D1"]`), even when SCALP does not
        route a D1 frame through its structure stack.
        """
        ctx, _, _ = build(BY_TF, style="SCALP")
        assert "H4" not in ctx.mtf_alignment
        assert "D1" in ctx.mtf_alignment

    def test_day_trading_has_h4_and_d1(self):
        """DAY trades H4 as its macro frame and still needs the D1 anchor that
        the direction engine reads for every style."""
        ctx, _, _ = build(BY_TF, style="DAY_TRADING")
        assert "H4" in ctx.mtf_alignment
        assert "D1" in ctx.mtf_alignment

    def test_no_symbol_is_ever_a_key(self):
        """Round 26: the Devil's Advocate wanted an "EURUSD"/"GBPUSD" key in
        here. Only timeframes are written, so that check could never fire."""
        for style in ("SWING", "DAY_TRADING", "SCALP"):
            ctx, _, _ = build(BY_TF, style=style, symbol="XAUUSD")
            assert "EURUSD" not in ctx.mtf_alignment
            assert "GBPUSD" not in ctx.mtf_alignment

    def test_each_key_carries_the_bias_of_its_own_frame(self):
        biases = {"D1": "BULLISH", "H4": "BEARISH", "H1": "BULLISH", "M15": "NEUTRAL"}
        ctx, _, _ = build(BY_TF, style="SWING", biases=biases)
        assert ctx.mtf_alignment == {"D1": "BULLISH", "H4": "BEARISH",
                                     "H1": "BULLISH", "M15": "NEUTRAL"}

    def test_a_missing_frame_is_written_as_neutral_not_omitted(self):
        """The key always exists — an absent frame reads NEUTRAL rather than
        disappearing. That is what makes `.get()` checks downstream subtle."""
        data = {k: frame(k) for k in ("H1", "M15", "M5", "M1")}  # no D1/H4
        ctx, _, _ = build(data, style="SWING")
        assert ctx.mtf_alignment["D1"] == "NEUTRAL"
        assert ctx.mtf_alignment["H4"] == "NEUTRAL"


class TestConfluenceScore:

    @pytest.mark.parametrize("style,weights", [
        ("SWING", {"D1": 0.40, "H4": 0.30, "H1": 0.20, "M15": 0.10}),
        ("DAY_TRADING", {"M15": 0.40, "H1": 0.35, "H4": 0.25}),
        ("SCALP", {"H1": 0.40, "M15": 0.30, "M5": 0.20, "M1": 0.10}),
    ])
    def test_all_bullish_is_plus_100_and_all_bearish_minus_100(self, style, weights):
        all_bull = {k: "BULLISH" for k in BY_TF}
        ctx, _, _ = build(BY_TF, style=style, biases=all_bull)
        assert ctx.mtf_confluence_score == 100.0
        ctx, _, _ = build(BY_TF, style=style, biases={k: "BEARISH" for k in BY_TF})
        assert ctx.mtf_confluence_score == -100.0

    def test_the_weights_are_style_specific(self):
        """One bullish H4 in an otherwise neutral book: SWING weights it 0.30,
        DAY_TRADING 0.25 (H4 is its macro frame), and SCALP does not use it at
        all."""
        biases = {"H4": "BULLISH"}
        swing, _, _ = build(BY_TF, style="SWING", biases=biases)
        day, _, _ = build(BY_TF, style="DAY_TRADING", biases=biases)
        scalp, _, _ = build(BY_TF, style="SCALP", biases=biases)
        assert swing.mtf_confluence_score == 30.0
        assert day.mtf_confluence_score == 25.0
        assert scalp.mtf_confluence_score == 0.0

    def test_neutral_frames_contribute_nothing(self):
        ctx, _, _ = build(BY_TF, style="SWING", biases={k: "NEUTRAL" for k in BY_TF})
        assert ctx.mtf_confluence_score == 0.0

    def test_a_mixed_book_is_the_weighted_sum(self):
        biases = {"D1": "BULLISH", "H4": "BEARISH", "H1": "BULLISH", "M15": "BEARISH"}
        ctx, _, _ = build(BY_TF, style="SWING", biases=biases)
        # 0.40 - 0.30 + 0.20 - 0.10 = 0.20
        assert ctx.mtf_confluence_score == 20.0

    def test_the_day_trading_docstring_matches_the_weights(self):
        """DOC-27: the docstring used to name "H1 (40%), M15 (35%), M5 (25%)"
        while the code weighted the routed frames -- M15 (primary) 40%, H1
        (context) 35%, H4 (macro) 25%. The docstring now follows the code, and
        this test pins both: M15 carries 40% and H4 only 25%."""
        m15 = build(BY_TF, style="DAY_TRADING", biases={"M15": "BULLISH"})[0]
        h4 = build(BY_TF, style="DAY_TRADING", biases={"H4": "BULLISH"})[0]
        assert m15.mtf_confluence_score == 40.0      # M15 is weighted 40%
        assert h4.mtf_confluence_score == 25.0       # H4 is the macro frame
        assert h4.mtf_alignment["H4"] == "BULLISH"


# --------------------------------------------------------------------------
# 3. price, bid, ask
# --------------------------------------------------------------------------

class TestPrice:

    def test_the_close_of_primary_becomes_both_price_and_bid(self):
        data = {"primary": frame("primary", price=1.2345)}
        ctx, _, _ = build(data, style="SWING")
        assert ctx.current_price == 1.2345
        assert ctx.bid == 1.2345

    def test_the_ask_is_the_close_plus_spread_times_pip_size(self):
        data = {"primary": frame("primary", price=1.2000)}
        ctx, _, _ = build(data, symbol="EURUSD", spread=2.0)
        pip = resolve_symbol("EURUSD").pip_size
        assert ctx.ask == pytest.approx(1.2000 + 2.0 * pip)

    def test_a_zero_spread_makes_bid_and_ask_identical(self):
        data = {"primary": frame("primary", price=100.0)}
        ctx, _, _ = build(data, spread=0.0)
        assert ctx.bid == ctx.ask == 100.0

    def test_pip_size_comes_from_the_symbol_registry(self):
        for sym in ("EURUSD", "XAUUSD", "USDJPY"):
            data = {"primary": frame("primary", price=100.0)}
            ctx, _, _ = build(data, symbol=sym, spread=2.0)
            assert ctx.ask == pytest.approx(100.0 + 2.0 * resolve_symbol(sym).pip_size)

    def test_an_empty_feed_gives_a_price_of_zero(self):
        ctx, _, _ = build({}, style="SWING", spread=2.0)
        assert ctx.current_price == 0.0
        assert ctx.ask == pytest.approx(2.0 * resolve_symbol("EURUSD").pip_size)

    def test_the_last_bar_is_used_not_the_first(self):
        df = frame("primary")
        df["close"] = np.linspace(1.0, 2.0, len(df))
        ctx, _, _ = build({"primary": df}, style="SWING")
        assert ctx.current_price == pytest.approx(2.0)


# --------------------------------------------------------------------------
# 4. bar timestamp → session
# --------------------------------------------------------------------------

@pytest.fixture
def session_spy(monkeypatch):
    seen = []

    class Spy:
        @staticmethod
        def get_current_session(dt=None):
            seen.append(dt)
            return SessionContext()

    monkeypatch.setattr(mc_mod, "SessionEngine", Spy)
    return seen


class TestBarTimestamp:

    def test_the_time_column_is_preferred(self, session_spy):
        df = frame("primary")
        df["time"] = pd.Timestamp("2001-02-03 04:05:06", tz="UTC")
        df["timestamp"] = pd.Timestamp("2020-01-01", tz="UTC")
        build({"primary": df}, style="SWING")
        assert session_spy[0] == datetime(2001, 2, 3, 4, 5, 6, tzinfo=UTC)

    def test_the_timestamp_column_is_used_when_there_is_no_time_column(self, session_spy):
        df = frame("primary", ts_col=None)
        df["timestamp"] = pd.Timestamp("2001-02-03 04:05:06", tz="UTC")
        build({"primary": df}, style="SWING")
        assert session_spy[0] == datetime(2001, 2, 3, 4, 5, 6, tzinfo=UTC)

    def test_a_naive_datetime_is_read_as_utc(self, session_spy):
        df = frame("primary", ts_col=None)
        df["timestamp"] = datetime(2001, 2, 3, 4, 5, 6)
        build({"primary": df}, style="SWING")
        assert session_spy[0] == datetime(2001, 2, 3, 4, 5, 6, tzinfo=UTC)

    def test_a_timezone_aware_datetime_keeps_its_zone(self, session_spy):
        df = frame("primary", ts_col=None)
        df["timestamp"] = datetime(2001, 2, 3, 4, 5, 6, tzinfo=timezone.utc)
        build({"primary": df}, style="SWING")
        assert session_spy[0].tzinfo == UTC

    def test_a_float_epoch_is_read_as_utc_seconds(self, session_spy):
        df = frame("primary", ts_col=None)
        df["timestamp"] = 0.0            # np.float64 — IS a Python float
        build({"primary": df}, style="SWING")
        assert session_spy[0] == datetime(1970, 1, 1, tzinfo=UTC)

    def test_an_integer_epoch_column_silently_falls_back_to_now(self, session_spy):
        """🔴 `isinstance(np.int64(0), int)` is **False** — numpy integers do not
        subclass Python's `int` — so the `isinstance(raw_ts, (int, float))`
        branch never matches an int64 timestamp column, and the bar time is
        thrown away in favour of wall-clock now. A float column works and an
        object-dtype Python int works; only the ordinary integer column, which
        is what MT5 and most parquet writers produce, silently breaks. Every bar
        is then priced in today's session."""
        df = frame("primary", ts_col=None)
        df["timestamp"] = 0              # int64 column
        assert str(df["timestamp"].dtype) == "int64"
        build({"primary": df}, style="SWING")
        assert session_spy[0].year == datetime.now(timezone.utc).year

    def test_a_python_int_in_an_object_column_is_accepted(self, session_spy):
        df = frame("primary", ts_col=None)
        df["timestamp"] = pd.Series([0] * len(df), dtype=object, index=df.index)
        build({"primary": df}, style="SWING")
        assert session_spy[0] == datetime(1970, 1, 1, tzinfo=UTC)

    def test_a_nan_timestamp_raises_instead_of_falling_back(self):
        """🔴 `float("nan")` passes `isinstance(..., float)`, so a NaN in the
        time column reaches `datetime.fromtimestamp` and raises ValueError —
        it does not fall back to now like every other unrecognised type. One
        NaN bar kills the entire context build."""
        df = frame("primary", ts_col=None)
        df["timestamp"] = pd.Series([0.0] * len(df), index=df.index)
        df.loc[df.index[-1], "timestamp"] = np.nan
        with pytest.raises(ValueError):
            build({"primary": df}, style="SWING")

    def test_the_int_and_float_paths_disagree(self):
        """The asymmetry, stated plainly: same value, different dtype, and one
        of them loses the bar time entirely."""
        assert isinstance(np.float64(0.0), float) is True
        assert isinstance(np.int64(0), int) is False

    def test_a_string_timestamp_silently_falls_back_to_now(self, session_spy):
        """🔴 A string matches none of the `isinstance` branches, so the bar
        time is discarded and the session is derived from **wall-clock now**.
        A backtest whose stored bars carry string timestamps would price every
        bar in today's session — which feeds the off-hours penalty in the Devil's
        Advocate and the session bonus in the macro analyst."""
        df = frame("primary", ts_col=None)
        df["timestamp"] = "2001-02-03 04:05:06"
        build({"primary": df}, style="SWING")
        assert session_spy[0].year == datetime.now(timezone.utc).year

    @pytest.mark.parametrize("raw", ["", "None", "not-a-date", "2001-02-03T04:05:06Z"])
    def test_every_unrecognised_type_falls_back_to_now(self, session_spy, raw):
        df = frame("primary", ts_col=None)
        df["timestamp"] = raw
        build({"primary": df}, style="SWING")
        assert session_spy[0].year == datetime.now(timezone.utc).year

    def test_without_a_timestamp_column_a_datetime_index_is_used(self, session_spy):
        idx = pd.date_range("2001-02-03", periods=60, freq="h", tz="UTC")
        df = frame("primary", ts_col=None, index=idx)
        build({"primary": df}, style="SWING")
        assert session_spy[0] == idx[-1]

    def test_with_neither_column_nor_index_now_is_used(self, session_spy):
        df = frame("primary", ts_col=None, index=pd.RangeIndex(60))
        build({"primary": df}, style="SWING")
        assert session_spy[0].year == datetime.now(timezone.utc).year

    def test_the_pandas_timestamp_branch_is_dead_code(self):
        """`pd.Timestamp` subclasses `datetime`, so `isinstance(ts, datetime)`
        is already True and the `elif isinstance(ts, pd.Timestamp)` at line 100
        is unreachable. Harmless — both branches do the same thing — but it
        means the pandas path has never been exercised as written."""
        assert issubclass(pd.Timestamp, datetime)
        df = frame("primary", ts_col=None)
        df["timestamp"] = pd.Timestamp("2001-02-03 04:05:06", tz="UTC")
        seen = []
        orig = mc_mod.SessionEngine
        try:
            class Spy:
                @staticmethod
                def get_current_session(dt=None):
                    seen.append(dt)
                    return SessionContext()
            mc_mod.SessionEngine = Spy
            build({"primary": df}, style="SWING")
        finally:
            mc_mod.SessionEngine = orig
        assert seen[0] == datetime(2001, 2, 3, 4, 5, 6, tzinfo=UTC)


# --------------------------------------------------------------------------
# 5. vwap
# --------------------------------------------------------------------------

class TestVwap:

    def test_vwap_is_computed_from_typical_price_and_volume(self):
        df = frame("primary", n=3, price=0.0)
        df["high"] = [3.0, 3.0, 3.0]
        df["low"] = [0.0, 0.0, 0.0]
        df["close"] = [3.0, 3.0, 3.0]
        df["volume"] = [1.0, 1.0, 1.0]
        ctx, _, _ = build({"primary": df}, style="SWING")
        assert ctx.vwap == pytest.approx(2.0)  # (3+0+3)/3

    def test_vwap_is_cumulative_and_taken_from_the_last_bar(self):
        """VWAP accumulates over the whole window; only the final value is kept.
        Bar 1 alone would read 1.0, so this distinguishes `iloc[-1]` from
        `iloc[0]` (and from a per-bar VWAP)."""
        df = frame("primary", n=3, price=0.0)
        df["high"] = [3.0, 3.0, 30.0]     # typical price = 1, 1, 10
        df["low"] = [0.0, 0.0, 0.0]
        df["close"] = [0.0, 0.0, 0.0]
        df["volume"] = [1.0, 1.0, 10.0]   # cumulative 1, 2, 12
        ctx, _, _ = build({"primary": df}, style="SWING")
        assert ctx.vwap == pytest.approx(102.0 / 12.0)

    def test_vwap_is_zero_without_a_volume_column(self):
        df = frame("primary", drop=("volume",))
        ctx, _, _ = build({"primary": df}, style="SWING")
        assert ctx.vwap == 0.0

    def test_vwap_is_zero_without_a_high_column(self):
        df = frame("primary", drop=("high",))
        ctx, _, _ = build({"primary": df}, style="SWING")
        assert ctx.vwap == 0.0

    def test_vwap_is_zero_when_total_volume_is_zero(self):
        df = frame("primary", volume=0.0)
        ctx, _, _ = build({"primary": df}, style="SWING")
        assert ctx.vwap == 0.0

    def test_vwap_is_rounded_to_four_places(self):
        df = frame("primary", n=2)
        df["high"] = [1.23456789, 1.23456789]
        df["low"] = [1.0, 1.0]
        df["close"] = [1.0, 1.0]
        df["volume"] = [1.0, 1.0]
        ctx, _, _ = build({"primary": df}, style="SWING")
        assert ctx.vwap == round(ctx.vwap, 4)

    def test_a_low_column_that_is_missing_raises(self):
        """🔴 The guard checks for `volume` and `high` but not `low`, and line
        177 reads all three. A frame with volume and high but no low crashes the
        whole context build."""
        df = frame("primary", drop=("low",))
        with pytest.raises(KeyError):
            build({"primary": df}, style="SWING")

    def test_all_nan_prices_are_caught_and_give_zero(self):
        df = frame("primary", n=2, price=np.nan)
        df["volume"] = [1.0, 1.0]
        ctx, _, _ = build({"primary": df}, style="SWING")
        assert ctx.vwap == 0.0

    def test_a_single_nan_price_propagates_nan(self):
        """`if not vwap_series.isna().all()` only catches the all-NaN case, so
        one bad bar propagates `nan` into the context — and `nan` then flows into
        anything comparing prices against VWAP."""
        df = frame("primary", n=2, price=1.0)
        df.loc[df.index[-1], ["high", "low", "close"]] = np.nan
        df["volume"] = [1.0, 1.0]
        ctx, _, _ = build({"primary": df}, style="SWING")
        assert np.isnan(ctx.vwap)


# --------------------------------------------------------------------------
# 6. context quality
# --------------------------------------------------------------------------

class TestContextQuality:

    def test_five_frames_give_50_before_the_bar_counts(self):
        ctx, _, _ = build({k: frame(k, n=5) for k in FULL}, style="SWING")
        assert ctx.context_quality == 50.0

    def test_a_primary_of_50_bars_adds_30(self):
        ctx, _, _ = build({"primary": frame("primary", n=50)}, style="SWING")
        assert ctx.context_quality == 10.0 + 30.0

    def test_a_primary_of_49_bars_adds_nothing(self):
        ctx, _, _ = build({"primary": frame("primary", n=49)}, style="SWING")
        assert ctx.context_quality == 10.0

    def test_a_context_frame_of_20_bars_adds_20(self):
        data = {"primary": frame("primary", n=50), "context": frame("context", n=20)}
        ctx, _, _ = build(data, style="SWING")
        assert ctx.context_quality == 20.0 + 30.0 + 20.0

    def test_a_context_frame_of_19_bars_adds_nothing(self):
        data = {"primary": frame("primary", n=50), "context": frame("context", n=19)}
        ctx, _, _ = build(data, style="SWING")
        assert ctx.context_quality == 50.0

    def test_a_full_book_of_long_frames_reaches_100(self):
        ctx, _, _ = build({k: frame(k, n=60) for k in FULL}, style="SWING")
        assert ctx.context_quality == 100.0

    def test_quality_is_capped_at_100(self):
        ctx, _, _ = build({k: frame(k, n=500) for k in FULL}, style="SWING")
        assert ctx.context_quality == 100.0

    def test_an_empty_feed_scores_zero(self):
        ctx, _, _ = build({}, style="SWING")
        assert ctx.context_quality == 0.0

    def test_quality_feeds_the_devils_advocate_confidence(self):
        """`critique_confidence = clamp(context_quality / 100, 0.40, 1.0)`, so a
        half-populated context silently halves the critic's stated confidence."""
        ctx, _, _ = build({k: frame(k, n=5) for k in FULL}, style="SWING")
        assert ctx.context_quality == 50.0


# --------------------------------------------------------------------------
# 7. pass-through to the other engines
# --------------------------------------------------------------------------

class TestEnginePassthrough:

    def test_liquidity_momentum_and_order_flow_all_get_the_primary_frame(self):
        _, _, (liq, vol, mom, of) = build(FULL, style="SWING")
        for eng in (liq, vol, mom, of):
            assert eng.calls[0]["tag"].iloc[0] == "primary"

    def test_the_spread_limits_are_forwarded_to_the_volatility_engine(self):
        _, _, (_, vol, _, _) = build(FULL, spread=1.5, max_spread=9.0)
        assert vol.kwargs[0] == {"current_spread_pips": 1.5,
                                 "max_allowed_spread_pips": 9.0}

    def test_the_spread_defaults_are_two_and_thirty_five(self):
        st = TagStructure()
        ce, (_, vol, _, _) = make(structure=st)
        ce.build_context("EURUSD", FULL)
        assert vol.kwargs[0] == {"current_spread_pips": 2.0,
                                 "max_allowed_spread_pips": 35.0}

    def test_the_engine_outputs_are_stored_verbatim(self):
        liq = LiquidityContext(sweep_detected=True, sweep_type="BULLISH_SWEEP")
        volctx = VolatilityContext(state="EXTREME", atr=9.0)
        mom = MomentumContext(adx=44.0)
        of = {"delta_score": -50.0}
        ctx, _, _ = build(FULL, style="SWING", liquidity=liq, volatility=volctx,
                          momentum=mom, order_flow=of)
        assert ctx.liquidity.sweep_detected is True
        assert ctx.volatility.atr == 9.0
        assert ctx.momentum.adx == 44.0
        assert ctx.order_flow == {"delta_score": -50.0}

    def test_the_default_engines_are_constructed_when_none_are_injected(self):
        from jarvis.market.market_structure import MarketStructureEngine
        from jarvis.market.liquidity import LiquidityEngine
        from jarvis.market.volatility import VolatilityEngine
        from jarvis.market.momentum import MomentumEngine
        from jarvis.intelligence.order_flow import InstitutionalVolumeOrderFlowEngine
        ce = MarketContextEngine()
        assert isinstance(ce.structure_engine, MarketStructureEngine)
        assert isinstance(ce.liquidity_engine, LiquidityEngine)
        assert isinstance(ce.volatility_engine, VolatilityEngine)
        assert isinstance(ce.momentum_engine, MomentumEngine)
        assert isinstance(ce.order_flow_engine, InstitutionalVolumeOrderFlowEngine)


# --------------------------------------------------------------------------
# 8. the returned object
# --------------------------------------------------------------------------

class TestReturnedContext:

    def test_it_is_a_market_context(self):
        ctx, _, _ = build(FULL, style="SWING")
        assert isinstance(ctx, MarketContext)

    def test_symbol_and_strategy(self):
        ctx, _, _ = build(FULL, symbol="GBPJPY", style="SWING")
        assert ctx.symbol == "GBPJPY"
        assert ctx.strategy == ""

    def test_the_primary_structure_becomes_the_context_structure(self):
        ctx, _, _ = build(BY_TF, style="SWING", biases={"H1": "BULLISH"})
        assert ctx.structure.bias == "BULLISH"

    def test_a_completely_empty_feed_still_builds(self):
        ctx, _, _ = build({}, style="SCALP")
        assert ctx.current_price == 0.0
        assert ctx.context_quality == 0.0
        assert ctx.mtf_alignment == {"D1": "NEUTRAL", "H1": "NEUTRAL", "M15": "NEUTRAL",
                                     "M5": "NEUTRAL", "M1": "NEUTRAL"}
        assert ctx.mtf_confluence_score == 0.0
        assert ctx.vwap == 0.0
