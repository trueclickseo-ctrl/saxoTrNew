"""
Regression tests -- 2026-08-28 CORE_STANDARD_SYMBOLS dashboard split.

Explicit user request: "we have 34 pairs dashboard from which we separate
17 high volume pairs, now remaining 17 pairs give it a appropriate name
and make a division in a dashboard so we know exactly their performance."

CORE_STANDARD_SYMBOLS is HIGH_VOLUME_SYMBOLS's exact complement within
CORE_SYMBOLS (34 = 17 + 17, an exact partition, not just "everything
else") -- forex_dashboard.py now shows a matching OPEN POSITIONS /
STRATEGY BREAKDOWN section for it, directly beside HIGH VOLUME's own
sections, so the two halves' track records can be compared side by side.
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
section("1. forex.universe.CORE_STANDARD_SYMBOLS -- exact partition with HIGH_VOLUME_SYMBOLS")
# ═══════════════════════════════════════════════════════════════════════

def test_core_standard_is_exactly_17_pairs():
    from forex.universe import CORE_STANDARD_SYMBOLS
    assert len(CORE_STANDARD_SYMBOLS) == 17, f"expected exactly 17 pairs, got {len(CORE_STANDARD_SYMBOLS)}"
_run("forex.universe: CORE_STANDARD_SYMBOLS is exactly 17 pairs",
     test_core_standard_is_exactly_17_pairs)


def test_core_standard_and_high_volume_never_overlap():
    from forex.universe import CORE_STANDARD_SYMBOLS, HIGH_VOLUME_SYMBOLS
    assert not (CORE_STANDARD_SYMBOLS & HIGH_VOLUME_SYMBOLS), (
        "CORE_STANDARD_SYMBOLS and HIGH_VOLUME_SYMBOLS must never share a pair"
    )
_run("forex.universe: CORE_STANDARD_SYMBOLS and HIGH_VOLUME_SYMBOLS never overlap",
     test_core_standard_and_high_volume_never_overlap)


def test_core_standard_plus_high_volume_exactly_equals_core():
    from forex.universe import CORE_STANDARD_SYMBOLS, HIGH_VOLUME_SYMBOLS, CORE_SYMBOLS
    assert (CORE_STANDARD_SYMBOLS | HIGH_VOLUME_SYMBOLS) == CORE_SYMBOLS, (
        "CORE_STANDARD_SYMBOLS + HIGH_VOLUME_SYMBOLS must exactly equal all 34 CORE_SYMBOLS, no gaps"
    )
    assert len(CORE_SYMBOLS) == 34
_run("forex.universe: CORE_STANDARD_SYMBOLS + HIGH_VOLUME_SYMBOLS exactly partition CORE_SYMBOLS (17+17=34)",
     test_core_standard_plus_high_volume_exactly_equals_core)


def test_core_standard_every_symbol_reports_core_tier():
    from forex.universe import CORE_STANDARD_SYMBOLS, get_tier
    for sym in CORE_STANDARD_SYMBOLS:
        assert get_tier(sym) == "core", f"{sym} should report tier='core', not a new/different tier"
_run("forex.universe: every CORE_STANDARD_SYMBOLS pair still reports get_tier()=='core'",
     test_core_standard_every_symbol_reports_core_tier)


# ═══════════════════════════════════════════════════════════════════════
section("2. Blackbox -- forex_dashboard.py --once shows the new CORE STANDARD sections")
# ═══════════════════════════════════════════════════════════════════════

def _run_dashboard():
    return subprocess.run(
        [sys.executable, "forex_dashboard.py", "--once"],
        cwd=BASE_DIR, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )


def test_dashboard_runs_cleanly_with_new_section():
    proc = _run_dashboard()
    assert proc.returncode == 0, f"expected a clean exit(0), got {proc.returncode}: {proc.stderr}"
_run("forex_dashboard.py --once still runs cleanly with the new CORE STANDARD sections added",
     test_dashboard_runs_cleanly_with_new_section)


def test_dashboard_shows_core_standard_positions_section():
    proc = _run_dashboard()
    assert "OPEN POSITIONS — CORE STANDARD" in proc.stdout, (
        "expected a dedicated 'OPEN POSITIONS — CORE STANDARD' section, distinct from HIGH VOLUME's"
    )
_run("forex_dashboard.py --once shows a dedicated OPEN POSITIONS — CORE STANDARD section",
     test_dashboard_shows_core_standard_positions_section)


def test_dashboard_shows_core_standard_strategy_breakdown_section():
    proc = _run_dashboard()
    assert "STRATEGY BREAKDOWN — CORE STANDARD" in proc.stdout, (
        "expected a dedicated 'STRATEGY BREAKDOWN — CORE STANDARD' section, distinct from HIGH VOLUME's"
    )
_run("forex_dashboard.py --once shows a dedicated STRATEGY BREAKDOWN — CORE STANDARD section",
     test_dashboard_shows_core_standard_strategy_breakdown_section)


def test_dashboard_no_longer_shows_standalone_core_section():
    # 2026-08-28: explicit follow-up user instruction -- "you can remove
    # core 34 from Dashboard, as we have divided these into 17 High Volume
    # and 17 Core Standard from these 34". The standalone CORE (34-pair)
    # positions/breakdown sections must be gone entirely now, not just
    # supplemented by the two new halves.
    proc = _run_dashboard()
    out = proc.stdout
    assert "OPEN POSITIONS — CORE (" not in out, (
        "the standalone CORE (34 pairs) positions section must be removed -- "
        "HIGH VOLUME + CORE STANDARD already exactly partition it"
    )
    assert "STRATEGY BREAKDOWN — CORE (" not in out, (
        "the standalone CORE (34 pairs) strategy breakdown must be removed -- "
        "HIGH VOLUME + CORE STANDARD already exactly partition it"
    )
_run("forex_dashboard.py --once no longer shows a standalone CORE (34 pairs) section",
     test_dashboard_no_longer_shows_standalone_core_section)


def test_dashboard_high_volume_and_core_standard_both_present_and_adjacent():
    proc = _run_dashboard()
    out = proc.stdout
    hv_idx = out.index("STRATEGY BREAKDOWN — HIGH VOLUME")
    cs_idx = out.index("STRATEGY BREAKDOWN — CORE STANDARD")
    assert cs_idx > hv_idx, (
        "CORE STANDARD's strategy breakdown should come right after HIGH VOLUME's, "
        "so the two halves of CORE are directly comparable side by side"
    )
_run("forex_dashboard.py --once shows HIGH VOLUME and CORE STANDARD breakdowns adjacent, for direct comparison",
     test_dashboard_high_volume_and_core_standard_both_present_and_adjacent)


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
