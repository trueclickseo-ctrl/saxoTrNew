"""
Regression tests — 2026-08-26 forex cost-clearance entry gate.

Root cause this closes: the live_eur account's first closed trade (EURPLN,
RSI Pullback) was directionally correct (+167 pips) but still lost money
net of cost, because a 1,000-unit position's expected profit could never
plausibly clear Saxo's real round-trip commission. Confirmed live via
Saxo's own /trade/v1/infoprices Commissions field across 5 pairs (EURUSD,
GBPUSD, EURPLN, USDTRY, EURHUF): the round-trip commission converts to
~5.15 EUR regardless of pair -- a FLAT per-trade cost, not an
exotic-pair-specific one. Nothing in forex/runner.py checked whether a
signal's own target could clear this before opening a position.

Fix: forex/runner.py's _round_trip_cost_quote_ccy() queries Saxo's live
commission quote for the exact position size about to be traded, and the
entry loop (_run_entries) skips any signal whose own target profit doesn't
clear MIN_EDGE_TO_COST_RATIO (3.0) times that cost -- the same skip-reason
pattern already used for spread/margin/exposure checks in that loop.
"""

import os
import sys
from unittest.mock import patch

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
section("1. _round_trip_cost_quote_ccy() — live commission quote, not a guess")
# ═══════════════════════════════════════════════════════════════════════

def test_round_trip_cost_doubles_cost_buy():
    import forex.runner as r
    r._COMMISSION_CACHE.clear()
    fake_resp = {"Commissions": {"CostBuy": 11.1, "CostSell": 11.1}}
    with patch.object(r, "_get", return_value=fake_resp):
        cost = r._round_trip_cost_quote_ccy(1343, 1000.0, "some-akey")
    assert cost == 22.2, f"expected 2x CostBuy (22.2), got {cost}"
    r._COMMISSION_CACHE.clear()
_run("forex/runner: _round_trip_cost_quote_ccy() returns 2x Saxo's live "
     "CostBuy quote for this exact position size",
     test_round_trip_cost_doubles_cost_buy)


def test_round_trip_cost_returns_none_on_missing_field():
    import forex.runner as r
    r._COMMISSION_CACHE.clear()
    with patch.object(r, "_get", return_value={"Commissions": {}}):
        cost = r._round_trip_cost_quote_ccy(1343, 1000.0, "some-akey")
    assert cost is None
    r._COMMISSION_CACHE.clear()
_run("forex/runner: _round_trip_cost_quote_ccy() returns None (never "
     "guesses) when Saxo's response has no CostBuy field",
     test_round_trip_cost_returns_none_on_missing_field)


def test_round_trip_cost_returns_none_on_lookup_failure():
    import forex.runner as r
    r._COMMISSION_CACHE.clear()
    with patch.object(r, "_get", side_effect=Exception("network")):
        cost = r._round_trip_cost_quote_ccy(1343, 1000.0, "some-akey")
    assert cost is None
    r._COMMISSION_CACHE.clear()
_run("forex/runner: _round_trip_cost_quote_ccy() returns None on a "
     "network/API failure, doesn't raise",
     test_round_trip_cost_returns_none_on_lookup_failure)


def test_round_trip_cost_is_cached_per_uic_and_qty():
    import forex.runner as r
    r._COMMISSION_CACHE.clear()
    call_count = {"n": 0}
    def fake_get(*a, **kw):
        call_count["n"] += 1
        return {"Commissions": {"CostBuy": 3.0}}
    with patch.object(r, "_get", side_effect=fake_get):
        c1 = r._round_trip_cost_quote_ccy(21, 1000.0, "akey")
        c2 = r._round_trip_cost_quote_ccy(21, 1000.0, "akey")
        c3 = r._round_trip_cost_quote_ccy(31, 1000.0, "akey")  # different uic
    assert c1 == c2 == 6.0
    assert call_count["n"] == 2, (
        f"expected exactly 2 live lookups (uic 21 once, uic 31 once), got "
        f"{call_count['n']} -- commission doesn't move intra-run, repeated "
        f"lookups for the same (uic, qty) are pure wasted latency, not "
        f"freshness, across a scan of many candidate signals")
    r._COMMISSION_CACHE.clear()
_run("forex/runner: _round_trip_cost_quote_ccy() caches per (uic, qty) for "
     "the run, doesn't re-query Saxo for every candidate on the same pair",
     test_round_trip_cost_is_cached_per_uic_and_qty)


# ═══════════════════════════════════════════════════════════════════════
section("2. _run_entries() — wired as a skip-reason, fails open on unknown cost")
# ═══════════════════════════════════════════════════════════════════════

def test_cost_gate_wired_into_entry_loop():
    import inspect
    import forex.runner as r
    src = inspect.getsource(r._run_entries)
    assert "_round_trip_cost_quote_ccy(uic, qty, akey)" in src, (
        "the cost-clearance check must run against the ACTUAL uic/qty being "
        "traded, not a generic/default size")
    assert "MIN_EDGE_TO_COST_RATIO" in src
_run("forex/runner: _run_entries() calls the cost-clearance gate with the "
     "real uic/qty for every candidate signal",
     test_cost_gate_wired_into_entry_loop)


def test_cost_gate_skip_condition_requires_both_values_present():
    import inspect
    import forex.runner as r
    src = inspect.getsource(r._run_entries)
    # Must be gated on "round_trip_cost is not None" -- a None (lookup
    # failed) must never block the entry, matching the spread check's
    # existing fail-open pattern just above it in the same loop.
    assert "if round_trip_cost is not None and expected_target_profit < round_trip_cost * MIN_EDGE_TO_COST_RATIO:" in src, (
        "an unknown cost (lookup failure) must fail OPEN (don't block the "
        "entry), not closed -- same discipline as the existing spread check")
_run("forex/runner: the cost gate fails OPEN (does not block entries) when "
     "the live commission lookup itself fails",
     test_cost_gate_skip_condition_requires_both_values_present)


def test_min_edge_to_cost_ratio_is_a_named_constant_not_a_bare_number():
    import forex.runner as r
    assert hasattr(r, "MIN_EDGE_TO_COST_RATIO")
    assert isinstance(r.MIN_EDGE_TO_COST_RATIO, float)
    assert r.MIN_EDGE_TO_COST_RATIO == 3.0
_run("forex/runner: MIN_EDGE_TO_COST_RATIO is a named, documented module "
     "constant (currently 3.0), not a bare literal buried in the loop",
     test_min_edge_to_cost_ratio_is_a_named_constant_not_a_bare_number)


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
