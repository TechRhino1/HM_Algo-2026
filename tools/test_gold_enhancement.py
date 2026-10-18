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
    m = res['metrics']
    print(f"Current Gold: Trades={len(res['trades'])} | Net=${m['net_profit']:.2f} | WR={m['win_rate_pct']:.1f}% | PF={m['profit_factor']:.2f}")
