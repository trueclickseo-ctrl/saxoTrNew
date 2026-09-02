"""
test_cnn_lstm.py
-----------------
Direct signal-logic tests for forex/strategy_cnn_lstm.py (FX Strategy 11).

Before this file existed, cnn_lstm had ZERO test coverage of any kind --
no unit test, no backtest -- despite trading real capital. It shares the
same generate_signals()/should_exit()/size_position() interface as every
other forex strategy, so its DECISION LOGIC (confidence thresholds, ADX
filter, stop placement, time stop, model-flip exit) is tested here the
same way ema/gap/london_breakout are -- by driving it with synthetic OHLC
data and asserting on the resulting signals.

This does NOT validate the trained model's actual predictive quality --
that requires the real model weights and either a historical backtest
(feasible here since cnn_lstm consumes plain daily bars, unlike
gap/london_breakout which need intraday data Yahoo doesn't carry) or live
forward performance. This file mocks `_predict()` directly so the
decision logic (thresholds/filters/stops) is pinned down independent of
what the model happens to output on any given day.

Run:  python test_cnn_lstm.py
"""

import os
import sys
from unittest.mock import patch

import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import forex.strategy_cnn_lstm as cl

GREEN, RED, YELLOW, CYAN, RESET, BOLD = (
    "\033[92m", "\033[91m", "\033[93m", "\033[96m", "\033[0m", "\033[1m"
)
_results = []


def _run(name, fn):
    try:
        result = fn()
        if result is None:
            result = True
        _results.append((name, bool(result), None))
    except Exception as e:
        _results.append((name, False, f"{type(e).__name__}: {e}"))


def section(title):
    print(f"\n{BOLD}{CYAN}{'-'*70}{RESET}")
    print(f"{BOLD}{CYAN}  {title}{RESET}")
    print(f"{BOLD}{CYAN}{'-'*70}{RESET}")


# ── Synthetic data helpers ──────────────────────────────────────────────

def _trending_df(n=None, base=1.1000, drift=0.0006, noise=0.00005, up=True):
    """A steadily trending series -- strong ADX, the condition cnn_lstm
    requires alongside model confidence before it will ever signal."""
    n = n or cl.MIN_BARS + 5
    sign = 1 if up else -1
    closes = []
    px = base
    for i in range(n):
        px += sign * drift + (noise if i % 2 == 0 else -noise)
        closes.append(px)
    return pd.DataFrame({
        "Open":  closes,
        "High":  [c + abs(drift) * 0.6 for c in closes],
        "Low":   [c - abs(drift) * 0.6 for c in closes],
        "Close": closes,
    })


def _choppy_df(n=None, base=1.1000, amplitude=0.0008):
    """A flat, oscillating series -- deliberately low trend strength (ADX)
    so the ADX_MIN filter should reject it regardless of model confidence."""
    n = n or cl.MIN_BARS + 5
    closes = [base + amplitude * (1 if i % 2 == 0 else -1) for i in range(n)]
    return pd.DataFrame({
        "Open":  closes,
        "High":  [c + amplitude * 0.2 for c in closes],
        "Low":   [c - amplitude * 0.2 for c in closes],
        "Close": closes,
    })


def _short_df(n=10):
    closes = [1.1000 + i * 0.0001 for i in range(n)]
    return pd.DataFrame({"Open": closes, "High": closes, "Low": closes, "Close": closes})


# ── generate_signals() ───────────────────────────────────────────────────
section("generate_signals()")


def test_buy_signal_fires_on_high_confidence_and_trend():
    df = _trending_df(up=True)
    with patch.object(cl, "_model_ready", return_value=True), \
         patch.object(cl, "_predict", return_value=(0.10, 0.20, 0.70)):  # sell, hold, buy
        sigs = cl.generate_signals({"EURUSD": df})
    assert len(sigs) == 1, f"expected 1 signal, got {sigs}"
    s = sigs[0]
    assert s["direction"] == "Buy"
    assert s["symbol"] == "EURUSD"
    assert s["stop_price"] < s["close"], "long stop must sit below entry"


def test_sell_signal_fires_on_high_confidence_and_trend():
    df = _trending_df(up=False)
    with patch.object(cl, "_model_ready", return_value=True), \
         patch.object(cl, "_predict", return_value=(0.70, 0.20, 0.10)):
        sigs = cl.generate_signals({"GBPUSD": df})
    assert len(sigs) == 1
    s = sigs[0]
    assert s["direction"] == "Sell"
    assert s["stop_price"] > s["close"], "short stop must sit above entry"


def test_no_signal_below_confidence_threshold():
    df = _trending_df(up=True)
    just_under = cl.CONFIDENCE_THRESHOLD - 0.01
    with patch.object(cl, "_model_ready", return_value=True), \
         patch.object(cl, "_predict", return_value=(0.10, 1 - just_under - 0.10, just_under)):
        sigs = cl.generate_signals({"EURUSD": df})
    assert sigs == [], f"must not signal below CONFIDENCE_THRESHOLD, got {sigs}"


def test_no_signal_when_adx_too_low_even_with_high_confidence():
    """High model confidence alone must not be enough -- a choppy/sideways
    market has to be rejected by the ADX filter regardless."""
    df = _choppy_df()
    with patch.object(cl, "_model_ready", return_value=True), \
         patch.object(cl, "_predict", return_value=(0.05, 0.05, 0.90)):
        sigs = cl.generate_signals({"EURUSD": df})
    assert sigs == [], f"low-ADX chop must be rejected even at high confidence, got {sigs}"


def test_symbol_already_open_is_skipped():
    df = _trending_df(up=True)
    with patch.object(cl, "_model_ready", return_value=True), \
         patch.object(cl, "_predict", return_value=(0.10, 0.20, 0.70)):
        sigs = cl.generate_signals({"EURUSD": df}, open_symbols={"EURUSD"})
    assert sigs == [], "a pair already held must not get a second signal"


def test_insufficient_bars_produces_no_signal():
    df = _short_df(n=10)
    with patch.object(cl, "_model_ready", return_value=True), \
         patch.object(cl, "_predict", return_value=(0.10, 0.20, 0.70)):
        sigs = cl.generate_signals({"EURUSD": df})
    assert sigs == [], "must not signal with fewer than MIN_BARS of history"


def test_no_model_available_returns_empty_without_predicting():
    df = _trending_df(up=True)
    predict_calls = []
    with patch.object(cl, "_model_ready", return_value=False), \
         patch.object(cl, "_predict", side_effect=lambda d: predict_calls.append(1)):
        sigs = cl.generate_signals({"EURUSD": df})
    assert sigs == []
    assert predict_calls == [], "must not even attempt inference when no model is trained"


def test_signals_sorted_by_confidence_descending():
    df_a = _trending_df(up=True, base=1.1000)
    df_b = _trending_df(up=True, base=0.9000)

    def fake_predict(df):
        # Distinguish the two pairs by their base price level.
        return (0.05, 0.10, 0.85) if df["Close"].iloc[0] > 1.0 else (0.05, 0.35, 0.60)

    with patch.object(cl, "_model_ready", return_value=True), \
         patch.object(cl, "_predict", side_effect=fake_predict):
        sigs = cl.generate_signals({"LOWCONF": df_b, "HICONF": df_a})
    assert [s["symbol"] for s in sigs] == ["HICONF", "LOWCONF"], sigs


# ── should_exit() ─────────────────────────────────────────────────────────
section("should_exit()")


def test_time_stop_fires_regardless_of_price():
    df = _trending_df(up=True)
    pos = {"direction": "Buy", "stop_price": 0}
    exit_flag, reason = cl.should_exit(pos, df, calendar_days_held=cl.TIME_STOP_DAYS)
    assert exit_flag and "time_stop" in reason


def test_time_stop_does_not_fire_early():
    df = _trending_df(up=True)
    pos = {"direction": "Buy", "stop_price": 0}
    with patch.object(cl, "_predict", return_value=(0.05, 0.90, 0.05)):
        exit_flag, _ = cl.should_exit(pos, df, calendar_days_held=cl.TIME_STOP_DAYS - 1)
    assert not exit_flag


def test_atr_hard_stop_fires_for_long_when_low_breaches_it():
    df = _trending_df(up=True)
    stop_above_low = float(df["Low"].iloc[-1]) + 0.01  # force a breach
    pos = {"direction": "Buy", "stop_price": stop_above_low}
    exit_flag, reason = cl.should_exit(pos, df, calendar_days_held=1)
    assert exit_flag and "hard_stop" in reason


def test_atr_hard_stop_fires_for_short_when_high_breaches_it():
    df = _trending_df(up=False)
    stop_below_high = float(df["High"].iloc[-1]) - 0.01  # force a breach
    pos = {"direction": "Sell", "stop_price": stop_below_high}
    exit_flag, reason = cl.should_exit(pos, df, calendar_days_held=1)
    assert exit_flag and "hard_stop" in reason


def test_model_flip_exits_a_long_on_high_sell_confidence():
    df = _trending_df(up=True)
    pos = {"direction": "Buy", "stop_price": float(df["Low"].iloc[-1]) - 1.0}  # unreachable stop
    with patch.object(cl, "_predict", return_value=(0.80, 0.10, 0.10)):
        exit_flag, reason = cl.should_exit(pos, df, calendar_days_held=1)
    assert exit_flag and "model_flip" in reason and "sell" in reason


def test_model_flip_exits_a_short_on_high_buy_confidence():
    df = _trending_df(up=False)
    pos = {"direction": "Sell", "stop_price": float(df["High"].iloc[-1]) + 1.0}  # unreachable stop
    with patch.object(cl, "_predict", return_value=(0.10, 0.10, 0.80)):
        exit_flag, reason = cl.should_exit(pos, df, calendar_days_held=1)
    assert exit_flag and "model_flip" in reason and "buy" in reason


def test_no_exit_when_nothing_triggers():
    df = _trending_df(up=True)
    pos = {"direction": "Buy", "stop_price": float(df["Low"].iloc[-1]) - 1.0}
    with patch.object(cl, "_predict", return_value=(0.10, 0.80, 0.10)):
        exit_flag, reason = cl.should_exit(pos, df, calendar_days_held=1)
    assert not exit_flag and reason == ""


def test_insufficient_data_never_exits():
    df = _short_df(n=10)
    pos = {"direction": "Buy", "stop_price": 999}
    exit_flag, reason = cl.should_exit(pos, df, calendar_days_held=cl.TIME_STOP_DAYS)
    assert not exit_flag, "must not evaluate (or falsely exit) without enough bars for its own indicators"


# ── size_position() ───────────────────────────────────────────────────────
section("size_position()")


def test_size_position_scales_inversely_with_atr():
    small_atr_size = cl.size_position(account_equity=100_000, atr=0.0010)
    large_atr_size = cl.size_position(account_equity=100_000, atr=0.0050)
    assert small_atr_size > large_atr_size, "a wider stop (higher ATR) must size smaller for the same risk"


def test_size_position_never_below_min_units():
    tiny = cl.size_position(account_equity=100, atr=0.0500)
    assert tiny == cl.LOT_ROUND


def test_size_position_zero_atr_falls_back_to_min_units():
    assert cl.size_position(account_equity=100_000, atr=0.0) == cl.LOT_ROUND


_run("Buy signal fires on high confidence + confirmed trend", test_buy_signal_fires_on_high_confidence_and_trend)
_run("Sell signal fires on high confidence + confirmed trend", test_sell_signal_fires_on_high_confidence_and_trend)
_run("no signal below CONFIDENCE_THRESHOLD", test_no_signal_below_confidence_threshold)
_run("no signal in a low-ADX (choppy) market even at high confidence", test_no_signal_when_adx_too_low_even_with_high_confidence)
_run("a pair already open is skipped", test_symbol_already_open_is_skipped)
_run("fewer than MIN_BARS produces no signal", test_insufficient_bars_produces_no_signal)
_run("no trained model -> empty result, never even calls _predict", test_no_model_available_returns_empty_without_predicting)
_run("multiple signals are sorted by confidence, highest first", test_signals_sorted_by_confidence_descending)
_run("time stop fires at exactly TIME_STOP_DAYS", test_time_stop_fires_regardless_of_price)
_run("time stop does not fire one day early", test_time_stop_does_not_fire_early)
_run("ATR hard stop exits a long when price breaches it", test_atr_hard_stop_fires_for_long_when_low_breaches_it)
_run("ATR hard stop exits a short when price breaches it", test_atr_hard_stop_fires_for_short_when_high_breaches_it)
_run("model flip (high sell prob) exits an open long", test_model_flip_exits_a_long_on_high_sell_confidence)
_run("model flip (high buy prob) exits an open short", test_model_flip_exits_a_short_on_high_buy_confidence)
_run("no exit when time/stop/model-flip all fail to trigger", test_no_exit_when_nothing_triggers)
_run("insufficient data never forces an exit", test_insufficient_data_never_exits)
_run("position sizing scales inversely with ATR (risk-normalized)", test_size_position_scales_inversely_with_atr)
_run("position sizing floors at LOT_ROUND for tiny equity", test_size_position_never_below_min_units)
_run("zero ATR falls back to LOT_ROUND instead of dividing by zero", test_size_position_zero_atr_falls_back_to_min_units)


print(f"\n{BOLD}{'='*70}{RESET}")
passed = sum(1 for _, ok, _ in _results if ok)
failed = [(n, e) for n, ok, e in _results if not ok]
for name, ok, err in _results:
    icon = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
    print(f"  [{icon}] {name}")
    if err:
        print(f"         {YELLOW}{err}{RESET}")
print(f"{BOLD}{'='*70}{RESET}")
if failed:
    print(f"{RED}{BOLD}  {len(failed)} / {len(_results)} TESTS FAILED{RESET}")
    sys.exit(1)
else:
    print(f"{GREEN}{BOLD}  ALL {len(_results)} TESTS PASSED{RESET}")
    sys.exit(0)
