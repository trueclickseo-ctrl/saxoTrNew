"""
Regression test -- 2026-09-02 exit-guard for stale positions.

INCIDENT: the LIVE EUR exit-check had been dead for ~2 days (a stale
`--strategy rsi` arg the 2026-09-01 SEK consolidation invalidated). When
it was fixed and re-run, `should_exit()` fired `hard_stop` on `rsi:NZDCAD`
from ~2-day-old local state -- but NZDCAD had already been stopped out
broker-side. `_run_exits` sent a market `Sell 9,000` anyway. FX has no
reduce-only, so Saxo OPENED a 9,000 short instead of closing anything.

FIX: `_live_position_open(uic, qty, direction, n_tracked)` -> "open" /
"gone" / "unknown". `_run_exits` calls it before every real LIVE close;
a definite "gone" books the close from Saxo's record WITHOUT sending an
order. "unknown" (API/snapshot problem) falls through to the normal close
so a genuine exit is never suppressed.
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


def _fake_positions(rows):
    """rows: list of (uic, amount) -> a /port/v1/positions/me-shaped dict."""
    return {"Data": [{"PositionBase": {"Uic": u, "Amount": a, "AssetType": "FxSpot"}}
                     for u, a in rows]}


def test_position_open_detects_a_matching_long(monkeypatch=None):
    fr._get = lambda path, *a, **k: _fake_positions([(33, 9000.0), (31, 10000.0)])
    assert fr._live_position_open(33, 9000, "Buy", 3) == "open"


def test_position_gone_when_snapshot_healthy_but_no_match():
    # snapshot has other FxSpot rows (healthy fetch) but not our uic 33 long
    fr._get = lambda path, *a, **k: _fake_positions([(31, 10000.0), (21, 10000.0)])
    assert fr._live_position_open(33, 9000, "Buy", 3) == "gone"


def test_wrong_side_is_gone_not_open():
    # a 9,000 SHORT on uic 33 is NOT the 9,000 LONG we track
    fr._get = lambda path, *a, **k: _fake_positions([(33, -9000.0), (31, 10000.0)])
    assert fr._live_position_open(33, 9000, "Buy", 3) == "gone"


def test_empty_snapshot_with_many_tracked_is_unknown_not_gone():
    # 0 FxSpot rows back while we track several locally -> bad fetch, not flat
    fr._get = lambda path, *a, **k: {"Data": []}
    assert fr._live_position_open(33, 9000, "Buy", 4) == "unknown"


def test_lookup_failure_is_unknown():
    def _boom(*a, **k):
        raise RuntimeError("network")
    fr._get = _boom
    assert fr._live_position_open(33, 9000, "Buy", 3) == "unknown"


def test_guard_is_wired_before_the_close_order():
    src = inspect.getsource(fr._run_exits)
    assert "_live_position_open(" in src, "guard helper not called in _run_exits"
    g = src.index("_live_position_open(")
    post = src.index('_post("/trade/v2/orders"')
    assert g < post, "guard must run BEFORE the market close order"
    # the guard only arms on a real LIVE run, never dry-run / paper / SIM
    window = src[g - 400:g]
    assert "not dry_run" in window and "not _paper" in window
    assert 'ACCOUNT_ENV in ("live", "live_eur")' in window


def test_broker_closed_branch_sends_no_order():
    src = inspect.getsource(fr._run_exits)
    assert "elif _broker_closed:" in src
    i = src.index("elif _broker_closed:")
    branch = src[i:i + 900]
    # books via _confirm_exit_fill, never _post
    assert "_confirm_exit_fill(" in branch
    assert "_post(" not in branch
    # still cancels any orphan resting leg
    assert "_cancel_order(" in branch


def test_broker_closed_still_reaches_the_booking_path():
    # the `if not dry_run:` block (pnl_tracker.log_close / observation card /
    # del positions[key]) must run for a _broker_closed close too -- it is
    # gated on `not dry_run`, not on which close branch ran.
    src = inspect.getsource(fr._run_exits)
    close_dispatch = src.index("elif _broker_closed:")
    booking = src.index("pnl_tracker.log_close(", close_dispatch)
    del_pos = src.index("del positions[key]", close_dispatch)
    assert close_dispatch < booking < del_pos


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
