"""
reports/_gather_live_capital_analysis.py
-------------------------------------------
Phase 1 (real Saxo data) of the LIVE minimum-account-size analysis --
2026-08-28, explicit user request following a live GBPPLN position
whose unrealized P&L looked cost-negative. Gathers, via real live Saxo
quotes (never guessed):

  1. The 34-cell table (17 HIGH_VOLUME_SYMBOLS pairs x rsi/bb): real
     ATR, real flat round-trip commission, and the minimum account
     equity (EUR-equivalent) needed for BOTH the risk gate
     (block_below_min -- naturally reaches 1,000 units at 0.25% risk)
     AND the cost gate (MIN_EDGE_TO_COST_RATIO=3.0x) to pass together.

  2. Every currently-open LIVE position (both accounts, including
     legacy exotic-pair positions like GBPPLN from before the
     2026-08-28 HIGH_VOLUME-only redesign) -- real current price,
     unrealized P&L, the trade's real cost-gate ratio AT ENTRY, and
     whether it's cost-positive right now if closed today.

Confirmed live (2026-08-28): round-trip commission is FLAT across the
realistic 1,000-30,000 unit range for every pair tested (e.g. GBPUSD
6.00 USD flat from 1,000 to 30,000 units) -- only scales up past
~50,000-100,000 units. So a bigger NATURAL position size (from more
capital, never forced) directly improves the cost-to-edge ratio, since
target profit scales with quantity while cost does not.

Run under the project's normal Python (has forex.runner/torch):
    python reports/_gather_live_capital_analysis.py
"""
import sys, os, json, math
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import forex.runner as r
from forex.strategy import _atr
from forex.universe import get_pair, HIGH_VOLUME_SYMBOLS

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")

r.set_account_env("sim")  # market-data-only calls -- same real Saxo quotes, no order placement
# 2026-08-28 (later same day): use the ACTUAL currently-configured LIVE risk
# (0.75% as of this decision), not the original 0.25% exploratory baseline --
# this report should reflect what LIVE really does today, not a hypothetical.
# Falls back to 0.0025 if the override is ever cleared back to None.
RISK_PCT = r.LIVE_RISK_PCT_OVERRIDE if r.LIVE_RISK_PCT_OVERRIDE is not None else 0.0025
TP_RR = r.DEFAULT_TP_RR
MIN_RATIO = r.MIN_EDGE_TO_COST_RATIO
STRAT_MULT = {"rsi": 1.5, "bb": 2.0, "donchian": 2.0}  # ATR_STOP_MULT per strategy

# ============================================================
# 1. The 34-cell table
# ============================================================
cells = []
for sym in sorted(HIGH_VOLUME_SYMBOLS):
    pinfo = get_pair(sym)
    quote = sym[3:6]
    uic = pinfo["uic"]
    min_units = pinfo["min_units"]
    df = r._fetch_history(uic, count=60)
    if df is None or len(df) < 20:
        continue
    atr = float(_atr(df["High"], df["Low"], df["Close"]).iloc[-1])
    eur_rate = r._eur_per_unit(quote)
    if not eur_rate:
        continue
    cost_quote = r._round_trip_cost_quote_ccy(uic, 1000, None)
    if cost_quote is None:
        continue

    for strat in ("rsi", "bb"):
        mult = STRAT_MULT[strat]
        stop_dist = mult * atr
        target_dist = stop_dist * TP_RR

        needed_qty_for_cost = (MIN_RATIO * cost_quote) / target_dist
        qty_cost_min = max(min_units, math.ceil(needed_qty_for_cost / min_units) * min_units)

        eq_quote_risk_only = min_units * stop_dist / RISK_PCT
        eq_eur_risk_only = eq_quote_risk_only * eur_rate

        eq_quote_both = qty_cost_min * stop_dist / RISK_PCT
        eq_eur_both = eq_quote_both * eur_rate

        cells.append({
            "symbol": sym, "strategy": strat, "atr": atr,
            "stop_distance": stop_dist, "target_distance": target_dist,
            "cost_quote": cost_quote, "cost_eur": cost_quote * eur_rate,
            "min_units": min_units, "qty_cost_min": qty_cost_min,
            "eq_eur_risk_only": eq_eur_risk_only, "eq_eur_both_gates": eq_eur_both,
        })
print(f"34-cell table: {len(cells)} cells gathered")

# ============================================================
# 2. Every currently-open LIVE position (both accounts)
# ============================================================
positions = []
for account_label, state_file in [("live (SEK)", "forex_live_state.json"),
                                    ("live_eur (EUR)", "forex_live_eur_state.json")]:
    path = os.path.join(DATA_DIR, state_file)
    if not os.path.exists(path):
        continue
    with open(path) as f:
        state = json.load(f)
    for key, pos in state.get("positions", {}).items():
        strat, sym = key.split(":", 1)
        uic = pos["uic"]
        quote = sym[3:6] if len(sym) >= 6 else ""
        live_px = r._live_price(uic, None)
        cost_quote = r._round_trip_cost_quote_ccy(uic, pos["quantity"], None)
        eur_rate = r._eur_per_unit(quote)
        direction = pos["direction"]
        entry, tp, qty = pos["entry_price"], pos["tp_price"], pos["quantity"]

        target_profit_quote = abs(tp - entry) * qty
        unrealized_quote = ((live_px - entry) if direction == "Buy" else (entry - live_px)) * qty if live_px else None

        entry_ratio = (target_profit_quote / cost_quote) if cost_quote else None
        unrealized_eur = (unrealized_quote * eur_rate) if (unrealized_quote is not None and eur_rate) else None
        cost_eur = (cost_quote * eur_rate) if (cost_quote is not None and eur_rate) else None
        net_if_closed_now_eur = (unrealized_eur - cost_eur) if (unrealized_eur is not None and cost_eur is not None) else None

        is_high_volume = sym in HIGH_VOLUME_SYMBOLS
        positions.append({
            "account": account_label, "strategy": strat, "symbol": sym,
            "is_high_volume": is_high_volume,
            "direction": direction, "quantity": qty,
            "entry_price": entry, "tp_price": tp, "stop_price": pos["stop_price"],
            "current_price": live_px, "entry_date": pos.get("entry_date"),
            "target_profit_quote": target_profit_quote,
            "unrealized_quote": unrealized_quote,
            "cost_quote": cost_quote,
            "entry_cost_gate_ratio": entry_ratio,
            "unrealized_eur": unrealized_eur,
            "cost_eur": cost_eur,
            "net_if_closed_now_eur": net_if_closed_now_eur,
        })
print(f"Open LIVE positions: {len(positions)} gathered")

out = {"cells": cells, "positions": positions, "risk_pct": RISK_PCT}
out_path = os.path.join(BASE_DIR, ".devtools", "_live_capital_analysis.json")
os.makedirs(os.path.dirname(out_path), exist_ok=True)
with open(out_path, "w") as f:
    json.dump(out, f, indent=2, default=str)
print("Saved:", out_path)
