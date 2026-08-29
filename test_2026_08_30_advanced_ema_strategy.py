"""
Regression tests -- 2026-08-30 "advanced_ema" strategy integration.

User-supplied strategy (strategy_advanced_ema.py), added as a SIM-only
parallel A/B test against the original "ema" strategy (forex/strategy.py,
untouched). EMA(5/30) crossover + EMA50 macro confirm + rising-ADX regime
+ ATR-percentile band + recent-crossover-only + trend-quality composite
score. Standard trailing_stop_update hook (no runner exit-loop change).
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


def test_registered_sim_only():
    import forex.runner as r
    assert "advanced_ema" in r.STRATEGIES
    assert r.SLOTS_PER_STRATEGY.get("advanced_ema") == r._SWING_SLOTS
    assert "advanced_ema" not in r.LIVE_ALLOWED_STRATEGIES
    assert "advanced_ema" not in r.LIVE_EUR_ALLOWED_STRATEGIES
_run("forex/runner: advanced_ema registered, uncapped slots, SIM-only", test_registered_sim_only)


def test_original_ema_untouched():
    import forex.strategy as e
    # the original "ema" strategy lives in forex/strategy.py -- identity check
    assert e.FAST_EMA == 5 and e.SLOW_EMA == 30
    assert not hasattr(e, "TREND_EMA")          # advanced_ema adds EMA50; original has none
    src = inspect.getsource(e)
    assert "atr_pct" not in src and "VOL_PCT" not in src   # no vol-percentile filter in the original
_run("forex/strategy (ema): original is untouched -- no EMA50, no vol-percentile filter", test_original_ema_untouched)


def test_min_bars_within_chart_bars():
    import forex.runner as r
    import forex.strategy_advanced_ema as a
    assert a.MIN_BARS <= r.CHART_BARS, f"MIN_BARS {a.MIN_BARS} must fit in CHART_BARS {r.CHART_BARS}"
    assert a.MIN_BARS == max(a.TREND_EMA, a.VOL_LOOKBACK) + a.ADX_PERIOD + 10 == 276
_run("forex/strategy_advanced_ema: MIN_BARS (276) fits inside CHART_BARS (500)", test_min_bars_within_chart_bars)


def test_recent_cross_helper():
    import forex.strategy_advanced_ema as a
    # fast rises steadily through a flat slow -> one bullish cross, 2 bars from the end
    #  index: -5   -4   -3   -2   -1
    fast = pd.Series([0.90, 0.94, 0.98, 1.02, 1.06])
    slow = pd.Series([1.00, 1.00, 1.00, 1.00, 1.00])
    # cross (f0<=s0, f1>s1) is at k=2: f[-3]=0.98<=1.00, f[-2]=1.02>1.00
    assert a._recent_cross(fast, slow, True) == 2
    assert a._recent_cross(fast, slow, False) is None
    # steadily falling fast -> one bearish cross, 2 bars from the end
    fastd = pd.Series([1.10, 1.06, 1.02, 0.98, 0.94])
    assert a._recent_cross(fastd, slow, False) == 2
    assert a._recent_cross(fastd, slow, True) is None
    # a cross 12 bars back is beyond SIGNAL_LOOKBACK=10
    f_old = pd.Series([0.98, 1.02] + [1.05] * 12)      # cross at index 0->1, i.e. k=13
    s_old = pd.Series([1.00] * 14)
    assert a._recent_cross(f_old, s_old, True) is None
_run("forex/strategy_advanced_ema: _recent_cross finds a fresh crossover, ignores stale ones (>10 bars)",
     test_recent_cross_helper)


def test_regime_filter_rejects_low_and_high_vol():
    import forex.strategy_advanced_ema as a
    adx_ok = pd.Series([20, 22, 24, 26, 28])           # rising, last >= ADX_MIN
    assert a._regime_ok(adx_ok, pd.Series([0.5])) is True
    assert a._regime_ok(adx_ok, pd.Series([0.05])) is False   # below VOL_PCT_MIN
    assert a._regime_ok(adx_ok, pd.Series([0.97])) is False   # above VOL_PCT_MAX
    adx_falling = pd.Series([30, 29, 28, 27, 26])
    assert a._regime_ok(adx_falling, pd.Series([0.5])) is False  # ADX not rising
    adx_weak = pd.Series([10, 12, 14, 16, 18])
    assert a._regime_ok(adx_weak, pd.Series([0.5])) is False     # ADX < 25
_run("forex/strategy_advanced_ema: _regime_ok needs ADX>=25 AND rising AND ATR-pct in [0.20,0.90]",
     test_regime_filter_rejects_low_and_high_vol)


def test_generate_signals_can_fire_and_is_wellformed():
    import forex.strategy_advanced_ema as a
    n = 340
    t = np.arange(n)
    found = None
    for freq in (20, 24, 28, 30, 32):
        for sd in range(8):
            rr = np.random.default_rng(sd)
            p = 1.2 + 0.06 * np.sin(t / float(freq)) + 0.0009 * t + rr.normal(0, 0.0018, n)
            d = pd.DataFrame({"High": p + np.abs(rr.normal(0, 0.0022, n)),
                              "Low":  p - np.abs(rr.normal(0, 0.0022, n)),
                              "Close": p})
            sigs = a.generate_signals({"X": d})
            if sigs:
                found = sigs[0]
                break
        if found:
            break
    assert found is not None, "advanced_ema never produced a signal across the synthetic sweep"
    assert found["direction"] in ("Buy", "Sell")
    assert found["strategy"] == "advanced_ema"
    assert found["cross_age"] is not None and 1 <= found["cross_age"] <= a.SIGNAL_LOOKBACK
    if found["direction"] == "Buy":
        assert found["stop_price"] < found["close"]
    else:
        assert found["stop_price"] > found["close"]
_run("forex/strategy_advanced_ema: generate_signals fires on a valid setup and returns a well-formed signal",
     test_generate_signals_can_fire_and_is_wellformed)


def test_trailing_stop_update_ratchets_one_way():
    import forex.strategy_advanced_ema as a
    # long: stop only moves up
    assert a.trailing_stop_update(1.00, 1.10, 0.02, "Buy") == max(1.00, 1.10 - 1.5 * 0.02)
    assert a.trailing_stop_update(1.09, 1.05, 0.02, "Buy") == 1.09
    # short: stop only moves down
    assert a.trailing_stop_update(1.20, 1.10, 0.02, "Sell") == min(1.20, 1.10 + 1.5 * 0.02)
_run("forex/strategy_advanced_ema: trailing_stop_update ratchets toward profit only (generic runner hook)",
     test_trailing_stop_update_ratchets_one_way)


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
