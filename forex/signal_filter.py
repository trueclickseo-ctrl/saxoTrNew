"""
forex/signal_filter.py
----------------------
Cross-strategy signal filter for the FX runner.

TWO PHASES:

Phase 1 — Consensus filter (active now, no training data needed)
  Before executing any signal, scan ALL 9 strategies on that pair.
  Count how many agree on the same direction.
  Only execute if agreement >= MIN_AGREEMENT (default 1 = at least the
  firing strategy itself, so min=1 means unrestricted; set min=2 to
  require at least one other strategy to confirm).

Phase 2 — ML meta-filter (auto-activates when >= MIN_TRADES_FOR_ML closed trades)
  Logistic regression trained on the features that Phase 1 logs.
  Features: per-strategy agreement booleans + ADX + ATR + day-of-week.
  Label: 1 = trade closed at profit, 0 = loss/stop.
  Retrained weekly; saved to data/fx_meta_model.pkl.

The two phases complement each other:
  - Phase 1 is always active (zero-data, interpretable).
  - Phase 2 refines Phase 1's confidence using historical patterns.

Usage in runner:
    sf = SignalFilter(market_data, live_prices, strategies)
    score, features = sf.evaluate(sym, direction, signal)
    if score.passes_threshold:
        execute_trade(...)
    sf.log_signal(key, features)     # saves to CSV for ML training
    # On trade close:
    sf.label_outcome(key, pnl > 0)  # marks the logged row as win/loss
"""

import csv
import json
import logging
import os
import pickle
from datetime import date, datetime

import numpy as np
import pandas as pd

logger = logging.getLogger("forex.signal_filter")

_ROOT      = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATA_DIR  = os.path.join(_ROOT, "data")
_LOG_CSV   = os.path.join(_DATA_DIR, "fx_signal_log.csv")
_MODEL_PKL = os.path.join(_DATA_DIR, "fx_meta_model.pkl")

# ── Config ────────────────────────────────────────────────────────────────────

MIN_AGREEMENT     = 1    # minimum strategies agreeing (1 = no filter; 2 = need 1 confirmer)
ML_THRESHOLD      = 0.58 # ML probability threshold to pass (Phase 2)
MIN_TRADES_FOR_ML = 150  # minimum closed trades before enabling ML meta-filter

_CSV_COLS = [
    "key", "date", "symbol", "direction",
    "ema", "rsi", "donchian", "bb", "pullback",
    "gap", "supertrend", "zscore", "ml",
    "agreement_count", "adx", "atr_pct", "day_of_week",
    "ml_prob",          # Phase 2: ML meta-filter output
    "outcome",          # 1=win, 0=loss, ''=open/unknown
]

# ── Agreement computation ─────────────────────────────────────────────────────

def _quick_signals(strat_mod, market_data: dict, live_prices: dict) -> dict:
    """
    Run one strategy's generate_signals() safely and return
    {sym: direction} for all signals it produces right now.
    """
    try:
        if getattr(strat_mod, "NEEDS_LIVE_PRICES", False):
            sigs = strat_mod.generate_signals(market_data, live_prices=live_prices)
        else:
            sigs = strat_mod.generate_signals(market_data)
        return {s["symbol"]: s["direction"] for s in (sigs or [])}
    except Exception:
        return {}


def compute_agreement(market_data: dict, live_prices: dict,
                      strategies: dict) -> dict:
    """
    Run ALL strategies dry and build an agreement map.

    Returns:
        {sym: {"Buy": [strat_names], "Sell": [strat_names]}}
    """
    agreement: dict = {}
    for name, mod in strategies.items():
        for sym, direction in _quick_signals(mod, market_data, live_prices).items():
            if sym not in agreement:
                agreement[sym] = {"Buy": [], "Sell": []}
            agreement[sym][direction].append(name)
    return agreement


# ── Feature extraction ────────────────────────────────────────────────────────

def extract_features(sym: str, direction: str,
                     agreement: dict,
                     signal: dict,
                     strategy_names: list) -> dict:
    """
    Build the feature vector for one signal event.

    Returns a dict with keys matching _CSV_COLS (minus key/date/outcome).
    """
    sym_agreement = agreement.get(sym, {"Buy": [], "Sell": []})
    agrees        = sym_agreement.get(direction, [])

    def flag(name: str) -> int:
        return 1 if name in agrees else 0

    atr   = signal.get("atr", 0)
    close = signal.get("close", 1)
    adx   = signal.get("adx", signal.get("adx_val", 0))
    dow   = date.today().weekday()   # 0=Mon … 4=Fri

    return {
        "symbol":          sym,
        "direction":       direction,
        "ema":             flag("ema"),
        "rsi":             flag("rsi"),
        "donchian":        flag("donchian"),
        "bb":              flag("bb"),
        "pullback":        flag("pullback"),
        "gap":             flag("gap"),
        "supertrend":      flag("supertrend"),
        "zscore":          flag("zscore"),
        "ml":              flag("ml"),
        "agreement_count": len(agrees),
        "adx":             round(float(adx), 2) if adx else 0,
        "atr_pct":         round(float(atr) / float(close) * 100, 4) if close else 0,
        "day_of_week":     dow,
        "ml_prob":         "",   # filled if Phase 2 model runs
        "outcome":         "",   # filled when trade closes
    }


# ── Phase 1: Consensus filter ─────────────────────────────────────────────────

def passes_consensus(features: dict, min_agreement: int = MIN_AGREEMENT) -> bool:
    """
    Returns True if this signal has at least `min_agreement` strategies confirming it.
    Agreement count includes the firing strategy itself, so:
        min_agreement=1 → unrestricted (always passes)
        min_agreement=2 → at least one other strategy also fires for this pair
        min_agreement=3 → two other strategies confirm
    """
    return features["agreement_count"] >= min_agreement


# ── Phase 2: ML meta-filter ───────────────────────────────────────────────────

_FEATURE_COLS = [
    "ema", "rsi", "donchian", "bb", "pullback",
    "gap", "supertrend", "zscore", "ml",
    "agreement_count", "adx", "atr_pct", "day_of_week",
]

_model_cache: dict = {}   # {"model": ..., "scaler": ..., "loaded_at": datetime}


def _load_model() -> dict | None:
    """Load the saved meta-model from disk. Returns None if not found."""
    global _model_cache
    if _model_cache and (datetime.now() - _model_cache["loaded_at"]).seconds < 3600:
        return _model_cache
    if not os.path.exists(_MODEL_PKL):
        return None
    try:
        with open(_MODEL_PKL, "rb") as f:
            obj = pickle.load(f)
        _model_cache = {**obj, "loaded_at": datetime.now()}
        logger.info(f"[meta_ml] Loaded model from {_MODEL_PKL}  "
                    f"(trained on {obj.get('n_samples', '?')} samples)")
        return _model_cache
    except Exception as exc:
        logger.warning(f"[meta_ml] Could not load model: {exc}")
        return None


def ml_probability(features: dict) -> float | None:
    """
    Run the ML meta-filter on a feature vector.
    Returns probability of win (0-1), or None if model not available.
    """
    obj = _load_model()
    if obj is None:
        return None
    try:
        model  = obj["model"]
        scaler = obj["scaler"]
        X      = [[features.get(c, 0) for c in _FEATURE_COLS]]
        X_sc   = scaler.transform(X)
        prob   = float(model.predict_proba(X_sc)[0][1])
        return prob
    except Exception as exc:
        logger.warning(f"[meta_ml] predict failed: {exc}")
        return None


def passes_ml(features: dict, threshold: float = ML_THRESHOLD) -> tuple[bool, float | None]:
    """
    Apply ML meta-filter. Returns (passes: bool, prob: float | None).
    If model not available, always passes (defers to consensus filter).
    """
    prob = ml_probability(features)
    if prob is None:
        return True, None
    return prob >= threshold, prob


# ── Combined filter ───────────────────────────────────────────────────────────

def evaluate(sym: str, direction: str, signal: dict,
             agreement: dict, strategies: dict,
             min_agreement: int = MIN_AGREEMENT) -> tuple[bool, dict, str]:
    """
    Run both Phase 1 and Phase 2 filters on a signal.

    Returns:
        (passes: bool, features: dict, reason: str)
        reason = "" if passes, or short explanation if blocked
    """
    features = extract_features(sym, direction, agreement, signal,
                                list(strategies.keys()))

    # Phase 1 — consensus
    if not passes_consensus(features, min_agreement):
        agreeing = features["agreement_count"]
        return False, features, (
            f"consensus={agreeing}/{min_agreement} required"
        )

    # Phase 2 — ML (only if model exists and we have enough historical data)
    ok, prob = passes_ml(features)
    if prob is not None:
        features["ml_prob"] = round(prob, 4)
        if not ok:
            return False, features, f"ml_prob={prob:.2f} < {ML_THRESHOLD}"

    return True, features, ""


# ── Signal logging ────────────────────────────────────────────────────────────

def _ensure_csv() -> None:
    os.makedirs(_DATA_DIR, exist_ok=True)
    if not os.path.exists(_LOG_CSV) or os.path.getsize(_LOG_CSV) == 0:
        with open(_LOG_CSV, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(_CSV_COLS)


def log_signal(key: str, features: dict) -> None:
    """
    Append a signal event to the training log CSV.
    key = "strategy:symbol", e.g. "pullback:AUDJPY"
    """
    try:
        _ensure_csv()
        row = {c: features.get(c, "") for c in _CSV_COLS}
        row["key"]  = key
        row["date"] = date.today().isoformat()
        with open(_LOG_CSV, "a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=_CSV_COLS).writerow(row)
    except Exception as exc:
        logger.warning(f"[signal_filter] log_signal failed: {exc}")


def label_outcome(key: str, won: bool) -> None:
    """
    Update the most-recent unresolved row for `key` with the trade outcome.
    Called when the trade closes.  Reads the whole CSV, patches, rewrites.
    Silently skips if no unlabeled row found.
    """
    if not os.path.exists(_LOG_CSV):
        return
    try:
        rows = []
        patched = False
        with open(_LOG_CSV, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
        # Patch the last unlabeled row matching this key
        for row in reversed(rows):
            if row.get("key") == key and row.get("outcome") == "":
                row["outcome"] = "1" if won else "0"
                patched = True
                break
        if patched:
            with open(_LOG_CSV, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=_CSV_COLS)
                w.writeheader()
                w.writerows(rows)
    except Exception as exc:
        logger.warning(f"[signal_filter] label_outcome failed: {exc}")


# ── Model training ────────────────────────────────────────────────────────────

def retrain(min_samples: int = MIN_TRADES_FOR_ML) -> dict | None:
    """
    (Re)train the ML meta-filter on all labeled signal rows.
    Saves to data/fx_meta_model.pkl.

    Returns dict with model stats, or None if not enough data.
    Call weekly (e.g. from a separate scheduler task or manually).
    """
    if not os.path.exists(_LOG_CSV):
        logger.info("[meta_ml] No signal log found — skipping retrain")
        return None

    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.model_selection import cross_val_score
        from sklearn.preprocessing import StandardScaler
    except ImportError:
        logger.warning("[meta_ml] scikit-learn not installed — cannot retrain")
        return None

    df = pd.read_csv(_LOG_CSV)
    df = df[df["outcome"].isin(["0", "1", 0, 1])].copy()
    df["outcome"] = df["outcome"].astype(int)

    if len(df) < min_samples:
        logger.info(f"[meta_ml] Only {len(df)} labeled trades "
                    f"(need {min_samples}) — skipping retrain")
        return None

    X = df[_FEATURE_COLS].fillna(0).values
    y = df["outcome"].values

    scaler = StandardScaler()
    X_sc   = scaler.fit_transform(X)

    model  = LogisticRegression(C=0.5, max_iter=500, random_state=42)
    model.fit(X_sc, y)

    cv_scores = cross_val_score(model, X_sc, y, cv=5, scoring="accuracy")
    wr_overall = int(y.sum()) / len(y) * 100

    obj = {
        "model":       model,
        "scaler":      scaler,
        "n_samples":   len(df),
        "cv_accuracy": float(cv_scores.mean()),
        "cv_std":      float(cv_scores.std()),
        "base_wr":     round(wr_overall, 1),
        "trained_at":  datetime.now().isoformat(),
        "feature_cols": _FEATURE_COLS,
    }
    with open(_MODEL_PKL, "wb") as f:
        pickle.dump(obj, f)

    global _model_cache
    _model_cache = {**obj, "loaded_at": datetime.now()}

    logger.info(
        f"[meta_ml] Model retrained: n={len(df)}  "
        f"CV accuracy={cv_scores.mean():.1%}±{cv_scores.std():.1%}  "
        f"base WR={wr_overall:.1f}%"
    )
    return obj


def training_status() -> dict:
    """
    Return current status: how many labeled rows, whether model exists,
    how far from the ML activation threshold.
    """
    n_labeled = 0
    n_wins    = 0
    if os.path.exists(_LOG_CSV):
        df = pd.read_csv(_LOG_CSV)
        labeled   = df[df["outcome"].isin(["0", "1"])]
        n_labeled = len(labeled)
        n_wins    = int((labeled["outcome"] == "1").sum())

    model_exists = os.path.exists(_MODEL_PKL)
    obj          = _load_model() if model_exists else None

    return {
        "labeled_trades":   n_labeled,
        "needed_for_ml":    max(0, MIN_TRADES_FOR_ML - n_labeled),
        "wins":             n_wins,
        "win_rate":         round(n_wins / n_labeled * 100, 1) if n_labeled else 0,
        "model_exists":     model_exists,
        "model_accuracy":   round(obj["cv_accuracy"] * 100, 1) if obj else None,
        "model_trained_at": obj.get("trained_at", "")[:10] if obj else None,
        "ml_active":        model_exists,
    }
