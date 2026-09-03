"""
Tests for forex/strategy_rsi_atr.py -- the ATR-percentile-gated RSI(2) twin.

Run:  py -3 -m pytest test_strategy_rsi_atr.py -v
"""
import sys
import os
import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import forex.strategy_rsi as _rsi
import forex.strategy_rsi_atr as rsi_atr


# ── helpers ────────────────────────────────────────────────────────────────

def _make_df(n=700, seed=7, atr_scale=1.0):
    """Synthetic OHLC with controllable ATR level."""
    rng = np.random.default_rng(seed)
    close = 1.0 + np.cumsum(rng.normal(0, 0.002, n))
    close = np.maximum(close, 0.5)
    spread = np.abs(rng.normal(0, 0.001 * atr_scale, n)) + 1e-5
    high = close + spread
    low  = close - spread
    idx  = pd.date_range("2013-01-01", periods=n, freq="B")
    return pd.DataFrame({"Open": close, "High": high, "Low": low, "Close": close}, index=idx)


def _make_trending_df(n=700, seed=8):
    """Trending OHLC to produce RSI(2) pullback signals reliably."""
    rng = np.random.default_rng(seed)
    # steady uptrend with noisy daily bars so RSI sometimes dips below 10
    close = 1.0 + np.linspace(0, 0.5, n) + rng.normal(0, 0.005, n)
    close = np.maximum(close, 0.5)
    spread = np.abs(rng.normal(0, 0.002, n)) + 1e-5
    high = close + spread
    low  = close - spread
    idx  = pd.date_range("2013-01-01", periods=n, freq="B")
    return pd.DataFrame({"Open": close, "High": high, "Low": low, "Close": close}, index=idx)


# ── module-level invariants ────────────────────────────────────────────────

def test_constants_match_rsi():
    assert rsi_atr.RSI_PERIOD     == _rsi.RSI_PERIOD
    assert rsi_atr.RSI_OVERSOLD   == _rsi.RSI_OVERSOLD
    assert rsi_atr.RSI_OVERBOUGHT == _rsi.RSI_OVERBOUGHT
    assert rsi_atr.ATR_STOP_MULT  == _rsi.ATR_STOP_MULT
    assert rsi_atr.RISK_PCT       == _rsi.RISK_PCT
    assert rsi_atr.MAX_POSITIONS  == _rsi.MAX_POSITIONS
    assert rsi_atr.TIME_STOP_DAYS == _rsi.TIME_STOP_DAYS


def test_delegates_should_exit():
    assert rsi_atr.should_exit is _rsi.should_exit


def test_delegates_size_position():
    assert rsi_atr.size_position is _rsi.size_position


def test_delegates_trailing_stop_update():
    assert rsi_atr.trailing_stop_update is _rsi.trailing_stop_update


def test_not_in_live_allowlists():
    from forex.runner import LIVE_ALLOWED_STRATEGIES, LIVE_EUR_ALLOWED_STRATEGIES
    assert "rsi_atr" not in LIVE_ALLOWED_STRATEGIES
    assert "rsi_atr" not in LIVE_EUR_ALLOWED_STRATEGIES


def test_in_sim_active_strategies():
    from forex.runner import SIM_ACTIVE_STRATEGIES
    assert "rsi_atr" in SIM_ACTIVE_STRATEGIES


def test_in_profit_ladder_strategies():
    from forex.runner import PROFIT_LADDER_STRATEGIES
    assert "rsi_atr" in PROFIT_LADDER_STRATEGIES


def test_in_strategies_registry():
    from forex.runner import STRATEGIES
    assert "rsi_atr" in STRATEGIES


# ── _cur_atr_pctile ────────────────────────────────────────────────────────

def test_atr_pctile_returns_float():
    df = _make_df(n=400)
    p = rsi_atr._cur_atr_pctile(df["High"], df["Low"], df["Close"])
    assert isinstance(p, float)
    assert 0.0 <= p <= 1.0


def test_atr_pctile_insufficient_history_returns_zero():
    df = _make_df(n=30)
    p = rsi_atr._cur_atr_pctile(df["High"], df["Low"], df["Close"])
    assert p == 0.0


def test_high_atr_has_high_pctile():
    """A series whose last ATR value is very large should have high percentile."""
    df = _make_df(n=400, atr_scale=1.0)
    # Spike the last bar's range to force a very high ATR
    df_spike = df.copy()
    df_spike.iloc[-1, df_spike.columns.get_loc("High")] = float(df["High"].iloc[-1]) * 5
    df_spike.iloc[-1, df_spike.columns.get_loc("Low")]  = float(df["Low"].iloc[-1])  / 5
    p = rsi_atr._cur_atr_pctile(df_spike["High"], df_spike["Low"], df_spike["Close"])
    assert p > 0.90


def test_low_atr_has_low_pctile():
    """100 consecutive dead-flat bars should drive EWM ATR below the gate."""
    df = _make_df(n=500, atr_scale=1.0)
    df_flat = df.copy()
    # Flatten the last 100 bars so EWM ATR(14) genuinely decays
    for i in range(len(df_flat) - 100, len(df_flat)):
        mid = float(df_flat["Close"].iloc[i])
        df_flat.iloc[i, df_flat.columns.get_loc("High")] = mid + 1e-8
        df_flat.iloc[i, df_flat.columns.get_loc("Low")]  = mid - 1e-8
    p = rsi_atr._cur_atr_pctile(df_flat["High"], df_flat["Low"], df_flat["Close"])
    assert p < rsi_atr._ATR_PCTILE_GATE


# ── generate_signals ───────────────────────────────────────────────────────

def test_generate_signals_subset_of_rsi():
    """Every rsi_atr signal must correspond to an rsi signal for the same symbol."""
    df = _make_trending_df()
    md = {"EURUSD": df}
    base_syms = {s["symbol"] for s in _rsi.generate_signals(md)}
    atr_syms  = {s["symbol"] for s in rsi_atr.generate_signals(md)}
    assert atr_syms <= base_syms


def test_generate_signals_carries_atr_pctile():
    """Any returned signal must have atr_pctile > 0.66."""
    df = _make_trending_df()
    # Force a very wide bar to ensure high ATR percentile
    df_hot = df.copy()
    n = len(df_hot)
    for i in range(n - 5, n):
        mid = float(df_hot["Close"].iloc[i])
        df_hot.iloc[i, df_hot.columns.get_loc("High")] = mid * 1.05
        df_hot.iloc[i, df_hot.columns.get_loc("Low")]  = mid * 0.95
    md = {"EURUSD": df_hot}
    sigs = rsi_atr.generate_signals(md)
    for sig in sigs:
        assert "atr_pctile" in sig
        assert sig["atr_pctile"] > rsi_atr._ATR_PCTILE_GATE


def test_generate_signals_filters_low_vol():
    """With a dead-flat tail, rsi_atr should return no signals."""
    df = _make_trending_df(n=700)
    df_flat = df.copy()
    # Zero out the last 20 bars' ranges so ATR plummets
    for i in range(len(df_flat) - 20, len(df_flat)):
        mid = float(df_flat["Close"].iloc[i])
        df_flat.iloc[i, df_flat.columns.get_loc("High")] = mid + 1e-8
        df_flat.iloc[i, df_flat.columns.get_loc("Low")]  = mid - 1e-8
    md = {"EURUSD": df_flat}
    sigs = rsi_atr.generate_signals(md)
    assert sigs == []


def test_generate_signals_empty_market_data():
    assert rsi_atr.generate_signals({}) == []


def test_generate_signals_none_df_skipped():
    assert rsi_atr.generate_signals({"EURUSD": None}) == []


def test_generate_signals_preserves_signal_fields():
    """Returned signals have all base rsi fields plus atr_pctile."""
    df = _make_trending_df(n=700)
    df_hot = df.copy()
    n = len(df_hot)
    for i in range(n - 10, n):
        mid = float(df_hot["Close"].iloc[i])
        df_hot.iloc[i, df_hot.columns.get_loc("High")] = mid * 1.04
        df_hot.iloc[i, df_hot.columns.get_loc("Low")]  = mid * 0.96
    md = {"EURUSD": df_hot}
    sigs = rsi_atr.generate_signals(md)
    for sig in sigs:
        assert "symbol" in sig
        assert "direction" in sig
        assert "stop_price" in sig
        assert "close" in sig
        assert "atr_pctile" in sig


# ── forbidden imports (governance) ────────────────────────────────────────

def test_no_forbidden_imports():
    import ast, textwrap
    src = open(
        os.path.join(os.path.dirname(__file__), "forex", "strategy_rsi_atr.py")
    ).read()
    tree = ast.parse(src)
    forbidden = {"forex.runner", "saxo_", "pnl_tracker", "housekeeping",
                 "safeguard", "saxo_client", "saxo_auth"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = ([n.name for n in node.names] if isinstance(node, ast.Import)
                     else ([node.module] if node.module else []))
            for name in names:
                for f in forbidden:
                    assert f not in (name or ""), f"forbidden import: {name}"
