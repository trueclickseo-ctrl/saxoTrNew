"""
Regression test -- 2026-09-01 legacy-position exit management.

The SEK LIVE account moved to rsi-only for ENTRIES on 2026-08-31
(`LIVE_ALLOWED_STRATEGIES = {"rsi"}`), but it still holds 4 open
`donchian:` positions from before. `_run_exits` is only called for
strategies in the entry allowlist, so those 4 positions had NO strategy
exit management at all -- no Donchian channel-break exit, no ATR trail,
no time stop -- only their frozen entry-day broker OCO bracket. The
config doc's "close manually" note was the stopgap.

Fix: `_legacy_exit_strategies()` finds strategies with an open position
that aren't in the entry allowlist; `run_daily` and `run_exits_only` run
`_run_exits` for each (exits only -- entries stay blocked).
"""

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


def test_legacy_helper_picks_held_non_allowlist_strategies():
    pos = {"rsi:EURUSD": {}, "rsi:GBPPLN": {},
           "donchian:EURNOK": {}, "donchian:GBPUSD": {}, "donchian:AUDCHF": {}}
    assert fr._legacy_exit_strategies(["rsi"], pos) == ["donchian"]
    # nothing held outside the allowlist -> empty
    assert fr._legacy_exit_strategies(["rsi", "donchian"], pos) == []
    # SIM (all strategies active) -> never any legacy work
    assert fr._legacy_exit_strategies(list(fr.STRATEGIES), pos) == []


def test_legacy_helper_ignores_unknown_and_bare_keys():
    pos = {"EURUSD": {}, "notastrat:GBPUSD": {}, "donchian:AUDUSD": {}}
    assert fr._legacy_exit_strategies(["rsi"], pos) == ["donchian"]


def test_run_exits_only_runs_legacy_exits():
    src = inspect.getsource(fr.run_exits_only)
    assert "_legacy_exit_strategies(active_strategies, positions)" in src
    i = src.index("_legacy_exit_strategies(active_strategies, positions)")
    tail = src[i:i + 700]
    assert "_run_exits(strat_name, STRATEGIES[strat_name]" in tail
    assert "_save_state(state)" in tail
    # runs AFTER the main allowlist loop, BEFORE the summary
    assert src.index("for strat_name in active_strategies:") < i < src.index("EXITS-ONLY complete")


def test_run_daily_runs_legacy_exits_but_not_entries():
    src = inspect.getsource(fr.run_daily)
    assert "_legacy_exit_strategies(active_strategies, positions)" in src
    i = src.index("_legacy_exit_strategies(active_strategies, positions)")
    tail = src[i:i + 700]
    assert "_run_exits(strat_name, STRATEGIES[strat_name]" in tail
    # crucially: no _run_entries in the legacy block
    assert "_run_entries(" not in tail
    assert src.index("_legacy_exit_strategies") < src.index('TOTAL — Exits:')


def test_donchian_has_the_exit_hooks_the_pass_needs():
    import forex.strategy_donchian as d
    assert hasattr(d, "should_exit") and hasattr(d, "trailing_stop_update")
    # should_exit signature matches _run_exits's call: (pos, df, cal_days)
    params = list(inspect.signature(d.should_exit).parameters)
    assert len(params) == 3


def test_profit_ladder_still_rsi_only_does_not_touch_donchian():
    # 2026-09-02: "rsi_trend" (the SIM regime-gated A/B twin of rsi) joined
    # the set so the A/B isolates the entry gate -- both arms share the
    # ladder. Still RSI-family only; no trend strategy is in it.
    assert fr.PROFIT_LADDER_STRATEGIES == {"rsi", "rsi_trend"}
    assert fr._profit_ladder_active("donchian") is False
    assert fr._profit_ladder_active("advanced_rsi_master") is False  # the no-ladder control


def test_module_parses():
    import ast
    ast.parse(inspect.getsource(fr))


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
