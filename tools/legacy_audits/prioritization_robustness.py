"""Stress-test the prioritization verdicts before publishing them.

`tools/verify_prioritization.py` produces a single verdict per mode from three
statistics. A verdict that only survives one arbitrary choice of parameters is
not a finding. This re-runs the same question under different choices and reports
whether the answer is stable:

  * **top-k** — the arbiter's contract is "pick one", but if its information sits
    in the top few, top-1 alone understates it.
  * **bucket count** — a quintile spread can be an artefact of the cut points.
  * **actionable-only** — the arbiter only ever trades `is_actionable`
    candidates; the ranking may behave differently inside that population.
  * **per-symbol Spearman** — does utility correlate with R inside each symbol,
    or only in the pooled average?
  * **threshold sweep** — is there any utility cut where the surviving subset is
    profitable? This is the practically useful question.

Reads the frames written by `tools/verify_prioritization.py`
(`reports/_prio_frame_<STYLE>.parquet`), so it costs no replay time.

Run:  python tools/prioritization_robustness.py
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np
import pandas as pd

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

FRAME = os.path.join(REPO, "reports", "_prio_frame_{}.parquet")
OUT = os.path.join(REPO, "reports", "prioritization_robustness.json")
STYLES = ["SWING", "DAY_TRADING", "SCALP"]


def topk_vs_rest(frame: pd.DataFrame, k: int) -> dict:
    picks, rests = [], []
    for _, g in frame.groupby("time", sort=False):
        if len(g) < 2:
            continue
        u = pd.to_numeric(g["utility_score"], errors="coerce").fillna(-1e18)
        order = u.sort_values(ascending=False).index
        kk = min(k, max(1, len(g) - 1))
        sel = g.loc[order[:kk]]
        rest = g.loc[order[kk:]]
        if len(rest) == 0:
            continue
        picks.append(float(sel["pnl_r"].mean()))
        rests.append(float(rest["pnl_r"].mean()))
    if not picks:
        return {"k": k, "n_sets": 0, "pick_r": 0.0, "rest_r": 0.0, "delta_r": 0.0}
    p, r = np.array(picks), np.array(rests)
    return {"k": k, "n_sets": int(len(p)),
            "pick_r": round(float(p.mean()), 4),
            "rest_r": round(float(r.mean()), 4),
            "delta_r": round(float((p - r).mean()), 4),
            "pct_sets_pick_better": round(float((p > r).mean()), 4)}


def bucket_gradient(frame: pd.DataFrame, q: int) -> dict:
    def rank(s):
        s = pd.to_numeric(s, errors="coerce")
        if s.notna().sum() < q:
            return pd.Series(np.nan, index=s.index)
        try:
            return pd.qcut(s.rank(method="first"), q, labels=False)
        except Exception:
            return pd.Series(np.nan, index=s.index)

    w = frame.copy()
    w["_b"] = w.groupby("symbol", group_keys=False)["utility_score"].apply(rank)
    w = w.dropna(subset=["_b"])
    if len(w) == 0:
        return {"q": q, "spread_r": 0.0, "agree": 0, "total": 0}
    top = w[w["_b"] == w["_b"].max()].groupby("symbol")["pnl_r"].mean()
    bot = w[w["_b"] == w["_b"].min()].groupby("symbol")["pnl_r"].mean()
    common = top.index.intersection(bot.index)
    agree = int((top[common] > bot[common]).sum())
    hi = w[w["_b"] == w["_b"].max()]["pnl_r"]
    lo = w[w["_b"] == w["_b"].min()]["pnl_r"]
    return {"q": q,
            "spread_r": round(float(hi.mean() - lo.mean()), 4),
            "agree": agree, "total": int(len(common))}


def per_symbol_spearman(frame: pd.DataFrame) -> dict:
    out = {}
    for sym, g in frame.groupby("symbol"):
        if len(g) < 30:
            continue
        u = pd.to_numeric(g["utility_score"], errors="coerce")
        r = pd.to_numeric(g["pnl_r"], errors="coerce")
        m = u.notna() & r.notna()
        if m.sum() < 30:
            continue
        rho = float(pd.Series(u[m]).rank().corr(pd.Series(r[m]).rank()))
        out[sym] = round(rho, 4)
    pos = sum(1 for v in out.values() if v > 0)
    return {"per_symbol_rho": out, "positive": pos, "total": len(out),
            "mean_rho": round(float(np.mean(list(out.values()))), 4) if out else 0.0}


def threshold_sweep(frame: pd.DataFrame) -> list:
    """Is there a utility cut whose surviving subset is profitable?"""
    u = pd.to_numeric(frame["utility_score"], errors="coerce")
    rows = []
    for cut in (0.0, 0.5, 1.0, 1.3, 1.8, 2.5, 3.5, 5.0, 7.5, 10.0, 15.0, 25.0):
        sel = frame[u >= cut]
        if len(sel) < 25:
            rows.append({"utility_min": cut, "n": int(len(sel)), "exp_r": None})
            continue
        rows.append({"utility_min": cut, "n": int(len(sel)),
                     "exp_r": round(float(sel["pnl_r"].mean()), 4),
                     "wr": round(float((sel["pnl_r"] > 0).mean()), 4),
                     "pct_of_all": round(len(sel) / len(frame) * 100, 2)})
    return rows


def analyse(frame: pd.DataFrame, style: str) -> dict:
    res = {"style": style, "n": int(len(frame)),
           "overall_exp_r": round(float(frame["pnl_r"].mean()), 4)}
    res["topk"] = [topk_vs_rest(frame, k) for k in (1, 2, 3, 5, 10)]
    res["bucket_counts"] = [bucket_gradient(frame, q) for q in (3, 4, 5, 10)]
    act = frame[frame["is_actionable"].astype(bool)]
    res["actionable_only"] = {
        "n": int(len(act)),
        "overall_exp_r": round(float(act["pnl_r"].mean()), 4) if len(act) else 0.0,
        "topk": [topk_vs_rest(act, k) for k in (1, 3)] if len(act) else [],
        "buckets": bucket_gradient(act, 5) if len(act) else {},
        "per_symbol": per_symbol_spearman(act) if len(act) else {},
    }
    res["per_symbol"] = per_symbol_spearman(frame)
    res["threshold_sweep"] = threshold_sweep(frame)
    return res


def main() -> int:
    report = {"note": "Robustness re-analysis of the prioritization verdicts. "
                      "Reads the replay frames; no new replay.",
              "modes": {}}
    for style in STYLES:
        p = FRAME.format(style)
        if not os.path.exists(p):
            print(f"{style}: no frame at {p}")
            continue
        frame = pd.read_parquet(p)
        r = analyse(frame, style)
        report["modes"][style] = r
        print(f"\n=== {style}  (n={r['n']}, overall {r['overall_exp_r']:+.4f} R) ===")
        print("  top-k vs rest:")
        for t in r["topk"]:
            print(f"    k={t['k']:2d} sets={t['n_sets']:6d} pick={t['pick_r']:+.4f} "
                  f"rest={t['rest_r']:+.4f} delta={t['delta_r']:+.4f} "
                  f"better={t['pct_sets_pick_better']*100:.1f}%")
        print("  bucket-count sensitivity (spread / symbol agreement):")
        for b in r["bucket_counts"]:
            print(f"    q={b['q']:2d} spread={b['spread_r']:+.4f} agree={b['agree']}/{b['total']}")
        ps = r["per_symbol"]
        print(f"  per-symbol Spearman: mean {ps['mean_rho']:+.4f}, "
              f"{ps['positive']}/{ps['total']} positive")
        ao = r["actionable_only"]
        print(f"  actionable-only: n={ao['n']} expR={ao['overall_exp_r']:+.4f} "
              f"top1delta={ao['topk'][0]['delta_r'] if ao['topk'] else 'n/a'} "
              f"spread={ao['buckets'].get('spread_r')}")
        print("  threshold sweep (utility >= cut):")
        for s in r["threshold_sweep"]:
            if s["exp_r"] is None:
                print(f"    >= {s['utility_min']:5.1f}  n={s['n']:6d}  (too few)")
            else:
                print(f"    >= {s['utility_min']:5.1f}  n={s['n']:6d} "
                      f"({s['pct_of_all']:5.2f}%)  expR={s['exp_r']:+.4f}  wr={s['wr']:.3f}")

    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)
    print(f"\n-> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
