"""
forex/strategy_gap.py
---------------------
FX Strategy 6 — Gap Fill (Weekly + Session).

SESSIONS TRADED:
  1. WEEKLY  — Sunday 22:00 UTC reopen vs Friday daily close (~80-85% WR)
     Entry: Monday 03:00 PKT (right after FX market reopens)
     Time stop: 7 calendar days

  2. LONDON  — Monday-Friday 07:00 UTC open vs Asian session close (~65% WR)
     Entry: 12:00 PKT (07:00 UTC) — first hour of London session
     Reference: last H1 bar before 07:00 UTC (i.e. 06:00 UTC close)
     Time stop: 8 hours

  3. NEWYORK — Monday-Friday 12:00 UTC open vs European morning close (~63% WR)
     Entry: 17:00 PKT (12:00 UTC) — first hour of NY session
     Reference: last H1 bar before 12:00 UTC (i.e. 11:00 UTC close)
     Time stop: 6 hours

ENTRY LOGIC (all sessions):
  GAP UP   → price opened ABOVE reference close → SHORT (fade back down)
  GAP DOWN → price opened BELOW reference close → LONG  (fade back up)

EXIT (first condition hit):
  A. Gap filled: price reaches reference close level (gap_target)
  B. Hard stop: ATR_STOP_MULT × gap_size against the position
  C. Time stop: session-specific (7 days / 8 hours / 6 hours)

SIZING:
  Risk RISK_PCT of account equity per trade.
  Stop distance = ATR_STOP_MULT × gap_size.

IMPORTANT:
  NEEDS_LIVE_PRICES = True — the runner fetches the current session-open price
  via Saxo infoprices. For weekly, the last D1 bar = Friday close; the live
  price = Sunday open. For London/NY, the last H1 bar before session open =
  reference; the live price = session open.

THIS MODULE IS PURE — no I/O, no orders, no state.
All execution lives in forex/runner.py.
"""

import pandas as pd
from datetime import datetime, timezone

# ── Weekly gap parameters ──────────────────────────────────────────────────────
MIN_GAP_PCT    = 0.10   # minimum gap size as % of price (filters spread noise)
MAX_GAP_PCT    = 2.00   # maximum gap size — extreme gaps fill less reliably
ATR_STOP_MULT  = 1.5    # stop = 1.5 × gap_size
RISK_PCT       = 0.01   # 1% equity risk per trade
TIME_STOP_DAYS = 7      # calendar days (≈ 5 trading days Mon-Fri)
LOT_ROUND      = 1_000  # Saxo micro-lot minimum
MIN_BARS       = 5      # need at least a few bars to confirm reference close

# Tells the runner to fetch live prices before calling generate_signals
NEEDS_LIVE_PRICES = True

# ── Session gap definitions ────────────────────────────────────────────────────
# ref_hour_utc: the H1 bar whose CLOSE is used as the reference price.
#   London open 07:00 UTC → reference = bar that closed at 06:00 UTC (hour=6)
#   NY open 12:00 UTC     → reference = bar that closed at 11:00 UTC (hour=11)
#   Tokyo open 00:00 UTC  → reference = bar that closed at 23:00 UTC (hour=23)
SESSION_GAPS = {
    "london": {
        "open_hour_utc":    7,      # London opens 07:00 UTC
        "ref_hour_utc":     6,      # last Asian hour bar (06:00-07:00 UTC)
        "min_gap_pct":      0.05,   # tighter: session moves are smaller
        "max_gap_pct":      0.40,
        "stop_mult":        2.0,    # wider relative stop — shorter time horizon
        "time_stop_hours":  8,      # exit if not filled within one London session
        "risk_pct":         0.005,  # half-size (0.5%) — lower WR than weekly
        "pairs": [                  # London-centric pairs
            "EURUSD", "GBPUSD", "EURJPY", "GBPJPY",
            "EURCHF", "USDCHF", "EURGBP",
        ],
    },
    "newyork": {
        "open_hour_utc":    12,
        "ref_hour_utc":     11,     # last European morning hour bar
        "min_gap_pct":      0.05,
        "max_gap_pct":      0.40,
        "stop_mult":        2.0,
        "time_stop_hours":  6,
        "risk_pct":         0.005,
        "pairs": [                  # NY-active pairs
            "EURUSD", "GBPUSD", "USDJPY", "AUDUSD",
            "USDCAD", "NZDUSD", "USDCHF",
        ],
    },
    "tokyo": {
        "open_hour_utc":    0,      # Tokyo opens 00:00 UTC (midnight)
        "ref_hour_utc":     23,     # last NY afternoon hour bar
        "min_gap_pct":      0.04,
        "max_gap_pct":      0.30,
        "stop_mult":        2.0,
        "time_stop_hours":  7,      # covers Tokyo + early London
        "risk_pct":         0.005,
        "pairs": [                  # JPY, AUD, NZD most active in Tokyo
            "USDJPY", "AUDJPY", "NZDJPY", "CADJPY",
            "AUDUSD", "NZDUSD", "AUDCAD",
        ],
    },
}


# ── Weekly gap — signal generation ────────────────────────────────────────────

def generate_signals(market_data: dict, open_symbols: set = None,
                     live_prices: dict = None) -> list:
    """Detect weekend gaps and return fade signals.

    market_data   : {symbol: pd.DataFrame} — daily bars, most recent = Friday close
    open_symbols  : symbols already held by this strategy (skip them)
    live_prices   : {symbol: float} — Sunday open mid prices from Saxo infoprices

    Returns list of signal dicts sorted by gap_pct descending.
    """
    if open_symbols is None:
        open_symbols = set()
    if live_prices is None:
        live_prices = {}

    signals = []

    for sym, df in market_data.items():
        if sym in open_symbols:
            continue
        if df is None or len(df) < MIN_BARS:
            continue

        sunday_open = live_prices.get(sym)
        if sunday_open is None or sunday_open <= 0:
            continue

        friday_close = float(df["Close"].iloc[-1])
        if friday_close <= 0:
            continue

        gap        = sunday_open - friday_close
        gap_pct    = abs(gap) / friday_close * 100.0
        gap_size   = abs(gap)

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
        })

    signals.sort(key=lambda x: x["score"], reverse=True)
    return signals


# ── Session gap — signal generation ───────────────────────────────────────────

def generate_session_signals(session: str,
                              market_data_h1: dict,
                              open_symbols: set = None,
                              live_prices: dict = None) -> list:
    """Detect London / NY / Tokyo session-open gaps using H1 bars.

    session        : "london", "newyork", or "tokyo"
    market_data_h1 : {symbol: pd.DataFrame} — H1 bars with a 'Time' column (UTC hour int)
    open_symbols   : symbols already held by this strategy (skip them)
    live_prices    : {symbol: float} — current session-open mid price

    Returns list of signal dicts (same shape as generate_signals).
    """
    cfg = SESSION_GAPS.get(session)
    if cfg is None:
        return []

    if open_symbols is None:
        open_symbols = set()
    if live_prices is None:
        live_prices = {}

    target_pairs = set(cfg["pairs"])
    signals = []

    for sym, df in market_data_h1.items():
        if sym not in target_pairs:
            continue
        if sym in open_symbols:
            continue
        if df is None or len(df) < 4:
            continue

        session_open = live_prices.get(sym)
        if session_open is None or session_open <= 0:
            continue

        # Find the H1 bar that closed at ref_hour_utc (the last bar before the session)
        ref_hour = cfg["ref_hour_utc"]
        ref_close = _find_ref_bar_close(df, ref_hour)
        if ref_close is None or ref_close <= 0:
            # Fallback: use the last completed bar
            ref_close = float(df["Close"].iloc[-1])

        gap      = session_open - ref_close
        gap_pct  = abs(gap) / ref_close * 100.0
        gap_size = abs(gap)

        if gap_pct < cfg["min_gap_pct"] or gap_pct > cfg["max_gap_pct"]:
            continue

        if gap > 0:
            direction  = "Sell"
            stop_price = session_open + cfg["stop_mult"] * gap_size
            gap_target = ref_close
        else:
            direction  = "Buy"
            stop_price = session_open - cfg["stop_mult"] * gap_size
            gap_target = ref_close

        signals.append({
            "symbol":        sym,
            "direction":     direction,
            "score":         gap_pct,
            "atr":           gap_size,
            "close":         session_open,
            "stop_price":    stop_price,
            "gap_target":    gap_target,
            "gap_pct":       gap_pct,
            "gap_size":      gap_size,
            "ref_close":     ref_close,
            "session_open":  session_open,
            "gap_type":      session,       # "london" / "newyork" / "tokyo"
            "risk_pct_override": cfg["risk_pct"],
        })

    signals.sort(key=lambda x: x["score"], reverse=True)
    return signals


def _find_ref_bar_close(df: pd.DataFrame, ref_hour: int) -> float | None:
    """Return the Close of the H1 bar that corresponds to ref_hour UTC.

    df must have a 'HourUTC' column (integer 0-23) populated by the runner.
    Falls back to scanning the last 24 rows if column missing.
    """
    if "HourUTC" in df.columns:
        mask = df["HourUTC"] == ref_hour
        rows = df[mask]
        if not rows.empty:
            return float(rows["Close"].iloc[-1])
        return None

    # Fallback: assume bars are ordered oldest-to-newest, scan last 24
    for i in range(len(df) - 1, max(len(df) - 25, -1), -1):
        row = df.iloc[i]
        if row.get("HourUTC", -1) == ref_hour:
            return float(row["Close"])
    return None


# ── Exit logic ─────────────────────────────────────────────────────────────────

def should_exit(position: dict, df: pd.DataFrame,
                calendar_days_held: int) -> tuple:
    """Decide whether to exit an open gap fill position.

    Handles both weekly (day-based stop) and session gaps (hour-based stop).
    Returns (exit: bool, reason: str).
    """
    if df is None or len(df) < 1:
        return False, ""

    cur_high  = float(df["High"].iloc[-1])
    cur_low   = float(df["Low"].iloc[-1])

    direction  = position.get("direction", "Buy")
    stop_price = position.get("stop_price", 0.0)
    gap_target = position.get("gap_target", position.get("entry_price", 0.0))
    gap_type   = position.get("gap_type", "weekly")

    # Time stop — session gaps use hours; weekly uses calendar days
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

    if direction == "Buy":
        if cur_high >= gap_target:
            return True, f"gap_filled (target={gap_target:.5f})"
        if cur_low <= stop_price:
            return True, f"hard_stop ({stop_price:.5f})"
    else:
        if cur_low <= gap_target:
            return True, f"gap_filled (target={gap_target:.5f})"
        if cur_high >= stop_price:
            return True, f"hard_stop ({stop_price:.5f})"

    return False, ""


def size_position(account_equity: float, atr: float,
                  min_units: int = LOT_ROUND,
                  risk_pct: float = RISK_PCT) -> int:
    """Size the position. atr = gap_size here.

    Accepts optional risk_pct override for session gaps (lower WR → smaller size).
    """
    risk_amount   = account_equity * risk_pct
    stop_distance = ATR_STOP_MULT * atr
    if stop_distance <= 0:
        return min_units
    raw   = risk_amount / stop_distance
    units = int(raw // min_units) * min_units
    return max(units, min_units)


def scan_summary(market_data: dict, live_prices: dict = None) -> list:
    """Gap snapshot for every pair — used by --scan display."""
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
