import sys, os, time
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from jarvis.application.orchestrator import JarvisOrchestrator, _resolve_sym

orc = JarvisOrchestrator(mode="PAPER", trade_style="ALL")
best_op, ranked, raw = orc.scan_all_modes(dry_run=True)

candidate = None
for c in ranked:
    if c.symbol == "NZDUSD" and c.trade_style == "SCALP":
        candidate = c
        break

if not candidate:
    print("No NZDUSD SCALP candidate found!")
    sys.exit(0)

print(f"Found candidate: {candidate.symbol} ({candidate.trade_style})")
decision = candidate.decision_obj
sym = candidate.symbol
canonical_sym = sym.upper().replace("/", "").replace("_", "").replace("-", "")

account = orc.state_manager.account or orc.mt5_client.get_account_snapshot()
print(f"Account: equity={getattr(account, 'equity', None)}, balance={getattr(account, 'balance', None)}")

positions = orc.state_manager.positions
print(f"Open positions count: {len(positions)}")
for p in positions:
    print(f"  Pos: {p.symbol} {p.type} lots={p.volume} profit={p.profit}")

_spec = _resolve_sym(sym)
sym_info = orc.mt5_client.get_symbol_trading_spec(sym) if hasattr(orc.mt5_client, "get_symbol_trading_spec") else {}
print(f"sym_info: {sym_info}")

ctx = candidate.context
cur_spread = ctx.volatility.current_spread_pips if ctx and hasattr(ctx, "volatility") else _spec.typical_spread_pips
print(f"Spread: live={cur_spread}, max={_spec.max_spread_pips}")

all_pending = orc.mt5_client.get_pending_orders() if hasattr(orc.mt5_client, "get_pending_orders") else []
print(f"Pending orders: {len(all_pending)}")

# Test unsplittable book check
_vol_min = float(sym_info.get("volume_min", 0.01) or 0.01)
_equity = float(getattr(account, "equity", 0.0) or 0.0)
_entry_px = float(getattr(decision, "price", 0.0) or getattr(decision, "entry_price", 0.0) or 0.0)
_sl_px = float(getattr(decision, "stop_loss", 0.0) or 0.0)
is_unsplit = orc._is_unsplittable_book(_entry_px, _sl_px, _equity, _spec, _vol_min)
print(f"is_unsplittable_book: {is_unsplit}, RR: {decision.risk_reward_ratio}")

# Test risk authorization
print("Calling risk_engine.authorize_execution...")
auth_res = orc.risk_engine.authorize_execution(
    decision, account, positions, sym_info,
    current_spread_pips=cur_spread,
    max_allowed_spread_pips=_spec.max_spread_pips,
    context=ctx,
    is_second_trade=False,
    pending_orders=all_pending,
    entry_authorized_override=True
)
print(f"Auth result: {auth_res}")
