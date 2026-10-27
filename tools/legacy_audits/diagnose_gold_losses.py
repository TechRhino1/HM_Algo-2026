"""
Diagnose the 11 losing TREND_PULLBACK trades on Gold.
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

def run_diagnosis():
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
            probs["LIQUIDITY_SWEEP_REVERSAL"] = 0.0
            probs["CHOCH_STRUCTURAL_REVERSAL"] = 5.0
            probs["TREND_PULLBACK"] = 1.0
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
            
            c_ts = getattr(context, "timestamp", None)
            if c_ts and hasattr(c_ts, "hour"):
                h = c_ts.hour
                is_kz = (7 <= h <= 9) or (12 <= h <= 15)
                if not is_kz and not has_choch:
                    dec.decision = "HOLD"
                    dec.quality_gate.passed = False

            if strat == "TREND_PULLBACK":
                atr = getattr(context.volatility, "atr", 1.0)
                price_diff = abs(context.current_price - dec.entry_price)
                if price_diff > (0.35 * atr):
                    dec.decision = "HOLD"
                    dec.quality_gate.passed = False
                    
        return dec
    DecisionEngine.evaluate = patched_evaluate
    
    engine = BacktestEngine(initial_balance=10000.0, risk_per_trade_pct=0.5, slippage_pips=0.5)
    res = engine.run_backtest(df_h1=df, symbol="XAUUSD", spread_pips=spec.typical_spread_pips, start_bar_idx=25)
    
    trades = res.get("trades", [])
    losses = [t for t in trades if not t.get("is_win", False) and t.get("strategy") == "TREND_PULLBACK"]
    print(f"Total Losing TREND_PULLBACK Trades: {len(losses)}")
    for t in losses:
        pnl = t.get("pnl", 0.0)
        t_type = t.get("type", "")
        op_t = t.get("open_time", "")
        op_p = t.get("open_price", 0.0)
        sl = t.get("sl", 0.0)
        tp = t.get("tp", 0.0)
        reg = t.get("regime", "")
        score = t.get("score", 0.0)
        print(f"LOSS | {t_type:<4} @ {op_p:.2f} | SL: {sl:.2f} | TP: {tp:.2f} | PnL: ${pnl:+6.2f} | Regime: {reg:<16} | Score: {score:.1f} | Time: {op_t}")

if __name__ == "__main__":
    run_diagnosis()
