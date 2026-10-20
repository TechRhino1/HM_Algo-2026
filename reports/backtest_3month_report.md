# HM Algo 2.0 — 3-Month Backtest on Real MT5 Data

**Mode:** calibrated per-symbol win-rate profiles  
**Data:** MT5 terminal, real H1 bars (validated non-synthetic)  
**Initial balance:** $10,000.00 at 0.5% risk per trade  
**Generated:** 2026-10-19T21:06:17.114973+00:00

## 1. Headline

| Metric | Value |
|---|---|
| Symbols traded | 1 |
| Total trades | 24 |
| Win rate | 33.33% |
| Expectancy | +0.2266 R per trade |
| Average win / loss | +2.688 R / -1.004 R |
| Payoff ratio | 2.677 |
| Total R | +5.44 R |
| Profit factor | 1.43 |
| Net profit | $+270.12 |
| Max drawdown | 1.67% |
| Sharpe / Sortino / Calmar | 0.7 / 5.97 / 1.57 |
| Expectancy (sample-uniqueness weighted) | -0.0069 R |

**Win-rate target outcome:** 2/3 symbols met the target out-of-sample. 1 met it in-sample but failed out-of-sample (i.e. the target was reachable only by overfitting).

## 2. Per-symbol results

| Symbol | Trades | WR % | Exp (R) | PF | Net $ | MaxDD % | OOS WR % | OOS Exp (R) | Target | Binding constraint |
|---|---:|---:|---:|---:|---:|---:|---:|---:|:--:|---|
| XAUUSD | 24 | 33.3 | +0.227 | 1.43 | +270.12 | 1.67 | 46.4 | +0.227 | in-sample | out-of-sample - met in-sample (51.9%) but not on purged folds; OOS win |

### 2a. What the learned regime policy contributes

Calibration reports the same out-of-sample sample twice: once with every regime enabled, and once with the regimes the learned policy has switched off removed. Both are purged out-of-sample w.r.t. the geometry and threshold; the policy itself is fitted on **training folds only**, so it has never seen the out-of-sample bars it filters. The gap between the two columns is the policy's entire contribution — read it as the honest size of the effect, not as free money.

| Symbol | OOS n (all regimes) | OOS WR | OOS Exp (R) | OOS n (policy applied) | OOS WR | OOS Exp (R) | Policy fitted on |
|---|---:|---:|---:|---:|---:|---:|---|
| XAUUSD | 283 | 45.9 | +0.004 | 278 | 46.4 | +0.227 | train folds |

## 3. Win-rate / expectancy frontier

For each target size (`tp_r`, as a multiple of initial risk) the table gives the highest win rate found at all, and the highest win rate that still carries positive expectancy. Where those two diverge, the gap is win rate that can only be bought by running a losing system. This is the direct evidence for whether the 75% target is reachable.

**USDCAD**

| tp_r | best WR | exp at best WR | trades | best WR with exp > 0 | exp | trades |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 63.4% | +0.014 | 41 | 63.4% | +0.020 | 41 |
| 1.5 | 63.2% | +0.036 | 38 | 63.2% | +0.036 | 38 |
| 2 | 62.5% | +0.096 | 24 | 62.5% | +0.096 | 24 |
| 2.5 | 62.5% | +0.082 | 24 | 62.5% | +0.082 | 24 |

**USDJPY**

| tp_r | best WR | exp at best WR | trades | best WR with exp > 0 | exp | trades |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 70.0% | +0.226 | 20 | 70.0% | +0.226 | 20 |
| 1.5 | 66.7% | +0.229 | 27 | 66.7% | +0.229 | 27 |
| 2 | 68.8% | +0.428 | 16 | 64.0% | +0.222 | 25 |
| 2.5 | 68.8% | +0.279 | 16 | 60.6% | +0.210 | 33 |

**XAUUSD**

| tp_r | best WR | exp at best WR | trades | best WR with exp > 0 | exp | trades |
|---:|---:|---:|---:|---:|---:|---:|
| 1.5 | 53.6% | +0.032 | 375 | 53.6% | +0.032 | 375 |
| 2 | 48.5% | -0.014 | 332 | 46.4% | +0.005 | 224 |
| 2.5 | 49.4% | +0.032 | 320 | 49.4% | +0.032 | 320 |

## 4. Calibrated geometry per symbol

`tp_r` is the target distance in multiples of initial risk; `be` is the breakeven trigger (off = never lock breakeven); `pc` the partial bank; `mb` the time stop in bars; `thr` the entry score threshold.

The last two columns come from the isotonic score calibration: the realised win probability the fitted map assigns to a score sitting exactly at the deployed threshold, and the Brier score of that map (lower is better; 0.25 is the skill-free baseline for a balanced sample). The calibration is monotone, so it cannot change which candidates pass — it exists to make the score interpretable and to give the AI layer a probability rather than an arbitrary index.

| Symbol | tp_r | be | pc | mb | threshold | P(win) @ thr | Brier | Enabled regimes |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| USDCAD | 1 | off | off | 48 | 0.521 | 33.3% | 0.205 | BREAKOUT, TREND_BEAR, TREND_BULL |
| USDJPY | 2.5 | off | 0.5 | 48 | 0.575 | 50.0% | 0.217 | BREAKOUT, LIQUIDITY_SWEEP, TREND_BEAR |
| XAUUSD | 4 | off | 1.5 | 200 | 0.660 | 47.5% | 0.247 | BREAKOUT, LIQUIDITY_SWEEP, TREND_BEAR, TREND_BULL, WEAK_TREND |

## 5. Portfolio risk allocation (HRP)

Not enough aligned per-symbol return history to compute HRP weights.

## 6. Entry rejection analysis

Why candidates were not traded under the calibrated profile. In calibrated mode these are the capital-protection gates plus the calibrated edge filter; the legacy 29-check stack no longer decides.

Counts are per **bar evaluated**, not per candidate: a bar on which the pipeline found no setup at all (`no directional bias`) is counted here too, because the engine has to consider and decline it. A large `no directional bias` count therefore means the pipeline rarely formed a view, not that a good trade was vetoed.

| Symbol | Top rejection reasons |
|---|---|
| XAUUSD | capital protection: Directional Bias (353); score 0.480 below calibrated threshold 0.660 (98); regime RANGE disabled by learned policy (expectancy -0. |

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
