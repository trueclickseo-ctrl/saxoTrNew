"""
validate_honest_split.py
-------------------------
Honest train/test validation for US Mean Reversion.

The leakage bug in validate_us_reversion.py:
  - simulate() ran on the full dataset, then trades were split by date.
  - The IS grid search used full-sample trades to pick RSI=33.
  - The "OOS" cash state was contaminated by IS trading results.

This script fixes all three:
  1. FULL grid search (all 6 params, 486 combos) on TRAINING DATA ONLY.
     Each combo's simulate() is restricted to bars[:train_cutoff].
     Cash resets to 300K SEK at the start of each IS simulation.
  2. FREEZE the winning combo from step 1.
  3. Run that frozen combo on OOS data ONLY, cash reset to 300K SEK.
     No information from OOS period was used in param selection.

Train: first ~18 months of trading activity after indicator warmup.
Test:  remaining period (~9 months).

Usage:
    python validate_honest_split.py
"""
import sys
import os
import pickle
import csv
from itertools import product
from datetime import date
import numpy as np
import pandas as pd

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
DATA_DIR  = os.path.join(BASE_DIR, "data")
CACHE_PKL = os.path.join(DATA_DIR, "backtest_reversion_cache.pkl")

sys.path.insert(0, BASE_DIR)
import atos.us_reversion as USR
from atos.universe import US_TICKERS

COMMISSION_PCT = 0.0008
SLEEVE_SEK     = float(USR.REVERSION_SLEEVE_SEK)

GRID = {
    "RSI_ENTRY":     [28, 30, 33],
    "DIP_PCT":       [0.04, 0.05, 0.06],
    "VOL_MULT":      [1.5, 1.8, 2.0],
    "STOP_PCT":      [0.04, 0.05, 0.06],
    "MAX_POSITIONS": [2, 3],
    "SLEEVE_DD_CAP": [0.10, 0.15, 0.20],
}

WARMUP = 220   # bars needed before any trading can start


# ── Core simulation — restricted to a date index slice ────────────────────────

def simulate_slice(close_df, ind, params, idx_start, idx_end):
    """
    Simulate on close_df.index[idx_start:idx_end] only.
    Cash starts fresh at SLEEVE_SEK regardless of prior results.
    idx_start must be >= WARMUP so indicators are valid.
    """
    rsi_entry     = params["RSI_ENTRY"]
    dip_pct       = params["DIP_PCT"]
    vol_mult      = params["VOL_MULT"]
    stop_pct      = params["STOP_PCT"]
    max_positions = params["MAX_POSITIONS"]
    sleeve_dd_cap = params["SLEEVE_DD_CAP"]
    rsi_exit      = USR.RSI_EXIT
    max_hold      = USR.MAX_HOLD_DAYS

    tickers   = [t for t in US_TICKERS if t in close_df.columns]
    all_dates = close_df.index.tolist()

    rsi_df    = ind["rsi"]
    sma20_df  = ind["sma20"]
    ema200_df = ind["ema200"]
    vrat_df   = ind["vrat"]

    cash     = SLEEVE_SEK
    peak_eq  = cash
    open_pos = {}
    trades   = []
    eq_curve = []

    for idx in range(idx_start, idx_end):
        today = all_dates[idx]

        def _cur(tk):
            v = close_df[tk].iat[idx] if tk in close_df.columns else np.nan
            return float(v) if not pd.isna(v) else None

        open_val  = sum(p["shares"] * (_cur(tk) or p["entry_price"]) for tk, p in open_pos.items())
        total_eq  = cash + open_val
        peak_eq   = max(peak_eq, total_eq)
        sleeve_dd = (peak_eq - total_eq) / peak_eq if peak_eq > 0 else 0.0

        # Exit
        to_close = []
        for tk, pos in list(open_pos.items()):
            cur       = _cur(tk)
            cur_rsi   = float(rsi_df[tk].iat[idx])   if tk in rsi_df.columns   else np.nan
            sma20     = float(sma20_df[tk].iat[idx])  if tk in sma20_df.columns else np.nan
            days_held = idx - pos["entry_idx"]
            exit_now, reason = False, ""
            if days_held >= max_hold:
                exit_now, reason = True, f"time-stop {days_held}d"
            elif cur and cur <= pos["entry_price"] * (1 - stop_pct):
                pct = (pos["entry_price"] - cur) / pos["entry_price"] * 100
                exit_now, reason = True, f"stop-loss -{pct:.1f}%"
            elif not pd.isna(cur_rsi) and cur_rsi > rsi_exit:
                exit_now, reason = True, f"RSI {cur_rsi:.0f}>{rsi_exit}"
            elif cur and not pd.isna(sma20) and cur >= sma20:
                g = (cur - pos["entry_price"]) / pos["entry_price"] * 100
                exit_now, reason = True, f"SMA20 +{g:.1f}%"
            if exit_now and cur:
                to_close.append((tk, cur, reason, pos))

        for tk, ep, reason, pos in to_close:
            proceeds = pos["shares"] * ep * (1 - COMMISSION_PCT)
            pnl_net  = proceeds - pos["cost"]
            cash    += proceeds
            trades.append(dict(
                ticker=tk, entry_date=pos["entry_date"], exit_date=today,
                entry_price=pos["entry_price"], exit_price=ep,
                shares=pos["shares"], pnl_net=pnl_net,
                reason=reason, days_held=days_held,
                entry_rsi=pos.get("entry_rsi", np.nan),
            ))
            del open_pos[tk]

        # Entry
        slots_free = max_positions - len(open_pos)
        if slots_free > 0 and sleeve_dd < sleeve_dd_cap:
            cur_total = cash + sum(p["shares"] * (_cur(t) or p["entry_price"]) for t, p in open_pos.items())
            for tk in tickers:
                if slots_free <= 0: break
                if tk in open_pos: continue
                price  = _cur(tk)
                if price is None: continue
                ema200 = float(ema200_df[tk].iat[idx]) if tk in ema200_df.columns else np.nan
                sma20  = float(sma20_df[tk].iat[idx])  if tk in sma20_df.columns  else np.nan
                rsi_v  = float(rsi_df[tk].iat[idx])    if tk in rsi_df.columns    else np.nan
                vrat   = float(vrat_df[tk].iat[idx])   if tk in vrat_df.columns   else np.nan
                if any(pd.isna(x) for x in [ema200, sma20, rsi_v, vrat]): continue
                dip = (sma20 - price) / sma20
                if not (price > ema200 and rsi_v < rsi_entry
                        and dip >= dip_pct and vrat >= vol_mult): continue
                slot   = cur_total / max_positions
                shares = int(slot / price)
                if shares < 1 or slot > cash: continue
                cost   = shares * price * (1 + COMMISSION_PCT)
                cash  -= cost
                open_pos[tk] = dict(
                    entry_price=price, entry_idx=idx, entry_date=today,
                    shares=shares, cost=cost, entry_rsi=rsi_v,
                )
                slots_free -= 1

        open_val = sum(p["shares"] * (_cur(tk) or p["entry_price"]) for tk, p in open_pos.items())
        eq_curve.append({"date": today, "equity": cash + open_val})

    # Close remaining at last price
    last_idx = idx_end - 1
    for tk, pos in open_pos.items():
        lp = _cur(tk) if _cur(tk) else pos["entry_price"]
        proceeds = pos["shares"] * lp * (1 - COMMISSION_PCT)
        pnl_net  = proceeds - pos["cost"]
        cash    += proceeds
        trades.append(dict(
            ticker=tk, entry_date=pos["entry_date"], exit_date=all_dates[last_idx],
            entry_price=pos["entry_price"], exit_price=lp,
            shares=pos["shares"], pnl_net=pnl_net,
            reason="end-of-period", days_held=last_idx - pos["entry_idx"],
            entry_rsi=pos.get("entry_rsi", np.nan),
        ))

    if not eq_curve or not trades:
        return dict(trades=[], sharpe=0, win_rate=0, max_dd=1, cagr=0, n=0)

    eq_df     = pd.DataFrame(eq_curve).set_index("date")
    daily_ret = eq_df["equity"].pct_change().dropna()
    sharpe    = float(daily_ret.mean() / daily_ret.std() * np.sqrt(252)
                      if daily_ret.std() > 0 else 0)
    peak      = eq_df["equity"].cummax()
    max_dd    = float(((peak - eq_df["equity"]) / peak).max())
    n_days    = (all_dates[idx_end-1] - all_dates[idx_start]).days
    years     = n_days / 365
    cagr      = float((eq_df["equity"].iloc[-1] / SLEEVE_SEK) ** (1/years) - 1
                      if years > 0 else 0)
    df        = pd.DataFrame(trades)
    win_rate  = float(len(df[df["pnl_net"] > 0]) / len(df)) if len(df) else 0

    return dict(trades=trades, sharpe=sharpe, win_rate=win_rate,
                max_dd=max_dd, cagr=cagr, n=len(df))


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if not os.path.exists(CACHE_PKL):
        print("Cache not found. Run:  python backtest_us_reversion.py --grid  first.")
        sys.exit(1)

    with open(CACHE_PKL, "rb") as f:
        c = pickle.load(f)
    close_df, ind = c["close"], c["ind"]

    all_dates  = close_df.index.tolist()
    n_bars     = len(all_dates)
    trade_bars = n_bars - WARMUP   # bars available for actual trading

    # Split: first 18 months of trading (~380 bars) = train; remainder = test
    # 1 month ≈ 21 trading days; 18m ≈ 378 bars
    train_bars = min(380, trade_bars // 2)
    train_end_idx = WARMUP + train_bars
    oos_start_idx = train_end_idx

    train_start = all_dates[WARMUP].date()
    train_end   = all_dates[train_end_idx - 1].date()
    oos_start   = all_dates[oos_start_idx].date()
    oos_end     = all_dates[-1].date()

    print("=" * 66)
    print("HONEST TRAIN/TEST VALIDATION — US Mean Reversion")
    print("=" * 66)
    print(f"  Total bars:   {n_bars} ({all_dates[0].date()} → {all_dates[-1].date()})")
    print(f"  Warmup:       {WARMUP} bars (indicators)")
    print(f"  TRAIN:        {train_bars} bars  ({train_start} → {train_end})")
    print(f"  OOS TEST:     {n_bars - oos_start_idx} bars  ({oos_start} → {oos_end})")
    print()

    # ── STEP 1: Full grid on training data only ────────────────────────────────
    keys   = list(GRID.keys())
    combos = list(product(*GRID.values()))
    print(f"Running full grid ({len(combos)} combos) on TRAINING data only…")
    print(f"  {'#':>4}  {'RSI':>4} {'Dip':>5} {'Vol':>5} {'Stop':>5} {'Pos':>4} {'DDcap':>6}"
          f"  {'Sharpe':>7} {'WR':>5} {'DD':>6} {'N':>4}  Tag")
    print("  " + "-" * 72)

    is_results = []
    for i, combo in enumerate(combos, 1):
        params = dict(zip(keys, combo))
        r = simulate_slice(close_df, ind, params, WARMUP, train_end_idx)
        df_t = pd.DataFrame(r["trades"]) if r["trades"] else pd.DataFrame()
        n    = len(df_t)
        passed = r["sharpe"] >= 0.8 and r["win_rate"] >= 0.5 and r["max_dd"] < 0.20 and n >= 10
        tag  = "PASS" if passed else "----"
        if i % 54 == 1 or passed:   # print every 54th + all passes
            print(f"  [{i:>3}] {tag}  RSI<{params['RSI_ENTRY']} "
                  f"Dip>{params['DIP_PCT']*100:.0f}% Vol>{params['VOL_MULT']}× "
                  f"Stop{params['STOP_PCT']*100:.0f}% Pos{params['MAX_POSITIONS']} "
                  f"DD{params['SLEEVE_DD_CAP']*100:.0f}%  "
                  f"Sharpe={r['sharpe']:.2f} WR={r['win_rate']*100:.0f}% "
                  f"DD={r['max_dd']*100:.1f}% N={n}")
        is_results.append(dict(params=params, sharpe=r["sharpe"], win_rate=r["win_rate"],
                               max_dd=r["max_dd"], cagr=r["cagr"], n=n, passed=passed))

    passing = [x for x in is_results if x["passed"]]
    print(f"\n  {len(passing)}/{len(combos)} combos passed on training data.")

    if not passing:
        print("\n  No combo passed IS criteria (Sharpe≥0.8, WR≥50%, DD<20%, N≥10).")
        print("  Strategy does not have sufficient IS edge — do not enable.")
        return

    passing.sort(key=lambda x: x["sharpe"], reverse=True)
    best = passing[0]
    frozen_params = best["params"]

    print(f"\n  IS WINNER (frozen): RSI<{frozen_params['RSI_ENTRY']} "
          f"Dip>{frozen_params['DIP_PCT']*100:.0f}% "
          f"Vol>{frozen_params['VOL_MULT']}× Stop{frozen_params['STOP_PCT']*100:.0f}% "
          f"Pos{frozen_params['MAX_POSITIONS']} DDcap{frozen_params['SLEEVE_DD_CAP']*100:.0f}%")
    print(f"  IS metrics:  Sharpe={best['sharpe']:.2f}  WR={best['win_rate']*100:.0f}%  "
          f"MaxDD={best['max_dd']*100:.1f}%  CAGR={best['cagr']*100:.0f}%  N={best['n']}")

    # ── Top-5 IS combos ────────────────────────────────────────────────────────
    print(f"\n  Top-5 IS combos by Sharpe:")
    print(f"  {'RSI':>5} {'Dip':>5} {'Vol':>5} {'Stop':>5} {'Pos':>4} {'DDcap':>6}"
          f"  {'Sharpe':>7} {'WR':>5} {'DD':>6} {'N':>4}")
    print("  " + "-" * 55)
    for x in passing[:5]:
        p = x["params"]
        print(f"  RSI<{p['RSI_ENTRY']} Dip>{p['DIP_PCT']*100:.0f}% Vol>{p['VOL_MULT']}× "
              f"Stop{p['STOP_PCT']*100:.0f}% Pos{p['MAX_POSITIONS']} "
              f"DDcap{p['SLEEVE_DD_CAP']*100:.0f}%  "
              f"{x['sharpe']:>7.2f} {x['win_rate']*100:>4.0f}% "
              f"{x['max_dd']*100:>5.1f}% {x['n']:>4}")

    # ── STEP 2: RSI dominance in IS top-10 ────────────────────────────────────
    print(f"\n  RSI dominance in top-10 IS combos:")
    from collections import Counter
    rsi_counts = Counter(x["params"]["RSI_ENTRY"] for x in passing[:10])
    for rsi, cnt in sorted(rsi_counts.items()):
        print(f"    RSI<{rsi}: {cnt}/10")

    # ── STEP 3: OOS test with frozen params ───────────────────────────────────
    print(f"\n{'='*66}")
    print(f"OOS TEST — frozen params, data it never saw")
    print(f"{'='*66}")
    print(f"  Period: {oos_start} → {oos_end}  ({n_bars - oos_start_idx} bars)")
    print(f"  Params: RSI<{frozen_params['RSI_ENTRY']} Dip>{frozen_params['DIP_PCT']*100:.0f}% "
          f"Vol>{frozen_params['VOL_MULT']}× Stop{frozen_params['STOP_PCT']*100:.0f}% "
          f"Pos{frozen_params['MAX_POSITIONS']} DDcap{frozen_params['SLEEVE_DD_CAP']*100:.0f}%")
    print()

    oos_r = simulate_slice(close_df, ind, frozen_params, oos_start_idx, n_bars)
    oos_df = pd.DataFrame(oos_r["trades"]) if oos_r["trades"] else pd.DataFrame()

    print(f"  OOS Sharpe:    {oos_r['sharpe']:.2f}  (IS: {best['sharpe']:.2f})")
    print(f"  OOS WinRate:   {oos_r['win_rate']*100:.0f}%  (IS: {best['win_rate']*100:.0f}%)")
    print(f"  OOS MaxDD:     {oos_r['max_dd']*100:.1f}%  (IS: {best['max_dd']*100:.1f}%)")
    print(f"  OOS CAGR:      {oos_r['cagr']*100:.0f}%  (IS: {best['cagr']*100:.0f}%)")
    print(f"  OOS Trades:    {oos_r['n']}  (IS: {best['n']})")

    if not oos_df.empty:
        wins = oos_df[oos_df["pnl_net"] > 0]
        loss = oos_df[oos_df["pnl_net"] <= 0]
        print(f"  OOS P&L:       {oos_df['pnl_net'].sum():+,.0f} SEK")
        if len(wins): print(f"  Avg win:       +{wins['pnl_net'].mean():,.0f} SEK")
        if len(loss): print(f"  Avg loss:      {loss['pnl_net'].mean():,.0f} SEK")

        # RSI band breakdown on OOS
        print(f"\n  OOS Trades by RSI band at entry:")
        for lo, hi, label in [(0,28,"RSI 0-28"), (28,30,"RSI 28-30"), (30,33,"RSI 30-33"),
                               (33,36,"RSI 33-36")]:
            band = oos_df[(oos_df["entry_rsi"]>=lo) & (oos_df["entry_rsi"]<hi)] if "entry_rsi" in oos_df.columns else pd.DataFrame()
            if len(band):
                wr  = len(band[band["pnl_net"]>0]) / len(band)
                pnl = band["pnl_net"].sum()
                print(f"    {label}: N={len(band):<3} WR={wr*100:.0f}%  P&L={pnl:+,.0f} SEK")

        # OOS trade log
        print(f"\n  OOS trade log:")
        print(f"  {'Ticker':<7} {'Entry':>11} {'Exit':>11} {'Held':>5} {'RSI':>5} "
              f"{'Entry $':>9} {'Exit $':>8} {'P&L SEK':>10} Reason")
        print("  " + "-"*88)
        for _, row in oos_df.iterrows():
            rsi_s = f"{row['entry_rsi']:.1f}" if not pd.isna(row.get("entry_rsi", np.nan)) else "—"
            print(f"  {row['ticker']:<7} {str(row['entry_date'])[:10]:>11} "
                  f"{str(row['exit_date'])[:10]:>11} {row['days_held']:>5}d "
                  f"{rsi_s:>5} ${row['entry_price']:>8.2f} ${row['exit_price']:>7.2f} "
                  f"{row['pnl_net']:>+10,.0f}  {row['reason']}")

    # ── STEP 4: Sensitivity around IS winner RSI, on OOS only ─────────────────
    print(f"\n{'='*66}")
    print(f"SENSITIVITY — RSI 28–36 tested on OOS only (no IS touch)")
    print(f"{'='*66}")
    print(f"  {'RSI':>5}  {'Sharpe':>7} {'WR':>5} {'DD':>6} {'N':>4}")
    print("  " + "-"*35)
    for rsi in range(27, 37):
        p = {**frozen_params, "RSI_ENTRY": rsi}
        r = simulate_slice(close_df, ind, p, oos_start_idx, n_bars)
        marker = " ◄ IS winner" if rsi == frozen_params["RSI_ENTRY"] else ""
        print(f"  RSI<{rsi}   {r['sharpe']:>7.2f} {r['win_rate']*100:>4.0f}% "
              f"{r['max_dd']*100:>5.1f}% {r['n']:>4}{marker}")

    # ── VERDICT ───────────────────────────────────────────────────────────────
    print(f"\n{'='*66}")
    print("HONEST VERDICT")
    print(f"{'='*66}")
    oos_pass = (oos_r["sharpe"] >= 0.5 and oos_r["win_rate"] >= 0.50
                and oos_r["max_dd"] < 0.25 and oos_r["n"] >= 8)
    is_rsi   = frozen_params["RSI_ENTRY"]

    checks = [
        ("OOS Sharpe ≥ 0.5",                oos_r["sharpe"] >= 0.5,         f"{oos_r['sharpe']:.2f}"),
        ("OOS WinRate ≥ 50%",               oos_r["win_rate"] >= 0.5,       f"{oos_r['win_rate']*100:.0f}%"),
        ("OOS MaxDD < 25%",                  oos_r["max_dd"] < 0.25,         f"{oos_r['max_dd']*100:.1f}%"),
        ("OOS N ≥ 8 trades",                 oos_r["n"] >= 8,                f"{oos_r['n']}"),
        ("IS winner RSI matches grid winner", is_rsi == 33,                  f"RSI<{is_rsi}"),
    ]
    for label, ok, val in checks:
        print(f"  {'✓' if ok else '✗'}  {label}: {val}")

    n_ok = sum(ok for _, ok, _ in checks)
    print()
    if n_ok == len(checks):
        print("  VERDICT: PASSES HONEST OOS TEST.")
        print("  The edge survives genuine out-of-sample validation.")
        print("  Small N remains a caveat — treat as early-stage evidence,")
        print("  not proof. SIM-first is still the right call.")
    elif n_ok >= 3:
        print("  VERDICT: PARTIAL PASS — some OOS evidence of edge,")
        print("  but not strong enough to call it validated.")
        print("  Extend SIM period before considering real capital.")
    else:
        print("  VERDICT: FAILS HONEST OOS TEST.")
        print("  Do not enable. The backtest edge did not survive.")
    print("=" * 66)

    # Save OOS trades
    if not oos_df.empty:
        oos_path = os.path.join(DATA_DIR, "oos_trade_log.csv")
        oos_df.to_csv(oos_path, index=False)
        print(f"\n  OOS trades saved → {oos_path}")

    # Save IS grid results
    is_path = os.path.join(DATA_DIR, "is_grid_results.csv")
    rows = []
    for x in is_results:
        row = {**x["params"], "sharpe": round(x["sharpe"],3), "win_rate": round(x["win_rate"],3),
               "max_dd": round(x["max_dd"],3), "cagr": round(x["cagr"],3),
               "n": x["n"], "passed": int(x["passed"])}
        rows.append(row)
    pd.DataFrame(rows).to_csv(is_path, index=False)
    print(f"  IS grid results saved → {is_path}")


if __name__ == "__main__":
    main()
