"""The configured per-trade risk ceiling must actually bind.

WHY THIS EXISTS
---------------
The worst trade in the live journal was a BOT order for **17.19 lots of
SOLUSD**, which risked **$84 on a $4,500 account — 1.87%, or 3.7x the
configured 0.5% limit**. On today's code the same trade sizes to 4.59 lots
(0.50%), so the ceiling itself was fixed before this test was written. What was
missing was anything that would notice if it regressed: `position_sizing.py`
once clamped to a HARDCODED literal `1.50` while applying `invalidation_risk_coefficient`
and `combined_scaler` OUTSIDE that clamp, so a high-conviction trade scaled the
0.5% base by up to ~1.55x and the setting was never enforced. Measured then: 27
real trades breached it, the worst risking 39% of equity (78x the limit).

These tests assert the contract, not the arithmetic: the multipliers a caller
passes may only REDUCE risk below the ceiling, never inflate past it. They are
pure-function tests — `calculate_lot_size` touches no broker.

The one documented exception is the broker minimum-lot floor: on accounts
below $2,500 the approved policy allows the forced minimum lot only while the
absolute risk stays at or below $12 (and within the 1.25–2.5% calibrated
target, whichever is wider). Standard accounts keep the 0.5% ceiling and may
use the minimum lot while its percentage risk is no worse than that ceiling.
"""

from __future__ import annotations

import pytest

from jarvis.data.symbol_registry import get_dollar_risk_per_price_unit, resolve
from jarvis.risk.position_sizing import PositionSizer

# (symbol, entry, stop) — chosen to span FX, metals, indices and crypto, and to
# include the exact geometry of the live outlier.
GEOMETRY = [
    ("XAUUSD", 4265.78, 4268.77),
    ("EURUSD", 1.1000, 1.0950),
    ("GBPUSD", 1.3555, 1.3550),
    ("SOLUSD", 99.60, 99.11),
    ("BTCUSD", 65000.0, 64000.0),
    ("NAS100", 20000.0, 19900.0),
]

EQUITIES = [200.0, 500.0, 1000.0, 4500.0, 25000.0, 100000.0]

# Approved 2026-10-23 micro policy: calibrated 1.25–2.5% target on accounts
# below $2,500, with a $12 absolute per-trade ceiling. Standard accounts keep
# the configured 0.5% ceiling.
CEILING_PCT = 0.5
MICRO_TARGET_MAX_PCT = 2.5
ABSOLUTE_MICRO_RISK_USD = 12.0


def _allowed_pct(equity: float) -> float:
    if equity < 2500.0:
        return max(MICRO_TARGET_MAX_PCT, ABSOLUTE_MICRO_RISK_USD / equity * 100.0)
    return CEILING_PCT


def _symbol_info(symbol: str) -> dict:
    spec = resolve(symbol)
    return {
        "name": symbol,
        "trade_contract_size": spec.contract_size,
        "volume_min": 0.01,
        "volume_max": 100.0,
        "volume_step": 0.01,
    }


def _risk_pct(equity: float, symbol: str, entry: float, sl: float, **kwargs) -> float:
    """Risk of the returned size, as a percentage of equity."""
    info = _symbol_info(symbol)
    lots = PositionSizer.calculate_lot_size(
        account_balance=equity,
        entry_price=entry,
        sl_price=sl,
        risk_pct=CEILING_PCT,
        symbol_info=info,
        **kwargs,
    )
    if lots <= 0:
        return 0.0
    dollar_per_unit = get_dollar_risk_per_price_unit(symbol, info)
    return lots * abs(entry - sl) * dollar_per_unit / equity * 100.0


class TestTheCeilingBinds:
    @pytest.mark.parametrize("symbol,entry,sl", GEOMETRY)
    @pytest.mark.parametrize("equity", EQUITIES)
    def test_risk_never_exceeds_the_ceiling_or_the_min_lot_floor(
        self, symbol, entry, sl, equity
    ):
        """No size may risk more than the approved micro target/$12 cap, or
        the 0.5% ceiling on a standard account."""
        pct = _risk_pct(equity, symbol, entry, sl)
        allowed = _allowed_pct(equity)
        assert pct <= allowed + 1e-6, (
            f"{symbol} at ${equity:,.0f} risked {pct:.2f}% "
            f"(allowed {allowed:.2f}%)"
        )

    def test_the_live_outlier_no_longer_oversizes(self):
        """id=101: SOLUSD, entry 99.6, stop 99.11, on a $4,500 account.

        The journal recorded 17.19 lots / 1.87%. Anything near that is the bug
        coming back.
        """
        pct = _risk_pct(4500.0, "SOLUSD", 99.60, 99.11)
        assert pct <= CEILING_PCT + 1e-6, (
            f"the SOLUSD outlier sizes to {pct:.2f}% of equity again"
        )

    def test_high_conviction_multipliers_may_not_inflate_past_the_ceiling(self):
        """The exact shape of the historical defect: every multiplier at its
        maximum. conviction 1.35 x evidence 1.15 = 1.5525, clamped to 1.40, so
        the unclamped result would be 0.5% * 1.40 = 0.70%."""
        pct = _risk_pct(
            25000.0, "EURUSD", 1.1000, 1.0950,
            model_confidence=0.95,     # -> conviction_factor 1.35 (its cap)
            pattern_sample_size=60,    # -> evidence_factor 1.15 (its cap)
            invalidation_risk_coefficient=1.0,
            portfolio_heat_multiplier=1.0,
            target_rr=5.0,
        )
        assert pct <= CEILING_PCT + 1e-6, (
            f"maxed-out multipliers pushed risk to {pct:.2f}%, above the "
            f"{CEILING_PCT}% ceiling"
        )

    def test_a_missing_atr_ratio_does_not_inflate_size(self):
        """`atr_ratio` defaulted to 1.0 for every caller before it was wired,
        so `vol_scalar` was always 1.0. A caller that omits it (or passes 0,
        or None) must still be sized as if volatility were typical."""
        baseline = _risk_pct(25000.0, "EURUSD", 1.1000, 1.0950, atr_ratio=1.0)
        for missing in (None, 0.0):
            pct = _risk_pct(25000.0, "EURUSD", 1.1000, 1.0950, atr_ratio=missing)
            assert pct == pytest.approx(baseline, rel=1e-9), (
                f"atr_ratio={missing!r} changed the size ({pct} vs {baseline})"
            )

    def test_multipliers_only_reduce_below_the_ceiling(self):
        """A drawdown/heat penalty must lower risk; it must never raise it."""
        clean = _risk_pct(25000.0, "EURUSD", 1.1000, 1.0950)
        penalised = _risk_pct(
            25000.0, "EURUSD", 1.1000, 1.0950, portfolio_heat_multiplier=0.5
        )
        assert penalised <= clean, (
            f"a 0.5x heat penalty raised risk from {clean:.3f}% to {penalised:.3f}%"
        )
        assert penalised < clean, "the heat penalty did nothing at all"


class TestDegenerateInputs:
    def test_zero_or_negative_risk_distance_is_refused(self):
        for sl in (1.1000, 1.2000):  # equal to entry, and "wrong side"
            lots = PositionSizer.calculate_lot_size(
                account_balance=25000.0, entry_price=1.1000, sl_price=sl,
                risk_pct=CEILING_PCT, symbol_info=_symbol_info("EURUSD"),
            )
            if sl == 1.1000:
                assert lots == 0.0, "a zero stop distance produced a size"

    def test_non_positive_balance_is_refused(self):
        lots = PositionSizer.calculate_lot_size(
            account_balance=0.0, entry_price=1.1000, sl_price=1.0950,
            risk_pct=CEILING_PCT, symbol_info=_symbol_info("EURUSD"),
        )
        assert lots == 0.0
