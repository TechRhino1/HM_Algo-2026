"""Run the style-aware 6-month backtest on real MT5 data, per trading mode.

The three modes differ only by the role→timeframe map (see
``jarvis.market.data_feed.STYLE_TIMEFRAMES``), so each mode is run over its own
primary series with real per-role frames supplied to the engine:

    SWING        primary H1   -> D1/H4/H1/H4/M15     (full ~180d window)
    DAY_TRADING  primary M15  -> H4/H1/M15/H1/M5     (full ~180d window)
    SCALP        primary M5   -> H1/M15/M5/M5/M1     (M1-capped, ~67d)

SCALP's window is truncated to the span where its *timing* timeframe (M1)
actually exists. Letting the M5 loop run past that would silently feed an empty
M1 frame into the context builder, which degrades to a NEUTRAL timing bias
rather than erroring — a quiet falsification of the mode under test.

Run:  python tools/run_mode_backtest.py --workers 4
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Any, Dict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(name)s: %(message)s")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL_DIR = os.path.join(REPO, "data", "market", "real")
OUT_DIR = os.path.join(REPO, "reports", "mode_backtest")
PROFILE_PATH = os.path.join(REPO, "config", "winrate_profiles.json")

ALL_TFS = ["M1", "M5", "M15", "H1", "H4", "D1"]
STYLES = ["SWING", "DAY_TRADING", "SCALP"]


def load_frames(symbol: str, days: int = 183) -> Dict[str, Any]:
    import pandas as pd

    out = {}
    for tf in ALL_TFS:
        p = os.path.join(REAL_DIR, symbol, f"{symbol}_{tf}_{days}d.parquet")
        if os.path.exists(p):
            df = pd.read_parquet(p)
            df["time"] = pd.to_datetime(df["time"], utc=True).dt.tz_localize(None)
            out[tf] = df
    return out


def style_window(frames: Dict[str, Any], style: str):
    """(start, end) over which EVERY timeframe the style needs has data."""
    from jarvis.market.data_feed import style_timeframe_set

    need = style_timeframe_set(style)
    have = [tf for tf in need if tf in frames]
    if len(have) < len(need):
        missing = sorted(need - set(have))
        return None, None, missing
    start = max(frames[tf]["time"].iloc[0] for tf in need)
    end = min(frames[tf]["time"].iloc[-1] for tf in need)
    return start, end, []


def run_one(args: Dict[str, Any]) -> Dict[str, Any]:
    """Backtest one (symbol, style) pair. Must be top-level for pickling."""
    symbol = args["symbol"]
    style = args["style"]
    days = args["days"]
    use_profile = args["profile"]
    try:
        from jarvis.backtesting.engine import BacktestEngine
        from jarvis.data.symbol_registry import resolve
        from jarvis.market.data_feed import style_timeframes
        from jarvis.intelligence.winrate_targeting import WRProfileStore

        frames = load_frames(symbol, days)
        if not frames:
            return {"symbol": symbol, "style": style, "error": "no cached frames"}

        start, end, missing = style_window(frames, style)
        if start is None:
            return {"symbol": symbol, "style": style,
                    "error": f"missing timeframes: {missing}"}

        roles = style_timeframes(style)
        primary = roles["primary"]
        df = frames[primary]
        df = df[(df["time"] >= start) & (df["time"] <= end)].reset_index(drop=True)
        if len(df) < 150:
            return {"symbol": symbol, "style": style,
                    "error": f"only {len(df)} primary bars in window"}

        prof = None
        if use_profile:
            store = WRProfileStore(PROFILE_PATH)
            prof = store.load().get(symbol)

        spec = resolve(symbol)
        t0 = time.time()
        res = BacktestEngine(initial_balance=10000.0, risk_per_trade_pct=0.5).run_backtest(
            df_h1=df,
            symbol=symbol,
            spread_pips=float(spec.typical_spread_pips),
            start_bar_idx=100,
            timeframe=primary,
            wr_profile=prof,
            trade_style=style,
            mtf_source=frames,
        )
        elapsed = time.time() - t0

        trades = res.get("trades", [])
        metrics = res.get("metrics", {}) or {}

        # R-multiple per trade, measured against the ORIGINAL stop. The trailing
        # stop moves ``sl`` during the trade, so using it would shrink the
        # denominator and flatter late exits; ``initial_sl`` is the risk actually
        # taken at entry.
        from jarvis.data.symbol_registry import get_dollar_risk_per_price_unit
        mpu = get_dollar_risk_per_price_unit(symbol)
        for t in trades:
            try:
                entry = float(t.get("entry", 0.0) or 0.0)
                init_sl = float(t.get("initial_sl", 0.0) or 0.0)
                lots = float(t.get("lots", 0.0) or 0.0)
                risk_dollars = abs(entry - init_sl) * lots * mpu
                t["risk_dollars"] = round(risk_dollars, 4)
                t["pnl_r"] = round(float(t.get("pnl", 0.0)) / risk_dollars, 4) if risk_dollars > 0 else 0.0
            except Exception:
                t["pnl_r"] = 0.0

        rs = [t.get("pnl_r", 0.0) for t in trades]
        wins_r = [r for r in rs if r > 0]
        losses_r = [r for r in rs if r < 0]
        r_stats = {
            "total_r": round(sum(rs), 4),
            "expectancy_r": round(sum(rs) / len(rs), 4) if rs else 0.0,
            "avg_win_r": round(sum(wins_r) / len(wins_r), 4) if wins_r else 0.0,
            "avg_loss_r": round(sum(losses_r) / len(losses_r), 4) if losses_r else 0.0,
            "payoff_r": round((sum(wins_r) / len(wins_r)) / abs(sum(losses_r) / len(losses_r)), 4)
            if wins_r and losses_r else 0.0,
        }
        metrics.update(r_stats)

        return {
            "symbol": symbol,
            "style": style,
            "primary_tf": primary,
            "window_start": str(start),
            "window_end": str(end),
            "window_days": round((end - start).total_seconds() / 86400, 1),
            "primary_bars": int(len(df)),
            "profile": "deployed" if prof is not None else "none",
            "elapsed_sec": round(elapsed, 1),
            "n_trades": len(trades),
            "metrics": metrics,
            "trades": trades,
        }
    except Exception as e:  # pragma: no cover
        return {"symbol": symbol, "style": style,
                "error": f"{type(e).__name__}: {e}",
                "trace": traceback.format_exc()[-1200:]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--days", type=int, default=183)
    ap.add_argument("--symbols", default="")
    ap.add_argument("--styles", default=",".join(STYLES))
    ap.add_argument("--profile", action="store_true",
                    help="use the deployed per-symbol calibrated profile")
    ap.add_argument("--out", default=OUT_DIR)
    args = ap.parse_args()

    from jarvis.data.symbol_registry import all_symbols

    symbols = [s.canonical for s in all_symbols()]
    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    styles = [s.strip() for s in args.styles.split(",") if s.strip()]

    os.makedirs(args.out, exist_ok=True)
    jobs = [{"symbol": s, "style": st, "days": args.days, "profile": args.profile}
            for st in styles for s in symbols]
    print(f"{len(jobs)} jobs ({len(symbols)} symbols x {len(styles)} styles), "
          f"{args.workers} workers", flush=True)

    done = 0
    t_start = time.time()
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(run_one, j): j for j in jobs}
        for fut in as_completed(futs):
            j = futs[fut]
            r = fut.result()
            done += 1
            path = os.path.join(args.out, f"{j['symbol']}__{j['style']}.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(r, fh, indent=2, default=str)
            if "error" in r:
                print(f"[{done}/{len(jobs)}] FAIL {j['symbol']:8s} {j['style']:12s} {r['error'][:80]}", flush=True)
            else:
                m = r["metrics"]
                print(f"[{done}/{len(jobs)}] {j['symbol']:8s} {j['style']:12s} "
                      f"bars={r['primary_bars']:6d} win={r['window_days']:5.1f}d "
                      f"trades={r['n_trades']:4d} "
                      f"expR={m.get('expectancy_r', 0.0):+.3f} "
                      f"wr={m.get('win_rate_pct', 0.0):5.1f}% "
                      f"pf={m.get('profit_factor', 0.0):.2f} ({r['elapsed_sec']}s)", flush=True)

    print(f"\nfinished {done} jobs in {(time.time()-t_start)/60:.1f} min -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
