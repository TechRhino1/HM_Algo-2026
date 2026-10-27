import os
import sys
from pathlib import Path
REPO_ROOT = str(Path(__file__).resolve().parent.parent.parent)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
"""
HM Algo 2.0 — 3-Week Live MT5 Backtest (All Primary Symbols)
Downloads 3 weeks of real H1 data from MT5, runs event-driven backtest,
and produces a comprehensive performance report.
"""
import sys
import os
import json
import logging
logging.disable(logging.CRITICAL)

import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta

from jarvis.backtesting.engine import BacktestEngine
from jarvis.backtesting.metrics import PerformanceMetricsCalculator
from jarvis.data.symbol_registry import resolve as resolve_symbol
import MetaTrader5 as mt5


def resolve_broker_symbol(mt5_instance, canonical_name: str) -> str:
    """Resolve broker-specific symbol name across MT5 broker naming conventions."""
    if mt5_instance is None:
        return canonical_name

    u_name = canonical_name.upper()

    alias_map = {
        "XAUUSD": ["GOLD.i#", "GOLD#", "GOLD", "XAUUSD", "XAUUSD#", "XAUUSD.i#", "XAUUSD.i", "XAUUSDm", "GOLDm"],
        "GOLD": ["GOLD.i#", "GOLD#", "GOLD", "XAUUSD", "XAUUSD#", "XAUUSD.i#", "XAUUSD.i", "XAUUSDm", "GOLDm"],
        "BTCUSD": ["BTCUSD#", "BTCUSD", "BTCUSD.i#", "BTCUSD.i", "BTCUSDm", "BITCOIN"],
        "BTC": ["BTCUSD#", "BTCUSD", "BTCUSD.i#", "BTCUSD.i", "BTCUSDm", "BITCOIN"],
        "EURUSD": ["EURUSD", "EURUSD#", "EURUSD.i#", "EURUSD.i", "EURUSDm", "EURUSD.m"],
        "GBPUSD": ["GBPUSD", "GBPUSD#", "GBPUSD.i#", "GBPUSD.i", "GBPUSDm", "GBPUSD.m"],
        "USDJPY": ["USDJPY", "USDJPY#", "USDJPY.i#", "USDJPY.i", "USDJPYm", "USDJPY.m"],
    }

    candidates = [canonical_name]
    if u_name in alias_map:
        for a in alias_map[u_name]:
            if a not in candidates:
                candidates.append(a)
    else:
        for suffix in ["#", ".i#", ".i", "m", ".m"]:
            c = f"{canonical_name}{suffix}"
            if c not in candidates:
                candidates.append(c)

    for cand in candidates:
        try:
            info = mt5_instance.symbol_info(cand)
            if info is not None:
                mt5_instance.symbol_select(cand, True)
                return cand
        except Exception:
            pass

    try:
        all_syms = mt5_instance.symbols_get()
        if all_syms:
            matches = []
            for s in all_syms:
                s_name_u = s.name.upper()
                if u_name in ["XAUUSD", "GOLD"]:
                    if any(k in s_name_u for k in ["GOLD", "XAUUSD"]):
                        matches.append(s)
                elif u_name in ["BTCUSD", "BTC"]:
                    if any(k in s_name_u for k in ["BTCUSD", "BTC", "BITCOIN"]):
                        matches.append(s)
                elif u_name in s_name_u:
                    matches.append(s)
            if matches:
                matches.sort(key=lambda s: (
                    0 if getattr(s, "trade_mode", 0) == 4 else 1,
                    0 if "#" in s.name else 1,
                    len(s.name)
                ))
                best_match = matches[0].name
                mt5_instance.symbol_select(best_match, True)
                return best_match
    except Exception:
        pass

    return canonical_name


def run_3week_backtest():
    print("=" * 125)
    print("          HM Algo 2.0 — 3-WEEK REAL MT5 LIVE DATA BACKTEST (504 H1 BARS)")
    print("=" * 125)

    mt5_active = mt5.initialize()
    if not mt5_active:
        print(f"ERROR: MT5 failed to initialize: {mt5.last_error()}")
        return

    acc = mt5.account_info()
    if acc is not None:
        print(f"[MT5 Connected] Server: {acc.server} | Account #{acc.login} | Broker: {acc.company} | Leverage: 1:{acc.leverage}")
    else:
        print(f"[MT5 Connected] Account info unavailable: {mt5.last_error()}")
    print("-" * 125)

    symbol_configs = [
        {"name": "XAUUSD", "broker_sym": "GOLD.i#", "asset_class": "COMMODITY"},
        {"name": "BTCUSD", "broker_sym": "BTCUSD#", "asset_class": "CRYPTO"},
        {"name": "EURUSD", "broker_sym": "EURUSD",  "asset_class": "FOREX"},
        {"name": "GBPUSD", "broker_sym": "GBPUSD",  "asset_class": "FOREX"},
    ]

    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(weeks=3)

    all_symbol_results = {}
    all_trades = []
    total_final_balance = 0.0
    all_rejection_stats = {}

    print(f"Data Window: {start_time.strftime('%Y-%m-%d %H:%M')} -> {end_time.strftime('%Y-%m-%d %H:%M')} UTC")
    print(f"Expected H1 bars: ~504")
    print()

    for cfg in symbol_configs:
        sym = cfg["name"]
        preferred_sym = cfg.get("broker_sym", sym)
        broker_sym = resolve_broker_symbol(mt5, preferred_sym)
        mt5.symbol_select(broker_sym, True)

        rates = mt5.copy_rates_range(broker_sym, mt5.TIMEFRAME_H1, start_time, end_time)

        if rates is None or len(rates) < 50:
            if broker_sym != sym:
                broker_sym = resolve_broker_symbol(mt5, sym)
                mt5.symbol_select(broker_sym, True)
                rates = mt5.copy_rates_range(broker_sym, mt5.TIMEFRAME_H1, start_time, end_time)

            if rates is None or len(rates) < 50:
                print(f"  ERROR: Unable to fetch MT5 rates for {sym} [{broker_sym}] — {mt5.last_error()}")
                continue

        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        df.rename(columns={"tick_volume": "volume"}, inplace=True)
        df = df[["time", "open", "high", "low", "close", "volume"]].copy()

        t_start = df["time"].iloc[0].strftime("%Y-%m-%d %H:%M")
        t_end = df["time"].iloc[-1].strftime("%Y-%m-%d %H:%M")
        last_price = df["close"].iloc[-1]
        print(f"Loaded {len(df)} REAL MT5 H1 bars for {sym} [{broker_sym}] | {t_start} -> {t_end} | Price: {last_price}")

        spec = resolve_symbol(sym)
        engine = BacktestEngine(initial_balance=10000.0, risk_per_trade_pct=0.75, commission_per_lot=5.0)

        res = engine.run_backtest(df, symbol=sym, spread_pips=spec.typical_spread_pips)

        trades = res.get("trades", [])
        m = res.get("metrics", {})
        final_bal = res.get("final_balance", 10000.0)
        net_pnl = final_bal - 10000.0
        roi_pct = (net_pnl / 10000.0) * 100.0

        m["symbol"] = sym
        m["broker_sym"] = broker_sym
        m["asset_class"] = cfg["asset_class"]
        m["start_time"] = t_start
        m["end_time"] = t_end
        m["bar_count"] = len(df)
        m["final_balance"] = final_bal
        m["net_profit"] = net_pnl
        m["roi_pct"] = roi_pct
        m["total_trades"] = len(trades)
        m["rejections"] = res.get("rejection_stats", {})

        wins = [t for t in trades if t.get("is_win", False)]
        losses = [t for t in trades if not t.get("is_win", False)]
        m["win_count"] = len(wins)
        m["loss_count"] = len(losses)
        m["win_rate"] = (len(wins) / max(1, len(trades))) * 100.0 if trades else 0.0

        m["avg_win_dollar"] = (sum(t.get("pnl", 0.0) for t in wins) / len(wins)) if wins else 0.0
        m["avg_loss_dollar"] = (abs(sum(t.get("pnl", 0.0) for t in losses)) / len(losses)) if losses else 0.0
        m["realized_rr"] = (m["avg_win_dollar"] / m["avg_loss_dollar"]) if m["avg_loss_dollar"] > 0 else 0.0
        m["avg_planned_rr"] = (sum(t.get("planned_rr", 0.0) for t in trades) / len(trades)) if trades else 0.0
        m["avg_bars_held"] = (sum(t.get("bars_held", 1) for t in trades) / len(trades)) if trades else 0.0

        max_consec_wins = 0
        max_consec_losses = 0
        cur_wins = 0
        cur_losses = 0
        for t in trades:
            if t.get("is_win"):
                cur_wins += 1
                cur_losses = 0
                max_consec_wins = max(max_consec_wins, cur_wins)
            else:
                cur_losses += 1
                cur_wins = 0
                max_consec_losses = max(max_consec_losses, cur_losses)
        m["max_consec_wins"] = max_consec_wins
        m["max_consec_losses"] = max_consec_losses

        all_symbol_results[sym] = m
        total_final_balance += final_bal
        all_trades.extend(trades)

        for rk, rv in res.get("rejection_stats", {}).items():
            all_rejection_stats[rk] = all_rejection_stats.get(rk, 0) + rv

        print(f"  -> {len(trades)} trades | WR: {m['win_rate']:.1f}% | PF: {m.get('profit_factor', 0):.2f} | Net: ${net_pnl:+,.2f} | ROI: {roi_pct:+.2f}%")
        print()

    mt5.shutdown()

    if not all_symbol_results:
        print("ERROR: No symbol data was successfully retrieved from MT5.")
        return

    total_initial_balance = len(all_symbol_results) * 10000.0
    portfolio_pnl = total_final_balance - total_initial_balance
    portfolio_roi = (portfolio_pnl / total_initial_balance) * 100.0 if total_initial_balance > 0 else 0.0
    portfolio_metrics = PerformanceMetricsCalculator.calculate_metrics(all_trades, total_initial_balance)

    port_wins = [t for t in all_trades if t.get("is_win", False)]
    port_losses = [t for t in all_trades if not t.get("is_win", False)]
    avg_win_p = (sum(t.get("pnl", 0.0) for t in port_wins) / len(port_wins)) if port_wins else 0.0
    avg_loss_p = (abs(sum(t.get("pnl", 0.0) for t in port_losses)) / len(port_losses)) if port_losses else 0.0
    realized_rr_p = (avg_win_p / avg_loss_p) if avg_loss_p > 0 else 0.0
    avg_planned_rr_p = (sum(t.get("planned_rr", 0.0) for t in all_trades) / len(all_trades)) if all_trades else 0.0
    avg_bars_p = (sum(t.get("bars_held", 1) for t in all_trades) / len(all_trades)) if all_trades else 0.0

    p_wins = 0
    p_losses = 0
    p_max_cw = 0
    p_max_cl = 0
    all_trades_sorted = sorted(all_trades, key=lambda x: str(x.get("open_time", x.get("exit_time", ""))))
    for t in all_trades_sorted:
        if t.get("is_win"):
            p_wins += 1
            p_losses = 0
            p_max_cw = max(p_max_cw, p_wins)
        else:
            p_losses += 1
            p_wins = 0
            p_max_cl = max(p_max_cl, p_losses)

    print("\n" + "=" * 125)
    print("                            3-WEEK MULTI-ASSET PERFORMANCE SUMMARY")
    print("=" * 125)
    header = f"{'Metric':<32} | " + " | ".join(f"{sym:<13}" for sym in all_symbol_results.keys()) + f" | {'PORTFOLIO':<13}"
    print(header)
    print("-" * len(header))

    def fmt_curr(v): return f"${float(v):,.2f}"
    def fmt_signed(v): return f"${float(v):+,.2f}"
    def fmt_pct(v): return f"{float(v):.2f}%"

    rows = [
        ("Data Span", lambda m: f"{m['start_time'][:10]}", f"3 Weeks"),
        ("H1 Bars Simulated", lambda m: f"{m['bar_count']:,d}", f"{sum(m['bar_count'] for m in all_symbol_results.values()):,d}"),
        ("Initial Balance ($)", lambda m: "$10,000.00", fmt_curr(total_initial_balance)),
        ("Final Balance ($)", lambda m: fmt_curr(m['final_balance']), fmt_curr(total_final_balance)),
        ("Net Profit / Loss ($)", lambda m: fmt_signed(m['net_profit']), fmt_signed(portfolio_pnl)),
        ("3-Week ROI %", lambda m: f"{m['roi_pct']:+.2f}%", f"{portfolio_roi:+.2f}%"),
        ("Total Trades", lambda m: f"{m.get('total_trades', 0):d}", f"{len(all_trades):d}"),
        ("Winning Trades", lambda m: f"{m.get('win_count', 0):d}", f"{len(port_wins):d}"),
        ("Losing Trades", lambda m: f"{m.get('loss_count', 0):d}", f"{len(port_losses):d}"),
        ("Win Rate %", lambda m: fmt_pct(m.get('win_rate', 0.0)), fmt_pct((len(port_wins)/max(1, len(all_trades)))*100.0)),
        ("Profit Factor", lambda m: f"{m.get('profit_factor', 0.0):.2f}", f"{portfolio_metrics.get('profit_factor', 0.0):.2f}"),
        ("Expectancy ($)", lambda m: fmt_signed(m.get('expectancy_dollars', 0.0)), fmt_signed(portfolio_metrics.get('expectancy_dollars', 0.0))),
        ("Avg Win ($)", lambda m: fmt_curr(m.get('avg_win_dollar', 0.0)), fmt_curr(avg_win_p)),
        ("Avg Loss ($)", lambda m: fmt_curr(m.get('avg_loss_dollar', 0.0)), fmt_curr(avg_loss_p)),
        ("Realized R:R", lambda m: f"1:{m.get('realized_rr', 0.0):.2f}", f"1:{realized_rr_p:.2f}"),
        ("Avg Planned R:R", lambda m: f"1:{m.get('avg_planned_rr', 0.0):.2f}", f"1:{avg_planned_rr_p:.2f}"),
        ("Max Consec Wins", lambda m: f"{m.get('max_consec_wins', 0):d}", f"{p_max_cw:d}"),
        ("Max Consec Losses", lambda m: f"{m.get('max_consec_losses', 0):d}", f"{p_max_cl:d}"),
        ("Avg Hold (Hours)", lambda m: f"{m.get('avg_bars_held', 0.0):.1f}h", f"{avg_bars_p:.1f}h"),
        ("Max Drawdown ($)", lambda m: fmt_curr(m.get('max_drawdown_dollars', 0.0)), fmt_curr(portfolio_metrics.get('max_drawdown_dollars', 0.0))),
        ("Max Drawdown %", lambda m: fmt_pct(m.get('max_drawdown_pct', 0.0)), fmt_pct(portfolio_metrics.get('max_drawdown_pct', 0.0))),
        ("Sharpe Ratio", lambda m: f"{m.get('sharpe_ratio', 0.0):.2f}", f"{portfolio_metrics.get('sharpe_ratio', 0.0):.2f}"),
        ("Sortino Ratio", lambda m: f"{m.get('sortino_ratio', 0.0):.2f}", f"{portfolio_metrics.get('sortino_ratio', 0.0):.2f}"),
        ("Calmar Ratio", lambda m: f"{m.get('calmar_ratio', 0.0):.2f}", f"{portfolio_metrics.get('calmar_ratio', 0.0):.2f}"),
    ]

    for label, fn, p_val in rows:
        row_str = f"{label:<32} | " + " | ".join(f"{fn(all_symbol_results[sym]):<13}" for sym in all_symbol_results.keys()) + f" | {p_val:<13}"
        print(row_str)

    print("=" * len(header))

    # Strategy Breakdown
    if all_trades:
        print("\n" + "=" * 118)
        print("                            STRATEGY-BY-STRATEGY BREAKDOWN")
        print("=" * 118)
        strat_groups = {}
        for t in all_trades:
            st = t.get("strategy", "UNKNOWN")
            if st not in strat_groups:
                strat_groups[st] = []
            strat_groups[st].append(t)

        strat_header = f"{'Strategy':<28} | {'Trades':<8} | {'Wins':<6} | {'Losses':<6} | {'Win Rate':<10} | {'Gross Win':<11} | {'Gross Loss':<11} | {'Net PnL':<11} | {'PF':<6}"
        print(strat_header)
        print("-" * len(strat_header))

        for st, s_trades in sorted(strat_groups.items(), key=lambda x: len(x[1]), reverse=True):
            s_wins = [t for t in s_trades if t.get("is_win")]
            s_losses = [t for t in s_trades if not t.get("is_win")]
            s_wr = (len(s_wins) / max(1, len(s_trades))) * 100.0
            s_gross_win = sum(t.get("pnl", 0.0) for t in s_wins)
            s_gross_loss = abs(sum(t.get("pnl", 0.0) for t in s_losses))
            s_net = sum(t.get("pnl", 0.0) for t in s_trades)
            s_pf = (s_gross_win / s_gross_loss) if s_gross_loss > 0 else (99.0 if s_gross_win > 0 else 0.0)
            print(f"{st:<28} | {len(s_trades):<8d} | {len(s_wins):<6d} | {len(s_losses):<6d} | {s_wr:>8.2f}%  | ${s_gross_win:>10,.2f} | ${s_gross_loss:>10,.2f} | ${s_net:>10,.2f} | {s_pf:>6.2f}")
        print("=" * len(strat_header))

    # Regime Breakdown
    if all_trades:
        print("\n" + "=" * 118)
        print("                            PERFORMANCE BY MARKET REGIME")
        print("=" * 118)
        reg_groups = {}
        for t in all_trades:
            r = t.get("regime", "GLOBAL")
            if r not in reg_groups:
                reg_groups[r] = []
            reg_groups[r].append(t)

        reg_header = f"{'Regime':<25} | {'Trades':<8} | {'Wins':<6} | {'Losses':<6} | {'Win Rate':<10} | {'Net PnL':<13} | {'PF':<6}"
        print(reg_header)
        print("-" * len(reg_header))

        for r, r_trades in sorted(reg_groups.items(), key=lambda x: len(x[1]), reverse=True):
            r_wins = [t for t in r_trades if t.get("is_win")]
            r_net = sum(t.get("pnl", 0.0) for t in r_trades)
            r_wr = (len(r_wins) / max(1, len(r_trades))) * 100.0
            r_gw = sum(t.get("pnl", 0.0) for t in r_wins)
            r_gl = abs(sum(t.get("pnl", 0.0) for t in r_trades if not t.get("is_win")))
            r_pf = (r_gw / r_gl) if r_gl > 0 else (99.0 if r_gw > 0 else 0.0)
            print(f"{r:<25} | {len(r_trades):<8d} | {len(r_wins):<6d} | {len(r_trades)-len(r_wins):<6d} | {r_wr:>8.2f}%  | ${r_net:>12,.2f} | {r_pf:>6.2f}")
        print("=" * len(reg_header))

    # Exit Reason Distribution
    if all_trades:
        print("\n" + "=" * 118)
        print("                            EXIT REASON DISTRIBUTION")
        print("=" * 118)
        exit_groups = {}
        for t in all_trades:
            res_k = t.get("result", "UNKNOWN")
            if res_k not in exit_groups:
                exit_groups[res_k] = []
            exit_groups[res_k].append(t)

        exit_header = f"{'Exit Reason':<22} | {'Count':<8} | {'Share %':<10} | {'Wins':<6} | {'Win Rate':<10} | {'Net PnL':<13}"
        print(exit_header)
        print("-" * len(exit_header))

        for ex, e_trades in sorted(exit_groups.items(), key=lambda x: len(x[1]), reverse=True):
            e_wins = [t for t in e_trades if t.get("is_win")]
            e_net = sum(t.get("pnl", 0.0) for t in e_trades)
            e_share = (len(e_trades) / max(1, len(all_trades))) * 100.0
            e_wr = (len(e_wins) / max(1, len(e_trades))) * 100.0
            print(f"{ex:<22} | {len(e_trades):<8d} | {e_share:>8.1f}%  | {len(e_wins):<6d} | {e_wr:>8.2f}%  | ${e_net:>12,.2f}")
        print("=" * len(exit_header))

    # Quality Gate Rejections
    if all_rejection_stats:
        print("\n" + "=" * 118)
        print("                     TOP QUALITY GATE REJECTIONS")
        print("=" * 118)
        sorted_rejections = sorted(all_rejection_stats.items(), key=lambda x: x[1], reverse=True)
        for r_reason, count in sorted_rejections[:15]:
            print(f"  [Filtered x{count:>5d} times] {r_reason}")
        print()

    # Save JSON report
    report_dict = {
        "timestamp": datetime.now().isoformat(),
        "backtest_period": "3 weeks",
        "data_window": {"start": start_time.isoformat(), "end": end_time.isoformat()},
        "total_initial_balance": total_initial_balance,
        "total_final_balance": total_final_balance,
        "net_profit": portfolio_pnl,
        "roi_pct": portfolio_roi,
        "portfolio_metrics": portfolio_metrics,
        "symbols": all_symbol_results,
        "strategies": {st: {"trades": len(tr), "net_pnl": sum(t.get("pnl", 0.0) for t in tr), "win_rate": sum(1 for t in tr if t.get("is_win", False))/max(1, len(tr))} for st, tr in strat_groups.items()} if all_trades else {},
        "regimes": {r: {"trades": len(tr), "net_pnl": sum(t.get("pnl", 0.0) for t in tr), "win_rate": sum(1 for t in tr if t.get("is_win", False))/max(1, len(tr))} for r, tr in reg_groups.items()} if all_trades else {},
        "exits": {ex: {"trades": len(tr), "net_pnl": sum(t.get("pnl", 0.0) for t in tr), "win_rate": sum(1 for t in tr if t.get("is_win", False))/max(1, len(tr))} for ex, tr in exit_groups.items()} if all_trades else {},
        "rejections": all_rejection_stats,
        "all_trades": all_trades
    }
    report_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_3week_mt5_report.json")
    with open(report_path, "w") as f:
        json.dump(report_dict, f, default=str, indent=2)
    print(f"\n[Report Saved] {report_path}")
    print("=" * 125)


if __name__ == "__main__":
    run_3week_backtest()
