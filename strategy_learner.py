"""
strategy_learner.py
-------------------
Adaptive strategy weight learner for Forex and Futures modules.

Mirrors the principle of atos/learner.py — gradient-free reinforcement
learning on real trade outcomes — but operates at the strategy level
rather than the detector level.

After each closed trade the strategy that generated it is:
  - REWARDED  if the trade was profitable
  - PENALISED if the trade was a loss

Over hundreds of trades, strategies that consistently produce winners
earn higher weights and receive priority slot access in the runner.
Underperforming strategies get fewer slots but are never fully silenced
(MIN_WEIGHT floor keeps them in the game for regime shifts).

Weights are stored in data/{module}_strategy_weights.json and read at
the start of every runner cycle. No structural changes to runners are
needed — just sort strategies by weight and pass the weight factor into
the entry function to scale max_slots.

API
---
  get_weights(module)           -> dict {strategy: weight}
  run_learning_pass(module)     -> dict summary
  slot_scale(weight)            -> float multiplier for max_slots
"""

import json
import logging
import os
from datetime import date

import pnl_tracker

logger  = logging.getLogger("strategy_learner")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

# ── Hyperparameters ────────────────────────────────────────────────────────
LEARN_RATE_REWARD   = 0.08   # weight bump per profitable trade
LEARN_RATE_PENALISE = 0.05   # weight drop per losing trade
MIN_WEIGHT          = 0.30   # floor — strategy always gets at least some slots
MAX_WEIGHT          = 2.00   # ceiling — no strategy dominates completely
MIN_TRADES_TO_LEARN = 5      # warm-up period before weights shift

# ── Strategy registries ────────────────────────────────────────────────────
# Must match the strategy names logged in pnl_tracker (from each runner's
# STRATEGIES dict key). New names auto-initialize to weight 1.0.
STRATEGY_NAMES = {
    "forex": [
        "ema", "rsi", "donchian", "bb", "pullback",
        "gap", "supertrend", "zscore", "ml", "cnn_lstm",
    ],
    "futures": [
        "donchian", "rsi", "ema", "macd", "squeeze",
        "ma_cross", "trend_ma",
    ],
}


# ── File helpers ───────────────────────────────────────────────────────────

def _weights_file(module: str) -> str:
    return os.path.join(DATA_DIR, f"{module}_strategy_weights.json")


def _default_weights(module: str) -> dict:
    """All strategies start at equal weight 1.0 (no prior knowledge assumed)."""
    return {name: 1.0 for name in STRATEGY_NAMES.get(module, [])}


def get_weights(module: str) -> dict:
    """
    Return current strategy weights for a module.
    Falls back to equal weights (1.0 each) if the file doesn't exist yet.
    Unknown strategy names in the file are kept; new strategies missing from
    the file are initialized to 1.0.
    """
    path = _weights_file(module)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            weights = dict(data.get("weights", {}))
            # Seed any new strategies added since the file was created
            for name in STRATEGY_NAMES.get(module, []):
                weights.setdefault(name, 1.0)
            return weights
        except Exception as exc:
            logger.warning(f"[learner] Could not load {module} weights: {exc} — using defaults")
    return _default_weights(module)


def _load_meta(module: str) -> dict:
    path = _weights_file(module)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f).get("meta", {})
        except Exception:
            pass
    return {}


def _save_weights(module: str, weights: dict, meta: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    path = _weights_file(module)
    tmp  = path + f".tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"weights": weights, "meta": meta}, f, indent=2)
    os.replace(tmp, path)


# ── Core learning ──────────────────────────────────────────────────────────

def run_learning_pass(module: str) -> dict:
    """
    Called once at the end of each daily runner cycle.

    Reads all closed trades from pnl_ledger.db that haven't been processed
    yet, and updates the strategy weights for this module.

    Older trades have decayed influence (0.95^i) so recent performance
    counts more than distant history.

    Returns a summary dict for logging.
    """
    weights = get_weights(module)
    meta    = _load_meta(module)
    num_processed = meta.get("num_processed", 0)

    # Fetch closed trades oldest-first so the slice picks up genuinely new ones
    all_closed = list(reversed(
        pnl_tracker.get_closed_trades(module=module, limit=1000)
    ))

    # Only trades where we know P&L and which strategy fired
    valid = [t for t in all_closed
             if t.get("realized_pnl") is not None and t.get("strategy")]

    unprocessed = valid[num_processed:]
    if not unprocessed:
        return {"module": module, "new_trades": 0, "weights": weights}

    if num_processed < MIN_TRADES_TO_LEARN:
        # Warm-up: count trades but don't shift weights yet
        meta["num_processed"] = num_processed + len(unprocessed)
        meta["last_updated"]  = date.today().isoformat()
        _save_weights(module, weights, meta)
        logger.info(f"[strategy_learner] {module}: warming up "
                    f"({meta['num_processed']}/{MIN_TRADES_TO_LEARN} trades) — weights unchanged")
        return {"module": module, "new_trades": len(unprocessed), "weights": weights}

    weights_before = dict(weights)

    for i, trade in enumerate(unprocessed):
        strat = trade["strategy"]
        if strat not in weights:
            weights[strat] = 1.0   # new strategy seen for first time

        pnl        = float(trade.get("realized_pnl") or 0)
        profitable = pnl > 0

        # Magnitude factor: big wins/losses shift weights more than small ones
        qty       = float(trade.get("quantity")    or 1)
        ep        = float(trade.get("entry_price") or 1)
        entry_val = qty * ep
        pnl_pct   = abs(pnl) / entry_val if entry_val > 0 else 0
        magnitude  = min(pnl_pct, 0.10) / 0.10   # normalise to [0, 1], cap at 10%
        mag_factor = 0.5 + magnitude               # range [0.5×, 1.5×]

        # Recency decay: most recent trade (i=0) gets full weight, older trades decay
        decay   = 0.95 ** i
        reward  = LEARN_RATE_REWARD   * mag_factor * decay
        penalty = LEARN_RATE_PENALISE * mag_factor * decay

        if profitable:
            weights[strat] = min(MAX_WEIGHT, weights[strat] + reward)
        else:
            weights[strat] = max(MIN_WEIGHT, weights[strat] - penalty)

    weights = {k: round(v, 4) for k, v in weights.items()}

    meta["num_processed"] = num_processed + len(unprocessed)
    meta["last_updated"]  = date.today().isoformat()
    _save_weights(module, weights, meta)

    # Build a readable delta string for the log
    changed = {k: (weights_before.get(k, 1.0), weights[k])
               for k in weights if weights[k] != weights_before.get(k, 1.0)}
    delta_str = "  ".join(f"{k} {b:.3f}→{a:.3f}" for k, (b, a) in changed.items())
    logger.info(f"[strategy_learner] {module}: {len(unprocessed)} new trades processed  "
                f"| changes: {delta_str or 'none'}")

    return {
        "module":          module,
        "new_trades":      len(unprocessed),
        "weights":         weights,
        "weights_before":  weights_before,
    }


# ── Runner helper ──────────────────────────────────────────────────────────

def slot_scale(weight: float) -> float:
    """
    Convert a strategy weight into a slot-count multiplier for the runner.

    weight 0.30 → 0.50×  (half slots — underperformer)
    weight 1.00 → 1.00×  (normal slots — baseline, unchanged from pre-learner)
    weight 2.00 → 1.50×  (50% extra slots — proven winner)

    Piecewise linear so weight=1.0 maps exactly to 1.0× (no disruption on
    day 1 when all strategies start at the default weight of 1.0).
    """
    if weight <= 1.0:
        # [MIN_WEIGHT, 1.0] → [0.5, 1.0]
        raw = 0.5 + 0.5 * (weight - MIN_WEIGHT) / (1.0 - MIN_WEIGHT)
    else:
        # (1.0, MAX_WEIGHT] → (1.0, 1.5]
        raw = 1.0 + 0.5 * (weight - 1.0) / (MAX_WEIGHT - 1.0)
    return round(max(0.5, min(1.5, raw)), 3)


def log_weights_table(module: str) -> None:
    """Print a readable weight table to the logger (INFO level)."""
    weights = get_weights(module)
    meta    = _load_meta(module)
    n       = meta.get("num_processed", 0)
    logger.info(f"[strategy_learner] {module} weights after {n} trades:")
    for strat, w in sorted(weights.items(), key=lambda x: x[1], reverse=True):
        bar   = "█" * int(w / MAX_WEIGHT * 20) + "░" * (20 - int(w / MAX_WEIGHT * 20))
        scale = slot_scale(w)
        logger.info(f"  {strat:<12} {bar}  {w:.3f}  (slots ×{scale:.2f})")
