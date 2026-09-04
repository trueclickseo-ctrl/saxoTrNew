"""
ibkr_module/ibkr_signals.py
----------------------------
Signal generators for all IBKR strategies (Yahoo Finance only).
No Saxo imports. No Avanza imports.

Exposed functions:
  blend_targets()            -> dict {risk_off, targets, reason, detail}
  reversion_candidates()     -> list ranked [{ticker, price, rsi, ...}]
  intraday_candidates()      -> list (intraday reversion, US session only)
  reversion_exit_indicators() -> {symbol: {price, rsi, sma20}} for exit checks

All strategy logic is the same validated code that powers the ATOS SIM books,
imported from the pure atos/ modules that have zero Saxo I/O.
"""
from __future__ import annotations

import datetime
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from atos.universe import US_TICKERS
from atos import us_momentum as _mom
from atos import us_reversion as _rev

_CACHE_FILE = _ROOT / "data" / "ibkr_price_cache.pkl"
_CACHE_MAX_AGE_HOURS = 8   # re-download if cache is older than this


def _load_cache(lookback_days: int) -> dict[str, pd.DataFrame] | None:
    """Return cached price data if it exists and is fresh enough, else None."""
    import pickle
    if not _CACHE_FILE.exists():
        return None
    age_h = (datetime.datetime.now().timestamp() - _CACHE_FILE.stat().st_mtime) / 3600
    if age_h > _CACHE_MAX_AGE_HOURS:
        return None
    try:
        with open(_CACHE_FILE, "rb") as f:
            meta, data = pickle.load(f)
        if meta.get("lookback_days", 0) >= lookback_days:
            return data
    except Exception:
        pass
    return None


def _save_cache(data: dict[str, pd.DataFrame], lookback_days: int) -> None:
    import pickle
    _CACHE_FILE.parent.mkdir(exist_ok=True)
    try:
        with open(_CACHE_FILE, "wb") as f:
            pickle.dump(({"lookback_days": lookback_days}, data), f)
    except Exception:
        pass


def _download(tickers: list[str], lookback_days: int = 260) -> dict[str, pd.DataFrame]:
    """Batch-download daily OHLCV from Yahoo Finance with an 8-hour disk cache.

    First call of the day downloads ~424 tickers (~30s).
    Subsequent calls within 8 hours return the cached data instantly.
    """
    # Try cache first (covers all-universe requests; skip for small targeted requests)
    if len(tickers) >= 100:
        cached = _load_cache(lookback_days)
        if cached is not None:
            subset = {t: cached[t] for t in tickers if t in cached}
            print(f"  [signals] cache hit — {len(subset)} tickers (age < {_CACHE_MAX_AGE_HOURS}h)")
            return subset

    import yfinance as yf

    end   = datetime.date.today()
    start = end - datetime.timedelta(days=lookback_days + 90)

    print(f"  [signals] downloading {len(tickers)} tickers ({start} → {end})...")
    try:
        raw = yf.download(
            tickers, start=str(start), end=str(end),
            progress=False, auto_adjust=True, threads=True,
        )
    except Exception as e:
        print(f"  [signals] yfinance error: {e}")
        return {}

    if len(tickers) == 1:
        return {tickers[0]: raw} if not raw.empty and len(raw) >= 20 else {}

    result: dict[str, pd.DataFrame] = {}
    if isinstance(raw.columns, pd.MultiIndex):
        for t in tickers:
            try:
                df = raw.xs(t, axis=1, level=1).dropna(how="all")
                if len(df) >= 20:
                    result[t] = df
            except KeyError:
                pass

    if len(tickers) >= 100:
        _save_cache(result, lookback_days)
        print(f"  [signals] cache saved ({len(result)} tickers)")

    return result


def blend_targets(lookback_days: int = 252) -> dict:
    """US Blend cross-sectional momentum signal.
    Returns {risk_off: bool, targets: list[str], reason: str, detail: dict}.
    """
    feat_data = _download(US_TICKERS, lookback_days=lookback_days)
    print(f"  [blend] {len(feat_data)}/{len(US_TICKERS)} tickers with sufficient history")
    result = _mom.compute_targets(feat_data, US_TICKERS)
    print(f"  [blend] risk_off={result['risk_off']}  targets={result.get('targets', [])}")
    return result


def reversion_candidates(lookback_days: int = 260) -> list[dict]:
    """US Reversion daily scan.
    Returns ranked [{ticker, price, rsi, sma20, dip_pct, vol_ratio, score}].
    """
    feat_data = _download(US_TICKERS, lookback_days=lookback_days)
    print(f"  [reversion] {len(feat_data)}/{len(US_TICKERS)} tickers with sufficient history")
    candidates = _rev.scan(feat_data, US_TICKERS)
    print(f"  [reversion] {len(candidates)} signal(s) found")
    return candidates


def intraday_candidates(lookback_days: int = 260) -> list[dict]:
    """Intraday reversion scan (5-min bars + daily history).
    Only meaningful during US market hours (09:30–16:00 ET = 18:30–01:00 PKT).
    Returns same format as reversion_candidates().
    """
    from atos.intraday_reversion import intraday_scan, fetch_intraday

    feat_data = _download(US_TICKERS, lookback_days=lookback_days)
    print(f"  [intraday] fetching live 5-min bars for {len(feat_data)} tickers...")
    intraday_data = fetch_intraday(list(feat_data.keys()))
    print(f"  [intraday] got live bars for {len(intraday_data)} tickers")
    candidates = intraday_scan(feat_data, US_TICKERS)
    print(f"  [intraday] {len(candidates)} intraday signal(s) found")
    return candidates


def yahoo_prices(symbols: list[str]) -> dict[str, float]:
    """Return the most-recent daily close for each symbol from Yahoo Finance.

    Uses the 8-hour cache when available (covers the full 424-ticker universe).
    Falls back to a small targeted download for symbols not in the cache.
    Used as a fallback when IBKR market data is unavailable (paper account,
    no live subscription, outside market hours).
    """
    result: dict[str, float] = {}
    cached = _load_cache(lookback_days=260)
    missing = []
    for sym in symbols:
        if cached and sym in cached:
            close = cached[sym]["Close"].dropna()
            if len(close):
                result[sym] = float(close.iloc[-1])
        else:
            missing.append(sym)
    if missing:
        extra = _download(missing, lookback_days=10)
        for sym, df in extra.items():
            close = df["Close"].dropna()
            if len(close):
                result[sym] = float(close.iloc[-1])
    return result


def reversion_exit_indicators(symbols: list[str], lookback_days: int = 40) -> dict[str, dict]:
    """Compute RSI(14) and SMA20 for a list of open reversion positions.
    Returns {symbol: {price: float, rsi: float, sma20: float}}.
    """
    if not symbols:
        return {}
    feat_data = _download(symbols, lookback_days=lookback_days)
    result: dict[str, dict] = {}
    for sym, df in feat_data.items():
        close = df["Close"].dropna()
        if len(close) < 22:
            continue
        price = float(close.iloc[-1])
        sma20 = float(close.rolling(20).mean().iloc[-1])
        delta = close.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rs    = gain / loss.replace(0, np.nan)
        rsi   = float((100 - 100 / (1 + rs)).iloc[-1])
        result[sym] = {"price": price, "rsi": rsi, "sma20": sma20}
    return result
