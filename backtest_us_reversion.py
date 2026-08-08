"""
backtest_us_reversion.py
-------------------------
Walk-forward backtest for the US Mean Reversion strategy.

Run (single pass with current parameters):
    python backtest_us_reversion.py

Run (full parameter grid search -- resumes from data/grid_results.csv if stopped):
    python backtest_us_reversion.py --grid

Run (print ranked summary from previous grid results):
    python backtest_us_reversion.py --summary

ENABLE criterion: Sharpe >= 0.8 AND Win Rate >= 50% AND Max Drawdown < 20% AND N >= 15.
Only set US_REVERSION_ENABLED = True in atos_runner.py after all criteria pass.
"""
import sys
import os
import argparse
import pickle
import csv
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import date, timedelta
from itertools import product

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
DATA_DIR  = os.path.join(BASE_DIR, "data")
CACHE_PKL = os.path.join(DATA_DIR, "backtest_reversion_cache.pkl")
CSV_PATH  = os.path.join(DATA_DIR, "grid_results.csv")

sys.path.insert(0, BASE_DIR)
import atos.us_reversion as USR
from atos.universe import US_TICKERS

BACKTEST_YEARS = 3
COMMISSION_PCT = 0.0008
ALL_TICKERS    = US_TICKERS + ["SPY"]

GRID = {
    "RSI_ENTRY":     [28, 30, 33],
    "DIP_PCT":       [0.04, 0.05, 0.06],
    "VOL_MULT":      [1.5, 1.8, 2.0],
    "STOP_PCT":      [0.04, 0.05, 0.06],
    "MAX_POSITIONS": [2, 3],
    "SLEEVE_DD_CAP": [0.10, 0.15, 0.20],
}

CSV_FIELDS = ["RSI_ENTRY", "DIP_PCT", "VOL_MULT", "STOP_PCT", "MAX_POSITIONS",
              "SLEEVE_DD_CAP", "sharpe", "win_rate", "max_dd", "cagr", "n_trades", "passed"]


# ── Data layer ────────────────────────────────────────────────────────────────

def _download_raw() -> tuple[pd.DataFrame, pd.DataFrame]:
    start = (date.today() - timedelta(days=BACKTEST_YEARS * 365 + 60)).isoformat()
    print(f"Downloading {len(ALL_TICKERS)} tickers from {start}...")
    raw   = yf.download(ALL_TICKERS, start=start, auto_adjust=True, progress=False)
    close = raw["Close"].ffill()
    vol   = raw["Volume"].ffill()
    return close, vol


def load_data() -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Return (close_df, vol_df, indicators). Uses disk cache if fresh (same day)."""
    if os.path.exists(CACHE_PKL):
        with open(CACHE_PKL, "rb") as f:
            cached = pickle.load(f)
        if cached.get("date") == date.today().isoformat():
            print("Using cached data from today.")
            return cached["close"], cached["vol"], cached["ind"]

    close, vol = _download_raw()
    ind = _precompute(close, vol)

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CACHE_PKL, "wb") as f:
        pickle.dump({"date": date.today().isoformat(),
                     "close": close, "vol": vol, "ind": ind}, f)
    return close, vol, ind


def _rsi_series(c: pd.Series) -> pd.Series:
    delta = c.diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _precompute(close: pd.DataFrame, vol: pd.DataFrame) -> dict:
    """Pre-compute RSI, SMA20, EMA200, vol_ratio for every ticker x date.
    These are parameter-independent -- computed once, reused across all 486 combos."""
    tickers = [t for t in US_TICKERS if t in close.columns]
    print(f"Pre-computing indicators for {len(tickers)} tickers...", flush=True)

    rsi_df   = pd.DataFrame(index=close.index, columns=tickers, dtype=float)
    sma20_df = pd.DataFrame(index=close.index, columns=tickers, dtype=float)
    ema200_df= pd.DataFrame(index=close.index, columns=tickers, dtype=float)
    vrat_df  = pd.DataFrame(index=close.index, columns=tickers, dtype=float)

    for tk in tickers:
        c = close[tk].dropna()
        v = vol[tk].dropna() if tk in vol.columns else pd.Series(dtype=float)
        rsi_df[tk]    = _rsi_series(c).reindex(close.index)
        sma20_df[tk]  = c.rolling(20).mean().reindex(close.index)
        ema200_df[tk] = c.ewm(span=200, adjust=False).mean().reindex(close.index)
        if len(v) >= 20:
            v20avg = v.rolling(20).mean()
            vrat   = (v / v20avg).reindex(close.index)
        else:
            vrat = pd.Series(np.nan, index=close.index)
        vrat_df[tk] = vrat

    print("  Done.\n")
    return {"rsi": rsi_df, "sma20": sma20_df, "ema200": ema200_df, "vrat": vrat_df}


# ── Simulation ─────────────────────────────────────────────────────────────────

def simulate(close_df: pd.DataFrame, ind: dict, params: dict) -> dict:
    """Walk-forward simulation using pre-computed indicators.
    Cash accounting: cost deducted on entry, proceeds credited on exit."""
    rsi_entry     = params["RSI_ENTRY"]
    dip_pct       = params["DIP_PCT"]
    vol_mult      = params["VOL_MULT"]
    stop_pct      = params["STOP_PCT"]
    max_positions = params["MAX_POSITIONS"]
    sleeve_dd_cap = params["SLEEVE_DD_CAP"]
    rsi_exit      = USR.RSI_EXIT
    max_hold      = USR.MAX_HOLD_DAYS

    tickers   = [t for t in US_TICKERS if t in close_df.columns]
    dates     = close_df.index.tolist()
    start_idx = 220

    rsi_df    = ind["rsi"]
    sma20_df  = ind["sma20"]
    ema200_df = ind["ema200"]
    vrat_df   = ind["vrat"]

    cash     = float(USR.REVERSION_SLEEVE_SEK)
    peak_eq  = cash
    open_pos = {}   # {ticker: {entry_price, entry_idx, entry_date, shares, cost}}
    trades   = []
    eq_curve = []

    for idx in range(start_idx, len(dates)):
        today = dates[idx]

        def _cur(tk):
            v = close_df[tk].iat[idx] if tk in close_df.columns else np.nan
            return float(v) if not pd.isna(v) else None

        # Mark-to-market
        open_val = sum(
            pos["shares"] * (_cur(tk) or pos["entry_price"])
            for tk, pos in open_pos.items()
        )
        total_eq = cash + open_val
        peak_eq  = max(peak_eq, total_eq)
        sleeve_dd = (peak_eq - total_eq) / peak_eq if peak_eq > 0 else 0.0

        # Exit check
        to_close = []
        for tk, pos in list(open_pos.items()):
            cur = _cur(tk)
            if cur is None:
                continue
            cur_rsi = float(rsi_df[tk].iat[idx])   if tk in rsi_df.columns   else np.nan
            sma20   = float(sma20_df[tk].iat[idx])  if tk in sma20_df.columns else np.nan
            days_held = idx - pos["entry_idx"]

            exit_now, reason = False, ""
            if days_held >= max_hold:
                exit_now, reason = True, f"time-stop {days_held}d"
            elif cur <= pos["entry_price"] * (1 - stop_pct):
                pct = (pos["entry_price"] - cur) / pos["entry_price"] * 100
                exit_now, reason = True, f"stop-loss -{pct:.1f}%"
            elif not pd.isna(cur_rsi) and cur_rsi > rsi_exit:
                exit_now, reason = True, f"RSI {cur_rsi:.0f}>{rsi_exit}"
            elif not pd.isna(sma20) and cur >= sma20:
                g = (cur - pos["entry_price"]) / pos["entry_price"] * 100
                exit_now, reason = True, f"SMA20 +{g:.1f}%"

            if exit_now:
                to_close.append((tk, cur, reason, pos))

        for tk, exit_price, reason, pos in to_close:
            proceeds = pos["shares"] * exit_price * (1 - COMMISSION_PCT)
            pnl_net  = proceeds - pos["cost"]
            cash    += proceeds
            trades.append({
                "ticker": tk, "entry_date": pos["entry_date"], "exit_date": today,
                "entry_price": pos["entry_price"], "exit_price": exit_price,
                "shares": pos["shares"], "pnl_net": pnl_net,
                "reason": reason, "days_held": idx - pos["entry_idx"],
            })
            del open_pos[tk]

        # Entry scan
        slots_free = max_positions - len(open_pos)
        if slots_free > 0 and sleeve_dd < sleeve_dd_cap:
            current_total = cash + sum(
                p["shares"] * (_cur(t) or p["entry_price"])
                for t, p in open_pos.items()
            )
            for tk in tickers:
                if slots_free <= 0:
                    break
                if tk in open_pos:
                    continue

                price  = _cur(tk)
                if price is None:
                    continue
                ema200 = float(ema200_df[tk].iat[idx]) if tk in ema200_df.columns else np.nan
                sma20  = float(sma20_df[tk].iat[idx])  if tk in sma20_df.columns  else np.nan
                rsi_v  = float(rsi_df[tk].iat[idx])    if tk in rsi_df.columns    else np.nan
                vrat   = float(vrat_df[tk].iat[idx])   if tk in vrat_df.columns   else np.nan

                if any(pd.isna(x) for x in [ema200, sma20, rsi_v, vrat]):
                    continue
                dip = (sma20 - price) / sma20

                if not (price > ema200 and rsi_v < rsi_entry
                        and dip >= dip_pct and vrat >= vol_mult):
                    continue

                slot   = current_total / max_positions
                shares = int(slot / price)
                if shares < 1 or slot > cash:
                    continue

                cost  = shares * price * (1 + COMMISSION_PCT)
                cash -= cost
                open_pos[tk] = {
                    "entry_price": price, "entry_idx": idx,
                    "entry_date": today, "shares": shares, "cost": cost,
                }
                slots_free -= 1

        open_val = sum(
            pos["shares"] * (_cur(tk) or pos["entry_price"])
            for tk, pos in open_pos.items()
        )
        eq_curve.append({"date": today, "equity": cash + open_val})

    # Close remaining at last price
    last_idx = len(dates) - 1
    for tk, pos in open_pos.items():
        lp = _cur(tk) if _cur(tk) else pos["entry_price"]
        proceeds = pos["shares"] * lp * (1 - COMMISSION_PCT)
        pnl_net  = proceeds - pos["cost"]
        cash += proceeds
        trades.append({
            "ticker": tk, "entry_date": pos["entry_date"],
            "exit_date": dates[last_idx], "entry_price": pos["entry_price"],
            "exit_price": lp, "shares": pos["shares"], "pnl_net": pnl_net,
            "reason": "end-of-backtest", "days_held": last_idx - pos["entry_idx"],
        })

    if not eq_curve or not trades:
        return {"trades": [], "sharpe": 0.0, "win_rate": 0.0,
                "max_dd": 1.0, "cagr": 0.0, "eq_curve": []}

    eq_df     = pd.DataFrame(eq_curve).set_index("date")
    daily_ret = eq_df["equity"].pct_change().dropna()
    sharpe    = float(daily_ret.mean() / daily_ret.std() * np.sqrt(252)
                      if daily_ret.std() > 0 else 0.0)
    peak      = eq_df["equity"].cummax()
    max_dd    = float(((peak - eq_df["equity"]) / peak).max())
    years     = (dates[-1] - dates[start_idx]).days / 365
    cagr      = float((eq_df["equity"].iloc[-1] / USR.REVERSION_SLEEVE_SEK) ** (1 / years) - 1
                      if years > 0 else 0.0)
    df        = pd.DataFrame(trades)
    win_rate  = float(len(df[df["pnl_net"] > 0]) / len(df)) if len(df) > 0 else 0.0

    return {"trades": trades, "sharpe": sharpe, "win_rate": win_rate,
            "max_dd": max_dd, "cagr": cagr, "eq_curve": eq_curve}


# ── Reporting ─────────────────────────────────────────────────────────────────

def print_result(params, r, close_df, dates, verbose=True) -> bool:
    df   = pd.DataFrame(r["trades"]) if r["trades"] else pd.DataFrame()
    wins = df[df["pnl_net"] > 0] if len(df) else pd.DataFrame()
    loss = df[df["pnl_net"] <= 0] if len(df) else pd.DataFrame()
    start_idx = 220
    years = (dates[-1] - dates[start_idx]).days / 365

    spy_start = float(close_df["SPY"].iloc[start_idx]) if "SPY" in close_df.columns else None
    spy_end   = float(close_df["SPY"].iloc[-1])        if "SPY" in close_df.columns else None
    spy_cagr  = ((spy_end / spy_start) ** (1 / years) - 1) if spy_start and years > 0 else None

    if verbose:
        print("=" * 62)
        print("  US MEAN REVERSION -- BACKTEST RESULTS")
        print("=" * 62)
        print(f"  Params:  RSI<{params['RSI_ENTRY']}  Dip>{params['DIP_PCT']*100:.0f}%  "
              f"Vol>{params['VOL_MULT']}x  Stop{params['STOP_PCT']*100:.0f}%  "
              f"MaxPos{params['MAX_POSITIONS']}  DDcap{params['SLEEVE_DD_CAP']*100:.0f}%")
        print(f"  Period:  {dates[start_idx].date()} -> {dates[-1].date()} ({years:.1f}y)")
        if len(df):
            print(f"  Trades:  {len(df)} ({len(wins)} wins / {len(loss)} losses)")
            print(f"  Win rate:      {r['win_rate']*100:.1f}%")
            print(f"  Avg hold:      {df['days_held'].mean():.1f}d")
            if len(wins): print(f"  Avg win:       +{wins['pnl_net'].mean():,.0f} SEK")
            if len(loss): print(f"  Avg loss:      {loss['pnl_net'].mean():,.0f} SEK")
            print(f"  Total P&L:     {df['pnl_net'].sum():+,.0f} SEK")
        print(f"  CAGR:          {r['cagr']*100:.1f}%" +
              (f"  (SPY: {spy_cagr*100:.1f}%)" if spy_cagr else ""))
        print(f"  Sharpe:        {r['sharpe']:.2f}")
        dd_flag = "" if r["max_dd"] < 0.20 else "  <- ABOVE 20% target"
        print(f"  Max drawdown:  {r['max_dd']*100:.1f}%{dd_flag}")

        if len(df) >= 5:
            top = df.nlargest(5, "pnl_net")
            bot = df.nsmallest(5, "pnl_net")
            print("  Best 5:")
            for _, row in top.iterrows():
                print(f"    {row['ticker']:<6} {str(row['entry_date'])[:10]}  "
                      f"{row['days_held']}d  {row['pnl_net']:+,.0f} SEK  [{row['reason']}]")
            print("  Worst 5:")
            for _, row in bot.iterrows():
                print(f"    {row['ticker']:<6} {str(row['entry_date'])[:10]}  "
                      f"{row['days_held']}d  {row['pnl_net']:+,.0f} SEK  [{row['reason']}]")

    passed = (r["sharpe"] >= 0.8 and r["win_rate"] >= 0.50
              and r["max_dd"] < 0.20 and len(df) >= 15)
    if verbose:
        print()
        print(f"  VERDICT: {'ENABLE' if passed else 'DO NOT ENABLE'}")
        print(f"  Sharpe>=0.8 {'OK' if r['sharpe']>=0.8 else 'X'}  "
              f"WinRate>=50% {'OK' if r['win_rate']>=0.5 else 'X'}  "
              f"MaxDD<20% {'OK' if r['max_dd']<0.20 else 'X'}  "
              f"Trades>=15 {'OK' if len(df)>=15 else 'X'}")
        print("=" * 62)
    return passed


def _combo_key(params: dict) -> str:
    return (f"{params['RSI_ENTRY']},{params['DIP_PCT']},{params['VOL_MULT']},"
            f"{params['STOP_PCT']},{params['MAX_POSITIONS']},{params['SLEEVE_DD_CAP']}")


def _load_done_keys() -> set:
    if not os.path.exists(CSV_PATH):
        return set()
    done = set()
    with open(CSV_PATH, newline="") as f:
        for row in csv.DictReader(f):
            key = (f"{row['RSI_ENTRY']},{row['DIP_PCT']},{row['VOL_MULT']},"
                   f"{row['STOP_PCT']},{row['MAX_POSITIONS']},{row['SLEEVE_DD_CAP']}")
            done.add(key)
    return done


def _append_csv(params: dict, r: dict, n_trades: int, passed: bool):
    write_header = not os.path.exists(CSV_PATH)
    with open(CSV_PATH, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if write_header:
            w.writeheader()
        w.writerow({
            "RSI_ENTRY":     params["RSI_ENTRY"],
            "DIP_PCT":       params["DIP_PCT"],
            "VOL_MULT":      params["VOL_MULT"],
            "STOP_PCT":      params["STOP_PCT"],
            "MAX_POSITIONS": params["MAX_POSITIONS"],
            "SLEEVE_DD_CAP": params["SLEEVE_DD_CAP"],
            "sharpe":        round(r["sharpe"], 3),
            "win_rate":      round(r["win_rate"], 3),
            "max_dd":        round(r["max_dd"], 3),
            "cagr":          round(r["cagr"], 3),
            "n_trades":      n_trades,
            "passed":        int(passed),
        })


# ── Grid runner ───────────────────────────────────────────────────────────────

def run_grid(close_df, ind, dates):
    keys   = list(GRID.keys())
    combos = list(product(*GRID.values()))
    total  = len(combos)

    done_keys = _load_done_keys()
    remaining = [(i, dict(zip(keys, c))) for i, c in enumerate(combos, 1)
                 if _combo_key(dict(zip(keys, c))) not in done_keys]

    if done_keys:
        print(f"Resuming: {len(done_keys)} combos already done, "
              f"{len(remaining)} remaining of {total}.\n")
    else:
        print(f"Grid search: {total} combinations...\n")

    for i, params in remaining:
        r       = simulate(close_df, ind, params)
        df      = pd.DataFrame(r["trades"]) if r["trades"] else pd.DataFrame()
        n       = len(df)
        passed  = r["sharpe"] >= 0.8 and r["win_rate"] >= 0.50 and r["max_dd"] < 0.20 and n >= 15
        tag     = "PASS" if passed else "----"
        print(f"  [{i:>3}/{total}] {tag}  "
              f"RSI<{params['RSI_ENTRY']} Dip>{params['DIP_PCT']*100:.0f}% "
              f"Vol>{params['VOL_MULT']}x Stop{params['STOP_PCT']*100:.0f}% "
              f"Pos{params['MAX_POSITIONS']} DD{params['SLEEVE_DD_CAP']*100:.0f}%  |  "
              f"Sharpe={r['sharpe']:.2f} WR={r['win_rate']*100:.0f}% "
              f"DD={r['max_dd']*100:.1f}% CAGR={r['cagr']*100:.0f}% N={n}",
              flush=True)
        _append_csv(params, r, n, passed)

    print_summary()


def print_summary():
    if not os.path.exists(CSV_PATH):
        print("No results file found. Run --grid first.")
        return

    df = pd.read_csv(CSV_PATH)
    total   = len(df)
    passing = df[df["passed"] == 1].copy()

    print(f"\n{'='*62}")
    print(f"  GRID SUMMARY -- {total} combos evaluated, {len(passing)} passed all criteria")
    print(f"{'='*62}")

    if passing.empty:
        print("  No combination passed.")
        misses = df.copy()
        misses["score"] = (misses["sharpe"] * (1 - misses["max_dd"])
                           * misses["win_rate"] * misses["n_trades"].clip(upper=15) / 15)
        misses = misses.nlargest(5, "score")
        print("\n  Top 5 closest misses:")
        for _, r in misses.iterrows():
            print(f"    RSI<{r['RSI_ENTRY']} Dip>{r['DIP_PCT']*100:.0f}% "
                  f"Vol>{r['VOL_MULT']}x Stop{r['STOP_PCT']*100:.0f}% "
                  f"Pos{int(r['MAX_POSITIONS'])} DD{r['SLEEVE_DD_CAP']*100:.0f}%  "
                  f"Sharpe={r['sharpe']:.2f} WR={r['win_rate']*100:.0f}% "
                  f"DD={r['max_dd']*100:.1f}% N={int(r['n_trades'])}")
        return

    passing = passing.sort_values("sharpe", ascending=False)
    print(f"\n  {'#':<4} {'RSI':>4} {'Dip':>5} {'Vol':>5} {'Stop':>5} "
          f"{'Pos':>4} {'DDcap':>6}  {'Sharpe':>7} {'WR':>6} {'DD':>6} "
          f"{'CAGR':>6} {'N':>4}")
    print("  " + "-" * 70)
    for rank, (_, r) in enumerate(passing.iterrows(), 1):
        print(f"  {rank:<4} RSI<{r['RSI_ENTRY']} "
              f"Dip>{r['DIP_PCT']*100:.0f}% "
              f"Vol>{r['VOL_MULT']}x "
              f"Stop{r['STOP_PCT']*100:.0f}% "
              f"Pos{int(r['MAX_POSITIONS'])} "
              f"DD{r['SLEEVE_DD_CAP']*100:.0f}%  "
              f"{r['sharpe']:>7.2f} "
              f"{r['win_rate']*100:>5.0f}% "
              f"{r['max_dd']*100:>5.1f}% "
              f"{r['cagr']*100:>5.0f}% "
              f"{int(r['n_trades']):>4}")

    best = passing.iloc[0]
    print(f"\n  BEST (by Sharpe): RSI<{best['RSI_ENTRY']} "
          f"Dip>{best['DIP_PCT']*100:.0f}% Vol>{best['VOL_MULT']}x "
          f"Stop{best['STOP_PCT']*100:.0f}% Pos{int(best['MAX_POSITIONS'])} "
          f"DDcap{best['SLEEVE_DD_CAP']*100:.0f}%")
    print(f"    Sharpe={best['sharpe']:.2f}  WR={best['win_rate']*100:.0f}%  "
          f"MaxDD={best['max_dd']*100:.1f}%  CAGR={best['cagr']*100:.0f}%  "
          f"N={int(best['n_trades'])}")

    # Consensus: which param values appear most in top-10 passing?
    top10 = passing.head(10)
    print(f"\n  CONSENSUS (top-{min(10, len(passing))} passing combos):")
    for col, label in [("RSI_ENTRY","RSI"), ("DIP_PCT","Dip%"), ("VOL_MULT","Volx"),
                       ("STOP_PCT","Stop%"), ("MAX_POSITIONS","MaxPos"),
                       ("SLEEVE_DD_CAP","DDcap")]:
        mode = top10[col].mode().iloc[0]
        count = (top10[col] == mode).sum()
        print(f"    {label:<10} most common = {mode}  ({count}/{len(top10)} top combos)")
    print(f"{'='*62}")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--grid",    action="store_true", help="Run full parameter grid search")
    parser.add_argument("--summary", action="store_true", help="Print ranked summary from CSV")
    parser.add_argument("--reset",   action="store_true", help="Delete cached data + results CSV")
    args = parser.parse_args()

    if args.reset:
        for p in [CACHE_PKL, CSV_PATH]:
            if os.path.exists(p):
                os.remove(p)
                print(f"Deleted {p}")
        sys.exit(0)

    if args.summary:
        print_summary()
        sys.exit(0)

    close_df, vol_df, ind = load_data()
    dates = close_df.index.tolist()
    n_tickers = len([t for t in US_TICKERS if t in close_df.columns])
    print(f"  {n_tickers} US tickers available.\n")

    if args.grid:
        run_grid(close_df, ind, dates)
    else:
        params = {
            "RSI_ENTRY":     USR.RSI_ENTRY,
            "DIP_PCT":       USR.DIP_PCT,
            "VOL_MULT":      USR.VOL_MULT,
            "STOP_PCT":      USR.STOP_PCT,
            "MAX_POSITIONS": USR.MAX_POSITIONS,
            "SLEEVE_DD_CAP": USR.SLEEVE_DD_CAP,
        }
        r      = simulate(close_df, ind, params)
        passed = print_result(params, r, close_df, dates, verbose=True)
        if passed:
            print("\n  All criteria met. Set US_REVERSION_ENABLED = True in atos_runner.py")
        else:
            print("\n  Run:  python backtest_us_reversion.py --grid")

