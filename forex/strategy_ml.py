"""
forex/strategy_ml.py
---------------------
FX Strategy 10 — Machine Learning Signals (Logistic Regression).

CONCEPT:
  Trains a logistic regression model on the last 126 daily bars for each pair.
  Features are normalized technical indicators that capture trend, momentum,
  volatility, and mean-reversion. The model outputs a buy/sell probability.
  Only trade when model confidence > CONFIDENCE_THRESHOLD.

FEATURES (7 normalized):
  1. RSI(14) — momentum oscillator, normalized 0-1
  2. ADX(14) — trend strength, normalized 0-1
  3. BB %B(20,2) — price position within Bollinger Band, 0-1
  4. EMA(5)/EMA(20) ratio − 1 — fast/slow EMA spread
  5. EMA(20)/EMA(50) ratio − 1 — medium trend
  6. Price/EMA(200) ratio − 1 — macro trend bias
  7. ATR(14)/close — normalized volatility

TARGET: next-day close > today's close → 1 (up), else 0 (down)

ENTRY:
  Long : predicted probability ≥ CONFIDENCE_THRESHOLD  AND  ADX ≥ 20
  Short: predicted probability ≤ (1 - CONFIDENCE_THRESHOLD)  AND  ADX ≥ 20

EXIT (first hit):
  A. Model prediction flips direction with confidence ≥ CONFIDENCE_THRESHOLD
  B. ATR hard stop: 2.0 × ATR(14)
  C. Time stop: 20 calendar days

SIZING: 1% equity risk per trade, ATR-based.
Expected WR: ~57-62% (varies by market regime)
"""

import numpy as np
import pandas as pd

LOOKBACK            = 126    # training window (6 months of daily bars)
CONFIDENCE_THRESHOLD = 0.58  # minimum model confidence to trade
ADX_MIN             = 20
ATR_PERIOD          = 14
EMA_TREND           = 200
ATR_STOP_MULT       = 2.0
RISK_PCT            = 0.01
TIME_STOP_DAYS      = 20
LOT_ROUND           = 1_000
MIN_BARS            = EMA_TREND + LOOKBACK + 10


def _atr(h, l, c, period=ATR_PERIOD):
    prev = c.shift(1)
    tr = pd.concat([h - l, (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def _rsi(c, period=14):
    delta = c.diff()
    gain  = delta.clip(lower=0).ewm(alpha=1/period, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(alpha=1/period, adjust=False).mean()
    rs    = gain / (loss + 1e-10)
    return 100 - 100 / (1 + rs)


def _adx(h, l, c, period=ATR_PERIOD):
    prev_c = c.shift(1); prev_h = h.shift(1); prev_l = l.shift(1)
    tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    dm_p = ((h - prev_h).clip(lower=0)).where(h - prev_h > prev_l - l, 0)
    dm_m = ((prev_l - l).clip(lower=0)).where(prev_l - l > h - prev_h, 0)
    atr14 = tr.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    di_p  = 100 * dm_p.ewm(alpha=1/period, adjust=False, min_periods=period).mean() / atr14
    di_m  = 100 * dm_m.ewm(alpha=1/period, adjust=False, min_periods=period).mean() / atr14
    dx    = 100 * (di_p - di_m).abs() / (di_p + di_m + 1e-9)
    return dx.ewm(alpha=1/period, adjust=False, min_periods=period).mean()


def _bb_pct_b(c, period=20, std=2.0):
    mid   = c.rolling(period).mean()
    band  = std * c.rolling(period).std(ddof=1)
    return (c - (mid - band)) / (2 * band + 1e-10)


def _build_features(h, l, c):
    rsi   = _rsi(c) / 100.0
    adx   = _adx(h, l, c) / 100.0
    bb    = _bb_pct_b(c).clip(0, 1)
    e5    = c.ewm(span=5,   adjust=False).mean()
    e20   = c.ewm(span=20,  adjust=False).mean()
    e50   = c.ewm(span=50,  adjust=False).mean()
    e200  = c.ewm(span=200, adjust=False).mean()
    atr   = _atr(h, l, c)
    f1 = rsi
    f2 = adx
    f3 = bb
    f4 = (e5 / e20 - 1) * 100
    f5 = (e20 / e50 - 1) * 100
    f6 = (c / e200 - 1) * 100
    f7 = atr / c * 100
    return pd.concat([f1, f2, f3, f4, f5, f6, f7], axis=1).values


def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-np.clip(x, -50, 50)))


def _logistic_regression(X_train, y_train, lr=0.05, epochs=200):
    """Minimal numpy logistic regression — no sklearn dependency."""
    n_features = X_train.shape[1]
    w = np.zeros(n_features)
    b = 0.0
    for _ in range(epochs):
        pred  = _sigmoid(X_train @ w + b)
        err   = pred - y_train
        w    -= lr * (X_train.T @ err) / len(y_train)
        b    -= lr * err.mean()
    return w, b


def _predict_proba(X, w, b):
    return _sigmoid(X @ w + b)


def _train_and_predict(h, l, c):
    """Train on LOOKBACK bars, return probability for today's direction."""
    feats  = _build_features(h, l, c)
    target = (c.diff().shift(-1) > 0).astype(float).values  # next-day up?

    # Use only the training window (excluding today's row)
    start  = len(feats) - LOOKBACK - 1
    end    = len(feats) - 1
    X_raw  = feats[start:end]
    y      = target[start:end]

    # Drop NaN rows
    mask   = ~np.isnan(X_raw).any(axis=1) & ~np.isnan(y)
    X_raw, y = X_raw[mask], y[mask]
    if len(X_raw) < 30:
        return None

    # Normalize features
    mu     = X_raw.mean(axis=0)
    sigma  = X_raw.std(axis=0) + 1e-8
    X      = (X_raw - mu) / sigma

    w, b   = _logistic_regression(X, y)

    # Predict on today's features
    today_feat = feats[-1:].copy()
    if np.isnan(today_feat).any():
        return None
    today_norm = (today_feat - mu) / sigma
    return float(_predict_proba(today_norm, w, b)[0])


def generate_signals(market_data: dict, open_symbols: set = None) -> list:
    if open_symbols is None:
        open_symbols = set()
    signals = []
    for sym, df in market_data.items():
        if sym in open_symbols or df is None or len(df) < MIN_BARS:
            continue
        h, l, c = df["High"], df["Low"], df["Close"]
        try:
            prob = _train_and_predict(h, l, c)
        except Exception:
            continue
        if prob is None:
            continue

        atr_val = float(_atr(h, l, c).iloc[-1])
        adx_val = float(_adx(h, l, c).iloc[-1])
        today   = float(c.iloc[-1])

        if np.isnan(atr_val) or atr_val <= 0 or adx_val < ADX_MIN:
            continue

        if prob >= CONFIDENCE_THRESHOLD:
            signals.append({
                "symbol":    sym, "direction": "Buy",
                "score":     prob, "atr": atr_val,
                "close":     today,
                "stop_price": today - ATR_STOP_MULT * atr_val,
                "ml_prob":   prob,
            })
        elif prob <= 1.0 - CONFIDENCE_THRESHOLD:
            signals.append({
                "symbol":    sym, "direction": "Sell",
                "score":     1.0 - prob, "atr": atr_val,
                "close":     today,
                "stop_price": today + ATR_STOP_MULT * atr_val,
                "ml_prob":   prob,
            })

    signals.sort(key=lambda x: x["score"], reverse=True)
    return signals


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    if df is None or len(df) < MIN_BARS:
        return False, ""
    if calendar_days_held >= TIME_STOP_DAYS:
        return True, f"time_stop ({calendar_days_held}d)"
    h, l, c  = df["High"], df["Low"], df["Close"]
    stop_px  = position["stop_price"]
    is_long  = position["direction"] == "Buy"
    high_now = float(h.iloc[-1])
    low_now  = float(l.iloc[-1])
    if is_long and stop_px > 0 and low_now <= stop_px:
        return True, f"hard_stop ({stop_px:.5f})"
    if not is_long and stop_px > 0 and high_now >= stop_px:
        return True, f"hard_stop ({stop_px:.5f})"
    # Model flip exit
    try:
        prob = _train_and_predict(h, l, c)
        if prob is not None:
            if is_long  and prob <= 1.0 - CONFIDENCE_THRESHOLD:
                return True, f"ml_flip (prob={prob:.2f})"
            if not is_long and prob >= CONFIDENCE_THRESHOLD:
                return True, f"ml_flip (prob={prob:.2f})"
    except Exception:
        pass
    return False, ""


def size_position(account_equity: float, atr: float, min_units: float = 1_000) -> int:
    risk_amount   = account_equity * RISK_PCT
    stop_distance = ATR_STOP_MULT * atr
    if stop_distance <= 0:
        return int(min_units)
    raw = risk_amount / stop_distance
    return max(int(min_units), int(raw / min_units) * int(min_units))


def scan_summary(market_data: dict) -> list:
    rows = []
    for sym, df in market_data.items():
        if df is None or len(df) < MIN_BARS:
            rows.append({"symbol": sym, "status": "no_data"})
            continue
        h, l, c = df["High"], df["Low"], df["Close"]
        try:
            prob = _train_and_predict(h, l, c)
        except Exception:
            prob = None
        rows.append({
            "symbol": sym, "close": float(c.iloc[-1]),
            "ml_prob": prob, "status": "ok",
        })
    return rows
