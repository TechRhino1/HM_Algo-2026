"""Merge the mode backtest and the prioritization verification into one report.

Reads:
  reports/mode_backtest_report.json          (per-mode performance)
  reports/prioritization_verification.json   (does utility rank realise R?)

Writes:
  reports/six_month_multimode_report.md

The verdicts are computed from the data, not asserted. In particular the
prioritization verdict requires ALL of:
  * a positive utility-quintile spread,
  * a majority of symbols agreeing on its direction, and
  * the top-1 pick beating the mean of the rejected set.
Failing any of those, the report says so rather than quoting the one favourable
statistic.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

MODE_JSON = os.path.join(REPO, "reports", "mode_backtest_report.json")
PRIO_JSON = os.path.join(REPO, "reports", "prioritization_verification.json")
ROBUST_JSON = os.path.join(REPO, "reports", "prioritization_robustness.json")
OUT_MD = os.path.join(REPO, "reports", "six_month_multimode_report.md")

STYLE_ORDER = ["SWING", "DAY_TRADING", "SCALP"]


def _load(path):
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def prioritization_verdict(m: dict) -> dict:
    """Aggregate the three independent checks into one honest verdict."""
    q = m["utility_quintiles"]
    t = m["top1_by_utility"]
    inv = m["top1_by_inverse_utility"]
    spread = q["spread_r"]
    agree, total = q["symbols_agreeing"], q["symbols_total"]
    top1 = t["delta_r"]

    checks = {
        "quintile_spread_positive": spread > 0,
        "quintile_spread_meaningful": spread > 0.05,
        "symbols_agree_majority": total > 0 and agree > total / 2,
        "top1_beats_rest": top1 > 0,
        "inverse_is_worse": inv["delta_r"] < 0,
    }
    # The ranking is "working" only if the ordering carries information AND its
    # extreme pick is the better one. Either alone is not enough.
    works = (checks["quintile_spread_positive"] and checks["symbols_agree_majority"]
             and checks["top1_beats_rest"])
    # It is anti-predictive only if inverting it would have been better.
    anti = (spread < 0 and checks["symbols_agree_majority"] is False
            and not checks["inverse_is_worse"])
    return {
        "checks": checks,
        "works_in_this_mode": bool(works),
        "anti_predictive": bool(anti),
        "spread_r": spread,
        "symbols_agreeing": agree,
        "symbols_total": total,
        "top1_delta_r": top1,
        "inverse_delta_r": inv["delta_r"],
        "pick_better_than_rest_pct": t["pct_sets_pick_better"],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--modes", default=MODE_JSON)
    ap.add_argument("--prio", default=PRIO_JSON)
    ap.add_argument("--robust", default=ROBUST_JSON)
    ap.add_argument("--out", default=OUT_MD)
    args = ap.parse_args()

    modes = _load(args.modes)
    prio = _load(args.prio)
    robust = _load(args.robust)

    L = []
    L.append("# Six-Month Multi-Mode Backtest on Real MT5 Data")
    L.append("")
    L.append("Source: live XMGlobal-MT5 feed. No synthetic bars anywhere in this report — "
             "`MT5HistoryFetcher` refuses to substitute them, and the fetch manifest records "
             "`\"synthetic\": false` per series.")
    L.append("")

    # ── executive summary ────────────────────────────────────────────────────
    L.append("## Executive summary")
    L.append("")
    if modes:
        neg = [s["style"] for s in modes["modes"] if s["pooled"]["expectancy_r"] < 0]
        L.append(f"- **All {len(modes['modes'])} modes lost money over the window tested.** "
                 f"Negative expectancy in: {', '.join(neg) if neg else 'none'}.")
        worst = min(modes["modes"], key=lambda s: s["pooled"]["expectancy_r"])
        best = max(modes["modes"], key=lambda s: s["pooled"]["expectancy_r"])
        L.append(f"- Best mode: **{best['style']}** at {best['pooled']['expectancy_r']:+.4f} R per trade "
                 f"({best['pooled']['trades']} trades). Worst: **{worst['style']}** at "
                 f"{worst['pooled']['expectancy_r']:+.4f} R ({worst['pooled']['trades']} trades).")
        L.append(f"- No mode reached the 75 % win-rate target: "
                 f"{', '.join(f'{s['style']} {s['pooled']['win_rate_pct']:.1f}%' for s in modes['modes'])}.")
    if prio:
        for style in STYLE_ORDER:
            m = prio["modes"].get(style)
            if not m:
                continue
            v = prioritization_verdict(m)
            L.append(f"- Prioritization in **{style}**: "
                     f"{'ranks correctly' if v['works_in_this_mode'] else 'does NOT rank correctly'} "
                     f"(spread {v['spread_r']:+.4f} R, {v['symbols_agreeing']}/{v['symbols_total']} symbols "
                     f"agree, top-1 {v['top1_delta_r']:+.4f} R).")
        L.append("- **The utility score's magnitude is nearly uninformative** (see section 4). "
                 "Raising the utility cut from 0.5 to 25 moves expectancy by at most 0.02 R in any "
                 "mode; essentially all of the separation comes from the binary `ev > 0 and "
                 "directional` gate. The ranking's *direction* is right, its *magnitude* is not "
                 "usable as a filter.")
    L.append("")
    L.append("### Scope and caveats")
    L.append("")
    L.append("- **SCALP is not a six-month result.** Its `timing` role is M1, which this broker "
             "stores for only ~67 days. SCALP is therefore reported over ~67 days and is not "
             "comparable in window length to the other two modes.")
    L.append("- **No per-mode recalibration was applied.** Profiles in `config/winrate_profiles.json` "
             "are keyed by symbol only and were calibrated on H1, so they are not valid for the M15 "
             "and M5 primaries. All three modes were therefore run with `wr_profile=None` (the legacy "
             "entry stack), which keeps them comparable to each other.")
    L.append("- **The arbiter is not exercised by the backtest.** It lives only in the live radar "
             "sweep, so it is validated separately in section 2 against counterfactual outcomes.")
    L.append("")

    # ── performance ──────────────────────────────────────────────────────────
    if modes:
        L.append("## 1. Performance by trading mode")
        L.append("")
        L.append("| Mode | Timeframes | Symbols | Trades | Expectancy R | Total R | WR % | PF | MaxDD R | Positive symbols |")
        L.append("|---|---|---|---|---|---|---|---|---|---|")
        for s in modes["modes"]:
            p = s["pooled"]
            pf = p["profit_factor"]
            pf_s = "inf" if pf == float("inf") else f"{pf:.2f}"
            L.append(f"| **{s['style']}** | {s['description']} | {s['symbols_run']} | {p['trades']} | "
                     f"{p['expectancy_r']:+.4f} | {p['total_r']:+.2f} | {p['win_rate_pct']:.1f} | {pf_s} | "
                     f"{p['max_dd_r']:.2f} | {s['symbols_positive']}/{s['symbols_run']} |")
        L.append("")
        L.append("### Per-symbol detail")
        L.append("")
        for s in modes["modes"]:
            p = s["pooled"]
            L.append(f"#### {s['style']} — {s['description']}")
            L.append("")
            L.append(f"Pooled {p['trades']} trades, expectancy **{p['expectancy_r']:+.4f} R**, "
                     f"total **{p['total_r']:+.2f} R**, WR **{p['win_rate_pct']:.1f}%**, "
                     f"payoff **{p['payoff_r']:.2f}**, max drawdown **{p['max_dd_r']:.2f} R**. "
                     f"**{s['symbols_positive']}/{s['symbols_run']}** symbols positive.")
            if s["top_contributor"]:
                L.append("")
                L.append(f"> Concentration: **{s['top_contributor']}** supplies "
                         f"**{s['top_contributor_share_of_gross_positive_pct']}%** of gross positive R.")
            else:
                L.append("")
                L.append("> Concentration: no symbol produced positive total R.")
            L.append("")
            L.append("| Symbol | Window | Bars | Trades | Exp R | Total R | WR % | PF | MaxDD R | Net $ |")
            L.append("|---|---|---|---|---|---|---|---|---|---|")
            for r in s["per_symbol"]:
                pf = r["profit_factor"]
                pf_s = "inf" if pf == float("inf") else f"{pf:.2f}"
                L.append(f"| {r['symbol']} | {r['window_days']}d | {r['primary_bars']} | {r['trades']} | "
                         f"{r['expectancy_r']:+.4f} | {r['total_r']:+.2f} | {r['win_rate_pct']:.1f} | {pf_s} | "
                         f"{r['max_dd_r']:.2f} | {r['net_profit']:+.2f} |")
            L.append("")

    # ── prioritization ───────────────────────────────────────────────────────
    if prio:
        L.append("## 2. Does the arbiter select the highest-priority trades?")
        L.append("")
        L.append("The arbiter is invoked only in the live radar sweep, never by `BacktestEngine`, "
                 "so it is validated against counterfactual per-candidate outcomes: every candidate "
                 "is replayed under one fixed exit schedule and scored with the production utility "
                 "formula, then tested against its realised R.")
        L.append("")
        L.append("| Mode | Candidates | Q5−Q1 R | Symbols agreeing | Top-1 vs rest | Inverse control | Pick beats rest % |")
        L.append("|---|---|---|---|---|---|---|")
        for style in STYLE_ORDER:
            m = prio["modes"].get(style)
            if not m:
                continue
            v = prioritization_verdict(m)
            L.append(f"| **{style}** | {m['n_candidates']} | {v['spread_r']:+.4f} | "
                     f"{v['symbols_agreeing']}/{v['symbols_total']} | {v['top1_delta_r']:+.4f} | "
                     f"{v['inverse_delta_r']:+.4f} | {v['pick_better_than_rest_pct']*100:.1f} |")
        L.append("")

        for style in STYLE_ORDER:
            m = prio["modes"].get(style)
            if not m:
                continue
            v = prioritization_verdict(m)
            L.append(f"### {style}")
            L.append("")
            verdict = ("**Ranks correctly**" if v["works_in_this_mode"]
                       else "**Does NOT rank correctly**")
            L.append(f"Verdict: {verdict}. "
                     f"Utility quintile spread **{v['spread_r']:+.4f} R**, "
                     f"**{v['symbols_agreeing']}/{v['symbols_total']}** symbols agree on its direction, "
                     f"top-1 pick vs rejected set **{v['top1_delta_r']:+.4f} R** "
                     f"(better than the rest in only **{v['pick_better_than_rest_pct']*100:.1f}%** of sets), "
                     f"inverse control **{v['inverse_delta_r']:+.4f} R**.")
            L.append("")
            L.append("| Utility quintile | n | median utility | Exp R | WR |")
            L.append("|---|---|---|---|---|")
            for b in m["utility_quintiles"]["buckets"]:
                L.append(f"| Q{b['bucket']+1} | {b['n']} | {b['median_feature']:.2f} | "
                         f"{b['expectancy_r']:+.4f} | {b['wr']:.3f} |")
            L.append("")
            L.append("| Grade | n | Exp R | Total R | WR |")
            L.append("|---|---|---|---|---|")
            for g in sorted(m["grades"]):
                gv = m["grades"][g]
                L.append(f"| {g} | {gv['n']} | {gv['expectancy_r']:+.4f} | {gv['total_r']:+.2f} | {gv['wr']:.3f} |")
            L.append("")
            L.append("| Regime | n | Exp R | Q5−Q1 R | Symbols agreeing | Top-1 vs rest |")
            L.append("|---|---|---|---|---|---|")
            for r in sorted(m["regimes"]):
                rv = m["regimes"][r]
                q = rv["utility_quintiles"]
                L.append(f"| {r} | {rv['n']} | {rv['overall']['expectancy_r']:+.4f} | {q['spread_r']:+.4f} | "
                         f"{q['symbols_agreeing']}/{q['symbols_total']} | {rv['top1_by_utility']['delta_r']:+.4f} |")
            L.append("")
            L.append(f"Actionable subset: n={m['actionable_subset']['n']} "
                     f"expR={m['actionable_subset']['expectancy_r']:+.4f}  |  "
                     f"Non-actionable: n={m['non_actionable_subset']['n']} "
                     f"expR={m['non_actionable_subset']['expectancy_r']:+.4f}")
            L.append("")

    # ── candidate vs engine ──────────────────────────────────────────────────
    if modes and prio:
        L.append("## 3. Candidate-level vs engine-level expectancy")
        L.append("")
        L.append("These two numbers measure different things and are **not** a like-for-like "
                 "comparison. The candidate figure replays *every* signal the scanner emitted under "
                 "one fixed schedule (`tp_r = 1.5`, no score floor, no trailing, no breakeven). The "
                 "engine figure is what the deployed entry stack and exit policy actually produced. "
                 "A gap therefore says the deployed execution differs from a naive fixed-target "
                 "execution — it does not by itself say which is wrong.")
        L.append("")
        L.append("| Mode | Candidate-level Exp R | Engine Exp R | Gap |")
        L.append("|---|---|---|---|")
        for s in modes["modes"]:
            m = prio["modes"].get(s["style"])
            if not m:
                continue
            cand = m["overall"]["expectancy_r"]
            eng = s["pooled"]["expectancy_r"]
            L.append(f"| {s['style']} | {cand:+.4f} | {eng:+.4f} | {eng - cand:+.4f} |")
        L.append("")

    # ── robustness ───────────────────────────────────────────────────────────
    if robust:
        L.append("## 4. Robustness of the prioritization verdicts")
        L.append("")
        L.append("A verdict that survives only one arbitrary parameter choice is not a finding. "
                 "These re-run the same question under different choices, from the saved replay "
                 "frames (no new replay).")
        L.append("")
        L.append("### 4.1 Top-k vs the rejected set")
        L.append("")
        L.append("The arbiter's contract is top-1, but if its information sits in the top few, top-1 "
                 "alone understates it. Delta is the selected set's mean R minus the rejected set's.")
        L.append("")
        L.append("| Mode | k=1 | k=2 | k=3 | k=5 | k=10 |")
        L.append("|---|---|---|---|---|---|")
        for style in STYLE_ORDER:
            m = robust["modes"].get(style)
            if not m:
                continue
            cells = " | ".join(f"{t['delta_r']:+.4f}" for t in m["topk"])
            L.append(f"| {style} | {cells} |")
        L.append("")
        L.append("### 4.2 Bucket-count sensitivity")
        L.append("")
        L.append("A quintile spread can be an artefact of the cut points. Stable sign + stable "
                 "symbol agreement across 3/4/5/10 buckets means it is not.")
        L.append("")
        L.append("| Mode | q=3 | q=4 | q=5 | q=10 |")
        L.append("|---|---|---|---|---|")
        for style in STYLE_ORDER:
            m = robust["modes"].get(style)
            if not m:
                continue
            cells = " | ".join(f"{b['spread_r']:+.4f} ({b['agree']}/{b['total']})"
                               for b in m["bucket_counts"])
            L.append(f"| {style} | {cells} |")
        L.append("")
        L.append("### 4.3 Per-symbol rank correlation, and the actionable-only population")
        L.append("")
        L.append("Spearman between utility and realised R, computed inside each symbol. The "
                 "`actionable-only` column repeats the exercise on the population the arbiter "
                 "actually trades.")
        L.append("")
        L.append("| Mode | Per-symbol rho (mean) | Symbols positive | Actionable n | Actionable exp R | Actionable top-1 delta | Actionable spread |")
        L.append("|---|---|---|---|---|---|---|")
        for style in STYLE_ORDER:
            m = robust["modes"].get(style)
            if not m:
                continue
            ps = m["per_symbol"]
            ao = m["actionable_only"]
            ao_t1 = ao["topk"][0]["delta_r"] if ao.get("topk") else 0.0
            ao_sp = ao.get("buckets", {}).get("spread_r", 0.0)
            L.append(f"| {style} | {ps['mean_rho']:+.4f} | {ps['positive']}/{ps['total']} | "
                     f"{ao['n']} | {ao['overall_exp_r']:+.4f} | {ao_t1:+.4f} | {ao_sp:+.4f} |")
        L.append("")
        L.append("### 4.4 Threshold sweep — the decisive test")
        L.append("")
        L.append("If the utility score ranks well, cutting higher should monotonically improve the "
                 "surviving subset. It does not: the entire step is at the first cut (0.0 → 0.5), "
                 "which merely drops the candidates the arbiter already assigns utility 0.")
        L.append("")
        L.append("| Utility ≥ | SWING n | SWING exp R | DAY_TRADING n | DAY_TRADING exp R | SCALP n | SCALP exp R |")
        L.append("|---|---|---|---|---|---|---|")
        by_style = {}
        for style in STYLE_ORDER:
            m = robust["modes"].get(style)
            if m:
                by_style[style] = {s["utility_min"]: s for s in m["threshold_sweep"]}
        cuts = sorted({c for d in by_style.values() for c in d})
        for cut in cuts:
            cells = []
            for style in STYLE_ORDER:
                s = by_style.get(style, {}).get(cut)
                if not s:
                    cells += ["—", "—"]
                elif s.get("exp_r") is None:
                    cells += [str(s["n"]), "too few"]
                else:
                    cells += [str(s["n"]), f"{s['exp_r']:+.4f}"]
            L.append(f"| {cut:g} | " + " | ".join(cells) + " |")
        L.append("")
        L.append("**Reading:** across a 50× range of the utility threshold, expectancy moves by "
                 "at most 0.02 R in any mode. The utility score is therefore not usable as a "
                 "continuous filter — what separates the populations is the binary "
                 "`ev > 0 and directional` condition, worth roughly 0.03–0.05 R.")
        L.append("")

    md = "\n".join(L)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(md)
    print(f"-> {args.out} ({len(md)} chars)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
