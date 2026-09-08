# AI-WRITTEN Phase 2+3 2026-09-05 by claude-sonnet-5
# Entry filter: none -- Phase 2 pass-through only, no entry override yet
# Exit filter: require 2nd consecutive close beyond hard_stop before confirming exit, unless breach is severe (>0.3*ATR)

import pandas as pd
from forex.strategy_advanced_ml import generate_signals as _orig_generate_signals
from forex.strategy_advanced_ml import should_exit as _orig_should_exit


def generate_signals(df: pd.DataFrame, symbol: str):
    return _orig_generate_signals(df, symbol)


def _atr14(df: pd.DataFrame, period: int = 14):
    h = df['High']
    l = df['Low']
    c = df['Close']
    prev = c.shift(1)
    tr = pd.concat([h - l, (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    exit_flag, reason = _orig_should_exit(position, df, calendar_days_held)

    if not exit_flag:
        return exit_flag, reason

    if reason == 'hard_stop':
        stop = position.get('stop_price')
        direction = str(position.get('direction', '')).lower()
        close = df['Close']

        if stop is not None and direction in ('long', 'buy', 'short', 'sell') and len(close) >= 2:
            atr_series = _atr14(df)
            last_atr = atr_series.iloc[-1]
            last_close = close.iloc[-1]
            prev_close = close.iloc[-2]

            is_long = direction in ('long', 'buy')

            if is_long:
                breach_dist = stop - last_close
                prev_breached = prev_close <= stop
            else:
                breach_dist = last_close - stop
                prev_breached = prev_close >= stop

            severe = pd.notna(last_atr) and breach_dist > 0.3 * last_atr

            if not severe and not prev_breached:
                # Only today's close breached the stop -- wait for a second
                # consecutive confirming close before exiting, unless the
                # breach is already severe (>0.3 ATR through the stop).
                position['_hard_stop_confirm_pending'] = True
                return False, 'hard_stop_pending_confirmation'

    return exit_flag, reason
