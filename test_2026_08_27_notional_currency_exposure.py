"""
Regression tests -- 2026-08-27 EUR-notional currency exposure (visibility).

User's finding reviewing the SIM workbook: _currency_exposure() counts
POSITIONS, not economic size -- a 1,000-unit position and a 48,000-unit
position both count as "1", which isn't meaningful across a 149-pair
universe with wildly varying sizes. This adds the real (EUR-notional)
measurement as VISIBILITY ONLY -- explicitly not wired into any gate,
per the 2026-08-27 decision to measure correctly first and decide on a
real threshold (preferably volatility-adjusted) as its own separate
decision later.
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


def test_notional_exposure_weights_by_real_size():
    import forex.runner as r
    with patch.object(r, "_eur_per_unit", side_effect=lambda ccy, akey: {"AUD": 0.6, "CHF": 1.0}.get(ccy)):
        positions = {
            "donchian:AUDCHF": {"direction": "Buy", "quantity": 1000},
            "pullback:AUDCHF": {"direction": "Buy", "quantity": 48000},
        }
        exposure = r._currency_exposure_notional_eur(positions)
    # AUD: (1000+48000)*0.6 = 29400, not "2" like the count-based version
    assert abs(exposure["AUD"] - 29400) < 1e-6, f"expected 29400, got {exposure['AUD']}"
_run("forex/runner: _currency_exposure_notional_eur() weights by real "
     "position size, not a flat +/-1 per position",
     test_notional_exposure_weights_by_real_size)


def test_notional_exposure_opposite_signs_for_opposite_directions():
    import forex.runner as r
    with patch.object(r, "_eur_per_unit", return_value=1.0):
        positions = {
            "donchian:AUDCHF": {"direction": "Buy", "quantity": 1000},
            "donchian:CHFAUD": {"direction": "Sell", "quantity": 1000},
        }
        exposure = r._currency_exposure_notional_eur(positions)
    # Buy AUDCHF (long AUD) + Sell CHFAUD (also long AUD) -> should stack, not cancel
    assert exposure["AUD"] == 2000.0, f"expected AUD +2000 (stacked), got {exposure['AUD']}"
    assert exposure["CHF"] == -2000.0
_run("forex/runner: the real AUDCHF+CHFAUD doubling still shows up "
     "correctly (stacked, not cancelled) in the notional measure",
     test_notional_exposure_opposite_signs_for_opposite_directions)


def test_notional_exposure_skips_unresolvable_rate():
    import forex.runner as r
    with patch.object(r, "_eur_per_unit", return_value=None):
        exposure = r._currency_exposure_notional_eur(
            {"donchian:AUDCHF": {"direction": "Buy", "quantity": 1000}})
    assert exposure == {}, "an unresolvable rate must be skipped, never treated as zero exposure"
_run("forex/runner: _currency_exposure_notional_eur() skips (never "
     "fakes) a position it can't get a live EUR rate for",
     test_notional_exposure_skips_unresolvable_rate)


def test_notional_exposure_not_wired_into_any_gate():
    import inspect
    import forex.runner as r
    src = inspect.getsource(r._run_entries)
    assert "_currency_exposure_notional_eur" not in src, (
        "this must stay visibility-only for now -- wiring it into the entry "
        "loop's gate is a separate, deliberate decision not yet made")
_run("forex/runner: notional exposure is NOT wired into the entry loop's "
     "gate yet (visibility only, by design)",
     test_notional_exposure_not_wired_into_any_gate)


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
