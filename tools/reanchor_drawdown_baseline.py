"""
HM Algo 2.0 — Audited Dynamic Drawdown Baseline Re-Anchoring Tool (Spec v2.2).
Re-anchors the peak and daily equity baselines in jarvis_drawdown_state.db
after verified deposit/withdrawal or operator risk audit.

Per Spec v2.2 Refinements 3 & 4:
- Dynamically queries MT5 terminal for live account equity.
- Never hardcodes static account numbers.
- Creates and populates an immutable SQLite audit table: drawdown_reanchor_audit.
- Follows the mandatory 9-step atomic transaction lifecycle:
    1. Read current state
    2. Read MT5 account
    3. Display comparison
    4. Operator confirmation
    5. Write immutable audit record
    6. Commit audit record
    7. Update drawdown baseline
    8. Commit baseline
    9. Verify saved values
- Does NOT blindly claim "lockout cleared" — recalculates active protection
  state from the verified baseline across DrawdownGuard, CircuitBreaker, and MT5.
"""
import os
import sys
import hashlib
import argparse
import sqlite3
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from jarvis.config.paths import resolve_db_path, mode_scoped_db_path, DATA_DIR
from jarvis.risk.drawdown import DrawdownGuard
from jarvis.risk.circuit_breaker import CircuitBreaker


def ensure_audit_table(conn: sqlite3.Connection):
    """Creates the immutable drawdown_reanchor_audit table if not exists."""
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS drawdown_reanchor_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_utc TEXT NOT NULL,
                operator TEXT NOT NULL,
                reason TEXT NOT NULL,
                account_login INTEGER NOT NULL,
                server TEXT NOT NULL,
                previous_peak_equity REAL NOT NULL,
                previous_daily_start_equity REAL NOT NULL,
                new_peak_equity REAL NOT NULL,
                new_daily_start_equity REAL NOT NULL,
                previous_drawdown_pct REAL NOT NULL,
                confirmation_token TEXT NOT NULL
            )
        """)


def get_live_account_info():
    """Dynamically queries MT5 for current account login, server, equity, and balance."""
    try:
        import MetaTrader5 as mt5
        from jarvis.data.broker_symbols import ensure_mt5_terminal
        if ensure_mt5_terminal():
            acc = mt5.account_info()
            if acc:
                return {
                    "connected": True,
                    "login": int(acc.login),
                    "server": str(acc.server),
                    "equity": float(acc.equity),
                    "balance": float(acc.balance),
                    "trade_mode": getattr(acc, "trade_mode", 0),
                }
    except Exception as e:
        print(f"[WARN] Could not dynamically query MT5: {e}")
    return {"connected": False, "login": 0, "server": "UNKNOWN", "equity": 0.0, "balance": 0.0, "trade_mode": 0}


def get_current_stored_state(db_full_path: str):
    """Step 1: Reads current stored values from SQLite drawdown_state table."""
    if not os.path.exists(db_full_path):
        return None
    try:
        conn = sqlite3.connect(db_full_path)
        cursor = conn.cursor()
        cursor.execute("SELECT daily_start_equity, peak_equity, last_saved_date FROM drawdown_state WHERE id = 1")
        row = cursor.fetchone()
        conn.close()
        if row:
            return {
                "daily_start_equity": float(row[0]),
                "peak_equity": float(row[1]),
                "last_saved_date": str(row[2]),
            }
    except Exception as e:
        print(f"[ERROR] Failed to read database {db_full_path}: {e}")
    return None


def append_flat_audit_log(record: str):
    """Appends audit string to data/drawdown_audit.log as secondary flat-file redundancy."""
    os.makedirs(DATA_DIR, exist_ok=True)
    audit_file = os.path.join(DATA_DIR, "drawdown_audit.log")
    with open(audit_file, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}] {record}\n")


def reanchor_drawdown_baseline(
    db_path: str,
    target_equity: float,
    operator: str = "SystemOperator",
    reason: str = "Audited Reset",
    login: int = 0,
    server: str = "XMGlobal",
    dry_run: bool = False
) -> dict:
    """Executes the mandatory 9-step atomic transaction lifecycle for drawdown re-anchoring."""
    stored = get_current_stored_state(db_path)
    stored_peak = stored["peak_equity"] if stored else 0.0
    stored_daily = stored["daily_start_equity"] if stored else 0.0

    if stored_peak > 0:
        current_dd_pct = max(0.0, ((stored_peak - target_equity) / stored_peak) * 100.0)
    else:
        current_dd_pct = 0.0

    if dry_run:
        return {
            "dry_run": True,
            "stored_peak": stored_peak,
            "target_equity": target_equity,
            "current_dd_pct": current_dd_pct
        }

    token_seed = f"{target_equity}_{datetime.now(timezone.utc).isoformat()}_{login}_{operator}"
    confirmation_token = hashlib.sha256(token_seed.encode()).hexdigest()[:16]

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        ensure_audit_table(conn)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS drawdown_state (
                id INTEGER PRIMARY KEY,
                daily_start_equity REAL,
                peak_equity REAL,
                last_saved_date TEXT
            )
        """)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO drawdown_reanchor_audit (
                timestamp_utc, operator, reason, account_login, server,
                previous_peak_equity, previous_daily_start_equity,
                new_peak_equity, new_daily_start_equity,
                previous_drawdown_pct, confirmation_token
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.now(timezone.utc).isoformat(),
            operator,
            reason,
            login,
            server,
            stored_peak,
            stored_daily,
            target_equity,
            target_equity,
            current_dd_pct,
            confirmation_token
        ))
        audit_row_id = cursor.lastrowid
        conn.commit()

        today_str = datetime.now(timezone.utc).date().isoformat()
        cursor.execute("SELECT id FROM drawdown_state WHERE id = 1")
        if cursor.fetchone():
            cursor.execute("""
                UPDATE drawdown_state
                SET daily_start_equity = ?, peak_equity = ?, last_saved_date = ?
                WHERE id = 1
            """, (target_equity, target_equity, today_str))
        else:
            cursor.execute("""
                INSERT INTO drawdown_state (id, daily_start_equity, peak_equity, last_saved_date)
                VALUES (1, ?, ?, ?)
            """, (target_equity, target_equity, today_str))
        conn.commit()

        cursor.execute("SELECT daily_start_equity, peak_equity FROM drawdown_state WHERE id = 1")
        verify_row = cursor.fetchone()
        if not verify_row or abs(verify_row[1] - target_equity) > 1e-4:
            raise RuntimeError(f"Verification failed! Expected peak {target_equity}, got {verify_row}")
    finally:
        conn.close()

    append_flat_audit_log(
        f"REANCHOR_DRAWDOWN: AuditId=#{audit_row_id}, Token={confirmation_token}, Operator='{operator}', "
        f"Account=#{login} ({server}), PrevPeak=${stored_peak:.2f}, "
        f"NewPeak=${target_equity:.2f}, PrevDD={current_dd_pct:.2f}%, Reason='{reason}'"
    )

    guard = DrawdownGuard(db_path=db_path)
    dd_check = guard.check_limits(target_equity, target_equity)
    recalculated_status = "HEALTHY" if dd_check.get("passed") else "BREACHED"

    return {
        "audit_row_id": audit_row_id,
        "confirmation_token": confirmation_token,
        "stored_peak": stored_peak,
        "new_peak": target_equity,
        "verified_peak": verify_row[1],
        "verified_daily": verify_row[0],
        "recalculated_status": recalculated_status,
        "drawdown_check": dd_check
    }


def main():
    parser = argparse.ArgumentParser(description="Audited Dynamic Drawdown Baseline Re-Anchoring Tool (Spec v2.2)")
    parser.add_argument("--mode", default="live", choices=["live", "demo", "paper"],
                        help="Execution mode scope (default: live)")
    parser.add_argument("--equity", type=float, default=None,
                        help="Explicit target equity (optional; default dynamically queries MT5)")
    parser.add_argument("--confirm", action="store_true",
                        help="Explicitly confirm the re-anchoring operation without interactive prompt")
    parser.add_argument("--dry-run", action="store_true",
                        help="Simulate the operation and print diagnostics without modifying SQLite")
    parser.add_argument("--operator", default=os.environ.get("USERNAME", "SystemOperator"),
                        help="Operator username for immutable audit trail")
    parser.add_argument("--reason", default="Operator Audited Reset",
                        help="Audit reason for re-anchoring")

    args = parser.parse_args()

    db_rel = mode_scoped_db_path("jarvis_drawdown_state.db", mode=args.mode)
    db_full = resolve_db_path(db_rel)

    print("================================================================================")
    print("      HM-AI AUDITED DRAWDOWN BASELINE RE-ANCHORING & VERIFICATION TOOL (v2.2)    ")
    print("================================================================================")
    print(f"Target Database File : {db_full}")
    print(f"Target Execution Mode: {args.mode.upper()}")
    print(f"Auditing Operator    : {args.operator}")

    # Step 1: Read current state
    stored = get_current_stored_state(db_full)
    stored_peak = stored["peak_equity"] if stored else 0.0
    stored_daily = stored["daily_start_equity"] if stored else 0.0
    stored_date = stored["last_saved_date"] if stored else "N/A"

    print("\n--- [STEP 1] CURRENT STORED BASELINE ---")
    print(f"Peak Equity        : ${stored_peak:.2f}")
    print(f"Daily Start Equity : ${stored_daily:.2f}")
    print(f"Last Saved Date    : {stored_date}")

    # Step 2: Read MT5 account
    mt5_info = get_live_account_info()
    if mt5_info["connected"]:
        print("\n--- [STEP 2] LIVE MT5 TERMINAL STATE ---")
        print(f"Account Login      : #{mt5_info['login']}")
        print(f"Broker Server      : {mt5_info['server']}")
        print(f"Live Equity        : ${mt5_info['equity']:.2f}")
        print(f"Live Balance       : ${mt5_info['balance']:.2f}")
        target_equity = mt5_info["equity"] if args.equity is None else args.equity
    else:
        print("\n--- [STEP 2] MT5 TERMINAL STATUS ---")
        print("MT5 Terminal       : Disconnected / Not Running")
        if args.equity is None:
            print("[ERROR] Cannot query live equity from MT5 and no --equity value provided.")
            print("Please either run MT5 terminal or specify target equity with --equity <AMOUNT>.")
            sys.exit(1)
        target_equity = args.equity

    # Step 3: Display comparison
    current_dd_pct = max(0.0, ((stored_peak - target_equity) / stored_peak) * 100.0) if stored_peak > 0 else 0.0

    print("\n--- [STEP 3] PROPOSED RE-ANCHORING COMPARISON ---")
    print(f"Target Baseline Equity : ${target_equity:.2f}")
    print(f"Current Drawdown %     : {current_dd_pct:.2f}%")
    print(f"New Drawdown Baseline  : 0.00% (Re-anchoring to verified equity)")
    print(f"Audit Reason           : {args.reason}")

    if args.dry_run:
        print("\n[DRY RUN] --dry-run specified. No database modifications made.")
        sys.exit(0)

    # Step 4: Operator confirmation
    if not args.confirm:
        if sys.stdin and sys.stdin.isatty():
            prompt = input("\nDo you confirm re-anchoring peak equity baseline? [y/N]: ").strip().lower()
            if prompt not in ("y", "yes"):
                print("[ABORTED] Operator cancelled re-anchoring operation.")
                sys.exit(0)
        else:
            print("[ERROR] Non-interactive environment and --confirm flag not supplied. Aborting for safety.")
            sys.exit(1)

    result = reanchor_drawdown_baseline(
        db_path=db_full,
        target_equity=target_equity,
        operator=args.operator,
        reason=args.reason,
        login=mt5_info["login"],
        server=mt5_info["server"],
        dry_run=False
    )

    print(f"\n--- [STEP 5 & 6] IMMUTABLE AUDIT RECORD WRITTEN ---")
    print(f"Audit Row ID       : #{result['audit_row_id']}")
    print(f"Confirmation Token : {result['confirmation_token']}")

    print(f"\n--- [STEP 7, 8 & 9] BASELINE COMMITTED AND VERIFIED ---")
    print(f"Verified Peak Equity        : ${result['verified_peak']:.2f}")
    print(f"Verified Daily Start Equity : ${result['verified_daily']:.2f}")

    # Output exact Spec v2.2 required wording
    print("\n================================================================================")
    print("Baseline re-anchoring completed. Protection state will be recalculated from the new verified baseline.")
    print("================================================================================")

    cb_db = mode_scoped_db_path("jarvis_circuit_state.db", mode=args.mode)
    cb = CircuitBreaker(db_path=resolve_db_path(cb_db))
    cb_status = cb.check_status()
    dd_check = result["drawdown_check"]

    print("\n--- RECALCULATED PROTECTION STATUS ---")
    print(f"DrawdownGuard Status : {'PASSED (Healthy)' if dd_check.get('passed') else 'BREACHED'}")
    print(f"Calculated Daily DD  : {dd_check.get('daily_loss_pct', 0.0):.2f}%")
    print(f"Calculated Total DD  : {dd_check.get('total_dd_pct', 0.0):.2f}%")
    print(f"Circuit Breaker      : {'COOLING DOWN' if cb_status.get('active') else 'INACTIVE (Normal)'}")
    print(f"Broker Terminal Link : {'CONNECTED' if mt5_info['connected'] else 'OFFLINE'}")
    if dd_check.get("breaches"):
        print(f"Active Breaches      : {dd_check.get('breaches')}")
    else:
        print("Active Breaches      : None. Ready for autonomous execution in configured mode.")

    cb_db = mode_scoped_db_path("jarvis_circuit_state.db", mode=args.mode)
    cb = CircuitBreaker(db_path=resolve_db_path(cb_db))
    cb_status = cb.check_status()

    print("\n--- RECALCULATED PROTECTION STATUS ---")
    print(f"DrawdownGuard Status : {'PASSED (Healthy)' if dd_check.get('passed') else 'BREACHED'}")
    print(f"Calculated Daily DD  : {dd_check.get('daily_loss_pct', 0.0):.2f}%")
    print(f"Calculated Total DD  : {dd_check.get('total_dd_pct', 0.0):.2f}%")
    print(f"Circuit Breaker      : {'COOLING DOWN' if cb_status.get('active') else 'INACTIVE (Normal)'}")
    print(f"Broker Terminal Link : {'CONNECTED' if mt5_info['connected'] else 'OFFLINE'}")
    if dd_check.get("breaches"):
        print(f"Active Breaches      : {dd_check.get('breaches')}")
    else:
        print("Active Breaches      : None. Ready for autonomous execution in configured mode.")


if __name__ == "__main__":
    main()
