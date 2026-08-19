"""
backtest_futures_all.py
-----------------------
Backtests ALL SEVEN futures strategies across ALL THIRTEEN markets.

Closes audit Finding 4 (6 of 7 strategies had no backtest) and Finding 9
(8 of 13 markets had no backtest data).

WHY THIS DRIVES THE REAL MODULES
    backtest_futures.py reimplements the Donchian rules inline, so it validates
    a *copy* of the strategy rather than the code that trades. This harness
    instead calls the live modules' own generate_signals() / should_exit() /
    size_position() against progressively-truncated market data. Whatever it
    measures is what runner.py actually executes -- if a module changes, this
    tracks it automatically.

    Cost: it is slower, because each bar recomputes indicators over the slice.
    That is the right trade for a validation tool.

METHOD
    For each bar t (walking forward, no lookahead):
      1. market_data_t = {sym: df.iloc[:t+1]}  -- strategies only ever see history
      2. Check exits on open positions via the module's should_exit()
      3. Ask the module for signals; open up to the slot limit
      4. Mark equity to market

    Entries and exits both fill at the NEXT bar's open where available, so a
    signal generated on bar t's close cannot be filled at that same close.

PROXIES
    Uses the ETF proxies declared in futures/universe.py (yf_ticker). These are
    correlated stand-ins, not the CfdOnIndex / FxSpot / ContractFutures
    instruments traded live -- results are indicative of edge, not of fills.

Usage:
    python backtest_futures_all.py                 # all strategies, all markets
    python backtest_futures_all.py --strategy macd
    python backtest_futures_all.py --years 3
"""
import warnings; warnings.filterwarnings("ignore")
import os
import sys
import json
import pickle
import argparse
from datetime import date, timedelta

import numpy as np
import pandas as pd
import yfinance as yf

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)

from futures.universe import MARKETS
import futures.strategy          as strat_donchian
import futures.strategy_rsi      as strat_rsi
import futures.strategy_ema      as strat_ema
import futures.strategy_macd     as strat_macd
import futures.strategy_squeeze  as strat_squeeze
import futures.strategy_ma_cross as strat_ma_cross
import futures.strategy_trend_ma as strat_trend_ma

STRATEGIES = {
    "donchian": strat_donchian,
    "rsi":      strat_rsi,
    "ema":      strat_ema,
    "macd":     strat_macd,
    "squeeze":  strat_squeeze,
    "ma_cross": strat_ma_cross,
    "trend_ma": strat_trend_ma,
}

DATA_DIR  = os.path.join(_ROOT, "data")
CACHE_PKL = os.path.join(DATA_DIR, "backtest_futures_all_cache.pkl")
OUT_JSON  = os.path.join(DATA_DIR, "futures_all_results.json")

INITIAL_EQUITY = 100_000.0   # USD notional
COMMISSION_PCT = 0.0005      # per side
SLOTS_PER_STRATEGY = 5

# Pass/fail thresholds — same bar backtest_futures.py holds Donchian to.
TH_SHARPE, TH_WR, TH_DD, TH_N = 0.70, 0.35, 0.30, 30


def load_prices(years: int) -> dict[str, pd.DataFrame]:
    cache_key = f"{years}y"
    if os.path.exists(CACHE_PKL):
        try:
            blob = pickle.load(open(CACHE_PKL, "rb"))
            if blob.get("key") == cache_key:
                print(f"[cache] {len(blob['data'])} markets")
                return blob["data"]
        except Exception:
            pass
    start = (date.today() - timedelta(days=years * 365 + 400)).isoformat()
    data = {}
    print(f"Downloading {len(MARKETS)} markets from {start} ...")
    for m in MARKETS:
        sym, tk = m["symbol"], m["yf_ticker"]
        try:
            df = yf.download(tk, start=start, auto_adjust=True, progress=False)
            if df is None or df.empty:
                print(f"  {sym:<6} ({tk}) NO DATA — excluded")
                continue
            df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
            df = df[["Open", "High", "Low", "Close"]].dropna()
            data[sym] = df
            print(f"  {sym:<6} ({tk:<5}) {len(df)} bars")
        except Exception as e:
            print(f"  {sym:<6} ({tk}) FAILED: {e}")
    os.makedirs(DATA_DIR, exist_ok=True)
    pickle.dump({"key": cache_key, "data": data}, open(CACHE_PKL, "wb"))
    return data


def _contract_size(sym: str) -> float:
    for m in MARKETS:
        if m["symbol"] == sym:
            return float(m.get("contract_size", 1))
    return 1.0


def simulate(mod, prices: dict[str, pd.DataFrame], warmup: int = 260,
             fill: str = "open", risk_guard: bool = True) -> dict:
    """Walk-forward simulation driving the module's own signal/exit functions.

    fill="open"  -- fill at the NEXT bar's open (realistic; default)
    fill="close" -- fill at the signal bar's close (optimistic; matches the
                    original backtest_futures.py, useful for comparison)
    risk_guard   -- apply runner.py's MAX_RISK_OVERSHOOT skip
    """
    index = sorted(set().union(*[set(df.index) for df in prices.values()]))
    if len(index) <= warmup + 10:
        return {}

    equity   = INITIAL_EQUITY
    open_pos = {}          # sym -> dict
    curve    = []
    trades   = []
    csize    = {s: _contract_size(s) for s in prices}

    for i in range(warmup, len(index) - 1):
        today, nxt = index[i], index[i + 1]

        # Strategies only ever see bars up to and including `today`.
        md = {}
        for sym, df in prices.items():
            sl = df.loc[:today]
            if len(sl) >= 30:
                md[sym] = sl

        # ── Exits ─────────────────────────────────────────────────────────
        for sym in list(open_pos):
            pos = open_pos[sym]
            df  = md.get(sym)
            if df is None:
                continue
            held = (today - pos["entry_date"]).days
            try:
                do_exit, reason = mod.should_exit(pos, df, held)
            except Exception:
                do_exit, reason = False, ""
            if not do_exit:
                continue
            src = prices[sym]
            if fill == "close" or nxt not in src.index:
                px_fill = float(df["Close"].iloc[-1])
            else:
                px_fill = float(src.loc[nxt, "Open"])
            sign = 1 if pos["direction"] == "Buy" else -1
            pnl  = (px_fill - pos["entry_price"]) * sign * pos["qty"] * csize[sym]
            pnl -= COMMISSION_PCT * abs(px_fill * pos["qty"] * csize[sym])
            equity += pnl
            trades.append({"symbol": sym, "pnl": pnl, "held": held,
                           "direction": pos["direction"], "reason": reason})
            del open_pos[sym]

        # ── Entries ───────────────────────────────────────────────────────
        free = SLOTS_PER_STRATEGY - len(open_pos)
        if free > 0 and equity > 0:
            try:
                sigs = mod.generate_signals(md, open_symbols=set(open_pos))
            except Exception:
                sigs = []
            for s in sigs[:free]:
                sym = s["symbol"]
                if sym in open_pos or sym not in prices:
                    continue
                atr = float(s.get("atr", 0) or 0)
                if atr <= 0:
                    continue
                qty = mod.size_position(equity, atr, csize[sym])
                # Mirror runner.py's MAX_RISK_OVERSHOOT guard so the backtest
                # measures what live will actually take.
                stop_mult = getattr(mod, "ATR_STOP_MULT", 2.0)
                risk_pct  = getattr(mod, "RISK_PCT", 0.01)
                if risk_guard and stop_mult * atr * csize[sym] > equity * risk_pct * 1.5:
                    continue
                src  = prices[sym]
                if fill == "close" or nxt not in src.index:
                    px_fill = float(s["close"])
                else:
                    px_fill = float(src.loc[nxt, "Open"])
                equity -= COMMISSION_PCT * abs(px_fill * qty * csize[sym])
                open_pos[sym] = {
                    "direction":   s["direction"],
                    "entry_price": px_fill,
                    "stop_price":  float(s.get("stop_price", 0) or 0),
                    "qty":         qty,
                    "entry_date":  today,
                }

        # ── Mark to market ────────────────────────────────────────────────
        unreal = 0.0
        for sym, pos in open_pos.items():
            df = md.get(sym)
            if df is None:
                continue
            px   = float(df["Close"].iloc[-1])
            sign = 1 if pos["direction"] == "Buy" else -1
            unreal += (px - pos["entry_price"]) * sign * pos["qty"] * csize[sym]
        curve.append(equity + unreal)

    if len(curve) < 30 or not trades:
        return {"n_trades": len(trades), "sharpe": 0, "win_rate": 0,
                "max_dd": 0, "cagr": 0, "final": equity, "insufficient": True}

    eq  = np.array(curve, dtype=float)
    rets = np.diff(eq) / np.where(eq[:-1] == 0, np.nan, eq[:-1])
    rets = rets[np.isfinite(rets)]
    sharpe = float(rets.mean() / rets.std() * np.sqrt(252)) if len(rets) > 1 and rets.std() > 0 else 0.0
    yrs = len(eq) / 252
    cagr = float(((eq[-1] / eq[0]) ** (1 / yrs) - 1) * 100) if eq[0] > 0 and yrs > 0 and eq[-1] > 0 else 0.0
    peak, mdd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        mdd  = max(mdd, (peak - v) / peak if peak > 0 else 0)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    return {
        "n_trades": len(trades),
        "sharpe":   round(sharpe, 3),
        "win_rate": round(wins / len(trades) * 100, 1),
        "max_dd":   round(mdd * 100, 1),
        "cagr":     round(cagr, 1),
        "final":    round(float(eq[-1])),
        "avg_hold": round(float(np.mean([t["held"] for t in trades])), 1),
        "insufficient": False,
    }


def verdict(r: dict) -> str:
    if r.get("insufficient") or r.get("n_trades", 0) < TH_N:
        return "INSUFFICIENT"
    ok = (r["sharpe"] >= TH_SHARPE and r["win_rate"] >= TH_WR * 100
          and r["max_dd"] < TH_DD * 100)
    return "PASS" if ok else "FAIL"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default="all",
                    choices=["all"] + list(STRATEGIES.keys()))
    ap.add_argument("--years", type=int, default=5)
    ap.add_argument("--markets", default="",
                    help="Comma-separated subset, e.g. ES,NQ,GC,CL,ZB "
                         "(default: all 13). Used to compare like-for-like "
                         "against the original 5-market backtest.")
    args = ap.parse_args()

    prices = load_prices(args.years)
    if args.markets:
        keep = {m.strip().upper() for m in args.markets.split(",")}
        prices = {s: d for s, d in prices.items() if s in keep}
    if not prices:
        print("No price data — aborting.")
        return
    print(f"\n{len(prices)} markets: {', '.join(sorted(prices))}")
    print(f"Thresholds: Sharpe>={TH_SHARPE}  WR>={TH_WR*100:.0f}%  "
          f"MaxDD<{TH_DD*100:.0f}%  N>={TH_N}\n")

    names = list(STRATEGIES) if args.strategy == "all" else [args.strategy]
    hdr = "%-11s%8s%9s%8s%8s%8s%9s   %s" % (
        "strategy", "trades", "Sharpe", "WR%", "MaxDD", "CAGR", "avgHold", "verdict")
    print(hdr); print("-" * len(hdr))

    results = {}
    for name in names:
        r = simulate(STRATEGIES[name], prices)
        if not r:
            print(f"{name:<11}  (no result)")
            continue
        v = verdict(r)
        results[name] = dict(r, verdict=v)
        print("%-11s%8d%9.3f%8.1f%8.1f%8.1f%9.1f   %s" % (
            name, r["n_trades"], r["sharpe"], r["win_rate"],
            r["max_dd"], r["cagr"], r.get("avg_hold", 0), v))

    os.makedirs(DATA_DIR, exist_ok=True)
    # Tag by market count so the 5-market and 13-market runs do not overwrite
    # each other -- the comparison between them is the whole point.
    out_path = OUT_JSON.replace(".json", f"_{len(prices)}mkt.json")
    with open(out_path, "w") as f:
        json.dump({"generated": date.today().isoformat(),
                   "years": args.years,
                   "markets": sorted(prices),
                   "results": results}, f, indent=2)
    print(f"\nWritten to {out_path}")

    passed = [n for n, r in results.items() if r["verdict"] == "PASS"]
    failed = [n for n, r in results.items() if r["verdict"] == "FAIL"]
    insuf  = [n for n, r in results.items() if r["verdict"] == "INSUFFICIENT"]
    print(f"\nPASS ({len(passed)}): {', '.join(passed) or '-'}")
    print(f"FAIL ({len(failed)}): {', '.join(failed) or '-'}")
    print(f"INSUFFICIENT ({len(insuf)}): {', '.join(insuf) or '-'}")


if __name__ == "__main__":
    main()
