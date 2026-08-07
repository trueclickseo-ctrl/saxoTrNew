"""
backtest_us_reversion.py
-------------------------
Walk-forward backtest for the US Mean Reversion strategy.

Run (single pass with current parameters):
    python backtest_us_reversion.py

Run (parameter grid search — finds combos with MaxDD < 20%):
    python backtest_us_reversion.py --grid

ENABLE criterion: Sharpe >= 0.8 AND Win Rate >= 50% AND Max Drawdown < 20%.
Only set US_REVERSION_ENABLED = True in atos_runner.py after all three pass.
"""
import sys
import os
import argparse
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import date, timedelta
from itertools import product

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import atos.us_reversion as USR
from atos.universe import US_TICKERS

BACKTEST_YEARS = 3
COMMISSION_PCT = 0.0008   # 0.08% per side (Saxo SIM rate)
ALL_TICKERS    = US_TICKERS + ["SPY"]

# ── Parameter grid (only used with --grid) ────────────────────────────────
GRID = {
    "RSI_ENTRY":     [28, 30, 33],
    "DIP_PCT":       [0.04, 0.05, 0.06],
    "VOL_MULT":      [1.5, 1.8, 2.0],
    "STOP_PCT":      [0.04, 0.05, 0.06],
    "MAX_POSITIONS": [2, 3],
    "SLEEVE_DD_CAP": [0.10, 0.15, 0.20],
}
# ─────────────────────────────────────────────────────────────────────────


def download_data():
    start = (date.today() - timedelta(days=BACKTEST_YEARS * 365 + 60)).isoformat()
    print(f"Downloading {len(ALL_TICKERS)} tickers from {start}…")
    raw = yf.download(ALL_TICKERS, start=start, auto_adjust=True, progress=False)
    close = raw["Close"].ffill() if isinstance(raw.columns, pd.MultiIndex) else raw[["Close"]].ffill()
    vol   = raw["Volume"].ffill() if isinstance(raw.columns, pd.MultiIndex) else raw[["Volume"]].ffill()
    return close, vol


def _rsi_series(c: pd.Series) -> pd.Series:
    delta = c.diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def simulate(close_df, vol_df, params: dict) -> dict:
    """Walk-forward simulation. Correct cash accounting: cost deducted on entry,
    proceeds credited on exit. Total = cash + mark-to-market of open positions."""
    rsi_entry     = params["RSI_ENTRY"]
    dip_pct       = params["DIP_PCT"]
    vol_mult      = params["VOL_MULT"]
    stop_pct      = params["STOP_PCT"]
    max_positions = params["MAX_POSITIONS"]
    sleeve_dd_cap = params["SLEEVE_DD_CAP"]
    rsi_exit      = USR.RSI_EXIT     # 60
    max_hold      = USR.MAX_HOLD_DAYS  # 10

    tickers   = [t for t in US_TICKERS if t in close_df.columns]
    dates     = close_df.index.tolist()
    start_idx = 220

    cash      = float(USR.REVERSION_SLEEVE_SEK)   # liquid cash in sleeve
    peak_eq   = cash
    open_pos  = {}          # {ticker: {entry_price, entry_idx, entry_date, shares, cost}}
    trades    = []
    eq_curve  = []

    for idx in range(start_idx, len(dates)):
        today = dates[idx]

        # ── Mark-to-market open positions ─────────────────────────
        def _cur(tk):
            v = close_df[tk].iloc[idx] if tk in close_df.columns else np.nan
            return float(v) if not pd.isna(v) else None

        open_val = sum(
            pos["shares"] * (_cur(tk) or pos["entry_price"])
            for tk, pos in open_pos.items()
        )
        total_eq = cash + open_val
        peak_eq  = max(peak_eq, total_eq)
        sleeve_dd = (peak_eq - total_eq) / peak_eq if peak_eq > 0 else 0.0

        # ── Exit check ────────────────────────────────────────────
        to_close = []
        for tk, pos in list(open_pos.items()):
            cur = _cur(tk)
            if cur is None:
                continue
            c_ser = close_df[tk].iloc[:idx + 1].dropna()
            cur_rsi = float(_rsi_series(c_ser).iloc[-1]) if len(c_ser) >= 15 else None
            sma20   = float(c_ser.rolling(20).mean().iloc[-1]) if len(c_ser) >= 20 else None
            days_held = idx - pos["entry_idx"]

            exit_now, reason = False, ""
            if days_held >= max_hold:
                exit_now, reason = True, f"time-stop {days_held}d"
            elif cur <= pos["entry_price"] * (1 - stop_pct):
                pct = (pos["entry_price"] - cur) / pos["entry_price"] * 100
                exit_now, reason = True, f"stop-loss -{pct:.1f}%"
            elif cur_rsi is not None and cur_rsi > rsi_exit:
                exit_now, reason = True, f"RSI {cur_rsi:.0f}>{rsi_exit}"
            elif sma20 is not None and cur >= sma20:
                g = (cur - pos["entry_price"]) / pos["entry_price"] * 100
                exit_now, reason = True, f"SMA20 +{g:.1f}%"

            if exit_now:
                to_close.append((tk, cur, reason, pos))

        for tk, exit_price, reason, pos in to_close:
            sh       = pos["shares"]
            proceeds = sh * exit_price * (1 - COMMISSION_PCT)
            pnl_net  = proceeds - pos["cost"]
            cash    += proceeds               # credit proceeds back to cash
            trades.append({
                "ticker":      tk,
                "entry_date":  pos["entry_date"],
                "exit_date":   today,
                "entry_price": pos["entry_price"],
                "exit_price":  exit_price,
                "shares":      sh,
                "pnl_net":     pnl_net,
                "reason":      reason,
                "days_held":   idx - pos["entry_idx"],
            })
            del open_pos[tk]

        # ── Entry scan ────────────────────────────────────────────
        # Pause new entries if sleeve drawdown exceeds circuit-breaker
        slots_free = max_positions - len(open_pos)
        if slots_free > 0 and sleeve_dd < sleeve_dd_cap:
            for tk in tickers:
                if slots_free <= 0:
                    break
                if tk in open_pos:
                    continue
                c_ser = close_df[tk].iloc[:idx + 1].dropna()
                v_ser = vol_df[tk].iloc[:idx + 1].dropna() if tk in vol_df.columns else pd.Series(dtype=float)
                if len(c_ser) < 220:
                    continue
                price   = float(c_ser.iloc[-1])
                ema200  = float(c_ser.ewm(span=200, adjust=False).mean().iloc[-1])
                sma20   = float(c_ser.rolling(20).mean().iloc[-1])
                rsi_val = float(_rsi_series(c_ser).iloc[-1])
                v20avg  = float(v_ser.rolling(20).mean().iloc[-1]) if len(v_ser) >= 20 else 0.0
                v_today = float(v_ser.iloc[-1]) if len(v_ser) > 0 else 0.0

                if pd.isna(ema200) or pd.isna(sma20) or pd.isna(rsi_val) or v20avg <= 0:
                    continue
                dip = (sma20 - price) / sma20

                if not (price > ema200 and rsi_val < rsi_entry
                        and dip >= dip_pct and v_today >= vol_mult * v20avg):
                    continue

                # Size: equal share of sleeve equity (cash + open positions)
                current_total = cash + sum(
                    p["shares"] * (_cur(t) or p["entry_price"])
                    for t, p in open_pos.items()
                )
                slot   = current_total / max_positions
                shares = int(slot / price)
                if shares < 1 or slot > cash:   # can't afford even if sized correctly
                    continue

                cost  = shares * price * (1 + COMMISSION_PCT)
                cash -= cost    # deduct from cash immediately
                open_pos[tk] = {
                    "entry_price": price,
                    "entry_idx":   idx,
                    "entry_date":  today,
                    "shares":      shares,
                    "cost":        cost,
                }
                slots_free -= 1

        # Record equity snapshot
        open_val = sum(
            pos["shares"] * (_cur(tk) or pos["entry_price"])
            for tk, pos in open_pos.items()
        )
        eq_curve.append({"date": today, "equity": cash + open_val})

    # Close remaining positions at last available price
    last_idx = len(dates) - 1
    for tk, pos in open_pos.items():
        lp = _cur(tk) if _cur(tk) else pos["entry_price"]
        proceeds = pos["shares"] * lp * (1 - COMMISSION_PCT)
        pnl_net  = proceeds - pos["cost"]
        cash += proceeds
        trades.append({
            "ticker": tk, "entry_date": pos["entry_date"],
            "exit_date": dates[last_idx], "entry_price": pos["entry_price"],
            "exit_price": lp, "shares": pos["shares"],
            "pnl_net": pnl_net, "reason": "end-of-backtest",
            "days_held": last_idx - pos["entry_idx"],
        })

    if not eq_curve or not trades:
        return {"trades": trades, "sharpe": 0, "win_rate": 0,
                "max_dd": 1.0, "cagr": 0, "eq_curve": eq_curve}

    eq_df     = pd.DataFrame(eq_curve).set_index("date")
    daily_ret = eq_df["equity"].pct_change().dropna()
    sharpe    = (daily_ret.mean() / daily_ret.std() * np.sqrt(252)
                 if daily_ret.std() > 0 else 0.0)
    peak      = eq_df["equity"].cummax()
    max_dd    = float(((peak - eq_df["equity"]) / peak).max())
    years     = (dates[-1] - dates[start_idx]).days / 365
    cagr      = ((eq_df["equity"].iloc[-1] / USR.REVERSION_SLEEVE_SEK) ** (1 / years) - 1
                 if years > 0 else 0.0)

    df       = pd.DataFrame(trades)
    win_rate = len(df[df["pnl_net"] > 0]) / len(df) if len(df) > 0 else 0.0

    return {"trades": trades, "sharpe": sharpe, "win_rate": win_rate,
            "max_dd": max_dd, "cagr": cagr, "eq_curve": eq_curve}


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
        print("  US MEAN REVERSION — BACKTEST RESULTS")
        print("=" * 62)
        print(f"  Params:  RSI<{params['RSI_ENTRY']}  Dip>{params['DIP_PCT']*100:.0f}%  "
              f"Vol>{params['VOL_MULT']}×  Stop{params['STOP_PCT']*100:.0f}%  "
              f"MaxPos{params['MAX_POSITIONS']}  DDcap{params['SLEEVE_DD_CAP']*100:.0f}%")
        print(f"  Period:  {dates[start_idx].date()} → {dates[-1].date()} ({years:.1f}y)")
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
        dd_flag = "" if r["max_dd"] < 0.20 else "  ← ABOVE 20% target"
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
        print(f"  Sharpe>=0.8 {'✓' if r['sharpe']>=0.8 else '✗'}  "
              f"WinRate>=50% {'✓' if r['win_rate']>=0.5 else '✗'}  "
              f"MaxDD<20% {'✓' if r['max_dd']<0.20 else '✗'}  "
              f"Trades>=15 {'✓' if len(df)>=15 else '✗'}")
        print("=" * 62)
    return passed


def run_grid(close_df, vol_df, dates):
    keys   = list(GRID.keys())
    combos = list(product(*GRID.values()))
    print(f"Grid search: {len(combos)} combinations…\n")
    passing = []
    for i, combo in enumerate(combos, 1):
        params = dict(zip(keys, combo))
        r = simulate(close_df, vol_df, params)
        df = pd.DataFrame(r["trades"]) if r["trades"] else pd.DataFrame()
        passed = (r["sharpe"] >= 0.8 and r["win_rate"] >= 0.50
                  and r["max_dd"] < 0.20 and len(df) >= 15)
        tag = "PASS" if passed else "----"
        print(f"  [{i:>3}/{len(combos)}] {tag}  "
              f"RSI<{params['RSI_ENTRY']} Dip>{params['DIP_PCT']*100:.0f}% "
              f"Vol>{params['VOL_MULT']}× Stop{params['STOP_PCT']*100:.0f}% "
              f"Pos{params['MAX_POSITIONS']} DD{params['SLEEVE_DD_CAP']*100:.0f}%  |  "
              f"Sharpe={r['sharpe']:.2f} WR={r['win_rate']*100:.0f}% "
              f"DD={r['max_dd']*100:.1f}% CAGR={r['cagr']*100:.0f}% "
              f"N={len(df)}")
        if passed:
            passing.append((params, r))

    print(f"\n{'='*62}")
    print(f"  {len(passing)} parameter set(s) passed all criteria.")
    if passing:
        passing.sort(key=lambda x: x[1]["sharpe"], reverse=True)
        best_params, best_r = passing[0]
        print(f"  Best by Sharpe:\n")
        print_result(best_params, best_r, close_df, dates, verbose=True)
        print("\n  Apply to atos/us_reversion.py:")
        for k, v in best_params.items():
            print(f"    {k:<20} = {v}")
    else:
        print("  No combination passed. Expand the grid or relax one threshold.")
        # Show closest misses
        all_results = []
        keys2 = list(GRID.keys())
        for combo in list(product(*GRID.values())):
            p = dict(zip(keys2, combo))
            r = simulate(close_df, vol_df, p)
            df = pd.DataFrame(r["trades"]) if r["trades"] else pd.DataFrame()
            score = (r["sharpe"] * (1 - r["max_dd"]) * r["win_rate"]
                     * min(1, len(df) / 15))
            all_results.append((score, p, r))
        all_results.sort(reverse=True)
        print("\n  Top 3 closest misses:")
        for _, p, r in all_results[:3]:
            df = pd.DataFrame(r["trades"]) if r["trades"] else pd.DataFrame()
            print(f"    RSI<{p['RSI_ENTRY']} Dip>{p['DIP_PCT']*100:.0f}% "
                  f"Vol>{p['VOL_MULT']}× Stop{p['STOP_PCT']*100:.0f}%  "
                  f"Sharpe={r['sharpe']:.2f} WR={r['win_rate']*100:.0f}% "
                  f"DD={r['max_dd']*100:.1f}% N={len(df)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--grid", action="store_true",
                        help="Run full parameter grid search")
    args = parser.parse_args()

    close_df, vol_df = download_data()
    dates = close_df.index.tolist()
    print(f"  {len([t for t in US_TICKERS if t in close_df.columns])} US tickers available.\n")

    if args.grid:
        run_grid(close_df, vol_df, dates)
    else:
        params = {
            "RSI_ENTRY":     USR.RSI_ENTRY,
            "DIP_PCT":       USR.DIP_PCT,
            "VOL_MULT":      USR.VOL_MULT,
            "STOP_PCT":      USR.STOP_PCT,
            "MAX_POSITIONS": USR.MAX_POSITIONS,
            "SLEEVE_DD_CAP": USR.SLEEVE_DD_CAP,
        }
        r      = simulate(close_df, vol_df, params)
        passed = print_result(params, r, close_df, dates, verbose=True)
        if passed:
            print("\n  All criteria met. Set US_REVERSION_ENABLED = True in atos_runner.py")
        else:
            print("\n  Run:  python backtest_us_reversion.py --grid")
            print("  to find a parameter set that meets all criteria.")
