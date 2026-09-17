"""
run_ibkr_momentum_check.py
--------------------------
Twice-weekly momentum check for IBKR blend positions.

Modes
-----
--live        Semi-manual: score held LIVE positions, print report, no trades.
              User decides whether to sell manually in IBKR.

--paper-auto  Automated: score held PAPER positions.
              Exits positions with score < EXIT_SCORE_THRESH or rank > EXIT_RANK_THRESH
              (pure momentum — P&L at exit is irrelevant for the exit decision).
              Buys top-ranked replacements up to max_positions.
              Logs every decision to data/ibkr_momentum_tracking.db so we can
              measure whether pure-momentum exits beat holding to the stop.

Usage
-----
    python run_ibkr_momentum_check.py --live          # report only
    python run_ibkr_momentum_check.py --paper-auto    # execute on paper account

Scheduled
---------
    --live       : Mon + Thu 17:30 PKT  (09:30 ET, right after open)
    --paper-auto : Mon + Thu 17:35 PKT  (5 min after live report)

Logs       : data/ibkr_momentum_check.log
Tracking DB: data/ibkr_momentum_tracking.db
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)

# ── Thresholds ────────────────────────────────────────────────────────────────
EXIT_SCORE_THRESH = 55    # score below this → flag for exit
EXIT_RANK_THRESH  = 120   # rank worse than this → flag for exit

LOG_FILE      = os.path.join(_ROOT, "data", "ibkr_momentum_check.log")
TRACKING_DB   = os.path.join(_ROOT, "data", "ibkr_momentum_tracking.db")

_TRACKING_SCHEMA = """
CREATE TABLE IF NOT EXISTS momentum_exits (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    exit_date     TEXT    NOT NULL,
    mode          TEXT    NOT NULL,     -- 'LIVE_REPORT' | 'PAPER_AUTO'
    symbol        TEXT    NOT NULL,
    rank_at_exit  INTEGER,
    score_at_exit REAL,
    entry_price   REAL,
    exit_price    REAL,                 -- NULL for live (report-only)
    pnl_usd       REAL,                 -- NULL for live
    pnl_pct       REAL,                 -- NULL for live
    days_held     INTEGER,
    replaced_by   TEXT,                 -- NULL for live; comma-separated for paper
    action        TEXT    NOT NULL      -- 'FLAGGED' | 'SOLD' | 'BOUGHT'
);
"""


# ── Logging ───────────────────────────────────────────────────────────────────
class _Tee:
    def __init__(self, *streams):
        self._s = streams

    def write(self, d):
        for s in self._s:
            try:
                s.write(d)
            except Exception:
                pass

    def flush(self):
        for s in self._s:
            try:
                s.flush()
            except Exception:
                pass


def _setup_logging():
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    log_f = open(LOG_FILE, "a", encoding="utf-8", errors="replace")
    tee = _Tee(sys.__stdout__, log_f)
    sys.stdout = tee
    sys.stderr = tee
    return log_f


# ── Tracking DB ───────────────────────────────────────────────────────────────
@contextmanager
def _tracking_conn():
    os.makedirs(os.path.dirname(TRACKING_DB), exist_ok=True)
    con = sqlite3.connect(TRACKING_DB)
    con.row_factory = sqlite3.Row
    try:
        con.execute(_TRACKING_SCHEMA)
        con.commit()
        yield con
        con.commit()
    finally:
        con.close()


def _record_exit(mode: str, symbol: str, rank: int, score: float,
                 entry_price: float, exit_price: float | None,
                 pnl_usd: float | None, pnl_pct: float | None,
                 days_held: int, replaced_by: str | None, action: str):
    now = datetime.now(timezone.utc).isoformat()
    with _tracking_conn() as con:
        con.execute(
            """INSERT INTO momentum_exits
               (exit_date, mode, symbol, rank_at_exit, score_at_exit,
                entry_price, exit_price, pnl_usd, pnl_pct,
                days_held, replaced_by, action)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (now, mode, symbol, rank, score, entry_price,
             exit_price, pnl_usd, pnl_pct, days_held, replaced_by, action),
        )


# ── Scorer helper ─────────────────────────────────────────────────────────────
def _run_scorer() -> dict[str, tuple[int, float]]:
    """Return {symbol: (rank, score)} for the full universe."""
    from ibkr_module.ibkr_scorer import run_scan
    result  = run_scan(verbose=False)
    df      = result["all_scored"]
    if df.empty:
        return {}
    df_sort = df.sort_values("trade_score", ascending=False).reset_index(drop=True)
    rank_map: dict[str, tuple[int, float]] = {}
    for i, row in df_sort.iterrows():
        sym = str(row.get("ticker") or row.get("symbol") or row.get("Ticker") or "")
        if sym:
            rank_map[sym] = (i + 1, float(row.get("trade_score", 0)))
    return rank_map


def _days_held(filled_at: str | None) -> int:
    if not filled_at:
        return 0
    try:
        dt = datetime.fromisoformat(filled_at.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).days
    except Exception:
        return 0


# ── Live mode: report only ────────────────────────────────────────────────────
def run_live_report():
    import ibkr_module.ibkr_state as st

    live_db = os.path.join(_ROOT, "data", "ibkr_live_stocks.db")
    os.environ["IBKR_DB_PATH"] = live_db

    held = st.get_open_positions(strategy="blend")
    if not held:
        print("\n  No open live blend positions — nothing to check.\n")
        return

    held_symbols = [p["symbol"] for p in held]
    print(f"\n  Held ({len(held)}): {', '.join(held_symbols)}")
    print("  Running scorer...")
    rank_map = _run_scorer()

    flagged = []
    print(f"\n  {'Symbol':<6}  {'Rank':>6}  {'Score':>6}  {'Entry':>8}  "
          f"{'Stop':>8}  {'P&L':>8}  {'Days':>5}  Status")
    print("  " + "-" * 76)

    for pos in held:
        sym   = pos["symbol"]
        entry = float(pos.get("fill_price") or pos.get("limit_price") or 0)
        stop  = float(pos.get("stop_price") or 0)
        qty   = int(pos.get("qty") or 0)
        days  = _days_held(pos.get("filled_at"))
        rank, score = rank_map.get(sym, (999, 0.0))
        is_flagged  = score < EXIT_SCORE_THRESH or rank > EXIT_RANK_THRESH

        # P&L shown as estimated (entry-based, no live price needed for report)
        status = "** EXIT **" if is_flagged else "OK"
        pnl_str = "—"

        print(f"  {sym:<6}  {rank:>6}  {score:>6.1f}  ${entry:>7.2f}  "
              f"${stop:>7.2f}  {pnl_str:>8}  {days:>5}d  {status}")

        if is_flagged:
            flagged.append((sym, rank, score, entry, stop, days))
            _record_exit("LIVE_REPORT", sym, rank, score, entry,
                         None, None, None, days, None, "FLAGGED")

    print("  " + "-" * 76)
    print()
    if flagged:
        print(f"  !! {len(flagged)} position(s) flagged for manual review:")
        for sym, rank, score, entry, stop, days in flagged:
            print(f"     {sym}: rank #{rank}, score {score:.1f}  "
                  f"(entry ${entry:.2f}, stop ${stop:.2f}, held {days}d)")
        print()
        print("  ACTION: sell manually in IBKR if you agree.")
        print("  Stops are in place as a safety net if you choose to hold.")
    else:
        print(f"  All {len(held)} position(s) are in momentum — no action needed.")


# ── Paper-auto mode: execute exits + buys ────────────────────────────────────
def run_paper_auto():
    import ibkr_module.ibkr_state as st
    import ibkr_module.ibkr_client as ic

    held = st.get_open_positions(strategy="blend")
    if not held:
        print("\n  No open paper blend positions — nothing to check.\n")
        return

    held_symbols = [p["symbol"] for p in held]
    print(f"\n  Held ({len(held)}): {', '.join(held_symbols)}")
    print("  Running scorer...")
    rank_map = _run_scorer()
    if not rank_map:
        print("  [ERROR] Scorer returned no data — aborting.")
        sys.exit(1)

    # Load config for paper blend settings
    cfg_path = os.path.join(_ROOT, "ibkr_module", "config", "ibkr_config.json")
    with open(cfg_path) as f:
        cfg = json.load(f)
    paper_account = cfg["paper_account_id"]
    blend_cfg     = cfg["strategies"]["blend"]
    budget_usd    = blend_cfg["budget_usd"]
    max_positions = blend_cfg["max_positions"]
    stop_pct      = blend_cfg.get("stop_pct", 0.08)
    min_trade_usd = blend_cfg.get("min_trade_usd", 50)

    # Connect to paper Gateway
    host = cfg.get("host", "127.0.0.1")
    port = cfg.get("port_paper", 4001)
    client_ids = cfg.get("client_ids", {})
    client_id  = client_ids.get("blend", cfg.get("client_id", 10))

    print(f"  Connecting to IB Gateway [PAPER] {host}:{port} clientId={client_id}...")
    ib = ic.connect(host, port, client_id)
    print(f"  Connected. Account: {paper_account}")

    # ── Identify flagged positions ─────────────────────────────────────────
    flagged  = []
    keep     = []
    for pos in held:
        sym  = pos["symbol"]
        rank, score = rank_map.get(sym, (999, 0.0))
        if score < EXIT_SCORE_THRESH or rank > EXIT_RANK_THRESH:
            flagged.append(pos)
        else:
            keep.append(sym)

    if not flagged:
        print(f"\n  All {len(held)} position(s) in momentum — no exits needed.")
        ic.disconnect(ib)
        return

    # ── Get prices for flagged positions ───────────────────────────────────
    flagged_syms = [p["symbol"] for p in flagged]
    print(f"\n  Flagged for exit ({len(flagged)}): {', '.join(flagged_syms)}")
    print("  Fetching current prices...")
    prices = ic.get_prices(ib, flagged_syms)

    # ── Execute exits ──────────────────────────────────────────────────────
    sold_symbols = []
    for pos in flagged:
        sym   = pos["symbol"]
        qty   = int(pos.get("qty") or 0)
        entry = float(pos.get("fill_price") or pos.get("limit_price") or 0)
        stop  = float(pos.get("stop_price") or 0)
        days  = _days_held(pos.get("filled_at"))
        rank, score = rank_map.get(sym, (999, 0.0))
        current_price = prices.get(sym, 0.0)

        if current_price <= 0:
            print(f"  [SKIP] {sym}: no price available — cannot sell safely.")
            continue
        if qty <= 0:
            print(f"  [SKIP] {sym}: qty={qty} — nothing to sell.")
            continue

        pnl_usd = round((current_price - entry) * qty, 2)
        pnl_pct = round((current_price - entry) / entry * 100, 2) if entry > 0 else 0.0
        pnl_sign = "+" if pnl_usd >= 0 else ""

        print(f"\n  SELL {qty} {sym} @ ~${current_price:.2f}  "
              f"P&L: {pnl_sign}${pnl_usd:.2f} ({pnl_sign}{pnl_pct:.2f}%)  "
              f"rank #{rank}  score {score:.1f}")

        trade = ic.place_market_order(ib, paper_account, sym, "SELL", qty)
        fill_price = ic.confirm_fill(ib, trade, timeout_s=120)

        if fill_price:
            actual_pnl = round((fill_price - entry) * qty, 2)
            actual_pct = round((fill_price - entry) / entry * 100, 2) if entry > 0 else 0.0
            print(f"    Filled @ ${fill_price:.2f}  actual P&L: ${actual_pnl:+.2f} ({actual_pct:+.2f}%)")

            # Cancel existing stop order if any
            stop_oid = pos.get("stop_order_id")
            if stop_oid:
                try:
                    open_orders = ib.openOrders()
                    for o in open_orders:
                        if str(o.orderId) == str(stop_oid):
                            ib.cancelOrder(o)
                            break
                except Exception:
                    pass

            # Mark as SOLD in state DB
            sell_oid = str(trade.order.orderId)
            st.record_order(sell_oid, sym, "SELL", qty, strategy="blend")
            st.mark_filled(sell_oid, fill_price, side="SELL")
            st.close_buy_position(sym, strategy="blend")

            _record_exit("PAPER_AUTO", sym, rank, score, entry,
                         fill_price, actual_pnl, actual_pct, days, None, "SOLD")
            sold_symbols.append(sym)
        else:
            print(f"    [WARN] {sym} fill timed out — position may still be open.")

    if not sold_symbols:
        print("\n  No exits completed. Skipping buys.")
        ic.disconnect(ib)
        return

    # ── Find replacements ──────────────────────────────────────────────────
    current_held = keep + [s for s in held_symbols if s not in flagged_syms]
    # Top ranked stocks not already held
    # Build ordered list from rank_map
    sorted_ranks = sorted(rank_map.items(), key=lambda x: x[1][0])
    candidates = [
        sym for sym, (rank, score) in sorted_ranks
        if sym not in current_held and score >= EXIT_SCORE_THRESH and rank <= EXIT_RANK_THRESH
    ]
    n_slots = len(sold_symbols)
    replacements = candidates[:n_slots]

    if not replacements:
        print("\n  No replacement candidates found above threshold.")
        ic.disconnect(ib)
        return

    print(f"\n  Replacements to buy: {', '.join(replacements)}")

    # Get prices for replacements
    rep_prices = ic.get_prices(ib, replacements)
    per_slot   = (budget_usd / max_positions) * (1 - blend_cfg.get("cash_buffer_pct", 0.05))

    bought_symbols = []
    for sym in replacements:
        price = rep_prices.get(sym, 0.0)
        if price <= 0:
            print(f"  [SKIP] {sym}: no price — cannot buy.")
            continue
        qty = int(per_slot // price)
        value = qty * price
        if qty <= 0 or value < min_trade_usd:
            print(f"  [SKIP] {sym}: ${value:.0f} below min_trade_usd=${min_trade_usd}.")
            continue

        stop_price = round(price * (1 - stop_pct), 2)
        print(f"\n  BUY {qty} {sym} @ ~${price:.2f}  value ~${value:.0f}  stop ${stop_price:.2f}")

        trade = ic.place_market_order(ib, paper_account, sym, "BUY", qty)
        fill_price = ic.confirm_fill(ib, trade, timeout_s=120)

        if fill_price:
            print(f"    Filled @ ${fill_price:.2f}")
            order_id = str(trade.order.orderId)
            stop_trade = ic.place_stop_order(ib, paper_account, sym, qty, stop_price)
            ib.sleep(1.0)

            st.record_order(order_id, sym, "BUY", qty,
                            limit_price=fill_price, strategy="blend")
            st.mark_filled(order_id, fill_price, side="BUY")
            st.update_stop(sym, stop_price, str(stop_trade.order.orderId),
                           trailing_high=fill_price, strategy="blend")
            _record_exit("PAPER_AUTO", sym, *rank_map.get(sym, (0, 0.0)),
                         fill_price, None, None, None, 0,
                         ",".join(sold_symbols), "BOUGHT")
            bought_symbols.append(sym)
        else:
            print(f"    [WARN] {sym} fill timed out.")

    ic.disconnect(ib)

    print()
    print("  ── Summary ──────────────────────────────────────────")
    print(f"  Exited : {', '.join(sold_symbols) or 'none'}")
    print(f"  Bought : {', '.join(bought_symbols) or 'none'}")
    print(f"  Tracking logged to: {TRACKING_DB}")


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="IBKR blend momentum check")
    group  = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--live",       action="store_true",
                       help="Live account: report only, no trades")
    group.add_argument("--paper-auto", action="store_true",
                       help="Paper account: automated exits + buys (pure momentum)")
    args = parser.parse_args()

    log_f = _setup_logging()
    now   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    mode  = "LIVE  (report only)" if args.live else "PAPER AUTO  (execute)"

    print()
    print("=" * 72)
    print(f"  IBKR Momentum Check  ·  {mode}  ·  {now}")
    print(f"  Thresholds: score < {EXIT_SCORE_THRESH}  OR  rank > {EXIT_RANK_THRESH}")
    print("=" * 72)

    try:
        if args.live:
            run_live_report()
        else:
            run_paper_auto()
    except Exception as e:
        print(f"\n  [ERROR] {e}")
        import traceback
        traceback.print_exc()
        log_f.close()
        sys.exit(1)

    print()
    print("=" * 72)
    print()
    log_f.close()


if __name__ == "__main__":
    main()
