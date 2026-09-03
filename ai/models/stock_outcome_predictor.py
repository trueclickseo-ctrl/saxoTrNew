"""
ai/models/stock_outcome_predictor.py
--------------------------------------
Stock Trade Outcome Predictor — GradientBoosting classifier trained on
data/stock_observation_cards.jsonl to predict whether a new stock entry
will produce r_multiple > 0.

Sibling of ai/models/trade_outcome_predictor.py (forex).

Sources
-------
data/stock_observation_cards.jsonl
    Entry events (rsi_at_entry, stop, target, strategy, ticker) + exit
    events (r_multiple). Written by ai/features/stock_cards.py.

data/ai_trade_proposals.jsonl
    Richer features logged by ai/features/stock_proposal.py since AI was
    enabled (2026-09-02): regime label, ADX, daily_vol_pct, reward_risk_ratio,
    n_open_positions, pair_history. Joined by the same key shape as forex.

Join key: account_env | strategy | symbol | entry_date (YYYY-MM-DD).

Label: r_multiple > 0  (binary: profitable=1, loss=0).

Feature differences vs forex TOP
----------------------------------
  rsi14            RSI(14) — stocks use a 14-bar period, not RSI(2)
  stop_pct         stop distance as % of entry price (risk width)
  target_pct       target (sma20) distance as % of entry (reward)
  risk_reward      target_pct / stop_pct (implied R:R)
  daily_vol_pct    20-day realised daily-vol % (ATR proxy from proposal)
  NO recovery_to_cost_ratio / recovery_thin  (forex-specific concepts)

Gate: 50 closed non-orphaned cards (stocks trade less frequently).
Ships OFF: config/ai.json stock_outcome_predictor.enabled: false

Read-only w.r.t. trading state. Never raises into a caller.
AST-locked by test: no imports of forex.runner, saxo_*, atos_runner, etc.
"""

from __future__ import annotations

import json
import os
import pickle
from datetime import datetime, timezone
from typing import Any

_ROOT     = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DATA_DIR = os.path.join(_ROOT, "data")

STOCK_CARDS_LOG  = os.path.join(_DATA_DIR, "stock_observation_cards.jsonl")
PROPOSALS_LOG    = os.path.join(_DATA_DIR, "ai_trade_proposals.jsonl")
MODEL_DIR        = os.path.join(_DATA_DIR, "stock_outcome_model")
MODEL_PKL        = os.path.join(MODEL_DIR, "model.pkl")
REPORT_JSON      = os.path.join(MODEL_DIR, "report.json")

MIN_SAMPLES = 50   # closed non-orphaned stock trades needed before training

# ── Feature schema ─────────────────────────────────────────────────────────────

_NUM_FEATURES = [
    "rsi14",          # RSI(14) at entry; 0 for US Blend (momentum, not RSI-gated)
    "adx",            # ADX from regime classifier (0 if no proposal match)
    "daily_vol_pct",  # 20-day realised daily-vol % (ATR proxy)
    "stop_pct",       # abs(entry - stop) / entry * 100 (risk width)
    "target_pct",     # abs(target - entry) / entry * 100 (reward width)
    "risk_reward",    # target_pct / stop_pct; 0 if stop or target missing
    "n_open_positions",
    "day_of_week",    # 0=Mon … 4=Fri
    "ticker_win_rate",   # historical WR for this ticker+strategy [0–100]
    "ticker_n_closed",   # sample size backing ticker_win_rate
]

_STRATEGIES = ["us_reversion", "us_blend", "unknown"]
_REGIMES    = [
    "TRENDING_BULLISH", "TRENDING_BEARISH", "RANGING", "BREAKOUT",
    "HIGH_VOLATILITY", "LOW_VOLATILITY", "CHAOTIC", "UNKNOWN",
]
_DIRECTIONS = ["Buy", "Sell"]

# Only train on the current active stock strategies.
_ACTIVE_STRATEGIES: frozenset[str] = frozenset({"us_reversion", "us_blend"})


# ── Internal helpers ───────────────────────────────────────────────────────────

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
    num = [_safe_float(row.get(f)) for f in _NUM_FEATURES]
    cat = (
        _one_hot(row.get("strategy"),     _STRATEGIES) +
        _one_hot(row.get("regime_label"), _REGIMES) +
        _one_hot(row.get("direction"),    _DIRECTIONS)
    )
    return num + cat


def _feature_names() -> list[str]:
    return (
        _NUM_FEATURES
        + [f"strategy_{s}" for s in _STRATEGIES]
        + [f"regime_{r}" for r in _REGIMES]
        + [f"dir_{d}" for d in _DIRECTIONS]
    )


# ── Data loading ───────────────────────────────────────────────────────────────

def _load_raw_data() -> list[dict]:
    """
    Join entry cards + exit cards + proposals into training rows.
    Only includes closed, non-orphaned trades from active strategies.
    Returns [] safely when any source is missing.
    """
    if not os.path.exists(STOCK_CARDS_LOG):
        return []

    entries: dict[str, dict] = {}
    exits:   dict[str, dict] = {}

    try:
        with open(STOCK_CARDS_LOG, encoding="utf-8") as f:
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

    # Proposals keyed by "account_env|strategy|symbol|YYYY-MM-DD"
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
                    # Only stock proposals (market=="equity") to avoid key
                    # collisions with forex proposals on the same ticker date.
                    if row.get("market") != "equity":
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

        # card_id format: "account_env:strategy:ticker:entry_date"
        parts    = card_id.split(":", 3)
        acct     = parts[0] if len(parts) > 0 else ""
        strategy = parts[1] if len(parts) > 1 else ""
        ticker   = parts[2] if len(parts) > 2 else ""
        date_str = parts[3][:10] if len(parts) > 3 else ""

        if strategy not in _ACTIVE_STRATEGIES:
            continue

        prop   = proposals.get(f"{acct}|{strategy}|{ticker}|{date_str}", {})
        regime = (prop.get("regime") or {}) if prop else {}
        econ   = (prop.get("trade_economics") or {}) if prop else {}
        pair_hist = (prop.get("pair_history") or {}) if prop else {}

        # day_of_week from entry timestamp
        dow = 0
        try:
            ts_src = entry.get("timestamp", "") or date_str
            if ts_src:
                dt  = datetime.fromisoformat(ts_src.replace("Z", "+00:00"))
                dow = dt.weekday()
        except Exception:
            pass

        # stop_pct and target_pct — prefer proposal economics; fall back to
        # entry card fields directly.
        ep    = _safe_float(entry.get("entry_price"), 1.0) or 1.0
        stop  = _safe_float(entry.get("current_stop"))
        stop_pct = abs(ep - stop) / ep * 100.0 if stop > 0 else 0.0

        # target_pct from entry card sma20_target if available
        target_raw = _safe_float(entry.get("sma20_target"))
        target_pct = abs(target_raw - ep) / ep * 100.0 if target_raw > 0 else 0.0

        # risk_reward: prefer proposal reward_risk_ratio, else compute
        rr = _safe_float(econ.get("reward_risk_ratio"))
        if rr == 0.0 and stop_pct > 0 and target_pct > 0:
            rr = target_pct / stop_pct

        rows.append({
            "strategy":         strategy or "unknown",
            "regime_label":     regime.get("label", "UNKNOWN"),
            "direction":        entry.get("direction", "Buy"),
            "rsi14":            _safe_float(entry.get("rsi_at_entry") or
                                            (prop.get("rsi2") if prop else None)),
            "adx":              _safe_float(regime.get("adx")),
            "daily_vol_pct":    _safe_float(prop.get("atr_pct") if prop else None),
            "stop_pct":         stop_pct,
            "target_pct":       target_pct,
            "risk_reward":      rr,
            "n_open_positions": _safe_float(prop.get("n_open_positions", 0) if prop else 0),
            "day_of_week":      float(dow),
            "ticker_win_rate":  _safe_float(pair_hist.get("win_rate_pct")),
            "ticker_n_closed":  _safe_float(pair_hist.get("n_closed")),
            # metadata
            "entry_date": date_str,
            "r_multiple": float(r_mult),
            "won":        1 if float(r_mult) > 0 else 0,
        })

    return rows


# ── Model cache ────────────────────────────────────────────────────────────────

_model_cache: dict | None = None
_cache_mtime: float       = 0.0


def _load_model() -> dict | None:
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


# ── Training ───────────────────────────────────────────────────────────────────

def train(min_samples: int = MIN_SAMPLES) -> dict:
    """
    Train on all available closed stock observation cards.
    Returns a report dict. Never raises.
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
        from sklearn.utils.class_weight import compute_sample_weight
    except ImportError:
        return {"trained": False, "reason": "scikit-learn not installed"}

    rows = _load_raw_data()
    if len(rows) < min_samples:
        return {
            "trained":     False,
            "reason":      f"not enough data: {len(rows)}/{min_samples} closed stock trades",
            "n_available": len(rows),
            "n_needed":    min_samples,
        }

    rows_sorted = sorted(rows, key=lambda r: r.get("entry_date", ""))
    split       = max(1, int(len(rows_sorted) * 0.70))
    train_rows  = rows_sorted[:split]
    test_rows   = rows_sorted[split:]

    X_train = [_extract_features(r) for r in train_rows]
    y_train = [r["won"] for r in train_rows]
    X_test  = [_extract_features(r) for r in test_rows]
    y_test  = [r["won"] for r in test_rows]

    scaler      = StandardScaler()
    X_train_sc  = scaler.fit_transform(X_train)
    X_test_sc   = scaler.transform(X_test)

    sample_weights = compute_sample_weight("balanced", y_train)

    model = GradientBoostingClassifier(
        n_estimators=200, max_depth=3, learning_rate=0.05,
        subsample=0.8, min_samples_leaf=3, random_state=42,
    )
    model.fit(X_train_sc, y_train, sample_weight=sample_weights)

    y_pred    = model.predict(X_test_sc)
    acc       = accuracy_score(y_test, y_pred)
    win_prec  = precision_score(y_test, y_pred, pos_label=1, zero_division=0.0)
    loss_prec = precision_score(y_test, y_pred, pos_label=0, zero_division=0.0)
    base_wr   = sum(y_test) / len(y_test) if y_test else 0.0

    feat_names  = _feature_names()
    importances = dict(zip(feat_names, model.feature_importances_))
    top_feats   = sorted(importances.items(), key=lambda kv: kv[1], reverse=True)[:10]

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


# ── Prediction ─────────────────────────────────────────────────────────────────

def predict(proposal: dict) -> float | None:
    """
    Score a stock trade proposal. Returns win probability [0, 1] or None.
    Never raises.
    """
    try:
        return _predict_inner(proposal)
    except Exception:
        return None


def _predict_inner(proposal: dict) -> float | None:
    obj = _load_model()
    if obj is None:
        return None

    regime = (proposal.get("regime") or {})
    econ   = (proposal.get("trade_economics") or {})
    pair_h = (proposal.get("pair_history") or {})

    ep     = _safe_float(proposal.get("entry_price"), 1.0) or 1.0
    stop   = _safe_float(proposal.get("stop_loss"))
    tp     = _safe_float(proposal.get("take_profit"))
    stop_pct   = abs(ep - stop) / ep * 100.0 if stop > 0 else 0.0
    target_pct = abs(tp - ep) / ep * 100.0 if tp > 0 else 0.0
    rr = _safe_float(econ.get("reward_risk_ratio"))
    if rr == 0.0 and stop_pct > 0 and target_pct > 0:
        rr = target_pct / stop_pct

    row = {
        "strategy":         proposal.get("strategy_name", "unknown"),
        "regime_label":     regime.get("label", "UNKNOWN"),
        "direction":        "Buy" if proposal.get("side") == "BUY" else "Sell",
        "rsi14":            _safe_float(proposal.get("rsi2")),
        "adx":              _safe_float(regime.get("adx")),
        "daily_vol_pct":    _safe_float(proposal.get("atr_pct")),
        "stop_pct":         stop_pct,
        "target_pct":       target_pct,
        "risk_reward":      rr,
        "n_open_positions": _safe_float(proposal.get("n_open_positions", 0)),
        "day_of_week":      float(datetime.now(timezone.utc).weekday()),
        "ticker_win_rate":  _safe_float(pair_h.get("win_rate_pct")),
        "ticker_n_closed":  _safe_float(pair_h.get("n_closed")),
    }

    X    = [_extract_features(row)]
    X_sc = obj["scaler"].transform(X)
    probs = obj["model"].predict_proba(X_sc)

    classes = list(obj["model"].classes_)
    if 1 not in classes:
        return None
    return round(float(probs[0][classes.index(1)]), 4)


# ── Status ─────────────────────────────────────────────────────────────────────

def status() -> dict:
    """Current state of the stock predictor. Safe to call at any time."""
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
