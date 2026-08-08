"""
omx30_backtest.py
------------------
Walk-forward backtest for both ATOS strategies on the OMX30 universe.

Run:
    python omx30_backtest.py                  # both strategies, 3-year window
    python omx30_backtest.py --strategy blend # Blend only
    python omx30_backtest.py --strategy rev   # Reversion only
    python omx30_backtest.py --years 5        # extend history

OUTPUT:
    Terminal summary (Sharpe, CAGR, MaxDD, Win Rate, N trades)
    data/omx30_backtest_trades.csv  — every simulated trade
    data/omx30_backtest_equity.csv  — daily equity for both strategies

VERDICT CRITERIA (same as US strategies):
    PASS: Sharpe >= 0.8  AND  Win Rate >= 50%  AND  MaxDD < 20%  AND  N >= 15

All prices are in SEK.  Commission is 0.08% round-trip (0.04% each way),
which is realistic for an OMX stock at Saxo SIM.
"""
import sys
import os
import argparse
import pickle
import csv
from datetime import date, timedelta

import numpy as np
import pandas as pd
import yfinance as yf

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
DATA_DIR  = os.path.join(BASE_DIR, "data")
CACHE_PKL = os.path.join(DATA_DIR, "omx30_backtest_cache.pkl")
TRADES_CSV = os.path.join(DATA_DIR, "omx30_backtest_trades.csv")
EQUITY_CSV = os.path.join(DATA_DIR, "omx30_backtest_equity.csv")

sys.path.insert(0, BASE_DIR)

from atos.omx30_universe import OMX30_TICKERS
import atos.omx_momentum  as OMM
import atos.omx_reversion as OMR

BACKTEST_YEARS  = 3
COMMISSION_PCT  = 0.0004   # 0.04% per side → 0.08% round-trip
STARTING_CAPITAL = 300_000.0  # SEK

BLEND_ALLOC  = 0.50   # 50% of capital to Blend
REV_ALLOC    = 0.50   # 50% of capital to Reversion
MAX_REV_SLOTS = 3     # max simultaneous Reversion positions


# ── Colours ───────────────────────────────────────────────────────────────────
GREEN, RED, YELLOW, CYAN, RESET, BOLD = (
    "\033[92m", "\033[91m", "\033[93m", "\033[96m", "\033[0m", "\033[1m"
)


# ── Data download & cache ─────────────────────────────────────────────────────

def _download() -> tuple[pd.DataFrame, pd.DataFrame]:
    start = (date.today() - timedelta(days=BACKTEST_YEARS * 365 + 90)).isoformat()
    print(f"  Downloading {len(OMX30_TICKERS)} OMX30 tickers from {start}...")
    raw   = yf.download(OMX30_TICKERS, start=start, auto_adjust=True, progress=False)
    close = raw["Close"].ffill()
    vol   = raw["Volume"].fillna(0)
    return close, vol


def load_data() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (close_df, vol_df). Disk-cached per run date."""
    if os.path.exists(CACHE_PKL):
        with open(CACHE_PKL, "rb") as f:
            cached = pickle.load(f)
        if cached.get("date") == date.today().isoformat():
            print("  Using cached OMX30 data from today.")
            return cached["close"], cached["vol"]

    close, vol = _download()
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CACHE_PKL, "wb") as f:
        pickle.dump({"date": date.today().isoformat(), "close": close, "vol": vol}, f)
    print(f"  Cached {len(close)} trading days for {len(close.columns)} tickers.")
    return close, vol


def _feat_data(close_df: pd.DataFrame, vol_df: pd.DataFrame, up_to_row: int) -> dict:
    """Build feat_data dict from the panel up to (not including) row index."""
    subset_close = close_df.iloc[:up_to_row]
    subset_vol   = vol_df.iloc[:up_to_row]
    fd = {}
    for t in close_df.columns:
        c = subset_close[t].dropna()
        v = subset_vol[t].dropna()
        if len(c) > 0:
            fd[t] = pd.DataFrame({"Close": c, "Volume": v})
    return fd


# ── Metrics ───────────────────────────────────────────────────────────────────

def _metrics(equity: pd.Series, trades: list[dict]) -> dict:
    """Compute Sharpe, CAGR, MaxDD, WinRate from equity series and trade list."""
    if len(equity) < 2:
        return {"sharpe": 0.0, "cagr": 0.0, "max_dd": 0.0, "win_rate": 0.0, "n": 0}

    rets  = equity.pct_change().dropna()
    sharpe = (rets.mean() / rets.std() * np.sqrt(252)) if rets.std() > 0 else 0.0

    n_years = len(equity) / 252
    cagr    = (equity.iloc[-1] / equity.iloc[0]) ** (1 / n_years) - 1 if n_years > 0 else 0.0

    roll_max = equity.cummax()
    dd       = (equity - roll_max) / roll_max
    max_dd   = float(dd.min())

    sell_trades = [t for t in trades if t["side"] == "Sell" and t.get("pnl_sek") is not None]
    win_rate = (sum(1 for t in sell_trades if t["pnl_sek"] > 0) / len(sell_trades)
                if sell_trades else 0.0)

    return {
        "sharpe":   round(float(sharpe), 2),
        "cagr":     round(float(cagr) * 100, 1),
        "max_dd":   round(float(max_dd) * 100, 1),
        "win_rate": round(float(win_rate) * 100, 1),
        "n":        len(sell_trades),
    }


def _verdict(m: dict) -> str:
    ok = m["sharpe"] >= 0.8 and m["win_rate"] >= 50.0 and m["max_dd"] > -20.0 and m["n"] >= 15
    return f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"


# ── Blend backtest ─────────────────────────────────────────────────────────────

def run_blend(close_df: pd.DataFrame, vol_df: pd.DataFrame) -> tuple[pd.Series, list]:
    """Walk-forward weekly Blend simulation.

    Rebalances every REBAL_DAYS calendar days. Returns (equity_series, trades).
    """
    tickers      = [t for t in OMX30_TICKERS if t in close_df.columns]
    capital      = STARTING_CAPITAL * BLEND_ALLOC
    dates        = close_df.index
    equity       = pd.Series(index=dates, dtype=float)
    equity.iloc[0] = capital

    holdings     = {}          # {ticker: shares}
    last_rebal   = dates[0]
    trades       = []

    warmup = 220   # bars needed before we can compute EMA200

    for i, dt in enumerate(dates):
        if i < warmup:
            equity.iloc[i] = capital
            continue

        # Mark equity to market daily
        current_value = sum(
            sh * float(close_df[t].iloc[i])
            for t, sh in holdings.items()
            if t in close_df.columns and not pd.isna(close_df[t].iloc[i])
        )
        cash_in_hand = capital - sum(
            sh * float(close_df[t].iloc[i])
            for t, sh in holdings.items()
            if t in close_df.columns and not pd.isna(close_df[t].iloc[i])
        )
        # simpler: track cash separately
        equity.iloc[i] = equity.iloc[i - 1] if i > 0 else capital

        # Weekly rebalance trigger
        if (dt - last_rebal).days < OMM.REBAL_DAYS:
            continue

        last_rebal = dt
        fd = _feat_data(close_df, vol_df, i)
        result = OMM.compute_targets(fd, tickers)

        if result.get("risk_off"):
            # Exit everything
            for t, sh in list(holdings.items()):
                if sh <= 0:
                    continue
                price  = float(close_df[t].iloc[i]) if t in close_df.columns else 0
                if price <= 0:
                    continue
                proceed = price * sh * (1 - COMMISSION_PCT)
                entry   = next((tr["price"] for tr in reversed(trades)
                                if tr["ticker"] == t and tr["side"] == "Buy"), price)
                pnl     = (price - entry) * sh - (price * sh * COMMISSION_PCT + entry * sh * COMMISSION_PCT)
                trades.append({"date": dt, "strategy": "OMX Blend", "ticker": t,
                                "side": "Sell", "shares": sh, "price": price,
                                "pnl_sek": round(pnl, 2), "reason": "risk-off"})
                capital += proceed
                holdings[t] = 0
            continue

        targets = result.get("targets", [])
        if not targets:
            continue

        # Compute equal-weight target position for each ticker
        sleeve = capital * BLEND_ALLOC if len(holdings) == 0 else capital
        per_sek = sleeve / len(targets)

        prices = {}
        for t in tickers:
            if t in close_df.columns and not pd.isna(close_df[t].iloc[i]):
                prices[t] = float(close_df[t].iloc[i])

        # Exit tickers no longer in target
        for t, sh in list(holdings.items()):
            if t not in targets and sh > 0:
                price = prices.get(t, 0)
                if price <= 0:
                    continue
                proceed = price * sh * (1 - COMMISSION_PCT)
                entry   = next((tr["price"] for tr in reversed(trades)
                                if tr["ticker"] == t and tr["side"] == "Buy"), price)
                pnl     = (price - entry) * sh - (price * sh * COMMISSION_PCT + entry * sh * COMMISSION_PCT)
                trades.append({"date": dt, "strategy": "OMX Blend", "ticker": t,
                                "side": "Sell", "shares": sh, "price": price,
                                "pnl_sek": round(pnl, 2), "reason": "rebalance exit"})
                capital += proceed
                holdings[t] = 0

        # Buy / adjust tickers in target
        for t in targets:
            price = prices.get(t, 0)
            if price <= 0:
                continue
            tgt_shares = int(per_sek / price)
            cur_shares = int(holdings.get(t, 0))
            delta      = tgt_shares - cur_shares
            if delta > 0:
                cost = price * delta * (1 + COMMISSION_PCT)
                if cost > capital * 0.01:   # sanity: don't spend if almost no cash
                    capital -= cost
                    holdings[t] = holdings.get(t, 0) + delta
                    trades.append({"date": dt, "strategy": "OMX Blend", "ticker": t,
                                   "side": "Buy", "shares": delta, "price": price,
                                   "pnl_sek": None, "reason": "rebalance entry"})
            elif delta < 0:
                proceed = price * (-delta) * (1 - COMMISSION_PCT)
                entry   = next((tr["price"] for tr in reversed(trades)
                                if tr["ticker"] == t and tr["side"] == "Buy"), price)
                pnl     = (price - entry) * (-delta) - (price * (-delta) * COMMISSION_PCT + entry * (-delta) * COMMISSION_PCT)
                capital += proceed
                holdings[t] = holdings.get(t, 0) + delta
                trades.append({"date": dt, "strategy": "OMX Blend", "ticker": t,
                                "side": "Sell", "shares": -delta, "price": price,
                                "pnl_sek": round(pnl, 2), "reason": "rebalance trim"})

        # Mark equity to market at end of day
        port_value = sum(
            int(holdings.get(t, 0)) * prices.get(t, 0)
            for t in holdings
        )
        equity.iloc[i] = capital + port_value

    # Close all remaining positions at last bar
    last_i = len(dates) - 1
    for t, sh in holdings.items():
        if sh <= 0:
            continue
        price = float(close_df[t].iloc[last_i]) if t in close_df.columns else 0
        if price <= 0:
            continue
        proceed = price * sh * (1 - COMMISSION_PCT)
        entry   = next((tr["price"] for tr in reversed(trades)
                        if tr["ticker"] == t and tr["side"] == "Buy"), price)
        pnl     = (price - entry) * sh - (price * sh * COMMISSION_PCT + entry * sh * COMMISSION_PCT)
        trades.append({"date": dates[last_i], "strategy": "OMX Blend", "ticker": t,
                       "side": "Sell", "shares": sh, "price": price,
                       "pnl_sek": round(pnl, 2), "reason": "end-of-test"})
        capital += proceed
        holdings[t] = 0

    equity = equity.ffill().fillna(STARTING_CAPITAL * BLEND_ALLOC)
    return equity, trades


# ── Reversion backtest ────────────────────────────────────────────────────────

def run_reversion(close_df: pd.DataFrame, vol_df: pd.DataFrame) -> tuple[pd.Series, list]:
    """Walk-forward daily Reversion simulation.

    Scans every trading day. Returns (equity_series, trades).
    """
    tickers   = [t for t in OMX30_TICKERS if t in close_df.columns]
    capital   = STARTING_CAPITAL * REV_ALLOC
    sleeve    = capital
    dates     = close_df.index
    equity    = pd.Series(index=dates, dtype=float)
    equity.iloc[0] = capital

    open_trades = {}   # {ticker: {entry_price, entry_date, entry_i, shares}}
    trades      = []
    warmup      = 220

    for i, dt in enumerate(dates):
        if i < warmup:
            equity.iloc[i] = capital
            continue

        fd = _feat_data(close_df, vol_df, i)

        # ── EXIT CHECK (first, to free slots) ─────────────────────────────
        to_close = []
        for t, pos in open_trades.items():
            if t not in close_df.columns:
                continue
            price = float(close_df[t].iloc[i])
            if pd.isna(price) or price <= 0:
                continue
            df_t  = fd.get(t)
            rsi   = None
            sma20 = None
            if df_t is not None and "Close" in df_t and len(df_t) >= 20:
                c      = df_t["Close"].dropna()
                sma20  = float(c.rolling(20).mean().iloc[-1])
                delta  = c.diff()
                gain   = delta.clip(lower=0).rolling(14).mean()
                loss   = (-delta.clip(upper=0)).rolling(14).mean()
                rs     = gain / loss.replace(0, np.nan)
                rsi_s  = 100 - (100 / (1 + rs))
                rsi    = float(rsi_s.iloc[-1]) if not pd.isna(rsi_s.iloc[-1]) else None

            days_held = sum(1 for d in dates[pos["entry_i"]:i] if d.weekday() < 5)
            exit_flag, reason = OMR.should_exit(
                {"entry_price": pos["entry_price"]},
                price, rsi, sma20, days_held
            )
            if exit_flag:
                to_close.append((t, price, reason, pos))

        for t, price, reason, pos in to_close:
            sh      = pos["shares"]
            proceed = price * sh * (1 - COMMISSION_PCT)
            cost    = pos["entry_price"] * sh * (1 + COMMISSION_PCT)
            pnl     = proceed - cost
            capital += proceed
            trades.append({"date": dt, "strategy": "OMX Reversion", "ticker": t,
                            "side": "Sell", "shares": sh, "price": round(price, 2),
                            "entry_price": pos["entry_price"],
                            "pnl_sek": round(pnl, 2), "reason": reason,
                            "days_held": sum(1 for d in dates[pos["entry_i"]:i] if d.weekday() < 5)})
            del open_trades[t]

        # ── ENTRY SCAN ─────────────────────────────────────────────────────
        open_count = len(open_trades)
        if open_count < MAX_REV_SLOTS:
            candidates = OMR.scan(fd, tickers)
            slots_free = MAX_REV_SLOTS - open_count
            per_sek    = sleeve / MAX_REV_SLOTS   # equal-weight within sleeve

            for cand in candidates[:slots_free]:
                t = cand["ticker"]
                if t in open_trades:
                    continue
                price = float(close_df[t].iloc[i]) if t in close_df.columns else 0
                if price <= 0:
                    continue
                shares = int(per_sek / price)
                if shares <= 0:
                    continue
                cost = price * shares * (1 + COMMISSION_PCT)
                if cost > capital:
                    continue
                capital -= cost
                open_trades[t] = {
                    "entry_price": price,
                    "entry_date":  dt,
                    "entry_i":     i,
                    "shares":      shares,
                }
                trades.append({"date": dt, "strategy": "OMX Reversion", "ticker": t,
                                "side": "Buy", "shares": shares, "price": round(price, 2),
                                "entry_price": price,
                                "pnl_sek": None, "reason": "entry signal",
                                "days_held": 0})

        # ── Mark-to-market ────────────────────────────────────────────────
        port_value = 0.0
        for t, pos in open_trades.items():
            if t in close_df.columns and not pd.isna(close_df[t].iloc[i]):
                port_value += pos["shares"] * float(close_df[t].iloc[i])
        equity.iloc[i] = capital + port_value

    # Close all remaining at final bar
    last_i = len(dates) - 1
    for t, pos in list(open_trades.items()):
        price = float(close_df[t].iloc[last_i]) if t in close_df.columns else 0
        if price <= 0:
            continue
        sh      = pos["shares"]
        proceed = price * sh * (1 - COMMISSION_PCT)
        cost    = pos["entry_price"] * sh * (1 + COMMISSION_PCT)
        pnl     = proceed - cost
        trades.append({"date": dates[last_i], "strategy": "OMX Reversion", "ticker": t,
                        "side": "Sell", "shares": sh, "price": round(price, 2),
                        "entry_price": pos["entry_price"],
                        "pnl_sek": round(pnl, 2), "reason": "end-of-test",
                        "days_held": sum(1 for d in dates[pos["entry_i"]:last_i] if d.weekday() < 5)})

    equity = equity.ffill().fillna(STARTING_CAPITAL * REV_ALLOC)
    return equity, trades


# ── Buy-and-hold benchmark ────────────────────────────────────────────────────

def run_benchmark(close_df: pd.DataFrame) -> pd.Series:
    """Equal-weight buy-and-hold of all OMX30 tickers from day 220 onward."""
    tickers  = [t for t in OMX30_TICKERS if t in close_df.columns]
    warmup   = 220
    capital  = STARTING_CAPITAL
    dates    = close_df.index
    equity   = pd.Series(index=dates, dtype=float)
    equity.iloc[:warmup] = capital

    entry_prices = close_df.iloc[warmup]
    per_sek      = capital / len(tickers)
    shares       = {t: int(per_sek / entry_prices[t]) for t in tickers
                   if t in entry_prices.index and entry_prices[t] > 0}

    for i in range(warmup, len(dates)):
        val = sum(shares.get(t, 0) * float(close_df[t].iloc[i])
                  for t in tickers
                  if t in close_df.columns and not pd.isna(close_df[t].iloc[i]))
        equity.iloc[i] = val if val > 0 else equity.iloc[i - 1]

    return equity.ffill()


# ── Reporting ─────────────────────────────────────────────────────────────────

def _bar(value: float, width: int = 30) -> str:
    """Simple ASCII bar from -100% to +100%."""
    filled = max(0, min(width, int((value + 100) / 200 * width)))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def print_report(
    blend_eq: pd.Series,    blend_trades: list,
    rev_eq: pd.Series,      rev_trades: list,
    bench_eq: pd.Series,
    years: int,
):
    bm  = _metrics(blend_eq, blend_trades)
    rm  = _metrics(rev_eq,   rev_trades)
    all_trades = blend_trades + rev_trades
    bv  = _verdict(bm)
    rv  = _verdict(rm)

    bench_cagr = ((bench_eq.iloc[-1] / bench_eq.iloc[0]) ** (1 / years) - 1) * 100

    print(f"\n{BOLD}{'='*62}{RESET}")
    print(f"{BOLD}  OMX30 BACKTEST RESULTS  ({years} years, {len(OMX30_TICKERS)} stocks){RESET}")
    print(f"{BOLD}{'='*62}{RESET}")
    print(f"  Starting capital:  {STARTING_CAPITAL:,.0f} SEK")
    print(f"  Blend sleeve:      {STARTING_CAPITAL * BLEND_ALLOC:,.0f} SEK (50%)")
    print(f"  Reversion sleeve:  {STARTING_CAPITAL * REV_ALLOC:,.0f} SEK (50%)")
    print(f"  Commission:        {COMMISSION_PCT*100:.2f}% per side")
    print()

    rows = [
        ("Metric",       "OMX Blend",      "OMX Reversion",  "Buy & Hold"),
        ("-"*14,         "-"*14,           "-"*14,           "-"*14),
        ("Sharpe",       f"{bm['sharpe']:.2f}",  f"{rm['sharpe']:.2f}",  "n/a"),
        ("CAGR %",       f"{bm['cagr']:.1f}%",   f"{rm['cagr']:.1f}%",   f"{bench_cagr:.1f}%"),
        ("Max DD %",     f"{bm['max_dd']:.1f}%",  f"{rm['max_dd']:.1f}%", "n/a"),
        ("Win Rate",     f"{bm['win_rate']:.1f}%", f"{rm['win_rate']:.1f}%", "n/a"),
        ("N trades",     str(bm['n']),     str(rm['n']),     "1"),
        ("VERDICT",      bv,               rv,               ""),
    ]
    col_w = 16
    for row in rows:
        print("  " + "".join(str(c).ljust(col_w) for c in row))

    # Pass/Fail explanation
    print(f"\n  Pass criteria: Sharpe >= 0.8  |  Win Rate >= 50%  |  MaxDD > -20%  |  N >= 15")

    # Per-strategy trade breakdown
    for label, trd_list, eq in [("OMX Blend", blend_trades, blend_eq),
                                  ("OMX Reversion", rev_trades, rev_eq)]:
        sells = [t for t in trd_list if t["side"] == "Sell" and t.get("pnl_sek") is not None]
        if not sells:
            continue
        wins   = [t for t in sells if t["pnl_sek"] > 0]
        losses = [t for t in sells if t["pnl_sek"] <= 0]
        avg_win  = sum(t["pnl_sek"] for t in wins)   / max(len(wins), 1)
        avg_loss = sum(t["pnl_sek"] for t in losses) / max(len(losses), 1)
        total_pnl = sum(t["pnl_sek"] for t in sells)
        print(f"\n  {CYAN}{label}{RESET}")
        print(f"    Total P&L:  {total_pnl:+,.0f} SEK")
        print(f"    Avg win:    {avg_win:+,.0f} SEK ({len(wins)} trades)")
        print(f"    Avg loss:   {avg_loss:+,.0f} SEK ({len(losses)} trades)")
        if sells:
            best  = max(sells, key=lambda t: t["pnl_sek"])
            worst = min(sells, key=lambda t: t["pnl_sek"])
            print(f"    Best:       {best['ticker']} {best['pnl_sek']:+,.0f} SEK on {best['date'].date()}")
            print(f"    Worst:      {worst['ticker']} {worst['pnl_sek']:+,.0f} SEK on {worst['date'].date()}")

    print(f"\n{BOLD}{'='*62}{RESET}\n")


def _save_trades(trades: list):
    os.makedirs(DATA_DIR, exist_ok=True)
    fields = ["date", "strategy", "ticker", "side", "shares", "price",
              "entry_price", "pnl_sek", "reason", "days_held"]
    with open(TRADES_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for t in trades:
            row = dict(t)
            row["date"] = t["date"].date() if hasattr(t["date"], "date") else t["date"]
            w.writerow(row)
    print(f"  Trades saved to {TRADES_CSV}")


def _save_equity(blend_eq: pd.Series, rev_eq: pd.Series, bench_eq: pd.Series):
    os.makedirs(DATA_DIR, exist_ok=True)
    df = pd.DataFrame({
        "date":          blend_eq.index,
        "blend_sek":     blend_eq.values,
        "reversion_sek": rev_eq.values,
        "combined_sek":  (blend_eq + rev_eq).values,
        "benchmark_sek": bench_eq.values,
    })
    df["date"] = df["date"].dt.date
    df.to_csv(EQUITY_CSV, index=False)
    print(f"  Equity curve saved to {EQUITY_CSV}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="OMX30 strategy backtest")
    parser.add_argument("--strategy", choices=["blend", "rev", "both"], default="both")
    parser.add_argument("--years",    type=int, default=BACKTEST_YEARS)
    parser.add_argument("--no-cache", action="store_true", help="Force fresh download")
    args = parser.parse_args()

    years = args.years

    if args.no_cache and os.path.exists(CACHE_PKL):
        os.remove(CACHE_PKL)
        print("  Cache cleared.")

    print(f"\n{BOLD}{CYAN}OMX30 Backtest — {years} years{RESET}")
    print(f"{CYAN}{'-'*40}{RESET}")

    close_df, vol_df = load_data()

    # Filter to tickers with enough history
    valid_tickers = [t for t in OMX30_TICKERS
                     if t in close_df.columns and close_df[t].count() >= 220]
    print(f"  Valid tickers with 220+ bars: {len(valid_tickers)} / {len(OMX30_TICKERS)}")
    if len(valid_tickers) < 10:
        print(f"{RED}  Too few tickers. Check internet connection or try --no-cache.{RESET}")
        sys.exit(1)

    bench_eq = run_benchmark(close_df)

    blend_eq     = pd.Series(dtype=float)
    blend_trades = []
    rev_eq       = pd.Series(dtype=float)
    rev_trades   = []

    if args.strategy in ("blend", "both"):
        print(f"\n{CYAN}  Running OMX Blend...{RESET}")
        blend_eq, blend_trades = run_blend(close_df, vol_df)
        print(f"  Done. {len([t for t in blend_trades if t['side']=='Sell'])} sell events.")

    if args.strategy in ("rev", "both"):
        print(f"\n{CYAN}  Running OMX Reversion...{RESET}")
        rev_eq, rev_trades = run_reversion(close_df, vol_df)
        sells = [t for t in rev_trades if t["side"] == "Sell"]
        print(f"  Done. {len(sells)} closed trades.")

    if blend_eq.empty:
        blend_eq = pd.Series(STARTING_CAPITAL * BLEND_ALLOC,
                             index=close_df.index, dtype=float)
    if rev_eq.empty:
        rev_eq = pd.Series(STARTING_CAPITAL * REV_ALLOC,
                           index=close_df.index, dtype=float)

    print_report(blend_eq, blend_trades, rev_eq, rev_trades, bench_eq, years)

    all_trades = blend_trades + rev_trades
    _save_trades(all_trades)
    _save_equity(blend_eq, rev_eq, bench_eq)


if __name__ == "__main__":
    main()
