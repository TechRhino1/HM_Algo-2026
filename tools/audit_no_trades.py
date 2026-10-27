import sys, os
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from jarvis.application.orchestrator import JarvisOrchestrator

print("Initializing Orchestrator in PAPER mode...")
orc = JarvisOrchestrator(mode="PAPER", trade_style="ALL")

print("\n--- Running scan_all_modes(dry_run=True) ---")
best_op, ranked, raw = orc.scan_all_modes(dry_run=True)
print(f"Total raw results: {len(raw)}")
print(f"Ranked candidates count: {len(ranked)}")
print(f"Best opportunity: {getattr(best_op, 'symbol', None)} {getattr(best_op, 'setup_grade', None) if best_op else 'None'}")

actionable_count = 0
for c in ranked:
    if c.is_actionable:
        actionable_count += 1
    print(f"[{c.setup_grade:8s}] {c.symbol:7s} ({c.trade_style:11s} {c.bias:4s}) | Util={c.utility_score:.3f} | WinP={c.win_prob:.0f}% | EV={c.expected_value:.2f}R | Actionable={c.is_actionable}")
    if not c.is_actionable:
        d = c.decision_obj
        qg = getattr(d, 'quality_gate', None)
        fr = getattr(qg, 'failing_reasons', []) if qg else []
        d_val = getattr(d, 'decision', None)
        print(f"    Reasons blocked: decision={d_val}, failing_reasons={fr}")

print(f"\nTotal Actionable Candidates: {actionable_count} / {len(ranked)}")
