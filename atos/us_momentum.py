"""
atos/us_momentum.py
-------------------
US cross-sectional momentum — the validated strategy (see STRATEGY_NOTES.md).
Hold the top-N US names by 6-month momentum that are above their EMA200, monthly
rebalance, with a daily market risk-off overlay and volatility targeting.

10y backtest: Sharpe 1.30, MaxDD 21.3%, CAGR 24.4% (beats buy&hold Sharpe 1.27).

This module is PURE (no I/O, no orders) so it can be unit-tested:
  - compute_targets(feat_data, us_tickers) -> what to hold now
  - plan_rebalance(current, targets, ...) -> the buy/sell actions to get there
The runner calls these and executes the actions through the normal order path.
"""
import numpy as np
import pandas as pd

LOOKBACK      = 120     # ~6-month momentum signal window
# BLEND: two low-correlation sleeves (validated Sharpe 1.16, DD 14.3% — the safest).
MOM_N         = 2       # offense: top-2 by risk-adjusted momentum (return / vol)
LOWVOL_N      = 2       # defense: top-2 by lowest 60-day volatility
TARGET_VOL    = 0.15    # annualized vol target
REBAL_DAYS    = 7       # calendar days between rebalances (weekly — was 28/monthly)
                        # Selection from full US_TICKERS universe each week:
                        #   Step 1 — filter: price > EMA200 AND 6-month return > 0
                        #   Step 2 — rank offense: top-2 by (6m return / 60d vol)
                        #   Step 3 — rank defense: top-2 by lowest 60d vol
                        #   Step 4 — combine + deduplicate → max 4 positions
US_SLEEVE_SEK = 1_095_000.0   # PAPER TRADING: 100,000 EUR → SEK at ~10.95.
                               # Switch to a hard cap before going live with real money.


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


def compute_targets(feat_data: dict, us_tickers) -> dict:
    """What the US sleeve should hold right now.

    Returns dict with: risk_off (bool), targets (list of tickers, best first),
    scale (vol-target scalar 0..1), momentum (%), reason (str)."""
    panel = _panel(feat_data, us_tickers)
    if panel is None:
        return {"risk_off": False, "targets": [], "scale": 0.0, "reason": "insufficient data"}
    if market_risk_off(panel):
        return {"risk_off": True, "targets": [], "scale": 0.0, "reason": "US market below 200d trend"}

    last = panel.iloc[-1]
    mom = panel.iloc[-1] / panel.iloc[-1 - LOOKBACK] - 1
    vol = panel.pct_change().rolling(60).std().iloc[-1] * np.sqrt(252)
    ema200 = panel.ewm(span=200, adjust=False).mean().iloc[-1]
    above = [t for t in panel.columns
             if pd.notna(ema200[t]) and last[t] > ema200[t] and pd.notna(vol[t]) and vol[t] > 0]
    # Offense: top-N by risk-adjusted momentum (positive momentum only).
    mom_elig = [t for t in above if pd.notna(mom[t]) and mom[t] > 0]
    momentum = sorted(mom_elig, key=lambda t: mom[t] / vol[t], reverse=True)[:MOM_N]
    # Defense: top-N by lowest volatility.
    lowvol = sorted(above, key=lambda t: vol[t])[:LOWVOL_N]
    targets = list(dict.fromkeys(momentum + lowvol))   # deduped, for display/holdings
    return {"risk_off": False, "momentum": momentum, "lowvol": lowvol,
            "targets": targets, "reason": "ok",
            "detail": {t: {"mom_pct": round(float(mom[t]) * 100, 1),
                           "vol_pct": round(float(vol[t]) * 100, 1)} for t in targets}}


def plan_rebalance(current_shares: dict, targets: list, scale: float,
                   prices_usd: dict, sleeve_sek: float, fx_usd_sek: float) -> list:
    """Actions to move current US holdings -> equal-weight target (top-N * scale).

    current_shares: {ticker: shares held}. targets: tickers to hold.
    Returns list of {'ticker','side','shares'} (Buy/Sell), shares > 0."""
    actions = []
    # Exit anything no longer in the target set.
    for t, sh in current_shares.items():
        if t not in targets and sh > 0:
            actions.append({"ticker": t, "side": "Sell", "shares": int(sh)})
    if not targets or fx_usd_sek <= 0:
        return actions
    per_usd = (sleeve_sek * scale / len(targets)) / fx_usd_sek
    for t in targets:
        price = prices_usd.get(t, 0)
        if not price or price <= 0:
            continue
        tgt = int(per_usd / price)
        cur = int(current_shares.get(t, 0))
        delta = tgt - cur
        if delta > 0:
            actions.append({"ticker": t, "side": "Buy", "shares": delta})
        elif delta < 0:
            actions.append({"ticker": t, "side": "Sell", "shares": -delta})
    return actions
