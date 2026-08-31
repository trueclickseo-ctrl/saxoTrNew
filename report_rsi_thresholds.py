"""
report_rsi_thresholds.py -- read-only. What RSI(2) entry threshold has the
best expectancy AFTER costs?

Source: data/rsi_signal_registry.jsonl (every RSI(2) trigger in the study
band, SIM + LIVE, with a forward-resolved hypothetical outcome -- see
forex/rsi_signal_registry.py). Buckets the RESOLVED rows cumulatively:

    RSI(2) <= 5 / <= 7 / <= 10 (current) / <= 12 / <= 15

and per bucket reports, after a modelled round-trip cost:

    trades · win rate · avg win R · avg loss R · profit factor ·
    max drawdown (R) · EXPECTANCY (R/trade)

The "best RSI" is the highest-EXPECTANCY bucket, not the highest win rate.

    python report_rsi_thresholds.py            # all accounts
    python report_rsi_thresholds.py sim        # one account
    python report_rsi_thresholds.py --cost 0.04
"""

import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
REG = os.path.join(BASE, "data", "rsi_signal_registry.jsonl")

G, R, Y, C, DIM, X, B = (
    "\033[92m", "\033[91m", "\033[93m", "\033[96m", "\033[2m", "\033[0m", "\033[1m"
)

BUCKETS = [5.0, 7.0, 10.0, 12.0, 15.0]
DEFAULT_COST_R = 0.03   # round-trip cost as a fraction of 1R (ladder-backtest default)


def _load():
    if not os.path.exists(REG):
        return []
    out = []
    for ln in open(REG, encoding="utf-8"):
        ln = ln.strip()
        if ln:
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
    return out


def _max_dd(r_series):
    peak = cum = mdd = 0.0
    for x in r_series:
        cum += x
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    return -mdd


def _bucket_stats(rows, cost_r):
    """rows already filtered to one threshold + one account, resolved only."""
    rs = []
    for row in rows:
        res = row.get("resolved") or {}
        rm = res.get("r_multiple")
        if rm is None:
            continue
        rs.append(float(rm) - cost_r)   # net of modelled cost
    n = len(rs)
    if not n:
        return None
    wins = [x for x in rs if x > 0]
    losses = [x for x in rs if x <= 0]
    gross_w = sum(wins)
    gross_l = -sum(losses)
    return {
        "n": n,
        "wr": 100 * len(wins) / n,
        "avg_win": (gross_w / len(wins)) if wins else 0.0,
        "avg_loss": (-gross_l / len(losses)) if losses else 0.0,
        "pf": (gross_w / gross_l) if gross_l > 0 else float("inf"),
        "max_dd": _max_dd(rs),
        "expectancy": sum(rs) / n,
    }


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    cost_r = DEFAULT_COST_R
    if "--cost" in sys.argv:
        try:
            cost_r = float(sys.argv[sys.argv.index("--cost") + 1])
        except Exception:
            pass
    want_accounts = args or None

    rows = _load()
    if not rows:
        print(f"{Y}No rows yet (data/rsi_signal_registry.jsonl). It fills as RSI scans run "
              f"with forex/rsi_signal_registry wired in.{X}")
        return 0

    resolved = [r for r in rows if r.get("resolved")]
    accounts = sorted({r.get("account_env") for r in resolved} & set(want_accounts)) if want_accounts \
        else sorted({r.get("account_env") for r in resolved})

    print(f"{B}RSI(2) threshold study{X}  {DIM}({len(rows)} logged, {len(resolved)} resolved · "
          f"cost model {cost_r:.2f} R/trade){X}")

    for acct in accounts:
        ar = [r for r in resolved if r.get("account_env") == acct]
        print(f"\n{B}{'='*78}{X}\n{B}  {acct.upper()}{X}   ({len(ar)} resolved triggers)\n{B}{'='*78}{X}")
        print(f"  {DIM}{'threshold':<12}{'n':>5}{'WR%':>7}{'avgWin':>8}{'avgLoss':>9}"
              f"{'PF':>7}{'maxDD':>8}{'EXPECT':>9}{X}")
        best = None
        for thr in BUCKETS:
            sub = [r for r in ar if float(r.get("rsi2", 99)) <= thr
                   or (r.get("direction") == "Sell" and float(r.get("rsi2", 0)) >= 100 - thr)]
            st = _bucket_stats(sub, cost_r)
            tag = "  <- current" if thr == 10.0 else ""
            if st is None:
                print(f"  RSI2<= {int(thr):<5}{DIM}  (no resolved trades yet){X}{tag}")
                continue
            if best is None or st["expectancy"] > best[1]:
                best = (thr, st["expectancy"])
            pf = "inf" if st["pf"] == float("inf") else f"{st['pf']:.2f}"
            ec = G if st["expectancy"] > 0 else R
            print(f"  RSI2<= {int(thr):<5}{st['n']:>5}{st['wr']:>7.1f}{st['avg_win']:>+8.2f}"
                  f"{st['avg_loss']:>+9.2f}{pf:>7}{st['max_dd']:>8.1f}"
                  f"{ec}{st['expectancy']:>+9.3f}{X}{Y}{tag}{X}")
        if best:
            print(f"\n  {G}best expectancy: RSI(2) <= {int(best[0])}  ({best[1]:+.3f} R/trade "
                  f"after {cost_r:.2f} R cost){X}")

    tot = len(resolved)
    print(f"\n{B}{'-'*78}{X}")
    if tot < 40:
        print(f"{Y}  {tot} resolved triggers -- too few. Re-run weekly; ~40-80+ per account "
              f"before the threshold comparison is worth acting on. (paper-fill keeps SIM "
              f"flowing through Saxo outages.){X}")
    else:
        print(f"{G}  Compare the EXPECT column across thresholds. Highest wins -- NOT the highest WR.{X}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
