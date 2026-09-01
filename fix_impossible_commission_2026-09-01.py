"""
One-time correction -- 2026-09-01 impossible net P&L from SIM's
positions/me ProfitLossOnTrade field.

forex/runner._run_exits recorded net_pnl_eur straight from Saxo's
positions/me net (ProfitLossOnTrade + TradeCostsTotal). On SIM that field
is unreliable and produced a NEGATIVE commission_eur (gross - net) -- i.e.
a broker rebate, which is impossible. commission is always a cost.

Affected (commission_eur < 0):
  * sim:rsi:MXNUSD          gross -3.37 -> net +8.23  ("commission" -11.59)
                            => a LOSS booked as +$10 WIN, ML label WON, r +0.12
  * sim:pullback / advanced_pullback_master : NZDPLN  (x23 -- a re-entry
                            loop: SIM holds no position, entry pinned at
                            2.22795 while the real quote is ~2.20)
  * sim:donchian:CHFMXN

forex/runner._sane_net_pnl_quote (same commit) gates this going forward.
This script:
  * recomputes net_pnl_eur = gross_pnl_eur - cost_eur (paired entry card's
    Saxo-quoted round-trip cost), commission_eur = -cost_eur, r_multiple,
    and marks net_pnl_reconstructed=true;
  * additionally marks pnl_suspect=true on any record that is part of a
    same-strategy/same-symbol/same-entry re-entry cluster (>=3) -- for
    those even `gross` is unreliable (phantom position, frozen scan
    price), so report_giveback / the journal must skip them entirely;
  * updates the matching pnl_ledger.db closed rows' realized_pnl.

Dry run by default; --apply to write.
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
CARDS = os.path.join(BASE, "data", "trade_observation_cards.jsonl")
LEDGER = os.path.join(BASE, "data", "pnl_ledger.db")
MARKER = "impossible-commission-fix-2026-09-01"
CLUSTER_MIN = 3

G, R, Y, DIM, X = "\033[92m", "\033[91m", "\033[93m", "\033[2m", "\033[0m"


def _impossible(exit_c: dict) -> bool:
    """The one reliable signal: a negative commission (net better than
    gross) is a rebate -- impossible. A cost-driven sign flip on a small
    trade (net<0 while gross>0) is legitimate and NOT flagged."""
    cm = exit_c.get("commission_eur")
    return cm is not None and cm < -0.01 and exit_c.get("gross_pnl_eur") is not None


def _gross_trustworthy(symbol: str) -> bool:
    """Once Saxo's SIM net P&L is proven bad for a trade (impossible
    commission), can we still trust our own price-move `gross`? Only for a
    liquid pair -- for a thin exotic the same SIM chart feed that gave the
    bad net also drives `gross`."""
    try:
        from forex.universe import HIGH_VOLUME_SYMBOLS, CORE_STANDARD_SYMBOLS
        return symbol in (set(HIGH_VOLUME_SYMBOLS) | set(CORE_STANDARD_SYMBOLS))
    except Exception:
        return symbol in {"EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD",
                          "USDCAD", "NZDUSD", "EURGBP", "EURJPY", "MXNUSD"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    lines = open(CARDS, encoding="utf-8").read().splitlines()
    entry = {}
    for ln in lines:
        if ln.strip():
            d = json.loads(ln)
            if d.get("event") == "entry" and d.get("card_id"):
                entry[d["card_id"]] = d

    # cluster detection: (strategy, symbol, round(entry_price,6)) seen >=3 times
    cluster_key = Counter()
    for ln in lines:
        if not ln.strip():
            continue
        d = json.loads(ln)
        if d.get("event") != "exit" or not _impossible(d):
            continue
        e = entry.get(d.get("card_id"), {})
        if e:
            cluster_key[(e.get("strategy"), e.get("symbol"), round(e.get("entry_price") or 0, 6))] += 1
    clusters = {k for k, n in cluster_key.items() if n >= CLUSTER_MIN}

    _env_mod = {"sim": "forex", "live": "forex_live", "live_eur": "forex_live_eur"}
    fixes, out = [], []
    for ln in lines:
        if not ln.strip():
            out.append(ln)
            continue
        d = json.loads(ln)
        if d.get("event") == "exit" and _impossible(d):
            e = entry.get(d.get("card_id"), {})
            sym = e.get("symbol")
            in_cluster = (e.get("strategy"), sym,
                          round(e.get("entry_price") or 0, 6)) in clusters
            module = _env_mod.get(e.get("account_env"))
            suspect = in_cluster or not _gross_trustworthy(sym)
            if suspect:
                # SIM's net was proven bad AND we can't trust `gross`
                # either (thin exotic feed, or a phantom re-entry loop with
                # a frozen scan entry price). Null the P&L fields (like
                # mae_mfe_invalidated) so the journal / give-back skip it.
                reason = ("re-entry loop / phantom SIM position" if in_cluster
                          else "thin-exotic SIM price feed unreliable")
                fixes.append((e.get("strategy"), sym, module,
                              d.get("net_pnl_eur"), None, d.get("r_multiple"), None,
                              True, d.get("timestamp")))
                d["gross_pnl_eur"] = d["net_pnl_eur"] = d["commission_eur"] = None
                d["r_multiple"] = None
                d["pnl_suspect"] = True
                d["pnl_suspect_reason"] = reason + " — prices unreliable"
                d["pnl_fix"] = MARKER
            else:
                cost = e.get("cost_eur")
                cost = float(cost) if isinstance(cost, (int, float)) and cost > 0 else 5.0
                new_net = round(d["gross_pnl_eur"] - cost, 2)
                risk = e.get("risk_eur")
                new_r = round(new_net / risk, 2) if risk and risk > 0 else d.get("r_multiple")
                fixes.append((e.get("strategy"), e.get("symbol"), module,
                              d.get("net_pnl_eur"), new_net, d.get("r_multiple"), new_r,
                              False, d.get("timestamp")))
                d["net_pnl_eur"] = new_net
                d["commission_eur"] = round(-cost, 2)
                d["r_multiple"] = new_r
                d["net_pnl_reconstructed"] = True
                d["pnl_fix"] = MARKER
        out.append(json.dumps(d, ensure_ascii=False))

    n_susp = sum(1 for f in fixes if f[7])
    print(f"{len(fixes)} exit cards ({n_susp} nulled as pnl_suspect — re-entry clusters, "
          f"{len(fixes) - n_susp} recomputed):\n")
    agg = {}
    for strat, sym, module, old_n, new_n, old_r, new_r, susp, _ in fixes:
        mark = "  ⚠" if susp else "   "
        shown = "NULL (suspect)" if susp else f"{new_n:>8.2f}"
        print(f"{mark} {DIM}{strat:26s} {sym:8s}{X} net {old_n:>8.2f} -> {R}{shown}{X}")
        agg.setdefault((strat, sym), [0, 0.0, 0.0, susp])
        agg[(strat, sym)][0] += 1
        agg[(strat, sym)][1] += (old_n or 0)
        agg[(strat, sym)][2] += (new_n if new_n is not None else 0.0)
    print(f"\n  {'strategy / symbol':34s}  n   old sum    new sum   note")
    for (s, sym), (n, o, nw, susp) in sorted(agg.items()):
        note = "nulled (suspect)" if susp else "recomputed"
        print(f"  {s + ' / ' + sym:34s}  {n:<3d} {o:>9.2f}  {nw:>9.2f}   {note}")

    # ── ledger (matched by strategy + symbol + MODULE + close time) ──────
    led = []
    if os.path.exists(LEDGER):
        con = sqlite3.connect(LEDGER)
        con.row_factory = sqlite3.Row
        for strat, sym, module, old_n, new_n, old_r, new_r, susp, xts in fixes:
            if not module:
                continue
            try:
                xdt = datetime.fromisoformat(xts.replace("Z", "+00:00")).replace(tzinfo=None)
            except (ValueError, AttributeError):
                continue
            lo = (xdt + timedelta(hours=5) - timedelta(minutes=8)).isoformat()
            hi = (xdt + timedelta(hours=5) + timedelta(minutes=8)).isoformat()
            for row in con.execute(
                "SELECT id, realized_pnl FROM trades WHERE module=? AND strategy=? AND symbol=? "
                "AND status='closed' AND timestamp_close BETWEEN ? AND ?",
                    (module, strat, sym, lo, hi)):
                led.append((row["id"], row["realized_pnl"], new_n, susp))
                if a.apply:
                    con.execute("UPDATE trades SET realized_pnl=? WHERE id=?",
                                (new_n, row["id"]))    # new_n is None for suspect -> NULL
        if a.apply:
            con.commit()
        con.close()
    print(f"\n  {len(led)} ledger rows matched:")
    for lid, old, new, susp in led:
        shown = "NULL" if new is None else f"{new:>8.2f}"
        print(f"    id {lid:5d}  realized_pnl {old:>8.2f} -> {shown}  {'(suspect)' if susp else ''}")

    if not a.apply:
        print(f"\n{Y}DRY RUN — re-run with --apply to write.{X}")
        return 0
    shutil.copy(CARDS, CARDS + ".bak_" + MARKER)
    with open(CARDS + ".tmp", "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    os.replace(CARDS + ".tmp", CARDS)
    print(f"\n{G}APPLIED — {len(fixes)} cards ({n_susp} suspect), {len(led)} ledger rows.{X}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
