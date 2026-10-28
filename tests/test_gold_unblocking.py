"""Comprehensive tests for Gold execution unblocking and micro-account min-lot sizing."""

import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock
from jarvis.risk.position_sizing import PositionSizer
from jarvis.data.symbol_registry import resolve
from jarvis.data.schemas import (
    DecisionObject,
    TradeQualityGateResult,
    MarketRegime,
    RegimeOutput,
)
from jarvis.risk.risk_engine import RiskEngine
from jarvis.risk.trade_guard import TradeGuard


def _gold_symbol_info(name: str = "GOLD.i#") -> dict:
    spec = resolve("XAUUSD")
    return {
        "name": name,
        "symbol": name,
        "canonical": "XAUUSD",
        "trade_contract_size": spec.contract_size,
        "volume_min": 0.01,
        "volume_max": 100.0,
        "volume_step": 0.01,
        "point": 0.01,
        "digits": 2,
    }


def _eur_symbol_info(name: str = "EURUSD.i#") -> dict:
    spec = resolve("EURUSD")
    return {
        "name": name,
        "symbol": name,
        "canonical": "EURUSD",
        "trade_contract_size": spec.contract_size,
        "volume_min": 0.01,
        "volume_max": 100.0,
        "volume_step": 0.01,
        "point": 0.00001,
        "digits": 5,
    }


def _make_decision(symbol: str = "XAUUSD", **kwargs) -> DecisionObject:
    regime = RegimeOutput(
        primary_regime=MarketRegime.TREND_BULL,
        probabilities={"TREND_BULL": 0.85, "RANGE": 0.15},
        confidence=0.85,
    )
    defaults = {
        "symbol": symbol,
        "timestamp": datetime.now(timezone.utc),
        "regime": regime,
        "bias": "BUY",
        "probabilities": {"buy": 0.85, "sell": 0.15},
        "strategy": "TREND_FOLLOWING",
        "entry_price": 4150.0,
        "stop_loss": 4126.0,
        "take_profit": 4198.0,
        "risk_reward_ratio": 2.0,
        "calculated_risk_percent": 1.25,
        "expected_value": 0.94,
        "expected_value_dollars": 12.0,
        "model_confidence": 0.85,
        "adversarial_penalty": 0.0,
        "invalidation_levels": ["4125.0"],
        "bull_case": ["Trend continuation"],
        "bear_case": [],
        "risk_factors": [],
        "quality_gate": TradeQualityGateResult(passed=True, checks={}),
        "decision": "EXECUTE",
        "ai_score": 85.7,
    }
    defaults.update(kwargs)
    return DecisionObject(**defaults)


class TestGoldMicroAccountSizing:
    def test_gold_min_lot_allowed_under_28_dollar_allowance(self):
        """A $507 account with a 24-point Gold stop ($24 risk, 4.73%) must be approved at 0.01 lot."""
        info = _gold_symbol_info("GOLD.i#")
        lots = PositionSizer.calculate_lot_size(
            account_balance=507.0,
            entry_price=4150.00,
            sl_price=4126.00,  # 24 point stop distance = $24.00 on 0.01 lot
            risk_pct=1.25,
            symbol_info=info,
            model_confidence=0.85,
        )
        assert lots == 0.01, f"Expected 0.01 lot, got {lots}"

    def test_gold_excessive_stop_distance_rejected(self):
        """A $507 account with a 35-point Gold stop ($35 risk, 6.90%) must be rejected (> $28 / 5.5%)."""
        info = _gold_symbol_info("GOLD.i#")
        lots = PositionSizer.calculate_lot_size(
            account_balance=507.0,
            entry_price=4150.00,
            sl_price=4115.00,  # 35 point stop distance = $35.00 on 0.01 lot
            risk_pct=1.25,
            symbol_info=info,
            model_confidence=0.85,
        )
        assert lots == 0.0, f"Expected 0.0 lots (rejection), got {lots}"

    def test_non_gold_still_enforces_12_dollar_cap(self):
        """EURUSD on a $507 account with a stop risking $15 must be rejected (> $12 / 2.5%)."""
        info = _eur_symbol_info("EURUSD.i#")
        # 150 pips stop = 0.0150 * 100,000 * 0.01 = $15.00
        lots = PositionSizer.calculate_lot_size(
            account_balance=507.0,
            entry_price=1.1000,
            sl_price=1.0850,
            risk_pct=1.25,
            symbol_info=info,
            model_confidence=0.85,
        )
        assert lots == 0.0, f"Expected non-gold to reject risk > $12, got {lots}"


class TestCalibratedEntryScoreResolution:
    def test_calibrated_decision_uses_max_ai_score(self):
        """When model_confidence is 0.43 (after penalties) but ai_score is 85.7, score_val is 0.857."""
        from jarvis.application.orchestrator import JarvisOrchestrator

        orch = MagicMock(spec=JarvisOrchestrator)
        mock_profile = MagicMock()
        mock_profile.oos_expectancy_r = 0.85
        mock_profile.oos_trades = 50
        mock_profile.expectancy_r = 0.90
        mock_profile.geometry.min_score = 0.5214
        mock_profile.regime_geometry = {}
        mock_profile.regime_edge = {}

        orch._calibrated_profile = MagicMock(return_value=mock_profile)

        decision = _make_decision(
            symbol="XAUUSD",
            decision="EXECUTE",
            bias="BUY",
            model_confidence=0.43,
            ai_score=85.7,
            expected_value=0.94,
        )

        regime = RegimeOutput(
            primary_regime=MarketRegime.TREND_BULL,
            probabilities={"TREND_BULL": 0.85, "RANGE": 0.15},
            confidence=0.85,
        )

        # Call real unbound _calibrated_entry_decision
        entry_dec = JarvisOrchestrator._calibrated_entry_decision(orch, "XAUUSD", decision, regime)
        assert entry_dec is not None
        assert entry_dec.allowed is True
        assert entry_dec.score >= 0.85


class TestTradeGuardAndRiskEngineGoldAuthorization:
    def test_trade_guard_allows_when_entry_authorized_override_is_true(self):
        account = MagicMock(equity=507.0, trade_allowed=True)
        decision = _make_decision(
            symbol="XAUUSD",
            decision="EXECUTE",
            bias="BUY",
            entry_price=4150.0,
            stop_loss=4126.0,
            take_profit=4198.0,
            model_confidence=0.85,
        )
        guard = TradeGuard()
        result = guard.validate_pre_execution(
            decision=decision,
            account=account,
            positions=[],
            entry_authorized_override=True,
        )
        assert result.get("passed") is True, f"Expected passed=True, got {result}"

    def test_risk_engine_authorizes_gold_min_lot(self):
        engine = RiskEngine()
        account = MagicMock(
            equity=507.0,
            balance=507.0,
            margin=0.0,
            margin_free=450.0,
            margin_level=900.0,
            trade_allowed=True,
        )
        decision = _make_decision(
            symbol="GOLD.i#",
            decision="EXECUTE",
            bias="BUY",
            entry_price=4150.0,
            stop_loss=4126.0,
            take_profit=4198.0,
            model_confidence=0.85,
            ai_score=85.7,
            risk_reward_ratio=2.0,
        )
        info = _gold_symbol_info("GOLD.i#")

        auth = engine.authorize_execution(
            decision=decision,
            account=account,
            positions=[],
            symbol_info=info,
            current_spread_pips=2.0,
            max_allowed_spread_pips=35.0,
            entry_authorized_override=True,
        )
        assert auth.get("authorized") is True, f"Expected authorization, got {auth}"
        assert auth.get("lots") == 0.01
