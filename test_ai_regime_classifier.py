"""
Sprint 1 test gate -- ai/regime/classifier.py.

Deterministic regime labels from price bars. Synthetic series exercise
each label; a stability check confirms the label doesn't flap bar-to-bar
(a regime has persistence).
"""

import os
import sys

import numpy as np
import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

GREEN, RED, YELLOW, RESET, BOLD = "\033[92m", "\033[91m", "\033[93m", "\033[0m", "\033[1m"
_results = []


def _run(name, fn):
    try:
        fn()
        _results.append((name, True, None))
    except Exception as e:
        import traceback
        _results.append((name, False, f"{type(e).__name__}: {e}\n{traceback.format_exc()}"))


from ai.regime.classifier import classify_regime, LABELS, MIN_BARS

_RNG = np.random.default_rng(7)


def _series_to_bars(closes, ranges):
    """Build an OHLC frame: each bar's High/Low straddle its Close by
    `ranges[i]` (half above, half below). `ranges` sets per-bar volatility."""
    closes = np.asarray(closes, float)
    ranges = np.asarray(ranges, float)
    return pd.DataFrame({
        "High":  closes + ranges / 2,
        "Low":   closes - ranges / 2,
        "Close": closes,
    })


def _trend(n, step, base=1.0, noise=0.0003, rng_range=0.0025):
    c = base + np.cumsum(np.full(n, step) + _RNG.normal(0, noise, n))
    return _series_to_bars(c, np.full(n, rng_range))


def _range_bound(n, base=1.0, amp=0.004, rng_range=0.0025):
    c = base + amp * np.sin(np.linspace(0, 8 * np.pi, n)) + _RNG.normal(0, 0.0004, n)
    return _series_to_bars(c, np.full(n, rng_range))


# ═══════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}1. each label is reachable from a matching synthetic series{RESET}")
# ═══════════════════════════════════════════════════════════════════════

def test_uptrend_is_trending_bullish():
    r = classify_regime(_trend(120, +0.0022))
    assert r["label"] == "TRENDING_BULLISH", r
    assert r["adx"] >= 25 and r["plus_di"] > r["minus_di"] and r["ma_slope"] > 0
_run("strong uptrend -> TRENDING_BULLISH", test_uptrend_is_trending_bullish)


def test_downtrend_is_trending_bearish():
    r = classify_regime(_trend(120, -0.0022, base=1.4))
    assert r["label"] == "TRENDING_BEARISH", r
_run("strong downtrend -> TRENDING_BEARISH", test_downtrend_is_trending_bearish)


def test_oscillating_is_ranging():
    r = classify_regime(_range_bound(140))
    assert r["label"] == "RANGING", r
    assert r["adx"] < 25
_run("oscillating / no ADX -> RANGING", test_oscillating_is_ranging)


def test_recent_vol_expansion_is_high_volatility():
    # 100 calm bars then 25 with 4x the per-bar range and bigger moves
    calm_c = 1.0 + _RNG.normal(0, 0.0004, 100).cumsum()
    hot_c  = calm_c[-1] + _RNG.normal(0, 0.004, 25).cumsum()
    c = np.concatenate([calm_c, hot_c])
    rng = np.concatenate([np.full(100, 0.0020), np.full(25, 0.0090)])
    r = classify_regime(_series_to_bars(c, rng))
    assert r["label"] in ("HIGH_VOLATILITY", "CHAOTIC"), r
    assert r["atr_ratio"] >= 1.6
_run("recent ATR expansion vs its own median -> HIGH_VOLATILITY/CHAOTIC", test_recent_vol_expansion_is_high_volatility)


def test_recent_vol_contraction_is_low_volatility():
    loud_c = 1.0 + _RNG.normal(0, 0.003, 100).cumsum()
    quiet_c = loud_c[-1] + _RNG.normal(0, 0.00015, 25).cumsum()
    c = np.concatenate([loud_c, quiet_c])
    rng = np.concatenate([np.full(100, 0.0060), np.full(25, 0.0010)])
    r = classify_regime(_series_to_bars(c, rng))
    assert r["label"] == "LOW_VOLATILITY", r
    assert r["atr_ratio"] <= 0.6
_run("recent ATR contraction -> LOW_VOLATILITY", test_recent_vol_contraction_is_low_volatility)


def test_chaotic_is_high_vol_no_direction():
    # big whippy moves, no net direction -> ADX stays low, ATR ratio high
    c = 1.0 + np.cumsum(_RNG.normal(0, 0.006, 130) * ((-1) ** np.arange(130)))
    rng = np.concatenate([np.full(105, 0.0030), np.full(25, 0.0130)])
    r = classify_regime(_series_to_bars(c, rng))
    assert r["label"] == "CHAOTIC", r
_run("high vol + no ADX structure -> CHAOTIC", test_chaotic_is_high_vol_no_direction)


def test_breakout_from_quiet_range():
    # long quiet stretch, then price clears the range on a modest push --
    # caught mid-breakout (ADX rising but not yet a matured >25 trend)
    quiet = 1.0 + _RNG.normal(0, 0.00025, 100).cumsum()
    burst = quiet[-1] + np.linspace(0, 0.007, 9) + _RNG.normal(0, 0.0002, 9)
    c = np.concatenate([quiet, burst])
    rng = np.concatenate([np.full(100, 0.0015), np.full(9, 0.0022)])
    r = classify_regime(_series_to_bars(c, rng))
    assert r["label"] in ("BREAKOUT", "TRENDING_BULLISH"), r
_run("ADX surging out of a squeeze + range broken -> BREAKOUT (or TRENDING once matured)",
     test_breakout_from_quiet_range)


# ═══════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}2. contract + robustness{RESET}")
# ═══════════════════════════════════════════════════════════════════════

def test_too_few_bars_is_unknown_not_a_crash():
    for bad in ([], [{"High": 1, "Low": 1, "Close": 1}] * 5, None):
        r = classify_regime(bad)
        assert r["label"] == "UNKNOWN" and r["confidence"] == 0.0
_run("too few / missing / None bars -> UNKNOWN, never raises", test_too_few_bars_is_unknown_not_a_crash)


def test_accepts_list_of_dicts_and_dataframe_equally():
    df = _trend(120, +0.0022)
    a = classify_regime(df)
    b = classify_regime(df.to_dict("records"))
    assert a == b, "list[dict] and DataFrame inputs must classify identically"
_run("accepts list[dict] and DataFrame, identical result", test_accepts_list_of_dicts_and_dataframe_equally)


def test_label_is_always_in_taxonomy():
    for _ in range(40):
        c = 1.0 + np.cumsum(_RNG.normal(0, _RNG.uniform(0.0002, 0.008), 130))
        rng = np.abs(_RNG.normal(0.003, 0.002, 130)) + 0.0005
        lab = classify_regime(_series_to_bars(c, rng))["label"]
        assert lab in LABELS or lab == "UNKNOWN", lab
_run("every output label is in the taxonomy (or UNKNOWN)", test_label_is_always_in_taxonomy)


def test_regime_does_not_flap_bar_to_bar():
    # slide a 100-bar window across a 200-bar trend; the label should be
    # stable across most steps, not a different regime every bar.
    full = _trend(220, +0.0018)
    labels = []
    for end in range(MIN_BARS, len(full), 2):
        labels.append(classify_regime(full.iloc[end - 100:end] if end >= 100 else full.iloc[:end])["label"])
    # count adjacent changes
    flips = sum(1 for i in range(1, len(labels)) if labels[i] != labels[i - 1])
    assert flips <= max(2, len(labels) // 6), f"{flips} label changes over {len(labels)} steps -- too flappy: {labels}"
_run("regime label is persistent across a moving window (a trend stays a trend)",
     test_regime_does_not_flap_bar_to_bar)


def test_not_wired_into_runner_yet():
    src = open(os.path.join(BASE_DIR, "forex", "runner.py"), encoding="utf-8").read()
    assert "classify_regime" not in src and "ai.regime" not in src, (
        "Sprint 1 exit criterion: the classifier is a standalone utility, "
        "not yet called from forex/runner.py"
    )
_run("classify_regime is NOT imported or called from forex/runner.py yet", test_not_wired_into_runner_yet)


print(f"\n{BOLD}{'='*66}{RESET}")
failed = [(n, e) for n, ok, e in _results if not ok]
for name, ok, err in _results:
    print(f"  [{GREEN}PASS{RESET}]" if ok else f"  [{RED}FAIL{RESET}]", name)
    if err:
        print(f"      {YELLOW}{err}{RESET}")
print(f"{BOLD}{'='*66}{RESET}")
if failed:
    print(f"{RED}{BOLD}  {len(failed)} / {len(_results)} FAILED{RESET}")
    sys.exit(1)
print(f"{GREEN}{BOLD}  ALL {len(_results)} TESTS PASSED{RESET}")
sys.exit(0)
