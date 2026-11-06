"""Remove phantom 'trades' left by pending orders that never filled.

WHY THIS EXISTS
---------------
`TradeMemory.record_trade` used to run when a limit/stop order was PLACED, not
when it filled. Every cancelled order therefore left a `trade_records` row with
`pnl = NULL` and `exit_price = NULL` forever. Measured 2026-11-06:
538 of 970 rows were NULL-PnL, while only 3 matched a live `executed_trades`
row still open. Those ghosts inflate "total trades" counts and make the journal
look like hundreds of unresolved positions.

The runtime fix defers the journal write until `POSITION_OPENED`. This tool
cleans the rows that already exist, and is deliberately conservative:

* rows with any recorded outcome are never deleted;
* rows matching an `executed_trades` row that is still open (`closed_at IS
  NULL`) are protected;
* rows with `realized_pnl` in `executed_trades` are backfilled instead;
* only rows older than ``--min-age-days`` (default 7) with no outcome are
  considered, which is far longer than any position this engine holds.

Dry-run by default. Pass ``--apply`` to write. Back up `data/` first.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sqlite3
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from jarvis.learning.trade_memory import TradeMemory  # noqa: E402


def _age_days(timestamp: str, now: dt.datetime) -> float:
    try:
        value = dt.datetime.fromisoformat(str(timestamp).replace(" ", "T"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return (now - value).total_seconds() / 86400.0
    except Exception:
        return float("inf")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--trade-db",
        default=os.path.join(REPO_ROOT, "data", "jarvis_trade_memory.db"),
    )
    parser.add_argument(
        "--history-db",
        default=os.path.join(REPO_ROOT, "data", "jarvis_history.db"),
    )
    parser.add_argument("--min-age-days", type=float, default=7.0)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Delete the candidates. Without this flag the tool only reports.",
    )
    args = parser.parse_args(argv)

    if not os.path.exists(args.trade_db):
        print(f"trade database not found: {args.trade_db}")
        return 2

    history = sqlite3.connect(args.history_db) if os.path.exists(args.history_db) else None
    if history is not None:
        history.row_factory = sqlite3.Row
    executed: dict[int, dict] = {}
    if history is not None:
        for row in history.execute(
            "SELECT ticket, position_id, realized_pnl, exit_price, closed_at "
            "FROM executed_trades"
        ):
            record = dict(row)
            for key in (row["ticket"], row["position_id"]):
                if key is not None:
                    executed.setdefault(int(key), record)
        history.close()

    memory = TradeMemory(db_path=args.trade_db)
    try:
        memory._conn.row_factory = sqlite3.Row  # noqa: SLF001 - maintenance tool
        rows = [
            dict(r)
            for r in memory._conn.execute(  # noqa: SLF001
                "SELECT ticket, symbol, timestamp, pnl, exit_price, entry_price, "
                "sl, tp, trade_type FROM trade_records WHERE pnl IS NULL"
            )
        ]

        now = dt.datetime.now(dt.timezone.utc)
        backfill: list[dict] = []
        candidates: list[dict] = []
        protected_open = 0

        for row in rows:
            match = executed.get(int(row["ticket"]))
            if match is not None:
                if match["realized_pnl"] is not None:
                    backfill.append({"row": row, "exec": match})
                    continue
                if match["closed_at"] is None:
                    protected_open += 1
                    continue
            if _age_days(row["timestamp"], now) >= args.min_age_days:
                candidates.append(row)

        print(f"NULL-PnL rows examined : {len(rows)}")
        print(f"backfillable outcomes  : {len(backfill)}")
        print(f"protected (still open) : {protected_open}")
        print(f"phantom candidates     : {len(candidates)}")

        for row in candidates[:10]:
            print(
                f"  DELETE candidate #{row['ticket']} {row['symbol']} "
                f"{row['timestamp']} entry={row['entry_price']} sl={row['sl']}"
            )
        if len(candidates) > 10:
            print(f"  ... and {len(candidates) - 10} more")

        if not args.apply:
            print("\nDRY RUN. Re-run with --apply to backfill outcomes and delete candidates.")
            return 0

        for item in backfill:
            memory.update_closed_trade(
                ticket=item["row"]["ticket"],
                exit_price=item["exec"]["exit_price"],
                pnl=item["exec"]["realized_pnl"],
                is_win=1 if float(item["exec"]["realized_pnl"]) > 0 else 0,
            )
        if backfill:
            print(f"backfilled {len(backfill)} outcome(s) from executed_trades")

        if candidates:
            with memory._lock:  # noqa: SLF001 - maintenance tool
                cur = memory._conn.cursor()  # noqa: SLF001
                cur.executemany(
                    "DELETE FROM trade_records "
                    "WHERE ticket = ? AND pnl IS NULL AND exit_price IS NULL",
                    [(row["ticket"],) for row in candidates],
                )
                memory._conn.commit()  # noqa: SLF001
                memory._checkpoint_locked()  # noqa: SLF001
            print(f"deleted {len(candidates)} phantom row(s)")

        remaining = memory._conn.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM trade_records WHERE pnl IS NULL"
        ).fetchone()[0]
        print(f"remaining NULL-PnL rows: {remaining}")
        return 0
    finally:
        memory.close()


if __name__ == "__main__":
    raise SystemExit(main())
