"""
atos/learner.py
----------------
Updates detector weights after every closed trade.

The core idea:
  - When a trade is PROFITABLE: reward detectors that were POSITIVE at entry
  - When a trade is a LOSS:     reward detectors that were NEGATIVE at entry
                                (they were trying to warn us)
  - After updating: normalize weights so they sum to 8.0 (one per detector)
  - Weights are bounded: min 0.3 (never fully ignored), max 2.5 (never dominant)

This is gradient-free reinforcement learning on real trade outcomes.
Over hundreds of trades, detectors that consistently predict correctly
earn more influence. Those that fire randomly lose influence.
"""

from . import database as db

# Learning rate — how much each trade shifts the weights
LEARN_RATE_REWARD  = 0.06   # reward correct detector
LEARN_RATE_PENALISE = 0.04  # penalise wrong detector

# Weight bounds — keep all detectors in the game
MIN_WEIGHT = 0.30
MAX_WEIGHT = 2.50

# Target sum of all weights (8 detectors × 1.0 = 8.0 at equal weights)
TARGET_SUM = 8.0

# Minimum trades before learning kicks in (avoid overreacting to early noise)
MIN_TRADES_TO_LEARN = 5


def _safe_score(val):
    """Guard against NULL/None detector scores from legacy imported trades."""
    if val is None:
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def update_weights_from_closed_trade(trade: dict) -> dict | None:
    """
    Given a single closed trade record from the DB, update detector weights.
    Returns new weights dict, or None if minimum trades threshold not met.

    trade dict keys used:
      was_profitable : 1 (profit) or 0 (loss)
      d1_trend to d8_regime
        (these are the scores at ENTRY time — positive means detector said BUY)
    """
    current = db.get_current_weights()
    num_trades = current.get("num_trades", 0)

    if num_trades < MIN_TRADES_TO_LEARN:
        # Just increment count and return without changing weights
        db.save_weights(current, num_trades + 1,
                        note=f"warming up ({num_trades+1}/{MIN_TRADES_TO_LEARN})")
        return None

    profitable = bool(trade.get("was_profitable"))

    # Each detector's entry score — positive means it was bullish at entry
    detector_scores = {
        'w_trend':       _safe_score(trade.get('d1_trend')),
        'w_momentum':    _safe_score(trade.get('d2_momentum')),
        'w_breakout':    _safe_score(trade.get('d3_breakout')),
        'w_mean_revert': _safe_score(trade.get('d4_mean_revert')),
        'w_volume':      _safe_score(trade.get('d5_volume')),
        'w_smart_money': _safe_score(trade.get('d6_smart_money')),
        'w_mom_quality': _safe_score(trade.get('d7_mom_quality')),
        'w_regime':      _safe_score(trade.get('d8_regime')),
    }

    # Calculate magnitude factor from trade P&L
    pnl_sek = trade.get('pnl_sek', 0) or 0
    entry_val = (trade.get('shares', 1) or 1) * (trade.get('entry_price', 1) or 1)
    if entry_val > 0:
        pnl_pct = abs(pnl_sek) / entry_val
    else:
        pnl_pct = 0
    magnitude = min(pnl_pct, 0.10) / 0.10  # normalize: 0 to 1, capped at 10%
    magnitude_factor = 0.5 + magnitude  # range: 0.5x to 1.5x

    reward_rate = LEARN_RATE_REWARD * magnitude_factor
    penalty_rate = LEARN_RATE_PENALISE * magnitude_factor

    decay = trade.get('_decay', 1.0)
    actual_reward = reward_rate * decay
    actual_penalty = penalty_rate * decay

    new_weights = {}
    for key, current_w in current.items():
        if key in ("num_trades",):
            continue
        det_score = detector_scores.get(key, 0)

        if profitable:
            # Trade won: reward detectors that were bullish (positive score)
            if det_score > 0:
                new_w = current_w + actual_reward
            elif det_score < 0:
                new_w = current_w - actual_penalty   # was negative, trade won = wrong
            else:
                new_w = current_w   # neutral detector — no change
        else:
            # Trade lost: reward detectors that were bearish (negative score)
            if det_score < 0:
                new_w = current_w + actual_reward     # it warned us — reward
            elif det_score > 0:
                new_w = current_w - actual_penalty   # it said buy, we lost
            else:
                new_w = current_w

        # Apply bounds
        new_weights[key] = max(MIN_WEIGHT, min(MAX_WEIGHT, new_w))

    # Normalize so total = TARGET_SUM
    total = sum(new_weights.values())
    if total > 0:
        factor = TARGET_SUM / total
        new_weights = {k: round(v * factor, 4) for k, v in new_weights.items()}

    note = (f"trade #{num_trades+1} {'PROFIT' if profitable else 'LOSS'} — "
            f"{trade.get('ticker', '?')} @ {trade.get('exit_date', '?')}")
    db.save_weights(new_weights, num_trades + 1, note=note)
    return new_weights


def run_learning_pass():
    """
    Called once per daily run. Reviews all trades closed since the last
    weight update and applies learning for each.

    Returns summary of what changed.
    """
    before = db.get_current_weights()
    # get_recent_closed_trades returns newest-first (ORDER BY exit_date DESC);
    # reverse to oldest-first so slicing by the processed-trade count picks up
    # genuinely NEW trades, not the most recent ones (which would re-learn old
    # trades and skip new ones).
    closed = list(reversed(db.get_recent_closed_trades(n=200)))

    num_trades_before = before.get("num_trades", 0)
    new_trades = [t for t in closed
                  if t.get("was_profitable") is not None]

    # Only process trades that haven't been learned from yet
    # (process trades beyond the count already folded into the weights)
    unprocessed = new_trades[num_trades_before:]

    if not unprocessed:
        return {"new_trades_processed": 0, "weights": before}

    for i, trade in enumerate(unprocessed):
        # Older trades have less influence
        trade['_decay'] = 0.95 ** i  # most recent = 1.0, older = 0.95^n
        update_weights_from_closed_trade(trade)

    after = db.get_current_weights()
    return {
        "new_trades_processed": len(unprocessed),
        "weights_before": before,
        "weights_after":  after,
    }


def format_weight_bar(weight: float, max_w: float = MAX_WEIGHT, width: int = 10) -> str:
    """Returns a simple ASCII bar for terminal display."""
    filled = round((weight / max_w) * width)
    return "█" * filled + "░" * (width - filled)
