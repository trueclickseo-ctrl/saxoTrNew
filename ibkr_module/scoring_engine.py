"""
ibkr_module/scoring_engine.py
------------------------------
ATOS US 500 Scoring Engine.

Pipeline:
  500 stocks → hard gates → Swing Score → Quality Score → Trade Score
  → top candidates → IBKR entry strategy → risk engine → order

Swing Score (100 pts):  Momentum 25 | Volatility 20 | Liquidity 20
                        Trend 15 | Rel-Strength vs SPY 10 | Catalyst 10

Quality Score (100 pts): Revenue growth 15 | EPS growth 15 | Op margin 15
                         FCF yield 15 | ROIC 15 | Debt 15 | Dividend 5 | Moat 5

Trade Score = 70% Swing + 30% Quality

Grades: A+ ≥ 85 | A ≥ 75 | B ≥ 65 | C ≥ 55 | PASS < 55

Universe types get differentiated top-N selection:
  Swing / Momentum  → ranked by swing_score (faster signals)
  Hybrid / Portfolio → ranked by trade_score (balanced)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class ScoringConfig:
    # Hard gates
    min_price: float = 5.0          # lowered from 10 to catch small-caps in list
    min_avg_volume: float = 500_000  # lowered from 1M; many solid mid-caps sit here
    min_dollar_volume: float = 10_000_000  # $10M daily dollar volume
    min_market_cap: float = 1_000_000_000  # $1B — captures mid-caps in universe

    # Swing weights (sum = 100)
    w_momentum: float = 25
    w_volatility: float = 20
    w_liquidity: float = 20
    w_trend: float = 15
    w_relative_strength: float = 10
    w_catalyst: float = 10

    # Quality weights (sum = 100)
    w_revenue_growth: float = 15
    w_eps_growth: float = 15
    w_margin: float = 15
    w_fcf: float = 15
    w_roic: float = 15
    w_debt: float = 15
    w_dividend: float = 5
    w_quality_moat: float = 5

    swing_weight: float = 0.70
    quality_weight: float = 0.30

    # ATR sweet-spot for swing trading (2–8% is ideal)
    atr_ideal_pct: float = 4.0
    atr_half_range: float = 4.0


def _pct_score(s: pd.Series, low: float = 0.05, high: float = 0.95) -> pd.Series:
    """Cross-sectional percentile score 0-100 clipped at low/high quantiles."""
    x = pd.to_numeric(s, errors="coerce")
    lo, hi = x.quantile(low), x.quantile(high)
    if pd.isna(lo) or pd.isna(hi) or hi <= lo:
        return pd.Series(50.0, index=s.index)
    return ((x - lo) / (hi - lo)).clip(0, 1) * 100


def calculate_scores(
    df: pd.DataFrame,
    cfg: Optional[ScoringConfig] = None,
) -> pd.DataFrame:
    """Score every row in df and return a sorted DataFrame.

    Required columns: ticker, price, avg_volume_20d, avg_dollar_volume_20d,
      market_cap, atr_pct, roc_20d, roc_60d, adx_14, sma50_distance_pct,
      sma200_distance_pct, rs_vs_spy_20d

    Optional columns (fall back to neutral defaults if absent):
      catalyst_score, revenue_growth, eps_growth, operating_margin,
      fcf_yield, roic, debt_to_equity, dividend_yield, moat_score, earnings_days
    """
    cfg = cfg or ScoringConfig()
    x = df.copy()
    x.columns = [c.lower().strip() for c in x.columns]

    required = [
        "ticker", "price", "avg_volume_20d", "avg_dollar_volume_20d", "market_cap",
        "atr_pct", "roc_20d", "roc_60d", "adx_14", "sma50_distance_pct",
        "sma200_distance_pct", "rs_vs_spy_20d",
    ]
    missing = [c for c in required if c not in x.columns]
    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    defaults = {
        "catalyst_score":    50,
        "revenue_growth":     0,
        "eps_growth":         0,
        "operating_margin":   0,
        "fcf_yield":          0,
        "roic":               0,
        "debt_to_equity":     np.nan,
        "dividend_yield":     0,
        "moat_score":        50,
        "earnings_days":      np.nan,
    }
    for col, val in defaults.items():
        if col not in x.columns:
            x[col] = val

    # ── Hard gates ────────────────────────────────────────────────────────────
    x["gate_price"]        = pd.to_numeric(x.price, errors="coerce") >= cfg.min_price
    x["gate_volume"]       = pd.to_numeric(x.avg_volume_20d, errors="coerce") >= cfg.min_avg_volume
    x["gate_dollar_vol"]   = pd.to_numeric(x.avg_dollar_volume_20d, errors="coerce") >= cfg.min_dollar_volume
    x["gate_market_cap"]   = pd.to_numeric(x.market_cap, errors="coerce") >= cfg.min_market_cap
    # Avoid earnings ±2 days (gap risk)
    ed = pd.to_numeric(x.earnings_days, errors="coerce")
    x["gate_earnings"]     = ed.isna() | (ed.abs() > 2)
    x["hard_gate"] = x[["gate_price", "gate_volume", "gate_dollar_vol",
                          "gate_market_cap", "gate_earnings"]].all(axis=1)

    # ── Swing sub-scores ──────────────────────────────────────────────────────

    # Momentum: 60% 20d ROC + 40% 60d ROC (higher = stronger near-term trend)
    momentum = (
        0.60 * _pct_score(x.roc_20d) +
        0.40 * _pct_score(x.roc_60d)
    )

    # Volatility: ideal swing ATR is cfg.atr_ideal_pct; penalise both tails
    atr = pd.to_numeric(x.atr_pct, errors="coerce")
    atr_dev = (atr - cfg.atr_ideal_pct).abs() / cfg.atr_half_range
    volatility = (100 - atr_dev * 100).clip(0, 100).fillna(50)

    # Liquidity: log-scaled dollar volume percentile
    liquidity = _pct_score(np.log1p(pd.to_numeric(x.avg_dollar_volume_20d, errors="coerce")))

    # Trend: above SMA50, above SMA200, ADX strength
    trend50  = ((pd.to_numeric(x.sma50_distance_pct,  errors="coerce") + 10) / 20 * 100).clip(0, 100)
    trend200 = ((pd.to_numeric(x.sma200_distance_pct, errors="coerce") + 10) / 30 * 100).clip(0, 100)
    adx      = ((pd.to_numeric(x.adx_14, errors="coerce") - 15) / 25 * 100).clip(0, 100)
    trend    = 0.35 * trend50 + 0.40 * trend200 + 0.25 * adx

    # Relative strength vs SPY (cross-sectional rank)
    relative_strength = _pct_score(pd.to_numeric(x.rs_vs_spy_20d, errors="coerce"))

    # Catalyst (external 0-100 score; defaults to neutral 50)
    catalyst = pd.to_numeric(x.catalyst_score, errors="coerce").clip(0, 100).fillna(50)

    x["momentum_score"]          = momentum
    x["volatility_score"]        = volatility
    x["liquidity_score"]         = liquidity
    x["trend_score"]             = trend
    x["relative_strength_score"] = relative_strength
    x["catalyst_score_final"]    = catalyst

    x["swing_score"] = (
        (cfg.w_momentum         / 100) * momentum          +
        (cfg.w_volatility       / 100) * volatility        +
        (cfg.w_liquidity        / 100) * liquidity         +
        (cfg.w_trend            / 100) * trend             +
        (cfg.w_relative_strength/ 100) * relative_strength +
        (cfg.w_catalyst         / 100) * catalyst
    )

    # ── Quality sub-scores ────────────────────────────────────────────────────
    revenue = _pct_score(pd.to_numeric(x.revenue_growth,    errors="coerce"))
    eps     = _pct_score(pd.to_numeric(x.eps_growth,        errors="coerce"))
    margin  = _pct_score(pd.to_numeric(x.operating_margin,  errors="coerce"))
    fcf     = _pct_score(pd.to_numeric(x.fcf_yield,         errors="coerce"))
    roic    = _pct_score(pd.to_numeric(x.roic,              errors="coerce"))

    de = pd.to_numeric(x.debt_to_equity, errors="coerce")
    de = de.fillna(de.median() if de.notna().any() else 0.0)
    debt_score = _pct_score(-de)   # lower D/E = better

    dividend = _pct_score(pd.to_numeric(x.dividend_yield, errors="coerce"))
    moat     = pd.to_numeric(x.moat_score, errors="coerce").clip(0, 100).fillna(50)

    x["quality_score"] = (
        (cfg.w_revenue_growth / 100) * revenue    +
        (cfg.w_eps_growth     / 100) * eps        +
        (cfg.w_margin         / 100) * margin     +
        (cfg.w_fcf            / 100) * fcf        +
        (cfg.w_roic           / 100) * roic       +
        (cfg.w_debt           / 100) * debt_score +
        (cfg.w_dividend       / 100) * dividend   +
        (cfg.w_quality_moat   / 100) * moat
    )

    # ── Trade Score ───────────────────────────────────────────────────────────
    x["trade_score_raw"] = cfg.swing_weight * x.swing_score + cfg.quality_weight * x.quality_score
    x["trade_score"]     = np.where(x.hard_gate, x.trade_score_raw, 0.0)

    x["rank"] = x.trade_score.rank(method="min", ascending=False).astype("Int64")
    x["setup"] = np.select(
        [x.trade_score >= 85, x.trade_score >= 75,
         x.trade_score >= 65, x.trade_score >= 55],
        ["A+", "A", "B", "C"],
        default="PASS",
    )

    def _gate_reason(row: pd.Series) -> str:
        checks = [
            ("price",        row.gate_price),
            ("volume",       row.gate_volume),
            ("dollar_vol",   row.gate_dollar_vol),
            ("market_cap",   row.gate_market_cap),
            ("earnings_win", row.gate_earnings),
        ]
        bad = [name for name, ok in checks if not ok]
        return "OK" if not bad else ",".join(bad)

    x["gate_reason"] = x.apply(_gate_reason, axis=1)

    return x.sort_values(
        ["trade_score", "swing_score", "quality_score"],
        ascending=False,
    ).reset_index(drop=True)


def top_candidates(
    scored: pd.DataFrame,
    n: int = 30,
    minimum_score: float = 60.0,
    universe_type: Optional[str] = None,
) -> pd.DataFrame:
    """Return top-n candidates that pass the hard gate and minimum score.

    Args:
        scored:         Output of calculate_scores().
        n:              Maximum number to return.
        minimum_score:  Minimum trade_score to include (default 60).
        universe_type:  If 'Swing / Momentum', rank by swing_score instead.
    """
    filtered = scored[(scored.hard_gate) & (scored.trade_score >= minimum_score)].copy()

    if universe_type and "swing" in universe_type.lower():
        filtered = filtered.sort_values("swing_score", ascending=False)
    else:
        filtered = filtered.sort_values("trade_score", ascending=False)

    return filtered.head(n).reset_index(drop=True)
