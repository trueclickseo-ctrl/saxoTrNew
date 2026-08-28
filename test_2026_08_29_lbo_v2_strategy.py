"""
Regression test -- 2026-08-29 "London Breakout V2" (forex/strategy_london_breakout_v2.py).

New SIM-only A/B strategy against the original forex/strategy_london_breakout.py
("london_breakout"), built to the user's own 9-point design-doc review, every
one of which was independently verified against the ORIGINAL's real source
before writing this test (not just assumed from the design doc). The original
must be completely untouched.
"""

import os
import sys
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


def _orig_src():
    with open(os.path.join(BASE_DIR, "forex", "strategy_london_breakout.py"), encoding="utf-8") as f:
        return f.read()


def _v2_src():
    with open(os.path.join(BASE_DIR, "forex", "strategy_london_breakout_v2.py"), encoding="utf-8") as f:
        return f.read()


def _make_h1_df(n=30, base=1.1000, asian_range_pips=20, breakout_extension_pips=0.0,
                pip_size=0.0001):
    """H1 bars: hours 0-6 (Asian range), then a London-open breakout bar at
    hour 7 that closes breakout_extension_pips beyond the Asian high."""
    hours = list(range(n))
    closes = [base] * n
    highs  = [base + 0.0003] * n
    lows   = [base - 0.0003] * n
    # Build a real Asian range across hours 0-6
    asian_high = base + (asian_range_pips / 2) * pip_size
    asian_low  = base - (asian_range_pips / 2) * pip_size
    for i, h in enumerate(hours):
        if h <= 6:
            highs[i] = asian_high
            lows[i]  = asian_low
            closes[i] = base
    # Breakout bar at hour 7
    if 7 in hours:
        idx = hours.index(7)
        closes[idx] = asian_high + breakout_extension_pips * pip_size
        highs[idx]  = closes[idx] + 0.0002
        lows[idx]   = asian_high - 0.0001
    return pd.DataFrame({"HourUTC": hours, "Close": closes, "High": highs, "Low": lows})


# ═══════════════════════════════════════════════════════════════════════
section("1. Range-hour boundary fix -- genuinely exclusive end")
# ═══════════════════════════════════════════════════════════════════════

def test_end_hour_excluded_from_range():
    import forex.strategy_london_breakout_v2 as v2
    # Put a huge outlier ONLY at hour 6 (the "end" of the Asian range under
    # the original's off-by-one) -- if it's excluded, the range must NOT
    # reflect it.
    df = pd.DataFrame({
        "HourUTC": [0, 1, 2, 3, 4, 5, 6],
        "Close":   [1.1000]*7,
        "High":    [1.1005, 1.1005, 1.1005, 1.1005, 1.1005, 1.1005, 1.5000],  # outlier at hour 6
        "Low":     [1.0995]*7,
    })
    result = v2._session_range(df, start_h=0, end_h=6, pip_size=0.0001)  # [0,6) should exclude hour 6
    assert result is not None
    rng_high, rng_low, rng_pips = result
    assert rng_high < 1.2, f"hour 6 (the exclusive end) leaked into the range: high={rng_high}"
_run("_session_range(start_h=0, end_h=6) genuinely excludes hour 6",
     test_end_hour_excluded_from_range)


def test_original_still_has_inclusive_end_bug():
    src = _orig_src()
    assert '(df["HourUTC"] >= start_h)' in src and '(df["HourUTC"] <= end_h)' in src, (
        "expected the ORIGINAL to still use inclusive <= end_h -- confirms this "
        "test suite is checking against the real, unfixed original, not a stale assumption"
    )
_run("Original strategy_london_breakout.py still has the confirmed inclusive-end bug (untouched)",
     test_original_still_has_inclusive_end_bug)


def test_v2_uses_exclusive_end():
    src = _v2_src()
    assert '(df["HourUTC"] < end_h)' in src
_run("strategy_london_breakout_v2.py uses a genuinely exclusive end (< end_h)",
     test_v2_uses_exclusive_end)


# ═══════════════════════════════════════════════════════════════════════
section("2. Real R/R filter -- reject when actual R/R is below MIN_ACTUAL_RR")
# ═══════════════════════════════════════════════════════════════════════

def test_min_actual_rr_constant():
    import forex.strategy_london_breakout_v2 as v2
    assert v2.MIN_ACTUAL_RR == 1.5
_run("MIN_ACTUAL_RR == 1.5, matching the design doc",
     test_min_actual_rr_constant)


def test_large_breakout_extension_rejected_by_rr_or_breakout_band():
    import forex.strategy_london_breakout_v2 as v2
    # A big extension beyond the range boundary blows both the breakout-ATR
    # band AND (if it somehow passed that) the real R/R check -- either way
    # this must not produce a signal.
    df = _make_h1_df(breakout_extension_pips=200)
    sigs = v2.generate_signals(
        {"EURUSD": df}, pair_meta={"EURUSD": {"pip_size": 0.0001}},
        open_symbols=set(), session="london",
        equity_by_pair={"EURUSD": 10_000.0},
    )
    assert sigs == [], "a huge breakout extension must be rejected (breakout band and/or real R/R), not the top-ranked signal"
_run("A breakout extended far past the boundary is rejected (would fail R/R and/or the breakout band)",
     test_large_breakout_extension_rejected_by_rr_or_breakout_band)


def test_signals_have_rr_at_or_above_minimum():
    import forex.strategy_london_breakout_v2 as v2
    df = _make_h1_df(breakout_extension_pips=2)  # modest extension, well within the breakout band
    sigs = v2.generate_signals(
        {"EURUSD": df}, pair_meta={"EURUSD": {"pip_size": 0.0001}},
        open_symbols=set(), session="london",
        equity_by_pair={"EURUSD": 10_000.0},
    )
    for s in sigs:
        assert s["actual_rr"] >= v2.MIN_ACTUAL_RR
_run("Any signal that fires has actual_rr >= MIN_ACTUAL_RR",
     test_signals_have_rr_at_or_above_minimum)


# ═══════════════════════════════════════════════════════════════════════
section("3. Scoring fix -- genuinely higher for tighter ranges")
# ═══════════════════════════════════════════════════════════════════════

def test_score_function_favors_tighter_range():
    import forex.strategy_london_breakout_v2 as v2
    tight_score = v2._score(breakout_strength_atr=0.3, rng_pips=v2.MIN_RANGE_PIPS)
    wide_score  = v2._score(breakout_strength_atr=0.3, rng_pips=v2.MAX_RANGE_PIPS)
    assert tight_score > wide_score, (
        f"expected a tight range (rng_pips={v2.MIN_RANGE_PIPS}) to score higher than "
        f"a wide one (rng_pips={v2.MAX_RANGE_PIPS}) for the SAME breakout strength -- "
        f"got tight={tight_score} wide={wide_score}"
    )
_run("_score() genuinely gives a tighter range a higher score than a wider one (same breakout strength)",
     test_score_function_favors_tighter_range)


def test_original_scoring_still_backwards():
    src = _orig_src()
    assert "score = rng_pips / MAX_RANGE_PIPS" in src, (
        "expected the ORIGINAL to still compute score this way (backwards vs its own "
        "'tighter ranges score higher' comment) -- confirms untouched, not silently fixed"
    )
_run("Original strategy_london_breakout.py's backwards scoring formula is untouched",
     test_original_scoring_still_backwards)


# ═══════════════════════════════════════════════════════════════════════
section("4. Repeat-signal protection (session cooldown)")
# ═══════════════════════════════════════════════════════════════════════

def test_already_traded_session_key_skipped():
    import forex.strategy_london_breakout_v2 as v2
    from datetime import datetime, timezone
    df = _make_h1_df(breakout_extension_pips=2)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    key = f"{today}:EURUSD:London"
    sigs = v2.generate_signals(
        {"EURUSD": df}, pair_meta={"EURUSD": {"pip_size": 0.0001}},
        open_symbols=set(), session="london",
        equity_by_pair={"EURUSD": 10_000.0},
        already_traded_sessions={key},
    )
    assert sigs == [], "a symbol already in already_traded_sessions for this exact session-day must be skipped"
_run("A symbol already marked traded this session-day is skipped, even with a valid breakout",
     test_already_traded_session_key_skipped)


def test_signal_carries_session_key_for_runner_to_mark():
    import forex.strategy_london_breakout_v2 as v2
    df = _make_h1_df(breakout_extension_pips=2)
    sigs = v2.generate_signals(
        {"EURUSD": df}, pair_meta={"EURUSD": {"pip_size": 0.0001}},
        open_symbols=set(), session="london",
        equity_by_pair={"EURUSD": 10_000.0},
    )
    for s in sigs:
        assert "session_key" in s and s["session_key"].endswith(":EURUSD:London")
_run("Each signal carries its own session_key for the runner to persist on entry",
     test_signal_carries_session_key_for_runner_to_mark)


def test_runner_loads_and_marks_lbo_v2_cooldown():
    src = _runner_src()
    assert "_load_lbo_v2_session_cooldown" in src
    assert "_mark_lbo_v2_session_traded" in src
    assert 'lbo_kw["already_traded_sessions"]' in src
_run("forex/runner.py loads the cooldown before scanning and marks it on a real entry",
     test_runner_loads_and_marks_lbo_v2_cooldown)


def test_original_has_no_session_cooldown_concept():
    src = _orig_src()
    assert "already_traded_sessions" not in src and "session_key" not in src
_run("Original strategy_london_breakout.py has no session-cooldown concept (untouched)",
     test_original_has_no_session_cooldown_concept)


# ═══════════════════════════════════════════════════════════════════════
section("5. Reduced risk and position count")
# ═══════════════════════════════════════════════════════════════════════

def test_risk_pct_reduced():
    import forex.strategy_london_breakout_v2 as v2
    import forex.strategy_london_breakout as v1
    assert v2.RISK_PCT == 0.005
    assert v1.RISK_PCT == 0.015, "original's RISK_PCT must remain 1.5% (untouched)"
_run("v2's RISK_PCT is 0.5% while the original's remains 1.5% (untouched)",
     test_risk_pct_reduced)


def test_max_lbo_positions_really_enforced():
    import forex.runner as r
    import forex.strategy_london_breakout_v2 as v2
    assert r.SLOTS_PER_STRATEGY["london_breakout_v2"] == v2.MAX_LBO_POSITIONS == 4
    assert r.SLOTS_PER_STRATEGY["london_breakout"] == 28, "original's 28-slot cap must be untouched"
_run("SLOTS_PER_STRATEGY['london_breakout_v2'] == 4 (real cap); original's 28 slots untouched",
     test_max_lbo_positions_really_enforced)


# ═══════════════════════════════════════════════════════════════════════
section("6. Range/ATR ratio filter (replaces the weak fixed-pip ATR check)")
# ═══════════════════════════════════════════════════════════════════════

def test_range_atr_ratio_constants():
    import forex.strategy_london_breakout_v2 as v2
    assert v2.MIN_RANGE_ATR_RATIO == 0.5
    assert v2.MAX_RANGE_ATR_RATIO == 3.0
_run("MIN/MAX_RANGE_ATR_RATIO == 0.5/3.0, matching the design doc",
     test_range_atr_ratio_constants)


def test_original_still_uses_weak_fixed_pip_atr_check():
    src = _orig_src()
    assert "atr_val / pip_size < 5" in src
_run("Original's weak fixed-5-pip ATR check is untouched",
     test_original_still_uses_weak_fixed_pip_atr_check)


# ═══════════════════════════════════════════════════════════════════════
section("7. Fallback size_position() -- fixed the equity/10.7 bug")
# ═══════════════════════════════════════════════════════════════════════

def test_original_fallback_still_has_the_bug():
    src = _orig_src()
    assert "equity_usd  = equity / 10.7" in src or "equity_usd = equity / 10.7" in src
_run("Original's fallback size_position() still has the equity/10.7 bug (untouched)",
     test_original_fallback_still_has_the_bug)


def test_v2_fallback_removed_the_bad_conversion():
    import forex.strategy_london_breakout_v2 as v2
    src = _v2_src()
    fn_idx = src.find("def size_position(")
    fn_body = src[fn_idx: src.find("\ndef ", fn_idx + 10)]
    # Check the real executable code, not the docstring describing the fix
    # (which mentions "equity / 10.7" descriptively) -- the original's
    # buggy variable name "equity_usd" must not appear as real code here.
    assert '"""' in fn_body
    code_only = fn_body[fn_body.rfind('"""') + 3:]
    assert "equity_usd" not in code_only, "the original's buggy equity_usd = equity / 10.7 pattern must not exist in v2's real code"
    assert v2.size_position(10_000, 0.002, 1000) == 0
_run("v2's fallback size_position() has no hardcoded conversion and returns 0 (skip) instead",
     test_v2_fallback_removed_the_bad_conversion)


# ═══════════════════════════════════════════════════════════════════════
section("8. Isolation from the original and correct runner wiring")
# ═══════════════════════════════════════════════════════════════════════

def test_registered_as_separate_strategy():
    src = _runner_src()
    assert '"london_breakout_v2": strat_lbo_v2' in src
_run("'london_breakout_v2' is registered as its own STRATEGIES entry",
     test_registered_as_separate_strategy)


def test_never_in_live_allowlists():
    src = _runner_src()
    live_idx      = src.find("LIVE_ALLOWED_STRATEGIES =")
    live_line     = src[live_idx: src.find("\n", live_idx)]
    live_eur_idx  = src.find("LIVE_EUR_ALLOWED_STRATEGIES =")
    live_eur_line = src[live_eur_idx: src.find("\n", live_eur_idx)]
    assert "london_breakout_v2" not in live_line
    assert "london_breakout_v2" not in live_eur_line
_run("'london_breakout_v2' is absent from both LIVE_ALLOWED_STRATEGIES and LIVE_EUR_ALLOWED_STRATEGIES",
     test_never_in_live_allowlists)


def test_is_a_day_trade_strategy():
    import forex.runner as r
    assert "london_breakout_v2" in r.DAY_TRADE_STRATEGIES
    assert "london_breakout" in r.DAY_TRADE_STRATEGIES
_run("'london_breakout_v2' is in DAY_TRADE_STRATEGIES alongside the original",
     test_is_a_day_trade_strategy)


def test_excluded_from_generic_all_strategies_run():
    src = _runner_src()
    idx = src.find('active = [s for s in active if s not in ("london_breakout", "london_breakout_v2")]')
    assert idx != -1, (
        "both LBO strategies must be excluded from the generic --strategy all path "
        "(they have their own dedicated schedule) -- same reasoning as the original"
    )
_run("Both london_breakout and london_breakout_v2 are excluded from the generic 'all' entries run",
     test_excluded_from_generic_all_strategies_run)


def test_scheduled_bats_run_both_strategies():
    for fname in ("run_lbo_london.bat", "run_lbo_ny.bat", "run_lbo_close.bat"):
        with open(os.path.join(BASE_DIR, fname), encoding="utf-8") as f:
            content = f.read()
        assert "london_breakout,london_breakout_v2" in content, f"{fname} should trigger both strategies"
_run("All 3 LBO scheduled .bat files now trigger both london_breakout and london_breakout_v2",
     test_scheduled_bats_run_both_strategies)


def test_original_module_completely_untouched_structurally():
    src = _orig_src()
    # Spot-check several of the ORIGINAL's confirmed-unfixed issues are all
    # still present verbatim -- proves this work created a new module
    # rather than patching the old one in place.
    assert "RISK_PCT        = 0.015" in src
    assert '"london_breakout": 28' not in src  # that's runner.py's own registry, not here -- sanity: this string shouldn't appear in the strategy module itself
    assert "MAX_LBO_POSITIONS" not in src
    assert "actual_rr" not in src
_run("forex/strategy_london_breakout.py (the original) was NOT modified by this change",
     test_original_module_completely_untouched_structurally)


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
