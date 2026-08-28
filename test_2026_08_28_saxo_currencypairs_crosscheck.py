"""
Regression tests -- 2026-08-28 Saxo /ref/v1/currencypairs cross-check.

Explicit user request: "Get all supported currency pairs from saxo add
in the relevant groups we have now... check which one we do not have
and which one we already have, add the missing one."

Called Saxo's real /ref/v1/currencypairs endpoint live (SIM): 186 total
supported pairs. Cross-checked against our then-149-pair universe: all
149 confirmed still valid (zero pairs we hold that Saxo no longer
supports), 37 genuinely missing. 2 of those 37 (XAUUSD/XAGUSD) were
deliberately skipped -- explicit user decision -- because they share the
EXACT SAME Uics (8176/8178) as futures/universe.py's GC/SI markets. The
remaining 35 were added: 15 joined CORE_SYMBOLS, 3 joined SCANDI_SYMBOLS,
and 17 precious-metal pairs became a brand-new METALS_SYMBOLS tier
(deliberately not folded into the fiat-currency tier system). Every
field (uic, pip_size, min_units) was pulled live from Saxo's
/ref/v1/instruments/details, not guessed.
"""

import os
import subprocess
import sys

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


# ═══════════════════════════════════════════════════════════════════════
section("1. forex.universe -- tier partition integrity after the 35-pair addition")
# ═══════════════════════════════════════════════════════════════════════

def test_pairs_total_grew_by_exactly_35():
    from forex.universe import PAIRS
    assert len(PAIRS) == 184, f"expected 149 + 35 = 184 total pairs, got {len(PAIRS)}"
_run("forex.universe.PAIRS grew by exactly 35 (149 -> 184)",
     test_pairs_total_grew_by_exactly_35)


def test_no_duplicate_symbols_or_uics():
    from forex.universe import PAIRS
    symbols = [p["symbol"] for p in PAIRS]
    uics = [p["uic"] for p in PAIRS]
    assert len(symbols) == len(set(symbols)), f"duplicate symbols: {[s for s in set(symbols) if symbols.count(s) > 1]}"
    assert len(uics) == len(set(uics)), f"duplicate uics: {[u for u in set(uics) if uics.count(u) > 1]}"
_run("forex.universe.PAIRS has no duplicate symbols or Uics",
     test_no_duplicate_symbols_or_uics)


def test_xauusd_xagusd_deliberately_excluded():
    from forex.universe import PAIRS
    symbols = {p["symbol"] for p in PAIRS}
    assert "XAUUSD" not in symbols and "XAGUSD" not in symbols, (
        "XAUUSD/XAGUSD must never be added to forex -- they share the exact "
        "same Uics (8176/8178) as futures/universe.py's GC/SI, explicit "
        "user decision to avoid a cross-module position-pooling risk"
    )
_run("forex.universe.PAIRS deliberately excludes XAUUSD/XAGUSD (already covered by futures GC/SI)",
     test_xauusd_xagusd_deliberately_excluded)


def test_metals_symbols_is_a_new_dedicated_tier():
    from forex.universe import METALS_SYMBOLS
    assert len(METALS_SYMBOLS) == 17, f"expected 17 metals pairs, got {len(METALS_SYMBOLS)}"
    for sym in METALS_SYMBOLS:
        assert sym[:3] in ("XAU", "XAG", "XPT") or sym[3:6] in ("XAU", "XAG", "XPT"), (
            f"{sym} in METALS_SYMBOLS doesn't look like a metal pair"
        )
_run("forex.universe.METALS_SYMBOLS is a new 17-pair tier, every symbol genuinely metal-related",
     test_metals_symbols_is_a_new_dedicated_tier)


def test_core_scandi_metals_exotic_exactly_partition_all_pairs():
    from forex.universe import PAIRS, CORE_SYMBOLS, SCANDI_SYMBOLS, METALS_SYMBOLS, EXOTIC_SYMBOLS
    all_symbols = {p["symbol"] for p in PAIRS}
    tiers = [CORE_SYMBOLS, SCANDI_SYMBOLS, METALS_SYMBOLS, EXOTIC_SYMBOLS]
    for i, t1 in enumerate(tiers):
        for t2 in tiers[i + 1:]:
            assert not (t1 & t2), f"two tiers overlap: {t1 & t2}"
    union = set().union(*tiers)
    assert union == all_symbols, (
        f"the 4 tiers must exactly partition all pairs -- "
        f"missing: {all_symbols - union}, extra: {union - all_symbols}"
    )
_run("forex.universe: CORE+SCANDI+METALS+EXOTIC exactly partition all 184 pairs, no gaps/overlap",
     test_core_scandi_metals_exotic_exactly_partition_all_pairs)


def test_high_volume_and_core_standard_still_partition_core():
    from forex.universe import CORE_SYMBOLS, HIGH_VOLUME_SYMBOLS, CORE_STANDARD_SYMBOLS
    assert len(CORE_SYMBOLS) == 49, f"expected CORE_SYMBOLS to grow to 49 (34+15), got {len(CORE_SYMBOLS)}"
    assert len(HIGH_VOLUME_SYMBOLS) == 17, "HIGH_VOLUME_SYMBOLS must stay unchanged at 17 (no new pairs joined it)"
    assert len(CORE_STANDARD_SYMBOLS) == 32, f"expected CORE_STANDARD_SYMBOLS to grow to 32 (49-17), got {len(CORE_STANDARD_SYMBOLS)}"
    assert not (HIGH_VOLUME_SYMBOLS & CORE_STANDARD_SYMBOLS)
    assert (HIGH_VOLUME_SYMBOLS | CORE_STANDARD_SYMBOLS) == CORE_SYMBOLS
_run("forex.universe: HIGH_VOLUME (17, unchanged) + CORE_STANDARD (32, grew) still exactly partition CORE (49)",
     test_high_volume_and_core_standard_still_partition_core)


def test_exotic_regional_groups_still_partition_exotic():
    from forex.universe import (EXOTIC_SYMBOLS, EXOTIC_ASIA_SYMBOLS, EXOTIC_EUROPE_SYMBOLS,
                                 EXOTIC_CARRY_SYMBOLS, EXOTIC_LATAM_MIDEAST_SYMBOLS)
    assert len(EXOTIC_SYMBOLS) == 83, "EXOTIC_SYMBOLS must stay unchanged at 83 -- none of the new pairs are exotic-fiat"
    union = EXOTIC_ASIA_SYMBOLS | EXOTIC_EUROPE_SYMBOLS | EXOTIC_CARRY_SYMBOLS | EXOTIC_LATAM_MIDEAST_SYMBOLS
    assert union == EXOTIC_SYMBOLS
_run("forex.universe: the 4 EXOTIC regions still exactly partition the unchanged 83-pair EXOTIC_SYMBOLS",
     test_exotic_regional_groups_still_partition_exotic)


def test_get_tier_recognizes_metals():
    from forex.universe import get_tier
    assert get_tier("XAUEUR") == "metals"
    assert get_tier("XAGJPY") == "metals"
    assert get_tier("EURUSD") == "core"
_run("forex.universe.get_tier() correctly returns 'metals' for the new tier",
     test_get_tier_recognizes_metals)


# ═══════════════════════════════════════════════════════════════════════
section("2. forex/runner.py -- SLOTS_PER_STRATEGY tracks the real universe size")
# ═══════════════════════════════════════════════════════════════════════

def test_slots_per_strategy_matches_real_universe_size():
    import forex.runner as r
    from forex.universe import PAIRS
    for strat in ("ema", "rsi", "donchian", "bb", "pullback", "gap",
                  "supertrend", "zscore", "ml", "cnn_lstm"):
        assert r.SLOTS_PER_STRATEGY[strat] == len(PAIRS), (
            f"{strat}'s slot cap ({r.SLOTS_PER_STRATEGY[strat]}) must match the "
            f"real universe size ({len(PAIRS)}), not a stale hardcoded literal -- "
            f"found stale at 117 (pre-SCANDI) while investigating this cross-check"
        )
    assert r.SLOTS_PER_STRATEGY["london_breakout"] == 28, "LBO's own fixed 28-pair subset cap must be untouched"
_run("forex/runner.SLOTS_PER_STRATEGY: every swing strategy's cap matches len(PAIRS), not a stale 117",
     test_slots_per_strategy_matches_real_universe_size)


# ═══════════════════════════════════════════════════════════════════════
section("3. Blackbox -- forex_dashboard.py --once shows the expanded universe correctly")
# ═══════════════════════════════════════════════════════════════════════

def _run_dashboard():
    return subprocess.run(
        [sys.executable, "forex_dashboard.py", "--once"],
        cwd=BASE_DIR, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )


def test_dashboard_runs_cleanly():
    proc = _run_dashboard()
    assert proc.returncode == 0, f"expected a clean exit(0), got {proc.returncode}: {proc.stderr}"
_run("forex_dashboard.py --once runs cleanly with the expanded 184-pair universe",
     test_dashboard_runs_cleanly)


def test_dashboard_shows_184_total_pairs():
    proc = _run_dashboard()
    assert "184 FX Pairs" in proc.stdout, "expected the header to show the real total (184), not the stale 149"
_run("forex_dashboard.py --once shows 184 total FX pairs in the header",
     test_dashboard_shows_184_total_pairs)


def test_dashboard_shows_metals_section():
    proc = _run_dashboard()
    out = proc.stdout
    assert "OPEN POSITIONS — METALS" in out
    assert "STRATEGY BREAKDOWN — METALS" in out
_run("forex_dashboard.py --once shows a dedicated METALS section (positions + breakdown)",
     test_dashboard_shows_metals_section)


def test_dashboard_shows_updated_core_standard_and_scandi_counts():
    proc = _run_dashboard()
    out = proc.stdout
    assert "CORE STANDARD (32 pairs" in out, "expected CORE STANDARD to show 32, not the stale 17"
    assert "SCANDI (35 pairs" in out, "expected SCANDI to show 35, not the stale 32"
_run("forex_dashboard.py --once shows the updated CORE STANDARD (32) and SCANDI (35) counts",
     test_dashboard_shows_updated_core_standard_and_scandi_counts)


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
