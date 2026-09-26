# HM Algo 2.0 — 3-Month Backtest on Real MT5 Data

**Mode:** uncalibrated baseline (legacy 29-gate stack)  
**Data:** MT5 terminal, real H1 bars (validated non-synthetic)  
**Initial balance:** $10,000.00 at 0.5% risk per trade  
**Generated:** 2026-09-26T22:43:00.260482+00:00

## 1. Headline

| Metric | Value |
|---|---|
| Symbols traded | 9 |
| Total trades | 202 |
| Win rate | 43.56% |
| Expectancy | -0.1893 R per trade |
| Average win / loss | +0.810 R / -0.961 R |
| Payoff ratio | 0.843 |
| Total R | -38.24 R |
| Profit factor | 0.67 |
| Net profit | $-965.79 |
| Max drawdown | 11.42% |
| Sharpe / Sortino / Calmar | -2.27 / -5.37 / -0.83 |
| Expectancy (sample-uniqueness weighted) | -0.1129 R |

## 2. Per-symbol results

| Symbol | Trades | WR % | Exp (R) | PF | Net $ | MaxDD % | OOS WR % | OOS Exp (R) | Target | Binding constraint |
|---|---:|---:|---:|---:|---:|---:|---:|---:|:--:|---|
| BTCUSD | 68 | 29.4 | -0.373 | 0.43 | -493.04 | 5.22 | 0.0 | +0.000 | — | — |
| GBPUSD | 47 | 61.7 | -0.046 | 1.10 | +56.49 | 1.62 | 0.0 | +0.000 | — | — |
| XAUUSD | 34 | 35.3 | -0.221 | 0.61 | -307.88 | 3.08 | 0.0 | +0.000 | — | — |
| SOLUSD | 21 | 47.6 | +0.146 | 1.14 | +26.08 | 0.87 | 0.0 | +0.000 | — | — |
| NAS100 | 11 | 36.4 | -0.102 | 0.73 | -53.99 | 0.98 | 0.0 | +0.000 | — | — |
| EURUSD | 9 | 44.4 | +0.086 | 0.98 | -2.60 | 0.71 | 0.0 | +0.000 | — | — |
| US30 | 8 | 37.5 | -0.620 | 0.05 | -136.81 | 1.38 | 0.0 | +0.000 | — | — |
| ETHUSD | 4 | 25.0 | -0.227 | 0.34 | -54.04 | 0.54 | 0.0 | +0.000 | — | — |
| USDJPY | 0 | 0.0 | +0.000 | 0.00 | +0.00 | 0.00 | 0.0 | +0.000 | — | — |

## 5. Portfolio risk allocation (HRP)

Inverse-variance weights from the HRP allocator over per-symbol daily R:

| Symbol | Risk weight |
|---|---:|
| ETHUSD | 41.73% |
| US30 | 23.71% |
| EURUSD | 10.11% |
| NAS100 | 9.99% |
| SOLUSD | 5.57% |
| GBPUSD | 5.12% |
| XAUUSD | 2.37% |
| BTCUSD | 1.41% |

## 6. Entry rejection analysis

Why candidates were not traded under the calibrated profile. In calibrated mode these are the capital-protection gates plus the calibrated edge filter; the legacy 29-check stack no longer decides.

Counts are per **bar evaluated**, not per candidate: a bar on which the pipeline found no setup at all (`no directional bias`) is counted here too, because the engine has to consider and decline it. A large `no directional bias` count therefore means the pipeline rarely formed a view, not that a good trade was vetoed.

| Symbol | Top rejection reasons |
|---|---|
| BTCUSD | Failed quality check: Crypto Macro Trend Filter. (661); Failed quality check: AI Multi-Score Gate. (453); Failed quality check: Risk/Reward >= 1.5. (3 |
| GBPUSD | Failed quality check: Directional Bias. (152); Failed quality check: AI Multi-Score Gate. (145); Failed quality check: Forex Prime Session. (120) |
| XAUUSD | Failed quality check: Directional Bias. (219); Risk Ceiling Exceeded: Minimum tradeable lot size exceeds safe account risk limits. (198); Awaiting val |
| SOLUSD | Negative / insufficient mathematical edge (EV: $0.00). (719); Failed quality check: Crypto Macro Trend Filter. (700); Failed quality check: SOL Conflu |
| NAS100 | Failed quality check: Forex Prime Session. (624); Failed quality check: Index Trend Alignment. (424); Failed quality check: Macro MTF Alignment. (380) |
| EURUSD | Failed quality check: Strategy Viable. (618); Awaiting validation check: Strategy Viable. (478); Failed quality check: Forex Prime Session. (471) |
| US30 | Failed quality check: US30 Confluence Guard. (861); Failed quality check: Forex Prime Session. (826); Failed quality check: Index Trend Alignment. (56 |
| ETHUSD | Failed quality check: Crypto Macro Trend Filter. (952); Awaiting validation check: Strategy Viable. (884); Failed quality check: Strategy Viable. (678 |
| USDJPY | Negative / insufficient mathematical edge (EV: $0.00). (1171); Failed quality check: JPY Momentum Guard. (1132); Failed quality check: Forex Prime Ses |

## 7. Method and honesty notes

**Calibration is walk-forward.** Each symbol's geometry and entry threshold were chosen only on training folds and reported on purged out-of-sample folds (`PurgedKFold`, with the embargo excluded from both train and test). The deployed configuration is the one the folds agree on most often, not the best in-sample point.

**Win rate is a constrained objective, not a bare target.** The calibrator maximises expectancy subject to `win_rate >= target`, `trades >= min_trades` and `expectancy > 0`. A high win rate is trivially obtainable by shrinking the target relative to the stop, so a profile that reached 75% with negative expectancy would be rejected.

**The regime policy is fitted out-of-sample.** Which regimes are switched off is learned from trades the fold-selected configuration would have taken *inside its training windows*, never from the out-of-sample bars it then filters. Fitting the policy on the same sample it filters is a filter tuned to the test set: measured on this data it moved the aggregate out-of-sample result from roughly break-even to about +52R, which is the size of the artefact. Section 2a publishes both figures so the contribution can be judged rather than assumed.

**Intrabar ordering is conservative.** A bar whose range spans both the stop and the target is booked as a loss, because OHLC data does not reveal which came first. The previous engine resolved this optimistically, which inflated win rate exactly where the target lives.

**The backtest is hermetic.** All persistent-state reads and writes on the decision path are disabled during simulation (`jarvis.config.runtime`), so results are reproducible and cannot be influenced by live trade history.

### Known limitations

* Three months of H1 data bounds the achievable sample: with one-position-at-a-time and a time stop, a symbol yields roughly `bars / max_bars` trades. Per-symbol win rates on 20-40 trades carry wide confidence intervals.
* Costs are modelled as the realised spread from the data plus the symbol profile's commission (zero on XM Ultra Low). Swap/financing is not modelled.
* Regime labels and the candidate set come from the same pipeline under test; a regime that the pipeline cannot detect will not appear here.
