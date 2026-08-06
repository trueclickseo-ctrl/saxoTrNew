"""
start_atos.py
==============
ATOS v3 — SINGLE LAUNCHER

This is the ONE FILE you run to use ATOS. It does everything:
  1. Checks Saxo token (uses your pasted token or auto-refreshes)
  2. Runs the v3 consensus scan with REAL-TIME Saxo prices
  3. Starts the dashboard at http://localhost:8070
  4. Optionally places orders on your SIM account

Usage:
    py -3 -X utf8 start_atos.py              # Full cycle: scan + dashboard
    py -3 -X utf8 start_atos.py --scan-only   # Just scan, no dashboard
    py -3 -X utf8 start_atos.py --dashboard    # Just start dashboard
"""

import os
import sys
import json
import time
import argparse
import subprocess
from datetime import datetime

# ── Path setup ───────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import requests
import pandas as pd
import numpy as np

# ── ATOS modules ─────────────────────────────────────────────────
from atos.features import add_all
from atos.strategies import ALL_STRATEGIES
from atos.decision_engine import consensus_evaluate
from atos.universe import OMX30_TICKERS, DAX40_TICKERS, US_TICKERS, ATOS_UNIVERSE, market_of

# ══════════════════════════════════════════════════════════════════
# Saxo API — Real-time prices
# ══════════════════════════════════════════════════════════════════

TOKEN_FILE = os.path.join(BASE_DIR, "saxo_token.json")
SIM_BASE = "https://gateway.saxobank.com/sim/openapi/"


def load_token():
    """Load and validate the Saxo access token."""
    if not os.path.exists(TOKEN_FILE):
        print("  [ERROR] No saxo_token.json found!")
        print("  Paste your token or run: py -3 saxo_auth_auto.py")
        sys.exit(1)

    with open(TOKEN_FILE) as f:
        data = json.load(f)

    token = data.get("access_token", "")
    obtained = float(data.get("obtained_at", 0))
    expires_in = int(data.get("expires_in", 1200))

    # Check expiry
    expiry = obtained + expires_in
    remaining = expiry - time.time()

    if remaining < 60:
        # Try refresh
        refresh = data.get("refresh_token", "")
        if refresh:
            print("  Token expiring, attempting refresh...")
            try:
                import saxo_auth
                token = saxo_auth.get_valid_access_token()
                print("  Token refreshed OK")
                return token
            except Exception as e:
                print(f"  Refresh failed: {e}")
        print("  [ERROR] Token expired! Paste a new token or run: py -3 saxo_auth_auto.py")
        sys.exit(1)

    minutes_left = int(remaining / 60)
    print(f"  Token valid ({minutes_left} min remaining)")
    return token


def saxo_headers(token):
    return {"Authorization": "Bearer " + token}


def saxo_get_quote(token, uic, asset_type="Stock"):
    """Get real-time quote from Saxo for a single instrument."""
    url = SIM_BASE + "trade/v1/infoprices"
    params = {
        "Uic": uic,
        "AssetType": asset_type,
        "FieldGroups": "Quote,PriceInfo,PriceInfoDetails",
    }
    try:
        r = requests.get(url, headers=saxo_headers(token), params=params, timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def saxo_get_historical(token, uic, asset_type="Stock", horizon=300):
    """Get historical OHLCV data from Saxo chart API."""
    url = SIM_BASE + "chart/v1/charts"
    params = {
        "Uic": uic,
        "AssetType": asset_type,
        "Horizon": min(horizon, 1200),
        "Count": min(horizon, 1200),
        "Mode": "From",
        "Time": (datetime.utcnow() - pd.Timedelta(days=horizon + 50)).strftime("%Y-%m-%dT00:00:00Z"),
    }
    try:
        r = requests.get(url, headers=saxo_headers(token), params=params, timeout=30)
        if r.status_code == 200:
            data = r.json()
            candles = data.get("Data", [])
            if candles:
                rows = []
                for c in candles:
                    rows.append({
                        "Open": c.get("Open", c.get("OpenAsk", 0)),
                        "High": c.get("High", c.get("HighAsk", 0)),
                        "Low": c.get("Low", c.get("LowAsk", 0)),
                        "Close": c.get("Close", c.get("CloseAsk", 0)),
                        "Volume": c.get("Volume", 0),
                        "Date": c.get("Time", ""),
                    })
                df = pd.DataFrame(rows)
                df["Date"] = pd.to_datetime(df["Date"])
                df = df.set_index("Date")
                df = df[df["Close"] > 0].dropna()
                return df
    except Exception as e:
        pass
    return None


def saxo_get_positions(token):
    """Get all open positions from Saxo."""
    url = SIM_BASE + "port/v1/positions/me"
    params = {"FieldGroups": "PositionBase,PositionView,DisplayAndFormat"}
    r = requests.get(url, headers=saxo_headers(token), params=params, timeout=10)
    if r.status_code == 200:
        return r.json().get("Data", [])
    return []


def saxo_get_balance(token):
    """Get account balance from Saxo."""
    r = requests.get(SIM_BASE + "port/v1/balances/me",
                     headers=saxo_headers(token), timeout=10)
    if r.status_code == 200:
        return r.json()
    return {}


# ══════════════════════════════════════════════════════════════════
# Instrument map: Yahoo ticker -> Saxo UIC
# ══════════════════════════════════════════════════════════════════

INSTRUMENT_MAP_FILE = os.path.join(BASE_DIR, "data", "instrument_map.csv")


def load_instrument_map():
    """Load ticker -> UIC mapping."""
    mapping = {}
    if os.path.exists(INSTRUMENT_MAP_FILE):
        import csv
        with open(INSTRUMENT_MAP_FILE) as f:
            reader = csv.DictReader(f)
            for row in reader:
                ticker = row.get("ticker", row.get("Ticker", ""))
                uic = row.get("uic", row.get("Uic", row.get("UIC", "")))
                if ticker and uic:
                    mapping[ticker] = int(uic)
    return mapping


# ══════════════════════════════════════════════════════════════════
# Data source: Saxo first, Yahoo fallback
# ══════════════════════════════════════════════════════════════════

def download_data(tickers, token, instrument_map):
    """
    Download data using Saxo API first (real-time).
    Falls back to Yahoo Finance for tickers without a UIC mapping.
    """
    result = {}
    saxo_hits = 0
    yahoo_hits = 0
    failures = []

    # Try Saxo API first for tickers we have UICs for
    for ticker in tickers:
        uic = instrument_map.get(ticker)
        if uic:
            df = saxo_get_historical(token, uic)
            if df is not None and len(df) >= 55:
                result[ticker] = df
                saxo_hits += 1
                continue

        # Fallback to Yahoo Finance
        try:
            import yfinance as yf
            yf_data = yf.download(ticker, period="300d", progress=False, auto_adjust=True)
            if yf_data is not None and len(yf_data) >= 55:
                # Fix: yfinance now returns MultiIndex columns — flatten them
                if isinstance(yf_data.columns, pd.MultiIndex):
                    yf_data.columns = yf_data.columns.get_level_values(0)
                result[ticker] = yf_data
                yahoo_hits += 1
                continue
        except Exception:
            pass

        failures.append(ticker)

    print(f"  Data: {saxo_hits} from Saxo (real-time), {yahoo_hits} from Yahoo, {len(failures)} failed")
    if failures:
        print(f"  Failed: {', '.join(failures[:10])}")

    return result


# ══════════════════════════════════════════════════════════════════
# V3 Consensus Scan
# ══════════════════════════════════════════════════════════════════

def run_scan(tickers, token, instrument_map, market_group="OMX30"):
    """Run v3 consensus scan on given tickers."""
    print(f"\n  Downloading data for {len(tickers)} tickers...")
    data = download_data(tickers, token, instrument_map)

    results = []
    print(f"\n  Running 6-strategy consensus engine on {len(data)} stocks...\n")
    print(f"  {'Ticker':15s} | {'Price':>10s} | {'Chg%':>7s} | {'Signal':8s} | {'Agree':>5s} | {'Regime':12s}")
    print(f"  {'-'*15}-+-{'-'*10}-+-{'-'*7}-+-{'-'*8}-+-{'-'*5}-+-{'-'*12}")

    for ticker, df in sorted(data.items()):
        try:
            df = add_all(df)
            mg = market_group
            # Auto-detect market group
            if ticker.endswith(".ST"):
                mg = "OMX30"
            elif ticker.endswith(".DE"):
                mg = "DAX40"
            elif ticker.endswith(".AS"):
                mg = "US Equities"  # Prosus
            elif not ticker.endswith((".ST", ".DE", ".AS", ".CO")):
                mg = "US Equities"

            decision = consensus_evaluate(df, mg, min_agreement=2)

            current_price = df.iloc[-1]["Close"]
            prev_price = df.iloc[-2]["Close"]
            change_pct = (current_price - prev_price) / prev_price * 100

            # Get real-time price from Saxo if available
            uic = instrument_map.get(ticker)
            rt_price = None
            if uic:
                quote = saxo_get_quote(token, uic)
                if quote:
                    qt = quote.get("Quote", {})
                    rt_price = qt.get("Mid", qt.get("Ask", qt.get("Bid", None)))

            display_price = rt_price if rt_price else current_price

            icon = {"BUY": "BUY ", "SELL": "SELL", "EXIT": "EXIT", "HOLD": "HOLD"}
            signal = icon.get(decision.final_action, "HOLD")

            print(f"  {ticker:15s} | {display_price:>10.2f} | {change_pct:>+6.2f}% | {signal:8s} | "
                  f"{decision.agreement_count}/{decision.total_strategies:1d}   | {decision.regime:12s}")

            results.append({
                "ticker": ticker,
                "price": display_price,
                "rt_price": rt_price,
                "change_pct": change_pct,
                "action": decision.final_action,
                "agreement": decision.agreement_count,
                "total": decision.total_strategies,
                "confidence": decision.combined_confidence,
                "regime": decision.regime,
                "detector_score": decision.detector_score,
                "breakdown": decision.strategy_breakdown,
            })
        except Exception as e:
            print(f"  {ticker:15s} | ERROR: {e}")

    return results


# ══════════════════════════════════════════════════════════════════
# Dashboard launcher
# ══════════════════════════════════════════════════════════════════

def start_dashboard():
    """Start the ATOS dashboard server."""
    dashboard_script = os.path.join(BASE_DIR, "atos_server.py")
    if not os.path.exists(dashboard_script):
        print("  [WARN] atos_server.py not found")
        return

    print("\n  Starting dashboard at http://localhost:8070 ...")
    try:
        proc = subprocess.Popen(
            [sys.executable, "-X", "utf8", dashboard_script],
            cwd=BASE_DIR,
            creationflags=0x00000008,  # DETACHED_PROCESS on Windows
        )
        print(f"  Dashboard running (PID {proc.pid})")
        print("  Open http://localhost:8070 in your browser")
    except Exception as e:
        print(f"  [ERROR] Could not start dashboard: {e}")
        print(f"  Run manually: py -3 -X utf8 atos_server.py")


# ══════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="ATOS v3 — Algorithmic Trading Operating System")
    parser.add_argument("--scan-only", action="store_true", help="Run scan without starting dashboard")
    parser.add_argument("--dashboard", action="store_true", help="Only start the dashboard")
    parser.add_argument("--market", default="OMX30", choices=["OMX30", "DAX40", "US", "ALL"],
                        help="Which market to scan (default: OMX30)")
    args = parser.parse_args()

    print()
    print("=" * 70)
    print("  ATOS v3 — Algorithmic Trading Operating System")
    print("  6 Strategies | Consensus Voting | Real-time Saxo Prices")
    print("=" * 70)
    now = datetime.now()
    print(f"  Started: {now.strftime('%Y-%m-%d %H:%M:%S')}")

    # Step 1: Check token
    print(f"\n[1/4] Checking Saxo SIM token...")
    token = load_token()

    # Step 2: Account info
    print(f"\n[2/4] Loading account data...")
    balance = saxo_get_balance(token)
    positions = saxo_get_positions(token)

    cash = balance.get("CashBalance", "N/A")
    total = balance.get("TotalValue", "N/A")
    currency = balance.get("Currency", "EUR")
    print(f"  Cash: {cash} {currency} | Total: {total} {currency} | Positions: {len(positions)}")

    if positions:
        print(f"\n  Open Positions:")
        for p in positions:
            pbase = p.get("PositionBase", {})
            pview = p.get("PositionView", {})
            disp = p.get("DisplayAndFormat", {})
            sym = disp.get("Symbol", "?")
            desc = disp.get("Description", "?")
            amt = pbase.get("Amount", 0)
            opn = pbase.get("OpenPrice", 0)
            pnl = pview.get("ProfitLossOnTrade", 0)
            print(f"    {sym:12s} | {desc:30s} | {amt} shares @ {opn} | P&L: {pnl}")

    # Dashboard only mode
    if args.dashboard:
        start_dashboard()
        return

    # Step 3: Load instrument map
    print(f"\n[3/4] Loading instrument map...")
    instrument_map = load_instrument_map()
    print(f"  {len(instrument_map)} tickers mapped to Saxo UICs")

    # Select market
    if args.market == "OMX30":
        tickers = OMX30_TICKERS
    elif args.market == "DAX40":
        tickers = DAX40_TICKERS
    elif args.market == "US":
        tickers = US_TICKERS[:15]  # Top 15 US for speed
    else:
        tickers = list(ATOS_UNIVERSE.keys())

    # Step 4: Run scan
    print(f"\n[4/4] Running v3 consensus scan — {args.market} ({len(tickers)} stocks)")
    results = run_scan(tickers, token, instrument_map, args.market)

    # Summary
    buys = [r for r in results if r["action"] == "BUY"]
    sells = [r for r in results if r["action"] in ("SELL", "EXIT")]
    holds = [r for r in results if r["action"] == "HOLD"]

    print(f"\n  {'='*60}")
    print(f"  SUMMARY: {len(buys)} BUY | {len(sells)} SELL | {len(holds)} HOLD | {len(results)} Total")
    print(f"  {'='*60}")

    if buys:
        print(f"\n  BUY OPPORTUNITIES:")
        for b in sorted(buys, key=lambda x: x["agreement"], reverse=True):
            print(f"    {b['ticker']:15s} @ {b['price']:.2f} | "
                  f"{b['agreement']}/{b['total']} agree | conf: {b['confidence']:.0%}")
            for name, info in b["breakdown"].items():
                marker = "+" if info["signal"] == "BUY" else "-" if info["signal"] in ("SELL", "EXIT") else " "
                print(f"      [{marker}] {name}: {info['signal']}")

    # Start dashboard unless --scan-only
    if not args.scan_only:
        start_dashboard()

    print(f"\n  Done at {datetime.now().strftime('%H:%M:%S')}")
    print(f"  Token expires in ~{int((float(json.load(open(TOKEN_FILE)).get('obtained_at',0)) + 1200 - time.time()) / 60)} min")
    print()


if __name__ == "__main__":
    main()
