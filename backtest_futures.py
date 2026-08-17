"""
backtest_futures.py
-------------------
Walk-forward backtest for the Futures Trend-Following strategy.

Downloads 5 years of futures-equivalent data from Yahoo Finance and simulates
the Donchian Channel (20/10) + ATR(14) stop strategy across 5 markets.

Usage:
    python backtest_futures.py              # single pass (current parameters)
    python backtest_futures.py --grid       # 81-combo parameter grid search
    python backtest_futures.py --summary    # print ranked grid results

ENABLE THRESHOLD (go live when ALL pass):
    Sharpe >= 0.70  AND  Win Rate >= 35%  AND  Max Drawdown < 30%  AND  N >= 30

Note on win rate: trend-following typically wins 35–45% of trades but wins
on size (big trends) and loses small (ATR stop cuts them fast). A 40% win
rate with Sharpe > 0.9 is excellent for this style.
"""

import sys
import os
import argparse
import pickle
import csv
import json

import numpy as np
import pandas as pd
import yfinance as yf
from datetime import date, timedelta
from itertools import product

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)

DATA_DIR   = os.path.join(_ROOT, "data")
CACHE_PKL  = os.path.join(DATA_DIR, "backtest_futures_cache.pkl")
GRID_CSV   = os.path.join(DATA_DIR, "futures_grid_results.csv")

BACKTEST_YEARS = 5
COMMISSION_PCT = 0.0005   # 0.05% per trade (round-trip halved)
INITIAL_EQUITY = 100_000  # USD notional for backtest

# ETF proxies for backtest — clean continuous data without futures roll gaps.
# In production, runner.py trades actual CfdOnFutures on Saxo SIM.
# Correlations: SPY≈ES, QQQ≈NQ, GLD≈GC, USO≈CL, TLT≈ZN (inverse rate proxy).
YF_TICKERS = {
    "ES": "SPY",     # S&P 500 → SPDR S&P 500 ETF
    "NQ": "QQQ",     # NASDAQ-100 → Invesco QQQ ETF
    "GC": "GLD",     # Gold → SPDR Gold Shares
    "CL": "USO",     # Crude Oil → United States Oil Fund
    "ZN": "TLT",     # US Treasuries → iShares 20Y Treasury Bond ETF
}

GRID = {
    "BREAKOUT_PERIOD": [15, 20, 30, 40, 55],
    "EXIT_PERIOD":     [5, 10, 15, 20],
    "ATR_STOP_MULT":   [1.5, 2.0, 2.5],
    "RISK_PCT":        [0.01, 0.015, 0.02],
}

CSV_FIELDS = [
    "BREAKOUT_PERIOD", "EXIT_PERIOD", "ATR_STOP_MULT", "RISK_PCT",
    "sharpe", "win_rate", "max_dd", "cagr", "n_trades", "passed",
]


# ── Data layer ─────────────────────────────────────────────────────────────

def _download() -> dict[str, pd.DataFrame]:
    start = (date.today() - timedelta(days=BACKTEST_YEARS * 365 + 90)).isoformat()
    print(f"Downloading {len(YF_TICKERS)} futures markets from {start}...")
    data = {}
    for sym, ticker in YF_TICKERS.items():
        try:
            df = yf.download(ticker, start=start, auto_adjust=True, progress=False)
            if not df.empty:
                df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
                data[sym] = df[["Open", "High", "Low", "Close", "Volume"]].ffill().dropna()
                print(f"  {sym}: {len(data[sym])} bars")
            else:
                print(f"  {sym}: no data returned")
        except Exception as exc:
            print(f"  {sym}: download failed — {exc}")
    return data


def load_data() -> dict[str, pd.DataFrame]:
    if os.path.exists(CACHE_PKL):
        with open(CACHE_PKL, "rb") as f:
            cached = pickle.load(f)
        if cached.get("date") == date.today().isoformat():
            print("Using cached data (today).")
            return cached["data"]

    data = _download()
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CACHE_PKL, "wb") as f:
        pickle.dump({"date": date.today().isoformat(), "data": data}, f)
    return data


# ── ATR helper ─────────────────────────────────────────────────────────────

def _atr(df: pd.DataFrame, period: int) -> pd.Series:
    prev  = df["Close"].shift(1)
    tr    = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - prev).abs(),
        (df["Low"]  - prev).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


# ── Backtest engine ────────────────────────────────────────────────────────

def run_backtest(
    data: dict[str, pd.DataFrame],
    breakout_period: int = 20,
    exit_period:     int = 10,
    atr_stop_mult:   float = 2.0,
    risk_pct:        float = 0.01,
    max_positions:   int = 5,
    regime_symbol:   str = "ES",
    verbose:         bool = False,
) -> dict:
    """Simulate the Donchian breakout strategy across all markets.

    Returns dict: {sharpe, win_rate, max_dd, cagr, n_trades, trades}
    """
    # Build a unified daily date index across all markets
    all_dates = None
    for sym, df in data.items():
        idx = df.index.normalize()
        all_dates = idx if all_dates is None else all_dates.union(idx)
    all_dates = all_dates.sort_values()

    # Precompute indicators per market
    indicators = {}
    for sym, df in data.items():
        df = df.copy()
        atr_s    = _atr(df, 14)
        # entry channels (shifted so today's close is not included)
        roll_hi20 = df["Close"].shift(1).rolling(breakout_period).max()
        roll_lo20 = df["Close"].shift(1).rolling(breakout_period).min()
        # exit channels (tighter)
        roll_lo10 = df["Close"].shift(1).rolling(exit_period).min()
        roll_hi10 = df["Close"].shift(1).rolling(exit_period).max()
        sma200    = df["Close"].rolling(200).mean()
        indicators[sym] = {
            "close":  df["Close"],
            "atr":    atr_s,
            "high20": roll_hi20,
            "low20":  roll_lo20,
            "low10":  roll_lo10,
            "high10": roll_hi10,
            "sma200": sma200,
        }

    equity     = INITIAL_EQUITY
    positions  = {}   # {symbol: {entry_price, stop_price, qty, entry_date}}
    equity_curve = []
    trades     = []

    for today in all_dates:
        # ── Exit review ──────────────────────────────────────────────────
        for sym in list(positions):
            ind       = indicators.get(sym, {})
            if today not in ind["close"].index:
                continue
            pos       = positions[sym]
            price     = float(ind["close"][today])
            stop      = float(pos.get("stop_price", 0))
            direction = pos.get("direction", "Buy")
            is_long   = direction == "Buy"
            held      = (today.date() - pos["entry_date"]).days if hasattr(pos["entry_date"], "year") else 0
            lo10_raw  = ind.get("low10", pd.Series()).get(today, float("nan"))
            hi10_raw  = ind.get("high10", pd.Series()).get(today, float("nan"))
            lo10      = float(lo10_raw) if not pd.isna(lo10_raw) else 0
            hi10      = float(hi10_raw) if not pd.isna(hi10_raw) else 999999

            reason = None
            if held >= 30:
                reason = "time-stop"
            elif is_long and stop > 0 and price <= stop:
                reason = "atr-stop"
            elif not is_long and stop > 0 and price >= stop:
                reason = "atr-stop"
            elif is_long and lo10 > 0 and price <= lo10:
                reason = "donchian-exit"
            elif not is_long and hi10 > 0 and price >= hi10:
                reason = "donchian-exit"

            if reason:
                # P&L sign: long profits when price rises; short profits when falls
                raw_pnl   = (price - pos["entry_price"]) if is_long else (pos["entry_price"] - price)
                pnl       = raw_pnl * pos["qty"]
                comm      = price * pos["qty"] * COMMISSION_PCT
                equity   += pnl - comm
                pnl_pct   = raw_pnl / pos["entry_price"] * 100
                trades.append({
                    "symbol":    sym,
                    "direction": direction,
                    "entry":     pos["entry_price"],
                    "exit":      price,
                    "qty":       pos["qty"],
                    "pnl":       pnl,
                    "pnl_pct":   pnl_pct,
                    "reason":    reason,
                    "days_held": held,
                })
                if verbose:
                    sign = "+" if pnl >= 0 else ""
                    print(f"  EXIT {direction[0]} {sym:<4}  {today.date()}  "
                          f"{sign}${pnl:.0f} ({pnl_pct:+.1f}%)  {reason}")
                del positions[sym]

        # ── Trail stops ──────────────────────────────────────────────────
        for sym, pos in positions.items():
            ind = indicators.get(sym, {})
            if today not in ind["close"].index:
                continue
            atr_raw = ind["atr"][today]
            if pd.isna(atr_raw):
                continue
            atr_v     = float(atr_raw)
            price     = float(ind["close"][today])
            direction = pos.get("direction", "Buy")
            if atr_v > 0:
                if direction == "Buy":
                    new_stop = price - atr_stop_mult * atr_v
                    pos["stop_price"] = max(pos.get("stop_price", 0), new_stop)
                else:
                    new_stop = price + atr_stop_mult * atr_v
                    cur_stop = pos.get("stop_price", 999999)
                    pos["stop_price"] = min(cur_stop, new_stop)

        # ── Entry signals (long + short) ──────────────────────────────────
        slots = max_positions - len(positions)
        if slots > 0:
            es_in = indicators.get(regime_symbol, {})
            es_risk_off = False
            if es_in and today in es_in.get("close", pd.Series()).index:
                es_close = float(es_in["close"][today])
                sma200_v = es_in["sma200"][today]
                if not pd.isna(sma200_v) and es_close < float(sma200_v):
                    es_risk_off = True

            # Markets that trade long AND short vs long-only
            long_only_markets = {"ES", "NQ", "CL"}   # equity + oil: no shorting
            bidirectional     = {"GC", "ZN"}          # gold + bonds: both directions

            equity_markets = {"ES", "NQ"}
            candidates = []
            for sym, ind in indicators.items():
                if sym in positions:
                    continue
                if today not in ind["close"].index:
                    continue
                price = float(ind["close"][today])
                hi20  = ind["high20"][today]
                lo20  = ind["low20"][today]
                atr_v = ind["atr"][today]
                if pd.isna(atr_v) or atr_v <= 0:
                    continue
                atr_v = float(atr_v)

                if not pd.isna(hi20):
                    hi20 = float(hi20)
                    # Skip equity longs during risk-off (index below SMA200)
                    if price > hi20 and not (es_risk_off and sym in equity_markets):
                        score = (price - hi20) / atr_v
                        candidates.append((score, sym, price, atr_v, "Buy",
                                           price - atr_stop_mult * atr_v))

                # SHORT signals only for bidirectional markets (bonds, gold)
                if sym in bidirectional and not pd.isna(lo20):
                    lo20 = float(lo20)
                    if price < lo20:
                        score = (lo20 - price) / atr_v
                        candidates.append((score, sym, price, atr_v, "Sell",
                                           price + atr_stop_mult * atr_v))

            for score, sym, price, atr_v, direction, stop in \
                    sorted(candidates, reverse=True)[:slots]:
                qty  = max(1, int((equity * risk_pct) / (atr_stop_mult * atr_v)))
                comm = price * qty * COMMISSION_PCT
                equity -= comm

                positions[sym] = {
                    "entry_price": price,
                    "stop_price":  stop,
                    "qty":         qty,
                    "direction":   direction,
                    "entry_date":  today.date(),
                }
                if verbose:
                    print(f"  {direction:<4} {sym:<4}  {today.date()}  "
                          f"@ {price:.4f}  stop={stop:.4f}  qty={qty}  score={score:.3f}")

        equity_curve.append(equity)

    # ── Performance metrics ────────────────────────────────────────────────
    n_trades = len(trades)
    if n_trades == 0:
        return {"sharpe": 0, "win_rate": 0, "max_dd": 0, "cagr": 0,
                "n_trades": 0, "trades": []}

    ec    = pd.Series(equity_curve)
    rets  = ec.pct_change().dropna()
    sharpe = float(rets.mean() / rets.std() * np.sqrt(252)) if rets.std() > 0 else 0.0

    roll_max = ec.cummax()
    dd       = (ec - roll_max) / roll_max
    max_dd   = float(dd.min())

    years    = BACKTEST_YEARS
    cagr     = float((equity / INITIAL_EQUITY) ** (1 / years) - 1) * 100

    wins     = [t for t in trades if t["pnl"] > 0]
    win_rate = len(wins) / n_trades * 100

    return {
        "sharpe":   round(sharpe, 3),
        "win_rate": round(win_rate, 1),
        "max_dd":   round(abs(max_dd) * 100, 1),
        "cagr":     round(cagr, 1),
        "n_trades": n_trades,
        "trades":   trades,
        "final_equity": round(equity, 0),
    }


# ── Grid search ─────────────────────────────────────────────────────────────

def load_done_combos() -> set:
    if not os.path.exists(GRID_CSV):
        return set()
    done = set()
    with open(GRID_CSV) as f:
        for row in csv.DictReader(f):
            done.add((
                int(row["BREAKOUT_PERIOD"]),
                int(row["EXIT_PERIOD"]),
                float(row["ATR_STOP_MULT"]),
                float(row["RISK_PCT"]),
            ))
    return done


def run_grid(data: dict) -> None:
    combos = list(product(
        GRID["BREAKOUT_PERIOD"],
        GRID["EXIT_PERIOD"],
        GRID["ATR_STOP_MULT"],
        GRID["RISK_PCT"],
    ))
    done = load_done_combos()
    remaining = [c for c in combos if c not in done]
    print(f"Grid: {len(combos)} combos, {len(done)} already done, "
          f"{len(remaining)} to run")

    os.makedirs(DATA_DIR, exist_ok=True)
    write_header = not os.path.exists(GRID_CSV)
    with open(GRID_CSV, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if write_header:
            writer.writeheader()

        for i, (bp, ep, mult, risk) in enumerate(remaining):
            res = run_backtest(data, breakout_period=bp, exit_period=ep,
                               atr_stop_mult=mult, risk_pct=risk)
            passed = (res["sharpe"] >= 0.70 and res["win_rate"] >= 35
                      and res["max_dd"] < 30 and res["n_trades"] >= 30)
            row = {
                "BREAKOUT_PERIOD": bp, "EXIT_PERIOD": ep,
                "ATR_STOP_MULT": mult, "RISK_PCT": risk,
                "sharpe":   res["sharpe"], "win_rate": res["win_rate"],
                "max_dd":   res["max_dd"], "cagr":     res["cagr"],
                "n_trades": res["n_trades"], "passed":  int(passed),
            }
            writer.writerow(row)
            f.flush()
            status = "PASS" if passed else "fail"
            if (i + 1) % 10 == 0 or passed:
                print(f"  [{i+1}/{len(remaining)}] bp={bp} ep={ep} mult={mult} "
                      f"risk={risk}  Sh={res['sharpe']:.2f}  WR={res['win_rate']:.0f}%  "
                      f"DD={res['max_dd']:.0f}%  N={res['n_trades']}  {status}")


def print_summary() -> None:
    if not os.path.exists(GRID_CSV):
        print("No grid results found. Run --grid first.")
        return
    rows = []
    with open(GRID_CSV) as f:
        for row in csv.DictReader(f):
            rows.append({k: float(v) if k not in ("passed",) else int(v)
                        for k, v in row.items()})
    rows.sort(key=lambda r: r["sharpe"], reverse=True)
    passing = [r for r in rows if r["passed"]]

    print(f"\n{'='*75}")
    print(f"  FUTURES GRID RESULTS — Top 10 by Sharpe ({len(rows)} combos total, "
          f"{len(passing)} passing)")
    print(f"{'='*75}")
    hdr = f"  {'BP':>3} {'EP':>3} {'Mult':>5} {'Risk':>6}  {'Sharpe':>6}  {'WR%':>5}  "
    hdr += f"{'DD%':>5}  {'CAGR%':>6}  {'N':>4}  Pass"
    print(hdr)
    print("  " + "-" * 71)
    for r in rows[:10]:
        flag = "YES" if r["passed"] else "---"
        print(f"  {int(r['BREAKOUT_PERIOD']):>3} {int(r['EXIT_PERIOD']):>3} "
              f"{r['ATR_STOP_MULT']:>5.1f} {r['RISK_PCT']:>6.3f}  "
              f"{r['sharpe']:>6.3f}  {r['win_rate']:>5.1f}  {r['max_dd']:>5.1f}  "
              f"{r['cagr']:>6.1f}  {int(r['n_trades']):>4}   {flag}")


# ── Main ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--grid",    action="store_true", help="Run full parameter grid")
    ap.add_argument("--summary", action="store_true", help="Print grid summary")
    ap.add_argument("--verbose", action="store_true", help="Print every trade")
    args = ap.parse_args()

    if args.summary:
        print_summary()
        sys.exit(0)

    data = load_data()

    if args.grid:
        run_grid(data)
        print_summary()
        sys.exit(0)

    # ── Single pass with current parameters ──────────────────────────────
    from futures.strategy import BREAKOUT_PERIOD, EXIT_PERIOD, ATR_STOP_MULT, RISK_PCT
    print(f"\nRunning single backtest: BP={BREAKOUT_PERIOD} EP={EXIT_PERIOD} "
          f"Mult={ATR_STOP_MULT} Risk={RISK_PCT}")
    res = run_backtest(
        data,
        breakout_period=BREAKOUT_PERIOD,
        exit_period=EXIT_PERIOD,
        atr_stop_mult=ATR_STOP_MULT,
        risk_pct=RISK_PCT,
        verbose=args.verbose,
    )

    PASS = (res["sharpe"] >= 0.70 and res["win_rate"] >= 35
            and res["max_dd"] < 30 and res["n_trades"] >= 30)
    verdict = "PASSES -- safe to enable LIVE" if PASS else "FAILS criteria"

    print(f"\n{'='*50}")
    print(f"  FUTURES BACKTEST — {BACKTEST_YEARS}y  |  5 markets")
    print(f"{'='*50}")
    print(f"  Sharpe ratio   : {res['sharpe']:.3f}  (threshold >= 0.70)")
    print(f"  Win rate       : {res['win_rate']:.1f}%   (threshold >= 35%)")
    print(f"  Max drawdown   : {res['max_dd']:.1f}%   (threshold < 30%)")
    print(f"  CAGR           : {res['cagr']:.1f}%")
    print(f"  Total trades   : {res['n_trades']}  (threshold >= 30)")
    print(f"  Final equity   : ${res['final_equity']:,.0f}  (started ${INITIAL_EQUITY:,})")
    print(f"{'='*50}")
    print(f"  {verdict}")
    print(f"{'='*50}\n")

    if res["trades"] and args.verbose:
        wins = [t for t in res["trades"] if t["pnl"] > 0]
        loses = [t for t in res["trades"] if t["pnl"] <= 0]
        avg_win  = np.mean([t["pnl_pct"] for t in wins])  if wins  else 0
        avg_loss = np.mean([t["pnl_pct"] for t in loses]) if loses else 0
        print(f"  Avg win  : +{avg_win:.1f}%")
        print(f"  Avg loss : {avg_loss:.1f}%")
        print(f"  Expectancy: {avg_win*len(wins)/res['n_trades'] + avg_loss*len(loses)/res['n_trades']:.2f}%\n")
