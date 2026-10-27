import MetaTrader5 as mt5
import pandas as pd
from datetime import datetime, timezone
import sys, os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

if not mt5.initialize():
    print("MT5 initialization failed!")
    sys.exit(1)

tick = mt5.symbol_info_tick("GOLD.i#")
sym_info = mt5.symbol_info("GOLD.i#")
spread = (tick.ask - tick.bid) if tick else 0

print("=" * 75)
print("               XAUUSD (GOLD) INSTITUTIONAL STATUS AUDIT")
print("=" * 75)
print(f"Server / Broker  : {sym_info.path if sym_info else 'XM'}")
print(f"Time (UTC)       : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
print(f"Time (Local IST) : {datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')}")
print(f"Live Price       : Bid ${tick.bid:.2f} | Ask ${tick.ask:.2f}")
print(f"Live Spread      : ${spread:.2f} ({spread*100:.1f} pts / {spread/0.1:.1f} pips)")

rates_h1 = mt5.copy_rates_from_pos("GOLD.i#", mt5.TIMEFRAME_H1, 0, 8)
if rates_h1 is not None and len(rates_h1) > 0:
    df_h1 = pd.DataFrame(rates_h1)
    df_h1["time_dt"] = pd.to_datetime(df_h1["time"], unit="s")
    print("\n--- Recent H1 Candle Price Action ---")
    for _, r in df_h1.iterrows():
        print(f"  {r['time_dt'].strftime('%Y-%m-%d %H:%M')} | O: {r['open']:8.2f} | H: {r['high']:8.2f} | L: {r['low']:8.2f} | C: {r['close']:8.2f} | Vol: {r['tick_volume']:5d}")

from jarvis.application.orchestrator import JarvisOrchestrator

orc = JarvisOrchestrator(mode="DEMO", trade_style="ALL")

print("\n--- Decision Engine & Multi-Style Analysis (Live Production) ---")
for style in ["SCALP", "DAY_TRADING", "SWING"]:
    res = orc.run_cycle_for_symbol("XAUUSD", style, dry_run=True)
    d = res.get("decision")
    ctx = res.get("context")
    smt_res = getattr(ctx, "metals_smt", {}) if ctx else {}
    print(f"\n==================== [{style}] ====================")
    if ctx:
        print(f"  Engine Price     : ${ctx.current_price:.2f}")
        print(f"  MTF Alignment    : {ctx.mtf_alignment}")
        print(f"  MTF Score        : {getattr(ctx, 'mtf_confluence_score', 0.0):.1f}")
        st = getattr(ctx, "structure", None)
        if st:
            print(f"  Structure Zone   : {getattr(st, 'discount_premium_zone', 'N/A')}")
            print(f"  Equilibrium Px   : ${getattr(st, 'equilibrium_price', 0.0):.2f}")
            print(f"  Recent Range     : ${getattr(st, 'range_low', 0.0):.2f} -> ${getattr(st, 'range_high', 0.0):.2f}")
            print(f"  CHOCH Reversal   : {getattr(st, 'choch', False)}")
            print(f"  BOS Continuation : {getattr(st, 'bos', False)}")
            obs = getattr(st, 'order_blocks', []) or []
            fvgs = getattr(st, 'fair_value_gaps', []) or []
            print(f"  Active OBs / FVGs: {len(obs)} OBs, {len(fvgs)} FVGs")
        vol = getattr(ctx, "volatility", None)
        if vol:
            print(f"  ATR              : ${getattr(vol, 'atr', 0.0):.2f}")
        mom = getattr(ctx, "momentum", None)
        if mom:
            print(f"  RSI              : {getattr(mom, 'rsi', 0.0):.1f}")
            print(f"  ADX Trend Power  : {getattr(mom, 'adx', 0.0):.1f}")
        if smt_res and smt_res.get("smt_active"):
            print(f"  Metals SMT Signal: {smt_res.get('smt_type')} ({smt_res.get('description')})")
    if d:
        print(f"  Resolved Bias    : {d.bias}")
        print(f"  Decision Action  : {d.decision}")
        print(f"  Strategy Model   : {getattr(d, 'strategy', 'N/A')}")
        print(f"  AI Score         : {d.ai_score:.1f} / 100")
        print(f"  Proposed Entry   : ${d.entry_price:.2f}")
        print(f"  Proposed Stop    : ${d.stop_loss:.2f} (Risk: ${abs(d.entry_price - d.stop_loss):.2f})")
        print(f"  Proposed TP      : ${d.take_profit:.2f} (Reward: ${abs(d.entry_price - d.take_profit):.2f})")
        print(f"  Risk:Reward (R:R): {d.risk_reward_ratio:.2f}:1")
        print(f"  Gate Authorization: {res.get('authorized')} ({res.get('auth_reason') or 'PASSED'})")
        qg = getattr(d, 'quality_gate', None)
        fr = getattr(qg, 'failing_reasons', []) if qg else []
        if fr:
            print(f"  Unmet Criteria   : {fr}")
        if d.rejection_reasons:
            print(f"  Adversarial Risks: {d.rejection_reasons}")

print("\n" + "=" * 75)
