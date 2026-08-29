"""
Advanced CNN-LSTM Master
Selective inference wrapper around the existing trained CNN-LSTM model.

Important: this does NOT retrain or recalibrate the model. It improves trade
selection around the existing model; any claimed win-rate improvement must be
validated out-of-sample.

Public interface is compatible with strategy_cnn_lstm.py.

INTEGRATION (2026-08-30, user request): registered as STRATEGIES key
"advanced_cnn_lstm_master" in forex/runner.py, in parallel with the
original "cnn_lstm" (forex/strategy_cnn_lstm.py, UNTOUCHED) for an A/B
comparison. SIM ONLY -- not in either LIVE allowlist. Uncapped slots
(mirrors "cnn_lstm"). MIN_BARS 282 fits the 500-bar fetch; standard
trailing_stop_update hook, no runner exit-loop change. Loads the SAME
pre-trained model file as "cnn_lstm" (its own module-level _cache dict,
no collision); does NOT retrain. Momentum-pre-filtered like the original.
"""
from __future__ import annotations

import logging
import os
import numpy as np
import pandas as pd
import torch

from forex.cnn_lstm_trainer import (
    SEQ_LEN, Scaler, build_features, build_model,
    MODEL_PATH, SCALER_PATH,
)

logger = logging.getLogger("forex.strategy_advanced_cnn_lstm")

# Model-selection gates
CONFIDENCE_THRESHOLD = 0.52
MIN_CLASS_MARGIN = 0.08
MAX_HOLD_PROB = 0.38

# Market-regime confirmation
ADX_PERIOD = 14
ADX_MIN = 20
ADX_RISE_BARS = 3
EMA_FAST = 20
EMA_SLOW = 50

# Risk / lifecycle
ATR_PERIOD = 14
ATR_STOP_MULT = 2.5
RISK_PCT = 0.0025
TIME_STOP_DAYS = 15
LOT_ROUND = 1_000

# Reject unusually quiet / extreme volatility regimes
VOL_LOOKBACK = 252
VOL_PCT_MIN = 0.20
VOL_PCT_MAX = 0.90

MIN_BARS = max(220 + SEQ_LEN, VOL_LOOKBACK + 30)
_cache: dict = {}


def _model_ready() -> bool:
    return os.path.exists(MODEL_PATH) and os.path.exists(SCALER_PATH)


def _load(device: torch.device | None = None):
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
        logger.info("[advanced_cnn_lstm] model loaded")
        return _cache
    except Exception as exc:
        logger.warning("[advanced_cnn_lstm] load failed: %s", exc)
        return None


def _atr(h, l, c, period=ATR_PERIOD):
    prev = c.shift(1)
    tr = pd.concat([h-l, (h-prev).abs(), (l-prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False, min_periods=period).mean()


def _adx(h, l, c, period=ADX_PERIOD):
    up, down = h.diff(), -l.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    prev = c.shift(1)
    tr = pd.concat([h-l, (h-prev).abs(), (l-prev).abs()], axis=1).max(axis=1)

    def wild(x):
        return x.ewm(alpha=1/period, adjust=False, min_periods=period).mean()

    atr = wild(tr)
    pdi = 100 * wild(plus_dm) / (atr + 1e-10)
    mdi = 100 * wild(minus_dm) / (atr + 1e-10)
    dx = 100 * (pdi-mdi).abs() / (pdi+mdi+1e-10)
    return wild(dx), pdi, mdi


def _predict(df):
    cache = _load()
    if cache is None:
        return None
    feat = build_features(df).dropna()
    if len(feat) < SEQ_LEN:
        return None
    seq = feat.values[-SEQ_LEN:].astype(np.float32)
    scaler = cache["scaler"]
    seq = (seq - scaler.mean_) / np.maximum(scaler.std_, 1e-10)
    x = torch.tensor(seq, dtype=torch.float32).unsqueeze(0).to(cache["device"])
    with torch.no_grad():
        logits = cache["model"](x)
        p = torch.softmax(logits, dim=1).squeeze(0).cpu().numpy()
    return float(p[0]), float(p[1]), float(p[2])  # sell, hold, buy


def _trade_decision(prob_sell, prob_hold, prob_buy):
    """Return direction, winning confidence, margin, or (None,...)."""
    if prob_hold > MAX_HOLD_PROB:
        return None, 0.0, 0.0

    if prob_buy >= prob_sell:
        winner, runner = prob_buy, max(prob_sell, prob_hold)
        if winner >= CONFIDENCE_THRESHOLD and winner-runner >= MIN_CLASS_MARGIN:
            return "Buy", winner, winner-runner
    else:
        winner, runner = prob_sell, max(prob_buy, prob_hold)
        if winner >= CONFIDENCE_THRESHOLD and winner-runner >= MIN_CLASS_MARGIN:
            return "Sell", winner, winner-runner
    return None, 0.0, 0.0


def generate_signals(market_data: dict, open_symbols: set | None = None,
                     live_prices: dict | None = None) -> list[dict]:
    open_symbols = open_symbols or set()
    if not _model_ready():
        return []

    signals = []
    for sym, df in market_data.items():
        if sym in open_symbols or df is None or len(df) < MIN_BARS:
            continue
        try:
            h, l, c = df["High"], df["Low"], df["Close"]
            atr = _atr(h, l, c)
            adx, pdi, mdi = _adx(h, l, c)
            ema20 = c.ewm(span=EMA_FAST, adjust=False).mean()
            ema50 = c.ewm(span=EMA_SLOW, adjust=False).mean()
            atr_pct = atr.rolling(VOL_LOOKBACK, min_periods=100).rank(pct=True)

            i = -1
            vals = [atr.iloc[i], adx.iloc[i], pdi.iloc[i], mdi.iloc[i], atr_pct.iloc[i]]
            if not all(np.isfinite(v) for v in vals) or atr.iloc[i] <= 0:
                continue
            if adx.iloc[i] < ADX_MIN:
                continue
            if not (VOL_PCT_MIN <= atr_pct.iloc[i] <= VOL_PCT_MAX):
                continue

            # Avoid entering after trend strength has already deteriorated.
            if len(adx) >= ADX_RISE_BARS + 1 and adx.iloc[-1] < adx.iloc[-1-ADX_RISE_BARS]:
                continue

            probs = _predict(df)
            if probs is None:
                continue
            ps, ph, pb = probs
            direction, confidence, margin = _trade_decision(ps, ph, pb)
            if direction is None:
                continue

            close = float(c.iloc[i])
            cur_atr = float(atr.iloc[i])

            # Directional market confirmation must agree with model direction.
            if direction == "Buy":
                if not (close > ema20.iloc[i] > ema50.iloc[i] and pdi.iloc[i] > mdi.iloc[i]):
                    continue
                stop = close - ATR_STOP_MULT * cur_atr
            else:
                if not (close < ema20.iloc[i] < ema50.iloc[i] and mdi.iloc[i] > pdi.iloc[i]):
                    continue
                stop = close + ATR_STOP_MULT * cur_atr

            # Rank by confidence and separation, not raw probability alone.
            score = float(confidence + 0.75 * margin + 0.01 * float(adx.iloc[i]))
            signals.append({
                "symbol": sym, "direction": direction, "score": score,
                "prob_buy": round(pb, 4), "prob_sell": round(ps, 4),
                "prob_hold": round(ph, 4), "confidence": round(confidence, 4),
                "class_margin": round(margin, 4),
                "adx": round(float(adx.iloc[i]), 1),
                "atr": cur_atr, "close": close, "stop_price": float(stop),
                "strategy": "advanced_cnn_lstm_master",
            })
        except Exception:
            continue

    return sorted(signals, key=lambda x: x["score"], reverse=True)


def should_exit(position: dict, df: pd.DataFrame,
                calendar_days_held: int = 0) -> tuple[bool, str]:
    if df is None or len(df) < MIN_BARS:
        return False, ""

    if calendar_days_held >= TIME_STOP_DAYS:
        return True, f"time_stop ({calendar_days_held}d)"

    h, l, c = df["High"], df["Low"], df["Close"]
    stop = float(position.get("stop_price", 0))
    is_long = position.get("direction") == "Buy"

    if is_long and stop > 0 and float(l.iloc[-1]) <= stop:
        return True, f"hard_stop ({stop:.5f})"
    if not is_long and stop > 0 and float(h.iloc[-1]) >= stop:
        return True, f"hard_stop ({stop:.5f})"

    # Only exit on a genuinely decisive opposite model signal.
    probs = _predict(df)
    if probs is not None:
        ps, ph, pb = probs
        direction, conf, margin = _trade_decision(ps, ph, pb)
        if is_long and direction == "Sell":
            return True, f"model_flip sell p={conf:.2f} margin={margin:.2f}"
        if not is_long and direction == "Buy":
            return True, f"model_flip buy p={conf:.2f} margin={margin:.2f}"

    return False, ""


def trailing_stop_update(current_stop: float, current_price: float,
                         current_atr: float, direction: str = "Buy") -> float:
    band = ATR_STOP_MULT * current_atr
    return max(current_stop, current_price-band) if direction == "Buy" else min(current_stop, current_price+band)


def size_position(account_equity: float, atr: float,
                  min_units: float = LOT_ROUND, risk_pct: float | None = None,
                  block_below_min: bool = False) -> int:
    risk = account_equity * (RISK_PCT if risk_pct is None else risk_pct)
    distance = ATR_STOP_MULT * atr
    if distance <= 0:
        return 0 if block_below_min else int(min_units)
    units = int((risk / distance) // min_units) * int(min_units)
    return units if units >= min_units else (0 if block_below_min else int(min_units))


def scan_summary(market_data: dict) -> list[dict]:
    rows = []
    for sym, df in market_data.items():
        if df is None or len(df) < MIN_BARS:
            rows.append({"symbol": sym, "status": "no_data"})
            continue
        try:
            h, l, c = df["High"], df["Low"], df["Close"]
            atr = _atr(h, l, c)
            adx, _, _ = _adx(h, l, c)
            probs = _predict(df) if _model_ready() else None
            if probs is None:
                rows.append({"symbol": sym, "status": "no_model"})
                continue
            ps, ph, pb = probs
            direction, conf, margin = _trade_decision(ps, ph, pb)
            rows.append({"symbol": sym, "status": "ok",
                "close": round(float(c.iloc[-1]), 5),
                "p_buy": round(pb, 3), "p_sell": round(ps, 3), "p_hold": round(ph, 3),
                "confidence": round(conf, 3), "class_margin": round(margin, 3),
                "adx": round(float(adx.iloc[-1]), 1), "atr": round(float(atr.iloc[-1]), 6),
                "signal": direction or "hold"})
        except Exception:
            rows.append({"symbol": sym, "status": "error"})
    return sorted(rows, key=lambda r: r.get("confidence", 0), reverse=True)
