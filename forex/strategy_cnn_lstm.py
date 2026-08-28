"""
forex/strategy_cnn_lstm.py
--------------------------
FX Strategy 11 — CNN-LSTM Deep Learning Model.

Loads the pre-trained global model from data/cnn_lstm/ and generates
Buy/Sell signals when model confidence exceeds CONFIDENCE_THRESHOLD.

Model: Multi-scale CNN + BiLSTM + Self-Attention (see cnn_lstm_trainer.py)
Data:  Trained on 5 years of daily bars across all 34 FX pairs via yfinance

ENTRY:
  Long : softmax P(Buy)  >= CONFIDENCE_THRESHOLD  AND  ADX >= ADX_MIN
  Short: softmax P(Sell) >= CONFIDENCE_THRESHOLD  AND  ADX >= ADX_MIN
  Hold class is never traded.

EXIT (first hit):
  A. Model flips — opposite direction confidence >= CONFIDENCE_THRESHOLD
  B. ATR hard stop: ATR_STOP_MULT × ATR(14)
  C. Time stop: TIME_STOP_DAYS calendar days

SIZING: 1% equity risk per trade, ATR-based.

Before this strategy produces signals, train the model once:
    python -m forex.cnn_lstm_trainer --train

Status check:
    python -m forex.cnn_lstm_trainer --status
"""

from __future__ import annotations

import json
import logging
import os

import numpy as np
import pandas as pd
import torch

# Feature engineering lives in the trainer — single source of truth
from forex.cnn_lstm_trainer import (
    N_FEATURES, SEQ_LEN,
    Scaler, build_features, build_model,
    MODEL_PATH, SCALER_PATH, CONFIG_PATH,
)

logger = logging.getLogger("forex.strategy_cnn_lstm")

# ── Config ────────────────────────────────────────────────────────────────────
CONFIDENCE_THRESHOLD = 0.45  # softmax prob threshold to emit a signal
ADX_MIN              = 15    # minimum trend strength (skip choppy pairs)
ATR_PERIOD           = 14
ATR_STOP_MULT        = 2.5
RISK_PCT             = 0.0025  # was 0.01->0.005 on 2026-08-22, cut again 2026-08-24 -- see strategy.py's RISK_PCT comment
TIME_STOP_DAYS       = 15
LOT_ROUND            = 1_000

# Need 200 EMA warmup + SEQ_LEN lookback + ADX warmup
MIN_BARS = 220 + SEQ_LEN

# ── Model loading (lazy, cached) ──────────────────────────────────────────────
_cache: dict = {}


def _model_ready() -> bool:
    return os.path.exists(MODEL_PATH) and os.path.exists(SCALER_PATH)


def _load(device: torch.device | None = None):
    """Load model + scaler, caching in-memory. Thread-safe for single-process use."""
    global _cache
    if _cache:
        return _cache

    if not _model_ready():
        return None

    if device is None:
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    try:
        model = build_model()
        state = torch.load(MODEL_PATH, map_location=device, weights_only=True)
        model.load_state_dict(state)
        model.to(device)
        model.eval()

        scaler = Scaler.load(SCALER_PATH)
        _cache = {"model": model, "scaler": scaler, "device": device}
        logger.info(f"[cnn_lstm] Model loaded from {MODEL_PATH}")
        return _cache
    except Exception as exc:
        logger.warning(f"[cnn_lstm] Could not load model: {exc}")
        return None


# ── Technical indicators (shared with trainer helpers but kept minimal here) ──

def _atr(h: pd.Series, l: pd.Series, c: pd.Series, period: int = ATR_PERIOD) -> pd.Series:
    prev = c.shift(1)
    tr = pd.concat([h - l, (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def _adx_val(h: pd.Series, l: pd.Series, c: pd.Series, period: int = ATR_PERIOD) -> float:
    prev_c = c.shift(1); prev_h = h.shift(1); prev_l = l.shift(1)
    tr   = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    dm_p = ((h - prev_h).clip(lower=0)).where(h - prev_h > prev_l - l, 0)
    dm_m = ((prev_l - l).clip(lower=0)).where(prev_l - l > h - prev_h, 0)
    atr14 = tr.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    di_p  = 100 * dm_p.ewm(alpha=1/period, adjust=False, min_periods=period).mean() / (atr14 + 1e-10)
    di_m  = 100 * dm_m.ewm(alpha=1/period, adjust=False, min_periods=period).mean() / (atr14 + 1e-10)
    dx    = 100 * (di_p - di_m).abs() / (di_p + di_m + 1e-9)
    adx   = dx.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    return float(adx.iloc[-1])


# ── Inference helper ──────────────────────────────────────────────────────────

def _predict(df: pd.DataFrame) -> tuple[float, float, float] | None:
    """
    Run inference on the last SEQ_LEN bars of df.

    Returns (prob_sell, prob_hold, prob_buy) or None if model unavailable.
    """
    cache = _load()
    if cache is None:
        return None

    feat_df = build_features(df)
    feat_df = feat_df.dropna()
    if len(feat_df) < SEQ_LEN:
        return None

    seq = feat_df.values[-SEQ_LEN:].astype(np.float32)   # (SEQ_LEN, N_FEATURES)

    # Normalize
    scaler = cache["scaler"]
    seq    = (seq - scaler.mean_) / scaler.std_

    x  = torch.tensor(seq, dtype=torch.float32).unsqueeze(0).to(cache["device"])

    with torch.no_grad():
        logits = cache["model"](x)
        probs  = torch.softmax(logits, dim=1).squeeze(0).cpu().numpy()

    return float(probs[0]), float(probs[1]), float(probs[2])  # Sell, Hold, Buy


# ── Strategy interface ────────────────────────────────────────────────────────

def generate_signals(market_data: dict, open_symbols: set | None = None,
                     live_prices: dict | None = None) -> list[dict]:
    """
    Scan all pairs and return Buy/Sell signals where model confidence is
    above CONFIDENCE_THRESHOLD and ADX confirms trend strength.
    """
    if open_symbols is None:
        open_symbols = set()

    if not _model_ready():
        logger.debug("[cnn_lstm] No trained model found — run --train first")
        return []

    signals = []
    for sym, df in market_data.items():
        if sym in open_symbols or df is None or len(df) < MIN_BARS:
            continue

        h, l, c = df["High"], df["Low"], df["Close"]
        atr_val  = float(_atr(h, l, c).iloc[-1])
        adx_val  = _adx_val(h, l, c)
        today    = float(c.iloc[-1])

        if np.isnan(atr_val) or atr_val <= 0:
            continue

        probs = _predict(df)
        if probs is None:
            continue

        prob_sell, prob_hold, prob_buy = probs

        # Skip if ADX too low (sideways market — model less reliable)
        if adx_val < ADX_MIN:
            continue

        if prob_buy >= CONFIDENCE_THRESHOLD:
            signals.append({
                "symbol":     sym,
                "direction":  "Buy",
                "score":      prob_buy,
                "prob_buy":   round(prob_buy,  4),
                "prob_sell":  round(prob_sell, 4),
                "prob_hold":  round(prob_hold, 4),
                "adx":        round(adx_val, 1),
                "atr":        atr_val,
                "close":      today,
                "stop_price": today - ATR_STOP_MULT * atr_val,
            })

        elif prob_sell >= CONFIDENCE_THRESHOLD:
            signals.append({
                "symbol":     sym,
                "direction":  "Sell",
                "score":      prob_sell,
                "prob_buy":   round(prob_buy,  4),
                "prob_sell":  round(prob_sell, 4),
                "prob_hold":  round(prob_hold, 4),
                "adx":        round(adx_val, 1),
                "atr":        atr_val,
                "close":      today,
                "stop_price": today + ATR_STOP_MULT * atr_val,
            })

    signals.sort(key=lambda x: x["score"], reverse=True)
    return signals


def should_exit(position: dict, df: pd.DataFrame,
                calendar_days_held: int = 0) -> tuple[bool, str]:
    """
    Exit when:
      A. Model flips with high confidence  (primary)
      B. ATR hard stop triggered           (risk management)
      C. Time stop                         (cleanup)
    """
    if df is None or len(df) < MIN_BARS:
        return False, ""

    # C. Time stop
    if calendar_days_held >= TIME_STOP_DAYS:
        return True, f"time_stop ({calendar_days_held}d)"

    h, l, c  = df["High"], df["Low"], df["Close"]
    atr_val  = float(_atr(h, l, c).iloc[-1])
    stop_px  = position.get("stop_price", 0)
    is_long  = position.get("direction") == "Buy"
    high_now = float(h.iloc[-1])
    low_now  = float(l.iloc[-1])

    # B. ATR hard stop
    if is_long and stop_px > 0 and low_now <= stop_px:
        return True, f"hard_stop ({stop_px:.5f})"
    if not is_long and stop_px > 0 and high_now >= stop_px:
        return True, f"hard_stop ({stop_px:.5f})"

    # A. Model flip
    probs = _predict(df)
    if probs is not None:
        prob_sell, _, prob_buy = probs
        if is_long and prob_sell >= CONFIDENCE_THRESHOLD:
            return True, f"model_flip sell p={prob_sell:.2f}"
        if not is_long and prob_buy >= CONFIDENCE_THRESHOLD:
            return True, f"model_flip buy p={prob_buy:.2f}"

    return False, ""


def size_position(account_equity: float, atr: float,
                  min_units: float = LOT_ROUND, block_below_min: bool = False) -> int:
    """block_below_min: see forex/strategy_rsi.py's size_position()
    docstring (2026-08-28, explicit user decision, LIVE/LIVE_EUR only)."""
    risk_amount   = account_equity * RISK_PCT
    stop_distance = ATR_STOP_MULT * atr
    if stop_distance <= 0:
        return 0 if block_below_min else int(min_units)
    raw     = risk_amount / stop_distance
    floored = int(raw / min_units) * int(min_units)
    if floored < min_units:
        return 0 if block_below_min else int(min_units)
    return floored


def scan_summary(market_data: dict) -> list[dict]:
    """Return per-pair model probabilities for the dashboard."""
    rows = []
    model_ok = _model_ready()

    for sym, df in market_data.items():
        if df is None or len(df) < MIN_BARS:
            rows.append({"symbol": sym, "status": "no_data"})
            continue

        h, l, c = df["High"], df["Low"], df["Close"]
        atr_val = float(_atr(h, l, c).iloc[-1])
        adx_val = _adx_val(h, l, c)

        if not model_ok:
            rows.append({"symbol": sym, "status": "no_model",
                         "close": float(c.iloc[-1]), "atr": atr_val})
            continue

        probs = _predict(df)
        if probs is None:
            rows.append({"symbol": sym, "status": "predict_failed"})
            continue

        prob_sell, prob_hold, prob_buy = probs
        signal = (
            "BUY"  if prob_buy  >= CONFIDENCE_THRESHOLD and adx_val >= ADX_MIN else
            "SELL" if prob_sell >= CONFIDENCE_THRESHOLD and adx_val >= ADX_MIN else
            "hold"
        )
        rows.append({
            "symbol":    sym,
            "status":    "ok",
            "close":     round(float(c.iloc[-1]), 5),
            "p_buy":     round(prob_buy,  3),
            "p_sell":    round(prob_sell, 3),
            "p_hold":    round(prob_hold, 3),
            "adx":       round(adx_val, 1),
            "atr":       round(atr_val, 6),
            "signal":    signal,
        })

    rows.sort(key=lambda r: max(r.get("p_buy", 0), r.get("p_sell", 0)), reverse=True)
    return rows
