# Production Engineering Standards & Operational Runbook

HM Algo 2.0 quantitative algorithmic trading system is governed by 6 strict Senior Software Engineering principles designed to guarantee stability, security, risk discipline, and high-availability operations.

---

## 🏛️ 1. Architecture: Modular Microservices & Clean Boundaries

The platform is organized into decoupled, single-responsibility microservices and packages with zero circular dependencies:

```
jarvis/
├── api/            # Independent REST & WebSocket server (public vs authenticated routes)
├── application/    # Autonomous orchestrator, cycle scheduling, multi-style arbitration
├── analysts/       # Multi-agent quantitative intelligence (Devil's Advocate, Wyckoff, Macro, Microstructure, SMT)
├── data/           # Symbol registry, database abstractions, schemas, broker time, data hydrators
├── intelligence/   # Decision engine, opportunity arbiter, gate policy, dynamic levels, regime classifier
├── risk/           # Position sizing, trade guard, exposure manager, portfolio heat, circuit breaker
├── execution/      # MT5 client, execution engine, order manager, position monitor, exit policy
├── observability/  # Structured contextual logging, Prometheus metrics, active alert manager
├── market/         # News feeds, economic calendar, sentiment analysis
└── backtesting/    # High-throughput backtest engine, signal scanner
```

### Architectural Mandates
- **Decoupled Lifecycles**: The REST API / Web Terminal runs as an independent daemon thread, serving clients even when market feeds or brokers undergo weekend maintenance.
- **Fail-Closed Gateways**: Every component defaults to refusing unvalidated states (`allowed=False`) rather than fabricating assumptions.
- **Bounded Contexts**: Analysts compute read-only probabilities and risk reward evaluations; only `RiskEngine` holds the authority to size and approve orders.

---

## 🔒 2. Version Control & Git Hygiene

- **Strict Exclusion Policy**: `.gitignore` strictly blocks all credential files, local environment overrides, certificate keys, and runtime artifacts:
  - `.env`, `.env.*` (while preserving `.env.example`)
  - `*.pem`, `*.key`, `*.cert`, `*.pfx`
  - `.jarvis_secret_key`, `.jarvis_admin_pass`
  - Local database caches and logs (`*.sqlite`, `*.db-wal`, `logs/*.log`)
- **Conventional Commits**: Every change must follow conventional commit naming (`feat(...)`, `fix(...)`, `refactor(...)`, `docs(...)`, `test(...)`).
- **Clean Main Branch**: All modifications are validated via automated tests before staging and pushing to `origin/main`.

---

## 🧪 3. Testing: Comprehensive Test Suites

The repository contains 180+ test files and ~3,900 automated unit and integration tests.

### Running Test Suites
```bash
# Core Production Compliance & Execution Suite
python -m pytest tests/test_production_engineering_compliance.py tests/test_gold_unblocking.py tests/test_position_sizing_ceiling.py

# Full Quantitative & Arbiter Suite
python -m pytest tests/test_opportunity_arbiter.py tests/test_gate_policy_deadlock.py tests/test_direction_verification_all_symbols.py
```

### Testing Mandates
- **Zero Network Mocking in CI**: Unit tests mock broker connections (`MT5Client`) and market feeds to guarantee determinism and sub-second runtimes.
- **Continuous Edge Case Regression**: Every production bug fix must leave behind a regression test verifying the fix.

---

## 🛡️ 4. Security & Access Control

- **Zero Hardcoded Secrets**: No passwords, tokens, private keys, or API credentials may exist in source files or JSON configurations.
- **Dynamic Credential Resolution**:
  - `JARVIS_ADMIN_PASS`: Set in environment or auto-generated at boot and stored in protected `.jarvis_admin_pass` (0600 file permissions).
  - `JARVIS_SECRET_KEY`: Used for HMAC-SHA256 session token signing; generated uniquely if not provided.
- **Cookie Security**: Authentication uses `HttpOnly`, `SameSite=Strict` cookies with no sensitive token storage in client-side `localStorage`.
- **Brute-Force Lockout**: Rate limiting enforces exponential cooldowns after failed authentication attempts.

---

## 📊 5. Observability & Active Alerting

### Structured Logging & Metrics
- **Contextual Fields**: Every log message carries bound request IDs, active symbols, trade styles, and durations (`jarvis.observability.context`).
- **Prometheus Telemetry**: Scraped via `/api/metrics` exposing order latencies, fill rates, decision times, and synthetic bar ratios.

### Active Multi-Channel Alerting (`jarvis.observability.alerting`)
The platform features an active event dispatcher alerting on critical operational anomalies:

| Alert Severity | Event Type | Description | Dispatch Channels |
| :--- | :--- | :--- | :--- |
| **CRITICAL** | `Circuit Breaker Tripped` | Daily loss ceiling reached or consecutive losses limit triggered | Logger, Ring Buffer, Webhook |
| **CRITICAL** | `Max Drawdown Breach` | Account drawdown exceeds hard circuit breaker threshold | Logger, Ring Buffer, Webhook |
| **WARNING** | `MT5 Terminal Disconnect` | Broker terminal disconnected; falling back to synthetic cache | Logger, Ring Buffer, Webhook |
| **WARNING** | `Spread Blowout` | Market spread exceeds allowed profile limit | Logger, Ring Buffer |
| **INFO** | `Order Executed` | Real-time position fill confirmation | Logger, Ring Buffer, Webhook |

#### Webhook Integration
Set the environment variable `JARVIS_ALERT_WEBHOOK_URL` to receive real-time notifications via Slack, Discord, Telegram Bot API, or custom monitoring endpoints:
```bash
export JARVIS_ALERT_WEBHOOK_URL="https://discord.com/api/webhooks/..."
```

---

## 📖 6. Documentation & Standard Operating Procedures

- **Architecture Documentation**: See [`docs/ARCHITECTURE.md`](file:///d:/HM_AI/HM-AI/docs/ARCHITECTURE.md) and [`README.md`](file:///d:/HM_AI/HM-AI/README.md).
- **Strategy & Confluence**: See [`docs/HM_AI_MASTER_ARCHITECTURE_AND_STRATEGY_GUIDE.md`](file:///d:/HM_AI/HM-AI/docs/HM_AI_MASTER_ARCHITECTURE_AND_STRATEGY_GUIDE.md).
- **Emergency Disaster Runbook**:
  - To immediately halt all risk: Stop the background daemon or trigger `KILL` via Web Terminal.
  - To restart in paper mode: Run `python HM_start.py --mode paper`.
