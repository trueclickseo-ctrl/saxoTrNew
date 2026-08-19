"""
atos/decision_engine.py
------------------------
Combines the 5 detector scores into a single buy/sell decision.

The engine:
  1. Loads current weights from the database
  2. Runs all 5 detectors on the feature row
  3. Computes a weighted average score
  4. Returns BUY / HOLD / EXIT with the score breakdown

Weights are learned over time by learner.py — this module only reads them.
"""

import pandas as pd
from typing import NamedTuple

from .detectors import (
    Detector1_Trend, Detector2_Momentum, Detector3_Breakout,
    Detector4_MeanReversion, Detector5_Volume,
)
from .universe import DETECTOR_MARKET_OVERRIDES
from . import database as db
from typing import NamedTuple as _NamedTuple_typing  # avoid conflict


# Thresholds
BUY_THRESHOLD  = 55   # combined score ≥ 55 → BUY signal
EXIT_THRESHOLD = 20   # combined score ≤ 20 on open position → EXIT


class Decision(NamedTuple):
    action:        str    # 'BUY' | 'EXIT' | 'HOLD'
    score:         float  # combined weighted score (-100 to 100)
    d1_trend:      float
    d2_momentum:   float
    d3_breakout:   float
    d4_mean_revert:float
    d5_volume:     float


_d1 = Detector1_Trend()
_d2 = Detector2_Momentum()
_d3 = Detector3_Breakout()
_d4 = Detector4_MeanReversion()
_d5 = Detector5_Volume()


def evaluate(row: pd.Series, market_group: str,
             weights: dict | None = None,
             is_open_position: bool = False) -> Decision:
    """
    Evaluate a single ticker's latest feature row.

    Parameters
    ----------
    row             : last row of features DataFrame (from features.add_all)
    market_group    : e.g. "US Equities", "Forex", "Commodities"
    weights         : dict of detector weights (if None, loaded from DB)
    is_open_position: True when checking exit for an already-open trade
    """
    if weights is None:
        weights = db.get_current_weights()

    # Per-asset-class overrides
    overrides = DETECTOR_MARKET_OVERRIDES.get(market_group, {})
    mr_enabled = overrides.get("mean_reversion_enabled", True)
    trend_boost   = overrides.get("trend_weight_boost", 1.0)
    breakout_boost = overrides.get("breakout_weight_boost", 1.0)

    # ── Run all 5 detectors ───────────────────────────────────────
    s1 = _d1.score(row)
    s2 = _d2.score(row)
    s3 = _d3.score(row)
    s4 = _d4.score(row, enabled=mr_enabled)
    s5 = _d5.score(row)

    # ── Apply weights ─────────────────────────────────────────────
    w1 = weights["w_trend"]       * trend_boost
    w2 = weights["w_momentum"]
    w3 = weights["w_breakout"]    * breakout_boost
    w4 = weights["w_mean_revert"] if mr_enabled else 0.0
    w5 = weights["w_volume"]

    total_weight = w1 + w2 + w3 + w4 + w5
    if total_weight == 0:
        combined = 0.0
    else:
        combined = (s1*w1 + s2*w2 + s3*w3 + s4*w4 + s5*w5) / total_weight

    # ── Determine action ──────────────────────────────────────────
    if is_open_position:
        action = "EXIT" if combined <= EXIT_THRESHOLD else "HOLD"
    else:
        action = "BUY" if combined >= BUY_THRESHOLD else "HOLD"

    return Decision(
        action=action,
        score=round(combined, 1),
        d1_trend=round(s1, 1),
        d2_momentum=round(s2, 1),
        d3_breakout=round(s3, 1),
        d4_mean_revert=round(s4, 1),
        d5_volume=round(s5, 1),
    )


def scan_universe(universe_data: dict[str, pd.DataFrame],
                  market_group_fn,
                  open_tickers: set[str],
                  weights: dict | None = None) -> dict[str, Decision]:
    """
    Evaluate every ticker in universe_data.

    Returns
    -------
    dict mapping ticker → Decision for all tickers with a non-HOLD signal,
    plus all tickers that are currently open positions (for exit checking).
    """
    if weights is None:
        weights = db.get_current_weights()

    results = {}
    for ticker, df in universe_data.items():
        if df is None or df.empty or len(df) < 50:
            continue
        row = df.iloc[-1]
        if pd.isna(row.get("ema50")) or pd.isna(row.get("atr")):
            continue   # not enough history yet

        market_group  = market_group_fn(ticker)
        is_open       = ticker in open_tickers
        decision      = evaluate(row, market_group, weights, is_open_position=is_open)

        # Keep: BUY signals, EXIT signals, and all open positions
        if decision.action != "HOLD" or is_open:
            results[ticker] = decision

    return results


# ─── ATOS v3: Multi-Strategy Consensus Engine ─────────────────────────────────

class ConsensusDecision(NamedTuple):
    """Result of multi-strategy consensus voting."""
    final_action:       str     # 'BUY' | 'EXIT' | 'HOLD'
    agreement_count:    int     # how many strategies voted for the action
    total_strategies:   int     # how many strategies were consulted
    combined_confidence: float  # average confidence of agreeing strategies
    regime:             str     # current market regime
    detector_score:     float   # ATOS v2 detector score (for reference)
    strategy_breakdown: dict    # {strategy_name: {'signal': str, 'confidence': float}}
    trailing_stop:      float   # from v2 engine


def consensus_evaluate(df: pd.DataFrame, market_group: str,
                       strategies: list = None,
                       min_agreement: int = 3,
                       is_open_position: bool = False,
                       weights: dict = None) -> ConsensusDecision:
    """
    Run all validated strategies and make decision by consensus.
    
    BUY requires >= min_agreement strategies voting BUY.
    EXIT requires >= 2 strategies voting SELL on an open position.
    In BEAR regime, min_agreement is increased by 1 (stricter).
    
    Parameters
    ----------
    df : pd.DataFrame
        Full feature DataFrame for one ticker
    market_group : str
        e.g. 'US Equities', 'OMX30'
    strategies : list of Strategy instances (if None, uses ALL_STRATEGIES)
    min_agreement : int
        Minimum strategies that must agree for a BUY (default 3)
    is_open_position : bool
        True when checking exit for an existing position
    weights : dict
        Detector weights (passed to v2 evaluate)
    """
    # Import strategies lazily to avoid circular import
    if strategies is None:
        try:
            from .strategies import ALL_STRATEGIES
            strategies = [cls() for cls in ALL_STRATEGIES]
        except ImportError:
            strategies = []
    
    if len(df) < 50:
        return ConsensusDecision(
            final_action='HOLD', agreement_count=0,
            total_strategies=len(strategies),
            combined_confidence=0.0, regime='TRANSITION',
            detector_score=0.0, strategy_breakdown={},
            trailing_stop=0.0
        )
    
    # Step 1: Run the v2 detector engine for reference
    row = df.iloc[-1]
    v2_decision = evaluate(row, market_group, weights, is_open_position)
    # Regime comes from the features DataFrame, not the Decision NamedTuple
    # (Decision has no regime field — getattr would always return 'TRANSITION')
    regime = row.get("regime", "TRANSITION") if hasattr(row, 'get') else "TRANSITION"
    
    # Step 2: Run each strategy
    breakdown = {}
    buy_votes = []
    sell_votes = []
    
    for strat in strategies:
        try:
            # Check if strategy applies to this market
            if market_group not in strat.asset_classes:
                continue
            if len(df) < strat.min_history:
                continue
            
            sig = strat.signal(df)
            conf = strat.confidence(df)
            breakdown[strat.name] = {'signal': sig, 'confidence': round(conf, 2)}
            
            if sig == 'BUY':
                buy_votes.append(conf)
            elif sig in ('SELL', 'EXIT'):
                sell_votes.append(conf)
        except Exception as e:
            breakdown[strat.name] = {'signal': 'ERROR', 'confidence': 0.0, 'error': str(e)}
    
    total_strategies = len(breakdown)
    
    # Step 3: Adjust min_agreement based on regime
    effective_min = min_agreement
    if regime == 'BEAR':
        effective_min = min_agreement + 1  # Stricter in bear markets
    elif regime == 'BULL':
        effective_min = max(min_agreement - 1, 2)  # Easier in bull markets
    
    # Step 4: Determine final action
    if is_open_position:
        # For exits: >= 2 strategies must vote SELL
        if len(sell_votes) >= 2:
            final_action = 'EXIT'
            agreement = len(sell_votes)
            avg_conf = sum(sell_votes) / len(sell_votes) if sell_votes else 0
        # Also exit if v2 engine says EXIT
        elif v2_decision.action == 'EXIT':
            final_action = 'EXIT'
            agreement = 1  # Only v2 voted
            avg_conf = abs(v2_decision.score) / 100.0
        else:
            final_action = 'HOLD'
            agreement = 0
            avg_conf = 0.0
    else:
        # For entries: >= effective_min strategies must vote BUY
        if len(buy_votes) >= effective_min:
            final_action = 'BUY'
            agreement = len(buy_votes)
            avg_conf = sum(buy_votes) / len(buy_votes) if buy_votes else 0
        else:
            final_action = 'HOLD'
            agreement = len(buy_votes)  # How many wanted to buy (not enough)
            avg_conf = sum(buy_votes) / len(buy_votes) if buy_votes else 0
    
    return ConsensusDecision(
        final_action=final_action,
        agreement_count=agreement,
        total_strategies=total_strategies,
        combined_confidence=round(avg_conf, 2),
        regime=regime,
        detector_score=v2_decision.score,
        strategy_breakdown=breakdown,
        trailing_stop=getattr(v2_decision, 'trailing_stop', 0.0),
    )
