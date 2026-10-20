# HM Algo 2.0 — 3-Month Backtest on Real MT5 Data

**Mode:** uncalibrated baseline (legacy 29-gate stack)  
**Data:** MT5 terminal, real H1 bars (validated non-synthetic)  
**Initial balance:** $10,000.00 at 0.5% risk per trade  
**Generated:** 2026-10-19T11:24:10.522127+00:00

## 1. Headline

| Metric | Value |
|---|---|
| Symbols traded | 16 |
| Total trades | 205 |
| Win rate | 36.10% |
| Expectancy | +0.0370 R per trade |
| Average win / loss | +1.156 R / -0.595 R |
| Payoff ratio | 1.942 |
| Total R | +7.58 R |
| Profit factor | 1.03 |
| Net profit | $+69.62 |
| Max drawdown | 3.34% |
| Sharpe / Sortino / Calmar | 0.16 / 0.4 / 0.21 |
| Expectancy (sample-uniqueness weighted) | -0.2237 R |

## 2. Per-symbol results

| Symbol | Trades | WR % | Exp (R) | PF | Net $ | MaxDD % | OOS WR % | OOS Exp (R) | Target | Binding constraint |
|---|---:|---:|---:|---:|---:|---:|---:|---:|:--:|---|
| EURUSD | 25 | 32.0 | +0.072 | 1.46 | +100.52 | 0.83 | 0.0 | +0.000 | — | — |
| NZDUSD | 21 | 23.8 | -0.024 | 1.02 | +5.89 | 1.05 | 0.0 | +0.000 | — | — |
| AUDUSD | 20 | 20.0 | -0.362 | 0.35 | -185.18 | 1.85 | 0.0 | +0.000 | — | — |
| USDCAD | 18 | 55.6 | +0.432 | 2.54 | +161.98 | 0.48 | 0.0 | +0.000 | — | — |
| XAUUSD | 16 | 43.8 | -0.073 | 1.10 | +20.54 | 0.73 | 0.0 | +0.000 | — | — |
| GBPUSD | 15 | 40.0 | +0.128 | 1.13 | +18.63 | 0.60 | 0.0 | +0.000 | — | — |
| USDCHF | 14 | 42.9 | +0.208 | 2.24 | +97.30 | 0.60 | 0.0 | +0.000 | — | — |
| XAGUSD | 13 | 46.1 | +0.301 | 1.04 | +9.85 | 1.77 | 0.0 | +0.000 | — | — |
| SOLUSD | 12 | 41.7 | +0.126 | 0.80 | -28.01 | 0.95 | 0.0 | +0.000 | — | — |
| NAS100 | 11 | 27.3 | -0.191 | 0.47 | -96.55 | 1.10 | 0.0 | +0.000 | — | — |
| BTCUSD | 9 | 44.4 | +0.252 | 2.05 | +81.12 | 0.30 | 0.0 | +0.000 | — | — |
| GER40 | 9 | 22.2 | -0.182 | 0.51 | -57.76 | 1.14 | 0.0 | +0.000 | — | — |
| US30 | 9 | 33.3 | -0.175 | 0.62 | -36.51 | 0.85 | 0.0 | +0.000 | — | — |
| ETHUSD | 8 | 25.0 | -0.044 | 0.81 | -13.50 | 0.38 | 0.0 | +0.000 | — | — |
| UK100 | 5 | 60.0 | +0.013 | 0.85 | -8.70 | 0.59 | 0.0 | +0.000 | — | — |
| USDJPY | 0 | 0.0 | +0.000 | 0.00 | +0.00 | 0.00 | 0.0 | +0.000 | — | — |

## 5. Portfolio risk allocation (HRP)

Inverse-variance weights from the HRP allocator over per-symbol daily R:

| Symbol | Risk weight |
|---|---:|
| UK100 | 29.52% |
| ETHUSD | 16.92% |
| US30 | 10.91% |
| GER40 | 7.80% |
| USDCHF | 4.57% |
| GBPUSD | 4.27% |
| BTCUSD | 3.50% |
| SOLUSD | 3.36% |
| XAUUSD | 3.30% |
| NAS100 | 3.08% |
| NZDUSD | 2.97% |
| AUDUSD | 2.86% |
| USDCAD | 2.68% |
| EURUSD | 2.46% |
| XAGUSD | 1.81% |

## 6. Entry rejection analysis

Why candidates were not traded under the calibrated profile. In calibrated mode these are the capital-protection gates plus the calibrated edge filter; the legacy 29-check stack no longer decides.

Counts are per **bar evaluated**, not per candidate: a bar on which the pipeline found no setup at all (`no directional bias`) is counted here too, because the engine has to consider and decline it. A large `no directional bias` count therefore means the pipeline rarely formed a view, not that a good trade was vetoed.

| Symbol | Top rejection reasons |
|---|---|
| EURUSD | Adversarial risk: Attempting SELL in unfavorable DISCOUNT zone relative to equilibrium. (90); Failed quality check: AI Multi-Score Gate. (84); Failed  |
| NZDUSD | Awaiting validation check: Devil Adversarial Guard. (111); Failed quality check: Directional Bias. (99); Adversarial risk: Attempting SELL in unfavora |
| AUDUSD | Failed quality check: Forex Prime Session. (80); Failed quality check: AI Multi-Score Gate. (78); Adversarial risk: Attempting BUY in unfavorable PREM |
| USDCAD | Awaiting validation check: Devil Adversarial Guard. (58); Failed quality check: Directional Bias. (45); Adversarial risk: Attempting SELL in unfavorab |
| XAUUSD | Failed quality check: Strategy Viable. (770); Failed quality check: Forex Prime Session. (558); Failed quality check: Directional Bias. (374) |
| GBPUSD | Failed quality check: AI Multi-Score Gate. (55); Failed quality check: Forex Prime Session. (49); Adversarial risk: Attempting BUY in unfavorable PREM |
| USDCHF | Negative / insufficient mathematical edge (EV: $0.00). (481); Adversarial risk: Attempting BUY in unfavorable PREMIUM zone relative to equilibrium. (2 |
| XAGUSD | Negative / insufficient mathematical edge (EV: $0.00). (857); Failed quality check: Forex Prime Session. (781); Failed quality check: Directional Bias |
| SOLUSD | Failed quality check: Crypto Macro Trend Filter. (282); Failed quality check: SOL Confluence Guard. (250); Adversarial risk: Attempting BUY in unfavor |
| NAS100 | Failed quality check: Forex Prime Session. (165); Failed quality check: Directional Bias. (98); Adversarial risk: Attempting SELL in unfavorable DISCO |
| BTCUSD | Failed quality check: Crypto Macro Trend Filter. (95); Failed quality check: AI Multi-Score Gate. (74); Failed quality check: Directional Bias. (58) |
| GER40 | Negative / insufficient mathematical edge (EV: $0.00). (567); Failed quality check: Forex Prime Session. (433); Failed quality check: Index Trend Alig |
| US30 | Failed quality check: US30 Confluence Guard. (445); Failed quality check: Forex Prime Session. (423); Adversarial risk: Attempting SELL in unfavorable |
| ETHUSD | Failed quality check: Crypto Macro Trend Filter. (148); Failed quality check: Directional Bias. (115); Awaiting validation check: Crypto Macro Trend F |
| UK100 | Negative / insufficient mathematical edge (EV: $0.00). (510); Failed quality check: Forex Prime Session. (381); Adversarial risk: Attempting SELL in u |
| USDJPY | Failed quality check: JPY Momentum Guard. (1324); Negative / insufficient mathematical edge (EV: $0.00). (1064); Failed quality check: Forex Prime Ses |

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
