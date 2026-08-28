"""
forex/strategy_gap_weekend.py
------------------------------
FX Strategy — "GAPFILL Weekend" (2026-08-29).

Explicit user request: fix confirmed bugs in the existing Gap Fill
strategy and test a reworked session-gap approach WITHOUT touching
forex/strategy_gap.py ("gap") at all -- this is a from-scratch parallel
module, registered as its own strategy key ("gap_weekend") in
forex/runner.py's STRATEGIES dict, running alongside the original "gap"
strategy in SIM only (never added to LIVE_ALLOWED_STRATEGIES or
LIVE_EUR_ALLOWED_STRATEGIES). Own position slots, own gap-cooldown file
(data/gap_weekend_cooldown.json, kept separate from gap's
data/gap_cooldown.json so a symbol traded by one doesn't block the other),
own pnl_ledger.db rows (strategy="gap_weekend") -- fully independent A/B
comparison against the original.

WHAT CHANGED vs strategy_gap.py, and why (per the user's own design doc):

1. FIXED — position sizing used the WRONG stop multiplier. The original
   module's size_position() always sized off the module-level
   ATR_STOP_MULT constant (1.5, weekly's own value) even for session gaps
   whose real stop is 2.0x gap_size -- every session-gap position was
   undersized relative to the risk it was actually taking. size_position()
   here takes stop_mult as an explicit required parameter; callers must
   pass the SAME multiplier used to compute that signal's own stop_price
   (1.5 for weekly, 2.0 for session gaps).

2. FIXED — session gap's reference-bar lookup (_find_ref_bar_close)
   silently fell back to "last available close" when the correct H1
   reference bar was missing, generating a signal priced off a bar that
   was never actually the session's true reference point. Now skips the
   candidate (`continue`) instead of guessing.

3. NEW — every signal carries its own gap_type ("weekly"/"london"/
   "newyork"/"tokyo"); forex/runner.py already threads gap_type through to
   pos_record and (as of this change) into pnl_tracker.log_open(), so
   per-gap-type win rate/profit factor/expectancy can be queried
   independently via report_gap_weekend_by_type.py -- never a combined
   "gap fill" statistic.

4. PHASED ROLLOUT — session variants (london/newyork/tokyo) are DISABLED
   by default (see ENABLED_SESSIONS below). Only weekly trades until the
   user reviews separate weekly-only results. The rebuilt session logic
   (ATR displacement filter, reversal confirmation, quality-score ranking
   -- items 5-7 below) is fully implemented and ready for Phase 3, just
   not scanned yet.

5. REBUILT (dormant until enabled) — session gap detection no longer uses
   a tiny fixed %-of-price threshold (0.04-0.05%, indistinguishable from
   spread noise). Displacement is now measured in ATR units:
   move_atr = |session_open - ref_close| / ATR(H1). A candidate needs
   0.8 <= move_atr <= 2.0 -- below that is noise, above that is an
   abnormal/news move neither fade should touch.

6. REBUILT (dormant until enabled) — no longer fades the instant a gap is
   detected. Requires the most recent completed H1 bar to already show a
   reversal candle in the fade direction (bearish close after a gap up
   before selling; bullish close after a gap down before buying) — a
   candidate with no confirming candle is skipped, not queued.

7. REBUILT (dormant until enabled) — ranking no longer assumes the
   largest raw gap is the best trade. quality_score is an explicit,
   documented-as-first-pass composite of ATR displacement, reversal
   candle strength, and distance from the recent 20-bar extreme -- see
   _session_quality_score()'s docstring. This is NOT a validated ranking
   model; it exists so signals aren't sorted by gap_pct alone, and is
   meant to be reviewed/adjusted once there's real session-gap data to
   judge it against.

8. UNCHANGED — weekly gap fill logic itself (entry condition, exit
   conditions, target = Friday close) is intentionally left as close to
   strategy_gap.py's original as possible, per the user's explicit "keep
   weekly strategy simpler... don't change too many weekly parameters
   until you get separate statistics" instruction.

THIS MODULE IS PURE — no I/O, no orders, no state. All execution lives in
forex/runner.py, same interface contract as every other strategy module.
"""

import pandas as pd
from datetime import datetime, timezone

# ── Weekly gap parameters (deliberately identical to strategy_gap.py) ──
MIN_GAP_PCT    = 0.10
MAX_GAP_PCT    = 2.00
ATR_STOP_MULT  = 1.5     # weekly's own stop multiplier -- pass to size_position() for weekly signals
RISK_PCT       = 0.0025
TIME_STOP_DAYS = 7
LOT_ROUND      = 1_000
MIN_BARS       = 5

NEEDS_LIVE_PRICES = True

# ── Session gap definitions -- ATR-based (item 5), present but only
# scanned for sessions listed in ENABLED_SESSIONS below (item 4). ──
SESSION_GAPS = {
    "london": {
        "open_hour_utc":   7,
        "ref_hour_utc":    6,
        "stop_mult":       2.0,
        "time_stop_hours": 8,
        "risk_pct":        0.0025,
        "min_atr_mult":    0.8,
        "max_atr_mult":    2.0,
    },
    "newyork": {
        "open_hour_utc":   12,
        "ref_hour_utc":    11,
        "stop_mult":       2.0,
        "time_stop_hours": 6,
        "risk_pct":        0.0025,
        "min_atr_mult":    0.8,
        "max_atr_mult":    2.0,
    },
    "tokyo": {
        "open_hour_utc":   0,
        "ref_hour_utc":    23,
        "stop_mult":       2.0,
        "time_stop_hours": 7,
        "risk_pct":        0.0025,
        "min_atr_mult":    0.8,
        "max_atr_mult":    2.0,
    },
}

# Phase 2 (user's explicit rollout plan): weekly alone. Add session names
# back here only once Phase 1/2 (bug fixes + weekly-only results) has been
# reviewed -- e.g. ENABLED_SESSIONS = {"london", "newyork", "tokyo"}.
ENABLED_SESSIONS: set[str] = set()


def _atr(df: pd.DataFrame, period: int = 14) -> float:
    """ATR over whatever bar timeframe df is in (H1 for session gaps).
    Used only by the (currently dormant) session displacement filter."""
    if df is None or len(df) < period + 1:
        return 0.0
    h, l, c = df["High"], df["Low"], df["Close"]
    prev = c.shift(1)
    tr = pd.concat([h - l, (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    val = tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean().iloc[-1]
    return float(val) if pd.notna(val) else 0.0


def _session_quality_score(move_atr: float, reversal_strength: float,
                            distance_from_extreme_atr: float) -> float:
    """First-pass composite ranking score for session-gap candidates.

    NOT a validated model -- exists only so signals stop being ranked by
    raw gap_pct (the user's explicit instruction: "if you don't have a
    better scoring system, do not assume the largest gap is the best
    trade"). Weighted average of three named, independently-computed
    components:
      - move_atr (0.8-2.0 range by construction): bigger ATR-normalized
        displacement weighted more within that allowed band.
      - reversal_strength (0-1): how decisive the confirming candle was
        (body size / full bar range) -- a weak-bodied "reversal" is a
        less convincing signal than a strong-bodied one.
      - distance_from_extreme_atr: how far session_open sits from the
        recent 20-bar H1 extreme, in ATR units -- a candidate still well
        inside the recent range is treated as a cleaner fade than one
        already at a fresh extreme (which may be a genuine breakout, not
        noise to fade). 2026-08-29 fix: the formula originally ADDED
        distance_from_extreme_atr directly, which does the OPPOSITE of
        this docstring's stated intent (bigger distance -> bigger score
        -> ranked HIGHER, when "still well inside the range" -- i.e.
        SMALLER distance -- was supposed to score higher). Caught before
        ever running live (sessions are dormant per ENABLED_SESSIONS) --
        now inverted via extreme_score = max(0, 1 - distance_from_extreme_atr)
        so a candidate close to a fresh extreme is penalized, not rewarded.
    Review this weighting once real session-gap trade data exists.
    """
    extreme_score = max(0.0, 1.0 - distance_from_extreme_atr)
    return (0.5 * move_atr) + (0.3 * reversal_strength) + (0.2 * extreme_score)


# ── Weekly gap — signal generation (unchanged from strategy_gap.py) ────

def generate_signals(market_data: dict, open_symbols: set = None,
                     live_prices: dict = None,
                     exhausted_symbols: set = None) -> list:
    """Detect weekend gaps and return fade signals. See strategy_gap.py's
    generate_signals docstring -- logic here is intentionally identical."""
    if open_symbols is None:
        open_symbols = set()
    if live_prices is None:
        live_prices = {}
    if exhausted_symbols is None:
        exhausted_symbols = set()

    signals = []

    for sym, df in market_data.items():
        if sym in open_symbols or sym in exhausted_symbols:
            continue
        if df is None or len(df) < MIN_BARS:
            continue

        sunday_open = live_prices.get(sym)
        if sunday_open is None or sunday_open <= 0:
            continue

        friday_close = float(df["Close"].iloc[-1])
        if friday_close <= 0:
            continue

        gap      = sunday_open - friday_close
        gap_pct  = abs(gap) / friday_close * 100.0
        gap_size = abs(gap)

        if gap_pct < MIN_GAP_PCT or gap_pct > MAX_GAP_PCT:
            continue

        if gap > 0:
            direction  = "Sell"
            stop_price = sunday_open + ATR_STOP_MULT * gap_size
            gap_target = friday_close
        else:
            direction  = "Buy"
            stop_price = sunday_open - ATR_STOP_MULT * gap_size
            gap_target = friday_close

        signals.append({
            "symbol":       sym,
            "direction":    direction,
            "score":        gap_pct,
            "atr":          gap_size,
            "close":        sunday_open,
            "stop_price":   stop_price,
            "gap_target":   gap_target,
            "gap_pct":      gap_pct,
            "gap_size":     gap_size,
            "friday_close": friday_close,
            "sunday_open":  sunday_open,
            "gap_type":     "weekly",
            "stop_mult":    ATR_STOP_MULT,   # fix #1: sizing must use this, not a hardcoded constant
        })

    signals.sort(key=lambda x: x["score"], reverse=True)
    return signals


# ── Session gap — signal generation (rebuilt; dormant unless the session
# is in ENABLED_SESSIONS) ───────────────────────────────────────────────

def generate_session_signals(session: str,
                              market_data_h1: dict,
                              open_symbols: set = None,
                              live_prices: dict = None,
                              exhausted_symbols: set = None) -> list:
    """Detect London / NY / Tokyo session-open gaps using H1 bars.

    Returns [] immediately if `session` isn't in ENABLED_SESSIONS -- the
    Phase 2 gate. When re-enabled, applies (in order): the fixed
    reference-bar lookup (no bad fallback), an ATR displacement filter
    (0.8-2.0x ATR, item 5), a reversal-confirmation requirement (item 6),
    and ranks by quality_score instead of raw gap size (item 7).
    """
    if session not in ENABLED_SESSIONS:
        return []

    cfg = SESSION_GAPS.get(session)
    if cfg is None:
        return []

    if open_symbols is None:
        open_symbols = set()
    if live_prices is None:
        live_prices = {}
    if exhausted_symbols is None:
        exhausted_symbols = set()

    signals = []

    for sym, df in market_data_h1.items():
        if sym in open_symbols or sym in exhausted_symbols:
            continue
        if df is None or len(df) < 21:   # need 20 bars of history + the current one
            continue

        session_open = live_prices.get(sym)
        if session_open is None or session_open <= 0:
            continue

        # Fix #2: no silent fallback to "last available close" -- if the
        # real reference bar isn't found, this candidate is skipped, not
        # priced off the wrong bar.
        ref_close = _find_ref_bar_close(df, cfg["ref_hour_utc"])
        if ref_close is None or ref_close <= 0:
            continue

        move = abs(session_open - ref_close)
        atr  = _atr(df)
        if atr <= 0:
            continue
        move_atr = move / atr

        # Item 5: ATR-based displacement filter replaces the old tiny
        # fixed-percent thresholds.
        if move_atr < cfg["min_atr_mult"] or move_atr > cfg["max_atr_mult"]:
            continue

        gap = session_open - ref_close

        # Item 6: require a confirming reversal candle on the most recent
        # COMPLETED bar before fading -- no longer fades the instant a
        # gap is measured.
        last       = df.iloc[-1]
        last_open  = float(last["Open"])
        last_close = float(last["Close"])
        last_high  = float(last["High"])
        last_low   = float(last["Low"])
        bullish = last_close > last_open
        bearish = last_close < last_open

        if gap > 0:
            if not bearish:
                continue
            direction  = "Sell"
            stop_price = session_open + cfg["stop_mult"] * move
            gap_target = ref_close
        else:
            if not bullish:
                continue
            direction  = "Buy"
            stop_price = session_open - cfg["stop_mult"] * move
            gap_target = ref_close

        bar_range = last_high - last_low
        reversal_strength = (abs(last_close - last_open) / bar_range) if bar_range > 0 else 0.0

        # Distance from the recent 20-bar extreme, in ATR units -- how far
        # session_open sits from the highest high / lowest low of the
        # prior 20 completed bars (excludes the current bar itself).
        window = df.iloc[-21:-1]
        if direction == "Sell":
            extreme = float(window["High"].max())
        else:
            extreme = float(window["Low"].min())
        distance_from_extreme_atr = abs(session_open - extreme) / atr if atr > 0 else 0.0

        quality_score = _session_quality_score(move_atr, reversal_strength, distance_from_extreme_atr)

        signals.append({
            "symbol":                     sym,
            "direction":                  direction,
            "score":                      quality_score,   # item 7: no longer raw gap size
            "atr":                        move,            # kept as "atr" key for size_position()'s generic interface (this is gap_size, not H1 ATR)
            "close":                      session_open,
            "stop_price":                 stop_price,
            "gap_target":                 gap_target,
            "gap_pct":                    (move / ref_close * 100.0) if ref_close else 0.0,
            "gap_size":                   move,
            "ref_close":                  ref_close,
            "session_open":               session_open,
            "gap_type":                   session,
            "stop_mult":                  cfg["stop_mult"],   # fix #1
            "risk_pct_override":          cfg["risk_pct"],
            "move_atr":                   move_atr,
            "reversal_strength":          reversal_strength,
            "distance_from_extreme_atr":  distance_from_extreme_atr,
        })

    signals.sort(key=lambda x: x["score"], reverse=True)
    return signals


def _find_ref_bar_close(df: pd.DataFrame, ref_hour: int) -> float | None:
    """Return the Close of the H1 bar that corresponds to ref_hour UTC, or
    None if it genuinely isn't present -- callers must skip (see fix #2),
    never substitute a different bar."""
    if "HourUTC" in df.columns:
        mask = df["HourUTC"] == ref_hour
        rows = df[mask]
        if not rows.empty:
            return float(rows["Close"].iloc[-1])
        return None
    for i in range(len(df) - 1, max(len(df) - 25, -1), -1):
        row = df.iloc[i]
        if row.get("HourUTC", -1) == ref_hour:
            return float(row["Close"])
    return None


# ── Exit logic (same structure as strategy_gap.py -- stop_price/gap_target
# already encode the correct per-type multiplier from signal generation,
# so no change is needed here for the sizing fix to take effect) ───────

def should_exit(position: dict, df: pd.DataFrame,
                calendar_days_held: int) -> tuple:
    if df is None or len(df) < 1:
        return False, ""

    cur_high  = float(df["High"].iloc[-1])
    cur_low   = float(df["Low"].iloc[-1])
    cur_close = float(df["Close"].iloc[-1])

    direction  = position.get("direction", "Buy")
    stop_price = position.get("stop_price", 0.0)
    gap_target = position.get("gap_target", position.get("entry_price", 0.0))
    gap_type   = position.get("gap_type", "weekly")

    if gap_type in SESSION_GAPS:
        cfg = SESSION_GAPS[gap_type]
        entry_ts = position.get("entry_datetime") or position.get("entry_date", "")
        if entry_ts:
            try:
                entry_dt   = datetime.fromisoformat(entry_ts)
                hours_held = (datetime.now() - entry_dt).total_seconds() / 3600
                if hours_held >= cfg["time_stop_hours"]:
                    return True, f"time_stop ({hours_held:.1f}h — {gap_type} session gap expired)"
            except Exception:
                pass
    else:
        if calendar_days_held >= TIME_STOP_DAYS:
            return True, f"time_stop ({calendar_days_held}d — gap not filled)"

    # Same current-close-only fix as strategy_gap.py's should_exit (never
    # reintroduce the cumulative-High/Low sticky-wick bug -- see that
    # module's should_exit docstring for the full 2026-08-24 incident).
    if direction == "Buy":
        if cur_close >= gap_target:
            return True, f"gap_filled (target={gap_target:.5f})"
        if cur_low <= stop_price:
            return True, f"hard_stop ({stop_price:.5f})"
    else:
        if cur_close <= gap_target:
            return True, f"gap_filled (target={gap_target:.5f})"
        if cur_high >= stop_price:
            return True, f"hard_stop ({stop_price:.5f})"

    return False, ""


def size_position(account_equity: float, gap_size: float,
                  min_units: int = LOT_ROUND,
                  stop_mult: float = ATR_STOP_MULT,
                  risk_pct: float = RISK_PCT,
                  block_below_min: bool = False) -> int:
    """Fix #1: stop_mult is now an explicit parameter instead of a hardcoded
    module constant -- callers must pass the SAME multiplier used to
    compute this signal's own stop_price (1.5 for weekly, 2.0 for session
    gaps), instead of this function silently assuming weekly's 1.5x for
    every gap type.

    Parameter ORDER deliberately differs from the user's original design
    doc (which put stop_mult 3rd, before min_units): forex/runner.py's
    entry loop calls every strategy's size_position() generically as
    size_position(equity, sig["atr"], pair_info["min_units"], **rp_kw) --
    min_units MUST stay the 3rd positional argument or that generic call
    site would silently bind pair_info["min_units"]'s value to stop_mult
    instead. stop_mult is supplied via **rp_kw (runner.py reads it from
    sig["stop_mult"] when present) exactly like risk_pct_override already
    works for session gaps -- same math, just a call-site-compatible
    parameter order.

    block_below_min: see forex/strategy_rsi.py's size_position() docstring
    (2026-08-28, explicit user decision, LIVE/LIVE_EUR only) -- kept for
    interface parity even though this strategy never runs on LIVE.
    """
    risk_amount   = account_equity * risk_pct
    stop_distance = stop_mult * gap_size
    if stop_distance <= 0:
        return 0 if block_below_min else min_units
    raw   = risk_amount / stop_distance
    units = int(raw // min_units) * min_units
    if units < min_units:
        return 0 if block_below_min else min_units
    return units


def scan_summary(market_data: dict, live_prices: dict = None) -> list:
    """Weekly-only gap snapshot for every pair -- used by --scan display."""
    if live_prices is None:
        live_prices = {}

    rows = []
    for sym, df in market_data.items():
        if df is None or len(df) < MIN_BARS:
            rows.append({"symbol": sym, "status": "no_data"})
            continue

        friday_close = float(df["Close"].iloc[-1])
        sunday_open  = live_prices.get(sym)

        if sunday_open is None:
            prev_close  = float(df["Close"].iloc[-2]) if len(df) >= 2 else friday_close
            gap         = friday_close - prev_close
            gap_pct     = abs(gap) / prev_close * 100.0 if prev_close > 0 else 0
            signal      = "(no live price)"
        else:
            gap     = sunday_open - friday_close
            gap_pct = abs(gap) / friday_close * 100.0 if friday_close > 0 else 0
            if gap_pct < MIN_GAP_PCT:
                signal = "no gap"
            elif gap_pct > MAX_GAP_PCT:
                signal = f"gap too large ({gap_pct:.2f}%)"
            elif gap > 0:
                signal = "*** GAP UP  → SHORT ***"
            else:
                signal = "*** GAP DOWN → LONG  ***"

        rows.append({
            "symbol":       sym,
            "friday_close": friday_close,
            "sunday_open":  sunday_open or 0.0,
            "gap":          gap if sunday_open else 0.0,
            "gap_pct":      gap_pct,
            "signal":       signal,
            "status":       "ok",
        })
    return rows
