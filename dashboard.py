"""
dashboard.py
------------
Generates a local HTML report explaining what the strategy is actually
seeing right now — open positions and how close they are to their stop,
which tickers are near a crossover signal (the bot's "watchlist"), and
the bot's recent actions from live_order_log.csv.

This is READ-ONLY — it never places orders, only reads current account
state and computes the same signals the live engine uses, for you to see.

Run any time:
    python dashboard.py

Then open results/dashboard.html in your browser. Re-run it whenever you
want a fresh snapshot (there's no auto-refresh — deliberately simple).
"""

import os
import csv
import webbrowser
from datetime import datetime

import pandas as pd
import config
import saxo_client
from strategy import add_indicators
from live_data import get_latest_universe_data
from instrument_map import load_instrument_map
from kill_switch import kill_switch_active, get_day_start_equity, daily_loss_cap_breached
from fx import get_rate_to_sek

OUTPUT_FILE = os.path.join(os.path.dirname(__file__), "results", "dashboard.html")
LOG_FILE = os.path.join(os.path.dirname(__file__), "results", "live_order_log.csv")


def _get_open_uics(saxo_positions: dict) -> dict:
    open_uics = {}
    for pos in saxo_positions.get("Data", []):
        base = pos.get("PositionBase", {})
        uic = base.get("Uic")
        amount = base.get("Amount", 0)
        if uic is not None and amount:
            open_uics[uic] = {
                "amount": amount,
                "entry_price": base.get("OpenPrice"),
                "current_price": pos.get("PositionView", {}).get("CurrentPrice"),
            }
    return open_uics


def _load_recent_log(n=20) -> list[dict]:
    if not os.path.exists(LOG_FILE):
        return []
    with open(LOG_FILE) as f:
        rows = list(csv.DictReader(f))
    return rows[-n:][::-1]  # most recent first


def build_dashboard():
    print("Fetching account state from Saxo...")
    balances = saxo_client.get_balances()
    equity = balances["TotalValue"]
    cash = balances["CashBalance"]
    currency = balances.get("Currency", "EUR")

    fx_rate = 1.0 if currency == "SEK" else get_rate_to_sek(currency)
    day_start_equity = get_day_start_equity(equity)
    daily_pnl_pct = (equity - day_start_equity) / day_start_equity * 100 if day_start_equity else 0

    positions = saxo_client.get_positions()
    open_uics = _get_open_uics(positions)
    instrument_map = load_instrument_map()
    uic_to_ticker = {v["uic"]: k for k, v in instrument_map.items()}

    print(f"Fetching signals for {len(config.ACTIVE_UNIVERSE)} tickers...")
    universe_data = get_latest_universe_data(config.ACTIVE_UNIVERSE)
    universe_data = {t: add_indicators(df) for t, df in universe_data.items()}

    # --- Open positions with distance to stop ---
    position_rows = []
    for uic, held in open_uics.items():
        ticker = uic_to_ticker.get(uic, f"Uic:{uic}")
        df = universe_data.get(ticker)
        atr = df["atr"].iloc[-1] if df is not None and not df.empty and pd.notna(df["atr"].iloc[-1]) else None
        stop_price = held["entry_price"] - config.ATR_STOP_MULTIPLE * atr if atr else None
        current = held.get("current_price") or (df["Close"].iloc[-1] if df is not None and not df.empty else None)
        pct_to_stop = ((current - stop_price) / current * 100) if (current and stop_price) else None
        unrealized_pct = ((current - held["entry_price"]) / held["entry_price"] * 100) if current else None
        position_rows.append({
            "ticker": ticker, "amount": held["amount"], "entry": held["entry_price"],
            "current": current, "stop": stop_price, "pct_to_stop": pct_to_stop,
            "unrealized_pct": unrealized_pct,
        })

    # --- Watchlist: tickers NOT held, ranked by how close fast_ma is to slow_ma ---
    watchlist_rows = []
    for ticker, df in universe_data.items():
        uic = instrument_map.get(ticker, {}).get("uic")
        if uic in open_uics or df.empty:
            continue
        last = df.iloc[-1]
        if pd.isna(last.get("fast_ma")) or pd.isna(last.get("slow_ma")):
            continue
        gap_pct = (last["fast_ma"] - last["slow_ma"]) / last["slow_ma"] * 100
        watchlist_rows.append({
            "ticker": ticker, "close": last["Close"], "gap_pct": gap_pct,
            "trend": "Up" if last.get("trend_up") else "Down",
        })
    watchlist_rows.sort(key=lambda r: abs(r["gap_pct"]))
    watchlist_rows = watchlist_rows[:15]  # nearest 15 to a crossover

    recent_log = _load_recent_log()

    _render_html(equity, cash, currency, daily_pnl_pct,
                 kill_switch_active(), position_rows, watchlist_rows, recent_log)
    print(f"\nDashboard written to {OUTPUT_FILE}")
    webbrowser.open(f"file://{os.path.abspath(OUTPUT_FILE)}")


def _fmt(v, decimals=2, suffix=""):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    return f"{v:,.{decimals}f}{suffix}"


def _render_html(equity, cash, currency, daily_pnl_pct, kill_active,
                  position_rows, watchlist_rows, recent_log):
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)

    pos_html = "".join(f"""
        <tr>
            <td>{r['ticker']}</td><td>{r['amount']}</td>
            <td>{_fmt(r['entry'])}</td><td>{_fmt(r['current'])}</td>
            <td>{_fmt(r['stop'])}</td>
            <td class="{'warn' if r['pct_to_stop'] is not None and r['pct_to_stop'] < 3 else ''}">{_fmt(r['pct_to_stop'], 1, '%')}</td>
            <td class="{'good' if (r['unrealized_pct'] or 0) >= 0 else 'bad'}">{_fmt(r['unrealized_pct'], 1, '%')}</td>
        </tr>""" for r in position_rows) or "<tr><td colspan='7'>No open positions</td></tr>"

    watch_html = "".join(f"""
        <tr>
            <td>{r['ticker']}</td><td>{_fmt(r['close'])}</td>
            <td>{r['trend']}</td><td>{_fmt(r['gap_pct'], 2, '%')}</td>
        </tr>""" for r in watchlist_rows) or "<tr><td colspan='4'>No data</td></tr>"

    log_html = "".join(f"""
        <tr><td>{row.get('timestamp','')}</td><td>{row.get('ticker','')}</td>
        <td>{row.get('action','')}</td><td>{row.get('price','')}</td>
        <td>{row.get('amount','')}</td><td>{row.get('reason','')}</td></tr>""" for row in recent_log) \
        or "<tr><td colspan='6'>No trades logged yet</td></tr>"

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Strategy Dashboard</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, sans-serif; background: #0f1115; color: #e4e6eb; padding: 24px; }}
  h1 {{ font-size: 20px; }} h2 {{ font-size: 15px; color: #9aa4b2; margin-top: 32px; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 13px; }}
  th, td {{ text-align: left; padding: 6px 10px; border-bottom: 1px solid #262a33; }}
  th {{ color: #9aa4b2; font-weight: 500; }}
  .good {{ color: #4ade80; }} .bad {{ color: #f87171; }} .warn {{ color: #fbbf24; }}
  .summary {{ display: flex; gap: 24px; margin-top: 8px; }}
  .card {{ background: #171a21; border-radius: 8px; padding: 14px 18px; }}
  .card .label {{ color: #9aa4b2; font-size: 12px; }} .card .value {{ font-size: 20px; margin-top: 4px; }}
  .kill {{ background: #7f1d1d; color: white; padding: 8px 14px; border-radius: 6px; margin-top: 12px; }}
</style></head><body>
<h1>Strategy Dashboard — {datetime.now():%Y-%m-%d %H:%M}</h1>
<div class="summary">
  <div class="card"><div class="label">Equity</div><div class="value">{_fmt(equity)} {currency}</div></div>
  <div class="card"><div class="label">Cash available</div><div class="value">{_fmt(cash)} {currency}</div></div>
  <div class="card"><div class="label">Today's P&amp;L</div><div class="value {'good' if daily_pnl_pct >= 0 else 'bad'}">{_fmt(daily_pnl_pct, 2, '%')}</div></div>
  <div class="card"><div class="label">Daily loss cap</div><div class="value">{config.MAX_DAILY_LOSS_PCT*100:.1f}%</div></div>
</div>
{'<div class="kill">⛔ KILL SWITCH ACTIVE — bot will not trade until STOP_TRADING file is removed</div>' if kill_active else ''}

<h2>Open Positions ({len(position_rows)})</h2>
<table><tr><th>Ticker</th><th>Shares</th><th>Entry</th><th>Current</th><th>Stop</th><th>Room to stop</th><th>Unrealized</th></tr>
{pos_html}</table>

<h2>Watchlist — nearest to a crossover signal (not currently held)</h2>
<table><tr><th>Ticker</th><th>Close</th><th>Trend</th><th>Fast/Slow MA gap</th></tr>
{watch_html}</table>

<h2>Recent Bot Actions</h2>
<table><tr><th>Time</th><th>Ticker</th><th>Action</th><th>Price</th><th>Amount</th><th>Reason</th></tr>
{log_html}</table>
</body></html>"""

    with open(OUTPUT_FILE, "w") as f:
        f.write(html)


if __name__ == "__main__":
    build_dashboard()
