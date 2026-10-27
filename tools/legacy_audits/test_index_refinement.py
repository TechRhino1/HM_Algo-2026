import sys
from pathlib import Path
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jarvis.backtesting.engine import BacktestEngine
from jarvis.config.runtime import offline_mode
from jarvis.intelligence.symbol_profile_config import SYMBOL_PROFILES
from jarvis.data.symbol_registry import resolve as resolve_symbol

for sym in ["US500", "GER40", "UK100"]:
    cfg = SYMBOL_PROFILES[sym]
    cfg.banned_strategies = ["BREAKOUT_EXPANSION", "TREND_FOLLOWING", "TREND_PULLBACK"]
    cfg.strategy_weights = {
        "LIQUIDITY_SWEEP_REVERSAL": 3.5,
        "CHOCH_STRUCTURAL_REVERSAL": 3.0,
        "RANGE_MEAN_REVERSION": 2.5,
        "TREND_PULLBACK": 0.0,
        "TREND_FOLLOWING": 0.0,
        "BREAKOUT_EXPANSION": 0.0,
    }
    df = pd.read_parquet(f"data/market/real/{sym}/{sym}_H1_122d.parquet")
    spec = resolve_symbol(sym)
    with offline_mode():
        res = BacktestEngine(10000.0, 0.5).run_backtest(df, sym, spec.typical_spread_pips)
        m = res["metrics"]
        print(f"{sym}: {len(res['trades'])} trades | Net: ${m['net_profit']:.2f} | WR: {m['win_rate_pct']:.1f}% | PF: {m['profit_factor']:.2f}")
