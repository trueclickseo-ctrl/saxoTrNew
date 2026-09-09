"""
atos/us_blend_v2.py
-------------------
US Blend V2 — a SIM-only experimental variant of us_momentum.py.

Two targeted improvements over the original strategy (which is unchanged):

  1. SKIP-MONTH MOMENTUM (12-1 style)
     Classic cross-sectional momentum research (Jegadeesh & Titman) shows
     the most-recent ~21 trading days exhibit short-term reversal — stocks
     that ran hard last month tend to give back near-term. V1 measures
     6m return through today; V2 measures from 6 months ago to 1 month ago,
     skipping the last SKIP_DAYS trading days. This removes that reversal
     drag from the ranking signal.

  2. VOLATILITY TARGETING
     V1 always deploys at full sleeve size regardless of current market
     turbulence. V2 scales position sizes down when realized portfolio
     volatility (measured over the last 20 bars) exceeds TARGET_VOL.
     This was validated in backtest_us_momentum.py (the +REGIME+VT variant)
     but was never wired into the live module. Scale = min(1, TARGET_VOL /
     realized_vol), applied to the sleeve budget before per-name sizing.
     Caller must supply the portfolio's recent daily returns via
     recent_port_rets; pass an empty list to disable (scale = 1.0).

All other logic is identical to us_momentum.py:
  - EMA200 above-trend filter
  - 6-month momentum threshold (MOM_THRESHOLD = 5%)
  - Offense ranked by return/vol (Sharpe-like ratio)
  - Defense = top-LOWVOL_N lowest-vol names above EMA200
  - 14-day rebalance cadence
  - Equal-weight sizing within the scaled sleeve budget
  - MAX_SHARES_PER_NAME and MIN_ENTRY_SLOT_MULT guards

This module is PURE (no I/O, no orders). SIM-only — do not use in LIVE
until it has a meaningful trade track record.
"""
import numpy as np
import pandas as pd
import atos.capital_config as CAP

# ── Parameters ────────────────────────────────────────────────────────────────
LOOKBACK       = 120    # ~6-month window start (same as V1)
SKIP_DAYS      = 21     # skip most-recent ~1 month to avoid reversal drag
MOM_THRESHOLD  = 0.05   # 5% minimum momentum over the measured window
TARGET_VOL     = 0.15   # annualized vol target for the scaling overlay
VOL_WINDOW     = 20     # bars of portfolio returns used to estimate realized vol
REBAL_DAYS     = 14     # fortnightly rebalance (same as V1)
US_SLEEVE_SEK  = 1_095_000.0  # fallback fixed sleeve (overridden by dynamic cash %)

# Position slots — same source as V1
MOM_N_MAX = CAP.blend_offense_slots()   # max offense positions
LOWVOL_N  = CAP.blend_defense_slots()  # defense positions

STRATEGY_NAME = "US Blend V2"


def _panel(feat_data: dict, us_tickers) -> pd.DataFrame | None:
    cols = {}
    for t in us_tickers:
        df = feat_data.get(t)
        if df is None or "Close" not in df or len(df) < LOOKBACK + 2:
            continue
        cols[t] = df["Close"]
    if len(cols) < 4:
        return None
    return pd.DataFrame(cols).ffill()


def market_risk_off(panel: pd.DataFrame) -> bool:
    """True when the equal-weight US index is below its 200-day SMA (risk-off)."""
    idx = (panel / panel.iloc[0]).mean(axis=1)
    sma = idx.rolling(200).mean()
    if pd.isna(sma.iloc[-1]):
        return False
    return bool(idx.iloc[-1] < sma.iloc[-1])


def vol_scale(recent_port_rets: list) -> float:
    """Volatility-targeting scale factor.

    Returns a value in (0, 1] that shrinks exposure when realized portfolio
    vol exceeds TARGET_VOL. Pass an empty list to get 1.0 (no scaling).
    """
    if len(recent_port_rets) < VOL_WINDOW:
        return 1.0
    rv = float(np.std(recent_port_rets[-VOL_WINDOW:])) * np.sqrt(252)
    if rv <= 0:
        return 1.0
    return min(1.0, TARGET_VOL / rv)


def compute_targets(feat_data: dict, us_tickers) -> dict:
    """What the US Blend V2 sleeve should hold right now.

    Differences vs compute_targets() in us_momentum.py:
      - Momentum is measured from [LOOKBACK] days ago to [SKIP_DAYS] days ago,
        skipping the most-recent month (skip-month / 12-1 style).
      - Returns a 'scale' key that the runner should pass to plan_rebalance()
        after computing vol_scale() from recent portfolio returns. This module
        does not know the portfolio's return history, so scale is left at 1.0
        here as a safe default; the caller should override it.

    Returns dict with: risk_off (bool), targets (list), momentum (list),
    lowvol (list), reason (str), detail (per-ticker stats), scale (float).
    """
    panel = _panel(feat_data, us_tickers)
    if panel is None:
        return {"risk_off": False, "targets": [], "scale": 1.0, "reason": "insufficient data"}
    if market_risk_off(panel):
        return {"risk_off": True, "targets": [], "scale": 0.0, "reason": "US market below 200d trend"}

    last   = panel.iloc[-1]
    ema200 = panel.ewm(span=200, adjust=False).mean().iloc[-1]
    vol    = panel.pct_change().rolling(60).std().iloc[-1] * np.sqrt(252)

    # Skip-month momentum: measure from LOOKBACK days ago to SKIP_DAYS days ago.
    # Requires at least LOOKBACK+2 bars (guaranteed by _panel check above).
    skip_idx = min(SKIP_DAYS, len(panel) - 2)
    mom = panel.iloc[-1 - skip_idx] / panel.iloc[-1 - LOOKBACK] - 1

    # Basic health check: above EMA200, vol computable
    above = [t for t in panel.columns
             if pd.notna(ema200[t]) and last[t] > ema200[t]
             and pd.notna(vol[t]) and vol[t] > 0]

    # OFFENSE — ranked by (skip-month return / 60d vol), must clear MOM_THRESHOLD
    mom_elig = [t for t in above if pd.notna(mom[t]) and mom[t] >= MOM_THRESHOLD]
    momentum = sorted(mom_elig, key=lambda t: mom[t] / vol[t], reverse=True)[:MOM_N_MAX]

    # DEFENSE — same as V1: lowest vol above EMA200
    lowvol = sorted(above, key=lambda t: vol[t])[:LOWVOL_N]

    targets = list(dict.fromkeys(momentum + lowvol))

    all_detail = {t: {"mom_pct": round(float(mom.get(t, 0)) * 100, 1),
                      "vol_pct": round(float(vol.get(t, 0)) * 100, 1)}
                  for t in targets}

    reason = (f"V2 {len(momentum)} offense (>{MOM_THRESHOLD*100:.0f}% skip-month mom) + "
              f"{len(lowvol)} defense = {len(targets)} positions")

    return {"risk_off": False, "momentum": momentum, "lowvol": lowvol,
            "targets": targets, "scale": 1.0, "reason": reason, "detail": all_detail}


def plan_rebalance(current_shares: dict, targets: list, scale: float,
                   prices_usd: dict, sleeve_sek: float, fx_usd_sek: float) -> list:
    """Actions to move current US Blend V2 holdings -> equal-weight target.

    Identical to us_momentum.plan_rebalance() except that `scale` now carries
    the vol-targeting factor from vol_scale(). A scale < 1.0 reduces the
    sleeve budget, so all positions shrink proportionally and the remaining
    cash sits idle — exactly the behaviour validated in backtest_us_momentum.py.

    Idempotency: positions within REBAL_THRESHOLD of target are left alone.
    """
    REBAL_THRESHOLD  = 0.10
    MAX_SHARES_PER_NAME = 50
    MIN_ENTRY_SLOT_MULT = 1.5

    actions = []
    for t, sh in current_shares.items():
        if t not in targets and sh > 0:
            actions.append({"ticker": t, "side": "Sell", "shares": int(sh)})
    if not targets or fx_usd_sek <= 0:
        return actions

    # scale shrinks the effective sleeve when realized vol > TARGET_VOL
    effective_sleeve = sleeve_sek * scale
    per_usd = (effective_sleeve / len(targets)) / fx_usd_sek

    for t in targets:
        price = prices_usd.get(t, 0)
        if not price or price <= 0:
            continue
        tgt = min(int(per_usd / price), MAX_SHARES_PER_NAME)
        cur = int(current_shares.get(t, 0))
        if tgt == 0 and cur == 0 and price <= per_usd * MIN_ENTRY_SLOT_MULT:
            tgt = 1
        delta = tgt - cur
        if tgt > 0 and abs(delta) / tgt < REBAL_THRESHOLD:
            continue
        if delta > 0:
            actions.append({"ticker": t, "side": "Buy", "shares": delta})
        elif delta < 0:
            actions.append({"ticker": t, "side": "Sell", "shares": -delta})
    return actions
