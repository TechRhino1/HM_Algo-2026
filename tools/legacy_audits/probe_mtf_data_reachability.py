"""Read-only probe: does the live decision path ever hand a non-empty mtf_data to
DynamicRiskAndLevelsEngine.calculate_levels (which would bypass the baseline stop floor
at dynamic_levels.py:273/404 in favour of the institutional engine)?

Evidence gathered empirically, not by reading:
  1. Patch calculate_levels to record the `mtf_data` it receives.
  2. Drive DecisionEngine.evaluate() with a populated mtf_data, exactly as the
     orchestrator (orchestrator.py:713) and the server fallback (server.py:262) do.
  3. Print what calculate_levels actually saw.

Run: python .scratch/probe_mtf_data_path.py
"""
import sys
from datetime import datetime, timezone

import pandas as pd

sys.path.insert(0, ".")

from jarvis.data.symbol_registry import resolve
from jarvis.data.schemas import (
    MarketContext, StructureContext, LiquidityContext, VolatilityContext,
    MomentumContext, SessionContext, RegimeOutput, MarketRegime, DevilAdvocateReport,
)
from jarvis.intelligence.decision_engine import DecisionEngine
from jarvis.intelligence.dynamic_levels import DynamicRiskAndLevelsEngine

UTC = timezone.utc
SPREAD_PIPS = 2.0

CAPTURED = []


def _capture(self, *args, **kwargs):
    CAPTURED.append(kwargs.get("mtf_data", "<absent>"))
    # Do NOT call the original: we only need to observe the argument, and the real
    # engine would try to build a full order. Returning a minimal dict is enough for
    # evaluate() to keep going.
    return {
        "bias": kwargs.get("tentative_bias", "BUY"),
        "entry_price": 1.0850, "sl_price": 1.0800, "tp_price": 1.0950,
        "tp1_price": 1.0885, "tp2_price": 1.0950, "risk_dist": 0.0050,
        "tp_dist": 0.0100, "rr_ratio": 2.0, "first_target_price": 1.0885,
        "first_target_volume_pct": 0.5, "runner_trail_distance_atr": 1.0,
        "entry_type": "PROBE", "protocol_details": {},
    }


def observed_context(symbol="EURUSD", price=1.0850):
    spec = resolve(symbol)
    return MarketContext(
        symbol=symbol,
        timestamp=datetime.now(UTC),
        current_price=price,
        bid=price - (SPREAD_PIPS * spec.pip_size) / 2.0,
        ask=price + (SPREAD_PIPS * spec.pip_size) / 2.0,
        structure=StructureContext(
            bias="BULLISH", demand_zone=(price * 0.99, price * 0.995),
            supply_zone=(price * 1.005, price * 1.01),
        ),
        liquidity=LiquidityContext(buy_side_liquidity=price * 1.01, sell_side_liquidity=price * 0.99),
        volatility=VolatilityContext(atr=price * 0.005, current_spread_pips=SPREAD_PIPS, state="NORMAL"),
        momentum=MomentumContext(trend_score=40.0, adx=28.0),
        session=SessionContext(is_prime_session=True),
    )


def main():
    DynamicRiskAndLevelsEngine.calculate_levels = _capture

    from jarvis.intelligence.self_learning import SelfLearningEngine
    from jarvis.intelligence.realtime_optimizer import RealtimeOptimizer
    de = DecisionEngine(
        self_learning=SelfLearningEngine(db_path=":memory:"),
        realtime_optimizer=RealtimeOptimizer(db_path=":memory:"),
    )

    ctx = observed_context()
    regime = RegimeOutput(
        primary_regime=MarketRegime.TREND_BULL,
        probabilities={"trend_bull": 0.8, "trend_bear": 0.2},
        confidence=0.8,
    )
    devil = DevilAdvocateReport(
        symbol="EURUSD", counter_bias="BEARISH", penalty_score=5.0,
        invalidation_risk_coefficient=1.0,
    )

    # Populated MTF frames, exactly the shape orchestrator/server pass in.
    n = 60
    df = pd.DataFrame({
        "open": [1.0850] * n, "high": [1.0860] * n, "low": [1.0840] * n,
        "close": [1.0855] * n, "volume": [100] * n, "spread": [7] * n,
    })
    mtf = {"primary": df, "M1": df, "M5": df, "M15": df, "H1": df, "H4": df, "D1": df}

    try:
        de.evaluate(ctx, regime, {}, devil, account_balance=10000.0, mtf_data=mtf,
                    trade_style="SWING")
    except Exception as e:  # evaluate may fail later on synthetic inputs; the capture stands
        print(f"[note] evaluate raised after levels capture: {type(e).__name__}: {e}")

    print("=" * 70)
    print("calculate_levels() was called", len(CAPTURED), "time(s)")
    for i, seen in enumerate(CAPTURED):
        if seen == "<absent>":
            print(f"  call #{i}: mtf_data was NOT PASSED (kwarg absent) -> baseline floor governs")
        elif seen is None:
            print(f"  call #{i}: mtf_data=None -> baseline floor governs")
        elif isinstance(seen, dict):
            nonempty = any(isinstance(v, pd.DataFrame) and not v.empty for v in seen.values())
            print(f"  call #{i}: mtf_data=dict(keys={list(seen)[:4]}...) non_empty={nonempty}"
                  f" -> {'INSTITUTIONAL path would win' if nonempty else 'baseline floor governs'}")
        else:
            print(f"  call #{i}: mtf_data={seen!r}")


if __name__ == "__main__":
    main()
