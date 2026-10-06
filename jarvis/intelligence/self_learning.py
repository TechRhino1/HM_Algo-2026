import logging
import sqlite3
import time
import threading

from jarvis.config.paths import resolve_db_path

logger = logging.getLogger('JARVIS_SelfLearning')

class SelfLearningEngine:
    def __init__(self, db_path='jarvis_history.db', cache_ttl_sec: float = 15.0):
        # Anchored on the repo data dir — see jarvis.config.paths.
        db_path = resolve_db_path(db_path)
        self.db_path = db_path
        self.cache_ttl_sec = cache_ttl_sec
        self._cache = {}
        self._lock = threading.Lock()

    def get_regime_multiplier(self, regime: str, lookback: int = 50) -> float:
        # Hermetic in backtests: reading realised-trade statistics from the live
        # DB made historical simulations depend on unrelated live results.
        from jarvis.config.runtime import is_offline

        if is_offline():
            return 1.0

        now = time.time()
        cache_key = f"{regime}_{lookback}"
        with self._lock:
            if cache_key in self._cache:
                ts, val = self._cache[cache_key]
                if now - ts < self.cache_ttl_sec:
                    return val

        conn = None
        try:
            conn = sqlite3.connect(self.db_path, timeout=5.0)
            conn.execute("PRAGMA journal_mode=WAL")
            cur = conn.cursor()
            # AI5: learn from the OUTCOME, read explicitly.
            #
            # This used to read `expected_value`, which is the FORECAST — and
            # which the close path overwrote with realised P&L, so on closed rows
            # it held the outcome anyway. That conflation is exec-summary #4: the
            # column meant "forecast" for open rows and "outcome" for closed ones,
            # and this engine could not tell which it was averaging. Reading
            # `realized_pnl` on CLOSED rows only makes the intent explicit and the
            # sample honest: an open trade has no outcome to learn from.
            cur.execute(
                "SELECT realized_pnl FROM executed_trades "
                "WHERE regime=? AND closed_at IS NOT NULL AND closed_at <> '' "
                "ORDER BY id DESC LIMIT ?",
                (regime, lookback),
            )
            rows = [r[0] for r in cur.fetchall() if r[0] is not None]

            if len(rows) < 5:
                res = 1.0 # Not enough data
            else:
                avg_ev = sum(rows) / len(rows)
                if avg_ev > 0.5:
                    res = 1.10 # Boost
                elif avg_ev < 0:
                    res = 0.95 # Mild penalty (was 0.90 which caused death spiral with buggy history)
                else:
                    res = 1.0

            with self._lock:
                self._cache[cache_key] = (now, res)
            return res
        except Exception as e:
            logger.warning(f"Self-learning DB read failed: {e}")
            return 1.0
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    def get_pattern_win_rate_and_ev(
        self,
        symbol: str,
        regime: str,
        session_name: str = "LONDON",
        is_prime: bool = True,
        lookback: int = 60
    ) -> dict:
        """
        Queries historical closed trades with similar market conditions (Symbol + Regime + Session)
        to yield empirical win rate, average EV, and sample size for evidence-based decision calibration.
        """
        # Hermetic in backtests: return the neutral prior instead of live history.
        from jarvis.config.runtime import is_offline

        if is_offline():
            return {
                "sample_size": 0,
                "avg_ev": 0.0,
                "win_rate": 0.50,
                "conviction_multiplier": 1.0,
                "empirical_edge": False,
            }

        now = time.time()
        cache_key = f"pattern_{symbol}_{regime}_{session_name}_{int(is_prime)}_{lookback}"
        with self._lock:
            if cache_key in self._cache:
                ts, val = self._cache[cache_key]
                if now - ts < self.cache_ttl_sec:
                    return val

        conn = None
        try:
            conn = sqlite3.connect(self.db_path, timeout=5.0)
            cur = conn.cursor()
            # AI5: `win_rate` was derived from `expected_value > 0` — the share of
            # rows with a POSITIVE FORECAST, which is not a win rate at all. It
            # only looked like one because the close path overwrote the forecast
            # with realised P&L. Both are now read from the columns that actually
            # hold them: outcomes from `realized_pnl` on closed rows, and the
            # forecast average from `expected_value` where one was recorded
            # (reconstructed broker rows have none, and say so with NULL).
            cur.execute("""
                SELECT expected_value, realized_pnl, closed_at
                FROM executed_trades
                WHERE symbol=? AND regime=?
                ORDER BY id DESC LIMIT ?
            """, (symbol, regime, lookback))
            rows = cur.fetchall()

            if not rows or len(rows) < 3:
                res = {
                    "sample_size": len(rows) if rows else 0,
                    "avg_ev": 0.0,
                    "win_rate": 0.50,
                    "conviction_multiplier": 1.0,
                    "empirical_edge": False
                }
            else:
                forecasts = [r[0] for r in rows if r[0] is not None]
                outcomes = [r[1] for r in rows
                            if r[2] is not None and r[2] != "" and r[1] is not None]

                # Averaged over the rows that actually carry a forecast, not over
                # every row fetched — otherwise a growing number of reconstructed
                # rows would quietly drag this towards zero.
                avg_ev = (sum(forecasts) / len(forecasts)) if forecasts else 0.0
                win_rate = (sum(1 for o in outcomes if o > 0) / len(outcomes)) if outcomes else 0.50

                # Conviction multiplier between 0.8x and 1.25x based on empirical pattern history
                if win_rate >= 0.65 and avg_ev >= 1.0:
                    conviction_mult = 1.25
                elif win_rate >= 0.55:
                    conviction_mult = 1.10
                elif win_rate <= 0.35 or avg_ev < 0:
                    conviction_mult = 0.95  # Mild penalty (was 0.80 which caused feedback loop)
                else:
                    conviction_mult = 1.0

                res = {
                    "sample_size": len(rows),
                    # `sample_size` is rows fetched; these are the rows the two
                    # statistics were ACTUALLY computed from. They differ: a
                    # reconstructed broker row has an outcome but no forecast, and
                    # an open row has a forecast but no outcome. Reporting only
                    # `sample_size` made "3 rows, no outcomes" read as a measured
                    # 50% win rate.
                    "outcome_sample_size": len(outcomes),
                    "forecast_sample_size": len(forecasts),
                    "avg_ev": round(avg_ev, 2),
                    "win_rate": round(win_rate, 2),
                    "conviction_multiplier": conviction_mult,
                    "empirical_edge": avg_ev > 0.5 and win_rate >= 0.55
                }

            with self._lock:
                self._cache[cache_key] = (now, res)
            return res
        except Exception as e:
            logger.warning(f"Self-learning pattern read failed: {e}")
            return {
                "sample_size": 0,
                "avg_ev": 0.0,
                "win_rate": 0.50,
                "conviction_multiplier": 1.0,
                "empirical_edge": False
            }
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
