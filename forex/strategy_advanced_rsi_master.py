"""
Advanced RSI(2) Pullback Master.

A selective upgrade of strategy_rsi.py. This does not guarantee a higher
win rate; filters and parameters must be validated with walk-forward,
out-of-sample testing before replacing the current LIVE strategy.

Key changes:
- robust RSI handling for one-sided moves
- EMA50/EMA200 trend alignment + EMA200 slope
- minimum distance from EMA200 to avoid regime-boundary trades
- ATR-percentile volatility filter
- reversal confirmation after the RSI(2) extreme
- directional DI confirmation
- fixed hard stop and explicit no-op trailing stop for a short mean-reversion hold

INTEGRATION (2026-08-30, user request "implement all these too on ATOS SIM
along with our original strategies"): registered as STRATEGIES key
"advanced_rsi_master" in forex/runner.py, in parallel with the original
"rsi" (forex/strategy_rsi.py, UNTOUCHED) for an A/B comparison. **SIM ONLY**
-- deliberately NOT in LIVE_ALLOWED_STRATEGIES or
LIVE_EUR_ALLOWED_STRATEGIES (the LIVE_EUR account keeps running the
original "rsi"; this is a shadow/A/B only). Uncapped slots (mirrors "rsi"
-- MAX_POSITIONS=4 in this file is NOT enforced by the runner, same as
the original "rsi"/"ema"/etc). MIN_BARS 272 fits the 500-bar fetch;
trailing_stop_update is an explicit no-op (short mean-reversion hold), no
runner exit-loop change. EXEMPT from the momentum pre-filter (added to
_NO_MOMENTUM_FILTER), same as the original "rsi".
"""

import numpy as np
import pandas as pd

RSI_PERIOD = 2
RSI_OVERSOLD = 10
RSI_OVERBOUGHT = 90
RSI_EXIT_LONG = 55
RSI_EXIT_SHORT = 45

TREND_EMA = 200
FAST_EMA = 50
TREND_SLOPE_BARS = 10
MIN_TREND_DISTANCE_ATR = 0.35

ATR_PERIOD = 14
ADX_PERIOD = 14
DI_ADX_MIN = 18

VOL_LOOKBACK = 252
VOL_PCT_MIN = 0.15
VOL_PCT_MAX = 0.90

ATR_STOP_MULT = 1.5
RISK_PCT = 0.0025
MAX_POSITIONS = 4
TIME_STOP_DAYS = 10
LOT_ROUND = 1_000

# Enough history for EMA regime and volatility percentile.
MIN_BARS = max(TREND_EMA + 30, VOL_LOOKBACK + 20)


def _rsi(closes: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    """Wilder RSI with explicit handling of zero gain/loss."""
    delta = closes.diff()
    gain = delta.clip(lower=0).ewm(
        alpha=1.0 / period, adjust=False, min_periods=period
    ).mean()
    loss = (-delta.clip(upper=0)).ewm(
        alpha=1.0 / period, adjust=False, min_periods=period
    ).mean()

    rs = gain / loss.replace(0.0, np.nan)
    rsi = 100.0 - 100.0 / (1.0 + rs)

    # A pure up sequence should be RSI=100, not NaN; pure down should be 0.
    rsi = rsi.mask((loss == 0) & (gain > 0), 100.0)
    rsi = rsi.mask((gain == 0) & (loss > 0), 0.0)
    rsi = rsi.mask((gain == 0) & (loss == 0), 50.0)
    return rsi


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _atr(highs: pd.Series, lows: pd.Series, closes: pd.Series,
         period: int = ATR_PERIOD) -> pd.Series:
    prev = closes.shift(1)
    tr = pd.concat([
        highs - lows,
        (highs - prev).abs(),
        (lows - prev).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False,
                  min_periods=period).mean()


def _adx_di(highs: pd.Series, lows: pd.Series, closes: pd.Series,
            period: int = ADX_PERIOD):
    up_move = highs.diff()
    down_move = -lows.diff()

    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

    prev = closes.shift(1)
    tr = pd.concat([
        highs - lows,
        (highs - prev).abs(),
        (lows - prev).abs(),
    ], axis=1).max(axis=1)

    def smooth(x):
        return x.ewm(alpha=1.0 / period, adjust=False,
                     min_periods=period).mean()

    atr = smooth(tr)
    plus_di = 100.0 * smooth(plus_dm) / (atr + 1e-10)
    minus_di = 100.0 * smooth(minus_dm) / (atr + 1e-10)
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di + 1e-10)
    adx = smooth(dx)
    return adx, plus_di, minus_di


def _volatility_percentile(atr: pd.Series) -> float:
    """Current ATR percentile within the recent regime."""
    rank = atr.rolling(VOL_LOOKBACK, min_periods=100).rank(pct=True)
    return float(rank.iloc[-1])


def generate_signals(market_data: dict, open_symbols: set = None) -> list:
    """Return selective RSI(2) pullback signals."""
    if open_symbols is None:
        open_symbols = set()

    signals = []

    for sym, df in market_data.items():
        if sym in open_symbols or df is None or len(df) < MIN_BARS:
            continue

        try:
            h, l, c = df["High"], df["Low"], df["Close"]
            rsi = _rsi(c)
            ema50 = _ema(c, FAST_EMA)
            ema200 = _ema(c, TREND_EMA)
            atr = _atr(h, l, c)
            adx, plus_di, minus_di = _adx_di(h, l, c)

            i = -1
            prev_i = -2
            cur_rsi = float(rsi.iloc[i])
            prev_rsi = float(rsi.iloc[prev_i])
            close = float(c.iloc[i])
            prev_close = float(c.iloc[prev_i])
            cur_atr = float(atr.iloc[i])
            cur_ema50 = float(ema50.iloc[i])
            cur_ema200 = float(ema200.iloc[i])
            adx_val = float(adx.iloc[i])
            pdi = float(plus_di.iloc[i])
            mdi = float(minus_di.iloc[i])
            vol_pct = _volatility_percentile(atr)

            vals = [cur_rsi, prev_rsi, close, cur_atr, cur_ema50,
                    cur_ema200, adx_val, pdi, mdi, vol_pct]
            if not all(np.isfinite(v) for v in vals) or cur_atr <= 0:
                continue
            if not (VOL_PCT_MIN <= vol_pct <= VOL_PCT_MAX):
                continue

            ema_slope = float(ema200.iloc[-1] - ema200.iloc[-1-TREND_SLOPE_BARS])
            trend_distance_atr = abs(close - cur_ema200) / cur_atr

            bull_regime = (
                close > cur_ema200
                and cur_ema50 > cur_ema200
                and ema_slope > 0
                and trend_distance_atr >= MIN_TREND_DISTANCE_ATR
                and adx_val >= DI_ADX_MIN
                and pdi > mdi
            )
            bear_regime = (
                close < cur_ema200
                and cur_ema50 < cur_ema200
                and ema_slope < 0
                and trend_distance_atr >= MIN_TREND_DISTANCE_ATR
                and adx_val >= DI_ADX_MIN
                and mdi > pdi
            )

            # Confirmation: today's bar must show the pullback has stopped
            # accelerating, while RSI remains at an extreme.
            long_reversal = close > prev_close and cur_rsi >= prev_rsi
            short_reversal = close < prev_close and cur_rsi <= prev_rsi

            if bull_regime and cur_rsi <= RSI_OVERSOLD and long_reversal:
                stop = close - ATR_STOP_MULT * cur_atr
                extremity = (RSI_OVERSOLD - cur_rsi) / max(RSI_OVERSOLD, 1)
                recovery = max(cur_rsi - prev_rsi, 0) / 100.0
                score = extremity + recovery + min(adx_val, 40) / 200.0
                signals.append({
                    "symbol": sym, "direction": "Buy", "score": float(score),
                    "rsi": cur_rsi, "prev_rsi": prev_rsi,
                    "adx": adx_val, "atr": cur_atr, "close": close,
                    "stop_price": float(stop),
                    "vol_pct": vol_pct,
                    "trend_distance_atr": trend_distance_atr,
                })

            elif bear_regime and cur_rsi >= RSI_OVERBOUGHT and short_reversal:
                stop = close + ATR_STOP_MULT * cur_atr
                extremity = (cur_rsi - RSI_OVERBOUGHT) / max(100-RSI_OVERBOUGHT, 1)
                recovery = max(prev_rsi - cur_rsi, 0) / 100.0
                score = extremity + recovery + min(adx_val, 40) / 200.0
                signals.append({
                    "symbol": sym, "direction": "Sell", "score": float(score),
                    "rsi": cur_rsi, "prev_rsi": prev_rsi,
                    "adx": adx_val, "atr": cur_atr, "close": close,
                    "stop_price": float(stop),
                    "vol_pct": vol_pct,
                    "trend_distance_atr": trend_distance_atr,
                })
        except (KeyError, ValueError, TypeError, IndexError):
            continue

    return sorted(signals, key=lambda x: x["score"], reverse=True)


def should_exit(position: dict, df: pd.DataFrame,
                calendar_days_held: int) -> tuple:
    if df is None or len(df) < RSI_PERIOD + 2:
        return False, ""

    h, l, c = df["High"], df["Low"], df["Close"]
    cur_rsi = float(_rsi(c).iloc[-1])
    cur_high = float(h.iloc[-1])
    cur_low = float(l.iloc[-1])
    stop_px = float(position.get("stop_price", 0))
    direction = position.get("direction")

    if calendar_days_held >= TIME_STOP_DAYS:
        return True, f"time_stop ({calendar_days_held}d)"

    # Stop check before indicator exit: conservative when OHLC can imply both.
    if direction == "Buy":
        if stop_px > 0 and cur_low <= stop_px:
            return True, f"hard_stop ({stop_px:.5f})"
        if cur_rsi >= RSI_EXIT_LONG:
            return True, f"rsi_recovery ({cur_rsi:.1f}>={RSI_EXIT_LONG})"
    else:
        if stop_px > 0 and cur_high >= stop_px:
            return True, f"hard_stop ({stop_px:.5f})"
        if cur_rsi <= RSI_EXIT_SHORT:
            return True, f"rsi_recovery ({cur_rsi:.1f}<={RSI_EXIT_SHORT})"

    return False, ""


def size_position(account_equity: float, atr: float,
                  min_units: float = LOT_ROUND, risk_pct: float | None = None,
                  block_below_min: bool = False) -> int:
    risk_amount = account_equity * (
        RISK_PCT if risk_pct is None else risk_pct
    )
    stop_distance = ATR_STOP_MULT * atr
    if stop_distance <= 0:
        return 0 if block_below_min else int(min_units)

    raw = risk_amount / stop_distance
    floored = int(raw / min_units) * int(min_units)
    if floored < min_units:
        return 0 if block_below_min else int(min_units)
    return floored


def trailing_stop_update(current_stop: float, current_price: float,
                         current_atr: float, direction: str = "Buy") -> float:
    """Explicitly disabled for this short mean-reversion strategy."""
    return current_stop


def scan_summary(market_data: dict) -> list:
    rows = []
    for sym, df in market_data.items():
        if df is None or len(df) < MIN_BARS:
            rows.append({"symbol": sym, "status": "no_data"})
            continue
        try:
            h, l, c = df["High"], df["Low"], df["Close"]
            rsi = _rsi(c)
            ema50 = _ema(c, FAST_EMA)
            ema200 = _ema(c, TREND_EMA)
            atr = _atr(h, l, c)
            adx, pdi, mdi = _adx_di(h, l, c)
            close = float(c.iloc[-1])
            r = float(rsi.iloc[-1])
            a = float(atr.iloc[-1])
            e200 = float(ema200.iloc[-1])
            slope = float(ema200.iloc[-1] - ema200.iloc[-1-TREND_SLOPE_BARS])
            dist = abs(close-e200) / a if a > 0 else np.nan
            rows.append({
                "symbol": sym, "status": "ok", "close": close,
                "rsi2": r, "ema50": float(ema50.iloc[-1]),
                "ema200": e200, "adx": float(adx.iloc[-1]),
                "plus_di": float(pdi.iloc[-1]), "minus_di": float(mdi.iloc[-1]),
                "trend": "BULL" if close > e200 else "BEAR",
                "ema200_slope": slope, "trend_distance_atr": dist,
                "vol_pct": _volatility_percentile(atr),
                "flag": "OS" if r <= RSI_OVERSOLD else
                        ("OB" if r >= RSI_OVERBOUGHT else ""),
            })
        except (KeyError, ValueError, TypeError, IndexError):
            rows.append({"symbol": sym, "status": "error"})
    return rows
