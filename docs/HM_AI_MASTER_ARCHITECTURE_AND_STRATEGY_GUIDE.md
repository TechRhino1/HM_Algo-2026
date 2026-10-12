# HM-AI Autonomous Quantitative Trading Engine: Master Architecture & Strategy Guide

---

## 1. Executive System Overview & End-to-End Pipeline

The **HM-AI** (HM Algo 2.0) platform is an enterprise-grade, autonomous quantitative trading system engineered for MetaTrader 5 (MT5). The architecture is built on institutional market microstructure principles, combining Smart Money Concepts (SMC/ICT), Order Flow Footprint Analytics, Multi-Agent Confluence, and Bayesian Strategy Routing.

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                                 EXECUTION TOPOLOGY                                     │
└────────────────────────────────────────────────────────────────────────────────────────┘
                                    [HM_start.py]
                                          │
                     ┌────────────────────┴────────────────────┐
                     ▼                                         ▼
         [JarvisOrchestrator]                       [Web Telemetry Server]
        (Autonomous Trading Loop)                   (Port 8501 / Serveo SSL)
                     │
     ┌───────────────┼──────────────────────────────┬────────────────────────┐
     ▼               ▼                              ▼                        ▼
[Layer 1: Ingest] [Layer 2: Features]        [Layer 3: Agents]     [Layer 4: Decision]
MT5Client (Lock)  MarketContextEngine        ParallelAnalyst (7)   MarketRegime (16)
DataFeedEngine    Structure, Liquidity,      HypothesisEngine      StrategySelector (Bayes)
BrokerSymbols     Momentum, Volatility,      Devil's Advocate      DynamicLevelsEngine
SymbolRegistry    OrderFlow Imbalance        MasterConfluence      DecisionEngine (Gate)
                     │
     ┌───────────────┴──────────────────────────────┬────────────────────────┐
     ▼                                              ▼                        ▼
[Layer 5: Risk Engine]                      [Layer 6: Execution]   [Layer 7: State & Sync]
Master 15-Gate Authorizer                   Universal Arbiter      MT5StateSynchronizer
PositionSizer (Kelly / Tier)                ExecutionEngine        PositionMonitorEngine (2s)
CircuitBreaker (Loss/Regime)                OrderManager           EventBus (Pub/Sub)
DrawdownGuard (Daily 4% / 10%)              Broker-Safe Clamping   StateManager (Global)
```

### The 7 Core Architectural Layers

1. **Layer 1: Data Ingestion (`jarvis/market/data_feed.py`, `jarvis/execution/mt5_client.py`)**
   - High-performance, multi-timeframe OHLCV streaming across M1, M5, M15, H1, H4, and D1.
   - Bounded TTL caching (512 entries, 8-second expiration) with bar freshness validation (`CLASSIFY_BAR_FRESHNESS`).
   - Thread-safe native broker serialization via `TrackedRLock("mt5_shared")`, bounding wait times and eliminating MT5 C-binding deadlocks.
2. **Layer 2: Feature & Signal Extraction (`jarvis/market/`, `jarvis/intelligence/order_flow.py`)**
   - Market Structure: Fractally validated Swing Highs/Lows, Break of Structure (BOS), Change of Character (CHoCH), Order Blocks (OB), and Fair Value Gaps (FVG).
   - Liquidity: Buy-Side Liquidity (BSL), Sell-Side Liquidity (SSL), and institutional wick sweep detection.
   - Momentum & Volatility: Multi-timeframe ADX levels/slopes, RSI dynamic extremes, ATR regime ratios (Compression vs Expansion), and Donchian channels.
   - Order Flow: Footprint volume delta imbalance, institutional absorption detection (`BUYER_ABSORPTION_TRAP` / `SELLER_ABSORPTION_TRAP`).
3. **Layer 3: Multi-Agent Confluence (`jarvis/analysts/parallel_runner.py`)**
   - Dispatches 7 specialized analytical sub-agents concurrently:
     - `StructureAnalyst`: Evaluates structural support/resistance, BOS, OBs, and zone discount/premium.
     - `MomentumAnalyst`: Trend strength, ADX, multi-timeframe momentum alignment.
     - `LiquidityAnalyst`: Liquidity sweeps, equal highs/lows (EQH/EQL), retail stop-run detection.
     - `VolatilityAnalyst`: ATR expansion/contraction feasibility.
     - `MacroAnalyst`: Trading session liquidity, news event shock impact with bounded wait time ($1.5\text{s}$).
     - `RiskAnalyst`: Reward-to-risk feasibility and spread sanity.
     - `DevilAdvocateAnalyst`: Counter-thesis generator, adversarial threat score ($0–50$), trap detection.
   - `HypothesisEngine`: Formulates dialectical theses (Primary vs Alternative vs No-Trade) and structural invalidation levels.
   - `ConfidenceCalibrationEngine`: Maps raw win probabilities through historical isotonic calibration curves to prevent model overconfidence.
4. **Layer 4: Strategy Selection & Direction Resolution (`jarvis/intelligence/`)**
   - `MarketRegimeClassifier`: Softmax probability vector across 16 regime states using CUSUM structural changepoint detection.
   - `StrategySelector`: Context-aware Bayesian posterior probabilities incorporating symbol profiles, account equity, and multi-armed bandit weights (`StrategyBandit`).
   - `DecisionEngine`: Hierarchical direction resolution, Dynamic Levels generation, and 30-rule quality gating.
5. **Layer 5: Institutional Risk Engine (`jarvis/risk/`)**
   - Evaluates trades against 15 strict conditions (exposure budget, max portfolio heat, correlation matrices, Asian session blackouts, spread limits, and loss cool-downs).
   - Sizing via fractional Kelly criterion and account-tier lot caps ($0.01$ micro floor to $0.20$ standard cap).
   - Dual-tier `CircuitBreaker` (45-min symbol pause after 2 consecutive losses; 60-min regime pause after 3 losses).
   - `DrawdownGuard` (daily $4\%$ loss cap, $10\%$ peak-to-trough max drawdown brake).
6. **Layer 6: Execution & Position Management (`jarvis/execution/`)**
   - `UniversalOpportunityArbiter`: Cross-asset and cross-style utility ranking, grading candidates (Grade A+, A, B) to allocate capital to highest-expected-value setups.
   - `ExecutionEngine`: Enforces broker stop-level constraints, slippage buffers, and passive limit vs sniper market routing.
   - `PositionMonitorEngine`: Independent 2-second background thread executing the unified `exit_policy.py` (fast-cash partial scale-outs, milestone ratchets, ATR trailing, and adversarial absorption shields).
7. **Layer 7: State Synchronization & Telemetry (`jarvis/execution/state_synchronizer.py`, `jarvis/api/server.py`)**
   - Reconciles open positions, pending orders, and closed deal tickets every 1 second.
   - Publishes synchronized lifecycle events (`POSITION_CLOSED`, `trade_closed`) to `GLOBAL_EVENT_BUS`.
   - Multithreaded REST/SSE web server on port 8501 with real-time UI dashboard.

---

## 2. Multi-Agent Confluence & Bayesian Decision Engine

### 2.1 The Parallel Analyst Cluster
The decision pipeline employs a dialectical consensus model. Rather than relying on a single neural network or static heuristic, the platform queries 7 independent quantitative agents:

$$\text{ConfluenceScore} = \sum_{i=1}^{7} w_i \cdot \text{AgentScore}_i$$

| Agent | Weight ($w_i$) | Core Institutional Metrics |
| :--- | :---: | :--- |
| **Structure Analyst** | $0.25$ | Swing fractal bias, BOS/CHoCH status, OB mitigation, Premium/Discount zone |
| **Momentum Analyst** | $0.20$ | ADX(14) level & slope, $+DI/-DI$ spread, EMA stack ($20/50/200$), regression $R^2$ |
| **Liquidity Analyst** | $0.20$ | BSL/SSL pool distance, Judas swing sweep magnitude, rejection wick ratio |
| **Volatility Analyst** | $0.10$ | ATR ratio ($ATR_{cur}/ATR_{median}$), compression squeeze percentile, spread ratio |
| **Macro Analyst** | $0.10$ | London/NY Prime Killzones, Asian session low-liquidity penalty, high-impact news |
| **Risk Analyst** | $0.15$ | Feasibility of minimum $1:2.0$ R:R, spread cost as % of risk, margin load |
| **Devil's Advocate** | *Adversarial Penalty* | Counter-trend traps, momentum exhaustion, HTF supply obstacles (Score: $0–50$) |

### 2.2 Adversarial Risk Filtering (Devil's Advocate)
The `DevilAdvocateAnalyst` acts as an automated red-team agent. It tests proposed trade setups across 7 distinct threat vectors:
1. **Structural Inversion (+18 pts):** Opposing fractal swing within $0.5 \times ATR$.
2. **Momentum Exhaustion (+12 pts):** RSI divergence ($>70$ for Buy, $<30$ for Sell) or counter-ADX dominance.
3. **Unfavorable Auction Location (+10 pts):** Buying in Premium ($>50\%$ of dealing range) or Selling in Discount ($<50\%$).
4. **HTF Structural Conflict (+12 pts):** Trade opposes D1/H4 macro anchor trend.
5. **Liquidity Trap (+8 pts):** Equal Highs/Lows left unmitigated behind entry zone.
6. **Order Flow Absorption (+15 pts):** Heavy volume delta printing against candle direction.
7. **Cross-Asset Divergence (+8 pts):** Conflicting movement in benchmark pairs (e.g., DXY rally during Gold Buy).

If the total penalty exceeds $50.0$, or $\ge 3$ critical threats fire, `TradeQualityGate` automatically vetos execution. For acceptable penalties, trade risk is scaled down using the Invalidation Risk Coefficient:

$$\lambda_{\text{invalidation}} = \max\left(0.20, \, 1.0 - \frac{\text{Penalty}}{60.0}\right)$$

### 2.3 Bayesian Strategy Selection & Multi-Armed Bandit
Strategies are selected using dynamic Bayesian posterior updates rather than static regime lookup tables:

$$P(\text{Strategy}_k \mid \text{Context}) = \frac{P(\text{Context} \mid \text{Strategy}_k) \cdot P(\text{Strategy}_k)}{\sum_j P(\text{Context} \mid \text{Strategy}_j) \cdot P(\text{Strategy}_j)}$$

- **Prior $P(\text{Strategy}_k)$:** Derived from the instrument's calibrated profile in `symbol_profile_config.py` and updated at runtime by the Multi-Armed Bandit (`StrategyBandit`) based on real trade win/loss feedback.
- **Likelihood $P(\text{Context} \mid \text{Strategy}_k)$:** Evaluated against ADX slope, liquidity sweep magnitude, footprint delta imbalance, and volatility squeeze state.
- **Regime Mutual Exclusion:**
  - In strong trend / high ADX ($\ge 25$), `RANGE_MEAN_REVERSION` likelihood is locked to $0.0$.
  - In ranging / low ADX ($< 20$), `TREND_FOLLOWING` likelihood is locked to $0.0$.
  - In micro accounts (equity $< \$100$), `MICRO_ACCOUNT_ADAPTIVE` is locked to $0.85$.

---

## 3. Master Catalog of 27+ Institutional Strategies

### 3.1 `TREND_FOLLOWING` (Institutional Trend Expansion)
- **Theoretical Basis (§3.1, §7.S1):** Captures institutional momentum expansion driven by macro order flow imbalances.
- **Formulas & Triggers:**
  - $ADX(14) \ge 25.0$ with positive slope: $\frac{d(ADX)}{dt} > 0.05$.
  - EMA Hierarchy: $Close > EMA_{20} > EMA_{50} > EMA_{200}$ (BUY) or $Close < EMA_{20} < EMA_{50} < EMA_{200}$ (SELL).
  - Confirmed Break of Structure (`BOS = True`), candle body closing beyond the swing fractal.
- **Entry & Anchors:** Market execution if within $0.15 \times ATR$ of broken swing; otherwise Avellaneda-Stoikov limit order at the broken swing retest.
- **SL & TP Geometry:**
  $$\text{SL}_{BUY} = \text{SwingLow} - (\text{Spread} + \max(1.2 \times ATR, \text{AntiWickBuffer}))$$
  $$\text{TP1} = +1.2R \quad (33\% \text{ partial}), \quad \text{TP2} = +2.5R, \quad \text{TP3} = +4.0R \text{ (runner)}$$
- **Trailing:** Breakeven moved at $+1.8R$. Runner trailed at $2.2 \times ATR$.

### 3.2 `TREND_PULLBACK` (Optimal Trade Entry - OTE)
- **Theoretical Basis (§3.5, §4.1, §7.S2):** Refills orders at wholesale prices following an impulse displacement leg.
- **Formulas & Triggers:**
  - Dealing Range: $Range = High_{\text{Swing}} - Low_{\text{Swing}}$.
  - Optimal Trade Entry Zone (OTE 62%–79% Fibonacci Retracement):
    $$\text{OTE}_{BUY} = [High - 0.79 \cdot Range, \, High - 0.62 \cdot Range] \quad (\text{Anchor} = 66\%)$$
  - Fair Value Gap Consequent Encroachment (CE 50%):
    $$CE_{FVG} = \frac{Low_{\text{Bar } i} + High_{\text{Bar } i-2}}{2} \quad (\text{for Bullish FVG})$$
- **Entry Mechanics:** Limit order placed at the intersection of OTE 66% and unmitigated FVG CE inside Discount ($<50\%$ of range).
- **SL & TP Geometry:**
  $$\text{SL}_{BUY} = Low_{\text{SwingOrigin}} - (\text{Spread} + 0.25 \times ATR)$$
  $$\text{TP1} = +1.5R \quad (33\% \text{ partial}), \quad \text{TP2} = +2.8R \text{ (Dealing Range High)}, \quad \text{TP3} = +4.2R$$

### 3.3 `BREAKOUT_EXPANSION` (Volatility Squeeze Expansion)
- **Theoretical Basis (§3.4, §4.7, §7.S4):** Capitalizes on stop-loss cascades and momentum ignition out of low-volatility compression.
- **Formulas & Triggers:**
  - Transition from `COMPRESSION` ($ATR \text{ ratio} < 0.65$ or Bollinger Bandwidth $< 20\text{th}$ percentile) to `EXPANSION` ($ATR \text{ ratio} > 1.4$).
  - 20-bar Donchian Channel breakout with Marubozu expansion candle ($\text{Body} \ge 65\%$ of range).
  - Footprint Volume Delta $|DeltaScore| \ge 25.0$ and Volume $\ge 1.8 \times VolMA_{20}$.
- **SL & TP Geometry:**
  $$\text{SL}_{BUY} = \max(Low_{\text{BreakoutBar}}, \, \text{BreakoutLevel} - 1.20 \times ATR) - \text{Spread}$$
  $$\text{TP1} = +1.5R, \quad \text{TP2} = +2.8R, \quad \text{TP3} = +4.5R$$
- **Stagnation Timeout:** If trade fails to generate $+0.5R$ within 6 hours, position is closed at market.

### 3.4 `LIQUIDITY_SWEEP_REVERSAL` (Judas Swing Stop-Run Fade)
- **Theoretical Basis (§4.1, §4.2, §7.S17, SMC/ICT Playbook):** Exploits institutional liquidity engineering where retail stops are purged before institutional reversal.
- **Formulas & Triggers:**
  - BSL/SSL Sweep: Candle pierces swing extreme by $0.10 \le \frac{|\Delta Price|}{ATR} \le 4.0$, then closes back inside the range.
  - Rejection Wick: Reversal wick $\ge 45\%$ of total candle range or displacement body $\ge 38\%$.
  - Volume participation: Candle volume $\ge 35\%$ of 14-period average volume.
- **SL & TP Geometry:**
  $$\text{SL}_{BUY} = Low_{\text{SweepExtreme}} - (1.0 \times \text{Spread} + 0.20 \times ATR)$$
  $$\text{TP1} = +1.35R \quad (50\% \text{ fast cash}), \quad \text{TP2} = +2.5R \text{ (Opposing Pool)}, \quad \text{TP3} = +3.5R$$
- **Rule Invariant:** Stop is NEVER anchored at the liquidity pool level; it is anchored strictly beyond the swept wick extreme.

### 3.5 `RANGE_MEAN_REVERSION` (Auction Value Area Fade)
- **Theoretical Basis (§3.4, §4.3, §7.S12, Dalton Auction Theory):** Exploits short-term statistical excess beyond 2 standard deviations in balanced auctions.
- **Formulas & Triggers:**
  - Mandatory $ADX(14) < 22.0$ (strongly optimal when $ADX < 18.0$).
  - Bollinger Bands $(20, 2.0\sigma)$ bandwidth between 20th and 80th historical percentile.
  - RSI(14) extreme: $RSI < 35$ (BUY) or $RSI > 65$ (SELL).
  - Rejection candle: Low pierces Lower Band but closes inside with bullish wick.
- **SL & TP Geometry:**
  $$\text{SL}_{BUY} = Low_{\text{Rejection}} - 1.0 \times ATR$$
  $$\text{TP} = SMA_{20} \text{ (Middle Band Mean, Target R:R } \approx 1.6R–2.0R)$$
- **Time Stop:** Maximum holding period of 12 bars. Position is closed if mean reversion thesis does not resolve.

### 3.6 `CHOCH_STRUCTURAL_REVERSAL` (Change of Character Shift)
- **Theoretical Basis (§3.1, §4.1, §7.S17, Wyckoff Accumulation/Distribution):** Confirms genuine structural market structure shifts (MSS) against prevailing momentum.
- **Formulas & Triggers:**
  - Bullish CHoCH: Candle closes strictly above the last lower swing high with displacement body $\ge 55\%$.
  - Bearish CHoCH: Candle closes strictly below the last higher swing low.
  - Confluence: RSI divergence and multi-timeframe structural alignment.
- **SL & TP Geometry:**
  $$\text{SL}_{BUY} = Low_{\text{DisplacementOrigin}} - (\text{Spread} + 0.30 \times ATR_{H1})$$
  $$\text{TP1} = +1.5R \quad (35\% \text{ partial}), \quad \text{TP2} = +3.0R, \quad \text{TP3} = +4.5R$$

### 3.7 `MICRO_ACCOUNT_ADAPTIVE` (Survival & Compounding Engine)
- **Theoretical Basis (§9.1, `algo_methods_and_tricks.md` §6):** Solves the retail **Broker Minimum Lot (0.01) Paradox** for accounts under $\$100$.
- **Formulas & Sizing Mechanics:**
  - Automatically triggered when `account_equity < 100.0`.
  - Absolute dollar floor raised to $\$35.00$ on Gold/Crypto and $\$15.00$ on Forex, permitting high-probability 0.01 lot entries without risk budget paralysis.
  - Step-wise lot scaling: Lot sizes recalculate upward only after equity grows by at least $+10\%$.
  - Single-Position Cap: Enforces a strict maximum of 1 open position at a time.
- **SL & TP Geometry:**
  - Limit orders placed strictly in deep Discount ($<40\%$) or deep Premium ($>60\%$) to compress stop distance.
  - Fast Cash: 50% closed at $+1.0R$ to $+1.2R$ to lock risk-free status immediately.

---

### 3.8 Multi-Horizon Execution Protocols

| Parameter | Scalp Protocol (M1 / M5) | Day Trading Protocol (M15 / H1) | Swing Protocol (H4 / D1) |
| :--- | :--- | :--- | :--- |
| **Killzones** | London (07:00–10:30), NY (12:30–16:30 UTC) | London (07:00–10:00), NY (12:00–16:00 UTC) | 24-hour macro alignment |
| **5-Step Sequence** | Sweep $\to$ Rejection $\to$ Displacement $\to$ BOS $\to$ Discount Retest | H1 structure + M15 FVG CE / Breaker retest | HTF OB in Discount/Premium + H1 CHoCH |
| **Entry Mechanics** | Sniper Immediate or Limit at Micro-FVG CE | Passive Limit at M15 FVG / Breaker block | Passive Limit at HTF Order Block |
| **Structural SL** | $\text{SweepExtreme} \pm (1.0 \times \text{Spread} + 0.20 \times ATR_{M5})$ | $\text{DisplacementOrigin} \pm (1.0 \times \text{Spread} + 0.30 \times ATR_{H1})$ | $\text{OuterBoundary} \pm (1.0 \times \text{Spread} + 0.40 \times ATR_{D1})$ |
| **Asset SL Floors** | BTC $\ge \$450$, ETH $\ge \$10$, Gold $\ge \$8.00$ | BTC $\ge \$650$, ETH $\ge \$20$, Gold $\ge \$8.00$ | BTC $\ge \$650$, ETH $\ge \$30$, SOL $\ge \$2.50$ |
| **Target Milestones** | TP1: $1.35R$ (50%), TP2: $2.20R$, TP3: $3.00R$ | TP1: $1.65R$ (50%), TP2: $2.85R$, TP3: $4.00R$ | TP1: $2.20R$ (50%), TP2: $4.00R$, TP3: $5.50R$ |
| **Runner Trailing** | $0.80 \times ATR_{M5}$ | $1.20 \times ATR_{H1}$ | $1.80 \times ATR_{D1}$ |
| **Timeout Stagnation** | 45 minutes if $< 0.50R$ | 6 hours if $< 0.75R$ | 36 hours if compression persists |

---

### 3.9 Asset-Specific Algorithmic Models

#### Gold (`XAUUSD` / `GOLD.i#`)
- **Momentum Immunity Guard:** Prohibits SELL orders when intraday momentum is bullish ($TrendScore \ge 15.0$ or macro D1 bull bias).
- **Anti-Wick Shield:** $0.35 \times ATR$ volatility buffer with an absolute minimum stop floor of **$\$8.00$** ($800\text{ pips}$) and $\ge 3.5 \times \text{spread}$.
- **Swing Stop Allowance:** Wide structural breathing room up to $2.80 \times ATR$ to withstand London/NY stop hunts.
- **Asymmetric R:R:** Baseline target set to $4.2R$. Banks 33% at $+1.5R$, trailing a 67% runner at $2.60 \times ATR$.

#### Crypto (`BTCUSD`, `ETHUSD`, `SOLUSD`)
- **24/7 Continuous Trading:** Session time constraints disabled.
- **Macro Trend Filter:** Requires $ADX(14) \ge 24.0$ with confirmed BOS, or authenticated sweep + CHoCH. Prohibits shallow counter-trend fades.
- **Anti-Wick Padding:** Padded to $0.45–0.60 \times ATR$ to absorb crypto liquidation wicks.
- **SL Floors:** `BTCUSD` $\ge \$450–\$650$; `ETHUSD` $\ge \$10–\$30$; `SOLUSD` $\ge \$2.50$.
- **Fast-Cash & Trailing:** Banks 35% at $+1.0R$, trailing runners at $2.2–2.6 \times ATR$.

---

## 4. Institutional Risk Management & Capital Preservation

### 4.1 Operating Rules R1 & R2 Compliance
- **Rule R1: Risk Precedes Direction:** Every executed setup must define: (1) entry price & type, (2) structural stop price + invalidation thesis, (3) TP1, TP2, TP3 in R, (4) risk percentage, (5) lot size, and (6) R:R ratio. Unobserved prices strictly halt order emission.
- **Rule R2: Evaluation in R-Multiples:** All performance tracking, win rate targeting, milestone partials, breakeven triggers, and trailing stops are scale-free in R ($1R = |entry - initial\_sl|$). The initial risk distance is **frozen at entry** and never rescaled.

### 4.2 The 15 Authorization Conditions of `RiskEngine`
Before an order is submitted to MT5, `RiskEngine.authorize_execution` validates 15 conditions:
1. **Trading Mode Check:** Validates live broker connectivity.
2. **Account Drawdown Guard:** Enforces daily $4\%$ loss limit and $10\%$ max peak-to-trough drawdown.
3. **Symbol Circuit Breaker:** Blocks symbol if 2 consecutive losses tripped 45-min pause.
4. **Regime Circuit Breaker:** Blocks regime if 3 consecutive losses tripped 60-min pause.
5. **Loss Cool-Down Timer:** Enforces 10-minute cooldown post-loss.
6. **Max Concurrent Symbol Limit:** Maximum 1 position + 1 pending order per symbol.
7. **Same-Symbol Pending Order Deduplication:** Rejects new orders if pending order already rests on symbol.
8. **Correlation Exposure Cap:** Rejects entry if correlated asset ($r \ge 0.75$) has open exposure.
9. **Portfolio Risk Budget:** Limits aggregate portfolio risk to $2.5\%$ ($7.5\%$ for micro accounts).
10. **Spread Spike Filter:** Rejects entry if current spread $> 2.0 \times TypicalSpread$.
11. **Asian Pre-Market Blackout:** Rejects Forex entries between 01:00 and 05:00 UTC.
12. **Minimum R:R Hurdle:** Requires target R:R $\ge 1.50$ (standard $\ge 2.0$).
13. **Minimum Expected Value (EV):** Requires $EV \ge 0.40R$.
14. **Devil's Advocate Adversarial Penalty:** Rejects setup if penalty $> 50.0$ or threats $\ge 3$.
15. **Broker Trade Stops Level Buffer:** Enforces minimum broker distance floor between entry and SL.

### 4.3 Position Sizing: Fractional Kelly Criterion
Lots are calculated dynamically by `PositionSizer`:

$$\text{KellyFraction} = \frac{p \cdot b - (1 - p)}{b} \times \text{SafetyFraction } (0.25)$$

$$\text{PlannedRiskDollars} = \text{Equity} \times \min(\text{MaxRiskPct}, \, \text{KellyFraction})$$

$$\text{LotSize} = \frac{\text{PlannedRiskDollars}}{\text{RiskDist} \times \text{DollarRiskPerPriceUnit}}$$

$$\text{FinalLots} = \text{clamp}(\text{round}(\text{LotSize}, \text{VolumeStep}), \, \text{VolumeMin}, \, \text{MaxLotCap})$$

---

## 5. Dynamic Trade Lifecycle & Trailing Architecture

All active positions are continuously monitored every 2 seconds by `PositionMonitorEngine` via `exit_policy.py`:

```
[Entry Executed] ──> Frozen 1R = |Entry - SL|
       │
       ├──> [+1.0R to +1.5R]: Fast-Cash Partial Scale-Out (Banks 33%–50% profit)
       │
       ├──> [+1.8R to +2.0R]: Breakeven Trigger (SL moved to Entry + Spread Buffer)
       │
       ├──> [+2.0R]: Progressive Ratchet 1 (Lock +0.5R)
       │
       ├──> [+2.8R]: Progressive Ratchet 2 (Lock +1.2R)
       │
       ├──> [+3.8R]: Progressive Ratchet 3 (Lock +2.2R)
       │
       └──> [Beyond +2.0R]: High-Velocity ATR Runner Trail (1.2–2.6x ATR behind fractals)
```

- **Fast Cash:** Closes 33%–50% of position at $+1.0R$ to $+1.5R$, securing net positive expectancy on the trade.
- **Delayed Breakeven:** Breakeven is intentionally delayed until $+1.8R$ to $+2.0R$, padded by $\max(2.5 \times \text{pip\_size}, |RiskDist| \times 0.08)$ to prevent premature stopouts during routine retests.
- **MFE Retracement Giveback Protection:** If maximum favorable excursion reaches $\ge 1.5 \times ATR$ and price gives back $\ge 40\%$, the stop ratchets to lock $50\%$ of peak profit.
- **Adversarial Order Flow Shield:** If Footprint delta detects an opposing institutional absorption trap ($|DeltaScore| > 60$) while trade is in profit ($\ge 0.8 \times ATR$), SL is immediately tightened to Bid/Ask $\pm 0.75 \times ATR$ to lock gains before the reversal springs.

---

## 6. Platform Operations & Configuration Reference

### 6.1 Running the Platform
To launch the trading system with MetaTrader 5:

```powershell
# Run in Demo mode (Recommended for validation)
python HM_start.py demo

# Run in Live mode (Real capital)
python HM_start.py live

# Run in Paper simulation mode
python HM_start.py paper
```

### 6.2 Key Configuration Files
- `config/settings.json`: Active trading symbols, default risk parameters, MT5 magic numbers, API ports.
- `jarvis/data/symbol_registry.py`: Canonical asset specifications (contract sizes, pip sizes, pip values, margin requirements).
- `jarvis/intelligence/symbol_profile_config.py`: Asset-class behavioral priors, anti-wick shields, stop floors, and target R:Rs.
- `jarvis/execution/exit_policy.py`: Single source of truth for all milestone partials, breakeven triggers, and trailing ratchets.

### 6.3 Telemetry & Web Dashboard
The system serves an interactive dashboard on **port 8501** (accessible locally at `http://localhost:8501` and remotely via Cloudflare / Serveo HTTPS tunnels).
- `/api/telemetry_state`: Live account equity, margin level, circuit breaker states, and open positions.
- `/api/radar`: Real-time cross-asset opportunity scanner displaying setup grades (Grade A+, A, B), utility scores, and confluence points.
- `/api/history`: Real-time execution log and trade performance metrics.

---

## 7. Codebase Audit & Remediation Summary

During the complete top-to-bottom architectural audit, the following issues were resolved:
1. **Thread-Safe MT5 Lock Unification:** Unified all MT5 calls across `DataFeedEngine`, `MT5StateSynchronizer`, and `server.py` under `MT5Client._shared_lock` to eliminate memory access violations and deadlocks.
2. **Circuit Breaker & Drawdown Guard Synchronization:** Unified duplicate state machines so that `Orchestrator` and `RiskEngine` share the exact same instances. Consecutive loss counts and asset pauses are now 100% synchronized.
3. **Strategy Bandit Learning Connection:** Passed `orchestrator.strategy_bandit` directly into `DecisionEngine` and `StrategySelector`, enabling real-time Bayesian likelihood updates from closed trade outcomes.
4. **Dead Code Elimination:** Removed unused instantiations (`EnsembleStrategyBandit`, `FractionalDifferentiationTransformer`, `MeanReversionEngine`) from `DecisionEngine`.
5. **Bounded Macro News Wait:** Enforced `max_wait=1.5` in `MacroAnalyst.analyze`, preventing socket hangs from exceeding the 2.0s analyst budget and eliminating fabricated neutral scores.
6. **Take-Profit Clamping & Floor Harmonization:** Clamped institutional TP expansion to a maximum of $3.0\times$ and capped TP distances to $5.0 \times \text{risk}$, preventing unreachable profit targets.
7. **Trailing Modification Race Prevention:** Deferred trailing management exclusively to `PositionMonitorEngine` during active scans, eliminating concurrent MT5 `modify_position` collisions.
