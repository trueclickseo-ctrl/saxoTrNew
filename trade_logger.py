"""
trade_logger.py  —  Persistent CSV trade log for all Saxo modules
-----------------------------------------------------------------
Appends every buy/sell to module-specific CSV files and a master log.
Files are NEVER truncated — every trade is kept forever.

Files written:
    data/trades_forex.csv
    data/trades_futures.csv
    data/trades_etf.csv
    data/trades_all.csv   ← master log across all modules

CSV columns:
    timestamp, date, time, module, strategy, symbol, side,
    quantity, price, order_id, mode, stop_price, notes

Usage:
    import trade_logger
    trade_logger.log_trade(
        module   = "forex",        # forex | futures | etf | stock
        strategy = "pullback",
        symbol   = "USDJPY",
        side     = "Buy",          # Buy | Sell
        quantity = 10000,
        price    = 149.532,
        order_id = "123456789",    # None if dry run
        dry_run  = False,
        stop_price = 148.900,      # optional
        notes    = "",             # optional free text
    )
"""

import csv
import os
from datetime import datetime

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
DATA_DIR  = os.path.join(BASE_DIR, "data")

COLUMNS = [
    "timestamp", "date", "time",
    "module", "strategy", "symbol",
    "side", "quantity", "price",
    "order_id", "mode", "stop_price", "notes",
]

_MODULE_FILES = {
    "forex":   "trades_forex.csv",
    "futures": "trades_futures.csv",
    "etf":     "trades_etf.csv",
    "stock":   "trades_stocks.csv",
}
_ALL_FILE = "trades_all.csv"


def _csv_path(filename: str) -> str:
    return os.path.join(DATA_DIR, filename)


def _ensure_header(path: str) -> None:
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(COLUMNS)


def log_trade(
    module:     str,
    strategy:   str,
    symbol:     str,
    side:       str,
    quantity:   float,
    price:      float,
    order_id:   str  = None,
    dry_run:    bool = False,
    stop_price: float = 0.0,
    notes:      str  = "",
) -> None:
    """Append one trade row to the module CSV and the master log.

    Call this immediately after placing (or dry-running) an order.
    Never raises — logs to stderr on write failure so runners keep going.
    """
    os.makedirs(DATA_DIR, exist_ok=True)

    now  = datetime.now()
    row  = [
        now.isoformat(timespec="seconds"),     # timestamp
        now.strftime("%Y-%m-%d"),              # date
        now.strftime("%H:%M:%S"),              # time
        module,
        strategy,
        symbol,
        side,
        int(quantity) if quantity == int(quantity) else quantity,
        round(float(price), 6),
        order_id or ("DRY" if dry_run else ""),
        "DRY" if dry_run else "LIVE",
        round(float(stop_price), 6) if stop_price else "",
        notes,
    ]

    module_file = _MODULE_FILES.get(module, f"trades_{module}.csv")

    for fname in (module_file, _ALL_FILE):
        path = _csv_path(fname)
        try:
            _ensure_header(path)
            with open(path, "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(row)
        except Exception as exc:
            import sys
            print(f"[trade_logger] WARNING: could not write {path}: {exc}", file=sys.stderr)


def tail(module: str = None, n: int = 20) -> list[dict]:
    """Return the last n rows from a module log (or master if module=None)."""
    if module:
        fname = _MODULE_FILES.get(module, f"trades_{module}.csv")
    else:
        fname = _ALL_FILE
    path = _csv_path(fname)
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return rows[-n:]


# ── CLI ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse

    GR = "\033[92m"; RD = "\033[91m"; CY = "\033[96m"
    YL = "\033[93m"; W  = "\033[0m";  BD = "\033[1m"; DM = "\033[2m"

    ap = argparse.ArgumentParser(description="View persistent trade logs")
    ap.add_argument("--module", choices=["forex", "futures", "etf", "stock"],
                    help="Filter to one module (default: all)")
    ap.add_argument("--n",      type=int, default=30, help="Rows to show (default 30)")
    ap.add_argument("--today",  action="store_true",  help="Show only today's trades")
    args = ap.parse_args()

    rows = tail(args.module, n=500)
    today = datetime.now().strftime("%Y-%m-%d")
    if args.today:
        rows = [r for r in rows if r.get("date") == today]

    rows = rows[-args.n:]

    if not rows:
        print(f"{YL}No trades logged yet.{W}")
    else:
        mod_label = args.module.upper() if args.module else "ALL MODULES"
        print(f"\n{BD}{CY}{'='*90}{W}")
        print(f"{BD}{CY}  TRADE LOG -- {mod_label}  (last {len(rows)} entries){W}")
        print(f"{BD}{CY}{'='*90}{W}")
        print(f"{DM}  {'DATE':<12} {'TIME':<9} {'MODULE':<8} {'STRATEGY':<12} "
              f"{'SYMBOL':<8} {'SIDE':<5} {'QTY':>10} {'PRICE':>12} {'ORDER ID':<14} {'MODE':<5} {'STOP':>10}  NOTES{W}")
        print(f"  {'─'*88}")

        for r in rows:
            side  = r.get("side", "")
            mode  = r.get("mode", "")
            col   = GR if side == "Buy"  else RD
            mcol  = DM if mode == "DRY"  else W
            qty_s = r.get("quantity", "")
            try:
                qty_s = f"{int(float(qty_s)):,}"
            except Exception:
                pass
            price_s = r.get("price", "")
            try:
                price_s = f"{float(price_s):.5f}"
            except Exception:
                pass
            stop_s = r.get("stop_price", "")
            try:
                stop_s = f"{float(stop_s):.5f}" if stop_s else ""
            except Exception:
                pass

            print(f"{mcol}  {r.get('date',''):<12} {r.get('time',''):<9} "
                  f"{r.get('module',''):<8} {r.get('strategy',''):<12} "
                  f"{col}{BD}{r.get('symbol',''):<8}{W}{mcol} "
                  f"{col}{side:<5}{W}{mcol} "
                  f"{qty_s:>10} {price_s:>12} "
                  f"{r.get('order_id',''):<14} "
                  f"{mode:<5} {stop_s:>10}  {r.get('notes','')}{W}")

        print(f"\n  {DM}Files: data/trades_{args.module or 'all'}.csv{W}\n")
