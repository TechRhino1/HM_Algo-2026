"""
Inspect the 8 TREND_PULLBACK trades on Gold in detail.
"""
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

os.environ["JARVIS_BACKTEST_MODE"] = "1"
os.environ["JARVIS_OFFLINE_MODE"] = "1"

import pandas as pd
import numpy as np

from run_3month_real_mt5_backtest import load_real_mt5_data, SYMBOL_CONFIGS
from jarvis.backtesting.engine import BacktestEngine
from jarvis.data.symbol_registry import resolve as resolve_symbol
from jarvis.intelligence.symbol_profile_config import SYMBOL_PROFILES
from jarvis.intelligence.strategy_selector import StrategySelector
from jarvis.intelligence.decision_engine import DecisionEngine

def inspect_pullbacks():
    cfg = [c for c in SYMBOL_CONFIGS if c["canonical"] == "XAUUSD"][0]
    df = load_real_mt5_data(cfg, days=90)
    spec = resolve_symbol("XAUUSD")
    
    orig_prof = SYMBOL_PROFILES["XAUUSD"]
    orig_prof.be_trigger_r = 0.85
    orig_prof.fast_cash_r = 0.85
    orig_prof.fast_cash_volume_pct = 0.70
    orig_prof.runner_trail_atr = 1.25
    orig_prof.allowed_utc_hours = (7, 16)
    orig_prof.premium_window_exception = False
    
    orig_select = StrategySelector.select_strategy_probabilities
    def patched_select(self, regime, context=None, **kwargs):
        probs = orig_select(self, regime, context, **kwargs)
        sym = getattr(context, "symbol", "") or ""
        if sym in ("XAUUSD", "GOLD", "GOLD.i#", "GOLD#", "XAGUSD", "SILVER"):
            probs["RANGE_MEAN_REVERSION"] = 0.0
            probs["TREND_FOLLOWING"] = 0.0
            probs["BREAKOUT_EXPANSION"] = 0.0
            probs["CHOCH_STRUCTURAL_REVERSAL"] = max(probs.get("CHOCH_STRUCTURAL_REVERSAL", 0.0), 4.5)
            probs["TREND_PULLBACK"] = 1.5
            probs["LIQUIDITY_SWEEP_REVERSAL"] = 0.5
            tot = sum(probs.values())
            if tot > 0:
                probs = {k: v / tot for k, v in probs.items()}
        return probs
    StrategySelector.select_strategy_probabilities = patched_select
    
    orig_evaluate = DecisionEngine.evaluate
    def patched_evaluate(self, context, regime, analyst_reports, devil_report, **kwargs):
        dec = orig_evaluate(self, context, regime, analyst_reports, devil_report, **kwargs)
        sym = getattr(context, "symbol", "") or ""
        if sym in ("XAUUSD", "GOLD", "GOLD.i#", "GOLD#", "XAGUSD", "SILVER"):
            strat = dec.strategy or getattr(context, "strategy", "")
            st = getattr(context, "structure", None)
            has_choch = bool(getattr(st, "choch", False))
            has_bos = bool(getattr(st, "bos", False))
            st_zone = getattr(st, "discount_premium_zone", "EQUILIBRIUM") if st else "EQUILIBRIUM"
            
            if dec.bias == "BUY" and st_zone != "DISCOUNT" and not has_choch:
                dec.decision = "HOLD"
                dec.quality_gate.passed = False
            elif dec.bias == "SELL" and st_zone != "PREMIUM" and not has_choch:
                dec.decision = "HOLD"
                dec.quality_gate.passed = False
                    
            if strat == "TREND_PULLBACK" and not (has_bos or has_choch):
                dec.decision = "HOLD"
                dec.quality_gate.passed = False
                
        return dec
    DecisionEngine.evaluate = patched_evaluate
    
    engine = BacktestEngine(initial_balance=10000.0, risk_per_trade_pct=0.5, slippage_pips=0.5)
    res = engine.run_backtest(df_h1=df, symbol="XAUUSD", spread_pips=spec.typical_spread_pips, start_bar_idx=25)
    
    trades = res.get("trades", [])
    for t in trades:
        strat = t.get("strategy", "")
        is_w = t.get("is_win", False)
        pnl = t.get("pnl", 0.0)
        t_type = t.get("type", "")
        op_t = t.get("open_time", "")
        op_p = t.get("open_price", 0.0)
        sl = t.get("sl", 0.0)
        tp = t.get("tp", 0.0)
        exit_r = t.get("exit_reason", "")
        print(f"[{'WIN' if is_w else 'LOSS'}] {strat:<28} | {t_type:<4} @ {op_p:.2f} | SL: {sl:.2f} | TP: {tp:.2f} | Exit: {exit_r:<12} | PnL: ${pnl:+6.2f} | Time: {op_t}")

if __name__ == "__main__":
    inspect_pullbacks()
