# -*- coding: utf-8 -*-
"""2026-10-20 profitability audit regression tests.

The audit: ~376 closed demo trades with expectancy -0.44R/trade, median
resolved at exactly -1.00R, 208 full stop-outs vs 52 TP exits, winners capped
at ~+0.5R. Root causes fixed here:

1. min-lot positions (0.01) can never partial-one-off, so the fast-cash step
   was a silent no-op and every micro trade rode all-or-nothing to the stop
   (evaluate_exit gains a fallback LOCK at the fast-cash level);
2. dead trades lived until the full stop (evaluate_exit gains a time stop);
3. a (symbol, strategy) pair that burns >= 20 live trades at <= -0.25R/trade
   is quarantined by the Strategy Viability gate;
4. TradingSettings gains a per-symbol daily entry cap default (4).
"""
import json
import os
import sqlite3
import unittest
from unittest.mock import patch

from jarvis.execution.exit_policy import ExitPolicy, evaluate_exit


def _policy(**overrides) -> ExitPolicy:
    base = dict(
        symbol="XAUUSD",
        be_trigger_r=2.0,
        fast_cash_r=1.0,
        fast_cash_volume_pct=0.5,
        be_buffer_pct=0.05,
        runner_trail_atr=2.6,
        trail_activation_r=2.0,
        time_stop_minutes=60.0,
        time_stop_min_r=0.10,
        digits=2,
        pip_size=0.01,
    )
    base.update(overrides)
    return ExitPolicy(**base)


class TestMinLotFallbackLock(unittest.TestCase):
    """The partial that cannot split must ratchet the stop instead."""

    def test_min_lot_buy_locks_at_fast_cash(self):
        policy = _policy()
        dec = evaluate_exit(
            side="BUY", entry=4200.0, initial_sl=4190.0, current_sl=4190.0,
            tp=4220.0, price=4212.0, favorable_dist=12.0,  # +1.2R
            atr=8.0, policy=policy, partial_executable=False,
        )
        # buffer = max(0.01*2.5, 10*0.05) = 0.5 -> lock at 4200 + (10 - 0.5) = 4209.5
        self.assertAlmostEqual(dec.new_sl, 4209.5, places=2)
        self.assertEqual(dec.partial_close_pct, 0.0, "a 0.01 lot cannot split")
        self.assertTrue(any("PARTIAL_FALLBACK_LOCK" in a for a in dec.actions))
        self.assertFalse(dec.close_position)

    def test_min_lot_sell_locks_at_fast_cash(self):
        policy = _policy()
        dec = evaluate_exit(
            side="SELL", entry=4200.0, initial_sl=4210.0, current_sl=4210.0,
            tp=4180.0, price=4188.0, favorable_dist=12.0,  # +1.2R
            atr=8.0, policy=policy, partial_executable=False,
        )
        self.assertAlmostEqual(dec.new_sl, 4190.5, places=2)
        self.assertEqual(dec.partial_close_pct, 0.0)
        self.assertTrue(any("PARTIAL_FALLBACK_LOCK" in a for a in dec.actions))

    def test_splittable_lot_still_requests_partial(self):
        policy = _policy()  # fast_cash_r=1.0
        dec = evaluate_exit(
            side="BUY", entry=4200.0, initial_sl=4190.0, current_sl=4190.0,
            tp=4220.0, price=4212.0, favorable_dist=12.0,
            atr=8.0, policy=policy, partial_executable=True,
        )
        self.assertGreater(dec.partial_close_pct, 0.0)
        self.assertAlmostEqual(dec.partial_price, 4210.0, places=2)
        self.assertFalse(any("PARTIAL_FALLBACK_LOCK" in a for a in dec.actions))

    def test_below_fast_cash_no_lock(self):
        policy = _policy()
        dec = evaluate_exit(
            side="BUY", entry=4200.0, initial_sl=4190.0, current_sl=4190.0,
            tp=4220.0, price=4204.0, favorable_dist=4.0,  # +0.4R < 1.0R
            atr=8.0, policy=policy, partial_executable=False,
        )
        self.assertAlmostEqual(dec.new_sl, 4190.0, places=2)


class TestTimeStop(unittest.TestCase):
    """Dead trades must be retired instead of riding to the full -1R stop."""

    def test_aged_dead_trade_is_closed(self):
        policy = _policy(time_stop_minutes=60.0, time_stop_min_r=0.10)
        dec = evaluate_exit(
            side="BUY", entry=4200.0, initial_sl=4190.0, current_sl=4190.0,
            tp=4220.0, price=4200.3, favorable_dist=0.3,  # +0.03R, aged
            atr=8.0, policy=policy, hold_minutes=90.0, partial_executable=False,
        )
        self.assertTrue(dec.should_close)
        self.assertIn("TIME_STOP", dec.close_reason)
        self.assertTrue(dec.changed)

    def test_young_dead_trade_is_not_closed(self):
        policy = _policy(time_stop_minutes=60.0, time_stop_min_r=0.10)
        dec = evaluate_exit(
            side="BUY", entry=4200.0, initial_sl=4190.0, current_sl=4190.0,
            tp=4220.0, price=4200.3, favorable_dist=0.3,
            atr=8.0, policy=policy, hold_minutes=10.0, partial_executable=False,
        )
        self.assertFalse(dec.should_close)

    def test_runner_is_exempt_from_time_stop(self):
        policy = _policy(time_stop_minutes=60.0, time_stop_min_r=0.10)
        dec = evaluate_exit(
            side="BUY", entry=4200.0, initial_sl=4190.0, current_sl=4200.0,
            tp=4230.0, price=4201.0, favorable_dist=9.0,
            atr=8.0, policy=policy, hold_minutes=120.0,
            partial_already_taken=True, partial_executable=False,
        )
        self.assertFalse(dec.should_close)

    def test_profitable_aged_trade_is_not_closed(self):
        policy = _policy(time_stop_minutes=60.0, time_stop_min_r=0.10)
        dec = evaluate_exit(
            side="BUY", entry=4200.0, initial_sl=4190.0, current_sl=4190.0,
            tp=4230.0, price=4215.0, favorable_dist=15.0,  # +1.5R
            atr=8.0, policy=policy, hold_minutes=200.0, partial_executable=False,
        )
        self.assertFalse(dec.should_close)

    def test_unknown_age_leaves_time_stop_inert(self):
        policy = _policy(time_stop_minutes=60.0, time_stop_min_r=0.10)
        dec = evaluate_exit(
            side="BUY", entry=4200.0, initial_sl=4190.0, current_sl=4190.0,
            tp=4220.0, price=4200.3, favorable_dist=0.3,
            atr=8.0, policy=policy, hold_minutes=None, partial_executable=False,
        )
        self.assertFalse(dec.should_close)


class TestSymbolProfileTimeStops(unittest.TestCase):
    def test_all_time_stops(self):
        gold = ExitPolicy.for_symbol("XAUUSD")
        btc = ExitPolicy.for_symbol("BTCUSD")
        us30 = ExitPolicy.for_symbol("US30")
        eurusd = ExitPolicy.for_symbol("EURUSD")
        self.assertAlmostEqual(gold.time_stop_minutes, 75.0, places=6)
        self.assertAlmostEqual(btc.time_stop_minutes, 90.0, places=6)
        self.assertAlmostEqual(us30.time_stop_minutes, 240.0, places=6)
        self.assertAlmostEqual(eurusd.time_stop_minutes, 120.0, places=6)


class TestStrategyQuarantine(unittest.TestCase):
    """A (symbol, strategy) pair burning live R must silence itself."""

    DB_ROWS = 25

    def _temp_db(self, rows_per_pair, expectancy_r=-1.0, symbol="XAUUSD",
                 strategy="TREND_PULLBACK", r_values=None):
        """Build a temp ledger. ``r_values`` (optional) overrides each row's R
        multiple in trade order (entry 4200.0 / sl 4190.0 = risk 10.0; BUY exit
        price 4200 + r*10). Rows repeat the provided list cyclically."""
        if not hasattr(self, "_db_counter"):
            self._db_counter = 0
        self._db_counter += 1
        path = os.path.join(self.temp_dir.name, f"quarantine_test_{self._db_counter}.db")
        conn = sqlite3.connect(path)
        conn.execute(
            """
            CREATE TABLE executed_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ticket INTEGER, position_id INTEGER,
                symbol TEXT, action TEXT, entry_price REAL, sl REAL, tp REAL, volume REAL,
                timestamp TEXT, ai_score REAL, regime TEXT, expected_value REAL,
                exit_price REAL, realized_pnl REAL, executor TEXT DEFAULT 'BOT (AI)',
                session_name TEXT DEFAULT 'UNKNOWN', is_prime_session INTEGER DEFAULT 1,
                adx REAL DEFAULT 0.0, plus_di REAL DEFAULT 0.0, minus_di REAL DEFAULT 0.0,
                spread_pips REAL DEFAULT 0.0, mtf_alignment TEXT DEFAULT '',
                threats_json TEXT DEFAULT '[]', features_json TEXT DEFAULT '{}',
                closed_at TEXT, origin TEXT, execution_mode TEXT, ticket_open REAL
            )
            """
        )
        for i in range(rows_per_pair):
            r_i = -1.0
            if r_values is not None:
                r_i = float(r_values[i % len(r_values)])
            exit_px = 4200.0 + r_i * 10.0
            conn.execute(
                """
                INSERT INTO executed_trades (ticket, symbol, action, entry_price, sl, tp,
                    volume, timestamp, exit_price, realized_pnl, features_json,
                    closed_at, execution_mode, origin)
                VALUES (?, 'XAUUSD', 'BUY', 4200.0, 4190.0, 4220.0, 0.01,
                        '2026-10-20T09:15:27+00:00', ?, ?, ?, 'x', 'demo', 'broker')
                """.replace("'XAUUSD'", "?"),
                (960000 + i, symbol, exit_px, -r_i * 10.0, json.dumps({"strategy": strategy})),
            )
        conn.commit()
        conn.close()
        return path

    def setUp(self):
        import tempfile

        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        from jarvis.execution.strategy_quarantine import clear_cache
        clear_cache()
        self.temp_dir.cleanup()

    def test_burning_pair_is_quarantined(self):
        from jarvis.execution.strategy_quarantine import is_quarantined

        path = self._temp_db(25)
        self.count_checks = 0
        self.assertTrue(is_quarantined("XAUUSD", "TREND_PULLBACK", db_path=path))

    def test_unknown_strategy_fails_open(self):
        from jarvis.execution.strategy_quarantine import is_quarantined

        path = self._temp_db(25)
        self.assertFalse(is_quarantined("XAUUSD", "CHOCH_STRUCTURAL_REVERSAL", db_path=path))
        self.assertFalse(is_quarantined("XAUUSD", "?", db_path=path))

    def test_thin_sample_is_not_quarantined(self):
        from jarvis.execution.strategy_quarantine import is_quarantined

        path = self._temp_db(5)
        self.assertFalse(is_quarantined("XAUUSD", "TREND_PULLBACK", db_path=path))

    def test_other_symbol_same_stats_not_quarantined(self):
        from jarvis.execution.strategy_quarantine import is_quarantined

        path = self._temp_db(25, symbol="BTCUSD")
        self.assertTrue(is_quarantined("BTCUSD", "TREND_PULLBACK", db_path=path))
        self.assertFalse(is_quarantined("XAUUSD", "TREND_PULLBACK", db_path=path))

    def test_report_shape(self):
        from jarvis.execution.strategy_quarantine import quarantine_report

        path = self._temp_db(25)
        report = quarantine_report("XAUUSD", db_path=path)
        entry = report.get("XAUUSD|TREND_PULLBACK")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["quarantined"], True)
        self.assertEqual(entry["n"], 25)

    def test_graduation_when_recent_cohort_recovers(self):
        from jarvis.execution.strategy_quarantine import (
            is_quarantined, GRADUATION_RECENT_N,
        )

        # 17 burned rows, then a fresh 8-trade cohort at +0.6R: aggregate is
        # still deeply negative but the freshest sample says the edge is back.
        rs = [-1.0] * 17 + [0.6] * GRADUATION_RECENT_N
        path = self._temp_db(17 + GRADUATION_RECENT_N, r_values=rs)
        self.assertFalse(is_quarantined("XAUUSD", "TREND_PULLBACK", db_path=path))

    def test_graduation_boundary_requires_strict_recovery(self):
        from jarvis.execution.strategy_quarantine import (
            is_quarantined, GRADUATION_RECENT_N, GRADUATION_RECENT_MIN_R,
        )

        # Recent cohort at exactly the graduation floor (>=) graduates;
        # just under it does not.
        at_floor = [GRADUATION_RECENT_MIN_R] * GRADUATION_RECENT_N
        path = self._temp_db(12 + GRADUATION_RECENT_N,
                             r_values=[-1.0] * 12 + at_floor)
        self.assertFalse(is_quarantined("XAUUSD", "TREND_PULLBACK", db_path=path))

        below = [0.02] * GRADUATION_RECENT_N
        path = self._temp_db(12 + GRADUATION_RECENT_N,
                             r_values=[-1.0] * 12 + below)
        self.assertTrue(is_quarantined("XAUUSD", "TREND_PULLBACK", db_path=path))

    def test_recent_window_is_a_sliding_average(self):
        """Graduation looks at the last GRADUATION_RECENT_N closes, so a 6-win
        streak inside an 8-trade window that also holds 2 older losers already
        clears the floor: a +0.3R average over the freshest cohort is real
        evidence. A weak cohort (+0.2R wins still losing on average over the
        window) must NOT graduate."""
        from jarvis.execution.strategy_quarantine import is_quarantined

        path = self._temp_db(22 + 6, r_values=[-1.0] * 22 + [0.8] * 6)
        # last-8 avg = (6*0.8 + 2*(-1.0)) / 8 = +0.35 >= +0.05 → graduates
        self.assertFalse(is_quarantined("XAUUSD", "TREND_PULLBACK", db_path=path))

        path = self._temp_db(22 + 6, r_values=[-1.0] * 22 + [0.2] * 6)
        # last-8 avg = (6*0.2 + 2*(-1.0)) / 8 = -0.1 < +0.05 → stays locked
        self.assertTrue(is_quarantined("XAUUSD", "TREND_PULLBACK", db_path=path))

    def test_expectancy_state_shape(self):
        from jarvis.execution.strategy_quarantine import expectancy_state

        rs = [-1.0] * 17 + [0.6] * 8
        path = self._temp_db(25, r_values=rs)
        state = expectancy_state("XAUUSD", "TREND_PULLBACK", db_path=path)
        self.assertEqual(state["n"], 25)
        self.assertAlmostEqual(state["expectancy_r"], -0.488, places=3)
        self.assertEqual(state["recent_n"], 8)
        self.assertAlmostEqual(state["recent_expectancy_r"], 0.6, places=6)
        self.assertFalse(state["quarantined"])
        self.assertTrue(state["graduated"])
        self.assertEqual(state["size_multiplier"], 0.80)  # negative aggregate haircut

        empty = expectancy_state("BTCUSD", "TREND_PULLBACK", db_path=path)
        self.assertEqual(empty["n"], 0)
        self.assertIsNone(empty["expectancy_r"])
        self.assertEqual(empty["size_multiplier"], 1.0)

    def test_expectancy_risk_multiplier_map(self):
        from jarvis.execution.strategy_quarantine import expectancy_risk_multiplier

        # Thin sample: no learning.
        self.assertEqual(
            expectancy_risk_multiplier("XAUUSD", "TREND_PULLBACK",
                                       db_path=self._temp_db(5)),
            1.0,
        )
        # Positive live edge earns the boost.
        self.assertEqual(
            expectancy_risk_multiplier("XAUUSD", "TREND_PULLBACK",
                                       db_path=self._temp_db(20, r_values=[0.5] * 20)),
            1.15,
        )
        # Mild negative earns the haircut (below quarantine floor of n>=20
        # at <= -0.25R: 15 trades at -0.4R is a haircut, not a ban).
        self.assertEqual(
            expectancy_risk_multiplier("XAUUSD", "TREND_PULLBACK",
                                       db_path=self._temp_db(15, r_values=[-0.4] * 15)),
            0.80,
        )
        # Definitely condemned and not graduated: zero risk budget.
        self.assertEqual(
            expectancy_risk_multiplier("XAUUSD", "TREND_PULLBACK",
                                       db_path=self._temp_db(25)),
            0.0,
        )
        # Unknown strategy bucket never learns anything.
        self.assertEqual(
            expectancy_risk_multiplier("XAUUSD", "?",
                                       db_path=self._temp_db(25)),
            1.0,
        )

    def test_multiplier_is_offline_inert(self):
        """In offline/backtest contexts the learned sizing must be neutral."""
        from unittest.mock import patch

        from jarvis.execution.strategy_quarantine import (
            expectancy_risk_multiplier, is_quarantined,
        )

        path = self._temp_db(25)
        with patch("jarvis.config.runtime.is_offline", return_value=True):
            self.assertEqual(
                expectancy_risk_multiplier("XAUUSD", "TREND_PULLBACK", db_path=path),
                1.0,
            )
            # Quarantine stays offline-inert as well (hermetic sims): the
            # offline gate fires before the ledger read even happens.
            self.assertFalse(
                is_quarantined("XAUUSD", "TREND_PULLBACK", db_path=path)
            )


class TestLearnedSizingInRiskEngine(unittest.TestCase):
    """The learned expectancy multiplier must reach the real sizing hot path."""

    def setUp(self):
        from datetime import datetime, timezone

        from jarvis.risk.risk_engine import RiskEngine
        from jarvis.data.schemas import (
            AccountSnapshot, DecisionObject, RegimeOutput, MarketRegime,
            TradeQualityGateResult,
        )

        self.engine = RiskEngine(
            max_daily_loss_pct=4.0,
            max_drawdown_pct=10.0,
            max_open_positions=2,
            max_symbol_positions=1,
            max_risk_per_trade_pct=0.5,
            is_backtest=True,
        )
        self.decision = DecisionObject(
            symbol="XAUUSD",
            timestamp=datetime.now(timezone.utc),
            regime=RegimeOutput(primary_regime=MarketRegime.TREND_BULL,
                                probabilities={"TREND_BULL": 0.8}, confidence=0.85),
            bias="BUY",
            probabilities={"buy": 0.75, "sell": 0.15, "no_trade": 0.10},
            strategy="TREND_PULLBACK",
            entry_price=2400.0,
            stop_loss=2390.0,
            take_profit=2425.0,
            risk_reward_ratio=2.5,
            calculated_risk_percent=0.5,
            expected_value=45.0,
            model_confidence=0.85,
            adversarial_penalty=8.0,
            invalidation_levels=["Close below 2390.0"],
            bull_case=["H1 Bullish structure"],
            bear_case=[],
            risk_factors=["Minor volatility"],
            quality_gate=TradeQualityGateResult(passed=True, checks={"Regime": True}),
            decision="EXECUTE",
            execution_authorized=True,
        )
        self.account = AccountSnapshot(
            login=12345, server="XM-Live", balance=10000.0, equity=10000.0,
            margin=0.0, free_margin=10000.0, margin_level=0.0, leverage=100,
        )
        self.sym_info = {"trade_contract_size": 100, "volume_min": 0.01,
                         "volume_max": 100.0, "volume_step": 0.01}

    def _capture_lot_size(self):
        captured = {}
        real_calc = self.engine.position_sizer.calculate_lot_size

        def spy(*args, **kwargs):
            captured["risk_pct"] = kwargs.get("risk_pct")
            return real_calc(*args, **kwargs)

        self.engine.position_sizer.calculate_lot_size = spy
        return captured

    def test_multiplier_reaches_base_risk(self):
        from unittest.mock import patch

        captured = self._capture_lot_size()
        with patch("jarvis.execution.strategy_quarantine.expectancy_risk_multiplier",
                   return_value=0.80):
            res = self.engine.authorize_execution(self.decision, self.account, [], self.sym_info)

        self.assertTrue(res["authorized"], res.get("reasons"))
        self.assertEqual(captured["risk_pct"], 0.5 * 0.80)

    def test_multiplier_boost_reaches_base_risk(self):
        from unittest.mock import patch

        captured = self._capture_lot_size()
        with patch("jarvis.execution.strategy_quarantine.expectancy_risk_multiplier",
                   return_value=1.15):
            res = self.engine.authorize_execution(self.decision, self.account, [], self.sym_info)

        self.assertTrue(res["authorized"], res.get("reasons"))
        self.assertEqual(captured["risk_pct"], 0.5 * 1.15)

    def test_neutral_multiplier_leaves_risk_untouched(self):
        from unittest.mock import patch

        captured = self._capture_lot_size()
        with patch("jarvis.execution.strategy_quarantine.expectancy_risk_multiplier",
                   return_value=1.0):
            res = self.engine.authorize_execution(self.decision, self.account, [], self.sym_info)

        self.assertTrue(res["authorized"], res.get("reasons"))
        self.assertEqual(captured["risk_pct"], 0.5)


class TestDailyEntryCapDefault(unittest.TestCase):
    def test_default_cap_is_four(self):
        from jarvis.config.settings import TradingSettings

        self.assertEqual(TradingSettings().max_daily_entries_per_symbol, 4)


class TestCancelledOrderGhostStamp(unittest.TestCase):
    """A cancelled, never-filled order's journal row must not stay open.

    The 2026-10-20 ledger carried gold SELL row #1016 (order 968492503,
    broker state CANCELLED, zero deals) open forever because the
    "really not filled?" probe used `history_deals_get(from, to,
    position=...)` — on this MT5 build that signature ignores `position=`
    and returned deals for every OTHER position, so every ghost looked
    filled and was never stamped.
    """

    def setUp(self):
        import tempfile
        from jarvis.data.database import SQLiteTradeDB

        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = SQLiteTradeDB(db_path=self.path)

    def tearDown(self):
        try:
            self.db._get_conn().close()
        except Exception:
            pass
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + suffix)
            except OSError:
                pass

    def _fake_mt5(self, ghost_deals, filled_deals):
        from types import SimpleNamespace

        # The stamping loop sits behind `if not deals: return`, so the window
        # scan must see at least one (unrelated) deal for the loop to run.
        decoy = SimpleNamespace(
            symbol="EURUSD", position_id=55555, entry=0, type=0, price=1.1000,
            volume=0.10, profit=0.0, swap=0.0, commission=0.0,
            time=1792501000, magic=1, comment="",
        )

        def deals_get(*args, **kwargs):
            position = kwargs.get("position")
            if position is not None:
                # position-only form: MUST be honoured (the formerly-buggy
                # call also passed dates and this fake ignores them, so a
                # regression back to the date+position signature would fail
                # this test by returning the wrong bucket).
                return list(ghost_deals) if int(position) == 98001 else list(filled_deals)
            # date-window form used by the main deal scan
            return [decoy] + list(ghost_deals) + list(filled_deals)

        cancelled_order = SimpleNamespace(ticket=98001, state=2, time_done=1792501084)
        filled_order = SimpleNamespace(ticket=98002, state=2, time_done=1792501100)
        return SimpleNamespace(
            terminal_info=lambda: SimpleNamespace(connected=True),
            initialize=lambda **kw: True,
            history_deals_get=deals_get,
            history_orders_get=lambda *a, **k: [cancelled_order, filled_order],
        )

    def _sync(self, ghost_deals, filled_deals):
        fake = self._fake_mt5(ghost_deals, filled_deals)
        with patch("jarvis.data.database.broker_utc_offset", return_value=0):
            self.db.sync_mt5_history(days=1, mt5_module=fake)

    def test_cancelled_never_filled_row_is_stamped_closed(self):
        self.db.log_trade(
            ticket=98001, position_id=None, symbol="XAUUSD", action="SELL",
            entry=4131.63, sl=4136.0, tp=4120.0, volume=0.01,
            score=51.0, regime="TREND_BEAR", ev=None, origin="broker",
        )
        self._sync(ghost_deals=[], filled_deals=[])

        cur = self.db._get_conn().cursor()
        cur.execute(
            "SELECT closed_at, realized_pnl FROM executed_trades WHERE ticket = 98001"
        )
        closed_at, pnl = cur.fetchone()
        self.assertIsNotNone(closed_at, "a cancelled order with no deals is dead")
        self.assertIsNone(pnl, "an unfilled order has no realised P&L")

    def test_a_row_whose_position_traded_is_never_stamped(self):
        from types import SimpleNamespace

        self.db.log_trade(
            ticket=98002, position_id=98002, symbol="XAUUSD", action="BUY",
            entry=4131.63, sl=4127.6, tp=4140.0, volume=0.01,
            score=51.0, regime="TREND_BULL", ev=None, origin="broker",
        )
        exit_deal = SimpleNamespace(
            ticket=5, position_id=98002, entry=1, type=1, price=4133.0,
            volume=0.01, profit=1.37, swap=0.0, commission=0.0,
            time=1792501900, magic=1, comment="", symbol="XAUUSD",
        )
        self._sync(ghost_deals=[], filled_deals=[exit_deal])

        cur = self.db._get_conn().cursor()
        cur.execute(
            "SELECT closed_at, realized_pnl FROM executed_trades WHERE ticket = 98002"
        )
        closed_at, pnl = cur.fetchone()
        self.assertIsNotNone(closed_at)
        self.assertIsNotNone(pnl, "the exit deal's P&L must land")

    def test_probe_failure_keeps_row_unstamped(self):
        self.db.log_trade(
            ticket=98001, position_id=None, symbol="XAUUSD", action="SELL",
            entry=4131.63, sl=4136.0, tp=4120.0, volume=0.01,
            score=51.0, regime="TREND_BEAR", ev=None, origin="broker",
        )
        fake = self._fake_mt5(ghost_deals=[], filled_deals=[])

        def broken_deals(*args, **kwargs):
            if kwargs.get("position") is not None:
                raise RuntimeError("terminal busy")
            return []

        fake.history_deals_get = broken_deals
        with patch("jarvis.data.database.broker_utc_offset", return_value=0):
            self.db.sync_mt5_history(days=1, mt5_module=fake)

        cur = self.db._get_conn().cursor()
        cur.execute(
            "SELECT closed_at FROM executed_trades WHERE ticket = 98001"
        )
        self.assertIsNone(
            cur.fetchone()[0],
            "a probe we could not trust must not authorize stamping",
        )


if __name__ == "__main__":
    unittest.main()
