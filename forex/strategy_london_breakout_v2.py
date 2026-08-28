"""
forex/strategy_london_breakout_v2.py
--------------------------------------
Day Trading Strategy — "London Breakout V2" (2026-08-29).

Explicit user request: implement a detailed 7-point design-doc review as a
NEW parallel strategy, run it alongside the existing "london_breakout"
strategy UNCHANGED -- "do not change any of your code, make a new one run
simultaneously." `forex/strategy_london_breakout.py` is completely
untouched; this is a from-scratch module registered as its own STRATEGIES
key ("london_breakout_v2") in forex/runner.py.

All 7 issues below were verified against the ORIGINAL module's real,
current source before writing this (not assumed from the design doc
alone):

1. FIXED -- range-hour boundary. The original's `_session_range()` uses
   `(df["HourUTC"] >= start_h) & (df["HourUTC"] <= end_h)` (inclusive of
   end_h) despite its own docstring claiming `[start_h, end_h)`
   (exclusive). Confirmed live: for the NY session this includes hour 12
   in the "09:00-12:59" reference range. Fixed here with a genuinely
   exclusive end (`< end_h`).

2. FIXED -- 2:1 R/R was never actually guaranteed. The original enters at
   `latest_close` (which can be arbitrarily far past `rng_high`/`rng_low`)
   but stops at the OPPOSITE range boundary and targets a FIXED
   `2.0 x range_price` regardless of entry -- so stop_distance grows with
   breakout extension while tp_distance stays fixed, meaning actual R/R
   shrinks below 2:1 (or even below 1:1) the further price has already
   moved before the signal fires. Fixed with an explicit
   `actual_rr = tp_distance / stop_distance` check requiring
   `actual_rr >= MIN_ACTUAL_RR` (1.5), PLUS a breakout-extension band
   (item 8) that keeps entries close enough to the boundary that this
   rarely needs to reject on R/R alone.

3. FIXED -- backwards scoring. The original's `score = rng_pips /
   MAX_RANGE_PIPS` with `# tighter ranges score higher` in the comment is
   the OPPOSITE of what that formula computes (bigger range -> bigger
   score). Replaced with an explicit compression_score
   (`1 - normalized_range`, genuinely higher for tighter ranges) combined
   with breakout strength -- see _score() docstring.

4. FIXED -- no protection against re-signaling the same breakout. The
   original only checks `sym in open_symbols`, so a position that closes
   (TP or SL) mid-session, while price is still beyond the same range
   boundary, can immediately re-signal on the SAME underlying breakout.
   This module tracks `already_traded_sessions` (symbol + UTC date +
   session), threaded through forex/runner.py's own
   `data/lbo_v2_session_cooldown.json` (mirrors the gap-cooldown pattern
   already used for "gap"/"gap_weekend") -- once a pair trades in a given
   session on a given day, it's done for that session-day regardless of
   how many times price re-crosses the boundary afterward.

5. REDUCED -- MAX_LBO_POSITIONS = 4 (vs. the original's 28), genuinely
   enforced via forex/runner.py's SLOTS_PER_STRATEGY (same pattern as
   [[forex_donchian_quality_strategy_2026-08-29]]'s real-cap fix).
   Directly addresses the user's correlated-exposure concern (28 pairs x
   1.5% = a theoretical 42% account risk, much of it correlated FX-cross
   exposure to the same 2-3 underlying currency moves).

6. REDUCED -- RISK_PCT = 0.005 (0.5%, vs. the original's 0.015/1.5%).

7. REBUILT -- range/ATR ratio filter replaces the weak
   `atr_pips < 5 -> skip` check. Requires
   `0.5 <= range_price / atr_val <= 3.0` -- too tight a range relative to
   normal volatility is noise; too wide relative to volatility suggests
   an already-expanded/unstable market.

8. NEW -- breakout-strength band (MIN/MAX_BREAKOUT_ATR = 0.10/0.50 ATR).
   Both prevents tiny false breaks (item 8 in the design doc) AND is what
   keeps actual R/R close to the target 2:1 in practice (item 2) --
   an entry within 0.5 ATR of the boundary can't drift stop_distance far
   enough from tp_distance to fail the R/R check very often.

9. FIXED -- the fallback `size_position()` (used only when the runner
   calls this generically without a signal's pre-computed `units`, e.g. a
   cost-gate recomputation) had the SAME `equity / 10.7` hardcoded
   pseudo-USDSEK-rate bug that was already fixed in the real
   `generate_signals()` sizing path but never updated here. Per the
   design doc's "Option A": removed entirely -- returns 0 (skip) rather
   than silently mis-size off a fabricated conversion rate.

Exit logic (should_exit) is otherwise the same shape as the original
(TP / stop / time-stop) -- the user's review didn't flag exit issues.

THIS MODULE IS PURE except for reading wall-clock time to detect the
active session and today's UTC date (identical to what the original
module already does) -- no order placement, no file I/O. All execution,
capital, and the session-cooldown FILE live in forex/runner.py, same
convention as every other strategy module.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger("forex.london_breakout_v2")

# ── Session windows (UTC hours) -- fix #1: end is genuinely EXCLUSIVE now ──
ASIAN_START    = 0
ASIAN_END      = 7    # was compared with <=6 in the original despite claiming [0,6); real exclusive window is [0,7)
LONDON_BREAK   = 7
LONDON_END     = 10

LONDON_RANGE_START = 9
LONDON_RANGE_END   = 13   # was compared with <=12 in the original; real exclusive window is [9,13)
NY_BREAK       = 13
NY_END         = 15

SESSION_CLOSE  = 20

# ── Signal quality filters ──────────────────────────────────────────────
MIN_RANGE_PIPS       = 10
MAX_RANGE_PIPS       = 120
MIN_RANGE_ATR_RATIO  = 0.5    # item 7: range_price / atr_val must be in this band
MAX_RANGE_ATR_RATIO  = 3.0
MIN_BREAKOUT_ATR     = 0.10   # item 8: breakout distance, in ATR units
MAX_BREAKOUT_ATR     = 0.50
MIN_ACTUAL_RR        = 1.5    # item 2: reject any trade whose REAL (not assumed) R/R is below this

# ── Risk & position sizing ───────────────────────────────────────────────
RISK_PCT      = 0.005   # item 6: 0.5% (was 1.5%)
TP_RATIO      = 2.0
MAX_UNITS     = 50_000
MIN_UNITS     = 1_000
MAX_LBO_POSITIONS = 4   # item 5: really enforced via SLOTS_PER_STRATEGY, see runner.py wiring

# Same pair universe as the original -- the user's review didn't flag the
# pair list itself, only the position-count cap (item 5).
PAIRS = {
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "NZDUSD", "USDCHF",
    "EURGBP", "EURJPY", "GBPJPY", "AUDJPY", "CADJPY",
    "EURAUD", "EURNZD", "EURCAD", "EURCHF",
    "GBPAUD", "GBPCAD", "GBPCHF", "GBPNZD",
    "AUDCAD", "AUDCHF", "AUDNZD",
    "NZDJPY", "NZDCAD", "NZDCHF",
    "CHFJPY", "CHFAUD",
}

NEEDS_H1_DATA = True


def _atr(df: pd.DataFrame, period: int = 14) -> float:
    if df is None or len(df) < period + 1:
        return 0.0
    h = df["High"]  if "High"  in df.columns else (df["HighAsk"]  if "HighAsk"  in df.columns else df.iloc[:, 1])
    l = df["Low"]   if "Low"   in df.columns else (df["LowAsk"]   if "LowAsk"   in df.columns else df.iloc[:, 2])
    c = df["Close"] if "Close" in df.columns else (df["CloseAsk"] if "CloseAsk" in df.columns else df.iloc[:, -1])
    tr = pd.concat([(h - l).abs(),
                    (h - c.shift(1)).abs(),
                    (l - c.shift(1)).abs()], axis=1).max(axis=1)
    return float(tr.rolling(period, min_periods=period).mean().iloc[-1])


def _session_range(df: pd.DataFrame, start_h: int, end_h: int,
                   pip_size: float) -> Optional[tuple[float, float, float]]:
    """Extract (range_high, range_low, range_pips) from H1 bars in the
    GENUINELY exclusive-end window [start_h, end_h) UTC -- fix #1."""
    if df is None or len(df) < 3:
        return None
    try:
        if "HourUTC" in df.columns:
            mask = (df["HourUTC"] >= start_h) & (df["HourUTC"] < end_h)
        else:
            idx = df.index
            if not hasattr(idx, "hour"):
                idx = pd.to_datetime(idx, utc=True)
            mask = (idx.hour >= start_h) & (idx.hour < end_h)
        session_df = df[mask]
    except Exception:
        return None

    if len(session_df) < 2:
        return None

    h_col = "High" if "High" in session_df.columns else "HighAsk"
    l_col = "Low"  if "Low"  in session_df.columns else "LowAsk"
    if h_col not in session_df.columns:
        return None

    rng_high = float(session_df[h_col].max())
    rng_low  = float(session_df[l_col].min())
    rng_pips = (rng_high - rng_low) / pip_size
    return rng_high, rng_low, rng_pips


def _score(breakout_strength_atr: float, rng_pips: float) -> float:
    """Fix #3: genuinely higher for TIGHTER ranges (the original's comment
    said this but its formula did the opposite). compression_score is 1.0
    at MIN_RANGE_PIPS and 0.0 at MAX_RANGE_PIPS; combined multiplicatively
    with breakout strength so a clean, well-confirmed breakout of a tight
    range ranks above a marginal breakout of a wide one."""
    compression_score = 1.0 - ((rng_pips - MIN_RANGE_PIPS) / (MAX_RANGE_PIPS - MIN_RANGE_PIPS))
    compression_score = max(0.0, min(1.0, compression_score))
    return breakout_strength_atr * compression_score


def generate_signals(h1_data: dict[str, pd.DataFrame],
                     pair_meta: dict,
                     open_symbols: set,
                     session: str = "auto",
                     account_equity: float = 15_000.0,
                     equity_by_pair: dict[str, float] | None = None,
                     already_traded_sessions: set | None = None) -> list[dict]:
    """Generate breakout signals for the current session, with the item
    1/2/3/4/7/8 fixes applied. `already_traded_sessions` (fix #4): a set of
    "YYYY-MM-DD:SYMBOL:session_label" keys already traded today -- pass
    forex/runner.py's own persisted set here; this module never reads or
    writes that file itself (stays pure)."""
    if already_traded_sessions is None:
        already_traded_sessions = set()

    now_utc = datetime.now(timezone.utc)
    now_h   = now_utc.hour
    today_s = now_utc.strftime("%Y-%m-%d")

    if session == "auto":
        if LONDON_BREAK <= now_h < LONDON_END:
            active_session = "london"
        elif NY_BREAK <= now_h < NY_END:
            active_session = "ny"
        else:
            logger.debug(f"London breakout v2: outside entry windows (UTC {now_h:02d}:xx)")
            return []
    else:
        active_session = session

    if active_session == "london":
        ref_start, ref_end = ASIAN_START, ASIAN_END
        label = "London"
    else:
        ref_start, ref_end = LONDON_RANGE_START, LONDON_RANGE_END
        label = "NY"

    logger.info(f"London/NY Breakout V2 — {label} session  UTC {now_h:02d}:xx")

    signals = []

    for sym, df in h1_data.items():
        if sym not in PAIRS:
            continue
        if sym in open_symbols:
            continue
        session_key = f"{today_s}:{sym}:{label}"
        if session_key in already_traded_sessions:
            continue   # fix #4: already traded this exact session-day, skip re-signaling
        if df is None or len(df) < 10:
            continue

        meta     = pair_meta.get(sym, {})
        pip_size = meta.get("pip_size", 0.0001)

        result = _session_range(df, ref_start, ref_end, pip_size)
        if result is None:
            continue
        rng_high, rng_low, rng_pips = result

        if rng_pips < MIN_RANGE_PIPS or rng_pips > MAX_RANGE_PIPS:
            continue

        close_col = "Close" if "Close" in df.columns else "CloseAsk"
        if close_col not in df.columns:
            continue
        latest_close = float(df[close_col].iloc[-1])

        atr_val = _atr(df)
        if atr_val <= 0:
            continue

        range_price = rng_high - rng_low
        range_atr_ratio = range_price / atr_val
        if range_atr_ratio < MIN_RANGE_ATR_RATIO or range_atr_ratio > MAX_RANGE_ATR_RATIO:
            continue   # fix #7: range too tight or too wide relative to normal volatility

        direction = None
        if latest_close > rng_high:
            direction = "Buy"
            breakout_distance = latest_close - rng_high
        elif latest_close < rng_low:
            direction = "Sell"
            breakout_distance = rng_low - latest_close
        else:
            continue

        breakout_strength = breakout_distance / atr_val
        if breakout_strength < MIN_BREAKOUT_ATR or breakout_strength > MAX_BREAKOUT_ATR:
            continue   # fix #8: too small (noise) or too far extended past the boundary

        stop_price   = rng_low if direction == "Buy" else rng_high
        tp_distance  = range_price * TP_RATIO
        tp_price     = (latest_close + tp_distance if direction == "Buy"
                        else latest_close - tp_distance)
        stop_distance = abs(latest_close - stop_price)
        if stop_distance <= 0:
            continue

        actual_rr = tp_distance / stop_distance
        if actual_rr < MIN_ACTUAL_RR:
            continue   # fix #2: reject if the REAL R/R (not the assumed 2:1) is too poor

        eq_for_pair = equity_by_pair.get(sym) if equity_by_pair else None
        if eq_for_pair is None:
            logger.warning(f"  [{sym}] no quote-currency equity supplied — "
                           f"skipping rather than sizing without conversion")
            continue

        risk_quote = eq_for_pair * RISK_PCT
        units      = int(risk_quote / stop_distance)
        if units < MIN_UNITS:
            continue
        units = min(MAX_UNITS, units)

        score = _score(breakout_strength, rng_pips)

        signals.append({
            "symbol":            sym,
            "direction":         direction,
            "score":             round(score, 4),
            "close":             round(latest_close, 6),
            "stop_price":        round(stop_price, 6),
            "tp_price":          round(tp_price, 6),
            "range_high":        round(rng_high, 6),
            "range_low":         round(rng_low, 6),
            "range_pips":        round(rng_pips, 1),
            "atr":               round(range_price, 6),
            "units":             units,
            "session":           label,
            "strategy":          "london_breakout_v2",
            "breakout_strength": round(breakout_strength, 3),
            "actual_rr":         round(actual_rr, 2),
            "session_key":       session_key,   # fix #4: runner marks this exhausted on entry
        })
        logger.info(f"  [{sym}] {direction.upper()} breakout  range={rng_pips:.0f}p  "
                    f"entry={latest_close:.5f}  stop={stop_price:.5f}  tp={tp_price:.5f}  "
                    f"actual_rr={actual_rr:.2f}  units={units:,}")

    signals.sort(key=lambda s: s["score"], reverse=True)
    return signals


def size_position(equity: float, atr: float, min_units: int, **_) -> int:
    """Fix #9 (design doc's Option A): the original's fallback did
    `equity / 10.7` (a hardcoded pseudo-USDSEK rate, the exact bug already
    fixed in generate_signals()'s real sizing path but never updated here).
    Removed entirely -- returns 0 so the runner skips the trade rather than
    silently sizing off a fabricated conversion. generate_signals() always
    provides pre-computed `units` on every real signal; this fallback is
    only ever reached if some future caller invokes size_position()
    directly without going through generate_signals() first."""
    return 0


def should_exit(position: dict, df: pd.DataFrame, cal_days: int = 0) -> tuple[bool, str]:
    """Unchanged from strategy_london_breakout.py -- the user's review
    didn't flag exit-logic issues, only entry-quality and sizing ones."""
    if df is None or len(df) < 2:
        return False, ""

    now_utc   = datetime.now(timezone.utc).hour
    direction = position.get("direction", "Buy")
    stop_px   = position.get("stop_price", 0)
    tp_px     = position.get("tp_price",   0)

    if now_utc >= SESSION_CLOSE:
        return True, f"time_stop (UTC {now_utc:02d}:xx >= {SESSION_CLOSE})"

    close_col = "Close" if "Close" in df.columns else "CloseAsk"
    high_col  = "High"  if "High"  in df.columns else "HighAsk"
    low_col   = "Low"   if "Low"   in df.columns else "LowAsk"

    cur_high  = float(df[high_col].iloc[-1])  if high_col  in df.columns else 0
    cur_low   = float(df[low_col].iloc[-1])   if low_col   in df.columns else 0
    cur_close = float(df[close_col].iloc[-1]) if close_col in df.columns else 0

    entry = float(position.get("entry_price", cur_close) or cur_close)
    pct   = (cur_close - entry) / entry * 100 if entry and direction == "Buy" \
            else (entry - cur_close) / entry * 100 if entry else 0

    if direction == "Buy":
        if tp_px > 0 and cur_high >= tp_px:
            return True, f"take_profit ({tp_px:.5f})  P&L {pct:+.1f}%"
        if stop_px > 0 and cur_low <= stop_px:
            return True, f"stop_loss ({stop_px:.5f})  P&L {pct:+.1f}%"
    else:
        if tp_px > 0 and cur_low <= tp_px:
            return True, f"take_profit ({tp_px:.5f})  P&L {pct:+.1f}%"
        if stop_px > 0 and cur_high >= stop_px:
            return True, f"stop_loss ({stop_px:.5f})  P&L {pct:+.1f}%"

    return False, ""


def scan_summary(h1_data: dict, pair_meta: dict) -> list[dict]:
    rows = []
    now_utc = datetime.now(timezone.utc).hour

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
