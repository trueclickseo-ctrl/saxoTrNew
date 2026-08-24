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


def make_position(uic, amount, asset_type="FxSpot"):
    """One raw Saxo position record, shaped like /port/v1/positions/me."""
    return {"PositionBase": {"Uic": uic, "Amount": amount, "AssetType": asset_type},
            "PositionView": {}}


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


_run("a live position with zero stop/TP is flagged naked and triggers exactly one email",
     test_naked_position_detected_with_no_stop_at_all)
_run("a live position with full stop coverage is never flagged",
     test_position_with_full_stop_coverage_is_not_naked)
_run("a take-profit-only position (no stop-loss) is flagged tp_only, not treated as protected",
     test_take_profit_only_is_flagged_tp_only_not_fully_protected)
_run("a stop covering less than the full position is flagged partial",
     test_partial_stop_coverage_flagged_partial)


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
