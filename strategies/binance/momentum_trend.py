"""
strategies/binance/momentum_trend.py
--------------------------------------
Crypto momentum / trend-following strategy for Binance.

Signal logic (both must fire simultaneously on a daily bar close):
  1. RSI-14 in [rsi_low, rsi_high]   -- in momentum zone; not oversold, not overbought
  2. Close > max(close, prior N days) -- Donchian channel breakout (new N-day high)

Entry: next bar's open (walk-forward; no look-ahead).

Exit (whichever triggers first):
  1. Close < min(close, prior M days) -- Donchian trailing stop (new M-day low)
  2. Hard stop-loss at stop_pct below entry (0 = disabled)
  3. Max-hold cap in days (0 = disabled; rely on Donchian exit only)

Rationale: mean-reversion v1 failed backtesting (Sharpe 0.45, WR 38% at best
in 8,505 combos -- see docs/binance/strategy_notes.md). Trend-following is the
structural complement: crypto exhibits strong momentum during bull phases, and
Donchian exits adapt to volatility rather than using a fixed take-profit target.

Pure module -- no network calls. Accepts a BrokerInterface adapter and config dict.
Returns a list of CandidateSignal objects for the bot entry point.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.broker_interface import BrokerInterface

# Reuse RSI implementation from mean_reversion -- same Wilder algorithm.
from strategies.binance.mean_reversion import _rsi

STRATEGY_ID = "momentum_trend_v1"


@dataclass
class CandidateSignal:
    symbol:         str
    price:          Decimal
    rsi:            float
    breakout_high:  float   # prior-N-day high that today's close exceeded
    days_high:      int     # breakout lookback used
    action:         str     # "BUY" | "QUEUED" | "SKIP"
    reason:         str = ""
    strategy:       str = STRATEGY_ID


def scan(
    adapter: "BrokerInterface",
    symbols: list[str],
    cfg: dict,
    open_slots: int,
) -> list[CandidateSignal]:
    """
    Scan symbols and return CandidateSignal list (BUY / QUEUED / SKIP).

    adapter     -- BinanceAdapter (or any BrokerInterface)
    symbols     -- list of symbols to scan
    cfg         -- strategy sub-dict from config yaml; expected keys:
                     rsi_low          (default 50)
                     rsi_high         (default 70)
                     breakout_days    (default 20)
                     exit_days        (default 10)
                     stop_loss_pct    (default 0.0 -- disabled)
                     max_hold_days    (default 0   -- disabled)
    open_slots  -- how many new positions can be opened right now
    """
    rsi_low       = float(cfg.get("rsi_low",       50))
    rsi_high      = float(cfg.get("rsi_high",      70))
    breakout_days = int(cfg.get("breakout_days",   20))
    rsi_period    = int(cfg.get("rsi_period",      14))
    kline_needed  = breakout_days + rsi_period + 10

    results: list[CandidateSignal] = []
    slots_remaining = open_slots

    for sym in symbols:
        try:
            bars   = adapter.get_ohlcv(sym, interval="1d", limit=kline_needed)
            closes = [float(b["close"]) for b in bars]

            if len(closes) < kline_needed:
                results.append(CandidateSignal(
                    sym, Decimal(0), 0.0, 0.0, breakout_days,
                    "SKIP", "insufficient_history",
                ))
                continue

            price    = closes[-1]
            rsi_val  = _rsi(closes, period=rsi_period)
            # Prior-N-day high: exclude today's bar to avoid look-ahead
            prior_high = max(closes[-(breakout_days + 1):-1])

            cond_rsi      = rsi_low <= rsi_val <= rsi_high
            cond_breakout = price > prior_high

            if not (cond_rsi and cond_breakout):
                skip_reasons = []
                if not cond_rsi:
                    skip_reasons.append(
                        f"RSI={rsi_val:.1f} not in [{rsi_low},{rsi_high}]"
                    )
                if not cond_breakout:
                    skip_reasons.append(
                        f"close={price:.4f} <= {breakout_days}d high={prior_high:.4f}"
                    )
                results.append(CandidateSignal(
                    sym, Decimal(str(price)), rsi_val, prior_high, breakout_days,
                    "SKIP", "; ".join(skip_reasons),
                ))
                continue

            action = "BUY" if slots_remaining > 0 else "QUEUED"
            if slots_remaining > 0:
                slots_remaining -= 1

            results.append(CandidateSignal(
                symbol=sym,
                price=Decimal(str(price)),
                rsi=rsi_val,
                breakout_high=prior_high,
                days_high=breakout_days,
                action=action,
            ))

        except Exception as exc:
            results.append(CandidateSignal(
                sym, Decimal(0), 0.0, 0.0, breakout_days,
                "SKIP", f"error: {exc}",
            ))

    return results


def should_exit(
    closes: list[float],
    entry_price: float,
    days_held: int,
    cfg: dict,
) -> tuple[bool, str]:
    """
    Check exit conditions given the recent close series ending with today's close.

    Returns (should_exit, reason_string).
    Called by the bot on each bar for every open position.

    closes       -- recent closes, last element = today's close; must have at
                    least exit_days + 1 elements
    entry_price  -- price at which the position was entered
    days_held    -- number of days the position has been open
    cfg          -- same strategy config dict as scan()
    """
    exit_days   = int(cfg.get("exit_days",      10))
    stop_pct    = float(cfg.get("stop_loss_pct", 0.0))
    max_hold    = int(cfg.get("max_hold_days",   0))

    today = closes[-1]

    # Hard stop (checked first — intraday trigger in live; close-based in backtest)
    if stop_pct > 0 and today < entry_price * (1 - stop_pct):
        return True, f"hard_stop -{stop_pct*100:.0f}%"

    # Max-hold safety cap
    if max_hold > 0 and days_held >= max_hold:
        return True, f"max_hold {days_held}d"

    # Donchian trailing stop: close below prior-M-day low
    if len(closes) >= exit_days + 1:
        trailing_low = min(closes[-(exit_days + 1):-1])
        if today < trailing_low:
            return True, f"donchian_exit below {exit_days}d low={trailing_low:.4f}"

    return False, ""
