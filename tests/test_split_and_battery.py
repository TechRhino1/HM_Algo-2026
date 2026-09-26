"""Unit tests for split guard, state snapshot, and 7-question decision battery."""
import pytest
import time
from jarvis.intelligence.split_guard import (
    ALLOWED_QUESTIONS,
    SplitViolation,
    assert_split_respected,
    deterministic_responsibilities,
    probabilistic_responsibilities,
)
from jarvis.intelligence.state_snapshot import (
    InventoryState,
    approx_token_count,
    build_state_snapshot,
)
from jarvis.intelligence.decision_battery import (
    MockDecisionBatteryClient,
    build_battery_questions,
    run_decision_battery,
    validate_battery_answers,
)


def test_split_guard_allows_valid_battery():
    questions = build_battery_questions()
    assert_split_respected(questions)
    assert len(questions) == 7


def test_split_guard_rejects_arithmetic():
    bad_questions = {
        "regime": {
            "type": "choice",
            "instructions": "Please calculate the exact average of moving average",
        }
    }
    with pytest.raises(SplitViolation):
        assert_split_respected(bad_questions)


def test_split_guard_rejects_unallowed_question_id():
    bad_questions = {
        "should_i_buy_now": {
            "type": "choice",
            "instructions": "Tell me whether to buy or sell",
        }
    }
    with pytest.raises(SplitViolation):
        assert_split_respected(bad_questions)


def test_state_snapshot_stays_under_400_tokens():
    now = time.time()
    inv = InventoryState(equity_usd=1000.0, high_water_mark_usd=1000.0, inventory=0.5, entry_price=2600.0)
    prices = [(now - i * 60, 2600.0 + (i % 3)) for i in range(40)]
    trades = [(now - i * 5, "buy" if i % 2 == 0 else "sell") for i in range(10)]

    snapshot = build_state_snapshot(
        as_of=now,
        mid=2602.5,
        microprice=2602.4,
        spread_bps=1.5,
        bid_depth=[(2602.0, 5.0), (2601.5, 10.0), (2601.0, 15.0)],
        ask_depth=[(2603.0, 4.0), (2603.5, 8.0), (2604.0, 12.0)],
        trade_prices=prices,
        trade_sides=trades,
        inv=inv,
        data_timestamp=now,
        has_depth=True,
    )

    assert "mid" in snapshot
    assert "spread_bps" in snapshot
    assert "imbalance" in snapshot
    assert "drawdown_pct" in snapshot
    tokens = approx_token_count(snapshot)
    assert tokens < 400


def test_decision_battery_mock_evaluation():
    client = MockDecisionBatteryClient(seed=123)
    now = time.time()
    inv = InventoryState(equity_usd=1000.0, high_water_mark_usd=1000.0)
    snapshot = build_state_snapshot(
        as_of=now,
        mid=100.0,
        microprice=100.0,
        spread_bps=2.0,
        bid_depth=[(99.9, 10.0)],
        ask_depth=[(100.1, 5.0)],
        trade_prices=[(now - 10, 99.8), (now, 100.0)],
        trade_sides=[(now - 5, "buy")],
        inv=inv,
        data_timestamp=now,
    )

    answers, meta = run_decision_battery(client, snapshot, timeout=2.0)
    assert "regime" in answers
    assert "direction" in answers
    assert "toxic_flow" in answers
    assert "liquidity_stressed" in answers
    assert "quote_environment" in answers
    assert "inventory_pressure" in answers
    assert "execution_health" in answers

    assert meta["route"] == "MOCK_BATTERY"
    assert meta["latency_ms"] >= 0.0
