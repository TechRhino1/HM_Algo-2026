"""Is the AJ scan deterministic? Measure it, do not assume it.

The earlier sweep concluded "the lever is noise" from a harness that leaked its
registry override. The fixed harness now says ADVERSE at every tp_r. Before
believing the new number, test the instrument: scan the SAME symbol under the
SAME registry twice in one process and compare.

Three AUDUSD A-arm values have already been seen for what should be one
measurement -- 51 (probe), 53 (fixed sweep), 56 (universe run). If those are
real, the scanner is non-deterministic and the cause is known to be reachable:
`ParallelAnalystCluster` allows the MACRO analyst 2.0s while
`jarvis/market/news.py` allows the fetch it calls 5s and 6s, so a cache miss
(90s TTL) replaces MACRO with a fabricated score-50 NEUTRAL reading.

This probe counts those fallbacks so the two facts are tied together.

Import-safe: the import sweep imports every tool, so the scan itself only runs
under ``__main__``.
"""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

import spread_registry_ab as ab  # noqa: E402

DEFAULT_SYMBOL = "AUDUSD"
DEFAULT_REPS = 2


class Counter(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.fallback = 0
        self.devil = 0

    def emit(self, record):
        msg = record.getMessage()
        if "NEUTRAL score-50 fallback" in msg:
            self.fallback += 1
        elif "uncriticised" in msg:
            self.devil += 1


def _parse_args(argv):
    """Positional args, with a non-numeric ``reps`` falling back to the default.

    The import sweep imports this module with pytest's argv, so parsing must
    never raise on argv it did not author.
    """
    symbol = argv[0] if len(argv) > 0 and argv[0] else DEFAULT_SYMBOL
    try:
        reps = int(argv[1]) if len(argv) > 1 else DEFAULT_REPS
    except (TypeError, ValueError):
        reps = DEFAULT_REPS
    return symbol, max(1, reps)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    symbol, reps = _parse_args(argv)

    counter = Counter()
    logging.getLogger("JARVIS_AnalystCluster").addHandler(counter)
    logging.getLogger("JARVIS_AnalystCluster").setLevel(logging.WARNING)

    df = ab.load_bars(symbol, "H1", 183)
    if df is None:
        print(f"no bars for {symbol}")
        return 1

    print(f"symbol={symbol}  bars={len(df)}  reps={reps}  registry=PRISTINE")
    results = []
    for i in range(reps):
        ab.apply_registry(None)
        before = counter.fallback
        res = ab.scan(symbol, df)
        n = int(res.executed)
        results.append(n)
        candidates = len(res.candidates) if res.candidates is not None else 0
        print(f"  rep {i + 1}: EXEC={n:5d}  candidates={candidates:5d} "
              f" MACRO_fallbacks_this_rep={counter.fallback - before}")

    print()
    print(f"EXEC values: {results}")
    print(f"scan deterministic: {len(set(results)) == 1}")
    print(f"total MACRO fallbacks: {counter.fallback}   devil fallbacks: {counter.devil}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
