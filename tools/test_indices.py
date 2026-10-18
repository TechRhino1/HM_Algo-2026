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
    p = REPO_ROOT / "data" / "market" / "real" / sym / f"{sym}_H1_122d.parquet"
    if not p.exists():
        continue
    df = pd.read_parquet(p)
    spec = resolve_symbol(sym)
    spread = getattr(spec, "typical_spread_pips", 1.0) or 1.0
    
    cfg = SYMBOL_PROFILES[sym]
    orig_banned = list(cfg.banned_strategies)
    orig_weights = dict(cfg.strategy_weights)
    
    with offline_mode():
        engine = BacktestEngine(initial_balance=10000.0, risk_per_trade_pct=0.5)
        res_orig = engine.run_backtest(df_h1=df, symbol=sym, spread_pips=spread)
        m_orig = res_orig['metrics']
        
        # Test calibrated profile
        cfg.banned_strategies = ["TREND_FOLLOWING"]
        cfg.strategy_weights = {
            "LIQUIDITY_SWEEP_REVERSAL": 3.2,
            "CHOCH_STRUCTURAL_REVERSAL": 2.8,
            "RANGE_MEAN_REVERSION": 2.2,
            "BREAKOUT_EXPANSION": 0.8,
            "TREND_PULLBACK": 1.2,
            "TREND_FOLLOWING": 0.0,
        }
        res_cal = engine.run_backtest(df_h1=df, symbol=sym, spread_pips=spread)
        m_cal = res_cal['metrics']
        
        print(f"{sym:<7} | Orig: Net=${m_orig['net_profit']:<+8.2f} (WR={m_orig['win_rate_pct']:.1f}%, T={len(res_orig['trades'])}) -> Calibrated: Net=${m_cal['net_profit']:<+8.2f} (WR={m_cal['win_rate_pct']:.1f}%, T={len(res_cal['trades'])})")
        
    cfg.banned_strategies = orig_banned
    cfg.strategy_weights = orig_weights
