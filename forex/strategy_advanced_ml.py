"""
forex/strategy_advanced_ml.py
-----------------------------
Advanced ML Strategy — regularized logistic regression with regime and trend filters.

Key upgrades:
- 5-day forward ATR-normalized target (better aligned with swing holding periods)
- Neutral/noise observations excluded from training
- Longer 252-bar training window
- L2 regularization
- Feature set focused on trend, momentum, returns, and volatility regime
- Volatility percentile filter
- Directional EMA trend confirmation
- More selective confidence threshold
- Same public interface as strategy_ml.py:
    generate_signals(), should_exit(), size_position(), scan_summary()
  ...plus update_stop_price(position, df) -- a combined breakeven+trail hook
  the runner calls generically (see forex/runner.py _run_exits, 2026-08-30).

INTEGRATION (2026-08-30, user request "implement this strategy too along our
ML, lets see if catch new signals"):
- Registered as its own STRATEGIES key "advanced_ml" in forex/runner.py,
  running in parallel with the original "ml" strategy (untouched) for an
  A/B comparison -- same pattern as donchian_quality / gap_weekend /
  london_breakout_v2.
- SIM ONLY. Deliberately NOT in LIVE_ALLOWED_STRATEGIES or
  LIVE_EUR_ALLOWED_STRATEGIES -- it can never place a real-money order.
- forex/runner.py's CHART_BARS was raised 340 -> 500 to satisfy this
  module's MIN_BARS (EMA200 + 252 training window + buffer = 492). Effect
  on every other strategy is negligible: a span-200 EMA / ATR(14) / ADX(14)
  read at iloc[-1] is already fully converged well before 340 bars, so
  their signals are unchanged; the only cost is a slightly larger daily-bar
  fetch per pair.
"""

import numpy as np
import pandas as pd

LOOKBACK = 252
FORECAST_HORIZON = 5
TARGET_ATR_MULT = 0.75

CONFIDENCE_THRESHOLD = 0.62
ADX_MIN = 22
ATR_PERIOD = 14
EMA_TREND = 200

ATR_STOP_MULT = 2.0
BREAKEVEN_TRIGGER_ATR = 1.0
TRAIL_TRIGGER_ATR = 2.0
TRAIL_ATR_MULT = 2.0

RISK_PCT = 0.0025
TIME_STOP_DAYS = 20
LOT_ROUND = 1_000
MIN_BARS = EMA_TREND + LOOKBACK + 40

VOL_LOOKBACK = 252
VOL_PCT_MIN = 0.20
VOL_PCT_MAX = 0.85


def _atr(h, l, c, period=ATR_PERIOD):
    prev = c.shift(1)
    tr = pd.concat([h-l, (h-prev).abs(), (l-prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0/period, adjust=False, min_periods=period).mean()


def _rsi(c, period=14):
    delta = c.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/period, adjust=False).mean()
    rs = gain / (loss + 1e-10)
    return 100 - 100/(1+rs)


def _adx(h, l, c, period=ATR_PERIOD):
    prev_c, prev_h, prev_l = c.shift(1), h.shift(1), l.shift(1)
    tr = pd.concat([h-l, (h-prev_c).abs(), (l-prev_c).abs()], axis=1).max(axis=1)
    up = h - prev_h
    down = prev_l - l
    dm_p = up.clip(lower=0).where(up > down, 0)
    dm_m = down.clip(lower=0).where(down > up, 0)
    atr = tr.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    di_p = 100 * dm_p.ewm(alpha=1/period, adjust=False, min_periods=period).mean() / atr
    di_m = 100 * dm_m.ewm(alpha=1/period, adjust=False, min_periods=period).mean() / atr
    dx = 100 * (di_p-di_m).abs() / (di_p+di_m+1e-9)
    adx = dx.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    return adx, di_p, di_m


def _bb_pct_b(c, period=20, std=2.0):
    mid = c.rolling(period).mean()
    band = std * c.rolling(period).std(ddof=1)
    return (c - (mid-band)) / (2*band + 1e-10)


def _indicators(h, l, c):
    atr = _atr(h, l, c)
    rsi = _rsi(c)
    adx, di_p, di_m = _adx(h, l, c)

    e5 = c.ewm(span=5, adjust=False).mean()
    e20 = c.ewm(span=20, adjust=False).mean()
    e50 = c.ewm(span=50, adjust=False).mean()
    e200 = c.ewm(span=EMA_TREND, adjust=False).mean()

    atr_pct = atr.rolling(VOL_LOOKBACK, min_periods=80).rank(pct=True)
    return {
        "atr": atr, "rsi": rsi, "adx": adx, "di_p": di_p, "di_m": di_m,
        "e5": e5, "e20": e20, "e50": e50, "e200": e200,
        "atr_pct": atr_pct, "bb": _bb_pct_b(c).clip(0, 1),
    }


def _build_features(h, l, c):
    d = _indicators(h, l, c)
    atr = d["atr"]

    # Less redundant feature set than the original strategy.
    f = pd.concat([
        d["rsi"] / 100.0,
        d["adx"] / 100.0,
        d["bb"],
        c.pct_change(1),
        c.pct_change(5),
        (d["e20"] / d["e50"] - 1.0),
        (c / d["e200"] - 1.0),
        d["e20"].pct_change(5),
        d["rsi"].diff(3) / 100.0,
        atr / (atr.rolling(100, min_periods=50).mean() + 1e-10),
    ], axis=1)
    return f.values


def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-np.clip(x, -50, 50)))


def _logistic_regression(X_train, y_train, lr=0.03, epochs=500, l2=0.01):
    n_features = X_train.shape[1]
    w = np.zeros(n_features)
    b = 0.0
    n = len(y_train)

    for _ in range(epochs):
        pred = _sigmoid(X_train @ w + b)
        err = pred - y_train
        grad_w = (X_train.T @ err) / n + l2 * w
        grad_b = err.mean()
        w -= lr * grad_w
        b -= lr * grad_b
    return w, b


def _target(c, atr):
    """
    Five-bar forward move, normalized by current ATR.
    Small moves are removed because they are mostly directional noise.
    """
    forward_return = c.shift(-FORECAST_HORIZON) - c
    threshold = TARGET_ATR_MULT * atr

    y = pd.Series(np.nan, index=c.index, dtype=float)
    y[forward_return >= threshold] = 1.0
    y[forward_return <= -threshold] = 0.0
    return y.values


def _train_and_predict(h, l, c):
    feats = _build_features(h, l, c)
    atr = _atr(h, l, c)
    target = _target(c, atr)

    # Exclude today's row and the final horizon rows whose future is unknown.
    end = len(feats) - FORECAST_HORIZON
    start = max(0, end - LOOKBACK)
    X_raw = feats[start:end]
    y = target[start:end]

    mask = ~np.isnan(X_raw).any(axis=1) & ~np.isnan(y)
    X_raw, y = X_raw[mask], y[mask]

    if len(X_raw) < 60 or len(np.unique(y)) < 2:
        return None

    mu = X_raw.mean(axis=0)
    sigma = X_raw.std(axis=0) + 1e-8
    X = (X_raw - mu) / sigma

    w, b = _logistic_regression(X, y)

    today = feats[-1:].copy()
    if np.isnan(today).any():
        return None

    return float(_sigmoid(((today - mu) / sigma) @ w + b)[0])


def _trend_ok(d, c, long_side):
    i = -1
    if long_side:
        return (
            d["e5"].iloc[i] > d["e20"].iloc[i] > d["e50"].iloc[i]
            and c.iloc[i] > d["e200"].iloc[i]
            and d["di_p"].iloc[i] > d["di_m"].iloc[i]
            and d["rsi"].iloc[i] >= 52
        )
    return (
        d["e5"].iloc[i] < d["e20"].iloc[i] < d["e50"].iloc[i]
        and c.iloc[i] < d["e200"].iloc[i]
        and d["di_m"].iloc[i] > d["di_p"].iloc[i]
        and d["rsi"].iloc[i] <= 48
    )


def _regime_ok(d):
    i = -1
    atr = float(d["atr"].iloc[i])
    adx = float(d["adx"].iloc[i])
    pct = float(d["atr_pct"].iloc[i])
    return (
        np.isfinite(atr) and atr > 0
        and np.isfinite(adx) and adx >= ADX_MIN
        and np.isfinite(pct) and VOL_PCT_MIN <= pct <= VOL_PCT_MAX
    )


def generate_signals(market_data: dict, open_symbols: set = None) -> list:
    if open_symbols is None:
        open_symbols = set()

    signals = []
    for sym, df in market_data.items():
        if sym in open_symbols or df is None or len(df) < MIN_BARS:
            continue

        try:
            h, l, c = df["High"], df["Low"], df["Close"]
            prob = _train_and_predict(h, l, c)
            if prob is None:
                continue

            d = _indicators(h, l, c)
            if not _regime_ok(d):
                continue

            atr = float(d["atr"].iloc[-1])
            close = float(c.iloc[-1])

            if prob >= CONFIDENCE_THRESHOLD and _trend_ok(d, c, True):
                signals.append({
                    "symbol": sym,
                    "direction": "Buy",
                    "score": prob,
                    "atr": atr,
                    "close": close,
                    "stop_price": close - ATR_STOP_MULT * atr,
                    "ml_prob": prob,
                    "strategy": "advanced_ml",
                })

            elif prob <= 1.0 - CONFIDENCE_THRESHOLD and _trend_ok(d, c, False):
                signals.append({
                    "symbol": sym,
                    "direction": "Sell",
                    "score": 1.0 - prob,
                    "atr": atr,
                    "close": close,
                    "stop_price": close + ATR_STOP_MULT * atr,
                    "ml_prob": prob,
                    "strategy": "advanced_ml",
                })
        except Exception:
            continue

    signals.sort(key=lambda x: x["score"], reverse=True)
    return signals


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    if df is None or len(df) < MIN_BARS:
        return False, ""

    if calendar_days_held >= TIME_STOP_DAYS:
        return True, f"time_stop ({calendar_days_held}d)"

    h, l, c = df["High"], df["Low"], df["Close"]
    is_long = position["direction"] == "Buy"
    stop_px = float(position.get("stop_price", 0))

    high_now = float(h.iloc[-1])
    low_now = float(l.iloc[-1])

    if stop_px > 0:
        if is_long and low_now <= stop_px:
            return True, f"hard_stop ({stop_px:.5f})"
        if not is_long and high_now >= stop_px:
            return True, f"hard_stop ({stop_px:.5f})"

    try:
        prob = _train_and_predict(h, l, c)
        if prob is not None:
            if is_long and prob <= 1.0 - CONFIDENCE_THRESHOLD:
                return True, f"ml_flip (prob={prob:.2f})"
            if not is_long and prob >= CONFIDENCE_THRESHOLD:
                return True, f"ml_flip (prob={prob:.2f})"
    except Exception:
        pass

    return False, ""


def update_stop_price(position: dict, df: pd.DataFrame) -> float:
    """
    Optional runner integration for dynamic stops.
    Returns the current/new stop. Call this before broker stop modification.
    """
    h, l, c = df["High"], df["Low"], df["Close"]
    d = _indicators(h, l, c)
    atr = float(d["atr"].iloc[-1])
    close = float(c.iloc[-1])

    entry = float(position.get("entry_price", position.get("close", close)))
    old_stop = float(position.get("stop_price", 0))
    is_long = position["direction"] == "Buy"

    profit_atr = ((close - entry) if is_long else (entry - close)) / max(atr, 1e-10)

    new_stop = old_stop
    if profit_atr >= BREAKEVEN_TRIGGER_ATR:
        breakeven = entry
        new_stop = max(new_stop, breakeven) if is_long else (
            min(new_stop, breakeven) if new_stop > 0 else breakeven
        )

    if profit_atr >= TRAIL_TRIGGER_ATR:
        trail = close - TRAIL_ATR_MULT * atr if is_long else close + TRAIL_ATR_MULT * atr
        new_stop = max(new_stop, trail) if is_long else (
            min(new_stop, trail) if new_stop > 0 else trail
        )

    return float(new_stop)


def size_position(account_equity: float, atr: float, min_units: float = 1_000,
                  block_below_min: bool = False) -> int:
    risk_amount = account_equity * RISK_PCT
    stop_distance = ATR_STOP_MULT * atr

    if stop_distance <= 0:
        return 0 if block_below_min else int(min_units)

    raw = risk_amount / stop_distance
    floored = int(raw / min_units) * int(min_units)

    if floored < min_units:
        return 0 if block_below_min else int(min_units)

    return floored


def scan_summary(market_data: dict) -> list:
    rows = []
    for sym, df in market_data.items():
        if df is None or len(df) < MIN_BARS:
            rows.append({"symbol": sym, "status": "no_data"})
            continue
        try:
            h, l, c = df["High"], df["Low"], df["Close"]
            prob = _train_and_predict(h, l, c)
            d = _indicators(h, l, c)
            rows.append({
                "symbol": sym,
                "close": float(c.iloc[-1]),
                "ml_prob": prob,
                "adx": float(d["adx"].iloc[-1]),
                "atr_pct": float(d["atr_pct"].iloc[-1]),
                "status": "ok",
            })
        except Exception:
            rows.append({"symbol": sym, "status": "error"})
    return rows
