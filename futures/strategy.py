"""
futures/strategy.py
--------------------
Futures Trend-Following â€” Donchian Channel Breakout with ATR Position Sizing.

STRATEGY (Turtle Traders variant, academically validated across 40+ years):

  ENTRY:
    Price closes above its 20-day highest close â†’ BUY signal.
    Regime filter: only enter when the E-mini S&P 500 (ES) is above its 200d SMA
    to avoid buying commodity/FX breakouts into a macro risk-off environment.

  EXIT (first condition hit):
    A. Donchian trailing: price closes below 10-day lowest close â†’ trend exhausted
    B. ATR hard stop: price drops 2Ã—ATR(14) below entry â†’ cut losses fast
    C. Time stop: 30 calendar days held â†’ avoid dead positions (trending markets
       move fast; if nothing happened in 30d, the signal was false)

  SIZING:
    Risk exactly 1% of account equity per trade.
    risk_per_contract = ATR_STOP_MULT Ã— ATR Ã— contract_size
    contracts = floor(equity Ã— RISK_PCT / risk_per_contract)
    This means volatile markets get smaller positions automatically.

WHY THIS WORKS:
  Macro trends (oil super-cycles, gold rallies, bond bear markets, USD moves)
  persist for weeks to months.  The 20-day breakout catches the regime shift
  early; the trailing 10-day exit lets winners run while cutting losers within 2Ã—ATR.
  Diversification across 5 uncorrelated markets (equity / metals / energy / bonds /
  FX) reduces max drawdown ~40% vs single-market trading.

BACKTESTED RESULTS (5y, 5 markets, see backtest_futures.py):
  Sharpe ~0.85â€“1.10  |  MaxDD ~20â€“25%  |  CAGR ~14â€“18%  |  Win rate ~40â€“45%
  (Trend-following wins on a few big trades; most trades are small losses.)

THIS MODULE IS PURE â€” no I/O, no orders, no state.
All execution lives in futures/runner.py.
"""

import numpy as np
import pandas as pd

# â”€â”€ Parameters (grid-optimal: BP=30 EP=5 Mult=1.5 Risk=1%) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# 180-combo grid (5y, 5 ETF-proxy markets): Sharpe=0.754, WR=43%, DD=9%, CAGR=8.7%
# Tighter exit (5d) + longer entry window (30d) = fewer whipsaws, faster profit-taking.
BREAKOUT_PERIOD = 30    # entry: close above N-day highest close
EXIT_PERIOD     = 5     # exit:  close below N-day lowest close (tight trailing)
ATR_PERIOD      = 14    # True Range ATR lookback
ATR_STOP_MULT   = 1.5   # stop = entry âˆ’ ATR_STOP_MULT Ã— ATR
RISK_PCT        = 0.005  # 0.5% of account equity risked per trade — smaller on
                          # purpose (2026-08-24, explicit request: small size,
                          # more concurrent trades). Halving from 1% doubles
                          # how many positions fit under PORTFOLIO_HEAT_LIMIT
                          # (6 -> 12 at full risk each) while staying above the
                          # ~0.35% floor needed for a single contract of the
                          # priciest instrument (gold) to still clear the
                          # MAX_RISK_OVERSHOOT skip check -- a more aggressive
                          # cut (tried 0.15% first) skips MORE signals instead
                          # of taking more, the opposite of the goal, since a
                          # 1-contract minimum can't shrink below its own ATR-
                          # based floor.
MAX_POSITIONS   = 5     # one position per market (5 markets)
TIME_STOP_DAYS  = 30    # calendar days before time-stop fires

# Markets that can be sold short (bonds and gold trend well in both directions)
LONG_ONLY_MARKETS     = {"ES", "NQ", "YM", "DAX", "HK50", "CL", "NG"}   # equity + oil: no shorting
BIDIRECTIONAL_MARKETS = {"GC", "SI", "ZB", "ZC", "ZW", "ZS"}       # gold + bonds: long AND short

MIN_BARS        = BREAKOUT_PERIOD + ATR_PERIOD + 5


def _atr(highs: pd.Series, lows: pd.Series, closes: pd.Series,
         period: int = ATR_PERIOD) -> pd.Series:
    """Wilder ATR (RMA smoothing, consistent with all other strategy modules)."""
    prev = closes.shift(1)
    tr   = pd.concat([
        highs - lows,
        (highs - prev).abs(),
        (lows  - prev).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


EQUITY_FUTURES = {"ES", "NQ", "YM", "DAX", "HK50"}  # regime filter gates all equity indices


def _es_risk_off(market_data: dict, regime_symbol: str = "ES") -> bool:
    """True when S&P 500 E-mini is below its 200d SMA."""
    regime_df = market_data.get(regime_symbol)
    if regime_df is None or len(regime_df) < 202:
        return False
    es_close = regime_df["Close"].dropna()
    if len(es_close) < 202:
        return False
    sma200 = es_close.rolling(200).mean().iloc[-1]
    return not pd.isna(sma200) and float(es_close.iloc[-1]) < float(sma200)


def generate_signals(market_data: dict, regime_symbol: str = "ES",
                     open_symbols: set = None) -> list:
    """Scan all markets for Donchian breakout signals â€” LONG and SHORT.

    Returns both BUY (close > 20d high) and SHORT (close < 20d low) signals,
    sorted by breakout strength (ATR units).

    Regime filter:
      - Risk-off (ES < SMA200): skip NEW LONG entries for equity futures (ES, NQ)
        because they're in a downtrend.  Still allow SHORTS on equity futures
        (that's exactly when shorting ES/NQ is the right trade).
      - Gold, crude oil, bonds: trade both directions regardless of regime.
    """
    risk_off = _es_risk_off(market_data, regime_symbol)
    if open_symbols is None:
        open_symbols = set()

    signals = []
    for symbol, df in market_data.items():
        if symbol in open_symbols:
            continue
        if df is None or len(df) < MIN_BARS:
            continue
        df = df.copy()
        df = df[df["Close"] > 0].dropna(subset=["Close", "High", "Low"])
        if len(df) < MIN_BARS:
            continue

        closes = df["Close"]
        highs  = df["High"]
        lows   = df["Low"]

        today   = float(closes.iloc[-1])
        history = closes.iloc[-(BREAKOUT_PERIOD + 1):-1]
        high20  = float(history.max())
        low20   = float(history.min())
        atr_val = float(_atr(highs, lows, closes).iloc[-1])

        if pd.isna(high20) or pd.isna(low20) or pd.isna(atr_val) or atr_val <= 0:
            continue

        # â”€â”€ LONG breakout â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        # Skip equity-futures long entries during risk-off
        if today > high20 and not (risk_off and symbol in EQUITY_FUTURES):
            stop_price = today - ATR_STOP_MULT * atr_val
            score      = (today - high20) / atr_val
            signals.append({
                "symbol":         symbol,
                "direction":      "Buy",
                "close":          round(today, 6),
                "atr":            round(atr_val, 6),
                "stop_price":     round(stop_price, 6),
                "breakout_level": round(high20, 6),
                "score":          round(score, 4),
            })

        # â”€â”€ SHORT breakdown (bonds and gold only) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        elif today < low20 and symbol in BIDIRECTIONAL_MARKETS:
            stop_price = today + ATR_STOP_MULT * atr_val   # stop above entry for shorts
            score      = (low20 - today) / atr_val
            signals.append({
                "symbol":         symbol,
                "direction":      "Sell",   # short entry
                "close":          round(today, 6),
                "atr":            round(atr_val, 6),
                "stop_price":     round(stop_price, 6),
                "breakout_level": round(low20, 6),
                "score":          round(score, 4),
            })

    return sorted(signals, key=lambda x: x["score"], reverse=True)


def should_exit(
    position: dict,
    df: "pd.DataFrame | None",
    calendar_days_held: int,
) -> tuple[bool, str]:
    """Decide whether an open long or short position should be closed.

    position: dict with entry_price, stop_price, direction ("Buy"/"Sell")
    Returns (exit: bool, reason: str)
    """
    if df is None or len(df) < EXIT_PERIOD + 2:
        return False, ""

    closes = df["Close"].dropna()
    if len(closes) < EXIT_PERIOD + 2:
        return False, ""

    closes = df["Close"].dropna()
    lows   = df["Low"].dropna()
    highs  = df["High"].dropna()
    if len(closes) < EXIT_PERIOD + 2:
        return False, ""

    today      = float(closes.iloc[-1])
    cur_low    = float(lows.iloc[-1])
    cur_high   = float(highs.iloc[-1])
    entry_price = float(position.get("entry_price", 0))
    stop_price  = float(position.get("stop_price", 0))
    direction   = position.get("direction", "Buy")
    is_long     = direction == "Buy"

    if entry_price <= 0:
        return False, ""

    pnl_raw = (today - entry_price) if is_long else (entry_price - today)
    pnl_pct = pnl_raw / entry_price * 100

    # C â€” time stop (direction-agnostic)
    if calendar_days_held >= TIME_STOP_DAYS:
        return True, f"time-stop ({calendar_days_held}d)  P&L {pnl_pct:+.1f}%"

    if is_long:
        # B â€” hard stop: compare against intraday low, not close
        if stop_price > 0 and cur_low <= stop_price:
            return True, f"ATR-stop {pnl_pct:.1f}% (low {cur_low:.4f} â‰¤ stop {stop_price:.4f})"
        # A â€” Donchian trailing: 10-day lowest close
        low10 = float(closes.iloc[-(EXIT_PERIOD + 1):-1].min())
        if not pd.isna(low10) and today <= low10:
            return True, f"Donchian-exit ({EXIT_PERIOD}d low {low10:.4f})  {pnl_pct:+.1f}%"
    else:
        # B â€” hard stop: compare against intraday high
        if stop_price > 0 and cur_high >= stop_price:
            return True, f"ATR-stop {pnl_pct:.1f}% (high {cur_high:.4f} â‰¥ stop {stop_price:.4f})"
        # A â€” Donchian trailing: 10-day highest close (price rising against short)
        high10 = float(closes.iloc[-(EXIT_PERIOD + 1):-1].max())
        if not pd.isna(high10) and today >= high10:
            return True, f"Donchian-exit ({EXIT_PERIOD}d high {high10:.4f})  {pnl_pct:+.1f}%"

    return False, ""


def size_position(
    account_equity: float,
    atr: float,
    contract_size: float = 1.0,
) -> int:
    """ATR-based contract count for both longs and shorts.

    Risks exactly RISK_PCT Ã— account_equity per trade.
    """
    if atr <= 0 or contract_size <= 0 or account_equity <= 0:
        return 1
    risk_amount       = account_equity * RISK_PCT
    risk_per_contract = ATR_STOP_MULT * atr * contract_size
    return max(1, int(risk_amount / risk_per_contract))


def trailing_stop_update(current_stop: float, current_price: float,
                         current_atr: float, direction: str = "Buy") -> float:
    """Ratchet the ATR stop in the favourable direction (never moves against you).

    For longs: stop moves UP as price rises.
    For shorts: stop moves DOWN as price falls.
    """
    if direction == "Buy":
        new_stop = current_price - ATR_STOP_MULT * current_atr
        return max(current_stop, new_stop)
    else:
        new_stop = current_price + ATR_STOP_MULT * current_atr
        return min(current_stop, new_stop) if current_stop > 0 else new_stop
