"""Risk-sizing invariants: configured maximum risk is a hard ceiling."""
from jarvis.risk.position_sizing import PositionSizer


def _info():
    return {
        "name": "EURUSD",
        "volume_min": 0.01,
        "volume_max": 100.0,
        "volume_step": 0.01,
        "trade_tick_value": 1.0,
        "trade_tick_size": 1.0,
    }


def test_minimum_broker_volume_cannot_exceed_configured_risk():
    lots = PositionSizer.calculate_lot_size(
        account_balance=100.0,
        entry_price=100.0,
        sl_price=0.0,
        risk_pct=0.5,
        symbol_info=_info(),
    )
    assert lots == 0.0


def test_volume_quantisation_rounds_down():
    lots = PositionSizer.calculate_lot_size(
        account_balance=100.0,
        entry_price=100.0,
        sl_price=99.09,
        risk_pct=0.5,
        symbol_info=_info(),
    )
    assert lots == 0.54
