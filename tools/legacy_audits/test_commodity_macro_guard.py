import sys
from pathlib import Path
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jarvis.backtesting.engine import BacktestEngine
from jarvis.config.runtime import offline_mode
from jarvis.intelligence.symbol_profile_config import SYMBOL_PROFILES

# Test on WTI with:
# 1. Banned TREND_PULLBACK, TREND_FOLLOWING
# 2. Strategy weights favoring REVERSION, CHOCH, SWEEP, BREAKOUT

cfg = SYMBOL_PROFILES["WTI"]
orig_banned = list(cfg.banned_strategies)
orig_weights = dict(cfg.strategy_weights)

cfg.banned_strategies = ["TREND_PULLBACK", "TREND_FOLLOWING"]
cfg.strategy_weights = {
    "LIQUIDITY_SWEEP_REVERSAL": 3.5,
    "CHOCH_STRUCTURAL_REVERSAL": 3.0,
    "RANGE_MEAN_REVERSION": 3.0,
    "BREAKOUT_EXPANSION": 2.0,
    "TREND_FOLLOWING": 0.0,
    "TREND_PULLBACK": 0.0,
}

df_wti = pd.read_parquet(REPO_ROOT / 'data/market/real/WTI/WTI_H1_122d.parquet')
with offline_mode():
    engine = BacktestEngine(initial_balance=10000.0, risk_per_trade_pct=0.5)
    res = engine.run_backtest(df_h1=df_wti, symbol='WTI', spread_pips=13.0)
    m = res['metrics']
    print(f"WTI Calibrated: Trades={len(res['trades'])} | WR={m['win_rate_pct']:.1f}% | Net=${m['net_profit']:.2f} | PF={m['profit_factor']:.2f}")

cfg.banned_strategies = orig_banned
cfg.strategy_weights = orig_weights

# Test on XAGUSD with:
# 1. Banned TREND_PULLBACK, TREND_FOLLOWING, BREAKOUT_EXPANSION
# 2. Allowed RANGE_MEAN_REVERSION, CHOCH, LIQUIDITY_SWEEP
cfg_xag = SYMBOL_PROFILES["XAGUSD"]
orig_xag_banned = list(cfg_xag.banned_strategies)
orig_xag_weights = dict(cfg_xag.strategy_weights)

cfg_xag.banned_strategies = ["BREAKOUT_EXPANSION", "TREND_PULLBACK", "TREND_FOLLOWING"]
cfg_xag.strategy_weights = {
    "LIQUIDITY_SWEEP_REVERSAL": 3.5,
    "CHOCH_STRUCTURAL_REVERSAL": 3.0,
    "RANGE_MEAN_REVERSION": 2.5,
    "TREND_PULLBACK": 0.0,
    "TREND_FOLLOWING": 0.0,
    "BREAKOUT_EXPANSION": 0.0,
}

df_xag = pd.read_parquet(REPO_ROOT / 'data/market/real/XAGUSD/XAGUSD_H1_122d.parquet')
with offline_mode():
    engine = BacktestEngine(initial_balance=10000.0, risk_per_trade_pct=0.5)
    res = engine.run_backtest(df_h1=df_xag, symbol='XAGUSD', spread_pips=4.0)
    m = res['metrics']
    print(f"XAGUSD Calibrated: Trades={len(res['trades'])} | WR={m['win_rate_pct']:.1f}% | Net=${m['net_profit']:.2f} | PF={m['profit_factor']:.2f}")

cfg_xag.banned_strategies = orig_xag_banned
cfg_xag.strategy_weights = orig_xag_weights
