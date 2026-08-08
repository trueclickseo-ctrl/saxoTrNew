"""
validate_us_reversion.py
-------------------------
Four validation tests before declaring the US Mean Reversion strategy live-ready.

1. SPLIT TEST       — grid search on first half of data; test top RSI on held-out second half
2. SENSITIVITY      — RSI 28–36 (every integer), fix other params; look for cliffs
3. WALK-FORWARD     — 12-month train / 6-month test windows rolling forward; track best RSI per window
4. TRADE LOG        — full trade-by-trade log for the confirmed winner (RSI<33, Dip>5%, ...)

Run:
    python validate_us_reversion.py

Output:
    data/validation_report.txt      — full printed report
    data/trade_log_winner.csv       — trade log (RSI<33 winner)
    data/sensitivity_rsi.csv        — sensitivity sweep results
    data/walkforward_windows.csv    — walk-forward window results
"""
import sys
import os
import pickle
import csv
from datetime import date, timedelta
import numpy as np
import pandas as pd

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
DATA_DIR  = os.path.join(BASE_DIR, "data")
CACHE_PKL = os.path.join(DATA_DIR, "backtest_reversion_cache.pkl")

sys.path.insert(0, BASE_DIR)
import atos.us_reversion as USR
from atos.universe import US_TICKERS

COMMISSION_PCT = 0.0008

WINNER = dict(
    RSI_ENTRY=33, DIP_PCT=0.05, VOL_MULT=1.5,
    STOP_PCT=0.05, MAX_POSITIONS=3, SLEEVE_DD_CAP=0.15,
)


# ── Data ──────────────────────────────────────────────────────────────────────

def load_cached():
    if not os.path.exists(CACHE_PKL):
        raise FileNotFoundError(
            "Cache not found. Run: python backtest_us_reversion.py --grid  first.")
    with open(CACHE_PKL, "rb") as f:
        c = pickle.load(f)
    return c["close"], c["vol"], c["ind"]


# ── Core sim (same as backtest_us_reversion.py) ───────────────────────────────

def _rsi_series(c):
    delta = c.diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def simulate(close_df, ind, params, date_range=None):
    """Run simulation. date_range=(start_date, end_date) to restrict period."""
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
    start_idx = 220

    rsi_df    = ind["rsi"]
    sma20_df  = ind["sma20"]
    ema200_df = ind["ema200"]
    vrat_df   = ind["vrat"]

    if date_range:
        dr_start, dr_end = date_range
        # start_idx must remain 220 for indicator warmup; just filter trade events
        def in_range(d): return dr_start <= d.date() <= dr_end
    else:
        def in_range(d): return True

    cash     = float(USR.REVERSION_SLEEVE_SEK)
    peak_eq  = cash
    open_pos = {}
    trades   = []
    eq_curve = []

    for idx in range(start_idx, len(all_dates)):
        today = all_dates[idx]

        def _cur(tk):
            v = close_df[tk].iat[idx] if tk in close_df.columns else np.nan
            return float(v) if not pd.isna(v) else None

        open_val  = sum(pos["shares"]*(_cur(tk) or pos["entry_price"]) for tk,pos in open_pos.items())
        total_eq  = cash + open_val
        peak_eq   = max(peak_eq, total_eq)
        sleeve_dd = (peak_eq - total_eq) / peak_eq if peak_eq > 0 else 0.0

        # Exit
        to_close = []
        for tk, pos in list(open_pos.items()):
            cur     = _cur(tk); days_held = idx - pos["entry_idx"]
            cur_rsi = float(rsi_df[tk].iat[idx])   if tk in rsi_df.columns   else np.nan
            sma20   = float(sma20_df[tk].iat[idx])  if tk in sma20_df.columns else np.nan
            exit_now, reason = False, ""
            if days_held >= max_hold:
                exit_now, reason = True, f"time-stop {days_held}d"
            elif cur and cur <= pos["entry_price"]*(1-stop_pct):
                exit_now, reason = True, f"stop-loss -{(pos['entry_price']-cur)/pos['entry_price']*100:.1f}%"
            elif not pd.isna(cur_rsi) and cur_rsi > rsi_exit:
                exit_now, reason = True, f"RSI {cur_rsi:.0f}>{rsi_exit}"
            elif cur and not pd.isna(sma20) and cur >= sma20:
                exit_now, reason = True, f"SMA20 +{(cur-pos['entry_price'])/pos['entry_price']*100:.1f}%"
            if exit_now and cur:
                to_close.append((tk, cur, reason, pos))

        for tk, ep, reason, pos in to_close:
            proceeds = pos["shares"]*ep*(1-COMMISSION_PCT)
            pnl_net  = proceeds - pos["cost"]
            cash    += proceeds
            trades.append(dict(
                ticker=tk, entry_date=pos["entry_date"], exit_date=today,
                entry_price=pos["entry_price"], exit_price=ep,
                shares=pos["shares"], pnl_net=pnl_net, reason=reason,
                days_held=idx-pos["entry_idx"],
                entry_rsi=pos.get("entry_rsi", np.nan),
                in_range=in_range(today),
            ))
            del open_pos[tk]

        # Entry (only enter if in_range)
        slots_free = max_positions - len(open_pos)
        if slots_free > 0 and sleeve_dd < sleeve_dd_cap and in_range(today):
            current_total = cash + sum(p["shares"]*(_cur(t) or p["entry_price"]) for t,p in open_pos.items())
            for tk in tickers:
                if slots_free <= 0: break
                if tk in open_pos: continue
                price = _cur(tk)
                if price is None: continue
                ema200 = float(ema200_df[tk].iat[idx]) if tk in ema200_df.columns else np.nan
                sma20  = float(sma20_df[tk].iat[idx])  if tk in sma20_df.columns  else np.nan
                rsi_v  = float(rsi_df[tk].iat[idx])    if tk in rsi_df.columns    else np.nan
                vrat   = float(vrat_df[tk].iat[idx])   if tk in vrat_df.columns   else np.nan
                if any(pd.isna(x) for x in [ema200, sma20, rsi_v, vrat]): continue
                dip = (sma20 - price) / sma20
                if not (price>ema200 and rsi_v<rsi_entry and dip>=dip_pct and vrat>=vol_mult): continue
                slot   = current_total / max_positions
                shares = int(slot / price)
                if shares < 1 or slot > cash: continue
                cost   = shares*price*(1+COMMISSION_PCT)
                cash  -= cost
                open_pos[tk] = dict(entry_price=price, entry_idx=idx, entry_date=today,
                                    shares=shares, cost=cost, entry_rsi=rsi_v)
                slots_free -= 1

        open_val = sum(pos["shares"]*(_cur(tk) or pos["entry_price"]) for tk,pos in open_pos.items())
        eq_curve.append({"date": today, "equity": cash+open_val})

    # Close remaining
    last_idx = len(all_dates)-1
    for tk, pos in open_pos.items():
        lp = _cur(tk) or pos["entry_price"]
        proceeds = pos["shares"]*lp*(1-COMMISSION_PCT)
        cash += proceeds
        trades.append(dict(
            ticker=tk, entry_date=pos["entry_date"], exit_date=all_dates[last_idx],
            entry_price=pos["entry_price"], exit_price=lp,
            shares=pos["shares"], pnl_net=proceeds-pos["cost"],
            reason="end-of-backtest", days_held=last_idx-pos["entry_idx"],
            entry_rsi=pos.get("entry_rsi", np.nan), in_range=True,
        ))

    if not eq_curve or not trades:
        return {"trades": [], "sharpe": 0, "win_rate": 0, "max_dd": 1, "cagr": 0, "n": 0}

    eq_df     = pd.DataFrame(eq_curve).set_index("date")
    daily_ret = eq_df["equity"].pct_change().dropna()
    sharpe    = float(daily_ret.mean()/daily_ret.std()*np.sqrt(252) if daily_ret.std()>0 else 0)
    peak      = eq_df["equity"].cummax()
    max_dd    = float(((peak-eq_df["equity"])/peak).max())
    years     = (all_dates[-1]-all_dates[start_idx]).days/365
    cagr      = float((eq_df["equity"].iloc[-1]/USR.REVERSION_SLEEVE_SEK)**(1/years)-1 if years>0 else 0)
    df        = pd.DataFrame(trades)
    win_rate  = float(len(df[df["pnl_net"]>0])/len(df)) if len(df)>0 else 0

    return {"trades": trades, "sharpe": sharpe, "win_rate": win_rate,
            "max_dd": max_dd, "cagr": cagr, "n": len(df), "eq_curve": eq_curve}


def _metrics_from_trades(trades_list, start_equity=300_000.0):
    """Compute metrics from a filtered list of trade dicts."""
    if not trades_list:
        return dict(sharpe=0, win_rate=0, max_dd=1, cagr=0, n=0, total_pnl=0)
    df = pd.DataFrame(trades_list)
    n   = len(df)
    wr  = len(df[df["pnl_net"]>0]) / n
    pnl = df["pnl_net"].sum()
    # Crude Sharpe from P&L series (daily bucketed)
    df["exit_date"] = pd.to_datetime(df["exit_date"])
    daily = df.groupby("exit_date")["pnl_net"].sum().sort_index()
    if len(daily) > 2 and daily.std() > 0:
        sharpe = float(daily.mean()/daily.std()*np.sqrt(252/daily.index.to_series().diff().dt.days.median()))
    else:
        sharpe = 0.0
    return dict(sharpe=round(sharpe,2), win_rate=round(wr,3), max_dd=0, cagr=0, n=n, total_pnl=round(pnl,0))


# ── TEST 1: SPLIT ─────────────────────────────────────────────────────────────

def test_split(close_df, ind, out):
    dates = close_df.index
    midpoint = dates[len(dates)//2].date()
    start    = dates[220].date()
    end      = dates[-1].date()

    out.write("\n" + "="*66 + "\n")
    out.write("TEST 1 — SPLIT TEST (in-sample vs out-of-sample)\n")
    out.write(f"  Full period: {start} → {end}\n")
    out.write(f"  Split at:    {midpoint}\n")
    out.write(f"  In-sample:   {start} → {midpoint}\n")
    out.write(f"  Out-of-sample: {midpoint} → {end}\n")
    out.write("="*66 + "\n\n")

    rsi_values = [28, 30, 33]
    header = f"  {'RSI':<6} {'IS Sharpe':>10} {'IS WR':>7} {'IS N':>5}   {'OOS Sharpe':>10} {'OOS WR':>7} {'OOS N':>5}\n"
    out.write(header)
    out.write("  " + "-"*62 + "\n")

    for rsi in rsi_values:
        p = {**WINNER, "RSI_ENTRY": rsi}
        r = simulate(close_df, ind, p)
        all_trades = r["trades"]
        if not all_trades:
            out.write(f"  RSI<{rsi:<3}  no trades\n"); continue
        is_trades  = [t for t in all_trades if pd.Timestamp(t["entry_date"]).date() < midpoint]
        oos_trades = [t for t in all_trades if pd.Timestamp(t["entry_date"]).date() >= midpoint]
        is_m  = _metrics_from_trades(is_trades)
        oos_m = _metrics_from_trades(oos_trades)
        out.write(f"  RSI<{rsi:<3}  {is_m['sharpe']:>10.2f} {is_m['win_rate']*100:>6.0f}% {is_m['n']:>5}"
                  f"   {oos_m['sharpe']:>10.2f} {oos_m['win_rate']*100:>6.0f}% {oos_m['n']:>5}\n")

    out.write("\n  INTERPRETATION:\n")
    out.write("  OOS Sharpe >> 0 and WR >> 50% for RSI<33 = real edge.\n")
    out.write("  OOS Sharpe << IS Sharpe = normal degradation; cliff to 0 = overfit.\n\n")


# ── TEST 2: SENSITIVITY ───────────────────────────────────────────────────────

def test_sensitivity(close_df, ind, out):
    out.write("="*66 + "\n")
    out.write("TEST 2 — RSI SENSITIVITY (integers 28–36, other params fixed)\n")
    out.write("="*66 + "\n\n")
    out.write(f"  Fixed: Dip={WINNER['DIP_PCT']*100:.0f}% Vol={WINNER['VOL_MULT']}× "
              f"Stop={WINNER['STOP_PCT']*100:.0f}% Pos={WINNER['MAX_POSITIONS']} "
              f"DDcap={WINNER['SLEEVE_DD_CAP']*100:.0f}%\n\n")
    out.write(f"  {'RSI':>5} {'Sharpe':>8} {'WR':>6} {'MaxDD':>7} {'CAGR':>7} {'N':>5}  Verdict\n")
    out.write("  " + "-"*55 + "\n")

    rows = []
    for rsi in range(27, 37):
        p = {**WINNER, "RSI_ENTRY": rsi}
        r = simulate(close_df, ind, p)
        passed = r["sharpe"]>=0.8 and r["win_rate"]>=0.5 and r["max_dd"]<0.20 and r["n"]>=15
        marker = " ◄ WINNER" if rsi==33 else (" PASS" if passed else "")
        out.write(f"  RSI<{rsi}  {r['sharpe']:>8.2f} {r['win_rate']*100:>5.0f}% "
                  f"{r['max_dd']*100:>6.1f}% {r['cagr']*100:>6.0f}% {r['n']:>5}{marker}\n")
        rows.append(dict(RSI_ENTRY=rsi, sharpe=round(r["sharpe"],3),
                         win_rate=round(r["win_rate"],3), max_dd=round(r["max_dd"],3),
                         cagr=round(r["cagr"],3), n=r["n"], passed=int(passed)))

    out.write("\n  INTERPRETATION:\n")
    out.write("  Gradual slope = robust. Sharp cliff at RSI=33 = curve-fit signal.\n\n")

    sens_path = os.path.join(DATA_DIR, "sensitivity_rsi.csv")
    pd.DataFrame(rows).to_csv(sens_path, index=False)
    out.write(f"  Saved → {sens_path}\n\n")
    return rows


# ── TEST 3: WALK-FORWARD ──────────────────────────────────────────────────────

def test_walkforward(close_df, ind, out):
    out.write("="*66 + "\n")
    out.write("TEST 3 — WALK-FORWARD (12m train / 6m test, rolling)\n")
    out.write("="*66 + "\n\n")
    out.write(f"  {'Window':>6} {'Train period':<24} {'OOS period':<24} "
              f"{'Best IS RSI':>11} {'OOS Sharpe':>10} {'OOS WR':>7} {'OOS N':>5}\n")
    out.write("  " + "-"*88 + "\n")

    dates       = close_df.index
    start_date  = dates[220].date()
    end_date    = dates[-1].date()
    rsi_candidates = [28, 30, 33]
    window_rows = []

    wf_start = start_date
    window_n = 0
    while True:
        train_start = wf_start
        train_end   = train_start + timedelta(days=365)
        oos_start   = train_end
        oos_end     = oos_start + timedelta(days=183)
        if oos_end > end_date:
            break
        window_n += 1

        # Find best RSI on training window
        best_rsi, best_is_sharpe = 33, -99
        for rsi in rsi_candidates:
            p = {**WINNER, "RSI_ENTRY": rsi}
            r = simulate(close_df, ind, p)
            is_trades = [t for t in r["trades"]
                         if train_start <= pd.Timestamp(t["entry_date"]).date() <= train_end]
            m = _metrics_from_trades(is_trades)
            if m["sharpe"] > best_is_sharpe and m["n"] >= 5:
                best_is_sharpe = m["sharpe"]
                best_rsi = rsi

        # Test best RSI on OOS
        p_best = {**WINNER, "RSI_ENTRY": best_rsi}
        r_best = simulate(close_df, ind, p_best)
        oos_trades = [t for t in r_best["trades"]
                      if oos_start <= pd.Timestamp(t["entry_date"]).date() <= oos_end]
        oos_m = _metrics_from_trades(oos_trades)

        out.write(f"  W{window_n:<5} {str(train_start):<12}→{str(train_end):<12} "
                  f"{str(oos_start):<12}→{str(oos_end):<12} "
                  f"RSI<{best_rsi:>2}       {oos_m['sharpe']:>10.2f} "
                  f"{oos_m['win_rate']*100:>6.0f}% {oos_m['n']:>5}\n")
        window_rows.append(dict(window=window_n, train_start=train_start, train_end=train_end,
                                oos_start=oos_start, oos_end=oos_end, best_is_rsi=best_rsi,
                                oos_sharpe=oos_m["sharpe"], oos_wr=oos_m["win_rate"],
                                oos_n=oos_m["n"]))
        wf_start += timedelta(days=183)

    if window_rows:
        df_wf = pd.DataFrame(window_rows)
        rsi_stability = df_wf["best_is_rsi"].value_counts().to_dict()
        avg_oos_sharpe = df_wf["oos_sharpe"].mean()
        pct_oos_pass   = (df_wf["oos_sharpe"] > 0).mean() * 100

        out.write(f"\n  RSI selected per window: {rsi_stability}\n")
        out.write(f"  Avg OOS Sharpe:          {avg_oos_sharpe:.2f}\n")
        out.write(f"  % windows with OOS Sharpe > 0: {pct_oos_pass:.0f}%\n")
        out.write("\n  INTERPRETATION:\n")
        out.write("  RSI=33 dominant across windows = stable. Mixed results = fragile.\n")
        out.write("  Avg OOS Sharpe > 0.5 = real edge surviving forward.\n\n")

        wf_path = os.path.join(DATA_DIR, "walkforward_windows.csv")
        df_wf.to_csv(wf_path, index=False)
        out.write(f"  Saved → {wf_path}\n\n")


# ── TEST 4: TRADE LOG ─────────────────────────────────────────────────────────

def test_tradelog(close_df, ind, out):
    out.write("="*66 + "\n")
    out.write("TEST 4 — FULL TRADE LOG (RSI<33 winner)\n")
    out.write("="*66 + "\n\n")

    r  = simulate(close_df, ind, WINNER)
    df = pd.DataFrame(r["trades"]) if r["trades"] else pd.DataFrame()
    if df.empty:
        out.write("  No trades.\n\n"); return

    df["entry_date"] = pd.to_datetime(df["entry_date"])
    df["exit_date"]  = pd.to_datetime(df["exit_date"])
    df = df.sort_values("entry_date")

    # Summary by RSI band
    out.write("  Trades by entry RSI band:\n")
    for lo, hi, label in [(0,28,"RSI 0-28  (deep oversold)"),
                          (28,30,"RSI 28-30"),
                          (30,33,"RSI 30-33"),
                          (33,40,"RSI 33-40 (shouldn't exist)")]:
        band = df[(df["entry_rsi"]>=lo) & (df["entry_rsi"]<hi)] if "entry_rsi" in df.columns else pd.DataFrame()
        if len(band):
            wr  = len(band[band["pnl_net"]>0]) / len(band)
            pnl = band["pnl_net"].sum()
            out.write(f"    {label}: N={len(band):<3} WR={wr*100:.0f}%  Total P&L={pnl:+,.0f} SEK\n")
    out.write("\n")

    # Full table
    out.write(f"  {'#':<4} {'Ticker':<7} {'Entry':>11} {'Exit':>11} "
              f"{'Held':>5} {'Entry RSI':>9} {'Entry $':>9} "
              f"{'Exit $':>9} {'P&L SEK':>10} {'Reason'}\n")
    out.write("  " + "-"*100 + "\n")

    for i, row in df.iterrows():
        rsi_str = f"{row['entry_rsi']:.1f}" if not pd.isna(row.get("entry_rsi", np.nan)) else "—"
        out.write(f"  {i+1:<4} {row['ticker']:<7} {str(row['entry_date'])[:10]:>11} "
                  f"{str(row['exit_date'])[:10]:>11} {row['days_held']:>5}d "
                  f"{rsi_str:>9} ${row['entry_price']:>8.2f} "
                  f"${row['exit_price']:>8.2f} {row['pnl_net']:>+10,.0f}  {row['reason']}\n")

    wins = df[df["pnl_net"]>0]; losses = df[df["pnl_net"]<=0]
    out.write(f"\n  TOTALS: {len(df)} trades | {len(wins)} wins ({len(wins)/len(df)*100:.0f}%) "
              f"| {len(losses)} losses | P&L {df['pnl_net'].sum():+,.0f} SEK\n")
    out.write(f"  Avg win: +{wins['pnl_net'].mean():,.0f} SEK  "
              f"Avg loss: {losses['pnl_net'].mean():,.0f} SEK  "
              f"Avg hold: {df['days_held'].mean():.1f}d\n\n")

    # Save CSV
    log_path = os.path.join(DATA_DIR, "trade_log_winner.csv")
    df.to_csv(log_path, index=False)
    out.write(f"  Saved → {log_path}\n\n")


# ── OVERALL VERDICT ───────────────────────────────────────────────────────────

def verdict(split_summary, sens_rows, wf_rows, out):
    out.write("="*66 + "\n")
    out.write("OVERALL VALIDATION VERDICT\n")
    out.write("="*66 + "\n\n")

    checks = []

    # Sensitivity: check if RSI=33 isn't a cliff
    if sens_rows:
        s_df = pd.DataFrame(sens_rows).set_index("RSI_ENTRY")
        rsi33_sh = s_df.loc[33, "sharpe"] if 33 in s_df.index else 0
        rsi32_sh = s_df.loc[32, "sharpe"] if 32 in s_df.index else 0
        rsi34_sh = s_df.loc[34, "sharpe"] if 34 in s_df.index else 0
        drop = max(rsi33_sh - rsi32_sh, rsi33_sh - rsi34_sh)
        cliff = drop > 0.8
        checks.append(("Sensitivity (no cliff at RSI=33)", not cliff,
                        f"Sharpe 32={rsi32_sh:.2f} 33={rsi33_sh:.2f} 34={rsi34_sh:.2f} — "
                        f"{'CLIFF (drop {:.2f}) → overfitting signal'.format(drop) if cliff else 'Gradual slope → robust'}"))

    # Walk-forward stability
    if wf_rows:
        wf_df = pd.DataFrame(wf_rows)
        rsi33_dom = (wf_df["best_is_rsi"] == 33).mean()
        avg_oos = wf_df["oos_sharpe"].mean()
        pct_pos = (wf_df["oos_sharpe"] > 0).mean()
        checks.append(("Walk-forward: RSI=33 dominant (>50% windows)", rsi33_dom >= 0.5,
                        f"RSI=33 chosen in {rsi33_dom*100:.0f}% of windows"))
        checks.append(("Walk-forward: avg OOS Sharpe > 0", avg_oos > 0,
                        f"Avg OOS Sharpe = {avg_oos:.2f}"))
        checks.append(("Walk-forward: >60% windows positive OOS Sharpe", pct_pos >= 0.60,
                        f"{pct_pos*100:.0f}% of windows had OOS Sharpe > 0"))

    for label, passed, note in checks:
        status = "✓ PASS" if passed else "✗ FAIL"
        out.write(f"  {status}  {label}\n")
        out.write(f"         {note}\n\n")

    n_pass = sum(1 for _, p, _ in checks if p)
    n_total = len(checks)
    out.write(f"  {n_pass}/{n_total} checks passed.\n\n")
    if n_pass == n_total:
        out.write("  VERDICT: VALIDATED — strategy shows robust out-of-sample edge.\n")
        out.write("           Safe to enable on SIM. Watch live results for 4–6 weeks\n")
        out.write("           before considering real capital.\n")
    elif n_pass >= n_total * 0.6:
        out.write("  VERDICT: CONDITIONAL — some concerns. Review FAIL items above.\n")
        out.write("           Enable on SIM but extend paper trading period to 3 months.\n")
    else:
        out.write("  VERDICT: NOT VALIDATED — significant validation failures.\n")
        out.write("           Do not enable. Consider looser criteria or different logic.\n")
    out.write("="*66 + "\n")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import io

    print("Loading cached data…")
    close_df, vol_df, ind = load_cached()
    print(f"  {len([t for t in US_TICKERS if t in close_df.columns])} tickers, "
          f"{len(close_df)} trading days.\n")

    os.makedirs(DATA_DIR, exist_ok=True)
    report_path = os.path.join(DATA_DIR, "validation_report.txt")

    buf = io.StringIO()
    buf.write(f"US MEAN REVERSION — VALIDATION REPORT\n")
    buf.write(f"Generated: {date.today().isoformat()}\n")
    buf.write(f"Winner params: RSI<{WINNER['RSI_ENTRY']} Dip>{WINNER['DIP_PCT']*100:.0f}% "
              f"Vol>{WINNER['VOL_MULT']}× Stop{WINNER['STOP_PCT']*100:.0f}% "
              f"Pos{WINNER['MAX_POSITIONS']} DDcap{WINNER['SLEEVE_DD_CAP']*100:.0f}%\n")

    print("Running TEST 1: Split test…")
    test_split(close_df, ind, buf)

    print("Running TEST 2: RSI sensitivity (27–36)…")
    sens_rows = test_sensitivity(close_df, ind, buf)

    print("Running TEST 3: Walk-forward…")
    test_walkforward(close_df, ind, buf)

    print("Running TEST 4: Trade log…")
    test_tradelog(close_df, ind, buf)

    verdict(None, sens_rows,
            pd.read_csv(os.path.join(DATA_DIR, "walkforward_windows.csv")).to_dict("records")
            if os.path.exists(os.path.join(DATA_DIR, "walkforward_windows.csv")) else [],
            buf)

    report_text = buf.getvalue()
    print(report_text)

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_text)

    print(f"\nFiles written:")
    for fn in ["validation_report.txt", "trade_log_winner.csv",
               "sensitivity_rsi.csv", "walkforward_windows.csv"]:
        fp = os.path.join(DATA_DIR, fn)
        if os.path.exists(fp):
            size = os.path.getsize(fp)
            print(f"  {fp}  ({size:,} bytes)")
