"""
atos_runner.py
---------------
ATOS v1 — Daily Entry Point.

Run this once per trading day (schedule with Windows Task Scheduler):
    python atos_runner.py

What it does each cycle:
  1. Kill switch + daily loss cap check
  2. Download latest daily bars for full universe
  3. Compute all features (EMA, ATR, RSI, MACD, Bollinger, Donchian, ADX)
  4. Run Decision Engine — 5 weighted detectors per ticker
  5. Risk Engine approval for each BUY signal
  6. Place approved orders on Saxo SIM via existing saxo_client.py
  7. Check exits on all open positions
  8. Run learning pass — update detector weights from closed trades
  9. Log everything to data/atos.db
 10. Generate HTML dashboard → dashboard/index.html
 11. Open dashboard locally at http://localhost:8070

Schedule: once per day after market close (e.g., 23:00 PKT)
Uses daily bars, so intraday timing doesn't affect signals.
"""

import os
import sys
import json
import ftplib
import threading
import webbrowser
from http.server import HTTPServer, SimpleHTTPRequestHandler
from functools import partial
from datetime import datetime, date

import pandas as pd
import yfinance as yf

# ── Path setup (run from any directory) ───────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

# ── ATOS modules ──────────────────────────────────────────────────
from atos import database as db
from atos.universe import ATOS_UNIVERSE, market_of, MARKET_GROUPS
from atos.features import add_all
from atos.decision_engine import scan_universe, BUY_THRESHOLD
from atos.learner import run_learning_pass, format_weight_bar
from atos.risk import (
    RiskEngine, get_risk_capital, record_fill, get_day_start_equity,
    daily_loss_cap_breached, kill_switch_active, commission_sek,
    STARTING_CAPITAL_SEK,
)
from atos.dashboard_gen import generate as gen_dashboard

# ── Existing infrastructure (unchanged) ───────────────────────────
import saxo_client
import fx

DEPLOY_CONFIG = os.path.join(BASE_DIR, "config", "deploy.json")

# ── Settings ───────────────────────────────────────────────────────
HISTORY_DAYS   = 300    # days of history to download (need 200 for EMA200)
ASSET_TYPE_MAP = {      # Saxo asset type per market group
    "US Equities":  "Stock",
    "OMX30":        "Stock",
    "DAX40":        "Stock",
    "Commodities":  "Etf",
    "Forex":        "FxSpot",
}


# ══════════════════════════════════════════════════════════════════
# Data
# ══════════════════════════════════════════════════════════════════

def download_universe(tickers: list[str]) -> dict[str, pd.DataFrame]:
    """Download HISTORY_DAYS of daily OHLCV from Yahoo Finance."""
    print(f"  Downloading data for {len(tickers)} tickers...", end=" ", flush=True)
    try:
        raw = yf.download(
            tickers,
            period=f"{HISTORY_DAYS}d",
            interval="1d",
            group_by="ticker",
            auto_adjust=True,
            threads=True,
            progress=False,
        )
    except Exception as e:
        print(f"FAILED: {e}")
        return {}

    result = {}
    if len(tickers) == 1:
        ticker = tickers[0]
        if not raw.empty:
            result[ticker] = raw
    else:
        for ticker in tickers:
            try:
                df = raw[ticker].dropna(how="all")
                if not df.empty and len(df) >= 50:
                    result[ticker] = df
            except (KeyError, TypeError):
                pass

    print(f"OK ({len(result)} with data)")
    return result


# ══════════════════════════════════════════════════════════════════
# FTP Upload  (disabled — using localhost mode)
# ══════════════════════════════════════════════════════════════════

def upload_dashboard(local_file: str):
    """Upload dashboard/index.html to namazic.com via FTP.
    Currently disabled — using localhost mode instead.
    Re-enable by uncommenting the call in run_cycle()."""
    if not os.path.exists(DEPLOY_CONFIG):
        print("  [SKIP] config/deploy.json not found — no FTP upload")
        return

    with open(DEPLOY_CONFIG) as f:
        cfg = json.load(f)

    try:
        print(f"  Uploading dashboard to {cfg.get('domain_url', 'FTP')}...", end=" ", flush=True)
        with ftplib.FTP() as ftp:
            ftp.connect(cfg["ftp_host"], int(cfg.get("ftp_port", 21)), timeout=30)
            ftp.login(cfg["ftp_user"], cfg["ftp_password"])

            # Ensure remote directory exists
            remote_dir = cfg.get("remote_dir", "/public_html/atos/")
            try:
                ftp.mkd(remote_dir)
            except ftplib.error_perm:
                pass  # already exists
            ftp.cwd(remote_dir)

            with open(local_file, "rb") as f_data:
                ftp.storbinary("STOR index.html", f_data)

        print(f"OK → {cfg.get('domain_url', remote_dir)}")
    except Exception as e:
        print(f"FAILED: {e}")


# ══════════════════════════════════════════════════════════════════
# Local Dashboard Server
# ══════════════════════════════════════════════════════════════════

LOCAL_PORT = 8070

def serve_dashboard_locally():
    """Start a lightweight HTTP server for the dashboard directory
    and open it in the default browser at http://localhost:8070.
    Server runs in a background daemon thread so the script can exit."""
    dashboard_dir = os.path.join(BASE_DIR, "dashboard")
    if not os.path.isdir(dashboard_dir):
        print(f"  [SKIP] dashboard directory not found: {dashboard_dir}")
        return

    handler = partial(SimpleHTTPRequestHandler, directory=dashboard_dir)

    try:
        server = HTTPServer(("", LOCAL_PORT), handler)
    except OSError as e:
        if "address already in use" in str(e).lower() or "10048" in str(e):
            # Server already running from a previous cycle — just open browser
            print(f"  Dashboard server already running on port {LOCAL_PORT}")
            webbrowser.open(f"http://localhost:{LOCAL_PORT}")
            return
        raise

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://localhost:{LOCAL_PORT}"
    print(f"  Dashboard live at {url}")
    webbrowser.open(url)


# ══════════════════════════════════════════════════════════════════
# Terminal Dashboard
# ══════════════════════════════════════════════════════════════════

def print_banner(total_equity: float, day_start: float, open_count: int,
                 weights: dict, todays_actions: list, learning_result: dict):
    pct = (total_equity - STARTING_CAPITAL_SEK) / STARTING_CAPITAL_SEK * 100
    day_pnl = total_equity - day_start
    sign    = "+" if pct >= 0 else ""
    dpnl_s  = ("+" if day_pnl >= 0 else "") + f"{day_pnl:,.0f}"
    num_t   = weights.get("num_trades", 0)

    w = weights
    print(f"""
╔══════════════════════════════════════════════════════════════════╗
║   ATOS Daily Run — {datetime.now().strftime('%a %Y-%m-%d  %H:%M PKT'):<38}║
╠══════════════════════════════════════════════════════════════════╣
║  Total Equity:  {total_equity:>8,.0f} SEK  ({sign}{pct:.2f}%)   Open: {open_count}/10     ║
║  Today's P&L:   {dpnl_s:>10} SEK                                 ║
╠══════════════════════════════════════════════════════════════════╣
║  ALGORITHM WEIGHTS  (learned from {num_t} trades)               ║
║  Trend      {format_weight_bar(w.get('w_trend',1.0))}  {w.get('w_trend',1.0):.3f}                           ║
║  Momentum   {format_weight_bar(w.get('w_momentum',1.0))}  {w.get('w_momentum',1.0):.3f}                           ║
║  Breakout   {format_weight_bar(w.get('w_breakout',1.0))}  {w.get('w_breakout',1.0):.3f}                           ║
║  Mean Rev   {format_weight_bar(w.get('w_mean_revert',1.0))}  {w.get('w_mean_revert',1.0):.3f}                           ║
║  Volume     {format_weight_bar(w.get('w_volume',1.0))}  {w.get('w_volume',1.0):.3f}                           ║
╠══════════════════════════════════════════════════════════════════╣
║  TODAY'S ACTIONS                                                 ║""")

    if todays_actions:
        for a in todays_actions[:8]:
            action = a.get("action","")
            ticker = a.get("ticker","")[:8]
            score  = a.get("score", 0)
            reason = a.get("reason","")[:25]
            pnl    = a.get("pnl_sek")
            pnl_s  = f"+{pnl:.0f}" if pnl and pnl >= 0 else (f"{pnl:.0f}" if pnl else "")
            print(f"║  {action:<5}  {ticker:<10}  Score:{score:<5}  {reason:<26}  {pnl_s:<8}║")
    else:
        print("║  No actions taken today                                          ║")

    new_t = learning_result.get("new_trades_processed", 0)
    print(f"""╠══════════════════════════════════════════════════════════════════╣
║  LEARNING: {new_t} new trades processed this cycle                   ║
╚══════════════════════════════════════════════════════════════════╝""")


# ══════════════════════════════════════════════════════════════════
# Main Cycle
# ══════════════════════════════════════════════════════════════════

def run_cycle():
    print(f"\n{'='*60}\nATOS Daily Cycle — {datetime.now():%Y-%m-%d %H:%M:%S}\n{'='*60}")

    # ── 0. Init DB ────────────────────────────────────────────────
    db.init_db()

    # ── 1. Safety checks ──────────────────────────────────────────
    if kill_switch_active():
        print("STOP_TRADING file present — halted. Delete it to resume.")
        return

    day_start = get_day_start_equity()

    if daily_loss_cap_breached():
        print("Daily loss cap breached — no new entries today.")

    # ── 2. Saxo account state ─────────────────────────────────────
    try:
        balances         = saxo_client.get_balances()
        cash_available   = balances.get("CashBalance", 0)
        account_currency = balances.get("Currency", "EUR")
        fx_rate          = fx.get_rate_to_sek(account_currency)
        cash_sek         = cash_available * fx_rate
        print(f"  Saxo SIM: {cash_available:,.2f} {account_currency} "
              f"= {cash_sek:,.0f} SEK available")
    except Exception as e:
        print(f"  [WARN] Could not fetch Saxo balances: {e}")
        cash_sek = get_risk_capital()

    # ── 3. Load state ─────────────────────────────────────────────
    open_trades   = db.get_open_trades()
    open_tickers  = {t["ticker"] for t in open_trades}
    risk_capital  = get_risk_capital()
    weights       = db.get_current_weights()

    print(f"  Risk capital: {risk_capital:,.0f} SEK | Open positions: {len(open_trades)}")

    # ── 4. Download & compute features ────────────────────────────
    print("  Fetching market data...")
    raw_data   = download_universe(ATOS_UNIVERSE)
    feat_data  = {}
    for ticker, df in raw_data.items():
        try:
            feat_data[ticker] = add_all(df)
        except Exception as e:
            print(f"  [WARN] feature calc failed for {ticker}: {e}")

    # ── 5. Decision Engine scan ───────────────────────────────────
    print(f"  Running Decision Engine on {len(feat_data)} instruments...")
    decisions = scan_universe(
        universe_data    = feat_data,
        market_group_fn  = market_of,
        open_tickers     = open_tickers,
        weights          = weights,
    )
    print(f"  Signals: {sum(1 for d in decisions.values() if d.action=='BUY')} BUY, "
          f"{sum(1 for d in decisions.values() if d.action=='EXIT')} EXIT")

    # ── 6. Risk Engine ────────────────────────────────────────────
    risk = RiskEngine(open_trades)
    todays_actions = []

    # ── 6a. Exits first ───────────────────────────────────────────
    for trade in list(open_trades):
        ticker   = trade["ticker"]
        decision = decisions.get(ticker)
        if decision is None or decision.action != "EXIT":
            # Also check ATR stop on latest price
            if ticker not in feat_data:
                continue
            last_row  = feat_data[ticker].iloc[-1]
            stop_price = trade.get("stop_price", 0) or 0
            hit_stop   = (pd.notna(last_row.get("Low")) and
                          last_row["Low"] <= stop_price and stop_price > 0)
            if not hit_stop:
                continue
            exit_reason = "stop_loss"
        else:
            exit_reason = "score_dropped"

        last_price = feat_data[ticker].iloc[-1]["Close"] if ticker in feat_data else None
        if last_price is None:
            continue

        mkt    = trade.get("market_group", "Unknown")
        rate   = fx.get_rate_to_sek(_currency_for(mkt))
        price_sek = last_price * rate
        comm   = commission_sek(trade.get("shares", 0), price_sek)
        pnl_sek = (trade.get("shares", 0) * (price_sek -
                   (trade.get("entry_price", last_price) * rate)) - comm)

        try:
            asset_type = ASSET_TYPE_MAP.get(mkt, "Stock")
            # Look up Saxo UIC from existing instrument map where available
            from instrument_map import load_instrument_map
            imap = load_instrument_map()
            if ticker in imap:
                uic = imap[ticker]["uic"]
                saxo_client.place_market_order(uic, asset_type, "Sell",
                                               int(trade.get("shares", 0)))
                order_ok = True
            else:
                order_ok = False
                print(f"  [SKIP EXIT] {ticker} not in instrument_map.csv")
        except Exception as e:
            order_ok = False
            print(f"  EXIT order failed {ticker}: {e}")

        db.close_trade(trade["id"], last_price, exit_reason, pnl_sek, comm)
        record_fill(trade.get("shares", 0) * price_sek)

        todays_actions.append({
            "action": "EXIT", "ticker": ticker, "market_group": mkt,
            "score": decision.score if decision else 0,
            "shares": trade.get("shares", 0),
            "price": last_price, "reason": exit_reason, "pnl_sek": pnl_sek,
        })
        db.insert_signal({
            "signal_date": date.today().isoformat(), "market_group": mkt,
            "ticker": ticker, "final_score": decision.score if decision else 0,
            "d1_trend": decision.d1_trend if decision else 0,
            "d2_momentum": decision.d2_momentum if decision else 0,
            "d3_breakout": decision.d3_breakout if decision else 0,
            "d4_mean_revert": decision.d4_mean_revert if decision else 0,
            "d5_volume": decision.d5_volume if decision else 0,
            "action": "EXIT", "executed": 1 if order_ok else 0,
            "block_reason": None if order_ok else "order_failed",
        })

    # ── 6b. New entries ───────────────────────────────────────────
    if not daily_loss_cap_breached():
        buy_candidates = [
            (ticker, dec) for ticker, dec in decisions.items()
            if dec.action == "BUY" and ticker not in open_tickers
        ]
        buy_candidates.sort(key=lambda x: x[1].score, reverse=True)

        for ticker, decision in buy_candidates:
            if ticker not in feat_data:
                continue
            last_row = feat_data[ticker].iloc[-1]
            mkt      = market_of(ticker)
            rate     = fx.get_rate_to_sek(_currency_for(mkt))

            entry_price = last_row["Close"]
            atr_raw     = last_row.get("atr")
            if pd.isna(atr_raw):
                continue

            entry_sek = entry_price * rate
            atr_sek   = atr_raw * rate

            approval = risk.approve_entry(
                ticker, mkt, decision.score,
                entry_sek, atr_sek, cash_sek
            )

            db.insert_signal({
                "signal_date": date.today().isoformat(), "market_group": mkt,
                "ticker": ticker, "final_score": decision.score,
                "d1_trend": decision.d1_trend, "d2_momentum": decision.d2_momentum,
                "d3_breakout": decision.d3_breakout,
                "d4_mean_revert": decision.d4_mean_revert,
                "d5_volume": decision.d5_volume,
                "action": "BUY",
                "executed": 1 if approval["approved"] else 0,
                "block_reason": None if approval["approved"] else approval["reason"],
            })

            if not approval["approved"]:
                todays_actions.append({
                    "action": "BLOCKED", "ticker": ticker, "market_group": mkt,
                    "score": decision.score, "shares": 0,
                    "price": entry_price, "reason": approval["reason"], "pnl_sek": None,
                })
                continue

            shares    = approval["shares"]
            stop_p    = approval["stop_price"] / rate   # back to instrument currency
            cost_sek  = approval["cost_sek"]
            comm_sek  = approval["comm_sek"]

            order_ok = False
            try:
                from instrument_map import load_instrument_map
                imap = load_instrument_map()
                if ticker in imap:
                    uic = imap[ticker]["uic"]
                    asset_type = ASSET_TYPE_MAP.get(mkt, "Stock")
                    saxo_client.place_market_order(uic, asset_type, "Buy", shares)
                    order_ok = True
                else:
                    print(f"  [SKIP BUY] {ticker}: not in instrument_map.csv")
            except Exception as e:
                print(f"  BUY order failed {ticker}: {e}")

            trade_id = db.insert_trade({
                "market_group": mkt, "ticker": ticker, "direction": "BUY",
                "entry_date": date.today().isoformat(),
                "entry_price": entry_price,
                "shares": shares,
                "commission_sek": comm_sek,
                "entry_score": decision.score,
                "d1_trend": decision.d1_trend,
                "d2_momentum": decision.d2_momentum,
                "d3_breakout": decision.d3_breakout,
                "d4_mean_revert": decision.d4_mean_revert,
                "d5_volume": decision.d5_volume,
                "stop_price": stop_p,
            })

            if order_ok:
                record_fill(-cost_sek)
                cash_sek -= cost_sek
                risk.register_fill(mkt)
                open_tickers.add(ticker)

            todays_actions.append({
                "action": "BUY" if order_ok else "BUY(LOGGED)",
                "ticker": ticker, "market_group": mkt,
                "score": decision.score, "shares": shares,
                "price": entry_price, "reason": "signal", "pnl_sek": None,
            })

    # ── 7. Learning pass ──────────────────────────────────────────
    print("  Running learning pass...")
    learning_result = run_learning_pass()
    weights = db.get_current_weights()  # refresh after learning

    # ── 8. Equity snapshot ────────────────────────────────────────
    open_trades_now = db.get_open_trades()
    total_equity    = get_risk_capital()

    equity_by_mkt = {k: 0.0 for k in ["US Equities","OMX30","DAX40","Commodities","Forex"]}
    for t in open_trades_now:
        mkt = t.get("market_group","Unknown")
        if mkt in equity_by_mkt:
            equity_by_mkt[mkt] += (t.get("shares",0) or 0) * (t.get("entry_price",0) or 0)

    db.upsert_equity({
        "snap_date":        date.today().isoformat(),
        "total_equity_sek": total_equity,
        "us_equity_sek":    equity_by_mkt["US Equities"],
        "omx30_equity_sek": equity_by_mkt["OMX30"],
        "dax_equity_sek":   equity_by_mkt["DAX40"],
        "commodities_sek":  equity_by_mkt["Commodities"],
        "forex_sek":        equity_by_mkt["Forex"],
        "open_positions":   len(open_trades_now),
        "trades_today":     len([a for a in todays_actions if a["action"] in ("BUY","EXIT")]),
    })

    # ── 9. Terminal output ────────────────────────────────────────
    print_banner(total_equity, day_start, len(open_trades_now),
                 weights, todays_actions, learning_result)

    # ── 10. Generate + serve HTML dashboard locally ────────────────
    print("\n  Generating HTML dashboard...")
    html_file = gen_dashboard(
        todays_actions = todays_actions,
        open_trades    = open_trades_now,
        run_summary    = {
            "total_equity_sek": total_equity,
            "day_start_equity": day_start,
            "trades_today":     len([a for a in todays_actions if a["action"] in ("BUY","EXIT")]),
            "errors":           [],
        },
    )
    print(f"  Dashboard saved: {html_file}")
    # upload_dashboard(html_file)  # ← uncomment to re-enable FTP upload
    serve_dashboard_locally()

    print("\nCycle complete.\n")


def _currency_for(market_group: str) -> str:
    """Best-guess currency per market group for FX conversion."""
    return {
        "US Equities": "USD",
        "Commodities": "USD",
        "Forex":       "USD",
        "OMX30":       "SEK",
        "DAX40":       "EUR",
    }.get(market_group, "USD")


if __name__ == "__main__":
    run_cycle()
