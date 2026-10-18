import sys
from pathlib import Path
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jarvis.backtesting.engine import BacktestEngine
from jarvis.config.runtime import offline_mode
from jarvis.intelligence.symbol_profile_config import SYMBOL_PROFILES

cfg = SYMBOL_PROFILES["WTI"]
orig_banned = list(cfg.banned_strategies)
orig_weights = dict(cfg.strategy_weights)

df = pd.read_parquet(REPO_ROOT / 'data/market/real/WTI/WTI_H1_122d.parquet')

# Test 1: Baseline
with offline_mode():
    engine = BacktestEngine(initial_balance=10000.0, risk_per_trade_pct=0.5)
    res = engine.run_backtest(df_h1=df, symbol='WTI', spread_pips=13.0)
    m = res['metrics']
    print(f"Baseline: Trades={len(res['trades'])} | WR={m['win_rate_pct']:.1f}% | Net=${m['net_profit']:.2f} | PF={m['profit_factor']:.2f}")

# Test 2: Ban TREND_PULLBACK on WTI
cfg.banned_strategies = ["TREND_PULLBACK"]
cfg.strategy_weights = {
    "LIQUIDITY_SWEEP_REVERSAL": 3.0,
    "CHOCH_STRUCTURAL_REVERSAL": 2.8,
    "RANGE_MEAN_REVERSION": 2.5,
    "BREAKOUT_EXPANSION": 1.5,
    "TREND_FOLLOWING": 1.0,
    "TREND_PULLBACK": 0.0,
}

with offline_mode():
    engine = BacktestEngine(initial_balance=10000.0, risk_per_trade_pct=0.5)
    res = engine.run_backtest(df_h1=df, symbol='WTI', spread_pips=13.0)
    m = res['metrics']
    print(f"Banned Pullback: Trades={len(res['trades'])} | WR={m['win_rate_pct']:.1f}% | Net=${m['net_profit']:.2f} | PF={m['profit_factor']:.2f}")

# Test 3: Ban TREND_PULLBACK and TREND_FOLLOWING
cfg.banned_strategies = ["TREND_PULLBACK", "TREND_FOLLOWING"]
cfg.strategy_weights = {
    "LIQUIDITY_SWEEP_REVERSAL": 3.5,
    "CHOCH_STRUCTURAL_REVERSAL": 3.0,
    "RANGE_MEAN_REVERSION": 3.0,
    "BREAKOUT_EXPANSION": 2.0,
    "TREND_FOLLOWING": 0.0,
    "TREND_PULLBACK": 0.0,
}

with offline_mode():
    engine = BacktestEngine(initial_balance=10000.0, risk_per_trade_pct=0.5)
    res = engine.run_backtest(df_h1=df, symbol='WTI', spread_pips=13.0)
    m = res['metrics']
    print(f"Pure Reversion/Breakout: Trades={len(res['trades'])} | WR={m['win_rate_pct']:.1f}% | Net=${m['net_profit']:.2f} | PF={m['profit_factor']:.2f}")

# Restore
cfg.banned_strategies = orig_banned
cfg.strategy_weights = orig_weights
