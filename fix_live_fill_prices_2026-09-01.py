"""
One-time correction -- 2026-09-01 LIVE fill-price bug.

Until forex/runner.py's fill-confirmation fix (this same commit), every LIVE
position was recorded at sig["close"] (the scan chart's last bar close),
not the real Saxo average fill. This walks the live Saxo API for the TRUE
OpenPrice / ClosingPrice of every currently-open LIVE position and the
MXNUSD round-trip that closed 2026-08-31, and rewrites:

  * data/forex_live_eur_state.json / data/forex_live_state.json  (entry_price)
  * data/pnl_ledger.db                                           (open rows + MXNUSD close)
  * data/trade_observation_cards.jsonl                           (entry + exit cards)

Price-derived fields (risk_eur, r_multiple, gross P&L) are recomputed; the
embedded FX rate is preserved by scaling on the stop distance. A
price_source marker is added so the AI journal knows these are corrected.
Run with --apply to write; default is a dry run.
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import saxo_client as sc

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
LEDGER = os.path.join(DATA, "pnl_ledger.db")
CARDS = os.path.join(DATA, "trade_observation_cards.jsonl")
EUR_STATE = os.path.join(DATA, "forex_live_eur_state.json")
SEK_STATE = os.path.join(DATA, "forex_live_state.json")
MARKER = "saxo-fill-truth-2026-09-01"

EUR_AK_PREFIX = "S1PSnoPluJIw"
SEK_AK_PREFIX = "VrmJQPNxqGgS"


def live_open_prices():
    """{(ak_prefix, uic): OpenPrice} for every open LIVE position."""
    out = {}
    for p in sc.get_positions(env="live_eur").get("Data", []):
        b = p.get("PositionBase", {})
        ak = (b.get("AccountKey") or "")[:12]
        if b.get("OpenPrice"):
            out[(ak, b.get("Uic"))] = float(b["OpenPrice"])
    return out


def mxnusd_closed():
    for c in sc.get_closed_positions(env="live_eur").get("Data", []):
        cp = c.get("ClosedPosition", {})
        if cp.get("Uic") == 17761:
            return cp
    return None


def fix_state(path, prices, ak_prefix, changes):
    s = json.load(open(path, encoding="utf-8"))
    for key, pos in s.get("positions", {}).items():
        uic = pos.get("uic")
        real = prices.get((ak_prefix, uic))
        if real is None:
            continue
        old = pos.get("entry_price")
        if old is None or abs(real - old) < 1e-9:
            continue
        changes.append(f"  state {os.path.basename(path)}  {key:24s} entry {old:.6f} -> {real:.6f}  ({(real/old-1)*100:+.3f}%)")
        pos["entry_price"] = real
        pos["entry_price_corrected"] = MARKER
    return s


def fix_ledger(prices, mxn, changes, apply):
    con = sqlite3.connect(LEDGER)
    con.row_factory = sqlite3.Row
    uic_by_sym = {}
    # open LIVE rows -> match symbol to a live position by module+symbol
    rows = con.execute(
        "SELECT * FROM trades WHERE module IN ('forex_live','forex_live_eur') AND status!='closed'"
    ).fetchall()
    live_syms = {  # symbol -> (ak_prefix, uic)
        "EURUSD": (EUR_AK_PREFIX, 21), "GBPUSD": (EUR_AK_PREFIX, 31), "GBPPLN": (EUR_AK_PREFIX, 8712),
    }
    sek_syms = {"AUDUSD": (SEK_AK_PREFIX, 4), "EURNOK": (SEK_AK_PREFIX, 19),
                "GBPUSD": (SEK_AK_PREFIX, 31), "AUDCHF": (SEK_AK_PREFIX, 5027)}
    for r in rows:
        sym = r["symbol"]
        lut = live_syms if r["module"] == "forex_live_eur" else sek_syms
        tgt = lut.get(sym)
        if not tgt:
            continue
        real = prices.get(tgt)
        if real is None or abs(real - r["entry_price"]) < 1e-9:
            continue
        changes.append(f"  ledger id {r['id']:5d} {r['module']:14s} {sym:7s} entry {r['entry_price']:.6f} -> {real:.6f}")
        if apply:
            con.execute("UPDATE trades SET entry_price=? WHERE id=?", (real, r["id"]))
    # MXNUSD closed row
    if mxn:
        op, clp = float(mxn["OpenPrice"]), float(mxn["ClosingPrice"])
        row = con.execute(
            "SELECT * FROM trades WHERE module='forex_live_eur' AND symbol='MXNUSD' AND status='closed' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row and (abs(row["entry_price"] - op) > 1e-9 or abs((row["exit_price"] or 0) - clp) > 1e-9):
            changes.append(f"  ledger id {row['id']:5d} forex_live_eur MXNUSD  entry {row['entry_price']:.6f} -> {op:.6f}   exit {row['exit_price']:.6f} -> {clp:.6f}")
            if apply:
                con.execute("UPDATE trades SET entry_price=?, exit_price=? WHERE id=?", (op, clp, row["id"]))
    if apply:
        con.commit()
    con.close()


def fix_cards(prices, mxn, changes, apply):
    if not os.path.exists(CARDS):
        return
    card_uic = {
        "live_eur:rsi:EURUSD:2026-08-30T22:09:41.977028+00:00": (EUR_AK_PREFIX, 21),
        "live_eur:rsi:GBPUSD:2026-08-30T22:09:44.446051+00:00": (EUR_AK_PREFIX, 31),
    }
    out = []
    for line in open(CARDS, encoding="utf-8"):
        s = line.strip()
        if not s:
            out.append(line)
            continue
        d = json.loads(s)
        cid = d.get("card_id", "")
        ev = d.get("event")
        # open-position entry cards
        if cid in card_uic and ev == "entry":
            real = prices.get(card_uic[cid])
            old = d.get("entry_price")
            if real and old and abs(real - old) > 1e-9:
                stop = d.get("current_stop")
                if stop and d.get("risk_eur"):
                    d["risk_eur"] = round(d["risk_eur"] * abs(real - stop) / abs(old - stop), 2)
                changes.append(f"  card  {cid[:52]:52s} entry {old:.6f} -> {real:.6f}  risk_eur~{d.get('risk_eur')}")
                d["entry_price"] = real
                d["price_source"] = MARKER
        # MXNUSD entry + exit
        if cid.startswith("live_eur:rsi:MXNUSD:2026-08-28") and mxn:
            op, clp = float(mxn["OpenPrice"]), float(mxn["ClosingPrice"])
            if ev == "entry" and abs(d.get("entry_price", 0) - op) > 1e-9:
                old = d["entry_price"]
                stop = d.get("current_stop")
                rate = None
                if stop and d.get("risk_eur"):
                    rate = d["risk_eur"] / (abs(old - stop) * d.get("quantity", 20000))
                    d["risk_eur"] = round(abs(op - stop) * d.get("quantity", 20000) * rate, 2)
                changes.append(f"  card  MXNUSD entry {old:.6f} -> {op:.6f}  risk_eur~{d.get('risk_eur')}")
                d["entry_price"] = op
                d["price_source"] = MARKER
                d["_rate_eur_per_quote"] = rate
            elif ev == "exit" and abs((d.get("exit_price") or 0) - clp) > 1e-9:
                old = d.get("exit_price")
                gross_q = float(mxn["ClosedProfitLoss"])            # +2.471 USD, authoritative
                cost_q = abs(float(mxn.get("CostOpening", 0))) + abs(float(mxn.get("CostClosing", 0)))
                # recover eur/quote rate from the entry card we just wrote
                rate = None
                for o in out:
                    try:
                        od = json.loads(o)
                        if od.get("card_id") == cid and od.get("event") == "entry":
                            rate = od.get("_rate_eur_per_quote")
                    except Exception:
                        pass
                if rate:
                    g = round(gross_q * rate, 2)
                    c = round(cost_q * rate, 2)
                    d["gross_pnl_eur"] = g
                    d["commission_eur"] = c
                    d["net_pnl_eur"] = round(g - c, 2)
                    rk = None
                    for o in out:
                        try:
                            od = json.loads(o)
                            if od.get("card_id") == cid and od.get("event") == "entry":
                                rk = od.get("risk_eur")
                        except Exception:
                            pass
                    if rk:
                        d["r_multiple"] = round(d["net_pnl_eur"] / rk, 2)
                changes.append(f"  card  MXNUSD exit  {old:.6f} -> {clp:.6f}  net_eur~{d.get('net_pnl_eur')}  R~{d.get('r_multiple')}")
                d["exit_price"] = clp
                d["price_source"] = MARKER
                # -207 EUR MAE on a ~5 EUR-risk trade is the separate window bug
                if d.get("mae_eur") is not None and abs(d["mae_eur"]) > 25 * (rk or 5):
                    d["mae_eur_raw"], d["mfe_eur_raw"] = d.get("mae_eur"), d.get("mfe_eur")
                    d["mae_eur"] = d["mfe_eur"] = None
                    d["mae_mfe_invalidated"] = "unbounded-daily-window-bug-2026-09-01"
        out.append(json.dumps(d, ensure_ascii=False) + "\n")
    # strip the scratch key
    cleaned = []
    for o in out:
        try:
            od = json.loads(o)
            od.pop("_rate_eur_per_quote", None)
            cleaned.append(json.dumps(od, ensure_ascii=False) + "\n")
        except Exception:
            cleaned.append(o)
    if apply:
        cleaned_txt = "".join(cleaned)
        open(CARDS, "w", encoding="utf-8").write(cleaned_txt)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    prices = live_open_prices()
    mxn = mxnusd_closed()
    print("Live OpenPrices:", {f"{k[0]}/{k[1]}": v for k, v in prices.items()})
    print("MXNUSD closed:", None if not mxn else
          f"open {mxn['OpenPrice']:.6f} close {mxn['ClosingPrice']:.6f} pnl {mxn['ClosedProfitLoss']:.3f} quote")
    print()

    changes = []
    eur = fix_state(EUR_STATE, prices, EUR_AK_PREFIX, changes)
    sek = fix_state(SEK_STATE, prices, SEK_AK_PREFIX, changes)
    fix_ledger(prices, mxn, changes, a.apply)
    fix_cards(prices, mxn, changes, a.apply)

    print("\n".join(changes) if changes else "  (nothing to change)")
    print()
    if not a.apply:
        print("DRY RUN — re-run with --apply to write.")
        return
    for path, obj in ((EUR_STATE, eur), (SEK_STATE, sek)):
        shutil.copy(path, path + ".bak_" + MARKER)
        json.dump(obj, open(path, "w", encoding="utf-8"), indent=2)
    print("APPLIED. State backups written with ." + MARKER + " suffix.")


if __name__ == "__main__":
    main()
