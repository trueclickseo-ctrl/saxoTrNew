"""
run_reversion_v2_now.py
------------------------
One-shot: scan for US Reversion V2 signals right now and place SIM entries.

Usage:
    python run_reversion_v2_now.py            # scan + buy on Saxo SIM
    python run_reversion_v2_now.py --scan-only # scan only, no orders
"""
import sys, os, argparse
_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)

import numpy as np
import pandas as pd
import yfinance as yf
from datetime import date, timedelta

from atos.universe import US_TICKERS
import atos.us_reversion_v2 as USR2
import atos.capital_config as CAP

LOOKBACK = 280   # trading days (need 220 for EMA200 + warmup)

parser = argparse.ArgumentParser()
parser.add_argument("--scan-only", action="store_true", help="Print signals, do not place orders")
args = parser.parse_args()

print("=" * 62)
print("  US REVERSION V2 — live scan")
print(f"  Date: {date.today()}  |  Universe: {len(US_TICKERS)} stocks")
print("=" * 62)

# ── Download data ────────────────────────────────────────────────
all_tickers = list(US_TICKERS) + ["SPY"]
start = (date.today() - timedelta(days=LOOKBACK + 90)).isoformat()
print(f"\n  Downloading {len(all_tickers)} tickers from {start}... ", end="", flush=True)

raw = yf.download(all_tickers, start=start, progress=False, auto_adjust=True, threads=True)
print("done")

feat_data: dict = {}
if isinstance(raw.columns, pd.MultiIndex):
    for t in all_tickers:
        try:
            df = raw.xs(t, axis=1, level=1).dropna(how="all")
            if len(df) >= 50:
                feat_data[t] = df
        except KeyError:
            pass
else:
    if len(raw) >= 50:
        feat_data[all_tickers[0]] = raw

print(f"  {len(feat_data)} tickers with sufficient data\n")

# ── SPY regime check ─────────────────────────────────────────────
spy_df = feat_data.get("SPY")
market_ok = True
spy_price = spy_ema50 = None
if spy_df is not None and "Close" in spy_df.columns:
    close = spy_df["Close"].dropna()
    if len(close) >= 55:
        spy_price = float(close.iloc[-1])
        spy_ema50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1])
        market_ok = spy_price > spy_ema50

regime_str = f"BULL (${spy_price:.2f} > EMA50 ${spy_ema50:.2f})" if market_ok else \
             f"BEAR (${spy_price:.2f} < EMA50 ${spy_ema50:.2f})"
print(f"  SPY Regime : {regime_str}")

# ── Scan ─────────────────────────────────────────────────────────
candidates = USR2.scan(feat_data, US_TICKERS, market_ok=market_ok)
max_slots  = CAP.reversion_slots(len(US_TICKERS))

print(f"  V2 signals : {len(candidates)} found  |  max slots: {max_slots}")
print(f"  Entry gate : RSI<{USR2.RSI_ENTRY}  Dip>={USR2.DIP_PCT*100:.0f}%  "
      f"Vol>={USR2.VOL_MULT}x  EMA200×{USR2.EMA200_MARGIN}  R:R>={USR2.MIN_RR}\n")

if not candidates:
    if not market_ok:
        print("  No entries: SPY is below EMA50 — bear regime filter active.")
    else:
        print("  No signals today.")
    sys.exit(0)

print(f"  {'Ticker':<8}  {'Price':>7}  {'RSI':>5}  {'Dip%':>6}  {'Vol×':>5}  {'R:R':>5}  Score")
print("  " + "-" * 62)
for i, h in enumerate(candidates):
    flag = "  <-- BUY" if i < max_slots else ""
    print(f"  {h['ticker']:<8}  ${h['price']:>6.2f}  {h['rsi']:>5.1f}  "
          f"{h['dip_pct']:>5.1f}%  {h['vol_ratio']:>4.1f}x  {h['rr_ratio']:>4.1f}  "
          f"{h['score']:.4f}{flag}")

print(f"\n  Top {min(max_slots, len(candidates))} would be bought (slots available: {max_slots})")

if args.scan_only:
    print("\n  [SCAN ONLY] Pass without --scan-only to place SIM orders.")
    sys.exit(0)

# ── Execute on Saxo SIM ──────────────────────────────────────────
print("\n  Placing SIM entries via Saxo...")
try:
    import atos.database as db
    import saxo_client
    from atos_runner import (
        run_us_reversion_v2, _rate_to_sek, commission_sek,
        _stocks_paper_fill_enabled, _confirm_stock_fill, _append_trade_log,
        _sx, STOCKS_SIM_PAPER_FILL_ON_REJECT,
    )
    db.init_db()
    open_trades = db.get_open_trades()
    todays_actions: list = []

    try:
        balances = saxo_client.get_balances()
        ccy = balances.get("Currency", "EUR")
        cash_sek = balances.get("CashBalance", 0) * _rate_to_sek(ccy)
    except Exception as e:
        print(f"  [WARN] Could not fetch Saxo balances ({e}) — using fallback sleeve")
        cash_sek = 0.0

    rev_budget = cash_sek * CAP.reversion_allocation_pct() if cash_sek > 0 else 0.0
    run_us_reversion_v2(feat_data, open_trades, todays_actions,
                        available_cash_sek=rev_budget)

    buys   = [a for a in todays_actions if a.get("action") == "BUY"]
    exits  = [a for a in todays_actions if a.get("action") == "SELL"]
    print(f"\n  Done — {len(buys)} BUY, {len(exits)} EXIT")
    for a in buys:
        pnl_str = ""
        print(f"    BUY  {a['ticker']}  {a['shares']}sh @ ${a['price']:.2f}  [{a['reason']}]")
    for a in exits:
        pnl = a.get('pnl_sek', 0) or 0
        print(f"    EXIT {a['ticker']}  {a['shares']}sh  P&L: {pnl:+,.0f} SEK  [{a['reason']}]")

except Exception as exc:
    print(f"\n  ERROR during SIM execution: {exc}")
    import traceback; traceback.print_exc()
    sys.exit(1)
