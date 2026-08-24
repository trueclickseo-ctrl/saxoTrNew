"""
test_housekeeping.py
---------------------
Regression tests for housekeeping.py, the cross-module (forex/futures/
etf/stocks) reconciliation engine built 2026-08-24 after a manual audit
found local state tracking positions that Saxo had silently netted away,
duplicate stop orders left over from breakeven moves, and multiple live
positions sitting with zero stop-loss protection.

No test here talks to real Saxo — every LiveSnapshot is hand-built so the
engine's DECISION LOGIC is pinned down independent of account state, using
a lightweight fake adapter that records what it was told to do instead of
touching real files/orders.
"""

import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import housekeeping as hk

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


# ── Test doubles ────────────────────────────────────────────────────────

class FakeAdapter(hk.BaseAdapter):
    """Records save()/replace_stop()/cancel_stop() calls instead of
    touching real files or Saxo orders, so tests can assert on decisions
    without any I/O."""

    def __init__(self, module, positions, can_auto_remove=True):
        self.module = module
        self.asset_types = {"Test"}
        self.can_auto_remove = can_auto_remove
        self._positions = positions
        self.saved_positions = None
        self.removed_keys = None
        self.replace_calls = []
        self.cancel_calls = []
        self._next_oid = 9000

    def load(self):
        return list(self._positions)

    def save(self, positions, removed_keys):
        self.saved_positions = positions
        self.removed_keys = removed_keys

    def replace_stop(self, pos, new_quantity, price):
        self.replace_calls.append((pos.key, new_quantity, price))
        self._next_oid += 1
        return f"NEWSTOP{self._next_oid}"

    def cancel_stop(self, order_id):
        self.cancel_calls.append(order_id)
        return True


def make_position(uic, amount, asset_type="FxSpot", current_price=None):
    """One raw Saxo position record, shaped like /port/v1/positions/me."""
    view = {"CurrentPrice": current_price} if current_price is not None else {}
    return {"PositionBase": {"Uic": uic, "Amount": amount, "AssetType": asset_type},
            "PositionView": view}


def make_order(order_id, uic, buy_sell, amount, price, order_type="Stop",
               status="Working", order_time="2026-08-20T00:00:00Z"):
    return {"OrderId": order_id, "Uic": uic, "BuySell": buy_sell, "Amount": amount,
            "Price": price, "OpenOrderType": order_type, "Status": status,
            "OrderTime": order_time}


def make_snapshot(positions=None, orders=None):
    positions = positions or []
    orders = orders or []
    positions_by_uic = {}
    for p in positions:
        positions_by_uic.setdefault(p["PositionBase"]["Uic"], []).append(p)
    orders_by_uic = {}
    for o in orders:
        orders_by_uic.setdefault(o["Uic"], []).append(o)
    return hk.LiveSnapshot(positions_by_uic, orders_by_uic)


# ═══════════════════════════════════════════════════════════════════════
section("Orphaned local entry: no live backing at all")
# ═══════════════════════════════════════════════════════════════════════

def test_orphan_removed_when_no_live_position():
    entries = [hk.LocalPosition("fake", "strat:AAA", 1, "AAA", "Buy", 100,
                                "FxSpot", stop_order_id="OLD1")]
    adapter = FakeAdapter("fake", entries)
    snap = make_snapshot(positions=[], orders=[])   # nothing live for uic 1 at all
    findings = hk.reconcile_module(adapter, snap)
    assert len(findings) == 1
    assert findings[0].kind == hk.KIND_REMOVED_ORPHAN
    assert adapter.removed_keys == ["strat:AAA"]
    assert adapter.cancel_calls == ["OLD1"]


def test_ledger_adapter_never_auto_removes_orphan():
    entries = [hk.LocalPosition("stocks", "42", 1, "AAA", "Buy", 100,
                                "Stock", stop_order_id=None)]
    adapter = FakeAdapter("stocks", entries, can_auto_remove=False)
    snap = make_snapshot(positions=[], orders=[])
    findings = hk.reconcile_module(adapter, snap)
    assert len(findings) == 1
    assert findings[0].kind == hk.KIND_LEDGER_DRIFT, (
        "a ledger-backed module (stocks) must report drift, never silently "
        "mark a row closed without a real exit price/date"
    )
    assert adapter.removed_keys is None, "must not have called save() with a removal"


_run("orphaned local entry removed + its stop cancelled when nothing live backs it",
     test_orphan_removed_when_no_live_position)
_run("ledger-backed module (stocks) reports drift instead of auto-removing a row",
     test_ledger_adapter_never_auto_removes_orphan)


# ═══════════════════════════════════════════════════════════════════════
section("Pending entry order: no live position yet, but not an orphan")
# ═══════════════════════════════════════════════════════════════════════

def test_pending_entry_left_alone_not_orphaned():
    """2026-08-24: safeguard incorrectly orphaned 7 ETF Market entries placed
    while the US market was closed (not yet filled -> zero live position),
    cancelling their bracket legs. A local entry with zero live position must
    NOT be treated as an orphan if a matching-direction Working entry order
    (Market/Limit/StopLimit, not an IfDoneSlave bracket child) is still open."""
    entries = [hk.LocalPosition("fake", "strat:XLB", 35414, "XLB", "Buy", 50,
                                "Etf", stop_order_id="STOP1")]
    adapter = FakeAdapter("fake", entries)
    orders = [make_order("ENTRY1", 35414, "Buy", 50, 53.54, order_type="Market")]
    snap = make_snapshot(positions=[], orders=orders)   # no live position yet
    findings = hk.reconcile_module(adapter, snap)
    assert len(findings) == 1
    assert findings[0].kind == hk.KIND_PENDING_ENTRY
    assert adapter.saved_positions is None, "must not touch local state for a pending entry"
    assert adapter.removed_keys is None, "must NOT orphan a not-yet-filled entry"
    assert adapter.cancel_calls == [], "must not cancel anything for a pending entry"


def test_bracket_child_orders_do_not_count_as_pending_entry():
    """An IfDoneSlave bracket child (the dormant stop/tp leg of an unfilled
    parent) must NOT be mistaken for a standalone pending entry order -
    only a real top-level entry order should suppress the orphan check."""
    order = make_order("CHILD1", 400, "Sell", 1000, 1.05, order_type="Stop")
    order["OrderRelation"] = "IfDoneSlave"
    snap = make_snapshot(positions=[], orders=[order])
    assert snap.has_pending_entry(400, "Buy") is False, (
        "a dormant bracket child leg must not be treated as a pending entry order"
    )


_run("a local entry with zero live position but a still-Working entry order is left alone, not orphaned",
     test_pending_entry_left_alone_not_orphaned)
_run("a dormant IfDoneSlave bracket child order does not count as a pending entry",
     test_bracket_child_orders_do_not_count_as_pending_entry)


# ═══════════════════════════════════════════════════════════════════════
section("Overstated local quantity: Saxo netted part of it away")
# ═══════════════════════════════════════════════════════════════════════

def test_two_strategies_scaled_down_proportionally_to_match_live():
    entries = [
        hk.LocalPosition("fake", "strat_a:BBB", 2, "BBB", "Buy", 81000,
                         "FxSpot", stop_order_id="OLD_A"),
        hk.LocalPosition("fake", "strat_b:BBB", 2, "BBB", "Buy", 61000,
                         "FxSpot", stop_order_id="OLD_B"),
    ]
    adapter = FakeAdapter("fake", entries)
    # live exposure is only 114,000, not the 142,000 the two entries sum to
    snap = make_snapshot(positions=[make_position(2, 114000)])
    findings = hk.reconcile_module(adapter, snap)

    scaled = [f for f in findings if f.kind == hk.KIND_SCALED_DOWN]
    assert len(scaled) == 2
    assert all(f.estimate for f in scaled), "a proportional split must always be flagged as an estimate"

    saved_qtys = {p.key: p.quantity for p in adapter.saved_positions}
    assert sum(saved_qtys.values()) == 114000, "corrected quantities must sum to exactly the live amount"
    # ratio preserved: 81000:61000 ~= new_a:new_b
    assert saved_qtys["strat_a:BBB"] > saved_qtys["strat_b:BBB"]

    # cancelling the old stop is the real adapters' own responsibility inside
    # replace_stop() (proven by the ForexAdapter/FuturesAdapter/ETFAdapter
    # implementations below) - the engine's job is just to call replace_stop
    # with the corrected quantity, which this asserts:
    assert len(adapter.replace_calls) == 2
    assert {c[0] for c in adapter.replace_calls} == {"strat_a:BBB", "strat_b:BBB"}
    assert {c[1] for c in adapter.replace_calls} == {65028, 48972}


def test_scale_down_persists_the_corrected_stop_price_not_just_the_order_id():
    """2026-08-24: found live -- save() wrote the new stop_order_id but
    never the price it was actually placed at, so local state's
    stop_price silently drifted from reality every time a scale-down fix
    touched a position (18 real drifted stops found on the live account
    the day this was built). trigger_price comes from the CURRENT live
    order (via _entry_stop_price, mocked here), not invented."""
    entries = [hk.LocalPosition("fake", "strat_a:CCC", 3, "CCC", "Buy", 81000,
                                "FxSpot", stop_order_id="OLD_A", stop_price=1.0000)]
    adapter = FakeAdapter("fake", entries)
    orig_get_orders = hk.saxo_client.get_orders
    try:
        hk.saxo_client.get_orders = lambda: {"Data": [
            {"OrderId": "OLD_A", "Price": 1.2345},
        ]}
        snap = make_snapshot(positions=[make_position(3, 40000)])
        hk.reconcile_module(adapter, snap)
        saved = {p.key: p for p in adapter.saved_positions}
        assert saved["strat_a:CCC"].stop_price == 1.2345, (
            "the corrected entry's stop_price must be updated to the price the new stop "
            "was actually placed at, not left at the old (now stale) 1.0000"
        )
    finally:
        hk.saxo_client.get_orders = orig_get_orders


def test_scale_down_never_exceeds_live_exposure_with_three_way_split():
    entries = [
        hk.LocalPosition("fake", f"s{i}:CCC", 3, "CCC", "Sell", qty, "FxSpot", stop_order_id=f"OLD{i}")
        for i, qty in enumerate([10000, 7000, 5000])
    ]
    adapter = FakeAdapter("fake", entries)
    snap = make_snapshot(positions=[make_position(3, -9000)])  # short 9,000 live, local sums to 22,000
    hk.reconcile_module(adapter, snap)
    total = sum(p.quantity for p in adapter.saved_positions)
    assert total == 9000, f"split across 3 strategies must still sum to live exposure exactly, got {total}"


def test_forex_adapter_replace_stop_cancels_old_before_placing_new():
    """The engine trusts each real adapter's replace_stop() to cancel the
    old order before placing the new one (see the scale-down test above,
    which only checks the engine's own call, not this). Pin that behavior
    down directly against ForexAdapter's actual implementation."""
    import forex.runner as fr
    adapter = hk.ForexAdapter()
    calls = []
    orig_cancel = saxo_client_cancel = hk.saxo_client.cancel_order
    orig_post = fr._post
    orig_get_key = hk.saxo_client.get_account_key
    try:
        hk.saxo_client.cancel_order = lambda oid: calls.append(("cancel", oid)) or True
        hk.saxo_client.get_account_key = lambda: "AKEY"
        fr._post = lambda path, body: calls.append(("post", body["Amount"])) or {"OrderId": "NEW123"}
        pos = hk.LocalPosition("forex", "donchian:EURUSD", 21, "EURUSD", "Buy", 100000,
                               "FxSpot", stop_order_id="OLDSTOP")
        new_oid = adapter.replace_stop(pos, 80000, 1.1000)
        assert new_oid == "NEW123"
        assert calls[0] == ("cancel", "OLDSTOP"), "must cancel the old stop BEFORE placing the new one"
        assert calls[1] == ("post", 80000), "the new order must carry the CORRECTED quantity"
    finally:
        hk.saxo_client.cancel_order = orig_cancel
        hk.saxo_client.get_account_key = orig_get_key
        fr._post = orig_post


_run("two strategies on the same symbol scaled down proportionally, summing exactly to live exposure",
     test_two_strategies_scaled_down_proportionally_to_match_live)
_run("a scale-down fix persists the corrected stop_price, not just the new order id",
     test_scale_down_persists_the_corrected_stop_price_not_just_the_order_id)
_run("ForexAdapter.replace_stop cancels the old order before placing the corrected one",
     test_forex_adapter_replace_stop_cancels_old_before_placing_new)
_run("proportional split across 3 strategies still sums exactly to live exposure",
     test_scale_down_never_exceeds_live_exposure_with_three_way_split)


# ═══════════════════════════════════════════════════════════════════════
section("Direction mismatch and untracked live exposure: report only, never auto-fix")
# ═══════════════════════════════════════════════════════════════════════

def test_direction_mismatch_is_flagged_not_corrected():
    entries = [hk.LocalPosition("fake", "strat:DDD", 4, "DDD", "Buy", 50000, "FxSpot")]
    adapter = FakeAdapter("fake", entries)
    snap = make_snapshot(positions=[make_position(4, -200000)])  # live is SHORT, local thinks Buy
    findings = hk.reconcile_module(adapter, snap)
    assert len(findings) == 1
    assert findings[0].kind == hk.KIND_DIRECTION_MISMATCH
    assert adapter.saved_positions is None, "must never touch state on a direction mismatch"
    assert adapter.replace_calls == [] and adapter.cancel_calls == []


def test_untracked_extra_live_exposure_is_reported_not_fabricated():
    entries = [hk.LocalPosition("fake", "strat:EEE", 5, "EEE", "Buy", 39000, "FxSpot")]
    adapter = FakeAdapter("fake", entries)
    snap = make_snapshot(positions=[make_position(5, 187000)])  # far more live than tracked
    findings = hk.reconcile_module(adapter, snap)
    assert len(findings) == 1
    assert findings[0].kind == hk.KIND_UNTRACKED_LIVE
    assert adapter.saved_positions is None, "must never invent a new local entry to explain untracked exposure"


_run("opposite-direction live position is flagged only, never auto-corrected",
     test_direction_mismatch_is_flagged_not_corrected)
_run("live exposure exceeding local tracking is reported, not fabricated into a new entry",
     test_untracked_extra_live_exposure_is_reported_not_fabricated)


# ═══════════════════════════════════════════════════════════════════════
section("Duplicate stop orders")
# ═══════════════════════════════════════════════════════════════════════

def test_exact_duplicate_stop_cancelled_keeps_newest():
    entries = [hk.LocalPosition("fake", "strat:FFF", 6, "FFF", "Buy", 1000, "FxSpot", stop_order_id="KEEP")]
    adapter = FakeAdapter("fake", entries)
    orders = [
        make_order("OLD_DUP", 6, "Sell", 1000, 1.2345, order_time="2026-08-18T10:00:00Z"),
        make_order("KEEP",    6, "Sell", 1000, 1.2345, order_time="2026-08-19T10:00:00Z"),
    ]
    snap = make_snapshot(positions=[make_position(6, 1000)], orders=orders)
    findings = hk.reconcile_module(adapter, snap)
    dup_findings = [f for f in findings if f.kind == hk.KIND_DUPLICATE_STOP]
    assert len(dup_findings) == 1
    assert adapter.cancel_calls == ["OLD_DUP"], "must cancel the older duplicate and keep the newer one"


def test_different_price_stops_are_not_treated_as_duplicates():
    entries = [hk.LocalPosition("fake", "strat:GGG", 7, "GGG", "Buy", 1000, "FxSpot", stop_order_id="A")]
    adapter = FakeAdapter("fake", entries)
    orders = [
        make_order("A", 7, "Sell", 1000, 1.1000),
        make_order("B", 7, "Sell", 500,  1.2000),   # different price -> a legitimate second order, not a dupe
    ]
    snap = make_snapshot(positions=[make_position(7, 1000)], orders=orders)
    findings = hk.reconcile_module(adapter, snap)
    assert not any(f.kind == hk.KIND_DUPLICATE_STOP for f in findings)
    assert adapter.cancel_calls == []


_run("literal duplicate stop orders (same side+price) collapsed to one, keeping the newest",
     test_exact_duplicate_stop_cancelled_keeps_newest)
_run("two stop orders at different prices on the same instrument are NOT treated as duplicates",
     test_different_price_stops_are_not_treated_as_duplicates)


# ═══════════════════════════════════════════════════════════════════════
section("Stop integrity: the real broker order, not just proximity to price")
# ═══════════════════════════════════════════════════════════════════════
# 2026-08-24: replaces the forex_dashboard.py-only "near stop" warning,
# which only ever got noticed if someone was watching the terminal at the
# right moment. This checks something more valuable than proximity: does
# the LOCAL entry's remembered stop_order_id actually correspond to a
# real, correctly-priced Working order right now?

def test_stop_missing_entirely_gets_replaced_at_local_stop_price():
    entries = [hk.LocalPosition("fake", "strat:HHH", 8, "HHH", "Buy", 1000, "FxSpot",
                                stop_order_id="GONE", stop_price=1.05)]
    adapter = FakeAdapter("fake", entries)
    # "GONE" isn't in the live orders at all -- filled, cancelled, or never really placed
    snap = make_snapshot(positions=[make_position(8, 1000)], orders=[])
    findings = hk.reconcile_module(adapter, snap)
    missing = [f for f in findings if f.kind == hk.KIND_STOP_MISSING]
    assert len(missing) == 1
    assert adapter.replace_calls == [("strat:HHH", 1000, 1.05)], (
        "must re-place a stop at THIS entry's own already-computed risk level, not guess a new one"
    )
    assert adapter.saved_positions is not None, "the corrected stop_order_id must be persisted"


def test_stop_present_but_not_working_status_also_gets_replaced():
    entries = [hk.LocalPosition("fake", "strat:III", 9, "III", "Sell", 1000, "FxSpot",
                                stop_order_id="CANCELLED_OID", stop_price=2.5)]
    adapter = FakeAdapter("fake", entries)
    stale = make_order("CANCELLED_OID", 9, "Buy", 1000, 2.5, status="Cancelled")
    snap = make_snapshot(positions=[make_position(9, -1000)], orders=[stale])
    findings = hk.reconcile_module(adapter, snap)
    assert any(f.kind == hk.KIND_STOP_MISSING for f in findings)
    assert adapter.replace_calls == [("strat:III", 1000, 2.5)]


def test_stop_matching_live_price_produces_no_finding():
    entries = [hk.LocalPosition("fake", "strat:JJJ", 10, "JJJ", "Buy", 1000, "FxSpot",
                                stop_order_id="GOOD", stop_price=1.10000)]
    adapter = FakeAdapter("fake", entries)
    good = make_order("GOOD", 10, "Sell", 1000, 1.10000)
    snap = make_snapshot(positions=[make_position(10, 1000)], orders=[good])
    findings = hk.reconcile_module(adapter, snap)
    assert not any(f.kind in (hk.KIND_STOP_MISSING, hk.KIND_STOP_STALE) for f in findings)
    assert adapter.replace_calls == [], "a correctly-priced live stop must not be touched"


def test_stop_price_drift_is_auto_corrected_to_match_the_real_broker_order():
    """A real Working order exists at that id, just at a different price
    than local state believes (the stop_price-never-persisted bug found
    2026-08-24). The broker's real Working order is what actually
    protects the position, so local adopts it -- no broker-side order is
    touched, only local's own bookkeeping."""
    entries = [hk.LocalPosition("fake", "strat:KKK", 11, "KKK", "Buy", 1000, "FxSpot",
                                stop_order_id="DRIFTED", stop_price=1.10000)]
    adapter = FakeAdapter("fake", entries)
    drifted = make_order("DRIFTED", 11, "Sell", 1000, 1.15000)  # 4.5% away from local's belief
    snap = make_snapshot(positions=[make_position(11, 1000)], orders=[drifted])
    findings = hk.reconcile_module(adapter, snap)
    stale = [f for f in findings if f.kind == hk.KIND_STOP_STALE]
    assert len(stale) == 1
    assert entries[0].stop_price == 1.15000, "local must adopt the broker's real stop price"
    assert adapter.saved_positions is not None and any(
        p.key == "strat:KKK" and p.stop_price == 1.15000 for p in adapter.saved_positions
    ), "the correction must actually be persisted"
    assert adapter.replace_calls == [], "must never touch the real (already-correct) broker order"
    assert adapter.cancel_calls == [], "must not cancel the real live stop"


def test_entry_with_no_stop_order_id_is_skipped_not_flagged():
    """No stop_order_id at all means nothing to verify -- that's
    scan_naked_positions()'s job (is there ANY stop covering this), not
    this check's (is the SPECIFIC remembered stop correct)."""
    entries = [hk.LocalPosition("fake", "strat:LLL", 12, "LLL", "Buy", 1000, "FxSpot", stop_order_id=None)]
    adapter = FakeAdapter("fake", entries)
    snap = make_snapshot(positions=[make_position(12, 1000)], orders=[])
    findings = hk.reconcile_module(adapter, snap)
    assert not any(f.kind in (hk.KIND_STOP_MISSING, hk.KIND_STOP_STALE) for f in findings)
    assert adapter.replace_calls == []


_run("a stop_order_id with no matching live order at all is re-placed at the entry's own stop_price",
     test_stop_missing_entirely_gets_replaced_at_local_stop_price)
_run("a stop order that exists but isn't Working (filled/cancelled) is treated the same as missing",
     test_stop_present_but_not_working_status_also_gets_replaced)
_run("a stop correctly matching the live order's price produces no finding and isn't touched",
     test_stop_matching_live_price_produces_no_finding)
_run("a stop whose live price differs from local's belief is auto-corrected to match the real broker order",
     test_stop_price_drift_is_auto_corrected_to_match_the_real_broker_order)
_run("an entry with no stop_order_id at all is skipped by this check (scan_naked_positions's job instead)",
     test_entry_with_no_stop_order_id_is_skipped_not_flagged)


# ═══════════════════════════════════════════════════════════════════════
section("Exact match: nothing to do")
# ═══════════════════════════════════════════════════════════════════════

def test_no_findings_when_everything_already_matches():
    entries = [hk.LocalPosition("fake", "strat:HHH", 8, "HHH", "Buy", 5000, "FxSpot", stop_order_id="S1")]
    adapter = FakeAdapter("fake", entries)
    snap = make_snapshot(
        positions=[make_position(8, 5000)],
        orders=[make_order("S1", 8, "Sell", 5000, 1.0)],
    )
    findings = hk.reconcile_module(adapter, snap)
    assert findings == [], "a fully-matching account must produce zero findings and touch nothing"
    assert adapter.saved_positions is None
    assert adapter.cancel_calls == []


_run("perfectly matching local state produces zero findings and no writes", test_no_findings_when_everything_already_matches)


# ═══════════════════════════════════════════════════════════════════════
section("Fully-untracked live position: zero local footprint in ANY module")
# ═══════════════════════════════════════════════════════════════════════

def test_fully_untracked_live_position_is_flagged():
    """2026-08-24: reconcile_module() groups by uic starting from LOCAL
    entries, so a uic with NO local record at all (in any module) never
    enters that loop -- structurally invisible. This is exactly what let
    a -2,381,000 EURCHF position and a 20,000-share stock position both
    hide from reconcile_all() entirely. _scan_fully_untracked() closes
    that gap with a dedicated live-position sweep."""
    orig_adapters = dict(hk.ADAPTERS)
    try:
        hk.ADAPTERS.clear()
        hk.ADAPTERS["stocks"] = FakeAdapter("stocks", [])  # nothing tracked anywhere
        snap = make_snapshot(positions=[make_position(999, -20000, asset_type="Stock")])
        findings = hk._scan_fully_untracked(snap, ["stocks"])
        assert len(findings) == 1
        assert findings[0].kind == hk.KIND_FULLY_UNTRACKED
        assert findings[0].module == "stocks"
    finally:
        hk.ADAPTERS.clear()
        hk.ADAPTERS.update(orig_adapters)


def test_fully_untracked_scan_ignores_uics_with_any_local_record():
    """A uic already tracked by SOME local entry -- even a mismatched one
    -- is reconcile_module()'s job, not this scan's. Only a truly BLANK
    uic (zero entries anywhere) should surface here."""
    orig_adapters = dict(hk.ADAPTERS)
    try:
        hk.ADAPTERS.clear()
        tracked = [hk.LocalPosition("stocks", "s:AAA", 999, "AAA", "Sell", 5000, "Stock")]
        hk.ADAPTERS["stocks"] = FakeAdapter("stocks", tracked)
        snap = make_snapshot(positions=[make_position(999, -20000, asset_type="Stock")])
        findings = hk._scan_fully_untracked(snap, ["stocks"])
        assert findings == [], "a uic with ANY local record must be left to reconcile_module(), not flagged here"
    finally:
        hk.ADAPTERS.clear()
        hk.ADAPTERS.update(orig_adapters)


def test_fully_untracked_reported_fixed_not_error_by_safeguard():
    import safeguard as sg
    orig_adapters = dict(hk.ADAPTERS)
    try:
        hk.ADAPTERS.clear()
        hk.ADAPTERS["stocks"] = FakeAdapter("stocks", [])
        snap = make_snapshot(positions=[make_position(999, -20000, asset_type="Stock")])
        outcomes = sg._fix_mismatches(["stocks"], snap)
        assert len(outcomes) == 1
        assert outcomes[0].fixed is True, "must not be reported as NOT FIXED -- this finding exists to surface, not to fail"
        assert outcomes[0].action == "no_local_record_needs_human_review"
    finally:
        hk.ADAPTERS.clear()
        hk.ADAPTERS.update(orig_adapters)


def test_fully_untracked_included_in_reconcile_all():
    orig_adapters = dict(hk.ADAPTERS)
    orig_fetch = hk.fetch_live_snapshot
    orig_send = hk._send_email
    emails = []
    try:
        hk.ADAPTERS.clear()
        hk.ADAPTERS["stocks"] = FakeAdapter("stocks", [])
        hk.fetch_live_snapshot = lambda: make_snapshot(positions=[make_position(999, -20000, asset_type="Stock")])
        hk._send_email = lambda subject, html: emails.append(subject) or True
        findings = hk.reconcile_all()
        assert any(f.kind == hk.KIND_FULLY_UNTRACKED for f in findings)
        assert len(emails) == 1
    finally:
        hk.ADAPTERS.clear()
        hk.ADAPTERS.update(orig_adapters)
        hk.fetch_live_snapshot = orig_fetch
        hk._send_email = orig_send


_run("a live position with ZERO local record in any module is flagged fully_untracked",
     test_fully_untracked_live_position_is_flagged)
_run("a uic with ANY local record (even mismatched) is left to reconcile_module(), not double-flagged",
     test_fully_untracked_scan_ignores_uics_with_any_local_record)
_run("safeguard reports a fully_untracked finding as fixed/informational, not an error",
     test_fully_untracked_reported_fixed_not_error_by_safeguard)
_run("reconcile_all() includes the fully-untracked sweep and still emails on any finding",
     test_fully_untracked_included_in_reconcile_all)


# ═══════════════════════════════════════════════════════════════════════
section("reconcile_all(): aggregation and email-on-mismatch-only")
# ═══════════════════════════════════════════════════════════════════════

def test_reconcile_all_sends_email_only_when_findings_exist():
    emails = []
    orig_adapters = dict(hk.ADAPTERS)
    orig_fetch = hk.fetch_live_snapshot
    orig_send = hk._send_email
    try:
        clean_entries = [hk.LocalPosition("clean", "s:CLN", 100, "CLN", "Buy", 10, "Test", stop_order_id="S")]
        hk.ADAPTERS.clear()
        hk.ADAPTERS["clean"] = FakeAdapter("clean", clean_entries)
        hk.fetch_live_snapshot = lambda: make_snapshot(
            positions=[make_position(100, 10)], orders=[make_order("S", 100, "Sell", 10, 1.0)])
        hk._send_email = lambda subject, html: emails.append(subject) or True

        findings = hk.reconcile_all()
        assert findings == []
        assert emails == [], "must not send an email when nothing was wrong"

        # now make it dirty and re-run
        dirty_entries = [hk.LocalPosition("dirty", "s:DRT", 101, "DRT", "Buy", 10, "Test")]
        hk.ADAPTERS["dirty"] = FakeAdapter("dirty", dirty_entries)
        hk.fetch_live_snapshot = lambda: make_snapshot(positions=[], orders=[])
        findings = hk.reconcile_all()
        assert len(findings) >= 1
        assert len(emails) == 1, "must send exactly one email summarizing every mismatch found this run"
    finally:
        hk.ADAPTERS.clear()
        hk.ADAPTERS.update(orig_adapters)
        hk.fetch_live_snapshot = orig_fetch
        hk._send_email = orig_send


_run("reconcile_all() stays silent on a clean account and emails once when mismatches exist",
     test_reconcile_all_sends_email_only_when_findings_exist)


# ═══════════════════════════════════════════════════════════════════════
section("scan_naked_positions(): live-Saxo-only, no local state touched")
# ═══════════════════════════════════════════════════════════════════════

def test_naked_position_detected_with_no_stop_at_all():
    orig_forex_load = hk.ADAPTERS["forex"].load
    orig_fetch = hk.fetch_live_snapshot
    orig_send = hk._send_email
    emails = []
    try:
        hk.ADAPTERS["forex"].load = lambda: []
        hk.fetch_live_snapshot = lambda: make_snapshot(
            positions=[make_position(200, -50000, asset_type="Stock")], orders=[])
        hk._send_email = lambda subject, html: emails.append(subject) or True

        naked = hk.scan_naked_positions()
        assert len(naked) == 1
        assert naked[0].protection == "none"
        assert len(emails) == 1
    finally:
        hk.ADAPTERS["forex"].load = orig_forex_load
        hk.fetch_live_snapshot = orig_fetch
        hk._send_email = orig_send


def test_position_with_full_stop_coverage_is_not_naked():
    orig_forex_load = hk.ADAPTERS["forex"].load
    orig_fetch = hk.fetch_live_snapshot
    try:
        hk.ADAPTERS["forex"].load = lambda: []
        hk.fetch_live_snapshot = lambda: make_snapshot(
            positions=[make_position(201, 10000, asset_type="Etf")],
            orders=[make_order("S", 201, "Sell", 10000, 50.0)],
        )
        naked = hk.scan_naked_positions()
        assert naked == []
    finally:
        hk.ADAPTERS["forex"].load = orig_forex_load
        hk.fetch_live_snapshot = orig_fetch


def test_take_profit_only_is_flagged_tp_only_not_fully_protected():
    orig_forex_load = hk.ADAPTERS["forex"].load
    orig_fetch = hk.fetch_live_snapshot
    try:
        hk.ADAPTERS["forex"].load = lambda: []
        hk.fetch_live_snapshot = lambda: make_snapshot(
            positions=[make_position(202, -20000, asset_type="Stock")],
            orders=[make_order("L", 202, "Buy", 20000, 40.0, order_type="Limit")],
        )
        naked = hk.scan_naked_positions()
        assert len(naked) == 1
        assert naked[0].protection == "tp_only", (
            "a take-profit limit order is not a stop-loss - must not count as real protection"
        )
    finally:
        hk.ADAPTERS["forex"].load = orig_forex_load
        hk.fetch_live_snapshot = orig_fetch


def test_partial_stop_coverage_flagged_partial():
    orig_forex_load = hk.ADAPTERS["forex"].load
    orig_fetch = hk.fetch_live_snapshot
    try:
        hk.ADAPTERS["forex"].load = lambda: []
        hk.fetch_live_snapshot = lambda: make_snapshot(
            positions=[make_position(203, 100000, asset_type="ContractFutures")],
            orders=[make_order("S", 203, "Sell", 40000, 90.0)],  # covers only 40k of 100k
        )
        naked = hk.scan_naked_positions()
        assert len(naked) == 1
        assert naked[0].protection == "partial"
    finally:
        hk.ADAPTERS["forex"].load = orig_forex_load
        hk.fetch_live_snapshot = orig_fetch


def test_multiple_tickets_same_uic_aggregated_into_one_finding_not_multiplied():
    """A uic with 2+ position tickets and SOME pre-existing partial stop
    coverage must produce exactly ONE naked finding sized to the TOTAL
    uncovered gap -- not one finding per ticket, each independently (and
    wrongly) crediting the same shared coverage. Found 2026-08-24: the
    per-ticket version let a real protection gap survive a "fix" because
    summed new stops still cleared each ticket's OWN amount individually
    even though the true aggregate gap was never closed."""
    orig_forex_load = hk.ADAPTERS["forex"].load
    orig_fetch = hk.fetch_live_snapshot
    try:
        hk.ADAPTERS["forex"].load = lambda: []
        # uic 300: two 92,000 tickets (net 184,000) + one existing 22,000
        # stop that covers neither ticket individually but IS real, shared
        # protection against the aggregate.
        hk.fetch_live_snapshot = lambda: make_snapshot(
            positions=[make_position(300, 92000, asset_type="FxSpot"),
                      make_position(300, 92000, asset_type="FxSpot")],
            orders=[make_order("S", 300, "Sell", 22000, 150.0)],
        )
        naked = hk.scan_naked_positions()
        assert len(naked) == 1, "must be ONE finding per uic, not one per ticket"
        assert naked[0].quantity == 184000
        assert naked[0].stop_coverage == 22000
        assert naked[0].uncovered_qty == 162000, (
            "uncovered gap must be computed once against the TOTAL, not per-ticket "
            f"(got {naked[0].uncovered_qty})"
        )
    finally:
        hk.ADAPTERS["forex"].load = orig_forex_load
        hk.fetch_live_snapshot = orig_fetch


def test_stopiftraded_order_counts_as_real_protection():
    """2026-08-24: ZC (corn)'s own SupportedOrderTypes has no "Stop"/
    "StopLimit" at all -- Saxo rejects those outright and only accepts
    StopIfTraded for this instrument. A real StopIfTraded protective order
    was being invisible to the naked scan, producing a false "naked" alert
    on an already-protected position."""
    orig_forex_load = hk.ADAPTERS["forex"].load
    orig_fetch = hk.fetch_live_snapshot
    try:
        hk.ADAPTERS["forex"].load = lambda: []
        hk.fetch_live_snapshot = lambda: make_snapshot(
            positions=[make_position(204, 1, asset_type="ContractFutures")],
            orders=[make_order("S", 204, "Sell", 1, 494.5, order_type="StopIfTraded")],
        )
        naked = hk.scan_naked_positions()
        assert naked == [], "a StopIfTraded protective order must count as real coverage, not be invisible"
    finally:
        hk.ADAPTERS["forex"].load = orig_forex_load
        hk.fetch_live_snapshot = orig_fetch


_run("a live position with zero stop/TP is flagged naked and triggers exactly one email",
     test_naked_position_detected_with_no_stop_at_all)
_run("a StopIfTraded order (ZC/corn's only supported stop type) counts as real protection, not invisible",
     test_stopiftraded_order_counts_as_real_protection)
_run("a live position with full stop coverage is never flagged",
     test_position_with_full_stop_coverage_is_not_naked)
_run("a take-profit-only position (no stop-loss) is flagged tp_only, not treated as protected",
     test_take_profit_only_is_flagged_tp_only_not_fully_protected)
_run("a stop covering less than the full position is flagged partial",
     test_partial_stop_coverage_flagged_partial)
_run("multiple position tickets on the same uic are aggregated into ONE finding, not multiplied",
     test_multiple_tickets_same_uic_aggregated_into_one_finding_not_multiplied)


# ═══════════════════════════════════════════════════════════════════════
section("scan_near_stop_positions(): replaces the old dashboard-only proximity warning")
# ═══════════════════════════════════════════════════════════════════════

def test_position_near_its_stop_is_flagged():
    orig_forex_load = hk.ADAPTERS["forex"].load
    orig_fetch = hk.fetch_live_snapshot
    emails = []
    orig_send = hk._send_email
    try:
        hk.ADAPTERS["forex"].load = lambda: []
        hk.fetch_live_snapshot = lambda: make_snapshot(
            positions=[make_position(400, 100000, current_price=1.10400)],  # LONG
            orders=[make_order("S", 400, "Sell", 100000, 1.10000)],          # 0.36% below price
        )
        hk._send_email = lambda subject, html: emails.append(subject) or True
        near = hk.scan_near_stop_positions()
        assert len(near) == 1
        assert near[0].distance_pct < 0.5
        assert len(emails) == 1
    finally:
        hk.ADAPTERS["forex"].load = orig_forex_load
        hk.fetch_live_snapshot = orig_fetch
        hk._send_email = orig_send


def test_position_far_from_stop_is_not_flagged():
    orig_forex_load = hk.ADAPTERS["forex"].load
    orig_fetch = hk.fetch_live_snapshot
    try:
        hk.ADAPTERS["forex"].load = lambda: []
        hk.fetch_live_snapshot = lambda: make_snapshot(
            positions=[make_position(401, 100000, current_price=1.20000)],  # LONG, well above stop
            orders=[make_order("S", 401, "Sell", 100000, 1.10000)],
        )
        near = hk.scan_near_stop_positions()
        assert near == []
    finally:
        hk.ADAPTERS["forex"].load = orig_forex_load
        hk.fetch_live_snapshot = orig_fetch


def test_naked_position_is_not_double_reported_as_near_stop():
    """A position with zero stop coverage is scan_naked_positions()'s
    finding, not this one's -- reporting it here too would double-count
    the same underlying gap as two different alert types."""
    orig_forex_load = hk.ADAPTERS["forex"].load
    orig_fetch = hk.fetch_live_snapshot
    try:
        hk.ADAPTERS["forex"].load = lambda: []
        hk.fetch_live_snapshot = lambda: make_snapshot(
            positions=[make_position(402, 100000, current_price=1.10000)], orders=[],
        )
        near = hk.scan_near_stop_positions()
        assert near == [], "an unprotected position must not appear in the near-stop scan at all"
    finally:
        hk.ADAPTERS["forex"].load = orig_forex_load
        hk.fetch_live_snapshot = orig_fetch


def test_closest_stop_used_when_multiple_strategies_share_a_uic():
    orig_forex_load = hk.ADAPTERS["forex"].load
    orig_fetch = hk.fetch_live_snapshot
    try:
        hk.ADAPTERS["forex"].load = lambda: []
        hk.fetch_live_snapshot = lambda: make_snapshot(
            positions=[make_position(403, 100000, current_price=1.10000)],
            orders=[make_order("FAR",  403, "Sell", 60000, 1.00000),
                   make_order("NEAR", 403, "Sell", 40000, 1.09800)],  # 0.18% away -- this one matters
        )
        near = hk.scan_near_stop_positions()
        assert len(near) == 1
        assert near[0].stop_price == 1.098
    finally:
        hk.ADAPTERS["forex"].load = orig_forex_load
        hk.fetch_live_snapshot = orig_fetch


_run("a position within threshold of its stop is flagged and emailed",
     test_position_near_its_stop_is_flagged)
_run("a position well clear of its stop is not flagged",
     test_position_far_from_stop_is_not_flagged)
_run("a fully naked position is not double-reported here -- that's scan_naked_positions()'s job",
     test_naked_position_is_not_double_reported_as_near_stop)
_run("with multiple stops on one uic, the CLOSEST one determines the distance reported",
     test_closest_stop_used_when_multiple_strategies_share_a_uic)


# ═══════════════════════════════════════════════════════════════════════
section("Real adapters load without crashing (structural smoke test)")
# ═══════════════════════════════════════════════════════════════════════

def test_all_four_real_adapters_load_without_crashing():
    for name, adapter in hk.ADAPTERS.items():
        try:
            positions = adapter.load()
        except FileNotFoundError:
            continue  # acceptable if this module's local state file doesn't exist in this environment
        assert isinstance(positions, list), f"{name} adapter must return a list from load()"
        for p in positions:
            assert isinstance(p, hk.LocalPosition)
            assert p.module == name
            assert p.direction in ("Buy", "Sell")


_run("every real module adapter (forex/futures/etf/stocks) loads its local state without crashing",
     test_all_four_real_adapters_load_without_crashing)


# ═══════════════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════════════

print(f"\n{BOLD}{'='*70}{RESET}")
passed = sum(1 for _, ok, _ in _results if ok)
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
