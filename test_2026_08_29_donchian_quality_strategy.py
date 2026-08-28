"""
Regression test -- 2026-08-29 "Donchian Quality" (forex/strategy_donchian_quality.py).

New SIM-only A/B strategy against the original forex/strategy_donchian.py
("donchian"), built to the user's own design doc (breakout-strength band,
ADX-rising requirement, max EMA200 distance, a REALLY enforced 4-position
cap). "donchian" itself must be completely untouched.
"""

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


def _runner_src():
    with open(os.path.join(BASE_DIR, "forex", "runner.py"), encoding="utf-8") as f:
        return f.read()


def _make_trending_df(n=260, start=1.1000, step=0.0006, breakout_jump=0.0, noise_seed=0):
    """Build a clean uptrend series long enough for EMA200/ADX to settle,
    with a controllable final breakout jump above the trailing 30-day high."""
    rng = np.random.default_rng(noise_seed)
    closes = [start + step * i for i in range(n)]
    if breakout_jump:
        closes[-1] = max(closes[-2:]) + breakout_jump
    highs = [c + 0.0008 for c in closes]
    lows  = [c - 0.0008 for c in closes]
    return pd.DataFrame({"Open": closes, "High": highs, "Low": lows, "Close": closes})


# ═══════════════════════════════════════════════════════════════════════
section("1. Minimum/maximum breakout-strength filter")
# ═══════════════════════════════════════════════════════════════════════

def test_tiny_breakout_filtered_as_noise():
    import forex.strategy_donchian_quality as dq
    df = _make_trending_df(breakout_jump=0.0)  # closes[-1] == closes[-2], no real breakout at all
    sigs = dq.generate_signals({"EURUSD": df})
    assert sigs == [], "a non-breakout (flat close) must not fire a signal"
_run("No signal when there's no real breakout above the 30-day high",
     test_tiny_breakout_filtered_as_noise)


def test_min_breakout_atr_constant_matches_doc():
    import forex.strategy_donchian_quality as dq
    assert dq.MIN_BREAKOUT_ATR == 0.10
    assert dq.MAX_BREAKOUT_ATR == 1.50
_run("MIN_BREAKOUT_ATR=0.10 and MAX_BREAKOUT_ATR=1.50 match the design doc",
     test_min_breakout_atr_constant_matches_doc)


def test_signal_records_breakout_strength_within_band():
    import forex.strategy_donchian_quality as dq
    # A deliberate, moderate breakout jump -- if a signal fires, its
    # recorded breakout_strength must fall inside [MIN, MAX].
    df = _make_trending_df(breakout_jump=0.004)
    sigs = dq.generate_signals({"EURUSD": df})
    for s in sigs:
        assert dq.MIN_BREAKOUT_ATR <= s["breakout_strength"] <= dq.MAX_BREAKOUT_ATR
_run("Any signal that does fire has breakout_strength inside the allowed band",
     test_signal_records_breakout_strength_within_band)


def test_extreme_breakout_capped_out():
    import forex.strategy_donchian_quality as dq
    # An absurdly large jump (many ATRs) must be rejected as
    # potentially-exhausted/news-driven, not treated as the best signal.
    df = _make_trending_df(breakout_jump=0.50)
    sigs = dq.generate_signals({"EURUSD": df})
    assert sigs == [], "an extreme breakout (>1.5 ATR) must be filtered out, not prioritized"
_run("An extreme breakout far beyond MAX_BREAKOUT_ATR is rejected, not ranked highest",
     test_extreme_breakout_capped_out)


# ═══════════════════════════════════════════════════════════════════════
section("2. ADX must be rising, not just above threshold")
# ═══════════════════════════════════════════════════════════════════════

def test_generate_signals_checks_adx_rising():
    with open(os.path.join(BASE_DIR, "forex", "strategy_donchian_quality.py"), encoding="utf-8") as f:
        src = f.read()
    idx = src.find("adx_val <= adx_prev")
    assert idx != -1 and "continue" in src[idx:idx+150]
    assert "ADX_RISING_LOOKBACK" in src
_run("generate_signals() rejects a candidate whose ADX isn't rising vs ADX_RISING_LOOKBACK bars back",
     test_generate_signals_checks_adx_rising)


def test_adx_rising_lookback_matches_doc():
    import forex.strategy_donchian_quality as dq
    assert dq.ADX_RISING_LOOKBACK == 2
_run("ADX_RISING_LOOKBACK == 2, matching the design doc's recommended parameters",
     test_adx_rising_lookback_matches_doc)


# ═══════════════════════════════════════════════════════════════════════
section("3. Maximum distance from EMA(200)")
# ═══════════════════════════════════════════════════════════════════════

def test_max_ema_distance_constant():
    import forex.strategy_donchian_quality as dq
    assert dq.MAX_EMA_DISTANCE_ATR == 3.0
_run("MAX_EMA_DISTANCE_ATR == 3.0, matching the design doc",
     test_max_ema_distance_constant)


def test_ema_distance_filter_present_for_both_directions():
    with open(os.path.join(BASE_DIR, "forex", "strategy_donchian_quality.py"), encoding="utf-8") as f:
        src = f.read()
    assert src.count("ema_distance > MAX_EMA_DISTANCE_ATR") == 2, (
        "expected the EMA-distance cap applied on BOTH the long and short paths"
    )
_run("The EMA-distance cap is applied on both long and short entry paths",
     test_ema_distance_filter_present_for_both_directions)


# ═══════════════════════════════════════════════════════════════════════
section("4. MAX_POSITIONS actually enforced (unlike the original 'donchian')")
# ═══════════════════════════════════════════════════════════════════════

def test_donchian_quality_slots_equal_its_own_max_positions():
    import forex.runner as r
    import forex.strategy_donchian_quality as dq
    assert r.SLOTS_PER_STRATEGY["donchian_quality"] == dq.MAX_POSITIONS == 4
_run("SLOTS_PER_STRATEGY['donchian_quality'] == the module's own MAX_POSITIONS (4) -- really enforced",
     test_donchian_quality_slots_equal_its_own_max_positions)


def test_original_donchian_slots_untouched():
    import forex.runner as r
    # Confirms the ORIGINAL strategy's slot cap is still the shared
    # _SWING_SLOTS value (i.e. this change did not touch "donchian" at all)
    # -- it should be much larger than 4, exactly the gap the design doc
    # flagged as unverified.
    assert r.SLOTS_PER_STRATEGY["donchian"] == r._SWING_SLOTS
    assert r.SLOTS_PER_STRATEGY["donchian"] > 4
_run("'donchian's own slot cap is untouched (_SWING_SLOTS, still far above its stated MAX_POSITIONS=4)",
     test_original_donchian_slots_untouched)


# ═══════════════════════════════════════════════════════════════════════
section("5. Trailing stop -- confirm the generic mechanism already covers this")
# ═══════════════════════════════════════════════════════════════════════

def test_trailing_stop_update_exists_and_matches_original_math():
    import forex.strategy_donchian as d_orig
    import forex.strategy_donchian_quality as dq
    stop, price, atr = 1.1000, 1.1050, 0.0020
    assert dq.trailing_stop_update(stop, price, atr, "Buy") == d_orig.trailing_stop_update(stop, price, atr, "Buy")
_run("trailing_stop_update() math is identical to the original (only entry filtering changed)",
     test_trailing_stop_update_exists_and_matches_original_math)


def test_runner_calls_trailing_stop_generically():
    src = _runner_src()
    assert 'hasattr(strat_mod, "trailing_stop_update")' in src, (
        "runner.py must call trailing_stop_update() generically via hasattr -- "
        "confirmed this already exists and needs no change for donchian_quality to get it too"
    )
_run("forex/runner.py's trailing-stop call is generic (hasattr-based) -- works for donchian_quality automatically",
     test_runner_calls_trailing_stop_generically)


# ═══════════════════════════════════════════════════════════════════════
section("6. scan_summary() naming fix")
# ═══════════════════════════════════════════════════════════════════════

def test_scan_summary_uses_correct_high30_low30_naming():
    import forex.strategy_donchian_quality as dq
    df = _make_trending_df()
    rows = dq.scan_summary({"EURUSD": df})
    assert rows and "high30" in rows[0] and "low30" in rows[0]
    assert "high20" not in rows[0] and "low20" not in rows[0]
_run("scan_summary() rows use high30/low30 (matching the real 30-bar channel), not the original's high20/low20",
     test_scan_summary_uses_correct_high30_low30_naming)


# ═══════════════════════════════════════════════════════════════════════
section("7. Isolation from the original 'donchian' strategy")
# ═══════════════════════════════════════════════════════════════════════

def test_registered_as_separate_strategy():
    src = _runner_src()
    assert '"donchian_quality": strat_donchian_quality' in src
_run("'donchian_quality' is registered as its own STRATEGIES entry",
     test_registered_as_separate_strategy)


def test_never_in_live_allowlists():
    src = _runner_src()
    live_idx      = src.find("LIVE_ALLOWED_STRATEGIES =")
    live_line     = src[live_idx: src.find("\n", live_idx)]
    live_eur_idx  = src.find("LIVE_EUR_ALLOWED_STRATEGIES =")
    live_eur_line = src[live_eur_idx: src.find("\n", live_eur_idx)]
    assert "donchian_quality" not in live_line
    assert "donchian_quality" not in live_eur_line
_run("'donchian_quality' is absent from both LIVE_ALLOWED_STRATEGIES and LIVE_EUR_ALLOWED_STRATEGIES",
     test_never_in_live_allowlists)


def test_original_donchian_module_untouched():
    with open(os.path.join(BASE_DIR, "forex", "strategy_donchian.py"), encoding="utf-8") as f:
        src = f.read()
    # The original's known gaps (no breakout-strength filter, no ADX-rising
    # check, no EMA-distance cap) must still be there -- proves this work
    # created a NEW module rather than patching the old one in place.
    assert "if today > high30 and today > ema200:          # breakout WITH macro trend" in src
    assert "MIN_BREAKOUT_ATR" not in src
    assert "adx_prev" not in src
_run("forex/strategy_donchian.py (the original) was NOT modified by this change",
     test_original_donchian_module_untouched)


print(f"\n{BOLD}{'='*70}{RESET}")
passed = sum(1 for _, ok, _ in _results)
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
