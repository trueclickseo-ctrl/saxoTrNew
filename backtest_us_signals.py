"""
backtest_us_signals.py
-----------------------
Backtest all 4 USA Strategy signals (US SMA Crossover, US RSI Reversal,
US Momentum, US Ensemble) against all 424 ATOS tickers.

Uses Yahoo Finance daily bars (historical/backtest-only per Saxo-Only-Live-Prices rule).
No Saxo, no orders, no state mutations.

Usage:
    python backtest_us_signals.py               # all 424 tickers, 3 years
    python backtest_us_signals.py --years 5     # longer lookback
    python backtest_us_signals.py --tickers AAPL,MSFT,NVDA  # subset
    python backtest_us_signals.py --strategy "US Momentum"  # one strategy only
    python backtest_us_signals.py --report      # print last saved results

Results saved to: data/backtest_us_signals_<YYYYMMDD>.json
                  data/backtest_us_signals_latest.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import numpy as np
import pandas as pd
import yfinance as yf

from atos.universe import ATOS_UNIVERSE
from atos.features import add_all
from atos.us_signals import (
    get_entry_signals,
    compute_stop,
    should_exit,
    ALL_SIGNAL_STRATEGY_NAMES,
    MAX_HOLD_DAYS,
    HARD_STOP_PCT,
)

DATA_DIR = os.path.join(BASE_DIR, "data")
LATEST_JSON = os.path.join(DATA_DIR, "backtest_us_signals_latest.json")

# ── Backtest parameters ────────────────────────────────────────────────────────

DEFAULT_YEARS = 3
MIN_BARS = 220          # need 200-bar SMA + some runway
RISK_PER_TRADE = 500.0  # fixed notional risk (USD) per trade for P&L calc
TP_MULT = 2.0           # take-profit at 2× initial stop distance


# ── Data fetching ─────────────────────────────────────────────────────────────

def _fetch(ticker: str, years: int) -> pd.DataFrame | None:
    """Download daily OHLCV bars from Yahoo Finance. Returns None on failure."""
    try:
        start = (date.today() - timedelta(days=int(years * 365.25))).isoformat()
        df = yf.download(ticker, start=start, interval="1d",
                         progress=False, auto_adjust=True)
        if df.empty or len(df) < MIN_BARS:
            return None
        # Flatten multi-level columns when downloading a single ticker
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0] for c in df.columns]
        df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
        if len(df) < MIN_BARS:
            return None
        return df
    except Exception:
        return None


def _build_features(df: pd.DataFrame) -> pd.DataFrame:
    return add_all(df)


# ── Single-ticker backtest ────────────────────────────────────────────────────

def _backtest_ticker(ticker: str, df: pd.DataFrame,
                     strategy_filter: str | None) -> list[dict]:
    """
    Walk forward bar-by-bar, generate signals, simulate entries/exits.
    Returns a list of closed trade dicts.
    """
    trades: list[dict] = []
    open_positions: dict[str, dict] = {}  # key = strategy_name

    feat = _build_features(df)
    dates = feat.index.tolist()
    n = len(dates)

    for i in range(MIN_BARS, n):
        bar_date = dates[i]
        close_price = float(feat["Close"].iloc[i])
        hist = feat.iloc[: i + 1]

        # ── Exit check for open positions ──────────────────────────────────
        to_close: list[str] = []
        for strat, pos in list(open_positions.items()):
            exit_flag, exit_reason = should_exit(
                {
                    "strategy": strat,
                    "ticker": ticker,
                    "entry_date": pos["entry_date"],
                    "stop_price": pos["stop_price"],
                },
                hist,
                close_price,
            )
            if exit_flag:
                ep = pos["entry_price"]
                sp = pos["stop_price"]
                risk = ep - sp  # per share
                if risk <= 0:
                    risk = ep * HARD_STOP_PCT

                tp = ep + TP_MULT * risk  # simple fixed TP
                # Determine exit price:  stop / TP / time-based (close)
                if close_price <= sp:
                    exit_price = sp
                elif "SELL" in exit_reason or "time" in exit_reason:
                    exit_price = close_price
                else:
                    exit_price = close_price

                r_multiple = (exit_price - ep) / risk if risk > 0 else 0.0
                pnl_usd = r_multiple * RISK_PER_TRADE

                trades.append({
                    "ticker": ticker,
                    "strategy": strat,
                    "entry_date": pos["entry_date"],
                    "exit_date": bar_date.strftime("%Y-%m-%d") if hasattr(bar_date, "strftime") else str(bar_date)[:10],
                    "entry_price": round(ep, 4),
                    "exit_price": round(exit_price, 4),
                    "stop_price": round(sp, 4),
                    "r_multiple": round(r_multiple, 4),
                    "pnl_usd": round(pnl_usd, 2),
                    "exit_reason": exit_reason,
                })
                to_close.append(strat)

        for strat in to_close:
            del open_positions[strat]

        # ── Entry check ────────────────────────────────────────────────────
        signals = get_entry_signals(ticker, hist)
        for sig in signals:
            strat = sig["strategy_name"]
            if strategy_filter and strat != strategy_filter:
                continue
            if strat in open_positions:
                continue  # already in this strategy
            ep = close_price
            sp = compute_stop(hist, ep)
            entry_date = bar_date.strftime("%Y-%m-%d") if hasattr(bar_date, "strftime") else str(bar_date)[:10]
            open_positions[strat] = {
                "entry_price": ep,
                "stop_price": sp,
                "entry_date": entry_date,
            }

    # Close any still-open positions at last bar close
    last_price = float(feat["Close"].iloc[-1])
    last_date = dates[-1]
    last_date_str = last_date.strftime("%Y-%m-%d") if hasattr(last_date, "strftime") else str(last_date)[:10]
    for strat, pos in open_positions.items():
        ep = pos["entry_price"]
        sp = pos["stop_price"]
        risk = ep - sp
        if risk <= 0:
            risk = ep * HARD_STOP_PCT
        r_multiple = (last_price - ep) / risk if risk > 0 else 0.0
        trades.append({
            "ticker": ticker,
            "strategy": strat,
            "entry_date": pos["entry_date"],
            "exit_date": last_date_str,
            "entry_price": round(ep, 4),
            "exit_price": round(last_price, 4),
            "stop_price": round(sp, 4),
            "r_multiple": round(r_multiple, 4),
            "pnl_usd": round(r_multiple * RISK_PER_TRADE, 2),
            "exit_reason": "open at end of backtest",
        })

    return trades


# ── Aggregation ───────────────────────────────────────────────────────────────

def _aggregate(all_trades: list[dict]) -> dict:
    by_strategy: dict[str, dict] = {
        s: {"wins": 0, "losses": 0, "gross_win": 0.0, "gross_loss": 0.0,
            "r_multiples": [], "best_ticker": None, "worst_ticker": None}
        for s in ALL_SIGNAL_STRATEGY_NAMES
    }
    by_ticker_strategy: dict[tuple, list] = defaultdict(list)

    for t in all_trades:
        s = t["strategy"]
        pnl = t["pnl_usd"]
        r = t["r_multiple"]
        if s not in by_strategy:
            continue
        d = by_strategy[s]
        d["r_multiples"].append(r)
        if pnl > 0:
            d["wins"] += 1
            d["gross_win"] += pnl
        else:
            d["losses"] += 1
            d["gross_loss"] += abs(pnl)
        by_ticker_strategy[(t["ticker"], s)].append(pnl)

    # Per-strategy summary
    summaries: list[dict] = []
    for s, d in by_strategy.items():
        n = d["wins"] + d["losses"]
        wr = round(d["wins"] / n * 100, 1) if n > 0 else None
        pf = round(d["gross_win"] / d["gross_loss"], 2) if d["gross_loss"] > 0 else None
        avg_r = round(float(np.mean(d["r_multiples"])), 4) if d["r_multiples"] else None
        # Best and worst tickers
        ticker_net = {
            k[0]: sum(v) for k, v in by_ticker_strategy.items() if k[1] == s
        }
        best = max(ticker_net, key=ticker_net.get) if ticker_net else None
        worst = min(ticker_net, key=ticker_net.get) if ticker_net else None
        summaries.append({
            "strategy": s,
            "n_trades": n,
            "wins": d["wins"],
            "losses": d["losses"],
            "win_rate_pct": wr,
            "profit_factor": pf,
            "avg_r_multiple": avg_r,
            "net_pnl_usd": round(d["gross_win"] - d["gross_loss"], 2),
            "best_ticker": best,
            "worst_ticker": worst,
        })

    # Per-ticker breakdown (top 10 by net P&L per strategy)
    top_tickers: dict[str, list] = {}
    for s in ALL_SIGNAL_STRATEGY_NAMES:
        ticker_net = {
            k[0]: round(sum(v), 2)
            for k, v in by_ticker_strategy.items() if k[1] == s
        }
        sorted_tickers = sorted(ticker_net.items(), key=lambda x: x[1], reverse=True)
        top_tickers[s] = [{"ticker": k, "net_pnl_usd": v} for k, v in sorted_tickers[:10]]

    return {
        "run_date": date.today().isoformat(),
        "n_tickers_attempted": None,  # filled by caller
        "n_tickers_with_data": None,
        "n_total_trades": len(all_trades),
        "strategy_summary": summaries,
        "top_tickers_per_strategy": top_tickers,
    }


# ── Report display ────────────────────────────────────────────────────────────

def _print_report(result: dict) -> None:
    print(f"\n{'='*70}")
    print(f"  US Signals Backtest — {result.get('run_date', '')}")
    print(f"  Tickers: {result['n_tickers_with_data']}/{result['n_tickers_attempted']} with data  |  Total trades: {result['n_total_trades']}")
    print(f"{'='*70}")
    for s in result["strategy_summary"]:
        wr = f"{s['win_rate_pct']}%" if s["win_rate_pct"] is not None else "—"
        pf = f"{s['profit_factor']:.2f}" if s["profit_factor"] is not None else "—"
        ar = f"{s['avg_r_multiple']:+.3f}R" if s["avg_r_multiple"] is not None else "—"
        print(
            f"  {s['strategy']:<22}  n={s['n_trades']:>4}  WR={wr:>6}  PF={pf:>5}  "
            f"avgR={ar:>8}  net=${s['net_pnl_usd']:>10,.0f}"
        )
    print(f"{'='*70}")
    print("\n  Top tickers per strategy (by net P&L):")
    for strat, tickers in result.get("top_tickers_per_strategy", {}).items():
        if not tickers:
            continue
        top3 = ", ".join(f"{t['ticker']}(${t['net_pnl_usd']:+,.0f})" for t in tickers[:3])
        print(f"    {strat:<22}: {top3}")
    print()


# ── Main ──────────────────────────────────────────────────────────────────────

def run_backtest(tickers: list[str], years: int,
                 strategy_filter: str | None) -> dict:
    all_trades: list[dict] = []
    n_with_data = 0
    n = len(tickers)

    print(f"Backtesting {n} tickers over {years} year(s)…")
    for idx, ticker in enumerate(tickers, 1):
        if idx % 50 == 0 or idx == n:
            print(f"  {idx}/{n} - {ticker} ...")
        df = _fetch(ticker, years)
        if df is None:
            continue
        n_with_data += 1
        try:
            trades = _backtest_ticker(ticker, df, strategy_filter)
            all_trades.extend(trades)
        except Exception as exc:
            print(f"  [SKIP] {ticker}: {exc}")

    result = _aggregate(all_trades)
    result["n_tickers_attempted"] = n
    result["n_tickers_with_data"] = n_with_data
    result["years"] = years
    result["strategy_filter"] = strategy_filter
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description="Backtest USA Strategy signals on ATOS universe")
    ap.add_argument("--years", type=int, default=DEFAULT_YEARS)
    ap.add_argument("--tickers", type=str, default="",
                    help="Comma-separated subset; default = all 424")
    ap.add_argument("--strategy", type=str, default="",
                    help="Limit to one strategy name (e.g. 'US Momentum')")
    ap.add_argument("--report", action="store_true",
                    help="Print last saved results instead of running")
    args = ap.parse_args()

    if args.report:
        if not os.path.exists(LATEST_JSON):
            print("No saved results. Run without --report first.")
            sys.exit(1)
        with open(LATEST_JSON, encoding="utf-8") as f:
            _print_report(json.load(f))
        return

    tickers = (
        [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
        if args.tickers else list(ATOS_UNIVERSE)
    )
    strategy_filter = args.strategy.strip() or None
    if strategy_filter and strategy_filter not in ALL_SIGNAL_STRATEGY_NAMES:
        print(f"Unknown strategy: {strategy_filter}")
        print(f"Valid: {ALL_SIGNAL_STRATEGY_NAMES}")
        sys.exit(1)

    result = run_backtest(tickers, args.years, strategy_filter)
    _print_report(result)

    os.makedirs(DATA_DIR, exist_ok=True)
    dated = os.path.join(DATA_DIR, f"backtest_us_signals_{date.today().strftime('%Y%m%d')}.json")
    for path in (dated, LATEST_JSON):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, default=str)
    print(f"Saved -> {dated}")
    print(f"       -> {LATEST_JSON}")


if __name__ == "__main__":
    main()
