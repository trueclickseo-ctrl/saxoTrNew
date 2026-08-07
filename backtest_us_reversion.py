"""
backtest_us_reversion.py
-------------------------
Walk-forward backtest for the US Mean Reversion strategy.

Run:  python backtest_us_reversion.py

What it does:
  1. Downloads 3 years of daily bars for the 61-stock universe (yfinance)
  2. Simulates the reversion strategy day by day (no look-ahead)
  3. Reports per-trade P&L, win rate, average hold, Sharpe ratio, max drawdown
  4. Compares equity curve vs SPY buy-and-hold over the same period

DECISION: only enable US_REVERSION_ENABLED in atos_runner.py after this
backtest shows at least Sharpe > 0.8 and Win Rate > 50% over 2+ years.
"""
import sys
import os
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import date, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from atos.us_reversion import scan, should_exit, MAX_POSITIONS, REVERSION_SLEEVE_SEK, STOP_PCT
from atos.universe import US_TICKERS

BACKTEST_YEARS = 3
COMMISSION_PCT = 0.0008   # 0.08% per side (Saxo SIM rate)
SLEEVE_START   = REVERSION_SLEEVE_SEK


def download_data(tickers, years=BACKTEST_YEARS):
    start = (date.today() - timedelta(days=years * 365 + 60)).isoformat()
    print(f"Downloading {len(tickers)} tickers from {start}…")
    raw = yf.download(tickers, start=start, auto_adjust=True, progress=False)
    if isinstance(raw.columns, pd.MultiIndex):
        close  = raw["Close"]
        volume = raw["Volume"]
    else:
        close  = raw[["Close"]]
        volume = raw[["Volume"]]
    return close.ffill(), volume.ffill()


def build_feat(close_df, volume_df, ticker, up_to_idx):
    """Build a pseudo feat_data dict for one ticker up to (not including) up_to_idx."""
    c = close_df[ticker].iloc[:up_to_idx].dropna()
    v = volume_df[ticker].iloc[:up_to_idx].dropna()
    if len(c) < 220:
        return None
    return {ticker: pd.DataFrame({"Close": c, "Volume": v})}


def run_backtest():
    close_df, vol_df = download_data(US_TICKERS)
    tickers_avail = [t for t in US_TICKERS if t in close_df.columns]
    print(f"  {len(tickers_avail)} tickers with data.\n")

    equity   = SLEEVE_START
    peak     = equity
    max_dd   = 0.0
    trades   = []          # completed trades
    open_pos = {}          # {ticker: {entry_price, entry_idx, entry_date}}
    equity_curve = []

    dates = close_df.index.tolist()
    start_idx = 220  # warm-up for EMA200

    for idx in range(start_idx, len(dates)):
        today = dates[idx]

        # ── Exit check for all open positions ──────────────────────
        to_close = []
        for ticker, pos in list(open_pos.items()):
            if ticker not in close_df.columns:
                continue
            cur_price = float(close_df[ticker].iloc[idx])
            if pd.isna(cur_price):
                continue

            # Compute current RSI and SMA20 up to today
            c_series = close_df[ticker].iloc[:idx + 1].dropna()
            delta = c_series.diff()
            gain  = delta.clip(lower=0).rolling(14).mean()
            loss  = (-delta.clip(upper=0)).rolling(14).mean()
            rs    = gain / loss.replace(0, np.nan)
            rsi_s = 100 - (100 / (1 + rs))
            cur_rsi = float(rsi_s.iloc[-1]) if not rsi_s.empty else None
            sma20   = float(c_series.rolling(20).mean().iloc[-1]) if len(c_series) >= 20 else None

            days_held = idx - pos["entry_idx"]
            exit_flag, reason = should_exit(
                {"entry_price": pos["entry_price"]},
                cur_price, cur_rsi, sma20, days_held
            )
            if exit_flag:
                to_close.append((ticker, cur_price, reason, pos))

        for ticker, exit_price, reason, pos in to_close:
            shares    = pos["shares"]
            entry_p   = pos["entry_price"]
            pnl_gross = (exit_price - entry_p) * shares
            comm      = (entry_p + exit_price) * shares * COMMISSION_PCT
            pnl_net   = pnl_gross - comm
            equity   += pnl_net
            trades.append({
                "ticker":      ticker,
                "entry_date":  pos["entry_date"],
                "exit_date":   today,
                "entry_price": entry_p,
                "exit_price":  exit_price,
                "shares":      shares,
                "pnl_net":     pnl_net,
                "reason":      reason,
                "days_held":   idx - pos["entry_idx"],
            })
            del open_pos[ticker]

        # ── Entry scan (only if slots available) ───────────────────
        slots_free = MAX_POSITIONS - len(open_pos)
        if slots_free > 0:
            # Build feat_data for all tickers up to today
            feat_data = {}
            for t in tickers_avail:
                fd = build_feat(close_df, vol_df, t, idx + 1)
                if fd:
                    feat_data[t] = fd[t]

            candidates = scan(feat_data, tickers_avail)
            # Skip tickers already held
            candidates = [c for c in candidates if c["ticker"] not in open_pos]

            for cand in candidates[:slots_free]:
                t     = cand["ticker"]
                price = cand["price"]
                slot  = equity / MAX_POSITIONS
                shares = int(slot / price)
                if shares < 1:
                    continue
                cost  = shares * price * (1 + COMMISSION_PCT)
                open_pos[t] = {
                    "entry_price": price,
                    "entry_idx":   idx,
                    "entry_date":  today,
                    "shares":      shares,
                    "cost":        cost,
                }

        # Track equity + drawdown
        open_val = sum(
            p["shares"] * float(close_df[t].iloc[idx])
            for t, p in open_pos.items()
            if t in close_df.columns and not pd.isna(close_df[t].iloc[idx])
        )
        total = equity + open_val
        peak  = max(peak, total)
        dd    = (peak - total) / peak
        max_dd = max(max_dd, dd)
        equity_curve.append({"date": today, "equity": total})

    # Close any still-open positions at last price
    last_idx = len(dates) - 1
    for ticker, pos in open_pos.items():
        lp = float(close_df[ticker].iloc[last_idx]) if ticker in close_df.columns else pos["entry_price"]
        pnl = (lp - pos["entry_price"]) * pos["shares"]
        equity += pnl
        trades.append({
            "ticker": ticker, "entry_date": pos["entry_date"],
            "exit_date": dates[last_idx], "entry_price": pos["entry_price"],
            "exit_price": lp, "shares": pos["shares"],
            "pnl_net": pnl, "reason": "end-of-backtest", "days_held": last_idx - pos["entry_idx"]
        })

    _print_results(trades, equity_curve, close_df, dates, start_idx)


def _print_results(trades, equity_curve, close_df, dates, start_idx):
    if not trades:
        print("No trades generated. Check parameters — RSI/dip thresholds may be too strict.")
        return

    df = pd.DataFrame(trades)
    wins  = df[df["pnl_net"] > 0]
    losses = df[df["pnl_net"] <= 0]
    total_pnl = df["pnl_net"].sum()
    win_rate  = len(wins) / len(df) * 100

    eq_df = pd.DataFrame(equity_curve).set_index("date")
    daily_ret = eq_df["equity"].pct_change().dropna()
    sharpe = daily_ret.mean() / daily_ret.std() * np.sqrt(252) if daily_ret.std() > 0 else 0

    start_equity = SLEEVE_START
    end_equity   = eq_df["equity"].iloc[-1]
    years = (dates[-1] - dates[start_idx]).days / 365
    cagr  = (end_equity / start_equity) ** (1 / years) - 1 if years > 0 else 0

    # SPY benchmark
    spy_start = float(close_df["SPY"].iloc[start_idx]) if "SPY" in close_df.columns else None
    spy_end   = float(close_df["SPY"].iloc[-1])        if "SPY" in close_df.columns else None
    spy_cagr  = ((spy_end / spy_start) ** (1 / years) - 1) if spy_start else None

    peak = eq_df["equity"].cummax()
    max_dd = ((peak - eq_df["equity"]) / peak).max()

    print("=" * 60)
    print("  US MEAN REVERSION — BACKTEST RESULTS")
    print("=" * 60)
    print(f"  Period:        {dates[start_idx].date()} → {dates[-1].date()} ({years:.1f}y)")
    print(f"  Trades:        {len(df)} total  ({len(wins)} wins / {len(losses)} losses)")
    print(f"  Win rate:      {win_rate:.1f}%")
    print(f"  Avg hold:      {df['days_held'].mean():.1f} trading days")
    print(f"  Avg win:       +{wins['pnl_net'].mean():,.0f} SEK" if len(wins) else "  Avg win:       —")
    print(f"  Avg loss:      {losses['pnl_net'].mean():,.0f} SEK" if len(losses) else "  Avg loss:      —")
    print(f"  Total P&L:     {total_pnl:+,.0f} SEK")
    print(f"  CAGR:          {cagr*100:.1f}%  (SPY: {spy_cagr*100:.1f}%)" if spy_cagr else f"  CAGR:          {cagr*100:.1f}%")
    print(f"  Sharpe ratio:  {sharpe:.2f}")
    print(f"  Max drawdown:  {max_dd*100:.1f}%")
    print()

    # Top 10 trades
    top = df.nlargest(5, "pnl_net")[["ticker", "entry_date", "exit_date", "days_held", "pnl_net", "reason"]]
    bot = df.nsmallest(5, "pnl_net")[["ticker", "entry_date", "exit_date", "days_held", "pnl_net", "reason"]]
    print("  Best 5 trades:")
    for _, r in top.iterrows():
        print(f"    {r['ticker']:<6} {str(r['entry_date'])[:10]} → {str(r['exit_date'])[:10]}  "
              f"{r['days_held']}d  {r['pnl_net']:+,.0f} SEK  [{r['reason']}]")
    print("  Worst 5 trades:")
    for _, r in bot.iterrows():
        print(f"    {r['ticker']:<6} {str(r['entry_date'])[:10]} → {str(r['exit_date'])[:10]}  "
              f"{r['days_held']}d  {r['pnl_net']:+,.0f} SEK  [{r['reason']}]")

    print()
    verdict = "ENABLE" if sharpe >= 0.8 and win_rate >= 50 else "DO NOT ENABLE"
    print(f"  VERDICT: {verdict} US_REVERSION_ENABLED in atos_runner.py")
    if verdict == "ENABLE":
        print("  (Sharpe >= 0.8 and Win Rate >= 50% — thresholds met)")
    else:
        print("  (Thresholds: Sharpe >= 0.8, Win Rate >= 50% — adjust parameters and re-run)")
    print("=" * 60)


if __name__ == "__main__":
    # Add SPY to the download for benchmark comparison
    ALL_TICKERS = US_TICKERS + ["SPY"]
    close_df, vol_df = download_data(ALL_TICKERS)

    tickers_avail = [t for t in US_TICKERS if t in close_df.columns]
    print(f"  {len(tickers_avail)} tickers with data.\n")

    # Rebuild with SPY included in close_df globally for benchmark
    import yfinance as yf
    from datetime import timedelta
    start = (date.today() - timedelta(days=BACKTEST_YEARS * 365 + 60)).isoformat()
    raw = yf.download(ALL_TICKERS, start=start, auto_adjust=True, progress=False)
    if isinstance(raw.columns, pd.MultiIndex):
        close_df2 = raw["Close"].ffill()
        vol_df2   = raw["Volume"].ffill()
    else:
        close_df2 = raw[["Close"]].ffill()
        vol_df2   = raw[["Volume"]].ffill()

    dates = close_df2.index.tolist()
    tickers_avail = [t for t in US_TICKERS if t in close_df2.columns]
    start_idx = 220

    equity   = SLEEVE_START
    peak     = equity
    trades   = []
    open_pos = {}
    equity_curve = []

    for idx in range(start_idx, len(dates)):
        today = dates[idx]

        # Exit check
        to_close = []
        for ticker, pos in list(open_pos.items()):
            if ticker not in close_df2.columns:
                continue
            cur_price = float(close_df2[ticker].iloc[idx])
            if pd.isna(cur_price):
                continue
            c_series = close_df2[ticker].iloc[:idx + 1].dropna()
            delta = c_series.diff()
            gain  = delta.clip(lower=0).rolling(14).mean()
            loss  = (-delta.clip(upper=0)).rolling(14).mean()
            rs    = gain / loss.replace(0, np.nan)
            rsi_s = 100 - (100 / (1 + rs))
            cur_rsi = float(rsi_s.iloc[-1]) if not rsi_s.empty else None
            sma20   = float(c_series.rolling(20).mean().iloc[-1]) if len(c_series) >= 20 else None
            days_held = idx - pos["entry_idx"]
            exit_flag, reason = should_exit(
                {"entry_price": pos["entry_price"]}, cur_price, cur_rsi, sma20, days_held
            )
            if exit_flag:
                to_close.append((ticker, cur_price, reason, pos))

        for ticker, exit_price, reason, pos in to_close:
            shares = pos["shares"]
            entry_p = pos["entry_price"]
            pnl_gross = (exit_price - entry_p) * shares
            comm = (entry_p + exit_price) * shares * COMMISSION_PCT
            pnl_net = pnl_gross - comm
            equity += pnl_net
            trades.append({
                "ticker": ticker, "entry_date": pos["entry_date"],
                "exit_date": today, "entry_price": entry_p,
                "exit_price": exit_price, "shares": pos["shares"],
                "pnl_net": pnl_net, "reason": reason,
                "days_held": idx - pos["entry_idx"],
            })
            del open_pos[ticker]

        # Entry scan
        slots_free = MAX_POSITIONS - len(open_pos)
        if slots_free > 0:
            feat_data = {}
            for t in tickers_avail:
                c = close_df2[t].iloc[:idx + 1].dropna()
                v = vol_df2[t].iloc[:idx + 1].dropna() if t in vol_df2.columns else pd.Series(dtype=float)
                if len(c) >= 220:
                    feat_data[t] = pd.DataFrame({"Close": c, "Volume": v.reindex(c.index)})
            candidates = scan(feat_data, tickers_avail)
            candidates = [c for c in candidates if c["ticker"] not in open_pos]
            for cand in candidates[:slots_free]:
                t = cand["ticker"]
                price = cand["price"]
                slot = equity / MAX_POSITIONS
                shares = int(slot / price)
                if shares < 1:
                    continue
                open_pos[t] = {
                    "entry_price": price, "entry_idx": idx,
                    "entry_date": today, "shares": shares,
                    "cost": shares * price * (1 + COMMISSION_PCT),
                }

        open_val = sum(
            p["shares"] * float(close_df2[t].iloc[idx])
            for t, p in open_pos.items()
            if t in close_df2.columns and not pd.isna(close_df2[t].iloc[idx])
        )
        total = equity + open_val
        peak  = max(peak, total)
        dd    = (peak - total) / peak
        equity_curve.append({"date": today, "equity": total})

    # Close remaining
    last_idx = len(dates) - 1
    for ticker, pos in open_pos.items():
        lp = float(close_df2[ticker].iloc[last_idx]) if ticker in close_df2.columns else pos["entry_price"]
        pnl = (lp - pos["entry_price"]) * pos["shares"]
        equity += pnl
        trades.append({
            "ticker": ticker, "entry_date": pos["entry_date"],
            "exit_date": dates[last_idx], "entry_price": pos["entry_price"],
            "exit_price": lp, "shares": pos["shares"],
            "pnl_net": pnl, "reason": "end-of-backtest",
            "days_held": last_idx - pos["entry_idx"],
        })

    _print_results(trades, equity_curve, close_df2, dates, start_idx)
