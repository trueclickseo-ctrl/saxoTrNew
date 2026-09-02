"""
forex/strategy_rsi_confirm.py
----------------------------
RSI(2) pullback with a CONFIRMATION-DELAY + CONVICTION single-slot.

*** RETIRED 2026-09-02 -- BACKTEST-FALSIFIED, NEVER SCANNED ***
Built and backtested the same day. A 12,700-signal / 12y / 49-CORE-pair
backtest (scratchpad/rsi_confirm_backtest.py) showed the confirmation delay
systematically enters AFTER the mean reversion it is trying to catch:
  immediate + rsi-exit (control)  -0.106 R/trade   win 56%   PF 0.65
  delayed  + rsi-exit             -0.242 R/trade   win 42%   PF 0.32
  delayed  + fast-TP (this)       -0.252 R/trade   win 45%   PF 0.32
Every variant is worse than entering on the signal, in both halves, and in
TRENDING_BULLISH-only too. RSI(2) is a mean-reversion signal -- its edge is
buying the instant of the extreme; waiting 6-30h means the bounce already
happened. There is no knob that fixes "don't wait to fade an extreme".

This module is UNWIRED from forex/runner.py (not in STRATEGIES) and kept only
as the documented negative result. See docs/forex_rsi_confirm_strategy.md.
Do not re-register it without a NEW backtest that overturns the above.

--------------------------------------------------------------------------
Original design (the user's idea, 2026-09-02):
A **SIM-only** strategy (never in either LIVE allowlist).

The user's idea (2026-09-02), built as-specified for a SIM forward-test:

  "When an RSI signal triggers we should NOT buy immediately. Keep it in a
   'LIVE CANDIDATE' bucket and observe ~8h-1 day: usually the price first
   goes against us, then starts climbing. Only enter once that turn is
   confirmed. And for a high-conviction setup, put ONE concentrated position
   on at a time (~600-800 EUR) and sell after a small profitable move."

## Lifecycle

1. **Queue.**  Every fresh `strategy_rsi` signal (that isn't already queued or
   open) becomes a *candidate*: `{direction, signal_px, signal_ts, signal_rsi,
   regime, best_adverse_px}`. Nothing is traded.  (`update_candidates`)

2. **Observe.**  Each subsequent run refreshes `best_adverse_px` (the worst
   excursion against the signal so far) from the latest bars.  A candidate
   older than `OBSERVE_MAX_HOURS` with no confirmation is dropped.

3. **Confirm & enter.**  After `OBSERVE_MIN_HOURS`, a candidate is entered iff
   the turn is confirmed (`generate_signals` returns it as a normal entry
   signal):

     Buy:  (dipped >= MIN_DIP_ATR below the signal  AND  has since recovered
            >= MIN_RECOVERY_ATR off that low, back to/above it)
           OR  immediate follow-through >= MIN_FOLLOW_ATR in our favour
           AND RSI(2) has not already fully mean-reverted (rsi_now < 65)
     Sell: mirror.

   Entry price = the *current* price (not the stale signal price); stop =
   ATR_STOP_MULT x ATR from there; a tight `tp_price` = FAST_TP_ATR x ATR
   ("sell after a small profitable move").

4. **Conviction slot.**  `SLOTS_PER_STRATEGY["rsi_confirm"] = 1` in the runner
   -- one position at a time.  Size targets `CONVICTION_NOTIONAL_QUOTE` of
   quote-currency notional, which on SIM resolves to the 1,000-unit minimum
   lot for virtually every pair (~600-1,000 EUR of base notional) -- i.e. the
   smallest concentrated position, by design.

## Governance

This is the hypothesis + deterministic code.  **Backtest is the NEXT step**
(the user asked to build first).  Until a walk-forward validates it: SIM
only, never `LIVE_ALLOWED_STRATEGIES` / `LIVE_EUR_ALLOWED_STRATEGIES`, not in
`PROFIT_LADDER_STRATEGIES`.  `strategy_rsi.py` is untouched.

PURE -- no I/O, no orders.  The candidate bucket is persisted by the runner
(`data/rsi_confirm_candidates.json`), passed in / out as a plain dict, exactly
like the gap-cooldown and lbo-v2-session state.
"""

from __future__ import annotations

from datetime import datetime, timezone

import forex.strategy_rsi as _rsi

# ── re-export the constants the runner / callers read off the module ────────
RSI_PERIOD     = _rsi.RSI_PERIOD
RSI_OVERSOLD   = _rsi.RSI_OVERSOLD
RSI_OVERBOUGHT = _rsi.RSI_OVERBOUGHT
RSI_EXIT_LONG  = _rsi.RSI_EXIT_LONG
RSI_EXIT_SHORT = _rsi.RSI_EXIT_SHORT
TREND_EMA      = _rsi.TREND_EMA
ATR_PERIOD     = _rsi.ATR_PERIOD
ATR_STOP_MULT  = _rsi.ATR_STOP_MULT
RISK_PCT       = _rsi.RISK_PCT
MAX_POSITIONS  = 1                       # conviction: one at a time
LOT_ROUND      = _rsi.LOT_ROUND
MIN_BARS       = _rsi.MIN_BARS

# ── confirmation-delay knobs (the NEW numbers -- all to be tuned by the
#    backtest that comes next; deliberately conservative starting points) ────
OBSERVE_MIN_HOURS   = 6.0     # don't act inside the first ~6h
OBSERVE_MAX_HOURS   = 30.0    # ~"first day"; drop an unconfirmed candidate after this
MIN_DIP_ATR         = 0.15    # it must actually have gone against us first ...
MIN_RECOVERY_ATR    = 0.35    # ... then climbed back this far off the extreme
MIN_FOLLOW_ATR      = 0.25    # OR: never dipped, just ran our way this far
RSI_STILL_OK_LONG   = 65.0    # a Buy candidate whose RSI already blew past this = edge gone
RSI_STILL_OK_SHORT  = 35.0
FAST_TP_ATR         = 0.60    # tight take-profit -- "sell after a small profitable move"
CONVICTION_TIME_STOP_DAYS = 4       # short leash (rsi's own is 12)
CONVICTION_NOTIONAL_QUOTE = 750.0   # target quote-ccy notional (-> 1,000-unit min lot on SIM)

_atr = _rsi._atr
_rsi_ind = _rsi._rsi


def _atr_now(df) -> float:
    try:
        return float(_atr(df["High"], df["Low"], df["Close"]).iloc[-1])
    except Exception:
        return 0.0


def _rsi_now(df) -> float:
    try:
        return float(_rsi_ind(df["Close"]).iloc[-1])
    except Exception:
        return 50.0


def _parse_ts(s):
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _conviction_units(price: float) -> int:
    if not price or price <= 0:
        return LOT_ROUND
    raw = CONVICTION_NOTIONAL_QUOTE / price
    return max(LOT_ROUND, int(raw // LOT_ROUND) * LOT_ROUND)


# ──────────────────────────────────────────────────────────────────────────
#  Stage 1-2: maintain the candidate bucket
# ──────────────────────────────────────────────────────────────────────────

def update_candidates(market_data: dict, candidates: dict | None,
                      open_symbols: set | None = None,
                      now: datetime | None = None) -> tuple[dict, list[str]]:
    """Add fresh RSI(2) signals as candidates, refresh the running adverse
    excursion of existing ones, drop the expired / now-open. Returns
    (new_candidate_dict, human_log_lines). PURE -- the runner persists it."""
    now = now or datetime.now(timezone.utc)
    open_symbols = open_symbols or set()
    out: dict = {}
    logs: list[str] = []

    for sym, cand in (candidates or {}).items():
        if sym in open_symbols:
            logs.append(f"{sym}: candidate cleared (position now open)")
            continue
        df = market_data.get(sym)
        if df is None or len(df) < 3:
            out[sym] = cand
            continue
        ts = _parse_ts(cand.get("signal_ts", ""))
        if ts is not None and (now - ts).total_seconds() / 3600.0 > OBSERVE_MAX_HOURS:
            logs.append(f"{sym}: candidate EXPIRED unconfirmed "
                        f"({(now - ts).total_seconds()/3600.0:.0f}h)")
            continue
        lo = float(df["Low"].iloc[-1]); hi = float(df["High"].iloc[-1])
        c = dict(cand)
        if c["direction"] == "Buy":
            c["best_adverse_px"] = min(float(c.get("best_adverse_px", c["signal_px"])), lo)
        else:
            c["best_adverse_px"] = max(float(c.get("best_adverse_px", c["signal_px"])), hi)
        out[sym] = c

    fresh = _rsi.generate_signals(market_data, open_symbols=open_symbols)
    for sig in fresh:
        sym = sig["symbol"]
        if sym in out or sym in open_symbols:
            continue
        out[sym] = {
            "direction":       sig["direction"],
            "signal_px":       float(sig["close"]),
            "signal_ts":       now.isoformat(),
            "signal_rsi":      float(sig.get("rsi", 0.0)),
            "regime":          sig.get("regime_at_entry"),
            "best_adverse_px": float(sig["close"]),
        }
        logs.append(f"{sym}: QUEUED {sig['direction']} candidate @ {sig['close']:.5f} "
                    f"(RSI {sig.get('rsi', 0):.0f}) -- observing")
    return out, logs


# ──────────────────────────────────────────────────────────────────────────
#  Stage 3: confirm -> entry signal
# ──────────────────────────────────────────────────────────────────────────

def _confirmed(cand: dict, df, now: datetime) -> dict | None:
    ts = _parse_ts(cand.get("signal_ts", ""))
    if ts is None:
        return None
    hrs = (now - ts).total_seconds() / 3600.0
    if hrs < OBSERVE_MIN_HOURS or hrs > OBSERVE_MAX_HOURS:
        return None
    atr = _atr_now(df)
    if atr <= 0:
        return None
    px = float(df["Close"].iloc[-1])
    rsi_now = _rsi_now(df)
    sig_px = float(cand["signal_px"])
    ext = float(cand.get("best_adverse_px", sig_px))
    direction = cand["direction"]

    if direction == "Buy":
        dipped = (sig_px - ext) >= MIN_DIP_ATR * atr
        recovered = px >= ext and (px - ext) >= MIN_RECOVERY_ATR * atr
        immediate = (px - sig_px) >= MIN_FOLLOW_ATR * atr
        # RSI(2) naturally shoots up on the bounce -- so the "still constructive"
        # gate ONLY applies to the immediate-follow-through path (there, a
        # blown-out RSI means we simply missed it and would be chasing).
        ok = (dipped and recovered) or (immediate and rsi_now < RSI_STILL_OK_LONG)
        stop = px - ATR_STOP_MULT * atr
        tp = px + FAST_TP_ATR * atr
    else:
        spiked = (ext - sig_px) >= MIN_DIP_ATR * atr
        recovered = px <= ext and (ext - px) >= MIN_RECOVERY_ATR * atr
        immediate = (sig_px - px) >= MIN_FOLLOW_ATR * atr
        ok = (spiked and recovered) or (immediate and rsi_now > RSI_STILL_OK_SHORT)
        stop = px + ATR_STOP_MULT * atr
        tp = px - FAST_TP_ATR * atr

    if not ok:
        return None
    adverse_atr = abs(sig_px - ext) / atr
    return {
        "symbol":          cand["_sym"],
        "direction":       direction,
        "close":           px,
        "stop_price":      float(stop),
        "tp_price":        float(tp),
        "atr":             atr,
        "rsi":             rsi_now,
        "score":           float(abs(px - ext) / atr),   # recovery strength
        "units":           _conviction_units(px),
        "stage":           "confirm",
        "regime_at_entry": cand.get("regime"),
        "observed_hours":  round(hrs, 1),
        "adverse_atr":     round(adverse_atr, 2),
        "signal_px":       sig_px,
    }


def generate_signals(market_data: dict, open_symbols: set | None = None,
                     candidates: dict | None = None,
                     now: datetime | None = None) -> list:
    """Return ONLY confirmed entries drawn from the candidate bucket. Fresh
    RSI signals are queued by `update_candidates`, not entered here."""
    now = now or datetime.now(timezone.utc)
    open_symbols = open_symbols or set()
    out = []
    for sym, cand in (candidates or {}).items():
        if sym in open_symbols:
            continue
        df = market_data.get(sym)
        if df is None or len(df) < MIN_BARS:
            continue
        c = dict(cand); c["_sym"] = sym
        sig = _confirmed(c, df, now)
        if sig:
            out.append(sig)
    out.sort(key=lambda s: s["score"], reverse=True)   # strongest turn first (slots=1)
    return out


# ──────────────────────────────────────────────────────────────────────────
#  Exits: fast take-profit + short time stop, else rsi's own management
# ──────────────────────────────────────────────────────────────────────────

def should_exit(position: dict, df, calendar_days_held: int) -> tuple:
    if df is None or len(df) < 3:
        return False, ""
    px = float(df["Close"].iloc[-1])
    entry = float(position.get("entry_price", px))
    atr0 = float(position.get("atr_at_entry", 0.0) or 0.0)
    direction = position.get("direction", "Buy")
    gain = (px - entry) if direction == "Buy" else (entry - px)
    if atr0 > 0 and gain >= FAST_TP_ATR * atr0:
        return True, f"fast_tp (+{gain/atr0:.2f} ATR)"
    if calendar_days_held >= CONVICTION_TIME_STOP_DAYS:
        return True, f"time_stop ({calendar_days_held}d)"
    if "stop_price" not in position:
        return False, ""                       # nothing more we can check
    return _rsi.should_exit(position, df, calendar_days_held)


size_position        = _rsi.size_position           # fallback; runner uses sig["units"]
trailing_stop_update = _rsi.trailing_stop_update    # ratchet-only safety on the stop


def scan_summary(market_data: dict) -> list:
    rows = _rsi.scan_summary(market_data) if hasattr(_rsi, "scan_summary") else []
    return rows
