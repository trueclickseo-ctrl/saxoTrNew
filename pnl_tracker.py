"""
pnl_tracker.py  —  Unified P&L ledger for all 4 trading modules
----------------------------------------------------------------
Database: data/pnl_ledger.db   (SQLite, separate from atos_live.db)

API:
    log_open(module, strategy, symbol, direction, qty, entry_price, ...)
    log_close(module, strategy, symbol, exit_price, exit_reason, ...)
    sync_all()              — bootstrap from existing state files
    get_summary()           — dict of stats per module + grand total
    get_open_positions()    — list of open rows
    get_closed_trades()     — list of closed rows
    print_statement()       — formatted P&L report to stdout
"""

import os, sys, json, sqlite3
from datetime import datetime, date

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
DB_PATH   = os.path.join(BASE_DIR, "data", "pnl_ledger.db")
ATOS_DB   = os.path.join(BASE_DIR, "data", "atos_live.db")
ETF_JSON  = os.path.join(BASE_DIR, "saxo_etf_strategy", "data", "etf_positions.json")
FUT_JSON  = os.path.join(BASE_DIR, "data", "futures_state.json")
FX_JSON   = os.path.join(BASE_DIR, "data", "forex_state.json")

MODULES   = ("stock", "etf", "futures", "forex")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    module           TEXT    NOT NULL,
    strategy         TEXT,
    symbol           TEXT    NOT NULL,
    direction        TEXT,
    quantity         REAL,
    entry_price      REAL,
    exit_price       REAL,
    stop_price       REAL    DEFAULT 0,
    realized_pnl     REAL,
    currency         TEXT    DEFAULT 'USD',
    order_id         TEXT,
    exit_reason      TEXT,
    status           TEXT    DEFAULT 'open',
    timestamp_open   TEXT,
    timestamp_close  TEXT,
    source_ref       TEXT,
    created_at       TEXT    DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_module  ON trades(module);
CREATE INDEX IF NOT EXISTS idx_status  ON trades(status);
CREATE INDEX IF NOT EXISTS idx_symbol  ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_ts_open ON trades(timestamp_open);
"""


# ── Connection ──────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    return c


# ── Write API ───────────────────────────────────────────────────────

def log_open(module: str, strategy: str, symbol: str, direction: str,
             quantity: float, entry_price: float, stop_price: float = 0,
             currency: str = "USD", order_id: str = None,
             timestamp: str = None, source_ref: str = None) -> int:
    """Record a new trade opening. Returns the new row id."""
    ts = timestamp or datetime.now().isoformat()
    with _conn() as c:
        cur = c.execute("""
            INSERT INTO trades
                (module, strategy, symbol, direction, quantity, entry_price,
                 stop_price, currency, order_id, status, timestamp_open, source_ref)
            VALUES (?,?,?,?,?,?,?,?,?,'open',?,?)
        """, (module, strategy, symbol, direction, quantity, entry_price,
              stop_price, currency, order_id, ts, source_ref))
        return cur.lastrowid


def log_close(module: str, symbol: str, exit_price: float,
              exit_reason: str = "", strategy: str = None,
              timestamp: str = None, order_id: str = None) -> float | None:
    """
    Close the most-recent open trade for module+symbol (optionally filtered by strategy).
    Returns realized P&L or None if no matching open trade found.
    """
    ts = timestamp or datetime.now().isoformat()
    q    = "SELECT * FROM trades WHERE module=? AND symbol=? AND status='open'"
    args = [module, symbol]
    if strategy:
        q += " AND strategy=?"
        args.append(strategy)
    q += " ORDER BY id DESC LIMIT 1"

    with _conn() as c:
        row = c.execute(q, args).fetchone()
        if not row:
            return None
        ep       = row["entry_price"]
        qty      = row["quantity"]
        direction = row["direction"]
        raw      = (exit_price - ep) if direction in ("Buy", "BUY") else (ep - exit_price)
        pnl      = raw * qty
        c.execute("""
            UPDATE trades
               SET exit_price=?, realized_pnl=?, exit_reason=?,
                   status='closed', timestamp_close=?
             WHERE id=?
        """, (exit_price, pnl, exit_reason, ts, row["id"]))
        return pnl


def update_stop(module: str, symbol: str, new_stop: float, strategy: str = None):
    """Update stop_price for an open trade."""
    q    = "UPDATE trades SET stop_price=? WHERE module=? AND symbol=? AND status='open'"
    args = [new_stop, module, symbol]
    if strategy:
        q += " AND strategy=?"
        args.append(strategy)
    with _conn() as c:
        c.execute(q, args)


# ── Sync from existing state files ──────────────────────────────────

def _already_synced(source_ref: str) -> bool:
    with _conn() as c:
        r = c.execute("SELECT 1 FROM trades WHERE source_ref=? LIMIT 1",
                      (source_ref,)).fetchone()
        return r is not None


def sync_stocks_from_atos() -> int:
    """Copy closed stock trades from atos_live.db into pnl_ledger. Returns count added."""
    if not os.path.exists(ATOS_DB):
        return 0
    added = 0
    try:
        src  = sqlite3.connect(ATOS_DB)
        src.row_factory = sqlite3.Row
        rows = src.execute(
            "SELECT * FROM trades WHERE exit_date IS NOT NULL ORDER BY id"
        ).fetchall()
        src.close()
        for r in rows:
            ref = f"stock:atos:{r['id']}"
            if _already_synced(ref):
                continue
            # Convert SEK P&L to USD estimate (skip conversion — store in SEK)
            pnl = r["pnl_sek"]
            with _conn() as c:
                c.execute("""
                    INSERT INTO trades
                        (module, strategy, symbol, direction, quantity,
                         entry_price, exit_price, realized_pnl, currency,
                         exit_reason, status, timestamp_open, timestamp_close,
                         source_ref)
                    VALUES ('stock',?,?,?,?,?,?,?,?,?,'closed',?,?,?)
                """, (r["strategy"] or "US Blend",
                      (r["ticker"] or "").split(":")[0].upper(),
                      r["direction"] or "BUY",
                      r["shares"], r["entry_price"], r["exit_price"],
                      pnl, "SEK",
                      r["exit_reason"] or "",
                      r["entry_date"], r["exit_date"],
                      ref))
            added += 1
        # Open stock positions
        src  = sqlite3.connect(ATOS_DB)
        src.row_factory = sqlite3.Row
        open_rows = src.execute(
            "SELECT * FROM trades WHERE exit_date IS NULL ORDER BY id"
        ).fetchall()
        src.close()
        for r in open_rows:
            ref = f"stock:atos:open:{r['id']}"
            if _already_synced(ref):
                continue
            with _conn() as c:
                c.execute("""
                    INSERT INTO trades
                        (module, strategy, symbol, direction, quantity,
                         entry_price, stop_price, currency, status,
                         timestamp_open, source_ref)
                    VALUES ('stock',?,?,?,?,?,?,'SEK','open',?,?)
                """, (r["strategy"] or "US Blend",
                      (r["ticker"] or "").split(":")[0].upper(),
                      r["direction"] or "BUY",
                      r["shares"], r["entry_price"],
                      r["stop_price"] or 0,
                      r["entry_date"], ref))
            added += 1
    except Exception as e:
        print(f"[pnl_tracker] sync_stocks error: {e}")
    return added


def sync_etf_from_json() -> int:
    """Sync ETF orders from etf_positions.json. Returns count added."""
    if not os.path.exists(ETF_JSON):
        return 0
    added = 0
    try:
        d      = json.load(open(ETF_JSON, encoding="utf-8"))
        orders = d.get("orders", [])
        # Track last known entry price per symbol for pairing
        entry_map: dict[str, dict] = {}

        for o in orders:
            sym  = o.get("symbol", "")
            side = (o.get("side") or "").lower()
            qty  = o.get("quantity", 0)
            ts   = o.get("timestamp", "")
            oid  = o.get("order_id", "")
            ref  = f"etf:{sym}:{ts}"
            if _already_synced(ref):
                if side == "buy":
                    entry_map[sym] = o
                continue

            if side == "buy":
                entry_map[sym] = o
                ep = o.get("entry_price", 0)
                with _conn() as c:
                    c.execute("""
                        INSERT INTO trades
                            (module, strategy, symbol, direction, quantity,
                             entry_price, currency, order_id, status,
                             timestamp_open, source_ref)
                        VALUES ('etf','ETF Rotation',?,?,?,?,'USD',?,'open',?,?)
                    """, (sym, "Buy", qty, ep, oid, ts, ref))
                added += 1

            elif side == "sell":
                xp    = o.get("exit_price", 0)
                ep    = entry_map.get(sym, {}).get("entry_price", 0)
                pnl   = (xp - ep) * qty if ep and xp else None
                reason= o.get("reason", "")
                with _conn() as c:
                    # Close the open trade
                    c.execute("""
                        UPDATE trades SET exit_price=?, realized_pnl=?,
                            exit_reason=?, status='closed', timestamp_close=?
                         WHERE module='etf' AND symbol=? AND status='open'
                         ORDER BY id DESC LIMIT 1
                    """, (xp, pnl, reason, ts, sym))
                    # Insert a closed record if no open row existed
                    if c.rowcount == 0 and ep:
                        c.execute("""
                            INSERT INTO trades
                                (module, strategy, symbol, direction, quantity,
                                 entry_price, exit_price, realized_pnl, currency,
                                 exit_reason, status, timestamp_open, timestamp_close, source_ref)
                            VALUES ('etf','ETF Rotation',?,'Buy',?,?,?,?,
                                    'USD',?,'closed',?,?,?)
                        """, (sym, qty, ep, xp, pnl, reason,
                              entry_map.get(sym, {}).get("timestamp", ""), ts, ref))
                added += 1

    except Exception as e:
        print(f"[pnl_tracker] sync_etf error: {e}")
    return added


def sync_futures_from_json() -> int:
    """Sync open futures positions from futures_state.json."""
    if not os.path.exists(FUT_JSON):
        return 0
    added = 0
    try:
        d   = json.load(open(FUT_JSON, encoding="utf-8"))
        for key, pos in d.get("positions", {}).items():
            ref = f"futures:open:{key}"
            if _already_synced(ref):
                continue
            strat, sym = key.split(":", 1) if ":" in key else ("donchian", key)
            with _conn() as c:
                c.execute("""
                    INSERT INTO trades
                        (module, strategy, symbol, direction, quantity,
                         entry_price, stop_price, currency, status,
                         timestamp_open, source_ref)
                    VALUES ('futures',?,?,?,?,?,?,'USD','open',?,?)
                """, (strat, sym,
                      pos.get("direction", "Buy"),
                      pos.get("quantity", 0),
                      pos.get("entry_price", 0),
                      pos.get("stop_price", 0),
                      pos.get("entry_date", ""),
                      ref))
            added += 1
    except Exception as e:
        print(f"[pnl_tracker] sync_futures error: {e}")
    return added


def sync_forex_from_json() -> int:
    """Sync open forex positions from forex_state.json."""
    if not os.path.exists(FX_JSON):
        return 0
    added = 0
    try:
        d   = json.load(open(FX_JSON, encoding="utf-8"))
        for key, pos in d.get("positions", {}).items():
            ref = f"forex:open:{key}"
            if _already_synced(ref):
                continue
            strat, sym = key.split(":", 1) if ":" in key else ("ema", key)
            with _conn() as c:
                c.execute("""
                    INSERT INTO trades
                        (module, strategy, symbol, direction, quantity,
                         entry_price, stop_price, currency, status,
                         timestamp_open, source_ref)
                    VALUES ('forex',?,?,?,?,?,?,'USD','open',?,?)
                """, (strat, sym,
                      pos.get("direction", "Buy"),
                      pos.get("quantity", 0),
                      pos.get("entry_price", 0),
                      pos.get("stop_price", 0),
                      pos.get("entry_date", ""),
                      ref))
            added += 1
        for t in d.get("closed_trades", []):
            ref = f"forex:closed:{t.get('symbol','')}:{t.get('exit_date','')}"
            if _already_synced(ref):
                continue
            strat = t.get("strategy", "ema")
            sym   = t.get("symbol", "")
            ep    = t.get("entry_price", 0)
            xp    = t.get("exit_price",  0)
            qty   = t.get("quantity",    0)
            direc = t.get("direction",   "Buy")
            raw   = (xp - ep) if direc == "Buy" else (ep - xp)
            pnl   = raw * qty
            with _conn() as c:
                c.execute("""
                    INSERT INTO trades
                        (module, strategy, symbol, direction, quantity,
                         entry_price, exit_price, realized_pnl, currency,
                         exit_reason, status, timestamp_open, timestamp_close, source_ref)
                    VALUES ('forex',?,?,?,?,?,?,?,'USD',?,'closed',?,?,?)
                """, (strat, sym, direc, qty, ep, xp, pnl,
                      t.get("exit_reason", ""),
                      t.get("entry_date", ""), t.get("exit_date", ""), ref))
            added += 1
    except Exception as e:
        print(f"[pnl_tracker] sync_forex error: {e}")
    return added


def sync_all() -> dict:
    """Bootstrap the ledger from all state files. Returns dict of counts."""
    return {
        "stock":   sync_stocks_from_atos(),
        "etf":     sync_etf_from_json(),
        "futures": sync_futures_from_json(),
        "forex":   sync_forex_from_json(),
    }


# ── Read API ────────────────────────────────────────────────────────

def get_open_positions(module: str = None) -> list[dict]:
    q    = "SELECT * FROM trades WHERE status='open'"
    args = []
    if module:
        q += " AND module=?"
        args.append(module)
    q += " ORDER BY timestamp_open DESC"
    with _conn() as c:
        return [dict(r) for r in c.execute(q, args).fetchall()]


def get_closed_trades(module: str = None, limit: int = 100,
                      since: str = None) -> list[dict]:
    q    = "SELECT * FROM trades WHERE status='closed'"
    args = []
    if module:
        q += " AND module=?"
        args.append(module)
    if since:
        q += " AND timestamp_close >= ?"
        args.append(since)
    q += " ORDER BY timestamp_close DESC"
    if limit:
        q += f" LIMIT {int(limit)}"
    with _conn() as c:
        return [dict(r) for r in c.execute(q, args).fetchall()]


def get_summary(module: str = None) -> dict:
    """
    Return P&L summary. If module given, returns stats for that module only.
    Otherwise returns dict keyed by module + 'total'.
    """
    modules = [module] if module else list(MODULES)
    result  = {}

    with _conn() as c:
        for mod in modules:
            closed = c.execute("""
                SELECT COUNT(*) AS n,
                       SUM(realized_pnl)                        AS total_pnl,
                       SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) AS wins,
                       SUM(CASE WHEN realized_pnl < 0 THEN 1 ELSE 0 END) AS losses,
                       SUM(CASE WHEN realized_pnl > 0 THEN realized_pnl ELSE 0 END) AS gross_profit,
                       SUM(CASE WHEN realized_pnl < 0 THEN realized_pnl ELSE 0 END) AS gross_loss,
                       MAX(realized_pnl)                        AS best_trade,
                       MIN(realized_pnl)                        AS worst_trade,
                       AVG(realized_pnl)                        AS avg_pnl,
                       MIN(timestamp_open)                      AS first_trade,
                       MAX(timestamp_close)                     AS last_close
                  FROM trades WHERE status='closed' AND module=?
            """, (mod,)).fetchone()

            open_n = c.execute(
                "SELECT COUNT(*) FROM trades WHERE status='open' AND module=?", (mod,)
            ).fetchone()[0]

            n          = closed["n"] or 0
            total_pnl  = closed["total_pnl"] or 0.0
            wins       = closed["wins"]       or 0
            losses     = closed["losses"]     or 0
            gp         = closed["gross_profit"] or 0.0
            gl         = abs(closed["gross_loss"] or 0.0)
            result[mod] = {
                "module":         mod,
                "closed_trades":  n,
                "open_trades":    open_n,
                "realized_pnl":   round(total_pnl, 2),
                "wins":           wins,
                "losses":         losses,
                "win_rate":       round(wins / n * 100, 1) if n else 0.0,
                "profit_factor":  round(gp / gl, 2) if gl > 0 else None,
                "best_trade":     round(closed["best_trade"] or 0, 2),
                "worst_trade":    round(closed["worst_trade"] or 0, 2),
                "avg_pnl":        round(closed["avg_pnl"] or 0, 2),
                "first_trade":    (closed["first_trade"] or "")[:10],
                "last_close":     (closed["last_close"]  or "")[:10],
            }

    # Grand total (USD-denominated modules only for clean aggregation)
    usd_modules = [m for m in modules if m != "stock"]
    total_pnl   = sum(result[m]["realized_pnl"] for m in usd_modules if m in result)
    total_trades = sum(result[m]["closed_trades"] for m in modules if m in result)
    total_wins   = sum(result[m]["wins"]          for m in modules if m in result)

    result["total"] = {
        "module":        "TOTAL",
        "closed_trades": total_trades,
        "realized_pnl":  round(total_pnl, 2),
        "wins":          total_wins,
        "win_rate":      round(total_wins / total_trades * 100, 1) if total_trades else 0.0,
    }
    return result


# ── Formatted report ────────────────────────────────────────────────

def print_statement(module: str = None):
    """Print a formatted P&L statement to stdout."""
    GR = "\033[92m"; RD = "\033[91m"; YL = "\033[93m"
    CY = "\033[96m"; W  = "\033[0m";  BD = "\033[1m"; DM = "\033[2m"
    HR = "─" * 80

    summary = get_summary(module)

    print(f"\n{BD}{CY}{'═'*80}{W}")
    title = f"P&L STATEMENT — {module.upper() if module else 'ALL MODULES'}"
    print(f"{BD}{CY}  {title:^76}{W}")
    print(f"{BD}{CY}  {'Generated: ' + datetime.now().strftime('%Y-%m-%d %H:%M:%S'):^76}{W}")
    print(f"{BD}{CY}{'═'*80}{W}\n")

    MODULE_LABEL = {
        "stock":   ("STOCKS",    "US Blend momentum strategy"),
        "etf":     ("ETF",       "Sector rotation (SL 8% / TP 20%)"),
        "futures": ("FUTURES",   "Donchian breakout — ES/NQ/GC/CL/ZB"),
        "forex":   ("FOREX",     "EMA trend · RSI pullback · Donchian — 7 FX pairs"),
    }

    modules_to_show = [module] if module else list(MODULES)
    for mod in modules_to_show:
        s     = summary.get(mod, {})
        label, desc = MODULE_LABEL.get(mod, (mod.upper(), ""))
        n     = s.get("closed_trades", 0)
        pnl   = s.get("realized_pnl", 0.0)
        wr    = s.get("win_rate", 0.0)
        pf    = s.get("profit_factor")
        cur   = "SEK" if mod == "stock" else "USD"
        pc    = GR if pnl >= 0 else RD
        sign  = "+" if pnl >= 0 else ""

        print(f"  {BD}{label:<10}{W}  {DM}{desc}{W}")
        print(f"  {HR}")
        print(f"  Closed trades : {BD}{n}{W}       Open : {BD}{s.get('open_trades',0)}{W}")
        print(f"  Win rate      : {BD}{wr:.1f}%{W}        Profit factor : {BD}{pf if pf else '—'}{W}")
        print(f"  Best trade    : {GR}{BD}{s.get('best_trade',0):+.2f} {cur}{W}    "
              f"Worst trade : {RD}{BD}{s.get('worst_trade',0):+.2f} {cur}{W}")
        print(f"  Avg per trade : {BD}{s.get('avg_pnl',0):+.2f} {cur}{W}")
        print(f"  {BD}REALIZED P&L  : {pc}{sign}{pnl:,.2f} {cur}{W}")
        print(f"  {HR}\n")

    if not module:
        tot = summary.get("total", {})
        tc  = GR if tot.get("realized_pnl", 0) >= 0 else RD
        ts  = "+" if tot.get("realized_pnl", 0) >= 0 else ""
        print(f"  {BD}{'─'*78}{W}")
        print(f"  {BD}GRAND TOTAL (ETF+Futures+Forex USD){W}   "
              f"{tc}{BD}{ts}{tot.get('realized_pnl',0):,.2f} USD{W}   "
              f"  Trades: {tot.get('closed_trades',0)}   "
              f"WR: {tot.get('win_rate',0):.1f}%")
        print(f"  {BD}{'─'*78}{W}\n")


# ── CLI ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="P&L ledger manager")
    ap.add_argument("--sync",      action="store_true", help="Sync from all state files")
    ap.add_argument("--statement", action="store_true", help="Print P&L statement")
    ap.add_argument("--module",    choices=list(MODULES), help="Filter to one module")
    ap.add_argument("--open",      action="store_true", help="Show open positions")
    ap.add_argument("--closed",    action="store_true", help="Show last 20 closed trades")
    args = ap.parse_args()

    if args.sync:
        counts = sync_all()
        print("Synced records:", counts)

    if args.statement:
        print_statement(args.module)

    if args.open:
        rows = get_open_positions(args.module)
        print(f"\nOpen positions ({len(rows)}):")
        for r in rows:
            print(f"  {r['module']:<8} {r['strategy']:<12} {r['symbol']:<8} "
                  f"{r['direction']:<5} qty={r['quantity']:>10,}  "
                  f"entry={r['entry_price']}")

    if args.closed:
        rows = get_closed_trades(args.module, limit=20)
        print(f"\nLast closed trades ({len(rows)}):")
        for r in rows:
            sign = "+" if (r["realized_pnl"] or 0) >= 0 else ""
            cur  = r["currency"] or "USD"
            print(f"  {r['module']:<8} {r['strategy']:<12} {r['symbol']:<8} "
                  f"{r['direction']:<5} P&L: {sign}{r['realized_pnl'] or 0:>10.2f} {cur}  "
                  f"[{r['exit_reason'] or '—'}]  {(r['timestamp_close'] or '')[:10]}")

    if not any([args.sync, args.statement, args.open, args.closed]):
        ap.print_help()
