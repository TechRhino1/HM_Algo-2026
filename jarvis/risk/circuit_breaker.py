"""
HM Algo 2.0 — Circuit Breaker & Safety Lockout Engine.
Halts trading during consecutive execution failures, rapid loss streaks, or platform anomalies.
"""
import time
import sqlite3
import os
from typing import Dict, Any, Optional

from jarvis.config.paths import resolve_db_path
from jarvis.data.schema_version import migrate

# D3: 1 = `circuit_state` as it exists today.
SCHEMA_VERSION = 1


def _migration_1(conn: sqlite3.Connection) -> None:
    """Version 1 *is* the shape created by the CREATE TABLE above.

    A file written before versioning existed already has that shape, so there is
    nothing to add — but it is recorded as a named step rather than left to
    `ensure_version`, which stamped whatever number the code declared. A
    hand-bumped SCHEMA_VERSION with no matching step now stops the migration and
    says so, instead of labelling a file with a version whose shape nobody made.
    """
    return None


MIGRATIONS = {1: _migration_1}


class CircuitBreaker:
    def __init__(self, db_path: str = "jarvis_circuit_state.db", clock=None, reset_on_boot: Optional[bool] = None):
        # Injectable clock. Live trading uses the real wall clock; a BACKTEST must
        # advance on BAR TIME (``BacktestEngine`` sets this to the current bar's
        # epoch seconds each bar). Without it, a 45-minute symbol pause is
        # measured against real CPU seconds, so whether it has expired depends
        # on how fast the machine is -- which made backtest trade counts vary
        # run to run (USDJPY 53 vs 63, XAUUSD 97 vs 84 on identical inputs).
        self._now = clock if clock is not None else time.time
        # Anchored on the repo data dir — a CWD-relative path let the breaker
        # "forget" it had tripped when launched from a different directory.
        db_path = resolve_db_path(db_path)
        # Hermetic backtesting: run in memory and persist nothing. Otherwise a
        # backtest that trips the breaker (or accumulates symbol/regime pauses)
        # leaves that state behind, and the NEXT run starts already tripped --
        # silently taking fewer trades. Measured symptom: two identical runs
        # produced different trade counts (NAS100 107 vs 118) and different
        # results (-0.037R vs -0.063R). "" is the documented in-memory sentinel.
        try:
            from jarvis.config.runtime import is_offline

            if is_offline():
                db_path = ""
        except Exception:
            pass
        self.enabled = True
        self.consecutive_losses = 0
        self.is_tripped = False
        self.tripped_timestamp = 0.0
        self.trip_reason = ""
        self.symbol_losses: Dict[str, int] = {}
        self.regime_losses: Dict[str, int] = {}
        self.symbol_paused_until: Dict[str, float] = {}
        self.regime_paused_until: Dict[str, float] = {}
        self.db_path = db_path
        if self.db_path:
            self._init_db()
            self._load_state()

        # If reset_on_boot requested explicitly or via env var JARVIS_RESET_CIRCUIT_BREAKER=1
        _do_reset = reset_on_boot if reset_on_boot is not None else (
            os.environ.get("JARVIS_RESET_CIRCUIT_BREAKER", "0").lower() in ("1", "true", "yes")
        )
        if _do_reset:
            self.reset()

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
                    CREATE TABLE IF NOT EXISTS circuit_state (
                        id INTEGER PRIMARY KEY,
                        consecutive_losses INTEGER,
                        is_tripped INTEGER,
                        tripped_timestamp REAL,
                        trip_reason TEXT
                    )
                ''')
                # D3: record the shape of this file.
                migrate(conn, "circuit_state", SCHEMA_VERSION, MIGRATIONS)
        finally:
            conn.close()

    def _load_state(self):
        if not self.db_path:
            return
        conn = self._connect()
        try:
            with conn:
                cursor = conn.execute('SELECT consecutive_losses, is_tripped, tripped_timestamp, trip_reason FROM circuit_state WHERE id = 1')
                row = cursor.fetchone()
                if row:
                    self.consecutive_losses, is_tripped_int, self.tripped_timestamp, self.trip_reason = row
                    self.is_tripped = bool(is_tripped_int)
                else:
                    conn.execute('INSERT INTO circuit_state (id, consecutive_losses, is_tripped, tripped_timestamp, trip_reason) VALUES (1, 0, 0, 0.0, "")')
        finally:
            conn.close()

    def _save_state(self):
        if not self.db_path:
            return
        conn = self._connect()
        try:
            with conn:
                conn.execute('''
                    UPDATE circuit_state
                    SET consecutive_losses = ?, is_tripped = ?, tripped_timestamp = ?, trip_reason = ?
                    WHERE id = 1
                ''', (self.consecutive_losses, int(self.is_tripped), self.tripped_timestamp, self.trip_reason))
        finally:
            conn.close()

    def enable(self):
        self.enabled = True

    def disable(self):
        self.enabled = False

    def set_clock(self, clock) -> None:
        """Inject the time source (backtests pass bar time; live passes None -> wall clock)."""
        self._now = clock if clock is not None else time.time

    def record_trade_result(self, is_win: bool, symbol: str = "", regime: str = ""):
        now = self._now()
        if is_win:
            self.consecutive_losses = 0
            if symbol and symbol in self.symbol_losses:
                self.symbol_losses[symbol] = 0
            if regime and regime in self.regime_losses:
                self.regime_losses[regime] = 0
            self._save_state()
        else:
            self.consecutive_losses += 1
            if symbol:
                self.symbol_losses[symbol] = self.symbol_losses.get(symbol, 0) + 1
                if self.symbol_losses[symbol] >= 2:
                    # Pause this specific asset for 45 minutes rather than taking whole bot flat
                    self.symbol_paused_until[symbol] = now + 2700.0

            if regime:
                self.regime_losses[regime] = self.regime_losses.get(regime, 0) + 1
                if self.regime_losses[regime] >= 3:
                    self.regime_paused_until[regime] = now + 3600.0

            if self.consecutive_losses >= 3:
                self.trip(f"{self.consecutive_losses} consecutive loss limit reached across portfolio.")
            else:
                self._save_state()

    def is_symbol_paused(self, symbol: str) -> bool:
        if not self.enabled:
            return False
        pause_until = self.symbol_paused_until.get(symbol, 0.0)
        return self._now() < pause_until

    def is_regime_paused(self, regime: str) -> bool:
        if not self.enabled:
            return False
        pause_until = self.regime_paused_until.get(regime, 0.0)
        return self._now() < pause_until

    def trip(self, reason: str):
        self.is_tripped = True
        self.tripped_timestamp = self._now()
        self.trip_reason = reason
        self._save_state()

    def reset(self):
        self.is_tripped = False
        self.consecutive_losses = 0
        self.symbol_losses.clear()
        self.regime_losses.clear()
        self.symbol_paused_until.clear()
        self.regime_paused_until.clear()
        self.trip_reason = ""
        self._save_state()
        
    def _get_adaptive_cooldown(self) -> float:
        if self.consecutive_losses >= 7:
            return 28800.0  # 8 hours
        elif self.consecutive_losses >= 5:
            return 7200.0   # 2 hours
        else:
            return 1800.0   # 30 min

    def check_status(self) -> Dict[str, Any]:
        if not self.enabled:
            return {"active": False, "reason": ""}
            
        if self.is_tripped:
            elapsed = self._now() - self.tripped_timestamp
            cooldown_seconds = self._get_adaptive_cooldown()
            
            if elapsed >= cooldown_seconds:
                self.reset()
                return {"active": False, "reason": ""}
            return {
                "active": True,
                "reason": self.trip_reason,
                "remaining_cooldown_sec": round(cooldown_seconds - elapsed, 0)
            }
        return {"active": False, "reason": ""}
