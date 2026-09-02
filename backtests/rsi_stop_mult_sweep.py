"""
backtests/rsi_stop_mult_sweep.py
--------------------------------
Sweeps the RSI(2) book's INITIAL hard-stop width (srsi.ATR_STOP_MULT) to
answer: "does a tighter stop policy help the LIVE RSI accounts?"

Both LIVE accounts (SEK + EUR) run forex.strategy_rsi at a FIXED EUR risk
per trade (runner.RSI_LIVE_FIXED_RISK_EUR = 45). So 1 R == EUR45 no matter
how wide the stop is -- which makes avg-R directly comparable across
multipliers as avg EUR P&L / trade.

For each multiplier m the script monkey-patches srsi.ATR_STOP_MULT = m
(so the initial stop AND the 1.5x-style trail band AND the 2R take-profit
all scale together), replays the REAL entry rule on 10y daily bars, and
runs the REAL exit stack (srsi.trailing_stop_update + one-shot breakeven
+ srsi.should_exit).

Transaction cost is modelled per-trade, not as a flat R:
    cost_eur = flat_commission (5.18) + (spread + slippage) * notional
    notional = risk_eur * entry / (m * atr)      # fixed-risk sizing
so a tighter stop => bigger position => a bit more spread cost.

Yahoo daily bars -- sanctioned for backtests only (Saxo-Only Live Prices
standing rule). Not a promise; a design-hypothesis test.

Usage:
    python backtests/rsi_stop_mult_sweep.py
    python backtests/rsi_stop_mult_sweep.py --years 15 --mults 1.0,1.25,1.5,1.75,2.0
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

PAIRS = {
    "EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "USDJPY=X",
    "AUDUSD": "AUDUSD=X", "USDCAD": "USDCAD=X", "NZDUSD": "NZDUSD=X",
    "USDCHF": "USDCHF=X", "EURGBP": "EURGBP=X", "EURJPY": "EURJPY=X",
    "GBPJPY": "GBPJPY=X", "AUDJPY": "AUDJPY=X", "EURAUD": "EURAUD=X",
    "EURCAD": "EURCAD=X", "EURCHF": "EURCHF=X", "GBPCHF": "GBPCHF=X",
    "GBPCAD": "GBPCAD=X", "GBPAUD": "GBPAUD=X",
}

RISK_EUR      = runner.RSI_LIVE_FIXED_RISK_EUR or 45.0
FLAT_COMM_EUR = 5.18                       # memory: Saxo FX flat ~EUR5.18
SPREAD_PIPS   = 0.8                        # representative HIGH_VOLUME major
SLIP_PIPS     = runner.RSI_LIVE_SLIPPAGE_PIPS   # 0.5 round-trip
DEFAULT_TP_RR = runner.DEFAULT_TP_RR       # 2.0
BREAKEVEN_ATR = runner.BREAKEVEN_THRESHOLD_ATR  # 1.0


def load_bars(years: int) -> dict:
    import time, datetime as _dt
    import yfinance as yf
    start = (_dt.date.today() - _dt.timedelta(days=int(years * 365.25 + 400))).isoformat()
    out = {}
    for sym, tk in PAIRS.items():
        df = None
        for _ in range(3):
            try:
                df = yf.download(tk, start=start, interval="1d",
                                 progress=False, auto_adjust=True)
                if df is not None and len(df) >= srsi.MIN_BARS + 30:
                    break
            except Exception:
                df = None
            time.sleep(1.5)
        if df is None or len(df) < srsi.MIN_BARS + 30:
            print(f"  {sym}: insufficient bars - skipped")
            continue
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df[["Open", "High", "Low", "Close"]].dropna()
        out[sym] = df
    return out


def _cost_r(entry_px: float, atr_entry: float, mult: float, quote_ccy: str) -> float:
    """Round-trip transaction cost of one fixed-risk LIVE trade, in R."""
    pip = 0.01 if quote_ccy == "JPY" else 0.0001
    stop_dist = mult * atr_entry
    notional_eur = RISK_EUR * entry_px / stop_dist          # fixed-risk sizing
    cost_frac = ((SPREAD_PIPS + SLIP_PIPS) * pip) / entry_px
    cost_eur = FLAT_COMM_EUR + cost_frac * notional_eur
    return cost_eur / RISK_EUR


def _quote_ccy(sym: str) -> str:
    return sym[3:]


def simulate_trade(df, i_entry, direction, entry_px, atr_entry, mult, sym) -> dict:
    is_long = direction == "Buy"
    R = mult * atr_entry
    init_stop = entry_px - R if is_long else entry_px + R
    tp = entry_px + DEFAULT_TP_RR * R if is_long else entry_px - DEFAULT_TP_RR * R
    pos = {
        "direction": direction, "entry_price": entry_px,
        "stop_price": init_stop, "initial_stop_price": init_stop,
        "atr_at_entry": atr_entry, "tp_price": tp,
        "entry_date": df.index[i_entry].date().isoformat(),
    }
    breakeven_done = False
    mfe_r = 0.0
    cost_r = _cost_r(entry_px, atr_entry, mult, _quote_ccy(sym))

    for t in range(i_entry + 1, len(df)):
        bar = df.iloc[t]
        hi, lo, cl = float(bar["High"]), float(bar["Low"]), float(bar["Close"])
        window = df.iloc[: t + 1]
        cal_days = (df.index[t].date() - df.index[i_entry].date()).days

        fav = (hi - entry_px) if is_long else (entry_px - lo)
        mfe_r = max(mfe_r, fav / R)

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
        stop_hit = (lo <= stop_px) if is_long else (hi >= stop_px)
        tp_hit = (hi >= tp) if is_long else (lo <= tp)
        exit_flag, reason = srsi.should_exit(pos, window, cal_days)

        if stop_hit:
            at_initial = abs(stop_px - init_stop) < 1e-9
            return _close(entry_px, stop_px, R, is_long, mfe_r, cost_r,
                          "hard_stop" if at_initial else "trail/lock", cal_days)
        if tp_hit:
            return _close(entry_px, tp, R, is_long, mfe_r, cost_r, "tp_2R", cal_days)
        if exit_flag:
            return _close(entry_px, cl, R, is_long, mfe_r, cost_r,
                          reason.split()[0], cal_days)

    return _close(entry_px, float(df.iloc[-1]["Close"]), R, is_long, mfe_r,
                  cost_r, "eod_open", len(df) - 1 - i_entry)


def _close(entry, exit_px, R, is_long, mfe_r, cost_r, reason, held) -> dict:
    raw_r = ((exit_px - entry) if is_long else (entry - exit_px)) / R
    return {"r": raw_r - cost_r, "raw_r": raw_r, "mfe_r": mfe_r,
            "giveback_r": max(0.0, mfe_r - raw_r), "reason": reason, "held": held,
            "cost_r": cost_r}


def run_mult(bars: dict, mult: float) -> list:
    trades = []
    for sym, df in bars.items():
        rsi_s = srsi._rsi(df["Close"])
        ema_s = srsi._ema(df["Close"], srsi.TREND_EMA)
        atr_s = srsi._atr(df["High"], df["Low"], df["Close"])
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
            tr = simulate_trade(df, i, direction, close_i, atr_i, mult, sym)
            tr["symbol"] = sym
            trades.append(tr)
            open_until = i + tr["held"]
    return trades


def stats(trades: list) -> dict:
    rs = np.array([t["r"] for t in trades])
    wins, losses = rs[rs > 0], rs[rs <= 0]
    eq = np.cumsum(rs)
    dd = np.maximum.accumulate(eq) - eq
    gb = np.array([t["giveback_r"] for t in trades if t["raw_r"] > 0])
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
        "avg_cost_r": np.mean([t["cost_r"] for t in trades]),
        "avg_held": np.mean([t["held"] for t in trades]),
        "stopped_pct": (reasons.get("hard_stop", 0) + reasons.get("trail/lock", 0)) / len(trades),
        "reasons": reasons,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=10)
    ap.add_argument("--mults", default="1.0,1.25,1.5,1.75,2.0")
    args = ap.parse_args()
    mults = [float(x) for x in args.mults.split(",")]

    print(f"Loading {args.years}y daily bars for {len(PAIRS)} pairs (yfinance)...")
    bars = load_bars(args.years)
    if not bars:
        print("No data."); sys.exit(1)
    span = (f"{min(d.index[0] for d in bars.values()).date()} -> "
            f"{max(d.index[-1] for d in bars.values()).date()}")
    print(f"{len(bars)} pairs, {span}")
    print(f"1 R = EUR{RISK_EUR:.0f} (fixed LIVE RSI risk). baseline mult = {srsi.ATR_STOP_MULT}\n")

    orig = srsi.ATR_STOP_MULT
    rows = []
    try:
        for m in mults:
            srsi.ATR_STOP_MULT = m
            s = stats(run_mult(bars, m))
            s["mult"] = m
            rows.append(s)
    finally:
        srsi.ATR_STOP_MULT = orig

    hdr = (f"{'mult':>5} {'n':>5} {'win%':>6} {'stop%':>6} {'avgR':>7} {'medR':>7} "
           f"{'totR':>8} {'EURtot':>9} {'PF':>5} {'ddR':>7} {'gbR':>6} {'costR':>6} {'held':>5}")
    print(hdr)
    print("-" * len(hdr))
    for s in rows:
        star = "  <- baseline" if abs(s["mult"] - orig) < 1e-9 else ""
        print(f"{s['mult']:>5.2f} {s['n']:>5} {s['win_rate']*100:>5.1f}% "
              f"{s['stopped_pct']*100:>5.1f}% {s['avg_r']:>+7.3f} {s['median_r']:>+7.3f} "
              f"{s['total_r']:>+8.1f} {s['total_r']*RISK_EUR:>+9.0f} {s['profit_factor']:>5.2f} "
              f"{s['max_dd_r']:>7.1f} {s['avg_giveback_r']:>6.3f} {s['avg_cost_r']:>6.3f} "
              f"{s['avg_held']:>5.1f}{star}")

    print("\nexits by multiplier:")
    for s in rows:
        print(f"  {s['mult']:.2f}: {s['reasons']}")

    print("\nNote: same entry rule for every row; daily bars; stop assumed hit")
    print("before TP within a bar. EURtot = total_R x EUR45 over the whole")
    print(f"{args.years}y sample across {len(bars)} pairs (NOT annual).")


if __name__ == "__main__":
    main()
