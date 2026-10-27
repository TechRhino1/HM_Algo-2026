import sys
from pathlib import Path
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pandas as pd
from jarvis.backtesting.engine import BacktestEngine
from jarvis.config.runtime import offline_mode

with offline_mode():
    df = pd.read_parquet(REPO_ROOT / 'data/market/real/WTI/WTI_H1_122d.parquet')
    engine = BacktestEngine(initial_balance=10000.0, risk_per_trade_pct=0.5)
    res = engine.run_backtest(df_h1=df, symbol='WTI', spread_pips=13.0)
    trades = res['trades']
    print(f"WTI total trades: {len(trades)}")
    buys = [t for t in trades if t['type'] == 'BUY']
    sells = [t for t in trades if t['type'] == 'SELL']
    b_wins = sum(1 for t in buys if t['pnl'] > 0)
    s_wins = sum(1 for t in sells if t['pnl'] > 0)
    b_pnl = sum(t['pnl'] for t in buys)
    s_pnl = sum(t['pnl'] for t in sells)
    print(f"BUY trades: {len(buys)}, Wins: {b_wins}, PnL: ${b_pnl:.2f}")
    print(f"SELL trades: {len(sells)}, Wins: {s_wins}, PnL: ${s_pnl:.2f}")
    strats = {}
    for t in trades:
        s = t.get('strategy', 'UNKNOWN')
        strats.setdefault(s, {'count': 0, 'wins': 0, 'pnl': 0.0})
        strats[s]['count'] += 1
        if t['pnl'] > 0:
            strats[s]['wins'] += 1
        strats[s]['pnl'] += t['pnl']
    for s, v in strats.items():
        print(f"{s}: {v['count']} trades, {v['wins']} wins, PnL: ${v['pnl']:.2f}")
