import sys
from pathlib import Path
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jarvis.backtesting.engine import BacktestEngine
from jarvis.config.runtime import offline_mode

df = pd.read_parquet(REPO_ROOT / 'data/market/real/XAUUSD/XAUUSD_H1_122d.parquet')

with offline_mode():
    engine = BacktestEngine(initial_balance=10000.0, risk_per_trade_pct=0.5)
    res = engine.run_backtest(df_h1=df, symbol='XAUUSD', spread_pips=2.0)
    trades = res['trades']
    m = res['metrics']
    print(f"XAUUSD Total Trades: {len(trades)} | Net: ${m['net_profit']:.2f} | WR: {m['win_rate_pct']:.1f}% | PF: {m['profit_factor']:.2f}")
    
    buys = [t for t in trades if t['type'] == 'BUY']
    sells = [t for t in trades if t['type'] == 'SELL']
    print(f"BUY: {len(buys)} trades, Wins: {sum(1 for t in buys if t['pnl']>0)}, PnL: ${sum(t['pnl'] for t in buys):.2f}")
    print(f"SELL: {len(sells)} trades, Wins: {sum(1 for t in sells if t['pnl']>0)}, PnL: ${sum(t['pnl'] for t in sells):.2f}")
    
    strats = {}
    for t in trades:
        s = t.get('strategy', 'UNKNOWN')
        strats.setdefault(s, {'cnt': 0, 'wins': 0, 'pnl': 0.0})
        strats[s]['cnt'] += 1
        if t['pnl'] > 0:
            strats[s]['wins'] += 1
        strats[s]['pnl'] += t['pnl']
        
    for s, v in strats.items():
        print(f"  {s:<30}: {v['cnt']:3} trades | Wins: {v['wins']:3} | PnL: ${v['pnl']:8.2f}")
