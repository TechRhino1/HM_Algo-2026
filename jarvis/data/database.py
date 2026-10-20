import sqlite3
import logging
import time
from datetime import datetime, timezone
import threading
import re
from typing import Optional

from jarvis.config.paths import resolve_db_path, ensure_data_dir
from jarvis.data.broker_time import broker_utc_offset
from jarvis.data.schema_version import add_columns, migrate

# Where a journalled price came from. The whole point of `origin` (D1): a
# realised-P&L statistic computed over the journal is meaningless if simulated
# fills and fallback quotes are indistinguishable from real ones — measured on
# data/jarvis_history.db, 158 of 269 rows are engine-logged and 0 of them have
# ever closed, so mixing them in silently understates every result.
# "unknown" is in the tuple because it is a value we STORE — the caller's
# choices are the other three. Anything not in the tuple lands as "unknown"
# rather than being guessed at.
ORIGINS = ("broker", "paper", "synthetic", "unknown")

#: The execution mode the row was produced under. Kept SEPARATE from
#: :data:`ORIGINS` on purpose: `origin` answers "where did the fill price come
#: from", `execution_mode` answers "was this real money". They are not the same
#: question and one cannot be derived from the other --
#: `_price_origin()` returns `synthetic` both for a paper fill with no quote and
#: for a LIVE fill whose client could not reach the terminal, so "synthetic"
#: rows are a mix of real money and simulated, with nothing in the row to tell
#: them apart. That is the gap D1 was filed for, and `origin` alone cannot close
#: it.
#:
#: "unknown" is stored, never guessed: rows written before this column existed
#: carry no evidence of the mode they were produced under.
EXECUTION_MODES = ("live", "paper", "demo", "unknown")

#: How long a read path will reuse the last broker sync before paying for
#: another one. A `/api/history` read used to re-read 30 days of deals every
#: time; on the live server that parked request threads inside the terminal
#: attach and timed the endpoint out.
_MT5_SYNC_MIN_INTERVAL_SEC = 30.0


def _mode_filter(execution_mode):
    """Normalise a mode filter to the values that can exist — `[]` if none can.

    Same contract as :func:`_origin_filter`: a caller asking for
    `execution_mode="real"` has made a typo, and answering with the whole table
    — paper fills included — looks exactly like a filter that worked.
    """
    if isinstance(execution_mode, str):
        wanted = [execution_mode]
    else:
        try:
            wanted = list(execution_mode)
        except TypeError:
            return []
    return [m for m in wanted if m in EXECUTION_MODES]


def _origin_filter(origin):
    """Normalise an origin filter to the values that can exist — `[]` if none can.

    A caller passing something that is not an origin has almost certainly made a
    typo (`origin="real"`, `origin="live"`), and answering that with the whole
    table is the worst possible response: it looks exactly like a working filter
    that happened to match everything.
    """
    if isinstance(origin, str):
        wanted = [origin]
    else:
        try:
            wanted = list(origin)
        except TypeError:
            return []
    return [o for o in wanted if o in ORIGINS]


def _pnl_stats(closed, total, winners, losers, open_rows):
    """One realised-P&L bucket.

    `expectancy` is None, not 0.0, when nothing has closed. 0.0 reads as
    "measured and break-even"; None reads as "not measurable", which is the
    truth for an empty bucket — and for the 158 engine-logged rows that have
    never closed.
    """
    return {
        "closed": int(closed),
        "open": int(open_rows),
        "realised_pnl": round(float(total), 2),
        "expectancy": round(float(total) / closed, 4) if closed else None,
        "winners": int(winners),
        "losers": int(losers),
    }


def _empty_pnl():
    return _pnl_stats(0, 0.0, 0, 0, 0)

# Bumped when the shape of `executed_trades` changes. Every file written before
# this existed is 0, which means "none of these have run".
#   1 — every column the old sweep used to add, plus `position_id` (D2)
#   2 — `origin`, so a row says where its price came from (D1)
#   3 — an index on `origin`, so it can be QUERIED (see _migration_3)
#   4 — `execution_mode`, so a row says whether it was real money (see
#       _migration_4). D1's original fix gave every row an `origin`, but `origin`
#       is price provenance, and its own docstring says so: "A microsecond
#       timestamp only tells us the ENGINE wrote it, not whether that engine was
#       trading real money or paper". The mode was still nowhere in the row.
#   5 — `exit_price` and a NULLable `realized_pnl` (see _migration_5). The table
#       described how a trade ENTERED and had nowhere to record how it LEFT, so
#       a closed row carried no outcome at all: `realized_pnl` defaulted to 0.0
#       (a scratch trade) whether the trade had closed or not, and the exit price
#       was never stored.
SCHEMA_VERSION = 5


def _migration_1(conn) -> None:
    """Everything the old sweep used to add, plus `position_id` (D2).

    Idempotent, so it is safe both for a file that predates all of it and for
    one that already has some of these columns from an earlier run of the sweep.
    """
    add_columns(conn, "executed_trades", {
        "realized_pnl": "REAL DEFAULT 0.0",
        "executor": "TEXT DEFAULT 'BOT (AI)'",
        "session_name": "TEXT DEFAULT 'UNKNOWN'",
        "is_prime_session": "INTEGER DEFAULT 1",
        "adx": "REAL DEFAULT 0.0",
        "plus_di": "REAL DEFAULT 0.0",
        "minus_di": "REAL DEFAULT 0.0",
        "spread_pips": "REAL DEFAULT 0.0",
        "mtf_alignment": "TEXT DEFAULT ''",
        "threats_json": "TEXT DEFAULT '[]'",
        "features_json": "TEXT DEFAULT '{}'",
        # `timestamp` means different things depending on where the row came
        # from — entry time for an engine-logged trade, exit time for one synced
        # from a closed broker deal. `closed_at` is unambiguous: null unless the
        # row really is a closed trade, so a chart can draw an exit marker
        # without guessing.
        "closed_at": "TEXT",
        # D2: `log_trade` stored whatever the broker call returned as "ticket",
        # which for a market order is `result.order` — the ORDER ticket.
        # `sync_mt5_history` closes rows by `deal.position_id`, the POSITION id.
        # Those are different numbers, so a row written at entry could never be
        # matched by the exit deal and stayed open forever (measured: 155 rows
        # with closed_at NULL and realized_pnl 0.0). Persist both, join on the
        # position id.
        "position_id": "INTEGER",
    })


def _migration_2(conn) -> None:
    """D1: tag every row with where its price came from.

    `broker` — the row is backed by a real broker deal. `paper` — simulated
    fills. `synthetic` — a fallback price standing in for a missing quote.

    The backfill can only go as far as the evidence allows, and the evidence is
    timestamp precision: `sync_mt5_history` formats an int epoch (whole
    seconds), `log_trade` uses `datetime.now()` (microseconds). So a whole-second
    timestamp means the row came from — or was later reconciled with — a real
    deal, and can be called `broker`.

    A microsecond timestamp only tells us the ENGINE wrote it, not whether that
    engine was trading real money or paper: nothing in the row records the
    execution mode. Those are `unknown`, not guessed. Measured on
    data/jarvis_history.db: 111 whole-second rows (110 closed) vs 158
    microsecond rows, of which **zero** have ever closed — which is what the
    un-matchable join (D2) looks like from the other end.

    New rows always carry a real value; `unknown` exists only for history.
    """
    add_columns(conn, "executed_trades", {"origin": "TEXT"})
    conn.execute(
        "UPDATE executed_trades SET origin = 'broker' "
        "WHERE origin IS NULL AND timestamp IS NOT NULL AND timestamp NOT LIKE '%.%'"
    )
    conn.execute("UPDATE executed_trades SET origin = 'unknown' WHERE origin IS NULL")


def _migration_3(conn) -> None:
    """Index `origin` so it can actually be queried.

    D1 gave every row an origin but left no way to select on it: without an
    index, "real money only" means a full scan of a table that grows without
    bound, and the statistic that motivated the column in the first place stayed
    as expensive as it was before it existed. A column nobody can filter on is
    documentation, not data.

    The index is composite — `(origin, timestamp)` — because every real question
    is "rows of THIS origin, most recent first": `origin` alone is the leftmost
    prefix, so a bare `WHERE origin = ?` uses it too, and `timestamp` then serves
    the ordering and the `days` window in the same scan instead of a separate
    sort. One index, both shapes.

    `origin` is added defensively first. This step can be retried against a file
    where step 2 was recorded but its ALTER did not land, and `CREATE INDEX` on a
    column that is not there raises — which would abort the whole init block the
    same way the old `position_id` index did.
    """
    add_columns(conn, "executed_trades", {"origin": "TEXT"})
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_executed_trades_origin_ts "
        "ON executed_trades(origin, timestamp)"
    )


def _migration_4(conn) -> None:
    """D1 (completed): record the execution mode the row was produced under.

    `origin` was the first half of this finding and it cannot finish it. It
    classifies the FILL PRICE, and `synthetic` covers two different worlds: a
    paper fill with no quote, and a live fill whose client had lost the
    terminal. Both are "the price is a stand-in"; only one of them is real
    money. Measured on data/jarvis_history.db, the two live questions a row has
    to answer —

        was this real money?          (execution_mode)
        can I trust the price?        (origin)

    — were being answered by one column, so the second was silently standing in
    for the first.

    Nothing in a historical row carries the mode, and the two are not
    recoverable from each other, so the backfill is `unknown` for every existing
    row. Guessing `live` would put 269 rows of unverifiable history behind the
    exact filter this column exists to enable — a filter that looks like
    evidence and isn't. New rows always carry a real value.
    """
    add_columns(conn, "executed_trades", {"execution_mode": "TEXT"})
    conn.execute("UPDATE executed_trades SET execution_mode = 'unknown' WHERE execution_mode IS NULL")
    add_columns(conn, "executed_trades", {"execution_mode": "TEXT"})
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_executed_trades_mode_ts "
        "ON executed_trades(execution_mode, timestamp)"
    )


def _migration_5(conn) -> None:
    """Record a closed trade's OUTCOME, not only how it entered.

    Every column on `executed_trades` described the ENTRY: `entry_price`, `sl`,
    `tp`, `volume`, `expected_value`. There was no `exit_price` and no real
    `realized_pnl`, so a row could not say how its trade ended — the only place
    a realised figure existed was a request-time dict built from live MT5 deals
    (server.py), never written back. `sync_mt5_history` is the one writer that
    had the outcome in hand and it dropped the exit price on the floor.

    `exit_price` is added as a plain NULLable REAL. `realized_pnl` already exists
    (migration 1) but with `DEFAULT 0.0`, and that default is the whole
    ambiguity: a row that was never closed, and a row that closed at break-even,
    both read `0.0`. The backfill below clears the sentinel for exactly the rows
    where it cannot be an outcome — those with no `closed_at`.

    **Only rows with `closed_at IS NULL` are cleared.** `closed_at` is written
    only when a real exit deal was matched, so it is the one field that says
    "this trade really closed". A closed row keeps its value even when that value
    is a genuine `0.0` scratch. Nothing is invented for history: a row that never
    closed gets NULL, which reads as "no outcome recorded", not "broke even".
    """
    add_columns(conn, "executed_trades", {
        "exit_price": "REAL",
        # Defensive: present on every file that ran migration 1, added here so a
        # file that somehow skipped it still gets the column before the UPDATE.
        "realized_pnl": "REAL",
    })
    conn.execute(
        "UPDATE executed_trades SET realized_pnl = NULL "
        "WHERE realized_pnl = 0.0 AND closed_at IS NULL"
    )


MIGRATIONS = {1: _migration_1, 2: _migration_2, 3: _migration_3, 4: _migration_4, 5: _migration_5}

# Executor tags, matched as TOKENS. The old `"ai" in comment_lower` substring test
# also matched "trailing", "pair", "main", "wait" and "chair", so a manual trade
# commented "trailing stop" was filed as BOT (AI) and skewed the per-executor
# performance stats. The magic number is authoritative; these are the fallback.
#
# The manual side is a plain substring on purpose: "manual"/"desk" are long and
# distinctive, and our own tag is the compound "ManualDesk", which lowercases to
# "manualdesk" - word boundaries would never match it. "ai" is the opposite case:
# three characters is short enough to hide inside ordinary words, so it needs
# boundaries on both sides.
_BOT_TAG_RX = re.compile(r"(?<![a-z0-9])(?:hm[_\s-]?algo2?|hma2|jarvis[_\s-]?auto|ai)(?![a-z0-9])")
_MANUAL_TAG_RX = re.compile(r"(?:manual|desk)")

logger = logging.getLogger("JARVIS_Database")


def _bracket_side_ok(action: str, entry: float, value: float, *, is_stop: bool) -> bool:
    """True when a bracket price sits on the correct side of the entry.

    `sl`/`tp` reach `sync_mt5_history` by string-parsing the broker order comment
    (`[sl...]` / `[tp...]`), so they are only as trustworthy as that comment.
    Measured on data/jarvis_history.db: 24 rows carried a stop on the WRONG side
    of entry — 14 BUY with `sl >= entry`, 10 SELL with `sl <= entry` — and the 20
    closed ones among them were **20 winners totalling +37.38**, which a genuine
    stop cannot produce. That is the signature of the TAKE-PROFIT level having
    been parsed into the `sl` field. A bracket that disagrees with the direction
    makes every risk, R-multiple and expectancy figure computed from it wrong, so
    it is refused rather than stored.

    A non-positive value means "no tag was parsed". That is not a claim about the
    market, so it is left alone — this function only rejects a value that is
    demonstrably on the wrong side.
    """
    if value <= 0.0:
        return True
    if action == "BUY":
        return value < entry if is_stop else value > entry
    if action == "SELL":
        return value > entry if is_stop else value < entry
    return False

class SQLiteTradeDB:
    def __init__(self, db_path="jarvis_history.db"):
        # Anchor relative paths on the repo data dir; a CWD-relative path meant
        # the learning engine could read a DB the live loop never wrote to.
        self.db_path = resolve_db_path(db_path)
        if self.db_path and self.db_path != ":memory:":
            ensure_data_dir()
        self._local = threading.local()
        #: Read paths call `sync_mt5_history`; this keeps a read from turning
        #: into a broker round-trip on every request. PER INSTANCE on purpose —
        #: class-level state leaked the window across unrelated databases, so a
        #: freshly built DB could inherit another one's stamp and silently skip
        #: its first sync. See the method for the measurement.
        self._last_mt5_sync = 0.0
        self._init_db()

    def _get_conn(self):
        if not hasattr(self._local, "conn"):
            self._local.conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=10.0)
            self._local.conn.execute("PRAGMA journal_mode=WAL;")
            self._local.conn.execute("PRAGMA synchronous=NORMAL;")
            self._local.conn.execute("PRAGMA cache_size=-64000;")
            self._local.conn.execute("PRAGMA temp_store=MEMORY;")
        return self._local.conn

    def close(self):
        if hasattr(self._local, "conn"):
            try:
                self._local.conn.close()
            except Exception:
                pass
            del self._local.conn

    def __del__(self):
        self.close()

    def _init_db(self):
        conn = self._get_conn()
        try:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS executed_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket INTEGER,
                    -- The MT5 POSITION id, which is not the same number as the
                    -- order ticket `send_market_order` returns. See `position_id`
                    -- in the migration map below.
                    position_id INTEGER,
                    symbol TEXT,
                    action TEXT,
                    entry_price REAL,
                    sl REAL,
                    tp REAL,
                    volume REAL,
                    timestamp TEXT,
                    ai_score REAL,
                    regime TEXT,
                    expected_value REAL,
                    -- The OUTCOME of the trade, written when it closes. NULL means
                    -- "not recorded", never 0.0: a 0.0 P&L is a real break-even
                    -- trade, and `dict.get()` on a missing key also yields 0.0,
                    -- so 0.0 cannot be allowed to stand for both.
                    exit_price REAL,
                    realized_pnl REAL,
                    executor TEXT DEFAULT 'BOT (AI)',
                    session_name TEXT DEFAULT 'UNKNOWN',
                    is_prime_session INTEGER DEFAULT 1,
                    adx REAL DEFAULT 0.0,
                    plus_di REAL DEFAULT 0.0,
                    minus_di REAL DEFAULT 0.0,
                    spread_pips REAL DEFAULT 0.0,
                    mtf_alignment TEXT DEFAULT '',
                    threats_json TEXT DEFAULT '[]',
                    features_json TEXT DEFAULT '{}'
                )
            ''')
            # Create fast query lookup indices
            conn.execute("CREATE INDEX IF NOT EXISTS idx_executed_trades_ticket ON executed_trades(ticket);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_executed_trades_symbol ON executed_trades(symbol);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_executed_trades_timestamp ON executed_trades(timestamp);")
            # D3: bring the file up to the current shape one numbered step at a
            # time. This replaces an unconditional "add whatever column is
            # missing" sweep, which ran on every open and could express neither
            # ordering, a backfill, nor a refusal — and which, because it was
            # inside the same try/except as everything else, could abort partway
            # through while the file still claimed to be fine.
            #
            # Steps run BEFORE the index on `position_id`: an index on a column
            # the table does not have yet raises, and that used to abort the
            # whole init block.
            version = migrate(conn, "executed_trades", SCHEMA_VERSION, MIGRATIONS)
            if version == SCHEMA_VERSION:
                conn.execute("CREATE INDEX IF NOT EXISTS idx_executed_trades_position_id ON executed_trades(position_id);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_executed_trades_sym_reg_id ON executed_trades(symbol, regime, id);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_executed_trades_reg_closed_id ON executed_trades(regime, closed_at, id);")
            conn.commit()

            logger.info("SQLite database initialized successfully.")
        except Exception as e:
            logger.error(f"Failed to initialize SQLite DB: {e}")

    def log_trade(
        self,
        ticket: int,
        symbol: str,
        action: str,
        entry: float,
        sl: float,
        tp: float,
        volume: float,
        score: float,
        regime: str,
        ev: float,
        executor: str = "BOT (AI)",
        session_name: str = "UNKNOWN",
        is_prime_session: bool = True,
        adx: float = 0.0,
        plus_di: float = 0.0,
        minus_di: float = 0.0,
        spread_pips: float = 0.0,
        mtf_alignment: str = "",
        threats_json: str = "[]",
        features_json: str = "{}",
        position_id: Optional[int] = None,
        origin: Optional[str] = None,
        execution_mode: Optional[str] = None
    ):
        """Journal an entry.

        `ticket` is the order ticket the broker call returned; `position_id` is
        the MT5 position id the exit deal will later be keyed on. They are
        different numbers — pass both, or the row can never be closed (D2).

        `origin` is where the price came from: `broker`, `paper` or `synthetic`.
        `execution_mode` is whether it was real money: `live`, `paper` or
        `demo`. **They are different questions** — a live fill whose client had
        lost the terminal is `origin='synthetic'` but `execution_mode='live'`,
        which is precisely the row that used to be unclassifiable. Pass both.

        Anything not in the respective tuple — including None — is recorded as
        `unknown` rather than guessed, so a statistic that filters on either can
        never silently include rows of unknown provenance.
        """
        # ORIGINS is a tuple, not a mapping. A `.get()` here raised
        # AttributeError on EVERY call — and `log_trade` swallows its
        # exceptions into a log line, so the symptom was not a crash but a
        # journal that silently stopped recording trades at all.
        origin = origin if origin in ORIGINS else "unknown"
        execution_mode = execution_mode if execution_mode in EXECUTION_MODES else "unknown"
        conn = self._get_conn()
        try:
            conn.execute('''
                INSERT INTO executed_trades (
                    ticket, position_id, origin, execution_mode, symbol, action, entry_price, sl, tp, volume, timestamp,
                    ai_score, regime, expected_value, exit_price, realized_pnl, executor, session_name,
                    is_prime_session, adx, plus_di, minus_di, spread_pips,
                    mtf_alignment, threats_json, features_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                ticket, position_id, origin, execution_mode, symbol, action, entry, sl, tp, volume,
                datetime.now(timezone.utc).isoformat(),
                score, regime, ev,
                # The outcome columns are written NULL, not omitted. `realized_pnl`
                # carries `DEFAULT 0.0` on files that ran migration 1, so an
                # omitted value would come back as 0.0 — indistinguishable from a
                # break-even close. At open there is no outcome, and NULL says so.
                None, None,
                executor, session_name, int(is_prime_session),
                adx, plus_di, minus_di, spread_pips, mtf_alignment, threats_json, features_json
            ))
            conn.commit()
        except Exception as e:
            logger.error(f"Failed to log trade to DB: {e}")

    def record_trade_exit(
        self,
        ticket: Optional[int] = None,
        position_id: Optional[int] = None,
        exit_price: Optional[float] = None,
        realized_pnl: Optional[float] = None,
        closed_at: Optional[str] = None,
    ) -> bool:
        """Write a trade's OUTCOME onto the row written when it opened.

        This is the other half of :meth:`log_trade`: a row says how a trade
        entered, and this says how it left. Without it a closed trade had no
        stored result at all — the only realised P&L lived in a request-time dict
        built from live MT5 deals (server.py) and was never written back.

        Keyed on `position_id` first, then on `ticket` for rows that predate that
        column, matching `sync_mt5_history` so both writers close the same row.

        **`None` means "not recorded" and is stored as NULL — never as 0.0.**
        A `0.0` P&L is a real break-even trade; a missing one is not, and
        `dict.get()` returns 0.0 for a missing key, so allowing the two to share
        a value is exactly the ambiguity that made every open row look like a
        scratch. `COALESCE` means a later call that does not know the outcome
        cannot erase one that is already stored.

        Returns True if a row was found and updated.
        """
        if ticket is None and position_id is None:
            return False
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            # Prefer the position id; a row written before `position_id` existed
            # still holds the id in `ticket` (see migration 1's note).
            if position_id is not None:
                cur.execute(
                    "SELECT id FROM executed_trades "
                    "WHERE position_id = ? OR (position_id IS NULL AND ticket = ?) "
                    "ORDER BY (position_id IS NOT NULL) DESC, id ASC LIMIT 1",
                    (position_id, position_id),
                )
            else:
                cur.execute(
                    "SELECT id FROM executed_trades WHERE ticket = ? "
                    "ORDER BY id ASC LIMIT 1",
                    (ticket,),
                )
            row = cur.fetchone()
            if not row:
                return False
            cur.execute(
                "UPDATE executed_trades SET "
                "realized_pnl = COALESCE(?, realized_pnl), "
                "exit_price = COALESCE(?, exit_price), "
                "closed_at = COALESCE(?, closed_at) "
                "WHERE id = ?",
                (realized_pnl, exit_price, closed_at, row[0]),
            )
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to record trade exit: {e}")
            return False

    def sync_mt5_history(self, days: int = 30, limit: int = 100):
        """Syncs executed and closed trades from MT5 broker history into SQLite database.

        Called from the READ path (`fetch_recent_trades`), so it must never
        attach to the terminal itself. It used to do exactly that:

            if not mt5.terminal_info():
                mt5.initialize()          # unbounded, and bypasses the one gate

        `initialize()` with no terminal to attach to tries to launch one and
        blocks 60-100s inside native code HOLDING THE GIL — and because this ran
        on the request thread, every `/api/history` request froze the whole
        process for that long. Measured on the live server: `py-spy` showed two
        request threads parked in `fetch_recent_trades -> sync_mt5_history`
        while `/api/history`, `/api/telemetry_state`, `/api/market-status` and
        `/api/radar` all timed out and the UI pages served fine.

        The gate also gives the honest answer: no terminal in this process means
        there is nothing to sync FROM, so returning early is correct, not a
        degradation.
        """
        from jarvis.data.broker_symbols import ensure_mt5_terminal

        if not ensure_mt5_terminal():
            return

        # A read must not trigger a sync per request. With a terminal up, every
        # `/api/history` call re-read 30 days of deals; the throttle keeps the
        # read cheap and leaves the polling to the background synchroniser.
        now = time.time()
        if now - self._last_mt5_sync < _MT5_SYNC_MIN_INTERVAL_SEC:
            return
        self._last_mt5_sync = now

        try:
            import MetaTrader5 as mt5
            from datetime import datetime, timedelta, timezone
            from jarvis.data.symbol_registry import resolve

            # Broker server time can be ahead of local machine time (e.g. GMT+3 / EET)
            from_date = datetime.now() - timedelta(days=days)
            to_date = datetime.now() + timedelta(days=7)
            deals = mt5.history_deals_get(from_date, to_date)
            if not deals:
                return

            # `deal.time` is broker SERVER time, so reading it as UTC dated every
            # synced trade 2-3 hours into the future (XM runs GMT+2/+3). Derive the
            # offset from the deals' own symbols and store true UTC.
            deal_symbols = sorted({str(d.symbol) for d in deals if getattr(d, "symbol", None)})
            broker_offset = broker_utc_offset(mt5_module=mt5, symbols=deal_symbols)

            conn = self._get_conn()
            pos_map = {}
            for d in deals:
                pid = d.position_id
                if not pid:
                    continue
                if pid not in pos_map:
                    pos_map[pid] = {"entry": None, "exit": None}
                if d.entry == 0:  # DEAL_ENTRY_IN
                    pos_map[pid]["entry"] = d
                elif d.entry == 1:  # DEAL_ENTRY_OUT
                    pos_map[pid]["exit"] = d

            for pid, pdata in pos_map.items():
                entry_deal = pdata["entry"]
                exit_deal = pdata["exit"]
                target_deal = exit_deal or entry_deal
                if not target_deal:
                    continue

                raw_sym = target_deal.symbol
                spec = resolve(raw_sym)
                clean_sym = spec.canonical if spec else raw_sym.replace(".i#", "").replace("#", "")
                
                # Determine buy/sell side accurately
                if entry_deal:
                    side = "BUY" if entry_deal.type == 0 else "SELL"
                elif exit_deal:
                    side = "BUY" if exit_deal.type == 1 else "SELL"
                else:
                    side = "BUY"

                entry_p = float(entry_deal.price) if entry_deal else float(target_deal.price)
                vol = float(target_deal.volume)
                # C8: NET realised P&L, matching the figure the state synchronizer
                # publishes on `trade_closed` (state_synchronizer.py:73,
                # `sum(d.profit + d.swap + d.commission ...)`). The closing
                # deal's `profit` is GROSS — it excludes the commission and swap
                # the broker books on BOTH the entry and the exit — so adding them
                # here is what reconciles the two numbers. An opening deal's
                # `profit` is 0, so commission+swap across entry and exit plus the
                # exit's profit reproduces that per-position total for the common
                # one-entry / one-exit case.
                entry_comm = float(getattr(entry_deal, "commission", 0.0) or 0.0) if entry_deal else 0.0
                entry_swap = float(getattr(entry_deal, "swap", 0.0) or 0.0) if entry_deal else 0.0
                exit_comm = float(getattr(exit_deal, "commission", 0.0) or 0.0) if exit_deal else 0.0
                exit_swap = float(getattr(exit_deal, "swap", 0.0) or 0.0) if exit_deal else 0.0
                # A position with no exit deal has NOT closed, so it has no
                # outcome: the commissions booked on the open alone are not one.
                # Recording 0.0 there would re-create the exact ambiguity
                # migration 5 removed, so the outcome stays NULL until a real
                # exit deal arrives.
                if exit_deal:
                    pnl = float(exit_deal.profit) + entry_comm + entry_swap + exit_comm + exit_swap
                    # `TradeDeal.price` on the CLOSING deal is the fill the broker
                    # actually gave. Verified against the MT5 5.0 `TradeDeal`
                    # struct, whose fields include `price`, `profit`, `commission`
                    # and `swap` — so the exit price is read, not invented.
                    exit_p = float(exit_deal.price)
                else:
                    pnl = None
                    exit_p = None
                target_time = exit_deal.time if exit_deal else target_deal.time
                dt_str = datetime.fromtimestamp(
                    float(target_time) - broker_offset, timezone.utc
                ).isoformat()

                # The ENTRY time is a different fact, from a different deal.
                #
                # Deriving both from `target_deal` is what destroyed every entry
                # timestamp in the journal: `dt_str` is the CLOSE time whenever
                # the position has closed, and the close UPDATE below wrote it
                # into `timestamp`. A row therefore kept its id while its
                # timestamp jumped forward to the exit second. Measured on
                # data/jarvis_history.db: 144/144 closed rows had
                # `closed_at == timestamp` — a zero-duration trade with a
                # non-zero realised P&L, which is arithmetically impossible —
                # and 33 rows sat out of chronological order, because the
                # primary key no longer tracked time. Hold time, session
                # attribution and the `days=N` history window were all reading
                # the exit time as if it were the entry.
                #
                # `entry_deal.time` is the fill that actually opened the
                # position. When there is no entry deal the row is being
                # reconstructed from a partial history and `target_deal` is the
                # best available anchor.
                entry_time = entry_deal.time if entry_deal else target_deal.time
                entry_dt_str = datetime.fromtimestamp(
                    float(entry_time) - broker_offset, timezone.utc
                ).isoformat()

                # Determine executor (BOT vs MANUAL)
                magic_num = getattr(target_deal, "magic", 0)
                raw_comment = str(exit_deal.comment if exit_deal else target_deal.comment or "")
                comment_lower = raw_comment.lower()
                
                # Determine executor (BOT vs MANUAL). The magic number decides:
                # the engine stamps 888999 on everything it places, manual tickets
                # carry 0. Comments are only consulted for foreign tickets.
                if magic_num == 888999:
                    exec_label = "BOT (AI)"
                elif magic_num == 0:
                    exec_label = "MANUAL"
                elif _MANUAL_TAG_RX.search(comment_lower):
                    exec_label = "MANUAL"
                elif _BOT_TAG_RX.search(comment_lower):
                    exec_label = "BOT (AI)"
                else:
                    exec_label = "BOT (AI)" if magic_num > 0 else "MANUAL"

                sl_val = 0.0
                tp_val = 0.0
                if "[sl" in raw_comment:
                    try:
                        sl_val = float(raw_comment.split("[sl")[1].split("]")[0].strip())
                    except Exception as e:
                        logger.debug(f"Could not parse [sl] tag from comment {raw_comment!r}: {e}")
                if "[tp" in raw_comment:
                    try:
                        tp_val = float(raw_comment.split("[tp")[1].split("]")[0].strip())
                    except Exception as e:
                        logger.debug(f"Could not parse [tp] tag from comment {raw_comment!r}: {e}")

                # A parsed bracket is only as good as the comment it came from.
                # `trade_guard.validate_pre_execution` already refuses an inverted
                # bracket at order build time, so a wrong-side value reaching this
                # point cannot have come from a valid order — the parse picked up
                # the wrong number (typically the target, which sits on the other
                # side of entry). Storing it would corrupt every risk and
                # R-multiple figure derived from the row.
                #
                # Only checked when the entry deal is in the window: without it
                # `entry_p` falls back to `target_deal.price`, which for a closed
                # position is the EXIT price, and the comparison would be against
                # the wrong reference. In that case the value is left as-is rather
                # than judged against a number we do not have.
                if entry_deal is not None:
                    if not _bracket_side_ok(side, entry_p, sl_val, is_stop=True):
                        logger.warning(
                            "Refusing a parsed stop for position %s: sl=%s is on the "
                            "wrong side of entry=%s for a %s order (comment=%r).",
                            pid, sl_val, entry_p, side, raw_comment,
                        )
                        sl_val = 0.0
                    if not _bracket_side_ok(side, entry_p, tp_val, is_stop=False):
                        logger.warning(
                            "Refusing a parsed target for position %s: tp=%s is on the "
                            "wrong side of entry=%s for a %s order (comment=%r).",
                            pid, tp_val, entry_p, side, raw_comment,
                        )
                        tp_val = 0.0

                # Find the row this position belongs to.
                #
                # D2: `pid` is the POSITION id. The row written when the trade
                # opened may hold it in `position_id`, or — for every row that
                # predates that column — in `ticket`, because the old code stored
                # `result.order` there and `ticket` was all we had. Matching on
                # `ticket` alone silently missed both cases: an engine-logged row
                # keyed by order ticket never equalled a position id, so it was
                # never updated and stayed open with realized_pnl 0.0 forever.
                # Prefer the position id; fall back to the ticket for legacy rows.
                cur = conn.cursor()
                cur.execute(
                    "SELECT id FROM executed_trades "
                    "WHERE position_id = ? OR (position_id IS NULL AND ticket = ?) "
                    "ORDER BY (position_id IS NOT NULL) DESC, id ASC LIMIT 1",
                    (pid, pid),
                )
                row = cur.fetchone()
                if not row:
                    regime_str = "TREND_BULL" if side == "BUY" else "TREND_BEAR"
                    # These rows ARE broker deals — they are reconstructed from
                    # `history_deals_get`, so `origin` is not in doubt here.
                    #
                    # AI5 / exec-summary #4: `ai_score` and `expected_value` used
                    # to be written as `85.0` and `pnl`. Neither is known here —
                    # a deal reconstructed from broker history carries no decision,
                    # so there is no forecast to record, and 85.0 was a fabricated
                    # score that every one of these rows shared. `expected_value`
                    # set to the realised P&L was the worse half: it made the
                    # forecast column mean "outcome" for reconstructed rows and
                    # "forecast" for engine-logged ones, which is why
                    # `self_learning` could not tell them apart. NULL is honest:
                    # no forecast was made.
                    # `execution_mode='live'`: these rows are reconstructed from
                    # `history_deals_get`, so they are real money by
                    # construction — a deal the broker actually booked — whether
                    # or not the engine that is reading them is currently
                    # trading paper. This is the one place the mode is a fact
                    # rather than something we were told.
                    conn.execute('''
                        INSERT INTO executed_trades (ticket, position_id, origin, execution_mode, symbol, action, entry_price, sl, tp, volume, timestamp, ai_score, regime, expected_value, exit_price, realized_pnl, executor, closed_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (pid, pid, "broker", "live", clean_sym, side, entry_p, sl_val, tp_val, vol, entry_dt_str, None, regime_str, None, exit_p, pnl, exec_label,
                          dt_str if exit_deal else None))
                else:
                    # Update realized PnL, executor, and close timestamp for completed positions.
                    # `closed_at` is only ever written when there really is an exit
                    # deal — rewriting it to null on a later sync would erase a
                    # close time we already knew.
                    # Keyed on the matched row id, not re-matched, so the UPDATE
                    # can never hit a different row than the SELECT just found.
                    # AI5 / exec-summary #4: `expected_value` is NOT updated here.
                    #
                    # It used to be set to `pnl` alongside `realized_pnl`, which
                    # overwrote the FORECAST with the OUTCOME the moment a trade
                    # closed — measured on the live journal: every closed row had
                    # expected_value == realized_pnl. That destroys the forecast,
                    # so forecast accuracy can never be measured (it blocks AI10
                    # calibration and any Brier scoring), and it is why
                    # `self_learning` was averaging the very thing it was supposed
                    # to predict. The forecast is written once, at open, and is
                    # left alone here.
                    # `timestamp` is deliberately NOT updated. It is the ENTRY
                    # time and this UPDATE only ever runs for a row that already
                    # exists, i.e. one whose entry time was written when the
                    # trade opened. Writing `dt_str` (the exit time) here was
                    # silently rewriting it — see the `entry_dt_str` note above.
                    # The close time has its own column, `closed_at`.
                    conn.execute('''
                        UPDATE executed_trades
                        SET realized_pnl = COALESCE(?, realized_pnl), executor = ?,
                            -- The exit price, when the closing deal carried one.
                            -- COALESCE for the same reason as `realized_pnl`: a
                            -- sync that cannot see the exit deal must not erase an
                            -- outcome the live close path already recorded.
                            exit_price = COALESCE(?, exit_price),
                            -- A row the broker's own history has now matched is
                            -- proven real, whatever it was labelled at entry.
                            origin = CASE WHEN origin IS NULL OR origin IN ('unknown', 'synthetic')
                                          THEN 'broker' ELSE origin END,
                            position_id = CASE WHEN ? IS NOT NULL THEN ? ELSE position_id END,
                            closed_at = CASE WHEN ? IS NOT NULL THEN ? ELSE closed_at END,
                            sl = CASE WHEN ? > 0 THEN ? ELSE sl END,
                            tp = CASE WHEN ? > 0 THEN ? ELSE tp END
                        WHERE id = ?
                    ''', (pnl, exec_label, exit_p,
                          pid, pid,
                          dt_str if exit_deal else None, dt_str if exit_deal else None,
                          sl_val, sl_val, tp_val, tp_val, row[0]))

            # ── Pending-order reconciliation ─────────────────────────────
            # A limit/stop entry is journalled the moment the broker accepts
            # the order (`is_fill or is_placed` in execution_engine), which is
            # correct: MT5 assigns the eventual position id = opening order
            # ticket, so when the limit actually fills the deal matches this
            # row and `sync_mt5_history` closes it. The gap is the order the
            # broker never fills: our own cycle loop surfaces a PendING entry
            # price every few minutes, cancelling and re-placing as the market
            # moves, and each cancelled order left its row open forever.
            # Measured on data/jarvis_history.db: 5 gold SELL rows from
            # 2026-10-20 05:10-05:51 UTC sat open with `realized_pnl=NULL`,
            # `position_id=NULL` and `closed_at=NULL` while the matching
            # broker orders (`history_orders_get`) were all state 2 =
            # CANCELLED with `position_id=0` and no deals anywhere. Those
            # ghost rows inflated every "open position" statistic count and
            # kept cancelled strategy labels inside the live trade stats.
            #
            # Reconciliation rule for an open row keyed by ticket:
            #   * the order exists in the broker history window,
            #   * its final state is CCANCELLED,  and
            #   * no deal was ever booked for that ticket,
            # → the trade will never happen: stamp `closed_at` with the
            #   order's own terminal time and leave the outcome NULL. An
            #   unfilled order has no realised P&L, and NULL is the honest
            #   value for "no outcome recorded" (migration 5's contract).
            #   The inconsequential `exit_price` COALESCE keeps the statement's
            #   intent explicit: no price was ever reached, so no price can
            #   appear that was not a fill.
            ORDER_STATE_CANCELLED = 2
            order_map: dict = {}
            try:
                hist_orders = mt5.history_orders_get(from_date, to_date) or []
                for ho in hist_orders:
                    order_map[int(ho.ticket)] = (int(getattr(ho, "state", -1)), getattr(ho, "time_done", 0))
            except Exception as e:
                logger.debug(f"Pending-order reconciliation could not read order history: {e}")
            if order_map:
                open_cur = conn.cursor()
                open_cur.execute(
                    "SELECT id, ticket FROM executed_trades "
                    "WHERE closed_at IS NULL AND position_id IS NULL AND ticket IS NOT NULL"
                )
                off_orders_touched = 0
                for row_id, row_ticket in open_cur.fetchall():
                    st_done = order_map.get(int(row_ticket))
                    if not st_done:
                        continue
                    state, time_done = st_done
                    if state != ORDER_STATE_CANCELLED:
                        continue
                    cancel_dt = ""
                    if time_done:
                        cancel_dt = datetime.fromtimestamp(
                            float(time_done) - broker_offset, timezone.utc
                        ).isoformat()
                    conn.execute(
                        "UPDATE executed_trades "
                        "SET closed_at = COALESCE(closed_at, ?), "
                        "    exit_price = COALESCE(exit_price, NULL) "
                        "WHERE id = ? AND closed_at IS NULL",
                        (cancel_dt or datetime.now(timezone.utc).isoformat(), row_id),
                    )
                    off_orders_touched += 1
                if off_orders_touched:
                    logger.info(
                        f"Pending-order reconciliation: stamped {off_orders_touched} "
                        "cancelled-order row(s) closed (no deals were ever booked)."
                    )
            conn.commit()

        except Exception as e:
            logger.error(f"Failed to sync MT5 history: {e}")

    def fetch_recent_trades(self, limit=100, days=None, origin=None, execution_mode=None):
        """Most recent journal rows, newest first.

        `days` narrows the window. It defaults to None — no window — so the
        callers that predate it (the classic terminal, the console) keep exactly
        the behaviour they have always had.

        The dashboard passes the user's Window selection here. Until it did, the
        selection reached only the MT5 half of /api/history and left journal rows
        unfiltered, so a "1 day" window answered with a month of trades: measured
        2026-09-17, `days=1` returned 185 rows whose oldest was 24 days old.
        The column is `timestamp` because that is the field the existing sort
        orders by, and filtering on the same field the sort uses keeps the two
        consistent.

        Note the internal sync still uses its own 30-day budget: it decides how
        much history to *populate*, which is a different question from how much
        to *display*.

        `origin` keeps rows whose price came from the given origin(s) — pass one
        string, or a collection to allow several. It is validated against
        :data:`ORIGINS` and an unknown value selects nothing rather than
        silently widening to everything, because a caller asking for
        `origin="real"` must not be handed the full table and believe it filtered.

        `execution_mode` keeps rows produced under the given mode(s): `live`,
        `paper`, `demo`, `unknown`. Use THIS one to separate real money from
        simulated — `origin` cannot do it, because `synthetic` covers a paper
        fill with no quote and a live fill whose client had lost the terminal
        alike. A caller that wants "real money, trustworthy price" asks for
        `execution_mode="live"` AND `origin="broker"`, which is only the same
        set by coincidence today and by no means guaranteed to stay that way.
        """
        self.sync_mt5_history(days=30, limit=limit)
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            # Stored timestamps carry a +00:00 offset; SQLite's datetime()
            # parses that and normalises to UTC, which is what datetime('now')
            # returns too, so the comparison is apples to apples.
            clauses = []
            params = []
            if days is not None:
                clauses.append("datetime(timestamp) >= datetime('now', ?)")
                params.append(f"-{max(1, int(days))} days")
            if origin is not None:
                wanted = _origin_filter(origin)
                if not wanted:
                    # Asked for origins that cannot exist. Return nothing: the
                    # alternative (ignoring the filter) looks like a working
                    # filter that happens to agree with no filter at all.
                    return []
                clauses.append(f"origin IN ({','.join('?' * len(wanted))})")
                params.extend(wanted)
            if execution_mode is not None:
                wanted_modes = _mode_filter(execution_mode)
                if not wanted_modes:
                    # Same contract: asking for a mode that cannot exist returns
                    # nothing, not everything.
                    return []
                clauses.append(f"execution_mode IN ({','.join('?' * len(wanted_modes))})")
                params.extend(wanted_modes)

            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            params.append(limit)
            cur.execute(
                "SELECT * FROM executed_trades" + where +
                " ORDER BY datetime(timestamp) DESC, id DESC LIMIT ?",
                params)
            columns = [description[0] for description in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]
        except Exception as e:
            logger.error(f"Failed to fetch trades: {e}")
            return []

    def realised_pnl_by_origin(self, days=None):
        """Realised P&L split by where each fill price came from.

        This is the statistic D1 exists to make possible. Mixed together, a
        simulated fill and a real one are one number that describes neither —
        measured on data/jarvis_history.db, 158 of 269 rows are engine-logged and
        none of them have ever closed, so pooling them drags every average toward
        zero and understates the result.

        Only CLOSED rows contribute: `realized_pnl = 0.0` is the sentinel for
        "not closed" (there is no `exit_price`, so an open row carries 0.0), and
        counting those would divide by a denominator that is mostly unfinished
        trades. A genuine scratch exit is therefore invisible — a known limit of
        the stored data, not of this query.

        `days` windows on `timestamp`; None means no window. Returns origins that
        are actually present, plus `ALL` (everything pooled, i.e. the number to
        distrust) and `BROKER_ONLY` (the number to believe).
        """
        conn = self._get_conn()
        params = []
        where = "WHERE realized_pnl <> 0"
        if days is not None:
            where += " AND datetime(timestamp) >= datetime('now', ?)"
            params.append(f"-{max(1, int(days))} days")

        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT origin, COUNT(*), SUM(realized_pnl), "
                "SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN realized_pnl < 0 THEN 1 ELSE 0 END) "
                "FROM executed_trades " + where + " GROUP BY origin",
                params)
            rows = cur.fetchall()

            # Open rows per origin, so a reader can see how much of the book is
            # still unresolved before trusting any average.
            cur.execute(
                "SELECT origin, COUNT(*) FROM executed_trades "
                "WHERE realized_pnl = 0 OR realized_pnl IS NULL "
                + (" AND datetime(timestamp) >= datetime('now', ?)" if days is not None else "")
                + " GROUP BY origin",
                params if days is not None else [])
            open_by_origin = {r[0]: r[1] for r in cur.fetchall()}
        except Exception as e:
            logger.error(f"Failed to aggregate realised P&L: {e}")
            return {"by_origin": {}, "ALL": _empty_pnl(), "BROKER_ONLY": _empty_pnl()}

        by_origin = {}
        for origin, closed, total, winners, losers in rows:
            by_origin[origin if origin in ORIGINS else "unknown"] = _pnl_stats(
                closed or 0, float(total or 0.0), winners or 0, losers or 0,
                open_by_origin.get(origin, 0))

        def _pool(keys):
            closed = sum(by_origin[k]["closed"] for k in keys if k in by_origin)
            wins = sum(by_origin[k]["winners"] for k in keys if k in by_origin)
            loss = sum(by_origin[k]["losers"] for k in keys if k in by_origin)
            total = sum(by_origin[k]["realised_pnl"] for k in keys if k in by_origin)
            opens = sum(by_origin[k]["open"] for k in keys if k in by_origin)
            return _pnl_stats(closed, total, wins, loss, opens)

        return {
            "by_origin": by_origin,
            "ALL": _pool(list(by_origin)),
            "BROKER_ONLY": _pool(["broker"]),
        }

TRADE_DB = SQLiteTradeDB()
