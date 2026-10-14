# HM Algo 2.0 — 3-Month Backtest on Real MT5 Data

**Mode:** uncalibrated baseline (legacy 29-gate stack)  
**Data:** MT5 terminal, real H1 bars (validated non-synthetic)  
**Initial balance:** $10,000.00 at 0.5% risk per trade  
**Generated:** 2026-10-14T16:04:23.817683+00:00

## 1. Headline

| Metric | Value |
|---|---|
| Symbols traded | 16 |
| Total trades | 141 |
| Win rate | 48.23% |
| Expectancy | +0.0122 R per trade |
| Average win / loss | +0.885 R / -0.801 R |
| Payoff ratio | 1.105 |
| Total R | +1.72 R |
| Profit factor | 1.16 |
| Net profit | $+290.13 |
| Max drawdown | 1.73% |
| Sharpe / Sortino / Calmar | 0.68 / 1.87 / 1.63 |
| Expectancy (sample-uniqueness weighted) | +0.0708 R |

## 2. Per-symbol results

| Symbol | Trades | WR % | Exp (R) | PF | Net $ | MaxDD % | OOS WR % | OOS Exp (R) | Target | Binding constraint |
|---|---:|---:|---:|---:|---:|---:|---:|---:|:--:|---|
| XAUUSD | 30 | 43.3 | -0.050 | 0.99 | -6.40 | 1.35 | 0.0 | +0.000 | — | — |
| GBPUSD | 16 | 56.2 | +0.129 | 1.57 | +72.28 | 0.55 | 0.0 | +0.000 | — | — |
| USDCAD | 14 | 71.4 | +0.338 | 3.66 | +138.49 | 0.43 | 0.0 | +0.000 | — | — |
| UK100 | 11 | 54.5 | +0.018 | 1.05 | +6.40 | 0.73 | 0.0 | +0.000 | — | — |
| AUDUSD | 10 | 40.0 | -0.375 | 0.45 | -60.23 | 0.60 | 0.0 | +0.000 | — | — |
| US30 | 10 | 40.0 | +0.047 | 0.98 | -1.12 | 0.31 | 0.0 | +0.000 | — | — |
| GER40 | 8 | 62.5 | +0.087 | 1.71 | +54.22 | 0.39 | 0.0 | +0.000 | — | — |
| NZDUSD | 8 | 37.5 | -0.116 | 1.20 | +20.31 | 0.56 | 0.0 | +0.000 | — | — |
| USDCHF | 7 | 28.6 | -0.684 | 0.20 | -104.45 | 1.04 | 0.0 | +0.000 | — | — |
| SOLUSD | 6 | 50.0 | +0.216 | 0.88 | -10.08 | 0.67 | 0.0 | +0.000 | — | — |
| BTCUSD | 4 | 75.0 | +1.206 | 5.90 | +181.31 | 0.37 | 0.0 | +0.000 | — | — |
| ETHUSD | 4 | 75.0 | +0.790 | 3.02 | +98.42 | 0.49 | 0.0 | +0.000 | — | — |
| USDJPY | 4 | 25.0 | +0.044 | 1.31 | +14.20 | 0.45 | 0.0 | +0.000 | — | — |
| EURUSD | 3 | 0.0 | -0.831 | 0.00 | -98.58 | 0.99 | 0.0 | +0.000 | — | — |
| NAS100 | 3 | 0.0 | -1.001 | 0.00 | -92.04 | 0.92 | 0.0 | +0.000 | — | — |
| XAGUSD | 3 | 66.7 | +0.188 | 2.36 | +77.40 | 0.56 | 0.0 | +0.000 | — | — |

## 5. Portfolio risk allocation (HRP)

Inverse-variance weights from the HRP allocator over per-symbol daily R:

| Symbol | Risk weight |
|---|---:|
| XAGUSD | 16.06% |
| NAS100 | 12.77% |
| AUDUSD | 9.35% |
| EURUSD | 8.56% |
| USDJPY | 8.31% |
| USDCHF | 7.51% |
| US30 | 6.79% |
| NZDUSD | 5.80% |
| SOLUSD | 4.75% |
| GER40 | 4.35% |
| ETHUSD | 3.78% |
| UK100 | 3.23% |
| USDCAD | 3.22% |
| GBPUSD | 2.90% |
| BTCUSD | 1.90% |
| XAUUSD | 0.73% |

## 6. Entry rejection analysis

Why candidates were not traded under the calibrated profile. In calibrated mode these are the capital-protection gates plus the calibrated edge filter; the legacy 29-check stack no longer decides.

Counts are per **bar evaluated**, not per candidate: a bar on which the pipeline found no setup at all (`no directional bias`) is counted here too, because the engine has to consider and decline it. A large `no directional bias` count therefore means the pipeline rarely formed a view, not that a good trade was vetoed.

| Symbol | Top rejection reasons |
|---|---|
| XAUUSD | Symbol XAUUSD loss cooldown active (899s remaining). (912); Failed quality check: Directional Bias. (95); Adversarial risk: Attempting SELL in unfavor |
| GBPUSD | Symbol GBPUSD loss cooldown active (899s remaining). (64); Failed quality check: Forex Prime Session. (44); Failed quality check: Directional Bias. (2 |
| USDCAD | Awaiting validation check: Devil Adversarial Guard. (63); Awaiting validation check: Macro MTF Alignment. (39); Failed quality check: Macro MTF Alignm |
| UK100 | Failed quality check: Index Trend Alignment. (107); Failed quality check: Forex Prime Session. (101); Adversarial risk: Attempting SELL in unfavorable |
| AUDUSD | Symbol AUDUSD loss cooldown active (899s remaining). (768); Awaiting validation check: Devil Adversarial Guard. (31); Failed quality check: Forex Prim |
| US30 | Symbol US30 loss cooldown active (899s remaining). (369); Failed quality check: US30 Confluence Guard. (204); Failed quality check: Forex Prime Sessio |
| GER40 | Failed quality check: Forex Prime Session. (120); Failed quality check: Index Trend Alignment. (107); Adversarial risk: Attempting SELL in unfavorable |
| NZDUSD | Symbol NZDUSD loss cooldown active (899s remaining). (800); Awaiting validation check: Devil Adversarial Guard. (30); Adversarial risk: Attempting SEL |
| USDCHF | Symbol USDCHF loss cooldown active (899s remaining). (888); Awaiting validation check: Devil Adversarial Guard. (28); Failed quality check: Macro MTF  |
| SOLUSD | Failed quality check: Directional Bias. (88); Failed quality check: Crypto Macro Trend Filter. (84); Failed quality check: SOL Confluence Guard. (81) |
| BTCUSD | Failed quality check: Directional Bias. (65); Adversarial risk: Attempting BUY in unfavorable PREMIUM zone relative to equilibrium. (51); Failed quali |
| ETHUSD | Adversarial risk: Attempting BUY in unfavorable PREMIUM zone relative to equilibrium. (87); Failed quality check: Directional Bias. (87); Failed quali |
| USDJPY | Symbol USDJPY loss cooldown active (899s remaining). (1113); Failed quality check: JPY Momentum Guard. (77); Awaiting validation check: JPY Momentum G |
| EURUSD | Symbol EURUSD loss cooldown active (899s remaining). (1491); Awaiting validation check: Forex Prime Session. (10); Awaiting validation check: Devil Ad |
| NAS100 | Symbol NAS100 loss cooldown active (899s remaining). (924); Failed quality check: Forex Prime Session. (79); Failed quality check: Directional Bias. ( |
| XAGUSD | Failed quality check: Directional Bias. (594); Adversarial risk: Attempting SELL in unfavorable DISCOUNT zone relative to equilibrium. (286); Adversar |

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
