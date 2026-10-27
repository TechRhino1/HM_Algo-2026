"""
Test requiring structural shift or sweep on Gold TREND_PULLBACK to push overall Gold WR to 70%+.
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

def run_test():
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
    
    # Configure Gold Precision Exit Parameters
    orig_prof.be_trigger_r = 0.85
    orig_prof.fast_cash_r = 0.85
    orig_prof.fast_cash_volume_pct = 0.70
    orig_prof.runner_trail_atr = 1.25
    orig_prof.allowed_utc_hours = (7, 16)
    orig_prof.premium_window_exception = False
    
    # 1. Strategy Selector patch
    orig_select = StrategySelector.select_strategy_probabilities
    def patched_select(self, regime, context=None, **kwargs):
        probs = orig_select(self, regime, context, **kwargs)
        sym = getattr(context, "symbol", "") or ""
        if sym in ("XAUUSD", "GOLD", "GOLD.i#", "GOLD#", "XAGUSD", "SILVER"):
            probs["RANGE_MEAN_REVERSION"] = 0.0
            probs["TREND_FOLLOWING"] = 0.0
            probs["BREAKOUT_EXPANSION"] = 0.0
            probs["CHOCH_STRUCTURAL_REVERSAL"] = max(probs.get("CHOCH_STRUCTURAL_REVERSAL", 0.0), 0.50)
            probs["LIQUIDITY_SWEEP_REVERSAL"] = max(probs.get("LIQUIDITY_SWEEP_REVERSAL", 0.0), 0.35)
            probs["TREND_PULLBACK"] = 0.15
            tot = sum(probs.values())
            if tot > 0:
                probs = {k: v / tot for k, v in probs.items()}
        return probs
    StrategySelector.select_strategy_probabilities = patched_select
    
    # 2. Decision Engine evaluate patch for Gold Pullbacks
    orig_evaluate = DecisionEngine.evaluate
    def patched_evaluate(self, context, regime, analyst_reports, devil_report, **kwargs):
        dec = orig_evaluate(self, context, regime, analyst_reports, devil_report, **kwargs)
        sym = getattr(context, "symbol", "") or ""
        if sym in ("XAUUSD", "GOLD", "GOLD.i#", "GOLD#", "XAGUSD", "SILVER"):
            strat = dec.strategy or getattr(context, "strategy", "")
            st = getattr(context, "structure", None)
            liq = getattr(context, "liquidity", None)
            has_sweep = bool(getattr(liq, "sweep_detected", False))
            has_choch = bool(getattr(st, "choch", False))
            has_bos = bool(getattr(st, "bos", False))
            st_zone = getattr(st, "discount_premium_zone", "EQUILIBRIUM") if st else "EQUILIBRIUM"
            
            # On Gold, TREND_PULLBACK must have structural confirmation (sweep or BOS or CHOCH)
            # and strictly be in DISCOUNT (for BUY) or PREMIUM (for SELL)
            if strat == "TREND_PULLBACK":
                if dec.bias == "BUY" and not (has_sweep or has_bos or has_choch or st_zone == "DISCOUNT"):
                    dec.decision = "HOLD"
                    dec.quality_gate.passed = False
                elif dec.bias == "SELL" and not (has_sweep or has_bos or has_choch or st_zone == "PREMIUM"):
                    dec.decision = "HOLD"
                    dec.quality_gate.passed = False
                    
            # In WEAK_TREND regime, Gold is prone to noise: require CHOCH or sweep
            reg_name = getattr(regime.primary_regime, "value", str(regime.primary_regime)).upper()
            if "WEAK" in reg_name and not (has_sweep or has_choch):
                dec.decision = "HOLD"
                dec.quality_gate.passed = False
                
        return dec
    DecisionEngine.evaluate = patched_evaluate
    
    engine = BacktestEngine(initial_balance=10000.0, risk_per_trade_pct=0.5, slippage_pips=0.5)
    res = engine.run_backtest(df_h1=df, symbol="XAUUSD", spread_pips=spec.typical_spread_pips, start_bar_idx=25)
    
    # Restore
    StrategySelector.select_strategy_probabilities = orig_select
    DecisionEngine.evaluate = orig_evaluate
    orig_prof.be_trigger_r = orig_be
    orig_prof.fast_cash_r = orig_fc
    orig_prof.fast_cash_volume_pct = orig_fc_vol
    orig_prof.runner_trail_atr = orig_trail
    orig_prof.allowed_utc_hours = orig_hours
    orig_prof.premium_window_exception = orig_prem
    
    trades = res.get("trades", [])
    wins = [t for t in trades if t.get("is_win", False)]
    losses = [t for t in trades if not t.get("is_win", False)]
    wr = (len(wins) / max(1, len(trades))) * 100.0 if trades else 0.0
    final_bal = res.get("final_balance", 10000.0)
    net_pnl = final_bal - 10000.0
    pf = res.get("metrics", {}).get("profit_factor", 0.0)
    max_dd = res.get("metrics", {}).get("max_drawdown_pct", 0.0)
    
    print("=" * 100)
    print("GOLD PULLBACK REFINEMENT RESULT:")
    print(f"Total Trades : {len(trades)}")
    print(f"Win Rate     : {wr:.1f}% ({len(wins)} W / {len(losses)} L)")
    print(f"Net Profit   : ${net_pnl:+,.2f}")
    print(f"Profit Factor: {pf:.2f}")
    print(f"Max Drawdown : {max_dd:.1f}%")
    print("=" * 100)
    
    df_trades = pd.DataFrame(trades)
    if not df_trades.empty:
        print("\nBreakdown by strategy:")
        for strat, grp in df_trades.groupby("strategy"):
            w = len(grp[grp["is_win"] == True])
            print(f"  {strat:<30}: {len(grp):>2} trades | {w:>2} wins | WR: {(w/len(grp))*100:>5.1f}% | PnL: ${grp['pnl'].sum():>+8.2f}")

if __name__ == "__main__":
    run_test()
