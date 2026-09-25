"""
HM Algo 2.0 — Canonical Exit Policy.

SINGLE SOURCE OF TRUTH for every trade-exit decision in the system.

WHY THIS MODULE EXISTS
----------------------
Historically the exit rules were re-implemented in at least three places with
divergent constants and semantics:

  * ``jarvis/backtesting/engine.py``      -> be_trigger_r, fast_cash_r, runner_trail_atr
  * ``jarvis/execution/position_monitor.py`` -> STAGE1/2/3 + STD ATR triggers
  * ``jarvis/execution/order_manager.py``    -> its own duplicate of the same stages

Because they drifted apart, the backtest could never reproduce live behaviour, and
parameters computed upstream (e.g. ``runner_trail_distance_atr`` from the entry
engine) were written into the trade plan but never read by the exit logic.

This module centralises the arithmetic. Both the backtester and the live position
monitor import :func:`evaluate_exit` and therefore cannot disagree.

DESIGN CONTRACT
---------------
``evaluate_exit`` is a *pure* function: given the current trade state and the
observed bar/price, it returns the new stop-loss and (optionally) a partial-close
instruction. It performs no I/O and mutates nothing, which makes it trivially
testable and safe to call from either an event loop or a live monitoring thread.

All thresholds are expressed in R-multiples (multiples of the initial risk
distance ``|entry - initial_sl|``) because R is the only scale-free unit that is
comparable across gold, FX, indices and crypto. ATR is used solely for the
*trailing distance*, where a volatility-relative width is genuinely desirable.

UNIT DISCIPLINE
---------------
``risk_dist`` is a PRICE DISTANCE, not pips. Any function receiving ``profit_dist``
must receive a price distance too. Mixing these was the source of a prior defect
where an ATR multiple was compared against a pip count.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional


# ─────────────────────────────────────────────────────────────────────────────
# Fallback defaults — used only when a symbol profile cannot be resolved.
# Kept deliberately conservative (wide stop protection, late breakeven) because
# premature breakeven was the single largest source of lost profit historically.
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_BE_TRIGGER_R = 2.0        # move SL to entry only after +2R
DEFAULT_FAST_CASH_R = 0.75        # bank the first partial at +0.75R
DEFAULT_FAST_CASH_VOLUME_PCT = 0.50
DEFAULT_BE_BUFFER_PCT = 0.05      # buffer above/below entry when locking BE
DEFAULT_RUNNER_TRAIL_ATR = 1.2    # trail width in ATR once running
DEFAULT_TRAIL_ACTIVATION_R = 2.0  # only start trailing beyond +2R


@dataclass
class ExitPolicy:
    """Resolved, symbol-specific exit parameters expressed in R-multiples."""

    symbol: str = "UNKNOWN"
    be_trigger_r: float = DEFAULT_BE_TRIGGER_R
    fast_cash_r: float = DEFAULT_FAST_CASH_R
    fast_cash_volume_pct: float = DEFAULT_FAST_CASH_VOLUME_PCT
    be_buffer_pct: float = DEFAULT_BE_BUFFER_PCT
    runner_trail_atr: float = DEFAULT_RUNNER_TRAIL_ATR
    trail_activation_r: float = DEFAULT_TRAIL_ACTIVATION_R
    # Milestone ratchets: (favorable R threshold, R level to lock in)
    milestones: List[tuple] = field(
        default_factory=lambda: [(2.0, 1.0), (3.0, 2.0), (5.0, 3.5)]
    )
    digits: int = 5
    pip_size: float = 0.0001

    # ── Construction ────────────────────────────────────────────────────────
    @classmethod
    def for_symbol(cls, symbol: str, spec: Any = None) -> "ExitPolicy":
        """Build a policy from the central symbol-profile matrix.

        Falls back to conservative defaults when the profile is unavailable, so the
        engine degrades gracefully instead of raising inside a monitoring loop.
        """
        digits = int(getattr(spec, "digits", 5) or 5)
        pip_size = float(getattr(spec, "pip_size", 0.0001) or 0.0001)

        try:
            from jarvis.intelligence.symbol_profile_config import get_symbol_profile_config

            cfg = get_symbol_profile_config(symbol)
            return cls(
                symbol=str(symbol),
                be_trigger_r=float(getattr(cfg, "be_trigger_r", DEFAULT_BE_TRIGGER_R)),
                fast_cash_r=float(getattr(cfg, "fast_cash_r", DEFAULT_FAST_CASH_R)),
                fast_cash_volume_pct=float(
                    getattr(cfg, "fast_cash_volume_pct", DEFAULT_FAST_CASH_VOLUME_PCT)
                ),
                be_buffer_pct=float(getattr(cfg, "be_buffer_pct", DEFAULT_BE_BUFFER_PCT)),
                runner_trail_atr=float(
                    getattr(cfg, "runner_trail_atr", DEFAULT_RUNNER_TRAIL_ATR)
                ),
                trail_activation_r=float(
                    getattr(cfg, "trail_activation_r", DEFAULT_TRAIL_ACTIVATION_R)
                ),
                digits=int(getattr(cfg, "digits", digits) or digits),
                pip_size=float(getattr(cfg, "pip_size", pip_size) or pip_size),
            )
        except Exception:
            return cls(symbol=str(symbol), digits=digits, pip_size=pip_size)

    def buffer_distance(self, risk_dist: float) -> float:
        """Breakeven buffer as a price distance, floored to cover spread noise."""
        return max(self.pip_size * 2.5, abs(risk_dist) * self.be_buffer_pct)


@dataclass
class ExitDecision:
    """Result of evaluating an exit policy against the current market state."""

    new_sl: float = 0.0
    partial_close_pct: float = 0.0
    partial_price: float = 0.0
    be_locked: bool = False
    trail_active: bool = False
    close_position: bool = False
    close_reason: str = ""
    close_volume: Optional[float] = None
    actions: List[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.close_position or self.partial_close_pct > 0.0 or bool(self.actions)

    @property
    def should_close(self) -> bool:
        """True when a full discretionary market exit is signalled."""
        return self.close_position

    @property
    def partial_due(self) -> bool:
        """True when a partial scale-out is signalled but not yet executed."""
        return self.partial_close_pct > 0.0


def evaluate_exit(
    *,
    side: str,
    entry: float,
    initial_sl: float,
    current_sl: float,
    tp: float,
    price: float,
    favorable_dist: float,
    atr: float,
    policy: ExitPolicy,
    partial_already_taken: bool = False,
    be_already_locked: bool = False,
    struct_level: Optional[float] = None,
) -> ExitDecision:
    """Compute the stop-loss / partial-profit action for an open trade.

    Parameters
    ----------
    side : "BUY" or "SELL"
    entry, initial_sl : the ORIGINAL entry and stop, which define 1R.
    current_sl : the stop currently resting on the position.
    price : the current (exit-side) price used for trailing arithmetic.
    favorable_dist : best favourable excursion so far, in PRICE units.
    atr : current ATR in PRICE units (used for trail width only).
    struct_level : optional structural support (BUY) / resistance (SELL) to ratchet behind.

    Returns
    -------
    ExitDecision with the new stop and any partial-close instruction.
    """
    dec = ExitDecision(new_sl=current_sl)
    is_buy = str(side).upper().startswith("BUY")

    # 1R is the absolute distance from entry to the ORIGINAL stop. If that is
    # degenerate we cannot reason in R at all, so leave the position untouched
    # rather than manufacturing a spurious stop.
    risk_dist = abs(entry - initial_sl)
    if risk_dist <= 0 or not (price > 0):
        return dec

    r_multiple = favorable_dist / risk_dist
    buffer_dist = policy.buffer_distance(risk_dist)
    digits = policy.digits

    # ── 1. Partial profit banking (risk reduction) ──────────────────────────
    # Take a partial BEFORE moving to breakeven so the position is de-risked
    # while still giving the remainder room to run.
    if not partial_already_taken and r_multiple >= policy.fast_cash_r:
        dec.partial_close_pct = policy.fast_cash_volume_pct
        dec.partial_price = entry + (risk_dist * policy.fast_cash_r) * (1 if is_buy else -1)
        dec.partial_price = round(dec.partial_price, digits)
        dec.actions.append(f"PARTIAL_{policy.fast_cash_r:.2f}R")

    # ── 2. Breakeven lock ───────────────────────────────────────────────────
    # Deferred until the trade has proven itself. Locking at 1R was the primary
    # cause of +20R winners being closed at +0.9R.
    #
    # A pending partial also unlocks breakeven, because taking risk off the table
    # is itself the proof the trade is working. NOTE: the caller owns the
    # "partial already taken" flag; if it cannot actually split the lots it must
    # still set `partial_already_taken=True` on subsequent calls, otherwise this
    # function will keep reporting the partial as due.
    be_triggered = be_already_locked or partial_already_taken or dec.partial_close_pct > 0.0
    if not be_triggered and r_multiple >= policy.be_trigger_r:
        be_triggered = True
        dec.actions.append(f"BE_LOCK_{policy.be_trigger_r:.2f}R")

    if be_triggered:
        dec.be_locked = True
        floor = round(entry + buffer_dist, digits) if is_buy else round(entry - buffer_dist, digits)
        # A locked breakeven must never loosen an already-better stop.
        if is_buy and floor > dec.new_sl:
            dec.new_sl = floor
        elif (not is_buy) and (dec.new_sl == 0 or floor < dec.new_sl):
            dec.new_sl = floor

    # ── 3. Milestone ratchet (progressive profit locking) ───────────────────
    for threshold_r, lock_r in policy.milestones:
        if r_multiple >= threshold_r:
            lock_price = round(
                entry + (risk_dist * lock_r) * (1 if is_buy else -1), digits
            )
            if is_buy and lock_price > dec.new_sl:
                dec.new_sl = lock_price
                dec.actions.append(f"MILESTONE_{lock_r:.1f}R")
            elif (not is_buy) and (dec.new_sl == 0 or lock_price < dec.new_sl):
                dec.new_sl = lock_price
                dec.actions.append(f"MILESTONE_{lock_r:.1f}R")

    # ── 4. ATR runner trail ─────────────────────────────────────────────────
    # Engages only beyond the activation threshold, and the width must be TIGHTER
    # than the initial stop or the trail sits behind the stop and never ratchets.
    if r_multiple >= policy.trail_activation_r and atr > 0:
        dec.trail_active = True
        trail_dist = atr * policy.runner_trail_atr
        trail_sl = round(price - trail_dist, digits) if is_buy else round(price + trail_dist, digits)

        if is_buy:
            # Only trail once it is above entry, otherwise it cannot lock profit.
            if trail_sl > entry and trail_sl > dec.new_sl and trail_sl < price:
                dec.new_sl = trail_sl
                dec.actions.append(f"TRAIL_ATR_{policy.runner_trail_atr:.2f}")
        else:
            if trail_sl < entry and (dec.new_sl == 0 or trail_sl < dec.new_sl) and trail_sl > price:
                dec.new_sl = trail_sl
                dec.actions.append(f"TRAIL_ATR_{policy.runner_trail_atr:.2f}")

    # ── 5. Structural ratchet ───────────────────────────────────────────────
    if struct_level and struct_level > 0 and atr > 0:
        struct_sl = (
            round(struct_level - atr * 0.20, digits) if is_buy
            else round(struct_level + atr * 0.20, digits)
        )
        if is_buy and struct_sl > dec.new_sl and struct_sl < price:
            dec.new_sl = struct_sl
            dec.actions.append("SR_RATCHET")
        elif (not is_buy) and (dec.new_sl == 0 or struct_sl < dec.new_sl) and struct_sl > price:
            dec.new_sl = struct_sl
            dec.actions.append("SR_RATCHET")

    # ── 6. Cross-cutting invariants ─────────────────────────────────────────
    # A stop must never cross the current price (the broker would reject it) and
    # must never move backwards (ratchets are one-way by definition).
    if is_buy:
        if dec.new_sl >= price:
            dec.new_sl = current_sl
        if dec.new_sl < current_sl:
            dec.new_sl = current_sl
    else:
        if dec.new_sl <= price:
            dec.new_sl = current_sl
        if current_sl > 0 and dec.new_sl > current_sl:
            dec.new_sl = current_sl

    return dec


__all__ = [
    "ExitPolicy",
    "ExitDecision",
    "evaluate_exit",
    "DEFAULT_BE_TRIGGER_R",
    "DEFAULT_FAST_CASH_R",
    "DEFAULT_RUNNER_TRAIL_ATR",
]
