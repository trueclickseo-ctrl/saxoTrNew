"""
backtest_etf.py
---------------
Sector Rotation ETF Strategy — 10-year backtest (2016-2026).

Mirrors the live saxo_etf_strategy exactly:
  - 11 US sector ETFs ranked by 63-day return (3 months) each day
  - Hold top 3; enter any top-3 sector not already held
  - Exit on: Stop-Loss (-8%), Take-Profit (+20%), or rank-drop (optional)
  - Equal-weight sizing — 1/3 of ETF budget per position
  - Commission: 0.08% per trade (matches ATOS default)

Usage:
  python backtest_etf.py                     # default: SL=8%, TP=20%, no rank-drop
  python backtest_etf.py --rank-drop         # also exit when sector falls from top 3
  python backtest_etf.py --grid              # grid search SL / TP / lookback combos
  python backtest_etf.py --plot              # show equity curve (requires matplotlib)
"""

import argparse
import sys
import numpy as np
import pandas as pd
import yfinance as yf
from itertools import product
from datetime import datetime, date

# ── Configuration (mirrors etf_config.py) ────────────────────────────────────
SECTORS      = ["XLK", "XLV", "XLE", "XLF", "XLI", "XLY", "XLP", "XLU", "XLRE", "XLB", "SPY"]
LOOKBACK     = 63        # 3-month return ranking window (trading days)
TOP_N        = 3         # top sectors to hold at any time
STOP_LOSS    = 0.08      # 8%  — exit if price drops 8% below entry
TAKE_PROFIT  = 0.20      # 20% — exit if price rises 20% above entry
COMMISSION   = 0.0008    # 0.08% per trade (round-trip = 0.16%)
STARTING_CAP = 100_000   # USD — results are %-based; size doesn't matter


# ── Data download ─────────────────────────────────────────────────────────────

def _download(years: int = 10) -> pd.DataFrame:
    print(f"Downloading {years}y of daily closes for {len(SECTORS)} sector ETFs...")
    raw = yf.download(SECTORS, period=f"{years}y", auto_adjust=True, progress=False)
    close = raw["Close"] if "Close" in raw.columns else raw
    # Flatten MultiIndex if present
    if isinstance(close.columns, pd.MultiIndex):
        close.columns = close.columns.get_level_values(0)
    close = close[SECTORS].dropna(how="all")
    print(f"  Got {len(close)} trading days  ({close.index[0].date()} → {close.index[-1].date()})")
    return close


# ── Core backtest ─────────────────────────────────────────────────────────────

def run_backtest(
    close:       pd.DataFrame,
    lookback:    int   = LOOKBACK,
    stop_loss:   float = STOP_LOSS,
    take_profit: float = TAKE_PROFIT,
    rank_drop:   bool  = False,
    verbose:     bool  = True,
) -> dict:
    """
    Run one backtest pass. Returns metrics dict.

    Positions: dict of {symbol: {"entry": price, "shares": n}}
    Equity: starts at STARTING_CAP, updated daily from closed P&L and open mark-to-market.
    """
    equity   = float(STARTING_CAP)
    cash     = float(STARTING_CAP)
    positions = {}   # {symbol: {"entry": float, "shares": float, "entry_idx": int}}

    daily_equity = []
    trades       = []   # list of closed trade dicts

    dates = close.index
    prices = {sym: close[sym].values for sym in SECTORS}
    n_days = len(dates)

    for i in range(lookback, n_days):
        day_prices = {sym: prices[sym][i] for sym in SECTORS
                      if not np.isnan(prices[sym][i])}
        if not day_prices:
            daily_equity.append(equity)
            continue

        # ── 1. Exit checks ──────────────────────────────────────────────────
        to_exit = []
        for sym, pos in positions.items():
            if sym not in day_prices:
                continue
            cur   = day_prices[sym]
            entry = pos["entry"]
            ret   = (cur - entry) / entry

            reason = None
            if ret <= -stop_loss:
                reason = "SL"
            elif ret >= take_profit:
                reason = "TP"
            elif rank_drop and sym not in _top_n(day_prices, prices, i, lookback, TOP_N):
                reason = "rank_drop"

            if reason:
                to_exit.append((sym, reason))

        for sym, reason in to_exit:
            pos    = positions.pop(sym)
            cur    = day_prices[sym]
            entry  = pos["entry"]
            shares = pos["shares"]
            gross  = (cur - entry) * shares
            cost   = cur * shares * COMMISSION
            pnl    = gross - cost
            cash  += entry * shares + pnl   # return capital + P&L
            equity = cash + _open_value(positions, day_prices)
            trades.append({
                "exit_date":  str(dates[i].date()),
                "symbol":     sym,
                "entry":      entry,
                "exit":       cur,
                "pnl":        pnl,
                "pnl_pct":    (cur / entry - 1) * 100,
                "reason":     reason,
                "hold_days":  i - pos["entry_idx"],
            })

        # ── 2. Rank sectors → top 3 ─────────────────────────────────────────
        top3 = _top_n(day_prices, prices, i, lookback, TOP_N)

        # ── 3. Enter any top-3 sector not already held ──────────────────────
        for sym in top3:
            if sym in positions:
                continue
            if len(positions) >= TOP_N:
                continue
            if sym not in day_prices:
                continue

            price  = day_prices[sym]
            budget = equity / TOP_N           # equal weight: 1/3 of total equity
            shares = budget / price
            cost   = price * shares * COMMISSION
            if cash < price * shares + cost:
                continue                       # not enough cash

            cash -= price * shares + cost
            positions[sym] = {
                "entry":     price,
                "shares":    shares,
                "entry_idx": i,
            }

        # ── 4. Mark-to-market equity ─────────────────────────────────────────
        open_val = _open_value(positions, day_prices)
        equity   = cash + open_val
        daily_equity.append(equity)

    # Close any remaining positions at last day's price
    last_prices = {sym: prices[sym][-1] for sym in SECTORS
                   if not np.isnan(prices[sym][-1])}
    for sym, pos in positions.items():
        if sym in last_prices:
            cur    = last_prices[sym]
            entry  = pos["entry"]
            shares = pos["shares"]
            gross  = (cur - entry) * shares
            cost   = cur * shares * COMMISSION
            pnl    = gross - cost
            trades.append({
                "exit_date":  str(dates[-1].date()),
                "symbol":     sym,
                "entry":      entry,
                "exit":       cur,
                "pnl":        pnl,
                "pnl_pct":    (cur / entry - 1) * 100,
                "reason":     "end",
                "hold_days":  n_days - 1 - pos["entry_idx"],
            })

    return _metrics(daily_equity, trades, verbose=verbose)


def _top_n(day_prices, prices, i, lookback, n):
    """Return set of top-N sector symbols by lookback-day return."""
    returns = {}
    for sym in SECTORS:
        if sym not in day_prices:
            continue
        past_idx = i - lookback
        if past_idx < 0:
            continue
        past = prices[sym][past_idx]
        if np.isnan(past) or past <= 0:
            continue
        returns[sym] = day_prices[sym] / past - 1.0
    ranked = sorted(returns, key=returns.get, reverse=True)
    return set(ranked[:n])


def _open_value(positions, day_prices):
    return sum(
        pos["shares"] * day_prices[sym]
        for sym, pos in positions.items()
        if sym in day_prices
    )


def _metrics(daily_equity: list, trades: list, verbose: bool = True) -> dict:
    eq   = np.array(daily_equity, dtype=float)
    rets = np.diff(eq) / eq[:-1]

    years   = len(eq) / 252
    cagr    = (eq[-1] / eq[0]) ** (1 / years) - 1 if years > 0 else 0
    sharpe  = (rets.mean() / rets.std() * np.sqrt(252)) if rets.std() > 0 else 0
    peak    = np.maximum.accumulate(eq)
    dd      = (eq - peak) / peak
    max_dd  = dd.min()

    closed  = [t for t in trades if t["reason"] != "end"]
    wins    = [t for t in closed if t["pnl"] > 0]
    wr      = len(wins) / len(closed) * 100 if closed else 0
    avg_win = np.mean([t["pnl_pct"] for t in wins]) if wins else 0
    avg_los = np.mean([t["pnl_pct"] for t in closed if t["pnl"] <= 0]) if closed else 0
    avg_hold= np.mean([t["hold_days"] for t in closed]) if closed else 0

    total_pnl     = eq[-1] - eq[0]
    total_pnl_pct = (eq[-1] / eq[0] - 1) * 100
    trades_per_yr = len(closed) / years if years > 0 else 0

    if verbose:
        _print_results(eq, closed, cagr, sharpe, max_dd, wr,
                       avg_win, avg_los, avg_hold, trades_per_yr,
                       total_pnl, total_pnl_pct)

    return {
        "cagr": cagr, "sharpe": sharpe, "max_dd": max_dd,
        "wr": wr, "n_trades": len(closed), "trades_per_yr": trades_per_yr,
        "avg_hold": avg_hold, "total_pnl_pct": total_pnl_pct,
        "avg_win": avg_win, "avg_loss": avg_los,
        "equity": eq, "trades": trades,
    }


def _print_results(eq, closed, cagr, sharpe, max_dd, wr,
                   avg_win, avg_los, avg_hold, trades_per_yr,
                   total_pnl, total_pnl_pct):
    years = len(eq) / 252
    print()
    print("=" * 60)
    print("  SECTOR ROTATION BACKTEST RESULTS")
    print(f"  Period : {len(eq)} trading days  (~{years:.1f} years)")
    print(f"  Capital: ${STARTING_CAP:,.0f} → ${eq[-1]:,.0f}")
    print("=" * 60)
    print(f"  CAGR            : {cagr*100:+.1f}% per year")
    print(f"  Total Return    : {total_pnl_pct:+.1f}%  (${total_pnl:+,.0f})")
    print(f"  Sharpe Ratio    : {sharpe:.2f}")
    print(f"  Max Drawdown    : {max_dd*100:.1f}%")
    print(f"  Win Rate        : {wr:.1f}%")
    print(f"  Avg Win         : {avg_win:+.1f}%")
    print(f"  Avg Loss        : {avg_los:+.1f}%")
    print(f"  Avg Hold        : {avg_hold:.0f} days")
    print(f"  Trades/Year     : {trades_per_yr:.1f}")
    print(f"  Total Trades    : {len(closed)}")
    print("=" * 60)

    # Signal frequency breakdown
    print(f"\n  SIGNAL FREQUENCY")
    print(f"  Per week   : ~{trades_per_yr/52:.1f} entries  +  ~{trades_per_yr/52:.1f} exits")
    print(f"  Per month  : ~{trades_per_yr/12:.1f} entries  +  ~{trades_per_yr/12:.1f} exits")
    print(f"  Per year   : ~{trades_per_yr:.0f} entries  +  ~{trades_per_yr:.0f} exits")

    # Per-sector breakdown
    from collections import Counter
    sector_counts = Counter(t["symbol"] for t in closed)
    sector_wins   = Counter(t["symbol"] for t in closed if t["pnl"] > 0)
    print(f"\n  PER-SECTOR BREAKDOWN")
    print(f"  {'Sector':<7}  {'Trades':>6}  {'WR':>6}  {'Avg P&L%':>9}")
    print(f"  {'──────':<7}  {'──────':>6}  {'──────':>6}  {'──────────':>9}")
    for sym in sorted(sector_counts, key=sector_counts.get, reverse=True):
        sym_trades = [t for t in closed if t["symbol"] == sym]
        sym_wr     = sector_wins[sym] / sector_counts[sym] * 100
        sym_avg    = np.mean([t["pnl_pct"] for t in sym_trades])
        print(f"  {sym:<7}  {sector_counts[sym]:>6}  {sym_wr:>5.0f}%  {sym_avg:>+9.1f}%")

    # Best / worst trades
    if closed:
        best  = max(closed, key=lambda t: t["pnl_pct"])
        worst = min(closed, key=lambda t: t["pnl_pct"])
        print(f"\n  Best trade : {best['symbol']}  {best['pnl_pct']:+.1f}%  ({best['exit_date']})")
        print(f"  Worst trade: {worst['symbol']}  {worst['pnl_pct']:+.1f}%  ({worst['exit_date']})")

    # Exit reason breakdown
    from collections import Counter as C2
    reasons = C2(t["reason"] for t in closed)
    print(f"\n  EXIT REASONS")
    for reason, cnt in sorted(reasons.items(), key=lambda x: -x[1]):
        pct = cnt / len(closed) * 100
        print(f"  {reason:<12}: {cnt:>3}  ({pct:.0f}%)")
    print()


# ── Buy & Hold benchmark ──────────────────────────────────────────────────────

def _buy_and_hold(close: pd.DataFrame) -> dict:
    """Equal-weight buy&hold across all 11 sectors as benchmark."""
    close_clean = close.dropna()
    daily_ret   = close_clean.pct_change().dropna()
    port_ret    = daily_ret.mean(axis=1)
    eq          = STARTING_CAP * (1 + port_ret).cumprod()

    years  = len(eq) / 252
    cagr   = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1
    sharpe = port_ret.mean() / port_ret.std() * np.sqrt(252)
    peak   = eq.cummax()
    max_dd = ((eq - peak) / peak).min()

    return {"cagr": cagr, "sharpe": sharpe, "max_dd": max_dd,
            "total_pct": (eq.iloc[-1] / eq.iloc[0] - 1) * 100}


# ── Grid search ───────────────────────────────────────────────────────────────

def run_grid(close: pd.DataFrame):
    sl_values  = [0.05, 0.08, 0.10, 0.12, 0.15]
    tp_values  = [0.15, 0.20, 0.25, 0.30]
    lb_values  = [42, 63, 84]   # 2m, 3m, 4m lookback

    combos  = list(product(sl_values, tp_values, lb_values))
    results = []
    total   = len(combos)

    print(f"\nGrid search: {total} combinations  (SL × TP × Lookback)")
    print("─" * 60)

    for k, (sl, tp, lb) in enumerate(combos):
        m = run_backtest(close, lookback=lb, stop_loss=sl,
                         take_profit=tp, verbose=False)
        results.append({
            "SL%": f"{sl*100:.0f}%",
            "TP%": f"{tp*100:.0f}%",
            "LB":  lb,
            "Sharpe": round(m["sharpe"], 3),
            "CAGR%":  round(m["cagr"] * 100, 1),
            "MaxDD%": round(m["max_dd"] * 100, 1),
            "WR%":    round(m["wr"], 1),
            "Trades/yr": round(m["trades_per_yr"], 1),
        })
        if (k + 1) % 10 == 0:
            print(f"  {k+1}/{total} done...")

    df = pd.DataFrame(results).sort_values("Sharpe", ascending=False)
    print("\n  TOP 10 by Sharpe:")
    print(df.head(10).to_string(index=False))

    best = results[0]
    for r in results:
        if r["Sharpe"] > best["Sharpe"]:
            best = r
    print(f"\n  BEST: SL={best['SL%']}  TP={best['TP%']}  LB={best['LB']}d  "
          f"→ Sharpe={best['Sharpe']}  CAGR={best['CAGR%']}%  DD={best['MaxDD%']}%")

    df.to_csv("data/etf_grid_results.csv", index=False)
    print("  Saved → data/etf_grid_results.csv")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="ETF Sector Rotation Backtest")
    ap.add_argument("--rank-drop", action="store_true",
                    help="Also exit when sector falls out of top 3")
    ap.add_argument("--grid",      action="store_true",
                    help="Run full parameter grid search")
    ap.add_argument("--plot",      action="store_true",
                    help="Plot equity curve (requires matplotlib)")
    ap.add_argument("--years",     type=int, default=10,
                    help="Years of history to download (default: 10)")
    args = ap.parse_args()

    close = _download(years=args.years)

    if args.grid:
        run_grid(close)
        return

    # ── Default run ──────────────────────────────────────────────────────────
    print(f"\n  Strategy : Sector Rotation (Top {TOP_N} of {len(SECTORS)} sectors)")
    print(f"  Rules    : SL={STOP_LOSS*100:.0f}%  TP={TAKE_PROFIT*100:.0f}%  "
          f"Lookback={LOOKBACK}d  RankDrop={'YES' if args.rank_drop else 'NO'}")
    print(f"  Positions: {TOP_N} slots  |  Commission: {COMMISSION*100:.2f}% per trade")

    m = run_backtest(close, rank_drop=args.rank_drop)

    # ── Buy & Hold benchmark ─────────────────────────────────────────────────
    bh = _buy_and_hold(close)
    print(f"  BUY & HOLD benchmark (equal-weight all sectors):")
    print(f"    CAGR {bh['cagr']*100:+.1f}%  |  Sharpe {bh['sharpe']:.2f}  "
          f"|  MaxDD {bh['max_dd']*100:.1f}%  |  Total {bh['total_pct']:+.1f}%")
    print()

    # ── Dollar P&L projection on actual account ──────────────────────────────
    etf_budget = 998_000 * 0.15   # 15% of ~998k EUR account
    proj_annual = etf_budget * m["cagr"]
    print(f"  PROJECTED ANNUAL PROFIT on your account:")
    print(f"    ETF budget (15% of 998k EUR) : ~{etf_budget:,.0f} EUR")
    print(f"    Strategy CAGR                : {m['cagr']*100:+.1f}%")
    print(f"    Expected annual P&L          : ~{proj_annual:+,.0f} EUR/year")
    print(f"    As % of total account (998k) : ~{m['cagr']*0.15*100:+.2f}% contribution")
    print()

    if args.plot:
        try:
            import matplotlib.pyplot as plt
            eq = m["equity"]
            days = np.arange(len(eq))
            fig, ax = plt.subplots(figsize=(12, 5))
            ax.plot(days, eq, label=f"Sector Rotation  CAGR={m['cagr']*100:.1f}%", color="royalblue")
            bh_eq = STARTING_CAP * (1 + close.pct_change().dropna().mean(axis=1)).cumprod()
            ax.plot(np.arange(len(bh_eq)), bh_eq.values,
                    label=f"Buy & Hold  CAGR={bh['cagr']*100:.1f}%",
                    color="grey", alpha=0.6, linestyle="--")
            ax.set_title("ETF Sector Rotation vs Buy & Hold")
            ax.set_xlabel("Trading Days")
            ax.set_ylabel("Equity (USD)")
            ax.legend()
            ax.grid(alpha=0.3)
            plt.tight_layout()
            plt.savefig("data/etf_backtest_equity.png", dpi=120)
            print("  Chart saved → data/etf_backtest_equity.png")
            plt.show()
        except ImportError:
            print("  matplotlib not installed — skipping plot")


if __name__ == "__main__":
    main()
