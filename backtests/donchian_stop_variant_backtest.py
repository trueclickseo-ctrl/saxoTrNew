"""
backtests/donchian_stop_variant_backtest.py
--------------------------------------------
Compares donchian's actual ATR stop (variant A, real recorded outcomes)
against two alternatives, simulated bar-by-bar on real historical daily
data pulled fresh from Saxo (/chart/v3/charts with Mode=UpTo):

  A. Current  -- 2.0x ATR hard stop (the real, already-recorded outcome).
  B. Structural -- stop = the EXIT_PERIOD (15-day) rolling channel's
     opposite band, trailed forward exactly like a Donchian exit channel.
  C. Hybrid -- same as B, but never tighter than a 1.0x ATR-at-entry floor
     (structural determines location, ATR checks the buffer is sufficient).

Real Saxo commission is pulled live per position size; position size is
held constant across variants (same units as the real trade) so the
comparison isolates stop PLACEMENT/TIMING, not a sizing-formula change.

RUN 2026-08-27, RESULT: inconclusive, not a real answer yet -- flagging
this prominently so a future run doesn't skip re-reading it. Donchian's
entire real trading history at the time was ~10 days old (started
2026-08-17); of the 27 trades tested, only 8 had resolved outcomes, and
those resolved in 1-5 real days. The window_end clamp to datetime.now()
(necessary -- Saxo has no future data) meant most simulated paths for B/C
never had enough elapsed real time to reach a structural-stop breach or
even get close, so B and C came back numerically identical in that run
(the ATR floor never bound) purely because the structural channel is far
wider than 1x ATR for these fast, trend-confirmed entries -- not evidence
that hybrid never matters. Re-run this once more donchian trades have
naturally closed (weeks, not days) for a result worth acting on.
"""

import sys, json, sqlite3
sys.path.insert(0, r"E:\SaxoTrNew\SaxoTrNew")
import pandas as pd
from datetime import datetime, timedelta
import saxo_client as sc
import forex.runner as r
from forex.universe import PAIRS
from forex.strategy_donchian import ATR_STOP_MULT, EXIT_PERIOD, TIME_STOP_DAYS, _atr

r.set_account_env("sim")
by_symbol = {p["symbol"]: p for p in PAIRS}


def fetch_window(uic, end_dt, count=90):
    resp = sc._request_with_retry(
        "GET", sc._base_url("sim") + "/chart/v3/charts", headers=sc._headers("sim"),
        params={"Uic": uic, "AssetType": "FxSpot", "Horizon": 1440, "Count": count,
                "Time": end_dt.strftime("%Y-%m-%dT00:00:00Z"), "Mode": "UpTo"})
    d = resp.json()
    rows = []
    for bar in d.get("Data", []):
        t = bar.get("Time")
        if "CloseAsk" in bar and "CloseBid" in bar:
            c = (float(bar["CloseAsk"]) + float(bar["CloseBid"])) / 2
            h = (float(bar.get("HighAsk", c)) + float(bar.get("HighBid", c))) / 2
            l = (float(bar.get("LowAsk", c)) + float(bar.get("LowBid", c))) / 2
        elif "Close" in bar:
            c, h, l = float(bar["Close"]), float(bar.get("High", bar["Close"])), float(bar.get("Low", bar["Close"]))
        else:
            continue
        rows.append({"Date": t[:10], "High": h, "Low": l, "Close": c})
    return pd.DataFrame(rows)


def parse_date(s):
    return datetime.fromisoformat(s[:10])


conn = sqlite3.connect("data/pnl_ledger.db")
conn.row_factory = sqlite3.Row
closed = conn.execute(
    "SELECT * FROM trades WHERE module='forex' AND strategy='donchian' AND status='closed'").fetchall()

with open("data/forex_state.json") as f:
    sim_state = json.load(f)
open_donchian = [(k.split(":", 1)[1], v) for k, v in sim_state["positions"].items()
                  if k.startswith("donchian:")]

results = {"A_current": [], "B_structural": [], "C_hybrid": []}
commission_cache = {}


def get_commission(uic, qty):
    key = (uic, qty)
    if key not in commission_cache:
        commission_cache[key] = r._round_trip_cost_quote_ccy(uic, qty, None)
    return commission_cache[key]


def simulate(sym, direction, entry_price, entry_date_str, qty, atr_at_entry_hint=None,
             real_exit_price=None, real_exit_date_str=None, real_pnl=None, real_reason=None):
    pinfo = by_symbol.get(sym)
    if pinfo is None:
        print(f"skip {sym}: not in universe")
        return
    uic = pinfo["uic"]
    entry_date = parse_date(entry_date_str)
    window_end = entry_date + timedelta(days=45)
    if window_end > datetime.now():
        window_end = datetime.now()
    df = fetch_window(uic, window_end, count=90)
    if df.empty:
        print(f"skip {sym}: no history returned")
        return
    df["Date"] = pd.to_datetime(df["Date"])
    entry_idx = df.index[df["Date"] >= entry_date]
    if len(entry_idx) == 0:
        print(f"skip {sym}: entry date not in fetched window")
        return
    i0 = entry_idx[0]
    if i0 < EXIT_PERIOD:
        print(f"skip {sym}: not enough pre-entry history for the {EXIT_PERIOD}-day channel")
        return

    is_long = direction in ("Buy", "BUY")
    quote_ccy = sym[3:6]
    rate = r._eur_per_unit(quote_ccy, None)
    if rate is None:
        print(f"skip {sym}: no EUR rate for {quote_ccy}")
        return
    cost_quote = get_commission(uic, qty)
    cost_eur = cost_quote * rate if cost_quote is not None else None

    atr_series = _atr(df["High"], df["Low"], df["Close"])
    atr_at_entry = atr_at_entry_hint or float(atr_series.iloc[i0])

    def structural_level(idx):
        window = df["Close"].iloc[max(0, idx - EXIT_PERIOD):idx]
        return float(window.min()) if is_long else float(window.max())

    # ---- Variant A: use the REAL recorded outcome directly (ground truth) ----
    if real_exit_price is not None:
        gross_quote = ((real_exit_price - entry_price) * qty if is_long
                        else (entry_price - real_exit_price) * qty)
        gross_eur = gross_quote * rate
        net_eur = gross_eur - cost_eur if cost_eur is not None else None
        risk_quote = abs(entry_price - (entry_price - ATR_STOP_MULT * atr_at_entry if is_long
                                         else entry_price + ATR_STOP_MULT * atr_at_entry))
        risk_eur = risk_quote * qty * rate
        results["A_current"].append({
            "symbol": sym, "net_pnl_eur": round(net_eur, 2) if net_eur is not None else None,
            "commission_eur": round(cost_eur, 2) if cost_eur is not None else None,
            "risk_eur": round(risk_eur, 2), "exit_reason": real_reason,
            "days_held": (parse_date(real_exit_date_str) - entry_date).days if real_exit_date_str else None,
        })
    else:
        # currently open -- mark to the last available close as an as-of-now proxy
        mtm_price = float(df["Close"].iloc[-1])
        gross_quote = (mtm_price - entry_price) * qty if is_long else (entry_price - mtm_price) * qty
        gross_eur = gross_quote * rate
        net_eur = gross_eur - cost_eur if cost_eur is not None else None
        risk_quote = ATR_STOP_MULT * atr_at_entry
        risk_eur = risk_quote * qty * rate
        results["A_current"].append({
            "symbol": sym, "net_pnl_eur": round(net_eur, 2) if net_eur is not None else None,
            "commission_eur": round(cost_eur, 2) if cost_eur is not None else None,
            "risk_eur": round(risk_eur, 2), "exit_reason": "OPEN (mark-to-market)",
            "days_held": None,
        })

    # ---- Variants B (structural) and C (hybrid) -- simulated day by day ----
    for variant_name, use_atr_floor in (("B_structural", False), ("C_hybrid", True)):
        stop = structural_level(i0)
        if use_atr_floor:
            floor_dist = ATR_STOP_MULT / 2 * atr_at_entry  # 1.0x ATR floor (half of the 2.0x mult used elsewhere)
            if is_long:
                stop = min(stop, entry_price - floor_dist)
            else:
                stop = max(stop, entry_price + floor_dist)
        initial_stop = stop
        exit_price, exit_day_idx, reason = None, None, None
        for idx in range(i0 + 1, len(df)):
            days_held = (df["Date"].iloc[idx] - entry_date).days
            if days_held >= TIME_STOP_DAYS:
                exit_price = float(df["Close"].iloc[idx])
                exit_day_idx, reason = idx, f"time_stop ({days_held}d)"
                break
            day_low, day_high = float(df["Low"].iloc[idx]), float(df["High"].iloc[idx])
            if is_long and day_low <= stop:
                exit_price, exit_day_idx, reason = stop, idx, "structural_stop"
                break
            if not is_long and day_high >= stop:
                exit_price, exit_day_idx, reason = stop, idx, "structural_stop"
                break
            new_level = structural_level(idx)
            if use_atr_floor:
                if is_long:
                    new_level = min(new_level, entry_price - floor_dist)
                else:
                    new_level = max(new_level, entry_price + floor_dist)
            stop = max(stop, new_level) if is_long else min(stop, new_level)
        if exit_price is None:
            exit_price = float(df["Close"].iloc[-1])
            reason = "window_end (still open)"
            exit_day_idx = len(df) - 1

        gross_quote = (exit_price - entry_price) * qty if is_long else (entry_price - exit_price) * qty
        gross_eur = gross_quote * rate
        net_eur = gross_eur - cost_eur if cost_eur is not None else None
        risk_quote = abs(entry_price - initial_stop)
        risk_eur = risk_quote * qty * rate
        results[variant_name].append({
            "symbol": sym, "net_pnl_eur": round(net_eur, 2) if net_eur is not None else None,
            "commission_eur": round(cost_eur, 2) if cost_eur is not None else None,
            "risk_eur": round(risk_eur, 2), "exit_reason": reason,
            "days_held": (df["Date"].iloc[exit_day_idx] - entry_date).days if exit_day_idx else None,
        })
    print(f"done: {sym} {direction} entry={entry_date_str}")


for row in closed:
    simulate(row["symbol"], row["direction"], row["entry_price"], row["timestamp_open"],
             row["quantity"], real_exit_price=row["exit_price"],
             real_exit_date_str=row["timestamp_close"], real_pnl=row["realized_pnl"],
             real_reason=row["exit_reason"])

for sym, pos in open_donchian:
    simulate(sym, pos["direction"], pos["entry_price"], pos.get("entry_date", ""),
             pos["quantity"], atr_at_entry_hint=pos.get("atr_at_entry"))

with open(r"E:\SaxoTrNew\SaxoTrNew\.devtools\_donchian_backtest_results.json", "w") as f:
    json.dump(results, f, indent=2, default=str)

for variant, rows in results.items():
    valid = [x for x in rows if x["net_pnl_eur"] is not None]
    total = sum(x["net_pnl_eur"] for x in valid)
    wins = [x for x in valid if x["net_pnl_eur"] > 0]
    losses = [x for x in valid if x["net_pnl_eur"] <= 0]
    print(f"\n{variant}: n={len(valid)} net_total={total:.2f} wins={len(wins)} losses={len(losses)}")

print("\nDONE")
