"""
reports/_gather_daily_sim_data.py
-----------------------------------
Phase 1 of the daily SIM report (see daily_sim_report.py for phase 2).
Runs under the project's normal Python (has pandas/requests/torch/etc.)
and writes a single JSON file the openpyxl-building phase (which runs
under a different Python that has openpyxl but not torch) can read
without needing forex.runner's heavier strategy imports at all.
"""
import sys, os, json, sqlite3
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime

import forex.runner as r
from forex.universe import PAIRS

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")

r.set_account_env("sim")
by_symbol = {p["symbol"]: p for p in PAIRS}

eur_cache, commission_cache = {}, {}

def eur_per_unit_cached(ccy):
    if ccy not in eur_cache:
        eur_cache[ccy] = r._eur_per_unit(ccy, None)
    return eur_cache[ccy]

def commission_cached(uic, qty):
    key = (uic, qty)
    if key not in commission_cache:
        commission_cache[key] = r._round_trip_cost_quote_ccy(uic, qty, None)
    return commission_cache[key]

conn = sqlite3.connect(os.path.join(DATA_DIR, "pnl_ledger.db"))
conn.row_factory = sqlite3.Row
closed_rows = conn.execute(
    "SELECT * FROM trades WHERE module='forex' AND status='closed'").fetchall()

trades = []
for row in closed_rows:
    sym = row["symbol"]
    pinfo = by_symbol.get(sym)
    if pinfo is None:
        continue
    direction, entry, exitp, qty = row["direction"], row["entry_price"], row["exit_price"], row["quantity"]
    quote_ccy = sym[3:6] if len(sym) >= 6 else ""
    rate = eur_per_unit_cached(quote_ccy)
    if entry is None or exitp is None or rate is None:
        continue
    gross_quote = (exitp - entry) * qty if direction in ("Buy", "BUY") else (entry - exitp) * qty
    cost_quote = commission_cached(pinfo["uic"], qty)
    gross_eur = gross_quote * rate
    cost_eur = cost_quote * rate if cost_quote is not None else None
    net_eur = gross_eur - cost_eur if cost_eur is not None else None
    trades.append({"strategy": row["strategy"], "symbol": sym, "direction": direction,
                    "quantity": qty, "entry": entry, "exit": exitp, "status": "closed",
                    "exit_reason": row["exit_reason"], "gross_pnl_eur": round(gross_eur, 2),
                    "commission_eur": round(cost_eur, 2) if cost_eur is not None else None,
                    "net_pnl_eur": round(net_eur, 2) if net_eur is not None else None})

with open(os.path.join(DATA_DIR, "forex_state.json")) as f:
    state = json.load(f)
for key, pos in state.get("positions", {}).items():
    strat, sym = key.split(":", 1)
    pinfo = by_symbol.get(sym)
    if pinfo is None:
        continue
    live_px = r._live_price(pinfo["uic"], None)
    if live_px is None:
        continue
    quote_ccy = sym[3:6] if len(sym) >= 6 else ""
    rate = eur_per_unit_cached(quote_ccy)
    if rate is None:
        continue
    direction, entry, qty = pos["direction"], pos["entry_price"], pos["quantity"]
    gross_quote = (live_px - entry) * qty if direction == "Buy" else (entry - live_px) * qty
    cost_quote = commission_cached(pinfo["uic"], qty)
    gross_eur = gross_quote * rate
    cost_eur = cost_quote * rate if cost_quote is not None else None
    net_eur = gross_eur - cost_eur if cost_eur is not None else None
    trades.append({"strategy": strat, "symbol": sym, "direction": direction, "quantity": qty,
                    "entry": entry, "exit": live_px, "status": "open",
                    "exit_reason": "OPEN (unrealized)", "gross_pnl_eur": round(gross_eur, 2),
                    "commission_eur": round(cost_eur, 2) if cost_eur is not None else None,
                    "net_pnl_eur": round(net_eur, 2) if net_eur is not None else None})

out = os.path.join(BASE_DIR, ".devtools", "_daily_sim_trades.json")
os.makedirs(os.path.dirname(out), exist_ok=True)
with open(out, "w") as f:
    json.dump(trades, f, indent=2, default=str)
print(f"Gathered {len(trades)} trades -> {out}")
