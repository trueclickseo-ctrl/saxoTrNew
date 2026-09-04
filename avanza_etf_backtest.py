"""
avanza_etf_backtest.py
----------------------
Step 1 — Discover which ETFs from our existing strategies are available on Avanza.
Step 2 — Backtest them with dual_ma + sector_rotation logic using Yahoo Finance data.

Usage:
  python avanza_etf_backtest.py --discover     # auth to Avanza, cache universe
  python avanza_etf_backtest.py --backtest     # Yahoo data + strategy analysis (no Avanza auth)
  python avanza_etf_backtest.py --all          # discover then backtest

Env:  .env.avanza must be loaded for --discover.
Data: all price history from Yahoo Finance (yfinance), not Avanza API.
Output:
  data/avanza_etf_universe.json   -- discovery cache
  data/avanza_etf_backtest.csv    -- per-ticker backtest results
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

BASE = Path(__file__).parent
UNIVERSE_PATH = BASE / "data" / "avanza_etf_universe.json"
RESULTS_PATH  = BASE / "data" / "avanza_etf_backtest.csv"

# ── Known tickers from all 4 ETF strategies (union) ──────────────────────────

DUAL_MA        = ["SPY","QQQ","IWM","GLD","SMH","SOXX","LQD","EWY","TLT","GDX",
                  "HYG","DIA","XLF","XLE","XLV","XBI","RSP","XLK","VTI","EEM"]
SECTOR_ROT     = ["XLC","XLK","XLV","XLE","XLF","XLI","XLY","XLP","XLU","XLRE","XLB"]
MEAN_REVERSION = ["SPY","QQQ","IWM","EFA","EEM"]
RISK_OFF       = ["SPY","QQQ","TLT","GLD"]

ALL_TICKERS = sorted({*DUAL_MA, *SECTOR_ROT, *MEAN_REVERSION, *RISK_OFF})

STRATEGIES = {
    "dual_ma":        set(DUAL_MA),
    "sector_rot":     set(SECTOR_ROT),
    "mean_reversion": set(MEAN_REVERSION),
    "risk_off":       set(RISK_OFF),
}

# ── Backtest parameters (mirror live ETF config) ──────────────────────────────

FAST_MA    = 20
SLOW_MA    = 100
STOP_LOSS  = 0.08
TAKE_PROFIT = 0.20
COMMISSION = 0.0008
YEARS      = 10


# ─────────────────────────────────────────────────────────────────────────────
# STEP 1: DISCOVER
# ─────────────────────────────────────────────────────────────────────────────

def _load_avanza_env():
    env_path = BASE / ".env.avanza"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def _exact_etf_match(hits: list[dict], ticker: str) -> dict | None:
    """Return the hit whose title contains exactly (TICKER), flagCode=US, currency=USD.
    Falls back to any USD/NYSE/NASDAQ hit if no perfect match."""
    exact = [h for h in hits
             if f"({ticker})" in h.get("title", "")
             and h.get("flagCode", "") == "US"
             and (h.get("price") or {}).get("currency", "") == "USD"]
    if exact:
        return exact[0]
    # Fallback: USD + major US exchange
    us_usd = [h for h in hits
              if (h.get("price") or {}).get("currency", "") == "USD"
              and h.get("flagCode", "") == "US"]
    return us_usd[0] if us_usd else None


def discover(tickers: list[str] | None = None) -> dict:
    """Auth to Avanza, search each ticker as EXCHANGE_TRADED_FUND, cache results."""
    _load_avanza_env()

    from avanza_module.avanza_client import get_client
    from avanza.constants import InstrumentType

    print("Authenticating to Avanza...")
    client = get_client()
    print("Authenticated.\n")

    universe = {}
    to_check = tickers or ALL_TICKERS

    for ticker in to_check:
        try:
            hits = client.search_for_instrument(
                InstrumentType.EXCHANGE_TRADED_FUND, ticker, limit=8
            )
        except Exception:
            hits = []

        match = _exact_etf_match(hits, ticker)

        if match:
            ob_id = match.get("orderBookId", "")
            name  = match.get("title", "").rsplit("(", 1)[0].strip()
            mkt   = match.get("marketPlaceName", "")
            ccy   = (match.get("price") or {}).get("currency", "")
            universe[ticker] = {
                "ticker":    ticker,
                "avanza_id": ob_id,
                "name":      name,
                "currency":  ccy,
                "market":    mkt,
                "available": True,
            }
            status = f"FOUND  id={ob_id:<10} {name[:38]}  [{mkt} {ccy}]"
        else:
            universe[ticker] = {"ticker": ticker, "available": False}
            status = "NOT FOUND"

        print(f"  {ticker:<8} {status}")
        time.sleep(0.3)

    UNIVERSE_PATH.write_text(json.dumps(universe, indent=2))
    found = sum(1 for v in universe.values() if v.get("available"))
    print(f"\nDiscovery complete: {found}/{len(to_check)} tickers available on Avanza.")
    print(f"Cached → {UNIVERSE_PATH}")
    return universe


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2: BACKTEST (Yahoo Finance only, no Avanza auth required)
# ─────────────────────────────────────────────────────────────────────────────

def _download(tickers: list[str], years: int = YEARS) -> pd.DataFrame:
    print(f"Downloading {years}y of daily closes for {len(tickers)} tickers from Yahoo...")
    raw = yf.download(tickers, period=f"{years}y", auto_adjust=True, progress=False)
    close = raw["Close"] if "Close" in raw.columns else raw
    if isinstance(close.columns, pd.MultiIndex):
        close.columns = close.columns.get_level_values(0)
    close = close[[t for t in tickers if t in close.columns]].dropna(how="all")
    print(f"  Got {len(close)} trading days  ({close.index[0].date()} → {close.index[-1].date()})")
    return close


def _per_ticker_backtest(close: pd.Series, ticker: str) -> dict:
    """
    Simulate dual_ma strategy on a single ticker:
      Enter: 20d MA crosses above 100d MA
      Exit:  stop-loss -8% OR take-profit +20% OR MA crosses back below
    Returns metrics dict.
    """
    prices = close.dropna().values
    dates  = close.dropna().index
    n = len(prices)
    if n < SLOW_MA + 10:
        return {"ticker": ticker, "error": "insufficient history"}

    fast = pd.Series(prices).rolling(FAST_MA).mean().values
    slow = pd.Series(prices).rolling(SLOW_MA).mean().values

    in_trade = False
    entry_price = 0.0
    trades = []
    equity = 1.0
    equity_curve = []

    for i in range(SLOW_MA, n):
        p = prices[i]
        if np.isnan(fast[i]) or np.isnan(slow[i]):
            equity_curve.append(equity)
            continue

        if in_trade:
            ret = p / entry_price - 1.0
            exit_reason = None
            if ret <= -STOP_LOSS:
                exit_reason = "SL"
            elif ret >= TAKE_PROFIT:
                exit_reason = "TP"
            elif fast[i] < slow[i]:
                exit_reason = "MA_cross"

            if exit_reason:
                net = ret - COMMISSION * 2  # round-trip commission
                equity *= (1 + net)
                trades.append({"ret": ret, "net": net, "reason": exit_reason,
                                "entry_date": str(dates[entry_idx].date()),
                                "exit_date": str(dates[i].date())})
                in_trade = False
        else:
            if fast[i] > slow[i] and (i == SLOW_MA or fast[i - 1] <= slow[i - 1]):
                entry_price = p * (1 + COMMISSION)  # buy cost
                entry_idx   = i
                in_trade    = True

        equity_curve.append(equity)

    if not trades:
        return {"ticker": ticker, "n_trades": 0, "note": "no signals in period"}

    rets    = [t["net"] for t in trades]
    wins    = [r for r in rets if r > 0]
    losses  = [r for r in rets if r <= 0]
    win_rate = len(wins) / len(rets) * 100

    gross_profit = sum(wins) if wins else 0
    gross_loss   = abs(sum(losses)) if losses else 0.0001
    pf = gross_profit / gross_loss

    # Annualised return from equity curve
    eq = np.array(equity_curve)
    total_ret = eq[-1] - 1.0
    years_held = len(eq) / 252
    ann_ret = (eq[-1] ** (1 / years_held) - 1) * 100 if years_held > 0 and eq[-1] > 0 else 0.0

    # Max drawdown on equity curve
    roll_max = np.maximum.accumulate(eq)
    dd       = (eq - roll_max) / roll_max
    max_dd   = dd.min() * 100

    # Current MA signal
    cur_signal = "BUY" if fast[-1] > slow[-1] else "—"
    cross_str  = f"{fast[-1]:.2f} / {slow[-1]:.2f}" if not np.isnan(fast[-1]) else "n/a"

    # 3-month return (for sector rotation scoring)
    ret_3m = (prices[-1] / prices[max(-63, -n)] - 1) * 100

    return {
        "ticker":      ticker,
        "n_trades":    len(trades),
        "win_rate":    round(win_rate, 1),
        "profit_factor": round(pf, 2),
        "avg_ret_pct": round(sum(rets) / len(rets) * 100, 2),
        "ann_return_pct": round(ann_ret, 1),
        "max_dd_pct":  round(max_dd, 1),
        "total_ret_pct": round(total_ret * 100, 1),
        "ret_3m_pct":  round(ret_3m, 1),
        "cur_signal":  cur_signal,
        "ma_fast_slow": cross_str,
    }


def backtest(universe: dict | None = None) -> pd.DataFrame:
    if universe is None:
        if not UNIVERSE_PATH.exists():
            print("No universe cache found. Run with --discover first.")
            sys.exit(1)
        universe = json.loads(UNIVERSE_PATH.read_text())

    avail = {t: v for t, v in universe.items() if v.get("available", False)}
    not_avail = [t for t, v in universe.items() if not v.get("available", False)]

    print(f"\nAvanza universe: {len(avail)} available, {len(not_avail)} not found")
    if not_avail:
        print(f"  Not on Avanza: {', '.join(sorted(not_avail))}")

    tickers = sorted(avail.keys())
    if not tickers:
        print("No available tickers to backtest.")
        return pd.DataFrame()

    close = _download(tickers)
    missing = [t for t in tickers if t not in close.columns]
    if missing:
        print(f"  Yahoo missing data for: {', '.join(missing)}")

    print(f"\nRunning dual_ma backtest on {len(close.columns)} tickers...\n")
    rows = []
    for ticker in close.columns:
        result = _per_ticker_backtest(close[ticker], ticker)
        # Tag which strategies include this ticker
        result["in_dual_ma"]    = "Y" if ticker in STRATEGIES["dual_ma"]    else ""
        result["in_sector_rot"] = "Y" if ticker in STRATEGIES["sector_rot"] else ""
        result["in_mean_rev"]   = "Y" if ticker in STRATEGIES["mean_reversion"] else ""
        result["in_risk_off"]   = "Y" if ticker in STRATEGIES["risk_off"]   else ""
        result["avanza_id"]     = avail.get(ticker, {}).get("avanza_id", "")
        result["avanza_name"]   = avail.get(ticker, {}).get("name", "")[:35]
        rows.append(result)

    df = pd.DataFrame(rows)
    df = df.sort_values("ann_return_pct", ascending=False).reset_index(drop=True)

    RESULTS_PATH.write_text(df.to_csv(index=False))
    print(f"Results saved → {RESULTS_PATH}\n")

    _print_report(df, not_avail)
    return df


def _print_report(df: pd.DataFrame, not_avail: list[str]):
    cols = ["ticker", "n_trades", "win_rate", "profit_factor",
            "avg_ret_pct", "ann_return_pct", "max_dd_pct",
            "ret_3m_pct", "cur_signal", "in_dual_ma", "in_sector_rot"]
    avail_cols = [c for c in cols if c in df.columns]
    valid = df[df["n_trades"].notna() & (df["n_trades"] > 0)].copy()

    print("=" * 90)
    print("AVANZA ETF BACKTEST — Dual MA strategy  (Buy: 20d > 100d MA | SL 8% / TP 20%)")
    print(f"Period: {YEARS} years | Commission: {COMMISSION*100:.2f}% per leg")
    print("=" * 90)

    if not valid.empty:
        print(valid[avail_cols].to_string(index=False))
    else:
        print("(no completed trades in backtest period)")

    print()
    no_sig = df[df["n_trades"] == 0] if "n_trades" in df else pd.DataFrame()
    if not no_sig.empty:
        print(f"Tickers with 0 signals: {', '.join(no_sig['ticker'].tolist())}")

    # Current BUY signals (dual_ma)
    if "cur_signal" in valid.columns and "in_dual_ma" in valid.columns:
        active = valid[(valid["cur_signal"] == "BUY") & (valid["in_dual_ma"] == "Y")]
        if not active.empty:
            print(f"\nCurrent DUAL_MA BUY signals on Avanza: {', '.join(active['ticker'].tolist())}")

    # Top 3 by 3-month return (sector rotation ranking)
    if "ret_3m_pct" in valid.columns and "in_sector_rot" in valid.columns:
        sector = valid[valid["in_sector_rot"] == "Y"].nlargest(3, "ret_3m_pct")
        if not sector.empty:
            print(f"\nTop sector_rot tickers by 3m return:")
            for _, row in sector.iterrows():
                print(f"  {row['ticker']:<8} 3m={row['ret_3m_pct']:+.1f}%  ann={row['ann_return_pct']:+.1f}%")

    print(f"\nNot available on Avanza ({len(not_avail)}): {', '.join(sorted(not_avail))}")
    print()


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Avanza ETF universe discovery + backtest")
    parser.add_argument("--discover",  action="store_true", help="Search Avanza for each ticker (requires .env.avanza)")
    parser.add_argument("--backtest",  action="store_true", help="Run Yahoo backtest on discovered universe")
    parser.add_argument("--all",       action="store_true", help="Discover then backtest")
    parser.add_argument("--tickers",   nargs="+",           help="Override ticker list for --discover")
    args = parser.parse_args()

    if not (args.discover or args.backtest or args.all):
        parser.print_help()
        sys.exit(0)

    universe = None
    if args.discover or args.all:
        universe = discover(args.tickers)

    if args.backtest or args.all:
        backtest(universe)
