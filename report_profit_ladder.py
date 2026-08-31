"""
report_profit_ladder.py -- read-only forward-test report for the RSI(2)
profit-protection ladder (enabled on sim + live + live_eur, 2026-08-31).

Answers the only question that matters: does the ladder take profit
EFFICIENTLY -- i.e. keep more of each trade's max favourable excursion
(MFE) without clipping winners so early that expectancy drops?

Method: pair entry+exit observation cards (data/trade_observation_cards.jsonl)
by card_id. For each closed trade:
  capture   = net_pnl_eur / mfe_eur           (of a trade that WAS in profit)
  give_back = mfe_eur - net_pnl_eur           (EUR left on the table)
Split by strategy:
  rsi                 -> ladder ACTIVE
  advanced_rsi_master -> ladder OFF (rsi's untouched A/B twin = the control)
and by the highest ladder rung each trade reached.

Nothing here mutates state or touches Saxo. Run any time:
    python report_profit_ladder.py
"""

import json
import os
import statistics
import sys
from collections import Counter, defaultdict

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CARDS = os.path.join(BASE_DIR, "data", "trade_observation_cards.jsonl")

GREEN, RED, YELLOW, CYAN, DIM, RESET, BOLD = (
    "\033[92m", "\033[91m", "\033[93m", "\033[96m", "\033[2m", "\033[0m", "\033[1m"
)


def _load_closed():
    if not os.path.exists(CARDS):
        return []
    entry, exits = {}, []
    for line in open(CARDS, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            c = json.loads(line)
        except Exception:
            continue
        if c.get("event") == "entry":
            entry[c["card_id"]] = c
        elif c.get("event") == "exit":
            exits.append(c)
    trades = []
    for x in exits:
        e = entry.get(x["card_id"])
        if not e:
            continue
        trades.append({**e, **x})   # exit fields win
    return trades


def _fmt(v, w=8, nd=1):
    return f"{v:>{w}.{nd}f}" if isinstance(v, (int, float)) else f"{'--':>{w}}"


def _stats(trades, label):
    n = len(trades)
    print(f"\n{BOLD}{CYAN}{label}{RESET}  ({n} closed trade(s))")
    if not n:
        print(f"  {DIM}no closed trades yet{RESET}")
        return
    nets = [t["net_pnl_eur"] for t in trades if t.get("net_pnl_eur") is not None]
    wins = [v for v in nets if v > 0]
    wr = 100 * len(wins) / len(nets) if nets else 0
    rs = [t["r_multiple"] for t in trades if t.get("r_multiple") is not None]
    # give-back: only meaningful for trades that were ever in profit
    inprofit = [t for t in trades if (t.get("mfe_eur") or 0) > 0
                and t.get("net_pnl_eur") is not None]
    givebacks = [t["mfe_eur"] - t["net_pnl_eur"] for t in inprofit]
    captures = [t["net_pnl_eur"] / t["mfe_eur"] for t in inprofit if t["mfe_eur"] > 0]
    print(f"  net P&L EUR      {sum(nets):+8.1f}   win rate {wr:4.1f}%   "
          f"avg R {(_fmt(statistics.mean(rs),6,2) if rs else '  --')}")
    if inprofit:
        print(f"  of {len(inprofit)} trades that reached profit:")
        print(f"    avg MFE EUR         {statistics.mean(t['mfe_eur'] for t in inprofit):7.1f}")
        print(f"    avg give-back EUR   {statistics.mean(givebacks):7.1f}   "
              f"(median {statistics.median(givebacks):.1f})")
        print(f"    avg capture ratio  {statistics.mean(captures):7.2f}   "
              f"{GREEN if statistics.mean(captures) > 0.5 else YELLOW}"
              f"(1.0 = kept all of MFE, <=0 = gave it all back){RESET}")
    print(f"  exit reasons: {dict(Counter(t.get('exit_reason','?').split(' ')[0] for t in trades))}")
    rungs = Counter(t.get("ladder_rung") or "none-reached" for t in trades)
    if any(k != "none-reached" for k in rungs):
        print(f"  ladder rung reached: {dict(rungs)}")


def main():
    trades = _load_closed()
    rsi_all   = [t for t in trades if t.get("strategy") == "rsi"]
    ctrl_all  = [t for t in trades if t.get("strategy") == "advanced_rsi_master"]

    print(f"{BOLD}RSI profit-ladder forward test{RESET}  "
          f"{DIM}(data/trade_observation_cards.jsonl, {len(trades)} paired trades total){RESET}")

    for env in ("sim", "live", "live_eur"):
        r = [t for t in rsi_all if t.get("account_env") == env]
        c = [t for t in ctrl_all if t.get("account_env") == env]
        if not r and not c:
            continue
        print(f"\n{BOLD}{'='*66}{RESET}\n{BOLD}  {env.upper()}{RESET}\n{BOLD}{'='*66}{RESET}")
        _stats(r, "rsi  (ladder ACTIVE)")
        _stats(c, "advanced_rsi_master  (ladder OFF -- control)")

    total = len(rsi_all) + len(ctrl_all)
    print(f"\n{BOLD}{'-'*66}{RESET}")
    if total < 30:
        print(f"{YELLOW}  {total} RSI trades closed so far -- too few to judge. "
              f"Re-run weekly; this becomes meaningful around 30-50+ per arm.{RESET}")
    else:
        print(f"{GREEN}  {total} RSI trades -- compare 'avg capture ratio' and 'avg R' "
              f"between rsi (ladder) and advanced_rsi_master (control) above.{RESET}")
    print()


if __name__ == "__main__":
    sys.exit(main())
