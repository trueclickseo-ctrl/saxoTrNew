"""
2026-09-02 -- the user cut the SIM forex book to 5 strategies:
  rsi (day-1 baseline) + rsi_trend + ema_trend + bb_quality + zscore_quality
and force-flattened everything else (close_all_forex_sim.py).

Locks: the roster is an explicit allowlist, it drives run_daily / the CLI
default, everything else still exit-manages, and LIVE is untouched.
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


import forex.runner as runner

_ROSTER = ["rsi", "rsi_trend", "ema_trend", "bb_quality", "zscore_quality"]


def test_roster_is_the_five():
    assert runner.SIM_ACTIVE_STRATEGIES == _ROSTER
    assert runner._ACTIVE_STRATEGIES == _ROSTER
    for s in _ROSTER:
        assert s in runner.STRATEGIES


def test_everything_else_is_dormant_for_new_entries():
    dormant = set(runner.STRATEGIES) - set(_ROSTER)
    assert len(dormant) >= 15
    # the originals whose twins we kept are dormant (twin runs, original doesn't)
    for s in ("ema", "bb", "zscore"):
        assert s in dormant


def test_run_daily_and_cli_default_to_the_roster():
    src = inspect.getsource(runner)
    assert "active_strategies = list(_ACTIVE_STRATEGIES)" in src
    assert "requested_strategies is not None else list(_ACTIVE_STRATEGIES)" in src
    assert "_ACTIVE_STRATEGIES = [k for k in SIM_ACTIVE_STRATEGIES if k in STRATEGIES]" in src


def test_dormant_positions_still_exit_managed():
    held = {"ml:EURUSD": {}, "bb:GBPUSD": {}, "zscore:AUDUSD": {}, "rsi:NZDUSD": {}}
    got = runner._legacy_exit_strategies(runner._ACTIVE_STRATEGIES, held)
    assert "ml" in got and "bb" in got and "zscore" in got   # dormant + held -> exit-managed
    assert "rsi" not in got                                   # in the roster -> main loop
    # run_exits_only iterates ALL of STRATEGIES regardless
    assert "active_strategies = list(STRATEGIES)" in inspect.getsource(runner.run_exits_only)


def test_live_is_unaffected():
    assert runner.LIVE_ALLOWED_STRATEGIES == {"rsi"}
    assert runner.LIVE_EUR_ALLOWED_STRATEGIES == set()
    # the LIVE/LIVE_EUR CLI branches resolve from those, not _ACTIVE_STRATEGIES
    src = inspect.getsource(runner)
    assert "requested_strategies or sorted(LIVE_ALLOWED_STRATEGIES)" in src


def test_offroster_explicit_request_warns():
    src = inspect.getsource(runner)
    assert "_explicit_offroster" in src
    assert "not in the current SIM roster" in src


def test_runner_parses():
    ast.parse(inspect.getsource(runner))


for _n, _f in list(globals().items()):
    if _n.startswith("test_") and callable(_f):
        _run(_n, _f)

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
