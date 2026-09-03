"""
ai_research_analyst.py -- AI Research Analyst (roadmap #19). Read-only.

  python ai_research_analyst.py                 run the pipeline:
                                                digest -> propose -> auto-gate
                                                -> append new hypotheses
  python ai_research_analyst.py --sweep         also refresh the decomposition
                                                harness cache first (slow: a
                                                ~13y yfinance replay per strategy)
  python ai_research_analyst.py --report        print the ranked backlog
  python ai_research_analyst.py --digest        print the aggregation digest only
  python ai_research_analyst.py --since 2026-08-15
  python ai_research_analyst.py --set-status H20260903-ab12cd shelved "overfit"

The analyst turns the trade record into a triaged backlog of SPECIFIED,
testable strategy-improvement hypotheses. It never edits a strategy or
touches a live order -- a human reads a hypothesis, writes the deterministic
gate, and forward-tests it as an isolated SIM A/B twin. Gated by
config/ai.json `research_analyst.enabled`. See
ai/features/research_analyst.py and docs/strategy_decomposition_2026-09-02.md.
"""

import argparse
import json
import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)

import ai.features.research_analyst as RA

G, R, Y, C, DIM, X, B = (
    "\033[92m", "\033[91m", "\033[93m", "\033[96m", "\033[2m", "\033[0m", "\033[1m"
)
_STATUS_COL = {"gate_passed": G, "validated": G, "shipped": G,
               "gate_failed": DIM, "shelved": DIM, "falsified": DIM,
               "proposed": Y, "backtesting": C}


def _emit(text: str) -> None:
    if not sys.stdout.isatty():
        import re
        text = re.sub(r"\033\[[0-9;]*m", "", text)
    print(text)


def _report(since):
    rows = RA.backlog_view()
    if since:
        rows = [r for r in rows if (r.get("ts") or "")[:10] >= since]
    if not rows:
        _emit(f"{Y}No hypotheses yet. Run `python ai_research_analyst.py` "
              f"(needs config/ai.json research_analyst.enabled + an ANTHROPIC_API_KEY "
              f"+ closed-trade history).{X}")
        return 0
    npass = sum(1 for r in rows if r.get("status") == "gate_passed")
    _emit(f"{B}AI Research Analyst -- hypothesis backlog{X}  "
          f"{DIM}({len(rows)} hypotheses, {npass} cleared the decomposition gate){X}\n")
    for r in rows:
        col = _STATUS_COL.get(r.get("status"), "")
        eff = r.get("expected_effect_r")
        eff_s = f"{eff:+.3f}R" if isinstance(eff, (int, float)) else "  ?  "
        _emit(f"{col}{B}[{r.get('status','?'):<11}]{X} {r.get('id','?')}  "
              f"{DIM}{r.get('strategy','?')} / {r.get('feature','?')}  exp {eff_s}{X}")
        _emit(f"   {r.get('claim','')}")
        if r.get("rule"):
            _emit(f"   {DIM}rule:{X} {r['rule']}")
        v = r.get("verdict") or {}
        for b in (v.get("buckets") or []):
            if b.get("gate_pass"):
                pf = f"PF {b['pf']:.2f}" if b.get("pf") else ""
                _emit(f"   {G}gate PASS{X} bucket {b['label']}: n={b['n']} "
                      f"avgR {b['avg_r']:+.3f} ({b['first_half_avg_r']:+.3f}/"
                      f"{b['second_half_avg_r']:+.3f}) {pf}")
        if r.get("note") and r.get("status") == "gate_failed":
            _emit(f"   {DIM}gate: {r['note']}{X}")
        if r.get("rationale"):
            _emit(f"   {DIM}{r['rationale']}{X}")
        if r.get("status") in ("gate_passed", "proposed"):
            _emit(f"   {Y}-> your call:{X} write the gate ({r.get('strategy')} keep-if {r.get('rule','?')}), "
                  f"ship it as a SIM A/B twin, then `--set-status {r.get('id')} backtesting`")
        _emit("")
    return 0


def _digest(since):
    import json
    d = RA.build_research_digest(since=since)
    _emit(json.dumps(d, indent=2, default=str))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report", action="store_true", help="print the ranked backlog")
    ap.add_argument("--digest", action="store_true", help="print the aggregation digest only")
    ap.add_argument("--sweep", action="store_true",
                    help="refresh the decomposition cache first (slow)")
    ap.add_argument("--since", metavar="YYYY-MM-DD")
    ap.add_argument("--set-status", nargs="+", metavar=("ID STATUS", "NOTE"),
                    help="human status transition, e.g. --set-status H2026... validated")
    args = ap.parse_args(argv)

    if args.set_status:
        hid, status = args.set_status[0], args.set_status[1] if len(args.set_status) > 1 else ""
        note = " ".join(args.set_status[2:]) if len(args.set_status) > 2 else ""
        if not status:
            ap.error("--set-status needs ID and STATUS")
        ok = RA.set_status(hid, status, note)
        print(f"{'updated' if ok else 'no such hypothesis id'}: {hid} -> {status}")
        return 0 if ok else 1

    if args.report:
        return _report(args.since)
    if args.digest:
        return _digest(args.since)

    res = RA.run(since=args.since, sweep_first=args.sweep)
    print(json.dumps(res, indent=2, default=str))
    if res.get("status") == "disabled":
        print("research_analyst is OFF -- set config/ai.json research_analyst.enabled = true")
        return 0
    if res.get("status") == "digest_only":
        print("digest written; propose step unavailable "
              f"({res.get('error')}). `python ai_research_analyst.py --digest`")
        return 0
    print(f"proposed {res.get('proposed', 0)} new hypothes(es); "
          f"{res.get('gate_passed', 0)} cleared the decomposition gate. "
          f"`python ai_research_analyst.py --report`")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
