"""
backtest_binance_rotation_v2.py
--------------------------------
Cross-sectional momentum rotation with BTC 50d SMA regime gate added.
Everything else is identical to v1 (equal-weight, same grid, same pass criteria).

CHANGE from v1:
  On each rebalance date: if BTC close <= BTC 50-day SMA the target portfolio is
  set to empty — all held positions are closed at next bar open and no new positions
  are opened. Normal rotation resumes the next rebalance after BTC crosses back above
  its 50d SMA.

LOOKAHEAD VERIFICATION (written once, checked in --analyze output):
  Gate signal:  BTC close[bar i]  vs  mean(BTC close[i-49 : i+1])
  Both values are fully known at bar[i] CLOSE (end of day i).
  Entry/exit execution is at bar[i+1] OPEN.
  No future data is used.

Run:
    python backtest_binance_rotation_v2.py                 # default params
    python backtest_binance_rotation_v2.py --grid          # 72-combo grid
    python backtest_binance_rotation_v2.py --summary       # ranked table from CSV
    python backtest_binance_rotation_v2.py --analyze       # deep IS/OOS analysis
    python backtest_binance_rotation_v2.py --reset         # wipe CSV, keep data cache
"""

from __future__ import annotations

import sys
import os
import argparse
import pickle
import csv
from datetime import date
from itertools import product

import numpy as np
import pandas as pd

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
DATA_DIR      = os.path.join(BASE_DIR, "data")
CACHE_PKL     = os.path.join(DATA_DIR, "backtest_binance_mom_cache.pkl")
CSV_PATH      = os.path.join(DATA_DIR, "backtest_binance_rot_v2_grid.csv")

sys.path.insert(0, BASE_DIR)

SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "ADAUSDT",
    "XRPUSDT", "DOGEUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT",
]
MAX_HISTORY_START = "1 Jan 2017"
COMMISSION_PCT    = 0.001
STARTING_USDT     = 10_000.0
WARMUP_BARS       = 210

REGIME_MA = 50      # BTC SMA period used as market-regime gate

# IS / OOS split — gate threshold NOT optimised against IS data (specified a priori)
OOS_START = pd.Timestamp("2024-01-01", tz="UTC")

# Pass criteria — unchanged from v1
ROT_SHARPE_MIN = 1.0
ROT_PF_MIN     = 2.0
ROT_DD_MAX     = 0.40
ROT_N_MIN      = 20
ROT_BEST1_MAX  = 0.15
ROT_BEST3_MAX  = 0.35

DEFAULT_PARAMS = {
    "LOOKBACK":   60,
    "TREND_MA":   200,
    "K":          3,
    "REBAL_DAYS": 7,
    "STOP_PCT":   0.12,
}

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

_SMA_PERIODS      = [REGIME_MA, 100, 200]   # 50 added for gate
_LOOKBACK_PERIODS = [30, 60, 90]


# ── Data ──────────────────────────────────────────────────────────────────────

def _download_raw() -> dict[str, pd.DataFrame]:
    try:
        from binance.client import Client
    except ImportError:
        print("ERROR: pip install python-binance")
        sys.exit(1)
    client = Client("", "", testnet=False)
    print(f"Downloading {len(SYMBOLS)} symbols (from {MAX_HISTORY_START})...")
    data: dict[str, pd.DataFrame] = {}
    for sym in SYMBOLS:
        print(f"  {sym} ... ", end="", flush=True)
        klines = client.get_historical_klines(sym, Client.KLINE_INTERVAL_1DAY,
                                              MAX_HISTORY_START)
        rows = [{"date":   pd.Timestamp(int(k[0]), unit="ms", tz="UTC").normalize(),
                 "open":   float(k[1]), "high":  float(k[2]),
                 "low":    float(k[3]), "close": float(k[4]),
                 "volume": float(k[5])} for k in klines]
        df = pd.DataFrame(rows).set_index("date")
        data[sym] = df
        print(f"{len(df)} bars")
    return data


def load_data() -> dict[str, pd.DataFrame]:
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(CACHE_PKL):
        with open(CACHE_PKL, "rb") as f:
            cached = pickle.load(f)
        if (cached.get("date") == date.today().isoformat()
                and cached.get("symbols") == sorted(SYMBOLS)):
            print("Using cached OHLCV data (shared with momentum backtest).\n")
            return cached["data"]
    data = _download_raw()
    with open(CACHE_PKL, "wb") as f:
        pickle.dump({"date": date.today().isoformat(),
                     "symbols": sorted(SYMBOLS), "data": data}, f)
    print(f"Cached to {CACHE_PKL}\n")
    return data


# ── Indicators ────────────────────────────────────────────────────────────────

def _precompute(data: dict[str, pd.DataFrame]) -> dict[str, dict]:
    """
    SMA for all periods in _SMA_PERIODS (includes REGIME_MA=50 for the gate).
    Trailing returns for all periods in _LOOKBACK_PERIODS.

    sma[lb][i]          = mean(close[i-lb+1 : i+1])  — includes bar i; known at close.
    trailing_ret[lb][i] = close[i] / close[i-lb] - 1
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
                prev = closes[:-lb]
                curr = closes[lb:]
                vals[lb:] = np.where(prev > 0, curr / prev - 1.0, np.nan)
            trailing_ret[lb] = vals

        ind[sym] = {"sma": sma, "trailing_ret": trailing_ret}
        print(f"  {sym}: {n} bars", flush=True)
    print("  Done.\n")
    return ind


# ── Simulation ────────────────────────────────────────────────────────────────

def simulate(data: dict, ind: dict, params: dict) -> dict:
    """
    Walk-forward portfolio simulation with BTC 50d SMA regime gate.

    REGIME GATE (no lookahead):
      At each rebalance bar[i]:
        - Compute BTC_close[i] and BTC_SMA50[i] = mean(BTC_close[i-49 : i+1]).
        - Both are known at bar[i] close. Execution is at bar[i+1] open.
        - If BTC_close[i] > BTC_SMA50[i]: BULL — run normal rotation logic.
        - Else: BEAR — set target portfolio to empty; all held positions exit
          at bar[i+1] open; no new entries until next BULL rebalance.

    All other logic identical to v1: stop-loss checked daily, equal-weight
    sizing for new slots, 0.1%/side fees on every order.
    """
    lookback   = params["LOOKBACK"]
    trend_ma   = params["TREND_MA"]
    k          = params["K"]
    rebal_days = params["REBAL_DAYS"]
    stop_pct   = params["STOP_PCT"]

    common_idx = sorted(set.intersection(*[set(data[s].index) for s in SYMBOLS]))
    if len(common_idx) < WARMUP_BARS + 2:
        return {"trades": [], "eq_curve": [], "common_idx": common_idx}

    sym_pos = {sym: {d: i for i, d in enumerate(data[sym].index)} for sym in SYMBOLS}

    cash            = STARTING_USDT
    open_pos: dict  = {}
    trades: list    = []
    eq_curve: list  = []
    last_rebal_date = None

    for bar, today in enumerate(common_idx):
        if bar < WARMUP_BARS:
            eq_curve.append({"date": today, "equity": cash})
            continue

        def _close(sym: str):
            si = sym_pos[sym].get(today)
            return float(data[sym]["close"].iat[si]) if si is not None else None

        open_val = sum(pos["units"] * (_close(s) or pos["entry_price"])
                       for s, pos in open_pos.items())
        total_eq = cash + open_val

        # ── 1. Stop-loss ──────────────────────────────────────────────────────
        for sym, pos in list(open_pos.items()):
            close = _close(sym)
            if close is not None and close < pos["entry_price"] * (1.0 - stop_pct):
                proceeds = pos["units"] * close * (1.0 - COMMISSION_PCT)
                cash    += proceeds
                trades.append({
                    "symbol":           sym,
                    "entry_date":       pos["entry_date"],
                    "exit_date":        today,
                    "entry_price":      pos["entry_price"],
                    "exit_price":       close,
                    "pnl_net":          proceeds - pos["cost"],
                    "pnl_pct":          (close / pos["entry_price"] - 1.0) * 100.0,
                    "reason":           f"stop_{stop_pct*100:.0f}pct",
                    "days_held":        bar - pos["entry_bar"],
                    "regime_at_entry":  pos["regime_at_entry"],
                })
                del open_pos[sym]

        # ── 2. Rebalance ──────────────────────────────────────────────────────
        is_rebal = (last_rebal_date is None
                    or (today - last_rebal_date).days >= rebal_days)

        if is_rebal and bar + 1 < len(common_idx):
            last_rebal_date = today
            next_date       = common_idx[bar + 1]

            # ── BTC regime gate ───────────────────────────────────────────────
            # Signal uses bar[i] close and bar[i] SMA — both known at bar[i] close.
            # Execution (exit/entry) at bar[i+1] open. No lookahead.
            btc_si = sym_pos["BTCUSDT"].get(today)
            if btc_si is not None:
                btc_sma_val  = ind["BTCUSDT"]["sma"][REGIME_MA][btc_si]
                btc_close_v  = float(data["BTCUSDT"]["close"].iat[btc_si])
                regime_bull  = (not np.isnan(btc_sma_val)
                                and btc_close_v > btc_sma_val)
            else:
                regime_bull = True   # default to bull if BTC data unavailable

            if regime_bull:
                # BULL: rank eligible symbols, select top K
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
            else:
                # BEAR: go to cash — target portfolio is empty
                target = set()

            # Exit held symbols not in target (covers both rebal rotation and bear exit)
            to_exit = [sym for sym in list(open_pos.keys()) if sym not in target]
            for sym in to_exit:
                pos     = open_pos[sym]
                n_si    = sym_pos[sym].get(next_date)
                exit_px = (float(data[sym]["open"].iat[n_si]) if n_si is not None
                           else _close(sym) or pos["entry_price"])
                proceeds = pos["units"] * exit_px * (1.0 - COMMISSION_PCT)
                cash    += proceeds
                reason   = "bear_exit" if not regime_bull else "rebal_exit"
                trades.append({
                    "symbol":           sym,
                    "entry_date":       pos["entry_date"],
                    "exit_date":        next_date,
                    "entry_price":      pos["entry_price"],
                    "exit_price":       exit_px,
                    "pnl_net":          proceeds - pos["cost"],
                    "pnl_pct":          (exit_px / pos["entry_price"] - 1.0) * 100.0,
                    "reason":           reason,
                    "days_held":        bar - pos["entry_bar"] + 1,
                    "regime_at_entry":  pos["regime_at_entry"],
                })
                del open_pos[sym]

            # Open new positions (only if BULL and there are new targets)
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
                        "entry_price":     entry_px,
                        "entry_bar":       bar,
                        "entry_date":      next_date,
                        "units":           units,
                        "cost":            cost,
                        "regime_at_entry": "SMA_BULL",  # only enter in bull
                    }

        eq_curve.append({"date": today, "equity": total_eq})

    # Force-close remaining positions at last bar close
    last_date = common_idx[-1]
    for sym, pos in list(open_pos.items()):
        si = sym_pos[sym].get(last_date)
        if si is None:
            continue
        close    = float(data[sym]["close"].iat[si])
        proceeds = pos["units"] * close * (1.0 - COMMISSION_PCT)
        trades.append({
            "symbol":          sym,
            "entry_date":      pos["entry_date"],
            "exit_date":       last_date,
            "entry_price":     pos["entry_price"],
            "exit_price":      close,
            "pnl_net":         proceeds - pos["cost"],
            "pnl_pct":         (close / pos["entry_price"] - 1.0) * 100.0,
            "reason":          "end_of_data",
            "days_held":       len(common_idx) - 1 - pos["entry_bar"],
            "regime_at_entry": pos["regime_at_entry"],
        })

    return {"trades": trades, "eq_curve": eq_curve, "common_idx": common_idx}


# ── Metric helpers ────────────────────────────────────────────────────────────

def _profit_factor(trades: list) -> float:
    gross_win  = sum(t["pnl_net"] for t in trades if t["pnl_net"] > 0)
    gross_loss = abs(sum(t["pnl_net"] for t in trades if t["pnl_net"] <= 0))
    return gross_win / gross_loss if gross_loss > 0 else float("inf")


def _concentration(trades: list) -> tuple[float, float]:
    wins         = sorted([t["pnl_net"] for t in trades if t["pnl_net"] > 0], reverse=True)
    total_profit = sum(wins)
    if total_profit <= 0 or not wins:
        return 1.0, 1.0
    return wins[0] / total_profit, sum(wins[:3]) / total_profit


def _scalar_metrics(trades: list, eq_curve: list, common_idx: list) -> dict:
    eq_vals = [e["equity"] for e in eq_curve if e["date"] >= common_idx[WARMUP_BARS]]
    if len(eq_vals) < 2:
        return {"sharpe": 0.0, "win_rate": 0.0, "profit_factor": 0.0,
                "max_dd": 1.0, "cagr": 0.0}
    daily_rets = np.diff(eq_vals) / np.maximum(np.array(eq_vals[:-1]), 1e-10)
    sharpe = (float(np.mean(daily_rets)) / float(np.std(daily_rets)) * (252 ** 0.5)
              if np.std(daily_rets) > 0 else 0.0)
    peak = eq_vals[0]; max_dd = 0.0
    for v in eq_vals:
        peak   = max(peak, v)
        max_dd = max(max_dd, (peak - v) / peak)
    years = (common_idx[-1] - common_idx[WARMUP_BARS]).days / 365.0
    cagr  = (eq_vals[-1] / eq_vals[0]) ** (1.0 / years) - 1.0 if years > 0 else 0.0
    wins  = [t for t in trades if t["pnl_net"] > 0]
    return {"sharpe": sharpe,
            "win_rate": len(wins) / len(trades) if trades else 0.0,
            "profit_factor": _profit_factor(trades),
            "max_dd": max_dd, "cagr": cagr}


def _period_metrics(trades: list, eq_curve: list, common_idx: list,
                    start=None, end=None) -> dict | None:
    """Metrics for a date sub-range.  start/end are pd.Timestamps (or None = open)."""
    def _ok(d):
        return (start is None or d >= start) and (end is None or d < end)
    t_p  = [t for t in trades  if _ok(t["exit_date"])]
    eq_p = [e for e in eq_curve if _ok(e["date"])]
    ci_p = [d for d in common_idx if _ok(d)]
    if len(eq_p) < 2 or not t_p:
        return None
    eq_vals    = [e["equity"] for e in eq_p]
    daily_rets = np.diff(eq_vals) / np.maximum(np.array(eq_vals[:-1]), 1e-10)
    sharpe     = (float(np.mean(daily_rets)) / float(np.std(daily_rets)) * (252 ** 0.5)
                  if np.std(daily_rets) > 0 else 0.0)
    peak = eq_vals[0]; max_dd = 0.0
    for v in eq_vals:
        peak   = max(peak, v)
        max_dd = max(max_dd, (peak - v) / peak)
    years = (ci_p[-1] - ci_p[0]).days / 365.0 if len(ci_p) > 1 else 1.0
    cagr  = (eq_vals[-1] / eq_vals[0]) ** (1.0 / years) - 1.0 if years > 0 else 0.0
    wins  = [t for t in t_p if t["pnl_net"] > 0]
    pf    = _profit_factor(t_p)
    b1, b3 = _concentration(t_p)
    return {"n": len(t_p), "sharpe": sharpe, "pf": pf, "max_dd": max_dd,
            "cagr": cagr, "wr": len(wins) / len(t_p) if t_p else 0.0,
            "total_pnl": sum(t["pnl_net"] for t in t_p),
            "best1": b1, "best3": b3,
            "start": ci_p[0].date(), "end": ci_p[-1].date()}


# ── Pass / fail ───────────────────────────────────────────────────────────────

def _fail_reasons(trades: list, m: dict) -> list[str]:
    fails: list[str] = []
    b1, b3 = _concentration(trades)
    if m["sharpe"]        < ROT_SHARPE_MIN: fails.append(f"Sharpe={m['sharpe']:.2f}<{ROT_SHARPE_MIN}")
    if m["profit_factor"] < ROT_PF_MIN:     fails.append(f"PF={m['profit_factor']:.2f}<{ROT_PF_MIN}")
    if m["max_dd"]        >= ROT_DD_MAX:    fails.append(f"DD={m['max_dd']*100:.1f}%>={ROT_DD_MAX*100:.0f}%")
    if len(trades)        < ROT_N_MIN:      fails.append(f"N={len(trades)}<{ROT_N_MIN}")
    if b1                 > ROT_BEST1_MAX:  fails.append(f"best1={b1*100:.1f}%>{ROT_BEST1_MAX*100:.0f}%")
    if b3                 > ROT_BEST3_MAX:  fails.append(f"best3={b3*100:.1f}%>{ROT_BEST3_MAX*100:.0f}%")
    return fails


def _passed(trades: list, m: dict) -> bool:
    return len(_fail_reasons(trades, m)) == 0


# ── Grid helpers ──────────────────────────────────────────────────────────────

def _combo_key(params: dict) -> str:
    return (f"{params['LOOKBACK']},{params['TREND_MA']},"
            f"{params['K']},{params['REBAL_DAYS']},{params['STOP_PCT']}")


def _load_done_keys() -> set:
    if not os.path.exists(CSV_PATH):
        return set()
    with open(CSV_PATH, newline="") as f:
        header = f.readline()
    expected = {"LOOKBACK", "TREND_MA", "K", "REBAL_DAYS", "STOP_PCT", "best1_pct"}
    if not expected.issubset(set(header.strip().split(","))):
        print(f"[grid] CSV schema changed — wiping {CSV_PATH}.")
        os.remove(CSV_PATH)
        return set()
    done: set = set()
    with open(CSV_PATH, newline="") as f:
        for row in csv.DictReader(f):
            done.add(f"{row['LOOKBACK']},{row['TREND_MA']},"
                     f"{row['K']},{row['REBAL_DAYS']},{row['STOP_PCT']}")
    return done


def _append_csv(params: dict, trades: list, m: dict, passed: bool) -> None:
    b1, b3 = _concentration(trades)
    fails  = _fail_reasons(trades, m)
    write_header = not os.path.exists(CSV_PATH)
    with open(CSV_PATH, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if write_header:
            w.writeheader()
        w.writerow({
            "LOOKBACK": params["LOOKBACK"], "TREND_MA": params["TREND_MA"],
            "K": params["K"], "REBAL_DAYS": params["REBAL_DAYS"],
            "STOP_PCT": params["STOP_PCT"],
            "sharpe": round(m["sharpe"], 3), "win_rate": round(m["win_rate"], 3),
            "profit_factor": round(m["profit_factor"], 3),
            "max_dd": round(m["max_dd"], 3), "cagr": round(m["cagr"], 3),
            "n_trades": len(trades),
            "best1_pct": round(b1, 4), "best3_pct": round(b3, 4),
            "passed": int(passed),
            "fail_reason": "; ".join(fails) if fails else "ok",
        })


# ── Summary printer ───────────────────────────────────────────────────────────

def _fmt_row(row: "pd.Series") -> str:
    return (f"Lb={int(row['LOOKBACK'])}d MA={int(row['TREND_MA'])}d "
            f"K={int(row['K'])} Rb={int(row['REBAL_DAYS'])}d "
            f"Stop={float(row['STOP_PCT'])*100:.0f}%  "
            f"Sh={float(row['sharpe']):.2f} PF={float(row['profit_factor']):.2f} "
            f"WR={float(row['win_rate'])*100:.0f}% DD={float(row['max_dd'])*100:.1f}% "
            f"CAGR={float(row['cagr'])*100:.0f}% N={int(row['n_trades'])} "
            f"b1={float(row['best1_pct'])*100:.1f}% b3={float(row['best3_pct'])*100:.1f}%")


def print_summary() -> None:
    if not os.path.exists(CSV_PATH):
        print("No results file found.  Run --grid first.")
        return
    df    = pd.read_csv(CSV_PATH)
    total = len(df)
    robust = df[df["passed"] == 1].sort_values("sharpe", ascending=False)
    std_pass = ((df["sharpe"] >= ROT_SHARPE_MIN) & (df["profit_factor"] >= ROT_PF_MIN)
                & (df["max_dd"] < ROT_DD_MAX) & (df["n_trades"] >= ROT_N_MIN))
    conc_fail = (df["best1_pct"] > ROT_BEST1_MAX) | (df["best3_pct"] > ROT_BEST3_MAX)
    outlier_risk = df[std_pass & conc_fail].sort_values("sharpe", ascending=False)

    print(f"\n{'='*78}")
    print(f"  ROTATION v2 (BTC {REGIME_MA}d SMA gate) -- {total} combos evaluated")
    print(f"  Pass: Sh>={ROT_SHARPE_MIN}  PF>={ROT_PF_MIN}  DD<{ROT_DD_MAX*100:.0f}%  "
          f"N>={ROT_N_MIN}  b1<={ROT_BEST1_MAX*100:.0f}%  b3<={ROT_BEST3_MAX*100:.0f}%")
    print(f"{'='*78}")

    print(f"\n  ROBUST ({len(robust)} passed all 6 criteria):")
    print("  " + "-" * 78)
    if robust.empty:
        print("  None.")
    else:
        for rank, (_, row) in enumerate(robust.head(5).iterrows(), 1):
            print(f"  #{rank}  {_fmt_row(row)}")

    print(f"\n  OUTLIER/CONC RISK ({len(outlier_risk)} combos -- std criteria met but concentration fails):")
    print("  " + "-" * 78)
    if outlier_risk.empty:
        print("  None.")
    else:
        for _, row in outlier_risk.head(3).iterrows():
            print(f"  [CONC]  {_fmt_row(row)}")

    if robust.empty:
        n20 = df[df["n_trades"] >= ROT_N_MIN].sort_values("sharpe", ascending=False)
        print(f"\n  CLOSEST MISSES (N>={ROT_N_MIN}):")
        print("  " + "-" * 78)
        if n20.empty:
            print(f"  No combo reached N>={ROT_N_MIN}.")
        else:
            for rank, (_, row) in enumerate(n20.head(5).iterrows(), 1):
                fr = str(row.get("fail_reason", ""))
                print(f"  #{rank}  {_fmt_row(row)}  <- MISS: {fr}")

    print(f"\n{'='*78}")


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
        tag    = "PASS" if passed else "----"
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


# ── Deep analysis ─────────────────────────────────────────────────────────────

def _print_period(label: str, pm: dict | None) -> None:
    if pm is None:
        print(f"  {label}: insufficient data")
        return
    pf_str = f"{pm['pf']:.2f}" if pm["pf"] != float("inf") else "inf"
    b1_flag = " [!]" if pm["best1"] > ROT_BEST1_MAX else ""
    b3_flag = " [!]" if pm["best3"] > ROT_BEST3_MAX else ""
    pass_str = ""
    fails = []
    if pm["sharpe"] < ROT_SHARPE_MIN: fails.append(f"Sh<{ROT_SHARPE_MIN}")
    if pm["pf"]     < ROT_PF_MIN:     fails.append(f"PF<{ROT_PF_MIN}")
    if pm["max_dd"] >= ROT_DD_MAX:    fails.append(f"DD>={ROT_DD_MAX*100:.0f}%")
    if pm["n"]      < ROT_N_MIN:      fails.append(f"N<{ROT_N_MIN}")
    pass_str = "PASS" if not fails else f"FAIL ({', '.join(fails)})"
    print(f"  {label} ({pm['start']} -> {pm['end']}, {pm['n']} trades):  [{pass_str}]")
    print(f"    Sharpe={pm['sharpe']:.2f}  PF={pf_str}  DD={pm['max_dd']*100:.1f}%  "
          f"CAGR={pm['cagr']*100:.0f}%  WR={pm['wr']*100:.0f}%  "
          f"TotalPnL=${pm['total_pnl']:+,.0f}")
    print(f"    best1={pm['best1']*100:.1f}%{b1_flag}  "
          f"best3={pm['best3']*100:.1f}%{b3_flag}  "
          f"(limits {ROT_BEST1_MAX*100:.0f}% / {ROT_BEST3_MAX*100:.0f}%)")


def analyze_combo(data: dict, ind: dict, params: dict) -> None:
    r          = simulate(data, ind, params)
    trades     = r["trades"]
    eq_curve   = r["eq_curve"]
    common_idx = r["common_idx"]
    n          = len(trades)
    m          = _scalar_metrics(trades, eq_curve, common_idx)
    b1, b3     = _concentration(trades)
    passed     = _passed(trades, m)

    print("=" * 74)
    print(f"  DEEP ANALYSIS -- ROTATION v2 (BTC {REGIME_MA}d SMA gate)")
    print(f"  Lb={params['LOOKBACK']}d  MA={params['TREND_MA']}d  K={params['K']}  "
          f"Rb={params['REBAL_DAYS']}d  Stop={params['STOP_PCT']*100:.0f}%")
    print(f"  Full-sample: Sh={m['sharpe']:.2f}  PF={m['profit_factor']:.2f}  "
          f"DD={m['max_dd']*100:.1f}%  CAGR={m['cagr']*100:.0f}%  "
          f"WR={m['win_rate']*100:.0f}%  N={n}")
    print("=" * 74)

    # ── 0. Regime gate lookahead verification ─────────────────────────────────
    print("\n  0. REGIME GATE — LOOKAHEAD VERIFICATION")
    print("  " + "-" * 66)
    print(f"  Gate signal : BTC close[bar i]  vs  mean(BTC close[i-{REGIME_MA-1}:i+1])")
    print(f"  Both values : fully known at bar[i] CLOSE (end of day i)")
    print(f"  Execution   : bar[i+1] OPEN (next day's opening price)")
    print(f"  Conclusion  : NO LOOKAHEAD — decision uses only data available at close.")
    bear_entries = [t for t in trades
                    if t.get("regime_at_entry") not in (None, "SMA_BULL")]
    print(f"  Gate check  : {len(bear_entries)} trades entered in SMA_BEAR regime "
          f"({'NONE -- gate is working' if not bear_entries else 'WARNING: gate leakage'})")

    # ── 1. IS / OOS split ─────────────────────────────────────────────────────
    print(f"\n  1. IN-SAMPLE vs OUT-OF-SAMPLE SPLIT  (cutoff: {OOS_START.date()})")
    print(f"     Gate threshold (50d) was specified a priori from regime analysis,")
    print(f"     not fitted to IS data. Grid params are the only thing being tested.")
    print("  " + "-" * 66)
    pm_full = _period_metrics(trades, eq_curve, common_idx)
    pm_is   = _period_metrics(trades, eq_curve, common_idx, end=OOS_START)
    pm_oos  = _period_metrics(trades, eq_curve, common_idx, start=OOS_START)
    _print_period("FULL", pm_full)
    print()
    _print_period("IS  (2021-2023)", pm_is)
    print()
    _print_period("OOS (2024-2026)", pm_oos)

    if pm_is and pm_oos:
        is_ok  = pm_is["sharpe"]  >= ROT_SHARPE_MIN and pm_is["pf"]  >= ROT_PF_MIN
        oos_ok = pm_oos["sharpe"] >= ROT_SHARPE_MIN and pm_oos["pf"] >= ROT_PF_MIN
        if is_ok and oos_ok:
            print(f"\n  OOS verdict: HOLDS UP — both IS and OOS clear Sharpe+PF thresholds.")
        elif is_ok and not oos_ok:
            print(f"\n  OOS verdict: DEGRADES — IS passes but OOS fails Sharpe or PF.")
        elif not is_ok and oos_ok:
            print(f"\n  OOS verdict: IMPROVES — stronger OOS than IS (unusual, inspect).")
        else:
            print(f"\n  OOS verdict: FAILS in both periods.")

    # ── 2. Regime split (confirm gate effect) ────────────────────────────────
    print(f"\n  2. REGIME SPLIT (confirm gate is gating correctly)")
    print("  " + "-" * 66)
    bull_t = [t for t in trades if t.get("regime_at_entry") == "SMA_BULL"]
    bear_t = [t for t in trades if t.get("regime_at_entry") not in (None, "SMA_BULL")]
    for label, tlist in [("SMA_BULL (entered)", bull_t), ("SMA_BEAR (leaked)", bear_t)]:
        if not tlist:
            print(f"  {label}: 0 trades")
            continue
        wins = [t for t in tlist if t["pnl_net"] > 0]
        pf   = _profit_factor(tlist)
        print(f"  {label}: N={len(tlist)}  WR={len(wins)/len(tlist)*100:.0f}%  "
              f"PF={pf:.2f}  AvgPnL={sum(t['pnl_pct'] for t in tlist)/len(tlist):+.1f}%")

    # ── 3. Year-by-year ───────────────────────────────────────────────────────
    print(f"\n  3. EQUITY CURVE BY YEAR (net of 0.1%/side fees)")
    print("  " + "-" * 60)
    eq_by_date   = {e["date"]: e["equity"] for e in eq_curve}
    active_dates = [d for d in common_idx if d >= common_idx[WARMUP_BARS]]
    print(f"  {'Year':<6} {'StartEq':>10} {'EndEq':>10} {'Ret':>8} {'MaxDD':>7} {'N':>5}")
    print("  " + "-" * 56)
    for yr in sorted({d.year for d in active_dates}):
        yr_eq = [eq_by_date[d] for d in active_dates if d.year == yr and d in eq_by_date]
        if not yr_eq:
            continue
        ret = (yr_eq[-1] / yr_eq[0] - 1.0) * 100.0
        pk  = yr_eq[0]; yr_dd = 0.0
        for v in yr_eq:
            pk = max(pk, v); yr_dd = max(yr_dd, (pk - v) / pk)
        yt = [t for t in trades
              if hasattr(t["exit_date"], "year") and t["exit_date"].year == yr]
        flag = "  <-- bear" if ret < -15 else ("  <-- bull" if ret > 50 else "")
        print(f"  {yr:<6} ${yr_eq[0]:>9,.0f} ${yr_eq[-1]:>9,.0f} "
              f"{ret:>+7.1f}% {yr_dd*100:>6.1f}%  {len(yt):>4}{flag}")

    # ── 4. Concentration check ────────────────────────────────────────────────
    print(f"\n  4. TOP 10 TRADES (outlier concentration)")
    print("  " + "-" * 76)
    total_profit = sum(t["pnl_net"] for t in trades if t["pnl_net"] > 0)
    top10 = sorted(trades, key=lambda t: t["pnl_net"], reverse=True)[:10]
    running = 0.0
    print(f"  Total gross profit: ${total_profit:,.0f}")
    print(f"  {'#':<3} {'Symbol':<10} {'Entry':>10} {'Exit':>10} {'Hold':>5} "
          f"{'PnL%':>7} {'PnL$':>10} {'Cum%':>7}")
    print("  " + "-" * 72)
    for i, t in enumerate(top10, 1):
        e_d  = t["entry_date"].date() if hasattr(t["entry_date"], "date") else t["entry_date"]
        x_d  = t["exit_date"].date()  if hasattr(t["exit_date"],  "date") else t["exit_date"]
        shr  = t["pnl_net"] / total_profit * 100.0 if total_profit > 0 and t["pnl_net"] > 0 else 0.0
        running += shr
        print(f"  {i:<3} {t['symbol']:<10} {str(e_d):>10} {str(x_d):>10} "
              f"{t['days_held']:>5}d {t['pnl_pct']:>+6.1f}% "
              f"${t['pnl_net']:>+9,.0f} {running:>6.1f}%")
    print(f"\n  best1={b1*100:.1f}%  best3={b3*100:.1f}%  "
          f"(limits {ROT_BEST1_MAX*100:.0f}% / {ROT_BEST3_MAX*100:.0f}%)")

    fails = _fail_reasons(trades, m)
    print()
    print(f"  {'[PASS]' if passed else '[FAIL]'}  Sh={m['sharpe']:.2f}  "
          f"PF={m['profit_factor']:.2f}  DD={m['max_dd']*100:.1f}%  N={n}  "
          f"b1={b1*100:.1f}%  b3={b3*100:.1f}%")
    if fails:
        print(f"  Fails: {'; '.join(fails)}")
    print("=" * 74)


# ── Best-combo selector ───────────────────────────────────────────────────────

def _best_combo_from_csv() -> dict | None:
    if not os.path.exists(CSV_PATH):
        return None
    df = pd.read_csv(CSV_PATH)
    if df.empty:
        return None
    passed = df[df["passed"] == 1]
    row = (passed.sort_values("sharpe", ascending=False).iloc[0]
           if not passed.empty
           else df.sort_values("sharpe", ascending=False).iloc[0])
    return {"LOOKBACK": int(row["LOOKBACK"]), "TREND_MA": int(row["TREND_MA"]),
            "K": int(row["K"]), "REBAL_DAYS": int(row["REBAL_DAYS"]),
            "STOP_PCT": float(row["STOP_PCT"])}


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Rotation v2: cross-sectional momentum + BTC 50d SMA gate")
    parser.add_argument("--grid",    action="store_true", help="72-combo grid search")
    parser.add_argument("--summary", action="store_true", help="Ranked summary from CSV")
    parser.add_argument("--analyze", action="store_true",
                        help="IS/OOS + regime analysis on best combo")
    parser.add_argument("--reset",   action="store_true",
                        help="Delete rotation v2 CSV (data cache kept)")
    args = parser.parse_args()

    if args.reset:
        if os.path.exists(CSV_PATH):
            os.remove(CSV_PATH); print(f"Deleted {CSV_PATH}")
        else:
            print("Nothing to delete.")
        sys.exit(0)

    if args.summary:
        print_summary(); sys.exit(0)

    data       = load_data()
    common_idx = sorted(set.intersection(*[set(data[s].index) for s in SYMBOLS]))
    print(f"  {len(SYMBOLS)} symbols  |  {len(common_idx)} common daily bars\n")
    ind        = _precompute(data)

    if args.analyze:
        params = _best_combo_from_csv()
        if params is None:
            print("No grid CSV found — using default params.")
            params = DEFAULT_PARAMS
        else:
            print(f"Best combo from grid: {params}\n")
        analyze_combo(data, ind, params)
    elif args.grid:
        run_grid(data, ind, common_idx)
    else:
        r      = simulate(data, ind, DEFAULT_PARAMS)
        trades = r["trades"]
        m      = _scalar_metrics(trades, r["eq_curve"], r["common_idx"])
        n      = len(trades)
        passed = _passed(trades, m)
        b1, b3 = _concentration(trades)
        print(f"Default params:  Sh={m['sharpe']:.2f}  PF={m['profit_factor']:.2f}  "
              f"DD={m['max_dd']*100:.1f}%  N={n}  b1={b1*100:.1f}%  b3={b3*100:.1f}%")
        print(f"{'[PASS]' if passed else '[FAIL]'}  {_fail_reasons(trades, m)}")
        sys.exit(0 if passed else 1)
