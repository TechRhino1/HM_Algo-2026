"""
Diagnostic tool to inspect detailed trade lifecycle and rejections for selected symbols
"""
import sys
from pathlib import Path
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jarvis.backtesting.engine import BacktestEngine
from jarvis.data.symbol_registry import resolve as resolve_symbol
from jarvis.config.runtime import offline_mode

REAL_DIR = REPO_ROOT / "data" / "market" / "real"

TARGETS = ["XAUUSD", "XAGUSD", "BTCUSD", "ETHUSD", "US500", "GER40"]

with offline_mode():
    for sym in TARGETS:
        p122 = REAL_DIR / sym / f"{sym}_H1_122d.parquet"
        if not p122.exists():
            continue
        df = pd.read_parquet(p122).reset_index(drop=True)
        spec = resolve_symbol(sym)
        spread = getattr(spec, "typical_spread_pips", 1.0) or 1.0
        
        print("=" * 100)
        print(f"DIAGNOSTIC FOR {sym} (Bars: {len(df)})")
        print("=" * 100)
        
        engine = BacktestEngine(initial_balance=10_000.0, risk_per_trade_pct=0.5)
        res = engine.run_backtest(df_h1=df, symbol=sym, spread_pips=spread)
        trades = res.get("trades", [])
        rejections = res.get("rejection_stats", {})
        
        print(f"Total Trades: {len(trades)}")
        print("Top Rejections:")
        for r, cnt in sorted(rejections.items(), key=lambda x: x[1], reverse=True)[:10]:
            print(f"  - {r}: {cnt}")
            
        print("\nTrades Details:")
        for i, t in enumerate(trades, 1):
            side = t.get("type")
            entry = t.get("entry")
            exit_p = t.get("exit")
            sl = t.get("initial_sl", t.get("sl"))
            tp = t.get("tp")
            pnl = t.get("pnl")
            lots = t.get("lots")
            result = t.get("result") or t.get("exit_reason")
            bars = t.get("bars_held")
            open_t = t.get("open_time")
            exit_t = t.get("exit_time")
            print(f"  Trade #{i}: {side} @ {entry:.2f} | Lots: {lots:.3f} | SL: {sl:.2f} | TP: {tp:.2f} | Exit: {exit_p:.2f} ({result}) | Bars: {bars} | PnL: ${pnl:.2f} | Open: {open_t} -> Exit: {exit_t}")
        print()
