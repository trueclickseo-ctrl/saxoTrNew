"""
ibkr_copilot_dashboard.py
-------------------------
Read-only terminal dashboard for AI Copilot decisions.
Reads data/ai_shadow_decisions.jsonl — zero API calls, zero cost.

Usage:
    python ibkr_copilot_dashboard.py           # refresh every 30s
    python ibkr_copilot_dashboard.py --once    # print once and exit
    python ibkr_copilot_dashboard.py --account ibkr_paper   # filter account
    python ibkr_copilot_dashboard.py --today   # today's decisions only
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

_ROOT = Path(__file__).parent
_SHADOW_FILE = _ROOT / "data" / "ai_shadow_decisions.jsonl"

# ── ANSI helpers ──────────────────────────────────────────────────────────────
_WIN = sys.platform == "win32"
if _WIN:
    import ctypes
    ctypes.windll.kernel32.SetConsoleMode(ctypes.windll.kernel32.GetStdHandle(-11), 7)

R  = "\033[91m"; G  = "\033[92m"; Y  = "\033[93m"; B  = "\033[94m"
C  = "\033[96m"; W  = "\033[97m"; DIM = "\033[2m"; BOLD = "\033[1m"
RST = "\033[0m"

def _clr(text: str, colour: str) -> str:
    return f"{colour}{text}{RST}"

def _action_clr(a: str) -> str:
    return {
        "APPROVE": G + "APPROVE" + RST,
        "REJECT":  R + "REJECT " + RST,
        "MODIFY":  Y + "MODIFY " + RST,
    }.get(a, a)

def _pct(n: int, total: int) -> str:
    return f"{100*n//total:3d}%" if total else "  —%"


# ── Data loading ──────────────────────────────────────────────────────────────

def _load(account_filter: str | None) -> list[dict]:
    if not _SHADOW_FILE.exists():
        return []
    decisions = []
    with open(_SHADOW_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if account_filter and d.get("account_env") != account_filter:
                continue
            decisions.append(d)
    return decisions


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ── Rendering ─────────────────────────────────────────────────────────────────

def _render(decisions: list[dict], today_only: bool) -> None:
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    today = _today_utc()

    # Partition
    today_dec  = [d for d in decisions if d.get("ts", "")[:10] == today]
    recent     = decisions[-50:]  # last 50 for stats

    total   = len(decisions)
    t_total = len(today_dec)

    # Overall counts
    cnt   = defaultdict(int)
    mults = []
    strat_cnt: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    regime_cnt: dict[str, dict] = defaultdict(lambda: defaultdict(int))

    for d in decisions:
        a = d.get("agent_action", "?")
        cnt[a] += 1
        if a == "MODIFY":
            mults.append(d.get("agent_size_multiplier", 1.0) or 1.0)
        strat_cnt[d.get("strategy", "?")][a] += 1
        regime_cnt[d.get("regime", "?")][a] += 1

    approve = cnt["APPROVE"]
    reject  = cnt["REJECT"]
    modify  = cnt["MODIFY"]
    avg_mult = sum(mults) / len(mults) if mults else 1.0

    os.system("cls" if _WIN else "clear")

    print(f"\n  {BOLD}AI Copilot Dashboard{RST}  {DIM}·  {now_str}  ·  zero API cost{RST}")
    print(f"  {'═'*84}")

    # ── Header stats ──────────────────────────────────────────────────────────
    print(f"\n  {BOLD}All-time  ({total} decisions){RST}")
    print(f"  {'─'*56}")
    bar_a = "█" * (approve * 40 // max(total,1))
    bar_r = "█" * (reject  * 40 // max(total,1))
    bar_m = "█" * (modify  * 40 // max(total,1))
    print(f"  {_clr('APPROVE','')}{G}{bar_a:<40}{RST}  {approve:>4}  {_pct(approve, total)}")
    print(f"  {_clr('REJECT ','')}{R}{bar_r:<40}{RST}  {reject:>4}  {_pct(reject,  total)}")
    print(f"  {_clr('MODIFY ','')}{Y}{bar_m:<40}{RST}  {modify:>4}  {_pct(modify,  total)}")
    print(f"  {DIM}Avg MODIFY multiplier: {avg_mult:.2f}x{RST}")

    # ── Today's decisions ─────────────────────────────────────────────────────
    print(f"\n  {BOLD}Today  ({t_total} decisions){RST}")
    print(f"  {'─'*84}")
    if not today_dec:
        print(f"  {DIM}No decisions yet today.{RST}")
    else:
        print(f"  {'Time':<8}  {'Action':<10}  {'Strategy':<20}  {'Symbol':<8}  {'Regime':<20}  {'Mult':>5}  Comment")
        print(f"  {'─'*8}  {'─'*10}  {'─'*20}  {'─'*8}  {'─'*20}  {'─'*5}  {'─'*30}")
        for d in sorted(today_dec, key=lambda x: x.get("ts","")):
            ts   = d.get("ts", "")[-15:-9] if len(d.get("ts","")) > 9 else "?"
            act  = _action_clr(d.get("agent_action","?"))
            strat = str(d.get("strategy","?"))[:20]
            sym  = str(d.get("symbol","?"))[:8]
            reg  = str(d.get("regime","?"))[:20]
            mult = d.get("agent_size_multiplier") or 1.0
            mult_str = f"{mult:.2f}x" if d.get("agent_action") == "MODIFY" else "   — "
            comment = str(d.get("agent_comment",""))[:55]
            print(f"  {ts:<8}  {act:<10}  {strat:<20}  {sym:<8}  {reg:<20}  {mult_str:>5}  {DIM}{comment}{RST}")

    if today_only:
        print(f"\n  {'─'*84}\n")
        return

    # ── Strategy breakdown ────────────────────────────────────────────────────
    print(f"\n  {BOLD}By strategy (all-time){RST}")
    print(f"  {'─'*72}")
    print(f"  {'Strategy':<22}  {'Total':>6}  {'APPROVE':>8}  {'REJECT':>7}  {'MODIFY':>7}  {'Avg mult':>8}")
    print(f"  {'─'*22}  {'─'*6}  {'─'*8}  {'─'*7}  {'─'*7}  {'─'*8}")
    for strat in sorted(strat_cnt, key=lambda s: -sum(strat_cnt[s].values())):
        sc = strat_cnt[strat]
        tot = sum(sc.values())
        ap  = sc.get("APPROVE", 0)
        rj  = sc.get("REJECT",  0)
        mo  = sc.get("MODIFY",  0)
        strat_mults = [d.get("agent_size_multiplier",1.0) or 1.0
                       for d in decisions
                       if d.get("strategy") == strat and d.get("agent_action") == "MODIFY"]
        sm = f"{sum(strat_mults)/len(strat_mults):.2f}x" if strat_mults else "  — "
        print(f"  {strat:<22}  {tot:>6}  "
              f"{G}{ap:>5}{RST} {_pct(ap,tot)}  "
              f"{R}{rj:>4}{RST} {_pct(rj,tot)}  "
              f"{Y}{mo:>4}{RST} {_pct(mo,tot)}  "
              f"{sm:>8}")

    # ── Regime breakdown ──────────────────────────────────────────────────────
    print(f"\n  {BOLD}By regime (all-time){RST}")
    print(f"  {'─'*64}")
    print(f"  {'Regime':<22}  {'Total':>6}  {'APPROVE':>8}  {'REJECT':>7}  {'MODIFY':>7}")
    print(f"  {'─'*22}  {'─'*6}  {'─'*8}  {'─'*7}  {'─'*7}")
    for reg in sorted(regime_cnt, key=lambda r: -sum(regime_cnt[r].values())):
        rc  = regime_cnt[reg]
        tot = sum(rc.values())
        ap  = rc.get("APPROVE", 0)
        rj  = rc.get("REJECT",  0)
        mo  = rc.get("MODIFY",  0)
        print(f"  {reg:<22}  {tot:>6}  "
              f"{G}{ap:>5}{RST} {_pct(ap,tot)}  "
              f"{R}{rj:>4}{RST} {_pct(rj,tot)}  "
              f"{Y}{mo:>4}{RST} {_pct(mo,tot)}")

    # ── Last 10 decisions ─────────────────────────────────────────────────────
    print(f"\n  {BOLD}Last 10 decisions{RST}")
    print(f"  {'─'*84}")
    print(f"  {'Date':<10}  {'Action':<10}  {'Strategy':<20}  {'Symbol':<8}  {'Regime':<20}  Comment")
    print(f"  {'─'*10}  {'─'*10}  {'─'*20}  {'─'*8}  {'─'*20}  {'─'*30}")
    for d in decisions[-10:]:
        ts    = d.get("ts","")[:10]
        act   = _action_clr(d.get("agent_action","?"))
        strat = str(d.get("strategy","?"))[:20]
        sym   = str(d.get("symbol","?"))[:8]
        reg   = str(d.get("regime","?"))[:20]
        comment = str(d.get("agent_comment",""))[:55]
        print(f"  {ts:<10}  {act:<10}  {strat:<20}  {sym:<8}  {reg:<20}  {DIM}{comment}{RST}")

    print(f"\n  {'─'*84}")
    print(f"  {DIM}Ctrl+C to quit  ·  --once to print once  ·  --today for today only{RST}\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once",    action="store_true")
    ap.add_argument("--today",   action="store_true")
    ap.add_argument("--account", default=None, help="Filter by account_env (e.g. ibkr_paper)")
    ap.add_argument("--interval", type=int, default=30)
    args = ap.parse_args()

    try:
        while True:
            decisions = _load(args.account)
            _render(decisions, today_only=args.today)
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n  Bye.\n")


if __name__ == "__main__":
    main()
