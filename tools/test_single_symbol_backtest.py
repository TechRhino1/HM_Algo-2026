import time
import os
import sys
from pathlib import Path
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pandas as pd
from jarvis.backtesting.engine import BacktestEngine
from jarvis.data.symbol_registry import resolve as resolve_symbol

def test_symbol(sym="EURUSD"):
    path = f"data/market/real/{sym}/{sym}_H1_122d.parquet"
    df = pd.read_parquet(path)
    print(f"Loaded {sym}: {len(df)} bars from {df['time'].iloc[0]} to {df['time'].iloc[-1]}")
    
    spec = resolve_symbol(sym)
    spread = getattr(spec, "typical_spread_pips", 1.0) or 1.0
    
    t0 = time.time()
    engine = BacktestEngine(initial_balance=10000.0, risk_per_trade_pct=0.5)
    res = engine.run_backtest(df_h1=df, symbol=sym, spread_pips=spread)
    dur = time.time() - t0
    
    trades = res.get("trades", [])
    m = res.get("metrics", {})
    print(f"[{sym}] Finished in {dur:.2f}s:")
    print(f"  Total Trades: {len(trades)}")
    print(f"  Win Rate:     {m.get('win_rate_pct', 0.0):.2f}%")
    print(f"  Profit Factor:{m.get('profit_factor', 0.0):.2f}")
    print(f"  Net Profit:   ${m.get('net_profit', 0.0):+,.2f}")
    print(f"  Max Drawdown: {m.get('max_drawdown_pct', 0.0):.2f}%")
    if trades:
        print(f"  First trade: {trades[0].get('type')} @ {trades[0].get('entry')} -> PnL: ${trades[0].get('pnl', 0.0):.2f}")
        print(f"  Last trade:  {trades[-1].get('type')} @ {trades[-1].get('entry')} -> PnL: ${trades[-1].get('pnl', 0.0):.2f}")

if __name__ == "__main__":
    test_symbol("EURUSD")
