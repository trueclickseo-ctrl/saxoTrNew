"""
backtests/rsi_exit_ladder_backtest.py
-------------------------------------
Compares the RSI(2) book's CURRENT exit management against the proposed
profit-protection LADDER, on the same historical entries.

Entries: the REAL forex.strategy_rsi.generate_signals rule (close vs EMA200
+ RSI(2) < 10 / > 90), replayed day-by-day on yfinance daily bars
(Yahoo is sanctioned for backtests only -- see the "Saxo-Only Live Prices"
standing rule; live trading never touches it).

Both policies share: RSI recovery exit (55/45), 2R broker take-profit,
12-day time stop, 1.5x ATR initial hard stop. They differ only in how the
stop is tightened while the trade is open:

  CURRENT : forex.strategy_rsi.trailing_stop_update every bar (1.5x ATR,
            active from day 1) + one-shot breakeven-to-entry once profit
            >= runner.BREAKEVEN_THRESHOLD_ATR x ATR_at_entry.

  LADDER  : forex.runner._profit_ladder_target_stop  (0.75R -> entry+0.1R,
            1.0R -> entry+0.5R, 1.25R -> max(entry+0.5R, close-1xATR)).

Both call the ACTUAL production functions, not re-implementations.

Usage:
    python backtests/rsi_exit_ladder_backtest.py            # 10y, default cost
    python backtests/rsi_exit_ladder_backtest.py --years 15
    python backtests/rsi_exit_ladder_backtest.py --cost-r 0.05
"""

import argparse
import os
import sys
import warnings

warnings.filterwarnings("ignore")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import numpy as np
import pandas as pd

import forex.strategy_rsi as srsi
import forex.runner as runner

# 17-pair HIGH_VOLUME universe (the LIVE_EUR RSI book) -> yfinance tickers
PAIRS = {
    "EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "USDJPY=X",
    "AUDUSD": "AUDUSD=X", "USDCAD": "USDCAD=X", "NZDUSD": "NZDUSD=X",
    "USDCHF": "USDCHF=X", "EURGBP": "EURGBP=X", "EURJPY": "EURJPY=X",
    "GBPJPY": "GBPJPY=X", "AUDJPY": "AUDJPY=X", "EURAUD": "EURAUD=X",
    "EURCAD": "EURCAD=X", "EURCHF": "EURCHF=X", "GBPCHF": "GBPCHF=X",
    "GBPCAD": "GBPCAD=X", "GBPAUD": "GBPAUD=X",
}

DEFAULT_TP_RR = runner.DEFAULT_TP_RR          # 2.0
BREAKEVEN_ATR = runner.BREAKEVEN_THRESHOLD_ATR  # 1.0
ATR_MULT      = srsi.ATR_STOP_MULT             # 1.5
TIME_STOP     = srsi.TIME_STOP_DAYS            # 12


# ── data ────────────────────────────────────────────────────────────────────

def load_bars(years: int) -> dict:
    import time
    import datetime as _dt
    import yfinance as yf
    start = (_dt.date.today() - _dt.timedelta(days=int(years * 365.25 + 400))).isoformat()
    out = {}
    for sym, tk in PAIRS.items():
        df = None
        for attempt in range(3):
            try:
                df = yf.download(tk, start=start, interval="1d",
                                 progress=False, auto_adjust=True)
                if df is not None and len(df) >= srsi.MIN_BARS + 30:
                    break
            except Exception:
                df = None
            time.sleep(1.5)
        if df is None or len(df) < srsi.MIN_BARS + 30:
            print(f"  {sym}: only {0 if df is None else len(df)} bars - skipped")
            continue
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df[["Open", "High", "Low", "Close"]].dropna()
        df.attrs["symbol"] = sym
        out[sym] = df
    return out


# ── one trade, simulated forward under a chosen stop-management policy ───────

def simulate_trade(df: pd.DataFrame, i_entry: int, direction: str,
                   entry_px: float, atr_entry: float, policy: str,
                   cost_r: float) -> dict:
    is_long   = direction == "Buy"
    R         = ATR_MULT * atr_entry
    init_stop = entry_px - R if is_long else entry_px + R
    tp        = entry_px + DEFAULT_TP_RR * R if is_long else entry_px - DEFAULT_TP_RR * R

    pos = {
        "direction": direction, "entry_price": entry_px,
        "stop_price": init_stop, "initial_stop_price": init_stop,
        "atr_at_entry": atr_entry, "tp_price": tp,
        "entry_date": df.index[i_entry].date().isoformat(),
    }
    breakeven_done = False
    mfe_r = 0.0

    for t in range(i_entry + 1, len(df)):
        bar   = df.iloc[t]
        hi, lo, cl = float(bar["High"]), float(bar["Low"]), float(bar["Close"])
        window = df.iloc[: t + 1]
        cal_days = (df.index[t].date() - df.index[i_entry].date()).days

        # running max favourable excursion (in R)
        fav = (hi - entry_px) if is_long else (entry_px - lo)
        mfe_r = max(mfe_r, fav / R)

        # ---- stop tightening ----
        if policy == "ladder":
            tgt = runner._profit_ladder_target_stop(pos, window, "rsi")
            if tgt is not None:
                pos["stop_price"] = max(pos["stop_price"], tgt) if is_long \
                                    else min(pos["stop_price"], tgt)
        else:  # current
            atr_now = float(srsi._atr(window["High"], window["Low"], window["Close"]).iloc[-1])
            new_stop = srsi.trailing_stop_update(pos["stop_price"], cl, atr_now, direction)
            if new_stop > 0:
                pos["stop_price"] = new_stop
            if not breakeven_done:
                prof = (cl - entry_px) if is_long else (entry_px - cl)
                if prof >= BREAKEVEN_ATR * atr_entry:
                    pos["stop_price"] = (max(pos["stop_price"], entry_px) if is_long
                                         else min(pos["stop_price"], entry_px))
                    breakeven_done = True

        stop_px = pos["stop_price"]

        # ---- exits, conservative intrabar order: stop before TP ----
        stop_hit = (lo <= stop_px) if is_long else (hi >= stop_px)
        tp_hit   = (hi >= tp)      if is_long else (lo <= tp)
        exit_flag, reason = srsi.should_exit(pos, window, cal_days)

        if stop_hit:
            at_initial = (abs(stop_px - init_stop) < 1e-9)
            return _close(entry_px, stop_px, R, is_long, mfe_r, cost_r,
                          "hard_stop" if at_initial else "trail/lock", cal_days)
        if tp_hit:
            return _close(entry_px, tp, R, is_long, mfe_r, cost_r, "tp_2R", cal_days)
        if exit_flag:
            # should_exit already ruled out the stop above (its hard_stop path
            # needs lo<=stop, handled) -> this is rsi_recovery or time_stop
            return _close(entry_px, cl, R, is_long, mfe_r, cost_r,
                          reason.split()[0], cal_days)

    # ran out of data -> mark to last close (open trade at series end)
    return _close(entry_px, float(df.iloc[-1]["Close"]), R, is_long, mfe_r,
                  cost_r, "eod_open", len(df) - 1 - i_entry)


def _close(entry, exit_px, R, is_long, mfe_r, cost_r, reason, held):
    raw_r = ((exit_px - entry) if is_long else (entry - exit_px)) / R
    net_r = raw_r - cost_r
    return {"r": net_r, "raw_r": raw_r, "mfe_r": mfe_r,
            "giveback_r": max(0.0, mfe_r - raw_r), "reason": reason, "held": held}


# ── run one policy across the whole universe ────────────────────────────────

def run_policy(bars: dict, policy: str, cost_r: float) -> list:
    trades = []
    for sym, df in bars.items():
        rsi_s  = srsi._rsi(df["Close"])
        ema_s  = srsi._ema(df["Close"], srsi.TREND_EMA)
        atr_s  = srsi._atr(df["High"], df["Low"], df["Close"])
        open_until = -1
        for i in range(srsi.MIN_BARS, len(df) - 1):
            if i <= open_until:
                continue
            rsi_i, ema_i = float(rsi_s.iloc[i]), float(ema_s.iloc[i])
            atr_i, close_i = float(atr_s.iloc[i]), float(df["Close"].iloc[i])
            if np.isnan(rsi_i) or np.isnan(ema_i) or atr_i <= 0:
                continue
            direction = None
            if close_i > ema_i and rsi_i <= srsi.RSI_OVERSOLD:
                direction = "Buy"
            elif close_i < ema_i and rsi_i >= srsi.RSI_OVERBOUGHT:
                direction = "Sell"
            if direction is None:
                continue
            tr = simulate_trade(df, i, direction, close_i, atr_i, policy, cost_r)
            tr["symbol"] = sym
            trades.append(tr)
            open_until = i + tr["held"]
    return trades


# ── stats ──────────────────────────────────────────────────────────────────

def stats(trades: list) -> dict:
    if not trades:
        return {}
    rs = np.array([t["r"] for t in trades])
    wins = rs[rs > 0]
    losses = rs[rs <= 0]
    gb = np.array([t["giveback_r"] for t in trades if t["raw_r"] > 0])
    eq = np.cumsum(rs)
    dd = np.maximum.accumulate(eq) - eq
    reasons = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    return {
        "n": len(trades),
        "win_rate": len(wins) / len(trades),
        "avg_r": rs.mean(),
        "total_r": rs.sum(),
        "median_r": float(np.median(rs)),
        "avg_win": wins.mean() if len(wins) else 0.0,
        "avg_loss": losses.mean() if len(losses) else 0.0,
        "profit_factor": (wins.sum() / -losses.sum()) if losses.sum() < 0 else float("inf"),
        "max_dd_r": dd.max(),
        "avg_giveback_r": gb.mean() if len(gb) else 0.0,
        "avg_mfe_r": np.mean([t["mfe_r"] for t in trades]),
        "avg_held": np.mean([t["held"] for t in trades]),
        "reasons": reasons,
    }


def _fmt(s: dict) -> str:
    return (f"  trades           {s['n']}\n"
            f"  win rate         {s['win_rate']*100:5.1f}%\n"
            f"  avg R / trade    {s['avg_r']:+.3f}\n"
            f"  total R          {s['total_r']:+.1f}\n"
            f"  median R         {s['median_r']:+.3f}\n"
            f"  avg win / loss   {s['avg_win']:+.2f} / {s['avg_loss']:+.2f}\n"
            f"  profit factor    {s['profit_factor']:.2f}\n"
            f"  max drawdown     {s['max_dd_r']:.1f} R\n"
            f"  avg give-back    {s['avg_giveback_r']:.3f} R   (avg MFE {s['avg_mfe_r']:.2f} R)\n"
            f"  avg days held    {s['avg_held']:.1f}\n"
            f"  exits            {s['reasons']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=10)
    ap.add_argument("--cost-r", type=float, default=0.03,
                    help="round-trip cost as a fraction of R (spread+commission), default 0.03")
    args = ap.parse_args()

    print(f"Loading {args.years}y daily bars for {len(PAIRS)} pairs (yfinance)...")
    bars = load_bars(args.years)
    if not bars:
        print("No data. Aborting."); sys.exit(1)
    span = f"{min(d.index[0] for d in bars.values()).date()} -> {max(d.index[-1] for d in bars.values()).date()}"
    print(f"{len(bars)} pairs, {span}, cost = {args.cost_r:.3f} R/trade\n")

    cur = run_policy(bars, "current", args.cost_r)
    lad = run_policy(bars, "ladder", args.cost_r)
    sc, sl = stats(cur), stats(lad)

    print("=" * 64)
    print("CURRENT  (1.5xATR trail from day 1 + one-shot breakeven->entry)")
    print("=" * 64)
    print(_fmt(sc))
    print("\n" + "=" * 64)
    print("LADDER   (0.75R->BE+0.1R, 1.0R->+0.5R, 1.25R->1xATR trail)")
    print("=" * 64)
    print(_fmt(sl))

    print("\n" + "=" * 64)
    print("DELTA  (ladder − current)")
    print("=" * 64)
    print(f"  win rate       {(sl['win_rate']-sc['win_rate'])*100:+.1f} pp")
    print(f"  avg R / trade  {sl['avg_r']-sc['avg_r']:+.3f}")
    print(f"  total R        {sl['total_r']-sc['total_r']:+.1f}")
    print(f"  profit factor  {sl['profit_factor']-sc['profit_factor']:+.2f}")
    print(f"  max drawdown   {sl['max_dd_r']-sc['max_dd_r']:+.1f} R")
    print(f"  avg give-back  {sl['avg_giveback_r']-sc['avg_giveback_r']:+.3f} R")
    print("\nNote: same entries for both; daily bars; stop assumed hit before TP")
    print("within a bar. A design-hypothesis test, not a promise — re-run with")
    print("--cost-r matched to the real per-lot Saxo cost before deciding.")


if __name__ == "__main__":
    main()
