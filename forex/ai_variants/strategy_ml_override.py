# AI-WRITTEN Phase 2+3 2026-09-17 by claude-sonnet-5
# Entry filter: exclude TRY (Turkish Lira) and XAU (gold) cross-pair symbols, which showed a consistent pattern of large stop-loss losses.
# Exit filter: retain the 3-consecutive-close hard_stop confirmation from the prior pass, and ADD a minimum-ATR-distance buffer so a breach must clear the stop by >=0.15*ATR(14) (not just barely touch it) before the exit is honored -- hard_stop remained the largest net-loss bucket (-326.30 over 16 trades, 37.5% WR) even after the 3-bar escalation, suggesting marginal/whipsaw breaches are still slipping through.

import pandas as pd
import numpy as np
from forex.strategy_ml import generate_signals as _orig_generate_signals
from forex.strategy_ml import should_exit as _orig_should_exit
from forex.strategy_ml import _atr as _atr

# Symbol substrings associated with outsized losses in the closed-trade sample:
#   TRY (Turkish Lira) trades: CHFTRY -60.77, AUDTRY -30.12, GBPTRY -12.42,
#       USDTRY -22.11, NZDTRY +26.61  -> net -98.81 over 5 trades
#   XAU (gold) cross trades:   XAUTRY -137.96, XAUTHB -107.83 -> net -245.79
# Combined these 7 trades cost the strategy ~-344.6 EUR out of a +623 EUR total,
# i.e. removing them would have roughly 1.5x'd total P&L on this sample.
_EXCLUDE_SUBSTRINGS = ("TRY", "XAU")


def _is_excluded(symbol: str) -> bool:
    if not symbol:
        return False
    s = symbol.upper()
    return any(sub in s for sub in _EXCLUDE_SUBSTRINGS)


def generate_signals(market_data: dict, open_symbols: set = None, **kwargs) -> list:
    """Wrapper around strategy_ml.generate_signals that filters out
    TRY (Turkish Lira) and XAU (gold) cross-pair symbols, which showed
    a consistent pattern of large stop-loss losses in the closed SIM
    trade record (34 trades sample).
    """
    if market_data:
        filtered_market_data = {
            sym: df for sym, df in market_data.items() if not _is_excluded(sym)
        }
    else:
        filtered_market_data = market_data

    signals = _orig_generate_signals(filtered_market_data, open_symbols=open_symbols, **kwargs)

    if not signals:
        return signals

    # Defensive post-filter in case original signal objects carry symbol
    # info differently (dict-like or attribute-like).
    def _sig_symbol(sig):
        if isinstance(sig, dict):
            return sig.get("symbol") or sig.get("Symbol")
        return getattr(sig, "symbol", None)

    return [sig for sig in signals if not _is_excluded(_sig_symbol(sig) or "")]


# --- Phase 3: exit logic override -----------------------------------------
#
# Updated closed-trade exit_reason breakdown (36 quality trades) shows:
#   hard_stop            : n=16, win_rate=37.5%, total_pnl=-326.30, avg=-20.4
#   STOP-LOSS hit @ ...   : n=4 individual broker-side fills, all losers
#                           (small n each, these are broker-executed stop
#                           fills that happen outside should_exit() and
#                           cannot be intercepted by this function)
#   ml_flip               : n=3,  win_rate=66.7%, total_pnl=+1.95
#   roster_flatten_...    : n=13, win_rate=38.5%, total_pnl=+1146.13 (forced)
#
# A prior Phase 3 pass already escalated the hard_stop confirmation from
# 2 to 3 consecutive adverse closes, which modestly improved the bucket
# (win rate 28.6% -> 37.5%, avg loss -27.3 -> -20.4). Despite that, hard_stop
# remains the single largest net-loss exit reason under our control
# (-326.30 total, 10 losses vs 6 wins). This suggests some 3-bar-confirmed
# breaches are still only marginally beyond the stop level -- i.e. slow
# grinding whipsaws rather than a clean directional break. We add a second,
# independent filter on top of the existing 3-bar rule: the final breaching
# close must clear the stop price by at least 0.15 * ATR(14), a small but
# non-trivial distance buffer that should filter out the weakest marginal
# breaches while still honoring clear, decisive stop-outs promptly.
# Broker-side 'STOP-LOSS hit @ X' fills remain outside this function's
# control and are left unchanged.


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    exit_flag, reason = _orig_should_exit(position, df, calendar_days_held)

    if not exit_flag or reason != "hard_stop":
        return exit_flag, reason

    try:
        stop_price = position.get("stop_price")
        direction = (position.get("direction") or "").lower()
        closes = df["Close"]
        highs = df["High"]
        lows = df["Low"]

        if stop_price is None or direction not in ("long", "short") or len(closes) < 3:
            return exit_flag, reason

        last_close = closes.iloc[-1]
        prev_close = closes.iloc[-2]
        prev2_close = closes.iloc[-3]

        if direction == "long":
            breached_last = last_close <= stop_price
            breached_prev = prev_close <= stop_price
            breached_prev2 = prev2_close <= stop_price
        else:
            breached_last = last_close >= stop_price
            breached_prev = prev_close >= stop_price
            breached_prev2 = prev2_close >= stop_price

        if not (breached_last and breached_prev and breached_prev2):
            # Fewer than 3 consecutive confirmed breaches -- hold and
            # let should_exit() re-evaluate on the next bar.
            return False, "hard_stop_pending_confirmation"

        # Second filter: require the breach distance to clear a minimum
        # ATR-based buffer, to avoid honoring marginal/whipsaw breaches
        # that happen to persist for 3 bars but barely clear the stop.
        try:
            atr_series = _atr(highs, lows, closes)
            atr_val = atr_series.iloc[-1] if len(atr_series) else None
            if atr_val is not None and not pd.isna(atr_val) and atr_val > 0:
                if direction == "long":
                    breach_distance = stop_price - last_close
                else:
                    breach_distance = last_close - stop_price
                if breach_distance < 0.15 * atr_val:
                    return False, "hard_stop_pending_buffer"
        except Exception:
            # If ATR cannot be computed, skip the buffer check and fall
            # back to the already-confirmed 3-bar decision.
            pass

    except Exception:
        # Any unexpected data shape: fall back to original decision, don't
        # risk silently overriding a legitimate stop.
        return exit_flag, reason

    return exit_flag, reason
