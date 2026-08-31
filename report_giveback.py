"""
report_giveback.py -- P2: how much favorable excursion does ATOS give back?

READ-ONLY. Reads data/trade_observation_cards.jsonl (paired entry+exit
observation cards), normalises everything by the trade's initial risk, and
answers -- per strategy and per account -- whether ATOS systematically
fails to monetise trades that went its way.

    MFE_R      = max favorable excursion / initial risk
    Final_R    = net P&L / initial risk           (the card's r_multiple)
    Giveback_R = MFE_R - Final_R                   (only for MFE_R > 0)
    Capture    = Final_R / MFE_R                   (1.0 = kept all of it)

Only CLEAN observations are used: the pre-2026-09-01 MAE/MFE window bug
(see fix_observation_card_mae_mfe.py) nulled 68 corrupted trades -- those
are skipped. Intraday-strategy trades (gap / london_breakout*) carry
`mae_mfe_coarse` -- their MFE_R is a loose upper bound from one daily bar,
so they are reported in a separate bucket, not mixed into the precise stats.

This report does NOT change anything. Its output is evidence for a human/
quant hypothesis -- per ATOS governance, a finding here becomes a
deterministic, backtested strategy change, never an automatic one.

    python report_giveback.py                    # all clean data
    python report_giveback.py --account live      # one account
    python report_giveback.py --strategy rsi      # one strategy
    python report_giveback.py --min-sample 15     # gate threshold (default 10)
"""

import argparse
import json
import os
import statistics
import sys
from collections import defaultdict

BASE = os.path.dirname(os.path.abspath(__file__))
CARDS = os.path.join(BASE, "data", "trade_observation_cards.jsonl")

G, R, Y, C, DIM, X, B = (
    "\033[92m", "\033[91m", "\033[93m", "\033[96m", "\033[2m", "\033[0m", "\033[1m"
)

# "went our way then went bad" -- the user's specific questions
GIVEBACK_RULES = [
    ("MFE>=1R -> final <0.25R", 1.0, 0.25),
    ("MFE>=2R -> final <0R",    2.0, 0.0),
    ("MFE>=3R -> final <1R",    3.0, 1.0),
]
# a fuller ladder for context
MFE_LADDER = [0.5, 1.0, 1.5, 2.0, 3.0]


def _load_trades():
    """Paired closed trades with clean MAE/MFE. One dict per trade."""
    if not os.path.exists(CARDS):
        return []
    entry, exits = {}, []
    for ln in open(CARDS, encoding="utf-8"):
        ln = ln.strip()
        if not ln:
            continue
        try:
            c = json.loads(ln)
        except Exception:
            continue
        if c.get("event") == "entry" and c.get("card_id"):
            entry[c["card_id"]] = c
        elif c.get("event") == "exit" and c.get("card_id"):
            exits.append(c)

    out = []
    for x in exits:
        e = entry.get(x["card_id"])
        if not e:
            continue
        if x.get("mae_mfe_invalidated"):
            continue                       # pre-fix corrupted -- skip
        risk = e.get("risk_eur")
        mfe, mae, net = x.get("mfe_eur"), x.get("mae_eur"), x.get("net_pnl_eur")
        if not risk or risk <= 0 or mfe is None or net is None:
            continue
        r_mult = x.get("r_multiple")
        final_r = r_mult if isinstance(r_mult, (int, float)) else net / risk
        out.append({
            "account": e.get("account_env"),
            "strategy": e.get("strategy"),
            "symbol": e.get("symbol"),
            "coarse": bool(x.get("mae_mfe_coarse")),
            "risk_eur": risk,
            "net_pnl_eur": net,
            "mfe_r": mfe / risk,
            "mae_r": (mae / risk) if mae is not None else None,
            "final_r": final_r,
            "giveback_r": (mfe / risk - final_r) if mfe / risk > 0 else 0.0,
            "exit_reason": (x.get("exit_reason") or "?").split(" ")[0],
        })
    return out


def _block(trades):
    """Metric dict for a set of trades (assumed precise, i.e. non-coarse)."""
    n = len(trades)
    if not n:
        return {"n": 0}
    finals = [t["final_r"] for t in trades]
    inprofit = [t for t in trades if t["mfe_r"] > 0]
    gb = [t["giveback_r"] for t in inprofit]
    cap = [t["final_r"] / t["mfe_r"] for t in inprofit if t["mfe_r"] > 0]
    d = {
        "n": n,
        "win_rate": round(100 * sum(1 for f in finals if f > 0) / n, 1),
        "avg_final_r": round(statistics.mean(finals), 2),
        "avg_mfe_r": round(statistics.mean(t["mfe_r"] for t in trades), 2),
        "avg_giveback_r": round(statistics.mean(gb), 2) if gb else None,
        "median_giveback_r": round(statistics.median(gb), 2) if gb else None,
        "avg_capture": round(statistics.mean(cap), 2) if cap else None,
    }
    # the user's "went our way then went bad" rules
    d["rules"] = {}
    for label, mfe_thr, final_thr in GIVEBACK_RULES:
        pool = [t for t in trades if t["mfe_r"] >= mfe_thr]
        bad = [t for t in pool if t["final_r"] < final_thr]
        d["rules"][label] = {
            "n_reached": len(pool),
            "n_bad": len(bad),
            "pct_bad": round(100 * len(bad) / len(pool), 1) if pool else None,
        }
    # fuller ladder
    d["ladder"] = {}
    for thr in MFE_LADDER:
        pool = [t for t in trades if t["mfe_r"] >= thr]
        if pool:
            kept = statistics.mean(t["final_r"] / t["mfe_r"] for t in pool)
            d["ladder"][f">={thr}R"] = {"n": len(pool), "avg_capture": round(kept, 2),
                                        "avg_final_r": round(statistics.mean(t["final_r"] for t in pool), 2)}
    # lifecycle distribution -- averages hide "big winner gave back a lot"
    buckets = {"loss (<0R)": [], "small win (0-1R)": [], "large win (>=1R)": []}
    for t in trades:
        k = "loss (<0R)" if t["final_r"] < 0 else ("small win (0-1R)" if t["final_r"] < 1 else "large win (>=1R)")
        buckets[k].append(t)
    d["lifecycle"] = {k: {"n": len(v),
                          "avg_mfe_r": round(statistics.mean(t["mfe_r"] for t in v), 2) if v else None,
                          "avg_giveback_r": round(statistics.mean(t["giveback_r"] for t in v), 2) if v else None}
                      for k, v in buckets.items()}
    return d


def summarize(account=None, strategy=None):
    trades = _load_trades()
    if account:
        trades = [t for t in trades if t["account"] == account]
    if strategy:
        trades = [t for t in trades if t["strategy"] == strategy]
    precise = [t for t in trades if not t["coarse"]]
    coarse = [t for t in trades if t["coarse"]]
    return {
        "n_total": len(trades), "n_precise": len(precise), "n_coarse": len(coarse),
        "overall": _block(precise),
        "by_strategy": {s: _block([t for t in precise if t["strategy"] == s])
                        for s in sorted({t["strategy"] for t in precise})},
        "by_account": {a: _block([t for t in precise if t["account"] == a])
                       for a in sorted({t["account"] for t in precise})},
        "coarse_note": _block(coarse) if coarse else None,
    }


def _print_block(d, indent="  "):
    if not d.get("n"):
        print(f"{indent}{DIM}(no trades){X}")
        return
    print(f"{indent}n={d['n']}  WR {d['win_rate']}%  avg final {d['avg_final_r']:+.2f}R  "
          f"avg MFE {d['avg_mfe_r']:.2f}R  "
          f"avg give-back {d['avg_giveback_r'] if d['avg_giveback_r'] is not None else '-'}R  "
          f"capture {d['avg_capture'] if d['avg_capture'] is not None else '-'}")
    for label, rd in d["rules"].items():
        if rd["pct_bad"] is None:
            continue
        col = R if (rd["pct_bad"] >= 33 and rd["n_reached"] >= 5) else Y
        print(f"{indent}  {label:<24} {rd['n_bad']}/{rd['n_reached']}  "
              f"{col}{rd['pct_bad']}%{X}")
    life = d["lifecycle"]
    print(f"{indent}  lifecycle: " + "  ".join(
        f"{k} n={v['n']}"
        + (f" (MFE {v['avg_mfe_r']}R, gave back {v['avg_giveback_r']}R)" if v["n"] else "")
        for k, v in life.items()))


def main():
    ap = argparse.ArgumentParser(description="P2 give-back analysis (read-only)")
    ap.add_argument("--account", default=None)
    ap.add_argument("--strategy", default=None)
    ap.add_argument("--min-sample", type=int, default=10)
    args = ap.parse_args()

    s = summarize(args.account, args.strategy)
    print(f"{B}ATOS give-back analysis{X}  {DIM}(data/trade_observation_cards.jsonl, "
          f"clean post-2026-09-01 fix only){X}")
    print(f"  {s['n_precise']} precise trade(s), {s['n_coarse']} coarse/intraday "
          f"(reported separately)\n")

    if s["n_precise"] < args.min_sample:
        print(f"{Y}  Only {s['n_precise']} clean trades so far -- need >= {args.min_sample} "
              f"before this is worth acting on. The MAE/MFE fix landed 2026-09-01; "
              f"clean data accrues at ~15-30 SIM closes/day. Re-run in a few days.{X}\n")
        # still show what we have
    print(f"{B}OVERALL{X}")
    _print_block(s["overall"])

    print(f"\n{B}BY STRATEGY{X}  {DIM}(don't make a global rule -- fix only the ones that show it){X}")
    for strat, d in s["by_strategy"].items():
        print(f"{C}{strat}{X}")
        _print_block(d, indent="    ")

    print(f"\n{B}BY ACCOUNT{X}  {DIM}(SIM vs LIVE -- LIVE has real spread/slippage/latency){X}")
    for acct, d in s["by_account"].items():
        print(f"{C}{acct}{X}")
        _print_block(d, indent="    ")

    if s["coarse_note"]:
        print(f"\n{B}COARSE / INTRADAY (loose MFE upper bound -- directional only){X}")
        _print_block(s["coarse_note"])

    print(f"\n{DIM}  Hypothesis test: if a strategy shows (say) >35% of >=2R trades finishing "
          f"<0R on a real sample, that's evidence for a strategy-specific exit change -- "
          f"to design, backtest and validate deterministically, not to auto-apply.{X}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
