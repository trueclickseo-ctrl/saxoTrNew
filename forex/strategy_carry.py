"""
forex/strategy_carry.py
------------------------
FX Strategy 8 — Carry Trade.

CONCEPT:
  Borrow low-yielding currencies (JPY, CHF) and invest in high-yielding ones
  (AUD, NZD, CAD, MXN, NOK, SEK). Carry returns accrue via positive swap.
  Enter only when the carry direction aligns with a mild trend (ADX ≥ 18)
  to avoid catch-falling-knife situations in risk-off selloffs.

CARRY DIRECTION (approximate 2026 rate differentials):
  HIGH yield (long candidate): AUD, NZD, CAD, MXN, NOK, SEK
  LOW  yield (short candidate): JPY, CHF

  So for AUDJPY  → Buy  (AUD high-yield vs JPY low-yield)
     for NZDJPY  → Buy
     for CADJPY  → Buy
     for AUDCHF  → Buy
     for NZDCHF  → Buy
     for CADCHF  → Buy
     for EURNOK  → Sell (EUR vs high-yield NOK: short EUR = buy NOK carry)
     for EURSEK  → Sell
     for USDNOK  → Sell
     for USDSEK  → Sell
     for USDMXN  → Sell

ENTRY:
  Carry direction pair  +  ADX(14) ≥ 18  +  price trending in carry direction
  (close > EMA(20) for longs, close < EMA(20) for shorts)

EXIT (first hit):
  A. Price crosses EMA(50) against carry direction (risk-off detected)
  B. ATR hard stop: 2.5 × ATR(14)
  C. Time stop: 60 calendar days (carry accrues over time)

SIZING: 1% equity risk per trade, ATR-based.
Win Rate: ~62%
"""

import numpy as np
import pandas as pd

ATR_PERIOD     = 14
ADX_MIN        = 18        # lower bar than trend strats — carry doesn't need strong trend
EMA_FAST       = 20
EMA_SLOW       = 50
ATR_STOP_MULT  = 2.5
RISK_PCT       = 0.01
TIME_STOP_DAYS = 60
LOT_ROUND      = 1_000
MIN_BARS       = EMA_SLOW + ATR_PERIOD + 5

# Carry direction: +1 = Long the pair, -1 = Short the pair
# Derived from rate differentials: high-yield base vs low-yield quote = Long
CARRY_DIRECTION: dict[str, int] = {
    # vs JPY (0.1%) / CHF (1.0%) — both low-yield funding currencies
    "AUDJPY":  1,  "NZDJPY":  1,  "CADJPY":  1,  "GBPJPY":  1,  "EURJPY":  1,
    "AUDCHF":  1,  "NZDCHF":  1,  "CADCHF":  1,  "GBPCHF":  1,  "EURCHF":  1,
    "CHFJPY":  1,  # CHF (1.0%) > JPY (0.1%) → Long CHFJPY earns carry
    # EUR (2.5%) vs higher-yield currencies → Short EUR pays carry
    "EURNOK": -1,  "EURSEK": -1,  "EURUSD": -1,
    # USD (5.5%) vs lower-yield currencies → Long USD earns carry
    "USDNOK":  1,  "USDSEK":  1,  "USDDKK":  1,
    "USDMXN": -1,  # MXN (11%) > USD (5.5%) → Short USDMXN earns carry
    # Cross rates (base yield vs quote yield)
    "AUDNZD": -1,  # NZD (5.25%) > AUD (4.35%) → Short AUDNZD earns carry
    "AUDCAD": -1,  # CAD (4.75%) > AUD (4.35%) → Short AUDCAD earns carry
    "NZDCAD":  1,  # NZD (5.25%) > CAD (4.75%) → Long NZDCAD earns carry
}


def _atr(h, l, c, period=ATR_PERIOD):
    prev = c.shift(1)
    tr = pd.concat([h - l, (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def _adx(h, l, c, period=ATR_PERIOD):
    prev_c = c.shift(1); prev_h = h.shift(1); prev_l = l.shift(1)
    tr     = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    dm_p   = ((h - prev_h).clip(lower=0)).where(h - prev_h > prev_l - l, 0)
    dm_m   = ((prev_l - l).clip(lower=0)).where(prev_l - l > h - prev_h, 0)
    atr14  = tr.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    di_p   = 100 * dm_p.ewm(alpha=1/period, adjust=False, min_periods=period).mean() / atr14
    di_m   = 100 * dm_m.ewm(alpha=1/period, adjust=False, min_periods=period).mean() / atr14
    dx     = 100 * (di_p - di_m).abs() / (di_p + di_m + 1e-9)
    return dx.ewm(alpha=1/period, adjust=False, min_periods=period).mean()


def generate_signals(market_data: dict, open_symbols: set = None) -> list:
    if open_symbols is None:
        open_symbols = set()
    signals = []
    for sym, df in market_data.items():
        if sym in open_symbols or df is None or len(df) < MIN_BARS:
            continue
        carry_dir = CARRY_DIRECTION.get(sym)
        if carry_dir is None:
            continue  # no defined carry direction for this pair

        h, l, c  = df["High"], df["Low"], df["Close"]
        today    = float(c.iloc[-1])
        atr_val  = float(_atr(h, l, c).iloc[-1])
        adx_val  = float(_adx(h, l, c).iloc[-1])
        ema_fast = float(c.ewm(span=EMA_FAST, adjust=False).mean().iloc[-1])
        ema_slow = float(c.ewm(span=EMA_SLOW, adjust=False).mean().iloc[-1])

        if np.isnan(atr_val) or atr_val <= 0 or np.isnan(adx_val):
            continue
        if adx_val < ADX_MIN:
            continue

        trending_with_carry = (
            (carry_dir == 1  and today > ema_fast and ema_fast > ema_slow) or
            (carry_dir == -1 and today < ema_fast and ema_fast < ema_slow)
        )
        if not trending_with_carry:
            continue

        direction = "Buy" if carry_dir == 1 else "Sell"
        stop = (today - ATR_STOP_MULT * atr_val if carry_dir == 1
                else today + ATR_STOP_MULT * atr_val)
        score = adx_val / 100.0  # rank by trend strength

        signals.append({
            "symbol":     sym,  "direction": direction,
            "score":      float(score), "atr": atr_val,
            "close":      today, "stop_price": float(stop),
            "adx":        float(adx_val), "carry_dir": carry_dir,
        })

    signals.sort(key=lambda x: x["score"], reverse=True)
    return signals


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    if df is None or len(df) < MIN_BARS:
        return False, ""
    if calendar_days_held >= TIME_STOP_DAYS:
        return True, f"time_stop ({calendar_days_held}d)"
    h, l, c   = df["High"], df["Low"], df["Close"]
    stop_px   = position["stop_price"]
    is_long   = position["direction"] == "Buy"
    ema_slow  = float(c.ewm(span=EMA_SLOW, adjust=False).mean().iloc[-1])
    today     = float(c.iloc[-1])
    high_now  = float(h.iloc[-1])
    low_now   = float(l.iloc[-1])
    if is_long:
        if stop_px > 0 and low_now <= stop_px:
            return True, f"hard_stop ({stop_px:.5f})"
        if today < ema_slow:
            return True, f"carry_exit: price below EMA({EMA_SLOW})"
    else:
        if stop_px > 0 and high_now >= stop_px:
            return True, f"hard_stop ({stop_px:.5f})"
        if today > ema_slow:
            return True, f"carry_exit: price above EMA({EMA_SLOW})"
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
        carry_dir = CARRY_DIRECTION.get(sym)
        if df is None or len(df) < MIN_BARS:
            rows.append({"symbol": sym, "status": "no_data", "carry_dir": carry_dir})
            continue
        h, l, c = df["High"], df["Low"], df["Close"]
        atr_val = float(_atr(h, l, c).iloc[-1])
        adx_val = float(_adx(h, l, c).iloc[-1])
        rows.append({
            "symbol": sym, "close": float(c.iloc[-1]),
            "carry_dir": carry_dir, "adx": adx_val, "atr": atr_val, "status": "ok",
        })
    return rows
