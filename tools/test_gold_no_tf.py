import sys
from pathlib import Path
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jarvis.backtesting.engine import BacktestEngine
from jarvis.config.runtime import offline_mode
from jarvis.intelligence.symbol_profile_config import SYMBOL_PROFILES

cfg = SYMBOL_PROFILES['XAUUSD']
print("Original banned:", cfg.banned_strategies)

# Test 1: Ban TREND_FOLLOWING on Gold
cfg.banned_strategies = ["RANGE_MEAN_REVERSION", "TREND_FOLLOWING"]
cfg.strategy_weights = {
    "TREND_FOLLOWING": 0.0,
    "TREND_PULLBACK": 3.5,
    "LIQUIDITY_SWEEP_REVERSAL": 3.5,
    "CHOCH_STRUCTURAL_REVERSAL": 3.0,
    "BREAKOUT_EXPANSION": 1.5,
    "RANGE_MEAN_REVERSION": 0.0,
}

df = pd.read_parquet(REPO_ROOT / 'data/market/real/XAUUSD/XAUUSD_H1_122d.parquet')
with offline_mode():
    engine = BacktestEngine(initial_balance=10000.0, risk_per_trade_pct=0.5)
    res = engine.run_backtest(df_h1=df, symbol='XAUUSD', spread_pips=2.0)
    m = res['metrics']
    print(f"Test 1 (No Trend Following): Trades={len(res['trades'])} | Net=${m['net_profit']:.2f} | WR={m['win_rate_pct']:.1f}% | PF={m['profit_factor']:.2f}")

    strats = {}
    for t in res['trades']:
        s = t.get('strategy', 'UNKNOWN')
        strats.setdefault(s, {'cnt': 0, 'wins': 0, 'pnl': 0.0})
        strats[s]['cnt'] += 1
        if t['pnl'] > 0:
            strats[s]['wins'] += 1
        strats[s]['pnl'] += t['pnl']
    for s, v in strats.items():
        print(f"  {s:<30}: {v['cnt']:3} trades | Wins: {v['wins']:3} | PnL: ${v['pnl']:8.2f}")
