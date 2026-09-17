"""
reports/live_readiness_check.py
--------------------------------
Weekly check: are CNN-LSTM, ML, or Pullback ready for live consideration?
Runs every Monday at 09:00 PKT via Task Scheduler and emails a report.

Thresholds:
  CNN-LSTM  >= 50 organic trades  AND  PF >= 1.3
  ML        >= 30 organic trades  AND  PF >= 1.2  AND  WR >= 60%
  Pullback  >= 50 clean trades (post-2026-09-18 JPY+NZD block)  AND  PF >= 1.2
"""
import os
import sqlite3
import sys
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

RESET_DATE = "2026-09-03"
PULLBACK_CLEAN_DATE = "2026-09-18"  # JPY block committed

THRESHOLDS = {
    "cnn_lstm": {
        "min_trades": 50,
        "min_pf":     1.3,
        "min_wr":     0.0,
        "note":       "Raise CONFIDENCE_THRESHOLD 0.45->0.65 before live flip",
    },
    "ml": {
        "min_trades": 30,
        "min_pf":     1.2,
        "min_wr":     60.0,
        "note":       "Verify RR improves with more trades before live",
    },
    "pullback": {
        "min_trades": 50,
        "min_pf":     1.2,
        "min_wr":     0.0,
        "note":       "Count only post-2026-09-18 trades (JPY+NZD block clean start)",
        "date_from":  PULLBACK_CLEAN_DATE,
    },
}


def _stats(con, strategy, date_from=None):
    since = date_from or RESET_DATE
    rows = con.execute("""
        SELECT realized_pnl FROM trades
        WHERE module='forex' AND strategy=? AND status='closed'
          AND realized_pnl IS NOT NULL
          AND exit_reason NOT LIKE '%roster_flatten%'
          AND DATE(timestamp_close) >= ?
    """, (strategy, since)).fetchall()
    if not rows:
        return {"n": 0, "wr": 0, "pf": 0, "net": 0}
    pnls = [r[0] for r in rows]
    wins = sum(1 for p in pnls if p > 0)
    gp = sum(p for p in pnls if p > 0)
    gl = -sum(p for p in pnls if p < 0)
    return {
        "n":   len(pnls),
        "wr":  round(wins / len(pnls) * 100, 1),
        "pf":  round(gp / gl, 2) if gl else 0,
        "net": round(sum(pnls), 0),
    }


def main():
    db = os.path.join(BASE, "data", "pnl_ledger.db")
    con = sqlite3.connect(db)

    now = datetime.now()
    ready = []
    watch = []
    lines = [f"Live Readiness Check — {now:%Y-%m-%d %H:%M}\n"]
    lines.append(f"{'Strategy':<12} {'N':>4} {'WR':>6} {'PF':>6} {'Net':>8}  Status")
    lines.append("-" * 65)

    for strat, thresh in THRESHOLDS.items():
        s = _stats(con, strat, thresh.get("date_from"))
        n_ok  = s["n"]  >= thresh["min_trades"]
        pf_ok = s["pf"] >= thresh["min_pf"]
        wr_ok = s["wr"] >= thresh["min_wr"]
        all_ok = n_ok and pf_ok and wr_ok

        if all_ok:
            status = "*** READY FOR REVIEW ***"
            ready.append(strat)
        else:
            gaps = []
            if not n_ok:  gaps.append(f"need {thresh['min_trades']-s['n']} more trades")
            if not pf_ok: gaps.append(f"PF {s['pf']} < {thresh['min_pf']}")
            if not wr_ok: gaps.append(f"WR {s['wr']}% < {thresh['min_wr']}%")
            status = "waiting: " + ", ".join(gaps)
            watch.append((strat, s, thresh, gaps))

        lines.append(f"{strat:<12} {s['n']:>4} {s['wr']:>5}% {s['pf']:>6} {s['net']:>+8.0f}  {status}")
        lines.append(f"             Note: {thresh['note']}")

    con.close()

    lines.append("")
    if ready:
        lines.append(f"ACTION NEEDED: {', '.join(ready)} crossed thresholds — review for live.")
    else:
        lines.append("No strategy has crossed all thresholds yet. Continue monitoring.")

    report = "\n".join(lines)
    print(report)

    try:
        from forex.notifier import _send
        subject = (f"[LIVE READINESS] {', '.join(ready)} READY — {now:%Y-%m-%d}"
                   if ready else
                   f"[Live Readiness] No strategy ready yet — {now:%Y-%m-%d}")
        html = f"<pre style='font-family:monospace'>{report}</pre>"
        _send(subject, html)
        print("[live_readiness] email sent")
    except Exception as e:
        print(f"[live_readiness] email failed: {e}")


if __name__ == "__main__":
    main()
