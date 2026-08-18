"""
forex/cnn_lstm_trainer.py
-------------------------
Train a CNN-LSTM deep learning model for FX daily bar direction prediction.

Key improvements over naive Conv1D→LSTM pseudo-code:
  1. Multi-scale CNN  — 3 parallel branches (kernel 3/7/14) capture daily,
     weekly, and bi-weekly patterns simultaneously
  2. Causal convolution — zero look-ahead bias in Conv layers
  3. Bidirectional LSTM — sequences processed both directions
  4. Self-attention  — learns which time steps matter most
  5. Batch normalization — training stability without large LR
  6. Walk-forward validation — time-series safe, NEVER shuffle
  7. ATR-normalized targets — only label SIGNIFICANT moves Buy/Sell
  8. Global model — trained on all 34 pairs together (more data, better
     generalization than 34 per-pair models)

Architecture:
  Input: (batch, 60, 16)   ← 60 daily bars × 16 features
    ↓  transpose
  MultiScaleCNN:
    Branch-A  CausalConv(k=3)×2 → BN → ReLU
    Branch-B  CausalConv(k=7)   → BN → ReLU
    Branch-C  CausalConv(k=14)  → BN → ReLU
    → Concat(192 ch) → Dropout(0.2) → MaxPool(2)
    ↓  transpose back
  Bidirectional LSTM(128 × 2 = 256) → Dropout(0.3)
  Self-Attention pooling → context(256)
  Dense: 256→128(BN,ReLU) → Dropout(0.25) → 64(ReLU) → 3(Softmax)
  Labels: 0=Sell  1=Hold  2=Buy

Usage:
    python -m forex.cnn_lstm_trainer --train
    python -m forex.cnn_lstm_trainer --train --pairs EURUSD GBPUSD USDJPY
    python -m forex.cnn_lstm_trainer --backtest
    python -m forex.cnn_lstm_trainer --status
    python -m forex.cnn_lstm_trainer --train --epochs 50   # quick test
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import time
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader, Dataset

logger = logging.getLogger("forex.cnn_lstm")

# ── Paths ──────────────────────────────────────────────────────────────────────
_ROOT     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATA_DIR = os.path.join(_ROOT, "data", "cnn_lstm")

MODEL_PATH  = os.path.join(_DATA_DIR, "model.pt")
SCALER_PATH = os.path.join(_DATA_DIR, "scaler.json")
CONFIG_PATH = os.path.join(_DATA_DIR, "config.json")
REPORT_PATH = os.path.join(_DATA_DIR, "report.json")

# ── Hyperparameters ─────────────────────────────────────────────────────────────
SEQ_LEN         = 60       # trading days of lookback per sample
N_FEATURES      = 16       # feature count (see build_features())
PRED_HORIZON    = 5        # days ahead to predict
ATR_THRESHOLD   = 0.5      # moves > 0.5×ATR% are labelled Buy/Sell (else Hold)
CONFIDENCE      = 0.58     # minimum softmax prob to emit a signal

TRAIN_INIT_BARS = 600      # walk-forward: minimum bars to start first training fold
FOLD_STEP       = 120      # walk-forward: test window size (≈6 months)

BATCH_SIZE   = 128
MAX_EPOCHS   = 150
LR           = 1e-3
GRAD_CLIP    = 1.0
EARLY_STOP   = 20    # patience (epochs without val-loss improvement)

# ── Feature engineering ─────────────────────────────────────────────────────────

def _atr(h: pd.Series, l: pd.Series, c: pd.Series, period: int = 14) -> pd.Series:
    prev = c.shift(1)
    tr = pd.concat([h - l, (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def _rsi(c: pd.Series, period: int = 14) -> pd.Series:
    delta = c.diff()
    gain  = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs    = gain / (loss + 1e-10)
    return 100 - 100 / (1 + rs)


def _adx(h: pd.Series, l: pd.Series, c: pd.Series, period: int = 14) -> pd.Series:
    prev_c = c.shift(1); prev_h = h.shift(1); prev_l = l.shift(1)
    tr   = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    dm_p = ((h - prev_h).clip(lower=0)).where(h - prev_h > prev_l - l, 0)
    dm_m = ((prev_l - l).clip(lower=0)).where(prev_l - l > h - prev_h, 0)
    atr14 = tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    di_p  = 100 * dm_p.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / (atr14 + 1e-10)
    di_m  = 100 * dm_m.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / (atr14 + 1e-10)
    dx    = 100 * (di_p - di_m).abs() / (di_p + di_m + 1e-9)
    return dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def _macd_hist(c: pd.Series, fast: int = 12, slow: int = 26, sig: int = 9) -> pd.Series:
    ema_fast = c.ewm(span=fast, adjust=False).mean()
    ema_slow = c.ewm(span=slow, adjust=False).mean()
    macd     = ema_fast - ema_slow
    signal   = macd.ewm(span=sig, adjust=False).mean()
    return macd - signal


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute 16 normalized features from OHLC data.

    All features are engineered to:
      - use only data up to and including row t (no look-ahead)
      - be roughly stationary and on similar scales

    Returns a DataFrame aligned to df's index. Rows with NaN are NOT dropped
    here — the caller drops them when slicing sequences.
    """
    h, l, c = df["High"], df["Low"], df["Close"]

    feat: dict[str, pd.Series] = {}

    # ── Returns (log-returns are additive, better for neural nets) ────────────
    log_c = np.log(c)
    feat["ret_1d"]  = log_c.diff(1)
    feat["ret_5d"]  = log_c.diff(5)
    feat["ret_20d"] = log_c.diff(20)

    # ── EMA trend ratios ──────────────────────────────────────────────────────
    ema5   = c.ewm(span=5,   adjust=False).mean()
    ema20  = c.ewm(span=20,  adjust=False).mean()
    ema50  = c.ewm(span=50,  adjust=False).mean()
    ema200 = c.ewm(span=200, adjust=False).mean()

    feat["ema5_20"]   = ema5  / (ema20 + 1e-10) - 1     # fast momentum
    feat["ema20_50"]  = ema20 / (ema50 + 1e-10) - 1     # medium trend
    feat["price_200"] = c     / (ema200 + 1e-10) - 1    # macro bias

    # ── Oscillators ───────────────────────────────────────────────────────────
    feat["rsi"] = _rsi(c, 14) / 100.0 - 0.5    # centred: range ≈ [-0.5, +0.5]
    feat["adx"] = _adx(h, l, c, 14) / 100.0    # range ≈ [0, 1]

    # ── Volatility ────────────────────────────────────────────────────────────
    atr = _atr(h, l, c, 14)
    feat["atr_pct"]   = atr / (c + 1e-10)                          # normalized vol
    feat["vol_ratio"] = atr / (atr.rolling(20).mean() + 1e-10) - 1 # rel. vol

    # ── Mean-reversion ────────────────────────────────────────────────────────
    bb_mid = c.rolling(20).mean()
    bb_std = c.rolling(20).std(ddof=1) + 1e-10
    feat["bb_pct_b"] = (c - bb_mid) / (2 * bb_std)    # BB position (±1 = at band)
    feat["zscore"]   = (c - bb_mid) / bb_std            # 20-day z-score

    # ── Channel position ──────────────────────────────────────────────────────
    high30 = c.rolling(30).max()
    low30  = c.rolling(30).min()
    feat["donchian"] = (c - low30) / (high30 - low30 + 1e-10)  # range [0, 1]

    # ── MACD histogram (normalized by ATR) ───────────────────────────────────
    feat["macd_h"] = _macd_hist(c) / (atr + 1e-10)

    # ── Day-of-week encoding (cyclical) ──────────────────────────────────────
    if isinstance(df.index, pd.DatetimeIndex):
        dow = df.index.dayofweek.astype(float)
    else:
        dow = pd.Series(range(len(df))) % 5
    feat["day_sin"] = np.sin(2 * math.pi * dow / 5)
    feat["day_cos"] = np.cos(2 * math.pi * dow / 5)

    result = pd.DataFrame(feat, index=df.index)
    # Clip extreme values to ±5σ to reduce outlier impact on training
    for col in result.columns:
        mu  = result[col].median()
        std = result[col].std() + 1e-10
        result[col] = result[col].clip(mu - 5 * std, mu + 5 * std)

    return result


def build_labels(df: pd.DataFrame, horizon: int = PRED_HORIZON,
                 threshold: float = ATR_THRESHOLD) -> pd.Series:
    """
    ATR-normalized forward-return labels.

    0 = Sell  (fwd return < –threshold × ATR%)
    1 = Hold  (within threshold band)
    2 = Buy   (fwd return > +threshold × ATR%)
    """
    c   = df["Close"]
    h, l = df["High"], df["Low"]
    atr = _atr(h, l, c, 14)

    fwd_ret   = c.shift(-horizon) / c - 1
    atr_band  = threshold * atr / (c + 1e-10)

    labels = pd.Series(1, index=df.index, dtype=int)  # default: Hold
    labels[fwd_ret >  atr_band] = 2   # Buy
    labels[fwd_ret < -atr_band] = 0   # Sell

    return labels


# ── Feature normalization ───────────────────────────────────────────────────────

class Scaler:
    """Z-score scaler fitted per feature (no sklearn required)."""

    def __init__(self) -> None:
        self.mean_: Optional[np.ndarray] = None
        self.std_:  Optional[np.ndarray] = None

    def fit(self, X: np.ndarray) -> "Scaler":
        # X shape: (samples, seq_len, features)
        flat = X.reshape(-1, X.shape[-1])
        self.mean_ = flat.mean(axis=0)
        self.std_  = flat.std(axis=0) + 1e-8
        return self

    def transform(self, X: np.ndarray) -> np.ndarray:
        return (X - self.mean_) / self.std_

    def fit_transform(self, X: np.ndarray) -> np.ndarray:
        return self.fit(X).transform(X)

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump({"mean": self.mean_.tolist(), "std": self.std_.tolist()}, f)

    @classmethod
    def load(cls, path: str) -> "Scaler":
        with open(path) as f:
            d = json.load(f)
        sc = cls()
        sc.mean_ = np.array(d["mean"], dtype=np.float32)
        sc.std_  = np.array(d["std"],  dtype=np.float32)
        return sc


# ── Dataset ─────────────────────────────────────────────────────────────────────

class FxDataset(Dataset):
    """Sliding-window sequences of features + labels for one FX pair."""

    def __init__(self, features: np.ndarray, labels: np.ndarray,
                 seq_len: int = SEQ_LEN) -> None:
        self.X = torch.tensor(features, dtype=torch.float32)
        self.y = torch.tensor(labels,   dtype=torch.long)
        self.seq_len = seq_len

    def __len__(self) -> int:
        return max(0, len(self.X) - self.seq_len)

    def __getitem__(self, idx: int):
        x = self.X[idx : idx + self.seq_len]           # (seq_len, features)
        y = self.y[idx + self.seq_len - 1]              # label at LAST bar
        return x, y


def build_dataset_arrays(pair_dfs: dict[str, pd.DataFrame],
                         scaler: Scaler | None = None,
                         fit_scaler: bool = False,
                         max_date: str | None = None
                         ) -> tuple[np.ndarray, np.ndarray, Scaler]:
    """
    Convert a dict of OHLC DataFrames into (X, y, scaler) arrays.

    pair_dfs : {symbol: DataFrame}
    max_date : if set, only use bars on or before this date (walk-forward)
    """
    all_feat, all_lbl = [], []

    for sym, df in pair_dfs.items():
        if df is None or len(df) < 250:
            continue
        if max_date:
            df = df[df.index <= pd.Timestamp(max_date)]
        if len(df) < 250:
            continue

        feat_df  = build_features(df)
        lbl_s    = build_labels(df)

        aligned  = feat_df.join(lbl_s.rename("label"), how="inner")
        aligned  = aligned.dropna()
        # Drop last PRED_HORIZON rows (labels use future close)
        aligned  = aligned.iloc[:-PRED_HORIZON]
        if len(aligned) < SEQ_LEN + 10:
            continue

        all_feat.append(aligned[feat_df.columns].values.astype(np.float32))
        all_lbl.append(aligned["label"].values.astype(np.int64))

    if not all_feat:
        raise ValueError("No valid pair data after feature engineering")

    # Concatenate all pairs (global model)
    feat_cat = np.concatenate(all_feat, axis=0)
    lbl_cat  = np.concatenate(all_lbl,  axis=0)

    if fit_scaler or scaler is None:
        # Reshape to (N, seq_len, F) for scaler fitting — approximate here
        # by fitting on the flat feature matrix (equivalent since it's per-column)
        sc = Scaler()
        sc.fit(feat_cat.reshape(1, -1, N_FEATURES).repeat(1, 1, 1)
               if feat_cat.shape[0] < N_FEATURES else
               feat_cat.reshape(-1, 1, N_FEATURES).squeeze(1).reshape(1, -1, N_FEATURES))
        # Simplified: fit on flat matrix rows
        sc.mean_ = feat_cat.mean(axis=0)
        sc.std_  = feat_cat.std(axis=0) + 1e-8
    else:
        sc = scaler

    feat_sc = (feat_cat - sc.mean_) / sc.std_

    # Build sequences
    # For each pair, build sequences independently to avoid cross-pair contamination
    all_X, all_y = [], []
    offset = 0
    for feat_arr, lbl_arr in zip(all_feat, all_lbl):
        n = len(feat_arr)
        feat_norm = (feat_arr - sc.mean_) / sc.std_
        for i in range(n - SEQ_LEN):
            all_X.append(feat_norm[i : i + SEQ_LEN])
            all_y.append(lbl_arr[i + SEQ_LEN - 1])

    X = np.stack(all_X).astype(np.float32)
    y = np.array(all_y, dtype=np.int64)
    return X, y, sc


# ── Model architecture ───────────────────────────────────────────────────────────

class CausalConv1d(nn.Module):
    """Conv1d with left-only padding so output at t uses only inputs ≤ t."""
    def __init__(self, in_ch: int, out_ch: int, kernel: int) -> None:
        super().__init__()
        self._pad  = kernel - 1
        self.conv  = nn.Conv1d(in_ch, out_ch, kernel, padding=0)
        self.bn    = nn.BatchNorm1d(out_ch)

    def forward(self, x: torch.Tensor) -> torch.Tensor:   # (B, C, L)
        x = F.pad(x, (self._pad, 0))
        return F.relu(self.bn(self.conv(x)))


class SelfAttention(nn.Module):
    """Additive self-attention pooling over the sequence dimension."""
    def __init__(self, hidden: int) -> None:
        super().__init__()
        self.score = nn.Linear(hidden, 1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:   # (B, L, H)
        w = torch.softmax(self.score(x), dim=1)            # (B, L, 1)
        return (x * w).sum(dim=1)                           # (B, H)


class CnnLstm(nn.Module):
    """
    Multi-scale CNN + Bidirectional LSTM + Self-Attention classifier.

    Input:  (batch, seq_len, n_features)
    Output: (batch, 3)  — raw logits for Sell/Hold/Buy
    """
    def __init__(self,
                 n_features: int = N_FEATURES,
                 n_classes:  int = 3,
                 cnn_ch:     int = 64,
                 lstm_h:     int = 128,
                 dropout:    float = 0.2) -> None:
        super().__init__()

        # Three parallel CNN branches (channels-first inside)
        self.branch_a1 = CausalConv1d(n_features, cnn_ch, kernel=3)
        self.branch_a2 = CausalConv1d(cnn_ch,     cnn_ch, kernel=3)
        self.branch_b  = CausalConv1d(n_features, cnn_ch, kernel=7)
        self.branch_c  = CausalConv1d(n_features, cnn_ch, kernel=14)

        merged_ch = cnn_ch * 3   # 192

        self.cnn_drop = nn.Dropout(dropout)
        self.pool     = nn.MaxPool1d(2)

        # Bidirectional LSTM (batch_first)
        # dropout on LSTM only applies between stacked layers (num_layers > 1);
        # use the explicit lstm_drop layer below instead
        self.lstm     = nn.LSTM(merged_ch, lstm_h, batch_first=True,
                                bidirectional=True)
        self.lstm_drop = nn.Dropout(dropout + 0.1)

        lstm_out = lstm_h * 2   # 256

        self.attn = SelfAttention(lstm_out)

        self.fc1  = nn.Linear(lstm_out, 128)
        self.bn1  = nn.BatchNorm1d(128)
        self.drop1 = nn.Dropout(dropout + 0.05)

        self.fc2   = nn.Linear(128, 64)
        self.fc_out = nn.Linear(64, n_classes)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: (B, L, F)  — batch_first
        xt = x.transpose(1, 2)           # → (B, F, L) for Conv1d

        a  = self.branch_a2(self.branch_a1(xt))
        b  = self.branch_b(xt)
        c  = self.branch_c(xt)

        merged = torch.cat([a, b, c], dim=1)    # (B, 192, L)
        merged = self.pool(self.cnn_drop(merged))  # (B, 192, L/2)
        merged = merged.transpose(1, 2)           # → (B, L/2, 192) for LSTM

        lstm_out, _ = self.lstm(merged)           # (B, L/2, 256)
        lstm_out    = self.lstm_drop(lstm_out)

        ctx = self.attn(lstm_out)                 # (B, 256)

        h = F.relu(self.bn1(self.fc1(ctx)))
        h = self.drop1(h)
        h = F.relu(self.fc2(h))
        return self.fc_out(h)                     # (B, 3)


def build_model(**kwargs) -> CnnLstm:
    return CnnLstm(**kwargs)


# ── Training loop ───────────────────────────────────────────────────────────────

def _class_weights(y: np.ndarray) -> torch.Tensor:
    counts = np.bincount(y, minlength=3).astype(float)
    total  = counts.sum()
    w      = total / (3 * counts + 1e-8)
    return torch.tensor(w / w.sum() * 3, dtype=torch.float32)  # normalized


def train_model(X_train: np.ndarray, y_train: np.ndarray,
                X_val:   np.ndarray, y_val:   np.ndarray,
                device:  torch.device,
                epochs:  int = MAX_EPOCHS,
                lr:      float = LR) -> tuple[CnnLstm, dict]:
    """
    Train a CnnLstm model with early stopping.

    Returns (model, history_dict).
    """
    model = build_model().to(device)

    cw   = _class_weights(y_train).to(device)
    loss_fn  = nn.CrossEntropyLoss(weight=cw)
    optimizer = torch.optim.Adam(model.parameters(), lr=lr, weight_decay=1e-4)
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
        optimizer, factor=0.5, patience=8, min_lr=1e-5)

    train_ds  = FxDataset(X_train, y_train, seq_len=1)  # sequences already built
    val_ds    = FxDataset(X_val,   y_val,   seq_len=1)

    # Sequences are pre-built (shape already (N, SEQ_LEN, F)), pass seq_len=1 trick:
    # Actually rewrite: just build TensorDatasets directly
    X_tr_t = torch.tensor(X_train, dtype=torch.float32)
    y_tr_t = torch.tensor(y_train, dtype=torch.long)
    X_va_t = torch.tensor(X_val,   dtype=torch.float32)
    y_va_t = torch.tensor(y_val,   dtype=torch.long)

    tr_loader = DataLoader(
        torch.utils.data.TensorDataset(X_tr_t, y_tr_t),
        batch_size=BATCH_SIZE, shuffle=True, drop_last=True)
    va_loader = DataLoader(
        torch.utils.data.TensorDataset(X_va_t, y_va_t),
        batch_size=BATCH_SIZE * 2, shuffle=False)

    history = {"train_loss": [], "val_loss": [], "val_acc": []}
    best_val_loss = float("inf")
    best_state    = None
    patience_cnt  = 0

    for epoch in range(1, epochs + 1):
        # ── Train ──────────────────────────────────────────────────────────────
        model.train()
        tr_loss, n_batch = 0.0, 0
        for xb, yb in tr_loader:
            xb, yb = xb.to(device), yb.to(device)
            optimizer.zero_grad()
            logits = model(xb)
            loss   = loss_fn(logits, yb)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), GRAD_CLIP)
            optimizer.step()
            tr_loss  += loss.item()
            n_batch  += 1

        # ── Validate ───────────────────────────────────────────────────────────
        model.eval()
        va_loss, correct, total = 0.0, 0, 0
        with torch.no_grad():
            for xb, yb in va_loader:
                xb, yb  = xb.to(device), yb.to(device)
                logits  = model(xb)
                va_loss += loss_fn(logits, yb).item()
                preds   = logits.argmax(dim=1)
                correct += (preds == yb).sum().item()
                total   += len(yb)

        avg_tr  = tr_loss / max(n_batch, 1)
        avg_va  = va_loss / max(len(va_loader), 1)
        acc     = correct / max(total, 1)
        scheduler.step(avg_va)

        history["train_loss"].append(avg_tr)
        history["val_loss"].append(avg_va)
        history["val_acc"].append(acc)

        if epoch % 10 == 0 or epoch <= 5:
            lr_now = optimizer.param_groups[0]["lr"]
            logger.info(f"  epoch {epoch:3d}/{epochs}  "
                        f"tr={avg_tr:.4f}  va={avg_va:.4f}  "
                        f"acc={acc:.3f}  lr={lr_now:.2e}")

        if avg_va < best_val_loss - 1e-5:
            best_val_loss = avg_va
            best_state    = {k: v.cpu().clone() for k, v in model.state_dict().items()}
            patience_cnt  = 0
        else:
            patience_cnt += 1
            if patience_cnt >= EARLY_STOP:
                logger.info(f"  Early stop at epoch {epoch} (patience={EARLY_STOP})")
                break

    if best_state:
        model.load_state_dict(best_state)
    return model, history


# ── Walk-forward evaluation ─────────────────────────────────────────────────────

def walk_forward_backtest(pair_dfs: dict[str, pd.DataFrame],
                          device: torch.device) -> list[dict]:
    """
    Time-series-safe walk-forward backtest.

    For each fold:
      - Train on all bars before fold_start
      - Evaluate on next FOLD_STEP bars
    Reports accuracy, per-class precision, and signal rate per fold.
    """
    # Find common date range across all pairs
    dates = sorted({d for df in pair_dfs.values() if df is not None
                    for d in df.index})
    if len(dates) < TRAIN_INIT_BARS + FOLD_STEP:
        logger.warning("Not enough data for walk-forward validation")
        return []

    fold_starts = range(TRAIN_INIT_BARS, len(dates) - FOLD_STEP, FOLD_STEP)
    results = []

    for fold_idx, train_end_idx in enumerate(fold_starts):
        train_end = str(dates[train_end_idx])[:10]
        test_end  = str(dates[min(train_end_idx + FOLD_STEP - 1, len(dates) - 1)])[:10]
        logger.info(f"\n[fold {fold_idx + 1}]  train ≤ {train_end}  test through {test_end}")

        try:
            X_all, y_all, sc = build_dataset_arrays(
                pair_dfs, fit_scaler=True, max_date=train_end)
        except ValueError as exc:
            logger.warning(f"  Fold {fold_idx + 1} skipped: {exc}")
            continue

        if len(X_all) < 200:
            logger.warning(f"  Fold {fold_idx + 1}: only {len(X_all)} training sequences — skipping")
            continue

        # Train / val split within training fold (last 15% = val)
        split  = int(len(X_all) * 0.85)
        X_tr, y_tr = X_all[:split], y_all[:split]
        X_va, y_va = X_all[split:], y_all[split:]

        model, _ = train_model(X_tr, y_tr, X_va, y_va, device, epochs=80)

        # Build test set (bars between train_end and test_end)
        test_dfs = {}
        for sym, df in pair_dfs.items():
            if df is None:
                continue
            mask = (df.index > pd.Timestamp(train_end)) & \
                   (df.index <= pd.Timestamp(test_end))
            if mask.sum() >= SEQ_LEN + PRED_HORIZON:
                # Include warmup bars before test window for feature calculation
                warmup_start_idx = df.index.get_loc(
                    df.index[df.index <= pd.Timestamp(train_end)][-1]) - SEQ_LEN - 220
                warmup_start_idx = max(0, warmup_start_idx)
                test_dfs[sym] = df.iloc[warmup_start_idx:]

        if not test_dfs:
            continue

        try:
            X_te, y_te, _ = build_dataset_arrays(
                test_dfs, scaler=sc, fit_scaler=False, max_date=test_end)
        except ValueError:
            continue

        model.eval()
        preds_list = []
        with torch.no_grad():
            for i in range(0, len(X_te), 256):
                xb     = torch.tensor(X_te[i:i+256], dtype=torch.float32).to(device)
                logits = model(xb)
                probs  = torch.softmax(logits, dim=1)
                preds_list.append(probs.cpu().numpy())
        probs_arr = np.concatenate(preds_list, axis=0)
        preds_cls = probs_arr.argmax(axis=1)

        acc       = (preds_cls == y_te).mean()
        buy_pr    = _precision(preds_cls, y_te, cls=2)
        sell_pr   = _precision(preds_cls, y_te, cls=0)
        sig_rate  = (probs_arr.max(axis=1) >= CONFIDENCE).mean()

        fold_result = {
            "fold":        fold_idx + 1,
            "train_end":   train_end,
            "test_end":    test_end,
            "n_train":     len(X_tr),
            "n_test":      len(X_te),
            "accuracy":    round(float(acc), 4),
            "buy_precision":  round(float(buy_pr), 4),
            "sell_precision": round(float(sell_pr), 4),
            "signal_rate":    round(float(sig_rate), 4),
        }
        results.append(fold_result)
        logger.info(
            f"  → acc={acc:.3f}  buy_prec={buy_pr:.3f}  "
            f"sell_prec={sell_pr:.3f}  signal_rate={sig_rate:.2%}")

    return results


def _precision(preds: np.ndarray, labels: np.ndarray, cls: int) -> float:
    predicted_cls = preds == cls
    if predicted_cls.sum() == 0:
        return 0.0
    return float((labels[predicted_cls] == cls).mean())


# ── Full training pipeline ──────────────────────────────────────────────────────

def _fetch_yf_data(yf_ticker: str, period: str = "5y") -> pd.DataFrame | None:
    """Download daily OHLC from yfinance. Returns None on failure."""
    try:
        import yfinance as yf
        df = yf.download(yf_ticker, period=period, interval="1d",
                         auto_adjust=True, progress=False)
        if df is None or len(df) < 250:
            return None
        df = df[["Open", "High", "Low", "Close"]].copy()
        df.index = pd.to_datetime(df.index)
        df = df.sort_index()
        df = df.dropna()
        return df
    except Exception as exc:
        logger.warning(f"  yfinance download failed for {yf_ticker}: {exc}")
        return None


def train(pairs: list[str] | None = None,
          epochs: int = MAX_EPOCHS,
          period: str = "5y") -> dict:
    """
    Full training pipeline.

    1. Download yfinance data for requested pairs
    2. Build features + labels
    3. Walk-forward backtest (reports expected live performance)
    4. Train final model on 100% of data
    5. Save model, scaler, report to data/cnn_lstm/
    """
    from forex.universe import PAIRS as ALL_PAIRS

    os.makedirs(_DATA_DIR, exist_ok=True)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    logger.info(f"Device: {device}")

    # Select pairs to train on
    all_symbols = {p["symbol"]: p["yf_ticker"] for p in ALL_PAIRS}
    if pairs:
        symbols = {s: all_symbols[s] for s in pairs if s in all_symbols}
    else:
        symbols = all_symbols

    logger.info(f"Downloading data for {len(symbols)} pairs...")
    pair_dfs: dict[str, pd.DataFrame] = {}
    for sym, ticker in symbols.items():
        df = _fetch_yf_data(ticker, period=period)
        if df is not None:
            pair_dfs[sym] = df
            logger.info(f"  {sym}: {len(df)} bars")
        else:
            logger.warning(f"  {sym}: no data")

    if not pair_dfs:
        raise RuntimeError("No data downloaded — check internet connection and yfinance version")

    logger.info(f"\nLoaded {len(pair_dfs)}/{len(symbols)} pairs")

    # Walk-forward backtest (honest performance estimate)
    logger.info("\n=== Walk-Forward Backtest ===")
    wf_results = walk_forward_backtest(pair_dfs, device)

    # Final model: train on ALL data
    logger.info("\n=== Final Model Training (all data) ===")
    X, y, sc = build_dataset_arrays(pair_dfs, fit_scaler=True)

    split = int(len(X) * 0.9)
    X_tr, y_tr = X[:split], y[:split]
    X_va, y_va = X[split:], y[split:]

    logger.info(f"Train sequences: {len(X_tr)}  Val: {len(X_va)}")
    logger.info(f"Class distribution: {np.bincount(y_tr)}")

    final_model, history = train_model(X_tr, y_tr, X_va, y_va, device, epochs=epochs)

    # Save
    torch.save(final_model.state_dict(), MODEL_PATH)
    sc.save(SCALER_PATH)

    config = {
        "seq_len":     SEQ_LEN,
        "n_features":  N_FEATURES,
        "pred_horizon": PRED_HORIZON,
        "atr_threshold": ATR_THRESHOLD,
        "confidence":  CONFIDENCE,
        "trained_at":  datetime.now().isoformat(),
        "n_pairs":     len(pair_dfs),
        "n_train_seq": len(X_tr),
        "epochs_run":  len(history["train_loss"]),
    }
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)

    report: dict = {
        "config": config,
        "final_val_accuracy": round(history["val_acc"][-1], 4) if history["val_acc"] else None,
        "walk_forward": wf_results,
    }
    if wf_results:
        accs = [r["accuracy"] for r in wf_results]
        buys = [r["buy_precision"] for r in wf_results]
        sells = [r["sell_precision"] for r in wf_results]
        report["wf_summary"] = {
            "mean_accuracy":      round(float(np.mean(accs)), 4),
            "mean_buy_precision": round(float(np.mean(buys)), 4),
            "mean_sell_precision": round(float(np.mean(sells)), 4),
            "n_folds":            len(wf_results),
        }
    with open(REPORT_PATH, "w") as f:
        json.dump(report, f, indent=2)

    logger.info(f"\n✓ Model saved  → {MODEL_PATH}")
    logger.info(f"✓ Scaler saved → {SCALER_PATH}")
    logger.info(f"✓ Report saved → {REPORT_PATH}")

    if wf_results:
        s = report["wf_summary"]
        logger.info(
            f"\nWalk-forward summary ({s['n_folds']} folds):\n"
            f"  Accuracy:       {s['mean_accuracy']:.3f}\n"
            f"  Buy precision:  {s['mean_buy_precision']:.3f}\n"
            f"  Sell precision: {s['mean_sell_precision']:.3f}"
        )
    return report


def status() -> None:
    """Print current model status."""
    if not os.path.exists(CONFIG_PATH):
        print("No model found. Run: python -m forex.cnn_lstm_trainer --train")
        return

    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    print(f"Model trained:    {cfg.get('trained_at', '?')[:19]}")
    print(f"Pairs:            {cfg.get('n_pairs', '?')}")
    print(f"Training seqs:    {cfg.get('n_train_seq', '?')}")
    print(f"Epochs run:       {cfg.get('epochs_run', '?')}")

    if os.path.exists(REPORT_PATH):
        with open(REPORT_PATH) as f:
            rpt = json.load(f)
        if "wf_summary" in rpt:
            s = rpt["wf_summary"]
            print(f"\nWalk-forward ({s['n_folds']} folds):")
            print(f"  Accuracy:       {s['mean_accuracy']:.3f}")
            print(f"  Buy precision:  {s['mean_buy_precision']:.3f}")
            print(f"  Sell precision: {s['mean_sell_precision']:.3f}")
        print(f"\nFinal val accuracy: {rpt.get('final_val_accuracy', '?')}")


# ── CLI entry point ─────────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="CNN-LSTM FX trainer")
    parser.add_argument("--train",    action="store_true", help="Train new model")
    parser.add_argument("--backtest", action="store_true", help="Walk-forward backtest only")
    parser.add_argument("--status",   action="store_true", help="Show model status")
    parser.add_argument("--pairs",    nargs="+",  help="Specific pairs (default: all 34)")
    parser.add_argument("--epochs",   type=int,  default=MAX_EPOCHS, help=f"Max epochs (default {MAX_EPOCHS})")
    parser.add_argument("--period",   default="5y", help="yfinance period (default 5y)")
    args = parser.parse_args()

    if args.status or not (args.train or args.backtest):
        status()
        return

    if args.train:
        train(pairs=args.pairs, epochs=args.epochs, period=args.period)
    elif args.backtest:
        from forex.universe import PAIRS as ALL_PAIRS
        symbols = {p["symbol"]: p["yf_ticker"] for p in ALL_PAIRS}
        if args.pairs:
            symbols = {s: symbols[s] for s in args.pairs if s in symbols}
        pair_dfs = {}
        for sym, ticker in symbols.items():
            df = _fetch_yf_data(ticker, period=args.period)
            if df is not None:
                pair_dfs[sym] = df
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        results = walk_forward_backtest(pair_dfs, device)
        for r in results:
            print(json.dumps(r, indent=2))


if __name__ == "__main__":
    main()
