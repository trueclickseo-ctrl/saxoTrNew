"""
atos/us_reversion.py
---------------------
US Mean Reversion strategy — second independent ATOS strategy.

STATUS: DISABLED by default (US_REVERSION_ENABLED = False in atos_runner.py).
Enable only after the backtest in backtest_us_reversion.py shows positive
risk-adjusted returns on at least 2 years of out-of-sample data.

LOGIC:
  Strong stocks sometimes dip sharply due to sector rotation, macro noise, or
  profit-taking — not because the business changed. Mean reversion catches the
  bounce-back as buyers return.

ENTRY (all conditions must hold simultaneously):
  1. Price > EMA200 — stock is in a long-term uptrend (not a falling knife)
  2. RSI(14) < RSI_ENTRY (default 35) — short-term oversold
  3. Price is > DIP_PCT (default 5%) below its 20-day SMA — meaningful dip
  4. Volume today >= VOL_MULT (1.5x) × 20-day avg volume — capitulation signal

EXIT (first condition hit):
  A. RSI(14) > RSI_EXIT (default 60) — recovery complete
  B. Price rises back to 20-day SMA (mean-reversion target hit)
  C. Hard stop: price drops STOP_PCT (7%) below entry
  D. Max hold: MAX_HOLD_DAYS (10) trading days — time-stop, no dead positions

CAPITAL:
  Separate sleeve of REVERSION_SLEEVE_SEK. Never touches the US Blend sleeve.
  Max MAX_POSITIONS (3) concurrent positions; equal-weight within the sleeve.

CORRELATION WITH US BLEND:
  Momentum and mean-reversion are structurally anti-correlated:
  - Momentum wins in trending markets (stocks keep going up)
  - Reversion wins in choppy markets (stocks snap back after dips)
  Together they reduce portfolio drawdown and smooth the equity curve.

This module is PURE (no I/O, no orders):
  - scan(feat_data, us_tickers) -> candidate list (entry signals)
  - should_exit(trade, current_price, current_rsi, sma20) -> (exit: bool, reason: str)
The runner executes orders and manages state.
"""
import numpy as np
import pandas as pd

# ── Strategy parameters ───────────────────────────────────────────────────
RSI_ENTRY    = 35      # RSI below this → oversold, eligible for entry
RSI_EXIT     = 60      # RSI above this → recovery complete, exit
DIP_PCT      = 0.05    # Price must be >= 5% below 20-day SMA to qualify
VOL_MULT     = 1.5     # Today's volume >= 1.5x 20-day avg volume (capitulation)
STOP_PCT     = 0.07    # Hard stop-loss: 7% below entry price
MAX_HOLD_DAYS = 10     # Time-stop: exit after 10 trading days regardless
MAX_POSITIONS = 3      # Max concurrent reversion positions
REVERSION_SLEEVE_SEK = 300_000.0   # Separate capital pool (do not mix with US Blend)
# ─────────────────────────────────────────────────────────────────────────


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def scan(feat_data: dict, us_tickers: list) -> list:
    """Scan the universe for mean-reversion entry signals.

    Returns a ranked list of dicts (strongest dip first):
      {ticker, price, rsi, sma20, dip_pct, vol_ratio, score}

    Caller filters by how many open reversion positions already exist.
    """
    candidates = []
    for ticker in us_tickers:
        df = feat_data.get(ticker)
        if df is None or "Close" not in df or "Volume" not in df:
            continue
        close = df["Close"].dropna()
        volume = df["Volume"].dropna()
        if len(close) < 220:   # need EMA200 + 20d SMA + RSI(14)
            continue

        price  = float(close.iloc[-1])
        ema200 = float(close.ewm(span=200, adjust=False).mean().iloc[-1])
        sma20  = float(close.rolling(20).mean().iloc[-1])
        rsi    = float(_rsi(close).iloc[-1])
        vol_20d_avg = float(volume.rolling(20).mean().iloc[-1])
        vol_today   = float(volume.iloc[-1])

        if pd.isna(ema200) or pd.isna(sma20) or pd.isna(rsi):
            continue
        if vol_20d_avg <= 0:
            continue

        # All four entry conditions must hold
        above_ema200 = price > ema200
        oversold     = rsi < RSI_ENTRY
        dip_pct      = (sma20 - price) / sma20   # positive = price below SMA
        deep_dip     = dip_pct >= DIP_PCT
        vol_ratio    = vol_today / vol_20d_avg
        vol_spike    = vol_ratio >= VOL_MULT

        if not (above_ema200 and oversold and deep_dip and vol_spike):
            continue

        # Score: deeper dip + more oversold = stronger signal
        score = dip_pct * (RSI_ENTRY - rsi)
        candidates.append({
            "ticker":    ticker,
            "price":     round(price, 2),
            "rsi":       round(rsi, 1),
            "sma20":     round(sma20, 2),
            "dip_pct":   round(dip_pct * 100, 1),   # e.g. 6.3 (%)
            "vol_ratio": round(vol_ratio, 2),
            "score":     round(score, 4),
        })

    return sorted(candidates, key=lambda x: x["score"], reverse=True)


def should_exit(
    trade: dict,
    current_price: float,
    current_rsi: float | None,
    sma20: float | None,
    trading_days_held: int,
) -> tuple[bool, str]:
    """Decide whether an open reversion position should be exited now.

    Returns (exit: bool, reason: str).
    trade must have: entry_price (float).
    """
    entry_price = float(trade.get("entry_price", 0))
    if entry_price <= 0:
        return False, ""

    # D — time stop (check first — always respected)
    if trading_days_held >= MAX_HOLD_DAYS:
        return True, f"time-stop: {trading_days_held}d held (max {MAX_HOLD_DAYS}d)"

    # C — hard stop-loss
    if current_price <= entry_price * (1 - STOP_PCT):
        loss_pct = (entry_price - current_price) / entry_price * 100
        return True, f"stop-loss: -{loss_pct:.1f}% (entry ${entry_price:.2f})"

    # A — RSI recovery
    if current_rsi is not None and current_rsi > RSI_EXIT:
        return True, f"RSI recovery: {current_rsi:.0f} > {RSI_EXIT}"

    # B — price returned to 20-day SMA (mean reversion complete)
    if sma20 is not None and current_price >= sma20:
        gain_pct = (current_price - entry_price) / entry_price * 100
        return True, f"SMA20 target hit: +{gain_pct:.1f}% (SMA ${sma20:.2f})"

    return False, ""


def print_candidates(feat_data: dict, us_tickers: list) -> None:
    """Print today's entry candidates. Use in backtest_us_reversion.py."""
    hits = scan(feat_data, us_tickers)
    if not hits:
        print("No mean-reversion signals today.")
        return
    print(f"\nMean-reversion entry candidates ({len(hits)} found):")
    print(f"  {'Ticker':<8}  {'Price':>7}  {'RSI':>5}  {'Dip%':>6}  {'Vol×':>5}  Score")
    print("  " + "-" * 50)
    for h in hits:
        print(f"  {h['ticker']:<8}  ${h['price']:>6.2f}  {h['rsi']:>5.1f}  "
              f"{h['dip_pct']:>5.1f}%  {h['vol_ratio']:>4.1f}×  {h['score']:.4f}")
