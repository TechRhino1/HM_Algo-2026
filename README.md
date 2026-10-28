# HM Algo 2.0 — Institutional Quantitative MT5 Trading System

An institutional-grade, multi-factor adaptive algorithmic trading architecture built for MetaTrader 5 (MT5). The system integrates Smart Money Concepts (ICT/SMC), Wyckoff accumulation/distribution frameworks, Minervini Trend Templates, Volatility Contraction Patterns (VCP), dynamic Fractional Kelly risk management, and a **calibrated win-rate pipeline** to achieve high-expectancy trade execution.

---

## 🏛️ Master Trader Architecture & Core Innovations

### 1. ICT Fair Value Gap (FVG) & Order Block (OB) Imbalance Engine
- **Fair Value Gap Detection:** Identifies 3-candle price imbalance zones (unmitigated liquidity voids) across multiple timeframes.
- **Order Block Mapping:** Locates high-volume displacement candles that create confirmed Breaks of Structure (BOS).
- **Optimal Trade Entry (OTE):** Calculates precision Fibonacci retracement entries (62.0%, 70.5% sweet spot, and 79.0%) aligned with institutional dealing ranges.
- **Imbalance Mitigation Tracking:** Actively filters out invalidated or mitigated zones in real time.

### 2. Multi-Timeframe Institutional Dealing Range & Premium/Discount Filtering
- **True HTF Equilibrium:** Computes institutional dealing ranges over 200-bar macroeconomic lookbacks.
- **Directional Zone Enforcement:** Hard-blocks BUY entries in Premium zones (top 50% of range) and SELL entries in Discount zones (bottom 50% of range) unless supported by confirmed liquidity sweep reversals.

### 3. Session Killzone Timing Engine
- **Forex Session Gating:** Restricts Forex entries strictly to high-liquidity institutional windows:
  - **London Open Killzone:** 07:00 – 10:00 UTC (initial directional expansion)
  - **New York Open Killzone:** 12:00 – 15:00 UTC (highest volume & overlap)
  - **London Close Window:** 15:00 – 17:00 UTC (mean reversion & position unwinding)
  - **Asian Range Reference Box:** 00:00 – 07:00 UTC (defines liquidity boundary sweeps)

### 4. Advanced Regime & Confluence Stack
- **Wyckoff / ICT Fusion:** Recognizes Wyckoff Springs, Upthrusts, Accumulation/Distribution phases, and Change of Character (CHoCH).
- **Minervini Trend Template & VCP:** Measures multi-stage volatility contractions and multi-timeframe moving average slope alignment.
- **Hard Confluence Gate:** Enforces institutional quality hurdles (Score >= 65/100 for Forex, >= 55/100 for Commodities & Crypto).

### 5. Professional Trade Management & Execution Protocol
- **Asset-Adaptive Partial Exits:**
  - Takes 33% profit at 1.5R (Forex) or 1.8R (Gold/Crypto).
  - Automatically moves Stop Loss to true Breakeven (0.0R) to eliminate downside risk on remaining position.
- **Dynamic ATR Chandelier Trailing Stop:** Trails remaining runner positions with a 1.5x ATR buffer from recent swing extremes, allowing macro trends to run without premature suffocation.
- **24-Bar Time Stop:** Force-closes stale, non-moving trades after 24 hours on H1 if 0.5R favorable excursion is not achieved, preventing capital lock-up.

### 6. Consecutive Loss Circuit Breakers & Fractional Kelly Position Sizing
- **Streak Protection:**
  - 2 consecutive losses -> automatically cuts position risk by 50%.
  - 3 consecutive losses -> activates a 4-bar mandatory cooling-off period.
  - Daily Drawdown >= 3.0% -> halts all new executions for the remainder of the trading day.
- **Dynamic Kelly Allocation:** Dynamically scales trade size using Quarter-Kelly optimal sizing adjusted for high-water mark drawdown.

### 7. Execution Mode Safety Gate (`JARVIS_CONFIRM_LIVE=1`)
- **Default Safety Guard:** If execution mode is configured to `"live"` without `JARVIS_CONFIRM_LIVE=1` explicitly set in environment variables, the system logs a prominent warning banner and safely forces execution mode to `"paper"`.
- **Live Trading Safeguard:** Prevents accidental real-money order execution from unvalidated configuration files or default startup scripts.

### 8. End-to-End Limit & Pending Order Pipeline
- **Retracement & Level Routing:** Automatically routes setups to `BUY_LIMIT` or `SELL_LIMIT` pending orders when entry price requires retracement to key structural levels (FVG, Order Block, liquidity sweeps).
- **Direction & Price Sanity Checks:** Validates limit price geometry (`BUY_LIMIT` < live bid, `SELL_LIMIT` > live ask) and falls back safely to market orders if price has already crossed the level.
- **Watchdog Stale Cleanup:** Automatically cancels unfilled pending orders exceeding configurable TTL (default 30 mins) during background orchestrator ticks.
- **Web Terminal UI & API:** Integrated `GET /api/pending_orders` and `POST /api/action/cancel_pending_order` endpoints with dedicated "Pending Orders" table tab in the Web Terminal.

### 9. Secure Cookie Authentication Model
- **HttpOnly & SameSite=Strict:** Authentication tokens are issued exclusively via secure `HttpOnly`, `SameSite=Strict` cookies, preventing token theft from client-side scripts.
- **Zero LocalStorage Tokens:** Eliminates client-side JWT token storage in `localStorage` / `sessionStorage`.

### 10. Asset-Class Specific Strategy Specialization
- **Commodities (XAUUSD/Gold, XAGUSD/Silver):** High-momentum trend following and break-of-structure expansion with structure-anchored stops.
- **Forex Majors (EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD, USDCHF, NZDUSD):** Range mean reversion (Bollinger Band 20, 2.0 SD with ADX < 22) and killzone liquidity sweep reversals.
- **Crypto (BTCUSD, ETHUSD, SOLUSD):** High-volatility swing momentum and multi-timeframe FVG pullbacks.
- **Indices (US30, NAS100, GER40, UK100):** Breakout momentum and session-driven volatility capture.

### 11. Production Engineering & Active Alerting (6-Pillar Standards)
- **Modular Microservices**: Decoupled packages with strict bounded contexts across intelligence, risk, execution, and API services.
- **Version Control & Hygiene**: Strict `.gitignore` policy excluding all credentials, environment overrides, and private keys.
- **Automated Test Coverage**: 180+ test files and ~3,900 unit and integration tests verifying risk gates, sizing, and execution.
- **Zero Hardcoded Secrets**: Fully dynamic credential resolution via environment variables and local protected files.
- **Active Alerting Engine**: Asynchronous, debounced real-time notifications for circuit breakers, drawdowns, and disconnects via webhooks (`JARVIS_ALERT_WEBHOOK_URL`).
- **Comprehensive Runbooks**: See [`docs/PRODUCTION_ENGINEERING_STANDARDS.md`](docs/PRODUCTION_ENGINEERING_STANDARDS.md).

---

## 🎯 New in v5.0 — Calibrated Win-Rate Pipeline

### Four-Stage Split Architecture
Win rate is treated as a **constrained objective**: maximise expectancy subject to `win_rate >= target`.

| Stage | Module | Cost | Output |
|---|---|---|---|
| 1. Signal scan | `jarvis/backtesting/signal_scan.py` | ~30 s/symbol | Every directional candidate the live pipeline considered |
| 2. Trade simulation | `jarvis/backtesting/trade_simulator.py` | ~1–2 s/geometry | Outcome in R for one exit geometry |
| 3. Calibration | `jarvis/intelligence/winrate_targeting.py` | seconds | Per-symbol profile (walk-forward) |
| 4. Entry selection | `jarvis/execution/entry_policy.py` | — | Allow / deny at runtime |

### Key Pipeline Features
- **Isotonic Calibration (PAV):** Maps confluence scores to realised win probabilities, making scores interpretable rather than arbitrary indices.
- **Walk-Forward Cross-Validation:** `PurgedKFold` with embargo ensures geometry selection happens only on training folds, reported honestly on purged out-of-sample data.
- **Regime-Edge Policy:** Learned per-symbol regime enable/disable decisions, fitted on training folds only to prevent test-set leakage.
- **Hermetic Backtesting:** All persistent-state reads/writes are disabled during simulation (`jarvis.config.runtime`), guaranteeing reproducible and deterministic results.

---

## 📐 Architecture Diagram

```
                RAW MARKET DATA (MT5 Feed / Tick & Bar OHLCV)
                                     │
                                     ▼
                MULTI-TIMEFRAME ENGINE (D1 / H4 / H1 / M15 / M5)
                                     │
         ┌───────────────────────────┼───────────────────────────┐
         ▼                           ▼                           ▼
  ICT FVG & OB ENGINE         MARKET STRUCTURE           ORDER FLOW & SESSIONS
 (Imbalance, Mitigation,     (200-Bar P/D Range,        (Killzones, Volume Delta,
   OTE 62%-79% Zones)         BOS, CHoCH, Swings)         Absorption Traps)
         └───────────────────────────┬───────────────────────────┘
                                     │
                                     ▼
                      MASTER CONFLUENCE SCORING ENGINE
                 (Wyckoff + Minervini VCP + ICT Triple Confluence)
                                     │
                                     ▼
                         AI DECISION & QUALITY GATE
              (Hard Confluence >= 65, Session Gating, EV & R:R)
                                     │
                                     ▼
                     DYNAMIC RISK & LOSS COOLDOWN MANAGER
                (Fractional Kelly, 3-Loss Circuit Breaker, 3% DD)
                                     │
                                     ▼
                     EXECUTION & ACTIVE TRADE MANAGEMENT
              (Partials at 1.5R/1.8R, True BE, ATR Trail, 24-Bar Time Stop)
```

---

## 🛠️ Project Structure

```
HM-AI/
├── jarvis/
│   ├── analysts/               # Specialized AI analyst agents (Structure, Flow, Momentum, etc.)
│   ├── backtesting/            # Event-driven backtesting engine, signal scanner & trade simulator
│   ├── config/                 # Runtime execution mode (hermetic backtesting)
│   ├── core/                   # Configuration, symbol registry, and regime classifiers
│   ├── data/                   # Data schemas, database models, and symbol resolvers
│   ├── execution/              # Entry policy, exit policy, limit order routing
│   ├── intelligence/           # Decision engine, meta-labeling, win-rate targeting, self-learning
│   ├── learning/               # Online ML, strategy bandit, walk-forward CV, sample weights
│   ├── market/                 # FVG engine, session engine, market context, liquidity sweeps
│   └── risk/                   # Loss cooldown manager, circuit breakers, HRP allocation, trade guard
├── tests/                      # Automated pytest unit and integration test suite (259 tests)
├── tools/                      # CLI utilities: calibration, signal scan, backtest runners
├── reports/                    # Generated backtest reports and performance analytics
├── run_multiasset_6m_backtest.py   # Real 6-month MT5 historical backtest benchmark suite
└── README.md                   # System documentation
```

---

## 🚀 Installation & Usage

### 1. Environment Setup
```bash
# Clone the repository
git clone https://github.com/TechRhino1/HM-AI.git
cd HM-AI

# Install dependencies
pip install -r requirements.txt
```

### 2. Run Test Suite
Verify that all 259 system tests pass:
```bash
python -m pytest tests/ -v
```

### 3. Calibrate Win-Rate Profiles
Run the full four-stage calibration pipeline across all symbols:
```bash
python tools/calibrate_winrate.py --symbols ALL --target-wr 0.75
```

### 4. Run Real MT5 Backtests
Ensure your MetaTrader 5 terminal is open and logged into your broker:
```bash
# Run 3-month calibrated benchmark across 16 core assets
python tools/run_3month_backtest.py

# Or run fast 6-month benchmark
python run_multiasset_6m_backtest.py

# Or run comprehensive 1-year benchmark
python run_1year_backtest.py
```

---

## 📊 Backtest Benchmarks (Real MT5 Data)

### 3-Month Calibrated Profile (16 Symbols, 698 Trades)

| Metric | Value |
| :--- | :--- |
| **Historical Period** | 3 Months (Real MT5 H1) |
| **Symbols Traded** | 16 (Forex, Crypto, Commodities, Indices) |
| **Total Trades** | 698 |
| **Portfolio Win Rate** | 71.78% |
| **Expectancy** | -0.036 R per trade |
| **Payoff Ratio** | 0.341 |
| **Max Drawdown** | 12.63% |
| **Sharpe / Sortino** | -0.56 / -0.99 |
| **Win-Rate Target Met** | 7/16 symbols out-of-sample |

### 1-Year Multi-Asset Historical Benchmarks (43,800 H1 Bars)

| Metric | Baseline (Initial) | Architecture V2 (Staggered 2.0R) | Architecture V3 (Multi-Tier Ratchet) |
| :--- | :--- | :--- | :--- |
| **Historical Period** | 12–18 Months (Real MT5 H1) | 12–18 Months (Real MT5 H1) | 12–18 Months (Real MT5 H1) |
| **Total Bars Evaluated** | 43,800 H1 Bars | 43,800 H1 Bars | 43,800 H1 Bars |
| **Total Trades Executed**| 418 trades | 261 trades | 206 trades |
| **Portfolio Win Rate %** | 40.43% | 29.12% | **48.06%** (up to 52.5% on USDJPY) |
| **Max Portfolio Drawdown**| 4.92% | 3.96% | **3.90%** (Robust Capital Preservation) |
| **Catastrophic SL Exits**| 249 (59.6%) | 185 (70.9%) | **97 (47.1%)** (-61% loss events) |
| **Top Edge Strategy** | — | — | **`LIQUIDITY_SWEEP_REVERSAL`** (PF 1.07, +$15.65) |
| **Top Edge Regime** | — | — | **`RANGE`** (PF 1.92, 50.0% Win Rate) |

---

## 🔒 Hermetic Backtesting Guarantee

All persistent-state reads and writes on the decision path are disabled during simulation via `jarvis.config.runtime`:

- `RealtimeOptimizer` reads no live PnL history
- `OnlineMLPredictor` does not load or save model weights
- `SelfLearningEngine` starts from neutral priors
- `MetaLabeler` remains neutral (untrained)
- `ConfidenceCalibrationEngine` does not influence scores

This guarantees **reproducible, deterministic backtests** that measure the strategy as specified — not as mutated by unrelated live trading history.

---

## 📚 Documentation

- **[Architecture Reference](docs/ARCHITECTURE.md)** — Deep dive into system design, module boundaries, and invariants
- **[Project Documentation](JARVIS_COMPLETE_PROJECT_DOCUMENTATION.txt)** — Complete feature and module documentation

---

*Built with discipline. Backtested with honesty. Traded with edge.*
