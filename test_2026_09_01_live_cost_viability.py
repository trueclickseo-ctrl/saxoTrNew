"""
Regression test -- 2026-09-01 stricter LIVE trade-viability gate (A + B).

MXNUSD closed a real -EUR3.05 LIVE loss whose only fault was size:
+EUR2.13 gross price move, -EUR5.19 flat round-trip commission. On real
money the cost gate is now:
  * edge-to-cost ratio 5x, not 3x  (_min_edge_ratio, LIVE only);
  * a position below MIN_LIVE_NOTIONAL_EUR (5,000) notional is skipped
    outright -- at EUR5k the flat ~EUR5 commission is <= ~0.1%;
  * an UNKNOWN round-trip cost (infoprices lookup failed) blocks on LIVE
    instead of passing (SIM still passes -- forward-test continuity).
No strategy change -- these only ever REMOVE marginal trades.
"""

import ast
import inspect
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

G, R, Y, X, B = "\033[92m", "\033[91m", "\033[93m", "\033[0m", "\033[1m"
_res = []


def _run(n, f):
    try:
        f()
        _res.append((n, True, None))
    except Exception as e:
        import traceback
        _res.append((n, False, f"{type(e).__name__}: {e}\n{traceback.format_exc()}"))


import forex.runner as fr


def test_min_edge_ratio_is_stricter_on_live():
    fr.set_account_env("live");     assert fr._min_edge_ratio() == 5.0
    fr.set_account_env("live_eur"); assert fr._min_edge_ratio() == 5.0
    fr.set_account_env("sim");      assert fr._min_edge_ratio() == 3.0
    assert fr.MIN_LIVE_EDGE_TO_COST_RATIO == 5.0
    assert fr.MIN_EDGE_TO_COST_RATIO == 3.0     # SIM untouched


def test_min_live_notional_constant():
    assert fr.MIN_LIVE_NOTIONAL_EUR == 5000.0


def test_gate_block_logic_in_source():
    src = inspect.getsource(fr._run_entries)
    # three block branches, LIVE-scoped for the new two
    assert 'block_reason = True, "cost_not_cleared"' in src
    assert '_is_live and round_trip_cost is None' in src and '"cost_unknown_live"' in src
    assert '_is_live and notional_eur is not None and notional_eur < MIN_LIVE_NOTIONAL_EUR' in src
    assert '"below_min_notional"' in src
    # ratio comes from the helper, not the bare constant
    assert "round_trip_cost * _edge_ratio" in src
    # notional_eur computed from the BASE currency rate (qty is base units)
    assert '_eur_per_unit(sym[:3], akey)' in src
    # SIM path unchanged: cost_unknown still passes for SIM
    i = src.index("_is_live and round_trip_cost is None")
    assert "elif" in src[i-8:i]          # only an elif, doesn't fire for sim


def test_sim_still_passes_unknown_cost():
    # structural: the cost_unknown_live branch is guarded by _is_live, so a
    # sim run with round_trip_cost None falls through to blocked=False
    src = inspect.getsource(fr._run_entries)
    # blocked stays False unless one of the three conditions hits; for sim
    # only the first (needs round_trip_cost is not None) can, so None -> pass
    assert "blocked = False" in src


def test_cost_gate_log_carries_notional_and_ratio():
    import forex.forward_observation as fo
    sig = inspect.signature(fo.log_cost_gate_decision)
    assert "notional_eur" in sig.parameters
    src = inspect.getsource(fr._run_entries)
    assert "min_edge_to_cost_ratio=_edge_ratio" in src
    assert "notional_eur=notional_eur" in src


def test_modules_parse():
    ast.parse(inspect.getsource(fr))
    import forex.forward_observation as fo
    ast.parse(inspect.getsource(fo))


for _n, _f in list(globals().items()):
    if _n.startswith("test_") and callable(_f):
        _run(_n, _f)
fr.set_account_env("sim")

print(f"\n{B}{'=' * 66}{X}")
bad = [(n, e) for n, ok, e in _res if not ok]
for n, ok, e in _res:
    print(f"  [{G}PASS{X}]" if ok else f"  [{R}FAIL{X}]", n)
    if e:
        print(f"      {Y}{e}{X}")
print(f"{B}{'=' * 66}{X}")
if bad:
    print(f"{R}{B}  {len(bad)} / {len(_res)} FAILED{X}")
    sys.exit(1)
print(f"{G}{B}  ALL {len(_res)} TESTS PASSED{X}")
sys.exit(0)
