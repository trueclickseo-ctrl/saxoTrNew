"""
ibkr_module/ibkr_scorer.py
---------------------------
Feature engineering: Yahoo OHLCV → scoring_engine inputs → scored DataFrame.

Entry point:
    run_scan(n_swing=20, n_portfolio=20, min_score=60.0) -> dict

The dict contains:
    'swing'       — top Swing/Momentum candidates (buy-list for momentum strategy)
    'portfolio'   — top Hybrid/Portfolio candidates (buy-list for blend/reversion)
    'all_scored'  — full 492-row scored DataFrame (for dashboards)
    'universe'    — universe DataFrame with ATOS_Universe type

No Saxo imports. No order placement. Dry-run safe.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from ibkr_module.scoring_engine import ScoringConfig, calculate_scores, top_candidates
from ibkr_module.ibkr_signals import _download  # re-use the 8-hour disk cache

_UNIVERSE_CSV = _ROOT / "config" / "atos_us_500_universe.csv"
_SPY = "SPY"


# ── Technical indicator helpers ───────────────────────────────────────────────

def _atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    prev = close.shift(1)
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / n, min_periods=n).mean()


def _adx(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    """ADX via EWM smoothing (fast, good approximation)."""
    alpha = 1.0 / n
    up   = high.diff()
    dn   = -low.diff()
    plus_dm  = up.where((up > dn) & (up > 0), 0.0)
    minus_dm = dn.where((dn > up) & (dn > 0), 0.0)

    atr14    = _atr(high, low, close, n)
    plus_di  = 100 * plus_dm.ewm(alpha=alpha, min_periods=n).mean()  / (atr14 + 1e-9)
    minus_di = 100 * minus_dm.ewm(alpha=alpha, min_periods=n).mean() / (atr14 + 1e-9)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di + 1e-9)
    return dx.ewm(alpha=alpha, min_periods=n).mean()


def _rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain  = delta.clip(lower=0).ewm(alpha=1.0 / n, min_periods=n).mean()
    loss  = (-delta.clip(upper=0)).ewm(alpha=1.0 / n, min_periods=n).mean()
    return 100 - 100 / (1 + gain / (loss + 1e-9))


# ── Feature engineering ───────────────────────────────────────────────────────

def _build_features(
    price_data: dict[str, pd.DataFrame],
    spy_data: pd.DataFrame,
    universe: pd.DataFrame,
) -> pd.DataFrame:
    """Compute scoring-engine inputs for every ticker with enough history."""

    _spy_col = spy_data["Close"]
    # yfinance single-ticker downloads can return a 1-column DataFrame
    if isinstance(_spy_col, pd.DataFrame):
        _spy_col = _spy_col.iloc[:, 0]
    spy_close = _spy_col.dropna()
    if len(spy_close) >= 21:
        spy_roc20 = (spy_close.iloc[-1] / spy_close.iloc[-21] - 1) * 100
        spy_roc20 = float(spy_roc20) if not isinstance(spy_roc20, float) else spy_roc20
    else:
        spy_roc20 = 0.0

    # Index universe type for fast lookup
    type_map = universe.set_index("Ticker")["ATOS_Universe"].to_dict()

    rows: list[dict] = []
    for ticker, df in price_data.items():
        if len(df) < 200:
            continue

        close  = df["Close"].dropna()
        high   = df["High"].dropna()
        low    = df["Low"].dropna()
        volume = df["Volume"].dropna()

        if len(close) < 200:
            continue

        price = float(close.iloc[-1])

        # Liquidity
        vol_20   = float(volume.iloc[-20:].mean())
        dvol_20  = vol_20 * price

        # ATR% (14-day)
        atr14_ser = _atr(high, low, close, 14)
        atr14     = float(atr14_ser.iloc[-1])
        atr_pct   = atr14 / price * 100

        # Rate of change
        def _safe_roc(n: int) -> float:
            if len(close) <= n:
                return 0.0
            base = float(close.iloc[-(n + 1)])
            return (price / base - 1) * 100 if base > 0 else 0.0

        roc_20 = _safe_roc(20)
        roc_60 = _safe_roc(60)

        # SMA distances
        sma50    = float(close.rolling(50).mean().iloc[-1])
        sma200   = float(close.rolling(200).mean().iloc[-1])
        sma50_d  = (price - sma50)  / max(sma50,  0.01) * 100
        sma200_d = (price - sma200) / max(sma200, 0.01) * 100

        # ADX(14)
        try:
            adx14 = float(_adx(high, low, close, 14).iloc[-1])
        except Exception:
            adx14 = 25.0

        # Relative strength vs SPY (excess return over 20 days)
        rs_vs_spy = roc_20 - spy_roc20

        # RSI (for signal-level filtering later)
        rsi14 = float(_rsi(close, 14).iloc[-1])

        # Market cap: use large default — all 492 are large/mid caps
        market_cap = price * vol_20 * 10  # very rough proxy; always >> $1B gate

        rows.append({
            "ticker":               ticker,
            "atos_universe":        type_map.get(ticker, "Hybrid / Portfolio"),
            "price":                round(price, 4),
            "avg_volume_20d":       round(vol_20, 0),
            "avg_dollar_volume_20d": round(dvol_20, 0),
            "market_cap":           round(market_cap, 0),
            "atr_pct":              round(atr_pct, 4),
            "roc_20d":              round(roc_20, 4),
            "roc_60d":              round(roc_60, 4),
            "adx_14":               round(adx14, 4),
            "sma50_distance_pct":   round(sma50_d, 4),
            "sma200_distance_pct":  round(sma200_d, 4),
            "rs_vs_spy_20d":        round(rs_vs_spy, 4),
            "rsi_14":               round(rsi14, 2),
            # Fundamentals: neutral defaults (no live feed yet)
            "catalyst_score":       50,
            "revenue_growth":       0,
            "eps_growth":           0,
            "operating_margin":     0,
            "fcf_yield":            0,
            "roic":                 0,
            "debt_to_equity":       np.nan,
            "dividend_yield":       0,
            "moat_score":           50,
            "earnings_days":        np.nan,
        })

    return pd.DataFrame(rows)


# ── Public API ────────────────────────────────────────────────────────────────

def run_scan(
    n_swing: int = 20,
    n_portfolio: int = 20,
    min_score: float = 60.0,
    lookback_days: int = 260,
    verbose: bool = True,
) -> dict:
    """Download data, compute features, score all stocks, return candidates.

    Args:
        n_swing:      Max Swing/Momentum candidates to return.
        n_portfolio:  Max Hybrid/Portfolio candidates to return.
        min_score:    Minimum trade_score (0-100) to include a candidate.
        lookback_days: History window for Yahoo download.
        verbose:      Print progress.

    Returns:
        {
          'swing':      pd.DataFrame of top swing candidates,
          'portfolio':  pd.DataFrame of top portfolio candidates,
          'all_scored': pd.DataFrame — full universe with scores,
          'universe':   pd.DataFrame — raw universe CSV,
          'features':   pd.DataFrame — computed feature inputs,
        }
    """
    # Load universe
    universe = pd.read_csv(_UNIVERSE_CSV)
    tickers  = universe["Ticker"].tolist()
    if verbose:
        print(f"[scorer] universe: {len(tickers)} tickers")

    # Download OHLCV — reuses ibkr_signals 8-hour cache.
    # Request all tickers + SPY together so they land in a single batch download.
    # If the batch cache exists but doesn't contain SPY (older cache), do a tiny
    # targeted download for SPY alone so RS vs SPY is always real.
    all_tickers = sorted(set(tickers + [_SPY]))
    price_data  = _download(all_tickers, lookback_days=lookback_days)

    spy_data = price_data.pop(_SPY, pd.DataFrame())
    if spy_data.empty:
        if verbose:
            print("[scorer] SPY not in cache — fetching separately")
        spy_extra = _download([_SPY], lookback_days=lookback_days)
        spy_data  = spy_extra.get(_SPY, pd.DataFrame())
    if spy_data.empty:
        print("[scorer] WARNING: SPY data unavailable — RS vs SPY will be 0")
        spy_close = pd.Series([100.0, 100.0], dtype=float)
        spy_data  = pd.DataFrame({"Close": spy_close})

    # Fetch any tickers not already in the batch cache (tickers added to the
    # universe after the cache was populated). Use chunks < 100 to bypass the
    # cache-read path in _download (which would return an empty subset again).
    missing = [t for t in tickers if t not in price_data]
    if missing:
        if verbose:
            print(f"[scorer] fetching {len(missing)} new tickers not in cache...")
        _CHUNK = 90
        for i in range(0, len(missing), _CHUNK):
            chunk = missing[i:i + _CHUNK]
            extra = _download(chunk, lookback_days=lookback_days)
            price_data.update(extra)

        # Some tickers fail in batch due to yfinance timezone quirks but are
        # live stocks. Retry them one at a time as a fallback.
        still_missing = [t for t in missing if t not in price_data]
        if still_missing:
            if verbose:
                print(f"[scorer] retrying {len(still_missing)} batch-fail tickers individually...")
            import yfinance as yf
            from datetime import datetime, timedelta
            end_dt   = datetime.today()
            start_dt = end_dt - timedelta(days=lookback_days + 30)
            recovered = 0
            for t in still_missing:
                try:
                    df_t = yf.download(t, start=start_dt.strftime("%Y-%m-%d"),
                                       end=end_dt.strftime("%Y-%m-%d"),
                                       auto_adjust=True, progress=False)
                    if not df_t.empty and len(df_t) >= 50:
                        price_data[t] = df_t
                        recovered += 1
                except Exception:
                    pass
            if verbose and recovered:
                print(f"[scorer] individually recovered {recovered}/{len(still_missing)} tickers")

        if verbose:
            print(f"[scorer] total tickers with data: {len(price_data)}")

    if verbose:
        print(f"[scorer] got OHLCV for {len(price_data)}/{len(tickers)} tickers")

    # Compute features
    features = _build_features(price_data, spy_data, universe)
    if verbose:
        print(f"[scorer] features computed for {len(features)} tickers")

    if features.empty:
        print("[scorer] ERROR: no features computed — check Yahoo connection")
        empty = pd.DataFrame()
        return {"swing": empty, "portfolio": empty, "all_scored": empty,
                "universe": universe, "features": empty}

    # Score
    scored = calculate_scores(features)
    passed = scored[scored.hard_gate & (scored.trade_score >= min_score)]
    if verbose:
        print(f"[scorer] {len(scored)} tickers scored | "
              f"{scored.hard_gate.sum()} pass hard gates | "
              f"{len(passed)} above min_score={min_score}")

    # Split by universe type
    swing_universe = scored[scored.atos_universe == "Swing / Momentum"]
    port_universe  = scored[scored.atos_universe != "Swing / Momentum"]

    swing_top = top_candidates(swing_universe, n=n_swing,     minimum_score=min_score, universe_type="Swing / Momentum")
    port_top  = top_candidates(port_universe,  n=n_portfolio, minimum_score=min_score, universe_type="Hybrid / Portfolio")

    if verbose:
        print(f"[scorer] swing candidates: {len(swing_top)} | portfolio candidates: {len(port_top)}")

    return {
        "swing":      swing_top,
        "portfolio":  port_top,
        "all_scored": scored,
        "universe":   universe,
        "features":   features,
    }
