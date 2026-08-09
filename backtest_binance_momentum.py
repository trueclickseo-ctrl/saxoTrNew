"""
backtest_binance_momentum.py
------------------------------
Walk-forward backtest for the Binance crypto momentum / trend-following strategy.

Run (single pass with default parameters):
    .venv/Scripts/python backtest_binance_momentum.py

Run (full parameter grid search -- resumes from CSV if stopped mid-way):
    .venv/Scripts/python backtest_binance_momentum.py --grid

Run (print ranked summary from a previous grid run):
    .venv/Scripts/python backtest_binance_momentum.py --summary

Run (delete cache + grid CSV and re-download fresh):
    .venv/Scripts/python backtest_binance_momentum.py --reset

Data: maximum available daily OHLCV per symbol from Binance mainnet public API
      (no API key required for klines). Each symbol returns whatever history
      Binance holds; the backtest uses the common-date intersection across all
      symbols. Cached to data/backtest_binance_mom_cache.pkl; re-fetched on
      --reset, if cache is from a previous day, or if SYMBOLS has changed.

Signal logic (both must fire on bar[i] close -> entry at bar[i+1] open):
    1. RSI-14 in [RSI_LOW, RSI_HIGH]      -- momentum zone (not oversold, not overbought)
    2. close[i] > max(close[i-N : i])     -- Donchian breakout above prior-N-day high

Exit (whichever fires first, checked each bar after entry):
    1. close[j] < min(close[j-M : j])     -- Donchian trailing stop (new M-day low)
    2. close[j] < entry * (1-STOP_PCT)    -- hard stop-loss  (0 = disabled)
    3. days_held >= MAX_HOLD              -- hard time cap    (0 = disabled)

Enable criterion: Sharpe >= 1.0 AND Win Rate >= 55% AND Max Drawdown < 20% AND N >= 20.
"""

import sys
import os
import argparse
import pickle
import csv
from datetime import date
from itertools import product

import numpy as np
import pandas as pd

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
DATA_DIR  = os.path.join(BASE_DIR, "data")
CACHE_PKL = os.path.join(DATA_DIR, "backtest_binance_mom_cache.pkl")
CSV_PATH  = os.path.join(DATA_DIR, "backtest_binance_mom_grid.csv")

sys.path.insert(0, BASE_DIR)

from strategies.binance.mean_reversion import _rsi

SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "ADAUSDT",
    "XRPUSDT", "DOGEUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT",
]
MAX_HISTORY_START = "1 Jan 2017"
COMMISSION_PCT    = 0.001     # Binance taker 0.1%
STARTING_USDT     = 10_000.0
MAX_POSITIONS     = 3
# Warmup: RSI-14 needs 14 bars; max breakout lookback is 25; add buffer.
WARMUP_BARS       = 40

DEFAULT_PARAMS = {
    "RSI_LOW":       50,
    "RSI_HIGH":      70,
    "BREAKOUT_DAYS": 20,
    "EXIT_DAYS":     10,
    "STOP_PCT":      0.0,
    "MAX_HOLD":      0,
}

# Grid: 3x3x3x3x3x3 = 729 combinations
GRID = {
    "RSI_LOW":       [45, 50, 55],
    "RSI_HIGH":      [65, 70, 75],
    "BREAKOUT_DAYS": [15, 20, 25],
    "EXIT_DAYS":     [7,  10, 15],
    "STOP_PCT":      [0.0, 0.05, 0.10],
    "MAX_HOLD":      [0,   30,   60],
}

CSV_FIELDS = [
    "RSI_LOW", "RSI_HIGH", "BREAKOUT_DAYS", "EXIT_DAYS", "STOP_PCT", "MAX_HOLD",
    "sharpe", "win_rate", "profit_factor", "max_dd", "cagr", "n_trades", "passed",
]

# Trend-following pass criteria (different from mean-reversion):
# WR is structurally low for breakout strategies; PF and MaxDD are the right levers.
TF_SHARPE_MIN = 1.0
TF_PF_MIN     = 2.0
TF_DD_MAX     = 0.40
TF_N_MIN      = 20


# ── Data layer ────────────────────────────────────────────────────────────────

def _download_raw() -> dict[str, pd.DataFrame]:
    try:
        from binance.client import Client
    except ImportError:
        print("ERROR: python-binance not installed. Run: pip install python-binance")
        sys.exit(1)

    client    = Client("", "", testnet=False)
    start_str = MAX_HISTORY_START

    print(f"Downloading {len(SYMBOLS)} symbols from Binance mainnet (from {start_str})...")
    data: dict[str, pd.DataFrame] = {}

    for sym in SYMBOLS:
        print(f"  {sym} ... ", end="", flush=True)
        klines = client.get_historical_klines(
            sym, Client.KLINE_INTERVAL_1DAY, start_str
        )
        rows = [
            {
                "date":   pd.Timestamp(int(k[0]), unit="ms", tz="UTC").normalize(),
                "open":   float(k[1]),
                "high":   float(k[2]),
                "low":    float(k[3]),
                "close":  float(k[4]),
                "volume": float(k[5]),
            }
            for k in klines
        ]
        df = pd.DataFrame(rows).set_index("date")
        data[sym] = df
        print(f"{len(df)} bars  ({df.index[0].date()} -> {df.index[-1].date()})")

    print()
    return data


def load_data() -> dict[str, pd.DataFrame]:
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(CACHE_PKL):
        with open(CACHE_PKL, "rb") as f:
            cached = pickle.load(f)
        same_date    = cached.get("date")    == date.today().isoformat()
        same_symbols = cached.get("symbols") == sorted(SYMBOLS)
        if same_date and same_symbols:
            print("Using cached data from today.\n")
            return cached["data"]
        if not same_symbols:
            print("Symbol set changed -- discarding stale cache and re-downloading...")
            if os.path.exists(CSV_PATH):
                os.remove(CSV_PATH)
                print(f"  Wiped stale grid CSV: {CSV_PATH}")

    data = _download_raw()
    with open(CACHE_PKL, "wb") as f:
        pickle.dump({
            "date":    date.today().isoformat(),
            "symbols": sorted(SYMBOLS),
            "data":    data,
        }, f)
    print(f"Cached to {CACHE_PKL}\n")
    return data


# ── Indicator pre-computation ─────────────────────────────────────────────────

# Lookback values used in the grid -- precompute rolling max/min for all of them.
_BREAKOUT_LOOKBACKS = [15, 20, 25]
_EXIT_LOOKBACKS     = [7, 10, 15]


def _precompute(data: dict[str, pd.DataFrame]) -> dict[str, dict]:
    """
    Pre-compute RSI-14, rolling max (for breakout signal), and rolling min
    (for Donchian exit) for every lookback value in the grid.

    roll_max_N[i] = max(close[i-N : i])  -- prior-N-day high, EXCLUDING bar i
    roll_min_M[i] = min(close[i-M : i])  -- prior-M-day low,  EXCLUDING bar i

    Both exclude bar i to avoid look-ahead bias: we know these values at bar i
    close and enter/exit at bar i+1 open.
    """
    print(f"Pre-computing indicators for {len(data)} symbols...", flush=True)
    ind: dict[str, dict] = {}

    for sym, df in data.items():
        closes = df["close"].tolist()
        n      = len(closes)

        rsi_vals = [_rsi(closes[:i + 1], period=14) for i in range(n)]

        roll_max = {}
        for lb in _BREAKOUT_LOOKBACKS:
            vals = []
            for i in range(n):
                if i < lb:
                    vals.append(None)
                else:
                    vals.append(max(closes[i - lb: i]))
            roll_max[lb] = vals

        roll_min = {}
        for lb in _EXIT_LOOKBACKS:
            vals = []
            for i in range(n):
                if i < lb:
                    vals.append(None)
                else:
                    vals.append(min(closes[i - lb: i]))
            roll_min[lb] = vals

        ind[sym] = {
            "rsi":      rsi_vals,
            "roll_max": roll_max,
            "roll_min": roll_min,
        }
        print(f"  {sym}: {n} bars", flush=True)

    print("  Done.\n")
    return ind


# ── Simulation ────────────────────────────────────────────────────────────────

def simulate(data: dict[str, pd.DataFrame], ind: dict, params: dict) -> dict:
    """
    Walk-forward simulation across all symbols with a shared USDT portfolio.

    Signal on bar[i] close -> entry at bar[i+1] open.
    Exit priority (checked each bar while position is open):
      1. Hard stop-loss: close < entry * (1 - STOP_PCT)   [if STOP_PCT > 0]
      2. Max-hold cap:   days_held >= MAX_HOLD             [if MAX_HOLD > 0]
      3. Donchian exit:  close < roll_min[EXIT_DAYS][bar]
    All exits execute at the open of the following bar.
    """
    rsi_low      = params["RSI_LOW"]
    rsi_high     = params["RSI_HIGH"]
    breakout_d   = params["BREAKOUT_DAYS"]
    exit_d       = params["EXIT_DAYS"]
    stop_pct     = params["STOP_PCT"]
    max_hold     = params["MAX_HOLD"]

    common_idx = sorted(
        set.intersection(*[set(data[s].index) for s in SYMBOLS])
    )
    if len(common_idx) < WARMUP_BARS + 2:
        return {"trades": [], "sharpe": 0.0, "win_rate": 0.0,
                "max_dd": 1.0, "cagr": 0.0, "sym_stats": {}, "eq_curve": []}

    sym_pos = {
        sym: {d: i for i, d in enumerate(data[sym].index)}
        for sym in SYMBOLS
    }

    cash       = STARTING_USDT
    peak_eq    = cash
    open_pos   = {}
    trades     = []
    eq_curve   = []

    for bar, today in enumerate(common_idx):
        if bar < WARMUP_BARS:
            eq_curve.append({"date": today, "equity": cash})
            continue

        def _get(sym, col):
            si = sym_pos[sym].get(today)
            return float(data[sym][col].iat[si]) if si is not None else None

        def _ind_rsi(sym):
            si = sym_pos[sym].get(today)
            return ind[sym]["rsi"][si] if si is not None else None

        def _roll_max(sym, lb):
            si = sym_pos[sym].get(today)
            return ind[sym]["roll_max"][lb][si] if si is not None else None

        def _roll_min(sym, lb):
            si = sym_pos[sym].get(today)
            return ind[sym]["roll_min"][lb][si] if si is not None else None

        # Mark-to-market
        open_val = sum(
            pos["units"] * (_get(sym, "close") or pos["entry_price"])
            for sym, pos in open_pos.items()
        )
        total_eq = cash + open_val
        peak_eq  = max(peak_eq, total_eq)

        # ── Exit checks ───────────────────────────────────────────────────────
        to_close = []
        for sym, pos in list(open_pos.items()):
            days_held = bar - pos["entry_bar"]
            close     = _get(sym, "close")
            if close is None:
                continue

            exit_price = None
            reason     = ""

            if stop_pct > 0 and close < pos["entry_price"] * (1 - stop_pct):
                exit_price = close
                reason     = f"hard_stop -{stop_pct*100:.0f}%"
            elif max_hold > 0 and days_held >= max_hold:
                exit_price = close
                reason     = f"max_hold {days_held}d"
            else:
                trail_low = _roll_min(sym, exit_d)
                if trail_low is not None and close < trail_low:
                    exit_price = close
                    reason     = f"donchian_exit {exit_d}d"

            if exit_price is not None:
                to_close.append((sym, exit_price, reason, pos, days_held))

        for sym, exit_price, reason, pos, days_held in to_close:
            # Exits execute at next bar's open; approximated as today's close
            # for P&L (consistent with entry approximation).
            proceeds = pos["units"] * exit_price * (1 - COMMISSION_PCT)
            pnl_net  = proceeds - pos["cost"]
            cash    += proceeds
            trades.append({
                "symbol":      sym,
                "entry_date":  pos["entry_date"],
                "exit_date":   today,
                "entry_price": pos["entry_price"],
                "exit_price":  exit_price,
                "units":       pos["units"],
                "pnl_net":     pnl_net,
                "pnl_pct":     (exit_price / pos["entry_price"] - 1) * 100,
                "reason":      reason,
                "days_held":   days_held,
            })
            del open_pos[sym]

        # ── Entry scan ────────────────────────────────────────────────────────
        slots_free = MAX_POSITIONS - len(open_pos)
        if slots_free > 0 and bar + 1 < len(common_idx):
            current_total = cash + sum(
                pos["units"] * (_get(s, "close") or pos["entry_price"])
                for s, pos in open_pos.items()
            )
            slot_size = current_total / MAX_POSITIONS

            for sym in SYMBOLS:
                if slots_free <= 0:
                    break
                if sym in open_pos:
                    continue

                close      = _get(sym, "close")
                rsi_val    = _ind_rsi(sym)
                prior_high = _roll_max(sym, breakout_d)

                if any(v is None for v in [close, rsi_val, prior_high]):
                    continue

                if not (rsi_low <= rsi_val <= rsi_high and close > prior_high):
                    continue

                # Entry at next bar's open
                next_date = common_idx[bar + 1]
                next_si   = sym_pos[sym].get(next_date)
                if next_si is None:
                    continue
                entry_price = float(data[sym]["open"].iat[next_si])
                if entry_price <= 0:
                    continue

                cost  = slot_size * (1 + COMMISSION_PCT)
                units = slot_size / entry_price
                cash -= cost

                open_pos[sym] = {
                    "entry_price": entry_price,
                    "entry_bar":   bar,
                    "entry_date":  next_date,
                    "units":       units,
                    "cost":        cost,
                }
                slots_free -= 1

        eq_curve.append({"date": today, "equity": total_eq})

    # Force-close any remaining positions at last bar's close
    last_date = common_idx[-1]
    for sym, pos in open_pos.items():
        close = float(data[sym]["close"].iat[sym_pos[sym][last_date]])
        proceeds = pos["units"] * close * (1 - COMMISSION_PCT)
        pnl_net  = proceeds - pos["cost"]
        trades.append({
            "symbol":      sym,
            "entry_date":  pos["entry_date"],
            "exit_date":   last_date,
            "entry_price": pos["entry_price"],
            "exit_price":  close,
            "units":       pos["units"],
            "pnl_net":     pnl_net,
            "pnl_pct":     (close / pos["entry_price"] - 1) * 100,
            "reason":      "end_of_data",
            "days_held":   len(common_idx) - 1 - pos["entry_bar"],
        })

    # ── Metrics ───────────────────────────────────────────────────────────────
    eq_vals = [e["equity"] for e in eq_curve if e["date"] >= common_idx[WARMUP_BARS]]
    if len(eq_vals) < 2:
        return {"trades": trades, "sharpe": 0.0, "win_rate": 0.0,
                "max_dd": 1.0, "cagr": 0.0, "sym_stats": {}, "eq_curve": eq_curve}

    daily_rets = np.diff(eq_vals) / np.array(eq_vals[:-1])
    sharpe     = (float(np.mean(daily_rets)) / float(np.std(daily_rets)) * (252 ** 0.5)
                  if np.std(daily_rets) > 0 else 0.0)

    peak = eq_vals[0]
    max_dd = 0.0
    for v in eq_vals:
        peak   = max(peak, v)
        max_dd = max(max_dd, (peak - v) / peak)

    years = (common_idx[-1] - common_idx[WARMUP_BARS]).days / 365
    cagr  = (eq_vals[-1] / eq_vals[0]) ** (1 / years) - 1 if years > 0 else 0.0

    wins     = [t for t in trades if t["pnl_net"] > 0]
    win_rate = len(wins) / len(trades) if trades else 0.0

    sym_stats = {}
    for sym in SYMBOLS:
        sym_trades = [t for t in trades if t["symbol"] == sym]
        if not sym_trades:
            sym_stats[sym] = {"n": 0}
            continue
        sym_wins = [t for t in sym_trades if t["pnl_net"] > 0]
        reasons  = {}
        for t in sym_trades:
            reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
        sym_stats[sym] = {
            "n":        len(sym_trades),
            "win_rate": len(sym_wins) / len(sym_trades),
            "avg_pct":  sum(t["pnl_pct"] for t in sym_trades) / len(sym_trades),
            "total":    sum(t["pnl_net"] for t in sym_trades),
            "reasons":  reasons,
        }

    return {
        "trades":    trades,
        "sharpe":    sharpe,
        "win_rate":  win_rate,
        "max_dd":    max_dd,
        "cagr":      cagr,
        "sym_stats": sym_stats,
        "eq_curve":  eq_curve,
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _profit_factor(trades: list) -> float:
    """Gross wins / gross losses. Returns inf if no losing trades."""
    gross_win  = sum(t["pnl_net"] for t in trades if t["pnl_net"] > 0)
    gross_loss = abs(sum(t["pnl_net"] for t in trades if t["pnl_net"] <= 0))
    return gross_win / gross_loss if gross_loss > 0 else float("inf")


def _metrics(trades: list, eq_curve: list, common_idx: list) -> dict:
    """Recompute all scalar metrics from a (possibly filtered) trade list."""
    eq_vals = [e["equity"] for e in eq_curve if e["date"] >= common_idx[WARMUP_BARS]]
    if len(eq_vals) < 2:
        return {"sharpe": 0.0, "win_rate": 0.0, "profit_factor": 0.0,
                "max_dd": 1.0, "cagr": 0.0}
    daily_rets = np.diff(eq_vals) / np.array(eq_vals[:-1])
    sharpe = (float(np.mean(daily_rets)) / float(np.std(daily_rets)) * (252 ** 0.5)
              if np.std(daily_rets) > 0 else 0.0)
    peak = eq_vals[0]; max_dd = 0.0
    for v in eq_vals:
        peak   = max(peak, v)
        max_dd = max(max_dd, (peak - v) / peak)
    years = (common_idx[-1] - common_idx[WARMUP_BARS]).days / 365
    cagr  = (eq_vals[-1] / eq_vals[0]) ** (1 / years) - 1 if years > 0 else 0.0
    wins  = [t for t in trades if t["pnl_net"] > 0]
    return {
        "sharpe":        sharpe,
        "win_rate":      len(wins) / len(trades) if trades else 0.0,
        "profit_factor": _profit_factor(trades),
        "max_dd":        max_dd,
        "cagr":          cagr,
    }


# ── Pass/fail criterion ───────────────────────────────────────────────────────

def _passed(r: dict, n: int) -> bool:
    """
    Trend-following criteria (NOT mean-reversion criteria).
    WR is structurally low for breakout strategies and is intentionally not
    in this gate. PF captures the asymmetric payoff that WR misses.
    N<20 is always an overfit risk regardless of other metrics.
    """
    pf = _profit_factor(r["trades"]) if r.get("trades") else 0.0
    return (r["sharpe"] >= TF_SHARPE_MIN
            and pf >= TF_PF_MIN
            and r["max_dd"] < TF_DD_MAX
            and n >= TF_N_MIN)


# ── Single-run print ─────────────────────────────────────────────────────────

def print_result(params: dict, r: dict, dates: list, verbose: bool = True) -> bool:
    df   = pd.DataFrame(r["trades"]) if r["trades"] else pd.DataFrame()
    wins = df[df["pnl_net"] > 0] if len(df) else pd.DataFrame()
    loss = df[df["pnl_net"] <= 0] if len(df) else pd.DataFrame()

    start_d = dates[WARMUP_BARS] if len(dates) > WARMUP_BARS else dates[0]
    years   = (dates[-1] - start_d).days / 365 if dates else 1.0
    passed  = _passed(r, len(df))

    if not verbose:
        return passed

    stop_str = f"  Stop{params['STOP_PCT']*100:.0f}%" if params["STOP_PCT"] > 0 else ""
    hold_str = f"  Hold{params['MAX_HOLD']}d"        if params["MAX_HOLD"] > 0  else ""
    print("=" * 66)
    print("  BINANCE CRYPTO MOMENTUM TREND -- BACKTEST RESULTS")
    print("=" * 66)
    print(
        f"  Params:  RSI[{params['RSI_LOW']},{params['RSI_HIGH']}]  "
        f"Break>{params['BREAKOUT_DAYS']}d  "
        f"Exit<{params['EXIT_DAYS']}d"
        f"{stop_str}{hold_str}"
    )
    print(f"  Period:  {start_d.date()} -> {dates[-1].date()} ({years:.1f}y)")
    print(f"  Universe: {', '.join(SYMBOLS)}")
    print()

    pf = _profit_factor(r["trades"])
    if len(df):
        print(f"  Trades:   {len(df)} ({len(wins)} wins / {len(loss)} losses)")
        print(f"  Win rate: {r['win_rate']*100:.1f}%")
        print(f"  Prof.Fac: {pf:.2f}  (gross wins / gross losses; target >=2.0)")
        print(f"  Avg hold: {df['days_held'].mean():.1f}d")
        if len(wins): print(f"  Avg win:  +{wins['pnl_pct'].mean():.1f}%  (+${wins['pnl_net'].mean():,.0f})")
        if len(loss): print(f"  Avg loss:  {loss['pnl_pct'].mean():.1f}%  (-${abs(loss['pnl_net'].mean()):,.0f})")
        print(f"  Total P&L: ${df['pnl_net'].sum():+,.0f} USDT")
    else:
        print("  No trades in backtest period.")

    print(f"  CAGR:     {r['cagr']*100:.1f}%")
    print(f"  Sharpe:   {r['sharpe']:.2f}")
    dd_flag = "" if r["max_dd"] < TF_DD_MAX else f"  <- ABOVE {TF_DD_MAX*100:.0f}% target"
    print(f"  Max DD:   {r['max_dd']*100:.1f}%{dd_flag}")

    print()
    print(f"  {'Symbol':<10} {'N':>4} {'WR':>6} {'Avg%':>7} {'Total$':>10}  {'Exit reasons'}")
    print("  " + "-" * 60)
    for sym in SYMBOLS:
        ss = r["sym_stats"].get(sym, {})
        if ss.get("n", 0) == 0:
            print(f"  {sym:<10} {'--':>4}")
            continue
        reason_str = "  ".join(f"{k}:{v}" for k, v in ss["reasons"].items())
        print(
            f"  {sym:<10} {ss['n']:>4} "
            f"{ss['win_rate']*100:>5.0f}% "
            f"{ss['avg_pct']:>+6.1f}% "
            f"${ss['total']:>+10,.0f}  {reason_str}"
        )

    clr_tag = "[PASS]" if passed else "[FAIL]"
    print()
    print(f"  {clr_tag}  Sharpe={r['sharpe']:.2f}  PF={pf:.2f}  WR={r['win_rate']*100:.0f}%  "
          f"DD={r['max_dd']*100:.1f}%  N={len(df)}  "
          f"{'(all criteria met)' if passed else '(did not meet all criteria)'}")
    print(f"  Trend-following criteria: Sharpe>={TF_SHARPE_MIN}  PF>={TF_PF_MIN}  MaxDD<{TF_DD_MAX*100:.0f}%  N>={TF_N_MIN}")
    print("=" * 66)
    return passed


# ── Grid-search helpers ──────────────────────────────────────────────────────

def _combo_key(params: dict) -> str:
    return (
        f"{params['RSI_LOW']},{params['RSI_HIGH']},"
        f"{params['BREAKOUT_DAYS']},{params['EXIT_DAYS']},"
        f"{params['STOP_PCT']},{params['MAX_HOLD']}"
    )


def _load_done_keys() -> set:
    if not os.path.exists(CSV_PATH):
        return set()
    with open(CSV_PATH, newline="") as f:
        header = f.readline()
    expected = {"RSI_LOW", "RSI_HIGH", "BREAKOUT_DAYS", "EXIT_DAYS", "STOP_PCT", "MAX_HOLD", "profit_factor"}
    if not expected.issubset(set(header.split(","))):
        print(f"[grid] CSV schema changed (profit_factor added) -- wiping {CSV_PATH} and starting fresh.")
        os.remove(CSV_PATH)
        return set()
    done = set()
    with open(CSV_PATH, newline="") as f:
        for row in csv.DictReader(f):
            done.add(
                f"{row['RSI_LOW']},{row['RSI_HIGH']},"
                f"{row['BREAKOUT_DAYS']},{row['EXIT_DAYS']},"
                f"{row['STOP_PCT']},{row['MAX_HOLD']}"
            )
    return done


def _append_csv(params: dict, r: dict, n: int, passed: bool) -> None:
    pf = _profit_factor(r["trades"])
    write_header = not os.path.exists(CSV_PATH)
    with open(CSV_PATH, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if write_header:
            w.writeheader()
        w.writerow({
            "RSI_LOW":       params["RSI_LOW"],
            "RSI_HIGH":      params["RSI_HIGH"],
            "BREAKOUT_DAYS": params["BREAKOUT_DAYS"],
            "EXIT_DAYS":     params["EXIT_DAYS"],
            "STOP_PCT":      params["STOP_PCT"],
            "MAX_HOLD":      params["MAX_HOLD"],
            "sharpe":        round(r["sharpe"],   3),
            "win_rate":      round(r["win_rate"], 3),
            "profit_factor": round(pf,            3),
            "max_dd":        round(r["max_dd"],   3),
            "cagr":          round(r["cagr"],     3),
            "n_trades":      n,
            "passed":        int(passed),
        })


# ── Summary printer ──────────────────────────────────────────────────────────

def _fmt_row(row: "pd.Series") -> str:
    stop_str = f"Stop{float(row['STOP_PCT'])*100:.0f}% " if float(row["STOP_PCT"]) > 0 else ""
    hold_str = f"Hold{int(row['MAX_HOLD'])}d "           if int(row["MAX_HOLD"]) > 0  else ""
    pf_val   = row.get("profit_factor", float("nan"))
    pf_str   = f"PF={float(pf_val):.2f} " if not pd.isna(pf_val) else ""
    return (
        f"RSI[{row['RSI_LOW']},{row['RSI_HIGH']}] "
        f"Break>{row['BREAKOUT_DAYS']}d "
        f"Exit<{row['EXIT_DAYS']}d "
        f"{stop_str}{hold_str}"
        f"Sharpe={row['sharpe']:.2f} "
        f"{pf_str}"
        f"WR={row['win_rate']*100:.0f}% "
        f"DD={row['max_dd']*100:.1f}% "
        f"CAGR={row['cagr']*100:.0f}% "
        f"N={int(row['n_trades'])}"
    )


def print_summary() -> None:
    if not os.path.exists(CSV_PATH):
        print("No results file found. Run --grid first.")
        return

    df    = pd.read_csv(CSV_PATH)
    total = len(df)

    robust = df[df["passed"] == 1].sort_values("sharpe", ascending=False)

    pf_col = df["profit_factor"] if "profit_factor" in df.columns else pd.Series([0.0]*len(df))
    overfit = df[
        (df["sharpe"]  >= TF_SHARPE_MIN)
        & (pf_col       >= TF_PF_MIN)
        & (df["max_dd"] <  TF_DD_MAX)
        & (df["n_trades"] < TF_N_MIN)
    ].sort_values("sharpe", ascending=False)

    thin_but_decent = df[
        (df["n_trades"] >= 20)
        & (df["passed"]  == 0)
    ].sort_values("sharpe", ascending=False)

    print(f"\n{'='*72}")
    print(f"  GRID SUMMARY -- {total} combos evaluated")
    print(f"  Trend-following criteria: Sharpe>={TF_SHARPE_MIN}, PF>={TF_PF_MIN}, MaxDD<{TF_DD_MAX*100:.0f}%, N>={TF_N_MIN} trades")
    print(f"{'='*72}")

    print(f"\n  ROBUST RESULTS ({len(robust)} combos passed all criteria incl. N>=20):")
    print(f"  (Top 5 by Sharpe -- the only combos safe to consider for live use)")
    print("  " + "-" * 72)
    if robust.empty:
        print("  None.")
    else:
        for rank, (_, row) in enumerate(robust.head(5).iterrows(), 1):
            print(f"  #{rank}  {_fmt_row(row)}")

    print(f"\n  OVERFIT RISK ({len(overfit)} combos) -- Sharpe/WR/DD look good but N<20:")
    print(f"  A 55% win rate on <20 trades is noise, not signal. Discard these.")
    print("  " + "-" * 72)
    if overfit.empty:
        print("  None.")
    else:
        for _, row in overfit.head(5).iterrows():
            print(f"  [!]  {_fmt_row(row)}")
        if len(overfit) > 5:
            print(f"  ... and {len(overfit)-5} more not shown")

    if robust.empty:
        print(f"\n  CLOSEST MISSES (N>=20, failed at least one criteria):")
        print("  " + "-" * 72)
        if thin_but_decent.empty:
            print("  No combo reached N>=20 trades.")
            top_any = df.sort_values("sharpe", ascending=False).head(5)
            print("  Top 5 by Sharpe (any N, for information only):")
            for _, row in top_any.iterrows():
                print(f"  [?]  {_fmt_row(row)}")
        else:
            for rank, (_, row) in enumerate(thin_but_decent.head(5).iterrows(), 1):
                fails = []
                if row["sharpe"] < TF_SHARPE_MIN:
                    fails.append(f"Sharpe={row['sharpe']:.2f}<{TF_SHARPE_MIN}")
                pf_val = row.get("profit_factor", float("nan"))
                if not pd.isna(pf_val) and float(pf_val) < TF_PF_MIN:
                    fails.append(f"PF={float(pf_val):.2f}<{TF_PF_MIN}")
                if row["max_dd"] >= TF_DD_MAX:
                    fails.append(f"DD={row['max_dd']*100:.1f}%>={TF_DD_MAX*100:.0f}%")
                print(f"  #{rank}  {_fmt_row(row)}  <- MISS: {', '.join(fails)}")

    if len(robust) >= 3:
        top_n = robust.head(10)
        n_top = len(top_n)
        print(f"\n  CONSENSUS (top-{n_top} robust combos):")
        for col, label in [
            ("RSI_LOW",       "RSI low"),
            ("RSI_HIGH",      "RSI high"),
            ("BREAKOUT_DAYS", "Breakout d"),
            ("EXIT_DAYS",     "Exit d"),
            ("STOP_PCT",      "Stop %"),
            ("MAX_HOLD",      "Max hold d"),
        ]:
            mode  = top_n[col].mode().iloc[0]
            count = int((top_n[col] == mode).sum())
            print(f"    {label:<14} most common = {mode}  ({count}/{n_top} top combos)")

    print(f"\n{'='*72}")


# ── Grid runner ──────────────────────────────────────────────────────────────

def run_grid(data: dict, ind: dict, dates: list) -> None:
    keys   = list(GRID.keys())
    combos = list(product(*[GRID[k] for k in keys]))
    total  = len(combos)
    done   = _load_done_keys()

    print(f"Grid search: {total} combinations...\n")

    for i, vals in enumerate(combos, 1):
        params = dict(zip(keys, vals))

        # Skip invalid RSI bands
        if params["RSI_LOW"] >= params["RSI_HIGH"]:
            continue

        key = _combo_key(params)
        if key in done:
            continue

        r      = simulate(data, ind, params)
        n      = len(r["trades"])
        passed = _passed(r, n)

        stop_str = f"Stop{params['STOP_PCT']*100:.0f}% " if params["STOP_PCT"] > 0 else ""
        hold_str = f"Hold{params['MAX_HOLD']}d "         if params["MAX_HOLD"] > 0  else ""
        tag = "PASS" if passed else "----"
        print(
            f"  [{i:>4}/{total}] {tag}  "
            f"RSI[{params['RSI_LOW']},{params['RSI_HIGH']}] "
            f"Break>{params['BREAKOUT_DAYS']}d "
            f"Exit<{params['EXIT_DAYS']}d "
            f"{stop_str}{hold_str} |  "
            f"Sharpe={r['sharpe']:.2f} "
            f"WR={r['win_rate']*100:.0f}% "
            f"DD={r['max_dd']*100:.1f}% "
            f"CAGR={r['cagr']*100:.0f}% "
            f"N={n}",
            flush=True,
        )

        _append_csv(params, r, n, passed)

    print()
    print_summary()


# ── Deep analysis of a single combo ──────────────────────────────────────────

def analyze_combo(data: dict, ind: dict, common_idx: list, params: dict) -> None:
    """
    Three robustness checks for the user before marking a strategy ready:

    1. Per-year equity: is the edge spread across years or concentrated in one bull run?
    2. Ex-top-3 trades: does the strategy survive removing its 3 best individual trades?
    3. Profit Factor summary by exit reason.
    """
    r      = simulate(data, ind, params)
    trades = r["trades"]
    n      = len(trades)
    pf     = _profit_factor(trades)

    stop_str = f"  Stop{params['STOP_PCT']*100:.0f}%" if params["STOP_PCT"] > 0 else ""
    hold_str = f"  Hold{params['MAX_HOLD']}d"         if params["MAX_HOLD"] > 0  else ""
    print("=" * 72)
    print("  DEEP ANALYSIS")
    print(f"  RSI[{params['RSI_LOW']},{params['RSI_HIGH']}]  "
          f"Break>{params['BREAKOUT_DAYS']}d  Exit<{params['EXIT_DAYS']}d"
          f"{stop_str}{hold_str}")
    print(f"  Full-sample: Sharpe={r['sharpe']:.2f}  PF={pf:.2f}  "
          f"WR={r['win_rate']*100:.0f}%  DD={r['max_dd']*100:.1f}%  "
          f"CAGR={r['cagr']*100:.0f}%  N={n}")
    print("=" * 72)

    # ── 1. Per-year equity ────────────────────────────────────────────────────
    print("\n  1. EQUITY CURVE BY YEAR")
    print("  " + "-" * 68)
    print(f"  {'Year':<6} {'Start eq':>10} {'End eq':>10} {'Return':>8} {'MaxDD':>7} {'N trades':>9}")
    print("  " + "-" * 68)

    eq_curve = r["eq_curve"]
    # Index equity curve by date for fast lookup
    eq_by_date = {e["date"]: e["equity"] for e in eq_curve}

    active_dates = [d for d in common_idx if d >= common_idx[WARMUP_BARS]]
    years_present = sorted({d.year for d in active_dates})

    for yr in years_present:
        yr_dates = [d for d in active_dates if d.year == yr]
        if not yr_dates:
            continue
        yr_eq    = [eq_by_date[d] for d in yr_dates if d in eq_by_date]
        if not yr_eq:
            continue
        start_eq = yr_eq[0]
        end_eq   = yr_eq[-1]
        ret_pct  = (end_eq / start_eq - 1) * 100

        peak = yr_eq[0]; yr_dd = 0.0
        for v in yr_eq:
            peak  = max(peak, v)
            yr_dd = max(yr_dd, (peak - v) / peak)

        yr_trades = [t for t in trades
                     if hasattr(t["exit_date"], "year") and t["exit_date"].year == yr]
        ret_flag = "  <-- bear" if ret_pct < -15 else ("  <-- bull" if ret_pct > 50 else "")
        print(
            f"  {yr:<6} ${start_eq:>9,.0f} ${end_eq:>9,.0f} "
            f"{ret_pct:>+7.1f}% {yr_dd*100:>6.1f}%  {len(yr_trades):>5}{ret_flag}"
        )

    # ── 2. Ex-top-3 robustness ────────────────────────────────────────────────
    print("\n  2. ROBUSTNESS CHECK -- EXCLUDING TOP-3 TRADES BY P&L")
    print("  " + "-" * 68)

    sorted_trades = sorted(trades, key=lambda t: t["pnl_net"], reverse=True)
    top3          = sorted_trades[:3]
    remaining     = sorted_trades[3:]

    print("  Top 3 trades removed:")
    for i, t in enumerate(top3, 1):
        entry = t["entry_date"].date() if hasattr(t["entry_date"], "date") else t["entry_date"]
        exit_ = t["exit_date"].date()  if hasattr(t["exit_date"],  "date") else t["exit_date"]
        print(f"    #{i}  {t['symbol']:<10}  {entry} -> {exit_}  "
              f"+{t['pnl_pct']:.1f}%  ${t['pnl_net']:+,.0f}")

    if remaining:
        ex3_wins = [t for t in remaining if t["pnl_net"] > 0]
        ex3_loss = [t for t in remaining if t["pnl_net"] <= 0]
        ex3_pf   = _profit_factor(remaining)
        ex3_wr   = len(ex3_wins) / len(remaining)
        ex3_total = sum(t["pnl_net"] for t in remaining)
        print(f"\n  Ex-top-3 ({len(remaining)} trades):")
        print(f"    Win rate:     {ex3_wr*100:.1f}%  ({len(ex3_wins)}W / {len(ex3_loss)}L)")
        print(f"    Profit Factor:{ex3_pf:.2f}  (target >=2.0)")
        print(f"    Total P&L:    ${ex3_total:+,.0f} USDT")
        if ex3_wins:
            print(f"    Avg win:      +{sum(t['pnl_pct'] for t in ex3_wins)/len(ex3_wins):.1f}%")
        if ex3_loss:
            print(f"    Avg loss:      {sum(t['pnl_pct'] for t in ex3_loss)/len(ex3_loss):.1f}%")
        ex3_verdict = "HOLDS UP" if ex3_pf >= TF_PF_MIN and ex3_total > 0 else "COLLAPSES"
        print(f"    Verdict:      {ex3_verdict}")

    # ── 3. Exit reason breakdown ──────────────────────────────────────────────
    print("\n  3. EXIT REASON BREAKDOWN")
    print("  " + "-" * 68)
    reasons: dict = {}
    for t in trades:
        key = t["reason"].split(" ")[0]  # "hard_stop", "donchian_exit", "max_hold", "end_of_data"
        if key not in reasons:
            reasons[key] = {"n": 0, "wins": 0, "pnl": 0.0}
        reasons[key]["n"]    += 1
        reasons[key]["wins"] += 1 if t["pnl_net"] > 0 else 0
        reasons[key]["pnl"]  += t["pnl_net"]
    for reason, stats in sorted(reasons.items()):
        wr = stats["wins"] / stats["n"] * 100
        print(f"  {reason:<18} N={stats['n']:>4}  WR={wr:.0f}%  P&L=${stats['pnl']:+,.0f}")

    print()
    passed = _passed(r, n)
    print(f"  {'[PASS]' if passed else '[FAIL]'}  Sharpe={r['sharpe']:.2f}  PF={pf:.2f}  "
          f"DD={r['max_dd']*100:.1f}%  N={n}")
    print(f"  Criteria: Sharpe>={TF_SHARPE_MIN}  PF>={TF_PF_MIN}  MaxDD<{TF_DD_MAX*100:.0f}%  N>={TF_N_MIN}")
    print("=" * 72)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Backtest: Binance crypto momentum trend strategy"
    )
    parser.add_argument("--grid",    action="store_true",
                        help="Run full parameter grid search (729 combinations)")
    parser.add_argument("--summary", action="store_true",
                        help="Print ranked summary from previous grid results CSV")
    parser.add_argument("--analyze", action="store_true",
                        help="Run deep analysis (per-year equity, ex-top-3 robustness) "
                             "on the best combo: RSI[55,65] Break>25d Exit<7d Stop5%% Hold60d")
    parser.add_argument("--reset",   action="store_true",
                        help="Delete cached data + results CSV, then exit")
    args = parser.parse_args()

    if args.reset:
        for path in [CACHE_PKL, CSV_PATH]:
            if os.path.exists(path):
                os.remove(path)
                print(f"Deleted {path}")
        sys.exit(0)

    if args.summary:
        print_summary()
        sys.exit(0)

    data = load_data()

    # Common date index (needed for print_result period label)
    common_idx = sorted(set.intersection(*[set(data[s].index) for s in SYMBOLS]))
    print(f"  {len(SYMBOLS)} symbols  |  {len(common_idx)} common daily bars\n")

    ind = _precompute(data)

    if args.analyze:
        best_combo = {
            "RSI_LOW": 55, "RSI_HIGH": 65,
            "BREAKOUT_DAYS": 25, "EXIT_DAYS": 7,
            "STOP_PCT": 0.05, "MAX_HOLD": 60,
        }
        analyze_combo(data, ind, common_idx, best_combo)
    elif args.grid:
        run_grid(data, ind, common_idx)
    else:
        r      = simulate(data, ind, DEFAULT_PARAMS)
        passed = print_result(DEFAULT_PARAMS, r, common_idx)
        sys.exit(0 if passed else 1)
