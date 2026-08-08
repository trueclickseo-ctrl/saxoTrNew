"""
atos/omx_reversion.py
----------------------
OMX30 Mean Reversion — adapted from atos/us_reversion.py for Swedish stocks.

Swedish stocks tend to be more illiquid than US mega-caps and exhibit
different volatility profiles. Parameters are intentionally re-validated
against OMX30 history before enabling live trading (see omx30_backtest.py).

ENTRY (all four conditions simultaneously):
  1. Price > EMA200      — long-term uptrend, not a falling knife
  2. RSI(14) < RSI_ENTRY (33) — short-term oversold
  3. Price >= DIP_PCT (5%) below 20-day SMA — meaningful pullback
  4. Volume >= VOL_MULT (1.5x) × 20-day avg — capitulation / panic selling

EXIT (first condition hit):
  A. RSI(14) > RSI_EXIT (60) — recovery complete
  B. Price returns to 20-day SMA — mean reversion target achieved
  C. Hard stop: price drops STOP_PCT (4%) below entry — cut the loss
  D. Time stop: MAX_HOLD_DAYS (10) trading days — no dead positions

DIFFERENCES FROM US VERSION:
  - Tickers use .ST suffix; prices already in SEK (no FX)
  - Lower universe (30 stocks) → fewer simultaneous signals expected
  - Volume filter still required: Swedish stocks can have low-volume dips
    that are NOT capitulation — the vol spike is the key discriminator

This module is PURE (no I/O, no orders):
  - scan(feat_data, tickers) -> candidate list sorted by score
  - should_exit(trade, current_price, current_rsi, sma20, trading_days_held)
"""
import numpy as np
import pandas as pd
import atos.capital_config as CAP

# ── Entry / exit signal parameters ───────────────────────────────────────────
RSI_ENTRY  = 33     # oversold threshold
RSI_EXIT   = 60     # recovery threshold
DIP_PCT    = 0.05   # price must be >= 5% below 20-day SMA
VOL_MULT   = 1.5    # today's volume >= 1.5x 20-day average

# ── Risk parameters from capital.json ─────────────────────────────────────────
STOP_PCT      = CAP.reversion_stop_pct()
MAX_HOLD_DAYS = CAP.reversion_max_hold_days()
# ─────────────────────────────────────────────────────────────────────────────


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def scan(feat_data: dict, tickers: list) -> list:
    """Scan OMX30 universe for mean-reversion entry signals.

    Returns a list of candidate dicts sorted by score (strongest signal first):
      {ticker, price, rsi, sma20, ema200, dip_pct, vol_ratio, score}

    The caller decides how many slots to fill; this just returns all signals.
    """
    candidates = []

    for ticker in tickers:
        df = feat_data.get(ticker)
        if df is None or "Close" not in df or "Volume" not in df:
            continue

        close  = df["Close"].dropna()
        volume = df["Volume"].dropna()

        # Need at least 220 bars: EMA200 warmup + SMA20 + RSI14
        if len(close) < 220:
            continue

        price       = float(close.iloc[-1])
        ema200      = float(close.ewm(span=200, adjust=False).mean().iloc[-1])
        sma20       = float(close.rolling(20).mean().iloc[-1])
        rsi         = float(_rsi(close).iloc[-1])
        vol_20d_avg = float(volume.rolling(20).mean().iloc[-1])
        vol_today   = float(volume.iloc[-1])

        if pd.isna(ema200) or pd.isna(sma20) or pd.isna(rsi):
            continue
        if vol_20d_avg <= 0:
            continue

        above_ema200 = price > ema200
        oversold     = rsi < RSI_ENTRY
        dip_pct      = (sma20 - price) / sma20   # positive = price below SMA
        deep_dip     = dip_pct >= DIP_PCT
        vol_ratio    = vol_today / vol_20d_avg
        vol_spike    = vol_ratio >= VOL_MULT

        if not (above_ema200 and oversold and deep_dip and vol_spike):
            continue

        score = dip_pct * (RSI_ENTRY - rsi)
        candidates.append({
            "ticker":    ticker,
            "price":     round(price, 2),
            "rsi":       round(rsi, 1),
            "sma20":     round(sma20, 2),
            "ema200":    round(ema200, 2),
            "dip_pct":   round(dip_pct * 100, 1),
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
    """Decide whether an open OMX30 reversion position should be exited.

    Returns (exit: bool, reason: str).
    trade must have key: entry_price (float in SEK).
    """
    entry_price = float(trade.get("entry_price", 0))
    if entry_price <= 0:
        return False, ""

    # D — time stop
    if trading_days_held >= MAX_HOLD_DAYS:
        return True, f"time-stop: {trading_days_held}d held (max {MAX_HOLD_DAYS}d)"

    # C — hard stop-loss
    if current_price <= entry_price * (1 - STOP_PCT):
        loss_pct = (entry_price - current_price) / entry_price * 100
        return True, f"stop-loss: -{loss_pct:.1f}% (entry {entry_price:.2f} SEK)"

    # A — RSI recovery
    if current_rsi is not None and current_rsi > RSI_EXIT:
        return True, f"RSI recovery: {current_rsi:.0f} > {RSI_EXIT}"

    # B — price returned to 20-day SMA
    if sma20 is not None and current_price >= sma20:
        gain_pct = (current_price - entry_price) / entry_price * 100
        return True, f"SMA20 target hit: +{gain_pct:.1f}% (SMA {sma20:.2f} SEK)"

    return False, ""


def print_candidates(feat_data: dict, tickers: list) -> None:
    """Print today's OMX30 mean-reversion candidates (for backtesting / debug)."""
    hits = scan(feat_data, tickers)
    if not hits:
        print("No OMX30 mean-reversion signals today.")
        return
    print(f"\nOMX30 mean-reversion candidates ({len(hits)} found):")
    print(f"  {'Ticker':<12}  {'Price':>8}  {'RSI':>5}  {'Dip%':>6}  {'Vol×':>5}  Score")
    print("  " + "-" * 55)
    for h in hits:
        print(f"  {h['ticker']:<12}  {h['price']:>8.2f}  {h['rsi']:>5.1f}  "
              f"{h['dip_pct']:>5.1f}%  {h['vol_ratio']:>4.1f}x  {h['score']:.4f}")
