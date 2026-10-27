import sys
from pathlib import Path
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pandas as pd
import numpy as np
from jarvis.backtesting.engine import BacktestEngine
from jarvis.config.runtime import offline_mode
from jarvis.data.symbol_registry import resolve as resolve_symbol

# We will test on XAUUSD first
sym = "XAUUSD"
p = f"data/market/real/{sym}/{sym}_H1_122d.parquet"
df = pd.read_parquet(p)

with offline_mode():
    engine = BacktestEngine(initial_balance=10000.0, risk_per_trade_pct=0.5)
    res_orig = engine.run_backtest(df_h1=df, symbol=sym, spread_pips=2.0)
    print("ORIGINAL XAUUSD:")
    print("Net Profit:", res_orig["metrics"]["net_profit"])
    print("Win Rate:", res_orig["metrics"]["win_rate_pct"])
    print("Trades:", len(res_orig["trades"]))
    reasons = {}
    for t in res_orig["trades"]:
        r = t.get("exit_reason", t.get("result", "UNKNOWN"))
        reasons[r] = reasons.get(r, 0) + 1
    print("Exit reasons:", reasons)
