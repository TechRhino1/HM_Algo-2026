"""What does the MACRO analyst actually contribute to `ai_score`?

`ai_score` is the plain mean of the six analyst scores
(`decision_engine.py:737`) and `ai_score >= min_score` is the hard
"AI Multi-Score Gate" (`decision_engine.py:662`) with thresholds of
70/72/75/78/80/82/85. So the MACRO analyst's score moves every trade
decision.

This measures that contribution under three news inputs:

  live      -- what the running system actually sees (the global engine)
  empty     -- no news at all
  upcoming  -- the same calendar with `actual` filled in, i.e. no longer
               "Upcoming"

The point is to separate "the macro calendar is quiet" from "the macro
calendar is a hardcoded list that systematically subtracts points".

Run:  PYTHONPATH=. NO_PROXY='*' <python> tools/measure_macro_analyst_input.py
"""
import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")

from jarvis.analysts.macro_analyst import MacroAnalyst  # noqa: E402
from jarvis.data.schemas import (  # noqa: E402
    MarketContext, StructureContext, LiquidityContext, VolatilityContext,
    MomentumContext, SessionContext, RegimeOutput, MarketRegime,
)
from jarvis.market.news import GLOBAL_NEWS_ENGINE  # noqa: E402


def ctx(symbol="EURUSD", session=None) -> MarketContext:
    return MarketContext(
        symbol=symbol,
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        current_price=100.0, bid=99.9, ask=100.1,
        structure=StructureContext(bias="NEUTRAL"),
        liquidity=LiquidityContext(),
        volatility=VolatilityContext(),
        momentum=MomentumContext(),
        session=session or SessionContext(),
        mtf_alignment={},
    )


def regime(name=MarketRegime.TREND_BULL) -> RegimeOutput:
    return RegimeOutput(primary_regime=name,
                        probabilities={name.value: 1.0}, confidence=0.9)


def show(label, calendar, symbol="EURUSD"):
    rep = MacroAnalyst(news_calendar=calendar).analyze(ctx(symbol), regime())
    print(f"  {label:26s} score={rep.score:5.1f}  bias={rep.bias:8s}  "
          f"evidence={len(rep.evidence)}  risks={len(rep.risk_factors)}")
    return rep


def main() -> int:
    live = GLOBAL_NEWS_ENGINE.get_news_calendar()
    filled = [
        dict(e, actual=(e.get("actual") if str(e.get("actual", "")) not in
                        ("Upcoming", "", "—", "Pending") else str(e.get("forecast", "1.0"))))
        for e in live
    ]

    print(f"live calendar: {len(live)} events "
          f"({sum(1 for e in live if str(e.get('actual', '')) == 'Upcoming')} marked 'Upcoming')")
    print("-" * 72)
    for sym in ("EURUSD", "XAUUSD", "USDJPY"):
        print(f"[{sym}]")
        r_live = show("live engine (as running)", live, sym)
        show("no news at all", [], sym)
        show("same, actuals filled in", filled, sym)
        print()
        if r_live.risk_factors:
            for rf in r_live.risk_factors[:3]:
                print(f"      risk: {rf[:96]}")
        print()

    print("-" * 72)
    print("score with NO news minus score with the live calendar = the systematic")
    print("distortion the hardcoded calendar injects into ai_score.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
