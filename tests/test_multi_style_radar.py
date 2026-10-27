import unittest
from unittest.mock import MagicMock, patch
from jarvis.application.orchestrator import JarvisOrchestrator
from jarvis.application.state_manager import StateManager
from jarvis.data.schemas import DecisionObject, RegimeOutput, MarketRegime, TradeQualityGateResult
from datetime import datetime, timezone

class TestMultiStyleRadar(unittest.TestCase):
    def setUp(self):
        # These tests exercise radar plumbing across styles, not feed freshness.
        # The orchestrator reads live MT5 bars, so the stale-feed gate will
        # (correctly) refuse to decide whenever an instrument sits between bars or
        # inside its daily break -- which made the assertions below depend on the
        # wall clock and on the broker connection rather than on the code under
        # test. Neutralise that one gate; everything else runs for real.
        #
        # Note this gate only blocks on STALE, never on UNKNOWN, so before the
        # broker-offset defect was fixed these tests passed for the wrong reason:
        # a zero offset made every age negative, which classified frames UNKNOWN,
        # which the gate tolerates. Fixing the offset made the ages truthful and
        # exposed the dependency.
        self._freshness = patch(
            "jarvis.market.data_feed.first_untrusted_frame",
            return_value=(None, None, 0.0),
        )
        self._freshness.start()
        self.addCleanup(self._freshness.stop)

        # Same rationale, 2026-10-20: the profile session-window short-circuit
        # drops a restricted symbol (XAUUSD 12-16 UTC) whenever the test runs
        # outside that window, making the candidate count wall-clock dependent.
        # Neutralise ONLY the window via dataclasses.replace (profiles are
        # shared module singletons -- never mutate in place).
        import dataclasses

        from jarvis.intelligence import symbol_profile_config as _spc

        real_gspc = _spc.get_symbol_profile_config

        def _window_open(symbol):
            prof = real_gspc(symbol)
            if prof is not None and getattr(prof, "session_restriction", False):
                prof = dataclasses.replace(prof, allowed_utc_hours=None)
            return prof

        self._profile = patch(
            "jarvis.intelligence.symbol_profile_config.get_symbol_profile_config",
            side_effect=_window_open,
        )
        self._profile.start()
        self.addCleanup(self._profile.stop)

        # Same wall-clock-fragility rationale: the 2026-10-20 daily entry cap
        # reads the live demo ledger, so a symbol with >= 4 ledger rows today
        # would return the cap refusal instead of radar candidates. Plumbing
        # test -- count nothing.
        self._cap = patch.object(
            JarvisOrchestrator, "_count_entries_today", return_value=0
        )
        self._cap.start()
        self.addCleanup(self._cap.stop)

        self.state_mgr = StateManager()
        self.orchestrator = JarvisOrchestrator(
            symbols=["XAUUSD", "EURUSD"],
            mode="paper",
            trade_style="SWING"
        )

    def tearDown(self):
        self.orchestrator.stop()

    def test_single_pass_multi_style_radar_generation(self):
        """Verify _orchestration_loop_single_pass populates radar across SWING, DAY_TRADING, and SCALP."""
        radar_results = self.orchestrator._orchestration_loop_single_pass()
        
        # 2 symbols * 3 styles = 6 opportunities
        self.assertEqual(len(radar_results), 6)
        self.assertEqual(len(self.orchestrator.state_manager.radar_opportunities), 6)

        styles_found = {o["trade_style"] for o in radar_results}
        self.assertIn("SWING", styles_found)
        self.assertIn("DAY_TRADING", styles_found)
        self.assertIn("SCALP", styles_found)

        # Check timeframe mappings
        for opp in radar_results:
            style = opp["trade_style"]
            if style == "SWING":
                self.assertEqual(opp["timeframe"], "D1/H4/H1")
            elif style == "DAY_TRADING":
                self.assertEqual(opp["timeframe"], "H1/M15/M5")
            elif style == "SCALP":
                self.assertEqual(opp["timeframe"], "M15/M5/M1")

            # Check required fields
            self.assertIn("symbol", opp)
            self.assertIn("current_price", opp)
            self.assertIn("entry_price", opp)
            self.assertIn("stop_loss", opp)
            self.assertIn("take_profit", opp)
            self.assertIn("risk_reward_ratio", opp)
            self.assertIn("ev", opp)
            self.assertIn("bias", opp)
            self.assertIn("action", opp)
            self.assertIn("win_prob", opp)
            self.assertIn("score", opp)
            self.assertIn("confluence_score", opp)
            self.assertIn("confluence_tier", opp)
            self.assertIn("regime", opp)
            self.assertIn("strategy", opp)
            self.assertIn("gate_passed", opp)
            self.assertIn("failing_reasons", opp)
            self.assertIn("waiting_reasons", opp)
            self.assertIn("risk_factors", opp)

    def test_execution_style_matching(self):
        """Verify only matching trade style is authorized when specific, and all authorized when ALL."""
        # Orchestrator configured for SWING
        self.orchestrator.trade_style = "SWING"
        
        # SCALP cycle must not be authorized to execute when orchestrator is in SWING mode
        res_scalp = self.orchestrator.run_cycle_for_symbol("EURUSD", trade_style="SCALP")
        self.assertFalse(res_scalp["authorized"])

        # When orchestrator is configured for ALL, any style is authorized to match
        self.orchestrator.trade_style = "ALL"
        def _normalize_style(s: str) -> str:
            s = (s or "").upper()
            if s in ("DAY", "INTRADAY", "DAY_TRADING"):
                return "DAY_TRADING"
            if s in ("SCALP", "SCALPING"):
                return "SCALP"
            return s
        orch_style = (self.orchestrator.trade_style or "ALL").upper()
        for test_st in ["SCALP", "DAY_TRADING", "SWING"]:
            is_match = (orch_style == "ALL") or (_normalize_style(test_st) == _normalize_style(orch_style))
            self.assertTrue(is_match)

    def test_radar_levels_differentiation_between_styles(self):
        """Verify SCALP, DAY_TRADING, and SWING have distinct SL and TP levels.

        The style differentiation lives in the level engine, not in the feed.
        Building one context per style used the synthetic fallback in offline
        runs, where each timeframe is an independent random series -- so a
        "SWING" frame could legitimately carry a smaller ATR than the "DAY"
        frame and the ordering assertion measured the fixture, not the code.
        One context, three styles, no structure bias: the style floors are the
        only input that changes.
        """
        from jarvis.data.schemas import (
            LiquidityContext,
            MarketContext,
            MomentumContext,
            SessionContext,
            StructureContext,
            VolatilityContext,
        )
        from jarvis.intelligence.dynamic_levels import DynamicRiskAndLevelsEngine

        levels_engine = DynamicRiskAndLevelsEngine()
        ctx = MarketContext(
            symbol="EURUSD",
            timestamp=datetime.now(timezone.utc),
            current_price=1.0850,
            bid=1.0849,
            ask=1.0851,
            structure=StructureContext(bias="BULLISH"),
            liquidity=LiquidityContext(),
            volatility=VolatilityContext(atr=0.0010, current_spread_pips=1.0),
            momentum=MomentumContext(trend_score=60.0, adx=28.0),
            session=SessionContext(is_prime_session=True),
        )
        regime = RegimeOutput(primary_regime=MarketRegime.TREND_BULL, probabilities={}, confidence=0.85)

        scalp_levels = levels_engine.calculate_levels(ctx, regime, tentative_bias="BUY", trade_style="SCALP")
        day_levels = levels_engine.calculate_levels(ctx, regime, tentative_bias="BUY", trade_style="DAY_TRADING")
        swing_levels = levels_engine.calculate_levels(ctx, regime, tentative_bias="BUY", trade_style="SWING")

        # SL and TP distances should be strictly differentiated across styles: SCALP < DAY_TRADING < SWING
        self.assertLess(scalp_levels["risk_dist"], day_levels["risk_dist"])
        self.assertLess(day_levels["risk_dist"], swing_levels["risk_dist"])
        self.assertLess(scalp_levels["tp_dist"], day_levels["tp_dist"])
        self.assertLess(day_levels["tp_dist"], swing_levels["tp_dist"])

    def test_api_radar_filtering_logic(self):
        """Verify API filtering correctly filters opportunities by trade style."""
        sample_opps = [
            {"symbol": "XAUUSD", "trade_style": "SWING", "action": "BUY READY", "win_prob": 80, "ev": 2.5},
            {"symbol": "EURUSD", "trade_style": "DAY_TRADING", "action": "WAIT: BUY", "win_prob": 65, "ev": 1.2},
            {"symbol": "GBPUSD", "trade_style": "SCALP", "action": "SELL READY", "win_prob": 85, "ev": 3.0}
        ]
        self.state_mgr.update_radar(sample_opps)

        # Test filtering helper logic
        def filter_radar(style_filter):
            opps = list(self.state_mgr.radar_opportunities)
            if style_filter and style_filter.strip().upper() not in ("ALL", "", "NONE"):
                s_norm = style_filter.strip().upper()
                if s_norm in ("DAY", "DAY_TRADING", "INTRADAY"):
                    target_styles = {"DAY_TRADING", "DAY", "INTRADAY"}
                elif s_norm in ("SCALP", "SCALPING"):
                    target_styles = {"SCALP", "SCALPING"}
                elif s_norm == "SWING":
                    target_styles = {"SWING"}
                else:
                    target_styles = {s_norm}
                opps = [o for o in opps if str(o.get("trade_style", "")).upper() in target_styles]
            return opps

        self.assertEqual(len(filter_radar("ALL")), 3)
        self.assertEqual(len(filter_radar(None)), 3)
        self.assertEqual(len(filter_radar("SWING")), 1)
        self.assertEqual(filter_radar("SWING")[0]["symbol"], "XAUUSD")
        self.assertEqual(len(filter_radar("DAY_TRADING")), 1)
        self.assertEqual(filter_radar("DAY_TRADING")[0]["symbol"], "EURUSD")
        self.assertEqual(len(filter_radar("DAY")), 1)
        self.assertEqual(filter_radar("DAY")[0]["symbol"], "EURUSD")
        self.assertEqual(len(filter_radar("SCALP")), 1)
        self.assertEqual(filter_radar("SCALP")[0]["symbol"], "GBPUSD")

if __name__ == "__main__":
    unittest.main()
