"""
Test adding a strict Gold/Silver displacement and structural shift gate to push Win Rate past 70%.
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
from jarvis.intelligence.decision_engine import DecisionEngine

def run_gate_experiment():
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
    
    orig_prof.be_trigger_r = 0.80
    orig_prof.fast_cash_r = 0.80
    orig_prof.fast_cash_volume_pct = 0.70
    orig_prof.runner_trail_atr = 1.20
    orig_prof.allowed_utc_hours = (7, 16)
    orig_prof.premium_window_exception = False
    
    # Let's inspect what happens if we patch DecisionEngine for Gold
    # Save original evaluate method
    orig_evaluate = DecisionEngine.evaluate
    
    def patched_evaluate(self, context, regime, analyst_reports, devil_report, **kwargs):
        dec = orig_evaluate(self, context, regime, analyst_reports, devil_report, **kwargs)
        sym = getattr(context, "symbol", "") or ""
        if sym in ("XAUUSD", "GOLD", "GOLD.i#", "GOLD#", "XAGUSD", "SILVER"):
            # Precision Filter for Metals:
            # Require minimum AI score >= 70.0 (filters out weak signals)
            # Require structural confirmation (sweep or choch or bos)
            # Pure Discount/Premium (no buying above 0.48 or selling below 0.52)
            ai_score = getattr(dec, "model_confidence", 0.0) or 0.0
            if hasattr(dec, "raw_score"):
                ai_score = max(ai_score, dec.raw_score)
                
            st = getattr(context, "structure", None)
            liq = getattr(context, "liquidity", None)
            has_sweep = bool(getattr(liq, "sweep_detected", False))
            has_choch = bool(getattr(st, "choch", False))
            has_bos = bool(getattr(st, "bos", False))
            st_zone = getattr(st, "discount_premium_zone", "EQUILIBRIUM") if st else "EQUILIBRIUM"
            
            # 1. Structural confirmation
            has_structural_confirm = has_sweep or has_choch or has_bos
            
            # 2. Zone discipline
            zone_ok = True
            if dec.bias == "BUY" and st_zone in ("PREMIUM", "DEEP_PREMIUM"):
                zone_ok = False
            elif dec.bias == "SELL" and st_zone in ("DISCOUNT", "DEEP_DISCOUNT"):
                zone_ok = False
                
            # 3. Minimum score threshold
            score_ok = ai_score >= 0.52  # normalized model_confidence
            
            if dec.decision == "EXECUTE" and not (has_structural_confirm and zone_ok and score_ok):
                dec.decision = "HOLD"
                dec.quality_gate.passed = False
                dec.quality_gate.failing_reasons.append("Gold Precision Structural Gate")
        return dec
        
    DecisionEngine.evaluate = patched_evaluate
    
    engine = BacktestEngine(initial_balance=10000.0, risk_per_trade_pct=0.5, slippage_pips=0.5)
    res = engine.run_backtest(df_h1=df, symbol="XAUUSD", spread_pips=spec.typical_spread_pips, start_bar_idx=25)
    
    # Restore evaluate
    DecisionEngine.evaluate = orig_evaluate
    
    trades = res.get("trades", [])
    wins = [t for t in trades if t.get("is_win", False)]
    losses = [t for t in trades if not t.get("is_win", False)]
    wr = (len(wins) / max(1, len(trades))) * 100.0 if trades else 0.0
    final_bal = res.get("final_balance", 10000.0)
    net_pnl = final_bal - 10000.0
    pf = res.get("metrics", {}).get("profit_factor", 0.0)
    max_dd = res.get("metrics", {}).get("max_drawdown_pct", 0.0)
    
    print("=" * 100)
    print("GOLD PRECISION STRUCTURAL GATE RESULT:")
    print(f"Total Trades : {len(trades)}")
    print(f"Win Rate     : {wr:.1f}% ({len(wins)} W / {len(losses)} L)")
    print(f"Net Profit   : ${net_pnl:+,.2f}")
    print(f"Profit Factor: {pf:.2f}")
    print(f"Max Drawdown : {max_dd:.1f}%")
    print("=" * 100)
    
    # Restore profile
    orig_prof.be_trigger_r = orig_be
    orig_prof.fast_cash_r = orig_fc
    orig_prof.fast_cash_volume_pct = orig_fc_vol
    orig_prof.runner_trail_atr = orig_trail
    orig_prof.allowed_utc_hours = orig_hours
    orig_prof.premium_window_exception = orig_prem

if __name__ == "__main__":
    run_gate_experiment()
