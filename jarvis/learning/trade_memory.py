"""
HM Algo 2.0 — Persistent Trade Memory & Journaling Engine.
Logs rich execution records, market snapshots, MFE/MAE excursions, and decision context to SQLite.
"""
import sqlite3
import json
import math
import logging
import threading
from typing import Dict, List, Any, Optional
from datetime import datetime, timezone

from jarvis.config.paths import resolve_db_path
from jarvis.data.schema_version import add_columns, migrate

logger = logging.getLogger("JARVIS_TradeMemory")

# D3: 1 = `ml_features` + `triple_barrier_label`. A file at 0 predates
# versioning and is brought up by migration 1 below.
# AI10: 2 = `raw_win_prob` (the pre-calibration forecast).
SCHEMA_VERSION = 2


def _migration_1(conn: sqlite3.Connection) -> None:
    """Add the two columns the old "add whatever is missing" sweep added on EVERY open.

    That sweep could express neither ordering, a backfill, nor a refusal, and it ran
    against files from NEWER code too — so a future rename (say `mfe` -> `mfe_r`) would be
    silently undone on the next open by re-adding the old column, while `ensure_version`
    logged an error the sweep had already made meaningless. As a numbered step it runs at
    most once, and the version is written immediately after it.
    """
    add_columns(conn, "trade_records", {
        "ml_features": "TEXT",
        "triple_barrier_label": "INTEGER DEFAULT 0",
    })


def _migration_2(conn: sqlite3.Connection) -> None:
    """AI10: persist the PRE-calibration win probability.

    Without it the only probability on a closed row is `model_confidence`, which is
    the decision pipeline's own downstream output — so refitting the reliability
    curve from the journal meant fitting the curve to itself. NULL means "not
    recorded", and a fit must skip those rows rather than read them as 0.0.
    """
    add_columns(conn, "trade_records", {"raw_win_prob": "REAL"})


MIGRATIONS = {1: _migration_1, 2: _migration_2}


def derive_triple_barrier_label(entry, exit_price, sl, tp, trade_type) -> Optional[int]:
    """Label a closed trade by WHICH BARRIER its exit reached.

    The triple-barrier method (Lopez de Prado) labels a trade **+1** when the upper
    (take-profit) barrier is touched first, **-1** when the lower (stop-loss) barrier is
    touched first, and **0** when the vertical (time) barrier is reached instead. Only the
    *first* touch decides, so strictly the label needs the trade's path -- but this journal
    records only the exit, so the label is derived from the strongest evidence a row
    actually holds: the exit price against the two barriers stored beside it.

    Returns ``None`` when the row cannot answer the question (no exit, no usable entry, or
    no barriers), so **"unlabelled" stays distinguishable from "the vertical barrier was
    hit"**. That distinction is the whole point here. The previous code wrote ``0`` at
    *open* time, from a ``pnl`` that does not exist yet, and ``update_closed_trade`` never
    revisited the column -- so every row was labelled "vertical barrier" whether or not it
    was. Measured on the live journal: 36/36 rows at 0 (one distinct value), of which 8
    have an exit sitting on or through a stored barrier.
    """
    try:
        entry = float(entry)
        exit_price = float(exit_price)
        sl = float(sl)
        tp = float(tp)
    except (TypeError, ValueError):
        return None

    if not all(math.isfinite(v) for v in (entry, exit_price, sl, tp)):
        return None
    # A barrier or price of 0 means "not recorded", not "a real level".
    if min(entry, exit_price, sl, tp) <= 0.0:
        return None

    side = str(trade_type or "").strip().upper()
    if side not in ("BUY", "LONG", "SELL", "SHORT"):
        return None

    # A float artifact can put the exit a hair INSIDE the barrier. Live row 938435830
    # stored `sl = 111.29999999999998` and filled at `111.30`, so a plain `exit <= sl`
    # answers "not reached" for a trade that was stopped out -- and the label would then
    # be decided by float noise rather than by the market. The tolerance is relative and
    # ~1e-9, far below one tick (a BTCUSD tick is ~1.2e-7 relative, a EURUSD pip ~8.7e-6),
    # so it cannot swallow a genuine near-miss.
    def _at(price, barrier):
        return abs(price - barrier) <= 1e-9 * max(abs(barrier), 1.0)

    if side in ("BUY", "LONG"):
        hit_tp = exit_price >= tp or _at(exit_price, tp)
        hit_sl = exit_price <= sl or _at(exit_price, sl)
    else:
        hit_tp = exit_price <= tp or _at(exit_price, tp)
        hit_sl = exit_price >= sl or _at(exit_price, sl)

    if hit_tp and hit_sl:
        # For a well-formed trade this is unreachable: a BUY cannot exit at or above its
        # target AND at or below its stop unless `tp <= sl`, i.e. the two barriers
        # contradict each other. (A gap does NOT produce it -- the fill lands on or beyond
        # whichever barrier was touched, which classifies cleanly.) A contradictory row
        # cannot be labelled, so refuse rather than guess a direction.
        return None
    if hit_tp:
        return 1
    if hit_sl:
        return -1
    return 0


class TradeMemory:
    def __init__(self, db_path: str = "jarvis_trade_memory.db"):
        # Anchored on the repo data dir — see jarvis.config.paths.
        db_path = resolve_db_path(db_path)
        self.db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=10.0)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.execute("PRAGMA cache_size=-32000;")
        self._init_db()

    def _checkpoint_locked(self) -> None:
        """Do the checkpoint. Caller must already hold `_lock`."""
        if not self._conn:
            return
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        except Exception as e:
            logger.debug(f"trade_memory checkpoint failed: {e}")

    def checkpoint(self) -> None:
        """Fold the WAL back into the main database file.

        With `journal_mode=WAL` and `synchronous=NORMAL`, committed rows live in
        `jarvis_trade_memory.db-wal`, not in the `.db`. Measured here: the `.db`
        alone held 16 of 35 trades while `.db` + `-wal` held all 35, so **any
        backup, copy or zip of the `.db` file by itself silently loses more than
        half the journal**. TRUNCATE also resets the WAL so it cannot grow
        without bound.
        """
        with self._lock:
            self._checkpoint_locked()

    def close(self):
        with self._lock:
            if self._conn:
                # Checkpoint BEFORE closing: otherwise everything committed in
                # this session stays in the -wal and a copy of the .db alone
                # loses it.
                self._checkpoint_locked()
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    def __del__(self):
        self.close()

    def _init_db(self):
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS trade_records (
                    ticket INTEGER PRIMARY KEY,
                    symbol TEXT,
                    timestamp TEXT,
                    trade_type TEXT,
                    entry_price REAL,
                    exit_price REAL,
                    sl REAL,
                    tp REAL,
                    lots REAL,
                    pnl REAL,
                    is_win INTEGER,
                    regime TEXT,
                    strategy TEXT,
                    model_confidence REAL,
                    raw_win_prob REAL,
                    adversarial_penalty REAL,
                    expected_value REAL,
                    mfe REAL,
                    mae REAL,
                    reasoning TEXT,
                    quality_gate TEXT,
                    ml_features TEXT
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_trade_records_ts ON trade_records(timestamp);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_trade_records_sym ON trade_records(symbol);")
            self._conn.commit()
            # D3: bring the file up to SCHEMA_VERSION one numbered step at a time.
            # A brand-new file already has every column (the CREATE above) and
            # `add_columns` skips what is present, so migration 1 is a no-op for it and
            # does the real work for a legacy file. Unlike the sweep this replaced, it
            # also REFUSES a file written by newer code instead of re-adding columns.
            migrate(self._conn, "trade_records", SCHEMA_VERSION, MIGRATIONS)
            self._conn.commit()


    def record_trade(self, trade_data: Dict[str, Any]):
        with self._lock:
            cur = self._conn.cursor()
            # The OUTCOME does not exist yet at open. `exit_price`, `pnl` and
            # `is_win` are therefore written NULL — never 0.0/0. A 0.0 P&L is a
            # real break-even trade and `dict.get()` returns 0.0 for a missing
            # key, so 0.0 cannot stand for both "closed flat" and "never closed".
            # Measured consequence of the old `.get(..., 0.0)` defaults: 42/93
            # rows in `data/jarvis_trade_memory.db` read `exit_price=0, pnl=0`,
            # indistinguishable from a scratch loser.
            #
            # `is_win` is derived from `pnl` ONLY when a caller actually supplies
            # one — at open nobody can. `0` already means "loss", so an unknown
            # outcome must not borrow it; an explicit `is_win` from the caller is
            # respected (it is the caller's measurement, not ours to override).
            pnl = trade_data.get("pnl")
            if trade_data.get("is_win") is not None:
                is_win = int(trade_data["is_win"])
            elif pnl is not None:
                is_win = 1 if pnl > 0 else 0
            else:
                is_win = None
            # Named columns, NOT `VALUES` with 22 bare placeholders. A positional insert
            # is pinned to this version's exact column count, so it fails outright against
            # a file that has one more column than the code knows about — which is exactly
            # what a file written by NEWER code looks like ("table trade_records has 23
            # columns but 22 values were supplied"). Naming them keeps the write working
            # when the file carries extra columns, and is immune to column reordering.
            cur.execute("""
                INSERT OR REPLACE INTO trade_records (
                    ticket, symbol, timestamp, trade_type, entry_price, exit_price,
                    sl, tp, lots, pnl, is_win, regime, strategy, model_confidence,
                    raw_win_prob, adversarial_penalty, expected_value, mfe, mae, reasoning,
                    quality_gate, ml_features, triple_barrier_label
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
            """, (
                trade_data.get("ticket", int(datetime.now().timestamp())),
                trade_data.get("symbol", "UNKNOWN"),
                trade_data.get("timestamp", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")),
                trade_data.get("type", "BUY"),
                trade_data.get("entry", 0.0),
                # NULL when not supplied — a missing exit is not a price of 0.
                trade_data.get("exit"),
                trade_data.get("sl", 0.0),
                trade_data.get("tp", 0.0),
                trade_data.get("lots", 0.01),
                pnl,
                is_win,
                trade_data.get("regime", "NEUTRAL"),
                trade_data.get("strategy", "TREND_FOLLOWING"),
                trade_data.get("model_confidence", 0.5),
                # AI10: the pre-calibration forecast, or NULL when none was recorded.
                # Not `.get(..., 0.5)` — a default here would manufacture a forecast
                # that the refit would then learn from.
                trade_data.get("raw_win_prob"),
                trade_data.get("adversarial_penalty", 0.0),
                trade_data.get("expected_value", 0.0),
                # D19: no fabricated 0.0. An excursion of 0.0 at open says nothing
                # about the path the trade is about to take, and it is what the
                # column reads as if the close is never recorded. NULL means "not
                # measured"; only a caller that actually measured passes a value.
                trade_data.get("mfe"),
                trade_data.get("mae"),
                json.dumps(trade_data.get("reasoning", {})),
                json.dumps(trade_data.get("quality_gate", {})),
                json.dumps(trade_data.get("ml_features", [])),
                # D10: do NOT invent a label here. At open there is no `pnl` to derive one
                # from, so the old fallback (`1 if pnl > 0 else (-1 if pnl < 0 else 0)`)
                # always evaluated to 0 -- minting "the vertical barrier was hit" for every
                # trade before it had been closed. `update_closed_trade` derives the real
                # label from the stored geometry; until then the row is honestly unlabelled.
                trade_data.get("triple_barrier_label")
            ))

            self._conn.commit()
            # Fold the WAL in now so the `.db` file alone always holds every
            # committed trade -- see `checkpoint()`.
            self._checkpoint_locked()

    def fetch_all_trades(self) -> List[Dict[str, Any]]:
        with self._lock:
            self._conn.row_factory = sqlite3.Row
            cur = self._conn.cursor()
            cur.execute("SELECT * FROM trade_records ORDER BY timestamp DESC")
            rows = cur.fetchall()
            return [dict(r) for r in rows]

    def fetch_recent_trades(self, n: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            self._conn.row_factory = sqlite3.Row
            cur = self._conn.cursor()
            cur.execute("SELECT * FROM trade_records ORDER BY timestamp DESC LIMIT ?", (n,))
            rows = cur.fetchall()
            return [dict(r) for r in rows]

    def fetch_trade(self, ticket) -> Optional[Dict[str, Any]]:
        """The one row for `ticket`, or None.

        AI6: the orchestrator keeps the features and geometry it needs at close in a
        process-local dict, so a restart loses them and the learning loop silently
        does nothing for every trade that spans one. Everything needed — type,
        entry, sl, tp, symbol, strategy, regime, and the feature vector — is already
        persisted here at open, so this is what lets a close be reconstructed after
        a restart instead of falling back to an invented R-multiple.
        """
        try:
            key = int(ticket)
        except (TypeError, ValueError):
            return None
        with self._lock:
            self._conn.row_factory = sqlite3.Row
            cur = self._conn.cursor()
            cur.execute("SELECT * FROM trade_records WHERE ticket = ?", (key,))
            row = cur.fetchone()
            return dict(row) if row else None

    def update_closed_trade(
        self,
        ticket: int,
        exit_price: Optional[float] = None,
        pnl: Optional[float] = None,
        is_win: Optional[int] = None,
        mfe: Optional[float] = None,
        mae: Optional[float] = None
    ) -> bool:
        """Write a trade's OUTCOME onto the row written when it OPENED (§17).

        This is the other half of :meth:`record_trade`, the counterpart of
        ``SQLiteTradeDB.record_trade_exit``. It must **UPDATE** the row keyed on
        ``ticket``, never re-insert it: ``record_trade`` is ``INSERT OR REPLACE``,
        so routing a close through it would wipe the entry geometry, the feature
        vector and the decision context that the open wrote.

        **`None` means "not recorded" and is stored as NULL — never as 0.0.**
        A ``0.0`` P&L is a real break-even trade; a missing one is not, and
        ``dict.get()`` returns 0.0 for a missing key, so allowing the two to share
        a value is exactly the ambiguity that made every open row look like a
        scratch. ``COALESCE`` means a later call that does not know the outcome
        cannot erase one that is already stored.

        ``is_win`` is optional. When omitted it is derived from ``pnl``
        (``pnl > 0``), and when ``pnl`` is unknown it stays NULL — ``0`` already
        means "loss" and must not be borrowed for "unknown".

        `mfe` / `mae` are maximum favourable / adverse excursion in PRICE UNITS.
        `None` means NOT MEASURED and is written as NULL — it must never be
        coerced to 0.0, which claims the excursion was measured and was zero.
        A caller that genuinely measured no movement passes 0.0 explicitly.

        Also derives `triple_barrier_label` from the row's OWN stored geometry: the close
        is the first moment the question can be answered at all, and this is the only place
        every caller converges. `COALESCE` keeps an explicit label supplied at open when the
        geometry cannot decide, so a row is never downgraded from labelled to NULL.

        Returns True if a row was found and updated.
        """
        with self._lock:
            cur = self._conn.cursor()
            row = cur.execute(
                "SELECT entry_price, sl, tp, trade_type FROM trade_records WHERE ticket = ?",
                (int(ticket),)
            ).fetchone()
            if row is None:
                # A close for a row that was never opened. Updating nothing is
                # correct; INSERTing one would fabricate an entry that never
                # happened. Mirrors `record_trade_exit` returning False.
                return False
            label = derive_triple_barrier_label(
                entry=row[0], exit_price=exit_price,
                sl=row[1], tp=row[2], trade_type=row[3],
            )
            # Derive the win flag only when the caller did not state it AND the
            # P&L is actually known. `pnl` None -> `is_win` None, not 0.
            if is_win is None and pnl is not None:
                is_win = 1 if float(pnl) > 0 else 0
            cur.execute("""
                UPDATE trade_records
                SET exit_price = COALESCE(?, exit_price),
                    pnl = COALESCE(?, pnl),
                    is_win = COALESCE(?, is_win),
                    mfe = COALESCE(?, mfe),
                    mae = COALESCE(?, mae),
                    triple_barrier_label = COALESCE(?, triple_barrier_label)
                WHERE ticket = ?
            """, (
                None if exit_price is None else float(exit_price),
                None if pnl is None else float(pnl),
                None if is_win is None else int(is_win),
                None if mfe is None else float(mfe),
                None if mae is None else float(mae),
                label,
                int(ticket)
            ))
            self._conn.commit()
            # A closed trade is the most valuable row in the table; make sure it
            # is in the `.db` itself, not only the WAL.
            self._checkpoint_locked()
            return True
