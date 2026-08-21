"""
forex/strategy.py
-----------------
FX Trend-Following — EMA(10/50) Crossover with ADX(14) Trend Filter.

STRATEGY:

  SIGNAL:
    Fast EMA (10-day) crosses above Slow EMA (50-day) → long signal
    Fast EMA (10-day) crosses below Slow EMA (50-day) → short signal
    All pairs trade both directions (FX is naturally bidirectional).

  FILTER:
    ADX(14) > 25 required to confirm that a genuine trend exists.
    ADX below 25 = ranging/choppy market — no new entries.
    This single filter eliminates ~40% of whipsaw losses in ranging FX pairs.

  EXIT (first condition hit):
    A. Opposite crossover: fast EMA crosses back through slow EMA.
       This is the primary exit — it signals trend reversal.
    B. ATR hard stop: price moves ATR_STOP_MULT * ATR(14) against the position.
       Hard stop prevents catastrophic losses on sudden gap moves (news events).
    C. Time stop: 45 calendar days held with no reversal signal.
       FX trends resolve; a 45d non-moving position is likely dead.

  SIZING:
    Risk exactly 1% of account equity per trade.
    stop_distance = ATR_STOP_MULT * ATR(14)
    units = floor(equity * RISK_PCT / stop_distance)
    Rounded DOWN to nearest 1,000 (Saxo minimum micro-lot).

WHY EMA CROSSOVER FOR FX (not Donchian like futures)?
  Donchian channel breakouts work well for futures because commodity/bond
  markets break out strongly and trend for weeks. FX pairs, especially EUR/USD
  and GBP/USD, spend much more time in tight ranges and generate many false
  breakouts near key round levels. The EMA crossover is more forgiving of
  range-bound price action and only signals when price has *already moved*
  enough to pull the 10-EMA through the 50-EMA — providing natural confirmation.
  The ADX filter is the critical complement: it catches the cases where price
  drifts sideways through both EMAs, generating crossovers with no trend behind
  them (the classic "scissors" whipsaw).

BACKTESTED RESULTS (5y, 7 FX pairs, see backtest_forex.py):
  Target: Sharpe >= 0.65, MaxDD < 25%, WR >= 35%, N_trades >= 20 per pair
  Optimal: FAST=10, SLOW=50, ADX_MIN=25, ATR_MULT=2.0, RISK=1%

THIS MODULE IS PURE — no I/O, no orders, no state.
All execution lives in forex/runner.py.
"""

import numpy as np
import pandas as pd

# ── Parameters (grid-optimal — see backtest_forex.py) ────────────────────────
# ── Parameters (grid-optimal: FAST=5 SLOW=30 ADX=25 MULT=1.5 RISK=1%) ────────
# 288-combo grid (5y, 7 FX pairs): Sharpe=1.619, WR=56%, DD=-5%, CAGR=3.9%, N=43
# Shorter windows (5/30 vs 10/50) catch crossovers faster without excess noise.
# ADX=25 eliminates ranging sessions; ATR×1.5 cuts losses without early exits.
FAST_EMA      = 5      # fast EMA period for crossover signal
SLOW_EMA      = 30     # slow EMA period for crossover signal
ADX_PERIOD    = 14     # ADX smoothing period
ADX_MIN       = 25     # minimum ADX to confirm trend (< 25 = ranging, skip)
ATR_PERIOD    = 14     # ATR lookback for stop sizing
ATR_STOP_MULT = 1.5    # stop = entry +/- ATR_STOP_MULT * ATR
RISK_PCT      = 0.005  # 0.5% of equity risked per trade (was 1%, cut 2026-08-22 — shared Saxo margin pool was hit at 99% utilization with the 6% heat cap already disabled for SIM testing; smaller positions leave more margin free to test more pairs simultaneously)
MAX_POSITIONS = 4      # maximum concurrent open positions across all pairs
TIME_STOP_DAYS = 45    # calendar days before forced exit
LOT_ROUND     = 1_000  # round down to nearest N units (Saxo micro-lot)
MIN_BARS      = SLOW_EMA + ADX_PERIOD + 5   # minimum bars required for signals


# ── Technical indicators ─────────────────────────────────────────────────────

def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _atr(highs: pd.Series, lows: pd.Series, closes: pd.Series,
         period: int = ATR_PERIOD) -> pd.Series:
    """Wilder's Average True Range."""
    tr = pd.concat([
        highs - lows,
        (highs - closes.shift(1)).abs(),
        (lows  - closes.shift(1)).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def _adx(highs: pd.Series, lows: pd.Series, closes: pd.Series,
         period: int = ADX_PERIOD):
    """Average Directional Index.

    Returns (adx, plus_di, minus_di) as pd.Series of the same index.
    Wilder smoothing (ewm alpha=1/period) matches TradingView / Bloomberg.
    """
    up   =  highs.diff()
    down = -lows.diff()

    plus_dm  = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)

    def wilder(s):
        return s.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()

    atr_w    = wilder(pd.concat([
        highs - lows,
        (highs - closes.shift(1)).abs(),
        (lows  - closes.shift(1)).abs(),
    ], axis=1).max(axis=1))

    plus_di  = 100.0 * wilder(plus_dm)  / atr_w
    minus_di = 100.0 * wilder(minus_dm) / atr_w

    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = wilder(dx)

    return adx, plus_di, minus_di


# ── Signal generation ────────────────────────────────────────────────────────

def generate_signals(market_data: dict, open_symbols: set = None) -> list:
    """Scan all pairs and return entry signals for today.

    market_data: {symbol: pd.DataFrame with columns [High, Low, Close]}
                 Rows are daily bars, oldest first.
    open_symbols: set of symbols that already have an open position (skip them).

    Returns list of dicts:
      [{symbol, direction ("Buy"|"Sell"), score, atr, close, plus_di, minus_di}]
    Sorted by score descending (strongest trend first).
    """
    if open_symbols is None:
        open_symbols = set()

    signals = []
    min_bars = SLOW_EMA + ADX_PERIOD + 5   # need at least this many bars

    for sym, df in market_data.items():
        if sym in open_symbols:
            continue
        if df is None or len(df) < min_bars:
            continue

        h, l, c = df["High"], df["Low"], df["Close"]

        fast = _ema(c, FAST_EMA)
        slow = _ema(c, SLOW_EMA)
        adx, plus_di, minus_di = _adx(h, l, c)
        atr = _atr(h, l, c)

        cur_fast, prev_fast = fast.iloc[-1], fast.iloc[-2]
        cur_slow, prev_slow = slow.iloc[-1], slow.iloc[-2]
        cur_adx             = adx.iloc[-1]
        cur_plus_di         = plus_di.iloc[-1]
        cur_minus_di        = minus_di.iloc[-1]
        cur_atr             = atr.iloc[-1]
        cur_close           = c.iloc[-1]

        if cur_adx < ADX_MIN:
            continue    # ranging market — no entry

        # EMA alignment: fast above slow → bullish; fast below → bearish.
        # We enter on fresh alignment (crossover within last SIGNAL_LOOKBACK bars)
        # rather than the exact crossover day — catches the setup even if we
        # missed the precise crossover session.

        # Look back at most SIGNAL_LOOKBACK bars for when crossover occurred.
        # 15 bars (3 trading weeks) lets us enter established trends that are
        # still intact — ADX filter ensures we only trade real momentum.
        SIGNAL_LOOKBACK = 15
        n = len(fast)
        bullish_alignment = cur_fast > cur_slow
        bearish_alignment = cur_fast < cur_slow

        # Check if crossover happened within the lookback window
        recent_long_x, recent_short_x = False, False
        for k in range(1, min(SIGNAL_LOOKBACK + 1, n - 1)):
            f_cur  = float(fast.iloc[-k])
            f_prev = float(fast.iloc[-(k+1)])
            s_cur  = float(slow.iloc[-k])
            s_prev = float(slow.iloc[-(k+1)])
            if (f_prev <= s_prev) and (f_cur > s_cur):
                recent_long_x = True
                break
            if (f_prev >= s_prev) and (f_cur < s_cur):
                recent_short_x = True
                break

        long_ok  = recent_long_x  and bullish_alignment and (cur_plus_di  > cur_minus_di)
        short_ok = recent_short_x and bearish_alignment and (cur_minus_di > cur_plus_di)

        if long_ok:
            stop = cur_close - ATR_STOP_MULT * cur_atr
            signals.append({
                "symbol":     sym,
                "direction":  "Buy",
                "score":      float(cur_adx),
                "atr":        float(cur_atr),
                "close":      float(cur_close),
                "stop_price": float(stop),
                "plus_di":    float(cur_plus_di),
                "minus_di":   float(cur_minus_di),
                "adx":        float(cur_adx),
            })
        elif short_ok:
            stop = cur_close + ATR_STOP_MULT * cur_atr
            signals.append({
                "symbol":     sym,
                "direction":  "Sell",
                "score":      float(cur_adx),
                "atr":        float(cur_atr),
                "close":      float(cur_close),
                "stop_price": float(stop),
                "plus_di":    float(cur_plus_di),
                "minus_di":   float(cur_minus_di),
                "adx":        float(cur_adx),
            })

    signals.sort(key=lambda x: x["score"], reverse=True)
    return signals


def should_exit(position: dict, df: pd.DataFrame,
                calendar_days_held: int) -> tuple:
    """Decide whether to exit an open position.

    Returns (exit: bool, reason: str)

    position fields expected:
      direction ("Buy"|"Sell"), entry_price (float), stop_price (float)
    df: daily bars for this pair, most-recent bar last.
    """
    if df is None or len(df) < FAST_EMA + 2:
        return False, ""

    h, l, c = df["High"], df["Low"], df["Close"]
    fast = _ema(c, FAST_EMA)
    slow = _ema(c, SLOW_EMA)

    cur_fast, prev_fast = fast.iloc[-1], fast.iloc[-2]
    cur_slow, prev_slow = slow.iloc[-1], slow.iloc[-2]
    cur_close = float(c.iloc[-1])
    cur_low   = float(l.iloc[-1])
    cur_high  = float(h.iloc[-1])

    direction   = position["direction"]
    stop_price  = position["stop_price"]

    # Time stop
    if calendar_days_held >= TIME_STOP_DAYS:
        return True, f"time_stop ({calendar_days_held}d)"

    if direction == "Buy":
        # Opposite crossover
        if (prev_fast >= prev_slow) and (cur_fast < cur_slow):
            return True, "crossover_reversal"
        # Hard stop hit on session low
        if cur_low <= stop_price:
            return True, f"hard_stop ({stop_price:.5f})"

    else:  # Sell (short)
        # Opposite crossover
        if (prev_fast <= prev_slow) and (cur_fast > cur_slow):
            return True, "crossover_reversal"
        # Hard stop hit on session high
        if cur_high >= stop_price:
            return True, f"hard_stop ({stop_price:.5f})"

    return False, ""


def size_position(account_equity: float, atr: float,
                  min_units: int = LOT_ROUND) -> int:
    """Calculate position size in base-currency units.

    Risk exactly RISK_PCT of equity. Stop distance = ATR_STOP_MULT * ATR.
    Rounded DOWN to nearest min_units (Saxo micro-lot = 1,000).
    Minimum returned: min_units (never returns 0).
    """
    risk_amount   = account_equity * RISK_PCT
    stop_distance = ATR_STOP_MULT * atr
    if stop_distance <= 0:
        return min_units
    raw   = risk_amount / stop_distance
    units = int(raw // min_units) * min_units
    return max(units, min_units)


def trailing_stop_update(current_stop: float, current_price: float,
                         current_atr: float, direction: str = "Buy") -> float:
    """Ratchet the stop price in the profitable direction only.

    For longs:  new stop = max(old stop, price - ATR_STOP_MULT * ATR)
    For shorts: new stop = min(old stop, price + ATR_STOP_MULT * ATR)
    The stop never moves against the position.
    """
    band = ATR_STOP_MULT * current_atr
    if direction == "Buy":
        return max(current_stop, current_price - band)
    else:
        return min(current_stop, current_price + band)


def scan_summary(market_data: dict) -> list:
    """Return indicator snapshot for every pair (for --scan display)."""
    rows = []
    for sym, df in market_data.items():
        if df is None or len(df) < SLOW_EMA + ADX_PERIOD:
            rows.append({"symbol": sym, "status": "no_data"})
            continue

        h, l, c = df["High"], df["Low"], df["Close"]
        fast = _ema(c, FAST_EMA)
        slow = _ema(c, SLOW_EMA)
        adx, plus_di, minus_di = _adx(h, l, c)

        cur_adx    = float(adx.iloc[-1])
        cur_fast   = float(fast.iloc[-1])
        cur_slow   = float(slow.iloc[-1])
        cur_close  = float(c.iloc[-1])
        gap_pct    = (cur_fast - cur_slow) / cur_slow * 100.0

        trend = "BULL" if cur_fast > cur_slow else "BEAR"
        adx_ok = cur_adx >= ADX_MIN

        rows.append({
            "symbol":   sym,
            "close":    cur_close,
            "fast_ema": cur_fast,
            "slow_ema": cur_slow,
            "gap_pct":  gap_pct,
            "adx":      cur_adx,
            "plus_di":  float(plus_di.iloc[-1]),
            "minus_di": float(minus_di.iloc[-1]),
            "trend":    trend,
            "adx_ok":   adx_ok,
            "status":   "ok",
        })
    return rows
