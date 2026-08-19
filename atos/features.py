"""
atos/features.py
-----------------
All technical indicator calculations for ATOS.
Strategies and detectors NEVER calculate their own indicators —
they always call this module. This ensures consistency across the system.

All functions accept a pandas DataFrame with columns:
  Open, High, Low, Close, Volume
indexed by date (datetime index).

All functions return the same DataFrame with new columns added.
"""

import pandas as pd
import numpy as np


def add_all(df: pd.DataFrame) -> pd.DataFrame:
    """Compute every feature used by ATOS detectors. One call, all features."""
    df = df.copy()
    df = _ema(df)
    df = _atr(df)
    df = _rsi(df)
    df = _macd(df)
    df = _bollinger(df)
    df = _donchian(df)
    df = _volume_features(df)
    df = _adx(df)
    df = _market_structure(df)
    df = _vwap(df)
    df = _obv(df)
    df = _rate_of_change(df)
    df = _volatility_regime(df)
    return df


# ── Trend indicators ───────────────────────────────────────────────

def _ema(df: pd.DataFrame) -> pd.DataFrame:
    df["ema20"]  = df["Close"].ewm(span=20,  adjust=False).mean()
    df["ema50"]  = df["Close"].ewm(span=50,  adjust=False).mean()
    df["ema200"] = df["Close"].ewm(span=200, adjust=False).mean()
    return df


def _atr(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Average True Range — Wilder smoothing (same as TradingView / industry standard)."""
    prev_close = df["Close"].shift(1)
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - prev_close).abs(),
        (df["Low"]  - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    return df


def _adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Average Directional Index — measures trend STRENGTH (not direction)."""
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_high  = high.shift(1)
    prev_low   = low.shift(1)
    prev_close = close.shift(1)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)

    dm_plus  = ((high - prev_high).clip(lower=0)
                .where(high - prev_high > prev_low - low, 0))
    dm_minus = ((prev_low - low).clip(lower=0)
                .where(prev_low - low > high - prev_high, 0))

    # Wilder smoothing for ATR14, DI, and ADX — matches standard charting tools
    _alpha   = 1.0 / period
    atr14    = tr.ewm(alpha=_alpha, adjust=False, min_periods=period).mean()
    di_plus  = 100 * dm_plus.ewm(alpha=_alpha, adjust=False, min_periods=period).mean()  / atr14.replace(0, np.nan)
    di_minus = 100 * dm_minus.ewm(alpha=_alpha, adjust=False, min_periods=period).mean() / atr14.replace(0, np.nan)
    dx       = (100 * (di_plus - di_minus).abs()
                / (di_plus + di_minus).replace(0, np.nan))
    df["adx"] = dx.ewm(alpha=_alpha, adjust=False, min_periods=period).mean()
    return df


# ── Momentum indicators ────────────────────────────────────────────

def _rsi(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    delta = df["Close"].diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))
    # RSI crossing 50 (momentum shift signal)
    df["rsi_cross_up50"]   = (df["rsi"] > 50) & (df["rsi"].shift(1) <= 50)
    df["rsi_cross_down50"] = (df["rsi"] < 50) & (df["rsi"].shift(1) >= 50)
    return df


def _macd(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9
          ) -> pd.DataFrame:
    ema_fast     = df["Close"].ewm(span=fast,   adjust=False).mean()
    ema_slow     = df["Close"].ewm(span=slow,   adjust=False).mean()
    df["macd"]   = ema_fast - ema_slow
    df["macd_signal"] = df["macd"].ewm(span=signal, adjust=False).mean()
    df["macd_hist"]   = df["macd"] - df["macd_signal"]
    return df


# ── Volatility / Range indicators ──────────────────────────────────

def _bollinger(df: pd.DataFrame, period: int = 20, std_dev: float = 2.0
               ) -> pd.DataFrame:
    ma  = df["Close"].rolling(period).mean()
    std = df["Close"].rolling(period).std()
    df["bb_upper"]  = ma + std_dev * std
    df["bb_lower"]  = ma - std_dev * std
    df["bb_middle"] = ma
    df["bb_width"]  = (df["bb_upper"] - df["bb_lower"]) / ma  # normalized
    # Position within band: 0 = at lower band, 1 = at upper band
    band_range = (df["bb_upper"] - df["bb_lower"]).replace(0, np.nan)
    df["bb_pct"]    = (df["Close"] - df["bb_lower"]) / band_range
    return df


def _donchian(df: pd.DataFrame, period: int = 20) -> pd.DataFrame:
    """Donchian Channel — used by Turtle Traders. 20-day high/low breakout."""
    df["donchian_high"] = df["High"].rolling(period).max()
    df["donchian_low"]  = df["Low"].rolling(period).min()
    # Is today's close breaking above the previous 20-day high?
    df["donchian_breakout_up"]   = df["Close"] >= df["donchian_high"].shift(1)
    df["donchian_breakout_down"] = df["Close"] <= df["donchian_low"].shift(1)
    return df


# ── Volume indicators ──────────────────────────────────────────────

def _volume_features(df: pd.DataFrame, period: int = 20) -> pd.DataFrame:
    if "Volume" not in df.columns or df["Volume"].isna().all():
        # Forex and some commodities have no volume data — skip gracefully
        df["vol_ratio"]    = 1.0
        df["vol_ma"]       = np.nan
        df["has_volume"]   = False
        return df
    df["vol_ma"]     = df["Volume"].rolling(period).mean()
    df["vol_ratio"]  = df["Volume"] / df["vol_ma"].replace(0, np.nan)
    df["has_volume"] = True
    return df


# ── Market structure ───────────────────────────────────────────────

def _market_structure(df: pd.DataFrame, lookback: int = 5) -> pd.DataFrame:
    """
    Detect Higher Highs / Higher Lows (uptrend structure).
    Uses a simple rolling window comparison — not peak-detection,
    but good enough for daily-bar scoring.
    """
    df["higher_high"] = df["High"] > df["High"].shift(lookback)
    df["higher_low"]  = df["Low"]  > df["Low"].shift(lookback)
    df["lower_high"]  = df["High"] < df["High"].shift(lookback)
    df["lower_low"]   = df["Low"]  < df["Low"].shift(lookback)

    # EMA crossover signals (used by legacy bot + ATOS)
    df["ema_cross_up"]   = (df["ema20"] > df["ema50"]) & (df["ema20"].shift(1) <= df["ema50"].shift(1))
    df["ema_cross_down"] = (df["ema20"] < df["ema50"]) & (df["ema20"].shift(1) >= df["ema50"].shift(1))

    # Pullback to EMA20 (price touched EMA20 from above)
    df["pullback_to_ema20"] = (
        (df["Low"] <= df["ema20"] * 1.01) &   # touched within 1%
        (df["Close"] > df["ema20"]) &          # closed above (bounce)
        (df["ema20"] > df["ema50"])             # still in uptrend
    )
    return df


def _vwap(df: pd.DataFrame) -> pd.DataFrame:
    if "Volume" not in df.columns or df["Volume"].isna().all():
        df["vwap"] = np.nan
        return df
    df["vwap"] = (df["Close"] * df["Volume"]).rolling(20).sum() / df["Volume"].rolling(20).sum()
    return df


def _obv(df: pd.DataFrame) -> pd.DataFrame:
    if "Volume" not in df.columns or df["Volume"].isna().all():
        df["obv"] = np.nan
        df["obv_ema20"] = np.nan
        df["obv_rising"] = False
        return df
    
    close_diff = df["Close"].diff()
    direction = pd.Series(0, index=df.index)
    direction[close_diff > 0] = 1
    direction[close_diff < 0] = -1
    
    df["obv"] = (direction * df["Volume"]).cumsum()
    df["obv_ema20"] = df["obv"].ewm(span=20, adjust=False).mean()
    df["obv_rising"] = df["obv"] > df["obv_ema20"]
    return df


def _rate_of_change(df: pd.DataFrame) -> pd.DataFrame:
    df["roc_10"] = (df["Close"] / df["Close"].shift(10) - 1) * 100
    df["roc_20"] = (df["Close"] / df["Close"].shift(20) - 1) * 100
    df["mom_acceleration"] = df["roc_10"] - df["roc_10"].shift(5)
    return df


def _volatility_regime(df: pd.DataFrame) -> pd.DataFrame:
    df["atr_pct_rank"] = df["atr"].rolling(60).rank(pct=True)
    
    conditions = [
        (df["Close"] > df["ema200"]) & (df["adx"] > 20),
        (df["Close"] < df["ema200"]) & (df["adx"] > 20),
        (df["adx"] < 15)
    ]
    choices = ['BULL', 'BEAR', 'SIDEWAYS']
    df["regime"] = np.select(conditions, choices, default='TRANSITION')
    df["regime_shift"] = df["regime"] != df["regime"].shift(1)
    return df
