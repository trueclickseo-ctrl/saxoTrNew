# AI-WRITTEN 2025-06-14 by claude-sonnet-5
# Rationale: Ledger shows rapid-fire repeated re-entries into the same symbol at nearly identical price levels (e.g. HKDJPY hard-stopped 19x, JPYHKD 16x, USDJPY 16x, many trades open<->close within 30min-few hours), churning small losses after the first 3 big trend-catch wins. A per-symbol cooldown after any signal blocks this same-symbol whipsaw re-entry pattern.

from __future__ import annotations

import pandas as pd

import forex.strategy_donchian_ai as _orig

# Minimum hours to wait before allowing another signal on the same symbol.
# Chosen from ledger evidence of repeated stop-outs on the same symbol
# recurring within minutes-to-hours of each other.
COOLDOWN_HOURS = 12.0

# Module-level state: last bar-timestamp a signal was emitted for a symbol.
# Persists across generate_signals() calls within the sim process.
_last_signal_time: dict = {}


def _bar_time(df):
    """Best-effort extraction of the current bar's timestamp."""
    try:
        ts = df.index[-1]
        if isinstance(ts, pd.Timestamp):
            return ts
        return pd.Timestamp(ts)
    except Exception:
        return None


def _get_symbol(sig):
    if isinstance(sig, dict):
        return sig.get("symbol") or sig.get("Symbol")
    return getattr(sig, "symbol", None)


def generate_signals(market_data: dict, open_symbols: set = None, **kwargs) -> list:
    """Wraps donchian_ai.generate_signals with a per-symbol cooldown filter.

    The underlying trade ledger showed the same symbol (HKDJPY, JPYHKD,
    USDJPY) being re-signalled and hard-stopped repeatedly within short
    windows (often <1 hour apart), producing a long string of small losses
    that erased the strategy's early large wins. This filter blocks a new
    signal on a symbol if a prior signal for that same symbol fired within
    COOLDOWN_HOURS, based on bar timestamps.
    """
    signals = _orig.generate_signals(market_data, open_symbols=open_symbols, **kwargs)
    if not signals:
        return signals

    filtered = []
    for sig in signals:
        sym = _get_symbol(sig)
        if sym is None:
            filtered.append(sig)
            continue

        df = market_data.get(sym)
        now_ts = _bar_time(df) if df is not None else None
        last_ts = _last_signal_time.get(sym)

        blocked = False
        if now_ts is not None and last_ts is not None:
            try:
                elapsed_hours = (now_ts - last_ts).total_seconds() / 3600.0
                if elapsed_hours < COOLDOWN_HOURS:
                    blocked = True
            except Exception:
                blocked = False

        if blocked:
            continue

        if now_ts is not None:
            _last_signal_time[sym] = now_ts
        filtered.append(sig)

    return filtered
