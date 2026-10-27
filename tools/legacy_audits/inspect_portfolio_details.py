import json
from pathlib import Path

p = Path("reports/backtest_4month_all_symbols.json")
if not p.exists():
    print("Report does not exist")
    exit(0)

data = json.load(open(p))
print("=" * 80)
print(f"PORTFOLIO TOTAL: Net Profit: ${data['portfolio']['net_profit']:.2f} | Trades: {data['portfolio']['trades']} | WR: {data['portfolio']['win_rate_pct']:.1f}% | PF: {data['portfolio']['profit_factor']:.2f}")
print("=" * 80)

for sym, v in sorted(data['symbol_results'].items(), key=lambda x: x[1]['net_profit']):
    print(f"{sym:<10} | Trades: {v['total_trades']:4} | WR: {v['win_rate_pct']:5.1f}% | PF: {v['profit_factor']:4.2f} | Net: ${v['net_profit']:8.2f} | R: {v['total_r']:6.2f}")
