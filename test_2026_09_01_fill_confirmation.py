"""
Regression test -- 2026-09-01 order fill-confirmation / real-fill-price.

Saxo's order POST returns only an OrderId -- no fill, no price. Both
forex/runner.py and atos_runner.py treated "got an OrderId" as "filled at
the scan price". Confirmed live: MXNUSD LIVE EUR booked at 0.058876 /
0.0588435 vs the real Saxo fills 0.058687 / 0.058811 (0.32% entry error,
enough to flip the recorded P&L sign) -- and an accepted-but-unfilled
order recorded a phantom position (the WSM/MTB/GEV stocks re-buy loop).

Fix:
  * forex._confirm_entry_fill / _confirm_exit_fill: poll positions/me and
    closedpositions/me for the REAL average fill via PositionBase.
    SourceOrderId (fallback: same-Uic position opened in the last 3 min);
  * _run_entries records sig["close"] = real fill; a LIVE order that never
    fills is cancelled (entry + bracket legs) and NO position is recorded;
    SIM keeps it at a live quote;
  * _run_exits records the true ClosingPrice, not the pre-close quote;
  * atos_runner._confirm_stock_fill wired into all 3 stock buy paths;
  * fix_live_fill_prices_2026-09-01.py rewrote the 7 open LIVE positions +
    the MXNUSD round-trip from the live Saxo API.
"""

import ast
import inspect
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

G, R, Y, X, B = "\033[92m", "\033[91m", "\033[93m", "\033[0m", "\033[1m"
_res = []


def _run(name, fn):
    try:
        fn()
        _res.append((name, True, None))
    except Exception as e:
        import traceback
        _res.append((name, False, f"{type(e).__name__}: {e}\n{traceback.format_exc()}"))


import forex.runner as fr
import atos_runner as ar


# ── forex._confirm_entry_fill ───────────────────────────────────────────
def _patch(mod, name, fn):
    old = getattr(mod, name)
    setattr(mod, name, fn)
    return lambda: setattr(mod, name, old)


def test_entry_fill_matches_by_source_order_id():
    undo = _patch(fr, "_get", lambda p, params=None: {"Data": [
        {"PositionBase": {"Uic": 21, "SourceOrderId": "999", "OpenPrice": 1.11111}},
        {"PositionBase": {"Uic": 21, "SourceOrderId": "ORD-1", "OpenPrice": 1.23456}},
    ]})
    try:
        ok, px = fr._confirm_entry_fill("ORD-1", 21)
        assert ok is True and px == 1.23456, (ok, px)
    finally:
        undo()


def test_entry_fill_fallback_to_recent_open_on_same_uic():
    from datetime import datetime, timezone
    recent = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    undo = _patch(fr, "_get", lambda p, params=None: {"Data": [
        {"PositionBase": {"Uic": 31, "SourceOrderId": "other", "OpenPrice": 1.35,
                          "ExecutionTimeOpen": recent}},
    ]})
    try:
        ok, px = fr._confirm_entry_fill("NO-MATCH", 31)
        assert ok is True and px == 1.35
    finally:
        undo()


def test_entry_fill_ignores_stale_same_uic_position():
    undo = _patch(fr, "_get", lambda p, params=None: {"Data": [
        {"PositionBase": {"Uic": 31, "SourceOrderId": "old", "OpenPrice": 1.30,
                          "ExecutionTimeOpen": "2020-01-01T00:00:00Z"}},
    ]})
    try:
        fr._FILL_CONFIRM_ATTEMPTS = 1
        ok, px = fr._confirm_entry_fill("NO-MATCH", 31)
        assert ok is False and px == 0.0
    finally:
        undo()
        fr._FILL_CONFIRM_ATTEMPTS = 3


def test_entry_fill_never_raises_on_api_error():
    def boom(p, params=None):
        raise RuntimeError("saxo down")
    undo = _patch(fr, "_get", boom)
    try:
        fr._FILL_CONFIRM_ATTEMPTS = 2
        assert fr._confirm_entry_fill("X", 1) == (False, 0.0)
    finally:
        undo()
        fr._FILL_CONFIRM_ATTEMPTS = 3


def test_exit_fill_takes_most_recent_matching_close():
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    undo = _patch(fr, "_get", lambda p, params=None: {"Data": [
        {"ClosedPosition": {"Uic": 17761, "Amount": 20000, "ClosingPrice": 0.058811,
                            "ExecutionTimeClose": now}},
        {"ClosedPosition": {"Uic": 17761, "Amount": 20000, "ClosingPrice": 0.05,
                            "ExecutionTimeClose": "2020-01-01T00:00:00Z"}},
    ]})
    try:
        assert fr._confirm_exit_fill(17761, 20000, "Buy") == 0.058811
    finally:
        undo()


def test_exit_fill_none_when_nothing_recent():
    undo = _patch(fr, "_get", lambda p, params=None: {"Data": []})
    try:
        assert fr._confirm_exit_fill(17761, 20000, "Buy") is None
    finally:
        undo()


# ── _run_entries / _run_exits wiring ────────────────────────────────────
def test_run_entries_uses_real_fill_and_guards_live_phantom():
    src = inspect.getsource(fr._run_entries)
    assert "_confirm_entry_fill(entry_oid, uic)" in src
    assert 'sig["close"] = _fill_px' in src
    # LIVE unfilled -> cancel entry + both bracket legs, record nothing
    i = src.index("_confirm_entry_fill(entry_oid, uic)")
    tail = src[i:i + 1600]
    assert 'ACCOUNT_ENV in ("live", "live_eur")' in tail
    assert "_cancel_order(_oid, akey)" in tail
    assert "for _oid in (entry_oid, stop_oid, tp_oid)" in tail
    assert tail.index("_cancel_order(_oid, akey)") < tail.index("continue")


def test_run_exits_records_true_close_price():
    src = inspect.getsource(fr._run_exits)
    assert "_confirm_exit_fill(uic, qty, direction)" in src
    i = src.index("_confirm_exit_fill(uic, qty, direction)")
    tail = src[i:i + 600]
    assert "live_px = _real_exit" in tail
    assert "pnl_pct =" in tail   # pnl_pct recomputed off the corrected price


# ── stocks ─────────────────────────────────────────────────────────────
def test_stock_confirm_fill_helper_and_wiring():
    s = inspect.getsource(ar)
    assert "def _confirm_stock_fill(" in s
    # all three buy paths call it
    assert s.count("_confirm_stock_fill(entry_oid,") == 3
    # unfilled -> cancel the orphan entry/stop, then paper (SIM) or skip
    src_fn = inspect.getsource(ar._place_us)
    assert "saxo_client.cancel_order(str(_o))" in src_fn


def test_stock_confirm_fill_source_order_id_match():
    class _C:
        @staticmethod
        def get_positions():
            return {"Data": [
                {"PositionBase": {"Uic": 5, "SourceOrderId": "E9", "OpenPrice": 228.4}},
            ]}
    old = ar.saxo_client
    ar.saxo_client = _C()
    try:
        ar._STOCK_FILL_ATTEMPTS = 1
        assert ar._confirm_stock_fill("E9", 5) == (True, 228.4)
        assert ar._confirm_stock_fill("NOPE", 999) == (False, 0.0)
    finally:
        ar.saxo_client = old
        ar._STOCK_FILL_ATTEMPTS = 3


def test_stock_confirm_fill_never_raises():
    class _C:
        @staticmethod
        def get_positions():
            raise RuntimeError("down")
    old = ar.saxo_client
    ar.saxo_client = _C()
    try:
        ar._STOCK_FILL_ATTEMPTS = 1
        assert ar._confirm_stock_fill("X", 1) == (False, 0.0)
    finally:
        ar.saxo_client = old
        ar._STOCK_FILL_ATTEMPTS = 3


# ── one-time correction applied ────────────────────────────────────────
def test_live_state_entry_prices_corrected():
    import json
    eur = json.load(open(os.path.join(BASE, "data", "forex_live_eur_state.json")))
    pos = eur["positions"]
    assert abs(pos["rsi:EURUSD"]["entry_price"] - 1.15827) < 1e-6
    assert abs(pos["rsi:GBPUSD"]["entry_price"] - 1.3534) < 1e-6
    assert pos["rsi:EURUSD"].get("entry_price_corrected") == "saxo-fill-truth-2026-09-01"
    sek = json.load(open(os.path.join(BASE, "data", "forex_live_state.json")))
    assert abs(sek["positions"]["donchian:EURNOK"]["entry_price"] - 10.87124016) < 1e-6


def test_mxnusd_ledger_and_card_corrected():
    import json
    import sqlite3
    con = sqlite3.connect(os.path.join(BASE, "data", "pnl_ledger.db"))
    row = con.execute("SELECT entry_price, exit_price FROM trades WHERE id=1750").fetchone()
    con.close()
    assert abs(row[0] - 0.058687433740979) < 1e-9 and abs(row[1] - 0.058811) < 1e-9
    cards = os.path.join(BASE, "data", "trade_observation_cards.jsonl")
    entry = exit_ = None
    for line in open(cards, encoding="utf-8"):
        d = json.loads(line)
        if d.get("card_id", "").startswith("live_eur:rsi:MXNUSD:2026-08-28"):
            if d.get("event") == "entry":
                entry = d
            elif d.get("event") == "exit":
                exit_ = d
    assert entry and abs(entry["entry_price"] - 0.058687433740979) < 1e-9
    assert entry.get("price_source") == "saxo-fill-truth-2026-09-01"
    assert exit_ and abs(exit_["exit_price"] - 0.058811) < 1e-9
    assert exit_.get("mae_mfe_invalidated")   # the -207 EUR MAE was nulled


def test_modules_still_parse():
    ast.parse(inspect.getsource(fr))
    ast.parse(open(os.path.join(BASE, "atos_runner.py"), encoding="utf-8").read())


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
