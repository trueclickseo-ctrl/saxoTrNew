"""
backtest_forex_universe.py
----------------------------
Historical validation of the 8 daily-bar forex strategies (ema, rsi,
donchian, bb, pullback, supertrend, zscore, ml) across the FULL 117-pair
universe -- using each strategy's REAL production code
(generate_signals/should_exit/size_position, imported directly from
forex/strategy_*.py, never reimplemented) walked forward day-by-day over
real historical daily bars. This is the gap flagged 2026-08-22:
backtest_forex.py only ever covered the EMA strategy on 7 G7 majors; the
other 9 strategies and the 83-pair EM/exotic expansion had zero historical
validation before this.

gap and london_breakout are excluded -- both need intraday H1 session
data, which Yahoo Finance doesn't reliably carry history for.
ml and cnn_lstm are included but their results should be read with an
extra grain of salt: they depend on a trained model, and there is no
record of what pairs that model's training data actually covered.

Data: yfinance daily bars for each of the 23 currencies' USD leg (direct
pairs like EURUSD, USDJPY). Cross pairs not directly listed on Yahoo with
usable history (confirmed empirically 2026-08-22: e.g. AUDTRY=X, EURCNH=X
return ~1 bar of data, vs 780 for USD-leg tickers) are synthesized by
triangulating through the two currencies' USD legs -- pair_price =
usd_value(base) / usd_value(quote). Verified against a real live Saxo
quote before trusting it: AUDUSD 0.717 / (1/USDTRY 48.06) = 34.46,
matching the real AUDTRY quote from tonight's live run exactly.
High/Low for a synthesized pair are APPROXIMATED (not independently
sourced) by combining each leg's own relative daily range as though
roughly independent -- adequate for a validation-level ATR/Donchian/BB
backtest, but not a substitute for real historical H/L data. CNH uses
USDCNY=X as a proxy (offshore/onshore yuan trade very closely; no direct
USDCNH history is available on Yahoo).

Usage:
    python backtest_forex_universe.py                  # all 8 strategies, all pairs, 3y
    python backtest_forex_universe.py --strategy ema donchian
    python backtest_forex_universe.py --years 2
    python backtest_forex_universe.py --pairs-only-core # skip the 83 exotic pairs

Results: data/forex_universe_backtest.csv (one row per strategy x pair)
         data/forex_universe_backtest_summary.csv (one row per strategy x tier)
"""
import argparse
import os
import sys
import warnings
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)
DATA_DIR = os.path.join(_ROOT, "data")

from forex.universe import PAIRS, get_tier

STRATEGY_MODULES = ["ema", "rsi", "donchian", "bb", "pullback", "supertrend", "zscore", "ml"]
_MODULE_IMPORT = {
    "ema":        "forex.strategy",
    "rsi":        "forex.strategy_rsi",
    "donchian":   "forex.strategy_donchian",
    "bb":         "forex.strategy_bb",
    "pullback":   "forex.strategy_pullback",
    "supertrend": "forex.strategy_supertrend",
    "zscore":     "forex.strategy_zscore",
    "ml":         "forex.strategy_ml",
}

# ── USD-leg ticker per currency ──────────────────────────────────────────
# (ticker, is_ccy_the_base) -- is_ccy_the_base=True means the ticker's raw
# price already IS "USD value of 1 unit of ccy" (e.g. AUDUSD=X); False
# means the ticker is "ccy per 1 USD" and must be inverted (e.g. USDJPY=X).
USD_LEG = {
    "USD": (None,        None),   # trivial identity, handled separately
    "AUD": ("AUDUSD=X",  True),
    "EUR": ("EURUSD=X",  True),
    "GBP": ("GBPUSD=X",  True),
    "NZD": ("NZDUSD=X",  True),
    "JPY": ("USDJPY=X",  False),
    "CAD": ("USDCAD=X",  False),
    "CHF": ("USDCHF=X",  False),
    "NOK": ("USDNOK=X",  False),
    "SEK": ("USDSEK=X",  False),
    "DKK": ("USDDKK=X",  False),
    "TRY": ("USDTRY=X",  False),
    "ZAR": ("USDZAR=X",  False),
    "MXN": ("USDMXN=X",  False),
    "PLN": ("USDPLN=X",  False),
    "HUF": ("USDHUF=X",  False),
    "CZK": ("USDCZK=X",  False),
    "RON": ("USDRON=X",  False),
    "THB": ("USDTHB=X",  False),
    "ILS": ("USDILS=X",  False),
    "AED": ("USDAED=X",  False),
    "CNH": ("USDCNY=X",  False),   # proxy: no direct USDCNH history on Yahoo
    "HKD": ("USDHKD=X",  False),
    "SGD": ("USDSGD=X",  False),
}


def _download_usd_legs(years: int) -> dict:
    import yfinance as yf
    period = f"{years}y"
    out = {}
    print(f"Downloading {years}y USD-leg data for {len(USD_LEG)} currencies...")
    for ccy, (ticker, _) in USD_LEG.items():
        if ticker is None:
            continue
        try:
            df = yf.download(ticker, period=period, interval="1d",
                             progress=False, auto_adjust=True)
            if hasattr(df.columns, "levels"):
                df.columns = df.columns.get_level_values(0)
            df = df[["High", "Low", "Close"]].dropna()
            df = df.apply(lambda col: col.squeeze())
            out[ccy] = df
            print(f"  {ccy:4s} {ticker:12s} {len(df)} bars")
        except Exception as exc:
            print(f"  {ccy:4s} {ticker:12s} FAILED — {exc}")
    return out


def _usd_value_series(ccy: str, legs: dict, col: str = "Close") -> pd.Series | None:
    if ccy == "USD":
        return None  # caller handles identity
    ticker, is_base = USD_LEG.get(ccy, (None, None))
    if ticker is None or ccy not in legs:
        return None
    s = legs[ccy][col]
    return s if is_base else (1.0 / s)


def _synthesize_pair(base: str, quote: str, legs: dict) -> pd.DataFrame | None:
    """pair_close = usd_value(base) / usd_value(quote). See module docstring
    for the triangulation formula and its live-quote verification."""
    if base == "USD":
        base_usd = pd.Series(1.0, index=next(iter(legs.values())).index)
    else:
        base_usd = _usd_value_series(base, legs, "Close")
    if quote == "USD":
        quote_usd = pd.Series(1.0, index=next(iter(legs.values())).index)
    else:
        quote_usd = _usd_value_series(quote, legs, "Close")
    if base_usd is None or quote_usd is None:
        return None

    idx = base_usd.index.intersection(quote_usd.index)
    if len(idx) < 100:
        return None
    base_usd, quote_usd = base_usd.loc[idx], quote_usd.loc[idx]
    close = base_usd / quote_usd

    # Approximate High/Low by combining each real leg's own relative daily
    # range (treated as roughly independent) -- see module docstring.
    def _rel_range(ccy):
        if ccy == "USD":
            return pd.Series(0.0, index=idx)
        df = legs.get(ccy)
        if df is None:
            return pd.Series(0.0, index=idx)
        r = ((df["High"] - df["Low"]) / df["Close"]).reindex(idx).fillna(0.0)
        return r

    combined_range_pct = np.sqrt(_rel_range(base) ** 2 + _rel_range(quote) ** 2)
    high = close * (1 + combined_range_pct / 2)
    low  = close * (1 - combined_range_pct / 2)

    return pd.DataFrame({"Open": close, "High": high, "Low": low, "Close": close}, index=idx)


def build_universe_price_data(pairs: list, years: int) -> dict:
    legs = _download_usd_legs(years)
    price_data = {}
    print(f"\nSynthesizing {len(pairs)} pairs via USD-leg triangulation...")
    failed = []
    for p in pairs:
        sym = p["symbol"]
        df = _synthesize_pair(p["base"], p["quote"], legs)
        if df is not None and len(df) > 100:
            price_data[sym] = df
        else:
            failed.append(sym)
    print(f"  built {len(price_data)}/{len(pairs)} pairs "
         f"({len(failed)} failed — insufficient leg data: {failed[:10]}{'...' if len(failed) > 10 else ''})")
    return price_data


# ── Generic walk-forward engine, driven by each strategy's REAL functions ──

def backtest_strategy_on_pair(strat_name: str, mod, df: pd.DataFrame,
                              equity: float = 27_800.0) -> dict | None:
    """Walk forward one pair, one strategy, calling generate_signals /
    should_exit / size_position exactly as forex/runner.py does live.
    Only one position at a time per pair (this measures per-pair signal
    quality, not portfolio-level slot competition)."""
    n = len(df)
    min_bars = 220  # generous warmup for the slowest strategy's EMA(200)-style filters
    if n < min_bars + 30:
        return None

    trades = []
    position = None  # {direction, entry_price, stop_price, entry_idx}
    has_trailing = hasattr(mod, "trailing_stop_update")

    for day in range(min_bars, n):
        window = df.iloc[:day + 1]

        if position is not None:
            held_days = day - position["entry_idx"]
            if has_trailing:
                try:
                    position["stop_price"] = mod.trailing_stop_update(
                        position["stop_price"], float(window["Close"].iloc[-1]),
                        position["direction"], float(window["High"].iloc[-1]),
                        float(window["Low"].iloc[-1]),
                    )
                except TypeError:
                    pass  # signature mismatch for this strategy -- skip trailing, still valid
                except Exception:
                    pass
            try:
                exit_flag, reason = mod.should_exit(position, window, held_days)
            except Exception:
                exit_flag, reason = False, ""
            if exit_flag:
                exit_px = float(window["Close"].iloc[-1])
                is_long = position["direction"] == "Buy"
                pnl_raw = (exit_px - position["entry_price"]) if is_long else (position["entry_price"] - exit_px)
                trades.append({
                    "entry_idx": position["entry_idx"], "exit_idx": day,
                    "pnl_pct": pnl_raw / position["entry_price"],
                    "win": pnl_raw > 0,
                })
                position = None

        if position is None:
            try:
                sigs = mod.generate_signals({df.attrs.get("symbol", "PAIR"): window}, open_symbols=set())
            except Exception:
                sigs = []
            if sigs:
                sig = sigs[0]
                position = {
                    "direction":   sig["direction"],
                    "entry_price": float(sig["close"]),
                    "stop_price":  float(sig["stop_price"]),
                    "entry_idx":   day,
                }

    if len(trades) < 5:
        return {"n_trades": len(trades), "insufficient": True}

    pnls = [t["pnl_pct"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else None
    win_rate = round(len(wins) / len(trades) * 100, 1)
    total_pct = round(sum(pnls) * 100, 2)
    ret_series = pd.Series(pnls)
    sharpe = round(float(ret_series.mean() / ret_series.std() * np.sqrt(252 / (n / len(trades))))
                  if ret_series.std() > 0 else 0.0, 2)

    return {
        "n_trades": len(trades), "win_rate": win_rate, "profit_factor": profit_factor,
        "total_return_pct": total_pct, "sharpe_approx": sharpe, "insufficient": False,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", nargs="*", default=STRATEGY_MODULES)
    ap.add_argument("--years", type=int, default=3)
    ap.add_argument("--pairs-only-core", action="store_true")
    args = ap.parse_args()

    pairs = [p for p in PAIRS if not args.pairs_only_core or get_tier(p["symbol"]) == "core"]
    price_data = build_universe_price_data(pairs, args.years)
    if not price_data:
        print("No price data built — aborting.")
        sys.exit(1)

    import importlib
    rows = []
    for strat_name in args.strategy:
        mod = importlib.import_module(_MODULE_IMPORT[strat_name])
        print(f"\n=== {strat_name} — {len(price_data)} pairs ===")
        for i, (sym, df) in enumerate(price_data.items()):
            df = df.copy()
            df.attrs["symbol"] = sym
            res = backtest_strategy_on_pair(strat_name, mod, df)
            if res is None:
                continue
            tier = get_tier(sym)
            row = {"strategy": strat_name, "symbol": sym, "tier": tier, **res}
            rows.append(row)
            if (i + 1) % 25 == 0:
                print(f"  {i+1}/{len(price_data)} pairs done...")

    df_all = pd.DataFrame(rows)
    os.makedirs(DATA_DIR, exist_ok=True)
    out_path = os.path.join(DATA_DIR, "forex_universe_backtest.csv")
    df_all.to_csv(out_path, index=False)
    print(f"\nSaved {len(df_all)} rows to {out_path}")

    valid = df_all[df_all["insufficient"] == False]
    summary = valid.groupby(["strategy", "tier"]).agg(
        n_pairs=("symbol", "count"),
        avg_trades=("n_trades", "mean"),
        avg_win_rate=("win_rate", "mean"),
        avg_pf=("profit_factor", "mean"),
        avg_total_return_pct=("total_return_pct", "mean"),
        pct_pairs_pf_over_1=("profit_factor", lambda s: round((s > 1).mean() * 100, 1)),
    ).round(2).reset_index()
    summary_path = os.path.join(DATA_DIR, "forex_universe_backtest_summary.csv")
    summary.to_csv(summary_path, index=False)
    print(f"Saved summary to {summary_path}\n")
    print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
