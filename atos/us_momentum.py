"""
atos/us_momentum.py
-------------------
US cross-sectional momentum — the validated strategy (see STRATEGY_NOTES.md).
Holds the top momentum names from a 61-stock universe above their EMA200, with
a fortnightly rebalance and a daily market risk-off overlay.

10y backtest: Sharpe 1.30, MaxDD 21.3%, CAGR 24.4% (beats buy&hold Sharpe 1.27).

REBALANCE CADENCE (REBAL_DAYS = 14, changed from 7 on 2026-08-19)
  Swept 4d/7d/10d/15d/21d/30d/quarterly on the 10y panel (385 names, TOPN=8,
  daily regime + vol target). Full sample favoured ~15 calendar days:
      weekly   Sharpe 0.98  CAGR 21.4%  MaxDD 33.6%
      15 days  Sharpe 1.13  CAGR 25.9%  MaxDD 30.6%
      monthly  Sharpe 0.68  CAGR 14.1%  MaxDD 48.8%
  A single-sample peak is usually curve-fit, so it was re-scored by mean rank
  over 8 independent tests (3 sub-periods x 5 TOPN settings):
      15d 2.31 | weekly 2.56 | 21d 2.56 | monthly 5.31 | quarterly 6.31
  The top three are a statistical tie (0.25 rank spread = noise). The tiebreak
  is TRADING COST — weekly decays much faster as costs rise:
      cost/side     0.11%   0.20%   0.35%   0.50%
      weekly        0.98    0.84    0.62    0.39
      15 days       1.13    1.04    0.90    0.75
  At ~37,500 SEK per slot Saxo's real cost lands near 0.15-0.25%/side, squarely
  where weekly starts bleeding. 14 (not 15) is used so rebalances land on the
  same weekday each time instead of drifting through the week.
  Monthly and slower ranked 5th-7th in EVERY test — do not go there.

POSITION COUNT IS DYNAMIC — not fixed at 4:
  Strong bull market (many stocks qualify):   up to MOM_N_MAX + LOWVOL_N positions
  Normal market:                              typically 4–6 positions
  Weak / narrow momentum:                     2–3 positions
  Risk-off (index below 200d SMA):            0 positions → full cash

Selection from 61-stock universe each rebalance:
  Step 1 — filter: price > EMA200 AND 6-month return > MOM_THRESHOLD (5%)
  Step 2 — offense: all qualifying stocks ranked by (6m return / 60d vol),
            take top MOM_N_MAX (up to 6)
  Step 3 — defense: top LOWVOL_N (2) by lowest 60d vol from all EMA200 stocks
  Step 4 — combine + deduplicate → 2 to 8 positions, equal-weighted

This module is PURE (no I/O, no orders) so it can be unit-tested:
  - compute_targets(feat_data, us_tickers) -> what to hold now
  - plan_rebalance(current, targets, ...) -> the buy/sell actions to get there
The runner calls these and executes the actions through the normal order path.
"""
import numpy as np
import pandas as pd
import atos.capital_config as CAP

LOOKBACK       = 120    # ~6-month momentum signal window
MOM_THRESHOLD  = 0.05   # minimum 6-month return to qualify for offense (5%)
TARGET_VOL     = 0.15   # annualized vol target
REBAL_DAYS     = 14     # calendar days between rebalances (fortnightly — see note below)
US_SLEEVE_SEK  = 1_095_000.0   # fallback fixed sleeve (overridden by dynamic cash %)

# Position slots — loaded from config/capital.json
MOM_N_MAX = CAP.blend_offense_slots()   # max offense positions
LOWVOL_N  = CAP.blend_defense_slots()  # defense positions


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

    Position count is DYNAMIC — more positions when more stocks have strong
    upward momentum, fewer when momentum is narrow or weak.

    Returns dict with: risk_off (bool), targets (list of tickers, best first),
    momentum (list), lowvol (list), reason (str), detail (per-ticker stats)."""
    panel = _panel(feat_data, us_tickers)
    if panel is None:
        return {"risk_off": False, "targets": [], "scale": 0.0, "reason": "insufficient data"}
    if market_risk_off(panel):
        return {"risk_off": True, "targets": [], "scale": 0.0, "reason": "US market below 200d trend"}

    last   = panel.iloc[-1]
    mom    = panel.iloc[-1] / panel.iloc[-1 - LOOKBACK] - 1
    vol    = panel.pct_change().rolling(60).std().iloc[-1] * np.sqrt(252)
    ema200 = panel.ewm(span=200, adjust=False).mean().iloc[-1]

    # Stocks above their 200-day EMA with valid volatility — basic health check
    above = [t for t in panel.columns
             if pd.notna(ema200[t]) and last[t] > ema200[t]
             and pd.notna(vol[t]) and vol[t] > 0]

    # OFFENSE — must clear the momentum threshold (not just > 0, but genuinely rising)
    # Dynamic count: all qualifying stocks up to MOM_N_MAX, ranked by return/vol
    mom_elig = [t for t in above
                if pd.notna(mom[t]) and mom[t] >= MOM_THRESHOLD]
    momentum = sorted(mom_elig, key=lambda t: mom[t] / vol[t], reverse=True)[:MOM_N_MAX]

    # DEFENSE — always top-LOWVOL_N steadiest stocks above EMA200 (regardless of momentum)
    lowvol = sorted(above, key=lambda t: vol[t])[:LOWVOL_N]

    targets = list(dict.fromkeys(momentum + lowvol))   # deduped; offense listed first

    all_detail = {t: {"mom_pct": round(float(mom.get(t, 0)) * 100, 1),
                      "vol_pct": round(float(vol.get(t, 0)) * 100, 1)}
                  for t in targets}

    reason = (f"{len(momentum)} offense (>{MOM_THRESHOLD*100:.0f}% momentum) + "
              f"{len(lowvol)} defense = {len(targets)} positions")

    return {"risk_off": False, "momentum": momentum, "lowvol": lowvol,
            "targets": targets, "reason": reason, "detail": all_detail}


def plan_rebalance(current_shares: dict, targets: list, scale: float,
                   prices_usd: dict, sleeve_sek: float, fx_usd_sek: float) -> list:
    """Actions to move current US holdings -> equal-weight target (top-N * scale).

    current_shares: {ticker: shares held}. targets: tickers to hold.
    Returns list of {'ticker','side','shares'} (Buy/Sell), shares > 0.

    Idempotency: positions already within REBAL_THRESHOLD of their target size
    are left alone, preventing unnecessary sell-then-rebuy round-trips when the
    runner is executed more than once with identical targets.
    """
    REBAL_THRESHOLD = 0.10   # skip adjustment if position is within 10% of target
    # Cap per-name share count — added 2026-08-22 at user's request, after
    # some positions (e.g. AES, U) grew past 100-300+ shares on cheaper
    # tickers, tying up disproportionate margin/collateral for their dollar
    # size (a low-priced stock needs far more SHARES to hit the same dollar
    # budget as a high-priced one). Capping keeps per-name capital committed
    # small and leaves more margin free to test the forex module across more
    # pairs, which is the current priority — not a strategy change, a sizing
    # ceiling on top of the existing dollar-budget calc below.
    MAX_SHARES_PER_NAME = 50

    actions = []
    # Exit anything no longer in the target set (full close, no threshold).
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
        tgt = min(int(per_usd / price), MAX_SHARES_PER_NAME)
        cur = int(current_shares.get(t, 0))
        delta = tgt - cur
        # Skip trivial size drift — only trade when the position is meaningfully off.
        if tgt > 0 and abs(delta) / tgt < REBAL_THRESHOLD:
            continue
        if delta > 0:
            actions.append({"ticker": t, "side": "Buy", "shares": delta})
        elif delta < 0:
            actions.append({"ticker": t, "side": "Sell", "shares": -delta})
    return actions
