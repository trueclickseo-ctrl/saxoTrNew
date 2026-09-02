"""
close_all_forex_sim.py
----------------------
Flatten the entire forex SIM book in one go.

Used on 2026-09-02 when the SIM roster was cut to the 5 kept strategies
(rsi / rsi_trend / ema_trend / bb_quality / zscore_quality).

SOURCE OF TRUTH = the pnl ledger (data/pnl_ledger.db, module='forex',
status='open'), NOT data/forex_state.json.

Saxo SIM NETS every position on a Uic into ONE net position, so this closes
BY UIC, once per Uic (trying to close two ledger rows on the same Uic
separately is what produced the 409 Conflict storm on the first attempt):
  1. cancel EVERY working order on that Uic
  2. flatten the net Saxo amount with ONE market order (throttled; backs off
     on 429; longer retry on 409)
  3. book EVERY ledger row for that Uic to 'closed'  (net P&L on the first
     row, 0 on the rest)
  4. drop them all from data/forex_state.json
A Uic that still won't flatten after cancelling its orders is booked out
LOCALLY and left for  housekeeping.py --reconcile-only  +  safeguard  (which
auto-closes untracked SIM positions every 30 min).

SAFETY
  * SIM ONLY -- never touches forex_live_state.json / forex_live_eur_state.json.
  * DRY RUN BY DEFAULT. Needs --execute AND typing FLATTEN.
  * Needs a Saxo SIM login (python saxo_auth.py).

    python close_all_forex_sim.py                 # dry run
    python close_all_forex_sim.py --execute       # real
"""
import argparse
import os
import sys
import time
from collections import defaultdict

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

THROTTLE_S     = 0.6     # between Uics
BACKOFF_429_S  = 8.0
RETRY_409_S    = 2.5


def _resolve_root() -> str:
    env = os.environ.get("ATOS_ROOT")
    if env and os.path.exists(os.path.join(env, "data", "pnl_ledger.db")):
        return env
    gitfile = os.path.join(BASE, ".git")
    if os.path.isfile(gitfile):
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
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--root")
    args = ap.parse_args()

    root = args.root or _resolve_root()
    db_path    = os.path.join(root, "data", "pnl_ledger.db")
    state_path = os.path.join(root, "data", "forex_state.json")
    print(f"  ledger : {db_path}\n  state  : {state_path}")
    if not os.path.exists(db_path):
        print("  no pnl_ledger.db -- use --root"); return 1

    import json
    import sqlite3
    import requests
    import saxo_client
    import pnl_tracker
    from forex.universe import get_pair

    con = sqlite3.connect(db_path); con.row_factory = sqlite3.Row
    rows = con.execute("SELECT id, strategy, symbol, direction, entry_price, quantity "
                       "FROM trades WHERE module='forex' AND status='open'").fetchall()
    if not rows:
        print("  forex SIM book already flat."); return 0

    state = json.load(open(state_path, encoding="utf-8")) if os.path.exists(state_path) else {"positions": {}}
    positions = state.setdefault("positions", {})

    # ── group open ledger rows by Uic ───────────────────────────────────
    by_uic = defaultdict(list)
    no_uic = []
    for r in rows:
        key = f"{r['strategy']}:{r['symbol']}"
        uic = (positions.get(key, {}) or {}).get("uic") or (get_pair(r["symbol"]) or {}).get("uic")
        if uic:
            by_uic[int(uic)].append(r)
        else:
            no_uic.append(r)

    # ── live Saxo state ─────────────────────────────────────────────────
    try:
        saxo_pos = {int(p["PositionBase"]["Uic"]): p
                    for p in saxo_client.get_positions(env="sim").get("Data", [])
                    if p.get("PositionBase", {}).get("Uic") is not None}
    except Exception as e:
        print(f"\n  cannot reach Saxo SIM ({str(e)[:90]})"); return 1
    try:
        all_orders = saxo_client.get_orders(env="sim").get("Data", [])
    except Exception:
        all_orders = []
    orders_by_uic = defaultdict(list)
    for o in all_orders:
        u = o.get("Uic")
        if u is not None:
            orders_by_uic[int(u)].append(o.get("OrderId"))

    print(f"\n{'='*74}\n  {'DRY RUN — ' if not args.execute else ''}FLATTEN forex SIM — "
          f"{len(rows)} ledger row(s) across {len(by_uic)} Uic(s)\n{'='*74}")
    live_net = {}
    for uic, rr in sorted(by_uic.items(), key=lambda kv: -len(kv[1])):
        p = saxo_pos.get(uic)
        amt = float(p["PositionBase"].get("Amount", 0.0)) if p else 0.0
        pl  = (p.get("PositionView", {}) or {}).get("ProfitLossOnTradeInBaseCurrency") if p else None
        live_net[uic] = (amt, pl)
        syms = ",".join(sorted({r["symbol"] for r in rr}))
        print(f"  uic {uic:<9} {syms:<10} rows {len(rr):<3} live {amt:+,.0f}  "
              f"P&L {pl:+,.2f} EUR" if pl is not None else
              f"  uic {uic:<9} {syms:<10} rows {len(rr):<3} live {amt:+,.0f}  (paper)")
    if no_uic:
        print(f"  + {len(no_uic)} row(s) with no resolvable uic -- will be booked out locally")
    print("\n  LIVE books NOT touched.")

    if not args.execute:
        print("\n  DRY RUN. Re-run with --execute."); return 0
    if input(f"\n  Type FLATTEN to close {len(rows)} row(s) across {len(by_uic)} Uic(s): ").strip() != "FLATTEN":
        print("  aborted."); return 1

    def _write():
        tmp = state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, state_path)

    def _book(rr, exit_px, pl_net, reason):
        for i, r in enumerate(rr):
            try:
                pnl_tracker.log_close("forex", r["symbol"], float(exit_px), reason,
                                      strategy=r["strategy"],
                                      gross_pnl_base_override=(float(pl_net) if (i == 0 and pl_net is not None) else 0.0))
            except Exception as e:
                print(f"    [warn] log_close {r['strategy']}:{r['symbol']} — {str(e)[:80]}")
            positions.pop(f"{r['strategy']}:{r['symbol']}", None)

    done = local = fail = 0
    booked = 0.0
    for n, (uic, rr) in enumerate(sorted(by_uic.items(), key=lambda kv: -len(kv[1])), 1):
        # 1. cancel every working order on this Uic
        for oid in orders_by_uic.get(uic, []):
            if oid:
                try:
                    saxo_client.cancel_order(str(oid), env="sim")
                except Exception:
                    pass
        time.sleep(0.25)
        amt, pl = live_net.get(uic, (0.0, None))
        exit_px = rr[0]["entry_price"]
        p = saxo_pos.get(uic)
        if p and p["PositionBase"].get("OpenPrice"):
            exit_px = float(p["PositionBase"]["OpenPrice"])

        if abs(amt) < 1:
            _book(rr, exit_px, pl, "roster_flatten_local_2026-09-02")
            local += 1
            continue

        side = "Sell" if amt > 0 else "Buy"
        qty  = int(abs(amt))
        ok = False
        for attempt in range(4):
            try:
                saxo_client.place_market_order(uic, "FxSpot", side, qty, env="sim")
                ok = True
                break
            except requests.exceptions.HTTPError as e:
                code = e.response.status_code if e.response is not None else 0
                if code == 429:
                    print(f"    429 — backing off {BACKOFF_429_S}s")
                    time.sleep(BACKOFF_429_S)
                elif code == 409:
                    time.sleep(RETRY_409_S)
                else:
                    print(f"    {code} {str(e)[:90]}")
                    time.sleep(RETRY_409_S)
            except Exception as e:
                print(f"    {str(e)[:90]}")
                time.sleep(RETRY_409_S)
        if ok:
            _book(rr, exit_px, pl, "roster_flatten_2026-09-02")
            if pl is not None:
                booked += pl
            done += 1
        else:
            # cancelled its orders, still won't flatten -> book local, leave to housekeeping
            _book(rr, exit_px, pl, "roster_flatten_stuck_2026-09-02")
            fail += 1
            print(f"  [stuck] uic {uic} ({rr[0]['symbol']}) — booked local, housekeeping will reconcile Saxo")

        if n % 10 == 0:
            _write()
            print(f"  ... {n}/{len(by_uic)}  (closed {done}, local {local}, stuck {fail})")
        time.sleep(THROTTLE_S)

    for r in no_uic:
        _book([r], r["entry_price"], None, "roster_flatten_local_2026-09-02")
        local += 1

    _write()
    print(f"\n{'='*74}\n  done: {done} flattened on Saxo, {local} booked local (already flat), "
          f"{fail} stuck (booked local)")
    print(f"  booked P&L this run (EUR): {booked:+,.2f}")
    print(f"  NEXT: python housekeeping.py --reconcile-only --modules forex")
    print(f"        (and safeguard's 30-min auto_close_untracked sweep clears any real residual)")
    print(f"{'='*74}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
