import sys, os
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from jarvis.application.orchestrator import JarvisOrchestrator

orc = JarvisOrchestrator(mode="PAPER", trade_style="ALL")
for sym in ["XAUUSD", "ETHUSD", "BTCUSD"]:
    res = orc.run_cycle_for_symbol(sym, "DAY_TRADING", dry_run=True)
    d = res.get("decision")
    print(f"\n{sym}:")
    print(f"   Authorized: {res.get('authorized')}, Auth reason: {res.get('auth_reason')}")
    if d:
        print(f"   Decision: {getattr(d, 'decision', None)}, Bias: {getattr(d, 'bias', None)}")
        print(f"   AI Score: {getattr(d, 'ai_score', None)}, Strategy: {getattr(d, 'strategy', None)}")
        qg = getattr(d, 'quality_gate', None)
        print(f"   Failing reasons: {getattr(qg, 'failing_reasons', []) if qg else []}")
    else:
        print("   Decision is None!")
