"""
HM Algo 2.0 — Autonomous Trade Quality Guard.
Executes hard independent pre-flight checks before approving any trade for execution.
"""
import math
from typing import Dict, List, Any, Optional
from datetime import datetime, timezone
from jarvis.data.schemas import (
    DecisionObject,
    AccountSnapshot,
    PositionSnapshot,
    is_observed_price,
)
from jarvis.data.symbol_registry import resolve


def _is_finite(value: Any) -> bool:
    """True only for a real, finite number.

    NaN compares False against everything, so `nan > cap` and `nan >= entry` are
    both False and a NaN sails through every threshold below. An infinity passes
    one direction of each pair. Neither is ever a tradeable order.
    """
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


class TradeGuard:
    @staticmethod
    def validate_pre_execution(
        decision: DecisionObject,
        account: AccountSnapshot,
        positions: List[PositionSnapshot],
        max_spread_pips: float = 35.0,
        current_spread_pips: float = 2.0,
        entry_authorized_override: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Validate order geometry and spread before execution.

        ``entry_authorized_override`` exists because entry selection has two
        legitimate authorities. The legacy path is the 29-check gate stack, whose
        verdict lands in ``decision.decision``. The calibrated path is
        ``jarvis.execution.entry_policy``, which deliberately trades setups the
        legacy stack rejected — so checking ``decision.decision != "EXECUTE"``
        here silently reimposed the legacy veto and blocked every calibrated
        trade. Passing an explicit verdict keeps this guard focused on what it
        actually owns: geometry, spread and account permissions.
        """
        reasons = []

        if entry_authorized_override is None:
            if decision.decision != "EXECUTE":
                reasons.append(f"AI Decision status is '{decision.decision}' (Requires 'EXECUTE').")
        elif not entry_authorized_override:
            reasons.append("Entry not authorized by the calibrated entry policy.")

        if not account.trade_allowed:
            reasons.append("Account trade permissions disabled by broker.")

        symbol_data = resolve(decision.symbol)
        
        # Fix #5: Use correct attribute name 'max_spread_pips' (not 'max_spread')
        allowed_max_spread = getattr(symbol_data, 'max_spread_pips', max_spread_pips)
        
        # Fix #10: Unified Asian session definition — 01:00 to 04:59 UTC
        # (Consistent with orchestrator.py)
        #
        # The hour must come from the BAR being evaluated, not from the wall
        # clock. Using datetime.now() made a backtest's spread allowance depend
        # on when the backtest was run — a reproducibility defect and, since the
        # allowance differs between sessions, a form of look-ahead.
        _bar_ts = getattr(getattr(decision, "context", None), "timestamp", None)
        if _bar_ts is not None and hasattr(_bar_ts, "hour"):
            # The window above is defined in UTC. Bar timestamps arrive with
            # their tzinfo intact (market_context passes them straight through)
            # and MT5 runs on the broker's clock, so an aware value has to be
            # converted — `.hour` alone would shift the window by the offset.
            if getattr(_bar_ts, "tzinfo", None) is not None:
                try:
                    _bar_ts = _bar_ts.astimezone(timezone.utc)
                except (AttributeError, TypeError, ValueError):
                    pass
            current_hour = int(_bar_ts.hour)
        else:
            current_hour = datetime.now(timezone.utc).hour
        is_asian_session = 1 <= current_hour < 5
        
        # Fix #5b: Use correct attribute for crypto detection
        is_crypto = getattr(symbol_data, 'is_crypto', False) or getattr(symbol_data, 'asset_class', '').upper() == 'CRYPTO' or 'BTC' in decision.symbol or 'ETH' in decision.symbol
        
        if is_crypto and is_asian_session:
            allowed_max_spread *= 2.0

        if not _is_finite(current_spread_pips):
            reasons.append(f"Spread is not a finite number ({current_spread_pips} pips).")
        elif current_spread_pips > allowed_max_spread:
            reasons.append(f"Spread ({current_spread_pips} pips) exceeds maximum threshold ({allowed_max_spread} pips).")

        # A price must be a real number AND strictly positive. `_is_finite(0.0)` is
        # True, so a finiteness check alone admitted a zero entry — and with entry at
        # ~0 the inverted-geometry comparisons below are all False, so a NEGATIVE stop
        # loss passed as well. Measured: an empty primary frame gives `market_context`
        # current_price = bid = 0.0 and ask = spread * pip, from which the levels
        # engine minted entry 0.02 / stop -0.04 for BTCUSD; the sizer read a risk
        # distance of 0.06 against a 65 000 instrument and returned 100 lots — 6.5M
        # USD of exposure for a 50 USD risk budget. `is_observed_price` is the same
        # predicate the producers of these numbers use, so the gate and the source
        # cannot drift apart.
        for _label, _value in (
            ("entry price", decision.entry_price),
            ("stop loss", decision.stop_loss),
            ("take profit", decision.take_profit),
        ):
            if not _is_finite(_value):
                reasons.append(f"Invalid order geometry: {_label} is not a finite number ({_value}).")
            elif not is_observed_price(_value):
                reasons.append(
                    f"Invalid order geometry: {_label} is not a positive, observed price ({_value})."
                )

        # Inverted or invalid stop loss / take profit check. The `else` matters:
        # geometry can only be judged against a direction, so a bias that is
        # neither BUY nor SELL (including a lower-cased one) used to skip both
        # checks and approve an inverted stop.
        if decision.bias == "BUY":
            if decision.stop_loss >= decision.entry_price:
                reasons.append("Invalid BUY order geometry: Stop loss is above entry price.")
            if decision.take_profit <= decision.entry_price:
                reasons.append("Invalid BUY order geometry: Take profit is below entry price.")
        elif decision.bias == "SELL":
            if decision.stop_loss <= decision.entry_price:
                reasons.append("Invalid SELL order geometry: Stop loss is below entry price.")
            if decision.take_profit >= decision.entry_price:
                reasons.append("Invalid SELL order geometry: Take profit is above entry price.")
        else:
            reasons.append(
                f"Invalid order geometry: unrecognised bias '{decision.bias}' (expected BUY or SELL)."
            )

        # Pre-Execution Asymmetric Risk:Reward Check
        rr_ratio = getattr(decision, "risk_reward_ratio", None)
        if _is_finite(rr_ratio) and rr_ratio is not None and 0.0 < float(rr_ratio) < 1.79:
            reasons.append(f"Sub-minimum Risk:Reward ratio {float(rr_ratio):.2f} (Minimum 1.80 required).")

        is_passed = len(reasons) == 0
        return {
            "passed": is_passed,
            "reasons": reasons
        }
