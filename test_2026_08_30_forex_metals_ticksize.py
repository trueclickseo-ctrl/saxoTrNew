"""
Regression tests -- 2026-08-30 forex metals tick-size rejection.

On the SIM forex account the precious-metal tier (METALS_SYMBOLS) is
traded by `ml` / `advanced_ml` (and any strategy that reaches those
pairs). Three of the pairs -- XAUJPY, XAUTHB, XPTZAR -- quote with
pip_size = 10, which forex.universe.price_decimals() maps to 1 decimal
place, but their real Saxo TickSize is 1.0. So a stop computed as
`close - ATR_STOP_MULT*ATR` and merely rounded to 1dp (e.g.
146892.59249725463 -> 146892.6) is NOT a whole tick, and Saxo rejects
the stop AND the take-profit with:

    400 Bad Request -- "PriceNotInTickSizeIncrements"

leaving a naked position (seen live 2026-08-30: ml:XAUTHB and
advanced_ml:XAUTHB, both stop_order_id = None in data/forex_state.json).

Same bug class as ZC's 0.25 tick in the futures module (2026-08-24).
saxo_order._round_price already accepts a tick_size override; forex just
never passed one. Fix: forex/runner.py fetches the real TickSize from
Saxo's live /ref/v1/instruments/details for metals pairs and rounds
every stop / TP / breakeven-amend price to it before placement.

SIM only -- METALS_SYMBOLS is a SIM-only tier, never traded on
live/live_eur.
"""

import inspect
import os
import sys

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


# ── The exact failing values from data/forex_state.json (2026-08-30) ──────────
_XAUTHB_STOP = 146892.59249725463
_XAUTHB_ENTRY = 151680.5


def test_round_order_price_snaps_metals_to_whole_tick():
    import forex.runner as r
    # Prime the cache so this stays offline / deterministic.
    r._METALS_TICK_CACHE.update({"XAUTHB": 1.0, "XAUJPY": 1.0, "XPTZAR": 1.0})

    rounded = r._round_order_price("XAUTHB", _XAUTHB_STOP)
    assert rounded == 146893.0, f"expected 146893.0, got {rounded}"
    assert rounded % 1.0 == 0.0, "must land on a whole 1.0 tick"

    # The pre-fix behaviour (plain 1dp rounding) produced this rejected value.
    assert round(_XAUTHB_STOP, 1) == 146892.6
    assert round(_XAUTHB_STOP, 1) % 1.0 != 0.0, "sanity: 1dp value is NOT a whole tick"
_run("forex/runner: _round_order_price snaps a metals stop onto its real 1.0 tick",
     test_round_order_price_snaps_metals_to_whole_tick)


def test_round_order_price_unchanged_for_non_metals():
    import forex.runner as r
    from forex.universe import price_decimals
    for sym, price in [("EURUSD", 1.234567891), ("AUDJPY", 95.123456),
                       ("GBPUSD", 1.290014999), ("USDCAD", 1.375550001)]:
        got = r._round_order_price(sym, price)
        want = round(price, price_decimals(sym))
        assert got == want, f"{sym}: non-metals rounding drifted {got} != {want}"
_run("forex/runner: _round_order_price is identical to decimal rounding for non-metals pairs",
     test_round_order_price_unchanged_for_non_metals)


def test_metals_tick_size_returns_none_for_non_metals_without_network():
    import forex.runner as r
    # Non-metals must short-circuit before any HTTP call -- break _get to prove it.
    orig_get = r._get
    r._get = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not hit the network for a non-metals pair"))
    try:
        assert r._metals_tick_size("EURUSD") is None
        assert r._metals_tick_size("USDMXN") is None
    finally:
        r._get = orig_get
_run("forex/runner: _metals_tick_size skips the ref-data call entirely for non-metals pairs",
     test_metals_tick_size_returns_none_for_non_metals_without_network)


def test_metals_tick_size_falls_back_to_none_on_lookup_failure():
    import forex.runner as r
    r._METALS_TICK_CACHE.pop("XAUCHF", None)
    orig_get = r._get
    r._get = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        assert r._metals_tick_size("XAUCHF") is None, "a failed lookup must degrade to None, not raise"
        assert r._round_order_price("XAUCHF", 3494.15089) == round(3494.15089, 2), \
            "with tick unknown, must fall back to decimal-place rounding"
    finally:
        r._get = orig_get
        r._METALS_TICK_CACHE.pop("XAUCHF", None)
_run("forex/runner: a failed TickSize lookup degrades to decimal rounding (never raises / blocks the stop)",
     test_metals_tick_size_falls_back_to_none_on_lookup_failure)


def test_entries_pass_tick_size_into_place_with_stop():
    import forex.runner as r
    src = inspect.getsource(r._run_entries)
    start = src.index("saxo_order.place_with_stop(")
    call = src[start:src.index("if entry_oid is None", start)]
    assert "tick_size" in call and "_metals_tick_size(sym)" in call, \
        "the entry path must pass the metals tick_size into place_with_stop"
_run("forex/runner: _run_entries passes tick_size=_metals_tick_size(sym) into place_with_stop",
     test_entries_pass_tick_size_into_place_with_stop)


def test_heal_paths_use_round_order_price():
    import forex.runner as r
    for fn in (r._heal_missing_stops, r._heal_missing_tp):
        src = inspect.getsource(fn)
        assert "_round_order_price(" in src, f"{fn.__name__} must tick-round via _round_order_price"
        assert "round(stop_price, get_price_decimals" not in src
        assert "round(tp_price, get_price_decimals" not in src
_run("forex/runner: both heal paths round through _round_order_price (not raw decimal rounding)",
     test_heal_paths_use_round_order_price)


def test_breakeven_amend_and_replace_tick_round():
    import forex.runner as r
    for fn in (r._amend_stop_order, r._replace_stop_order):
        src = inspect.getsource(fn)
        assert "_round_order_price(" in src, f"{fn.__name__} must tick-round the amended stop price"
_run("forex/runner: breakeven amend + cancel/replace also tick-round the new stop price",
     test_breakeven_amend_and_replace_tick_round)


def test_place_with_stop_bracket_sends_whole_tick_prices_for_xauthb():
    """End-to-end through saxo_order: a bracket for XAUTHB with tick_size=1.0
    must send whole-tick OrderPrice for BOTH the stop leg and the TP leg."""
    import saxo_order

    captured = {}

    def fake_post(path, body):
        captured["entry"] = body
        legs = body.get("Orders", [])
        return {"OrderId": "ENTRY", "Orders": [{"OrderId": "TP"}, {"OrderId": "STOP"}]}

    entry_oid, stop_oid, tp_oid = saxo_order.place_with_stop(
        post_fn=fake_post, account_key="AKEY", uic=53684, asset_type="FxSpot",
        amount=1, buy_sell="Buy", stop_price=_XAUTHB_STOP, label="ml:XAUTHB",
        take_profit_price=_XAUTHB_ENTRY + 4000.371, symbol="XAUTHB",
        price_decimals=1, tick_size=1.0,
    )
    legs = {leg["OrderType"]: leg["OrderPrice"] for leg in captured["entry"]["Orders"]}
    assert legs["Stop"] == 146893.0, f"stop leg not tick-aligned: {legs['Stop']}"
    assert legs["Stop"] % 1.0 == 0.0
    assert legs["Limit"] % 1.0 == 0.0, f"TP leg not tick-aligned: {legs['Limit']}"
_run("saxo_order: place_with_stop bracket sends whole-tick stop + TP prices when tick_size=1.0 (XAUTHB)",
     test_place_with_stop_bracket_sends_whole_tick_prices_for_xauthb)


def test_live_saxo_tick_sizes_for_metals():
    """Verify against Saxo's live SIM reference data that the three pip_size=10
    metals pairs really do use a 1.0 tick (and a representative 0.01/0.0001
    pair is what it should be). Skipped cleanly when no SIM token is present."""
    try:
        import saxo_auth
        saxo_auth.get_valid_access_token(env="sim")
    except Exception as exc:
        _results.append(("saxo live: metals TickSize matches expectation (SKIPPED -- no SIM token)",
                         True, f"skipped: {type(exc).__name__}"))
        return "skip"

    import forex.runner as r
    r._METALS_TICK_CACHE.clear()
    expected = {"XAUJPY": 1.0, "XAUTHB": 1.0, "XPTZAR": 1.0,
                "XAUEUR": 0.01, "XAGCNH": 0.0001}
    for sym, want in expected.items():
        got = r._metals_tick_size(sym)
        assert got == want, f"{sym}: live Saxo TickSize {got} != expected {want}"


def _run_live(name, fn):
    try:
        out = fn()
        if out == "skip":
            return  # already recorded inside
        _results.append((name, True, None))
    except Exception as e:
        _results.append((name, False, f"{type(e).__name__}: {e}"))


_run_live("saxo live: XAUJPY/XAUTHB/XPTZAR really use a 1.0 tick (ref-data cross-check)",
          test_live_saxo_tick_sizes_for_metals)


print(f"\n{BOLD}{'='*70}{RESET}")
passed = sum(1 for _, ok, _ in _results)
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
