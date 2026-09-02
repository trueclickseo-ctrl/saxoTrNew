"""
close_all_forex_sim.py
----------------------
Flatten the entire forex SIM book in one go.

Used on 2026-09-02 when the SIM roster was cut to the 5 kept strategies
(rsi / rsi_trend / ema_trend / bb_quality / zscore_quality) -- everything the
dormant strategies were still holding gets closed so the dashboard starts
clean.

SOURCE OF TRUTH = the pnl ledger (data/pnl_ledger.db, module='forex',
status='open'), NOT data/forex_state.json. The state file is a per-cycle
cache; the ledger is the durable record. (This matters after a partial /
interrupted run: the ledger still has every open row even if the state file
lost some.) data/forex_state.json is cross-referenced for uic + the resting
stop/TP order ids, and reconciled at the end.

Per open ledger row:
  1. cancel its resting stop + TP orders   (from forex_state.json, if present)
  2. MARKET-close it on Saxo SIM the opposite way (only if it's actually a
     live Saxo position -- paper fills are just booked out)
  3. log_close() -> flips the ledger row to 'closed' (Saxo's own
     ProfitLossOnTrade when found live, else mark-to-entry)
  4. drop it from data/forex_state.json

SAFETY
  * SIM ONLY -- never opens/reads/writes forex_live_state.json or
    forex_live_eur_state.json. The real-money SEK / EUR books are untouched.
  * DRY-RUN BY DEFAULT. It prints what it WOULD do and exits. Nothing happens
    without  --execute  AND typing  FLATTEN  at the prompt.
  * A close whose ORDER fails is reported and its ledger row is LEFT open --
    re-run for stragglers. Exit code is non-zero on any failure.
  * Needs a Saxo SIM login (`python saxo_auth.py` once) to place the real
    close orders. Without one it can only mark paper rows out -- it will warn
    and refuse --execute.

    python close_all_forex_sim.py                 # dry run (safe, default)
    python close_all_forex_sim.py --execute       # real: preview + type FLATTEN
    python close_all_forex_sim.py --root E:\SaxoTrNew\SaxoTrNew   # explicit tree
"""
import argparse
import os
import sys
import time
from collections import defaultdict

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)


def _resolve_root() -> str:
    """The PRIMARY checkout (real runner state + Saxo token), even when this
    script is run from a git worktree."""
    env = os.environ.get("ATOS_ROOT")
    if env and os.path.exists(os.path.join(env, "data", "pnl_ledger.db")):
        return env
    gitfile = os.path.join(BASE, ".git")
    if os.path.isfile(gitfile):                       # worktree: .git is a FILE
        try:
            gitdir = open(gitfile).read().split("gitdir:", 1)[1].strip()
            main = os.path.abspath(os.path.join(gitdir, "..", "..", ".."))
            if os.path.exists(os.path.join(main, "data", "pnl_ledger.db")):
                return main
        except Exception:
            pass
    return BASE


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true",
                    help="actually place the close orders (default: dry run)")
    ap.add_argument("--root", help="repo root holding data/ (default: auto-detect)")
    args = ap.parse_args()

    root = args.root or _resolve_root()
    db_path    = os.path.join(root, "data", "pnl_ledger.db")
    state_path = os.path.join(root, "data", "forex_state.json")
    print(f"  ledger : {db_path}")
    print(f"  state  : {state_path}")
    if not os.path.exists(db_path):
        print("  no pnl_ledger.db there -- use --root to point at the primary checkout")
        return 1

    import json
    import sqlite3
    from forex.universe import get_pair

    # ── authoritative open list = the ledger ─────────────────────────────
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    open_rows = con.execute(
        "SELECT id, strategy, symbol, direction, entry_price, quantity "
        "FROM trades WHERE module='forex' AND status='open'").fetchall()
    if not open_rows:
        print("  ledger shows the forex SIM book already flat (0 open rows).")
        return 0

    state = json.load(open(state_path, encoding="utf-8")) if os.path.exists(state_path) else {"positions": {}}
    positions = state.setdefault("positions", {})

    # ── live Saxo SIM positions (real amount + P&L) ──────────────────────
    saxo_by_uic = {}
    have_saxo = False
    try:
        import saxo_client
        for p in saxo_client.get_positions(env="sim").get("Data", []):
            pb, pv = p.get("PositionBase", {}), p.get("PositionView", {})
            if pb.get("Uic") is not None:
                saxo_by_uic[int(pb["Uic"])] = {
                    "amount":  float(pb.get("Amount", 0.0)),
                    "pl_base": pv.get("ProfitLossOnTradeInBaseCurrency"),
                    "open":    float(pb.get("OpenPrice", 0.0) or 0.0),
                }
        have_saxo = True
    except Exception as e:
        print(f"\n  [WARN] no live Saxo SIM data ({str(e)[:90]})")

    # ── preview ─────────────────────────────────────────────────────────
    by_strat = defaultdict(list)
    for r in open_rows:
        by_strat[r["strategy"]].append(r)
    print(f"\n{'='*78}\n  {'DRY RUN — ' if not args.execute else ''}FLATTEN forex SIM book — "
          f"{len(open_rows)} open ledger row(s), {len(by_strat)} strateg"
          f"{'y' if len(by_strat)==1 else 'ies'}\n{'='*78}")
    real_ct = paper_ct = 0
    for strat in sorted(by_strat):
        rows = by_strat[strat]
        print(f"\n  {strat}  ({len(rows)})")
        for r in rows:
            key = f"{strat}:{r['symbol']}"
            sp  = positions.get(key, {})
            uic = sp.get("uic") or (get_pair(r["symbol"]) or {}).get("uic")
            sx  = saxo_by_uic.get(int(uic)) if uic else None
            if sx and abs(sx["amount"]) > 0:
                real_ct += 1
                pl = f"{sx['pl_base']:+,.2f} EUR" if sx.get("pl_base") is not None else "?"
                tag = f"LIVE {sx['amount']:+,.0f}  P&L {pl}"
            else:
                paper_ct += 1
                tag = "paper / not on Saxo" if have_saxo else "(Saxo unknown)"
            print(f"    {r['symbol']:<9} {r['direction']:<4} {r['quantity']:>10,.0f}  @ {r['entry_price']:<12}  {tag}")
    print(f"\n  {real_ct} live Saxo position(s), {paper_ct} paper/unknown.")
    print("  LIVE books (forex_live*.json) NOT touched.")

    if not args.execute:
        print(f"\n  DRY RUN — nothing done. Re-run with --execute to flatten.")
        return 0
    if not have_saxo:
        print(f"\n  REFUSING --execute without a Saxo SIM login (can't place real closes, "
              f"can't read real P&L). Run: python saxo_auth.py")
        return 1
    if input(f"\n  Type FLATTEN to close all {len(open_rows)} positions: ").strip() != "FLATTEN":
        print("  aborted."); return 1

    # ── close loop ──────────────────────────────────────────────────────
    import pnl_tracker
    closed = failed = 0
    booked = 0.0
    for r in open_rows:
        strat, sym, direction = r["strategy"], r["symbol"], r["direction"]
        key = f"{strat}:{sym}"
        sp  = positions.get(key, {})
        uic = sp.get("uic") or (get_pair(sym) or {}).get("uic")
        if not uic:
            print(f"  [skip] {key}: no uic")
            failed += 1
            continue
        uic = int(uic)
        for oid_key in ("stop_order_id", "tp_order_id"):
            if sp.get(oid_key):
                try:
                    saxo_client.cancel_order(str(sp[oid_key]), env="sim")
                except Exception:
                    pass
        sx = saxo_by_uic.get(uic)
        exit_px = r["entry_price"]
        pl_override = None
        if sx and abs(sx["amount"]) > 0:
            qty = int(abs(sx["amount"]))
            side = "Sell" if sx["amount"] > 0 else "Buy"
            ok = False
            for attempt in (1, 2):
                try:
                    saxo_client.place_market_order(uic, "FxSpot", side, qty, env="sim")
                    ok = True
                    break
                except Exception as e:
                    print(f"  [retry {attempt}] {key}: {str(e)[:110]}")
                    time.sleep(1.5)
            if not ok:
                print(f"  [FAIL] {key}: not flattened — ledger row left open")
                failed += 1
                continue
            pl_override = sx.get("pl_base")
            if sx.get("open"):
                exit_px = sx["open"]
        try:
            net = pnl_tracker.log_close(
                "forex", sym, float(exit_px), "roster_flatten_2026-09-02", strategy=strat,
                gross_pnl_base_override=(float(pl_override) if pl_override is not None else None))
            if net is not None:
                booked += net
        except Exception as e:
            print(f"  [warn] {key}: log_close failed ({str(e)[:90]})")
        positions.pop(key, None)
        closed += 1
        if closed % 25 == 0:
            _write(state_path, state)
            print(f"  ... {closed}/{len(open_rows)}")

    _write(state_path, state)
    print(f"\n{'='*78}\n  done: {closed} closed, {failed} failed")
    print(f"  booked P&L this run (EUR): {booked:+,.2f}")
    if failed:
        print(f"  -> re-run for the {failed} straggler(s)")
    print(f"  then: python housekeeping.py --reconcile-only --modules forex")
    print(f"{'='*78}")
    return 1 if failed else 0


def _write(path, state):
    import json
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


if __name__ == "__main__":
    sys.exit(main())
