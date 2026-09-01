"""
ai_trade_journal.py -- AI Trading Journal (roadmap #18). Read-only.

  python ai_trade_journal.py            generate journal entries for every
                                        closed trade not yet journaled
                                        (one batched LLM call per day)
  python ai_trade_journal.py --report   print the journal + roll-ups
  python ai_trade_journal.py --since 2026-08-28    only that date forward

The journal is an LLM retrospective on trades that have ALREADY closed:
entry quality, exit quality, why it won/lost, the one lesson, plus a daily
pattern summary. It never touches an order, position, stop, or strategy --
see ai/features/trade_journal.py's module docstring. Gated by
config/ai.json `journal_enabled`.
"""

import argparse
import os
import sys
from collections import Counter

# The roll-up printout uses box-drawing chars; a Windows console defaults to
# cp1252 and raises UnicodeEncodeError on them. Force UTF-8 on our streams.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

import ai.features.trade_journal as tj

G, R, Y, C, DIM, X, B = (
    "\033[92m", "\033[91m", "\033[93m", "\033[96m", "\033[2m", "\033[0m", "\033[1m"
)


def _report(since: str | None) -> int:
    rows = tj._load_jsonl(tj.JOURNAL_LOG)
    trades = [r for r in rows if r.get("event") == "trade"]
    # one summary per day -- the latest real one (re-runs can append more)
    day_map: dict = {}
    for r in sorted((x for x in rows if x.get("event") == "day_summary"),
                    key=lambda x: x.get("ts") or ""):
        if tj._real_summary(r.get("summary")):
            day_map[r.get("day")] = r
    days = list(day_map.values())
    if since:
        trades = [r for r in trades if (r.get("day") or "") >= since]
        days = [r for r in days if (r.get("day") or "") >= since]
    if not trades:
        print(f"{Y}No journal entries yet. Run `python ai_trade_journal.py` "
              f"(needs config/ai.json journal_enabled + closed trades).{X}")
        return 0

    print(f"{B}AI Trading Journal{X}  {DIM}({len(trades)} trades, {len(days)} day summaries){X}\n")

    for d in sorted(days, key=lambda r: r.get("day") or ""):
        net = d.get("net_eur")
        col = G if (net or 0) >= 0 else R
        print(f"{B}── {d.get('day')} ──{X}  {d.get('n_trades')} trades  "
              f"{col}{net:+.0f} EUR{X}")
        print(f"  {d.get('summary')}\n")

    recent = sorted(trades, key=lambda r: r.get("ts") or "")[-25:]
    print(f"{B}Last {len(recent)} trades{X}")
    for t in recent:
        net = t.get("net_pnl_eur")
        col = G if (net or 0) >= 0 else R
        r_mult = t.get("r_multiple")
        rs = f"{r_mult:+.2f}R" if isinstance(r_mult, (int, float)) else "  -  "
        eq, xq = t.get("entry_quality") or "-", t.get("exit_quality") or "-"
        head = (f"  {col}{(net or 0):+8.1f}{X} {rs:>7}  "
                f"{t.get('strategy','?'):<22} {t.get('symbol','?'):<9} "
                f"{t.get('direction','?'):<4}  {DIM}{t.get('account_env','?')}/{t.get('regime_at_entry') or '?'}{X}")
        print(head)
        print(f"      entry:{C}{eq}{X}  exit:{C}{xq}{X}  "
              f"{DIM}{', '.join(t.get('tags') or [])}{X}")
        if t.get("why_result"):
            print(f"      why: {t['why_result']}")
        if t.get("lesson") and t["lesson"].lower() != "none":
            print(f"      {Y}lesson:{X} {t['lesson']}")

    # roll-ups
    print(f"\n{B}{'─'*60}{X}")
    eq = Counter(t.get("entry_quality") for t in trades if t.get("entry_quality"))
    xq = Counter(t.get("exit_quality") for t in trades if t.get("exit_quality"))
    tags = Counter(tag for t in trades for tag in (t.get("tags") or []))
    print(f"  entry quality: {dict(eq)}")
    print(f"  exit quality:  {dict(xq)}")
    print(f"  top tags:      {dict(tags.most_common(10))}")

    narrated = sum(1 for t in trades if t.get("narrated"))
    if narrated < len(trades):
        print(f"  {Y}{len(trades) - narrated} trade(s) logged without a narrative "
              f"(LLM call failed that day -- re-run to backfill won't retry; check _agent.error){X}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="AI Trading Journal (read-only)")
    ap.add_argument("--report", action="store_true", help="print the journal instead of generating")
    ap.add_argument("--since", default=None, help="YYYY-MM-DD lower bound")
    args = ap.parse_args()

    if args.report:
        return _report(args.since)

    if not tj.ai_config.journal_enabled():
        print(f"{Y}Journal is disabled. Set config/ai.json \"journal_enabled\": true.{X}")
        return 0
    res = tj.run(since=args.since)
    print(f"journal run: {res['status']} -- {res.get('journaled', 0)} trade(s) "
          f"across {res.get('days', 0)} day(s)")
    for e in res.get("errors", []):
        print(f"  {R}! {e}{X}")
    if res.get("journaled"):
        print(f"  {DIM}python ai_trade_journal.py --report{X}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
