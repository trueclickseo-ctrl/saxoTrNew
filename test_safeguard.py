"""
test_safeguard.py
-------------------
Regression tests for safeguard.py, the agent that ACTS on housekeeping.py's
findings (naked positions, direction mismatches) instead of only reporting
them, then re-verifies against a fresh Saxo snapshot before claiming a fix.

No test here talks to real Saxo. LiveSnapshots are hand-built and every
mutating call (place_protective_stop, cancel_stop, email) is a recorded
fake, same pattern as test_housekeeping.py.
"""

import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import housekeeping as hk
import safeguard as sg

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


# ── Test doubles (mirrors test_housekeeping.py) ────────────────────────────

def make_position(uic, amount, asset_type="FxSpot", current_price=1.0):
    return {"PositionBase": {"Uic": uic, "Amount": amount, "AssetType": asset_type},
            "PositionView": {"CurrentPrice": current_price}}


def make_order(order_id, uic, buy_sell, amount, price, order_type="Stop",
               status="Working", order_time="2026-08-20T00:00:00Z"):
    return {"OrderId": order_id, "Uic": uic, "BuySell": buy_sell, "Amount": amount,
            "Price": price, "OpenOrderType": order_type, "Status": status,
            "OrderTime": order_time}


def make_snapshot(positions=None, orders=None):
    positions = positions or []
    orders = orders or []
    pu, ou = {}, {}
    for p in positions:
        pu.setdefault(p["PositionBase"]["Uic"], []).append(p)
    for o in orders:
        ou.setdefault(o["Uic"], []).append(o)
    return hk.LiveSnapshot(pu, ou)


class FakeAdapter(hk.BaseAdapter):
    def __init__(self, module, positions, can_auto_remove=True):
        self.module = module
        self.asset_types = {"Test"}
        self.can_auto_remove = can_auto_remove
        self._positions = list(positions)
        self.cancel_calls = []
        self.saved_positions = None
        self.removed_keys = None

    def load(self):
        return list(self._positions)

    def save(self, positions, removed_keys):
        self.saved_positions = positions
        self.removed_keys = removed_keys
        self._positions = [p for p in self._positions if p.key not in removed_keys]

    def replace_stop(self, pos, new_quantity, price):
        return "NEWOID"

    def cancel_stop(self, order_id):
        self.cancel_calls.append(order_id)
        return True


# ═══════════════════════════════════════════════════════════════════════
section("Naked-position fix: places a correctly-sized/priced stop")
# ═══════════════════════════════════════════════════════════════════════

def test_naked_none_protection_gets_full_stop_placed():
    calls = []
    orig = sg.saxo_order.place_protective_stop
    orig_key = sg.saxo_client.get_account_key
    try:
        sg.saxo_order.place_protective_stop = lambda **kw: (calls.append(kw), "OID1")[1]
        sg.saxo_client.get_account_key = lambda: "AKEY"
        n = hk.NakedPosition(module="futures", symbol="AUDCAD", uic=999, direction="Sell",
                             quantity=100000, protection="none", asset_type="ContractFutures",
                             current_price=0.90, stop_coverage=0, uncovered_qty=100000)
        outcome = sg._fix_naked_position(n)
        assert outcome.fixed is True
        assert len(calls) == 1
        kw = calls[0]
        assert kw["amount"] == 100000
        assert kw["direction"] == "Sell"
        # short position -> stop must be ABOVE current price (adverse move for a short)
        assert kw["stop_price"] > 0.90
        # 3% default for ContractFutures
        assert abs(kw["stop_price"] - 0.90 * 1.03) < 1e-9
    finally:
        sg.saxo_order.place_protective_stop = orig
        sg.saxo_client.get_account_key = orig_key


def test_naked_partial_protection_only_covers_the_uncovered_remainder():
    calls = []
    orig = sg.saxo_order.place_protective_stop
    orig_key = sg.saxo_client.get_account_key
    try:
        sg.saxo_order.place_protective_stop = lambda **kw: (calls.append(kw), "OID2")[1]
        sg.saxo_client.get_account_key = lambda: "AKEY"
        n = hk.NakedPosition(module="forex", symbol="CADJPY", uic=888, direction="Buy",
                             quantity=187000, protection="partial", asset_type="FxSpot",
                             current_price=95.0, stop_coverage=100000, uncovered_qty=87000)
        outcome = sg._fix_naked_position(n)
        assert outcome.fixed is True
        assert calls[0]["amount"] == 87000, "must protect only the gap, not the whole position again"
        # long position -> stop must be BELOW current price
        assert calls[0]["stop_price"] < 95.0
    finally:
        sg.saxo_order.place_protective_stop = orig
        sg.saxo_client.get_account_key = orig_key


def test_naked_already_covered_by_the_time_it_runs_needs_no_action():
    n = hk.NakedPosition(module="etf", symbol="XLF", uic=1, direction="Buy", quantity=1000,
                         protection="none", asset_type="Etf", current_price=50.0,
                         stop_coverage=1000, uncovered_qty=0)
    outcome = sg._fix_naked_position(n)
    assert outcome.fixed is True
    assert outcome.action == "none needed"


def test_naked_fix_reports_not_fixed_when_saxo_rejects_the_order():
    orig = sg.saxo_order.place_protective_stop
    orig_key = sg.saxo_client.get_account_key
    try:
        sg.saxo_order.place_protective_stop = lambda **kw: None   # Saxo rejected it
        sg.saxo_client.get_account_key = lambda: "AKEY"
        n = hk.NakedPosition(module="stocks", symbol="AAPL", uic=2, direction="Buy", quantity=10,
                             protection="none", asset_type="Stock", current_price=200.0,
                             stop_coverage=0, uncovered_qty=10)
        outcome = sg._fix_naked_position(n)
        assert outcome.fixed is False, "must never claim fixed when the order was rejected"
    finally:
        sg.saxo_order.place_protective_stop = orig
        sg.saxo_client.get_account_key = orig_key


def test_non_forex_fxspot_uses_live_decimals_lookup_not_generic_5dp_guess():
    """A futures-module symbol like CADMXN can have Saxo AssetType="FxSpot"
    without being in forex.runner's cached 117-pair universe. Found
    2026-08-24 live: the generic FxSpot 5dp guess triggered a real
    PriceNotInTickSizeIncrements rejection (CADMXN actually needs 4dp) --
    must go through _live_price_decimals() instead for any module."""
    calls = []
    orig_place = sg.saxo_order.place_protective_stop
    orig_key = sg.saxo_client.get_account_key
    orig_lookup = sg._live_price_decimals
    try:
        sg.saxo_order.place_protective_stop = lambda **kw: (calls.append(kw), "OID")[1]
        sg.saxo_client.get_account_key = lambda: "AKEY"
        sg._live_price_decimals = lambda uic, asset_type: 4
        n = hk.NakedPosition(module="futures", symbol="CADMXN", uic=23739, direction="Buy",
                             quantity=54000, protection="none", asset_type="FxSpot",
                             current_price=12.35, stop_coverage=0, uncovered_qty=54000)
        outcome = sg._fix_naked_position(n)
        assert outcome.fixed is True
        assert calls[0]["price_decimals"] == 4, "must use the live-looked-up precision, not a generic guess"
    finally:
        sg.saxo_order.place_protective_stop = orig_place
        sg.saxo_client.get_account_key = orig_key
        sg._live_price_decimals = orig_lookup


_run("a non-forex FxSpot symbol (e.g. futures-module CADMXN) uses the live decimals lookup",
     test_non_forex_fxspot_uses_live_decimals_lookup_not_generic_5dp_guess)
_run("naked position with zero protection gets a stop for the FULL quantity, correct side",
     test_naked_none_protection_gets_full_stop_placed)
_run("partially-protected position only gets a stop for the UNCOVERED remainder",
     test_naked_partial_protection_only_covers_the_uncovered_remainder)
_run("a position already fully covered by fix-time needs no action and counts as fixed",
     test_naked_already_covered_by_the_time_it_runs_needs_no_action)
_run("a rejected order is honestly reported as NOT fixed, never assumed",
     test_naked_fix_reports_not_fixed_when_saxo_rejects_the_order)


# ═══════════════════════════════════════════════════════════════════════
section("Per-asset-class default stop distance")
# ═══════════════════════════════════════════════════════════════════════

def test_default_stop_pct_known_asset_types():
    assert sg._stop_pct("FxSpot") == 0.02
    assert sg._stop_pct("Stock") == 0.08
    assert sg._stop_pct("Etf") == 0.05


def test_default_stop_pct_unknown_asset_type_falls_back_not_crashes():
    assert sg._stop_pct("SomeNewAssetType") == sg._DEFAULT_FALLBACK_PCT


_run("known asset types map to their documented default stop %", test_default_stop_pct_known_asset_types)
_run("an unrecognized asset type falls back to a safe default instead of crashing", test_default_stop_pct_unknown_asset_type_falls_back_not_crashes)


# ═══════════════════════════════════════════════════════════════════════
section("Mismatch fix: aggressive removal of direction-mismatched entries")
# ═══════════════════════════════════════════════════════════════════════

def test_direction_mismatch_entry_removed_and_reported_fixed():
    orig_adapters = dict(hk.ADAPTERS)
    try:
        entries = [hk.LocalPosition("fake", "strat:AAA", 5, "AAA", "Buy", 50000, "FxSpot",
                                    stop_order_id="OLD")]
        adapter = FakeAdapter("fake", entries)
        hk.ADAPTERS.clear()
        hk.ADAPTERS["fake"] = adapter
        snap = make_snapshot(positions=[make_position(5, -200000)])  # live is short, local claims long
        outcomes = sg._fix_mismatches(["fake"], snap)
        assert len(outcomes) == 1
        assert outcomes[0].fixed is True
        assert outcomes[0].action == "removed_wrong_direction_entry"
        assert adapter.removed_keys == ["strat:AAA"]
        assert "OLD" in adapter.cancel_calls
    finally:
        hk.ADAPTERS.clear()
        hk.ADAPTERS.update(orig_adapters)


def test_untracked_live_reported_fixed_but_defers_protection_to_naked_pass():
    orig_adapters = dict(hk.ADAPTERS)
    try:
        entries = [hk.LocalPosition("fake", "strat:BBB", 6, "BBB", "Buy", 39000, "FxSpot")]
        adapter = FakeAdapter("fake", entries)
        hk.ADAPTERS.clear()
        hk.ADAPTERS["fake"] = adapter
        snap = make_snapshot(positions=[make_position(6, 187000)])  # far more live than tracked
        outcomes = sg._fix_mismatches(["fake"], snap)
        assert len(outcomes) == 1
        assert outcomes[0].action == "no_local_entry_to_fix"
        assert "naked-position fix pass" in outcomes[0].detail
    finally:
        hk.ADAPTERS.clear()
        hk.ADAPTERS.update(orig_adapters)


def test_pending_entry_reported_fixed_not_error():
    """2026-08-24: a pending_entry finding (order still Working, not filled
    yet) fell through _fix_mismatches's generic else-branch before this fix
    and got reported as action="error", fixed=False -- i.e. "NOT FIXED" in
    the safeguard email/report, misrepresenting a normal, temporary,
    working-as-intended state as a real failure."""
    orig_adapters = dict(hk.ADAPTERS)
    try:
        entries = [hk.LocalPosition("fake", "strat:XLB", 35414, "XLB", "Buy", 50, "Etf",
                                    stop_order_id="STOP1")]
        adapter = FakeAdapter("fake", entries)
        hk.ADAPTERS.clear()
        hk.ADAPTERS["fake"] = adapter
        orders = [make_order("ENTRY1", 35414, "Buy", 50, 53.54, order_type="Market")]
        snap = make_snapshot(positions=[], orders=orders)  # not filled yet
        outcomes = sg._fix_mismatches(["fake"], snap)
        assert len(outcomes) == 1
        assert outcomes[0].fixed is True, "must not be reported as NOT FIXED -- nothing is actually wrong"
        assert outcomes[0].action == "entry_not_filled_yet"
        assert adapter.removed_keys is None
    finally:
        hk.ADAPTERS.clear()
        hk.ADAPTERS.update(orig_adapters)


def test_ledger_drift_never_auto_resolved_even_by_safeguard():
    orig_adapters = dict(hk.ADAPTERS)
    try:
        entries = [hk.LocalPosition("stocks", "77", 7, "AAA", "Buy", 10, "Stock")]
        adapter = FakeAdapter("stocks", entries, can_auto_remove=False)
        hk.ADAPTERS.clear()
        hk.ADAPTERS["stocks"] = adapter
        snap = make_snapshot(positions=[make_position(7, -500)])  # opposite direction
        outcomes = sg._fix_mismatches(["stocks"], snap)
        assert len(outcomes) == 1
        assert outcomes[0].fixed is False, "a ledger row must never be silently closed, even by safeguard"
        assert adapter.removed_keys is None
    finally:
        hk.ADAPTERS.clear()
        hk.ADAPTERS.update(orig_adapters)


_run("a direction-mismatched local entry gets removed + its stop cancelled, reported fixed",
     test_direction_mismatch_entry_removed_and_reported_fixed)
_run("untracked live exposure has no local entry to remove -- reported fixed but defers protection to the naked pass",
     test_untracked_live_reported_fixed_but_defers_protection_to_naked_pass)
_run("a pending (not-yet-filled) entry is reported fixed, not NOT FIXED/error",
     test_pending_entry_reported_fixed_not_error)
_run("a ledger row (stocks) is NEVER auto-closed, even in safeguard's aggressive mode",
     test_ledger_drift_never_auto_resolved_even_by_safeguard)


# ═══════════════════════════════════════════════════════════════════════
section("run_safeguard(): verification pass catches a fix that didn't actually take")
# ═══════════════════════════════════════════════════════════════════════

def test_verification_catches_a_fix_that_did_not_actually_take():
    """If the post-fix re-check still finds the position naked (e.g. Saxo
    accepted the order but it got cancelled/rejected asynchronously), the
    outcome must flip to NOT fixed rather than trust the initial success
    return value."""
    orig_adapters = dict(hk.ADAPTERS)
    orig_fetch = hk.fetch_live_snapshot
    orig_place = sg.saxo_order.place_protective_stop
    orig_key = sg.saxo_client.get_account_key
    orig_email = sg._send_safeguard_email
    emails = []
    call_n = {"n": 0}
    try:
        hk.ADAPTERS.clear()
        # uic=50 must be a known forex uic so scan_naked_positions's FxSpot
        # fallback classifies the naked position as "forex", matching the
        # ["forex"]-scoped run_safeguard() call below.
        hk.ADAPTERS["forex"] = FakeAdapter(
            "forex", [hk.LocalPosition("forex", "other:XXX", 50, "XXX", "Buy", 1, "FxSpot")])
        sg.saxo_client.get_account_key = lambda: "AKEY"
        sg.saxo_order.place_protective_stop = lambda **kw: "OID"  # claims success

        def fetch_side_effect():
            call_n["n"] += 1
            if call_n["n"] == 1:
                # first fetch: sees the naked position
                return make_snapshot(positions=[make_position(50, -10000, current_price=1.5)])
            # second fetch (post-fix verification): STILL shows no stop --
            # simulates the placed order having failed silently downstream.
            return make_snapshot(positions=[make_position(50, -10000, current_price=1.5)])

        hk.fetch_live_snapshot = fetch_side_effect
        sg._send_safeguard_email = lambda outcomes: emails.append(outcomes) or True

        outcomes = sg.run_safeguard(["forex"])
        naked_outcomes = [o for o in outcomes if o.category == "naked"]
        assert len(naked_outcomes) == 1
        assert naked_outcomes[0].fixed is False, "verification must catch a fix that didn't really take"
        assert "VERIFICATION FAILED" in naked_outcomes[0].detail
        assert len(emails) == 1
    finally:
        hk.ADAPTERS.clear()
        hk.ADAPTERS.update(orig_adapters)
        hk.fetch_live_snapshot = orig_fetch
        sg.saxo_order.place_protective_stop = orig_place
        sg.saxo_client.get_account_key = orig_key
        sg._send_safeguard_email = orig_email


def test_run_safeguard_noop_on_clean_account_sends_no_email():
    orig_adapters = dict(hk.ADAPTERS)
    orig_fetch = hk.fetch_live_snapshot
    orig_email = sg._send_safeguard_email
    emails = []
    try:
        hk.ADAPTERS.clear()
        hk.ADAPTERS["forex"] = FakeAdapter("forex", [])
        hk.fetch_live_snapshot = lambda: make_snapshot()
        sg._send_safeguard_email = lambda outcomes: emails.append(outcomes) or True
        outcomes = sg.run_safeguard(["forex"])
        assert outcomes == []
        assert emails == []
    finally:
        hk.ADAPTERS.clear()
        hk.ADAPTERS.update(orig_adapters)
        hk.fetch_live_snapshot = orig_fetch
        sg._send_safeguard_email = orig_email


_run("verification re-check flips a claimed fix to NOT-fixed if it didn't actually stick",
     test_verification_catches_a_fix_that_did_not_actually_take)
_run("a clean account with nothing to fix sends no email", test_run_safeguard_noop_on_clean_account_sends_no_email)


# ═══════════════════════════════════════════════════════════════════════
section("Wired into every module's live post-run hook")
# ═══════════════════════════════════════════════════════════════════════

def test_forex_runner_calls_run_safeguard():
    import inspect
    import forex.runner as runner
    src = inspect.getsource(runner)
    assert "safeguard.run_safeguard([\"forex\"])" in src


def test_futures_runner_calls_run_safeguard():
    import inspect
    import futures.runner as runner
    src = inspect.getsource(runner)
    assert "safeguard.run_safeguard([\"futures\"])" in src


def test_etf_bot_calls_run_safeguard():
    import inspect
    sys.path.insert(0, os.path.join(BASE_DIR, "saxo_etf_strategy"))
    import run_etf_bot
    src = inspect.getsource(run_etf_bot)
    assert "safeguard.run_safeguard([\"etf\"])" in src


def test_atos_runner_calls_run_safeguard():
    import inspect
    import atos_runner
    src = inspect.getsource(atos_runner)
    assert "safeguard.run_safeguard([\"stocks\"])" in src


_run("forex/runner.py's live post-run hook calls safeguard.run_safeguard", test_forex_runner_calls_run_safeguard)
_run("futures/runner.py's live post-run hook calls safeguard.run_safeguard", test_futures_runner_calls_run_safeguard)
_run("saxo_etf_strategy/run_etf_bot.py's live post-run hook calls safeguard.run_safeguard", test_etf_bot_calls_run_safeguard)
_run("atos_runner.py's post-cycle hook calls safeguard.run_safeguard", test_atos_runner_calls_run_safeguard)


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
