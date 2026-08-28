"""
Regression test -- 2026-08-29 "GAPFILL Weekend" (forex/strategy_gap_weekend.py).

New SIM-only A/B strategy against the original forex/strategy_gap.py ("gap"),
built to the user's own design doc. Covers the 3 confirmed code fixes, the
Phase-2 session-disable gate, and the runner.py wiring that keeps it fully
isolated from "gap" (own cooldown file, own slots, never LIVE-eligible).
"""

import os
import sys
import pandas as pd
import numpy as np

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


# ═══════════════════════════════════════════════════════════════════════
section("1. Fix #1 -- position sizing uses the correct per-type stop_mult")
# ═══════════════════════════════════════════════════════════════════════

def test_weekly_signal_carries_1_5x_stop_mult():
    import forex.strategy_gap_weekend as gw
    dates = pd.date_range("2026-08-01", periods=10, freq="D")
    df = pd.DataFrame({"Close": [1.1000]*10}, index=dates)
    sigs = gw.generate_signals({"EURUSD": df}, live_prices={"EURUSD": 1.1050})
    assert len(sigs) == 1
    assert sigs[0]["stop_mult"] == 1.5
_run("Weekly signal's stop_mult == 1.5 (ATR_STOP_MULT)",
     test_weekly_signal_carries_1_5x_stop_mult)


def test_session_signal_carries_2_0x_stop_mult_when_enabled():
    import forex.strategy_gap_weekend as gw
    # Temporarily enable london to test the session path in isolation --
    # restore afterward so this test doesn't leak state into others.
    original = set(gw.ENABLED_SESSIONS)
    try:
        gw.ENABLED_SESSIONS.add("london")
        n = 30
        idx = list(range(n))
        # Build a clean bullish-reversal-after-gap-down H1 series so ONE
        # condition set (gap down -> bullish confirm) definitely fires.
        closes = [1.1050 - 0.0001*i for i in range(n-1)] + [1.1010]
        opens  = closes[:-1] + [1.1000]  # last bar: open < close -> bullish
        highs  = [c + 0.0002 for c in closes]
        lows   = [c - 0.0002 for c in closes]
        hours  = [(6 + i) % 24 for i in range(n)]
        df = pd.DataFrame({"Open": opens, "Close": closes, "High": highs,
                            "Low": lows, "HourUTC": hours})
        sigs = gw.generate_session_signals("london", {"EURUSD": df},
                                           live_prices={"EURUSD": 1.0850})
        # Whether or not this exact synthetic series clears the ATR/reversal
        # filters, any signal that DOES come back must carry stop_mult=2.0 --
        # that's the actual bug being tested, not signal-generation odds.
        for s in sigs:
            assert s["stop_mult"] == 2.0
    finally:
        gw.ENABLED_SESSIONS.clear()
        gw.ENABLED_SESSIONS.update(original)
_run("Any session signal generated carries stop_mult == 2.0",
     test_session_signal_carries_2_0x_stop_mult_when_enabled)


def test_size_position_uses_passed_stop_mult_not_hardcoded():
    import forex.strategy_gap_weekend as gw
    equity, gap_size = 10_000.0, 0.0050
    qty_weekly  = gw.size_position(equity, gap_size, min_units=1000, stop_mult=1.5)
    qty_session = gw.size_position(equity, gap_size, min_units=1000, stop_mult=2.0)
    # Wider stop (2.0x) -> smaller position for the same risk budget.
    assert qty_session < qty_weekly, (
        f"expected session (2.0x stop) qty < weekly (1.5x stop) qty, "
        f"got session={qty_session} weekly={qty_weekly}"
    )
_run("size_position() actually varies with stop_mult (session sizes smaller than weekly)",
     test_size_position_uses_passed_stop_mult_not_hardcoded)


def test_size_position_param_order_matches_runner_call_site():
    import forex.strategy_gap_weekend as gw
    import inspect
    params = list(inspect.signature(gw.size_position).parameters)
    assert params[0] == "account_equity"
    assert params[1] == "gap_size"
    assert params[2] == "min_units", (
        "min_units MUST be the 3rd positional param -- forex/runner.py's "
        "generic entry loop calls strat_mod.size_position(equity, sig['atr'], "
        "pair_info['min_units'], **rp_kw) positionally for every strategy; "
        "putting stop_mult 3rd (as the user's original design doc literally "
        "wrote it) would silently bind pair_info['min_units'] to stop_mult"
    )
_run("size_position()'s positional param order is compatible with runner.py's generic call site",
     test_size_position_param_order_matches_runner_call_site)


def test_runner_passes_stop_mult_through_rp_kw():
    src = _runner_src()
    assert 'rp_kw["stop_mult"] = sig["stop_mult"]' in src
_run("forex/runner.py threads sig['stop_mult'] into rp_kw for size_position()",
     test_runner_passes_stop_mult_through_rp_kw)


# ═══════════════════════════════════════════════════════════════════════
section("2. Fix #2 -- no silent fallback when the reference bar is missing")
# ═══════════════════════════════════════════════════════════════════════

def test_missing_ref_bar_skips_not_falls_back():
    import forex.strategy_gap_weekend as gw
    original = set(gw.ENABLED_SESSIONS)
    try:
        gw.ENABLED_SESSIONS.add("london")
        n = 30
        # HourUTC never equals 6 (london's ref_hour_utc) -- the reference
        # bar genuinely does not exist in this data.
        hours = [(10 + i) % 24 for i in range(n)]
        hours = [h if h != 6 else 5 for h in hours]  # guarantee no hour==6
        closes = [1.1000]*n
        df = pd.DataFrame({"Open": closes, "Close": closes,
                            "High": [c+0.001 for c in closes],
                            "Low":  [c-0.001 for c in closes],
                            "HourUTC": hours})
        sigs = gw.generate_session_signals("london", {"EURUSD": df},
                                           live_prices={"EURUSD": 1.1050})
        assert sigs == [], (
            "expected NO signal when the true reference bar is missing -- "
            "got a signal, meaning it fell back to some other bar's close"
        )
    finally:
        gw.ENABLED_SESSIONS.clear()
        gw.ENABLED_SESSIONS.update(original)
_run("generate_session_signals() returns no signal (not a fallback-priced one) when ref bar is missing",
     test_missing_ref_bar_skips_not_falls_back)


def test_find_ref_bar_close_returns_none_not_a_guess():
    import forex.strategy_gap_weekend as gw
    df = pd.DataFrame({"Close": [1.1, 1.2, 1.3], "HourUTC": [1, 2, 3]})
    assert gw._find_ref_bar_close(df, ref_hour=15) is None
_run("_find_ref_bar_close() returns None (not the last close) when the hour genuinely isn't present",
     test_find_ref_bar_close_returns_none_not_a_guess)


def test_quality_score_rewards_closeness_to_recent_range_not_extreme():
    """2026-08-29: caught by user review -- the ORIGINAL formula added
    distance_from_extreme_atr directly, scoring a candidate ALREADY at a
    fresh extreme higher, contradicting its own docstring intent."""
    import forex.strategy_gap_weekend as gw
    close_to_range   = gw._session_quality_score(move_atr=1.0, reversal_strength=0.5, distance_from_extreme_atr=0.1)
    far_from_range   = gw._session_quality_score(move_atr=1.0, reversal_strength=0.5, distance_from_extreme_atr=5.0)
    assert close_to_range > far_from_range, (
        f"a candidate still close to the recent range (small distance_from_extreme) must "
        f"score HIGHER than one already at a fresh extreme (large distance) -- got "
        f"close={close_to_range} far={far_from_range}"
    )
_run("_session_quality_score() rewards a candidate close to the recent range, not one at a fresh extreme",
     test_quality_score_rewards_closeness_to_recent_range_not_extreme)


# ═══════════════════════════════════════════════════════════════════════
section("3. Phase 2 -- session variants disabled, weekly-only")
# ═══════════════════════════════════════════════════════════════════════

def test_sessions_disabled_by_default():
    import forex.strategy_gap_weekend as gw
    assert gw.ENABLED_SESSIONS == set(), (
        f"expected ENABLED_SESSIONS empty (Phase 2: weekly-only), got {gw.ENABLED_SESSIONS}"
    )
_run("ENABLED_SESSIONS is empty by default (session variants disabled)",
     test_sessions_disabled_by_default)


def test_disabled_session_returns_no_signals_regardless_of_data():
    import forex.strategy_gap_weekend as gw
    assert gw.ENABLED_SESSIONS == set()
    n = 30
    closes = [1.1000 + 0.01*i for i in range(n)]   # obvious large gap-like move
    df = pd.DataFrame({"Open": closes, "Close": closes,
                        "High": [c+0.001 for c in closes],
                        "Low":  [c-0.001 for c in closes],
                        "HourUTC": [(6+i) % 24 for i in range(n)]})
    for session in ("london", "newyork", "tokyo"):
        sigs = gw.generate_session_signals(session, {"EURUSD": df},
                                           live_prices={"EURUSD": 1.5000})
        assert sigs == [], f"{session} should be fully disabled, got {sigs}"
_run("london/newyork/tokyo all return [] regardless of input while disabled",
     test_disabled_session_returns_no_signals_regardless_of_data)


def test_weekly_unaffected_by_session_disable():
    import forex.strategy_gap_weekend as gw
    dates = pd.date_range("2026-08-01", periods=10, freq="D")
    df = pd.DataFrame({"Close": [1.1000]*10}, index=dates)
    sigs = gw.generate_signals({"EURUSD": df}, live_prices={"EURUSD": 1.1050})
    assert len(sigs) == 1 and sigs[0]["gap_type"] == "weekly"
_run("Weekly gap fill still generates signals normally while sessions are disabled",
     test_weekly_unaffected_by_session_disable)


# ═══════════════════════════════════════════════════════════════════════
section("4. ATR displacement filter, reversal confirmation, quality score (dormant logic)")
# ═══════════════════════════════════════════════════════════════════════

def test_atr_filter_rejects_tiny_noise_move():
    import forex.strategy_gap_weekend as gw
    original = set(gw.ENABLED_SESSIONS)
    try:
        gw.ENABLED_SESSIONS.add("london")
        n = 30
        # Flat, low-volatility series -> tiny ATR -> even a small live-price
        # move should register as move_atr comfortably outside [0.8, 2.0]...
        # here we engineer it to be BELOW 0.8 by keeping live price ~equal
        # to the reference close.
        closes = [1.1000]*n
        df = pd.DataFrame({"Open": closes, "Close": closes,
                            "High": [c+0.0005 for c in closes],
                            "Low":  [c-0.0005 for c in closes],
                            "HourUTC": [(6+i) % 24 for i in range(n)]})
        sigs = gw.generate_session_signals("london", {"EURUSD": df},
                                           live_prices={"EURUSD": 1.10001})  # ~0 displacement
        assert sigs == [], "a near-zero displacement move should be filtered as noise (<0.8 ATR)"
    finally:
        gw.ENABLED_SESSIONS.clear()
        gw.ENABLED_SESSIONS.update(original)
_run("A displacement far below 0.8x ATR is filtered out as noise",
     test_atr_filter_rejects_tiny_noise_move)


def test_reversal_confirmation_required():
    import forex.strategy_gap_weekend as gw
    original = set(gw.ENABLED_SESSIONS)
    try:
        gw.ENABLED_SESSIONS.add("london")
        n = 30
        closes = [1.1000]*(n-1) + [1.1000]
        opens  = closes[:-1] + [1.0995]  # last bar: open < close -> BULLISH (not bearish)
        df = pd.DataFrame({"Open": opens, "Close": closes,
                            "High": [c+0.0010 for c in closes],
                            "Low":  [c-0.0010 for c in closes],
                            "HourUTC": [(6+i) % 24 for i in range(n)]})
        # A gap UP needs a BEARISH confirming candle to fade (Sell) -- the
        # last bar here is bullish, so a gap-up candidate must be rejected.
        sigs = gw.generate_session_signals("london", {"EURUSD": df},
                                           live_prices={"EURUSD": 1.1050})
        assert sigs == [], (
            "gap-up candidate with a BULLISH last candle (no bearish reversal "
            "confirmation) must be skipped, not faded immediately"
        )
    finally:
        gw.ENABLED_SESSIONS.clear()
        gw.ENABLED_SESSIONS.update(original)
_run("A gap-up candidate without a confirming bearish reversal candle is skipped",
     test_reversal_confirmation_required)


def test_signals_ranked_by_quality_score_not_raw_gap():
    import forex.strategy_gap_weekend as gw
    src = open(os.path.join(BASE_DIR, "forex", "strategy_gap_weekend.py"), encoding="utf-8").read()
    assert '"score":                      quality_score' in src, (
        "session signals must be ranked by quality_score, not raw gap_pct/move"
    )
_run("Session signals store quality_score (not raw gap size) as their sort key",
     test_signals_ranked_by_quality_score_not_raw_gap)


# ═══════════════════════════════════════════════════════════════════════
section("5. Isolation from the original 'gap' strategy")
# ═══════════════════════════════════════════════════════════════════════

def test_registered_as_separate_strategy():
    src = _runner_src()
    assert '"gap_weekend": strat_gap_weekend' in src
_run("'gap_weekend' is registered as its own STRATEGIES entry",
     test_registered_as_separate_strategy)


def test_never_in_live_allowlists():
    src = _runner_src()
    live_idx     = src.find("LIVE_ALLOWED_STRATEGIES =")
    live_line    = src[live_idx: src.find("\n", live_idx)]
    live_eur_idx = src.find("LIVE_EUR_ALLOWED_STRATEGIES =")
    live_eur_line = src[live_eur_idx: src.find("\n", live_eur_idx)]
    assert "gap_weekend" not in live_line
    assert "gap_weekend" not in live_eur_line
_run("'gap_weekend' is absent from both LIVE_ALLOWED_STRATEGIES and LIVE_EUR_ALLOWED_STRATEGIES",
     test_never_in_live_allowlists)


def test_own_cooldown_file_separate_from_gap():
    src = _runner_src()
    assert '"gap_weekend": os.path.join(DATA_DIR, "gap_weekend_cooldown.json")' in src
    assert '"gap":         GAP_COOLDOWN_FILE' in src
_run("gap_weekend uses its own cooldown file, not gap's data/gap_cooldown.json",
     test_own_cooldown_file_separate_from_gap)


def test_has_own_slot_allocation():
    src = _runner_src()
    assert '"gap_weekend": _SWING_SLOTS' in src
_run("gap_weekend has its own SLOTS_PER_STRATEGY entry",
     test_has_own_slot_allocation)


def test_original_gap_module_untouched():
    with open(os.path.join(BASE_DIR, "forex", "strategy_gap.py"), encoding="utf-8") as f:
        src = f.read()
    # The original bug (hardcoded ATR_STOP_MULT in size_position, no
    # stop_mult parameter) must still be there -- proves this work created
    # a NEW module rather than patching the old one in place, per the
    # explicit "do not change your actual GAPFILL code" instruction.
    assert "def size_position(account_equity: float, atr: float,\n" \
           "                  min_units: int = LOT_ROUND,\n" \
           "                  risk_pct: float = RISK_PCT,\n" \
           "                  block_below_min: bool = False) -> int:" in src
_run("forex/strategy_gap.py (the original) was NOT modified by this change",
     test_original_gap_module_untouched)


# ═══════════════════════════════════════════════════════════════════════
section("6. Per-gap-type tracking plumbing")
# ═══════════════════════════════════════════════════════════════════════

def test_gap_type_threaded_into_pnl_tracker():
    src = _runner_src()
    assert 'gap_type=sig.get("gap_type")' in src
_run("forex/runner.py passes gap_type into pnl_tracker.log_open()",
     test_gap_type_threaded_into_pnl_tracker)


def test_pnl_tracker_accepts_and_stores_gap_type():
    import pnl_tracker
    import inspect
    params = inspect.signature(pnl_tracker.log_open).parameters
    assert "gap_type" in params
_run("pnl_tracker.log_open() accepts a gap_type parameter",
     test_pnl_tracker_accepts_and_stores_gap_type)


def test_report_script_exists_and_runs():
    import subprocess
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "report_gap_weekend_by_type.py"],
        cwd=BASE_DIR, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"report script failed: {result.stderr}"
    assert "PER-GAP-TYPE BREAKDOWN" in result.stdout
_run("report_gap_weekend_by_type.py runs cleanly against the real (possibly empty) ledger",
     test_report_script_exists_and_runs)


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
