"""
Advanced 70%+ Win-Rate Calibration for XAUUSD & XAGUSD on Real 90-Day MT5 Historical Data.
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
from datetime import datetime, timezone

from run_3month_real_mt5_backtest import load_real_mt5_data, SYMBOL_CONFIGS
from jarvis.backtesting.engine import BacktestEngine
from jarvis.data.symbol_registry import resolve as resolve_symbol
from jarvis.intelligence.symbol_profile_config import get_symbol_profile_config, SYMBOL_PROFILES

def run_advanced_gold_test():
    cfg = [c for c in SYMBOL_CONFIGS if c["canonical"] == "XAUUSD"][0]
    df = load_real_mt5_data(cfg, days=90)
    spec = resolve_symbol("XAUUSD")
    
    orig_prof = SYMBOL_PROFILES["XAUUSD"]
    orig_be = orig_prof.be_trigger_r
    orig_fc = orig_prof.fast_cash_r
    orig_fc_vol = orig_prof.fast_cash_volume_pct
    orig_trail = orig_prof.runner_trail_atr
    orig_hours = orig_prof.allowed_utc_hours
    orig_prem = orig_prof.premium_window_exception
    orig_strat_weights = dict(orig_prof.strategy_weights)
    orig_banned = list(orig_prof.banned_strategies)
    
    experiments = [
        {
            "name": "1. Baseline (Current)",
            "be_r": 1.80, "fc_r": 1.50, "fc_vol": 0.33, "trail": 2.00,
            "hours": (12, 16), "prem_exc": True,
            "weights": {"TREND_PULLBACK": 3.8, "LIQUIDITY_SWEEP_REVERSAL": 3.8, "CHOCH_STRUCTURAL_REVERSAL": 3.0},
            "banned": ["RANGE_MEAN_REVERSION", "TREND_FOLLOWING", "BREAKOUT_EXPANSION"],
        },
        {
            "name": "2. Fast Cash 1.0R + BE 1.0R + Trail 1.5 ATR",
            "be_r": 1.00, "fc_r": 1.00, "fc_vol": 0.50, "trail": 1.50,
            "hours": (12, 16), "prem_exc": True,
            "weights": {"TREND_PULLBACK": 3.8, "LIQUIDITY_SWEEP_REVERSAL": 3.8, "CHOCH_STRUCTURAL_REVERSAL": 3.0},
            "banned": ["RANGE_MEAN_REVERSION", "TREND_FOLLOWING", "BREAKOUT_EXPANSION"],
        },
        {
            "name": "3. Promote CHOCH Reversal (4.5) + Ban Dead Zone (No Prem Exception)",
            "be_r": 1.00, "fc_r": 1.00, "fc_vol": 0.50, "trail": 1.50,
            "hours": (12, 16), "prem_exc": False,
            "weights": {"CHOCH_STRUCTURAL_REVERSAL": 4.5, "LIQUIDITY_SWEEP_REVERSAL": 4.0, "TREND_PULLBACK": 2.0},
            "banned": ["RANGE_MEAN_REVERSION", "TREND_FOLLOWING", "BREAKOUT_EXPANSION"],
        },
        {
            "name": "4. London + NY Killzones (07-16 UTC) + CHOCH Priority (4.5) + 1.0R Fast Cash",
            "be_r": 1.00, "fc_r": 1.00, "fc_vol": 0.50, "trail": 1.50,
            "hours": (7, 16), "prem_exc": False,
            "weights": {"CHOCH_STRUCTURAL_REVERSAL": 4.5, "LIQUIDITY_SWEEP_REVERSAL": 4.0, "TREND_PULLBACK": 2.0},
            "banned": ["RANGE_MEAN_REVERSION", "TREND_FOLLOWING", "BREAKOUT_EXPANSION"],
        },
        {
            "name": "5. Ultra Precision: Fast Cash 0.9R + BE 0.9R + Bank 65% + Trail 1.4 ATR",
            "be_r": 0.90, "fc_r": 0.90, "fc_vol": 0.65, "trail": 1.40,
            "hours": (12, 16), "prem_exc": True,
            "weights": {"CHOCH_STRUCTURAL_REVERSAL": 4.5, "LIQUIDITY_SWEEP_REVERSAL": 4.0, "TREND_PULLBACK": 2.0},
            "banned": ["RANGE_MEAN_REVERSION", "TREND_FOLLOWING", "BREAKOUT_EXPANSION"],
        },
    ]
    
    print("=" * 130)
    print("                 ADVANCED XAUUSD 70%+ WIN-RATE TARGETING EXPERIMENTS")
    print("=" * 130)
    
    for exp in experiments:
        orig_prof.be_trigger_r = exp["be_r"]
        orig_prof.fast_cash_r = exp["fc_r"]
        orig_prof.fast_cash_volume_pct = exp["fc_vol"]
        orig_prof.runner_trail_atr = exp["trail"]
        orig_prof.allowed_utc_hours = exp["hours"]
        orig_prof.premium_window_exception = exp["prem_exc"]
        orig_prof.strategy_weights = exp["weights"]
        orig_prof.banned_strategies = exp["banned"]
        
        engine = BacktestEngine(
            initial_balance=10000.0,
            risk_per_trade_pct=0.5,
            commission_per_lot=0.0,
            slippage_pips=0.5
        )
        
        res = engine.run_backtest(
            df_h1=df,
            symbol="XAUUSD",
            spread_pips=spec.typical_spread_pips,
            start_bar_idx=25
        )
        
        trades = res.get("trades", [])
        wins = [t for t in trades if t.get("is_win", False)]
        losses = [t for t in trades if not t.get("is_win", False)]
        wr = (len(wins) / max(1, len(trades))) * 100.0 if trades else 0.0
        final_bal = res.get("final_balance", 10000.0)
        net_pnl = final_bal - 10000.0
        pf = res.get("metrics", {}).get("profit_factor", 0.0)
        max_dd = res.get("metrics", {}).get("max_drawdown_pct", 0.0)
        
        print(f"[{exp['name']:<75}] Trades: {len(trades):>2} | WR: {wr:>5.1f}% | Net: ${net_pnl:>+8.2f} | PF: {pf:>4.2f} | MaxDD: {max_dd:>4.1f}%", flush=True)

    # Restore original profile
    orig_prof.be_trigger_r = orig_be
    orig_prof.fast_cash_r = orig_fc
    orig_prof.fast_cash_volume_pct = orig_fc_vol
    orig_prof.runner_trail_atr = orig_trail
    orig_prof.allowed_utc_hours = orig_hours
    orig_prof.premium_window_exception = orig_prem
    orig_prof.strategy_weights = orig_strat_weights
    orig_prof.banned_strategies = orig_banned
    print("=" * 130)

if __name__ == "__main__":
    run_advanced_gold_test()
