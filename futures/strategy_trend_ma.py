"""
futures/strategy_trend_ma.py
-----------------------------
Futures Strategy 7 — Medium-Term Dual-MA Trend with Trend Strength Filter.

CONCEPT:
  The classic medium-term trend signal from your strategy notes.
  MA(20) vs MA(100) sits between the fast EMA(5/20) and the slow SMA(50/200),
  capturing multi-week trends that last 1–6 weeks — the sweet spot for
  liquid futures (ES, NQ, GC, CL, ZB).

  Trend Strength (TS) normalises the MA gap by price, filtering out
  weak crossovers where the MAs have barely separated. Only trade when
  the gap exceeds 0.3% of price.

  Volatility regime filter: skip entries when 20-day realised vol is in
  the top-quartile of its own 252-day history. High-vol regimes punish
  trend entries with wide stops and frequent whipsaws.

  Trailing stop follows MA(50) ± 1.5×ATR, ratcheting in the favourable
  direction — lets winners run while giving the trend room to breathe.

ENTRY (both directions for GC/ZB; long-only for ES/NQ/CL):
  LONG  : MA20 > MA100  AND  TS > +TS_THRESHOLD  AND  low-vol regime
  SHORT : MA20 < MA100  AND  TS < −TS_THRESHOLD  AND  low-vol regime
         (GC / ZB only for shorts)

EXIT (first condition hit):
  A. Trailing stop: MA50 ± 1.5×ATR ratchet (primary trend exit)
  B. Hard stop: 2×ATR(20) from entry (catastrophic protection)
  C. Time stop: 60 calendar days

SIZING: 1% equity per trade, 2×ATR stop distance.

EXPECTED PERFORMANCE:
  ~15–25 signals/year across 5 markets
  Win rate ~50–55% (trend-following edge comes from large winners, not WR)
  Avg hold ~18–35 days
  Fills the gap between EMA(5/20) [too fast] and SMA(50/200) [too slow]
"""

import numpy as np
import pandas as pd

# ── Parameters ────────────────────────────────────────────────────────────────
FAST_MA        = 20    # fast moving average
SLOW_MA        = 100   # slow moving average
TRAIL_MA       = 50    # MA used for trailing stop
ATR_PERIOD     = 20    # ATR period (20d = one trading month)
ATR_STOP_MULT  = 2.0   # initial stop: 2×ATR from entry
TRAIL_MULT     = 1.5   # trailing stop: MA50 ± 1.5×ATR
TS_THRESHOLD   = 0.003 # trend strength min: 0.3% of price
RISK_PCT       = 0.01  # 1% equity per trade
TIME_STOP_DAYS = 60    # 60 calendar days (~3 months)

# How many bars of vol history to compute the vol percentile
VOL_LOOKBACK   = 252   # 1 year of daily bars
# Skip entries when realised vol is in top fraction of 1-year history
VOL_BLOCK_PCT  = 0.80  # block if current 20d vol > 80th percentile of 1-year vol

LONG_ONLY_MARKETS     = {"ES", "NQ", "CL"}
BIDIRECTIONAL_MARKETS = {"GC", "ZB"}
EQUITY_FUTURES        = {"ES", "NQ"}

MIN_BARS = SLOW_MA + ATR_PERIOD + VOL_LOOKBACK + 5


# ── Indicators ────────────────────────────────────────────────────────────────

def _sma(s: pd.Series, p: int) -> pd.Series:
    return s.rolling(p, min_periods=p).mean()


def _atr(h: pd.Series, l: pd.Series, c: pd.Series,
         period: int = ATR_PERIOD) -> pd.Series:
    prev = c.shift(1)
    tr   = pd.concat([h - l, (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=period).mean()


def _realised_vol(c: pd.Series, window: int = 20) -> pd.Series:
    """Standard deviation of log returns over `window` days."""
    log_ret = np.log(c / c.shift(1))
    return log_ret.rolling(window, min_periods=window).std()


def _vol_percentile(rv: pd.Series, lookback: int = VOL_LOOKBACK) -> pd.Series:
    """Rolling percentile rank of current vol within lookback history."""
    def pct_rank(window):
        cur = window[-1]
        return float(np.sum(window < cur)) / len(window)
    return rv.rolling(lookback, min_periods=lookback // 2).apply(pct_rank, raw=True)


# ── Signal generation ─────────────────────────────────────────────────────────

def generate_signals(market_data: dict, regime_symbol: str = "ES",
                     open_symbols: set | None = None) -> list:
    """
    Return Buy/Sell signals for markets that pass all three filters:
      1. MA(20) vs MA(100) alignment + trend strength threshold
      2. Vol regime: current 20d vol below 80th percentile of 1-year history
      3. Regime filter: ES < SMA200 → skip NEW longs on equity futures
    """
    if open_symbols is None:
        open_symbols = set()

    # Regime filter — is S&P in a confirmed downtrend?
    risk_off = False
    es_df = market_data.get(regime_symbol)
    if es_df is not None and len(es_df) >= 202:
        es_c    = es_df["Close"].dropna()
        sma200  = es_c.rolling(200).mean()
        if not pd.isna(sma200.iloc[-1]):
            risk_off = float(es_c.iloc[-1]) < float(sma200.iloc[-1])

    signals = []
    for sym, df in market_data.items():
        if sym in open_symbols:
            continue
        if df is None or len(df) < MIN_BARS:
            continue

        h, l, c = df["High"], df["Low"], df["Close"]

        ma20  = _sma(c, FAST_MA)
        ma100 = _sma(c, SLOW_MA)
        atr_s = _atr(h, l, c, ATR_PERIOD)
        rv    = _realised_vol(c, 20)
        vol_p = _vol_percentile(rv, VOL_LOOKBACK)

        cur_ma20  = float(ma20.iloc[-1])
        cur_ma100 = float(ma100.iloc[-1])
        cur_price = float(c.iloc[-1])
        cur_atr   = float(atr_s.iloc[-1])
        cur_vol_p = float(vol_p.iloc[-1])

        if any(np.isnan(x) for x in [cur_ma20, cur_ma100, cur_price, cur_atr]):
            continue
        if cur_atr <= 0:
            continue

        # Trend Strength: normalised MA gap
        ts = (cur_ma20 - cur_ma100) / cur_price

        # Vol filter: skip if current vol is in top quintile of last year
        in_high_vol = not np.isnan(cur_vol_p) and cur_vol_p >= VOL_BLOCK_PCT

        # ── LONG ──────────────────────────────────────────────────────────
        if (ts > TS_THRESHOLD
                and not in_high_vol
                and not (risk_off and sym in EQUITY_FUTURES)):
            stop = cur_price - ATR_STOP_MULT * cur_atr
            signals.append({
                "symbol":     sym,
                "direction":  "Buy",
                "score":      ts,
                "close":      round(cur_price, 6),
                "atr":        round(cur_atr,   6),
                "stop_price": round(stop,       6),
                "ts":         round(ts,         5),
                "ma20":       round(cur_ma20,   4),
                "ma100":      round(cur_ma100,  4),
                "vol_pct":    round(cur_vol_p,  3) if not np.isnan(cur_vol_p) else None,
            })

        # ── SHORT (GC / ZB only) ───────────────────────────────────────
        elif (sym in BIDIRECTIONAL_MARKETS
              and ts < -TS_THRESHOLD
              and not in_high_vol):
            stop = cur_price + ATR_STOP_MULT * cur_atr
            signals.append({
                "symbol":     sym,
                "direction":  "Sell",
                "score":      abs(ts),
                "close":      round(cur_price, 6),
                "atr":        round(cur_atr,   6),
                "stop_price": round(stop,       6),
                "ts":         round(ts,         5),
                "ma20":       round(cur_ma20,   4),
                "ma100":      round(cur_ma100,  4),
                "vol_pct":    round(cur_vol_p,  3) if not np.isnan(cur_vol_p) else None,
            })

    signals.sort(key=lambda x: x["score"], reverse=True)
    return signals


# ── Exit logic ────────────────────────────────────────────────────────────────

def should_exit(position: dict, df: pd.DataFrame,
                calendar_days_held: int) -> tuple[bool, str]:
    """
    Exit when:
      A. Trailing stop (MA50 ± 1.5×ATR) is hit
      B. Hard ATR stop (2×ATR from entry)
      C. 60-calendar-day time stop
    """
    if df is None or len(df) < TRAIL_MA + ATR_PERIOD + 5:
        return False, ""

    # C — time stop
    if calendar_days_held >= TIME_STOP_DAYS:
        entry = float(position.get("entry_price", 0))
        now   = float(df["Close"].iloc[-1])
        pct   = (now - entry) / entry * 100 if entry else 0
        return True, f"time_stop ({calendar_days_held}d)  P&L {pct:+.1f}%"

    h, l, c = df["High"], df["Low"], df["Close"]
    cur_low   = float(l.iloc[-1])
    cur_high  = float(h.iloc[-1])
    cur_close = float(c.iloc[-1])

    ma50      = float(_sma(c, TRAIL_MA).iloc[-1])
    cur_atr   = float(_atr(h, l, c, ATR_PERIOD).iloc[-1])
    stop_px   = float(position.get("stop_price", 0))
    direction = position.get("direction", "Buy")
    is_long   = direction == "Buy"

    entry  = float(position.get("entry_price", 1))
    raw_pnl = (cur_close - entry) if is_long else (entry - cur_close)
    pct     = raw_pnl / entry * 100

    if is_long:
        # B — hard stop
        if stop_px > 0 and cur_low <= stop_px:
            return True, f"hard_stop ({stop_px:.4f})  P&L {pct:+.1f}%"
        # A — trailing stop: MA50 − 1.5×ATR
        trail = ma50 - TRAIL_MULT * cur_atr
        if cur_low <= trail:
            return True, f"trail_stop MA50-{TRAIL_MULT}xATR ({trail:.4f})  P&L {pct:+.1f}%"
    else:
        # B — hard stop (above entry for shorts)
        if stop_px > 0 and cur_high >= stop_px:
            return True, f"hard_stop ({stop_px:.4f})  P&L {pct:+.1f}%"
        # A — trailing stop: MA50 + 1.5×ATR
        trail = ma50 + TRAIL_MULT * cur_atr
        if cur_high >= trail:
            return True, f"trail_stop MA50+{TRAIL_MULT}xATR ({trail:.4f})  P&L {pct:+.1f}%"

    return False, ""


# ── Trailing stop update (called each bar by runner) ──────────────────────────

def trailing_stop_update(current_stop: float, current_price: float,
                         current_atr: float, direction: str = "Buy",
                         ma50: float | None = None) -> float:
    """
    Ratchet the trailing stop toward the MA50 band.
    Falls back to ATR-only if ma50 not supplied.
    Never moves the stop against the position.
    """
    if ma50 is not None and ma50 > 0:
        if direction == "Buy":
            new_stop = ma50 - TRAIL_MULT * current_atr
            return max(current_stop, new_stop)
        else:
            new_stop = ma50 + TRAIL_MULT * current_atr
            return min(current_stop, new_stop) if current_stop > 0 else new_stop
    else:
        # ATR-only fallback
        if direction == "Buy":
            return max(current_stop, current_price - ATR_STOP_MULT * current_atr)
        else:
            new_s = current_price + ATR_STOP_MULT * current_atr
            return min(current_stop, new_s) if current_stop > 0 else new_s


# ── Position sizing ───────────────────────────────────────────────────────────

def size_position(account_equity: float, atr: float,
                  contract_size: float = 1.0) -> int:
    """Risk 1% of equity over 2×ATR(20) stop distance."""
    if atr <= 0 or contract_size <= 0 or account_equity <= 0:
        return 1
    risk_amount       = account_equity * RISK_PCT
    risk_per_contract = ATR_STOP_MULT * atr * contract_size
    return max(1, int(risk_amount / risk_per_contract))


# ── Dashboard summary ─────────────────────────────────────────────────────────

def scan_summary(market_data: dict) -> list:
    rows = []
    for sym, df in market_data.items():
        if df is None or len(df) < SLOW_MA + ATR_PERIOD + 5:
            rows.append({"symbol": sym, "status": "no_data"})
            continue
        h, l, c = df["High"], df["Low"], df["Close"]
        ma20  = float(_sma(c, FAST_MA).iloc[-1])
        ma100 = float(_sma(c, SLOW_MA).iloc[-1])
        ma50  = float(_sma(c, TRAIL_MA).iloc[-1])
        atr_v = float(_atr(h, l, c).iloc[-1])
        cur   = float(c.iloc[-1])
        ts    = (ma20 - ma100) / cur if cur else 0

        rv    = _realised_vol(c, 20)
        vp    = _vol_percentile(rv, VOL_LOOKBACK)
        vol_p = float(vp.iloc[-1]) if not pd.isna(vp.iloc[-1]) else None

        bias  = "BULL" if ts > TS_THRESHOLD else "BEAR" if ts < -TS_THRESHOLD else "flat"
        rows.append({
            "symbol":   sym,
            "close":    round(cur, 4),
            "ma20":     round(ma20,  4),
            "ma100":    round(ma100, 4),
            "ma50":     round(ma50,  4),
            "ts":       round(ts, 5),
            "atr":      round(atr_v, 4),
            "vol_pct":  round(vol_p, 2) if vol_p is not None else None,
            "bias":     bias,
            "status":   "ok",
        })
    return rows
