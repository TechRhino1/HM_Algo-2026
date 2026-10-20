"""2026-10-21 master-autonomy round: book discipline.

Two evidence-led disciplines from the ledger (31% WR, median -1.00R):

1. BRIDGE PROTECTION — the milestone ladder only engages at +1.00R, but the
   death zone sits below it (trades peaking ~+0.6..0.9R still die at the full
   -1R stop). evaluate_exit now caps the loss at a small fixed level once the
   PEAK favourable excursion reaches 0.60R. It never locks profit, so it
   cannot choke a future runner (the historic BE-at-1R mistake).
2. UNSPLITTABLE-BOOK RR FLOOR — when the expected post-shrink volume cannot
   reach 2x the broker minimum, the book is all-or-nothing: no partial can be
   banked, so sub-2R geometry cannot compound. Dispatch requires RR >= 2.0
   for those books.
"""
import unittest

from jarvis.execution.exit_policy import ExitPolicy, evaluate_exit
from jarvis.data.symbol_registry import resolve as resolve_spec


def _policy(**kw):
    return ExitPolicy(symbol="TEST", **kw)


def _run_exit_buy(entry, initial_sl, price, peak_dist, tp, current_sl=None, **kw):
    return evaluate_exit(
        side="BUY",
        entry=entry,
        initial_sl=initial_sl,
        current_sl=current_sl if current_sl is not None else initial_sl,
        tp=tp,
        price=price,
        favorable_dist=max(0.0, price - entry),
        atr=0.6,
        policy=kw.pop("policy"),
        peak_favorable_dist=peak_dist,
        **kw,
    )


class TestBridgeProtection(unittest.TestCase):
    def test_buy_peak_0_7r_cap(self):
        # Peak reached +0.7R (>= 0.60R activate), price collapsed back to entry:
        # the stop must ratchet to entry - 0.25R instead of leaving the full
        # -1R disaster zone exposed.
        pol = _policy()
        dec = _run_exit_buy(4200.0, 4190.0, 4200.0, peak_dist=7.0, tp=4235.0, policy=pol)
        self.assertIn("BRIDGE_CAP_-0.25R", dec.actions)
        self.assertEqual(dec.new_sl, 4197.5)

    def test_buy_below_activate_no_cap(self):
        pol = _policy()
        dec = _run_exit_buy(4200.0, 4190.0, 4200.0, peak_dist=5.0, tp=4235.0, policy=pol)
        self.assertNotIn("BRIDGE_CAP_-0.25R", dec.actions)
        self.assertEqual(dec.new_sl, 4190.0)

    def test_runner_at_its_peak_is_untouched(self):
        # Canonical-policy invariant: a trade sitting AT its peak (0.8R, no
        # retrace yet) must not get any stop ratchet — the bridge only arms
        # when the peak is being given back.
        pol = _policy()
        dec = evaluate_exit(
            side="BUY",
            entry=2400.0,
            initial_sl=2390.0,
            current_sl=2390.0,
            tp=2440.0,
            price=2408.0,
            favorable_dist=8.0,
            atr=0.6,
            policy=pol,
            peak_favorable_dist=8.0,
        )
        self.assertNotIn("BRIDGE_CAP_-0.25R", dec.actions)
        self.assertEqual(dec.new_sl, 2390.0)

    def test_cap_never_loosens_a_better_stop(self):
        pol = _policy()
        dec = _run_exit_buy(
            4200.0, 4190.0, 4200.0, peak_dist=7.0, tp=4235.0,
            current_sl=4199.0, policy=pol,
        )
        self.assertNotIn("BRIDGE_CAP_-0.25R", dec.actions)
        self.assertEqual(dec.new_sl, 4199.0)

    def test_sell_symmetric(self):
        pol = _policy()
        dec = evaluate_exit(
            side="SELL",
            entry=4200.0,
            initial_sl=4210.0,
            current_sl=4210.0,
            tp=4170.0,
            price=4200.0,
            favorable_dist=max(0.0, 4200.0 - 4200.0),
            atr=0.6,
            policy=pol,
            peak_favorable_dist=8.0,
        )
        self.assertIn("BRIDGE_CAP_-0.25R", dec.actions)
        self.assertEqual(dec.new_sl, 4202.5)


class TestUnsplittableBookGate(unittest.TestCase):
    """_is_unsplittable_book: expected post-shrink volume vs 2x broker minimum."""

    def setUp(self):
        from jarvis.application.orchestrator import JarvisOrchestrator

        self.orchestrator = JarvisOrchestrator(
            symbols=["XAUUSD"], mode="paper", trade_style="SWING"
        )
        self.orchestrator.risk_engine.max_risk_per_trade_pct = 0.5  # percent

    def tearDown(self):
        self.orchestrator.stop()

    def test_gold_micro_book_unsplittable(self):
        # $2k @ 0.5% = $10 risk budget; gold stop 40 pips * $10/pip-lot:
        # ideal 0.025 lots, expected (x0.55) 0.0138 < 0.02 -> all-or-nothing.
        spec = resolve_spec("XAUUSD")
        self.assertTrue(
            self.orchestrator._is_unsplittable_book(
                entry_price=4200.0, stop_loss=4196.0,
                equity=2000.0, spec=spec, volume_min=0.01,
            )
        )

    def test_fx_majors_still_split(self):
        # EURUSD 12-pip stop: ideal ~0.083 lots -> expected ~0.046 >= 0.02.
        spec = resolve_spec("EURUSD")
        self.assertFalse(
            self.orchestrator._is_unsplittable_book(
                entry_price=1.1000, stop_loss=1.0988,
                equity=2000.0, spec=spec, volume_min=0.01,
            )
        )

    def test_tiny_equity_marks_everything_unsplittable(self):
        # <$250 micro tier (1.5%): budget $3.75 -> EURUSD ideal 0.031,
        # expected ~0.017 < 0.02 -> all-or-nothing even on majors.
        spec = resolve_spec("EURUSD")
        self.assertTrue(
            self.orchestrator._is_unsplittable_book(
                entry_price=1.1000, stop_loss=1.0988,
                equity=200.0, spec=spec, volume_min=0.01,
            )
        )

    def test_degenerate_inputs_inert(self):
        spec = resolve_spec("XAUUSD")
        self.assertFalse(
            self.orchestrator._is_unsplittable_book(
                entry_price=4200.0, stop_loss=4200.0,
                equity=2000.0, spec=spec, volume_min=0.01,
            )
        )
        self.assertFalse(
            self.orchestrator._is_unsplittable_book(
                entry_price=4200.0, stop_loss=4196.0,
                equity=0.0, spec=spec, volume_min=0.01,
            )
        )


if __name__ == "__main__":
    unittest.main()
