"""
atos/us_reversion_v2.py
------------------------
US Mean Reversion v2 — enhanced variant of us_reversion.py.

The original strategy (us_reversion.py) runs unchanged on SIM.
This module is a SIM-only twin for A/B evaluation.

IMPROVEMENTS OVER V1:
  1. SPY regime filter   — entries blocked when SPY < SPY_EMA50 (no buying dips
                           in bear markets; market_ok flag passed by caller)
  2. EMA200 margin       — price must be >= EMA200 × EMA200_MARGIN (1.02) instead
                           of just > EMA200; avoids false signals near the boundary
  3. Minimum R:R filter  — skip entries where SMA20 target < MIN_RR × stop distance;
                           ensures each trade has a reward/risk >= 1.5 by default
  4. Volume in score     — vol_ratio contributes to ranking (capped at 2× so noisy
                           spikes don't dominate); v1 uses volume only as a gate

Exit logic, capital config, and slot math are IDENTICAL to v1.

LOGIC:
  ENTRY (all conditions must hold):
    1. market_ok (SPY > SPY_EMA50)          — regime filter (NEW)
    2. Price > EMA200 × EMA200_MARGIN       — uptrend with buffer (tightened)
    3. RSI(14) < RSI_ENTRY (38)             — oversold
    4. Price >= DIP_PCT (5%) below SMA20    — meaningful dip
    5. Volume >= VOL_MULT (1.5×) 20d avg   — capitulation
    6. (SMA20 − price)/price >= MIN_RR × STOP_PCT — R:R gate (NEW)

  EXIT (first condition hit):
    A. RSI(14) > RSI_EXIT (60)              — recovery complete
    B. Price >= SMA20                       — mean-reversion target
    C. price <= entry × (1 − STOP_PCT)     — hard stop-loss
    D. trading_days_held >= MAX_HOLD_DAYS   — time-stop

This module is PURE (no I/O, no orders):
  scan(feat_data, us_tickers, market_ok) -> candidate list
  should_exit(trade, price, rsi, sma20, days_held) -> (bool, reason)
"""
import numpy as np
import pandas as pd
import atos.capital_config as CAP

# ── Parameters ────────────────────────────────────────────────────────────────
RSI_ENTRY     = 38      # RSI below this → oversold
RSI_EXIT      = 60      # RSI above this → recovery, exit
DIP_PCT       = 0.05    # price must be >= 5% below SMA20
VOL_MULT      = 1.5     # volume must be >= 1.5× 20d average
EMA200_MARGIN = 1.02    # price must be >= 2% above EMA200  [v2]
MIN_RR        = 1.5     # SMA20 target must be >= 1.5× the stop distance  [v2]

STOP_PCT             = CAP.reversion_stop_pct()
MAX_HOLD_DAYS        = CAP.reversion_max_hold_days()
MAX_UNIVERSE_PCT     = CAP.reversion_max_universe_pct()
SLEEVE_DD_CAP        = CAP.reversion_sleeve_dd_cap()
REVERSION_SLEEVE_SEK = CAP.reversion_fallback_sleeve_sek()
MAX_POSITIONS        = CAP.reversion_max_slots()
# ─────────────────────────────────────────────────────────────────────────────


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def scan(feat_data: dict, us_tickers: list, market_ok: bool = True) -> list:
    """Scan universe for v2 mean-reversion entry signals.

    market_ok: caller passes False when SPY < SPY_EMA50 (bear regime).
               Returns empty list immediately — no new entries in downtrend.

    Returns ranked list (highest score first):
      {ticker, price, rsi, sma20, dip_pct, vol_ratio, rr_ratio, score}
    """
    if not market_ok:
        return []

    candidates = []
    for ticker in us_tickers:
        df = feat_data.get(ticker)
        if df is None or "Close" not in df or "Volume" not in df:
            continue
        close  = df["Close"].dropna()
        volume = df["Volume"].dropna()
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

        above_ema200 = price > ema200 * EMA200_MARGIN   # tightened vs v1
        oversold     = rsi < RSI_ENTRY
        dip_pct      = (sma20 - price) / sma20
        deep_dip     = dip_pct >= DIP_PCT
        vol_ratio    = vol_today / vol_20d_avg
        vol_spike    = vol_ratio >= VOL_MULT

        if not (above_ema200 and oversold and deep_dip and vol_spike):
            continue

        # R:R gate: SMA20 must be far enough away to justify the stop risk
        target_dist = (sma20 - price) / price
        rr_ratio    = target_dist / STOP_PCT
        if rr_ratio < MIN_RR:
            continue

        # Score: dip depth × oversold degree × volume conviction (capped at 2×)
        vol_bonus = min(vol_ratio / VOL_MULT, 2.0)
        score     = dip_pct * (RSI_ENTRY - rsi) * vol_bonus

        candidates.append({
            "ticker":    ticker,
            "price":     round(price, 2),
            "rsi":       round(rsi, 1),
            "sma20":     round(sma20, 2),
            "dip_pct":   round(dip_pct * 100, 1),
            "vol_ratio": round(vol_ratio, 2),
            "rr_ratio":  round(rr_ratio, 2),
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
    """Exit logic — identical to us_reversion.py v1."""
    entry_price = float(trade.get("entry_price", 0))
    if entry_price <= 0:
        return False, ""

    if trading_days_held >= MAX_HOLD_DAYS:
        return True, f"time-stop: {trading_days_held}d held (max {MAX_HOLD_DAYS}d)"

    if current_price <= entry_price * (1 - STOP_PCT):
        loss_pct = (entry_price - current_price) / entry_price * 100
        return True, f"stop-loss: -{loss_pct:.1f}% (entry ${entry_price:.2f})"

    if current_rsi is not None and current_rsi > RSI_EXIT:
        return True, f"RSI recovery: {current_rsi:.0f} > {RSI_EXIT}"

    if sma20 is not None and current_price >= sma20:
        gain_pct = (current_price - entry_price) / entry_price * 100
        return True, f"SMA20 target hit: +{gain_pct:.1f}% (SMA ${sma20:.2f})"

    return False, ""


def print_candidates(
    feat_data: dict, us_tickers: list, market_ok: bool = True
) -> None:
    """Print today's v2 signals. Use in preview_us_reversion_v2.py."""
    if not market_ok:
        print("No entries: SPY is below its 50-day EMA (bear regime filter active).")
        return
    hits = scan(feat_data, us_tickers, market_ok=market_ok)
    if not hits:
        print("No mean-reversion v2 signals today.")
        return
    print(f"\nMean-reversion v2 candidates ({len(hits)} found):")
    print(f"  {'Ticker':<8}  {'Price':>7}  {'RSI':>5}  {'Dip%':>6}  {'Vol×':>5}  {'R:R':>5}  Score")
    print("  " + "-" * 62)
    for h in hits:
        print(
            f"  {h['ticker']:<8}  ${h['price']:>6.2f}  {h['rsi']:>5.1f}  "
            f"{h['dip_pct']:>5.1f}%  {h['vol_ratio']:>4.1f}x  "
            f"{h['rr_ratio']:>4.1f}  {h['score']:.4f}"
        )
