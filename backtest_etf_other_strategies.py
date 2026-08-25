"""
backtest_etf_other_strategies.py
-----------------------------------
10-year historical backtest for the 3 remaining ETF strategies that were
never backtested before (Risk-Off, Mean Reversion, Dual MA) -- added
2026-08-26 after the user asked "which is more profitable than
[Sector] Rotation?" and there was no real data to answer with, only
backtest_etf.py's Sector Rotation numbers.

Entry logic for each strategy is transcribed faithfully from the REAL
production code (saxo_etf_strategy/core/etf_strategy.py) -- same SMA/RSI/
MA-crossover formulas, same lookback windows, same target universes --
not reimplemented from scratch or guessed. Confirmed first (by reading
saxo_etf_strategy/core/etf_executor.py) that EXIT logic is uniform across
ALL 5 strategies: always just stop_loss_pct=8% / take_profit_pct=20%
(ETFRiskConfig defaults) via review_exits() -- no strategy has its own
distinct exit rule, only entry signal generation differs. This means one
shared backtest engine (adapted from backtest_etf.py's, which already
handles SL/TP + CAGR/Sharpe/MaxDD/WR reporting for Sector Rotation) can
drive all 3 strategies here, differing only in each day's ranked
candidate list.

Momentum Scan (the 5th strategy) is deliberately excluded -- it scans
Saxo's live ~8,924-instrument full universe with exchange/ticker-length/
description filtering, which has no equivalent historical dataset to
replay accurately (no historical exchange listing/description data,
survivorship-bias risk). It's also already the least-mature strategy per
docs/etf_strategies.md ("not currently selected... not documented until
the 2026-08-20 audit").

Position sizing: equal-weight across up to max_positions open slots,
matching backtest_etf.py's existing Sector Rotation methodology exactly
(not the real bot's rank-weighted budget allocation) -- kept consistent
so the CAGR/Sharpe/MaxDD numbers are directly comparable apples-to-apples
against Sector Rotation's already-published baseline, rather than
differing due to a sizing-methodology change.

Usage:
    python backtest_etf_other_strategies.py                 # all 3 strategies, 10y
    python backtest_etf_other_strategies.py --years 5
    python backtest_etf_other_strategies.py --strategy risk_off
"""

import argparse
import sys
import numpy as np
import pandas as pd
import yfinance as yf

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

STOP_LOSS    = 0.08
TAKE_PROFIT  = 0.20
COMMISSION   = 0.0008
STARTING_CAP = 100_000

# ── Strategy definitions (transcribed from saxo_etf_strategy/core/etf_strategy.py) ──

RISK_OFF_EQUITY     = ["SPY", "QQQ"]
RISK_OFF_DEFENSIVE  = ["TLT", "GLD"]
RISK_OFF_SMA        = 200

MEAN_REV_TARGETS    = ["SPY", "QQQ", "IWM", "EFA", "EEM"]
MEAN_REV_RSI_ENTRY  = 30
MEAN_REV_DIP_PCT    = 0.05
MEAN_REV_SMA        = 20

DUAL_MA_UNIVERSE = [
    "SPY", "QQQ", "IWM", "EFA", "EEM", "VTI", "AGG", "LQD", "TLT", "GLD",
    "SHY", "HYG", "XLK", "XLV", "XLF", "XLE", "XLI", "XLY", "XLP", "XLU",
    "XLRE", "XLB", "SMH", "XHB", "XBI", "IBB", "KWEB", "ARKG", "ARKK", "ARKF",
    "VWO", "VEA", "BND", "VNQ", "GDX", "GDXJ", "SLV", "IAU", "USO", "UNG",
    "DIA", "MDY", "IJR", "IEMG", "SPYV", "SPYG", "VTV", "VUG", "VO", "VB",
]
DUAL_MA_FAST = 20
DUAL_MA_SLOW = 100
DUAL_MA_MAX_CANDIDATES = 10   # matches etf_config.py's current max_candidates_per_run

MAX_POSITIONS = 10   # matches ETFRiskConfig.max_positions (account-wide cap, all strategies)


def _sma(closes: np.ndarray, i: int, period: int) -> float:
    if i + 1 < period:
        return np.nan
    return closes[i - period + 1:i + 1].mean()


def _rsi(closes: np.ndarray, i: int, period: int = 14) -> float:
    if i + 1 < period + 1:
        return 50.0
    window = closes[i - period:i + 1]
    deltas = np.diff(window)
    gains  = deltas[deltas > 0].sum() / period
    losses = -deltas[deltas < 0].sum() / period
    if losses == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + gains / losses)


def _download(tickers: list, years: int) -> pd.DataFrame:
    print(f"Downloading {years}y of daily closes for {len(tickers)} tickers...")
    raw = yf.download(tickers, period=f"{years}y", auto_adjust=True, progress=False)
    close = raw["Close"] if "Close" in raw.columns else raw
    if isinstance(close.columns, pd.MultiIndex):
        close.columns = close.columns.get_level_values(0)
    close = close.reindex(columns=tickers)
    print(f"  Got {len(close)} trading days  ({close.index[0].date()} -> {close.index[-1].date()})")
    return close


# ── Per-strategy daily signal functions ────────────────────────────────────
# Each returns a ranked list of symbols (best first) eligible for entry
# TODAY (index i), or [] if none qualify. Mirrors each strategy's real
# generate_signals() logic exactly.

def _signal_risk_off(close: pd.DataFrame, i: int) -> list:
    if "SPY" not in close.columns:
        return []
    spy = close["SPY"].values
    if np.isnan(spy[i]):
        return []
    sma200 = _sma(spy, i, RISK_OFF_SMA)
    if np.isnan(sma200):
        return []
    return RISK_OFF_EQUITY if spy[i] > sma200 else RISK_OFF_DEFENSIVE


def _signal_mean_reversion(close: pd.DataFrame, i: int) -> list:
    scored = []
    for sym in MEAN_REV_TARGETS:
        if sym not in close.columns:
            continue
        closes = close[sym].values
        if i < 22 or np.isnan(closes[i]):
            continue
        rsi   = _rsi(closes, i)
        sma20 = _sma(closes, i, MEAN_REV_SMA)
        if np.isnan(sma20) or sma20 <= 0:
            continue
        price = closes[i]
        dip   = (sma20 - price) / sma20
        if rsi < MEAN_REV_RSI_ENTRY and dip >= MEAN_REV_DIP_PCT:
            score = (MEAN_REV_RSI_ENTRY - rsi) + dip * 100
            scored.append((score, sym))
    scored.sort(reverse=True)
    return [sym for _, sym in scored]


def _signal_dual_ma(close: pd.DataFrame, i: int) -> list:
    scored = []
    for sym in DUAL_MA_UNIVERSE:
        if sym not in close.columns:
            continue
        closes = close[sym].values
        if i < DUAL_MA_SLOW or np.isnan(closes[i]):
            continue
        fast_ma = _sma(closes, i, DUAL_MA_FAST)
        slow_ma = _sma(closes, i, DUAL_MA_SLOW)
        if np.isnan(slow_ma) or slow_ma <= 0 or fast_ma <= slow_ma:
            continue
        scored.append((fast_ma / slow_ma - 1.0, sym))
    scored.sort(reverse=True)
    return [sym for _, sym in scored[:DUAL_MA_MAX_CANDIDATES]]


STRATEGIES = {
    "risk_off":       (_signal_risk_off, RISK_OFF_EQUITY + RISK_OFF_DEFENSIVE, RISK_OFF_SMA),
    "mean_reversion": (_signal_mean_reversion, MEAN_REV_TARGETS, 60),
    "dual_ma":        (_signal_dual_ma, DUAL_MA_UNIVERSE, DUAL_MA_SLOW),
}


# ── Shared backtest engine (adapted from backtest_etf.py) ─────────────────

def run_backtest(close: pd.DataFrame, signal_fn, warmup: int, max_positions: int = MAX_POSITIONS) -> dict:
    equity = float(STARTING_CAP)
    cash   = float(STARTING_CAP)
    positions = {}   # {symbol: {"entry": float, "shares": float, "entry_idx": int}}

    daily_equity = []
    trades = []

    dates  = close.index
    n_days = len(dates)

    for i in range(warmup, n_days):
        day_prices = {sym: close[sym].values[i] for sym in close.columns
                      if not np.isnan(close[sym].values[i])}
        if not day_prices:
            daily_equity.append(equity)
            continue

        # ── Exits: SL/TP only, identical rule for every strategy ──────────
        to_exit = []
        for sym, pos in positions.items():
            if sym not in day_prices:
                continue
            cur, entry = day_prices[sym], pos["entry"]
            ret = (cur - entry) / entry
            if ret <= -STOP_LOSS:
                to_exit.append((sym, "SL"))
            elif ret >= TAKE_PROFIT:
                to_exit.append((sym, "TP"))

        for sym, reason in to_exit:
            pos = positions.pop(sym)
            cur, entry, shares = day_prices[sym], pos["entry"], pos["shares"]
            gross = (cur - entry) * shares
            cost  = cur * shares * COMMISSION
            pnl   = gross - cost
            cash += entry * shares + pnl
            trades.append({
                "exit_date": str(dates[i].date()), "symbol": sym,
                "entry": entry, "exit": cur, "pnl": pnl,
                "pnl_pct": (cur / entry - 1) * 100, "reason": reason,
                "hold_days": i - pos["entry_idx"],
            })

        # ── Entries: ranked signal list, skip already-held, fill free slots ──
        ranked = signal_fn(close, i)
        for sym in ranked:
            if len(positions) >= max_positions:
                break
            if sym in positions or sym not in day_prices:
                continue
            price  = day_prices[sym]
            budget = equity / max_positions
            shares = budget / price
            cost   = price * shares * COMMISSION
            if cash < price * shares + cost:
                continue
            cash -= price * shares + cost
            positions[sym] = {"entry": price, "shares": shares, "entry_idx": i}

        open_val = sum(pos["shares"] * day_prices[sym] for sym, pos in positions.items() if sym in day_prices)
        equity = cash + open_val
        daily_equity.append(equity)

    last_prices = {sym: close[sym].values[-1] for sym in close.columns if not np.isnan(close[sym].values[-1])}
    for sym, pos in positions.items():
        if sym in last_prices:
            cur, entry, shares = last_prices[sym], pos["entry"], pos["shares"]
            gross = (cur - entry) * shares
            pnl = gross - cur * shares * COMMISSION
            trades.append({
                "exit_date": str(dates[-1].date()), "symbol": sym,
                "entry": entry, "exit": cur, "pnl": pnl,
                "pnl_pct": (cur / entry - 1) * 100, "reason": "end",
                "hold_days": n_days - 1 - pos["entry_idx"],
            })

    return _metrics(daily_equity, trades)


def _metrics(daily_equity: list, trades: list) -> dict:
    eq = np.array(daily_equity, dtype=float)
    if len(eq) < 2:
        return {"error": "insufficient data"}
    rets = np.diff(eq) / eq[:-1]

    years  = len(eq) / 252
    cagr   = (eq[-1] / eq[0]) ** (1 / years) - 1 if years > 0 and eq[0] > 0 else 0
    sharpe = (rets.mean() / rets.std() * np.sqrt(252)) if rets.std() > 0 else 0
    peak   = np.maximum.accumulate(eq)
    dd     = (eq - peak) / peak
    max_dd = dd.min()

    closed  = [t for t in trades if t["reason"] != "end"]
    wins    = [t for t in closed if t["pnl"] > 0]
    wr      = len(wins) / len(closed) * 100 if closed else 0
    trades_per_yr = len(closed) / years if years > 0 else 0

    return {
        "cagr": cagr * 100, "sharpe": sharpe, "max_dd": max_dd * 100,
        "win_rate": wr, "n_trades": len(closed), "trades_per_yr": trades_per_yr,
        "total_return_pct": (eq[-1] / eq[0] - 1) * 100,
        "final_equity": eq[-1],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=10)
    ap.add_argument("--strategy", choices=list(STRATEGIES), default=None)
    args = ap.parse_args()

    strategies = [args.strategy] if args.strategy else list(STRATEGIES)
    all_tickers = sorted(set(t for s in strategies for t in STRATEGIES[s][1]))
    close = _download(all_tickers, args.years)

    print()
    print("=" * 70)
    print(f"  {'Strategy':<16} {'CAGR':>8} {'Sharpe':>8} {'MaxDD':>8} {'WR%':>7} {'Trades':>8} {'Trades/Yr':>10}")
    print("=" * 70)
    results = {}
    for strat in strategies:
        fn, tickers, warmup = STRATEGIES[strat]
        r = run_backtest(close, fn, warmup)
        results[strat] = r
        if "error" in r:
            print(f"  {strat:<16} ERROR: {r['error']}")
            continue
        print(f"  {strat:<16} {r['cagr']:>+7.1f}% {r['sharpe']:>8.2f} {r['max_dd']:>+7.1f}% "
              f"{r['win_rate']:>6.1f}% {r['n_trades']:>8} {r['trades_per_yr']:>10.1f}")
    print("=" * 70)
    print()
    print("  (reference) Sector Rotation, already backtested via backtest_etf.py:")
    print(f"  {'sector_rotation':<16} {'+12.4%':>8} {'0.84':>8} {'-31.6%':>8} {'57.1%':>7} {'49':>8} {'5.0':>10}")


if __name__ == "__main__":
    main()
