"""Unit & regression tests for Spread-Buffered Front-Run Take Profit,
Relative Progress-to-TP Ratchets, and Proximity Harvesting.
"""

import pytest
from jarvis.execution.exit_policy import ExitPolicy, evaluate_exit
from jarvis.intelligence.dynamic_levels import DynamicRiskAndLevelsEngine
from jarvis.data.symbol_registry import resolve


def test_exit_policy_relative_tp_progress_breakeven():
    """Verify that achieving 50% progress toward a sub-1.6R target triggers breakeven lock."""
    policy = ExitPolicy.for_symbol("GOLD.i#", resolve("GOLD.i#"))
    
    # SELL position: entry=4150.00, SL=4170.00 (20 pts risk = 1R), TP=4140.00 (10 pts target = 0.50R)
    entry = 4150.00
    initial_sl = 4170.00
    current_sl = 4170.00
    tp = 4140.00
    price = 4145.00
    favorable_dist = 5.00  # 5 pts = exactly 50% of 10 pts target move

    dec = evaluate_exit(
        side="SELL",
        entry=entry,
        initial_sl=initial_sl,
        current_sl=current_sl,
        tp=tp,
        price=price,
        favorable_dist=favorable_dist,
        atr=8.0,
        policy=policy,
    )

    assert dec.be_locked is True
    # Breakeven stop should be placed below entry for a SELL (entry - buffer)
    assert dec.new_sl < entry
    assert dec.new_sl < initial_sl
    assert any("BE_LOCK_50%TP" in act for act in dec.actions)


def test_exit_policy_relative_tp_progress_milestones():
    """Verify tiered profit locking at 65%, 80%, 90%, and 94% progress to TP."""
    policy = ExitPolicy.for_symbol("GOLD.i#", resolve("GOLD.i#"))
    
    entry = 4153.41
    initial_sl = 4172.96  # 19.55 pts risk
    current_sl = 4172.96
    tp = 4143.88          # 9.53 pts target move
    tp_dist = abs(tp - entry)

    # 1. At 65% progress: price reached 4147.20 (6.21 pts excursion)
    dec_65 = evaluate_exit(
        side="SELL",
        entry=entry,
        initial_sl=initial_sl,
        current_sl=current_sl,
        tp=tp,
        price=4147.20,
        favorable_dist=tp_dist * 0.66,
        atr=7.0,
        policy=policy,
    )
    assert any("PROGRESS_LOCK_65%TP" in act for act in dec_65.actions)
    assert dec_65.new_sl < entry

    # 2. At 80% progress: price reached 4145.78 (7.63 pts excursion)
    dec_80 = evaluate_exit(
        side="SELL",
        entry=entry,
        initial_sl=initial_sl,
        current_sl=dec_65.new_sl,
        tp=tp,
        price=4145.78,
        favorable_dist=tp_dist * 0.82,
        atr=7.0,
        policy=policy,
    )
    assert any("PROGRESS_LOCK_80%TP" in act for act in dec_80.actions)
    assert dec_80.new_sl < dec_65.new_sl  # Tighter stop on SELL

    # 3. At 94% progress: price reached 4144.50 (live near-target price)
    dec_94_live = evaluate_exit(
        side="SELL",
        entry=entry,
        initial_sl=initial_sl,
        current_sl=current_sl,
        tp=tp,
        price=4144.50,
        favorable_dist=9.50,
        atr=7.0,
        policy=policy,
    )
    assert any("PROGRESS_LOCK_94%TP" in act for act in dec_94_live.actions)
    assert dec_94_live.new_sl < 4147.00
    assert dec_94_live.new_sl < entry

    # 4. Retracement case: price retraced back to 4146.90
    dec_94_retrace = evaluate_exit(
        side="SELL",
        entry=entry,
        initial_sl=initial_sl,
        current_sl=current_sl,
        tp=tp,
        price=4146.90,
        favorable_dist=9.50,
        atr=7.0,
        policy=policy,
    )
    # Even after retracing, stop loss is safely locked below entry and above price!
    assert dec_94_retrace.new_sl > 4146.90
    assert dec_94_retrace.new_sl < entry
    assert dec_94_retrace.new_sl < initial_sl


def test_buy_tp_progress_ratchets():
    """Verify BUY trades ratchet stops up progressively as TP is approached."""
    policy = ExitPolicy.for_symbol("EURUSD", resolve("EURUSD"))
    
    entry = 1.08000
    initial_sl = 1.07600  # 40 pips risk
    current_sl = 1.07600
    tp = 1.08400          # 40 pips target (1.0R)
    tp_dist = 0.00400

    # 85% progress: price reached 1.08340
    dec = evaluate_exit(
        side="BUY",
        entry=entry,
        initial_sl=initial_sl,
        current_sl=current_sl,
        tp=tp,
        price=1.08320,
        favorable_dist=tp_dist * 0.85,
        atr=0.0020,
        policy=policy,
    )

    assert any("PROGRESS_LOCK_80%TP" in act for act in dec.actions)
    assert dec.new_sl > entry  # Guaranteed profit lock above entry
    assert dec.new_sl < 1.08320  # Below current price
