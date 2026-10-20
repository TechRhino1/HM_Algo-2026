"""
HM Algo 2.0 -- Real-Time Adaptive Feedback Optimizer.
Queries recent closed trade PnLs from SQLite to adjust win probability, score, and R:R deltas dynamically.
"""
from typing import Dict, Optional
import time
import threading
import logging

from jarvis.config.paths import resolve_db_path

logger = logging.getLogger("JARVIS_RealtimeOptimizer")

class RealtimeOptimizer:
    def __init__(self, db_path: str = "jarvis_history.db", cache_ttl: float = 60.0):
        # Anchored on the repo data dir — see jarvis.config.paths.
        db_path = resolve_db_path(db_path)
        self.db_path = db_path
        self.cache_ttl = cache_ttl
        self._cache: Dict[str, tuple] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _query_pnls(db_path: str, where_sql: str, params: tuple) -> list:
        """Runs a SELECT realized_pnl query, guaranteeing the connection is
        always closed even if the query raises (prevents fd/connection leaks)."""
        import sqlite3
        conn = sqlite3.connect(db_path, timeout=5.0)
        try:
            cur = conn.cursor()
            cur.execute(
                f"SELECT realized_pnl FROM executed_trades WHERE realized_pnl != 0 AND {where_sql} "
                f"ORDER BY id DESC LIMIT 30",
                params
            )
            return cur.fetchall()
        finally:
            conn.close()

    def get_adjustments(self, symbol: str, regime: str = "GLOBAL") -> Dict[str, float]:
        # In a backtest the optimiser must be inert. Reading the live trade DB
        # here let *today's* realised results change gate thresholds for bars
        # dated months ago, and made repeated runs disagree with each other.
        from jarvis.config.runtime import is_offline

        if is_offline():
            return {"win_p_delta": 0.0, "score_delta": 0.0, "rr_delta": 0.0}

        key = f"{symbol}_{regime}"
        now = time.time()
        with self._lock:
            if key in self._cache:
                ts, val = self._cache[key]
                if now - ts < self.cache_ttl:
                    return val
        adj = {"win_p_delta": 0.0, "score_delta": 0.0, "rr_delta": 0.0}
        try:
            rows = self._query_pnls(self.db_path, "symbol=? AND regime=?", (symbol, regime))
            if len(rows) < 10:
                rows = self._query_pnls(self.db_path, "symbol=?", (symbol,))
                if len(rows) < 10:
                    with self._lock:
                        self._cache[key] = (now, adj)
                    return adj
            wins = sum(1 for r in rows if r[0] is not None and float(r[0]) > 0)
            win_rate = wins / len(rows)
            if win_rate < 0.50:
                adj = {"win_p_delta": 0.0, "score_delta": 0.0, "rr_delta": 0.0}
            elif win_rate < 0.55:
                adj = {"win_p_delta": 0.0, "score_delta": 0.0, "rr_delta": 0.0}
            elif win_rate > 0.68:
                adj = {"win_p_delta": -0.02, "score_delta": -2.0, "rr_delta": -0.05}
            elif win_rate > 0.62:
                adj = {"win_p_delta": -0.01, "score_delta": -1.0, "rr_delta": 0.0}
            with self._lock:
                self._cache[key] = (now, adj)
            return adj
        except Exception as e:
            logger.debug(f"RealtimeOptimizer DB failed ({symbol}): {e}")
            with self._lock:
                self._cache[key] = (now, adj)
            return adj

    def get_recent_win_rate(self, min_samples: int = 10, limit: int = 30) -> Optional[float]:
        """Win rate over the last ``limit`` closed trades, across all symbols.

        Evidence supplier for the gate policy's SOFTEN path: the line that fed it
        ``recent_win_rate=None`` made SOFTEN dead code (the policy could only
        BLOCK, identical to no policy at all). Fewer than ``min_samples`` closed
        trades returns None, which the policy treats as "no evidence" and blocks.

        Backtests must not read the live DB, so offline mode returns None and the
        BacktestEngine rebinds ``DecisionEngine.recent_win_rate_fn`` to a supplier
        over its own replayed trades.
        """
        from jarvis.config.runtime import is_offline

        if is_offline():
            return None
        key = "__recent_win_rate__"
        now = time.time()
        with self._lock:
            if key in self._cache:
                ts, val = self._cache[key]
                if now - ts < self.cache_ttl:
                    return val
        rate: Optional[float] = None
        try:
            import sqlite3
            conn = sqlite3.connect(self.db_path, timeout=5.0)
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT realized_pnl FROM executed_trades "
                    "WHERE realized_pnl IS NOT NULL AND realized_pnl != 0 "
                    f"ORDER BY id DESC LIMIT {int(limit)}"
                )
                rows = cur.fetchall()
            finally:
                conn.close()
            if len(rows) >= min_samples:
                wins = sum(1 for r in rows if r[0] is not None and float(r[0]) > 0)
                rate = wins / len(rows)
        except Exception as e:
            logger.debug(f"RealtimeOptimizer win-rate query failed: {e}")
            rate = None
        with self._lock:
            self._cache[key] = (now, rate)
        return rate
