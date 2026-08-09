"""
backtest_binance_meanreversion.py
----------------------------------
Walk-forward backtest for the Binance crypto mean-reversion strategy.

Run (single pass with current live parameters):
    .venv/Scripts/python backtest_binance_meanreversion.py

Run (full parameter grid search -- resumes from CSV if stopped mid-way):
    .venv/Scripts/python backtest_binance_meanreversion.py --grid

Run (print ranked summary from a previous grid run):
    .venv/Scripts/python backtest_binance_meanreversion.py --summary

Run (delete cache + grid CSV and re-download fresh):
    .venv/Scripts/python backtest_binance_meanreversion.py --reset

Data: maximum available daily OHLCV per symbol from Binance mainnet public
      API (no API key required for klines).  Each symbol returns whatever
      history Binance holds; shorter-listed coins (AVAX, DOT etc) get fewer
      bars automatically.  The backtest uses the common-date intersection.
      Cached to data/backtest_binance_mr_cache.pkl; re-fetched on --reset,
      if cache is from a previous day, or if SYMBOLS has changed.

Signal logic (all 4 must fire simultaneously on bar[i]):
    RSI-14 < RSI_OVERSOLD
    price >= DIP_PCT below SMA-20
    24h volume > VOL_MULT x 20d avg volume
    price above EMA-200

Entry:  next bar's open (bar[i+1].open) -- never at the signal bar's close
Exit:   stop-loss, take-profit, or max-hold -- whichever fires first

Enable criterion: Sharpe >= 1.0 AND Win Rate >= 55% AND Max Drawdown < 20% AND N >= 20.
"""

import sys
import os
import argparse
import pickle
import csv
from datetime import date, timedelta
from itertools import product

import numpy as np
import pandas as pd

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
DATA_DIR  = os.path.join(BASE_DIR, "data")
CACHE_PKL = os.path.join(DATA_DIR, "backtest_binance_mr_cache.pkl")
CSV_PATH  = os.path.join(DATA_DIR, "backtest_binance_mr_grid.csv")

sys.path.insert(0, BASE_DIR)

# Reuse the existing RSI + EMA implementations unchanged (requirement)
from strategies.binance.mean_reversion import _rsi, _ema

SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "ADAUSDT",
    "XRPUSDT", "DOGEUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT",
]
# Earliest start date sent to Binance; each symbol returns what it has.
MAX_HISTORY_START = "1 Jan 2017"
COMMISSION_PCT = 0.001    # Binance taker fee 0.1% (standard tier, no BNB discount)
STARTING_USDT  = 10_000.0
MAX_POSITIONS  = 3        # mirrors live config capital.max_slots
WARMUP_BARS    = 210      # EMA-200 needs 200 + buffer; skip first 210 bars

# Default params -- mirrors binance_testnet_config.yaml
DEFAULT_PARAMS = {
    "RSI_OVERSOLD":  35,
    "DIP_PCT":       0.05,
    "VOL_MULT":      1.5,
    "STOP_PCT":      0.04,
    "TAKE_PROFIT":   0.08,
    "MAX_HOLD":      10,
    "COOLDOWN_DAYS": 0,
}

# Grid: 7x5x3x3x3x3x3 = 8,505 combinations
# RSI and DIP ranges widened vs prior run to generate enough trades (N>=20 floor).
# COOLDOWN_DAYS: bars to block re-entry on a symbol after a stop-loss exit.
# 0 = no cooldown (current behaviour), 3 = 3 trading days, 7 = 1 week.
GRID = {
    "RSI_OVERSOLD":  [30, 33, 35, 38, 40, 42, 45],
    "DIP_PCT":       [0.02, 0.03, 0.04, 0.05, 0.06],
    "VOL_MULT":      [1.5, 2.0, 2.5],
    "STOP_PCT":      [0.03, 0.04, 0.05],
    "TAKE_PROFIT":   [0.06, 0.08, 0.10],
    "MAX_HOLD":      [7, 10, 14],
    "COOLDOWN_DAYS": [0, 3, 7],
}

CSV_FIELDS = [
    "RSI_OVERSOLD", "DIP_PCT", "VOL_MULT", "STOP_PCT", "TAKE_PROFIT", "MAX_HOLD",
    "COOLDOWN_DAYS",
    "sharpe", "win_rate", "max_dd", "cagr", "n_trades", "passed",
]


# ── Data layer ────────────────────────────────────────────────────────────────

def _download_raw() -> dict[str, pd.DataFrame]:
    """
    Fetch 2 years of daily OHLCV for each symbol from the Binance mainnet
    public API.  No API key required -- klines are a public endpoint.
    Returns {symbol: DataFrame(open, high, low, close, volume)}.
    """
    try:
        from binance.client import Client       # type: ignore
    except ImportError:
        print("ERROR: python-binance not installed. Run: pip install python-binance")
        sys.exit(1)

    # Empty-key client for public endpoints only -- no signing needed for klines
    client = Client("", "", testnet=False)
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
    """Return cached OHLCV data, re-downloading if cache is stale, missing, or
    the SYMBOLS list has changed since the cache was written."""
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
            # Grid CSV results were computed on the old symbol set; they're invalid.
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

def _precompute(data: dict[str, pd.DataFrame]) -> dict[str, dict]:
    """
    Pre-compute RSI-14, SMA-20, EMA-200, and vol-ratio series for each symbol.
    Uses _rsi() and _ema() from strategies/binance/mean_reversion.py unchanged.

    Because _rsi() and _ema() are stateless and take a growing slice, this
    runs in O(N^2) time -- fine for N=730 (2 years of daily bars).
    """
    print(f"Pre-computing indicators for {len(data)} symbols...", flush=True)
    ind: dict[str, dict] = {}

    for sym, df in data.items():
        closes  = df["close"].tolist()
        volumes = df["volume"].tolist()
        n       = len(closes)

        rsi_vals   = []
        ema200_vals = []
        sma20_vals  = []
        vrat_vals   = []

        for i in range(n):
            # RSI and EMA from mean_reversion.py -- unchanged, just fed a growing window
            rsi_vals.append(_rsi(closes[:i + 1], period=14))
            ema200_vals.append(_ema(closes[:i + 1], period=200))

            # SMA-20: trailing 20 closes including today
            window = closes[max(0, i - 19): i + 1]
            sma20_vals.append(sum(window) / len(window))

            # Volume ratio: today / 20d avg
            v_window = volumes[max(0, i - 19): i + 1]
            v_avg    = sum(v_window) / len(v_window)
            vrat_vals.append(volumes[i] / v_avg if v_avg > 0 else 0.0)

        ind[sym] = {
            "rsi":    rsi_vals,
            "sma20":  sma20_vals,
            "ema200": ema200_vals,
            "vrat":   vrat_vals,
        }
        print(f"  {sym}: {len(closes)} bars", flush=True)

    print("  Done.\n")
    return ind


# ── Simulation ────────────────────────────────────────────────────────────────

def simulate(data: dict[str, pd.DataFrame], ind: dict, params: dict) -> dict:
    """
    Walk-forward simulation across all symbols with a single USDT portfolio.

    Signal on bar[i] close -> entry at bar[i+1] open.
    Exit priority (checked each bar after entry bar):
      1. days_held >= MAX_HOLD          -> exit at close
      2. low  <= entry * (1-STOP_PCT)   -> exit at stop price (intraday trigger)
      3. high >= entry * (1+TAKE_PROFIT)-> exit at TP price   (intraday trigger)
    """
    rsi_thr  = params["RSI_OVERSOLD"]
    dip_pct  = params["DIP_PCT"]
    vol_mult = params["VOL_MULT"]
    stop_pct = params["STOP_PCT"]
    take_pf  = params["TAKE_PROFIT"]
    max_hold = params["MAX_HOLD"]
    cooldown = params.get("COOLDOWN_DAYS", 0)   # bars to block re-entry after stop-loss

    # Align all symbols onto a common date index
    common_idx = sorted(
        set.intersection(*[set(data[s].index) for s in SYMBOLS])
    )
    if len(common_idx) < WARMUP_BARS + 2:
        return {"trades": [], "sharpe": 0.0, "win_rate": 0.0,
                "max_dd": 1.0, "cagr": 0.0, "eq_curve": []}

    # Index each symbol's precomputed series to the common dates
    sym_pos = {
        sym: {d: i for i, d in enumerate(data[sym].index)}
        for sym in SYMBOLS
    }

    cash           = STARTING_USDT
    peak_eq        = cash
    open_pos       = {}   # {sym: {entry_price, entry_bar, entry_date, units, cost}}
    trades         = []
    eq_curve       = []
    last_stop_bar  = {}   # sym -> bar index of most recent stop-loss exit

    for bar, today in enumerate(common_idx):
        if bar < WARMUP_BARS:
            eq_curve.append({"date": today, "equity": cash})
            continue

        def _get(sym, col, bar_idx=None):
            si = sym_pos[sym].get(today if bar_idx is None else common_idx[bar_idx])
            if si is None:
                return None
            return float(data[sym][col].iat[si])

        def _ind(sym, key):
            si = sym_pos[sym].get(today)
            return ind[sym][key][si] if si is not None else None

        # Mark-to-market (use close for current equity)
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
            close = _get(sym, "close")
            low   = _get(sym, "low")
            high  = _get(sym, "high")
            if close is None:
                continue

            stop_price = pos["entry_price"] * (1 - stop_pct)
            tp_price   = pos["entry_price"] * (1 + take_pf)

            exit_price = None
            reason     = ""

            if days_held >= max_hold:
                exit_price = close
                reason     = f"max-hold {days_held}d"
            elif low is not None and low <= stop_price:
                exit_price = stop_price
                reason     = f"stop-loss -{stop_pct*100:.0f}%"
            elif high is not None and high >= tp_price:
                exit_price = tp_price
                reason     = f"take-profit +{take_pf*100:.0f}%"

            if exit_price is not None:
                to_close.append((sym, exit_price, reason, pos, days_held))

        for sym, exit_price, reason, pos, days_held in to_close:
            proceeds  = pos["units"] * exit_price * (1 - COMMISSION_PCT)
            pnl_net   = proceeds - pos["cost"]
            cash     += proceeds
            trades.append({
                "symbol":       sym,
                "entry_date":   pos["entry_date"],
                "exit_date":    today,
                "entry_price":  pos["entry_price"],
                "exit_price":   exit_price,
                "units":        pos["units"],
                "pnl_net":      pnl_net,
                "pnl_pct":      (exit_price / pos["entry_price"] - 1) * 100,
                "reason":       reason,
                "days_held":    days_held,
            })
            if "stop-loss" in reason:
                last_stop_bar[sym] = bar   # start cooldown clock
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
                # Cooldown: block re-entry within N bars of a stop-loss on this symbol
                if cooldown > 0 and sym in last_stop_bar:
                    if bar - last_stop_bar[sym] < cooldown:
                        continue

                close_now = _get(sym, "close")
                rsi_now   = _ind(sym, "rsi")
                sma20_now = _ind(sym, "sma20")
                ema200_now= _ind(sym, "ema200")
                vrat_now  = _ind(sym, "vrat")

                if any(v is None for v in [close_now, rsi_now, sma20_now, ema200_now, vrat_now]):
                    continue

                dip = (sma20_now - close_now) / sma20_now

                if not (
                    rsi_now   < rsi_thr
                    and dip   >= dip_pct
                    and vrat_now >= vol_mult
                    and close_now > ema200_now
                ):
                    continue

                # Entry at NEXT bar's open
                next_open = _get(sym, "open", bar + 1)
                if next_open is None:
                    continue

                units = slot_size / next_open
                if units <= 0 or slot_size > cash:
                    continue

                cost  = units * next_open * (1 + COMMISSION_PCT)
                cash -= cost
                open_pos[sym] = {
                    "entry_price": next_open,
                    "entry_bar":   bar + 1,   # position starts from next bar
                    "entry_date":  common_idx[bar + 1],
                    "units":       units,
                    "cost":        cost,
                }
                slots_free -= 1

        open_val = sum(
            pos["units"] * (_get(sym, "close") or pos["entry_price"])
            for sym, pos in open_pos.items()
        )
        eq_curve.append({"date": today, "equity": cash + open_val})

    # Force-close remaining at last bar's close
    last_bar = len(common_idx) - 1
    last_date = common_idx[last_bar]
    for sym, pos in open_pos.items():
        lp = _get(sym, "close") or pos["entry_price"]
        proceeds  = pos["units"] * lp * (1 - COMMISSION_PCT)
        pnl_net   = proceeds - pos["cost"]
        cash     += proceeds
        trades.append({
            "symbol":      sym,
            "entry_date":  pos["entry_date"],
            "exit_date":   last_date,
            "entry_price": pos["entry_price"],
            "exit_price":  lp,
            "units":       pos["units"],
            "pnl_net":     pnl_net,
            "pnl_pct":     (lp / pos["entry_price"] - 1) * 100,
            "reason":      "end-of-backtest",
            "days_held":   last_bar - pos["entry_bar"],
        })

    if not eq_curve:
        return {"trades": [], "sharpe": 0.0, "win_rate": 0.0,
                "max_dd": 1.0, "cagr": 0.0, "eq_curve": []}

    return _compute_metrics(trades, eq_curve, common_idx)


def _compute_metrics(trades: list, eq_curve: list, dates: list) -> dict:
    """Compute Sharpe, win rate, max drawdown, CAGR from trades + equity curve."""
    eq_df     = pd.DataFrame(eq_curve).set_index("date")
    daily_ret = eq_df["equity"].pct_change().dropna()
    sharpe    = float(
        daily_ret.mean() / daily_ret.std() * np.sqrt(365)   # 365 trading days (crypto)
        if daily_ret.std() > 0 else 0.0
    )
    peak   = eq_df["equity"].cummax()
    max_dd = float(((peak - eq_df["equity"]) / peak).max())

    start_eq = eq_df["equity"].iloc[WARMUP_BARS] if len(eq_df) > WARMUP_BARS else STARTING_USDT
    end_eq   = eq_df["equity"].iloc[-1]
    years    = (dates[-1] - dates[WARMUP_BARS]).days / 365 if len(dates) > WARMUP_BARS else 1.0
    cagr     = float((end_eq / start_eq) ** (1 / years) - 1 if years > 0 and start_eq > 0 else 0.0)

    df       = pd.DataFrame(trades) if trades else pd.DataFrame()
    win_rate = float(len(df[df["pnl_net"] > 0]) / len(df)) if len(df) > 0 else 0.0

    # Per-symbol breakdown
    sym_stats = {}
    for sym in SYMBOLS:
        st = df[df["symbol"] == sym] if len(df) else pd.DataFrame()
        if len(st) == 0:
            sym_stats[sym] = {"n": 0, "win_rate": 0.0, "avg_pnl_pct": 0.0, "total_pnl": 0.0}
        else:
            sym_stats[sym] = {
                "n":           len(st),
                "win_rate":    len(st[st["pnl_net"] > 0]) / len(st),
                "avg_pnl_pct": float(st["pnl_pct"].mean()),
                "total_pnl":   float(st["pnl_net"].sum()),
            }

    return {
        "trades":    trades,
        "sharpe":    sharpe,
        "win_rate":  win_rate,
        "max_dd":    max_dd,
        "cagr":      cagr,
        "eq_curve":  eq_curve,
        "sym_stats": sym_stats,
    }


# ── Reporting ─────────────────────────────────────────────────────────────────

def _passed(r: dict, n: int) -> bool:
    """Minimum bar to call a combo 'enabled'.  N<20 is flagged as overfit risk."""
    return (r["sharpe"] >= 1.0 and r["win_rate"] >= 0.55
            and r["max_dd"] < 0.20 and n >= 20)


def print_result(params: dict, r: dict, dates: list, verbose: bool = True) -> bool:
    df   = pd.DataFrame(r["trades"]) if r["trades"] else pd.DataFrame()
    wins = df[df["pnl_net"] > 0] if len(df) else pd.DataFrame()
    loss = df[df["pnl_net"] <= 0] if len(df) else pd.DataFrame()

    start_d = dates[WARMUP_BARS] if len(dates) > WARMUP_BARS else dates[0]
    years   = (dates[-1] - start_d).days / 365 if dates else 1.0
    passed  = _passed(r, len(df))

    if not verbose:
        return passed

    print("=" * 66)
    print("  BINANCE CRYPTO MEAN REVERSION -- BACKTEST RESULTS")
    print("=" * 66)
    cd_str = f"  CD{params.get('COOLDOWN_DAYS', 0)}d" if params.get("COOLDOWN_DAYS", 0) else ""
    print(
        f"  Params:  RSI<{params['RSI_OVERSOLD']}  "
        f"Dip>{params['DIP_PCT']*100:.0f}%  "
        f"Vol>{params['VOL_MULT']}x  "
        f"Stop{params['STOP_PCT']*100:.0f}%  "
        f"TP{params['TAKE_PROFIT']*100:.0f}%  "
        f"Hold{params['MAX_HOLD']}d{cd_str}"
    )
    print(f"  Period:  {start_d.date()} -> {dates[-1].date()} ({years:.1f}y)")
    print(f"  Universe: {', '.join(SYMBOLS)}")
    print()

    if len(df):
        print(f"  Trades:    {len(df)} ({len(wins)} wins / {len(loss)} losses)")
        print(f"  Win rate:  {r['win_rate']*100:.1f}%")
        print(f"  Avg hold:  {df['days_held'].mean():.1f}d")
        if len(wins): print(f"  Avg win:   +{wins['pnl_pct'].mean():.1f}%  (+${wins['pnl_net'].mean():,.0f})")
        if len(loss): print(f"  Avg loss:   {loss['pnl_pct'].mean():.1f}%  (-${abs(loss['pnl_net'].mean()):,.0f})")
        print(f"  Total P&L: ${df['pnl_net'].sum():+,.0f} USDT")
    else:
        print("  No trades in backtest period.")

    print(f"  CAGR:      {r['cagr']*100:.1f}%")
    print(f"  Sharpe:    {r['sharpe']:.2f}")
    dd_flag = "" if r["max_dd"] < 0.20 else "  <- ABOVE 20% target"
    print(f"  Max DD:    {r['max_dd']*100:.1f}%{dd_flag}")

    # Per-symbol table
    print()
    print(f"  {'Symbol':<10} {'N':>4} {'WR':>6} {'Avg%':>7} {'Total$':>10}  {'Exit reasons'}")
    print("  " + "-" * 60)
    for sym in SYMBOLS:
        ss = r["sym_stats"].get(sym, {})
        if ss.get("n", 0) == 0:
            print(f"  {sym:<10} {'--':>4}")
            continue
        sym_df = df[df["symbol"] == sym]
        reasons = sym_df["reason"].value_counts().to_dict()
        reason_str = "  ".join(f"{k}:{v}" for k, v in sorted(reasons.items()))
        print(
            f"  {sym:<10} {ss['n']:>4} "
            f"{ss['win_rate']*100:>5.0f}% "
            f"{ss['avg_pnl_pct']:>+6.1f}% "
            f"${ss['total_pnl']:>9,.0f}  {reason_str}"
        )

    # Best / worst trades
    if len(df) >= 4:
        top = df.nlargest(min(3, len(wins)), "pnl_net")
        bot = df.nsmallest(min(3, len(loss)), "pnl_net")
        if len(top):
            print()
            print("  Best trades:")
            for _, row in top.iterrows():
                print(f"    {row['symbol']:<10} {str(row['entry_date'])[:10]}  "
                      f"{row['days_held']}d  {row['pnl_pct']:+.1f}%  ${row['pnl_net']:+,.0f}  [{row['reason']}]")
        if len(bot):
            print("  Worst trades:")
            for _, row in bot.iterrows():
                print(f"    {row['symbol']:<10} {str(row['entry_date'])[:10]}  "
                      f"{row['days_held']}d  {row['pnl_pct']:+.1f}%  ${row['pnl_net']:+,.0f}  [{row['reason']}]")

    print()
    print(f"  VERDICT: {'ENABLE' if passed else 'DO NOT ENABLE'}")
    print(
        f"  Sharpe>=1.0 {'OK' if r['sharpe']>=1.0 else 'X'}  "
        f"WinRate>=55% {'OK' if r['win_rate']>=0.55 else 'X'}  "
        f"MaxDD<20% {'OK' if r['max_dd']<0.20 else 'X'}  "
        f"Trades>=10 {'OK' if len(df)>=10 else 'X'}"
    )
    print("=" * 66)
    return passed


# ── Grid search ───────────────────────────────────────────────────────────────

def _combo_key(params: dict) -> str:
    return (f"{params['RSI_OVERSOLD']},{params['DIP_PCT']},{params['VOL_MULT']},"
            f"{params['STOP_PCT']},{params['TAKE_PROFIT']},{params['MAX_HOLD']},"
            f"{params.get('COOLDOWN_DAYS', 0)}")


def _load_done_keys() -> set:
    if not os.path.exists(CSV_PATH):
        return set()
    # If the CSV was written by an older run without COOLDOWN_DAYS, wipe it so
    # we don't mix incompatible schemas mid-grid.
    with open(CSV_PATH, newline="") as f:
        header = f.readline()
    if "COOLDOWN_DAYS" not in header:
        print(f"[grid] CSV schema changed (COOLDOWN_DAYS added) -- wiping {CSV_PATH} and starting fresh.")
        os.remove(CSV_PATH)
        return set()
    done = set()
    with open(CSV_PATH, newline="") as f:
        for row in csv.DictReader(f):
            done.add(
                f"{row['RSI_OVERSOLD']},{row['DIP_PCT']},{row['VOL_MULT']},"
                f"{row['STOP_PCT']},{row['TAKE_PROFIT']},{row['MAX_HOLD']},"
                f"{row.get('COOLDOWN_DAYS', 0)}"
            )
    return done


def _append_csv(params: dict, r: dict, n: int, passed: bool) -> None:
    write_header = not os.path.exists(CSV_PATH)
    with open(CSV_PATH, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if write_header:
            w.writeheader()
        w.writerow({
            "RSI_OVERSOLD":  params["RSI_OVERSOLD"],
            "DIP_PCT":       params["DIP_PCT"],
            "VOL_MULT":      params["VOL_MULT"],
            "STOP_PCT":      params["STOP_PCT"],
            "TAKE_PROFIT":   params["TAKE_PROFIT"],
            "MAX_HOLD":      params["MAX_HOLD"],
            "COOLDOWN_DAYS": params.get("COOLDOWN_DAYS", 0),
            "sharpe":        round(r["sharpe"],   3),
            "win_rate":      round(r["win_rate"], 3),
            "max_dd":        round(r["max_dd"],   3),
            "cagr":          round(r["cagr"],     3),
            "n_trades":      n,
            "passed":        int(passed),
        })


def run_grid(data: dict, ind: dict, dates: list) -> None:
    keys   = list(GRID.keys())
    combos = list(product(*GRID.values()))
    total  = len(combos)

    done_keys = _load_done_keys()
    remaining = [
        (i, dict(zip(keys, c)))
        for i, c in enumerate(combos, 1)
        if _combo_key(dict(zip(keys, c))) not in done_keys
    ]

    if done_keys:
        print(f"Resuming: {len(done_keys)} done, {len(remaining)} remaining of {total}.\n")
    else:
        print(f"Grid search: {total} combinations...\n")

    for i, params in remaining:
        r      = simulate(data, ind, params)
        df     = pd.DataFrame(r["trades"]) if r["trades"] else pd.DataFrame()
        n      = len(df)
        passed = _passed(r, n)
        tag    = "PASS" if passed else "----"
        cd_str = f"CD{params.get('COOLDOWN_DAYS',0)}d " if params.get("COOLDOWN_DAYS", 0) else ""
        print(
            f"  [{i:>4}/{total}] {tag}  "
            f"RSI<{params['RSI_OVERSOLD']} "
            f"Dip>{params['DIP_PCT']*100:.0f}% "
            f"Vol>{params['VOL_MULT']}x "
            f"Stop{params['STOP_PCT']*100:.0f}% "
            f"TP{params['TAKE_PROFIT']*100:.0f}% "
            f"Hold{params['MAX_HOLD']}d {cd_str} |  "
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


def _fmt_row(row: "pd.Series") -> str:
    cd_str = f"CD{int(row.get('COOLDOWN_DAYS', 0))}d " if row.get("COOLDOWN_DAYS", 0) else "      "
    return (
        f"RSI<{row['RSI_OVERSOLD']} "
        f"Dip>{row['DIP_PCT']*100:.0f}% "
        f"Vol>{row['VOL_MULT']}x "
        f"Stop{row['STOP_PCT']*100:.0f}% "
        f"TP{row['TAKE_PROFIT']*100:.0f}% "
        f"Hold{int(row['MAX_HOLD'])}d {cd_str}"
        f"Sharpe={row['sharpe']:.2f} "
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

    # ── Segment the results ───────────────────────────────────────────────────
    # Robust: all criteria met including N >= 20
    robust = df[df["passed"] == 1].sort_values("sharpe", ascending=False)

    # Overfit risk: Sharpe/WR/DD would pass, but N < 20 — statistically meaningless.
    # These look great on paper and are exactly what you want to avoid putting live.
    overfit = df[
        (df["sharpe"]   >= 1.0)
        & (df["win_rate"] >= 0.55)
        & (df["max_dd"]   <  0.20)
        & (df["n_trades"] <  20)
    ].sort_values("sharpe", ascending=False)

    # Top-5 by Sharpe from N >= 20 combos regardless of WR / DD pass (for context)
    thin_but_decent = df[
        (df["n_trades"] >= 20)
        & (df["passed"]  == 0)
    ].sort_values("sharpe", ascending=False)

    print(f"\n{'='*72}")
    print(f"  GRID SUMMARY -- {total} combos evaluated")
    print(f"  Enable criteria: Sharpe>=1.0, WR>=55%, MaxDD<20%, N>=20 trades")
    print(f"{'='*72}")

    # ── Section 1: Robust results (all criteria, N >= 20) ────────────────────
    print(f"\n  ROBUST RESULTS ({len(robust)} combos passed all criteria incl. N>=20):")
    print(f"  (Top 5 by Sharpe -- these are the only combos safe to consider for live use)")
    print("  " + "-" * 72)
    if robust.empty:
        print("  None.")
    else:
        for rank, (_, row) in enumerate(robust.head(5).iterrows(), 1):
            print(f"  #{rank}  {_fmt_row(row)}")

    # ── Section 2: Overfit-risk combos (high Sharpe but N < 20) ──────────────
    print(f"\n  OVERFIT RISK ({len(overfit)} combos) -- Sharpe/WR/DD look good but N<20:")
    print(f"  These pass on fewer than 20 trades. Variance is enormous at this scale.")
    print(f"  A 55% win rate on 10 trades is noise, not signal. Do not use for live config.")
    print("  " + "-" * 72)
    if overfit.empty:
        print("  None.")
    else:
        for _, row in overfit.head(5).iterrows():
            print(f"  [!]  {_fmt_row(row)}")
        if len(overfit) > 5:
            print(f"  ... and {len(overfit)-5} more overfit-risk combos not shown")

    # ── Section 3: Best N>=20 combos that didn't pass (closest misses) ───────
    if robust.empty:
        print(f"\n  CLOSEST MISSES (N>=20, failed at least one criteria):")
        print("  " + "-" * 72)
        if thin_but_decent.empty:
            # Even N>=20 combos don't exist — loosen entry conditions
            print("  No combo reached N>=20 trades. The strategy fires too rarely on this")
            print("  universe with these parameter ranges. Consider:")
            print("    - Loosening RSI threshold (e.g., RSI<40 or RSI<45)")
            print("    - Reducing DIP_PCT minimum (e.g., 2-3%)")
            print("    - Expanding universe (more symbols = more opportunities)")
            print()
            # Show the top raw-Sharpe combos regardless of N, for curiosity
            top_any = df.sort_values("sharpe", ascending=False).head(5)
            print("  Top 5 by Sharpe (any N, for information only):")
            for _, row in top_any.iterrows():
                print(f"  [?]  {_fmt_row(row)}")
        else:
            for rank, (_, row) in enumerate(thin_but_decent.head(5).iterrows(), 1):
                fails = []
                if row["sharpe"]   < 1.0:  fails.append(f"Sharpe={row['sharpe']:.2f}<1.0")
                if row["win_rate"] < 0.55:  fails.append(f"WR={row['win_rate']*100:.0f}%<55%")
                if row["max_dd"]   >= 0.20: fails.append(f"DD={row['max_dd']*100:.1f}%>=20%")
                print(f"  #{rank}  {_fmt_row(row)}  <- MISS: {', '.join(fails)}")

    # ── Section 4: Consensus across top-10 robust (if any) ───────────────────
    if len(robust) >= 3:
        top_n = robust.head(10)
        n_top = len(top_n)
        has_cd = "COOLDOWN_DAYS" in top_n.columns
        print(f"\n  CONSENSUS (top-{n_top} robust combos):")
        consensus_cols = [
            ("RSI_OVERSOLD", "RSI"),
            ("DIP_PCT",      "Dip%"),
            ("VOL_MULT",     "Vol x"),
            ("STOP_PCT",     "Stop%"),
            ("TAKE_PROFIT",  "TP%"),
            ("MAX_HOLD",     "Hold d"),
        ]
        if has_cd:
            consensus_cols.append(("COOLDOWN_DAYS", "Cooldown"))
        for col, label in consensus_cols:
            if col not in top_n.columns:
                continue
            mode  = top_n[col].mode().iloc[0]
            count = int((top_n[col] == mode).sum())
            print(f"    {label:<12} most common = {mode}  ({count}/{n_top} top combos)")

    print(f"\n{'='*72}")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Backtest: Binance crypto mean-reversion strategy"
    )
    parser.add_argument("--grid",    action="store_true",
                        help="Run full parameter grid search (8,505 combinations)")
    parser.add_argument("--summary", action="store_true",
                        help="Print ranked summary from previous grid results CSV")
    parser.add_argument("--reset",   action="store_true",
                        help="Delete cached data + results CSV, then exit")
    args = parser.parse_args()

    if args.reset:
        for p in [CACHE_PKL, CSV_PATH]:
            if os.path.exists(p):
                os.remove(p)
                print(f"Deleted {p}")
            else:
                print(f"Not found (already clean): {p}")
        sys.exit(0)

    if args.summary:
        print_summary()
        sys.exit(0)

    data = load_data()
    # Common date list (intersection of all symbols)
    common_dates = sorted(set.intersection(*[set(data[s].index) for s in SYMBOLS]))
    print(f"  {len(SYMBOLS)} symbols  |  {len(common_dates)} common daily bars\n")

    ind = _precompute(data)

    if args.grid:
        run_grid(data, ind, common_dates)
    else:
        r      = simulate(data, ind, DEFAULT_PARAMS)
        passed = print_result(DEFAULT_PARAMS, r, common_dates, verbose=True)
        df     = pd.DataFrame(r["trades"]) if r["trades"] else pd.DataFrame()
        if passed:
            print("\n  All criteria met. Update dry_run in binance_testnet_config.yaml")
            print("  and set --execute when running bots/run_binance_bot.py.")
        else:
            print("\n  Run:  .venv/Scripts/python backtest_binance_meanreversion.py --grid")
