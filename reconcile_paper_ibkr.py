"""
reconcile_paper_ibkr.py
-----------------------
Reconcile local ibkr_stocks.db against the actual IBKR paper account.

Compares what IBKR says is open vs what the local DB says is FILLED.
Fixes the mismatch caused by $0 paper-fill exits that never actually closed
the position on IBKR, which later caused orphan short positions.

Usage:
    python reconcile_paper_ibkr.py                  # dry-run, report only
    python reconcile_paper_ibkr.py --apply          # write fixes to DB
    python reconcile_paper_ibkr.py --backfill-dates # fix 0d rows from IBKR execution history
    python reconcile_paper_ibkr.py --apply --close-shorts
        # also cancel any open stop orders for orphan shorts
        # (you still need to close the short positions manually in TWS/portal)
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)

DB_PATH = os.path.join(_ROOT, "data", "ibkr_stocks.db")

# ── ANSI colours ──────────────────────────────────────────────────────────────
GRN  = "\033[92m"
RED  = "\033[91m"
YLW  = "\033[93m"
DIM  = "\033[2m"
BOLD = "\033[1m"
RST  = "\033[0m"


def _last_known_strategy(con: sqlite3.Connection, symbol: str) -> str:
    """Best-guess strategy for a symbol from its most recent trade history.
    Prefer blend/scorer strategies over signal strategies for paper module."""
    # Prefer the most recent blend/scorer entry if one exists
    row = con.execute(
        "SELECT strategy FROM trades WHERE symbol=? AND strategy IN "
        "('blend','blend_v2','scorer_swing','scorer_portfolio','reversion') "
        "ORDER BY created_at DESC LIMIT 1",
        (symbol,)
    ).fetchone()
    if row:
        return row[0]
    row = con.execute(
        "SELECT strategy FROM trades WHERE symbol=? ORDER BY created_at DESC LIMIT 1",
        (symbol,)
    ).fetchone()
    return row[0] if row else "blend"


def _last_known_stop(con: sqlite3.Connection, symbol: str) -> float | None:
    row = con.execute(
        "SELECT stop_price FROM trades WHERE symbol=? AND stop_price IS NOT NULL "
        "ORDER BY created_at DESC LIMIT 1",
        (symbol,)
    ).fetchone()
    return float(row[0]) if row and row[0] else None


_IBKR_TIME_RE = re.compile(r"(\d{8})\s+(\d{2}:\d{2}:\d{2})")


def _parse_ibkr_time(t) -> datetime | None:
    """Parse IBKR execution time -> UTC datetime.

    Accepts either a datetime object (returned by live ib-insync gateway) or the
    legacy string format '20260901 14:30:00 US/Eastern' (returned by some paper
    gateway versions).
    """
    if isinstance(t, datetime):
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    m = _IBKR_TIME_RE.search(t or "")
    if not m:
        return None
    try:
        from datetime import timedelta
        naive = datetime.strptime(m.group(1) + " " + m.group(2), "%Y%m%d %H:%M:%S")
        return (naive + timedelta(hours=4)).replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _fetch_buy_dates(ib) -> dict[str, datetime]:
    """Return {symbol: earliest_buy_fill_datetime_utc} from IBKR execution history."""
    ib.reqExecutions()
    ib.sleep(2)
    result: dict[str, datetime] = {}
    for f in ib.fills():
        if f.execution.side not in ("BOT", "BUY"):
            continue
        sym = f.contract.symbol
        dt  = _parse_ibkr_time(f.execution.time)
        if dt is None:
            continue
        # Keep the earliest buy fill per symbol (first entry)
        if sym not in result or dt < result[sym]:
            result[sym] = dt
    return result


def _dates_from_sold_rows(con: sqlite3.Connection, targets: list) -> dict[str, str]:
    """Match reconciled FILLED rows against SOLD rows by symbol + closest price.

    Returns {symbol: filled_at_iso_string} for symbols where a SOLD row
    with price within 5% is found.  Prefers the closest-price match when
    multiple SOLD rows exist for the same symbol.
    """
    result: dict[str, str] = {}
    for row in targets:
        sym        = row["symbol"]
        entry      = float(row["fill_price"] or 0)
        sold_rows  = con.execute(
            "SELECT fill_price, filled_at FROM trades "
            "WHERE symbol=? AND status='SOLD' AND side='BUY' AND fill_price IS NOT NULL "
            "ORDER BY filled_at DESC",
            (sym,)
        ).fetchall()
        if not sold_rows:
            continue
        best      = min(sold_rows, key=lambda r: abs(float(r[0]) - entry) / max(entry, 0.01))
        diff_pct  = abs(float(best[0]) - entry) / max(entry, 0.01) * 100
        if diff_pct <= 5.0 and best[1]:
            result[sym] = best[1]
    return result


def _backfill_dates(port: int, client_id: int, apply: bool) -> None:
    """Update filled_at on 0-day rows using DB SOLD history, then IBKR executions."""
    print(f"\n{BOLD}Backfill fill dates from trade history{RST}")
    print(f"  DB   : {DB_PATH}")
    print(f"  Mode : {'** APPLY **' if apply else 'DRY-RUN'}\n")

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row

    # Target: RECONCILE_ rows or any FILLED BUY row filled today (0-day rows)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = con.execute(
        "SELECT id, symbol, fill_price, filled_at FROM trades "
        "WHERE side='BUY' AND status='FILLED' "
        "AND (order_id LIKE 'RECONCILE_%' OR filled_at LIKE ?)",
        (f"{today}%",)
    ).fetchall()
    print(f"  Rows to backfill : {len(rows)}")

    # Primary source: match against SOLD rows in the DB
    db_dates = _dates_from_sold_rows(con, rows)
    print(f"  DB SOLD matches  : {len(db_dates)}/{len(rows)}")

    # Secondary source: IBKR execution history (covers current session only)
    ibkr_dates: dict[str, datetime] = {}
    missing = [r for r in rows if r["symbol"] not in db_dates]
    if missing and port:
        try:
            from ibkr_module import ibkr_client as ic
            print(f"  Connecting to 127.0.0.1:{port} for execution history...")
            ib = ic.connect("127.0.0.1", port, client_id)
            ibkr_dates = _fetch_buy_dates(ib)
            ic.disconnect(ib)
            print(f"  IBKR exec history: {len(ibkr_dates)} symbols")
        except Exception as exc:
            print(f"  {DIM}IBKR exec history unavailable: {exc}{RST}")

    print()
    updated = 0
    for row in rows:
        sym     = row["symbol"]
        old_ts  = (row["filled_at"] or "")[:19]

        if sym in db_dates:
            new_ts = db_dates[sym]   # full ISO string including timezone
            src    = "sold-row"
        elif sym in ibkr_dates:
            new_ts = ibkr_dates[sym].isoformat()
            src    = "ibkr-exec"
        else:
            print(f"  {DIM}{sym:<8}  {old_ts}  (no history found){RST}")
            continue

        print(f"  {GRN}+{RST} {sym:<8}  {old_ts}  ->  {new_ts}  [{src}]")
        if apply:
            con.execute("UPDATE trades SET filled_at=? WHERE id=?", (new_ts, row["id"]))
            updated += 1

    if apply:
        con.commit()
        print(f"\n  {GRN}Updated {updated}/{len(rows)} rows.{RST}")
    else:
        print(f"\n  {DIM}Pass --apply to write these dates to the DB.{RST}")

    con.close()
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconcile IBKR paper positions vs local DB")
    parser.add_argument("--apply",          action="store_true", help="Write fixes to ibkr_stocks.db")
    parser.add_argument("--backfill-dates", action="store_true", help="Fix 0-day rows using IBKR execution history")
    parser.add_argument("--client-id",      type=int, default=20, help="ib_insync client ID (default 20)")
    parser.add_argument("--port",           type=int, default=4002, help="IB Gateway port (default 4002)")
    args = parser.parse_args()

    if args.backfill_dates:
        _backfill_dates(args.port, args.client_id, args.apply)
        return

    # ── Connect to IBKR ──────────────────────────────────────────────────────
    print(f"\n{BOLD}Reconcile IBKR paper positions -> ibkr_stocks.db{RST}")
    print(f"  DB   : {DB_PATH}")
    print(f"  Mode : {'** APPLY (writing fixes) **' if args.apply else 'DRY-RUN (no changes)'}")
    print()

    from ibkr_module import ibkr_client as ic
    print(f"  Connecting to IB Gateway 127.0.0.1:{args.port} clientId={args.client_id}...")
    ib = ic.connect("127.0.0.1", args.port, args.client_id)
    print("  Connected.\n")

    # ── Fetch IBKR positions ──────────────────────────────────────────────────
    ibkr_positions: dict[str, dict] = {}   # symbol -> {qty, avg_cost, account}
    for p in ib.positions():
        sym = p.contract.symbol
        ibkr_positions[sym] = {
            "qty":      float(p.position),
            "avg_cost": float(p.avgCost),
            "account":  p.account,
        }

    # ── Fetch IBKR open orders (for stop order IDs) ───────────────────────────
    ib.reqAllOpenOrders()
    ib.sleep(2)
    open_orders: dict[str, dict] = {}   # symbol -> {order_id, aux_price}
    for t in ib.openTrades():
        sym = t.contract.symbol
        if t.order.action == "SELL" and t.order.orderType in ("STP", "STOP"):
            open_orders[sym] = {
                "order_id":  str(t.order.orderId),
                "stop_price": float(getattr(t.order, "auxPrice", 0)),
                "status":    t.orderStatus.status,
            }

    # ── Fetch execution history (for fill dates) ─────────────────────────────
    buy_dates = _fetch_buy_dates(ib)
    print(f"  Execution history: {len(buy_dates)} symbols with buy fills")

    ic.disconnect(ib)
    print(f"  IBKR positions   : {len(ibkr_positions)}")
    print(f"  IBKR stop orders : {len(open_orders)}")

    # ── Load local DB ─────────────────────────────────────────────────────────
    if not os.path.exists(DB_PATH):
        print(f"\n{RED}  ERROR: {DB_PATH} not found.{RST}")
        return

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row

    local_open: dict[str, dict] = {}   # symbol -> row
    for row in con.execute(
        "SELECT * FROM trades WHERE side='BUY' AND status='FILLED' ORDER BY filled_at"
    ).fetchall():
        local_open[row["symbol"]] = dict(row)

    print(f"  Local DB FILLED  : {len(local_open)}")
    print()

    # ── Classify discrepancies ────────────────────────────────────────────────
    orphan_longs  = []   # IBKR long, not in local DB (or marked SOLD)
    orphan_shorts = []   # IBKR short, we need to warn
    ghost_locals  = []   # local says FILLED but not on IBKR

    for sym, ibkr in ibkr_positions.items():
        qty = ibkr["qty"]
        if qty > 0:
            if sym not in local_open:
                orphan_longs.append((sym, ibkr))
            # if already in local_open and qty matches, all good
        elif qty < 0:
            orphan_shorts.append((sym, ibkr))

    for sym in local_open:
        if sym not in ibkr_positions:
            ghost_locals.append((sym, local_open[sym]))

    # ── Report ────────────────────────────────────────────────────────────────
    print(f"{BOLD}{'='*70}{RST}")

    # 1. Orphan shorts — must close manually
    if orphan_shorts:
        print(f"\n{RED}{BOLD}  ORPHAN SHORTS ({len(orphan_shorts)}) — close these manually in TWS/Portal:{RST}")
        print(f"  {'Symbol':<8}  {'Qty':>8}  {'AvgCost':>10}  {'Account'}")
        print("  " + "-" * 50)
        for sym, d in orphan_shorts:
            print(f"  {YLW}{sym:<8}{RST}  {RED}{d['qty']:>8.0f}{RST}  "
                  f"${d['avg_cost']:>9.2f}  {d['account']}")
        print(f"\n  {DIM}-> These are short positions the local DB has no record of.")
        print(f"     Go to IBKR portal, buy to cover each one.{RST}")
    else:
        print(f"\n{GRN}  No orphan shorts.{RST}")

    # 2. Orphan longs — in IBKR but not in local DB
    if orphan_longs:
        print(f"\n{YLW}{BOLD}  ORPHAN LONGS ({len(orphan_longs)}) — in IBKR, missing from local DB:{RST}")
        print(f"  {'Symbol':<8}  {'Qty':>6}  {'AvgCost':>10}  {'LastStrategy':<22}  {'StopOnIBKR':>10}")
        print("  " + "-" * 70)
        for sym, d in orphan_longs:
            strat = _last_known_strategy(con, sym)
            stop  = open_orders.get(sym, {}).get("stop_price", 0)
            stop_s = f"${stop:.2f}" if stop else "none"
            print(f"  {sym:<8}  {d['qty']:>6.0f}  ${d['avg_cost']:>9.2f}  {strat:<22}  {stop_s:>10}")
        if args.apply:
            print(f"\n  {GRN}Inserting missing rows into DB...{RST}")
            now = datetime.now(timezone.utc).isoformat()
            for sym, d in orphan_longs:
                strat      = _last_known_strategy(con, sym)
                stop_price = open_orders.get(sym, {}).get("stop_price") or _last_known_stop(con, sym)
                stop_oid   = open_orders.get(sym, {}).get("order_id")
                trail_high = d["avg_cost"]   # conservative: use avg cost as floor
                exec_dt    = buy_dates.get(sym)
                filled_ts  = exec_dt.isoformat() if exec_dt else now
                date_src   = f"exec {filled_ts[:10]}" if exec_dt else "now (no exec history)"
                con.execute("""
                    INSERT INTO trades
                      (order_id, symbol, side, qty, fill_price, stop_price, stop_order_id,
                       trailing_high, status, created_at, filled_at, strategy)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    f"RECONCILE_{sym}_{now[:10]}",
                    sym,
                    "BUY",
                    d["qty"],
                    d["avg_cost"],
                    stop_price,
                    stop_oid,
                    trail_high,
                    "FILLED",
                    now,
                    filled_ts,
                    strat,
                ))
                print(f"    {GRN}+{RST} {sym:<6}  qty={d['qty']:.0f}  entry=${d['avg_cost']:.2f}"
                      f"  stop={f'${stop_price:.2f}' if stop_price else 'none'}"
                      f"  strategy={strat}  date={date_src}")
            con.commit()
            print(f"  {GRN}Done.{RST}")
        else:
            print(f"\n  {DIM}Pass --apply to insert these rows into the local DB.{RST}")
    else:
        print(f"\n{GRN}  No orphan longs. Local DB matches IBKR.{RST}")

    # 3. Ghost locals — local says open but IBKR says closed/not held
    if ghost_locals:
        print(f"\n{YLW}{BOLD}  GHOST LOCALS ({len(ghost_locals)}) — local DB says FILLED but not on IBKR:{RST}")
        print(f"  {'Symbol':<8}  {'Qty':>6}  {'Entry':>10}  {'Strategy'}")
        print("  " + "-" * 50)
        for sym, row in ghost_locals:
            print(f"  {sym:<8}  {row['qty']:>6.0f}  ${row['fill_price'] or 0:>9.2f}  {row['strategy']}")
        if args.apply:
            print(f"\n  {YLW}Marking ghost locals as SOLD (position no longer on IBKR)...{RST}")
            for sym, row in ghost_locals:
                con.execute(
                    "UPDATE trades SET status='SOLD', fill_price=COALESCE(NULLIF(fill_price,0), trailing_high) "
                    "WHERE id=? AND status='FILLED'",
                    (row["id"],)
                )
                print(f"    {YLW}~{RST} {sym:<6} marked SOLD")
            con.commit()
        else:
            print(f"\n  {DIM}Pass --apply to mark these as SOLD in the local DB.{RST}")
    else:
        print(f"\n{GRN}  No ghost locals.{RST}")

    # 4. Summary
    print(f"\n{BOLD}{'='*70}{RST}")
    print(f"  Orphan shorts (close manually) : {RED}{len(orphan_shorts)}{RST}")
    print(f"  Orphan longs  (add to DB)      : {YLW}{len(orphan_longs)}{RST}")
    print(f"  Ghost locals  (mark SOLD)      : {YLW}{len(ghost_locals)}{RST}")

    in_sync = [sym for sym, d in ibkr_positions.items()
               if d["qty"] > 0 and sym in local_open]
    print(f"  Already in sync                : {GRN}{len(in_sync)}{RST}")

    if not args.apply and (orphan_longs or ghost_locals):
        print(f"\n  {DIM}Run with --apply to fix the local DB.{RST}")
    elif args.apply:
        print(f"\n  {GRN}Local DB updated. Run ibkr_dashboard.py to verify.{RST}")

    con.close()
    print()


if __name__ == "__main__":
    main()
