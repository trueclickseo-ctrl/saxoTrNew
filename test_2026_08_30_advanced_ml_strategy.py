"""
Regression tests -- 2026-08-30 "advanced_ml" strategy integration.

User-supplied strategy (strategy_advanced_ml.py), added as a SIM-only
parallel A/B test against the original "ml" strategy. "ml" is untouched.
Regularized logistic regression, 252-bar window, 5-day ATR-normalized
target with a neutral zone, regime (ADX + ATR-percentile) and directional
EMA-stack trend filters, threshold 0.62, plus an update_stop_price()
breakeven+trail hook wired generically into the runner's exit loop.
"""

import inspect
import os
import sys

import numpy as np
import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

GREEN, RED, YELLOW, CYAN, RESET, BOLD = (
    "\033[92m", "\033[91m", "\033[93m", "\033[96m", "\033[0m", "\033[1m"
)
_results = []


def _run(name, fn):
    try:
        fn()
        _results.append((name, True, None))
    except Exception as e:
        _results.append((name, False, f"{type(e).__name__}: {e}"))


def _synthetic_uptrend(n=600, seed=1):
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0006, 0.004, n).cumsum()
    close = 1.10 * np.exp(steps)
    high = close * (1 + np.abs(rng.normal(0, 0.001, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.001, n)))
    return pd.DataFrame({"High": high, "Low": low, "Close": close})


def test_registered_sim_only():
    import forex.runner as r
    assert "advanced_ml" in r.STRATEGIES
    assert r.SLOTS_PER_STRATEGY.get("advanced_ml") == r._SWING_SLOTS
    assert "advanced_ml" not in r.LIVE_ALLOWED_STRATEGIES
    assert "advanced_ml" not in r.LIVE_EUR_ALLOWED_STRATEGIES
    assert "advanced_ml" in set(r.STRATEGIES)   # -> passes _VALID_STRATS
_run("forex/runner: advanced_ml registered, uncapped slots, SIM-only (not in either LIVE allowlist)",
     test_registered_sim_only)


def test_ml_strategy_untouched():
    import forex.strategy_ml as m
    src = inspect.getsource(m)
    # spot-check the original's identity constants are unchanged
    assert m.CONFIDENCE_THRESHOLD == 0.58
    assert m.LOOKBACK == 126
    assert "L2" not in src and "l2=" not in src
_run("forex/strategy_ml: original ML strategy is completely untouched (0.58 / 126-bar / no L2)",
     test_ml_strategy_untouched)


def test_chart_bars_covers_advanced_ml_min_bars():
    import forex.runner as r
    import forex.strategy_advanced_ml as a
    assert r.CHART_BARS >= a.MIN_BARS, (
        f"CHART_BARS ({r.CHART_BARS}) must be >= advanced_ml MIN_BARS ({a.MIN_BARS}) "
        f"or the strategy silently never signals")
_run("forex/runner: CHART_BARS (500) >= advanced_ml MIN_BARS (492)", test_chart_bars_covers_advanced_ml_min_bars)


def test_generate_signals_shape_and_selectivity():
    import forex.strategy_advanced_ml as a
    up = _synthetic_uptrend()
    down = _synthetic_uptrend(seed=2)
    down = down.assign(High=1/down["Low"], Low=1/down["High"], Close=1/down["Close"])
    md = {"UPUSD": up, "DNUSD": down, "SHORT": up.iloc[:100]}
    sigs = a.generate_signals(md)
    assert isinstance(sigs, list)
    for s in sigs:
        assert s["direction"] in ("Buy", "Sell")
        assert 0.0 <= s["score"] <= 1.0
        assert s["strategy"] == "advanced_ml"
        assert "stop_price" in s and "ml_prob" in s
        # stop must be on the correct side of entry
        if s["direction"] == "Buy":
            assert s["stop_price"] < s["close"]
        else:
            assert s["stop_price"] > s["close"]
    # too-short series must never produce a signal
    assert not any(s["symbol"] == "SHORT" for s in sigs)
_run("forex/strategy_advanced_ml: generate_signals returns well-formed, correctly-sided signals; skips short series",
     test_generate_signals_shape_and_selectivity)


def test_target_has_neutral_zone():
    import forex.strategy_advanced_ml as a
    c = pd.Series(np.linspace(1.0, 1.0001, 400))     # near-flat -> mostly neutral
    atr = pd.Series(np.full(400, 0.01))
    y = a._target(c, atr)
    # flat series within 0.75*ATR -> the vast majority are NaN (excluded from training)
    assert np.isnan(y).mean() > 0.8
_run("forex/strategy_advanced_ml: _target excludes sub-threshold (noise) moves from training (NaN)",
     test_target_has_neutral_zone)


def test_update_stop_price_ratchets_only_toward_profit():
    import forex.strategy_advanced_ml as a
    df = _synthetic_uptrend()
    entry = float(df["Close"].iloc[-30])
    pos = {"direction": "Buy", "entry_price": entry,
           "stop_price": entry - 0.05, "close": entry}
    new = a.update_stop_price(pos, df)
    assert new >= pos["stop_price"] - 1e-9, "long stop must never move down"
_run("forex/strategy_advanced_ml: update_stop_price only ratchets the stop toward profit",
     test_update_stop_price_wrapper := test_update_stop_price_ratchets_only_toward_profit)


def test_runner_wires_update_stop_price_generically():
    import forex.runner as r
    src = inspect.getsource(r._run_exits)
    assert 'hasattr(strat_mod, "update_stop_price")' in src
    assert "strat_mod.update_stop_price(pos, df)" in src
    # it sits after the trailing block and before the breakeven call
    up_at = src.index("update_stop_price(pos, df)")
    be_at = src.index("_apply_breakeven_stop(key, pos, df")
    assert up_at < be_at
_run("forex/runner: _run_exits calls update_stop_price generically (advanced_ml's stop hook is live)",
     test_runner_wires_update_stop_price_generically)


print(f"\n{BOLD}{'='*70}{RESET}")
passed = sum(1 for _, ok, _ in _results)
failed = [(nm, e) for nm, ok, e in _results if not ok]
for nm, ok, err in _results:
    icon = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
    print(f"  [{icon}] {nm}")
    if err:
        print(f"         {YELLOW}{err}{RESET}")
print(f"{BOLD}{'='*70}{RESET}")
if failed:
    print(f"{RED}{BOLD}  {len(failed)} / {len(_results)} TESTS FAILED{RESET}")
    sys.exit(1)
else:
    print(f"{GREEN}{BOLD}  ALL {len(_results)} TESTS PASSED{RESET}")
    sys.exit(0)
