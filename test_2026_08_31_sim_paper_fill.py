"""
Regression test -- 2026-08-31 SIM paper-fill fallback + enhanced venue-down email.

Saxo's SIM order engine had two multi-hour outages in a week
(CouldNotCompleteRequest (90) on every order while reads kept working).

PART 1 -- paper-fill: on SIM, a rejected ENTRY is booked LOCALLY at the live
quote with a PAPER- order id and pos["paper"]=True, then managed entirely
by ATOS's own exit logic. LIVE is never paper-filled.

PART 2 -- the venue-down email now names every blocked/paper-filled
strategy:pair and the real Saxo error; forex/runner writes
data/forex_venue_down.flag and scheduler_watchdog re-fires the scan.
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
        fn()
        _results.append((name, True, None))
    except Exception as e:
        import traceback
        _results.append((name, False, f"{type(e).__name__}: {e}\n{traceback.format_exc()}"))


def section(t):
    print(f"\n{BOLD}{CYAN}{'-'*70}{RESET}\n{BOLD}{CYAN}  {t}{RESET}\n{BOLD}{CYAN}{'-'*70}{RESET}")


import forex.runner as r
import forex.notifier as fx_notify

# never send a real email from this test
fx_notify._send = lambda *a, **k: True


# ═══════════════════════════════════════════════════════════════════════
section("1. paper-fill is SIM-only, LIVE is hard-off")
# ═══════════════════════════════════════════════════════════════════════

def test_paper_fill_gating():
    old = r.ACCOUNT_ENV
    try:
        r.set_account_env("sim");      assert r._sim_paper_fill_enabled() is True
        r.set_account_env("live");     assert r._sim_paper_fill_enabled() is False
        r.set_account_env("live_eur"); assert r._sim_paper_fill_enabled() is False
    finally:
        r.set_account_env(old if old in ("sim", "live", "live_eur") else "sim")
_run("_sim_paper_fill_enabled(): True for sim, False for both LIVE accounts", test_paper_fill_gating)


def test_is_paper_position():
    assert r._is_paper_position({"paper": True}) is True
    assert r._is_paper_position({}) is False
    assert r._is_paper_position({"paper": False}) is False
_run("_is_paper_position reads the pos['paper'] flag", test_is_paper_position)


# ═══════════════════════════════════════════════════════════════════════
section("2. broker-touching helpers no-op for a paper position")
# ═══════════════════════════════════════════════════════════════════════

def test_breakeven_stop_is_local_only_for_paper():
    src = inspect.getsource(r._apply_breakeven_stop)
    assert "_is_paper_position(pos)" in src
    assert src.index("_is_paper_position(pos)") < src.index("_amend_stop_order"), \
        "the paper check must gate out the broker amend"
_run("_apply_breakeven_stop: paper position -> set local stop_price, return, no broker call",
     test_breakeven_stop_is_local_only_for_paper)


def test_profit_ladder_is_local_only_for_paper():
    src = inspect.getsource(r._apply_profit_ladder_stop)
    assert "_is_paper_position(pos)" in src
    assert src.index("_is_paper_position(pos)") < src.index("_amend_stop_order")
_run("_apply_profit_ladder_stop: paper position -> local ratchet only, no broker call",
     test_profit_ladder_is_local_only_for_paper)


def test_exit_close_has_a_paper_branch():
    src = inspect.getsource(r._run_exits)
    assert "elif _paper:" in src, "the exit close must have a paper branch"
    paper_blk = src[src.index("elif _paper:"): src.index("elif _paper:") + 500]
    assert "_post(" not in paper_blk and "_cancel_order(" not in paper_blk, \
        "the paper close must not touch the broker"
    assert "[PAPER] CLOSE" in paper_blk
_run("_run_exits: paper close is logged locally, never sends/cancels an order",
     test_exit_close_has_a_paper_branch)


def test_entry_rejection_paper_fills_on_sim():
    src = inspect.getsource(r._run_entries)
    assert "_sim_paper_fill_enabled()" in src
    blk = src[src.index("if entry_oid is None:"):]
    assert 'entry_oid = "PAPER-"' in blk
    assert 'pos_record["paper"] = True' in inspect.getsource(r._run_entries)
_run("_run_entries: entry rejection on SIM -> PAPER- id + pos_record['paper']=True",
     test_entry_rejection_paper_fills_on_sim)


# ═══════════════════════════════════════════════════════════════════════
section("3. housekeeping skips paper positions")
# ═══════════════════════════════════════════════════════════════════════

def test_housekeeping_forex_adapter_skips_paper():
    import housekeeping
    src = inspect.getsource(housekeeping.ForexAdapter.load)
    assert 'v.get("paper")' in src and "continue" in src, \
        "ForexAdapter.load must skip paper positions so reconcile never flags them as phantom"
_run("housekeeping ForexAdapter.load() skips pos['paper'] positions", test_housekeeping_forex_adapter_skips_paper)


# ═══════════════════════════════════════════════════════════════════════
section("4. circuit breaker: blocked list, flag file, richer email")
# ═══════════════════════════════════════════════════════════════════════

def test_circuit_records_blocked_and_writes_flag():
    old = r.ACCOUNT_ENV
    try:
        r.set_account_env("sim")
        r._reset_order_circuit()
        r._clear_venue_down_flag()
        for i in range(r.CIRCUIT_BREAKER_MAX_CONSECUTIVE_REJECTS):
            r._record_entry_result(rejected=True, saxo_error="CouldNotCompleteRequest (90)")
            r._note_blocked_signal("gap_weekend", f"XAU{i}", "Buy", paper_filled=True)
        assert r._order_circuit_is_open()
        assert os.path.exists(r.VENUE_DOWN_FLAG), "circuit trip must write the retry flag"
        assert len(r._order_circuit["blocked"]) == r.CIRCUIT_BREAKER_MAX_CONSECUTIVE_REJECTS
        assert r._order_circuit["last_saxo_error"] == "CouldNotCompleteRequest (90)"
        # a subsequent success resets the consecutive count but not 'open'
        r._record_entry_result(rejected=False)
        assert r._order_circuit["consecutive_rejects"] == 0
    finally:
        r._reset_order_circuit()
        r._clear_venue_down_flag()
        r.set_account_env(old if old in ("sim", "live", "live_eur") else "sim")
_run("circuit trip: records every blocked signal + Saxo error + writes forex_venue_down.flag",
     test_circuit_records_blocked_and_writes_flag)


def test_clean_run_clears_the_flag():
    src = inspect.getsource(r.run_daily)
    assert "_clear_venue_down_flag()" in src and "_venue_down_email_if_needed()" in src
    # the clear must be on the not-open branch
    assert src.index("if _order_circuit_is_open():") < src.index("_clear_venue_down_flag()")
_run("run_daily: emails once if circuit open, else clears the flag", test_clean_run_clears_the_flag)


def test_venue_down_email_renders_with_blocked_table():
    captured = {}
    fx_notify._send = lambda subject, html: captured.update(subject=subject, html=html) or True
    try:
        fx_notify.send_order_venue_down(
            account_env="sim", consecutive=8, saxo_error="CouldNotCompleteRequest (90)",
            blocked=[("gap_weekend", "XAUCNH", "Buy", True), ("london_breakout", "EURJPY", "Sell", False)],
            paper_fill=True,
        )
        assert "XAUCNH" in captured["html"] and "EURJPY" in captured["html"]
        assert "PAPER-FILLED" in captured["html"]
        assert "CouldNotCompleteRequest (90)" in captured["html"]
        assert "paper-filled" in captured["subject"]
    finally:
        fx_notify._send = lambda *a, **k: True
_run("send_order_venue_down: email lists every blocked pair + the real Saxo error",
     test_venue_down_email_renders_with_blocked_table)


def test_watchdog_has_fast_retry():
    import scheduler_watchdog as w
    src = inspect.getsource(w._venue_down_fast_retry)
    assert "forex_venue_down.flag" in inspect.getsource(w) or "_VENUE_DOWN_FLAG" in src
    assert "ATOS Forex Intraday Scan" in src
    assert "Running" in src, "must not re-fire a task that's already running"
    assert "live" not in src.lower() or "SIM-only" in src, "must never start a LIVE task"
    assert "_venue_down_fast_retry(verbose=args.verbose)" in inspect.getsource(w.main)
_run("scheduler_watchdog._venue_down_fast_retry: re-fires the SIM scan while the flag is fresh",
     test_watchdog_has_fast_retry)


print(f"\n{BOLD}{'='*70}{RESET}")
failed = [(n, e) for n, ok, e in _results if not ok]
for name, ok, err in _results:
    print(f"  [{GREEN}PASS{RESET}]" if ok else f"  [{RED}FAIL{RESET}]", name)
    if err:
        print(f"      {YELLOW}{err}{RESET}")
print(f"{BOLD}{'='*70}{RESET}")
if failed:
    print(f"{RED}{BOLD}  {len(failed)} / {len(_results)} FAILED{RESET}")
    sys.exit(1)
print(f"{GREEN}{BOLD}  ALL {len(_results)} TESTS PASSED{RESET}")
sys.exit(0)
