"""How often does the news engine serve REAL data vs its synthetic calendar?

WHY THIS EXISTS. `LiveNewsEngine.get_news_calendar()` falls back to
`_generate_dynamic_calendar()` whenever `_fetch_all_live_sources()` returns
nothing. That synthetic calendar is a hardcoded plan of realistic-looking
events ("US S&P Global Composite Flash PMI", "Federal Reserve Jackson Hole
Monetary Assessment") and carries NO provenance flag -- `is_live` is a
*timing* flag (is the event's shock window active now), not a source flag.
So a synthetic calendar is indistinguishable from real data to every
consumer, including `MacroAnalyst`, whose `score` and `bias` feed the
`ai_score` gate in `decision_engine.py`.

This measures the real/synthetic split, and probes each feed separately so
a dead source can be told apart from a rate limit.

Run:  PYTHONPATH=. NO_PROXY='*' <python> tools/measure_news_provenance.py [n] [delay]
"""
import sys
import time

sys.path.insert(0, ".")

from jarvis.market.news import GLOBAL_NEWS_ENGINE  # noqa: E402

BUDGET = 2.0


def _names(events):
    """The event-name set, whatever key this stage of the pipeline used.

    The raw generator emits `title`; `_organize_news_feed` renames it to
    `event`. Comparing the wrong key silently yields {""} for every result,
    which classifies everything as 'real' -- so accept either.
    """
    out = set()
    for e in events:
        name = str(e.get("event") or e.get("title") or "").strip()
        if name:
            out.add(name)
    return out


def classify(events, synthetic_names):
    """'synthetic' if every event name comes from the hardcoded plan, else 'real'."""
    if not events:
        return "empty"
    names = _names(events)
    if not names:
        return "unknown"
    if names <= synthetic_names:
        return "synthetic"
    return "real"


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    delay = float(sys.argv[2]) if len(sys.argv) > 2 else 3.0

    synthetic_names = _names(
        GLOBAL_NEWS_ENGINE._organize_news_feed(
            GLOBAL_NEWS_ENGINE._generate_dynamic_calendar()
        )
    )
    print(f"synthetic calendar fingerprint: {len(synthetic_names)} distinct event names")
    print(f"probing n={n}, delay={delay}s, cluster budget={BUDGET}s")
    print("-" * 68)

    tally = {"real": 0, "synthetic": 0, "empty": 0, "unknown": 0}
    for i in range(n):
        t0 = time.perf_counter()
        events = GLOBAL_NEWS_ENGINE.get_news_calendar(force_refresh=True)
        dt = time.perf_counter() - t0
        kind = classify(events, synthetic_names)
        tally[kind] += 1
        flag = " <-- FABRICATED" if kind == "synthetic" else ""
        print(f"  run {i + 1}: {dt:6.3f}s  events={len(events):3d}  "
              f"{kind:9s}  {dt / BUDGET * 100:5.1f}% of budget{flag}")
        if i < n - 1:
            time.sleep(delay)

    print("-" * 68)
    print(f"  real={tally['real']}  synthetic={tally['synthetic']}  "
          f"empty={tally['empty']}  unknown={tally['unknown']}")
    print()
    print("per-source probe (each feed called directly):")
    for label, fn in (("FairEconomy", GLOBAL_NEWS_ENGINE._fetch_faireconomy_feed),
                      ("MyFxBook   ", GLOBAL_NEWS_ENGINE._fetch_myfxbook_feed)):
        t0 = time.perf_counter()
        try:
            items = fn()
            print(f"  {label}: {len(items):3d} items in {time.perf_counter() - t0:5.3f}s")
        except Exception as exc:
            print(f"  {label}: RAISED {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
