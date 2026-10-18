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
    trades = [t for t in res['trades'] if t.get('strategy') == 'TREND_PULLBACK']
    
    losses = [t for t in trades if t['pnl'] < 0]
    wins = [t for t in trades if t['pnl'] > 0]
    print(f"TREND_PULLBACK: {len(trades)} trades | {len(wins)} wins, {len(losses)} losses | Net: ${sum(t['pnl'] for t in trades):.2f}")
    
    # Check by direction
    buy_trades = [t for t in trades if t['type'] == 'BUY']
    sell_trades = [t for t in trades if t['type'] == 'SELL']
    print(f"  BUY : {len(buy_trades)} trades | Net: ${sum(t['pnl'] for t in buy_trades):.2f}")
    print(f"  SELL: {len(sell_trades)} trades | Net: ${sum(t['pnl'] for t in sell_trades):.2f}")
    
    # Check exit reasons
    reasons = {}
    for t in trades:
        r = t.get('exit_reason', 'UNKNOWN')
        reasons.setdefault(r, {'cnt': 0, 'pnl': 0.0})
        reasons[r]['cnt'] += 1
        reasons[r]['pnl'] += t['pnl']
    for r, v in reasons.items():
        print(f"  Exit {r:<25}: {v['cnt']:2} trades | PnL: ${v['pnl']:+7.2f}")
