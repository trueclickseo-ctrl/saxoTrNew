"""
atos/omx_momentum.py
---------------------
OMX30 Weekly Blend — cross-sectional momentum strategy adapted for the
Stockholm exchange.

Mirrors the logic of atos/us_momentum.py but scoped to the 30-stock OMX30
universe. All prices are in SEK; no FX conversion is required.

STRATEGY LOGIC (identical to US Blend):
  Weekly rebalance, equal-weight, dynamic position count:
  - Offense: top up to MOM_N_MAX stocks ranked by (6m return / 60d vol),
    filtered to price > EMA200 AND 6m return > MOM_THRESHOLD (5%)
  - Defense: top LOWVOL_N stocks by lowest 60d vol among EMA200 stocks
  - Risk-off overlay: if equal-weight OMX30 index falls below its own 200d SMA,
    go to 0 positions (full cash)

PARAMETER NOTE:
  MOM_N_MAX and LOWVOL_N are inherited from config/capital.json (same as US).
  If you want different slot counts for OMX vs US, add an omx_blend section
  to capital.json and wire a getter in capital_config.py.

This module is PURE (no I/O, no orders):
  - compute_targets(feat_data, tickers) -> what to hold now
  - plan_rebalance(current_shares, targets, ...) -> buy/sell actions
"""
import numpy as np
import pandas as pd
import atos.capital_config as CAP

LOOKBACK       = 120    # 6-month signal window (same as US)
MOM_THRESHOLD  = 0.05   # 6-month return must exceed 5% to qualify for offense
REBAL_DAYS     = 7      # weekly rebalance

# Slot counts inherited from capital.json (override there, not here)
MOM_N_MAX = CAP.blend_offense_slots()
LOWVOL_N  = CAP.blend_defense_slots()


def _panel(feat_data: dict, tickers: list) -> pd.DataFrame | None:
    """Build a price panel from feat_data. Requires LOOKBACK + 2 bars minimum."""
    cols = {}
    for t in tickers:
        df = feat_data.get(t)
        if df is None or "Close" not in df or len(df) < LOOKBACK + 2:
            continue
        cols[t] = df["Close"]
    if len(cols) < 4:
        return None
    return pd.DataFrame(cols).ffill()


def market_risk_off(panel: pd.DataFrame) -> bool:
    """True when the equal-weight OMX30 index is below its 200-day SMA.

    Uses the same rule as the US Blend: equal-weight synthetic index of all
    stocks in the panel, normalized to 1.0 at the start of history.
    """
    idx = (panel / panel.iloc[0]).mean(axis=1)
    sma = idx.rolling(200).mean()
    if pd.isna(sma.iloc[-1]):
        return False
    return bool(idx.iloc[-1] < sma.iloc[-1])


def compute_targets(feat_data: dict, tickers: list) -> dict:
    """What the OMX30 Blend sleeve should hold right now.

    Returns:
        risk_off  (bool)   — True → go to cash
        targets   (list)   — tickers to hold, offense first
        momentum  (list)   — offense picks
        lowvol    (list)   — defense picks
        reason    (str)    — human-readable explanation
        detail    (dict)   — per-ticker {'mom_pct', 'vol_pct'}
    """
    panel = _panel(feat_data, tickers)
    if panel is None:
        return {"risk_off": False, "targets": [], "reason": "insufficient data"}

    if market_risk_off(panel):
        return {"risk_off": True, "targets": [], "reason": "OMX30 below 200d trend"}

    last   = panel.iloc[-1]
    mom    = panel.iloc[-1] / panel.iloc[-1 - LOOKBACK] - 1
    vol    = panel.pct_change().rolling(60).std().iloc[-1] * np.sqrt(252)
    ema200 = panel.ewm(span=200, adjust=False).mean().iloc[-1]

    # Stocks above EMA200 with valid vol
    above = [t for t in panel.columns
             if pd.notna(ema200[t]) and last[t] > ema200[t]
             and pd.notna(vol[t]) and vol[t] > 0]

    # Offense: must clear momentum threshold, ranked by return/vol ratio
    mom_elig = [t for t in above if pd.notna(mom[t]) and mom[t] >= MOM_THRESHOLD]
    momentum = sorted(mom_elig, key=lambda t: mom[t] / vol[t], reverse=True)[:MOM_N_MAX]

    # Defense: steadiest stocks above EMA200 regardless of momentum
    lowvol = sorted(above, key=lambda t: vol[t])[:LOWVOL_N]

    targets = list(dict.fromkeys(momentum + lowvol))   # deduped; offense first

    detail = {t: {"mom_pct": round(float(mom.get(t, 0)) * 100, 1),
                  "vol_pct": round(float(vol.get(t, 0)) * 100, 1)}
              for t in targets}

    reason = (f"{len(momentum)} offense (>{MOM_THRESHOLD*100:.0f}% momentum) + "
              f"{len(lowvol)} defense = {len(targets)} positions")

    return {"risk_off": False, "momentum": momentum, "lowvol": lowvol,
            "targets": targets, "reason": reason, "detail": detail}


def plan_rebalance(
    current_shares: dict,
    targets: list,
    sleeve_sek: float,
    prices_sek: dict,
) -> list:
    """Actions to move current OMX30 holdings to equal-weight target.

    current_shares : {ticker: shares held}
    targets        : tickers to hold (already ranked)
    sleeve_sek     : total SEK budget allocated to this sleeve
    prices_sek     : {ticker: last close price in SEK}

    Returns list of {'ticker', 'side', 'shares'} with shares > 0.
    """
    actions = []

    # Exit anything no longer in the target set
    for t, sh in current_shares.items():
        if t not in targets and sh > 0:
            actions.append({"ticker": t, "side": "Sell", "shares": int(sh)})

    if not targets or sleeve_sek <= 0:
        return actions

    per_sek = sleeve_sek / len(targets)
    for t in targets:
        price = prices_sek.get(t, 0)
        if not price or price <= 0:
            continue
        tgt = int(per_sek / price)
        cur = int(current_shares.get(t, 0))
        delta = tgt - cur
        if delta > 0:
            actions.append({"ticker": t, "side": "Buy", "shares": delta})
        elif delta < 0:
            actions.append({"ticker": t, "side": "Sell", "shares": -delta})

    return actions
