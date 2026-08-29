"""
Advanced Pullback Master
High-selectivity trend-pullback continuation strategy.

Designed to improve signal quality, not to guarantee a higher win rate.
Public API remains compatible with the existing pullback strategy.

INTEGRATION (2026-08-30, user request "implement all these too on ATOS SIM
along with our original strategies"): registered as STRATEGIES key
"advanced_pullback_master" in forex/runner.py, running in parallel with
the original "pullback" (forex/strategy_pullback.py, UNTOUCHED) for an A/B
comparison. SIM ONLY -- not in either LIVE allowlist. Uncapped slots
(mirrors "pullback"). MIN_BARS 276 fits the runner's 500-bar fetch; uses
the standard trailing_stop_update hook so no runner exit-loop change.
Momentum-pre-filtered like the original "pullback" (trend-continuation).
"""
import numpy as np
import pandas as pd

TREND_EMA = 50
PULLBACK_EMA = 20
FAST_CONFIRM_EMA = 5
ADX_PERIOD = 14
ADX_MIN = 25
ATR_PERIOD = 14
ATR_STOP_MULT = 1.5
RISK_PCT = 0.0025
TIME_STOP_DAYS = 25
LOT_ROUND = 1_000
PULLBACK_LOOKBACK = 3

VOL_LOOKBACK = 252
VOL_PCT_MIN = 0.20
VOL_PCT_MAX = 0.90
MIN_BARS = max(VOL_LOOKBACK, TREND_EMA) + ADX_PERIOD + 10


def _ema(s, period):
    return s.ewm(span=period, adjust=False).mean()


def _atr(h, l, c, period=ATR_PERIOD):
    prev = c.shift(1)
    tr = pd.concat([h-l, (h-prev).abs(), (l-prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False, min_periods=period).mean()


def _adx(h, l, c, period=ADX_PERIOD):
    up, down = h.diff(), -l.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)

    def wilder(x):
        return x.ewm(alpha=1/period, adjust=False, min_periods=period).mean()

    atr = wilder(tr)
    plus_di = 100 * wilder(plus_dm) / (atr + 1e-10)
    minus_di = 100 * wilder(minus_dm) / (atr + 1e-10)
    dx = 100 * (plus_di-minus_di).abs() / (plus_di+minus_di+1e-10)
    return wilder(dx), plus_di, minus_di


def _recent_touch(h, l, ema, side):
    for k in range(1, PULLBACK_LOOKBACK + 1):
        idx = -(k + 1)
        if abs(idx) > len(ema):
            break
        if side == "Buy" and l.iloc[idx] <= ema.iloc[idx]:
            return k
        if side == "Sell" and h.iloc[idx] >= ema.iloc[idx]:
            return k
    return None


def generate_signals(market_data: dict, open_symbols: set = None) -> list:
    open_symbols = open_symbols or set()
    signals = []

    for sym, df in market_data.items():
        if sym in open_symbols or df is None or len(df) < MIN_BARS:
            continue
        try:
            h, l, c = df["High"], df["Low"], df["Close"]
            e5, e20, e50 = _ema(c, FAST_CONFIRM_EMA), _ema(c, PULLBACK_EMA), _ema(c, TREND_EMA)
            atr = _atr(h, l, c)
            adx, plus_di, minus_di = _adx(h, l, c)
            atr_pct = atr.rolling(VOL_LOOKBACK, min_periods=100).rank(pct=True)

            i = -1
            if not all(np.isfinite(x) for x in [atr.iloc[i], adx.iloc[i], atr_pct.iloc[i]]):
                continue
            if adx.iloc[i] < ADX_MIN or not (VOL_PCT_MIN <= atr_pct.iloc[i] <= VOL_PCT_MAX):
                continue

            # Avoid entering when trend strength is fading.
            if len(adx) >= 4 and adx.iloc[-1] < adx.iloc[-4]:
                continue

            long_touch = _recent_touch(h, l, e20, "Buy")
            short_touch = _recent_touch(h, l, e20, "Sell")
            close, cur_atr = float(c.iloc[i]), float(atr.iloc[i])

            # Strong trend structure + directional DI + bounce confirmation.
            long_ok = (
                long_touch is not None and
                close > e20.iloc[i] > e50.iloc[i] and
                e5.iloc[i] > e20.iloc[i] and
                plus_di.iloc[i] > minus_di.iloc[i] and
                c.iloc[i] > c.iloc[-2]
            )
            short_ok = (
                short_touch is not None and
                close < e20.iloc[i] < e50.iloc[i] and
                e5.iloc[i] < e20.iloc[i] and
                minus_di.iloc[i] > plus_di.iloc[i] and
                c.iloc[i] < c.iloc[-2]
            )

            di_edge = abs(float(plus_di.iloc[i] - minus_di.iloc[i]))
            separation = abs(float(e20.iloc[i] / e50.iloc[i] - 1.0))
            score = float(adx.iloc[i]) + 0.25 * di_edge + 1000 * separation

            if long_ok:
                signals.append({"symbol": sym, "direction": "Buy", "score": score,
                    "atr": cur_atr, "close": close,
                    "stop_price": close - ATR_STOP_MULT * cur_atr,
                    "ema20": float(e20.iloc[i]), "ema50": float(e50.iloc[i]),
                    "adx": float(adx.iloc[i]), "plus_di": float(plus_di.iloc[i]),
                    "minus_di": float(minus_di.iloc[i]), "touch_age": long_touch,
                    "strategy": "advanced_pullback_master"})
            elif short_ok:
                signals.append({"symbol": sym, "direction": "Sell", "score": score,
                    "atr": cur_atr, "close": close,
                    "stop_price": close + ATR_STOP_MULT * cur_atr,
                    "ema20": float(e20.iloc[i]), "ema50": float(e50.iloc[i]),
                    "adx": float(adx.iloc[i]), "plus_di": float(plus_di.iloc[i]),
                    "minus_di": float(minus_di.iloc[i]), "touch_age": short_touch,
                    "strategy": "advanced_pullback_master"})
        except Exception:
            continue

    return sorted(signals, key=lambda x: x["score"], reverse=True)


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    if df is None or len(df) < TREND_EMA + 2:
        return False, ""
    if calendar_days_held >= TIME_STOP_DAYS:
        return True, f"time_stop ({calendar_days_held}d)"

    h, l, c = df["High"], df["Low"], df["Close"]
    e50 = _ema(c, TREND_EMA)
    direction, stop = position["direction"], float(position.get("stop_price", 0))

    if direction == "Buy":
        if c.iloc[-1] < e50.iloc[-1]:
            return True, "trend_break"
        if stop > 0 and l.iloc[-1] <= stop:
            return True, f"hard_stop ({stop:.5f})"
    else:
        if c.iloc[-1] > e50.iloc[-1]:
            return True, "trend_break"
        if stop > 0 and h.iloc[-1] >= stop:
            return True, f"hard_stop ({stop:.5f})"
    return False, ""


def trailing_stop_update(current_stop: float, current_price: float,
                         current_atr: float, direction: str = "Buy") -> float:
    band = ATR_STOP_MULT * current_atr
    if direction == "Buy":
        return max(current_stop, current_price - band)
    return min(current_stop, current_price + band)


def size_position(account_equity: float, atr: float, min_units: int = LOT_ROUND,
                  risk_pct: float | None = None, block_below_min: bool = False) -> int:
    risk_amount = account_equity * (RISK_PCT if risk_pct is None else risk_pct)
    stop_distance = ATR_STOP_MULT * atr
    if stop_distance <= 0:
        return 0 if block_below_min else min_units
    units = int((risk_amount / stop_distance) // min_units) * min_units
    return units if units >= min_units else (0 if block_below_min else min_units)


def scan_summary(market_data: dict) -> list:
    rows = []
    for sym, df in market_data.items():
        if df is None or len(df) < MIN_BARS:
            rows.append({"symbol": sym, "status": "no_data"})
            continue
        try:
            h, l, c = df["High"], df["Low"], df["Close"]
            e20, e50 = _ema(c, 20), _ema(c, 50)
            atr = _atr(h, l, c)
            adx, pdi, mdi = _adx(h, l, c)
            rows.append({"symbol": sym, "close": float(c.iloc[-1]),
                "ema20": float(e20.iloc[-1]), "ema50": float(e50.iloc[-1]),
                "adx": float(adx.iloc[-1]), "plus_di": float(pdi.iloc[-1]),
                "minus_di": float(mdi.iloc[-1]), "status": "ok"})
        except Exception:
            rows.append({"symbol": sym, "status": "error"})
    return rows
