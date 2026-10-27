#!/usr/bin/env python
"""Repair the `expected_value` / `ai_score` damage in `executed_trades` (AI5).

READ-ONLY unless `--apply` is passed.

Two things were written into `executed_trades` that were never true:

* `expected_value` set to the realised P&L, at close and for every deal
  reconstructed from broker history. That is the OUTCOME, not the forecast.
  Measured: **112/112 closed rows** have `expected_value == realized_pnl`.
* `ai_score` set to a literal `85.0` for those reconstructed deals, which carry no
  decision and therefore have no score. Measured: **84 rows**.

The code is fixed, so no NEW row is corrupted. This repairs the existing ones.

The forecast itself is **unrecoverable** — it was overwritten in place, not stored
anywhere else. So the honest repair is not to guess it back but to record NULL:
"no forecast is known for this row". A NULL is something a calibration routine can
skip; a copied outcome is something it will silently learn from.

Two classes of row, handled separately:

* `ai_score = 85.0`  -> definitionally fabricated. That literal was only ever
  written by the reconstructed-deal INSERT. Nulled along with its `expected_value`.
* closed rows where `expected_value == realized_pnl` -> the forecast was destroyed
  by the close UPDATE. Nulled; there is nothing to recover.

Usage:
    python tools/repair_forecast_column.py            # report only
    python tools/repair_forecast_column.py --apply    # write
"""
from __future__ import annotations

import argparse
import sqlite3
import sys

from jarvis.config.paths import resolve_db_path


def _counts(conn: sqlite3.Connection) -> dict:
    q = lambda sql: conn.execute(sql).fetchone()[0]
    return {
        "rows": q("SELECT COUNT(*) FROM executed_trades"),
        "closed": q("SELECT COUNT(*) FROM executed_trades "
                    "WHERE closed_at IS NOT NULL AND closed_at <> ''"),
        "fabricated_score": q("SELECT COUNT(*) FROM executed_trades WHERE ai_score = 85.0"),
        "forecast_is_outcome": q(
            "SELECT COUNT(*) FROM executed_trades "
            "WHERE closed_at IS NOT NULL AND closed_at <> '' "
            "  AND expected_value IS NOT NULL "
            "  AND ABS(expected_value - realized_pnl) < 1e-9"),
        "forecast_null": q("SELECT COUNT(*) FROM executed_trades WHERE expected_value IS NULL"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="write the repair; without it this only reports")
    ap.add_argument("--db", default="jarvis_history.db")
    args = ap.parse_args()

    path = resolve_db_path(args.db)
    conn = sqlite3.connect(path, timeout=10.0)
    try:
        before = _counts(conn)
        print(f"file: {path}")
        print(f"  rows                      : {before['rows']}")
        print(f"  closed                    : {before['closed']}")
        print(f"  ai_score = 85.0 fabricated: {before['fabricated_score']}")
        print(f"  forecast == outcome       : {before['forecast_is_outcome']}")
        print(f"  forecast already NULL     : {before['forecast_null']}")

        if not args.apply:
            print("\nread-only: re-run with --apply to write. Take a copy first.")
            return 0

        with conn:
            cur = conn.execute(
                "UPDATE executed_trades SET ai_score = NULL, expected_value = NULL "
                "WHERE ai_score = 85.0")
            scored = cur.rowcount
            cur = conn.execute(
                "UPDATE executed_trades SET expected_value = NULL "
                "WHERE closed_at IS NOT NULL AND closed_at <> '' "
                "  AND expected_value IS NOT NULL AND ABS(expected_value - realized_pnl) < 1e-9")
            forecast = cur.rowcount

        after = _counts(conn)
        print("\napplied:")
        print(f"  fabricated score rows nulled : {scored}")
        print(f"  overwritten forecasts nulled : {forecast}")
        print(f"  forecast NULL now             : {after['forecast_null']}")
        print("\nThese forecasts are NOT recoverable — they were overwritten in place.")
        print("NULL is the honest value; a copied outcome is not.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
