import os
import sys
from pathlib import Path
import pandas as pd
import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jarvis.backtesting.engine import BacktestEngine
from jarvis.config.runtime import offline_mode
from jarvis.data.symbol_registry import resolve as resolve_symbol

print("--- TESTING PROFIT LEVERS ON REAL MT5 DATA ---")

for sym in ["XAUUSD", "WTI", "EURUSD", "NAS100"]:
    p = REPO_ROOT / "data" / "market" / "real" / sym / f"{sym}_H1_122d.parquet"
    if not p.exists():
        continue
    df = pd.read_parquet(p)
    spec = resolve_symbol(sym)
    spread = getattr(spec, "typical_spread_pips", 1.0) or 1.0

    with offline_mode():
        engine = BacktestEngine(initial_balance=10000.0, risk_per_trade_pct=0.5)
        res = engine.run_backtest(df_h1=df, symbol=sym, spread_pips=spread)
        m = res["metrics"]
        print(f"BASELINE {sym:<7}: PnL=${m['net_profit']:<+9.2f} | WR={m['win_rate_pct']:<5.1f}% | PF={m['profit_factor']:<4.2f} | Trades={len(res['trades'])}")
