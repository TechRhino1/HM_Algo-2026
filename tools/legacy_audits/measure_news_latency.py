"""Measure the real latency of a cold-cache news fetch.

The MACRO analyst runs inside ParallelAnalystCluster, which allows it 2.0s.
`LiveNewsEngine._fetch_all_live_sources` uses 5s and 6s socket timeouts and
tries both sources SEQUENTIALLY, so the worst case is 11s of blocking on a
2.0s budget. This measures what actually happens.

Run:  PYTHONPATH=. NO_PROXY='*' <python> tools/measure_news_latency.py [n]
"""
import sys
import time

sys.path.insert(0, ".")

from jarvis.market.news import GLOBAL_NEWS_ENGINE  # noqa: E402

BUDGET = 2.0


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    print(f"cold-cache fetch latency, n={n}, cluster budget={BUDGET}s")
    print("-" * 62)
    times = []
    for i in range(n):
        t0 = time.perf_counter()
        events = GLOBAL_NEWS_ENGINE.get_news_calendar(force_refresh=True)
        dt = time.perf_counter() - t0
        times.append(dt)
        verdict = "WOULD FABRICATE" if dt > BUDGET else "ok"
        # Distinguish real feed data from the deterministic synthetic calendar.
        real = bool(events) and any(
            str(e.get("source", "")).lower() not in ("", "synthetic", "dynamic")
            for e in events
        )
        print(f"  run {i + 1}: {dt:6.3f}s  events={len(events):3d}  "
              f"real={str(real):5s}  {verdict}  "
              f"({dt / BUDGET * 100:5.1f}% of budget)")
    over = [t for t in times if t > BUDGET]
    print("-" * 62)
    print(f"  min={min(times):.3f}s  max={max(times):.3f}s  "
          f"mean={sum(times) / len(times):.3f}s")
    print(f"  exceeded the 2.0s budget: {len(over)}/{len(times)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
