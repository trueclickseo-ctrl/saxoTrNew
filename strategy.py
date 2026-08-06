"""
strategy.py
-----------
The trading logic itself. Deliberately simple and readable, because a
system you can't inspect is a system you can't trust with real money.

Rule set (long-only trend following):
  BUY  when the fast moving average crosses ABOVE the slow moving average
       (an "uptrend just started" signal)
  SELL when the fast moving average crosses BELOW the slow moving average
       OR price hits the ATR-based stop-loss, whichever comes first

This is intentionally not trying to catch every wiggle. It aims to catch
sustained trends and get out fast when a trend fails.
"""

import pandas as pd
import config


def commission(shares: float, price: float) -> float:
    """
    Realistic Saxo commission: max(rate % of trade value, minimum ticket
    fee). For small trades the $1 minimum dominates — e.g. a $50 trade at
    0.08% would be $0.04, but you actually pay $1, which is 2% of the
    trade value. This matters a LOT for smaller positions and for day
    trading where trade sizes tend to be smaller and more frequent.
    """
    trade_value = shares * price
    return max(trade_value * config.COMMISSION_RATE_PCT, config.MIN_COMMISSION_USD)


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Adds moving averages, ATR, and crossover signal columns to price data."""
    df = df.copy()

    df["fast_ma"] = df["Close"].rolling(config.FAST_MA).mean()
    df["slow_ma"] = df["Close"].rolling(config.SLOW_MA).mean()

    # True Range and ATR (volatility measure, used for stop-loss distance)
    prev_close = df["Close"].shift(1)
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - prev_close).abs(),
        (df["Low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["atr"] = tr.rolling(config.ATR_PERIOD).mean()

    # Signal: +1 when fast MA is above slow MA (uptrend), else 0
    df["trend_up"] = (df["fast_ma"] > df["slow_ma"]).astype(int)

    # A "cross" happens where trend_up changes value vs the previous day
    df["cross_up"] = (df["trend_up"] == 1) & (df["trend_up"].shift(1) == 0)
    df["cross_down"] = (df["trend_up"] == 0) & (df["trend_up"].shift(1) == 1)

    return df


def position_size(capital: float, entry_price: float, stop_price: float) -> int:
    """
    Risk-based position sizing: never risk more than RISK_PER_TRADE_PCT
    of capital on a single trade, based on the distance to the stop-loss.
    This is the core of NOT blowing up the account on a bad trade.
    """
    risk_amount = capital * config.RISK_PER_TRADE_PCT
    per_share_risk = entry_price - stop_price
    if per_share_risk <= 0:
        return 0
    shares = int(risk_amount / per_share_risk)
    return max(shares, 0)


def position_size_cfd(capital: float, entry_price: float, stop_price: float,
                       margin_rate: float = None) -> int:
    """
    Margin-aware position sizing for CFD indices. Unlike position_size()
    above, "how many contracts can I afford" is NOT the same question as
    "how many contracts fit my risk %" — a CFD position can pass the risk
    check and still be un-postable because required margin (a % of the
    FULL notional value, not just the risk amount) exceeds available
    capital. This function checks both and returns the smaller (safer)
    number of contracts.

    NOTE: margin_rate defaults to config.CFD_MARGIN_RATE, which is a
    placeholder estimate — see the warning comment in config.py. Verify
    the real per-instrument margin rate in SaxoTraderGO before trusting
    sizing decisions based on this.
    """
    if margin_rate is None:
        margin_rate = config.CFD_MARGIN_RATE

    # Same risk-based calc as the stock version
    risk_amount = capital * config.RISK_PER_TRADE_PCT
    per_unit_risk = entry_price - stop_price
    if per_unit_risk <= 0:
        return 0
    risk_based_contracts = int(risk_amount / per_unit_risk)

    # Margin check: how many contracts can this capital actually support,
    # independent of the risk %? Notional per contract = entry_price (Saxo
    # index CFDs are typically 1 unit = 1 index point in the quote currency).
    margin_per_contract = entry_price * margin_rate
    if margin_per_contract <= 0:
        return 0
    margin_based_contracts = int(capital / margin_per_contract)

    return max(min(risk_based_contracts, margin_based_contracts), 0)
