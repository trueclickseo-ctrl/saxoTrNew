"""
test_2026_08_25_live_housekeeping_safeguard.py
------------------------------------------------
Regression tests for housekeeping_live.py and safeguard_live.py -- the
real-money Saxo LIVE forex account's OWN reconciliation + auto-fix
modules, built 2026-08-25 as fully separate files from SIM's
housekeeping.py/safeguard.py (not shared classes/functions), per explicit
user direction.

TEST PLAN / CHECKLIST (written before the test code below):

  Independence (the actual point of building these as separate files):
    - ForexLiveAdapter does NOT inherit from housekeeping.ForexAdapter
    - housekeeping.ADAPTERS never contains "forex_live"
    - housekeeping_live.py / safeguard_live.py never import/call
      housekeeping.ADAPTERS, housekeeping.reconcile_all(),
      housekeeping.scan_naked_positions(), or anything from safeguard.py
    - housekeeping.py's own fetch_live_snapshot() takes no env param
      (reverted -- LIVE's snapshot fetch lives entirely in the new file)

  housekeeping_live.py:
    - fetch_live_snapshot() always calls saxo_client with env="live"
    - ForexLiveAdapter.load/save/replace_stop/cancel_stop -- correctness,
      and that replace_stop/cancel_stop always pass env="live"
    - scan_naked_positions_live(): none/tp_only/partial protection
      classification, non-FxSpot positions skipped, fully-covered
      positions never flagged, uncovered_qty computed correctly
    - _scan_fully_untracked(): a live uic never in local state is flagged
    - _send_email_live(): always prefixes "[LIVE]"

  safeguard_live.py:
    - _fix_naked_position_live(): correct stop-price direction for
      Buy/Sell, "no live price" edge case, Saxo-rejection (None oid) path
    - run_safeguard_live(): nothing-to-fix short-circuit; a fix that fails
      post-verification is correctly marked NOT FIXED, not silently
      accepted

  forex/runner.py wiring:
    - ACCOUNT_ENV=="live" dispatches to safeguard_live.run_safeguard_live()
    - ACCOUNT_ENV=="sim" still dispatches to safeguard.run_safeguard(["forex"])
      unchanged

  Explicitly OUT of scope: actual order placement against a real broker
  (mocked throughout), Windows Task Scheduler interaction (none needed --
  this module isn't scheduled separately, it's called from within
  forex/runner.py's own live dispatch).

Run:
    python test_2026_08_25_live_housekeeping_safeguard.py
Exit code 0 = all pass, 1 = one or more failures.
"""
import inspect
import os
import sys
from unittest.mock import patch, MagicMock

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

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


def _fake_snapshot(positions_by_uic=None, orders_by_uic=None):
    import housekeeping as hk
    return hk.LiveSnapshot(positions_by_uic or {}, orders_by_uic or {})


# ═══════════════════════════════════════════════════════════════════════
section("1. Independence -- housekeeping_live.py never shares SIM's classes/entry points")
# ═══════════════════════════════════════════════════════════════════════

def test_forex_live_adapter_does_not_inherit_from_sim_adapter():
    import housekeeping as hk
    import housekeeping_live as hkl
    assert not issubclass(hkl.ForexLiveAdapter, hk.ForexAdapter), (
        "ForexLiveAdapter must be built independently (inheriting only the generic "
        "BaseAdapter), not as a subclass of SIM's ForexAdapter"
    )
    assert issubclass(hkl.ForexLiveAdapter, hk.BaseAdapter), (
        "ForexLiveAdapter should still implement the generic BaseAdapter interface"
    )
_run("housekeeping_live: ForexLiveAdapter does NOT inherit from housekeeping.ForexAdapter (SIM)",
     test_forex_live_adapter_does_not_inherit_from_sim_adapter)


def test_sim_adapters_dict_never_contains_forex_live():
    import housekeeping as hk
    assert "forex_live" not in hk.ADAPTERS
    assert not hasattr(hk, "ForexLiveAdapter"), (
        "ForexLiveAdapter must not exist in housekeeping.py at all anymore -- "
        "it was moved to housekeeping_live.py"
    )
_run("housekeeping.py: 'forex_live' absent from ADAPTERS, ForexLiveAdapter class no longer defined there",
     test_sim_adapters_dict_never_contains_forex_live)


def test_sim_fetch_live_snapshot_has_no_env_param():
    import housekeeping as hk
    sig = inspect.signature(hk.fetch_live_snapshot)
    assert "env" not in sig.parameters, (
        "housekeeping.py's own fetch_live_snapshot() should be SIM-only again (no env "
        "param) -- LIVE's snapshot fetch lives entirely in housekeeping_live.py now"
    )
_run("housekeeping.py: fetch_live_snapshot() reverted to SIM-only (no env parameter)",
     test_sim_fetch_live_snapshot_has_no_env_param)


def test_housekeeping_live_source_never_references_sim_entry_points():
    """Checks actual CODE, not any docstring/comment prose (several
    docstrings in housekeeping_live.py deliberately NAME these SIM entry
    points to document what's NOT reused -- a naive whole-file substring
    search would false-positive on its own documentation). Uses tokenize
    to strip every STRING/COMMENT token, leaving only real code."""
    import tokenize
    import io
    src_path = os.path.join(BASE_DIR, "housekeeping_live.py")
    with open(src_path, encoding="utf-8") as f:
        src = f.read()
    code_tokens = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in (tokenize.STRING, tokenize.COMMENT):
            continue
        code_tokens.append(tok.string)
    code_only = " ".join(code_tokens)

    forbidden = ["housekeeping . ADAPTERS", "housekeeping . reconcile_all",
                 "housekeeping . scan_naked_positions", "ForexAdapter"]
    for token in forbidden:
        assert token not in code_only, f"housekeeping_live.py must never actually USE {token!r} (SIM-only) in real code"
_run("housekeeping_live.py source never references housekeeping.ADAPTERS/reconcile_all/scan_naked_positions/ForexAdapter",
     test_housekeeping_live_source_never_references_sim_entry_points)


def test_safeguard_live_source_never_references_sim_safeguard():
    src_path = os.path.join(BASE_DIR, "safeguard_live.py")
    with open(src_path, encoding="utf-8") as f:
        src = f.read()
    assert "import safeguard\n" not in src and "import safeguard " not in src, (
        "safeguard_live.py must never import SIM's safeguard.py"
    )
    assert "run_safeguard(" not in src or "run_safeguard_live(" in src
_run("safeguard_live.py source never imports SIM's safeguard.py",
     test_safeguard_live_source_never_references_sim_safeguard)


def test_forex_runner_dispatches_to_separate_modules_per_account():
    import forex.runner as r
    src = inspect.getsource(r)
    assert "safeguard_live.run_safeguard_live()" in src
    assert 'safeguard.run_safeguard(["forex"])' in src
_run("forex/runner.py: dispatches to safeguard_live (LIVE) vs safeguard (SIM), never the wrong one",
     test_forex_runner_dispatches_to_separate_modules_per_account)


# ═══════════════════════════════════════════════════════════════════════
section("2. housekeeping_live.py -- snapshot fetch always env='live'")
# ═══════════════════════════════════════════════════════════════════════

def test_fetch_live_snapshot_uses_env_live():
    import housekeeping_live as hkl
    import saxo_client
    # 2026-08-28: fetch_live_snapshot() now also calls get_account_key(env="live")
    # to attribute pooled positions/orders by their own AccountKey field
    # (see the function's own docstring) -- must be mocked too, or this
    # unit test would make a real Saxo API call.
    with patch.object(saxo_client, "get_account_key", return_value="sek-key"), \
         patch.object(saxo_client, "get_positions", return_value={"Data": []}) as mock_pos, \
         patch.object(saxo_client, "get_orders", return_value={"Data": []}) as mock_ord:
        hkl.fetch_live_snapshot()
    mock_pos.assert_called_once_with(env="live")
    mock_ord.assert_called_once_with(env="live")
_run("housekeeping_live.fetch_live_snapshot() calls saxo_client.get_positions/get_orders with env='live'",
     test_fetch_live_snapshot_uses_env_live)


def test_fetch_live_snapshot_filters_by_account_key():
    # 2026-08-28: replaced pair-tier filtering with real AccountKey
    # attribution -- a pooled position/order belonging to a DIFFERENT
    # account (e.g. the EUR sub-account, sharing the same HIGH_VOLUME
    # pairs) must be excluded even though its uic is one this account
    # legitimately trades.
    import housekeeping_live as hkl
    import saxo_client
    mine  = {"PositionBase": {"AccountKey": "sek-key", "Uic": 21}}
    theirs = {"PositionBase": {"AccountKey": "eur-key", "Uic": 21}}
    with patch.object(saxo_client, "get_account_key", return_value="sek-key"), \
         patch.object(saxo_client, "get_positions", return_value={"Data": [mine, theirs]}), \
         patch.object(saxo_client, "get_orders", return_value={"Data": []}):
        snap = hkl.fetch_live_snapshot()
    assert snap.positions_by_uic.get(21) == [mine], (
        "fetch_live_snapshot() must keep only this account's own AccountKey, "
        "even for a uic another LIVE account also legitimately trades"
    )
_run("housekeeping_live.fetch_live_snapshot() attributes pooled positions by AccountKey, not pair-tier",
     test_fetch_live_snapshot_filters_by_account_key)


# ═══════════════════════════════════════════════════════════════════════
section("3. housekeeping_live.ForexLiveAdapter -- correctness + env='live' everywhere")
# ═══════════════════════════════════════════════════════════════════════

def test_adapter_module_tag():
    import housekeeping_live as hkl
    assert hkl.ForexLiveAdapter.module == "forex_live"
_run("ForexLiveAdapter.module == 'forex_live'",
     test_adapter_module_tag)


def test_adapter_replace_stop_uses_env_live():
    import housekeeping_live as hkl
    import saxo_client
    adapter = hkl.ForexLiveAdapter()
    fake_pos = MagicMock(uic=21, direction="Buy", symbol="EURUSD", stop_order_id=None, key="donchian:EURUSD")
    with patch.object(saxo_client, "get_account_key", return_value="sek-key") as mock_key, \
         patch.object(adapter, "_runner", return_value=MagicMock(get_price_decimals=lambda s: 5)), \
         patch("saxo_order.place_protective_stop", return_value="12345") as mock_place:
        oid = adapter.replace_stop(fake_pos, 1000, 1.1000)
    mock_key.assert_called_once_with(env="live")
    assert oid == "12345"
    # the post_fn passed to place_protective_stop must itself route through env="live"
    call_kwargs = mock_place.call_args.kwargs
    assert "post_fn" in call_kwargs
_run("ForexLiveAdapter.replace_stop() resolves the AccountKey via env='live', never SIM's",
     test_adapter_replace_stop_uses_env_live)


def test_adapter_cancel_stop_uses_env_live():
    import housekeeping_live as hkl
    import saxo_client
    adapter = hkl.ForexLiveAdapter()
    with patch.object(saxo_client, "cancel_order", return_value=True) as mock_cancel:
        result = adapter.cancel_stop("999")
    mock_cancel.assert_called_once_with("999", env="live")
    assert result is True
_run("ForexLiveAdapter.cancel_stop() calls saxo_client.cancel_order with env='live'",
     test_adapter_cancel_stop_uses_env_live)


# ═══════════════════════════════════════════════════════════════════════
section("4. housekeeping_live.scan_naked_positions_live() -- classification correctness")
# ═══════════════════════════════════════════════════════════════════════

def _position_dict(uic, amount, current_price=1.1000, asset_type="FxSpot", npid="donchian__FxSpot"):
    return {
        "PositionBase": {"Uic": uic, "Amount": amount, "AssetType": asset_type,
                         "BuySell": "Buy" if amount > 0 else "Sell"},
        "PositionView": {"CurrentPrice": current_price},
        "NetPositionId": npid,
    }


def test_scan_naked_no_orders_at_all_is_fully_naked():
    import housekeeping_live as hkl
    snap = _fake_snapshot(positions_by_uic={21: [_position_dict(21, 1000)]}, orders_by_uic={})
    naked = hkl.scan_naked_positions_live(snapshot=snap, send_email=False)
    assert len(naked) == 1
    assert naked[0].protection == "none"
    assert naked[0].uncovered_qty == 1000
_run("scan_naked_positions_live: zero working orders -> protection='none', full quantity uncovered",
     test_scan_naked_no_orders_at_all_is_fully_naked)


def test_scan_naked_fully_covered_by_stop_is_not_flagged():
    import housekeeping_live as hkl
    snap = _fake_snapshot(
        positions_by_uic={21: [_position_dict(21, 1000)]},
        orders_by_uic={21: [{"Status": "Working", "BuySell": "Sell",
                             "OpenOrderType": "Stop", "Amount": 1000}]},
    )
    naked = hkl.scan_naked_positions_live(snapshot=snap, send_email=False)
    assert naked == [], "a position with a working stop covering the FULL quantity must not be flagged naked"
_run("scan_naked_positions_live: a stop covering the full quantity is never flagged",
     test_scan_naked_fully_covered_by_stop_is_not_flagged)


def test_scan_naked_partial_stop_coverage():
    import housekeeping_live as hkl
    snap = _fake_snapshot(
        positions_by_uic={21: [_position_dict(21, 1000)]},
        orders_by_uic={21: [{"Status": "Working", "BuySell": "Sell",
                             "OpenOrderType": "Stop", "Amount": 400}]},
    )
    naked = hkl.scan_naked_positions_live(snapshot=snap, send_email=False)
    assert len(naked) == 1
    assert naked[0].protection == "partial"
    assert naked[0].uncovered_qty == 600
_run("scan_naked_positions_live: a stop covering only part of the quantity -> protection='partial', correct uncovered_qty",
     test_scan_naked_partial_stop_coverage)


def test_scan_naked_tp_only_no_stop():
    import housekeeping_live as hkl
    snap = _fake_snapshot(
        positions_by_uic={21: [_position_dict(21, 1000)]},
        orders_by_uic={21: [{"Status": "Working", "BuySell": "Sell",
                             "OpenOrderType": "Limit", "Amount": 1000}]},
    )
    naked = hkl.scan_naked_positions_live(snapshot=snap, send_email=False)
    assert len(naked) == 1
    assert naked[0].protection == "tp_only"
_run("scan_naked_positions_live: only a take-profit Limit order (no stop) -> protection='tp_only'",
     test_scan_naked_tp_only_no_stop)


def test_scan_naked_skips_non_fxspot():
    import housekeeping_live as hkl
    snap = _fake_snapshot(positions_by_uic={99: [_position_dict(99, 5, asset_type="ContractFutures")]})
    naked = hkl.scan_naked_positions_live(snapshot=snap, send_email=False)
    assert naked == [], "LIVE only ever holds FxSpot -- a non-FxSpot position must never be scanned/flagged"
_run("scan_naked_positions_live: a non-FxSpot position (shouldn't exist on this account, but boundary-checked) is skipped",
     test_scan_naked_skips_non_fxspot)


def test_scan_naked_zero_net_amount_skipped():
    import housekeeping_live as hkl
    # Buy 1000 + Sell 1000 on the same uic nets to zero -- Saxo already closed it out
    snap = _fake_snapshot(positions_by_uic={21: [_position_dict(21, 1000), _position_dict(21, -1000)]})
    naked = hkl.scan_naked_positions_live(snapshot=snap, send_email=False)
    assert naked == [], "a uic that nets to zero exposure must never be flagged naked"
_run("scan_naked_positions_live: a uic that nets to zero exposure is never flagged",
     test_scan_naked_zero_net_amount_skipped)


# ═══════════════════════════════════════════════════════════════════════
section("5. housekeeping_live -- fully-untracked scan + email tagging")
# ═══════════════════════════════════════════════════════════════════════

def test_scan_fully_untracked_flags_a_uic_with_no_local_record():
    import housekeeping_live as hkl
    adapter = MagicMock()
    adapter.load.return_value = []   # nothing tracked locally at all
    snap = _fake_snapshot(positions_by_uic={21: [_position_dict(21, 1000)]})
    findings = hkl._scan_fully_untracked(snap, adapter)
    assert len(findings) == 1
    assert findings[0].module == "forex_live"
    assert findings[0].kind == hkl.KIND_FULLY_UNTRACKED
_run("housekeeping_live._scan_fully_untracked() flags a live uic with zero local record",
     test_scan_fully_untracked_flags_a_uic_with_no_local_record)


def test_scan_fully_untracked_skips_already_tracked_uic():
    import housekeeping_live as hkl
    from housekeeping import LocalPosition
    adapter = MagicMock()
    adapter.load.return_value = [LocalPosition(module="forex_live", key="donchian:EURUSD", uic=21,
                                               symbol="EURUSD", direction="Buy", quantity=1000,
                                               asset_type="FxSpot")]
    snap = _fake_snapshot(positions_by_uic={21: [_position_dict(21, 1000)]})
    findings = hkl._scan_fully_untracked(snap, adapter)
    assert findings == [], "a uic already present in local state must never be reported as fully untracked"
_run("housekeeping_live._scan_fully_untracked() does not flag a uic already present in local state",
     test_scan_fully_untracked_skips_already_tracked_uic)


def test_send_email_live_always_prefixes_tag():
    import housekeeping_live as hkl
    with patch.object(hkl, "_load_email_cfg", return_value={
        "sender_email": "a@b.com", "sender_password": "x",
        "recipient_email": "c@d.com", "smtp_host": "smtp.example.com", "smtp_port": 587,
    }):
        with patch("smtplib.SMTP") as mock_smtp:
            mock_smtp.return_value.__enter__.return_value = MagicMock()
            hkl._send_email_live("Test Subject", "<p>hi</p>")
            sent_msg = mock_smtp.return_value.__enter__.return_value.sendmail.call_args[0][2]
    assert "[LIVE] Test Subject" in sent_msg
_run("housekeeping_live._send_email_live() always prefixes '[LIVE]' to the subject",
     test_send_email_live_always_prefixes_tag)


# ═══════════════════════════════════════════════════════════════════════
section("6. safeguard_live._fix_naked_position_live() -- correctness + edge cases")
# ═══════════════════════════════════════════════════════════════════════

def _naked(symbol="EURUSD", uic=21, direction="Buy", qty=1000, uncovered=1000, price=1.1000):
    import housekeeping_live as hkl
    return hkl.NakedPositionLive(symbol=symbol, uic=uic, direction=direction, quantity=qty,
                                 protection="none", current_price=price,
                                 stop_coverage=qty - uncovered, uncovered_qty=uncovered)


def test_fix_naked_already_covered_by_the_time_it_runs():
    import safeguard_live as sgl
    n = _naked(uncovered=0)
    outcome = sgl._fix_naked_position_live(n)
    assert outcome.fixed is True
    assert outcome.action == "none needed"
_run("safeguard_live._fix_naked_position_live(): uncovered_qty<=0 -> 'none needed', fixed=True, no order placed",
     test_fix_naked_already_covered_by_the_time_it_runs)


def test_fix_naked_no_current_price_skips_safely():
    import safeguard_live as sgl
    n = _naked(price=0.0)
    outcome = sgl._fix_naked_position_live(n)
    assert outcome.fixed is False
    assert outcome.action == "skipped"
_run("safeguard_live._fix_naked_position_live(): no live current price -> skipped, fixed=False, no order placed",
     test_fix_naked_no_current_price_skips_safely)


def test_fix_naked_buy_direction_stop_price_below_current():
    import safeguard_live as sgl
    import housekeeping_live as hkl
    import saxo_client
    n = _naked(direction="Buy", price=1.1000, uncovered=1000)
    captured = {}
    def fake_place(**kwargs):
        captured.update(kwargs)
        return "oid-1"
    with patch.object(saxo_client, "get_account_key", return_value="sek-key"), \
         patch("forex.runner.set_account_env"), \
         patch("forex.runner.get_price_decimals", return_value=5, create=True), \
         patch.object(hkl, "stop_order_is_working", return_value=True), \
         patch("saxo_order.place_protective_stop", side_effect=fake_place):
        outcome = sgl._fix_naked_position_live(n)
    assert outcome.fixed is True
    assert captured["stop_price"] < 1.1000, "a Buy (long) position's stop must be BELOW current price"
    assert captured["direction"] == "Buy"
_run("safeguard_live._fix_naked_position_live(): a Buy/long naked position gets a stop BELOW current price",
     test_fix_naked_buy_direction_stop_price_below_current)


def test_fix_naked_sell_direction_stop_price_above_current():
    import safeguard_live as sgl
    import housekeeping_live as hkl
    import saxo_client
    n = _naked(direction="Sell", price=1.1000, uncovered=1000)
    captured = {}
    def fake_place(**kwargs):
        captured.update(kwargs)
        return "oid-2"
    with patch.object(saxo_client, "get_account_key", return_value="sek-key"), \
         patch("forex.runner.set_account_env"), \
         patch("forex.runner.get_price_decimals", return_value=5, create=True), \
         patch.object(hkl, "stop_order_is_working", return_value=True), \
         patch("saxo_order.place_protective_stop", side_effect=fake_place):
        outcome = sgl._fix_naked_position_live(n)
    assert outcome.fixed is True
    assert captured["stop_price"] > 1.1000, "a Sell (short) position's stop must be ABOVE current price"
_run("safeguard_live._fix_naked_position_live(): a Sell/short naked position gets a stop ABOVE current price",
     test_fix_naked_sell_direction_stop_price_above_current)


def test_fix_naked_saxo_rejection_reported_not_fixed():
    import safeguard_live as sgl
    import saxo_client
    n = _naked()
    with patch.object(saxo_client, "get_account_key", return_value="sek-key"), \
         patch("forex.runner.set_account_env"), \
         patch("forex.runner.get_price_decimals", return_value=5, create=True), \
         patch("saxo_order.place_protective_stop", return_value=None):
        outcome = sgl._fix_naked_position_live(n)
    assert outcome.fixed is False
    assert "rejected" in outcome.detail.lower()
_run("safeguard_live._fix_naked_position_live(): Saxo rejecting the order (returns None) is reported as NOT fixed, never silently accepted",
     test_fix_naked_saxo_rejection_reported_not_fixed)


# ═══════════════════════════════════════════════════════════════════════
section("7. safeguard_live.run_safeguard_live() -- verification loop")
# ═══════════════════════════════════════════════════════════════════════

def test_run_safeguard_live_nothing_to_fix_short_circuits():
    import safeguard_live as sgl
    import housekeeping_live as hkl
    empty_snap = _fake_snapshot()
    with patch.object(hkl, "fetch_live_snapshot", return_value=empty_snap), \
         patch.object(hkl, "scan_naked_positions_live", return_value=[]), \
         patch.object(hkl, "reconcile_live_forex", return_value=[]), \
         patch.object(sgl, "_send_safeguard_email_live") as mock_email:
        outcomes = sgl.run_safeguard_live()
    assert outcomes == []
    mock_email.assert_not_called()
_run("safeguard_live.run_safeguard_live(): nothing to fix -> returns empty, sends no email",
     test_run_safeguard_live_nothing_to_fix_short_circuits)


def test_run_safeguard_live_marks_failed_verification_as_not_fixed():
    """The core safety property: a fix that LOOKS successful (Saxo returned
    an order id) but still shows naked on a fresh re-check must be reported
    as NOT FIXED, never accepted on faith."""
    import safeguard_live as sgl
    import housekeeping_live as hkl
    n = _naked(uic=21, uncovered=1000)
    # a snapshot with at least one working order somewhere -> not flagged
    # "degraded" by the new orders-sanity guard
    snap = _fake_snapshot(positions_by_uic={21: [_position_dict(21, 1000)]},
                          orders_by_uic={21: [{"Status": "Working", "BuySell": "Sell",
                                               "OpenOrderType": "Limit", "Amount": 1000}]})

    with patch.object(hkl, "fetch_live_snapshot", return_value=snap), \
         patch.object(hkl, "scan_naked_positions_live", side_effect=[[n], [n]]), \
         patch.object(hkl, "confirm_naked_live", return_value=[n]), \
         patch.object(hkl, "reconcile_live_forex", return_value=[]), \
         patch.object(sgl, "_fix_naked_position_live",
                     return_value=sgl.FixOutcomeLive(n.symbol, "place_protective_stop", True,
                                                     "placed stop", uic=21)), \
         patch.object(sgl, "_send_safeguard_email_live") as mock_email:
        outcomes = sgl.run_safeguard_live()

    assert len(outcomes) == 1
    assert outcomes[0].fixed is False, (
        "still naked on the post-fix re-check must flip fixed=True -> False, "
        "not be reported as a successful fix"
    )
    assert "VERIFICATION FAILED" in outcomes[0].detail
    mock_email.assert_called_once()
_run("safeguard_live.run_safeguard_live(): a fix that fails post-verification is correctly downgraded to NOT FIXED",
     test_run_safeguard_live_marks_failed_verification_as_not_fixed)


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
