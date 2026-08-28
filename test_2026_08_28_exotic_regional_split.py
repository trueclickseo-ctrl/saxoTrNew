"""
Regression tests -- 2026-08-28 EXOTIC 83-pair regional dashboard split.

Explicit user request: "we have core 83 Core Exotic Pairs, you can divide
this large in to smaller groups or more meaningful group so we will find
tradeable pairs?" -- answered with a 4-way regional/thematic split by each
pair's own non-G10 (exotic) currency, verified programmatically as a
strict, non-overlapping, exhaustive partition of EXOTIC_SYMBOLS:

  EM ASIA (CNH/HKD/SGD/THB)        -- 30 pairs
  EM EUROPE (CZK/HUF/PLN/RON)      -- 25 pairs
  HIGH-YIELD/CARRY (TRY/ZAR)       -- 17 pairs
  LATAM/MIDEAST (MXN/ILS/AED)      -- 11 pairs

Unlike the CORE_STANDARD split (which replaced the standalone CORE
section entirely), the blended 83-pair EXOTIC section is deliberately
KEPT here -- the four regions are additive detail alongside it, not a
replacement, since the user asked to divide it for visibility, not to
remove the aggregate view.
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
section("1. forex.universe -- EXOTIC regional groups exactly partition EXOTIC_SYMBOLS")
# ═══════════════════════════════════════════════════════════════════════

def test_exotic_symbols_is_exactly_83_pairs():
    from forex.universe import EXOTIC_SYMBOLS
    assert len(EXOTIC_SYMBOLS) == 83, f"expected exactly 83 pairs, got {len(EXOTIC_SYMBOLS)}"
_run("forex.universe: EXOTIC_SYMBOLS is exactly 83 pairs",
     test_exotic_symbols_is_exactly_83_pairs)


def test_exotic_regional_group_sizes():
    from forex.universe import (EXOTIC_ASIA_SYMBOLS, EXOTIC_EUROPE_SYMBOLS,
                                 EXOTIC_CARRY_SYMBOLS, EXOTIC_LATAM_MIDEAST_SYMBOLS)
    assert len(EXOTIC_ASIA_SYMBOLS) == 30, f"ASIA: expected 30, got {len(EXOTIC_ASIA_SYMBOLS)}"
    assert len(EXOTIC_EUROPE_SYMBOLS) == 25, f"EUROPE: expected 25, got {len(EXOTIC_EUROPE_SYMBOLS)}"
    assert len(EXOTIC_CARRY_SYMBOLS) == 17, f"CARRY: expected 17, got {len(EXOTIC_CARRY_SYMBOLS)}"
    assert len(EXOTIC_LATAM_MIDEAST_SYMBOLS) == 11, f"LATAM/MIDEAST: expected 11, got {len(EXOTIC_LATAM_MIDEAST_SYMBOLS)}"
_run("forex.universe: the 4 EXOTIC regional groups have the expected sizes (30/25/17/11)",
     test_exotic_regional_group_sizes)


def test_exotic_regional_groups_never_overlap():
    from forex.universe import (EXOTIC_ASIA_SYMBOLS, EXOTIC_EUROPE_SYMBOLS,
                                 EXOTIC_CARRY_SYMBOLS, EXOTIC_LATAM_MIDEAST_SYMBOLS)
    groups = [EXOTIC_ASIA_SYMBOLS, EXOTIC_EUROPE_SYMBOLS, EXOTIC_CARRY_SYMBOLS, EXOTIC_LATAM_MIDEAST_SYMBOLS]
    for i, g1 in enumerate(groups):
        for g2 in groups[i + 1:]:
            assert not (g1 & g2), f"two EXOTIC regional groups overlap: {g1 & g2}"
_run("forex.universe: no two EXOTIC regional groups ever share a pair",
     test_exotic_regional_groups_never_overlap)


def test_exotic_regional_groups_exactly_partition_exotic_symbols():
    from forex.universe import (EXOTIC_SYMBOLS, EXOTIC_ASIA_SYMBOLS, EXOTIC_EUROPE_SYMBOLS,
                                 EXOTIC_CARRY_SYMBOLS, EXOTIC_LATAM_MIDEAST_SYMBOLS)
    union = EXOTIC_ASIA_SYMBOLS | EXOTIC_EUROPE_SYMBOLS | EXOTIC_CARRY_SYMBOLS | EXOTIC_LATAM_MIDEAST_SYMBOLS
    assert union == EXOTIC_SYMBOLS, (
        f"the 4 regional groups must exactly equal EXOTIC_SYMBOLS, no gaps -- "
        f"missing: {EXOTIC_SYMBOLS - union}, extra: {union - EXOTIC_SYMBOLS}"
    )
_run("forex.universe: the 4 EXOTIC regional groups exactly partition EXOTIC_SYMBOLS (30+25+17+11=83)",
     test_exotic_regional_groups_exactly_partition_exotic_symbols)


def test_exotic_regional_groups_every_symbol_reports_exotic_tier():
    from forex.universe import (EXOTIC_ASIA_SYMBOLS, EXOTIC_EUROPE_SYMBOLS,
                                 EXOTIC_CARRY_SYMBOLS, EXOTIC_LATAM_MIDEAST_SYMBOLS, get_tier)
    for group in (EXOTIC_ASIA_SYMBOLS, EXOTIC_EUROPE_SYMBOLS, EXOTIC_CARRY_SYMBOLS, EXOTIC_LATAM_MIDEAST_SYMBOLS):
        for sym in group:
            assert get_tier(sym) == "exotic", f"{sym} should report tier='exotic'"
_run("forex.universe: every EXOTIC regional-group pair still reports get_tier()=='exotic'",
     test_exotic_regional_groups_every_symbol_reports_exotic_tier)


# ═══════════════════════════════════════════════════════════════════════
section("2. Blackbox -- forex_dashboard.py --once shows the 4 EXOTIC regional sections")
# ═══════════════════════════════════════════════════════════════════════

def _run_dashboard():
    return subprocess.run(
        [sys.executable, "forex_dashboard.py", "--once"],
        cwd=BASE_DIR, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )


def test_dashboard_runs_cleanly_with_exotic_regions():
    proc = _run_dashboard()
    assert proc.returncode == 0, f"expected a clean exit(0), got {proc.returncode}: {proc.stderr}"
_run("forex_dashboard.py --once still runs cleanly with the 4 EXOTIC regional sections added",
     test_dashboard_runs_cleanly_with_exotic_regions)


def test_dashboard_shows_all_four_exotic_regional_positions_sections():
    proc = _run_dashboard()
    out = proc.stdout
    for label in ("OPEN POSITIONS — EXOTIC ASIA", "OPEN POSITIONS — EXOTIC EUROPE",
                  "OPEN POSITIONS — EXOTIC HIGH-YIELD/CARRY", "OPEN POSITIONS — EXOTIC LATAM/MIDEAST"):
        assert label in out, f"expected a dedicated '{label}' section"
_run("forex_dashboard.py --once shows all 4 EXOTIC regional OPEN POSITIONS sections",
     test_dashboard_shows_all_four_exotic_regional_positions_sections)


def test_dashboard_shows_all_four_exotic_regional_breakdown_sections():
    proc = _run_dashboard()
    out = proc.stdout
    for label in ("STRATEGY BREAKDOWN — EXOTIC ASIA", "STRATEGY BREAKDOWN — EXOTIC EUROPE",
                  "STRATEGY BREAKDOWN — EXOTIC HIGH-YIELD/CARRY", "STRATEGY BREAKDOWN — EXOTIC LATAM/MIDEAST"):
        assert label in out, f"expected a dedicated '{label}' section"
_run("forex_dashboard.py --once shows all 4 EXOTIC regional STRATEGY BREAKDOWN sections",
     test_dashboard_shows_all_four_exotic_regional_breakdown_sections)


def test_dashboard_still_shows_the_blended_exotic_section_too():
    # Unlike CORE (replaced entirely by HIGH VOLUME + CORE STANDARD), the
    # blended 83-pair EXOTIC section is deliberately KEPT alongside the 4
    # new regional ones -- the user asked to divide it for visibility, not
    # to remove the aggregate view.
    proc = _run_dashboard()
    out = proc.stdout
    assert "OPEN POSITIONS — EXOTIC (83 pairs" in out
    assert "STRATEGY BREAKDOWN — EXOTIC (83 pairs" in out
_run("forex_dashboard.py --once still shows the blended 83-pair EXOTIC section alongside the 4 regions",
     test_dashboard_still_shows_the_blended_exotic_section_too)


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
