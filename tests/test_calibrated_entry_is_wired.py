"""AI9 — walk-forward must not certify a run it never made, and the calibrated
entry policy must be able to reach a trade.

Two halves of one finding: the optimiser's output never reaches trading.

HALF 1 — ``jarvis.backtesting.walk_forward.WalkForwardEngine``
--------------------------------------------------------------
The engine has no production caller (only ``compare_performance.py`` and a dead
import in ``tests/test_backtesting_lab.py``), so none of this was reachable.
That makes the fallback branch the most dangerous part of it: with fewer than
200 bars it ran a single in-sample backtest and returned

    walk_forward_efficiency = 1.0     passed_wfe = True

A perfect score and a pass, for a run in which no out-of-sample window was ever
measured. The one case that could not validate was the one case that certified
itself — and ``compare_performance.py`` printed it as ``PASSED``.

The same branch also named its fold key ``wfe`` where every validated fold uses
``wfe_fold``, so a consumer reading the real key hit a ``KeyError`` on exactly
the runs it most needed to treat carefully.

A pass is now also *explained*. ``passed_wfe`` was
``WFE >= 0.50 or OOS profit factor >= 1.25``, so a validation whose edge was
entirely lost (WFE 0.00) could still report ``passed_wfe: True`` on an absolute
profit factor alone, with nothing saying so.

HALF 2 — the calibrated entry policy
------------------------------------
``config/winrate_profiles.json`` holds 16 calibrated symbol profiles,
``jarvis.execution.entry_policy.evaluate_entry`` implements the policy, and
``RiskEngine.authorize_execution`` / ``TradeGuard.validate_pre_execution``
already accept ``entry_authorized_override`` for its verdict. Nothing joined
them: ``evaluate_entry`` had exactly one caller outside tests, the backtest
engine, so live entry selection still ran on the legacy 29-check stack that
``entry_policy`` itself documents as unvalidated.

The wire is added behind ``trading.use_calibrated_entry_policy``, default OFF.
That default is deliberate, not timidity: 11 of the 16 profiles on disk carry a
NEGATIVE out-of-sample expectancy, so enabling the policy refuses most of the
universe. Switching a live book from 16 symbols to 5 is a trading decision and
is left to the operator.

**Coverage honesty.** ``run_cycle_for_symbol`` cannot be driven without a live
MT5 client, and the walk-forward engine's validated path needs real H1 history,
so the fallback branch is driven with a patched ``BacktestEngine`` and the
wire is driven through the orchestrator's own helper. The guard-keying change
(``wants_entry``) is pinned by source assertion.
"""

import inspect
import re
from types import SimpleNamespace
from unittest import mock

import pandas as pd
import pytest

import jarvis.application.orchestrator as orch_mod
import jarvis.backtesting.walk_forward as wf_mod
from jarvis.backtesting.walk_forward import WalkForwardEngine
from jarvis.config.settings import SETTINGS
from jarvis.intelligence.winrate_targeting import DEFAULT_PROFILE_PATH, load_profiles


# ─────────────────────────────────────────────────────────────────────────────
# Half 1 — walk-forward
# ─────────────────────────────────────────────────────────────────────────────

class _FakeBacktestEngine:
    """Stands in for BacktestEngine so the fold logic can run without MT5."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def run_backtest(self, df, **kwargs):
        return {
            "metrics": {
                "profit_factor": 1.40,
                "sharpe_ratio": 0.80,
                "net_profit": 100.0,
            },
            "trades": [{"pnl": 10.0}, {"pnl": -5.0}, {"pnl": 4.0}],
        }


def _run_walk_forward(n_bars, num_folds=3):
    df = pd.DataFrame({"close": range(n_bars)})
    with mock.patch.object(wf_mod, "BacktestEngine", _FakeBacktestEngine):
        return WalkForwardEngine(num_folds=num_folds).run_walk_forward_validation(df, "XAUUSD")


class TestTheFallbackDoesNotCertifyItself:
    """Fewer than 200 bars: nothing was validated, so nothing passed."""

    def test_no_efficiency_is_reported(self):
        assert _run_walk_forward(100)["walk_forward_efficiency"] is None

    def test_it_does_not_pass(self):
        assert _run_walk_forward(100)["passed_wfe"] is False

    def test_it_says_it_was_not_validated(self):
        res = _run_walk_forward(100)
        assert res["validated"] is False
        assert "not validated" in res["note"]

    def test_the_note_names_the_shortfall(self):
        # A bare "not validated" would leave the reader to guess whether the
        # data, the fold count or the bar minimum was at fault.
        assert "100" in _run_walk_forward(100)["note"]

    def test_no_out_of_sample_trade_is_claimed(self):
        # The fallback runs ONE in-sample backtest. Its trades are not
        # out-of-sample, and counting them as such is what made the aggregate
        # look measured.
        assert _run_walk_forward(100)["total_oos_trades"] == 0
        assert _run_walk_forward(100)["fold_results"][0]["oos_trades_count"] == 0

    def test_the_fold_key_matches_the_validated_path(self):
        # "wfe" in the fallback vs "wfe_fold" everywhere else: a consumer
        # reading the real key raised KeyError on precisely these runs.
        res = _run_walk_forward(100)
        assert "wfe_fold" in res["fold_results"][0]
        assert "wfe" not in res["fold_results"][0]


class TestAValidatedRunStillReports:
    def test_it_is_marked_validated(self):
        res = _run_walk_forward(600)
        assert res["validated"] is True
        assert res["walk_forward_efficiency"] is not None

    def test_the_pass_is_explained(self):
        assert _run_walk_forward(600)["note"]

    def test_efficiency_and_pass_agree_in_sign(self):
        # The old code could report efficiency 0.00 alongside passed_wfe True.
        res = _run_walk_forward(600)
        if res["walk_forward_efficiency"] >= 0.50:
            assert res["passed_wfe"] is True


class TestThePassCriterionIsNamed:
    """WFE is not the only way to pass; say which one carried it."""

    def test_a_profit_factor_only_pass_says_so(self):
        # In-sample profit factor 10.0 against an out-of-sample 1.30: the edge
        # decayed to 13% of its fitted value, so retention fails badly while the
        # held-out window is still profitable in absolute terms. The old code
        # reported passed_wfe=True here with nothing to distinguish it from a
        # run that actually retained its edge.
        class _DecayingEngine(_FakeBacktestEngine):
            def run_backtest(self, df, **kwargs):
                res = super().run_backtest(df, **kwargs)
                res["metrics"] = dict(res["metrics"], profit_factor=10.0)
                return res

        oos_metrics = {
            "profit_factor": 1.30,
            "sharpe_ratio": 0.5,
            "net_profit": 50.0,
            "total_trades": 20,
        }
        df = pd.DataFrame({"close": range(600)})
        with mock.patch.object(wf_mod, "BacktestEngine", _DecayingEngine), \
             mock.patch.object(
                 wf_mod.PerformanceMetricsCalculator, "calculate_metrics",
                 return_value=oos_metrics,
             ):
            res = WalkForwardEngine(num_folds=3).run_walk_forward_validation(df, "XAUUSD")

        assert res["walk_forward_efficiency"] < 0.50
        assert res["passed_wfe"] is True
        assert "profit factor" in res["note"]
        assert "not on retention" in res["note"]


# ─────────────────────────────────────────────────────────────────────────────
# Half 2 — the calibrated entry policy
# ─────────────────────────────────────────────────────────────────────────────

def _profile(oos_exp=-0.25, oos_n=49, min_score=0.65, regime_edge=None):
    return SimpleNamespace(
        oos_expectancy_r=oos_exp,
        oos_trades=oos_n,
        geometry=SimpleNamespace(min_score=min_score),
        regime_edge=regime_edge or {},
    )


def _decision(conf=0.9):
    return SimpleNamespace(model_confidence=conf, quality_gate=SimpleNamespace(checks={}))


def _regime(name="TREND_BULL"):
    return SimpleNamespace(primary_regime=name)


@pytest.fixture
def orch(monkeypatch):
    """An orchestrator with no broker, and a profile set we control."""
    obj = orch_mod.JarvisOrchestrator.__new__(orch_mod.JarvisOrchestrator)
    obj._wr_profiles = None
    monkeypatch.setattr(SETTINGS.trading, "use_calibrated_entry_policy", True)
    return obj


def _with_profiles(mapping):
    """Patch the profile source. Returns a context manager."""
    return mock.patch.object(orch_mod, "load_profiles", return_value=mapping)


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(SETTINGS.trading, "use_calibrated_entry_policy", True)


@pytest.fixture
def disabled(monkeypatch):
    monkeypatch.setattr(SETTINGS.trading, "use_calibrated_entry_policy", False)


class TestThePolicyIsInertWhileDisabled:
    def test_no_verdict_is_issued(self, disabled):
        obj = orch_mod.JarvisOrchestrator.__new__(orch_mod.JarvisOrchestrator)
        obj._wr_profiles = None
        with mock.patch.object(orch_mod, "load_profiles", return_value={"XAUUSD": _profile()}):
            assert obj._calibrated_entry_decision("XAUUSD", _decision(), _regime()) is None

    def test_the_default_in_a_fresh_config_is_off(self):
        # If this ever flips, the live book silently narrows to the five symbols
        # with a positive out-of-sample expectancy. Assert on the *dataclass
        # default*, not the deployed config/settings.json: the deployment has
        # legitimately flipped the switch ON (2026-10 runner), and disk state
        # is not what this guard is about -- what flips silently is the
        # code-level default a fresh install would fall back to.
        from jarvis.config.settings import TradingSettings

        assert TradingSettings().use_calibrated_entry_policy is False


class TestThePolicyDecidesWhenEnabled:
    def test_a_negative_out_of_sample_edge_refuses_the_symbol(self, orch):
        with _with_profiles({"XAUUSD": _profile()}):
            dec = orch._calibrated_entry_decision("XAUUSD", _decision(), _regime())
        assert dec is not None
        assert dec.allowed is False
        assert "no validated edge" in dec.reason

    def test_a_positive_edge_above_threshold_allows(self, orch):
        with _with_profiles({"XAUUSD": _profile(oos_exp=0.22, oos_n=54, min_score=0.60)}):
            dec = orch._calibrated_entry_decision("XAUUSD", _decision(conf=0.9), _regime())
        assert dec.allowed is True

    def test_below_the_calibrated_threshold_refuses(self, orch):
        with _with_profiles({"XAUUSD": _profile(oos_exp=0.22, oos_n=54, min_score=0.95)}):
            dec = orch._calibrated_entry_decision("XAUUSD", _decision(conf=0.5), _regime())
        assert dec.allowed is False
        assert "below calibrated threshold" in dec.reason

    def test_a_disabled_regime_refuses(self, orch):
        edge = {"TREND_BULL": SimpleNamespace(enabled=False, reason="n=12, exp=-0.10R")}
        with _with_profiles({"XAUUSD": _profile(oos_exp=0.22, oos_n=54, min_score=0.2, regime_edge=edge)}):
            dec = orch._calibrated_entry_decision("XAUUSD", _decision(conf=0.9), _regime("TREND_BULL"))
        assert dec.allowed is False
        assert "disabled by learned policy" in dec.reason


class TestAnAbsentProfileIsNotAPermission:
    def test_an_uncalibrated_symbol_gets_no_verdict(self, orch):
        # None means "this policy did not decide" -- the legacy stack still
        # does. Returning an allowed decision here would turn every uncalibrated
        # symbol into a free trade.
        with _with_profiles({"EURUSD": _profile()}):
            assert orch._calibrated_entry_decision("XAUUSD", _decision(), _regime()) is None

    def test_an_empty_store_gets_no_verdict(self, orch):
        with _with_profiles({}):
            assert orch._calibrated_entry_decision("XAUUSD", _decision(), _regime()) is None

    def test_a_symbol_is_matched_on_its_canonical_name(self, orch):
        # The profile keys are canonical ("XAUUSD"); the cycle may be running
        # on a broker alias. A miss must not silently become a legacy decision.
        with _with_profiles({"XAUUSD": _profile(oos_exp=0.22, oos_n=54, min_score=0.60)}):
            dec = orch._calibrated_entry_decision("XAUUSD.m", _decision(conf=0.9), _regime())
        assert dec is not None
        assert dec.allowed is True


class TestTheStoreLoader:
    def test_a_missing_file_is_no_profiles(self):
        assert load_profiles("definitely/not/here.json") == {}

    def test_a_corrupt_file_is_no_profiles(self, tmp_path):
        # A calibration must never be able to take the engine down by being
        # unreadable: "no profiles" means the legacy stack decides, which is
        # what the platform did before the wire existed.
        bad = tmp_path / "winrate_profiles.json"
        bad.write_text("{ this is not json", encoding="utf-8")
        assert load_profiles(str(bad)) == {}

    def test_a_malformed_profile_is_no_profiles(self, tmp_path):
        # Unreadable JSON is caught inside WRProfileStore, so it never reaches
        # the loader's guard. A well-formed file whose *contents* do not parse
        # does -- and that is the case that would otherwise raise out of a
        # symbol scan and kill the cycle.
        bad = tmp_path / "winrate_profiles.json"
        bad.write_text('{"profiles": {"XAUUSD": "not-a-profile"}}', encoding="utf-8")
        assert load_profiles(str(bad)) == {}

    def test_it_points_at_the_file_the_calibrator_writes(self):
        # If the loader and tools/calibrate_winrate.py ever disagree, the live
        # engine silently calibrates on nothing while the tool reports success.
        assert DEFAULT_PROFILE_PATH.name == "winrate_profiles.json"
        src = open("tools/calibrate_winrate.py", encoding="utf-8").read()
        assert "winrate_profiles.json" in src


class TestTheWireIsConnected:
    """Source assertions on the two ends that no unit test can drive."""

    def test_the_verdict_reaches_the_risk_engine(self):
        src = inspect.getsource(orch_mod.JarvisOrchestrator.run_cycle_for_symbol)
        assert "entry_authorized_override=entry_override" in src

    def test_the_verdict_comes_from_the_policy(self):
        src = inspect.getsource(orch_mod.JarvisOrchestrator.run_cycle_for_symbol)
        assert "_calibrated_entry_decision(" in src

    def test_the_hard_guards_do_not_key_on_the_legacy_verdict(self):
        # A candidate the calibrated policy accepts can still carry
        # decision.decision == "WAIT". Keying the in-process lock, the symbol
        # limit, the cooldown and the Asian blackout on the legacy verdict would
        # skip all four for exactly the candidates the policy adds. The iSAR
        # flip helper downstream is exempt from this check by design: it
        # *closes an opposing live trade*, an aggressive act that should stay
        # keyed to the legacy EXECUTE verdict until the calibrated policy has
        # earned execution authority there too.
        src = inspect.getsource(orch_mod.JarvisOrchestrator.run_cycle_for_symbol)
        guard_block = src[src.index("wants_entry ="):src.index("Institutional Stop-and-Reverse")]
        assert "decision.decision == \"EXECUTE\"" not in guard_block

    def test_the_legacy_confidence_floor_does_not_veto_the_policy(self):
        src = inspect.getsource(orch_mod.JarvisOrchestrator.run_cycle_for_symbol)
        # The calibrated branch must be tried before the MIN_CONFIDENCE branch,
        # or the unvalidated floor vetoes the validated replacement.
        assert src.index("entry_dec is not None") < src.index("MIN_CONFIDENCE:")
