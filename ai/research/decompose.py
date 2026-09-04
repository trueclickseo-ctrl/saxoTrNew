"""
ai/research/decompose.py -- reusable strategy edge-decomposition harness.

Replays a strategy's REAL production functions (generate_signals /
should_exit, imported straight from forex/strategy_*.py -- never
reimplemented) over historical daily bars, records a per-trade R-multiple
plus the entry-context features, then buckets the trades by any one
feature and keeps only buckets that are:

  1. positive in BOTH halves of the sample (not a single-regime artefact), and
  2. bootstrap 95% CI (5000x) excludes zero.

This is the 2026-09-02 methodology (docs/strategy_decomposition_2026-09-02.md)
turned into a tool. READ-ONLY: imports nothing trade-capable -- no
forex.runner, no saxo_*, no pnl_tracker, no order/position/stop calls.
Yahoo daily bars only (Saxo-Only-Live-Prices: analytics/backtest only);
R-normalised, never absolute P&L (the SIM ledger is mixed-currency).

    python -m ai.research.decompose --strategy bb --feature di_spread
    python -m ai.research.decompose --strategy rsi_trend --sweep
    python -m ai.research.decompose --strategy ema_trend --sweep --core-only --json
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import time
from dataclasses import dataclass, field, asdict

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Pure signal modules + pure indicator helpers + the pure price-synthesis
# engine. None of these can touch a live order.
from forex.strategy import _adx, _atr, _ema                      # noqa: E402
from forex.universe import PAIRS, get_tier                       # noqa: E402
from ai.regime.classifier import classify_regime                 # noqa: E402
import backtest_forex_universe as _bfu                           # noqa: E402

_DATA_DIR = os.path.join(_ROOT, "data")
CACHE_PATH = os.path.join(_DATA_DIR, "ai_research_decomp_cache.json")

# name -> pure signal module. The 5-strategy SIM roster + the base
# strategies each roster twin was decomposed from. Kept here (not imported
# from forex.runner, which is trade-capable) -- same pattern as
# backtest_forex_universe._MODULE_IMPORT.
MODULE_IMPORT = {
    "ema":            "forex.strategy",
    "ema_trend":      "forex.strategy_ema_trend",
    "rsi":            "forex.strategy_rsi",
    "rsi_trend":      "forex.strategy_rsi_trend",
    "rsi_atr":        "forex.strategy_rsi_atr",
    "bb":             "forex.strategy_bb",
    "bb_quality":     "forex.strategy_bb_quality",
    "zscore":         "forex.strategy_zscore",
    "zscore_quality": "forex.strategy_zscore_quality",
}

# The roster the analyst sweeps by default (forex v1).
ROSTER = ["rsi", "rsi_trend", "ema_trend", "bb_quality", "zscore_quality"]

# ── stocks Phase 2 ──────────────────────────────────────────────────────
# strategy snake_key -> usa_strategy class name (used by us_signals._run_one)
STOCKS_MODULE_IMPORT = {
    "us_sma_crossover": "SMAStrategy",
    "us_rsi_reversal":  "RSIStrategy",
    "us_momentum":      "MomentumStrategy",
    "us_ensemble":      "EnsembleStrategy",
}
STOCKS_ROSTER = ["us_sma_crossover", "us_rsi_reversal", "us_momentum", "us_ensemble"]
STOCKS_CACHE_PATH = os.path.join(_DATA_DIR, "ai_research_stocks_decomp_cache.json")

_MIN_BARS = 220          # warmup for EMA(200)-style filters (matches _bfu)
# The live runner fetches CHART_BARS=500 daily bars per pair and passes
# exactly that to generate_signals / should_exit. Replaying with the full
# growing history would be both O(n^2) AND unfaithful -- use the same
# trailing window the production scan sees.
_WINDOW = 500
_BOOTSTRAP_N = 5000
_MIN_BUCKET = 30         # don't gate a bucket smaller than this


# ── per-trade record ────────────────────────────────────────────────────

@dataclass
class Trade:
    strategy: str
    symbol: str
    tier: str
    direction: str
    entry_date: str
    entry_price: float
    r_price: float                 # |entry - initial stop|, the R unit
    r_multiple: float              # realised pnl / R
    mfe_r: float
    mae_r: float
    holding_bars: int
    exit_reason: str
    half: int = 0                  # 1 = first half of the sample, 2 = second
    # entry-context features
    regime: str | None = None
    di_spread: float | None = None
    adx: float | None = None
    rsi: float | None = None
    atr_pctile: float | None = None
    dist_ema200_atr: float | None = None
    dow: int | None = None
    crossover_age: int | None = None
    confidence: float | None = None    # signal confidence (stocks only)


# ── replay ─────────────────────────────────────────────────────────────

def _precompute(df: pd.DataFrame) -> dict:
    h, l, c = df["High"], df["Low"], df["Close"]
    adx_s, plus_s, minus_s = _adx(h, l, c)
    atr_s = _atr(h, l, c)
    ema200 = _ema(c, 200)
    from forex.strategy_rsi import _rsi as _rsi_fn
    rsi_s = _rsi_fn(c)
    atr_pctile = atr_s.rolling(252, min_periods=60).apply(
        lambda w: (w.rank(pct=True).iloc[-1]), raw=False)
    di_spread = (plus_s - minus_s).abs()
    # EMA(5/30) crossover age -- only meaningful for the ema family
    ema_f, ema_s = _ema(c, 5), _ema(c, 30)
    up = ema_f > ema_s
    cross = up.ne(up.shift(1))
    age = np.zeros(len(df), dtype=float)
    last = 0
    cross_vals = cross.to_numpy()
    for i in range(len(df)):
        last = 0 if cross_vals[i] else last + 1
        age[i] = last
    return {
        "adx": adx_s, "di_spread": di_spread, "atr": atr_s, "ema200": ema200,
        "rsi": rsi_s, "atr_pctile": atr_pctile,
        "crossover_age": pd.Series(age, index=df.index),
    }


def _replay_one_pair(strategy: str, mod, sym: str, df: pd.DataFrame) -> list[Trade]:
    n = len(df)
    if n < _MIN_BARS + 30:
        return []
    df = df.copy()
    df.attrs["symbol"] = sym
    ind = _precompute(df)
    tier = get_tier(sym)
    is_ema_family = strategy in ("ema", "ema_trend")
    has_trailing = hasattr(mod, "trailing_stop_update")

    out: list[Trade] = []
    pos = None
    for day in range(_MIN_BARS, n):
        window = df.iloc[max(0, day - _WINDOW + 1):day + 1]     # matches CHART_BARS
        cur_close = float(window["Close"].iloc[-1])

        if pos is not None:
            held = day - pos["entry_idx"]
            hi = float(window["High"].iloc[-1])
            lo = float(window["Low"].iloc[-1])
            if pos["direction"] == "Buy":
                pos["mfe"] = max(pos["mfe"], hi - pos["entry_price"])
                pos["mae"] = min(pos["mae"], lo - pos["entry_price"])
            else:
                pos["mfe"] = max(pos["mfe"], pos["entry_price"] - lo)
                pos["mae"] = min(pos["mae"], pos["entry_price"] - hi)
            if has_trailing:
                try:
                    pos["stop_price"] = mod.trailing_stop_update(
                        pos["stop_price"], cur_close, pos["direction"], hi, lo)
                except Exception:
                    pass
            try:
                exit_flag, reason = mod.should_exit(pos, window, held)
            except Exception:
                exit_flag, reason = False, ""
            if exit_flag:
                is_long = pos["direction"] == "Buy"
                pnl = (cur_close - pos["entry_price"]) if is_long else (pos["entry_price"] - cur_close)
                r = pos["r_price"] or np.nan
                out.append(Trade(
                    strategy=strategy, symbol=sym, tier=tier, direction=pos["direction"],
                    entry_date=str(df.index[pos["entry_idx"]])[:10],
                    entry_price=pos["entry_price"], r_price=pos["r_price"],
                    r_multiple=float(pnl / r) if r and not np.isnan(r) else 0.0,
                    mfe_r=float(pos["mfe"] / r) if r and not np.isnan(r) else 0.0,
                    mae_r=float(pos["mae"] / r) if r and not np.isnan(r) else 0.0,
                    holding_bars=held, exit_reason=str(reason or ""),
                    **pos["feat"],
                ))
                pos = None

        if pos is None:
            try:
                sigs = mod.generate_signals({sym: window}, open_symbols=set())
            except Exception:
                sigs = []
            if sigs:
                sig = sigs[0]
                entry = float(sig["close"])
                stop = float(sig["stop_price"])
                # features at the signal bar
                try:
                    reg = classify_regime(window.iloc[-140:])["label"]
                except Exception:
                    reg = None
                atrv = float(ind["atr"].iloc[day]) if not np.isnan(ind["atr"].iloc[day]) else np.nan
                feat = {
                    "regime": reg,
                    "di_spread": _f(ind["di_spread"].iloc[day]),
                    "adx": _f(ind["adx"].iloc[day]),
                    "rsi": _f(ind["rsi"].iloc[day]),
                    "atr_pctile": _f(ind["atr_pctile"].iloc[day]),
                    "dist_ema200_atr": (
                        _f((entry - ind["ema200"].iloc[day]) / atrv)
                        if atrv and not np.isnan(atrv) else None),
                    "dow": int(df.index[day].dayofweek),
                    "crossover_age": (int(ind["crossover_age"].iloc[day]) if is_ema_family else None),
                }
                pos = {
                    "direction": sig["direction"], "entry_price": entry,
                    "stop_price": stop, "entry_idx": day,
                    "r_price": abs(entry - stop), "mfe": 0.0, "mae": 0.0,
                    "feat": feat,
                }
    return out


def _f(x) -> float | None:
    try:
        v = float(x)
        return None if np.isnan(v) else v
    except (TypeError, ValueError):
        return None


def replay_trades(strategy: str, pairs: list[str] | None = None, years: int = 13,
                  price_data: dict | None = None, core_only: bool = False) -> list[Trade]:
    """Replay `strategy` across the universe. `price_data` (symbol -> OHLC
    DataFrame) can be supplied to skip the yfinance download (tests)."""
    if strategy not in MODULE_IMPORT:
        raise ValueError(f"unknown strategy {strategy!r} -- one of {sorted(MODULE_IMPORT)}")
    mod = importlib.import_module(MODULE_IMPORT[strategy])

    if price_data is None:
        pdicts = [p for p in PAIRS
                  if (pairs is None or p["symbol"] in pairs)
                  and (not core_only or get_tier(p["symbol"]) == "core")]
        price_data = _bfu.build_universe_price_data(pdicts, years)

    trades: list[Trade] = []
    for sym, df in price_data.items():
        if pairs is not None and sym not in pairs:
            continue
        trades.extend(_replay_one_pair(strategy, mod, sym, df))

    trades.sort(key=lambda t: t.entry_date)
    mid = len(trades) // 2
    for i, t in enumerate(trades):
        t.half = 1 if i < mid else 2
    return trades


# ── bucket + gate ──────────────────────────────────────────────────────

@dataclass
class Bucket:
    label: str
    n: int
    avg_r: float
    win_rate: float
    pf: float | None
    first_half_avg_r: float
    second_half_avg_r: float
    max_dd_r: float
    ci_low: float
    ci_high: float
    gate_pass: bool


@dataclass
class Verdict:
    strategy: str
    feature: str
    n_trades: int
    base_avg_r: float
    buckets: list[Bucket] = field(default_factory=list)

    @property
    def passing(self) -> list[Bucket]:
        return [b for b in self.buckets if b.gate_pass]


_DEFAULT_BUCKETS = {
    "regime": None,   # one bucket per label
    "di_spread": [("<=14", lambda v: v is not None and v <= 14),
                  ("15-25", lambda v: v is not None and 14 < v <= 25),
                  (">25", lambda v: v is not None and v > 25)],
    "adx": [("<20", lambda v: v is not None and v < 20),
            ("20-25", lambda v: v is not None and 20 <= v < 25),
            (">=25", lambda v: v is not None and v >= 25)],
    "crossover_age": [("<=3", lambda v: v is not None and v <= 3),
                      ("4-10", lambda v: v is not None and 3 < v <= 10),
                      (">10", lambda v: v is not None and v > 10)],
    "atr_pctile": [("low<=0.33", lambda v: v is not None and v <= 0.33),
                   ("mid", lambda v: v is not None and 0.33 < v <= 0.66),
                   ("high>0.66", lambda v: v is not None and v > 0.66)],
    "dow": [("Mon-Tue", lambda v: v in (0, 1)),
            ("Wed", lambda v: v == 2),
            ("Thu-Fri", lambda v: v in (3, 4))],
    "dist_ema200_atr": [("near<=1", lambda v: v is not None and abs(v) <= 1),
                        ("mid", lambda v: v is not None and 1 < abs(v) <= 3),
                        ("far>3", lambda v: v is not None and abs(v) > 3)],
    "rsi": [("oversold<30",     lambda v: v is not None and v < 30),
            ("neutral30-70",   lambda v: v is not None and 30 <= v < 70),
            ("overbought>=70", lambda v: v is not None and v >= 70)],
    # stocks-only
    "confidence": [("low<0.5",    lambda v: v is not None and v < 0.5),
                   ("mid0.5-0.75", lambda v: v is not None and 0.5 <= v < 0.75),
                   ("high>=0.75", lambda v: v is not None and v >= 0.75)],
}


def _pf(rs: list[float]) -> float | None:
    g = sum(r for r in rs if r > 0)
    b = abs(sum(r for r in rs if r <= 0))
    return round(g / b, 2) if b > 0 else None


def _max_dd_r(rs: list[float]) -> float:
    eq = 0.0
    peak = 0.0
    dd = 0.0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return round(dd, 1)


def _bootstrap_ci(rs: list[float], n: int = _BOOTSTRAP_N) -> tuple[float, float]:
    if len(rs) < 5:
        return (float("nan"), float("nan"))
    arr = np.asarray(rs, dtype=float)
    rng = np.random.default_rng(12345)          # fixed seed -> reproducible verdict
    means = rng.choice(arr, size=(n, len(arr)), replace=True).mean(axis=1)
    return (float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5)))


def _bucket_from(trades: list[Trade], feature: str, label: str, pred) -> Bucket | None:
    sel = [t for t in trades if pred(getattr(t, feature))]
    if len(sel) < _MIN_BUCKET:
        return None
    rs = [t.r_multiple for t in sel]
    h1 = [t.r_multiple for t in sel if t.half == 1]
    h2 = [t.r_multiple for t in sel if t.half == 2]
    lo, hi = _bootstrap_ci(rs)
    a1 = float(np.mean(h1)) if h1 else 0.0
    a2 = float(np.mean(h2)) if h2 else 0.0
    return Bucket(
        label=label, n=len(sel), avg_r=round(float(np.mean(rs)), 3),
        win_rate=round(sum(1 for r in rs if r > 0) / len(rs) * 100, 1),
        pf=_pf(rs), first_half_avg_r=round(a1, 3), second_half_avg_r=round(a2, 3),
        max_dd_r=_max_dd_r(rs), ci_low=round(lo, 3), ci_high=round(hi, 3),
        gate_pass=bool(a1 > 0 and a2 > 0 and lo > 0 and len(h1) >= 5 and len(h2) >= 5),
    )


def bucket_and_gate(trades: list[Trade], feature: str,
                    buckets=None) -> Verdict:
    """Split `trades` by `feature` and gate each bucket. `buckets` is an
    optional list of (label, predicate) pairs; otherwise a sensible default
    for that feature is used. `regime` always buckets one-per-label."""
    valid = [t for t in trades if getattr(t, feature, None) is not None]
    base = round(float(np.mean([t.r_multiple for t in valid])), 3) if valid else 0.0
    v = Verdict(strategy=(trades[0].strategy if trades else "?"), feature=feature,
                n_trades=len(valid), base_avg_r=base)

    if buckets is None and feature == "regime":
        labels = sorted({t.regime for t in valid if t.regime})
        buckets = [(lab, (lambda x, _l=lab: x == _l)) for lab in labels]
    elif buckets is None:
        buckets = _DEFAULT_BUCKETS.get(feature)
        if buckets is None:
            raise ValueError(f"no default bucketing for feature {feature!r}; pass `buckets`")

    for label, pred in buckets:
        b = _bucket_from(valid, feature, label, pred)
        if b is not None:
            v.buckets.append(b)
    return v


# ── sweep (what the analyst + the weekly digest call) ───────────────────

_SWEEP_FEATURES = {
    "rsi":            ["regime", "di_spread", "adx", "atr_pctile", "dow"],
    "rsi_trend":      ["regime", "di_spread", "adx", "atr_pctile", "dow"],
    "ema_trend":      ["regime", "di_spread", "crossover_age", "adx", "dist_ema200_atr"],
    "bb_quality":     ["regime", "di_spread", "adx", "atr_pctile", "dist_ema200_atr"],
    "zscore_quality": ["regime", "di_spread", "adx", "atr_pctile"],
    "ema":            ["regime", "di_spread", "crossover_age", "adx", "dist_ema200_atr"],
    "bb":             ["regime", "di_spread", "adx", "atr_pctile", "dist_ema200_atr"],
    "zscore":         ["regime", "di_spread", "adx", "atr_pctile"],
}

_STOCKS_SWEEP_FEATURES = {
    "us_sma_crossover": ["confidence", "dow", "atr_pctile", "adx"],
    "us_rsi_reversal":  ["confidence", "rsi", "atr_pctile", "dow"],
    "us_momentum":      ["confidence", "dow", "atr_pctile", "adx"],
    "us_ensemble":      ["confidence", "dow", "atr_pctile"],
}


def sweep(strategy: str, years: int = 13, core_only: bool = True,
          price_data: dict | None = None, trades: list[Trade] | None = None) -> list[Verdict]:
    """Run bucket_and_gate over the standard feature set for `strategy`."""
    if trades is None:
        trades = replay_trades(strategy, years=years, core_only=core_only, price_data=price_data)
    feats = _SWEEP_FEATURES.get(strategy, ["regime", "di_spread", "adx"])
    return [bucket_and_gate(trades, f) for f in feats]


# ── stocks replay (Phase 2) ─────────────────────────────────────────────

def _default_stocks_tickers() -> list[str]:
    """Top 50 S&P 500 components as a replay default (yfinance tickers)."""
    try:
        import atos_runner as _ar
        cand = getattr(_ar, "US_TICKERS", None) or getattr(_ar, "_UNIVERSE_TICKERS", None)
        if cand:
            return list(cand)[:100]
    except Exception:
        pass
    return [
        "AAPL", "MSFT", "AMZN", "NVDA", "GOOGL", "META", "TSLA", "UNH", "LLY", "JPM",
        "XOM", "JNJ", "V", "PG", "MA", "HD", "CVX", "MRK", "ABBV", "PEP",
        "KO", "AVGO", "BAC", "WMT", "COST", "TMO", "MCD", "CSCO", "ACN", "DHR",
        "NEE", "NKE", "PM", "T", "VZ", "BMY", "AMGN", "RTX", "HON", "INTC",
        "QCOM", "IBM", "CAT", "BA", "GE", "MMM", "DE", "AXP", "GS", "MS",
    ]


def _replay_one_stock(strategy_key: str, ticker: str, df: pd.DataFrame) -> list[Trade]:
    """Bar-by-bar replay for one US equity ticker.

    Uses the real usa_strategy generate/SELL signal and ATR stop logic.
    Avoids date.today() comparisons (uses held_days counter instead).
    """
    from atos.us_signals import _run_one, _prep_df, MAX_HOLD_DAYS, ATR_STOP_MULT, HARD_STOP_PCT

    pkg_cls = STOCKS_MODULE_IMPORT[strategy_key]
    n = len(df)
    if n < _MIN_BARS + 30:
        return []

    ind = _precompute(df)   # works on yfinance OHLCV (High/Low/Close)
    out: list[Trade] = []
    pos = None

    for day in range(_MIN_BARS, n):
        window = df.iloc[max(0, day - _WINDOW + 1):day + 1]
        hi_col = "High" if "High" in window.columns else "high"
        lo_col = "Low"  if "Low"  in window.columns else "low"
        cl_col = "Close" if "Close" in window.columns else "close"
        cur_close = float(window[cl_col].iloc[-1])

        if pos is not None:
            held = day - pos["entry_idx"]
            hi = float(window[hi_col].iloc[-1])
            lo = float(window[lo_col].iloc[-1])
            pos["mfe"] = max(pos["mfe"], hi - pos["entry_price"])
            pos["mae"] = min(pos["mae"], lo - pos["entry_price"])

            # exit checks
            exit_flag, reason = False, ""
            if cur_close <= pos["stop_price"]:
                exit_flag, reason = True, "stop"
            elif held >= MAX_HOLD_DAYS:
                exit_flag, reason = True, f"time exit {held}d"
            else:
                try:
                    wdf = _prep_df(window)
                    res = _run_one(pkg_cls, ticker, wdf)
                    if res is not None and res.signal == "SELL":
                        exit_flag, reason = True, "SELL signal"
                except Exception:
                    pass

            if exit_flag:
                pnl = cur_close - pos["entry_price"]    # always long
                r = pos["r_price"] or np.nan
                out.append(Trade(
                    strategy=strategy_key, symbol=ticker, tier="US", direction="Buy",
                    entry_date=pos["entry_date_str"], entry_price=pos["entry_price"],
                    r_price=pos["r_price"],
                    r_multiple=float(pnl / r) if r and not np.isnan(r) else 0.0,
                    mfe_r=float(pos["mfe"] / r) if r and not np.isnan(r) else 0.0,
                    mae_r=float(pos["mae"] / r) if r and not np.isnan(r) else 0.0,
                    holding_bars=held, exit_reason=str(reason),
                    regime=pos["feat"].get("regime"),
                    di_spread=pos["feat"].get("di_spread"),
                    adx=pos["feat"].get("adx"),
                    rsi=pos["feat"].get("rsi"),
                    atr_pctile=pos["feat"].get("atr_pctile"),
                    dist_ema200_atr=pos["feat"].get("dist_ema200_atr"),
                    dow=pos["feat"].get("dow"),
                    crossover_age=None,
                    confidence=pos["feat"].get("confidence"),
                ))
                pos = None

        if pos is None:
            try:
                wdf = _prep_df(window)
                result = _run_one(pkg_cls, ticker, wdf)
            except Exception:
                result = None
            if result is not None and result.signal == "BUY":
                entry = cur_close
                atrv = float(ind["atr"].iloc[day])
                atrv = None if np.isnan(atrv) else atrv
                atr_stop = (entry - ATR_STOP_MULT * atrv) if atrv else 0.0
                hard_stop = entry * (1 - HARD_STOP_PCT)
                stop = max(atr_stop, hard_stop)
                try:
                    reg = classify_regime(window.iloc[-140:])["label"]
                except Exception:
                    reg = None
                feat = {
                    "regime": reg,
                    "di_spread": _f(ind["di_spread"].iloc[day]),
                    "adx": _f(ind["adx"].iloc[day]),
                    "rsi": _f(ind["rsi"].iloc[day]),
                    "atr_pctile": _f(ind["atr_pctile"].iloc[day]),
                    "dist_ema200_atr": (
                        _f((entry - ind["ema200"].iloc[day]) / atrv) if atrv else None),
                    "dow": int(df.index[day].dayofweek),
                    "confidence": _f(getattr(result, "confidence", None)),
                }
                pos = {
                    "direction": "Buy", "entry_price": entry, "stop_price": stop,
                    "entry_idx": day, "entry_date_str": str(df.index[day])[:10],
                    "r_price": abs(entry - stop), "mfe": 0.0, "mae": 0.0,
                    "feat": feat,
                }
    return out


def replay_stock_trades(strategy_key: str, tickers: list[str] | None = None,
                        years: int = 5, price_data: dict | None = None) -> list[Trade]:
    """Replay `strategy_key` across US equity tickers. `price_data` (ticker ->
    OHLCV DataFrame) can be supplied to skip the yfinance download (tests)."""
    if strategy_key not in STOCKS_MODULE_IMPORT:
        raise ValueError(
            f"unknown stock strategy {strategy_key!r} -- one of {sorted(STOCKS_MODULE_IMPORT)}")

    if price_data is None:
        if tickers is None:
            tickers = _default_stocks_tickers()
        import yfinance as yf
        end   = pd.Timestamp.now()
        start = end - pd.Timedelta(days=int(years * 365.25) + 30)
        price_data = {}
        for tk in tickers:
            try:
                df = yf.download(tk, start=start, end=end, progress=False, auto_adjust=True)
                # yfinance may return MultiIndex columns -- flatten
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                if len(df) > _MIN_BARS:
                    price_data[tk] = df
            except Exception:
                pass

    trades: list[Trade] = []
    for ticker, df in price_data.items():
        if tickers is not None and ticker not in tickers:
            continue
        trades.extend(_replay_one_stock(strategy_key, ticker, df))

    trades.sort(key=lambda t: t.entry_date)
    mid = len(trades) // 2
    for i, t in enumerate(trades):
        t.half = 1 if i < mid else 2
    return trades


def sweep_stocks(strategy_key: str, years: int = 5,
                 tickers: list[str] | None = None,
                 price_data: dict | None = None,
                 trades: list[Trade] | None = None) -> list[Verdict]:
    """Run bucket_and_gate over the standard stock feature set."""
    if trades is None:
        trades = replay_stock_trades(strategy_key, tickers=tickers, years=years,
                                     price_data=price_data)
    feats = _STOCKS_SWEEP_FEATURES.get(strategy_key, ["confidence", "dow", "atr_pctile"])
    # only sweep features that actually have values in this replay
    active = [f for f in feats if any(getattr(t, f, None) is not None for t in trades)]
    return [bucket_and_gate(trades, f) for f in active]


def refresh_stocks_cache(strategies: list[str] | None = None, years: int = 5,
                         tickers: list[str] | None = None) -> dict:
    """Re-run sweep_stocks() for each stock strategy and write the cache."""
    strategies = strategies or STOCKS_ROSTER
    cache = _load_stocks_cache()
    for s in strategies:
        try:
            verds = sweep_stocks(s, years=years, tickers=tickers)
            cache[s] = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "years": years,
                "verdicts": [_verdict_to_dict(v) for v in verds],
            }
        except Exception as exc:
            cache[s] = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "error": str(exc)[:300]}
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        tmp = STOCKS_CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2)
        os.replace(tmp, STOCKS_CACHE_PATH)
    except Exception:
        pass
    return cache


def _load_stocks_cache() -> dict:
    try:
        with open(STOCKS_CACHE_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def cached_stock_verdicts() -> dict:
    """The last refresh_stocks_cache() output. Read-only."""
    return _load_stocks_cache()


def refresh_cache(strategies: list[str] | None = None, years: int = 13) -> dict:
    """Re-run sweep() for each strategy and persist to CACHE_PATH. Returns
    the cache dict. (Weekly job -- the yfinance replay takes a few minutes.)"""
    strategies = strategies or ROSTER
    cache = _load_cache()
    for s in strategies:
        try:
            verds = sweep(s, years=years)
            cache[s] = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "years": years,
                "verdicts": [_verdict_to_dict(v) for v in verds],
            }
        except Exception as exc:                       # pragma: no cover - defensive
            cache[s] = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "error": str(exc)[:300]}
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2)
        os.replace(tmp, CACHE_PATH)
    except Exception:
        pass
    return cache


def _load_cache() -> dict:
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def cached_verdicts() -> dict:
    """The last refresh_cache() output, for the analyst digest. Read-only."""
    return _load_cache()


def _verdict_to_dict(v: Verdict) -> dict:
    return {
        "strategy": v.strategy, "feature": v.feature, "n_trades": v.n_trades,
        "base_avg_r": v.base_avg_r,
        "buckets": [asdict(b) for b in v.buckets],
        "passing": [b.label for b in v.passing],
    }


# ── CLI ────────────────────────────────────────────────────────────────

def _print_verdict(v: Verdict) -> None:
    print(f"\n{v.strategy}  |  feature = {v.feature}  |  n = {v.n_trades}  |  base avg R = {v.base_avg_r:+.3f}")
    print(f"  {'bucket':<16}{'n':>7}{'avgR':>9}{'WR%':>7}{'PF':>7}{'1st':>8}{'2nd':>8}{'maxDD':>8}{'CI low':>9}  gate")
    for b in v.buckets:
        pf = f"{b.pf:.2f}" if b.pf is not None else "  -  "
        mark = "PASS" if b.gate_pass else "-"
        print(f"  {b.label:<16}{b.n:>7}{b.avg_r:>+9.3f}{b.win_rate:>7.1f}{pf:>7}"
              f"{b.first_half_avg_r:>+8.3f}{b.second_half_avg_r:>+8.3f}"
              f"{b.max_dd_r:>8.1f}{b.ci_low:>+9.3f}  {mark}")


def main(argv=None) -> int:
    all_strategies = sorted(MODULE_IMPORT) + sorted(STOCKS_MODULE_IMPORT)
    ap = argparse.ArgumentParser(description="Strategy edge-decomposition harness (read-only).")
    ap.add_argument("--strategy", required=True, choices=all_strategies)
    ap.add_argument("--feature", help="one feature to bucket by (default: full sweep)")
    ap.add_argument("--sweep", action="store_true", help="run the standard feature set")
    ap.add_argument("--years", type=int, default=None,
                    help="lookback years (default 13 for forex, 5 for stocks)")
    ap.add_argument("--core-only", action="store_true",
                    help="forex: 49 CORE pairs only (matches the 2026-09-02 sweep)")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    ap.add_argument("--refresh-cache", action="store_true",
                    help="re-run the full roster sweep and write the decomp cache")
    ap.add_argument("--stocks", action="store_true",
                    help="refresh the stocks cache (use with --refresh-cache)")
    args = ap.parse_args(argv)

    is_stock = args.strategy in STOCKS_MODULE_IMPORT
    years = args.years or (5 if is_stock else 13)

    if args.refresh_cache:
        if args.stocks or is_stock:
            cache = refresh_stocks_cache(years=years)
        else:
            cache = refresh_cache(years=years)
        print(json.dumps({k: (v.get("ts") or v.get("error")) for k, v in cache.items()}, indent=2))
        return 0

    if is_stock:
        trades = replay_stock_trades(args.strategy, years=years)
    else:
        trades = replay_trades(args.strategy, years=years, core_only=args.core_only)
    print(f"replayed {len(trades)} {args.strategy} trades "
          f"({'stock' if is_stock else ('core' if args.core_only else 'full')} universe, {years}y)")
    if not trades:
        return 1

    if args.feature and not args.sweep:
        verds = [bucket_and_gate(trades, args.feature)]
    elif is_stock:
        verds = sweep_stocks(args.strategy, years=years, trades=trades)
    else:
        verds = sweep(args.strategy, years=years, core_only=args.core_only, trades=trades)

    if args.json:
        print(json.dumps([_verdict_to_dict(v) for v in verds], indent=2))
    else:
        for v in verds:
            _print_verdict(v)
        passing = [(v.feature, b.label) for v in verds for b in v.passing]
        print(f"\n{len(passing)} bucket(s) cleared the gate: {passing}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
