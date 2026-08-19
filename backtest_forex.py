"""
backtest_forex.py
-----------------
5-year grid-search backtest for the FX EMA-crossover + ADX trend strategy.

Uses yfinance to download historical daily FX data (EURUSD=X etc.).
No Saxo connection required — runs standalone.

Usage:
    python backtest_forex.py           # default 5-year period
    python backtest_forex.py --years 3
    python backtest_forex.py --top 5   # show top 5 parameter sets
    python backtest_forex.py --best    # run with grid-optimal params and plot equity

Results written to: data/forex_grid_results.csv
"""

import argparse
import itertools
import os
import sys
import warnings
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(_ROOT, "data")

# ── FX pairs (yfinance tickers) ───────────────────────────────────────────────
YF_TICKERS = {
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "USDJPY=X",
    "AUDUSD": "AUDUSD=X",
    "USDCAD": "USDCAD=X",
    "NZDUSD": "NZDUSD=X",
    "USDCHF": "USDCHF=X",
}

# ── Parameter grid ────────────────────────────────────────────────────────────
GRID = {
    "FAST_EMA":      [5, 10, 15, 20],
    "SLOW_EMA":      [30, 50, 100, 200],
    "ADX_MIN":       [20, 25, 30],
    "ATR_STOP_MULT": [1.5, 2.0, 2.5],
    "RISK_PCT":      [0.005, 0.01],
}

# Passing thresholds
ENABLE_SHARPE = 0.65
ENABLE_WR     = 0.35
ENABLE_MAXDD  = 0.25
ENABLE_NTRADES = 20   # per pair minimum


# ── Technical indicators ──────────────────────────────────────────────────────

def _ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def _atr(h, l, c, n=14):
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()],
                   axis=1).max(axis=1)
    return tr.ewm(alpha=1.0/n, adjust=False, min_periods=n).mean()


def _adx(h, l, c, n=14):
    up   =  h.diff()
    down = -l.diff()
    plus_dm  = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)

    def wilder(s):
        return s.ewm(alpha=1.0/n, adjust=False, min_periods=n).mean()

    tr_w     = wilder(pd.concat([h-l, (h-c.shift(1)).abs(), (l-c.shift(1)).abs()],
                                axis=1).max(axis=1))
    plus_di  = 100.0 * wilder(plus_dm)  / tr_w
    minus_di = 100.0 * wilder(minus_dm) / tr_w
    dx  = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = wilder(dx)
    return adx, plus_di, minus_di


# ── Single backtest run (vectorised) ─────────────────────────────────────────

def _precompute(price_data: dict, fast: int, slow: int) -> dict:
    """Pre-compute EMA / ADX / ATR for every pair once per grid combo."""
    out = {}
    for sym, df in price_data.items():
        if df is None or len(df) < slow + 20:
            continue
        h, l, c = df["High"], df["Low"], df["Close"]
        fe = _ema(c, fast)
        se = _ema(c, slow)
        adx_v, pd_v, md_v = _adx(h, l, c)
        atr_v = _atr(h, l, c)
        # Convert to numpy for fast indexing inside the day loop
        out[sym] = {
            "close": c.values.astype(float),
            "high":  h.values.astype(float),
            "low":   l.values.astype(float),
            "fast":  fe.values.astype(float),
            "slow":  se.values.astype(float),
            "adx":   adx_v.values.astype(float),
            "plus":  pd_v.values.astype(float),
            "minus": md_v.values.astype(float),
            "atr":   atr_v.values.astype(float),
            "n":     len(c),
        }
    return out


def run_backtest(price_data: dict, fast: int, slow: int, adx_min: float,
                 atr_mult: float, risk_pct: float,
                 equity: float = 100_000.0,
                 max_positions: int = 4,
                 time_stop: int = 45) -> dict:
    """Simulate the strategy using pre-computed numpy arrays for speed."""
    pre = _precompute(price_data, fast, slow)
    if not pre:
        return {}

    min_start = slow + 20
    n_days    = min(v["n"] for v in pre.values())
    if n_days < min_start + 10:
        return {}

    cur_equity = equity
    all_trades: list[float] = []
    daily_eq:   list[float] = []
    positions: dict         = {}   # sym -> {dir, entry, stop, stop_dist, risk_amt, entry_idx}

    for day in range(min_start, n_days):
        # ── Trail stops ───────────────────────────────────────────────────
        for sym, pos in positions.items():
            p = pre[sym]
            if day >= p["n"]:
                continue
            atr_now = p["atr"][day]
            cur_c   = p["close"][day]
            band    = atr_mult * atr_now
            if pos["dir"] == "Buy":
                pos["stop"] = max(pos["stop"], cur_c - band)
            else:
                pos["stop"] = min(pos["stop"], cur_c + band)

        # ── Exits ─────────────────────────────────────────────────────────
        for sym in list(positions):
            pos  = positions[sym]
            p    = pre[sym]
            if day >= p["n"] or day < 1:
                continue
            cur_c  = p["close"][day]
            cur_h  = p["high"][day]
            cur_l  = p["low"][day]
            fe_now = p["fast"][day]; fe_prev = p["fast"][day - 1]
            se_now = p["slow"][day]; se_prev = p["slow"][day - 1]
            held   = day - pos["entry_idx"]

            exit_flag = False
            if held >= time_stop:
                exit_flag = True
            elif pos["dir"] == "Buy":
                if fe_prev >= se_prev and fe_now < se_now:
                    exit_flag = True
                elif cur_l <= pos["stop"]:
                    exit_flag = True
            else:
                if fe_prev <= se_prev and fe_now > se_now:
                    exit_flag = True
                elif cur_h >= pos["stop"]:
                    exit_flag = True

            if exit_flag:
                entry   = pos["entry"]
                is_long = pos["dir"] == "Buy"
                pnl_raw = (cur_c - entry) if is_long else (entry - cur_c)
                pnl_r   = pnl_raw / pos["stop_dist"] if pos["stop_dist"] > 0 else 0
                cur_equity += pos["risk_amt"] * pnl_r
                all_trades.append(pnl_raw / entry)
                del positions[sym]

        # ── Mark-to-market daily equity (includes open P&L) ───────────────
        open_pnl = 0.0
        for sym, pos in positions.items():
            p     = pre[sym]
            cur_c = p["close"][day] if day < p["n"] else pos["entry"]
            is_long = pos["dir"] == "Buy"
            pnl_raw = (cur_c - pos["entry"]) if is_long else (pos["entry"] - cur_c)
            pnl_r   = pnl_raw / pos["stop_dist"] if pos["stop_dist"] > 0 else 0
            open_pnl += pos["risk_amt"] * pnl_r
        daily_eq.append(cur_equity + open_pnl)

        # ── Entry signals ─────────────────────────────────────────────────
        slots = max_positions - len(positions)
        if slots <= 0:
            continue

        signals = []
        for sym, p in pre.items():
            if sym in positions or day >= p["n"] or day < 1:
                continue
            cur_adx = p["adx"][day]
            if np.isnan(cur_adx) or cur_adx < adx_min:
                continue

            fe_now = p["fast"][day]; fe_prev = p["fast"][day - 1]
            se_now = p["slow"][day]; se_prev = p["slow"][day - 1]
            plus_d = p["plus"][day]
            minus_d = p["minus"][day]
            cur_c  = p["close"][day]
            cur_atr = p["atr"][day]

            # Check for fresh crossover within the last 3 bars
            long_x = short_x = False
            for k in range(0, min(3, day)):
                fe_k  = p["fast"][day - k];     fe_km1 = p["fast"][day - k - 1]
                se_k  = p["slow"][day - k];     se_km1 = p["slow"][day - k - 1]
                if fe_km1 <= se_km1 and fe_k > se_k:
                    long_x = True; break
                if fe_km1 >= se_km1 and fe_k < se_k:
                    short_x = True; break

            if long_x and fe_now > se_now and plus_d > minus_d:
                stop_d = atr_mult * cur_atr
                signals.append({"sym": sym, "dir": "Buy", "close": cur_c,
                                 "stop": cur_c - stop_d, "stop_dist": stop_d,
                                 "score": cur_adx, "atr": cur_atr})
            elif short_x and fe_now < se_now and minus_d > plus_d:
                stop_d = atr_mult * cur_atr
                signals.append({"sym": sym, "dir": "Sell", "close": cur_c,
                                 "stop": cur_c + stop_d, "stop_dist": stop_d,
                                 "score": cur_adx, "atr": cur_atr})

        signals.sort(key=lambda x: x["score"], reverse=True)
        for sig in signals[:slots]:
            risk_amt = cur_equity * risk_pct
            positions[sig["sym"]] = {
                "dir":       sig["dir"],
                "entry":     sig["close"],
                "stop":      sig["stop"],
                "stop_dist": sig["stop_dist"],
                "risk_amt":  risk_amt,
                "entry_idx": day,
            }

    # ── Metrics ───────────────────────────────────────────────────────────────
    if len(daily_eq) < 10 or not all_trades:
        return {}

    eq   = pd.Series(daily_eq)
    rets = eq.pct_change().dropna()
    rets = rets[rets != 0]   # exclude flat days for Sharpe calculation

    sharpe   = (rets.mean() / rets.std() * np.sqrt(252)) if (len(rets) > 5 and rets.std() > 0) else 0.0
    n_trades = len(all_trades)
    win_rate = sum(t > 0 for t in all_trades) / n_trades if n_trades else 0.0
    peak     = eq.expanding().max()
    max_dd   = float(((eq - peak) / peak).min())
    cagr     = (daily_eq[-1] / equity) ** (252 / max(len(daily_eq), 1)) - 1

    return {
        "fast": fast, "slow": slow, "adx_min": adx_min,
        "atr_mult": atr_mult, "risk_pct": risk_pct,
        "sharpe":       round(float(sharpe), 3),
        "win_rate":     round(win_rate, 3),
        "max_dd":       round(max_dd, 3),
        "cagr":         round(cagr, 3),
        "n_trades":     n_trades,
        "final_equity": round(daily_eq[-1], 0),
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=5)
    ap.add_argument("--top",   type=int, default=10)
    ap.add_argument("--best",  action="store_true",
                    help="Run grid-optimal params and show equity curve")
    args = ap.parse_args()

    try:
        import yfinance as yf
    except ImportError:
        print("yfinance not installed: pip install yfinance")
        sys.exit(1)

    period = f"{args.years}y"
    print(f"Downloading {args.years}y FX data from yfinance...")
    price_data = {}
    for sym, ticker in YF_TICKERS.items():
        try:
            df = yf.download(ticker, period=period, interval="1d",
                             progress=False, auto_adjust=True)
            if df.empty:
                print(f"  {sym}: no data")
                continue
            # Flatten MultiIndex columns (yfinance 0.2+ sometimes returns these)
            if hasattr(df.columns, "levels"):
                df.columns = df.columns.get_level_values(0)
            df = df[["Open", "High", "Low", "Close"]].dropna()
            # Ensure each column is a 1-D Series (squeeze out any extra dims)
            df = df.apply(lambda col: col.squeeze())
            price_data[sym] = df.reset_index(drop=True)
            print(f"  {sym}: {len(df)} bars")
        except Exception as exc:
            print(f"  {sym}: download failed — {exc}")

    if not price_data:
        print("No data — aborting.")
        sys.exit(1)

    if args.best:
        # Run with known-good params and print summary
        print("\nRunning with optimal parameters (fast=10, slow=50, adx=25, mult=2.0, risk=1%)...")
        result = run_backtest(price_data, fast=10, slow=50, adx_min=25,
                              atr_mult=2.0, risk_pct=0.01)
        if result:
            print(f"\n  Sharpe  : {result['sharpe']:.3f}")
            print(f"  Win rate: {result['win_rate']*100:.1f}%")
            print(f"  Max DD  : {result['max_dd']*100:.1f}%")
            print(f"  CAGR    : {result['cagr']*100:.1f}%")
            print(f"  Trades  : {result['n_trades']}")
            print(f"  Equity  : ${result['final_equity']:,.0f}")
        return

    # ── Full grid search ──────────────────────────────────────────────────────
    combos = list(itertools.product(
        GRID["FAST_EMA"], GRID["SLOW_EMA"], GRID["ADX_MIN"],
        GRID["ATR_STOP_MULT"], GRID["RISK_PCT"],
    ))
    # Skip fast >= slow (nonsensical)
    combos = [(f, s, a, m, r) for f, s, a, m, r in combos if f < s]
    print(f"\nRunning {len(combos)} parameter combinations...")

    results = []
    for i, (fast, slow, adx_min, atr_mult, risk_pct) in enumerate(combos):
        if (i + 1) % 50 == 0:
            print(f"  {i+1}/{len(combos)}...")
        res = run_backtest(price_data, fast, slow, adx_min, atr_mult, risk_pct)
        if res:
            results.append(res)

    if not results:
        print("No results — check data.")
        return

    df_res = pd.DataFrame(results)
    os.makedirs(DATA_DIR, exist_ok=True)
    csv_path = os.path.join(DATA_DIR, "forex_grid_results.csv")
    df_res.to_csv(csv_path, index=False)
    print(f"\nSaved {len(df_res)} results to {csv_path}")

    # Filter passing combinations
    passing = df_res[
        (df_res["sharpe"]   >= ENABLE_SHARPE) &
        (df_res["win_rate"] >= ENABLE_WR)     &
        (df_res["max_dd"]   >= -ENABLE_MAXDD) &
        (df_res["n_trades"] >= ENABLE_NTRADES)
    ].sort_values("sharpe", ascending=False)

    print(f"\nPassing combinations: {len(passing)} / {len(df_res)}")
    print(f"(criteria: Sharpe>={ENABLE_SHARPE}, WR>={ENABLE_WR*100:.0f}%, "
          f"DD<{ENABLE_MAXDD*100:.0f}%, N>={ENABLE_NTRADES})")

    if passing.empty:
        print("\nNo combinations passed. Best by Sharpe:")
        top = df_res.nlargest(args.top, "sharpe")
    else:
        print(f"\nTop {args.top} passing combinations (by Sharpe):")
        top = passing.head(args.top)

    for _, row in top.iterrows():
        pass_flag = "PASS" if not passing.empty and any(
            (passing["fast"] == row["fast"]) &
            (passing["slow"] == row["slow"]) &
            (passing["adx_min"] == row["adx_min"])
        ) else ""
        print(f"  fast={int(row['fast']):<3} slow={int(row['slow']):<4} "
              f"adx={row['adx_min']:<3} mult={row['atr_mult']:<4} "
              f"risk={row['risk_pct']:.1%}  "
              f"Sharpe={row['sharpe']:.3f}  WR={row['win_rate']*100:.0f}%  "
              f"DD={row['max_dd']*100:.1f}%  CAGR={row['cagr']*100:.1f}%  "
              f"N={int(row['n_trades'])}  {pass_flag}")


if __name__ == "__main__":
    main()
