"""
Regression tests -- 2026-08-28 stocks universe expansion (HIGH_GROWTH_TICKERS).

Explicit user request, after liking a real US Reversion trade (CRWD):
"expand stock universe but only with high growth volume share, good
companies so we catch more these signals."

Added atos/universe.py's HIGH_GROWTH_TICKERS (22 net-new tickers across
fintech, AI/compute infra, space/quantum, crypto-treasury, power/AI-
datacenter demand, consumer growth, and biotech), folded into US_TICKERS/
ATOS_UNIVERSE alongside the existing ~385-ticker SP500_TICKERS list.
Every new ticker was verified against Saxo's own live instrument search
via lookup_missing.py, not just added blindly.

Found and fixed one real, dangerous mismatch during that verification:
"SQ" (Square's old ticker) no longer resolves to Block, Inc. -- Block
renamed its ticker to "XYZ" in January 2025 -- so looking up "SQ" against
Saxo's search returned a completely unrelated instrument (BMY / Bristol-
Myers Squibb). Corrected to "XYZ" before it was ever left in
instrument_map.csv as a live (if flagged) mismatch.
"""

import csv
import os
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
section("1. atos.universe -- HIGH_GROWTH_TICKERS additions")
# ═══════════════════════════════════════════════════════════════════════

def test_high_growth_tickers_has_no_internal_duplicates():
    from atos.universe import HIGH_GROWTH_TICKERS
    assert len(HIGH_GROWTH_TICKERS) == len(set(HIGH_GROWTH_TICKERS)), (
        "HIGH_GROWTH_TICKERS must not contain duplicate tickers"
    )
_run("atos.universe.HIGH_GROWTH_TICKERS has no internal duplicates",
     test_high_growth_tickers_has_no_internal_duplicates)


def test_sq_never_reintroduced():
    from atos.universe import HIGH_GROWTH_TICKERS, SP500_TICKERS
    assert "SQ" not in HIGH_GROWTH_TICKERS and "SQ" not in SP500_TICKERS, (
        "SQ must never be in the universe -- Block Inc. renamed its ticker to "
        "XYZ in January 2025, and 'SQ' resolves to an unrelated instrument "
        "(Bristol-Myers Squibb) against Saxo's own search. Block is already "
        "correctly present as XYZ in SP500_TICKERS (was almost duplicated, "
        "under the wrong old ticker, while building HIGH_GROWTH_TICKERS)"
    )
    assert "XYZ" in SP500_TICKERS, "XYZ (Block's real current ticker) must be present"
_run("atos.universe: SQ never present anywhere, XYZ (Block's real ticker) already covered",
     test_sq_never_reintroduced)


def test_us_tickers_includes_both_lists_deduplicated():
    from atos.universe import (SP500_TICKERS, HIGH_GROWTH_TICKERS,
                               NASDAQ100_DOW_TICKERS, US_TICKERS)
    assert set(SP500_TICKERS) <= set(US_TICKERS)
    assert set(HIGH_GROWTH_TICKERS) <= set(US_TICKERS)
    assert set(NASDAQ100_DOW_TICKERS) <= set(US_TICKERS)
    # 2026-09-03: a 3rd source list (Nasdaq-100 + Dow-30 gap-fill) folded in.
    assert len(US_TICKERS) == len(set(SP500_TICKERS) | set(HIGH_GROWTH_TICKERS)
                                  | set(NASDAQ100_DOW_TICKERS)), (
        "US_TICKERS must be the deduplicated union of all 3 lists, no double-counting overlaps"
    )
_run("atos.universe.US_TICKERS is the deduplicated union of SP500 + HIGH_GROWTH + NASDAQ100_DOW",
     test_us_tickers_includes_both_lists_deduplicated)


def test_atos_universe_grew_by_the_expected_net_new_count():
    from atos.universe import SP500_TICKERS, HIGH_GROWTH_TICKERS, NASDAQ100_DOW_TICKERS, US_TICKERS
    # HIGH_GROWTH: 22 net-new over SP500 (3 overlaps CROX/PINS/NRG).
    # NASDAQ100_DOW (2026-09-03): 17 net-new, none overlapping either list.
    assert len(NASDAQ100_DOW_TICKERS) == 17
    assert not (set(NASDAQ100_DOW_TICKERS) & (set(SP500_TICKERS) | set(HIGH_GROWTH_TICKERS)))
    assert len(US_TICKERS) - len(SP500_TICKERS) == 22 + 17, (
        f"expected 22 (high-growth) + 17 (nasdaq100/dow) net-new tickers, "
        f"got {len(US_TICKERS) - len(SP500_TICKERS)}"
    )
_run("atos.universe: US_TICKERS grew by 22 + 17 net-new tickers over SP500_TICKERS",
     test_atos_universe_grew_by_the_expected_net_new_count)


# ═══════════════════════════════════════════════════════════════════════
section("2. data/instrument_map.csv -- every new ticker resolved to a real, correct UIC")
# ═══════════════════════════════════════════════════════════════════════

def _load_map():
    path = os.path.join(BASE_DIR, "data", "instrument_map.csv")
    with open(path, encoding="utf-8") as f:
        return {row["yahoo_ticker"]: row for row in csv.DictReader(f)}


def test_every_new_high_growth_ticker_resolved_with_no_review_flag():
    from atos.universe import HIGH_GROWTH_TICKERS
    m = _load_map()
    unresolved = []
    for t in HIGH_GROWTH_TICKERS:
        row = m.get(t)
        if row is None or not row.get("uic") or row.get("needs_review"):
            unresolved.append((t, row))
    assert not unresolved, f"tickers with no clean UIC resolution: {unresolved}"
_run("instrument_map.csv: every HIGH_GROWTH_TICKERS entry has a real UIC with no needs_review flag",
     test_every_new_high_growth_ticker_resolved_with_no_review_flag)


def test_instrument_map_has_no_duplicate_ticker_rows():
    path = os.path.join(BASE_DIR, "data", "instrument_map.csv")
    with open(path, encoding="utf-8") as f:
        tickers = [row["yahoo_ticker"] for row in csv.DictReader(f)]
    dupes = {t for t in tickers if tickers.count(t) > 1}
    assert not dupes, (
        f"instrument_map.csv has duplicate rows for: {dupes} -- "
        f"lookup_missing.py re-appends unresolved tickers on every rerun "
        f"without deduping first (pre-existing gap, not fixed here beyond "
        f"cleaning up the rows this session's runs added)"
    )
_run("instrument_map.csv has no duplicate ticker rows (cleaned up 2026-08-28)",
     test_instrument_map_has_no_duplicate_ticker_rows)


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
