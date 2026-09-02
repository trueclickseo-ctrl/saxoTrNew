"""
2026-09-02 -- disable the gap `london` + `tokyo` session legs; regime-filter
the surviving `newyork` leg.

A ~2.8y / H1-bar decomposition (docs/strategy_decomposition_2026-09-02.md):
  newyork  +0.090 R/trade, PF 1.33, stable both halves        -> KEEP
  london   -0.008 R/trade, PF 0.98, 2nd half negative         -> disable
  tokyo    untestable, ~0 ledger volume                       -> disable
  newyork + HIGH_VOLATILITY regime  -0.357 R, 43% WR          -> skip

Locks: the disable is a runner-level gate (strategy_gap.py entry logic
untouched), scoped to `gap` (not gap_weekend), exits-only for open
london/tokyo positions, and reversible.
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
import forex.strategy_gap as gap


def test_disabled_set_is_london_and_tokyo():
    assert runner.DISABLED_GAP_SESSIONS == {"london", "tokyo"}
    # newyork + weekly stay ON
    assert "newyork" not in runner.DISABLED_GAP_SESSIONS
    assert "weekly" not in runner.DISABLED_GAP_SESSIONS


def test_strategy_gap_entry_logic_untouched():
    # the disable is runner-side -- the pure module still DEFINES all sessions
    assert set(gap.SESSION_GAPS) == {"london", "newyork", "tokyo"}
    try:
        import subprocess
        head = subprocess.run(["git", "show", "HEAD:forex/strategy_gap.py"],
                              capture_output=True, text=True, cwd=BASE, timeout=15).stdout
        disk = open(os.path.join(BASE, "forex", "strategy_gap.py"), encoding="utf-8").read()
        assert head and head == disk, "forex/strategy_gap.py was modified -- the disable must be runner-side"
    except FileNotFoundError:
        pass


def test_run_entries_gates_gap_but_not_gap_weekend():
    src = inspect.getsource(runner._run_entries)
    # the guard is scoped to strat_name == "gap" exactly -> gap_weekend (which
    # only ever runs the `weekly` window anyway) can never hit it
    assert 'strat_name == "gap" and gap_session in DISABLED_GAP_SESSIONS' in src
    assert 'strat_name == "gap_weekend" and gap_session in DISABLED_GAP_SESSIONS' not in src


def test_disabled_leg_returns_zero_without_touching_orders():
    # simulate: the guard is a plain `return 0` before any fetch / order path
    src = inspect.getsource(runner._run_entries)
    i = src.index('gap_session in DISABLED_GAP_SESSIONS')
    block = src[i:i + 400]
    assert "return 0" in block
    for bad in ("_place", "generate_session_signals", "_fetch_history_h1", "insert_trade"):
        assert bad not in block


def test_newyork_regime_skip_is_high_volatility():
    assert runner.GAP_NEWYORK_SKIP_REGIMES == {"HIGH_VOLATILITY"}
    src = inspect.getsource(runner._run_entries)
    assert 'gap_session == "newyork"' in src
    assert "GAP_NEWYORK_SKIP_REGIMES" in src
    # the filter must be non-fatal (a classifier failure can't break the scan)
    j = src.index("GAP_NEWYORK_SKIP_REGIMES and regime_data")
    assert "try:" in src[j:j + 200] and "non-fatal" in src[j:j + 900]


def test_open_disabled_leg_positions_still_exit_managed():
    # _legacy_exit_strategies keys on strategy name, not gap session -- so any
    # open gap:X position (london/tokyo/newyork/weekly alike) is exit-managed
    # as long as "gap" is in STRATEGIES (it is) whenever "gap" isn't active
    assert "gap" in runner.STRATEGIES
    # and run_exits_only iterates all of STRATEGIES, so exits run every cycle
    assert "active_strategies = list(STRATEGIES)" in inspect.getsource(runner.run_exits_only)


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
