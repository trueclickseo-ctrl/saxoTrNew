"""
ai_shadow_report.py -- AI Sprint 3 evidence report. Read-only.

Joins data/ai_shadow_decisions.jsonl (the Trading Copilot's shadow
verdicts, logged next to what ATOS actually did) against the real closed
trades in data/pnl_ledger.db, and asks:

  * Do AI-APPROVED signals outperform AI-REJECTED ones on WR / expectancy?
  * How would size-MODIFY multipliers have changed realised P&L?
  * On the roughest days in the window, did the agent's judgement hold up?

The Sprint 3 exit gate is EVIDENCE, not a calendar: enough decisions,
spanning at least one adverse stretch that the USER explicitly agrees
counts (this script only flags candidate rough days -- it does not rule).

    python ai_shadow_report.py [account]
"""

import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
DECISIONS = os.path.join(BASE, "data", "ai_shadow_decisions.jsonl")
LEDGER = os.path.join(BASE, "data", "pnl_ledger.db")

G, R, Y, C, DIM, X, B = (
    "\033[92m", "\033[91m", "\033[93m", "\033[96m", "\033[2m", "\033[0m", "\033[1m"
)


def _load_decisions():
    if not os.path.exists(DECISIONS):
        return []
    out = []
    for ln in open(DECISIONS, encoding="utf-8"):
        ln = ln.strip()
        if ln:
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
    return out


def _closed_trades(account):
    """{(strategy, symbol, date): (realized_pnl, timestamp_close)} from the ledger."""
    if not os.path.exists(LEDGER):
        return {}
    module = {"sim": "forex", "live": "forex_live", "live_eur": "forex_live_eur",
              "live_stocks": "stock_live"}.get(account, "forex")
    con = sqlite3.connect(LEDGER)
    rows = con.execute(
        "select strategy, symbol, realized_pnl, timestamp_open, timestamp_close "
        "from trades where module=? and exit_price is not null", (module,)).fetchall()
    con.close()
    by_key = defaultdict(list)
    for strat, sym, pnl, t_open, t_close in rows:
        d = (t_open or "")[:10]
        by_key[(strat, sym, d)].append((pnl or 0.0, t_close))
    return by_key


def _match(dec, closed):
    key = (dec.get("strategy"), dec.get("symbol"), (dec.get("ts") or "")[:10])
    hits = closed.get(key)
    return hits[0][0] if hits else None   # realised P&L of the first matching close


def main():
    account = (sys.argv[1] if len(sys.argv) > 1 else None)
    decs = _load_decisions()
    if not decs:
        print(f"{Y}No shadow decisions yet (data/ai_shadow_decisions.jsonl). "
              f"Set config/ai.json enabled_sim + agent_enabled to true and let SIM run.{X}")
        return 0

    accounts = [account] if account else sorted({d.get("account_env") for d in decs})
    print(f"{B}AI Trading Copilot -- shadow scorecard{X}  {DIM}({len(decs)} decisions logged){X}")

    for acct in accounts:
        ad = [d for d in decs if d.get("account_env") == acct]
        if not ad:
            continue
        closed = _closed_trades(acct)
        print(f"\n{B}{'='*74}{X}\n{B}  {acct.upper()}{X}   ({len(ad)} decisions)\n{B}{'='*74}{X}")

        actions = Counter(d.get("agent_action") for d in ad)
        ok = sum(1 for d in ad if (d.get("agent_meta") or {}).get("ok"))
        print(f"  agent verdicts: {dict(actions)}   ({ok}/{len(ad)} real LLM calls, "
              f"{len(ad) - ok} HOLD/degraded)")

        # approved vs rejected, joined to realised P&L
        buckets = {"APPROVE": [], "REJECT": [], "MODIFY": []}
        for d in ad:
            pnl = _match(d, closed)
            if pnl is None or d.get("agent_action") not in buckets:
                continue
            buckets[d["agent_action"]].append(pnl)
        print(f"\n  {DIM}verdict     n   win%    total P&L   avg P&L{X}")
        for act, pnls in buckets.items():
            if not pnls:
                print(f"  {act:<9}  {DIM}(no matched closed trades yet){X}")
                continue
            wr = 100 * sum(1 for p in pnls if p > 0) / len(pnls)
            print(f"  {act:<9} {len(pnls):>3}  {wr:>5.1f}  {sum(pnls):>+10.1f}  {sum(pnls)/len(pnls):>+8.2f}")

        appr = buckets["APPROVE"]
        rej = buckets["REJECT"]
        if appr and rej:
            ea, er = sum(appr) / len(appr), sum(rej) / len(rej)
            verdict = (f"{G}approved trades outperform rejected by {ea - er:+.2f} avg P&L{X}"
                       if ea > er else
                       f"{R}rejected trades did BETTER than approved ({er - ea:+.2f}) -- agent is mis-ranking{X}")
            print(f"\n  {verdict}")

        # candidate rough days -- for the USER to judge, not this script
        day_pnl = defaultdict(float)
        for (strat, sym, d), lst in closed.items():
            for pnl, _ in lst:
                day_pnl[d] += pnl
        rough = sorted(day_pnl.items(), key=lambda kv: kv[1])[:5]
        if rough:
            print(f"\n  {Y}candidate adverse days in the ledger (you decide if the window qualifies):{X}")
            for d, p in rough:
                print(f"    {d}   {p:>+10.1f}")

    n = len(decs)
    print(f"\n{B}{'-'*74}{X}")
    if n < 40:
        print(f"{Y}  {n} decisions -- not enough. The Sprint 3 gate needs a real sample "
              f"AND an adverse stretch you've explicitly agreed counts. Re-run weekly.{X}")
    else:
        print(f"{G}  Review the APPROVE-vs-REJECT rows and confirm the window includes a "
              f"rough patch before moving to Sprint 4.{X}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
