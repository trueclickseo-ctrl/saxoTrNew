# AI-WRITTEN Phase 2+3 2026-09-19 by claude-sonnet-5
# Entry filter: restrict signals to JPY-cross basket (HKDJPY/JPYHKD/USDJPY) with demonstrated edge
# Exit filter: no change -- single exit_reason bucket (hard_stop, 100% of trades) shows no actionable sub-pattern to isolate

import forex.strategy_donchian_ai as _orig
from forex.strategy_donchian_ai import should_exit as _orig_should_exit

# Closed-trade ledger (30 trades, SIM) decomposition by symbol:
#   HKDJPY: 9 trades, net +48.05 EUR (6W/3L)
#   JPYHKD: 9 trades, net +36.83 EUR (6W/3L)
#   USDJPY: 8 trades, net +4.57 EUR (4W/4L)
#   -> JPY-cross basket subtotal: 26 trades, net +89.45 EUR
#   JPYEUR/CHFJPY/CADTRY/AUDTRY: 4 trades, net -72.45 EUR (1W/3L, incl. -49.50 outlier)
# The strategy's realized edge lives almost entirely in the JPY-cross basket.
# The small sample of 'other' exotic-cross symbols produced disproportionately
# large losses relative to trade count (one single JPYEUR loss of -49.50 wiped
# out ~3x the strategy's total net profit). Until more data accumulates on
# those symbols, restrict signals to the basket with demonstrated edge.

SYMBOL_WHITELIST = frozenset({"HKDJPY", "JPYHKD", "USDJPY"})


def _get_symbol(sig):
    if isinstance(sig, dict):
        return sig.get("symbol") or sig.get("Symbol")
    return getattr(sig, "symbol", None)


def generate_signals(market_data: dict, open_symbols: set = None, **kwargs) -> list:
    """Wraps donchian_ai.generate_signals, restricting output to the JPY-cross
    basket (HKDJPY/JPYHKD/USDJPY) that has shown the strategy's actual edge in
    the closed-trade ledger. Other symbols (JPYEUR, CHFJPY, CADTRY, AUDTRY,
    etc.) are blocked pending more evidence they can be profitable.
    """
    signals = _orig.generate_signals(market_data, open_symbols=open_symbols, **kwargs)
    if not signals:
        return signals

    filtered = []
    for sig in signals:
        sym = _get_symbol(sig)
        if sym is None:
            # Unable to determine symbol -- fail open rather than silently
            # dropping a signal we can't classify.
            filtered.append(sig)
            continue
        if sym in SYMBOL_WHITELIST:
            filtered.append(sig)
    return filtered


# --- Exit-reason ledger decomposition (30 closed trades) ---
#   hard_stop: n=30, wins=16, losses=14, win_rate=53.3%, avg_pnl=+0.6 EUR
#   (no other exit_reason -- trailing 15d channel exit and 30d time stop
#    never fired ahead of the hard stop in this sample)
#
# Because 100% of closed trades share a single exit_reason bucket, there is
# no cross-reason contrast (e.g. "trend_break is fine but time_stop bleeds")
# to act on. The hard stop itself is roughly breakeven-to-slightly-positive
# (53.3% win rate, avg +0.6 EUR/trade) -- it is not a runaway loss driver,
# it is simply the *only* exit path exercised so far. Loosening or adding a
# confirmation-bar delay on the sole risk-capping exit without evidence of
# premature/whipsaw stop-outs (e.g. a same-bar re-entry-after-stop pattern,
# which is not present in this breakdown) would only increase tail risk for
# an unproven benefit. Per policy, we preserve should_exit() unchanged and
# pass through to the original logic, keeping the exit path intact until a
# richer exit_reason mix (trend_break vs time_stop vs hard_stop) is available
# to decompose.

def should_exit(position: dict, df, calendar_days_held: int):
    return _orig_should_exit(position, df, calendar_days_held)
