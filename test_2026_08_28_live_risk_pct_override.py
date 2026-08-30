"""
Regression tests -- 2026-08-28 LIVE risk-pct override mechanism.

User's explicit request (readiness-report discussion, point 10): LIVE
should start at a smaller risk % than SIM for the initial pilot (SIM
0.25%, LIVE something like 0.10-0.15%), scaling up only once live
execution confirms costs/spreads/fills match SIM's assumptions.

This adds the MECHANISM: size_position() in bb/rsi/pullback all accept
an optional risk_pct override (defaults to the module's own RISK_PCT,
so SIM is completely unaffected), and forex/runner.py's _live_risk_pct()
applies it only for live/live_eur. The actual override VALUE
(LIVE_RISK_PCT_OVERRIDE) is deliberately left as None -- a real number
is a business decision pending the user's explicit choice, not
something to guess here.
"""

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


def test_size_position_defaults_to_module_risk_pct_unchanged():
    import forex.strategy_rsi as rsi
    import forex.strategy_bb as bb
    import forex.strategy_pullback as pullback
    for mod in (rsi, bb, pullback):
        default_qty = mod.size_position(10000.0, 0.01)
        explicit_qty = mod.size_position(10000.0, 0.01, risk_pct=mod.RISK_PCT)
        assert default_qty == explicit_qty, (
            f"{mod.__name__}: omitting risk_pct must behave identically to "
            f"passing the module's own RISK_PCT explicitly -- SIM must see "
            f"zero behavior change")
_run("forex.strategy_{rsi,bb,pullback}: size_position() with no risk_pct "
     "arg is identical to passing the module's own RISK_PCT (SIM unaffected)",
     test_size_position_defaults_to_module_risk_pct_unchanged)


def test_size_position_override_actually_changes_size():
    import forex.strategy_rsi as rsi
    normal_qty = rsi.size_position(10_000_000.0, 0.01, risk_pct=0.0025)
    reduced_qty = rsi.size_position(10_000_000.0, 0.01, risk_pct=0.001)
    assert reduced_qty < normal_qty, (
        "a smaller risk_pct override must produce a smaller position size "
        "(large equity used so the min_units floor doesn't mask the effect)")
_run("forex.strategy_rsi: a smaller risk_pct override genuinely reduces "
     "position size, not a no-op", test_size_position_override_actually_changes_size)


def test_live_risk_pct_override_is_an_explicit_user_decision():
    """Originally this asserted the value stayed None (undecided). On
    2026-08-28 the user made the call (0.25% -> 0.75%) as part of the
    LIVE capital/risk decision -- see docs / memory. It must be either
    None (undecided) or a small, sane, explicitly-set fraction; never a
    careless large number."""
    import forex.runner as r
    v = r.LIVE_RISK_PCT_OVERRIDE
    assert v is None or (isinstance(v, float) and 0.0 < v <= 0.02), (
        f"LIVE_RISK_PCT_OVERRIDE = {v!r} -- expected None or a deliberate "
        f"fraction in (0, 0.02]; a value outside that range looks like a bug, "
        f"not a decision")
_run("forex/runner: LIVE_RISK_PCT_OVERRIDE is None or a sane explicit "
     "fraction (user set 0.75% on 2026-08-28)",
     test_live_risk_pct_override_is_an_explicit_user_decision)


def test_live_risk_pct_only_applies_to_live_accounts():
    import forex.runner as r
    orig = r.LIVE_RISK_PCT_OVERRIDE
    try:
        r.LIVE_RISK_PCT_OVERRIDE = 0.0012
        r.set_account_env("sim")
        assert r._live_risk_pct() is None, "SIM must never see the LIVE override"
        r.set_account_env("live")
        assert r._live_risk_pct() == 0.0012
        r.set_account_env("live_eur")
        assert r._live_risk_pct() == 0.0012
    finally:
        r.LIVE_RISK_PCT_OVERRIDE = orig
        r.set_account_env("sim")
_run("forex/runner: _live_risk_pct() only applies to live/live_eur, never SIM",
     test_live_risk_pct_only_applies_to_live_accounts)


def test_entry_loop_wires_live_risk_pct_into_sizing():
    import inspect
    import forex.runner as r
    src = inspect.getsource(r._run_entries)
    assert '_live_risk_pct()' in src
    assert 'rp_kw["risk_pct"] = _live_risk_pct()' in src
_run("forex/runner: _run_entries() actually passes the live risk_pct "
     "override into size_position() via rp_kw", test_entry_loop_wires_live_risk_pct_into_sizing)


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
