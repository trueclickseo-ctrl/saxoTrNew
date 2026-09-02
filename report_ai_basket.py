"""
report_ai_basket.py -- read-only. Compares the AI shadow basket-ranker's
offense pick against US Blend's deterministic pick on FORWARD realised
returns.

For each row in data/ai_basket_shadow.jsonl:
  * the deterministic offense basket (det_offense[:det_count])
  * the AI offense basket (ai_offense[:ai_count])
  * equal-weight forward return of each over the holding window (to the
    next rebalance row, or `--horizon` days, whichever first), from
    yfinance daily closes (backtest-sanctioned; live trading never uses it)

Then: n rebalances scored, mean/median return delta (AI - det), AI-beat
hit rate, worst miss, and a regime breakdown.

"Not enough data" under ~6 scored rebalances.

    python report_ai_basket.py
    python report_ai_basket.py --horizon 14
"""

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta

_BASE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(_BASE, "data", "ai_basket_shadow.jsonl")


def _load():
    rows = []
    try:
        with open(LOG, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    r = json.loads(ln)
                    if isinstance(r, dict) and r.get("as_of_date"):
                        rows.append(r)
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    rows.sort(key=lambda r: r["as_of_date"])
    return rows


def _fwd_return(tickers, start, end):
    """Equal-weight simple return of `tickers` from `start` to `end` (dates)."""
    if not tickers:
        return None
    try:
        import yfinance as yf
        import pandas as pd
    except Exception:
        return None
    try:
        df = yf.download(list(tickers), start=start.isoformat(),
                         end=(end + timedelta(days=4)).isoformat(),
                         progress=False, auto_adjust=True)["Close"]
        if isinstance(df, pd.Series):
            df = df.to_frame()
        rets = []
        for tk in tickers:
            s = df[tk].dropna() if tk in df.columns else None
            if s is None or len(s) < 2:
                continue
            rets.append(float(s.iloc[-1] / s.iloc[0] - 1))
        return sum(rets) / len(rets) if rets else None
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon", type=int, default=14, help="max holding window in days")
    args = ap.parse_args()

    rows = _load()
    if not rows:
        print(f"No shadow rows in {LOG} yet."); return

    scored = []
    for i, r in enumerate(rows):
        try:
            d0 = date.fromisoformat(r["as_of_date"])
        except Exception:
            continue
        d1 = d0 + timedelta(days=args.horizon)
        if i + 1 < len(rows):
            try:
                nxt = date.fromisoformat(rows[i + 1]["as_of_date"])
                d1 = min(d1, nxt)
            except Exception:
                pass
        if d1 > date.today():
            continue  # window not closed yet
        det = (r.get("det_offense") or [])[:r.get("det_count") or 0]
        ai = (r.get("ai_offense") or det)
        det_ret = _fwd_return(det, d0, d1)
        ai_ret = _fwd_return(ai, d0, d1)
        if det_ret is None or ai_ret is None:
            continue
        scored.append({
            "date": r["as_of_date"], "regime": r.get("regime"),
            "det": det, "ai": ai, "det_ret": det_ret, "ai_ret": ai_ret,
            "delta": ai_ret - det_ret, "changed": bool(r.get("changed")),
        })

    print(f"AI basket-ranker vs deterministic -- {len(scored)} scored rebalance(s), "
          f"horizon {args.horizon}d\n")
    if len(scored) < 6:
        print("Not enough data for a verdict (need ~6 closed windows). Rows so far:")
    for s in scored:
        mark = "*" if s["changed"] else " "
        print(f"  {s['date']} {mark} [{s['regime'] or '?':<18}] "
              f"det {s['det_ret']*100:+5.1f}%  ai {s['ai_ret']*100:+5.1f}%  "
              f"delta {s['delta']*100:+5.1f}%   det={s['det']} ai={s['ai']}")

    if len(scored) >= 6:
        import statistics as st
        deltas = [s["delta"] for s in scored]
        changed = [s for s in scored if s["changed"]]
        wins = [s for s in changed if s["delta"] > 0]
        print(f"\n  mean delta (AI-det)   {st.mean(deltas)*100:+.2f}%")
        print(f"  median delta          {st.median(deltas)*100:+.2f}%")
        print(f"  rebalances AI changed  {len(changed)}/{len(scored)}")
        if changed:
            print(f"  AI-beat when it changed {len(wins)}/{len(changed)} "
                  f"({len(wins)/len(changed)*100:.0f}%)")
            print(f"  worst AI miss          {min(s['delta'] for s in changed)*100:+.1f}%")
        print("\n  NOTE: a hypothesis generator, not a rule. A deterministic re-ranking")
        print("  rule (if any) comes from reading this + a proper backtest.")


if __name__ == "__main__":
    main()
