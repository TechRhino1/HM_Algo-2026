"""Can a FABRICATED news event grant a conviction boost on the decision path?

`decision_engine.py:780` calls `GLOBAL_NEWS_ENGINE.evaluate_post_news_sweep_reaction`
and, when it reports a setup, adds `conviction_boost` (0.20) to BOTH
`calibrated_win_p` and `final_win_p` and +8.0 to `ai_score`.

That method reads `self.get_news_calendar()` -- the same calendar that falls
back to a hardcoded plan. Its filter is `is_past and diff_seconds >= -45min
and impact in (HIGH, MEDIUM) and the symbol is affected`. The hardcoded plan
contains three PAST events anchored to the most recent Friday, each with a
real-looking `actual`. So on a Friday, within 45 minutes of 13:45 / 17:00 /
18:00 UTC, a fabricated event satisfies the filter and grants the boost.

This demonstrates the mechanism directly by handing the method a synthetic
event stamped as recent.

Run:  PYTHONPATH=. NO_PROXY='*' <python> tools/measure_post_news_boost.py
"""
import sys

sys.path.insert(0, ".")

from jarvis.market.news import GLOBAL_NEWS_ENGINE  # noqa: E402


def synthetic_recent_past():
    """A hardcoded-plan event, marked past and released one minute ago."""
    return {
        "event": "US S&P Global Composite Flash PMI",
        "currency": "USD", "impact": "HIGH",
        "actual": "51.8", "forecast": "51.4", "previous": "51.1",
        "is_past": True, "is_upcoming": False, "is_live": False,
        "diff_seconds": -60.0,
        "affected_pairs": ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "BTCUSD"],
        "is_fallback": True, "source": "synthetic_calendar",
    }


def main() -> int:
    engine = GLOBAL_NEWS_ENGINE
    original = engine.get_news_calendar
    try:
        engine.get_news_calendar = lambda *a, **k: [synthetic_recent_past()]
        res = engine.evaluate_post_news_sweep_reaction(
            symbol="EURUSD", sweep_detected=True, sweep_type="SELL_SIDE",
            sweep_magnitude_pips=12.0,
        )
    finally:
        engine.get_news_calendar = original

    print("sweep detected, calendar holds ONE synthetic past event:")
    print(f"  news_reversal_setup : {res.get('news_reversal_setup')}")
    print(f"  conviction_boost    : {res.get('conviction_boost')}")
    print(f"  catalyst_event      : {res.get('catalyst_event')}")
    print(f"  reason              : {res.get('reason')}")
    print()
    if res.get("news_reversal_setup"):
        print("  => a FABRICATED event grants +8.0 ai_score and +0.20 on both")
        print("     win probabilities (decision_engine.py:781-788).")
    else:
        print("  => fabricated events are correctly refused.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
