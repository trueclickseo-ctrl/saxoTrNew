"""
test_2026_09_02_safeguard_snapshot_guards.py
---------------------------------------------
The real-money naked-position safeguards (safeguard_live.py / safeguard_
live_eur.py) used to fire a protective-stop POST off ONE Saxo snapshot and
then escalate "place_protective_stop keeps failing on real money" when a
second, equally-degraded snapshot still looked naked.

2026-09-02: a EURUSD false "naked" did exactly that during a Saxo 429
storm -- the position (on the EUR sub-account) was fully protected by a
working OCO stop the whole time. Root cause: Saxo's pooled
/port/v1/orders/me can transiently come back empty/short, so a position
with a good stop momentarily shows zero stop coverage.

Three guards added (this file tests all three, on both real-money
accounts):

  1. two-snapshot agreement gate -- a uic must look naked in TWO
     snapshots ~3s apart before the safeguard places a real stop
  2. orders-fetch sanity -- open positions + ZERO working orders == a
     degraded fetch, skip the whole fix pass that cycle (and don't let a
     degraded VERIFY snapshot flip a real fix to "VERIFICATION FAILED")
  3. post-place confirmation -- Saxo returns an OrderId on ACCEPTANCE, so
     confirm the stop actually reached the order book before calling it
     fixed; a returned-but-never-live id is cancelled and reported unfixed

Run:  python test_2026_09_02_safeguard_snapshot_guards.py
"""
import inspect
import os
import sys
from unittest.mock import patch

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

GREEN, RED, YELLOW, CYAN, RESET, BOLD = (
    "\033[92m", "\033[91m", "\033[93m", "\033[96m", "\033[0m", "\033[1m"
)
_results = []


def _run(name, fn):
    try:
        fn()
        _results.append((name, True, None))
    except Exception as e:
        import traceback
        _results.append((name, False, f"{type(e).__name__}: {e}\n{traceback.format_exc()}"))


def section(title):
    print(f"\n{BOLD}{CYAN}{'-'*70}{RESET}")
    print(f"{BOLD}{CYAN}  {title}{RESET}")
    print(f"{BOLD}{CYAN}{'-'*70}{RESET}")


def _snap(positions_by_uic=None, orders_by_uic=None):
    import housekeeping as hk
    return hk.LiveSnapshot(positions_by_uic or {}, orders_by_uic or {})


def _pos(uic, amount, price=1.1000, asset_type="FxSpot"):
    return {
        "PositionBase": {"Uic": uic, "Amount": amount, "AssetType": asset_type,
                         "BuySell": "Buy" if amount > 0 else "Sell"},
        "PositionView": {"CurrentPrice": price},
        "NetPositionId": "x__FxSpot",
    }


def _stop(uic=21, amount=1000, side="Sell"):
    return {"Status": "Working", "BuySell": side, "OpenOrderType": "Stop", "Amount": amount}


# both accounts, same behaviour -- parametrise over the module pair
_MODS = [
    ("SEK", "housekeeping_live", "safeguard_live", "scan_naked_positions_live",
     "confirm_naked_live", "_fix_naked_position_live", "run_safeguard_live", "live"),
    ("EUR", "housekeeping_live_eur", "safeguard_live_eur", "scan_naked_positions_live_eur",
     "confirm_naked_live_eur", "_fix_naked_position_live_eur", "run_safeguard_live_eur", "live_eur"),
]


# ═══════════════════════════════════════════════════════════════════════
section("1. orders_snapshot_looks_unreliable() -- the degraded-fetch heuristic")
# ═══════════════════════════════════════════════════════════════════════

def _t_unreliable_true_when_positions_but_no_orders():
    for _, hk_name, *_ in _MODS:
        hk = __import__(hk_name)
        s = _snap(positions_by_uic={21: [_pos(21, 1000)]}, orders_by_uic={})
        assert hk.orders_snapshot_looks_unreliable(s) is True, hk_name
_run("positions present + zero working orders -> looks unreliable (both accounts)",
     _t_unreliable_true_when_positions_but_no_orders)


def _t_unreliable_false_when_some_order_present():
    for _, hk_name, *_ in _MODS:
        hk = __import__(hk_name)
        s = _snap(positions_by_uic={21: [_pos(21, 1000)]}, orders_by_uic={21: [_stop()]})
        assert hk.orders_snapshot_looks_unreliable(s) is False, hk_name
_run("at least one working order somewhere -> NOT unreliable (both accounts)",
     _t_unreliable_false_when_some_order_present)


def _t_unreliable_false_when_no_positions_at_all():
    for _, hk_name, *_ in _MODS:
        hk = __import__(hk_name)
        assert hk.orders_snapshot_looks_unreliable(_snap()) is False, hk_name
_run("no open positions -> not 'unreliable' (nothing to protect)",
     _t_unreliable_false_when_no_positions_at_all)


# ═══════════════════════════════════════════════════════════════════════
section("2. confirm_naked_live[_eur]() -- two-snapshot agreement gate")
# ═══════════════════════════════════════════════════════════════════════

def _naked_obj(hk, symbol="EURUSD", uic=21):
    cls = getattr(hk, "NakedPositionLive", None) or getattr(hk, "NakedPositionLiveEur")
    return cls(symbol=symbol, uic=uic, direction="Buy", quantity=1000,
              protection="none", current_price=1.1, stop_coverage=0, uncovered_qty=1000)


def _t_confirm_skips_when_first_snapshot_degraded():
    for _, hk_name, _sg, _scan, confirm_name, *_ in _MODS:
        hk = __import__(hk_name)
        n = _naked_obj(hk)
        degraded = _snap(positions_by_uic={21: [_pos(21, 1000)]}, orders_by_uic={})
        with patch.object(hk.time, "sleep"):
            out = getattr(hk, confirm_name)(degraded, [n])
        assert out == [], f"{hk_name}: must not act on a degraded first snapshot"
_run("first snapshot degraded -> confirm returns [] (skip fix pass)",
     _t_confirm_skips_when_first_snapshot_degraded)


def _t_confirm_skips_when_second_snapshot_degraded():
    for _, hk_name, _sg, _scan, confirm_name, *_ in _MODS:
        hk = __import__(hk_name)
        n = _naked_obj(hk)
        good_first = _snap(positions_by_uic={21: [_pos(21, 1000)]}, orders_by_uic={21: [_stop()]})
        degraded_second = _snap(positions_by_uic={21: [_pos(21, 1000)]}, orders_by_uic={})
        with patch.object(hk.time, "sleep"), \
             patch.object(hk, "fetch_live_snapshot", return_value=degraded_second):
            out = getattr(hk, confirm_name)(good_first, [n])
        assert out == [], f"{hk_name}: must not act when the confirming snapshot is degraded"
_run("confirming snapshot degraded -> confirm returns [] (skip fix pass)",
     _t_confirm_skips_when_second_snapshot_degraded)


def _t_confirm_drops_uic_naked_in_only_one_snapshot():
    for _, hk_name, _sg, scan_name, confirm_name, *_ in _MODS:
        hk = __import__(hk_name)
        n = _naked_obj(hk)
        good = _snap(positions_by_uic={21: [_pos(21, 1000)]}, orders_by_uic={21: [_stop()]})
        with patch.object(hk.time, "sleep"), \
             patch.object(hk, "fetch_live_snapshot", return_value=good), \
             patch.object(hk, scan_name, return_value=[]):   # 2nd scan: clean
            out = getattr(hk, confirm_name)(good, [n])
        assert out == [], f"{hk_name}: a uic naked in only ONE snapshot must be dropped"
_run("uic naked in first snapshot but clean in the confirming one -> dropped, not acted on",
     _t_confirm_drops_uic_naked_in_only_one_snapshot)


def _t_confirm_keeps_uic_naked_in_both_snapshots():
    for _, hk_name, _sg, scan_name, confirm_name, *_ in _MODS:
        hk = __import__(hk_name)
        n = _naked_obj(hk)
        n2 = _naked_obj(hk)
        good = _snap(positions_by_uic={21: [_pos(21, 1000)]}, orders_by_uic={21: [_stop()]})
        with patch.object(hk.time, "sleep"), \
             patch.object(hk, "fetch_live_snapshot", return_value=good), \
             patch.object(hk, scan_name, return_value=[n2]):   # 2nd scan: still naked
            out = getattr(hk, confirm_name)(good, [n])
        assert [x.uic for x in out] == [21], f"{hk_name}: a genuinely naked uic must survive the gate"
        assert out[0] is n2, f"{hk_name}: confirm returns the FRESH snapshot's object"
_run("uic naked in BOTH snapshots -> survives the gate (uses the fresh object)",
     _t_confirm_keeps_uic_naked_in_both_snapshots)


def _t_confirm_empty_input_is_noop():
    for _, hk_name, _sg, _scan, confirm_name, *_ in _MODS:
        hk = __import__(hk_name)
        with patch.object(hk, "fetch_live_snapshot") as fetch:
            assert getattr(hk, confirm_name)(_snap(), []) == []
        fetch.assert_not_called()   # no second fetch when there was nothing naked
_run("no naked positions in -> confirm is a no-op, no extra snapshot fetched",
     _t_confirm_empty_input_is_noop)


# ═══════════════════════════════════════════════════════════════════════
section("3. stop_order_is_working() -- Saxo returns an OrderId on ACCEPTANCE, not on 'live'")
# ═══════════════════════════════════════════════════════════════════════

def _t_stop_confirm_true_when_order_present_and_working():
    for _, hk_name, *_ in _MODS:
        hk = __import__(hk_name)
        with patch.object(hk.time, "sleep"), \
             patch("saxo_client.get_orders",
                   return_value={"Data": [{"OrderId": "abc", "Status": "Working"}]}):
            assert hk.stop_order_is_working("abc") is True, hk_name
_run("stop order present at the broker as Working -> confirmed",
     _t_stop_confirm_true_when_order_present_and_working)


def _t_stop_confirm_false_when_order_absent():
    for _, hk_name, *_ in _MODS:
        hk = __import__(hk_name)
        with patch.object(hk.time, "sleep"), \
             patch("saxo_client.get_orders", return_value={"Data": [{"OrderId": "other"}]}):
            assert hk.stop_order_is_working("abc") is False, hk_name
_run("returned OrderId never appears in the order book -> NOT confirmed",
     _t_stop_confirm_false_when_order_absent)


def _t_stop_confirm_fails_open_on_read_error():
    for _, hk_name, *_ in _MODS:
        hk = __import__(hk_name)
        with patch.object(hk.time, "sleep"), \
             patch("saxo_client.get_orders", side_effect=RuntimeError("429")):
            assert hk.stop_order_is_working("abc") is True, f"{hk_name}: fail OPEN on a flaky read"
_run("the confirm read itself errors -> fail open (don't invent a failure)",
     _t_stop_confirm_fails_open_on_read_error)


def _t_stop_confirm_false_on_empty_id():
    for _, hk_name, *_ in _MODS:
        hk = __import__(hk_name)
        assert hk.stop_order_is_working("") is False
        assert hk.stop_order_is_working(None) is False
_run("empty/None order id -> not working (never happens, but boundary-checked)",
     _t_stop_confirm_false_on_empty_id)


# ═══════════════════════════════════════════════════════════════════════
section("4. _fix_naked_position_live[_eur]() -- post-place confirmation")
# ═══════════════════════════════════════════════════════════════════════

def _t_fix_reports_unfixed_when_stop_never_goes_live():
    import saxo_client
    for _, hk_name, sg_name, _scan, _c, fix_name, _r, env in _MODS:
        hk = __import__(hk_name)
        sg = __import__(sg_name)
        n = _naked_obj(hk)
        cancels = []
        with patch.object(saxo_client, "get_account_key", return_value="k"), \
             patch("forex.runner.set_account_env"), \
             patch("forex.runner.get_price_decimals", return_value=5, create=True), \
             patch("saxo_order.place_protective_stop", return_value="oid-zombie"), \
             patch.object(hk, "stop_order_is_working", return_value=False), \
             patch.object(saxo_client, "cancel_order",
                          side_effect=lambda oid, env=None: cancels.append((oid, env)) or True):
            out = getattr(sg, fix_name)(n)
        assert out.fixed is False, f"{sg_name}: accepted-but-never-live == NOT fixed"
        assert "never became a live order" in out.detail, sg_name
        assert cancels and cancels[0][0] == "oid-zombie", f"{sg_name}: the zombie id must be cancelled"
        assert cancels[0][1] == env, f"{sg_name}: cancel on the right env"
_run("place returns an id but the stop never goes live -> unfixed + zombie cancelled (both accounts)",
     _t_fix_reports_unfixed_when_stop_never_goes_live)


def _t_fix_reports_fixed_when_stop_confirmed_live():
    import saxo_client
    for _, hk_name, sg_name, _scan, _c, fix_name, _r, _env in _MODS:
        hk = __import__(hk_name)
        sg = __import__(sg_name)
        n = _naked_obj(hk)
        with patch.object(saxo_client, "get_account_key", return_value="k"), \
             patch("forex.runner.set_account_env"), \
             patch("forex.runner.get_price_decimals", return_value=5, create=True), \
             patch("saxo_order.place_protective_stop", return_value="oid-good"), \
             patch.object(hk, "stop_order_is_working", return_value=True), \
             patch.object(saxo_client, "cancel_order") as cancel:
            out = getattr(sg, fix_name)(n)
        assert out.fixed is True, sg_name
        cancel.assert_not_called()
_run("place returns an id and the stop is confirmed live -> fixed, nothing cancelled",
     _t_fix_reports_fixed_when_stop_confirmed_live)


# ═══════════════════════════════════════════════════════════════════════
section("5. run_safeguard_live[_eur]() -- a degraded VERIFY snapshot never flips a real fix")
# ═══════════════════════════════════════════════════════════════════════

def _t_degraded_verify_snapshot_does_not_flip_fixed_to_failed():
    for _, hk_name, sg_name, scan_name, confirm_name, fix_name, run_name, _env in _MODS:
        hk = __import__(hk_name)
        sg = __import__(sg_name)
        n = _naked_obj(hk)
        degraded = _snap(positions_by_uic={21: [_pos(21, 1000)]}, orders_by_uic={})
        FixCls = getattr(sg, "FixOutcomeLive" if hk_name == "housekeeping_live" else "FixOutcomeLiveEur")
        reconcile = "reconcile_live_forex" if hk_name == "housekeeping_live" else "reconcile_live_eur_forex"
        with patch.object(hk, "fetch_live_snapshot", return_value=degraded), \
             patch.object(hk, scan_name, return_value=[n]), \
             patch.object(hk, confirm_name, return_value=[n]), \
             patch.object(hk, reconcile, return_value=[]), \
             patch.object(sg, fix_name,
                          return_value=FixCls(n.symbol, "place_protective_stop", True, "placed stop", uic=21)), \
             patch.object(sg, "_send_safeguard_email_live" if hk_name == "housekeeping_live"
                          else "_send_safeguard_email_live_eur"), \
             patch("attention.flush"), patch("attention.raise_attention"), patch("attention.clear_attention"):
            outcomes = getattr(sg, run_name)()
        assert len(outcomes) == 1, sg_name
        assert outcomes[0].fixed is True, (
            f"{sg_name}: a degraded verify snapshot must NOT flip a confirmed fix to "
            f"'VERIFICATION FAILED' -- that was the false-page bug")
        assert "VERIFICATION FAILED" not in outcomes[0].detail, sg_name
_run("degraded verify snapshot -> a confirmed fix stays FIXED (no false 'VERIFICATION FAILED')",
     _t_degraded_verify_snapshot_does_not_flip_fixed_to_failed)


# ═══════════════════════════════════════════════════════════════════════
section("6. Wiring -- the safeguards actually call the gate before acting")
# ═══════════════════════════════════════════════════════════════════════

def _t_safeguards_call_confirm_before_fix():
    for _, _hk, sg_name, _scan, confirm_name, fix_name, run_name, _env in _MODS:
        src = inspect.getsource(getattr(__import__(sg_name), run_name))
        assert confirm_name in src, f"{sg_name}: must route naked findings through {confirm_name}"
        i_confirm = src.index(confirm_name)
        i_fix = src.index(fix_name + "(")
        assert i_confirm < i_fix, f"{sg_name}: the agreement gate must run BEFORE the fix"
_run("run_safeguard_live[_eur] calls confirm_naked_live[_eur] before _fix_naked_position_live[_eur]",
     _t_safeguards_call_confirm_before_fix)


def _t_each_account_has_its_own_copy_of_the_guards():
    # per the "each real-money account gets its own independent safety net"
    # rule -- the EUR file must not import the guards from the SEK file
    src = inspect.getsource(__import__("housekeeping_live_eur"))
    assert "def orders_snapshot_looks_unreliable" in src
    assert "def confirm_naked_live_eur" in src
    assert "def stop_order_is_working" in src
    assert "import housekeeping_live" not in src and "from housekeeping_live " not in src
_run("housekeeping_live_eur has its OWN copy of all three guards (never imports the SEK file's)",
     _t_each_account_has_its_own_copy_of_the_guards)


# ── summary ───────────────────────────────────────────────────────────
print(f"\n{BOLD}{'='*70}{RESET}")
_failed = [(n, e) for n, ok, e in _results if not ok]
for n, ok, e in _results:
    print(f"  [{GREEN}PASS{RESET}]" if ok else f"  [{RED}FAIL{RESET}]", n)
    if e:
        print(f"      {YELLOW}{e}{RESET}")
print(f"{BOLD}{'='*70}{RESET}")
if _failed:
    print(f"{RED}{BOLD}  {len(_failed)} / {len(_results)} FAILED{RESET}")
    sys.exit(1)
print(f"{GREEN}{BOLD}  ALL {len(_results)} TESTS PASSED{RESET}")
sys.exit(0)
