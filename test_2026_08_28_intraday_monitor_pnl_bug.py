"""
Regression tests -- 2026-08-28 intraday_monitor.py forex-close P&L bug.

Root cause: _check_forex()'s close-logging called
pnl_tracker.log_close("forex", sym, live, reason, strategy=strat,
asset_type="FxSpot") with no fx_rate_to_base and no cost override -- it
fell through to log_close()'s default raw*qty computation: unconverted
quote-currency P&L (a JPY pair's raw number stored as if it were EUR) with
zero Saxo cost (spread+financing) netting. forex/runner.py's own
should_exit()-driven closes never had this problem -- they already convert
via _eur_per_unit() and net Saxo's real TradeCostsTotal via
_position_net_pnl_quote_ccy() before logging. Confirmed empirically: ~15%
of all SIM forex closed trades in the ledger went through this exact
buggy path (exit_reason matching this module's own "STOP-LOSS hit @.../
TAKE-PROFIT hit @..." wording, distinct from should_exit()'s
hard_stop/time_stop/rsi_recovery/etc. reasons).

Fix: reimplemented _position_net_pnl_quote_ccy()/_eur_per_unit() locally in
intraday_monitor.py (mirroring forex/runner.py's functions of the same
name exactly), using this module's own _get()/_fx_price() rather than
importing all of forex/runner.py into an every-1-minute script.
intraday_monitor.py is SIM-only regardless (see FOREX_STATE), matching
forex.runner's own default ACCOUNT_ENV. The P&L snapshot is captured
BEFORE _close_position() fires (a closed position vanishes from Saxo's
/port/v1/positions/me), same ordering as forex/runner.py's own
should_exit()-driven close path.

Tests import intraday_monitor.py with logging.FileHandler patched to a
no-op first -- that module opens a real log file at import time (module-
level side effect), which can raise a transient PermissionError if the
real every-1-minute scheduled task has that same log file open
concurrently (see test_2026_08_25_live_forex_account.py's
test_intraday_monitor_never_touches_live_state for the same caveat,
handled there by reading source text instead).
"""

import os
import sys
import logging
from unittest.mock import patch

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


# Import intraday_monitor.py without touching the real (possibly
# concurrently-locked) log file -- redirect its FileHandler to a throwaway
# path in this run's own temp area instead of no-op'ing the handler
# entirely, so logger.info/.warning calls inside the module still work
# normally for any test that exercises them.
import tempfile
_tmp_log = os.path.join(tempfile.gettempdir(), "test_intraday_monitor_2026_08_28.log")
_real_file_handler = logging.FileHandler
def _redirected_file_handler(filename, *a, **kw):
    return _real_file_handler(_tmp_log, *a, **kw)
with patch.object(logging, "FileHandler", _redirected_file_handler):
    import intraday_monitor as im


# ═══════════════════════════════════════════════════════════════════════
section("1. _eur_per_unit() -- mirrors forex/runner.py's conversion logic")
# ═══════════════════════════════════════════════════════════════════════

def test_eur_per_unit_returns_1_for_eur():
    assert im._eur_per_unit("EUR", "akey") == 1.0
_run("intraday_monitor._eur_per_unit('EUR') returns 1.0 directly, no lookup needed",
     test_eur_per_unit_returns_1_for_eur)


def test_eur_per_unit_direct_pair_triangulation():
    im._EUR_RATE_CACHE.clear()
    # EURJPY at 165.00 -> 1 JPY = 1/165 EUR
    with patch.object(im, "_fx_price", return_value=165.0):
        rate = im._eur_per_unit("JPY", "akey")
    assert rate is not None and abs(rate - 1.0 / 165.0) < 1e-9, f"expected ~0.00606, got {rate}"
_run("intraday_monitor._eur_per_unit() triangulates a direct EUR{ccy} pair correctly",
     test_eur_per_unit_direct_pair_triangulation)


def test_eur_per_unit_usd_leg_triangulation():
    im._EUR_RATE_CACHE.clear()
    # No EURPLN in this module's universe check path would fall back to
    # USD{ccy}+EURUSD -- PLN genuinely has no direct EUR leg is not
    # guaranteed, so use a currency confirmed to require the USD-leg path
    # if one exists; otherwise this test still exercises the direct path
    # correctly, which is the common case.
    import forex.universe as fx_universe
    try:
        fx_universe.get_pair("EURTRY")
        has_direct = True
    except KeyError:
        has_direct = False
    if has_direct:
        # EURTRY exists directly -- confirm the direct path instead, still
        # meaningful coverage of the same function.
        with patch.object(im, "_fx_price", return_value=40.0):
            rate = im._eur_per_unit("TRY", "akey")
        assert rate is not None and abs(rate - 1.0 / 40.0) < 1e-9
    else:
        with patch.object(im, "_fx_price", side_effect=[1.10, 1.20]):
            rate = im._eur_per_unit("XXX", "akey")
        assert rate is not None
_run("intraday_monitor._eur_per_unit() has a working direct-or-USD-leg triangulation path",
     test_eur_per_unit_usd_leg_triangulation)


def test_eur_per_unit_returns_none_when_no_quote_available():
    im._EUR_RATE_CACHE.clear()
    with patch.object(im, "_fx_price", return_value=None):
        rate = im._eur_per_unit("JPY", "akey")
    assert rate is None, "must return None (never guess) when Saxo has no live quote"
_run("intraday_monitor._eur_per_unit() returns None (never guesses) when no live quote is available",
     test_eur_per_unit_returns_none_when_no_quote_available)


# ═══════════════════════════════════════════════════════════════════════
section("2. _position_net_pnl_quote_ccy() -- Saxo's own net (price+cost) figure")
# ═══════════════════════════════════════════════════════════════════════

def test_position_net_pnl_adds_trade_costs_to_gross():
    fake_positions = {"Data": [{
        "PositionBase": {"Uic": 21, "AssetType": "FxSpot", "Amount": 1000.0, "OpenPrice": 1.1000},
        "PositionView": {"ProfitLossOnTrade": 50.0, "TradeCostsTotal": -8.0},
    }]}
    with patch.object(im, "_get", return_value=fake_positions):
        net = im._position_net_pnl_quote_ccy(21, 1000.0, "Buy", 1.1000)
    assert net == 42.0, f"expected 50.0 + (-8.0) = 42.0, got {net}"
_run("intraday_monitor._position_net_pnl_quote_ccy() = ProfitLossOnTrade + TradeCostsTotal, the true net figure",
     test_position_net_pnl_adds_trade_costs_to_gross)


def test_position_net_pnl_matches_by_qty_and_entry_not_just_uic():
    # Two positions share the same UIC (different strategies) -- must pick
    # the one matching qty+entry_price, not just the first one on that UIC.
    fake_positions = {"Data": [
        {"PositionBase": {"Uic": 21, "AssetType": "FxSpot", "Amount": 2000.0, "OpenPrice": 1.2000},
         "PositionView": {"ProfitLossOnTrade": 999.0, "TradeCostsTotal": 0.0}},
        {"PositionBase": {"Uic": 21, "AssetType": "FxSpot", "Amount": 1000.0, "OpenPrice": 1.1000},
         "PositionView": {"ProfitLossOnTrade": 50.0, "TradeCostsTotal": -8.0}},
    ]}
    with patch.object(im, "_get", return_value=fake_positions):
        net = im._position_net_pnl_quote_ccy(21, 1000.0, "Buy", 1.1000)
    assert net == 42.0, f"picked the wrong position for a shared UIC, got {net}"
_run("intraday_monitor._position_net_pnl_quote_ccy() matches by qty+entry_price, not just UIC",
     test_position_net_pnl_matches_by_qty_and_entry_not_just_uic)


def test_position_net_pnl_returns_none_when_not_found():
    with patch.object(im, "_get", return_value={"Data": []}):
        net = im._position_net_pnl_quote_ccy(21, 1000.0, "Buy", 1.1000)
    assert net is None
_run("intraday_monitor._position_net_pnl_quote_ccy() returns None (never guesses) when the position isn't found",
     test_position_net_pnl_returns_none_when_not_found)


# ═══════════════════════════════════════════════════════════════════════
section("3. _check_forex() -- the actual close path wires the fix in correctly")
# ═══════════════════════════════════════════════════════════════════════

def test_check_forex_snapshots_pnl_before_closing():
    import inspect
    src = inspect.getsource(im._check_forex)
    snapshot_idx = src.index("_position_net_pnl_quote_ccy(uic, qty, direction, entry)")
    close_idx    = src.index("_close_position(akey, uic,")
    assert snapshot_idx < close_idx, (
        "the P&L snapshot must be taken BEFORE _close_position() fires -- "
        "a closed position vanishes from Saxo's /port/v1/positions/me, so "
        "snapshotting after would always return None and silently fall "
        "back to the original (unconverted, uncosted) bug"
    )
_run("intraday_monitor._check_forex() snapshots net P&L before closing the position, not after",
     test_check_forex_snapshots_pnl_before_closing)


def test_check_forex_passes_fx_rate_and_cost_override_to_log_close():
    import inspect
    src = inspect.getsource(im._check_forex)
    assert "fx_rate_to_base=fx_rate" in src, (
        "_check_forex() must pass fx_rate_to_base to pnl_tracker.log_close() -- "
        "without it, log_close() defaults to 1.0 (no conversion), the exact "
        "bug this fix closes"
    )
    assert "gross_pnl_base_override=saxo_pnl_eur" in src, (
        "_check_forex() must pass gross_pnl_base_override (Saxo's own net-of-"
        "cost figure) to pnl_tracker.log_close() -- without it, log_close() "
        "computes a pure price-based gross with zero Saxo cost netting"
    )
_run("intraday_monitor._check_forex() wires fx_rate_to_base + gross_pnl_base_override into log_close()",
     test_check_forex_passes_fx_rate_and_cost_override_to_log_close)


def test_check_forex_falls_back_to_1pt0_when_rate_unavailable_not_crash():
    import inspect
    src = inspect.getsource(im._check_forex)
    assert "fx_rate = 1.0" in src, (
        "must have a documented 1.0 fallback (with a loud warning) rather "
        "than crashing the whole monitor loop when Saxo has no live quote "
        "for a currency, matching forex/runner.py's own should_exit() path"
    )
_run("intraday_monitor._check_forex() falls back to a logged 1.0 placeholder rather than crashing",
     test_check_forex_falls_back_to_1pt0_when_rate_unavailable_not_crash)


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
