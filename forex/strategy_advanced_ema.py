"""
forex/strategy_advanced_ema.py
------------------------------
Advanced EMA Strategy — trend-following crossover system.

Upgrades over the original:
- fixes stale documentation/parameter inconsistencies
- requires a rising ADX regime rather than only ADX >= threshold
- adds EMA50 macro trend confirmation
- adds volatility percentile filter to avoid dead/extreme regimes
- limits entry to genuinely recent crossovers
- scores signals by trend quality, not ADX alone
- keeps compatible public functions with the original strategy

INTEGRATION (2026-08-30, user request "implement this too on ATOS SIM
account like above"):
- Registered as its own STRATEGIES key "advanced_ema" in forex/runner.py,
  running in parallel with the original "ema" strategy (forex/strategy.py,
  completely untouched) for an A/B comparison -- same pattern as
  advanced_ml / donchian_quality / gap_weekend / london_breakout_v2.
- SIM ONLY. Deliberately NOT in LIVE_ALLOWED_STRATEGIES or
  LIVE_EUR_ALLOWED_STRATEGIES -- can never place a real-money order.
- No CHART_BARS change needed: MIN_BARS is 276, already inside the 500
  daily bars the runner fetches (raised to 500 for advanced_ml, 2026-08-30).
- trailing_stop_update() is the standard hook name -- the runner already
  calls it generically for any strategy that defines one. No runner exit-
  loop change needed (unlike advanced_ml's update_stop_price).
"""

import numpy as np
import pandas as pd

FAST_EMA = 5
SLOW_EMA = 30
TREND_EMA = 50

ADX_PERIOD = 14
ADX_MIN = 25
ADX_RISING_BARS = 3

ATR_PERIOD = 14
ATR_STOP_MULT = 1.5

RISK_PCT = 0.0025
TIME_STOP_DAYS = 45
SIGNAL_LOOKBACK = 10
LOT_ROUND = 1_000

VOL_LOOKBACK = 252
VOL_PCT_MIN = 0.20
VOL_PCT_MAX = 0.90

MIN_BARS = max(TREND_EMA, VOL_LOOKBACK) + ADX_PERIOD + 10


def _ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


def _atr(h, l, c, period=ATR_PERIOD):
    prev = c.shift(1)
    tr = pd.concat([h-l, (h-prev).abs(), (l-prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0/period, adjust=False, min_periods=period).mean()


def _adx(h, l, c, period=ADX_PERIOD):
    prev_h, prev_l, prev_c = h.shift(1), l.shift(1), c.shift(1)
    tr = pd.concat([h-l, (h-prev_c).abs(), (l-prev_c).abs()], axis=1).max(axis=1)

    up = h - prev_h
    down = prev_l - l
    plus_dm = up.clip(lower=0).where(up > down, 0.0)
    minus_dm = down.clip(lower=0).where(down > up, 0.0)

    def wild(s):
        return s.ewm(alpha=1.0/period, adjust=False, min_periods=period).mean()

    atr = wild(tr)
    plus_di = 100 * wild(plus_dm) / (atr + 1e-10)
    minus_di = 100 * wild(minus_dm) / (atr + 1e-10)
    dx = 100 * (plus_di-minus_di).abs() / (plus_di+minus_di+1e-10)
    adx = wild(dx)
    return adx, plus_di, minus_di


def _recent_cross(fast, slow, bullish):
    n = len(fast)
    for k in range(1, min(SIGNAL_LOOKBACK + 1, n - 1)):
        f0, f1 = float(fast.iloc[-(k+1)]), float(fast.iloc[-k])
        s0, s1 = float(slow.iloc[-(k+1)]), float(slow.iloc[-k])
        if bullish and f0 <= s0 and f1 > s1:
            return k
        if not bullish and f0 >= s0 and f1 < s1:
            return k
    return None


def _regime_ok(adx, atr_pct):
    if not np.isfinite(adx.iloc[-1]) or adx.iloc[-1] < ADX_MIN:
        return False
    if len(adx) > ADX_RISING_BARS:
        recent = adx.iloc[-ADX_RISING_BARS-1:]
        if recent.iloc[-1] < recent.iloc[0]:
            return False
    p = float(atr_pct.iloc[-1])
    return np.isfinite(p) and VOL_PCT_MIN <= p <= VOL_PCT_MAX


def generate_signals(market_data: dict, open_symbols: set = None) -> list:
    if open_symbols is None:
        open_symbols = set()

    signals = []
    for sym, df in market_data.items():
        if sym in open_symbols or df is None or len(df) < MIN_BARS:
            continue
        try:
            h, l, c = df["High"], df["Low"], df["Close"]

            fast = _ema(c, FAST_EMA)
            slow = _ema(c, SLOW_EMA)
            trend = _ema(c, TREND_EMA)
            atr = _atr(h, l, c)
            adx, plus_di, minus_di = _adx(h, l, c)
            atr_pct = atr.rolling(VOL_LOOKBACK, min_periods=80).rank(pct=True)

            if not _regime_ok(adx, atr_pct):
                continue

            close = float(c.iloc[-1])
            cur_atr = float(atr.iloc[-1])
            cur_adx = float(adx.iloc[-1])
            gap = abs(float(fast.iloc[-1] / slow.iloc[-1] - 1.0))

            long_cross_age = _recent_cross(fast, slow, True)
            short_cross_age = _recent_cross(fast, slow, False)

            long_ok = (
                long_cross_age is not None
                and fast.iloc[-1] > slow.iloc[-1]
                and close > trend.iloc[-1]
                and plus_di.iloc[-1] > minus_di.iloc[-1]
            )
            short_ok = (
                short_cross_age is not None
                and fast.iloc[-1] < slow.iloc[-1]
                and close < trend.iloc[-1]
                and minus_di.iloc[-1] > plus_di.iloc[-1]
            )

            # Composite ranking: trend strength + directional dominance + EMA separation.
            di_strength = abs(float(plus_di.iloc[-1] - minus_di.iloc[-1]))
            score = cur_adx + 0.25 * di_strength + 1000.0 * gap

            if long_ok:
                signals.append({
                    "symbol": sym, "direction": "Buy", "score": score,
                    "atr": cur_atr, "close": close,
                    "stop_price": close - ATR_STOP_MULT * cur_atr,
                    "adx": cur_adx, "plus_di": float(plus_di.iloc[-1]),
                    "minus_di": float(minus_di.iloc[-1]),
                    "cross_age": long_cross_age, "strategy": "advanced_ema",
                })
            elif short_ok:
                signals.append({
                    "symbol": sym, "direction": "Sell", "score": score,
                    "atr": cur_atr, "close": close,
                    "stop_price": close + ATR_STOP_MULT * cur_atr,
                    "adx": cur_adx, "plus_di": float(plus_di.iloc[-1]),
                    "minus_di": float(minus_di.iloc[-1]),
                    "cross_age": short_cross_age, "strategy": "advanced_ema",
                })
        except Exception:
            continue

    signals.sort(key=lambda x: x["score"], reverse=True)
    return signals


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    if df is None or len(df) < max(SLOW_EMA, ADX_PERIOD) + 2:
        return False, ""

    if calendar_days_held >= TIME_STOP_DAYS:
        return True, f"time_stop ({calendar_days_held}d)"

    h, l, c = df["High"], df["Low"], df["Close"]
    fast, slow = _ema(c, FAST_EMA), _ema(c, SLOW_EMA)
    direction = position["direction"]
    stop = float(position.get("stop_price", 0))

    if direction == "Buy":
        if fast.iloc[-2] >= slow.iloc[-2] and fast.iloc[-1] < slow.iloc[-1]:
            return True, "crossover_reversal"
        if stop > 0 and float(l.iloc[-1]) <= stop:
            return True, f"hard_stop ({stop:.5f})"
    else:
        if fast.iloc[-2] <= slow.iloc[-2] and fast.iloc[-1] > slow.iloc[-1]:
            return True, "crossover_reversal"
        if stop > 0 and float(h.iloc[-1]) >= stop:
            return True, f"hard_stop ({stop:.5f})"

    return False, ""


def size_position(account_equity: float, atr: float, min_units: int = LOT_ROUND,
                  block_below_min: bool = False) -> int:
    stop_distance = ATR_STOP_MULT * atr
    if stop_distance <= 0:
        return 0 if block_below_min else min_units

    units = int((account_equity * RISK_PCT / stop_distance) // min_units) * min_units
    if units < min_units:
        return 0 if block_below_min else min_units
    return units


def trailing_stop_update(current_stop: float, current_price: float,
                         current_atr: float, direction: str = "Buy") -> float:
    band = ATR_STOP_MULT * current_atr
    if direction == "Buy":
        return max(current_stop, current_price - band)
    return min(current_stop, current_price + band)


def scan_summary(market_data: dict) -> list:
    rows = []
    for sym, df in market_data.items():
        if df is None or len(df) < MIN_BARS:
            rows.append({"symbol": sym, "status": "no_data"})
            continue
        try:
            h, l, c = df["High"], df["Low"], df["Close"]
            fast, slow = _ema(c, FAST_EMA), _ema(c, SLOW_EMA)
            trend = _ema(c, TREND_EMA)
            atr = _atr(h, l, c)
            adx, plus_di, minus_di = _adx(h, l, c)
            atr_pct = atr.rolling(VOL_LOOKBACK, min_periods=80).rank(pct=True)
            rows.append({
                "symbol": sym, "close": float(c.iloc[-1]),
                "fast_ema": float(fast.iloc[-1]),
                "slow_ema": float(slow.iloc[-1]),
                "trend_ema": float(trend.iloc[-1]),
                "adx": float(adx.iloc[-1]),
                "plus_di": float(plus_di.iloc[-1]),
                "minus_di": float(minus_di.iloc[-1]),
                "atr_pct": float(atr_pct.iloc[-1]),
                "status": "ok",
            })
        except Exception:
            rows.append({"symbol": sym, "status": "error"})
    return rows
