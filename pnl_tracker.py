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
    commission       REAL    DEFAULT 0,
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

# Per-module commission rates applied at open AND close.
# ContractFutures: $2.00/contract/side (Saxo typical for CL/ZB).
# ETF: 0.10% of trade value per side (Saxo ETF, min $5).
# Forex/CfdOnIndex/FxSpot: spread-embedded, no explicit commission line.
COMMISSION_FUTURES_PER_CONTRACT = 2.00   # USD per contract per side
COMMISSION_ETF_PCT              = 0.001  # 0.10% per side
COMMISSION_ETF_MIN              = 5.00   # USD minimum per trade


def calc_commission(module: str, quantity: float, price: float,
                    asset_type: str = "") -> float:
    """Return one-side commission in the trade's currency (USD)."""
    if module == "futures" and asset_type == "ContractFutures":
        return round(quantity * COMMISSION_FUTURES_PER_CONTRACT, 2)
    if module == "etf":
        return round(max(COMMISSION_ETF_MIN, quantity * price * COMMISSION_ETF_PCT), 2)
    return 0.0   # forex spread-embedded; stock handled by atos.risk


# ── Connection ──────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    # One-time migration: add commission column to existing databases
    try:
        c.execute("ALTER TABLE trades ADD COLUMN commission REAL DEFAULT 0")
        c.commit()
    except sqlite3.OperationalError:
        pass  # column already exists
    return c


# ── Write API ───────────────────────────────────────────────────────

def log_open(module: str, strategy: str, symbol: str, direction: str,
             quantity: float, entry_price: float, stop_price: float = 0,
             currency: str = "USD", order_id: str = None,
             timestamp: str = None, source_ref: str = None,
             commission: float = 0.0, asset_type: str = "") -> int:
    """Record a new trade opening. Returns the new row id.

    commission: one-side entry commission in trade currency.
                Pass 0 to auto-calculate via calc_commission(), or supply
                explicitly. If both are 0, calc_commission() is called.
    """
    if commission == 0.0:
        commission = calc_commission(module, quantity, entry_price, asset_type)
    ts = timestamp or datetime.now().isoformat()
    with _conn() as c:
        cur = c.execute("""
            INSERT INTO trades
                (module, strategy, symbol, direction, quantity, entry_price,
                 stop_price, commission, currency, order_id, status,
                 timestamp_open, source_ref)
            VALUES (?,?,?,?,?,?,?,?,?,?,'open',?,?)
        """, (module, strategy, symbol, direction, quantity, entry_price,
              stop_price, commission, currency, order_id, ts, source_ref))
        return cur.lastrowid


def log_close(module: str, symbol: str, exit_price: float,
              exit_reason: str = "", strategy: str = None,
              timestamp: str = None, order_id: str = None,
              commission: float = 0.0, asset_type: str = "",
              fx_rate_to_base: float = 1.0,
              gross_pnl_base_override: float | None = None,
              contract_size: float = 1.0) -> float | None:
    """Close the most-recent open trade for module+symbol.

    fx_rate_to_base: multiplier converting the raw (entry/exit price currency)
    P&L into the ledger's base currency before storing. Defaults to 1.0 (no
    conversion) for modules already denominated in the base currency. Forex
    trades quote in the PAIR's quote currency (e.g. JPY, NOK, CHF) — pass the
    quote-currency -> base-currency rate here or every pair's raw P&L gets
    summed together as if they were all the same currency (they were not: a
    JPY pair's raw P&L number is ~150x its true base-currency value).

    contract_size: multiplier for exchange-traded futures (ContractFutures)
    where 1 point of price movement is worth more than $1/unit -- e.g. ZC
    (corn) is $50/point/contract. Found missing entirely 2026-08-25 while
    investigating a real ZC stop-loss: the raw*qty calc without this
    understated a real -$475 loss as -$9.50. Defaults to 1.0 (correct for
    FxSpot/CfdOnIndex-style instruments already quoted at $1/point per unit,
    and for any close using gross_pnl_base_override instead, which already
    encodes the real broker-dealt amount and ignores this parameter
    entirely). Callers for ContractFutures should look this up the same way
    futures/runner.py's entries already do (uic cache's contract_size).

    gross_pnl_base_override: use this exact base-currency gross P&L instead of
    computing raw*qty*contract_size*fx_rate_to_base — pass Saxo's own
    PositionView.ProfitLossOnTradeInBaseCurrency here when available. That's
    the broker's own real dealt conversion, which is what actually happens to
    the account balance — more authoritative than any rate we look up
    ourselves. fx_rate_to_base/contract_size are the fallback when this isn't
    available (e.g. the position lookup failed).

    Returns net realized P&L (after entry + exit commission), in base
    currency, or None if not found.
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
        ep        = row["entry_price"]
        qty       = row["quantity"]
        direction = row["direction"]
        if gross_pnl_base_override is not None:
            gross_pnl = gross_pnl_base_override
        else:
            raw       = (exit_price - ep) if direction in ("Buy", "BUY") else (ep - exit_price)
            gross_pnl = raw * qty * contract_size * fx_rate_to_base
        if commission == 0.0:
            commission = calc_commission(module, qty, exit_price, asset_type)
        entry_comm = row["commission"] or 0.0
        total_comm = entry_comm + commission
        net_pnl    = gross_pnl - total_comm
        c.execute("""
            UPDATE trades
               SET exit_price=?, realized_pnl=?, commission=?,
                   exit_reason=?, status='closed', timestamp_close=?
             WHERE id=?
        """, (exit_price, net_pnl, total_comm, exit_reason, ts, row["id"]))
        return net_pnl


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
                    # SQLite's UPDATE does not support ORDER BY/LIMIT (that
                    # requires a non-default compile flag Python's sqlite3
                    # doesn't have) -- the old query here was a silent no-op
                    # syntax error that only surfaced the first time a real
                    # ETF sell ever ran through this path. Find the target
                    # row's id first, then UPDATE WHERE id=<that row>.
                    row = c.execute("""
                        SELECT id, quantity FROM trades
                         WHERE module='etf' AND symbol=? AND status='open'
                         ORDER BY id DESC LIMIT 1
                    """, (sym,)).fetchone()
                    if row is None:
                        if ep:
                            c.execute("""
                                INSERT INTO trades
                                    (module, strategy, symbol, direction, quantity,
                                     entry_price, exit_price, realized_pnl, currency,
                                     exit_reason, status, timestamp_open, timestamp_close, source_ref)
                                VALUES ('etf','ETF Rotation',?,'Buy',?,?,?,?,
                                        'USD',?,'closed',?,?,?)
                            """, (sym, qty, ep, xp, pnl, reason,
                                  entry_map.get(sym, {}).get("timestamp", ""), ts, ref))
                    elif qty >= row["quantity"]:
                        # Selling the full remaining quantity -- close the row.
                        c.execute("""
                            UPDATE trades SET exit_price=?, realized_pnl=?,
                                exit_reason=?, status='closed', timestamp_close=?
                             WHERE id=?
                        """, (xp, pnl, reason, ts, row["id"]))
                    else:
                        # Partial sell: this used to unconditionally mark the
                        # WHOLE position closed off a partial-quantity P&L,
                        # silently dropping the remaining shares from the
                        # ledger entirely. Reduce the open row's quantity
                        # instead, and record the sold portion as its own
                        # closed row -- same pattern as pnl_tracker's forex
                        # partial-close handling.
                        c.execute("UPDATE trades SET quantity=? WHERE id=?",
                                 (row["quantity"] - qty, row["id"]))
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
    """Sync open futures positions from futures_state.json.

    futures/runner.py already logs every open directly and in real time via
    log_open() (source_ref=None) the moment an order is placed. This sync
    exists as a catch-up net, same as sync_forex_from_json() below -- and
    had the EXACT SAME bug that one was fixed for on 2026-08-21: it only
    deduped against its OWN previous sync ref ("futures:open:{key}"), never
    against the real-time-logged row for the same position. Since that row's
    source_ref is None (never equal to the sync ref), every open futures
    position got silently double-inserted each time --sync ran. Found
    2026-08-25 investigating why a real ZC stop-loss the intraday monitor
    correctly closed didn't show up cleanly -- two open 'ZC' rows existed
    (id 290 from the real-time entry log, id 320 from this sync bug),
    log_close()'s ORDER BY id DESC LIMIT 1 would only ever close the newer
    (sync-duplicate) one, leaving the original stuck open forever. Fixed the
    same way forex's was: skip if ANY open row already exists for this
    strategy+symbol, not just a prior sync-created one.
    """
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
                existing = c.execute(
                    "SELECT 1 FROM trades WHERE module='futures' AND strategy=? "
                    "AND symbol=? AND status='open' LIMIT 1",
                    (strat, sym),
                ).fetchone()
            if existing:
                continue
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
    """Sync open forex positions from forex_state.json.

    forex/runner.py already logs every open directly and in real time via
    log_open() (source_ref=None) the moment an order is placed. This sync
    exists as a catch-up net (e.g. after a gap in the runner's own logging),
    but it used to only dedupe against its OWN previous sync ref
    ("forex:open:{key}") — never against the real-time-logged row for the
    same position. Since the sync ref never matches the real-time row's
    (None) source_ref, EVERY open position sitting in forex_state.json got
    silently double-inserted every time --sync ran, double-counting exposure/
    notional in any aggregate. Found 2026-08-21 via duplicate rows for
    donchian:CNHHKD, ema:EURSGD, ema:GBPUSD, ema:USDJPY, rsi:EURAUD after
    running --sync to backfill a separate (unrelated) stock data gap. Fixed:
    skip if ANY open row already exists for this strategy+symbol, not just
    a prior sync-created one.
    """
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
                existing = c.execute(
                    "SELECT 1 FROM trades WHERE module='forex' AND strategy=? "
                    "AND symbol=? AND status='open' LIMIT 1",
                    (strat, sym),
                ).fetchone()
            if existing:
                continue
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
            # Same class of bug as the open-position dedup above: forex/runner.py
            # already logs every close directly and in real time via log_close()
            # (which uses Saxo's own ProfitLossOnTradeInBaseCurrency — the
            # authoritative converted P&L). That direct UPDATE never sets
            # source_ref, so it can never match this ref and this catch-up path
            # would silently insert a SECOND, duplicate row for the same close —
            # AND with a wrong P&L, since raw*qty here is in the pair's quote
            # currency, not the ledger's base currency (the exact currency-
            # mixing bug fixed elsewhere in this file). Guard against both:
            # skip if a closed row for this strategy+symbol+entry+qty already
            # exists, and tag the currency honestly as the pair's quote
            # currency (not 'USD') since no base-currency conversion happens
            # on this fallback path.
            with _conn() as c:
                existing = c.execute(
                    "SELECT 1 FROM trades WHERE module='forex' AND strategy=? "
                    "AND symbol=? AND status='closed' AND entry_price=? AND quantity=? "
                    "LIMIT 1",
                    (strat, sym, ep, qty),
                ).fetchone()
            if existing:
                continue
            raw   = (xp - ep) if direc == "Buy" else (ep - xp)
            pnl   = raw * qty
            quote_ccy = sym[-3:] if len(sym) >= 6 else "USD"
            with _conn() as c:
                c.execute("""
                    INSERT INTO trades
                        (module, strategy, symbol, direction, quantity,
                         entry_price, exit_price, realized_pnl, currency,
                         exit_reason, status, timestamp_open, timestamp_close, source_ref)
                    VALUES ('forex',?,?,?,?,?,?,?,?,?,'closed',?,?,?)
                """, (strat, sym, direc, qty, ep, xp, pnl, quote_ccy,
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
    # Secondary sort key (id) makes this deterministic when multiple rows
    # share the same timestamp_close -- confirmed this actually happens
    # (stock trades synced with a date-only exit_date collide by the dozen
    # on a single rebalance day; even real-time forex closes can share a
    # microsecond-precision timestamp within one script run). Without it,
    # SQLite's tie-break order for "ORDER BY timestamp_close DESC" alone is
    # not guaranteed stable across calls -- strategy_learner.py relies on
    # this ordering being stable/monotonic for its incremental
    # num_processed cursor, so an unstable tie-break could silently
    # reprocess or skip trades.
    q += " ORDER BY timestamp_close DESC, id DESC"
    if limit:
        q += f" LIMIT {int(limit)}"
    with _conn() as c:
        return [dict(r) for r in c.execute(q, args).fetchall()]


def get_strategy_summary(module: str = "forex", symbols: set | None = None) -> list[dict]:
    """
    Per-strategy P&L breakdown within a module.
    Returns list of dicts sorted by total_pnl descending.
    Only includes strategies with at least one closed trade.

    `symbols`, if given, restricts to trades on those symbols only -- e.g.
    forex_dashboard.py passes forex.universe.CORE_SYMBOLS / the exotic
    complement to compare the two universe tiers' track records side by
    side (which is the actual live-vs-SIM-only decision that split exists
    to inform).
    """
    sym_filter = ""
    params: tuple = (module,)
    if symbols:
        sym_filter = f" AND symbol IN ({','.join('?' * len(symbols))})"
        params = (module, *symbols)

    with _conn() as c:
        # WHERE must filter to status='closed' -- open positions have
        # realized_pnl=NULL, which SUM()/CASE-WHEN silently skip for the
        # P&L/wins/losses columns, but COUNT(*) still counted them into n,
        # diluting win_rate for any strategy holding open positions
        # (confirmed live 2026-08-22: donchian showed 14.3% WR from 2
        # wins/14 total rows, when its real closed-trade WR was 2/2=100% --
        # 12 of those 14 rows were open, not losses). Strategies with ONLY
        # open positions (0 closed) used to show a misleading "0% WR /
        # $0 P&L" instead of correctly having no row at all.
        rows = c.execute(f"""
            SELECT strategy,
                   COUNT(*)                                                     AS n,
                   SUM(realized_pnl)                                            AS total_pnl,
                   SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END)           AS wins,
                   SUM(CASE WHEN realized_pnl < 0 THEN 1 ELSE 0 END)           AS losses,
                   SUM(CASE WHEN realized_pnl > 0 THEN realized_pnl ELSE 0 END) AS gross_profit,
                   SUM(CASE WHEN realized_pnl < 0 THEN realized_pnl ELSE 0 END) AS gross_loss,
                   MAX(realized_pnl)                                            AS best,
                   MIN(realized_pnl)                                            AS worst
              FROM trades
             WHERE module=? AND status='closed'{sym_filter}
             GROUP BY strategy
             ORDER BY total_pnl DESC
        """, params).fetchall()
        # Open count needs its own query -- computing it from the same
        # closed-only rowset (the previous approach, shared with the WHERE
        # above) can only ever return 0, since status='open' rows were
        # already excluded before the CASE WHEN could match them. Confirmed
        # this exact zero-every-time bug in get_pair_summary below too.
        open_counts = {row["strategy"]: row["n"] for row in c.execute(f"""
            SELECT strategy, COUNT(*) AS n FROM trades
             WHERE module=? AND status='open'{sym_filter} GROUP BY strategy
        """, params).fetchall()}
        # 2026-08-28: which symbol(s) each strategy actually traded --
        # futures_dashboard's STRATEGY BREAKDOWN showed strategy-level P&L
        # with no indication of which market it came from (e.g. "MACD 2
        # 0W/0L") -- same DISTINCT-symbol pattern already used by
        # get_strategy_summary_since() below, just also computed for the
        # all-time view.
        symbols_by_strategy: dict = {}
        for row in c.execute(f"""
            SELECT DISTINCT strategy, symbol FROM trades
             WHERE module=? AND status='closed'{sym_filter}
        """, params).fetchall():
            symbols_by_strategy.setdefault(row["strategy"], []).append(row["symbol"])

    result = []
    for r in rows:
        n  = r["n"] or 0
        gp = r["gross_profit"] or 0.0
        gl = abs(r["gross_loss"] or 0.0)
        strat = r["strategy"] or "—"
        result.append({
            "strategy":      strat,
            "trades":        n,
            "wins":          r["wins"]   or 0,
            "losses":        r["losses"] or 0,
            "open":          open_counts.get(strat, 0),
            "win_rate":      round((r["wins"] or 0) / n * 100, 1) if n else 0.0,
            "total_pnl":     round(r["total_pnl"] or 0.0, 2),
            "profit_factor": round(gp / gl, 2) if gl > 0 else None,
            "gross_profit":  round(gp, 2),
            "gross_loss":    round(gl, 2),
            "best":          round(r["best"]  or 0.0, 2),
            "worst":         round(r["worst"] or 0.0, 2),
            "symbols":       sorted(symbols_by_strategy.get(strat, [])),
        })
    return result


def get_strategy_summary_since(module: str, since: str, symbols: set | None = None) -> list[dict]:
    """Same shape as get_strategy_summary(), scoped to trades closed on or
    after `since` (e.g. today's date, "YYYY-MM-DD") -- for a daily digest
    rather than the all-time picture. Also returns the distinct symbols
    each strategy traded in that window (daily_summary.py's "currencies"
    column) since that's naturally computed alongside the rest here.

    `symbols`, if given, restricts to trades on those symbols only -- see
    get_strategy_summary()'s docstring for why (core/exotic tier split)."""
    sym_filter = ""
    params: tuple = (module, since)
    if symbols:
        sym_filter = f" AND symbol IN ({','.join('?' * len(symbols))})"
        params = (module, since, *symbols)

    with _conn() as c:
        rows = c.execute(f"""
            SELECT strategy,
                   COUNT(*)                                                     AS n,
                   SUM(realized_pnl)                                            AS total_pnl,
                   SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END)           AS wins,
                   SUM(CASE WHEN realized_pnl < 0 THEN 1 ELSE 0 END)           AS losses,
                   SUM(CASE WHEN realized_pnl > 0 THEN realized_pnl ELSE 0 END) AS gross_profit,
                   SUM(CASE WHEN realized_pnl < 0 THEN realized_pnl ELSE 0 END) AS gross_loss,
                   MAX(realized_pnl)                                            AS best,
                   MIN(realized_pnl)                                            AS worst,
                   SUM(commission)                                              AS total_costs
              FROM trades
             WHERE module=? AND status='closed' AND timestamp_close >= ?{sym_filter}
             GROUP BY strategy
             ORDER BY total_pnl DESC
        """, params).fetchall()
        open_counts = {row["strategy"]: row["n"] for row in c.execute(f"""
            SELECT strategy, COUNT(*) AS n FROM trades
             WHERE module=? AND status='open'{sym_filter} GROUP BY strategy
        """, (module, *symbols) if symbols else (module,)).fetchall()}
        symbols_by_strategy: dict = {}
        for row in c.execute(f"""
            SELECT DISTINCT strategy, symbol FROM trades
             WHERE module=? AND status='closed' AND timestamp_close >= ?{sym_filter}
        """, params).fetchall():
            symbols_by_strategy.setdefault(row["strategy"], []).append(row["symbol"])

    result = []
    for r in rows:
        n  = r["n"] or 0
        gp = r["gross_profit"] or 0.0
        gl = abs(r["gross_loss"] or 0.0)
        strat = r["strategy"] or "—"
        result.append({
            "strategy":      strat,
            "trades":        n,
            "wins":          r["wins"]   or 0,
            "losses":        r["losses"] or 0,
            "open":          open_counts.get(strat, 0),
            "win_rate":      round((r["wins"] or 0) / n * 100, 1) if n else 0.0,
            "total_pnl":     round(r["total_pnl"] or 0.0, 2),
            "profit_factor": round(gp / gl, 2) if gl > 0 else None,
            "best":          round(r["best"]  or 0.0, 2),
            "worst":         round(r["worst"] or 0.0, 2),
            "total_costs":   round(r["total_costs"] or 0.0, 2),
            "symbols":       sorted(symbols_by_strategy.get(strat, [])),
        })
    return result


def get_strategy_symbol_summary(module: str, since: str | None = None) -> list[dict]:
    """
    Per (strategy, symbol) P&L breakdown within a module -- one row per
    combination that has at least one closed trade, sorted by strategy
    then total_pnl descending. Added 2026-08-28: futures_dashboard's
    STRATEGY BREAKDOWN's new "Markets" column (get_strategy_summary's
    "symbols" field) only lists WHICH symbols a strategy traded, with no
    per-symbol stats -- e.g. "DONCHIAN ... GC, NQ, ZC" gives no way to
    tell that GC alone was the big winner and the others were flat/
    losing. This gives the real per-symbol breakdown the "Markets"
    column couldn't.

    `since`, if given (a "YYYY-MM-DD" date string), restricts to trades
    closed on or after that date -- for a "Today" column, same as
    get_strategy_summary_since().
    """
    since_filter = ""
    params: tuple = (module,)
    if since:
        since_filter = " AND timestamp_close >= ?"
        params = (module, since)

    with _conn() as c:
        rows = c.execute(f"""
            SELECT strategy, symbol,
                   COUNT(*)                                                     AS n,
                   SUM(realized_pnl)                                            AS total_pnl,
                   SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END)           AS wins,
                   SUM(CASE WHEN realized_pnl < 0 THEN 1 ELSE 0 END)           AS losses,
                   SUM(CASE WHEN realized_pnl > 0 THEN realized_pnl ELSE 0 END) AS gross_profit,
                   SUM(CASE WHEN realized_pnl < 0 THEN realized_pnl ELSE 0 END) AS gross_loss,
                   SUM(CASE WHEN realized_pnl IS NULL THEN 1 ELSE 0 END)       AS unresolved
              FROM trades
             WHERE module=? AND status='closed'{since_filter}
             GROUP BY strategy, symbol
             ORDER BY strategy, total_pnl DESC
        """, params).fetchall()

    result = []
    for r in rows:
        n  = r["n"] or 0
        gp = r["gross_profit"] or 0.0
        gl = abs(r["gross_loss"] or 0.0)
        unresolved = r["unresolved"] or 0
        # A closed trade can have realized_pnl=NULL (documented broker-side
        # ambiguity -- see pnl_tracker's own "MACD's 2 historical non-trades
        # remain honestly recorded" test). SQL SUM()/CASE-WHEN silently
        # treat NULL as "contributes 0" -- without this flag a fully-
        # unresolved (strategy,symbol) combo would render as a real "+0.00"
        # trade instead of "we don't actually know its P&L", which is a
        # meaningfully different (and misleading) claim.
        all_unresolved = n > 0 and unresolved == n
        result.append({
            "strategy":      r["strategy"] or "—",
            "symbol":        r["symbol"] or "—",
            "trades":        n,
            "wins":          r["wins"]   or 0,
            "losses":        r["losses"] or 0,
            "win_rate":      round((r["wins"] or 0) / n * 100, 1) if n else 0.0,
            "total_pnl":     None if all_unresolved else round(r["total_pnl"] or 0.0, 2),
            "profit_factor": None if all_unresolved else (round(gp / gl, 2) if gl > 0 else None),
            "unresolved":    all_unresolved,
        })
    return result


def get_pair_summary(module: str = "forex") -> list[dict]:
    """
    Per-symbol (currency pair / ticker) P&L breakdown within a module.
    Returns list of dicts sorted by total_pnl descending.
    Only includes symbols with at least one closed trade.
    """
    with _conn() as c:
        rows = c.execute("""
            SELECT symbol,
                   COUNT(*)                                                     AS n,
                   SUM(realized_pnl)                                            AS total_pnl,
                   SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END)           AS wins,
                   SUM(CASE WHEN realized_pnl < 0 THEN 1 ELSE 0 END)           AS losses,
                   SUM(CASE WHEN realized_pnl > 0 THEN realized_pnl ELSE 0 END) AS gross_profit,
                   SUM(CASE WHEN realized_pnl < 0 THEN realized_pnl ELSE 0 END) AS gross_loss,
                   MAX(realized_pnl)                                            AS best,
                   MIN(realized_pnl)                                            AS worst
              FROM trades
             WHERE module=? AND status='closed'
             GROUP BY symbol
             ORDER BY total_pnl DESC
        """, (module,)).fetchall()
        # Same fix as get_strategy_summary above: open_count can only ever
        # be 0 when computed from a rowset the WHERE clause already
        # filtered to status='closed' -- confirmed live, every pair showed
        # open=0 regardless of real open positions. Needs its own query.
        open_counts = {row["symbol"]: row["n"] for row in c.execute("""
            SELECT symbol, COUNT(*) AS n FROM trades
             WHERE module=? AND status='open' GROUP BY symbol
        """, (module,)).fetchall()}

    result = []
    for r in rows:
        n  = r["n"] or 0
        gp = r["gross_profit"] or 0.0
        gl = abs(r["gross_loss"] or 0.0)
        result.append({
            "symbol":        r["symbol"] or "—",
            "trades":        n,
            "wins":          r["wins"]   or 0,
            "losses":        r["losses"] or 0,
            "open":          open_counts.get(r["symbol"], 0),
            "win_rate":      round((r["wins"] or 0) / n * 100, 1) if n else 0.0,
            "total_pnl":     round(r["total_pnl"] or 0.0, 2),
            "profit_factor": round(gp / gl, 2) if gl > 0 else None,
            "best":          round(r["best"]  or 0.0, 2),
            "worst":         round(r["worst"] or 0.0, 2),
        })
    return result


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
                       SUM(commission)                          AS total_commission,
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
            total_comm = closed["total_commission"] or 0.0
            wins       = closed["wins"]       or 0
            losses     = closed["losses"]     or 0
            gp         = closed["gross_profit"] or 0.0
            gl         = abs(closed["gross_loss"] or 0.0)
            result[mod] = {
                "module":           mod,
                "closed_trades":    n,
                "open_trades":      open_n,
                "realized_pnl":     round(total_pnl, 2),
                "total_commission": round(total_comm, 2),
                "wins":             wins,
                "losses":           losses,
                "win_rate":         round(wins / n * 100, 1) if n else 0.0,
                "profit_factor":    round(gp / gl, 2) if gl > 0 else None,
                "best_trade":       round(closed["best_trade"] or 0, 2),
                "worst_trade":      round(closed["worst_trade"] or 0, 2),
                "avg_pnl":          round(closed["avg_pnl"] or 0, 2),
                "first_trade":      (closed["first_trade"] or "")[:10],
                "last_close":       (closed["last_close"]  or "")[:10],
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
        cur   = "SEK" if mod == "stock" else "EUR" if mod == "forex" else "USD"
        pc    = GR if pnl >= 0 else RD
        sign  = "+" if pnl >= 0 else ""

        print(f"  {BD}{label:<10}{W}  {DM}{desc}{W}")
        print(f"  {HR}")
        comm = s.get("total_commission", 0.0)
        print(f"  Closed trades : {BD}{n}{W}       Open : {BD}{s.get('open_trades',0)}{W}")
        print(f"  Win rate      : {BD}{wr:.1f}%{W}        Profit factor : {BD}{pf if pf else '—'}{W}")
        print(f"  Best trade    : {GR}{BD}{s.get('best_trade',0):+.2f} {cur}{W}    "
              f"Worst trade : {RD}{BD}{s.get('worst_trade',0):+.2f} {cur}{W}")
        print(f"  Avg per trade : {BD}{s.get('avg_pnl',0):+.2f} {cur}{W}    "
              f"Commission : {RD}{BD}-{comm:,.2f} {cur}{W}")
        print(f"  {BD}REALIZED P&L  : {pc}{sign}{pnl:,.2f} {cur}{W}  {DM}(net of commission){W}")
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
    ap.add_argument("--pairs",     action="store_true", help="Show per-symbol P&L breakdown")
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

    if args.pairs:
        for mod in ([args.module] if args.module else list(MODULES)):
            rows = get_pair_summary(mod)
            if not rows:
                continue
            print(f"\n{mod.upper()} — P&L by pair ({len(rows)} pairs):")
            for r in rows:
                sign = "+" if r["total_pnl"] >= 0 else ""
                pf   = r["profit_factor"] if r["profit_factor"] is not None else "—"
                print(f"  {r['symbol']:<8} trades={r['trades']:>3}  "
                      f"W/L={r['wins']}/{r['losses']}  win_rate={r['win_rate']:>5.1f}%  "
                      f"PF={pf}  best={r['best']:+.2f}  worst={r['worst']:+.2f}  "
                      f"total: {sign}{r['total_pnl']:>10.2f}")

    if not any([args.sync, args.statement, args.open, args.closed, args.pairs]):
        ap.print_help()
