"""
backtest_binance_rotation.py
----------------------------
Walk-forward backtest: Binance cross-sectional momentum rotation.

Strategy:
  Rank all 10 symbols by trailing N-day return each rebalance date.
  Hold the top K that are above their long-term SMA (trend filter).
  Rebalance on a fixed calendar schedule (weekly or bi-weekly).
  Equal-weight across K slots. Hard stop-loss between rebalances.
  All returns are net of 0.1 pct Binance taker fee per trade side.

Run:
    python backtest_binance_rotation.py                  # default params
    python backtest_binance_rotation.py --grid           # 72-combo search
    python backtest_binance_rotation.py --summary        # ranked table from CSV
    python backtest_binance_rotation.py --analyze        # deep analysis, best combo
    python backtest_binance_rotation.py --reset          # wipe rotation CSV + exit

Data:
    Reuses data/backtest_binance_mom_cache.pkl (same 10 symbols, same period).
    Downloads fresh via Binance mainnet public klines if cache is stale/missing.
"""

from __future__ import annotations

import sys
import os
import argparse
import pickle
import csv
from datetime import date, timedelta
from itertools import product

import numpy as np
import pandas as pd

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
DATA_DIR     = os.path.join(BASE_DIR, "data")
CACHE_PKL    = os.path.join(DATA_DIR, "backtest_binance_mom_cache.pkl")
ROT_CSV_PATH = os.path.join(DATA_DIR, "backtest_binance_rot_grid.csv")

sys.path.insert(0, BASE_DIR)

SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "ADAUSDT",
    "XRPUSDT", "DOGEUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT",
]
MAX_HISTORY_START = "1 Jan 2017"
COMMISSION_PCT    = 0.001      # Binance taker 0.1% per trade side
STARTING_USDT     = 10_000.0
WARMUP_BARS       = 210        # SMA-200 needs 200 bars; add 10-bar buffer

# Pass criteria — same reasoning as Momentum Trend v1
ROT_SHARPE_MIN = 1.0
ROT_PF_MIN     = 2.0
ROT_DD_MAX     = 0.40
ROT_N_MIN      = 20
ROT_BEST1_MAX  = 0.15   # best single trade must be <= 15% of total profit
ROT_BEST3_MAX  = 0.35   # best 3 trades combined must be <= 35% of total profit

DEFAULT_PARAMS = {
    "LOOKBACK":   60,
    "TREND_MA":   200,
    "K":          3,
    "REBAL_DAYS": 7,
    "STOP_PCT":   0.12,
}

# Grid: 3 x 2 x 2 x 2 x 3 = 72 combinations
GRID = {
    "LOOKBACK":   [30, 60, 90],
    "TREND_MA":   [100, 200],
    "K":          [2, 3],
    "REBAL_DAYS": [7, 14],
    "STOP_PCT":   [0.08, 0.12, 0.15],
}

CSV_FIELDS = [
    "LOOKBACK", "TREND_MA", "K", "REBAL_DAYS", "STOP_PCT",
    "sharpe", "win_rate", "profit_factor", "max_dd", "cagr", "n_trades",
    "best1_pct", "best3_pct",
    "passed", "fail_reason",
]

_SMA_PERIODS      = [100, 200]
_LOOKBACK_PERIODS = [30, 60, 90]


# ── Data layer ────────────────────────────────────────────────────────────────

def _download_raw() -> dict[str, pd.DataFrame]:
    try:
        from binance.client import Client
    except ImportError:
        print("ERROR: python-binance not installed.  Run: pip install python-binance")
        sys.exit(1)

    client = Client("", "", testnet=False)
    print(f"Downloading {len(SYMBOLS)} symbols from Binance mainnet (from {MAX_HISTORY_START})...")
    data: dict[str, pd.DataFrame] = {}

    for sym in SYMBOLS:
        print(f"  {sym} ... ", end="", flush=True)
        klines = client.get_historical_klines(
            sym, Client.KLINE_INTERVAL_1DAY, MAX_HISTORY_START
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
    """Load cached OHLCV data; re-download if stale, wrong symbols, or missing."""
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(CACHE_PKL):
        with open(CACHE_PKL, "rb") as f:
            cached = pickle.load(f)
        same_date    = cached.get("date")    == date.today().isoformat()
        same_symbols = cached.get("symbols") == sorted(SYMBOLS)
        if same_date and same_symbols:
            print("Using cached OHLCV data from today (shared with momentum backtest).\n")
            return cached["data"]
        if not same_symbols:
            print("Symbol set differs from cache — re-downloading...")
            if os.path.exists(ROT_CSV_PATH):
                os.remove(ROT_CSV_PATH)
                print(f"  Wiped stale rotation grid CSV: {ROT_CSV_PATH}")

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
    Pre-compute SMA and trailing returns for all lookback periods.

    sma[lb][i]          = mean(close[i-lb+1 : i+1])  -- includes bar i (no lookahead:
                          computed from bars we know at bar i close)
    trailing_ret[lb][i] = close[i] / close[i-lb] - 1  -- lb-day trailing return at bar i
    """
    print(f"Pre-computing indicators for {len(data)} symbols...", flush=True)
    ind: dict[str, dict] = {}

    for sym, df in data.items():
        closes = df["close"].to_numpy(dtype=float)
        n      = len(closes)

        sma: dict[int, np.ndarray] = {}
        for lb in _SMA_PERIODS:
            vals = np.full(n, np.nan)
            if n >= lb:
                cs = np.concatenate(([0.0], np.cumsum(closes)))
                vals[lb - 1:] = (cs[lb:] - cs[:-lb]) / lb
            sma[lb] = vals

        trailing_ret: dict[int, np.ndarray] = {}
        for lb in _LOOKBACK_PERIODS:
            vals = np.full(n, np.nan)
            if n > lb:
                prev  = closes[:-lb]
                curr  = closes[lb:]
                valid = prev > 0
                ret   = np.where(valid, curr / prev - 1.0, np.nan)
                vals[lb:] = ret
            trailing_ret[lb] = vals

        ind[sym] = {"sma": sma, "trailing_ret": trailing_ret}
        print(f"  {sym}: {n} bars", flush=True)

    print("  Done.\n")
    return ind


# ── Simulation ────────────────────────────────────────────────────────────────

def simulate(data: dict, ind: dict, params: dict) -> dict:
    """
    Walk-forward portfolio simulation.

    Each rebalance date:
      1. Compute trailing-LOOKBACK-day returns for every symbol.
      2. Keep only symbols whose close > SMA(TREND_MA) (trend filter).
      3. Rank eligible by trailing return; select top K.
      4. Exit any held symbol no longer in top K — execution at next bar open.
      5. Enter newly qualifying symbols — execution at next bar open.
      6. Held symbols that remain in top K are NOT churned.

    Between rebalances:
      Hard stop-loss checked daily at close.  Exit approximated at that close.

    Position sizing:
      Available cash / number_of_new_slots.  Existing positions keep their units.

    All entries and exits have COMMISSION_PCT applied (0.1% taker per side).
    N counted as individual position round-trips (entry + exit pairs).
    """
    lookback   = params["LOOKBACK"]
    trend_ma   = params["TREND_MA"]
    k          = params["K"]
    rebal_days = params["REBAL_DAYS"]
    stop_pct   = params["STOP_PCT"]

    common_idx = sorted(set.intersection(*[set(data[s].index) for s in SYMBOLS]))
    if len(common_idx) < WARMUP_BARS + 2:
        return _empty_result(common_idx)

    sym_pos = {sym: {d: i for i, d in enumerate(data[sym].index)} for sym in SYMBOLS}

    cash            = STARTING_USDT
    open_pos: dict  = {}   # symbol -> position dict
    trades: list    = []
    eq_curve: list  = []
    last_rebal_date = None

    for bar, today in enumerate(common_idx):
        if bar < WARMUP_BARS:
            eq_curve.append({"date": today, "equity": cash})
            continue

        def _close(sym: str) -> float | None:
            si = sym_pos[sym].get(today)
            return float(data[sym]["close"].iat[si]) if si is not None else None

        # Mark-to-market equity
        open_val = sum(
            pos["units"] * (_close(s) or pos["entry_price"])
            for s, pos in open_pos.items()
        )
        total_eq = cash + open_val

        # ── 1. Stop-loss ──────────────────────────────────────────────────────
        to_stop = []
        for sym, pos in list(open_pos.items()):
            close = _close(sym)
            if close is not None and close < pos["entry_price"] * (1.0 - stop_pct):
                to_stop.append((sym, close, pos))

        for sym, exit_px, pos in to_stop:
            proceeds = pos["units"] * exit_px * (1.0 - COMMISSION_PCT)
            cash    += proceeds
            trades.append({
                "symbol":      sym,
                "entry_date":  pos["entry_date"],
                "exit_date":   today,
                "entry_price": pos["entry_price"],
                "exit_price":  exit_px,
                "pnl_net":     proceeds - pos["cost"],
                "pnl_pct":     (exit_px / pos["entry_price"] - 1.0) * 100.0,
                "reason":      f"stop_{stop_pct*100:.0f}pct",
                "days_held":   bar - pos["entry_bar"],
            })
            del open_pos[sym]

        # ── 2. Rebalance ──────────────────────────────────────────────────────
        is_rebal = (
            last_rebal_date is None
            or (today - last_rebal_date).days >= rebal_days
        )

        if is_rebal and bar + 1 < len(common_idx):
            last_rebal_date = today
            next_date       = common_idx[bar + 1]

            # Rank eligible symbols by trailing return
            candidates: list[tuple[str, float]] = []
            for sym in SYMBOLS:
                si = sym_pos[sym].get(today)
                if si is None:
                    continue
                sma_val = ind[sym]["sma"][trend_ma][si]
                ret_val = ind[sym]["trailing_ret"][lookback][si]
                close   = _close(sym)
                if close is None or np.isnan(sma_val) or np.isnan(ret_val):
                    continue
                if close > sma_val:
                    candidates.append((sym, ret_val))

            candidates.sort(key=lambda x: x[1], reverse=True)
            target: set[str] = {sym for sym, _ in candidates[:k]}

            # Exit held symbols not in new target set
            to_exit = [sym for sym in list(open_pos.keys()) if sym not in target]
            for sym in to_exit:
                pos     = open_pos[sym]
                n_si    = sym_pos[sym].get(next_date)
                exit_px = (float(data[sym]["open"].iat[n_si]) if n_si is not None
                           else _close(sym) or pos["entry_price"])
                proceeds = pos["units"] * exit_px * (1.0 - COMMISSION_PCT)
                cash    += proceeds
                trades.append({
                    "symbol":      sym,
                    "entry_date":  pos["entry_date"],
                    "exit_date":   next_date,
                    "entry_price": pos["entry_price"],
                    "exit_price":  exit_px,
                    "pnl_net":     proceeds - pos["cost"],
                    "pnl_pct":     (exit_px / pos["entry_price"] - 1.0) * 100.0,
                    "reason":      "rebal_exit",
                    "days_held":   bar - pos["entry_bar"] + 1,
                })
                del open_pos[sym]

            # Enter symbols in target not already held
            new_syms = [sym for sym in target if sym not in open_pos]
            if new_syms:
                slot_size = cash / len(new_syms)
                for sym in new_syms:
                    n_si = sym_pos[sym].get(next_date)
                    if n_si is None:
                        continue
                    entry_px = float(data[sym]["open"].iat[n_si])
                    if entry_px <= 0:
                        continue
                    cost  = slot_size * (1.0 + COMMISSION_PCT)
                    units = slot_size / entry_px
                    cash -= cost
                    open_pos[sym] = {
                        "entry_price": entry_px,
                        "entry_bar":   bar,
                        "entry_date":  next_date,
                        "units":       units,
                        "cost":        cost,
                    }

        eq_curve.append({"date": today, "equity": total_eq})

    # Force-close any remaining positions at last bar's close
    last_date = common_idx[-1]
    for sym, pos in list(open_pos.items()):
        si = sym_pos[sym].get(last_date)
        if si is None:
            continue
        close    = float(data[sym]["close"].iat[si])
        proceeds = pos["units"] * close * (1.0 - COMMISSION_PCT)
        trades.append({
            "symbol":      sym,
            "entry_date":  pos["entry_date"],
            "exit_date":   last_date,
            "entry_price": pos["entry_price"],
            "exit_price":  close,
            "pnl_net":     proceeds - pos["cost"],
            "pnl_pct":     (close / pos["entry_price"] - 1.0) * 100.0,
            "reason":      "end_of_data",
            "days_held":   len(common_idx) - 1 - pos["entry_bar"],
        })

    return {"trades": trades, "eq_curve": eq_curve, "common_idx": common_idx}


def _empty_result(common_idx: list) -> dict:
    return {"trades": [], "eq_curve": [], "common_idx": common_idx}


# ── Metric helpers ────────────────────────────────────────────────────────────

def _profit_factor(trades: list) -> float:
    gross_win  = sum(t["pnl_net"] for t in trades if t["pnl_net"] > 0)
    gross_loss = abs(sum(t["pnl_net"] for t in trades if t["pnl_net"] <= 0))
    return gross_win / gross_loss if gross_loss > 0 else float("inf")


def _concentration(trades: list) -> tuple[float, float]:
    """
    Returns (best1_pct, best3_pct):
      best1_pct -- single largest winning trade as fraction of total gross profit
      best3_pct -- 3 largest winning trades combined as fraction of total gross profit

    Returns (1.0, 1.0) when there is no profit (worst-case outlier concentration).
    """
    wins         = sorted([t["pnl_net"] for t in trades if t["pnl_net"] > 0], reverse=True)
    total_profit = sum(wins)
    if total_profit <= 0 or not wins:
        return 1.0, 1.0
    best1 = wins[0]          / total_profit
    best3 = sum(wins[:3])    / total_profit
    return best1, best3


def _scalar_metrics(trades: list, eq_curve: list, common_idx: list) -> dict:
    """Compute Sharpe, WR, PF, MaxDD, CAGR from trades and equity curve."""
    eq_vals = [e["equity"] for e in eq_curve if e["date"] >= common_idx[WARMUP_BARS]]
    if len(eq_vals) < 2:
        return {"sharpe": 0.0, "win_rate": 0.0, "profit_factor": 0.0,
                "max_dd": 1.0, "cagr": 0.0}

    daily_rets = np.diff(eq_vals) / np.maximum(np.array(eq_vals[:-1]), 1e-10)
    sharpe = (float(np.mean(daily_rets)) / float(np.std(daily_rets)) * (252 ** 0.5)
              if np.std(daily_rets) > 0 else 0.0)

    peak = eq_vals[0]
    max_dd = 0.0
    for v in eq_vals:
        peak   = max(peak, v)
        max_dd = max(max_dd, (peak - v) / peak)

    years = (common_idx[-1] - common_idx[WARMUP_BARS]).days / 365.0
    cagr  = (eq_vals[-1] / eq_vals[0]) ** (1.0 / years) - 1.0 if years > 0 else 0.0

    wins = [t for t in trades if t["pnl_net"] > 0]
    return {
        "sharpe":        sharpe,
        "win_rate":      len(wins) / len(trades) if trades else 0.0,
        "profit_factor": _profit_factor(trades),
        "max_dd":        max_dd,
        "cagr":          cagr,
    }


# ── Pass / fail ───────────────────────────────────────────────────────────────

def _fail_reasons(trades: list, m: dict) -> list[str]:
    """Return list of human-readable fail reasons; empty list = passed."""
    fails: list[str] = []
    n = len(trades)
    best1, best3 = _concentration(trades)

    if m["sharpe"] < ROT_SHARPE_MIN:
        fails.append(f"Sharpe={m['sharpe']:.2f}<{ROT_SHARPE_MIN}")
    if m["profit_factor"] < ROT_PF_MIN:
        fails.append(f"PF={m['profit_factor']:.2f}<{ROT_PF_MIN}")
    if m["max_dd"] >= ROT_DD_MAX:
        fails.append(f"DD={m['max_dd']*100:.1f}%>={ROT_DD_MAX*100:.0f}%")
    if n < ROT_N_MIN:
        fails.append(f"N={n}<{ROT_N_MIN}")
    if best1 > ROT_BEST1_MAX:
        fails.append(f"best1={best1*100:.1f}%>{ROT_BEST1_MAX*100:.0f}%")
    if best3 > ROT_BEST3_MAX:
        fails.append(f"best3={best3*100:.1f}%>{ROT_BEST3_MAX*100:.0f}%")
    return fails


def _passed(trades: list, m: dict) -> bool:
    return len(_fail_reasons(trades, m)) == 0


# ── Single-run printer ────────────────────────────────────────────────────────

def print_result(params: dict, r: dict, verbose: bool = True) -> bool:
    trades     = r["trades"]
    eq_curve   = r["eq_curve"]
    common_idx = r["common_idx"]

    if not common_idx:
        print("No data.")
        return False

    m      = _scalar_metrics(trades, eq_curve, common_idx)
    n      = len(trades)
    passed = _passed(trades, m)
    best1, best3 = _concentration(trades)

    if not verbose:
        return passed

    start_d = common_idx[WARMUP_BARS] if len(common_idx) > WARMUP_BARS else common_idx[0]
    years   = (common_idx[-1] - start_d).days / 365.0
    wins    = [t for t in trades if t["pnl_net"] > 0]
    losses  = [t for t in trades if t["pnl_net"] <= 0]

    print("=" * 70)
    print("  BINANCE CROSS-SECTIONAL MOMENTUM ROTATION -- BACKTEST RESULTS")
    print("=" * 70)
    print(
        f"  Params:  Lookback={params['LOOKBACK']}d  "
        f"TrendMA={params['TREND_MA']}d  "
        f"K={params['K']}  "
        f"Rebal={params['REBAL_DAYS']}d  "
        f"Stop={params['STOP_PCT']*100:.0f}%"
    )
    print(f"  Period:  {start_d.date()} -> {common_idx[-1].date()} ({years:.1f}y)")
    print(f"  Universe: {', '.join(SYMBOLS)}")
    print(f"  Fees:    0.1% taker per side (applied to every entry and exit)")
    print()

    if n:
        print(f"  Trades:      {n}  ({len(wins)} wins / {len(losses)} losses)")
        print(f"  Win rate:    {m['win_rate']*100:.1f}%")
        print(f"  Prof.Factor: {m['profit_factor']:.2f}  (target >= {ROT_PF_MIN})")
        print(f"  Avg hold:    {sum(t['days_held'] for t in trades)/n:.1f}d")
        if wins:   print(f"  Avg win:    +{sum(t['pnl_pct'] for t in wins)/len(wins):.1f}%")
        if losses: print(f"  Avg loss:    {sum(t['pnl_pct'] for t in losses)/len(losses):.1f}%")
        print(f"  Total P&L:   ${sum(t['pnl_net'] for t in trades):+,.0f} USDT (net of fees)")
        print()
        print(f"  Concentration (outlier check):")
        print(f"    Best single trade: {best1*100:.1f}% of total profit  (limit: {ROT_BEST1_MAX*100:.0f}%)")
        print(f"    Best 3 trades:     {best3*100:.1f}% of total profit  (limit: {ROT_BEST3_MAX*100:.0f}%)")
    else:
        print("  No trades in backtest period.")

    print()
    print(f"  CAGR:     {m['cagr']*100:.1f}%")
    print(f"  Sharpe:   {m['sharpe']:.2f}")
    dd_flag = f"  <- ABOVE {ROT_DD_MAX*100:.0f}% target" if m["max_dd"] >= ROT_DD_MAX else ""
    print(f"  Max DD:   {m['max_dd']*100:.1f}%{dd_flag}")

    print()
    # Per-symbol summary
    print(f"  {'Symbol':<10} {'N':>4} {'WR':>6} {'Avg%':>7} {'Total$':>11}  Exit reasons")
    print("  " + "-" * 66)
    for sym in SYMBOLS:
        st = [t for t in trades if t["symbol"] == sym]
        if not st:
            print(f"  {sym:<10} {'--':>4}")
            continue
        st_wins = [t for t in st if t["pnl_net"] > 0]
        reasons: dict = {}
        for t in st:
            k2 = t["reason"].split("_")[0]
            reasons[k2] = reasons.get(k2, 0) + 1
        rsn = "  ".join(f"{k}:{v}" for k, v in reasons.items())
        print(
            f"  {sym:<10} {len(st):>4} "
            f"{len(st_wins)/len(st)*100:>5.0f}% "
            f"{sum(t['pnl_pct'] for t in st)/len(st):>+6.1f}% "
            f"${sum(t['pnl_net'] for t in st):>+10,.0f}  {rsn}"
        )

    clr = "[PASS]" if passed else "[FAIL]"
    fails = _fail_reasons(trades, m)
    print()
    print(f"  {clr}  Sharpe={m['sharpe']:.2f}  PF={m['profit_factor']:.2f}  "
          f"DD={m['max_dd']*100:.1f}%  N={n}  "
          f"best1={best1*100:.1f}%  best3={best3*100:.1f}%")
    if fails:
        print(f"  Fails: {'; '.join(fails)}")
    print(f"  Criteria: Sharpe>={ROT_SHARPE_MIN}  PF>={ROT_PF_MIN}  "
          f"MaxDD<{ROT_DD_MAX*100:.0f}%  N>={ROT_N_MIN}  "
          f"best1<={ROT_BEST1_MAX*100:.0f}%  best3<={ROT_BEST3_MAX*100:.0f}%")
    print("=" * 70)
    return passed


# ── Grid helpers ──────────────────────────────────────────────────────────────

def _combo_key(params: dict) -> str:
    return (
        f"{params['LOOKBACK']},{params['TREND_MA']},"
        f"{params['K']},{params['REBAL_DAYS']},{params['STOP_PCT']}"
    )


def _load_done_keys() -> set:
    if not os.path.exists(ROT_CSV_PATH):
        return set()
    with open(ROT_CSV_PATH, newline="") as f:
        header = f.readline()
    expected = {"LOOKBACK", "TREND_MA", "K", "REBAL_DAYS", "STOP_PCT", "best1_pct", "best3_pct"}
    if not expected.issubset(set(header.strip().split(","))):
        print(f"[grid] CSV schema changed — wiping {ROT_CSV_PATH} and starting fresh.")
        os.remove(ROT_CSV_PATH)
        return set()
    done: set = set()
    with open(ROT_CSV_PATH, newline="") as f:
        for row in csv.DictReader(f):
            done.add(
                f"{row['LOOKBACK']},{row['TREND_MA']},"
                f"{row['K']},{row['REBAL_DAYS']},{row['STOP_PCT']}"
            )
    return done


def _append_csv(params: dict, trades: list, m: dict, passed: bool) -> None:
    best1, best3 = _concentration(trades)
    fails        = _fail_reasons(trades, m)
    n            = len(trades)
    write_header = not os.path.exists(ROT_CSV_PATH)
    with open(ROT_CSV_PATH, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if write_header:
            w.writeheader()
        w.writerow({
            "LOOKBACK":      params["LOOKBACK"],
            "TREND_MA":      params["TREND_MA"],
            "K":             params["K"],
            "REBAL_DAYS":    params["REBAL_DAYS"],
            "STOP_PCT":      params["STOP_PCT"],
            "sharpe":        round(m["sharpe"],        3),
            "win_rate":      round(m["win_rate"],      3),
            "profit_factor": round(m["profit_factor"], 3),
            "max_dd":        round(m["max_dd"],        3),
            "cagr":          round(m["cagr"],          3),
            "n_trades":      n,
            "best1_pct":     round(best1, 4),
            "best3_pct":     round(best3, 4),
            "passed":        int(passed),
            "fail_reason":   "; ".join(fails) if fails else "ok",
        })


# ── Summary printer ───────────────────────────────────────────────────────────

def _fmt_row(row: "pd.Series") -> str:
    return (
        f"Lb={int(row['LOOKBACK'])}d "
        f"MA={int(row['TREND_MA'])}d "
        f"K={int(row['K'])} "
        f"Rb={int(row['REBAL_DAYS'])}d "
        f"Stop={float(row['STOP_PCT'])*100:.0f}%  "
        f"Sharpe={float(row['sharpe']):.2f} "
        f"PF={float(row['profit_factor']):.2f} "
        f"WR={float(row['win_rate'])*100:.0f}% "
        f"DD={float(row['max_dd'])*100:.1f}% "
        f"CAGR={float(row['cagr'])*100:.0f}% "
        f"N={int(row['n_trades'])} "
        f"b1={float(row['best1_pct'])*100:.1f}% "
        f"b3={float(row['best3_pct'])*100:.1f}%"
    )


def print_summary() -> None:
    if not os.path.exists(ROT_CSV_PATH):
        print("No results file found.  Run --grid first.")
        return

    df    = pd.read_csv(ROT_CSV_PATH)
    total = len(df)

    # Apply all 6 criteria including concentration caps
    robust = df[df["passed"] == 1].sort_values("sharpe", ascending=False)

    # Outlier/concentration risk: passed 4 standard criteria but failed on concentration
    std_pass = (
        (df["sharpe"]        >= ROT_SHARPE_MIN)
        & (df["profit_factor"] >= ROT_PF_MIN)
        & (df["max_dd"]        <  ROT_DD_MAX)
        & (df["n_trades"]      >= ROT_N_MIN)
    )
    conc_fail = (
        (df["best1_pct"] > ROT_BEST1_MAX)
        | (df["best3_pct"] > ROT_BEST3_MAX)
    )
    outlier_risk = df[std_pass & conc_fail].sort_values("sharpe", ascending=False)

    # N<20 overfit risk (standard criteria look good but N is too low)
    overfit = df[
        std_pass
        & (df["n_trades"] < ROT_N_MIN)
        & ~conc_fail
    ].sort_values("sharpe", ascending=False)

    # Closest misses: N>=20, passed==0 (may overlap with outlier_risk — both sections are shown)
    closest = df[
        (df["n_trades"] >= ROT_N_MIN)
        & (df["passed"] == 0)
    ].sort_values("sharpe", ascending=False)

    print(f"\n{'='*76}")
    print(f"  ROTATION GRID SUMMARY -- {total} combos evaluated")
    print(f"  Pass: Sharpe>={ROT_SHARPE_MIN}  PF>={ROT_PF_MIN}  "
          f"MaxDD<{ROT_DD_MAX*100:.0f}%  N>={ROT_N_MIN}  "
          f"best1<={ROT_BEST1_MAX*100:.0f}%  best3<={ROT_BEST3_MAX*100:.0f}%")
    print(f"{'='*76}")

    print(f"\n  ROBUST RESULTS ({len(robust)} combos cleared all 6 criteria):")
    print("  " + "-" * 76)
    if robust.empty:
        print("  None.")
    else:
        for rank, (_, row) in enumerate(robust.head(5).iterrows(), 1):
            print(f"  #{rank}  {_fmt_row(row)}")

    print(f"\n  OUTLIER/CONCENTRATION RISK ({len(outlier_risk)} combos) --")
    print(f"  Standard criteria met but best1>{ROT_BEST1_MAX*100:.0f}% or best3>{ROT_BEST3_MAX*100:.0f}%:")
    print(f"  Edge may depend on 1-3 exceptional trades, not repeatable signal.")
    print("  " + "-" * 76)
    if outlier_risk.empty:
        print("  None.")
    else:
        for _, row in outlier_risk.head(5).iterrows():
            print(f"  [CONC]  {_fmt_row(row)}")
            fr = str(row.get("fail_reason", ""))
            if fr and fr != "ok":
                print(f"          Fail: {fr}")
        if len(outlier_risk) > 5:
            print(f"  ... and {len(outlier_risk)-5} more")

    print(f"\n  OVERFIT RISK ({len(overfit)} combos) -- N<{ROT_N_MIN}, discard:")
    print("  " + "-" * 76)
    if overfit.empty:
        print("  None.")
    else:
        for _, row in overfit.head(3).iterrows():
            print(f"  [N<20]  {_fmt_row(row)}")

    if robust.empty:
        print(f"\n  CLOSEST MISSES (N>={ROT_N_MIN}, failed at least one criterion):")
        print("  " + "-" * 76)
        n20 = df[df["n_trades"] >= ROT_N_MIN].sort_values("sharpe", ascending=False)
        if n20.empty:
            print(f"  No combo reached N>={ROT_N_MIN}.")
            top5 = df.sort_values("sharpe", ascending=False).head(5)
            print("  Top 5 by Sharpe (any N, for information only):")
            for _, row in top5.iterrows():
                print(f"  [?]  {_fmt_row(row)}")
        else:
            for rank, (_, row) in enumerate(n20.head(5).iterrows(), 1):
                fr = str(row.get("fail_reason", ""))
                print(f"  #{rank}  {_fmt_row(row)}  <- MISS: {fr}")

    print(f"\n{'='*76}")


# ── Deep analysis ─────────────────────────────────────────────────────────────

def _year_table(trades: list, eq_curve: list, common_idx: list) -> None:
    eq_by_date   = {e["date"]: e["equity"] for e in eq_curve}
    active_dates = [d for d in common_idx if d >= common_idx[WARMUP_BARS]]
    years_present = sorted({d.year for d in active_dates})

    print(f"  {'Year':<6} {'StartEq':>10} {'EndEq':>10} {'Return':>8} {'MaxDD':>7} {'N':>5}")
    print("  " + "-" * 60)
    for yr in years_present:
        yr_dates = [d for d in active_dates if d.year == yr]
        yr_eq    = [eq_by_date[d] for d in yr_dates if d in eq_by_date]
        if not yr_eq:
            continue
        start_eq = yr_eq[0]
        end_eq   = yr_eq[-1]
        ret_pct  = (end_eq / start_eq - 1.0) * 100.0
        peak = yr_eq[0]; yr_dd = 0.0
        for v in yr_eq:
            peak  = max(peak, v)
            yr_dd = max(yr_dd, (peak - v) / peak)
        yr_trades = [t for t in trades
                     if hasattr(t["exit_date"], "year") and t["exit_date"].year == yr]
        flag = "  <-- bear" if ret_pct < -15 else ("  <-- bull" if ret_pct > 80 else "")
        print(
            f"  {yr:<6} ${start_eq:>9,.0f} ${end_eq:>9,.0f} "
            f"{ret_pct:>+7.1f}% {yr_dd*100:>6.1f}%  {len(yr_trades):>4}{flag}"
        )


def analyze_combo(data: dict, ind: dict, params: dict) -> None:
    """
    Three required sub-analyses:
      1. Year-by-year equity table
      2. Re-run excluding 2020 + 2021 (2022-onward metrics only)
      3. Explicit outlier/concentration breakdown (top 5 individual trades)
    """
    r          = simulate(data, ind, params)
    trades     = r["trades"]
    eq_curve   = r["eq_curve"]
    common_idx = r["common_idx"]
    n          = len(trades)
    m          = _scalar_metrics(trades, eq_curve, common_idx)
    best1, best3 = _concentration(trades)
    passed     = _passed(trades, m)

    print("=" * 72)
    print("  DEEP ANALYSIS -- CROSS-SECTIONAL MOMENTUM ROTATION")
    print(
        f"  Lb={params['LOOKBACK']}d  MA={params['TREND_MA']}d  "
        f"K={params['K']}  Rb={params['REBAL_DAYS']}d  "
        f"Stop={params['STOP_PCT']*100:.0f}%"
    )
    print(
        f"  Full-sample (net of fees): "
        f"Sharpe={m['sharpe']:.2f}  PF={m['profit_factor']:.2f}  "
        f"WR={m['win_rate']*100:.0f}%  DD={m['max_dd']*100:.1f}%  "
        f"CAGR={m['cagr']*100:.0f}%  N={n}"
    )
    print(f"  Concentration: best1={best1*100:.1f}%  best3={best3*100:.1f}%")
    print("=" * 72)

    # ── 1. Per-year equity ────────────────────────────────────────────────────
    print("\n  1. EQUITY CURVE BY YEAR  (all values net of 0.1%/side fees)")
    print("  " + "-" * 64)
    _year_table(trades, eq_curve, common_idx)

    # ── 2. 2022-onward re-run ─────────────────────────────────────────────────
    print("\n  2. OUT-OF-SAMPLE: 2022-01-01 ONWARD  (excludes 2020 + 2021 bull run)")
    print("  " + "-" * 64)
    cutoff = pd.Timestamp("2022-01-01", tz="UTC")

    # Filter trades and equity curve to 2022+
    t22  = [t for t in trades if t["exit_date"] >= cutoff]
    eq22 = [e for e in eq_curve if e["date"] >= cutoff]
    ci22 = [d for d in common_idx if d >= cutoff]

    if len(eq22) < 2 or not t22:
        print("  Insufficient data after 2022-01-01.")
    else:
        # Patch: treat the first 2022 equity value as the "start" for metrics
        eq_vals22  = [e["equity"] for e in eq22]
        daily_r22  = np.diff(eq_vals22) / np.maximum(np.array(eq_vals22[:-1]), 1e-10)
        sharpe22   = (float(np.mean(daily_r22)) / float(np.std(daily_r22)) * (252 ** 0.5)
                      if np.std(daily_r22) > 0 else 0.0)
        peak22 = eq_vals22[0]; dd22 = 0.0
        for v in eq_vals22:
            peak22 = max(peak22, v)
            dd22   = max(dd22, (peak22 - v) / peak22)
        years22  = (ci22[-1] - ci22[0]).days / 365.0 if len(ci22) > 1 else 1.0
        cagr22   = (eq_vals22[-1] / eq_vals22[0]) ** (1.0 / years22) - 1.0 if years22 > 0 else 0.0
        pf22     = _profit_factor(t22)
        wr22     = len([t for t in t22 if t["pnl_net"] > 0]) / len(t22)

        print(f"  Period:   {ci22[0].date()} -> {ci22[-1].date()}  ({years22:.1f}y)")
        print(f"  N trades: {len(t22)}")
        print(f"  Sharpe:   {sharpe22:.2f}  (target >= {ROT_SHARPE_MIN})")
        print(f"  PF:       {pf22:.2f}  (target >= {ROT_PF_MIN})")
        print(f"  MaxDD:    {dd22*100:.1f}%  (target < {ROT_DD_MAX*100:.0f}%)")
        print(f"  CAGR:     {cagr22*100:.1f}%")
        print(f"  WR:       {wr22*100:.1f}%")
        oos_pass = (sharpe22 >= ROT_SHARPE_MIN and pf22 >= ROT_PF_MIN)
        verdict  = "SURVIVES 2022+ on Sharpe+PF" if oos_pass else "FAILS 2022+ (Sharpe or PF below threshold)"
        print(f"  Verdict:  {verdict}")
        if not oos_pass:
            print("  Strategy edge does not survive without the 2020-2021 bull run.")

    # ── 3. Outlier trade breakdown ────────────────────────────────────────────
    print("\n  3. TOP 10 INDIVIDUAL TRADES BY P&L  (outlier concentration check)")
    print("  " + "-" * 64)
    sorted_trades  = sorted(trades, key=lambda t: t["pnl_net"], reverse=True)
    total_profit   = sum(t["pnl_net"] for t in trades if t["pnl_net"] > 0)
    running_share  = 0.0
    print(f"  Total gross profit: ${total_profit:,.0f}")
    print(f"  {'#':<3} {'Symbol':<10} {'Entry':>10} {'Exit':>10} {'Hold':>5} "
          f"{'PnL%':>7} {'PnL$':>10} {'Cumul%':>8}  Reason")
    print("  " + "-" * 80)
    for i, t in enumerate(sorted_trades[:10], 1):
        e_d = t["entry_date"].date() if hasattr(t["entry_date"], "date") else t["entry_date"]
        x_d = t["exit_date"].date()  if hasattr(t["exit_date"],  "date") else t["exit_date"]
        share = t["pnl_net"] / total_profit * 100.0 if total_profit > 0 and t["pnl_net"] > 0 else 0.0
        running_share += share
        flag = "  <-- outlier" if i <= 3 and share > 5.0 else ""
        print(
            f"  {i:<3} {t['symbol']:<10} {str(e_d):>10} {str(x_d):>10} "
            f"{t['days_held']:>5}d "
            f"{t['pnl_pct']:>+6.1f}% "
            f"${t['pnl_net']:>+9,.0f} "
            f"{running_share:>7.1f}%{flag}"
        )
    print()
    print(f"  best1={best1*100:.1f}%  best3={best3*100:.1f}%  "
          f"(limits: {ROT_BEST1_MAX*100:.0f}% / {ROT_BEST3_MAX*100:.0f}%)")
    conc_ok = best1 <= ROT_BEST1_MAX and best3 <= ROT_BEST3_MAX
    print(f"  Concentration verdict: {'WITHIN LIMITS' if conc_ok else 'EXCEEDS LIMITS -- outlier-driven'}")

    # ── Overall verdict ───────────────────────────────────────────────────────
    print()
    fails = _fail_reasons(trades, m)
    print(f"  {'[PASS]' if passed else '[FAIL]'}  "
          f"Sharpe={m['sharpe']:.2f}  PF={m['profit_factor']:.2f}  "
          f"DD={m['max_dd']*100:.1f}%  N={n}  "
          f"best1={best1*100:.1f}%  best3={best3*100:.1f}%")
    if fails:
        print(f"  Fails: {'; '.join(fails)}")
    print("=" * 72)


# ── Grid runner ───────────────────────────────────────────────────────────────

def run_grid(data: dict, ind: dict, common_idx: list) -> None:
    keys   = list(GRID.keys())
    combos = list(product(*[GRID[k] for k in keys]))
    total  = len(combos)
    done   = _load_done_keys()

    print(f"Grid search: {total} combinations ({total - len(done)} remaining)...\n")

    for i, vals in enumerate(combos, 1):
        params = dict(zip(keys, vals))
        key    = _combo_key(params)
        if key in done:
            continue

        r      = simulate(data, ind, params)
        trades = r["trades"]
        n      = len(trades)
        m      = _scalar_metrics(trades, r["eq_curve"], r["common_idx"])
        passed = _passed(trades, m)
        b1, b3 = _concentration(trades)

        tag = "PASS" if passed else "----"
        print(
            f"  [{i:>3}/{total}] {tag}  "
            f"Lb={params['LOOKBACK']}d MA={params['TREND_MA']}d "
            f"K={params['K']} Rb={params['REBAL_DAYS']}d "
            f"Stop={params['STOP_PCT']*100:.0f}%  |  "
            f"Sh={m['sharpe']:.2f} PF={m['profit_factor']:.2f} "
            f"DD={m['max_dd']*100:.1f}% CAGR={m['cagr']*100:.0f}% "
            f"N={n} b1={b1*100:.1f}% b3={b3*100:.1f}%",
            flush=True,
        )

        _append_csv(params, trades, m, passed)
        done.add(key)

    print()
    print_summary()


# ── Best-combo selector for --analyze ────────────────────────────────────────

def _best_combo_from_csv() -> dict | None:
    if not os.path.exists(ROT_CSV_PATH):
        return None
    df = pd.read_csv(ROT_CSV_PATH)
    if df.empty:
        return None
    passed = df[df["passed"] == 1]
    row    = (passed.sort_values("sharpe", ascending=False).iloc[0]
              if not passed.empty
              else df.sort_values("sharpe", ascending=False).iloc[0])
    return {
        "LOOKBACK":   int(row["LOOKBACK"]),
        "TREND_MA":   int(row["TREND_MA"]),
        "K":          int(row["K"]),
        "REBAL_DAYS": int(row["REBAL_DAYS"]),
        "STOP_PCT":   float(row["STOP_PCT"]),
    }


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Backtest: Binance cross-sectional momentum rotation"
    )
    parser.add_argument("--grid",    action="store_true",
                        help="Run full 72-combo parameter grid search")
    parser.add_argument("--summary", action="store_true",
                        help="Print ranked summary from existing grid CSV")
    parser.add_argument("--analyze", action="store_true",
                        help="Run deep analysis on best combo from grid CSV")
    parser.add_argument("--reset",   action="store_true",
                        help="Delete rotation grid CSV and exit (data cache kept)")
    args = parser.parse_args()

    if args.reset:
        if os.path.exists(ROT_CSV_PATH):
            os.remove(ROT_CSV_PATH)
            print(f"Deleted {ROT_CSV_PATH}")
        else:
            print("Nothing to delete.")
        sys.exit(0)

    if args.summary:
        print_summary()
        sys.exit(0)

    data       = load_data()
    common_idx = sorted(set.intersection(*[set(data[s].index) for s in SYMBOLS]))
    print(f"  {len(SYMBOLS)} symbols  |  {len(common_idx)} common daily bars\n")

    ind = _precompute(data)

    if args.analyze:
        params = _best_combo_from_csv()
        if params is None:
            print("No grid CSV found — running default params for analysis.")
            params = DEFAULT_PARAMS
        else:
            print(f"Analyzing best combo from grid CSV: {params}\n")
        analyze_combo(data, ind, params)

    elif args.grid:
        run_grid(data, ind, common_idx)

    else:
        r      = simulate(data, ind, DEFAULT_PARAMS)
        passed = print_result(DEFAULT_PARAMS, r)
        sys.exit(0 if passed else 1)
