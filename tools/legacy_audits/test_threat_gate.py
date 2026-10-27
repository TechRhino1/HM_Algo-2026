import sys
from pathlib import Path
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pandas as pd
from jarvis.backtesting.engine import BacktestEngine
from jarvis.config.runtime import offline_mode
from jarvis.data.symbol_registry import resolve as resolve_symbol

sym = "XAUUSD"
p = REPO_ROOT / "data" / "market" / "real" / sym / f"{sym}_H1_122d.parquet"
df = pd.read_parquet(p)

with offline_mode():
    engine = BacktestEngine(initial_balance=10000.0, risk_per_trade_pct=0.5)
    
    orig_eval = engine.decision_engine.evaluate
    def patched_eval(context, regime, analyst_reports, devil_report, *args, **kwargs):
        dec = orig_eval(context, regime, analyst_reports, devil_report, *args, **kwargs)
        if dec.decision == "EXECUTE" and getattr(dec, "sl_distance", 0) > 0:
            threat_lvl = getattr(devil_report, "threat_price_level", None) if devil_report else None
            entry_p = dec.entry_price
            rd = dec.sl_distance
            
            # Check Devil threat level
            if threat_lvl is not None and threat_lvl > 0:
                if dec.bias == "BUY" and entry_p < threat_lvl < dec.take_profit:
                    tuck_dist = threat_lvl - (rd * 0.1) - entry_p
                    tuck_r = tuck_dist / rd
                    if tuck_r < 1.40:
                        # Threat wall is too close! Reject trade to prevent buying into resistance
                        dec.decision = "HOLD"
                        dec.quality_gate.passed = False
                        dec.quality_gate.failing_reasons.append(f"Devil Threat Wall Too Close ({tuck_r:.2f}R < 1.40R)")
                        return dec
                    else:
                        dec.take_profit = round(entry_p + tuck_dist, 2)
                elif dec.bias == "SELL" and dec.take_profit < threat_lvl < entry_p:
                    tuck_dist = entry_p - (threat_lvl + (rd * 0.1))
                    tuck_r = tuck_dist / rd
                    if tuck_r < 1.40:
                        dec.decision = "HOLD"
                        dec.quality_gate.passed = False
                        dec.quality_gate.failing_reasons.append(f"Devil Threat Wall Too Close ({tuck_r:.2f}R < 1.40R)")
                        return dec
                    else:
                        dec.take_profit = round(entry_p - tuck_dist, 2)
            
            # Cap realistic target at 2.4R if no intermediate target
            if dec.bias == "BUY":
                dec.take_profit = min(dec.take_profit, round(entry_p + (2.4 * rd), 2))
            elif dec.bias == "SELL":
                dec.take_profit = max(dec.take_profit, round(entry_p - (2.4 * rd), 2))
        return dec

    engine.decision_engine.evaluate = patched_eval
    res = engine.run_backtest(df_h1=df, symbol=sym, spread_pips=2.0)
    print("=== PATCHED XAUUSD PERFORMANCE ===")
    print("Net Profit: $", res["metrics"]["net_profit"])
    print("Win Rate:", res["metrics"]["win_rate_pct"], "%")
    print("Profit Factor:", res["metrics"]["profit_factor"])
    print("Total Trades:", len(res["trades"]))
    reasons = {}
    for t in res["trades"]:
        r = t.get("exit_reason", t.get("result", "UNKNOWN"))
        reasons[r] = reasons.get(r, 0) + 1
    print("Exit breakdown:", reasons)
