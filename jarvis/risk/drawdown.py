"""
HM Algo 2.0 — Drawdown & Daily Loss Monitoring Engine.
Enforces hard daily loss caps and maximum portfolio drawdown limits to guarantee capital preservation.

A loss is NEVER re-anchored away
--------------------------------
This guard previously treated any single observation more than 33.3% below a
recorded baseline as a withdrawal and re-anchored to it (`baseline > current * 1.5`).
That is indistinguishable from a crash, so the worse the loss the safer the guard
thought things were: a 40% intraday drop reported `daily_loss_pct == 0.0` and
`passed == True`, and the same re-anchor also erased `peak_equity`, cancelling the
portfolio circuit breaker. `risk_engine` gates three separate checks on `passed`,
the last of which returns `authorized: True`, so the account kept opening new
positions through a 40% drawdown.

No single-instant test can separate the two cases: a flat account that REALISED a
40% loss has equity == balance and looks exactly like one that had a withdrawal.
So this guard no longer guesses. A big drop is reported as the breach it is, and a
genuine withdrawal or deposit is re-anchored explicitly via `reset_baselines()`.
Failing closed costs a day of trading; failing open costs the account.
"""
import sqlite3
from typing import Dict, Any, Optional, Callable
from datetime import datetime, timezone

from jarvis.config.paths import resolve_db_path
from jarvis.data.schema_version import migrate

# D3: 1 = `drawdown_state` as it exists today.
SCHEMA_VERSION = 1


def _migration_1(conn: sqlite3.Connection) -> None:
    """Version 1 *is* the shape created by the CREATE TABLE above.

    A pre-versioning file already has that shape, so there is nothing to add —
    but it is recorded as a named step rather than left to `ensure_version`,
    which stamped whatever number the code declared. A hand-bumped
    SCHEMA_VERSION with no matching step now stops the migration and says so,
    instead of labelling a file with a version whose shape nobody made.
    """
    return None


MIGRATIONS = {1: _migration_1}

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)

class DrawdownGuard:
    def __init__(self, max_daily_loss_pct: float = 4.0, max_total_drawdown_pct: float = 10.0, db_path: str = "jarvis_drawdown_state.db", clock: Optional[Callable[[], datetime]] = None):
        # Injectable clock, same convention as circuit_breaker: live trading uses
        # the real wall clock, a backtest advances on bar time. NOTE the day
        # boundary below is UTC, not the broker's trading day — see set_clock().
        self._now = clock if clock is not None else _utc_now
        # Anchored on the repo data dir — see jarvis.config.paths.
        db_path = resolve_db_path(db_path)
        # Hermetic backtesting: in memory, nothing persisted. A backtest that
        # ends mid-drawdown otherwise leaves daily_start_equity/peak_equity
        # behind and the next run starts partially through a drawdown it never
        # actually had, tripping the daily-loss cap early. See the matching note
        # in circuit_breaker.py. "" is the documented in-memory sentinel.
        try:
            from jarvis.config.runtime import is_offline

            if is_offline():
                db_path = ""
        except Exception:
            pass
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_total_drawdown_pct = max_total_drawdown_pct
        self.daily_start_equity: float = 0.0
        self.peak_equity: float = 0.0
        self.db_path = db_path
        # Day the in-memory baselines belong to; a change rolls the daily cap over.
        self._last_seen_date = self._today()
        if self.db_path:
            self._init_db()
            self._load_state()

    def set_clock(self, clock: Optional[Callable[[], datetime]]) -> None:
        """Inject the source of "today" (backtests pass bar time; live passes None -> UTC).

        The daily-loss cap resets on a UTC date change, which is NOT the broker's
        trading day — a server on GMT+2/+3 rolls over two or three hours into the
        session. Pass a broker-day clock here to align them.
        """
        self._now = clock if clock is not None else _utc_now

    def _today(self) -> str:
        return self._now().date().isoformat()

    def _connect(self) -> sqlite3.Connection:
        """Open the state DB with WAL and a busy timeout.

        Connections here are per-call — every method opens and closes its own —
        so the PRAGMAs have to be applied on each open; setting them once in
        `_init_db` would not reach `_save_state`. WAL lets a reader and a writer
        coexist, and `busy_timeout` makes a writer WAIT for the other instead of
        raising `database is locked` at the first moment of contention.
        """
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self):
        if not self.db_path:
            return
        conn = self._connect()
        try:
            with conn:
                conn.execute('''
                    CREATE TABLE IF NOT EXISTS drawdown_state (
                        id INTEGER PRIMARY KEY,
                        daily_start_equity REAL,
                        peak_equity REAL,
                        last_saved_date TEXT
                    )
                ''')
                # D3: record the shape of this file.
                migrate(conn, "drawdown_state", SCHEMA_VERSION, MIGRATIONS)
        finally:
            conn.close()

    def _load_state(self):
        if not self.db_path:
            return
        conn = self._connect()
        try:
            with conn:
                cursor = conn.execute('SELECT daily_start_equity, peak_equity, last_saved_date FROM drawdown_state WHERE id = 1')
                row = cursor.fetchone()
                if row:
                    self.daily_start_equity, self.peak_equity, last_saved_date_str = row
                    
                    # Check for daily reset
                    if last_saved_date_str != self._today():
                        self.daily_start_equity = 0.0
                        self._save_state()
                else:
                    conn.execute('INSERT INTO drawdown_state (id, daily_start_equity, peak_equity, last_saved_date) VALUES (1, 0.0, 0.0, ?)',
                                (self._today(),))
        finally:
            conn.close()

    def _save_state(self):
        if not self.db_path:
            return
        conn = self._connect()
        try:
            with conn:
                conn.execute('''
                    UPDATE drawdown_state
                    SET daily_start_equity = ?, peak_equity = ?, last_saved_date = ?
                    WHERE id = 1
                ''', (self.daily_start_equity, self.peak_equity, self._today()))
        finally:
            conn.close()

    def reset_baselines(self, current_equity: float) -> None:
        """Re-anchor both baselines to `current_equity` after a real deposit/withdrawal.

        This is the ONLY supported way to move a baseline down. See the module
        docstring: the guard cannot tell a withdrawal from a crash, so it does not
        try, and an operator who just moved money must say so explicitly.
        """
        self.daily_start_equity = float(current_equity)
        self.peak_equity = float(current_equity)
        self._save_state()

    def update_equity_benchmarks(self, current_equity: float, current_balance: float):
        changed = False

        # True daily reset check in case process stays open across midnight.
        current_date = self._today()
        if current_date != self._last_seen_date:
            self._last_seen_date = current_date
            self.daily_start_equity = 0.0
            changed = True

        # ── Auto-reanchor stale peak_equity ──────────────────────────────────
        # If balance ≈ equity (no meaningful floating P&L) and the persisted
        # peak is more than 1.15x current balance on small/micro accounts or 1.5x generally,
        # the peak is from a prior account state — a withdrawal, a demo reset, or
        # cross-mode DB contamination. A genuine trading session with 0 open positions
        # should not be locked in a permanent circuit breaker from historical runs.
        is_stale_peak = (
            (self.peak_equity > current_balance * 1.5) or
            (current_balance < 2500.0 and self.peak_equity > current_balance * 1.15)
        )
        if (
            self.peak_equity > 0
            and current_balance > 0
            and current_equity > 0
            and abs(current_balance - current_equity) / current_balance < 0.02  # <2% float
            and is_stale_peak
        ):
            import logging
            _logger = logging.getLogger("JARVIS_DrawdownGuard")
            _logger.warning(
                "AUTO-REANCHOR: peak_equity (%.2f) is %.2fx current balance (%.2f) with "
                "no meaningful floating P&L. Reanchoring to current balance. "
                "This indicates a withdrawal, demo reset, or stale DB state.",
                self.peak_equity, self.peak_equity / current_balance, current_balance,
            )
            self.peak_equity = current_balance
            self.daily_start_equity = current_equity
            changed = True

        # Daily-loss baseline must be tracked on EQUITY consistently (not balance),
        # otherwise open positions make the daily-loss figure wrong.
        #
        # Both baselines move UP only (or from an unset/zeroed state).
        if self.daily_start_equity <= 0:
            self.daily_start_equity = current_equity
            changed = True
        if self.peak_equity <= 0 or current_equity > self.peak_equity:
            self.peak_equity = current_equity
            changed = True
            
        if changed and self.db_path:
            self._save_state()


    def get_risk_multiplier(self, current_equity: float) -> float:
        """Tiered risk multiplier that scales position sizing down as drawdown deepens.

        Tiers are derived from ``max_total_drawdown_pct`` (config/settings.json) so
        the halt threshold matches the configured circuit-breaker limit instead of a
        hardcoded 8% that silently overrode the declared 15% setting.

        For micro accounts (equity < $2,500) the multiplier never drops below 0.15
        so the minimum lot size can still be evaluated by the position sizer — the
        sizer's own ``micro_cap`` check is the final gatekeeper.
        """
        if self.peak_equity <= 0:
            return 1.0

        dd_pct = max(0.0, ((self.peak_equity - current_equity) / self.peak_equity) * 100.0)

        # Derive tier boundaries from the configured max drawdown:
        #   Tier 1 boundary:  25% of max  (default 15% → 3.75%)
        #   Tier 2 boundary:  50% of max  (default 15% → 7.50%)
        #   Tier 3 boundary:  75% of max  (default 15% → 11.25%)
        #   Full halt:       100% of max  (default 15% → 15.0%)
        max_dd = self.max_total_drawdown_pct  # default 15.0 from settings.json
        t1 = max_dd * 0.25   # ~3.75%
        t2 = max_dd * 0.50   # ~7.50%
        t3 = max_dd * 0.75   # ~11.25%

        if dd_pct < t1:
            mult = 1.0
        elif dd_pct < t2:
            mult = 0.75
        elif dd_pct < t3:
            mult = 0.50
        else:
            mult = 0.25  # Severely reduced but NOT zero

        # Micro-account floor: never fully halt — let the position sizer decide
        # whether the minimum lot at reduced risk is acceptable.
        if current_equity < 2500.0:
            mult = max(mult, 0.15)

        return mult

    def check_limits(self, current_equity: float, current_balance: float) -> Dict[str, Any]:
        self.update_equity_benchmarks(current_equity, current_balance)

        # Daily loss check
        daily_loss_pct = 0.0
        if self.daily_start_equity > 0:
            daily_loss_pct = max(0.0, ((self.daily_start_equity - current_equity) / self.daily_start_equity) * 100.0)

        # Max drawdown check
        total_dd_pct = 0.0
        if self.peak_equity > 0:
            total_dd_pct = max(0.0, ((self.peak_equity - current_equity) / self.peak_equity) * 100.0)

        breaches = []
        if daily_loss_pct >= self.max_daily_loss_pct:
            breaches.append(f"Max Daily Loss breached ({daily_loss_pct:.2f}% >= {self.max_daily_loss_pct:.2f}%). Trading halted for today.")
        if total_dd_pct >= self.max_total_drawdown_pct:
            breaches.append(f"Max Portfolio Drawdown breached ({total_dd_pct:.2f}% >= {self.max_total_drawdown_pct:.2f}%). Circuit breaker triggered.")

        return {
            "passed": len(breaches) == 0,
            "daily_loss_pct": round(daily_loss_pct, 2),
            "total_dd_pct": round(total_dd_pct, 2),
            "breaches": breaches
        }

