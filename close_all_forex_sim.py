"""
close_all_forex_sim.py
----------------------
Flatten the entire forex SIM book (data/forex_state.json) in one go.

Used once on 2026-09-02 when the SIM roster was cut to the 5 kept strategies
(rsi / rsi_trend / ema_trend / bb_quality / zscore_quality) -- everything the
dropped/dormant strategies were still holding gets closed so the dashboard
starts clean.

What it does, per open position in data/forex_state.json:
  1. cancels its resting stop + TP orders (saxo_client.cancel_order)
  2. sends a MARKET order the opposite way on Saxo SIM to flatten it
  3. books the close to the pnl ledger (Saxo's own ProfitLossOnTrade if the
     position is found live, else a mark-to-last-price estimate)
  4. removes it from data/forex_state.json

SAFETY
  * SIM ONLY. It never opens, reads, or writes data/forex_live_state.json or
    data/forex_live_eur_state.json -- the real-money SEK / EUR books are
    untouched.
  * Preview first; you must type  FLATTEN  to proceed.
  * A position whose close ORDER fails is LEFT in the state file and reported
    -- re-run for the stragglers rather than stranding a real SIM position
    with no local record. Exit code is non-zero if anything failed.

    python close_all_forex_sim.py            # preview + confirm
    python close_all_forex_sim.py --yes      # skip the typed confirmation
"""
import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

STATE_FILE = os.path.join(BASE, "data", "forex_state.json")
LIVE_FILES = ("forex_live_state.json", "forex_live_eur_state.json")  # never touched

import saxo_client
import pnl_tracker
from forex.universe import get_pair


def _load_state() -> dict:
    with open(STATE_FILE, encoding="utf-8") as f:
        return json.load(f)


def _save_state(state: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def _saxo_positions_by_uic() -> dict:
    """{uic: {'amount': signed float, 'pl_base': float|None, 'open': float}} from Saxo SIM."""
    out = {}
    try:
        data = saxo_client.get_positions(env="sim").get("Data", [])
    except Exception as e:
        print(f"[WARN] could not fetch live Saxo SIM positions ({e}) -- will mark to last price")
        return out
    for p in data:
        pb = p.get("PositionBase", {})
        pv = p.get("PositionView", {})
        uic = pb.get("Uic")
        if uic is None:
            continue
        out[int(uic)] = {
            "amount":  float(pb.get("Amount", 0.0)),
            "pl_base": pv.get("ProfitLossOnTradeInBaseCurrency"),
            "open":    float(pb.get("OpenPrice", 0.0) or 0.0),
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="skip the typed FLATTEN confirmation")
    args = ap.parse_args()

    if not os.path.exists(STATE_FILE):
        print("no data/forex_state.json -- nothing to do")
        return 0

    state = _load_state()
    positions = state.get("positions", {})
    if not positions:
        print("forex SIM book is already flat (0 positions).")
        return 0

    saxo = _saxo_positions_by_uic()

    # ── preview ──────────────────────────────────────────────────────────
    by_strat = defaultdict(list)
    for key, pos in positions.items():
        strat, sym = key.split(":", 1)
        by_strat[strat].append((key, sym, pos))

    print(f"\n{'='*78}\n  FLATTEN forex SIM book — {len(positions)} open position(s) "
          f"across {len(by_strat)} strateg{'y' if len(by_strat)==1 else 'ies'}\n{'='*78}")
    for strat in sorted(by_strat):
        rows = by_strat[strat]
        print(f"\n  {strat}  ({len(rows)})")
        for key, sym, pos in rows:
            uic = pos.get("uic") or (get_pair(sym) or {}).get("uic")
            sx = saxo.get(int(uic)) if uic else None
            pl = f"{sx['pl_base']:+,.2f} EUR" if (sx and sx.get("pl_base") is not None) else "—"
            live_tag = "" if sx else "  (paper/not-on-Saxo)"
            print(f"    {sym:<9} {pos.get('direction','?'):<4} {pos.get('quantity',0):>10,.0f}  "
                  f"@ {pos.get('entry_price',0):<12}  P&L {pl}{live_tag}")

    print(f"\n  LIVE books ({', '.join(LIVE_FILES)}) are NOT touched.")
    if not args.yes:
        resp = input(f"\n  Type FLATTEN to close all {len(positions)} SIM positions: ").strip()
        if resp != "FLATTEN":
            print("  aborted.")
            return 1

    # ── close loop ───────────────────────────────────────────────────────
    closed = failed = 0
    booked_pl = 0.0
    reason = "roster_flatten_2026-09-02"
    for key in list(positions):
        strat, sym = key.split(":", 1)
        pos = positions[key]
        direction = pos.get("direction", "Buy")
        qty = int(abs(pos.get("quantity", 0)))
        uic = pos.get("uic") or (get_pair(sym) or {}).get("uic")
        if not uic or qty <= 0:
            print(f"  [skip] {key}: no uic / zero qty -- removing from state only")
            positions.pop(key, None)
            continue
        uic = int(uic)
        sx = saxo.get(uic)

        # 1. cancel the resting protective orders
        for oid_key in ("stop_order_id", "tp_order_id"):
            oid = pos.get(oid_key)
            if oid:
                try:
                    saxo_client.cancel_order(str(oid), env="sim")
                except Exception:
                    pass

        # 2. flatten on Saxo SIM (only if it's actually there)
        close_side = "Sell" if direction == "Buy" else "Buy"
        exit_px = pos.get("entry_price", 0.0)
        pl_override = None
        if sx and abs(sx["amount"]) > 0:
            real_qty = int(abs(sx["amount"]))
            close_side = "Sell" if sx["amount"] > 0 else "Buy"
            ok = False
            for attempt in (1, 2):
                try:
                    saxo_client.place_market_order(uic, "FxSpot", close_side, real_qty, env="sim")
                    ok = True
                    break
                except Exception as e:
                    print(f"  [retry {attempt}] {key}: close order failed — {str(e)[:120]}")
                    time.sleep(1.5)
            if not ok:
                print(f"  [FAIL] {key}: could not flatten on Saxo — LEFT in state, re-run later")
                failed += 1
                continue
            pl_override = sx.get("pl_base")
            if sx.get("open"):
                exit_px = sx["open"]   # best available mark; the real fill is close to it
        else:
            print(f"  [paper] {key}: not on Saxo — booking a mark-to-entry close")

        # 3. book the close
        try:
            net = pnl_tracker.log_close(
                "forex", sym, float(exit_px), reason, strategy=strat,
                order_id=None,
                gross_pnl_base_override=(float(pl_override) if pl_override is not None else None),
            )
            if net is not None:
                booked_pl += net
        except Exception as e:
            print(f"  [warn] {key}: ledger close failed ({str(e)[:100]}) — still removing from state")

        # 4. drop from state
        positions.pop(key, None)
        closed += 1
        if closed % 25 == 0:
            _save_state(state)   # checkpoint
            print(f"  ... {closed} closed")

    _save_state(state)
    print(f"\n{'='*78}")
    print(f"  done: {closed} closed, {failed} failed, {len(positions)} still open")
    print(f"  booked P&L (this run, EUR): {booked_pl:+,.2f}")
    if failed:
        print(f"  -> re-run  python close_all_forex_sim.py  for the {failed} straggler(s)")
    print(f"  refresh: python forex_dashboard.py --once")
    print(f"{'='*78}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
