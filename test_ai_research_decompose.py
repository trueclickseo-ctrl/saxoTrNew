"""
test_ai_research_decompose.py -- unit tests for ai/research/decompose.py.

Tests:
  1. AST forbidden-import guard (no forex.runner, saxo_*, order calls)
  2. _bootstrap_ci reproducibility, edge cases
  3. gate pass / fail logic with synthetic Trade objects
  4. bucket_and_gate() structure and regime bucketing
  5. replay_trades() with injected price_data (no yfinance download)
  6. MODULE_IMPORT every strategy importable
  7. sweep() returns the correct feature set

Nothing here downloads live market data or places orders.
"""

import ast
import importlib
import os
import sys

import numpy as np
import pandas as pd
import pytest

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from ai.research.decompose import (
    Trade, Bucket, Verdict,
    _bootstrap_ci, _bucket_from, bucket_and_gate,
    replay_trades, sweep, MODULE_IMPORT,
    _MIN_BUCKET, _SWEEP_FEATURES, ROSTER,
)


# ─── helpers ──────────────────────────────────────────────────────────────────

def _make_trades(n: int, r: float = 1.0, di: float = 10.0,
                 strategy: str = "bb", half_split: float = 0.5) -> list[Trade]:
    """Synthetic trades with constant R and DI-spread, split into halves at half_split."""
    return [
        Trade(
            strategy=strategy, symbol="EURUSD", tier="core",
            direction="Buy", entry_date=f"2020-01-{i % 28 + 1:02d}",
            entry_price=1.1, r_price=0.01, r_multiple=r,
            mfe_r=abs(r), mae_r=-0.3, holding_bars=3, exit_reason="tp",
            half=1 if i < n * half_split else 2,
            di_spread=di,
        )
        for i in range(n)
    ]


def _mixed_di_trades(n_per_bucket: int = 60) -> list[Trade]:
    """Three di buckets: <=14 positive both halves, 15-25 neg 2nd half, >25 positive."""
    trades: list[Trade] = []
    for bucket_di, r_fn in [
        (10.0, lambda half: 1.0),           # <=14, always positive → PASS
        (20.0, lambda half: 1.0 if half == 1 else -1.0),  # 15-25, neg 2nd half → FAIL
        (30.0, lambda half: 0.8),           # >25, always positive → PASS
    ]:
        for i in range(n_per_bucket):
            half = 1 if i < n_per_bucket // 2 else 2
            trades.append(Trade(
                strategy="bb", symbol="EURUSD", tier="core",
                direction="Buy", entry_date=f"2020-01-01",
                entry_price=1.1, r_price=0.01,
                r_multiple=r_fn(half),
                mfe_r=1.0, mae_r=-0.3, holding_bars=3, exit_reason="tp",
                half=half, di_spread=bucket_di,
            ))
    return trades


def _make_price_data(sym: str = "EURUSD", n: int = 700, seed: int = 42) -> dict:
    """Synthetic OHLC DataFrame. Mean-reverting random walk so bb may generate signals."""
    rng = np.random.default_rng(seed)
    closes = [1.1]
    for _ in range(n - 1):
        closes.append(max(0.5, closes[-1] + rng.normal(0, 0.005)))
    closes = np.array(closes)
    idx = pd.date_range("2016-01-01", periods=n, freq="B")
    df = pd.DataFrame({
        "Open": closes * (1 + rng.uniform(-0.001, 0.001, n)),
        "High": closes + np.abs(rng.normal(0, 0.003, n)),
        "Low": closes - np.abs(rng.normal(0, 0.003, n)),
        "Close": closes,
    }, index=idx)
    return {sym: df}


# ─── 1. forbidden imports ─────────────────────────────────────────────────────

_FORBIDDEN = frozenset({
    "forex.runner", "saxo_api", "pnl_tracker", "housekeeping",
    "pnl_ledger", "place_order", "cancel_order", "amend_order",
})


def test_forbidden_imports_decompose():
    src = os.path.join(_ROOT, "ai", "research", "decompose.py")
    with open(src, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module)
    for bad in _FORBIDDEN:
        assert bad not in imported, f"decompose.py must not import {bad!r}"


# ─── 2. _bootstrap_ci ────────────────────────────────────────────────────────

def test_bootstrap_ci_fixed_seed_reproducible():
    rs = [1.0, -0.5, 0.8, -0.3, 1.2, 0.6] * 10
    lo1, hi1 = _bootstrap_ci(rs)
    lo2, hi2 = _bootstrap_ci(rs)
    assert lo1 == lo2 and hi1 == hi2, "CI must be reproducible (fixed seed 12345)"


def test_bootstrap_ci_excludes_zero_for_all_wins():
    rs = [1.0] * 60
    lo, hi = _bootstrap_ci(rs)
    assert lo > 0, "all-positive series: CI_low must be > 0"


def test_bootstrap_ci_nan_for_too_few_samples():
    lo, hi = _bootstrap_ci([1.0, 2.0])  # < 5 → NaN
    assert np.isnan(lo) and np.isnan(hi)


# ─── 3. gate pass / fail ──────────────────────────────────────────────────────

def test_gate_passes_when_both_halves_positive():
    trades = _make_trades(80, r=1.0, di=10.0)
    pred = lambda v: v is not None and v <= 14
    b = _bucket_from(trades, "di_spread", "<=14", pred)
    assert b is not None
    assert b.gate_pass is True
    assert b.first_half_avg_r > 0
    assert b.second_half_avg_r > 0
    assert b.ci_low > 0


def test_gate_fails_when_second_half_negative():
    trades = _make_trades(40, r=1.0, di=20.0)   # 1st half
    trades += _make_trades(40, r=-1.0, di=20.0)  # 2nd half (manual halves below)
    for i, t in enumerate(trades):
        t.half = 1 if i < 40 else 2
    pred = lambda v: v is not None and 14 < v <= 25
    b = _bucket_from(trades, "di_spread", "15-25", pred)
    assert b is not None
    assert b.gate_pass is False, "negative 2nd half must fail the gate"


def test_gate_excluded_when_bucket_too_small():
    trades = _make_trades(20, r=1.0, di=10.0)   # < _MIN_BUCKET (30)
    pred = lambda v: v is not None and v <= 14
    b = _bucket_from(trades, "di_spread", "<=14", pred)
    assert b is None, "bucket < _MIN_BUCKET should return None"


def test_gate_fails_when_ci_includes_zero():
    rng = np.random.default_rng(99)
    rs = rng.choice([-1.0, 1.0], size=60).tolist()   # 50/50 → avg ≈ 0
    trades = [
        Trade("bb", "EURUSD", "core", "Buy", "2020-01-01", 1.1, 0.01,
              r, 1.0, -0.3, 3, "tp", half=1 if i < 30 else 2, di_spread=10.0)
        for i, r in enumerate(rs)
    ]
    pred = lambda v: v is not None and v <= 14
    b = _bucket_from(trades, "di_spread", "<=14", pred)
    # CI should include zero for 50/50 series → gate_pass must be False
    assert b is not None
    assert b.gate_pass is False, "noisy 50/50 bucket should fail the gate"


# ─── 4. bucket_and_gate ───────────────────────────────────────────────────────

def test_bucket_and_gate_shape():
    v = bucket_and_gate(_mixed_di_trades(), "di_spread")
    assert isinstance(v, Verdict)
    assert v.strategy == "bb"
    assert v.feature == "di_spread"
    assert len(v.buckets) == 3


def test_bucket_and_gate_correct_pass_fail():
    v = bucket_and_gate(_mixed_di_trades(n_per_bucket=80), "di_spread")
    labels_pass = {b.label for b in v.passing}
    labels_fail = {b.label for b in v.buckets if not b.gate_pass}
    assert "<=14" in labels_pass, "<=14 bucket should PASS"
    assert ">25" in labels_pass, ">25 bucket should PASS"
    assert "15-25" in labels_fail, "15-25 bucket (neg 2nd half) should FAIL"


def test_regime_bucketing_one_per_label():
    regimes = ["TRENDING_BULLISH", "RANGING", "TRENDING_BEARISH"]
    trades = [
        Trade("rsi", "EURUSD", "core", "Buy", "2020-01-01", 1.1, 0.01,
              1.0, 1.0, -0.3, 3, "tp", half=1 if i < 150 else 2,
              regime=regimes[i % 3])
        for i in range(300)
    ]
    v = bucket_and_gate(trades, "regime")
    bucket_labels = {b.label for b in v.buckets}
    for reg in regimes:
        assert reg in bucket_labels, f"Expected a bucket for regime {reg!r}"


def test_bucket_and_gate_unknown_feature_raises():
    trades = _make_trades(50)
    with pytest.raises((ValueError, KeyError)):
        bucket_and_gate(trades, "nonexistent_feature_xyz")


# ─── 5. replay_trades (injected price_data) ───────────────────────────────────

def test_replay_returns_list_of_trades():
    pd_data = _make_price_data("EURUSD", n=700)
    trades = replay_trades("bb", price_data=pd_data)
    assert isinstance(trades, list)
    assert all(isinstance(t, Trade) for t in trades)


def test_replay_deterministic():
    pd_data = _make_price_data("EURUSD", n=700)
    t1 = replay_trades("bb", price_data=pd_data)
    t2 = replay_trades("bb", price_data=pd_data)
    assert len(t1) == len(t2), "replay must be deterministic"
    for a, b in zip(t1, t2):
        assert a.entry_date == b.entry_date
        assert abs(a.r_multiple - b.r_multiple) < 1e-9


def test_replay_halves_assigned_in_date_order():
    pd_data = _make_price_data("EURUSD", n=700)
    trades = replay_trades("bb", price_data=pd_data)
    if len(trades) < 4:
        pytest.skip("synthetic data produced too few trades to test halves")
    h1 = [t for t in trades if t.half == 1]
    h2 = [t for t in trades if t.half == 2]
    assert h1 and h2, "both halves must be non-empty"
    assert max(t.entry_date for t in h1) <= min(t.entry_date for t in h2), (
        "half 1 dates must all precede half 2 dates")


def test_replay_unknown_strategy_raises():
    pd_data = _make_price_data("EURUSD")
    with pytest.raises(ValueError, match="unknown strategy"):
        replay_trades("nonexistent_xyz", price_data=pd_data)


def test_replay_captures_entry_context_features():
    pd_data = _make_price_data("EURUSD", n=700)
    trades = replay_trades("bb", price_data=pd_data)
    if not trades:
        pytest.skip("synthetic data produced no trades")
    t = trades[0]
    assert hasattr(t, "di_spread")
    assert hasattr(t, "adx")
    assert hasattr(t, "regime")
    assert hasattr(t, "atr_pctile")


# ─── 6. MODULE_IMPORT registry ────────────────────────────────────────────────

def test_module_import_all_importable():
    for name, modpath in MODULE_IMPORT.items():
        mod = importlib.import_module(modpath)
        assert hasattr(mod, "generate_signals"), (
            f"{name}: {modpath} must expose generate_signals")


# ─── 7. sweep ────────────────────────────────────────────────────────────────

def test_sweep_features_for_bb_and_rsi():
    assert "di_spread" in _SWEEP_FEATURES["bb"]
    assert "regime" in _SWEEP_FEATURES["rsi"]
    assert "crossover_age" in _SWEEP_FEATURES["ema_trend"]


def test_sweep_returns_correct_number_of_verdicts():
    pd_data = _make_price_data("EURUSD", n=700)
    verdicts = sweep("bb", price_data=pd_data)
    expected = len(_SWEEP_FEATURES["bb"])
    assert len(verdicts) == expected, (
        f"sweep('bb') should return {expected} verdicts, got {len(verdicts)}")
    for v in verdicts:
        assert isinstance(v, Verdict)
        assert v.strategy == "bb"


def test_sweep_uses_injected_trades():
    """sweep() accepts pre-built trades to skip the replay download."""
    trades = _mixed_di_trades(n_per_bucket=80)
    verdicts = sweep("bb", trades=trades)
    assert any(v.feature == "di_spread" for v in verdicts)
