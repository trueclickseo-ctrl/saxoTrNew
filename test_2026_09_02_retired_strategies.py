"""
2026-09-02 -- retire donchian / donchian_quality / pullback / ml / supertrend.

A 12y / 49-CORE-pair edge decomposition
(docs/strategy_decomposition_2026-09-02.md) showed each is net-negative with
no filter that survives a both-halves + bootstrap-CI test.

Retirement = still in STRATEGIES (open positions keep full exit management,
dashboard / ledger keep their history) but excluded from the default entry
rotation. An explicit `--strategy <name>` still runs one, with a warning.

These tests lock that contract.
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

_EXPECTED = {"donchian", "donchian_quality", "pullback", "ml", "supertrend"}


def test_the_five_are_the_retired_set():
    assert runner.RETIRED_STRATEGIES == _EXPECTED


def test_retired_stay_registered_for_exit_management():
    # MUST stay in STRATEGIES -- _legacy_exit_strategies only manages held
    # positions whose strategy is in STRATEGIES. Dropping them would strand
    # any open donchian:/ml:/... position with no trailing / time-stop / exit.
    for s in _EXPECTED:
        assert s in runner.STRATEGIES, f"{s} dropped from STRATEGIES -- open positions would go unmanaged"
        assert s in runner.SLOTS_PER_STRATEGY, f"{s} lost its SLOTS entry"


def test_retired_excluded_from_default_entry_rotation():
    # 2026-09-02: the SIM roster is now an explicit 5-strategy allowlist
    # (SIM_ACTIVE_STRATEGIES), which of course excludes every retired one.
    assert not (set(runner._ACTIVE_STRATEGIES) & runner.RETIRED_STRATEGIES)
    assert not (set(runner.SIM_ACTIVE_STRATEGIES) & runner.RETIRED_STRATEGIES)
    # run_daily's default and the CLI default both use _ACTIVE_STRATEGIES
    src = inspect.getsource(runner)
    assert src.count("active_strategies = list(_ACTIVE_STRATEGIES)") >= 1
    assert "requested_strategies is not None else list(_ACTIVE_STRATEGIES)" in src


def test_run_exits_only_still_covers_everything():
    # the exits-only path must NOT filter retired -- every open position gets
    # its stops checked every cycle regardless of retirement
    src = inspect.getsource(runner.run_exits_only)
    assert "active_strategies = list(STRATEGIES)" in src
    assert "_ACTIVE_STRATEGIES" not in src


def test_legacy_exit_path_picks_up_a_retired_position():
    held = {"ml:EURUSD": {}, "supertrend:GBPUSD": {}, "rsi:AUDUSD": {}}
    got = runner._legacy_exit_strategies(runner._ACTIVE_STRATEGIES, held)
    assert "ml" in got and "supertrend" in got      # retired + held -> exit-managed
    assert "rsi" not in got                          # active -> handled by the main loop


def test_explicit_request_still_runs_with_a_warning():
    src = inspect.getsource(runner)
    assert "_explicit_retired" in src
    assert "running RETIRED strateg" in src


def test_not_in_live_allowlists():
    for s in _EXPECTED:
        assert s not in runner.LIVE_ALLOWED_STRATEGIES
        assert s not in runner.LIVE_EUR_ALLOWED_STRATEGIES


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
