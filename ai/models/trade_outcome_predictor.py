"""
ai/models/trade_outcome_predictor.py
--------------------------------------
Trade Outcome Predictor (TOP) — a gradient-boosting classifier trained on
our actual closed observation cards to predict whether a new trade signal
will produce a positive R-multiple.

Why this replaces the CNN-LSTM approach
----------------------------------------
The CNN-LSTM predicts raw price *direction* from 13 years of Yahoo bars.
What we actually want to know is: "given OUR strategy, OUR entry context,
and this pair's history, will THIS trade be profitable?" That's a function
of features we already log at entry time — regime, RSI, ADX, spread cost,
strategy type, portfolio state.

Sources
-------
data/trade_observation_cards.jsonl
    Entry events (context at entry) + exit events (r_multiple, outcome).
    Written unconditionally by forex/runner.py for every trade.

data/ai_trade_proposals.jsonl
    Richer entry-context features (regime label, ADX from classifier,
    signal_strength, n_open_positions).  Written since AI was enabled
    (2026-08-31).  Cards without a matching proposal still train on the
    subset of features available from the entry card alone.

Join key: account_env | strategy | symbol | entry_date (YYYY-MM-DD).

Label: r_multiple > 0  (binary: profitable=1, loss=0).

Model: scikit-learn GradientBoostingClassifier — handles tabular data,
small samples, and mixed feature types better than a linear baseline; no
extra dependencies beyond what signal_filter.py already uses.

Walk-forward split: train on chronologically first 70%, test on last 30%.
Consistent with the decomposition harness (ai/research/decompose.py).

Ships OFF:  config/ai.json  outcome_predictor.enabled: false
Gate:       won't train below MIN_SAMPLES closed, non-orphaned cards.
Output:     data/trade_outcome_model/model.pkl + report.json

Read-only w.r.t. trading state.  Never raises into a caller.
AST-locked by test: no imports of forex.runner, saxo_*, atos_runner, etc.
"""

from __future__ import annotations

import json
import os
import pickle
from datetime import datetime, timezone
from typing import Any

_ROOT      = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DATA_DIR  = os.path.join(_ROOT, "data")

CARDS_LOG      = os.path.join(_DATA_DIR, "trade_observation_cards.jsonl")
PROPOSALS_LOG  = os.path.join(_DATA_DIR, "ai_trade_proposals.jsonl")
MODEL_DIR      = os.path.join(_DATA_DIR, "trade_outcome_model")
MODEL_PKL      = os.path.join(MODEL_DIR, "model.pkl")
REPORT_JSON    = os.path.join(MODEL_DIR, "report.json")

MIN_SAMPLES = 100   # closed, non-orphaned trades needed before training

# ── Feature schema ────────────────────────────────────────────────────────────
# Keep these lists stable — they define the column order of every feature
# vector.  Changing them invalidates saved models (bump a version or retrain).

_NUM_FEATURES = [
    "rsi2",                    # RSI at entry (0–100; 0 when not available)
    "adx",                     # ADX from regime classifier
    "atr_pct",                 # ATR as % of price (volatility context)
    "signal_strength",         # strategy-stack agreement ratio [0–1]
    "n_open_positions",        # portfolio load at entry
    "recovery_to_cost_ratio",  # edge/cost; 0 when not computed
    "recovery_thin",           # 0/1: edge barely clears the 3× cost floor
    "day_of_week",             # 0=Mon … 4=Fri
    "pair_win_rate",           # historical WR for this pair+strategy [0–100]
    "pair_n_closed",           # sample size backing pair_win_rate
]

# Canonical lists — new values are bucketed into "unknown"/"UNKNOWN".
_STRATEGIES = [
    "rsi", "rsi_trend", "rsi_atr", "ema_trend",
    "bb_quality", "zscore_quality", "unknown",
]
_REGIMES = [
    "TRENDING_BULLISH", "TRENDING_BEARISH", "RANGING", "BREAKOUT",
    "HIGH_VOLATILITY", "LOW_VOLATILITY", "CHAOTIC", "UNKNOWN",
]
_DIRECTIONS = ["Buy", "Sell"]


# ── Internal helpers ──────────────────────────────────────────────────────────

def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v) if v is not None else default
    except (ValueError, TypeError):
        return default


def _one_hot(value: str | None, choices: list[str]) -> list[float]:
    v = str(value or "").strip() or choices[-1]
    if v not in choices:
        v = choices[-1]
    return [1.0 if v == c else 0.0 for c in choices]


def _extract_features(row: dict) -> list[float]:
    """Build a flat feature vector from a training/prediction row dict."""
    num = [_safe_float(row.get(f)) for f in _NUM_FEATURES]
    cat = (
        _one_hot(row.get("strategy"),    _STRATEGIES) +
        _one_hot(row.get("regime_label"), _REGIMES) +
        _one_hot(row.get("direction"),   _DIRECTIONS)
    )
    return num + cat


def _feature_names() -> list[str]:
    return (
        _NUM_FEATURES
        + [f"strategy_{s}" for s in _STRATEGIES]
        + [f"regime_{r}" for r in _REGIMES]
        + [f"dir_{d}" for d in _DIRECTIONS]
    )


# ── Data loading ──────────────────────────────────────────────────────────────

def _load_raw_data() -> list[dict]:
    """
    Join entry cards + exit cards + proposals into a list of training rows.
    Skips orphaned cards and entries without a matching exit (still open).
    Returns [] if any source file is missing — safe by design.
    """
    if not os.path.exists(CARDS_LOG):
        return []

    entries: dict[str, dict] = {}
    exits:   dict[str, dict] = {}

    try:
        with open(CARDS_LOG, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get("orphaned"):
                    continue
                event   = row.get("event")
                card_id = row.get("card_id", "")
                if event == "entry":
                    entries[card_id] = row
                elif event == "exit":
                    exits[card_id] = row
    except Exception:
        return []

    # Proposals: keyed by "account_env|strategy|symbol|YYYY-MM-DD"
    proposals: dict[str, dict] = {}
    if os.path.exists(PROPOSALS_LOG):
        try:
            with open(PROPOSALS_LOG, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    key = "|".join([
                        str(row.get("account_env", "")),
                        str(row.get("strategy_name", "")),
                        str(row.get("symbol", "")),
                        str(row.get("ts", ""))[:10],
                    ])
                    proposals[key] = row
        except Exception:
            pass

    rows: list[dict] = []
    for card_id, entry in entries.items():
        exit_card = exits.get(card_id)
        if exit_card is None:
            continue
        r_mult = exit_card.get("r_multiple")
        if r_mult is None:
            continue

        # card_id format: "env:strategy:symbol:ISO8601"
        # Split at most 3 times so the datetime suffix stays intact.
        parts    = card_id.split(":", 3)
        acct     = parts[0] if len(parts) > 0 else ""
        strategy = parts[1] if len(parts) > 1 else ""
        symbol   = parts[2] if len(parts) > 2 else ""
        date_str = parts[3][:10] if len(parts) > 3 else ""

        prop   = proposals.get(f"{acct}|{strategy}|{symbol}|{date_str}", {})
        regime = (prop.get("regime") or {}) if prop else {}

        # day_of_week from entry timestamp
        dow = 0
        try:
            ts_src = entry.get("timestamp", "") or date_str
            if ts_src:
                dt  = datetime.fromisoformat(ts_src.replace("Z", "+00:00"))
                dow = dt.weekday()
        except Exception:
            pass

        # pair_win_rate / pair_n_closed from the proposal's pair_history
        pair_hist = (prop.get("pair_history") or {}) if prop else {}

        # atr_pct: prefer proposal; fall back to entry-card atr / price
        atr_pct = _safe_float(prop.get("atr_pct") if prop else None)
        if atr_pct == 0.0:
            ep = _safe_float(entry.get("entry_price"), 1.0) or 1.0
            atr_pct = _safe_float(entry.get("atr_at_entry")) / ep * 100.0

        rows.append({
            "strategy":               strategy or "unknown",
            "regime_label":           regime.get("label", "UNKNOWN"),
            "direction":              entry.get("direction", "Buy"),
            "rsi2":                   _safe_float(prop.get("rsi2") if prop else None),
            "adx":                    _safe_float(regime.get("adx")),
            "atr_pct":                atr_pct,
            "signal_strength":        _safe_float(prop.get("signal_strength") if prop else None),
            "n_open_positions":       _safe_float(prop.get("n_open_positions", 0) if prop else 0),
            "recovery_to_cost_ratio": _safe_float(entry.get("recovery_to_cost_ratio")),
            "recovery_thin":          1.0 if entry.get("recovery_thin") else 0.0,
            "day_of_week":            float(dow),
            "pair_win_rate":          _safe_float(pair_hist.get("win_rate_pct")),
            "pair_n_closed":          _safe_float(pair_hist.get("n_closed")),
            # metadata (not used as features)
            "entry_date":             date_str,
            "r_multiple":             float(r_mult),
            "won":                    1 if float(r_mult) > 0 else 0,
        })

    return rows


# ── Model cache ───────────────────────────────────────────────────────────────

_model_cache:  dict | None = None
_cache_mtime:  float       = 0.0


def _load_model() -> dict | None:
    """Load model from disk; caches by mtime so retraining is picked up."""
    global _model_cache, _cache_mtime
    if not os.path.exists(MODEL_PKL):
        return None
    mtime = os.path.getmtime(MODEL_PKL)
    if _model_cache is not None and mtime == _cache_mtime:
        return _model_cache
    try:
        with open(MODEL_PKL, "rb") as f:
            _model_cache = pickle.load(f)
        _cache_mtime = mtime
        return _model_cache
    except Exception:
        return None


# ── Training ──────────────────────────────────────────────────────────────────

def train(min_samples: int = MIN_SAMPLES) -> dict:
    """
    Train the TOP on all available closed observation cards.

    Returns a report dict — keys include `trained` (bool), `reason` on skip,
    accuracy/precision metrics on train.  Saves model.pkl + report.json.
    Never raises.
    """
    try:
        return _train_inner(min_samples)
    except Exception as exc:
        return {"trained": False, "error": str(exc)}


def _train_inner(min_samples: int) -> dict:
    try:
        from sklearn.ensemble import GradientBoostingClassifier
        from sklearn.preprocessing import StandardScaler
        from sklearn.metrics import accuracy_score, precision_score
    except ImportError:
        return {"trained": False, "reason": "scikit-learn not installed"}

    rows = _load_raw_data()
    if len(rows) < min_samples:
        return {
            "trained":      False,
            "reason":       f"not enough data: {len(rows)}/{min_samples} closed trades",
            "n_available":  len(rows),
            "n_needed":     min_samples,
        }

    # Chronological split — first 70% train, last 30% held-out test.
    rows_sorted  = sorted(rows, key=lambda r: r.get("entry_date", ""))
    split        = max(1, int(len(rows_sorted) * 0.70))
    train_rows   = rows_sorted[:split]
    test_rows    = rows_sorted[split:]

    X_train = [_extract_features(r) for r in train_rows]
    y_train = [r["won"] for r in train_rows]
    X_test  = [_extract_features(r) for r in test_rows]
    y_test  = [r["won"] for r in test_rows]

    scaler     = StandardScaler()
    X_train_sc = scaler.fit_transform(X_train)
    X_test_sc  = scaler.transform(X_test)

    model = GradientBoostingClassifier(
        n_estimators=200, max_depth=3, learning_rate=0.05,
        subsample=0.8, min_samples_leaf=5, random_state=42,
    )
    model.fit(X_train_sc, y_train)

    y_pred    = model.predict(X_test_sc)
    acc       = accuracy_score(y_test, y_pred)
    win_prec  = precision_score(y_test, y_pred, pos_label=1,  zero_division=0.0)
    loss_prec = precision_score(y_test, y_pred, pos_label=0,  zero_division=0.0)
    base_wr   = sum(y_test) / len(y_test) if y_test else 0.0

    feat_names   = _feature_names()
    importances  = dict(zip(feat_names, model.feature_importances_))
    top_feats    = sorted(importances.items(), key=lambda kv: kv[1], reverse=True)[:10]

    obj = {
        "model":         model,
        "scaler":        scaler,
        "n_total":       len(rows),
        "n_train":       len(train_rows),
        "n_test":        len(test_rows),
        "feature_names": feat_names,
        "importances":   importances,
        "trained_at":    datetime.now(timezone.utc).isoformat(),
    }
    os.makedirs(MODEL_DIR, exist_ok=True)
    with open(MODEL_PKL, "wb") as f:
        pickle.dump(obj, f)

    # Invalidate cache so next predict() reloads
    global _model_cache, _cache_mtime
    _model_cache = obj
    _cache_mtime = os.path.getmtime(MODEL_PKL)

    report = {
        "trained":        True,
        "trained_at":     obj["trained_at"],
        "n_total":        len(rows),
        "n_train":        len(train_rows),
        "n_test":         len(test_rows),
        "test_accuracy":  round(float(acc),       4),
        "test_win_prec":  round(float(win_prec),  4),
        "test_loss_prec": round(float(loss_prec), 4),
        "base_win_rate":  round(float(base_wr),   4),
        "lift":           round(float(win_prec) - float(base_wr), 4),
        "top_features":   [(k, round(float(v), 4)) for k, v in top_feats],
    }
    with open(REPORT_JSON, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    return report


# ── Prediction ────────────────────────────────────────────────────────────────

def predict(proposal: dict) -> float | None:
    """
    Score a trade proposal.  Returns estimated win probability in [0, 1],
    or None if the model isn't available.  Never raises.
    """
    try:
        return _predict_inner(proposal)
    except Exception:
        return None


def _predict_inner(proposal: dict) -> float | None:
    obj = _load_model()
    if obj is None:
        return None

    regime    = (proposal.get("regime") or {})
    economics = (proposal.get("trade_economics") or {})
    pair_hist = (proposal.get("pair_history") or {})

    recovery_ratio = _safe_float(economics.get("recovery_0p5R_to_cost_ratio"))
    recovery_thin  = 1.0 if (0.0 < recovery_ratio < 3.0) else 0.0

    row = {
        "strategy":               proposal.get("strategy_name", "unknown"),
        "regime_label":           regime.get("label", "UNKNOWN"),
        "direction":              "Buy" if proposal.get("side") == "BUY" else "Sell",
        "rsi2":                   _safe_float(proposal.get("rsi2")),
        "adx":                    _safe_float(regime.get("adx")),
        "atr_pct":                _safe_float(proposal.get("atr_pct")),
        "signal_strength":        _safe_float(proposal.get("signal_strength")),
        "n_open_positions":       _safe_float(proposal.get("n_open_positions", 0)),
        "recovery_to_cost_ratio": recovery_ratio,
        "recovery_thin":          recovery_thin,
        "day_of_week":            float(datetime.now(timezone.utc).weekday()),
        "pair_win_rate":          _safe_float(pair_hist.get("win_rate_pct")),
        "pair_n_closed":          _safe_float(pair_hist.get("n_closed")),
    }

    X     = [_extract_features(row)]
    X_sc  = obj["scaler"].transform(X)
    probs = obj["model"].predict_proba(X_sc)

    classes = list(obj["model"].classes_)
    if 1 not in classes:
        return None
    win_idx = classes.index(1)
    return round(float(probs[0][win_idx]), 4)


# ── Status ────────────────────────────────────────────────────────────────────

def status() -> dict:
    """
    Return a dict describing the predictor's current state — safe to call
    at any time, even with no data or model on disk.
    """
    try:
        n_closed = len(_load_raw_data())
    except Exception:
        n_closed = 0

    report: dict = {}
    if os.path.exists(REPORT_JSON):
        try:
            with open(REPORT_JSON, encoding="utf-8") as f:
                report = json.load(f)
        except Exception:
            pass

    return {
        "n_closed_cards":   n_closed,
        "needed_for_train": max(0, MIN_SAMPLES - n_closed),
        "gate_cleared":     n_closed >= MIN_SAMPLES,
        "model_exists":     os.path.exists(MODEL_PKL),
        "trained_at":       report.get("trained_at", "")[:10] or None,
        "test_accuracy":    report.get("test_accuracy"),
        "test_win_prec":    report.get("test_win_prec"),
        "base_win_rate":    report.get("base_win_rate"),
        "lift":             report.get("lift"),
        "n_train":          report.get("n_train"),
        "n_test":           report.get("n_test"),
        "top_features":     report.get("top_features", []),
    }
