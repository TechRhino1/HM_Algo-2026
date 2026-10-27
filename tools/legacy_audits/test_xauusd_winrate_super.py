"""
Super Precision Calibration for XAUUSD targeting 70%+ Win Rate.
Tests the fusion of High-Conviction Gating + Fast Cash at 1.0R + Breakeven Lock at 1.0R + Dead Zone Filtering.
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
from jarvis.intelligence.symbol_profile_config import get_symbol_profile_config, SYMBOL_PROFILES
from jarvis.intelligence.winrate_targeting import WRProfileStore, WRTargetProfile

def run_test():
    cfg = [c for c in SYMBOL_CONFIGS if c["canonical"] == "XAUUSD"][0]
    df = load_real_mt5_data(cfg, days=90)
    spec = resolve_symbol("XAUUSD")
    
    # Check calibrated WR profile from winrate_profiles.json
    store = WRProfileStore(os.path.join(BASE_DIR, "config", "winrate_profiles.json"))
    profiles = store.load()
    xau_profile = profiles.get("XAUUSD")
    
    print("=" * 120)
    print("             XAUUSD 70%+ WIN-RATE SUPER PRECISION CALIBRATION")
    print("=" * 120)
    
    # 1. Baseline
    engine = BacktestEngine(initial_balance=10000.0, risk_per_trade_pct=0.5, slippage_pips=0.5)
    res_base = engine.run_backtest(df_h1=df, symbol="XAUUSD", spread_pips=spec.typical_spread_pips, start_bar_idx=25)
    t_base = res_base.get("trades", [])
    w_base = sum(1 for t in t_base if t.get("is_win"))
    wr_base = (w_base / max(1, len(t_base))) * 100.0
    pnl_base = res_base.get("final_balance", 10000.0) - 10000.0
    print(f"[1. Baseline                             ] Trades: {len(t_base):>2} | WR: {wr_base:>5.1f}% | Net: ${pnl_base:>+8.2f} | PF: {res_base.get('metrics', {}).get('profit_factor', 0):>4.2f}")
    
    # 2. Run with WR profile if present
    if xau_profile:
        engine2 = BacktestEngine(initial_balance=10000.0, risk_per_trade_pct=0.5, slippage_pips=0.5)
        res_prof = engine2.run_backtest(df_h1=df, symbol="XAUUSD", spread_pips=spec.typical_spread_pips, start_bar_idx=25, wr_profile=xau_profile)
        t_prof = res_prof.get("trades", [])
        w_prof = sum(1 for t in t_prof if t.get("is_win"))
        wr_prof = (w_prof / max(1, len(t_prof))) * 100.0
        pnl_prof = res_prof.get("final_balance", 10000.0) - 10000.0
        print(f"[2. Default WR Profile                   ] Trades: {len(t_prof):>2} | WR: {wr_prof:>5.1f}% | Net: ${pnl_prof:>+8.2f} | PF: {res_prof.get('metrics', {}).get('profit_factor', 0):>4.2f}")

    # 3. Custom 75% Target Profile with tp_r=1.0, fast_cash_r=0.8, be_trigger_r=0.8, min_score=0.62
    prof_70 = WRTargetProfile.from_dict(xau_profile.to_dict()) if xau_profile else None
    if prof_70:
        prof_70.target_wr = 0.75
        prof_70.geometry.tp_r = 1.2
        prof_70.geometry.fast_cash_r = 0.8
        prof_70.geometry.fast_cash_pct = 0.5
        prof_70.geometry.be_trigger_r = 0.8
        prof_70.geometry.min_score = 0.62
        prof_70.geometry.max_bars = 48
        
        # Disable WEAK_TREND and RANGE
        if "WEAK_TREND" in prof_70.regime_edge:
            prof_70.regime_edge["WEAK_TREND"].enabled = False
        if "RANGE" in prof_70.regime_edge:
            prof_70.regime_edge["RANGE"].enabled = False
            
        engine3 = BacktestEngine(initial_balance=10000.0, risk_per_trade_pct=0.5, slippage_pips=0.5)
        res_70 = engine3.run_backtest(df_h1=df, symbol="XAUUSD", spread_pips=spec.typical_spread_pips, start_bar_idx=25, wr_profile=prof_70)
        t_70 = res_70.get("trades", [])
        w_70 = sum(1 for t in t_70 if t.get("is_win"))
        wr_70 = (w_70 / max(1, len(t_70))) * 100.0
        pnl_70 = res_70.get("final_balance", 10000.0) - 10000.0
        print(f"[3. Tuned 70%+ Profile (tp1.2, be0.8)    ] Trades: {len(t_70):>2} | WR: {wr_70:>5.1f}% | Net: ${pnl_70:>+8.2f} | PF: {res_70.get('metrics', {}).get('profit_factor', 0):>4.2f}")

        # 4. Ultra Selective: min_score=0.66, tp_r=1.0, be_trigger_r=0.75
        prof_ultra = WRTargetProfile.from_dict(prof_70.to_dict())
        prof_ultra.geometry.tp_r = 1.0
        prof_ultra.geometry.fast_cash_r = 0.75
        prof_ultra.geometry.be_trigger_r = 0.75
        prof_ultra.geometry.min_score = 0.66
        engine4 = BacktestEngine(initial_balance=10000.0, risk_per_trade_pct=0.5, slippage_pips=0.5)
        res_ultra = engine4.run_backtest(df_h1=df, symbol="XAUUSD", spread_pips=spec.typical_spread_pips, start_bar_idx=25, wr_profile=prof_ultra)
        t_u = res_ultra.get("trades", [])
        w_u = sum(1 for t in t_u if t.get("is_win"))
        wr_u = (w_u / max(1, len(t_u))) * 100.0
        pnl_u = res_ultra.get("final_balance", 10000.0) - 10000.0
        print(f"[4. Ultra High Precision (min_score=0.66)] Trades: {len(t_u):>2} | WR: {wr_u:>5.1f}% | Net: ${pnl_u:>+8.2f} | PF: {res_ultra.get('metrics', {}).get('profit_factor', 0):>4.2f}")

    print("=" * 120)

if __name__ == "__main__":
    run_test()
