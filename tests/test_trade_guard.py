"""Tests for jarvis.risk.trade_guard.TradeGuard.

This is the last hard gate before an order goes to the broker: `risk_engine`
runs it as step 7 of authorisation and extends `rejection_reasons` from it. It
had no tests at all.

The fixture deliberately passes `None` for the DecisionObject fields the guard
never reads (regime, probabilities, quality_gate) — it is a plain dataclass with
no validation, and filling them in would only suggest they matter here.
"""

import math
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from jarvis.data.schemas import AccountSnapshot, DecisionObject
from jarvis.data.symbol_registry import resolve
from jarvis.risk.trade_guard import TradeGuard

UTC = timezone.utc
BROKER_TZ = timezone(timedelta(hours=2))  # XM in winter — MT5 timestamps run on this clock

# Registry caps, read rather than hardcoded so a registry edit shows up here.
EURUSD_CAP = resolve("EURUSD").max_spread_pips      # 2.0
XAUUSD_CAP = resolve("XAUUSD").max_spread_pips      # 5.0
BTCUSD_CAP = resolve("BTCUSD").max_spread_pips      # 3000.0


def decision(
    symbol="EURUSD",
    bias="BUY",
    entry=1.1000,
    stop_loss=1.0950,
    take_profit=1.1100,
    verdict="EXECUTE",
    bar_ts=None,
):
    """A DecisionObject with everything the guard does not read set to None."""
    return DecisionObject(
        symbol=symbol,
        timestamp=datetime(2026, 1, 7, 12, 0, tzinfo=UTC),
        regime=None,
        bias=bias,
        probabilities={},
        strategy="TEST",
        entry_price=entry,
        stop_loss=stop_loss,
        take_profit=take_profit,
        risk_reward_ratio=2.0,
        calculated_risk_percent=1.0,
        expected_value=1.0,
        model_confidence=0.7,
        adversarial_penalty=0.0,
        invalidation_levels=[],
        bull_case=[],
        bear_case=[],
        risk_factors=[],
        quality_gate=None,
        decision=verdict,
        context=SimpleNamespace(timestamp=bar_ts) if bar_ts is not None else None,
    )


def account(trade_allowed=True):
    return AccountSnapshot(
        login=1, server="XM", balance=10_000.0, equity=10_000.0, margin=0.0,
        free_margin=10_000.0, margin_level=0.0, leverage=500,
        trade_allowed=trade_allowed,
    )


def run(dec, acct=None, spread=0.5, override=None, cap=35.0, positions=None):
    return TradeGuard.validate_pre_execution(
        dec,
        acct if acct is not None else account(),
        positions if positions is not None else [],
        max_spread_pips=cap,
        current_spread_pips=spread,
        entry_authorized_override=override,
    )


def bar(hour, minute=0, tz=UTC):
    return datetime(2026, 1, 7, hour, minute, tzinfo=tz)


# ---------------------------------------------------------------------------
# Result shape — `risk_engine` does `guard.get("passed")`, so the keys matter.
# ---------------------------------------------------------------------------

class TestResultShape:
    def test_returns_passed_and_reasons(self):
        r = run(decision())
        assert set(r) == {"passed", "reasons"}
        assert isinstance(r["passed"], bool)
        assert isinstance(r["reasons"], list)

    def test_passed_is_exactly_the_absence_of_reasons(self):
        for dec, spread in [
            (decision(), 0.5),
            (decision(bias="BUY", stop_loss=1.1050), 0.5),
            (decision(verdict="HOLD"), 0.5),
            (decision(), EURUSD_CAP * 10),
        ]:
            r = run(dec, spread=spread)
            assert r["passed"] is (len(r["reasons"]) == 0)

    def test_a_clean_decision_passes_with_no_reasons(self):
        r = run(decision())
        assert r["passed"] is True
        assert r["reasons"] == []

    def test_every_failure_is_reported_not_just_the_first(self):
        r = run(decision(verdict="HOLD", bias="BUY", stop_loss=1.2, take_profit=1.0),
                acct=account(trade_allowed=False), spread=EURUSD_CAP * 10)
        assert r["passed"] is False
        assert len(r["reasons"]) == 6
        joined = " ".join(r["reasons"])
        assert "EXECUTE" in joined
        assert "permissions" in joined
        assert "Spread" in joined
        assert "Stop loss" in joined
        assert "Take profit" in joined
        assert "Risk:Reward" in joined


# ---------------------------------------------------------------------------
# The entry-authority override
# ---------------------------------------------------------------------------

class TestEntryAuthority:
    def test_non_execute_verdict_is_rejected_by_default(self):
        r = run(decision(verdict="HOLD"))
        assert r["passed"] is False
        assert "HOLD" in r["reasons"][0]
        assert "EXECUTE" in r["reasons"][0]

    @pytest.mark.parametrize("verdict", ["HOLD", "WAIT", "REJECT", "NO_TRADE", ""])
    def test_any_non_execute_verdict_is_rejected(self, verdict):
        assert run(decision(verdict=verdict))["passed"] is False

    def test_override_true_lets_a_non_execute_verdict_through(self):
        """The calibrated entry policy deliberately trades legacy-rejected setups.

        Reading `decision.decision` here would reimpose the legacy veto and block
        every calibrated trade, which is why the override exists.
        """
        assert run(decision(verdict="HOLD"), override=True)["passed"] is True

    def test_override_false_blocks_even_an_execute_verdict(self):
        r = run(decision(verdict="EXECUTE"), override=False)
        assert r["passed"] is False
        assert "authorized" in r["reasons"][0].lower()

    def test_override_none_is_the_legacy_path(self):
        assert run(decision(verdict="EXECUTE"), override=None)["passed"] is True
        assert run(decision(verdict="HOLD"), override=None)["passed"] is False


# ---------------------------------------------------------------------------
# Account permissions
# ---------------------------------------------------------------------------

class TestAccountPermissions:
    def test_trade_disabled_is_rejected(self):
        r = run(decision(), acct=account(trade_allowed=False))
        assert r["passed"] is False
        assert "permission" in r["reasons"][0].lower()

    def test_trade_allowed_passes(self):
        assert run(decision(), acct=account(trade_allowed=True))["passed"] is True

    def test_permission_failure_is_independent_of_geometry(self):
        r = run(decision(bias="BUY", stop_loss=1.2, take_profit=1.46), acct=account(trade_allowed=False))
        assert len(r["reasons"]) == 2


# ---------------------------------------------------------------------------
# Spread
# ---------------------------------------------------------------------------

class TestSpread:
    def test_within_the_registry_cap_passes(self):
        assert run(decision(symbol="EURUSD"), spread=EURUSD_CAP - 0.01)["passed"] is True

    def test_over_the_registry_cap_is_rejected(self):
        r = run(decision(symbol="EURUSD"), spread=EURUSD_CAP + 0.01)
        assert r["passed"] is False
        assert "spread" in r["reasons"][0].lower()

    def test_exactly_at_the_cap_passes(self):
        """The comparison is `>`, so the cap itself is allowed."""
        assert run(decision(symbol="EURUSD"), spread=EURUSD_CAP)["passed"] is True

    def test_the_cap_is_per_symbol_not_global(self):
        """XAUUSD tolerates 5 pips; that same spread would fail EURUSD."""
        assert run(decision(symbol="XAUUSD"), spread=4.0)["passed"] is True
        assert run(decision(symbol="EURUSD"), spread=4.0)["passed"] is False

    def test_the_cap_comes_from_the_registry_not_the_caller(self):
        """The `max_spread_pips` argument only fires if the spec lacks the attribute.

        SymbolSpec always has `max_spread_pips`, so the value `risk_engine` passes
        (default 35.0) never widens anything — EURUSD is still held to 2.0. Pinned
        deliberately: BTC's registry cap is 3000, so "widening" to a caller's 35
        would reject every crypto trade.
        """
        r = run(decision(symbol="EURUSD"), spread=10.0, cap=35.0)
        assert r["passed"] is False
        assert f"{EURUSD_CAP}" in r["reasons"][0]

    def test_the_reason_names_both_numbers(self):
        r = run(decision(symbol="EURUSD"), spread=9.0)
        assert "9.0" in r["reasons"][0]
        assert f"{EURUSD_CAP}" in r["reasons"][0]

    def test_an_unregistered_symbol_falls_back_to_the_generic_fx_cap(self):
        """`resolve` returns a generic FX spec (5.0 pips) for anything unregistered."""
        assert run(decision(symbol="XRPUSD"), spread=6.0)["passed"] is False
        assert run(decision(symbol="XRPUSD"), spread=4.0)["passed"] is True


class TestAsianSessionSpreadAllowance:
    """Crypto gets double the cap between 01:00 and 04:59 UTC."""

    @pytest.mark.parametrize("hour", [1, 2, 3, 4])
    def test_crypto_cap_doubles_in_the_asian_window(self, hour):
        r = run(decision(symbol="BTCUSD", bar_ts=bar(hour)), spread=BTCUSD_CAP * 1.5)
        assert r["passed"] is True

    @pytest.mark.parametrize("hour", [0, 5, 6, 12, 23])
    def test_crypto_cap_is_untouched_outside_it(self, hour):
        r = run(decision(symbol="BTCUSD", bar_ts=bar(hour)), spread=BTCUSD_CAP * 1.5)
        assert r["passed"] is False

    def test_the_window_is_half_open(self):
        assert run(decision(symbol="BTCUSD", bar_ts=bar(0, 59)), spread=BTCUSD_CAP * 1.5)["passed"] is False
        assert run(decision(symbol="BTCUSD", bar_ts=bar(5, 0)), spread=BTCUSD_CAP * 1.5)["passed"] is False

    def test_non_crypto_gets_no_allowance_in_the_asian_window(self):
        r = run(decision(symbol="EURUSD", bar_ts=bar(3)), spread=EURUSD_CAP * 1.5)
        assert r["passed"] is False

    def test_eth_and_sol_are_crypto_too(self):
        """Covered by the registry's `is_crypto`, not by the BTC/ETH substring check."""
        for sym in ("ETHUSD", "SOLUSD"):
            r = run(decision(symbol=sym, bar_ts=bar(3)),
                    spread=resolve(sym).max_spread_pips * 1.5)
            assert r["passed"] is True, sym


class TestAsianWindowHourSource:
    def test_the_hour_comes_from_the_bar_not_the_wall_clock(self):
        """A 03:00 bar must widen the cap regardless of when the check runs."""
        assert run(decision(symbol="BTCUSD", bar_ts=bar(3)), spread=BTCUSD_CAP * 1.5)["passed"] is True
        assert run(decision(symbol="BTCUSD", bar_ts=bar(14)), spread=BTCUSD_CAP * 1.5)["passed"] is False

    def test_a_tz_aware_bar_timestamp_is_converted_to_utc(self):
        """06:00 on the broker clock (GMT+2) is 04:00 UTC — inside the window.

        The window is documented in UTC and `market_context` passes bar
        timestamps through with their tzinfo intact, so reading `.hour` off them
        shifts the window by the broker offset the same way it did everywhere
        else MT5 time was assumed to be UTC.
        """
        r = run(decision(symbol="BTCUSD", bar_ts=bar(6, tz=BROKER_TZ)),
                spread=BTCUSD_CAP * 1.5)
        assert r["passed"] is True

    def test_conversion_also_narrows_the_window(self):
        """00:30 broker time is 22:30 UTC the previous day — outside the window."""
        r = run(decision(symbol="BTCUSD", bar_ts=bar(0, 30, tz=BROKER_TZ)),
                spread=BTCUSD_CAP * 1.5)
        assert r["passed"] is False

    def test_a_naive_bar_timestamp_is_read_as_utc(self):
        naive = datetime(2026, 1, 7, 3, 0)
        assert run(decision(symbol="BTCUSD", bar_ts=naive), spread=BTCUSD_CAP * 1.5)["passed"] is True

    def test_no_context_does_not_crash(self):
        r = run(decision(symbol="BTCUSD"))
        assert "passed" in r

    def test_context_without_a_timestamp_does_not_crash(self):
        dec = decision(symbol="BTCUSD")
        dec.context = SimpleNamespace(timestamp=None)
        assert "passed" in run(dec)

    def test_context_without_the_attribute_does_not_crash(self):
        dec = decision(symbol="BTCUSD")
        dec.context = SimpleNamespace()
        assert "passed" in run(dec)


# ---------------------------------------------------------------------------
# Order geometry
# ---------------------------------------------------------------------------

class TestStopLossGeometry:
    def test_buy_with_a_stop_below_entry_passes(self):
        assert run(decision(bias="BUY", entry=1.1, stop_loss=1.09, take_profit=1.12))["passed"] is True

    def test_buy_with_a_stop_above_entry_is_rejected(self):
        r = run(decision(bias="BUY", entry=1.1, stop_loss=1.11))
        assert r["passed"] is False
        assert "BUY" in r["reasons"][0] and "Stop loss" in r["reasons"][0]

    def test_buy_with_a_stop_exactly_at_entry_is_rejected(self):
        assert run(decision(bias="BUY", entry=1.1, stop_loss=1.1))["passed"] is False

    def test_sell_with_a_stop_above_entry_passes(self):
        assert run(decision(bias="SELL", entry=1.1, stop_loss=1.11, take_profit=1.08))["passed"] is True

    def test_sell_with_a_stop_below_entry_is_rejected(self):
        r = run(decision(bias="SELL", entry=1.1, stop_loss=1.09, take_profit=1.09))
        assert r["passed"] is False
        assert "SELL" in r["reasons"][0]
        assert "Stop loss" in r["reasons"][0]

    def test_sell_with_a_stop_exactly_at_entry_is_rejected(self):
        """Isolated: the take profit is valid, so only the stop can fail.

        With the default BUY-shaped take profit this test passed even when the
        stop check was mutated away, because the TP check failed instead.
        """
        r = run(decision(bias="SELL", entry=1.1, stop_loss=1.1, take_profit=1.09))
        assert r["passed"] is False
        assert "Stop loss" in r["reasons"][0]


class TestTakeProfitGeometry:
    def test_buy_with_a_target_above_entry_passes(self):
        assert run(decision(bias="BUY", entry=1.1, take_profit=1.12))["passed"] is True

    def test_buy_with_a_target_below_entry_is_rejected(self):
        r = run(decision(bias="BUY", entry=1.1, take_profit=1.09))
        assert r["passed"] is False
        assert "Take profit" in r["reasons"][0]

    def test_buy_with_a_target_exactly_at_entry_is_rejected(self):
        assert run(decision(bias="BUY", entry=1.1, take_profit=1.1))["passed"] is False

    def test_sell_with_a_target_below_entry_passes(self):
        assert run(decision(bias="SELL", entry=1.1, stop_loss=1.11, take_profit=1.08))["passed"] is True

    def test_sell_with_a_target_above_entry_is_rejected(self):
        r = run(decision(bias="SELL", entry=1.1, stop_loss=1.11, take_profit=1.12))
        assert r["passed"] is False
        assert "Take profit" in r["reasons"][0]

    def test_sell_with_a_target_exactly_at_entry_is_rejected(self):
        """Isolated: the stop is valid, so only the target can fail."""
        r = run(decision(bias="SELL", entry=1.1, stop_loss=1.11, take_profit=1.1))
        assert r["passed"] is False
        assert "Take profit" in r["reasons"][0]

    def test_both_sides_wrong_gives_two_reasons(self):
        r = run(decision(bias="BUY", entry=1.1, stop_loss=1.11, take_profit=1.08))
        assert len(r["reasons"]) == 2


class TestUnknownBias:
    @pytest.mark.parametrize("bias", ["NEUTRAL", "HOLD", "buy", "Sell", "LONG", "SHORT", ""])
    def test_a_bias_that_is_not_buy_or_sell_is_rejected(self, bias):
        """Geometry can only be checked against a direction.

        The old `if BUY ... elif SELL ...` had no else, so any other bias —
        including a lowercase one — skipped both checks and an inverted stop
        passed the guard.
        """
        r = run(decision(bias=bias, entry=1.1, stop_loss=1.2, take_profit=1.0))
        assert r["passed"] is False
        assert "bias" in r["reasons"][0].lower()

    def test_the_reason_names_the_offending_value(self):
        r = run(decision(bias="NEUTRAL"))
        assert "NEUTRAL" in r["reasons"][0]


# ---------------------------------------------------------------------------
# Non-finite numbers
# ---------------------------------------------------------------------------

class TestNonFiniteValues:
    """NaN compares False against everything, so it slips past every `>` check."""

    @pytest.mark.parametrize("field", ["entry", "stop_loss", "take_profit"])
    def test_nan_geometry_is_rejected(self, field):
        r = run(decision(bias="BUY", **{field: float("nan")}))
        assert r["passed"] is False
        assert "finite" in r["reasons"][0].lower() or "nan" in r["reasons"][0].lower()

    @pytest.mark.parametrize("value", [float("inf"), float("-inf")])
    @pytest.mark.parametrize("field", ["entry", "stop_loss", "take_profit"])
    def test_infinite_geometry_is_rejected(self, field, value):
        assert run(decision(bias="BUY", **{field: value}))["passed"] is False

    def test_nan_spread_is_rejected(self):
        r = run(decision(), spread=float("nan"))
        assert r["passed"] is False

    def test_infinite_spread_is_rejected(self):
        assert run(decision(), spread=float("inf"))["passed"] is False

    def test_a_nan_stop_does_not_pass_just_because_nan_comparisons_are_false(self):
        """`nan >= entry` is False, so the old BUY check silently passed it."""
        assert math.isnan(float("nan")) is True
        assert run(decision(bias="BUY", entry=1.1, stop_loss=float("nan")))["passed"] is False

    def test_negative_infinity_stop_on_a_sell_is_rejected(self):
        """`-inf <= entry` is True, so this one used to be caught — keep it that way."""
        assert run(decision(bias="SELL", entry=1.1, stop_loss=float("-inf")))["passed"] is False


class TestNonPositivePrices:
    """A price must be strictly positive, not merely finite.

    `_is_finite(0.0)` is True, so the finiteness loop admitted a zero price — and
    with the entry at ~0 the inverted-geometry comparisons below it are all False,
    so a NEGATIVE stop loss passed too. A zero price is not a rare edge: it is
    exactly what `market_context` produces for an empty primary frame
    (`current_price = bid = 0.0`, `ask = spread * pip`). Measured consequence — the
    levels engine minted entry 0.02 / stop -0.04 for BTCUSD, and the sizer read a
    risk distance of 0.06 against a 65 000 instrument and returned 100 lots: 6.5M
    USD of exposure against a 50 USD risk budget.
    """

    def test_a_zero_entry_is_rejected(self):
        r = run(decision(bias="BUY", entry=0.0, stop_loss=-0.001, take_profit=0.001))
        assert r["passed"] is False
        assert any("entry price" in x for x in r["reasons"])

    def test_a_negative_stop_is_rejected(self):
        """`stop_loss >= entry_price` is False when the stop is negative."""
        r = run(decision(bias="BUY", entry=0.02, stop_loss=-0.04, take_profit=0.17))
        assert r["passed"] is False
        assert any("stop loss" in x for x in r["reasons"])

    def test_a_negative_take_profit_is_rejected(self):
        r = run(decision(bias="SELL", entry=0.0, stop_loss=0.06, take_profit=-0.15))
        assert r["passed"] is False
        assert any("take profit" in x for x in r["reasons"])

    def test_a_zero_stop_is_rejected(self):
        assert run(decision(bias="BUY", entry=1.1, stop_loss=0.0))["passed"] is False

    def test_a_zero_take_profit_is_rejected(self):
        assert run(decision(bias="BUY", entry=1.1, take_profit=0.0))["passed"] is False

    def test_the_reason_says_not_positive(self):
        r = run(decision(bias="BUY", entry=0.0, stop_loss=1.0, take_profit=2.0))
        assert "positive" in r["reasons"][0]

    def test_the_exact_decision_an_unobserved_market_used_to_produce_is_rejected(self):
        """All three prices at zero — the shape an empty primary frame yields."""
        r = run(decision(symbol="BTCUSD", bias="BUY", entry=0.0, stop_loss=0.0, take_profit=0.0))
        assert r["passed"] is False
        assert len(r["reasons"]) >= 3

    def test_nan_is_still_reported_as_non_finite_not_as_non_positive(self):
        """The two branches must stay distinct: NaN is not merely "not positive"."""
        r = run(decision(bias="BUY", entry=1.1, stop_loss=float("nan")))
        assert "finite" in r["reasons"][0].lower()

    def test_a_genuinely_positive_decision_still_passes(self):
        """The positivity requirement must not narrow the guard for real prices."""
        assert run(decision(bias="BUY", entry=1.1, stop_loss=1.09, take_profit=1.12))["passed"] is True
        assert run(decision(bias="SELL", entry=1.1, stop_loss=1.11, take_profit=1.08))["passed"] is True


# ---------------------------------------------------------------------------
# Arguments the guard takes but does not act on
# ---------------------------------------------------------------------------

class TestUnusedArguments:
    def test_open_positions_do_not_change_the_verdict(self):
        """`positions` is accepted and never read — correlation is checked in
        `risk_engine`, not here. Pinned so the parameter is not mistaken for a
        working control."""
        from jarvis.data.schemas import PositionSnapshot
        pos = PositionSnapshot(
            ticket=1, symbol="EURUSD", type="BUY", volume=0.1, open_price=1.1,
            current_price=1.1, sl=1.09, tp=1.12, profit=0.0, swap=0.0,
            commission=0.0, open_time="2026-01-07T12:00:00", magic=0,
        )
        assert run(decision(), positions=[pos]) == run(decision(), positions=[])

    def test_an_empty_symbol_does_not_crash(self):
        r = run(decision(symbol=""))
        assert "passed" in r
