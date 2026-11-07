# HM-AI / JARVIS — Engineering & Safety Audit
**Date:** 2026-11-07  
**Scope:** correctness, risk controls, execution, journal integrity, test coverage  
**Mode audited:** DEMO (`101059540`, balance ≈ $481.13, equity ≈ $483.60 at last check)

---

## 1. Executive verdict

The system is **safer and materially more correct after this pass**, but it is **not profitable and not ready for live capital**.

- The live book has **788 closed bot trades**, **23.1% win rate**, **net −$996.26**, **profit factor 0.31**, **expectancy −$1.264/trade**, **t ≈ −6.41**, and **Kelly < 0**. That is a statistically negative edge, not a drawdown.
- The $100/day objective cannot be reached by loosening risk. On a ~$481 account it would require roughly **20%+ return per day**, which is not a realistic edge target. It is an **edge + capital-scaling** problem.
- This pass fixed real safety, accounting, and execution defects. It did **not** create alpha, and no profitability claim is made.

**Readiness:** DEMO only. Do not enable LIVE until a positive-expectancy strategy is validated out-of-sample and the account is adequately capitalised.

---

## 2. Critical defects fixed

### 2.1 DEMO/LIVE account-type safety blocks had been weakened
- **Files:** `jarvis/execution/mt5_client.py` → `send_market_order`, `place_pending_order`
- **Defect:** A DEMO bot connected to a REAL account (or a LIVE bot connected to a DEMO account) only logged a warning and proceeded.
- **Fix:** Both paths now return `{"status": "BLOCKED", "reason": "SAFETY_BLOCK: ..."}` before any order is sent.
- **Impact:** Prevents the single most dangerous misconfiguration in the system — real money routed by a demo-mode process or vice versa.

### 2.2 Circuit breaker was reset on every boot
- **Files:** `jarvis/application/orchestrator.py`, `jarvis/risk/circuit_breaker.py`
- **Defect:** The orchestrator unconditionally called `circuit_breaker.reset()` at startup, and `JARVIS_RESET_CIRCUIT_BREAKER` defaulted to `1`. A process restart erased a loss-streak lockout.
- **Fix:** Removed the boot reset; the env-var default is now `0`. A tripped breaker survives restarts. Operator reset requires an explicit `JARVIS_RESET_CIRCUIT_BREAKER=1`.
- **Impact:** Bouncing the process can no longer bypass a risk halt.

### 2.3 API drawdown gate could crash or fail open
- **File:** `jarvis/api/server.py` → `_validate_trade_risk`
- **Defect:** It unpacked `check_limits()` as `ok, reason`, but the guard returns a dict (`{"passed": ..., "breaches": [...]}`), and it assumed an object attribute for balance.
- **Fix:** Reads `dd.get("passed")` / `dd.get("breaches")` and handles both dict and object account snapshots.
- **Impact:** Manual/pending orders are now actually gated by drawdown state through the API.

### 2.4 Pending orders polluted the trade journal as fake “trades”
- **Files:** `jarvis/application/orchestrator.py`, `jarvis/execution/execution_engine.py`, `jarvis/learning/trade_memory.py`
- **Defect:** `TradeMemory.record_trade()` ran when a limit/stop was **PLACED**, not when it filled. Every cancelled limit left a permanent `pnl = NULL` row. This inflated trade counts and made the journal look like hundreds of unresolved positions.
- **Fix:**
  - Pending payloads are held in `_pending_trade_memory` and written only on `POSITION_OPENED` (`_on_position_opened`).
  - `_on_trade_closed` writes a complete row if the open event was missed (restart/dropped event).
  - `_discard_pending_intent` removes the deferred payload and any legacy phantom row when `cleanup_stale_pending_orders` cancels an order.
  - `TradeMemory.delete_unfilled_intent()` deletes only rows with **both** `pnl IS NULL` and `exit_price IS NULL`.
  - `execution_engine.py` still journals **PLACED** orders into `executed_trades` (`is_fill or is_placed`) so `sync_mt5_history` can reconcile the eventual fill or cancellation.
- **Impact:** The journal now distinguishes an intention from a trade.

### 2.5 No hard monetary risk ceiling at dispatch
- **File:** `jarvis/execution/execution_engine.py` → `execute_decision`
- **Defect:** Sizing could be overridden by a caller-supplied lot, a broker minimum-lot floor, or a widened stop. The breach was only logged **after** the fill.
- **Fix:** A pre-dispatch check computes `stop_distance × $/price-unit × lots` and refuses the order before any broker call if it exceeds `max_risk_per_trade_pct` (0.5%) of equity.
- **Impact:** On the current ~$481 account, 0.01-lot gold/BTC trades with wide stops are **rejected** instead of silently risking several percent. This is intentional capital preservation.

### 2.6 Pending-order TTL never matched real order comments
- **Files:** `jarvis/execution/order_manager.py`, `jarvis/application/orchestrator.py`
- **Defect:** The cleanup compared comments against `"SCALP"` / `"SWING"` / `"DAY"`, but execution stamps compact codes `HMA2_SCLP_...`, `HMA2_SWNG_...`, `HMA2_DAY_...`. Every real order fell through to the 30-minute default, and structural invalidation was never given the market-context map.
- **Fix:** Matches `SCLP`/`SWNG`/`DAY` (and the full names), and both cleanup call sites pass `state_manager.market_contexts`.
- **Impact:** Pending orders now expire on their intended timeframe and can be cancelled when their structure is invalidated.

### 2.7 Unvalidated early-exit pre-filter ran by default
- **File:** `jarvis/application/orchestrator.py`
- **Defect:** A provisional ADX/trend/MTF filter could skip the full `DecisionEngine` before the real gates ran. Its thresholds were never validated, so it silently changed live behaviour.
- **Fix:** Gated behind `JARVIS_EARLY_EXIT_FILTER=1`, **default OFF**.
- **Impact:** The full decision pipeline is authoritative unless an operator explicitly opts into the benchmark path.

### 2.8 Test-only crash on instances that bypass `__init__`
- **Files:** `jarvis/application/orchestrator.py`, `tests/test_regression_fixes.py`
- **Defect:** `_on_trade_closed`, `_on_position_opened`, and `_discard_pending_intent` accessed `_pending_trade_memory` directly. Tests that construct an orchestrator without `__init__` raised `AttributeError`.
- **Fix:** Defensive `getattr(..., None) or {}` lookups, and the HM_start smoke test now stubs the listener and terminal probe. The test previously deadlocked because `threading.Thread` was mocked, so `serve_forever()` never ran and `BaseServer.shutdown()` blocked forever.
- **Impact:** The relevant suite runs to completion instead of hanging.

---

## 3. Journal remediation (applied)

The historical journal contained **538 NULL-PnL rows out of 970**. Analysis found only **3** matched a still-open `executed_trades` row; **3** had a recoverable outcome; the rest were never-filled intents.

- Backed up: `data/jarvis_trade_memory.backup_20261106.db`
- Backfilled **3** outcomes from `executed_trades`
- Deleted **504** phantom rows (343 + 161 in two passes)
- Remaining: **466 total rows, 31 NULL-PnL**
  - 3 protected open positions
  - 2 stale EURUSD rows from 2026-10-12 still matching open `executed_trades` rows
  - remainder are recent intents (< 3 days old at cleanup time)
- Tool: `tools/cleanup_phantom_trade_memory.py` (dry-run by default)

Re-run later with:

```powershell
python tools/cleanup_phantom_trade_memory.py --min-age-days 3
python tools/cleanup_phantom_trade_memory.py --min-age-days 3 --apply
```

---

## 4. Verification performed

### Focused safety/execution suite
```
tests/test_gold_entry_fixes.py
tests/test_live_safety_gate.py
tests/test_decoupled_circuit_breaker.py
tests/test_mode_scoped_state.py
tests/test_pending_orders.py
tests/test_trade_memory_migration.py
tests/test_trade_outcome_unknown_is_null.py
tests/test_risk_ceiling_enforced.py
tests/test_position_sizing_ceiling.py
tests/test_drawdown_guard.py
tests/test_safety_regressions.py
tests/test_learning_survives_restart.py
tests/test_hybrid_order_routing.py
tests/test_regression_fixes.py
```
**Result: 244 passed, 1 failed** — the single failure is pre-existing (see §5).

### Broader changed-module sweep (28 files)
**Result: 380 passed, 4 failed.** All four reproduce at parent commit `437fb2a`; none are regressions from this pass.

New regression coverage:
- `tests/test_safety_regressions.py` — 7 tests:
  - DEMO refuses REAL account (market + pending)
  - LIVE refuses DEMO account
  - breaker survives restart by default
  - explicit env reset still works
  - API drawdown gate reads the dict result
  - dispatch refuses risk above the configured ceiling

Not completed: the entire repository suite. It is large and order-dependent, with known pre-existing failures in `test_analyst_scoring.py` and `test_ai_dissector.py`.

---

## 5. Known remaining failures / limitations

1. **`test_regression_fixes.py::test_a1_risk_ceiling_rejection` (pre-existing).** It expects `PositionSizer` to reject a $50 account risking 4% on gold. The sizer deliberately allows a micro-account floor (up to 2.5% / $12, or 5.5% / $28 for gold), asserted by `test_gold_unblocking.py`. The execution-level 0.5% ceiling blocks these at dispatch, but the sizer contract remains contradictory. **Not changed in this pass** because changing it would reverse an intentional micro-account policy and break the paired gold tests; the execution ceiling is the effective control.
2. **`test_v2_spec_parity.py` — 2 failures (pre-existing).** `test_manual_trade_isolation_protect_only` and `test_pre_fill_broker_safe_sl_adjustment` fail identically at parent commit.
3. **`test_gold_entry_timing_and_news_choke.py` — weekend-dependent failure (pre-existing).** The real wall clock is Saturday, so the session gate rejects before the collision gate the test asserts. Freeze time in the fixture to remove flakiness.
4. **No demonstrated edge.** Engineering correctness does not create positive expectancy. The signal must be validated out-of-sample before scaling.
5. **Process-local pending context.** `_pending_features` and `_pending_trade_memory` do not survive a restart. A pending order that fills after a restart loses most ML context; `_on_trade_closed` still writes a row, but the feature vector may be absent.
6. **Pending reconciliation depends on MT5 semantics** (position id == opening order ticket). Verified for the current broker; re-check on any broker change.
7. **Current capital is too small for wide-stop instruments at 0.5% risk.** Gold/BTC minimum lots will often be rejected. That is the honest result, not a bug to route around by raising risk.
8. **Subagents were unavailable** on the current plan; all work was done directly.
9. **31 NULL-PnL rows remain**, mostly recent intents and three protected open positions. Re-run the cleanup tool after a few days.

---

## 6. Safety configuration

| Control | Default | Notes |
|---|---|---|
| DEMO/LIVE account mismatch | **BLOCK** | No override in code |
| `JARVIS_RESET_CIRCUIT_BREAKER` | `0` | Set `1` only after operator review |
| `JARVIS_EARLY_EXIT_FILTER` | `0` | Opt-in benchmark path |
| Per-trade risk ceiling | **0.5%** | Enforced pre-dispatch |
| Circuit breaker persistence | **on** | Survives restart |
| Pending intent journaling | **on fill only** | `POSITION_OPENED`, with close fallback |

---

## 7. Recommended next steps

1. **Stay on DEMO** and run the fixed build for at least 100 closed trades.
2. Measure **expectancy, profit factor, and per-symbol breakdown**; do not tune thresholds on the same sample used to evaluate them.
3. Fix or explicitly retire the stale `test_a1_risk_ceiling_rejection` / micro-account policy contradiction so the safety contract is unambiguous.
4. Reconcile the remaining 31 NULL-PnL rows after they age past 3 days.
5. Freeze time in session-sensitive tests; eliminate the weekend flake.
6. Only after a positive out-of-sample expectancy exists, consider capital scaling — not leverage or a higher risk ceiling.

---

## 8. Bottom line

This pass fixed genuine, high-severity defects: a broker/account safety bypass, a restart-erasable circuit breaker, a broken API drawdown gate, a phantom-trade journal, an unenforced risk ceiling, broken pending-order TTLs, and a default-on unvalidated filter. The system is now **honest about what it is doing** and **harder to misconfigure into a large loss**.

It is **not** a profitable system. The measured edge is negative, and no code change in this pass alters that fact. DEMO is the correct operating mode until a validated edge exists.
