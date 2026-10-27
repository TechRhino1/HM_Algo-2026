import sys
from pathlib import Path
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jarvis.backtesting.engine import BacktestEngine
from jarvis.config.runtime import offline_mode

df = pd.read_parquet(REPO_ROOT / 'data/market/real/US500/US500_H1_122d.parquet')

with offline_mode():
    engine = BacktestEngine(initial_balance=10000.0, risk_per_trade_pct=0.5)
    res = engine.run_backtest(df_h1=df, symbol='US500', spread_pips=0.6)
    trades = res['trades']
    m = res['metrics']
    print(f"US500: {len(trades)} trades | Net: ${m['net_profit']:.2f} | WR: {m['win_rate_pct']:.1f}% | PF: {m['profit_factor']:.2f}")

    strats = {}
    for i, t in enumerate(trades, 1):
        s = t.get('strategy', 'UNKNOWN')
        strats.setdefault(s, {'cnt': 0, 'wins': 0, 'pnl': 0.0})
        strats[s]['cnt'] += 1
        if t['pnl'] > 0:
            strats[s]['wins'] += 1
        strats[s]['pnl'] += t['pnl']
        print(f"  #{i:2}: {t['type']} {s:<26} Entry={t['entry']:.2f} Exit={t['exit']:.2f} PnL=${t['pnl']:+6.2f} ExitReason={t.get('exit_reason')}")

    print("\nStrategy Breakdown:")
    for s, v in strats.items():
        print(f"  {s:<28}: {v['cnt']:2} trades | Wins: {v['wins']:2} | PnL: ${v['pnl']:+7.2f}")
