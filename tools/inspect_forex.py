import sys
from pathlib import Path
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jarvis.backtesting.engine import BacktestEngine
from jarvis.config.runtime import offline_mode
from jarvis.data.symbol_registry import resolve as resolve_symbol

for sym in ["USDJPY", "GBPUSD", "USDCHF"]:
    df = pd.read_parquet(REPO_ROOT / f'data/market/real/{sym}/{sym}_H1_122d.parquet')
    spec = resolve_symbol(sym)
    with offline_mode():
        engine = BacktestEngine(initial_balance=10000.0, risk_per_trade_pct=0.5)
        res = engine.run_backtest(df_h1=df, symbol=sym, spread_pips=spec.typical_spread_pips)
        m = res['metrics']
        print(f"=== {sym} ===")
        print(f"Trades: {len(res['trades'])} | Net: ${m['net_profit']:.2f} | WR: {m['win_rate_pct']:.1f}% | PF: {m['profit_factor']:.2f}")
        strats = {}
        for t in res['trades']:
            s = t.get('strategy', 'UNKNOWN')
            strats.setdefault(s, {'cnt': 0, 'wins': 0, 'pnl': 0.0})
            strats[s]['cnt'] += 1
            if t['pnl'] > 0:
                strats[s]['wins'] += 1
            strats[s]['pnl'] += t['pnl']
        for s, v in strats.items():
            print(f"  {s:<28}: {v['cnt']:2} trades | Wins: {v['wins']:2} | PnL: ${v['pnl']:+7.2f}")
