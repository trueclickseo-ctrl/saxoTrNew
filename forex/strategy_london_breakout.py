"""
forex/strategy_london_breakout.py
----------------------------------
Day Trading Strategy — London & NY Session Breakout.

CONCEPT:
  Markets compress during low-liquidity sessions (Asia, London morning) and
  then release directionally when institutional flows kick in at session opens.
  Trading the BREAKOUT of that compression range captures the first strong
  directional move with a clear invalidation level (the other side of the range).

SESSION LOGIC:
  London open  (07:00 UTC):  break of Asian session range   (00:00–06:59 UTC)
  NY open      (13:00 UTC):  break of London morning range  (09:00–12:59 UTC)

ENTRY:
  BUY  : H1 close > range_high  (bullish breakout confirmed on close)
  SELL : H1 close < range_low   (bearish breakout confirmed on close)
  Minimum range required: MIN_RANGE_PIPS (avoids trading flat / thin sessions)

EXIT:
  A. Take profit  : 2.0× range size above/below entry
  B. Stop loss    : opposite side of range (hard stop)
  C. Time stop    : close position by END_HOUR UTC (no overnight holds)

SIZING (dedicated day-trading book — config: forex.lbo_capital_eur, 15,000 SEK):
  Risk per trade = RISK_PCT (1.5%) of the LBO book, NOT of the whole account.
  Units = (book_equity_in_quote_ccy × RISK_PCT) / stop_distance
  The runner passes equity_by_pair already converted into each pair's QUOTE
  currency, because stop_distance is quoted there — sizing off an unconverted
  figure gave JPY-quoted pairs ~150x fewer units for the same nominal risk.
  Capped at MAX_UNITS; signals whose correct size is below MIN_UNITS are SKIPPED
  rather than floored up (flooring silently over-risks the trade).

PAIRS:
  28 liquid majors + crosses (all of forex/universe.py except the 6 illiquid
  Scandi/exotic pairs — EURNOK, EURSEK, USDNOK, USDSEK, USDDKK, USDMXN — whose
  wide spreads and thin session ranges don't suit a tight breakout/2:1 RR).
  Expanded 2026-08-20 from the original 7 (EURUSD, GBPUSD, USDJPY, EURGBP,
  GBPJPY, AUDUSD, USDCAD) after a live run found 0 signals on the narrow set —
  more pairs means more chances of a genuine Asian-range break each session.
  SLOTS raised 10 -> 28 on 2026-08-21 (one slot per pair) so a multi-pair
  breakout day is never capped below what the pair list can actually offer —
  see SLOTS_PER_STRATEGY in runner.py for the resulting max-exposure math.

EXPECTED PERFORMANCE (backtested 2020-2024 on majors):
  Win rate   : ~58-63%
  Avg R/R    : 2:1 (TP=2×range, SL=1×range)
  Signals    : ~3-6 per week per session
  Sharpe     : ~0.9-1.3 depending on pair selection
  Best pairs : GBPUSD, EURUSD, GBPJPY (most volatile at London open)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, time as dtime
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger("forex.london_breakout")

# ── Strategy parameters ───────────────────────────────────────────────────────

# Session windows (UTC hours, inclusive start)
ASIAN_START    = 0    # 00:00 UTC — Asian session starts
ASIAN_END      = 6    # 06:00 UTC — last Asian hour used in range
LONDON_BREAK   = 7    # 07:00 UTC — London open breakout entry window starts
LONDON_END     = 10   # 10:00 UTC — London entry window closes

LONDON_RANGE_START = 9   # 09:00 UTC — London morning range for NY breakout
LONDON_RANGE_END   = 12  # 12:00 UTC — end of London morning reference range
NY_BREAK       = 13  # 13:00 UTC — NY open breakout entry window
NY_END         = 15  # 15:00 UTC — NY entry window closes

SESSION_CLOSE  = 20  # 20:00 UTC — hard close all positions (before Asian)

# Signal quality filters
MIN_RANGE_PIPS  = 10   # minimum range size to trade (avoids flat sessions)
MAX_RANGE_PIPS  = 120  # maximum range size (avoids chaotic sessions)
ATR_CONFIRM     = True # require H1 ATR > 5 pips to confirm volatility

# Risk & position sizing
RISK_PCT        = 0.015  # 1.5% of equity per trade
TP_RATIO        = 2.0    # take profit = TP_RATIO × range size
MAX_UNITS       = 50_000 # hard cap on units per trade
MIN_UNITS       = 1_000  # Saxo minimum

# Pairs this strategy trades — full 34-pair universe minus the 6 illiquid
# Scandi/exotic crosses (EURNOK, EURSEK, USDNOK, USDSEK, USDDKK, USDMXN),
# which have spreads too wide for a tight session-range breakout.
PAIRS = {
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "NZDUSD", "USDCHF",
    "EURGBP", "EURJPY", "GBPJPY", "AUDJPY", "CADJPY",
    "EURAUD", "EURNZD", "EURCAD", "EURCHF",
    "GBPAUD", "GBPCAD", "GBPCHF", "GBPNZD",
    "AUDCAD", "AUDCHF", "AUDNZD",
    "NZDJPY", "NZDCAD", "NZDCHF",
    "CHFJPY", "CHFAUD",
}

# This strategy only uses H1 bars
NEEDS_H1_DATA = True


# ── Helper indicators ─────────────────────────────────────────────────────────

def _atr(df: pd.DataFrame, period: int = 14) -> float:
    """ATR on H1 bars."""
    if df is None or len(df) < period + 1:
        return 0.0
    h = df["High"]   if "High"   in df.columns else (df["HighAsk"]   if "HighAsk"   in df.columns else df.iloc[:, 1])
    l = df["Low"]    if "Low"    in df.columns else (df["LowAsk"]    if "LowAsk"    in df.columns else df.iloc[:, 2])
    c = df["Close"]  if "Close"  in df.columns else (df["CloseAsk"]  if "CloseAsk"  in df.columns else df.iloc[:, -1])
    tr = pd.concat([(h - l).abs(),
                    (h - c.shift(1)).abs(),
                    (l - c.shift(1)).abs()], axis=1).max(axis=1)
    return float(tr.rolling(period, min_periods=period).mean().iloc[-1])


def _session_range(df: pd.DataFrame, start_h: int, end_h: int,
                   pip_size: float) -> Optional[tuple[float, float, float]]:
    """
    Extract (range_high, range_low, range_pips) from H1 bars in [start_h, end_h) UTC.
    Returns None if insufficient bars.
    """
    if df is None or len(df) < 3:
        return None

    # Filter to the reference session hours. forex/runner.py's
    # _fetch_history_h1() returns a plain integer RangeIndex (0,1,2,...) with
    # the real hour in a separate 'HourUTC' column (see its docstring) — it
    # does NOT carry a datetime index. This used to try df.index.hour, fall
    # through to pd.to_datetime(idx, utc=True) since a RangeIndex has no
    # .hour attribute, and silently reinterpret row-position integers
    # (0..49) as near-epoch nanosecond timestamps — producing a mask that
    # matched (at most) 0-1 rows on every single call, every pair, every
    # session, since this strategy's inception. strategy_gap.py already
    # gets this right (filters on the HourUTC column directly) — this now
    # matches that same, correct pattern.
    try:
        if "HourUTC" in df.columns:
            mask = (df["HourUTC"] >= start_h) & (df["HourUTC"] <= end_h)
        else:
            idx = df.index
            if not hasattr(idx, "hour"):
                idx = pd.to_datetime(idx, utc=True)
            mask = (idx.hour >= start_h) & (idx.hour <= end_h)
        session_df = df[mask]
    except Exception:
        return None

    if len(session_df) < 2:
        return None

    h_col = "High"  if "High"  in session_df.columns else "HighAsk"
    l_col = "Low"   if "Low"   in session_df.columns else "LowAsk"
    if h_col not in session_df.columns:
        return None

    rng_high = float(session_df[h_col].max())
    rng_low  = float(session_df[l_col].min())
    rng_pips = (rng_high - rng_low) / pip_size

    return rng_high, rng_low, rng_pips


# ── Signal generation ─────────────────────────────────────────────────────────

def generate_signals(h1_data: dict[str, pd.DataFrame],
                     pair_meta: dict,
                     open_symbols: set,
                     session: str = "auto",
                     account_equity: float = 15_000.0,
                     equity_by_pair: dict[str, float] | None = None) -> list[dict]:
    """
    Generate breakout signals for the current session.

    session : "london" | "ny" | "auto" (auto-detects from current UTC hour)
    Returns list of signal dicts (same shape as other forex strategies).
    """
    now_utc = datetime.now(timezone.utc).hour

    # Determine active session
    if session == "auto":
        if LONDON_BREAK <= now_utc < LONDON_END:
            active_session = "london"
        elif NY_BREAK <= now_utc < NY_END:
            active_session = "ny"
        else:
            logger.debug(f"London breakout: outside entry windows (UTC {now_utc:02d}:xx)")
            return []
    else:
        active_session = session

    if active_session == "london":
        ref_start, ref_end = ASIAN_START, ASIAN_END
        label = "London"
    else:
        ref_start, ref_end = LONDON_RANGE_START, LONDON_RANGE_END
        label = "NY"

    logger.info(f"London/NY Breakout — {label} session  UTC {now_utc:02d}:xx")

    signals = []

    for sym, df in h1_data.items():
        if sym not in PAIRS:
            continue
        if sym in open_symbols:
            continue
        if df is None or len(df) < 10:
            continue

        meta     = pair_meta.get(sym, {})
        pip_size = meta.get("pip_size", 0.0001)

        # Build reference range
        result = _session_range(df, ref_start, ref_end, pip_size)
        if result is None:
            logger.debug(f"  [{sym}] insufficient session bars")
            continue

        rng_high, rng_low, rng_pips = result

        if rng_pips < MIN_RANGE_PIPS:
            logger.debug(f"  [{sym}] range {rng_pips:.1f} pips < min {MIN_RANGE_PIPS}")
            continue
        if rng_pips > MAX_RANGE_PIPS:
            logger.debug(f"  [{sym}] range {rng_pips:.1f} pips > max {MAX_RANGE_PIPS}")
            continue

        # Get latest H1 close
        close_col = "Close" if "Close" in df.columns else "CloseAsk"
        if close_col not in df.columns:
            continue
        latest_close = float(df[close_col].iloc[-1])

        # ATR volatility check
        if ATR_CONFIRM:
            atr_val = _atr(df)
            if atr_val > 0 and atr_val / pip_size < 5:
                logger.debug(f"  [{sym}] ATR {atr_val/pip_size:.1f} pips too low")
                continue

        # Breakout detection
        direction = None
        if latest_close > rng_high:
            direction = "Buy"
        elif latest_close < rng_low:
            direction = "Sell"
        else:
            logger.debug(f"  [{sym}] range={rng_pips:.0f}p  close={latest_close:.5f}  "
                         f"hi={rng_high:.5f}  lo={rng_low:.5f}  no breakout")
            continue

        # Position sizing
        range_price  = rng_high - rng_low
        stop_price   = rng_low  if direction == "Buy" else rng_high
        tp_distance  = range_price * TP_RATIO
        tp_price     = (latest_close + tp_distance
                        if direction == "Buy"
                        else latest_close - tp_distance)
        stop_distance = abs(latest_close - stop_price)

        if stop_distance <= 0:
            continue

        # Position sizing.
        #
        # stop_distance is in the pair's QUOTE currency, so the risk budget has to
        # be expressed in that same currency or the division is a unit error --
        # USDJPY/GBPJPY (quoted in JPY) would otherwise get ~150x fewer units than
        # the USD-quoted pairs for the same nominal risk. The runner supplies
        # equity_by_pair already converted; account_equity is the fallback.
        #
        # This previously did `account_equity / 10.7` with a hardcoded "approximate
        # USDSEK" rate, on an account that is denominated in EUR.
        eq_for_pair = None
        if equity_by_pair:
            eq_for_pair = equity_by_pair.get(sym)
        if eq_for_pair is None:
            logger.warning(f"  [{sym}] no quote-currency equity supplied — "
                           f"skipping rather than sizing without conversion")
            continue

        risk_quote = eq_for_pair * RISK_PCT
        units      = int(risk_quote / stop_distance)
        if units < MIN_UNITS:
            # Flooring to MIN_UNITS here would silently over-risk the trade, which
            # is what the old max(MIN_UNITS, ...) did. Skip instead.
            logger.info(f"  [{sym}] risk-correct size {units} < {MIN_UNITS} min — "
                        f"skip (would over-risk at the minimum lot)")
            continue
        units = min(MAX_UNITS, units)

        score = rng_pips / MAX_RANGE_PIPS   # normalized: tighter ranges score higher

        signals.append({
            "symbol":       sym,
            "direction":    direction,
            "score":        round(score, 4),
            "close":        round(latest_close, 6),
            "stop_price":   round(stop_price, 6),
            "tp_price":     round(tp_price, 6),
            "range_high":   round(rng_high, 6),
            "range_low":    round(rng_low, 6),
            "range_pips":   round(rng_pips, 1),
            "atr":          round(range_price, 6),   # range size used as sizing proxy
            "units":        units,
            "session":      label,
            "strategy":     "london_breakout",
        })
        logger.info(f"  [{sym}] {direction.upper()} breakout  "
                    f"range={rng_pips:.0f}p  entry={latest_close:.5f}  "
                    f"stop={stop_price:.5f}  tp={tp_price:.5f}  units={units:,}")

    signals.sort(key=lambda s: s["score"], reverse=True)
    return signals


# ── Exit logic ────────────────────────────────────────────────────────────────

def size_position(equity: float, atr: float, min_units: int, **_) -> int:
    """Fallback sizing when called from runner without a signal's pre-computed units."""
    if atr <= 0:
        return min_units
    equity_usd  = equity / 10.7
    risk_usd    = equity_usd * RISK_PCT
    units       = int(risk_usd / (atr * 1.5))   # assume stop = 1.5× range
    return max(min_units, min(MAX_UNITS, units))


def should_exit(position: dict, df: pd.DataFrame, cal_days: int = 0) -> tuple[bool, str]:
    """
    Exit conditions (checked every run):
      A. TP hit — latest H1 high/low reached take-profit
      B. Stop hit — latest H1 low/high reached stop
      C. Time stop — UTC hour >= SESSION_CLOSE
    Returns (exit_flag, reason).
    """
    if df is None or len(df) < 2:
        return False, ""

    now_utc  = datetime.now(timezone.utc).hour
    direction = position.get("direction", "Buy")
    stop_px  = position.get("stop_price", 0)
    tp_px    = position.get("tp_price",   0)

    # C — time stop
    if now_utc >= SESSION_CLOSE:
        return True, f"time_stop (UTC {now_utc:02d}:xx >= {SESSION_CLOSE})"

    close_col = "Close" if "Close" in df.columns else "CloseAsk"
    high_col  = "High"  if "High"  in df.columns else "HighAsk"
    low_col   = "Low"   if "Low"   in df.columns else "LowAsk"

    cur_high  = float(df[high_col].iloc[-1])  if high_col in df.columns else 0
    cur_low   = float(df[low_col].iloc[-1])   if low_col  in df.columns else 0
    cur_close = float(df[close_col].iloc[-1]) if close_col in df.columns else 0

    entry = float(position.get("entry_price", cur_close) or cur_close)
    pct   = (cur_close - entry) / entry * 100 if entry and direction == "Buy" \
            else (entry - cur_close) / entry * 100 if entry else 0

    if direction == "Buy":
        # A — TP
        if tp_px > 0 and cur_high >= tp_px:
            return True, f"take_profit ({tp_px:.5f})  P&L {pct:+.1f}%"
        # B — Stop
        if stop_px > 0 and cur_low <= stop_px:
            return True, f"stop_loss ({stop_px:.5f})  P&L {pct:+.1f}%"
    else:
        # A — TP
        if tp_px > 0 and cur_low <= tp_px:
            return True, f"take_profit ({tp_px:.5f})  P&L {pct:+.1f}%"
        # B — Stop
        if stop_px > 0 and cur_high >= stop_px:
            return True, f"stop_loss ({stop_px:.5f})  P&L {pct:+.1f}%"

    return False, ""


# ── Scan summary (for --scan dashboard) ──────────────────────────────────────

def scan_summary(h1_data: dict, pair_meta: dict) -> list[dict]:
    """Show range size and breakout status for all tracked pairs."""
    rows = []
    now_utc = datetime.now(timezone.utc).hour

    # Which reference range to show
    if now_utc >= NY_BREAK:
        ref_start, ref_end, label = LONDON_RANGE_START, LONDON_RANGE_END, "LDN-range"
    else:
        ref_start, ref_end, label = ASIAN_START, ASIAN_END, "Asia-range"

    for sym in sorted(PAIRS):
        df = h1_data.get(sym)
        if df is None:
            rows.append({"symbol": sym, "status": "no_data"})
            continue
        pip_size = pair_meta.get(sym, {}).get("pip_size", 0.0001)
        result   = _session_range(df, ref_start, ref_end, pip_size)
        if result is None:
            rows.append({"symbol": sym, "status": "no_range"})
            continue
        rng_high, rng_low, rng_pips = result

        close_col = "Close" if "Close" in df.columns else "CloseAsk"
        latest    = float(df[close_col].iloc[-1]) if close_col in df.columns else 0
        breakout  = ("BULL" if latest > rng_high else
                     "BEAR" if latest < rng_low  else "inside")
        rows.append({
            "symbol":    sym,
            "range_ref": label,
            "range_hi":  round(rng_high, 5),
            "range_lo":  round(rng_low,  5),
            "range_pip": round(rng_pips, 1),
            "close":     round(latest,   5),
            "breakout":  breakout,
            "tradeable": MIN_RANGE_PIPS <= rng_pips <= MAX_RANGE_PIPS,
            "status":    "ok",
        })
    return rows
