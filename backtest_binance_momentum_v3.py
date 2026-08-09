"""
backtest_binance_momentum_v3.py
--------------------------------
Momentum v2 + hard 12% per-position notional cap.

The only change from v2:
  slot_size = min(current_total / MAX_POSITIONS,  current_total * CAP_PCT)

If CAP_PCT × current_total < current_total / MAX_POSITIONS, the cap is binding.
At 12% cap with 3 slots, maximum deployment is 36% of portfolio equity at any
entry. Positions held can appreciate beyond 12%; the cap applies only at entry.

Rationale: Momentum v2 passed Sharpe/PF/DD/N on all time periods but failed the
concentration caps (best1=20.7%, best3=35.6%). Root cause: 1 trade (XRPUSDT Nov
2024, +298%, $+217k) dominated total profit. ATR-based sizing is more principled
but the 12% hard cap is the cheapest fix — if it passes, no further complexity needed.

Pass criteria (unchanged from v2):
  Sharpe >= 1.0, PF >= 2.0, MaxDD < 40%, N >= 20,
  best1_pct <= 15%, best3_pct <= 35%

Run:
    python backtest_binance_momentum_v3.py                 # best v2 params, 12% cap
    python backtest_binance_momentum_v3.py --grid          # 729-combo grid
    python backtest_binance_momentum_v3.py --summary       # ranked table from CSV
    python backtest_binance_momentum_v3.py --analyze       # IS/OOS + ex-top-3 + concentration
    python backtest_binance_momentum_v3.py --reset         # wipe v3 CSV (data cache kept)
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

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
DATA_DIR  = os.path.join(BASE_DIR, "data")
CACHE_PKL = os.path.join(DATA_DIR, "backtest_binance_mom_cache.pkl")
CSV_PATH  = os.path.join(DATA_DIR, "backtest_binance_mom_v3_grid.csv")

sys.path.insert(0, BASE_DIR)

from strategies.binance.mean_reversion import _rsi

SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "ADAUSDT",
    "XRPUSDT", "DOGEUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT",
]
MAX_HISTORY_START = "1 Jan 2017"
COMMISSION_PCT    = 0.001
STARTING_USDT     = 10_000.0
MAX_POSITIONS     = 3
WARMUP_BARS       = 50

REGIME_MA = 50
OOS_START = pd.Timestamp("2024-01-01", tz="UTC")

# Per-position notional cap: entry cost <= CAP_PCT × total_equity.
# With 3 slots at 12% each, max deployment = 36% at any entry.
CAP_PCT = 0.12

TF_SHARPE_MIN = 1.0
TF_PF_MIN     = 2.0
TF_DD_MAX     = 0.40
TF_N_MIN      = 20
TF_BEST1_MAX  = 0.15
TF_BEST3_MAX  = 0.35

DEFAULT_PARAMS = {
    "RSI_LOW":       45,
    "RSI_HIGH":      65,
    "BREAKOUT_DAYS": 25,
    "EXIT_DAYS":     7,
    "STOP_PCT":      0.05,
    "MAX_HOLD":      60,
}

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
    "sharpe", "win_rate", "profit_factor", "max_dd", "cagr", "n_trades",
    "best1_pct", "best3_pct",
    "ex3_pf",
    "passed", "fail_reason",
]

_BREAKOUT_LOOKBACKS = [15, 20, 25]
_EXIT_LOOKBACKS     = [7, 10, 15]


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
            print("Using cached OHLCV data.\n")
            return cached["data"]
    data = _download_raw()
    with open(CACHE_PKL, "wb") as f:
        pickle.dump({"date": date.today().isoformat(),
                     "symbols": sorted(SYMBOLS), "data": data}, f)
    print(f"Cached to {CACHE_PKL}\n")
    return data


# ── Indicators ────────────────────────────────────────────────────────────────

def _precompute(data: dict[str, pd.DataFrame]) -> dict[str, dict]:
    print(f"Pre-computing indicators for {len(data)} symbols...", flush=True)
    ind: dict[str, dict] = {}
    for sym, df in data.items():
        closes    = df["close"].tolist()
        n         = len(closes)
        np_closes = np.array(closes, dtype=float)

        rsi_vals = [_rsi(closes[:i + 1], period=14) for i in range(n)]

        roll_max = {}
        for lb in _BREAKOUT_LOOKBACKS:
            roll_max[lb] = [None if i < lb else max(closes[i - lb: i]) for i in range(n)]

        roll_min = {}
        for lb in _EXIT_LOOKBACKS:
            roll_min[lb] = [None if i < lb else min(closes[i - lb: i]) for i in range(n)]

        sma50 = np.full(n, np.nan)
        if n >= REGIME_MA:
            cs = np.concatenate(([0.0], np.cumsum(np_closes)))
            sma50[REGIME_MA - 1:] = (cs[REGIME_MA:] - cs[:-REGIME_MA]) / REGIME_MA

        ind[sym] = {"rsi": rsi_vals, "roll_max": roll_max,
                    "roll_min": roll_min, "sma50": sma50}
        print(f"  {sym}: {n} bars", flush=True)
    print("  Done.\n")
    return ind


# ── Simulation ────────────────────────────────────────────────────────────────

def simulate(data: dict[str, pd.DataFrame], ind: dict, params: dict) -> dict:
    """
    Identical to momentum v2 except:
      slot_size = min(current_total / MAX_POSITIONS,  current_total * CAP_PCT)

    CAP_PCT = 0.12 caps each new entry to 12% of portfolio equity.
    The cap applies at entry only — open positions can appreciate beyond 12%.
    """
    rsi_low    = params["RSI_LOW"]
    rsi_high   = params["RSI_HIGH"]
    breakout_d = params["BREAKOUT_DAYS"]
    exit_d     = params["EXIT_DAYS"]
    stop_pct   = params["STOP_PCT"]
    max_hold   = params["MAX_HOLD"]

    common_idx = sorted(set.intersection(*[set(data[s].index) for s in SYMBOLS]))
    if len(common_idx) < WARMUP_BARS + 2:
        return {"trades": [], "eq_curve": [], "common_idx": common_idx}

    sym_pos = {sym: {d: i for i, d in enumerate(data[sym].index)} for sym in SYMBOLS}

    cash     = STARTING_USDT
    open_pos: dict = {}
    trades:   list = []
    eq_curve: list = []

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

        open_val = sum(pos["units"] * (_get(s, "close") or pos["entry_price"])
                       for s, pos in open_pos.items())
        total_eq = cash + open_val

        # ── Exits (unchanged from v2) ─────────────────────────────────────────
        to_close = []
        for sym, pos in list(open_pos.items()):
            days_held = bar - pos["entry_bar"]
            close     = _get(sym, "close")
            if close is None:
                continue
            exit_price = None; reason = ""
            if stop_pct > 0 and close < pos["entry_price"] * (1 - stop_pct):
                exit_price = close; reason = f"hard_stop -{stop_pct*100:.0f}%"
            elif max_hold > 0 and days_held >= max_hold:
                exit_price = close; reason = f"max_hold {days_held}d"
            else:
                trail_low = _roll_min(sym, exit_d)
                if trail_low is not None and close < trail_low:
                    exit_price = close; reason = f"donchian_exit {exit_d}d"
            if exit_price is not None:
                to_close.append((sym, exit_price, reason, pos, days_held))

        for sym, exit_price, reason, pos, days_held in to_close:
            proceeds = pos["units"] * exit_price * (1 - COMMISSION_PCT)
            cash    += proceeds
            trades.append({
                "symbol":          sym,
                "entry_date":      pos["entry_date"],
                "exit_date":       today,
                "entry_price":     pos["entry_price"],
                "exit_price":      exit_price,
                "pnl_net":         proceeds - pos["cost"],
                "pnl_pct":         (exit_price / pos["entry_price"] - 1) * 100,
                "reason":          reason,
                "days_held":       days_held,
                "regime_at_entry": pos["regime_at_entry"],
                "entry_cost":      pos["cost"],
            })
            del open_pos[sym]

        # ── BTC regime gate ───────────────────────────────────────────────────
        btc_si      = sym_pos["BTCUSDT"].get(today)
        regime_bull = True
        if btc_si is not None:
            btc_sma50  = ind["BTCUSDT"]["sma50"][btc_si]
            btc_close  = float(data["BTCUSDT"]["close"].iat[btc_si])
            regime_bull = not np.isnan(btc_sma50) and btc_close > btc_sma50

        # ── Entry scan (BULL only) ────────────────────────────────────────────
        slots_free = MAX_POSITIONS - len(open_pos)
        if regime_bull and slots_free > 0 and bar + 1 < len(common_idx):
            current_total = cash + sum(
                pos["units"] * (_get(s, "close") or pos["entry_price"])
                for s, pos in open_pos.items()
            )
            # Hard 12% per-position notional cap (the only change from v2)
            uncapped_slot = current_total / MAX_POSITIONS
            slot_size     = min(uncapped_slot, current_total * CAP_PCT)

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

                next_date   = common_idx[bar + 1]
                next_si     = sym_pos[sym].get(next_date)
                if next_si is None:
                    continue
                entry_price = float(data[sym]["open"].iat[next_si])
                if entry_price <= 0:
                    continue

                cost  = slot_size * (1 + COMMISSION_PCT)
                units = slot_size / entry_price
                cash -= cost
                open_pos[sym] = {
                    "entry_price":     entry_price,
                    "entry_bar":       bar,
                    "entry_date":      next_date,
                    "units":           units,
                    "cost":            cost,
                    "regime_at_entry": "SMA_BULL",
                }
                slots_free -= 1

        eq_curve.append({"date": today, "equity": total_eq})

    last_date = common_idx[-1]
    for sym, pos in open_pos.items():
        si = sym_pos[sym].get(last_date)
        if si is None:
            continue
        close    = float(data[sym]["close"].iat[si])
        proceeds = pos["units"] * close * (1 - COMMISSION_PCT)
        trades.append({
            "symbol":          sym,
            "entry_date":      pos["entry_date"],
            "exit_date":       last_date,
            "entry_price":     pos["entry_price"],
            "exit_price":      close,
            "pnl_net":         proceeds - pos["cost"],
            "pnl_pct":         (close / pos["entry_price"] - 1) * 100,
            "reason":          "end_of_data",
            "days_held":       len(common_idx) - 1 - pos["entry_bar"],
            "regime_at_entry": pos["regime_at_entry"],
            "entry_cost":      pos["cost"],
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


def _ex_top3_pf(trades: list) -> float:
    remaining = sorted(trades, key=lambda t: t["pnl_net"], reverse=True)[3:]
    return _profit_factor(remaining)


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
    ex3   = _ex_top3_pf(t_p)
    return {"n": len(t_p), "sharpe": sharpe, "pf": pf, "max_dd": max_dd,
            "cagr": cagr, "wr": len(wins) / len(t_p) if t_p else 0.0,
            "total_pnl": sum(t["pnl_net"] for t in t_p),
            "best1": b1, "best3": b3, "ex3_pf": ex3,
            "start": ci_p[0].date(), "end": ci_p[-1].date()}


# ── Pass / fail ───────────────────────────────────────────────────────────────

def _fail_reasons(trades: list, m: dict) -> list[str]:
    fails: list[str] = []
    b1, b3 = _concentration(trades)
    if m["sharpe"]        < TF_SHARPE_MIN: fails.append(f"Sharpe={m['sharpe']:.2f}<{TF_SHARPE_MIN}")
    if m["profit_factor"] < TF_PF_MIN:     fails.append(f"PF={m['profit_factor']:.2f}<{TF_PF_MIN}")
    if m["max_dd"]        >= TF_DD_MAX:    fails.append(f"DD={m['max_dd']*100:.1f}%>={TF_DD_MAX*100:.0f}%")
    if len(trades)        < TF_N_MIN:      fails.append(f"N={len(trades)}<{TF_N_MIN}")
    if b1                 > TF_BEST1_MAX:  fails.append(f"best1={b1*100:.1f}%>{TF_BEST1_MAX*100:.0f}%")
    if b3                 > TF_BEST3_MAX:  fails.append(f"best3={b3*100:.1f}%>{TF_BEST3_MAX*100:.0f}%")
    return fails


def _passed(trades: list, m: dict) -> bool:
    return len(_fail_reasons(trades, m)) == 0


# ── Grid helpers ──────────────────────────────────────────────────────────────

def _combo_key(params: dict) -> str:
    return (f"{params['RSI_LOW']},{params['RSI_HIGH']},"
            f"{params['BREAKOUT_DAYS']},{params['EXIT_DAYS']},"
            f"{params['STOP_PCT']},{params['MAX_HOLD']}")


def _load_done_keys() -> set:
    if not os.path.exists(CSV_PATH):
        return set()
    with open(CSV_PATH, newline="") as f:
        header = f.readline()
    expected = {"RSI_LOW", "RSI_HIGH", "BREAKOUT_DAYS", "EXIT_DAYS",
                "STOP_PCT", "MAX_HOLD", "ex3_pf"}
    if not expected.issubset(set(header.strip().split(","))):
        print(f"[grid] CSV schema changed — wiping {CSV_PATH}.")
        os.remove(CSV_PATH)
        return set()
    done: set = set()
    with open(CSV_PATH, newline="") as f:
        for row in csv.DictReader(f):
            done.add(f"{row['RSI_LOW']},{row['RSI_HIGH']},"
                     f"{row['BREAKOUT_DAYS']},{row['EXIT_DAYS']},"
                     f"{row['STOP_PCT']},{row['MAX_HOLD']}")
    return done


def _append_csv(params: dict, trades: list, m: dict, passed: bool) -> None:
    b1, b3 = _concentration(trades)
    ex3    = _ex_top3_pf(trades)
    fails  = _fail_reasons(trades, m)
    write_header = not os.path.exists(CSV_PATH)
    with open(CSV_PATH, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if write_header:
            w.writeheader()
        w.writerow({
            "RSI_LOW": params["RSI_LOW"], "RSI_HIGH": params["RSI_HIGH"],
            "BREAKOUT_DAYS": params["BREAKOUT_DAYS"], "EXIT_DAYS": params["EXIT_DAYS"],
            "STOP_PCT": params["STOP_PCT"], "MAX_HOLD": params["MAX_HOLD"],
            "sharpe": round(m["sharpe"], 3), "win_rate": round(m["win_rate"], 3),
            "profit_factor": round(m["profit_factor"], 3),
            "max_dd": round(m["max_dd"], 3), "cagr": round(m["cagr"], 3),
            "n_trades": len(trades),
            "best1_pct": round(b1, 4), "best3_pct": round(b3, 4),
            "ex3_pf": round(ex3, 3),
            "passed": int(passed),
            "fail_reason": "; ".join(fails) if fails else "ok",
        })


# ── Summary printer ───────────────────────────────────────────────────────────

def _fmt_row(row: "pd.Series") -> str:
    stop_str = f"Stop{float(row['STOP_PCT'])*100:.0f}% " if float(row["STOP_PCT"]) > 0 else ""
    hold_str = f"Hold{int(row['MAX_HOLD'])}d "           if int(row["MAX_HOLD"]) > 0   else ""
    ex3_str  = f" ex3PF={float(row['ex3_pf']):.2f}" if "ex3_pf" in row.index else ""
    return (f"RSI[{int(row['RSI_LOW'])},{int(row['RSI_HIGH'])}] "
            f"Break>{int(row['BREAKOUT_DAYS'])}d Exit<{int(row['EXIT_DAYS'])}d "
            f"{stop_str}{hold_str}"
            f"Sh={float(row['sharpe']):.2f} PF={float(row['profit_factor']):.2f} "
            f"WR={float(row['win_rate'])*100:.0f}% DD={float(row['max_dd'])*100:.1f}% "
            f"CAGR={float(row['cagr'])*100:.0f}% N={int(row['n_trades'])} "
            f"b1={float(row['best1_pct'])*100:.1f}% b3={float(row['best3_pct'])*100:.1f}%"
            f"{ex3_str}")


def print_summary() -> None:
    if not os.path.exists(CSV_PATH):
        print("No results file found.  Run --grid first.")
        return
    df    = pd.read_csv(CSV_PATH)
    total = len(df)
    robust = df[df["passed"] == 1].sort_values("sharpe", ascending=False)
    std_pass = ((df["sharpe"] >= TF_SHARPE_MIN) & (df["profit_factor"] >= TF_PF_MIN)
                & (df["max_dd"] < TF_DD_MAX) & (df["n_trades"] >= TF_N_MIN))
    conc_fail = (df["best1_pct"] > TF_BEST1_MAX) | (df["best3_pct"] > TF_BEST3_MAX)
    outlier_risk = df[std_pass & conc_fail].sort_values("sharpe", ascending=False)

    print(f"\n{'='*80}")
    print(f"  MOMENTUM v3 (BTC {REGIME_MA}d SMA gate + {CAP_PCT*100:.0f}% position cap)"
          f" -- {total} combos")
    print(f"  Pass: Sh>={TF_SHARPE_MIN}  PF>={TF_PF_MIN}  DD<{TF_DD_MAX*100:.0f}%  "
          f"N>={TF_N_MIN}  b1<={TF_BEST1_MAX*100:.0f}%  b3<={TF_BEST3_MAX*100:.0f}%")
    print(f"{'='*80}")

    print(f"\n  ROBUST ({len(robust)} passed all 6 criteria):")
    print("  " + "-" * 80)
    if robust.empty:
        print("  None.")
    else:
        for rank, (_, row) in enumerate(robust.head(5).iterrows(), 1):
            print(f"  #{rank}  {_fmt_row(row)}")

    print(f"\n  OUTLIER/CONC RISK ({len(outlier_risk)} combos -- std criteria met "
          f"but concentration fails):")
    print("  " + "-" * 80)
    if outlier_risk.empty:
        print("  None.")
    else:
        for _, row in outlier_risk.head(3).iterrows():
            print(f"  [CONC]  {_fmt_row(row)}")

    if robust.empty:
        n20 = df[df["n_trades"] >= TF_N_MIN].sort_values("sharpe", ascending=False)
        print(f"\n  CLOSEST MISSES (N>={TF_N_MIN}):")
        print("  " + "-" * 80)
        if n20.empty:
            print(f"  No combo reached N>={TF_N_MIN}.")
        else:
            for rank, (_, row) in enumerate(n20.head(5).iterrows(), 1):
                fr = str(row.get("fail_reason", ""))
                print(f"  #{rank}  {_fmt_row(row)}  <- MISS: {fr}")

    print(f"\n{'='*80}")


# ── Grid runner ───────────────────────────────────────────────────────────────

def run_grid(data: dict, ind: dict, common_idx: list) -> None:
    keys   = list(GRID.keys())
    combos = list(product(*[GRID[k] for k in keys]))
    total  = len(combos)
    done   = _load_done_keys()
    print(f"Grid search: {total} combinations ({total - len(done)} remaining)...\n")
    for i, vals in enumerate(combos, 1):
        params = dict(zip(keys, vals))
        if params["RSI_LOW"] >= params["RSI_HIGH"]:
            continue
        key = _combo_key(params)
        if key in done:
            continue
        r      = simulate(data, ind, params)
        trades = r["trades"]
        m      = _scalar_metrics(trades, r["eq_curve"], r["common_idx"])
        passed = _passed(trades, m)
        b1, b3 = _concentration(trades)
        ex3    = _ex_top3_pf(trades)
        stop_str = f"Stop{params['STOP_PCT']*100:.0f}% " if params["STOP_PCT"] > 0 else ""
        hold_str = f"Hold{params['MAX_HOLD']}d "          if params["MAX_HOLD"] > 0  else ""
        tag = "PASS" if passed else "----"
        print(
            f"  [{i:>4}/{total}] {tag}  "
            f"RSI[{params['RSI_LOW']},{params['RSI_HIGH']}] "
            f"Break>{params['BREAKOUT_DAYS']}d Exit<{params['EXIT_DAYS']}d "
            f"{stop_str}{hold_str}|  "
            f"Sh={m['sharpe']:.2f} PF={m['profit_factor']:.2f} "
            f"DD={m['max_dd']*100:.1f}% N={len(trades)} "
            f"b1={b1*100:.1f}% b3={b3*100:.1f}% ex3PF={ex3:.2f}",
            flush=True,
        )
        _append_csv(params, trades, m, passed)
        done.add(key)
    print()
    print_summary()


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
    return {"RSI_LOW": int(row["RSI_LOW"]), "RSI_HIGH": int(row["RSI_HIGH"]),
            "BREAKOUT_DAYS": int(row["BREAKOUT_DAYS"]), "EXIT_DAYS": int(row["EXIT_DAYS"]),
            "STOP_PCT": float(row["STOP_PCT"]), "MAX_HOLD": int(row["MAX_HOLD"])}


# ── Deep analysis ─────────────────────────────────────────────────────────────

def _print_period(label: str, pm: dict | None) -> None:
    if pm is None:
        print(f"  {label}: insufficient data")
        return
    pf_str = f"{pm['pf']:.2f}" if pm["pf"] != float("inf") else "inf"
    b1_flag = " [!]" if pm["best1"] > TF_BEST1_MAX else ""
    b3_flag = " [!]" if pm["best3"] > TF_BEST3_MAX else ""
    ex3_flag = "  ex3PF COLLAPSES" if pm["ex3_pf"] < 1.5 else (
               "  ex3PF WEAKENS" if pm["ex3_pf"] < TF_PF_MIN else "  ex3PF HOLDS")
    fails = []
    if pm["sharpe"] < TF_SHARPE_MIN: fails.append(f"Sh<{TF_SHARPE_MIN}")
    if pm["pf"]     < TF_PF_MIN:     fails.append(f"PF<{TF_PF_MIN}")
    if pm["max_dd"] >= TF_DD_MAX:    fails.append(f"DD>={TF_DD_MAX*100:.0f}%")
    if pm["n"]      < TF_N_MIN:      fails.append(f"N<{TF_N_MIN}")
    pass_str = "PASS" if not fails else f"FAIL ({', '.join(fails)})"
    print(f"  {label} ({pm['start']} -> {pm['end']}, {pm['n']} trades):  [{pass_str}]")
    print(f"    Sharpe={pm['sharpe']:.2f}  PF={pf_str}  DD={pm['max_dd']*100:.1f}%  "
          f"CAGR={pm['cagr']*100:.0f}%  WR={pm['wr']*100:.0f}%  "
          f"TotalPnL=${pm['total_pnl']:+,.0f}")
    print(f"    best1={pm['best1']*100:.1f}%{b1_flag}  "
          f"best3={pm['best3']*100:.1f}%{b3_flag}  "
          f"ex3PF={pm['ex3_pf']:.2f}{ex3_flag}")


def analyze_combo(data: dict, ind: dict, params: dict) -> None:
    r          = simulate(data, ind, params)
    trades     = r["trades"]
    eq_curve   = r["eq_curve"]
    common_idx = r["common_idx"]
    n          = len(trades)
    m          = _scalar_metrics(trades, eq_curve, common_idx)
    b1, b3     = _concentration(trades)
    ex3_pf     = _ex_top3_pf(trades)
    passed     = _passed(trades, m)

    stop_str = f"  Stop{params['STOP_PCT']*100:.0f}%" if params["STOP_PCT"] > 0 else ""
    hold_str = f"  Hold{params['MAX_HOLD']}d"          if params["MAX_HOLD"] > 0  else ""
    print("=" * 74)
    print(f"  DEEP ANALYSIS -- MOMENTUM v3"
          f" (BTC {REGIME_MA}d SMA gate + {CAP_PCT*100:.0f}% cap)")
    print(f"  RSI[{params['RSI_LOW']},{params['RSI_HIGH']}]  "
          f"Break>{params['BREAKOUT_DAYS']}d  Exit<{params['EXIT_DAYS']}d"
          f"{stop_str}{hold_str}")
    print(f"  Full-sample: Sh={m['sharpe']:.2f}  PF={m['profit_factor']:.2f}  "
          f"WR={m['win_rate']*100:.0f}%  DD={m['max_dd']*100:.1f}%  "
          f"CAGR={m['cagr']*100:.0f}%  N={n}")
    print("=" * 74)

    # ── 0. Cap confirmation ───────────────────────────────────────────────────
    print(f"\n  0. POSITION CAP VERIFICATION")
    print("  " + "-" * 66)
    print(f"  CAP_PCT = {CAP_PCT*100:.0f}%  (entry cost <= {CAP_PCT*100:.0f}% × total_equity)")
    print(f"  MAX_POSITIONS = {MAX_POSITIONS}  "
          f"(uncapped slot = {100/MAX_POSITIONS:.0f}% each)")
    costs = [t["entry_cost"] for t in trades if "entry_cost" in t]
    if costs:
        entry_costs_pct = []
        eq_by_date = {e["date"]: e["equity"] for e in eq_curve}
        for t in trades:
            eq = eq_by_date.get(t["entry_date"])
            if eq and eq > 0 and "entry_cost" in t:
                entry_costs_pct.append(t["entry_cost"] / eq * 100)
        if entry_costs_pct:
            print(f"  Entry cost as % of equity:  "
                  f"avg={sum(entry_costs_pct)/len(entry_costs_pct):.1f}%  "
                  f"max={max(entry_costs_pct):.1f}%  "
                  f"(cap limit={CAP_PCT*100:.0f}%)")

    # ── 1. IS / OOS split ─────────────────────────────────────────────────────
    print(f"\n  1. IN-SAMPLE vs OUT-OF-SAMPLE SPLIT  (cutoff: {OOS_START.date()})")
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
        is_ok  = pm_is["sharpe"]  >= TF_SHARPE_MIN and pm_is["pf"]  >= TF_PF_MIN
        oos_ok = pm_oos["sharpe"] >= TF_SHARPE_MIN and pm_oos["pf"] >= TF_PF_MIN
        if is_ok and oos_ok:
            print(f"\n  OOS verdict: HOLDS UP — both IS and OOS clear Sharpe+PF.")
        elif is_ok and not oos_ok:
            print(f"\n  OOS verdict: DEGRADES — IS passes but OOS fails.")
        elif not is_ok and oos_ok:
            print(f"\n  OOS verdict: IMPROVES OOS (unusual, inspect).")
        else:
            print(f"\n  OOS verdict: FAILS in both periods.")

    # ── 2. Year-by-year ───────────────────────────────────────────────────────
    print(f"\n  2. EQUITY CURVE BY YEAR")
    print("  " + "-" * 66)
    eq_by_date   = {e["date"]: e["equity"] for e in eq_curve}
    active_dates = [d for d in common_idx if d >= common_idx[WARMUP_BARS]]
    print(f"  {'Year':<6} {'StartEq':>10} {'EndEq':>10} {'Ret':>8} {'MaxDD':>7} {'N':>5}")
    print("  " + "-" * 58)
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

    # ── 3. Ex-top-3 stress test (key check for this version) ─────────────────
    print(f"\n  3. EX-TOP-3 STRESS TEST  (key question for v3)")
    print("  " + "-" * 66)
    sorted_t  = sorted(trades, key=lambda t: t["pnl_net"], reverse=True)
    top3      = sorted_t[:3]
    remaining = sorted_t[3:]
    total_profit = sum(t["pnl_net"] for t in trades if t["pnl_net"] > 0)
    print(f"  Full-sample total gross profit: ${total_profit:,.0f}")
    print("  Top 3 trades removed:")
    for i, t in enumerate(top3, 1):
        e_d  = t["entry_date"].date() if hasattr(t["entry_date"], "date") else t["entry_date"]
        x_d  = t["exit_date"].date()  if hasattr(t["exit_date"],  "date") else t["exit_date"]
        shr  = t["pnl_net"] / total_profit * 100.0 if total_profit > 0 and t["pnl_net"] > 0 else 0.0
        print(f"    #{i}  {t['symbol']:<10}  {e_d} -> {x_d}  "
              f"+{t['pnl_pct']:.1f}%  ${t['pnl_net']:+,.0f}  ({shr:.1f}% of profit)")
    if remaining:
        ex3_wins = [t for t in remaining if t["pnl_net"] > 0]
        ex3_pf_v = _profit_factor(remaining)
        ex3_wr   = len(ex3_wins) / len(remaining)
        ex3_total = sum(t["pnl_net"] for t in remaining)
        print(f"\n  Ex-top-3 ({len(remaining)} trades):")
        print(f"    PF = {ex3_pf_v:.2f}  WR={ex3_wr*100:.1f}%  Total=${ex3_total:+,.0f}")
        if ex3_pf_v >= TF_PF_MIN:
            verdict = "HOLDS UP -- PF >= 2.0 even without top 3 trades"
        elif ex3_pf_v >= 1.5:
            verdict = "WEAKENS but acceptable -- PF in 1.5-2.0 range, edge is real"
        else:
            verdict = "COLLAPSES -- PF < 1.5, strategy depends on outlier events"
        print(f"    Verdict: {verdict}")

    # ── 4. Concentration ──────────────────────────────────────────────────────
    print(f"\n  4. TOP 10 TRADES (outlier concentration)")
    print("  " + "-" * 76)
    top10 = sorted_t[:10]
    running = 0.0
    print(f"  Total gross profit: ${total_profit:,.0f}")
    print(f"  {'#':<3} {'Symbol':<10} {'Entry':>10} {'Exit':>10} {'Hold':>5} "
          f"{'PnL%':>7} {'PnL$':>10} {'Cum%':>7}")
    print("  " + "-" * 72)
    for i, t in enumerate(top10, 1):
        e_d = t["entry_date"].date() if hasattr(t["entry_date"], "date") else t["entry_date"]
        x_d = t["exit_date"].date()  if hasattr(t["exit_date"],  "date") else t["exit_date"]
        shr = t["pnl_net"] / total_profit * 100.0 if total_profit > 0 and t["pnl_net"] > 0 else 0.0
        running += shr
        print(f"  {i:<3} {t['symbol']:<10} {str(e_d):>10} {str(x_d):>10} "
              f"{t['days_held']:>5}d {t['pnl_pct']:>+6.1f}% "
              f"${t['pnl_net']:>+9,.0f} {running:>6.1f}%")
    print(f"\n  best1={b1*100:.1f}%  best3={b3*100:.1f}%  ex3PF={ex3_pf:.2f}  "
          f"(limits {TF_BEST1_MAX*100:.0f}% / {TF_BEST3_MAX*100:.0f}%)")

    # ── 5. Exit reason breakdown ──────────────────────────────────────────────
    print(f"\n  5. EXIT REASON BREAKDOWN")
    print("  " + "-" * 56)
    reasons: dict = {}
    for t in trades:
        key = t["reason"].split(" ")[0]
        reasons.setdefault(key, {"n": 0, "wins": 0, "pnl": 0.0})
        reasons[key]["n"]    += 1
        reasons[key]["wins"] += 1 if t["pnl_net"] > 0 else 0
        reasons[key]["pnl"]  += t["pnl_net"]
    for reason, stats in sorted(reasons.items()):
        wr = stats["wins"] / stats["n"] * 100
        print(f"  {reason:<18} N={stats['n']:>4}  WR={wr:.0f}%  P&L=${stats['pnl']:+,.0f}")

    fails = _fail_reasons(trades, m)
    print()
    print(f"  {'[PASS]' if passed else '[FAIL]'}  Sh={m['sharpe']:.2f}  "
          f"PF={m['profit_factor']:.2f}  DD={m['max_dd']*100:.1f}%  N={n}  "
          f"b1={b1*100:.1f}%  b3={b3*100:.1f}%  ex3PF={ex3_pf:.2f}")
    if fails:
        print(f"  Fails: {'; '.join(fails)}")
    print("=" * 74)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=f"Momentum v3: BTC {REGIME_MA}d SMA gate + {CAP_PCT*100:.0f}%% position cap")
    parser.add_argument("--grid",    action="store_true", help="729-combo grid")
    parser.add_argument("--summary", action="store_true", help="Ranked summary from CSV")
    parser.add_argument("--analyze", action="store_true",
                        help="IS/OOS + ex-top-3 + concentration on best combo")
    parser.add_argument("--reset",   action="store_true",
                        help="Delete v3 CSV (data cache kept)")
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
            print("No v3 grid CSV — using best v2 combo as default.")
            params = DEFAULT_PARAMS
        else:
            print(f"Best combo from v3 grid: {params}\n")
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
        ex3    = _ex_top3_pf(trades)
        print(f"Default params (12% cap):  Sh={m['sharpe']:.2f}  PF={m['profit_factor']:.2f}  "
              f"DD={m['max_dd']*100:.1f}%  N={n}  b1={b1*100:.1f}%  b3={b3*100:.1f}%  "
              f"ex3PF={ex3:.2f}")
        print(f"{'[PASS]' if passed else '[FAIL]'}  {_fail_reasons(trades, m)}")
        sys.exit(0 if passed else 1)
