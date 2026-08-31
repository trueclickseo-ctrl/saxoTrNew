"""
Regression test — 2026-08-31 order-venue circuit breaker.

Incident: Saxo SIM rejected ~every order all day (`CouldNotCompleteRequest
(90)`) — ~4,000 rejections, 0 fills, 769 stranded working orders by noon.
Each rejected entry costs ~4 API calls + rate-limit backoff, so a full
scan ran 60–90 min instead of ~13, overran its Task Scheduler window, and
piled up orphan orders.

Fix (`forex/runner.py`): `_record_entry_result()` counts CONSECUTIVE entry
rejections across one run; after `CIRCUIT_BREAKER_MAX_CONSECUTIVE_REJECTS`
the breaker opens and `_run_entries()` returns immediately for every
remaining strategy. Exits / stop-loss healing are untouched. `run_daily()`
calls `_reset_order_circuit()` so state is clean per run.

These tests stub the notifier so NO email is sent.
"""

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


def section(title):
    print(f"\n{BOLD}{CYAN}{'-'*70}{RESET}")
    print(f"{BOLD}{CYAN}  {title}{RESET}")
    print(f"{BOLD}{CYAN}{'-'*70}{RESET}")


import forex.runner as r
import forex.notifier as fx_notify

# Hard stub: never send a real email from a test run, and count calls.
_email_calls = []
fx_notify.send_order_venue_down = lambda **kw: _email_calls.append(kw)
r.fx_notify.send_order_venue_down = fx_notify.send_order_venue_down

N = r.CIRCUIT_BREAKER_MAX_CONSECUTIVE_REJECTS


# ═══════════════════════════════════════════════════════════════════════
section("1. Breaker opens on N consecutive rejections, not before")
# ═══════════════════════════════════════════════════════════════════════

def test_opens_exactly_at_threshold():
    r._reset_order_circuit()
    for _ in range(N - 1):
        r._record_entry_result(rejected=True)
    assert not r._order_circuit_is_open(), f"must not open before {N} rejections"
    r._record_entry_result(rejected=True)
    assert r._order_circuit_is_open(), f"must open on the {N}th consecutive rejection"
_run(f"opens on exactly {N} consecutive rejections", test_opens_exactly_at_threshold)


def test_success_resets_the_count():
    r._reset_order_circuit()
    for _ in range(N - 1):
        r._record_entry_result(rejected=True)
    r._record_entry_result(rejected=False)          # a fill resets the streak
    for _ in range(N - 1):
        r._record_entry_result(rejected=True)
    assert not r._order_circuit_is_open(), "a successful entry must reset the consecutive count"
    r._record_entry_result(rejected=True)
    assert r._order_circuit_is_open(), "…and then N more in a row still trips it"
_run("a successful entry resets the consecutive-rejection count", test_success_resets_the_count)


def test_reset_clears_open_state():
    r._reset_order_circuit()
    for _ in range(N):
        r._record_entry_result(rejected=True)
    assert r._order_circuit_is_open()
    r._reset_order_circuit()
    assert not r._order_circuit_is_open(), "_reset_order_circuit must clear the open flag"
    assert r._order_circuit["consecutive_rejects"] == 0
_run("_reset_order_circuit clears open flag + count", test_reset_clears_open_state)


# ═══════════════════════════════════════════════════════════════════════
section("2. Notification fires once per run, and never raises")
# ═══════════════════════════════════════════════════════════════════════

def test_notifies_once_per_run():
    _email_calls.clear()
    r._reset_order_circuit()
    for _ in range(N + 5):   # keep rejecting after it's already open
        r._record_entry_result(rejected=True)
    # the email is emitted once, at end of run, by _venue_down_email_if_needed
    r._venue_down_email_if_needed()
    r._venue_down_email_if_needed()   # idempotent -- 'notified' guard
    assert len(_email_calls) == 1, f"expected exactly one venue-down email, got {len(_email_calls)}"
    assert _email_calls[0].get("consecutive") == N + 5
    r._reset_order_circuit()
_run("venue-down email is sent exactly once per run", test_notifies_once_per_run)


def test_notifier_exception_does_not_propagate():
    def _boom(**kw):
        raise RuntimeError("smtp exploded")
    old = r.fx_notify.send_order_venue_down
    r.fx_notify.send_order_venue_down = _boom
    try:
        r._reset_order_circuit()
        for _ in range(N):
            r._record_entry_result(rejected=True)
        r._venue_down_email_if_needed()   # must not raise even if the email boom-s
        assert r._order_circuit_is_open(), "breaker still opens even if the email fails"
    finally:
        r.fx_notify.send_order_venue_down = old
_run("a failing notifier never propagates out of the venue-down emitter", test_notifier_exception_does_not_propagate)


# ═══════════════════════════════════════════════════════════════════════
section("3. _run_entries early-returns 0 while the breaker is open (real fn)")
# ═══════════════════════════════════════════════════════════════════════

def test_run_entries_bails_when_open_and_no_paper_fill():
    # With SIM paper-fill ON (the default), an open breaker does NOT stop
    # entries -- signals are still generated and booked locally. The bail
    # only applies when paper-fill is off (LIVE, or SIM with the flag off).
    old_env, old_flag = r.ACCOUNT_ENV, r.SIM_PAPER_FILL_ON_REJECT
    try:
        r.SIM_PAPER_FILL_ON_REJECT = False
        r.set_account_env("sim")
        r._reset_order_circuit()
        for _ in range(N):
            r._record_entry_result(rejected=True)
        assert r._order_circuit_is_open()
        out = r._run_entries("rsi", None, {}, {}, 1000.0, "acct", dry_run=False, today_str="2026-08-31")
        assert out == 0, f"expected 0 entries while breaker open + no paper-fill, got {out!r}"
    finally:
        r.SIM_PAPER_FILL_ON_REJECT = old_flag
        r.set_account_env(old_env if old_env in ("sim", "live", "live_eur") else "sim")
        r._reset_order_circuit()
_run("_run_entries returns 0 when the breaker is open AND paper-fill is off", test_run_entries_bails_when_open_and_no_paper_fill)


def test_dry_run_ignores_breaker():
    # A dry run never places orders, so the breaker must not gate it — the
    # early-return is guarded on `not dry_run`.
    r._reset_order_circuit()
    for _ in range(N):
        r._record_entry_result(rejected=True)
    assert r._order_circuit_is_open()
    src = open(os.path.join(BASE_DIR, "forex", "runner.py"), encoding="utf-8").read()
    assert "if not dry_run and _order_circuit_is_open() and not _sim_paper_fill_enabled():" in src, (
        "the _run_entries early-return must be guarded on `not dry_run` (and now paper-fill)"
    )
_run("dry-run entries are not gated by the breaker (guard is `not dry_run`)", test_dry_run_ignores_breaker)


# ═══════════════════════════════════════════════════════════════════════
section("4. run_daily resets the breaker; exits path does not touch it")
# ═══════════════════════════════════════════════════════════════════════

def test_run_daily_resets_circuit():
    src = open(os.path.join(BASE_DIR, "forex", "runner.py"), encoding="utf-8").read()
    di = src.find("def run_daily(")
    ei = src.find("def run_exits_only(")
    assert di != -1 and ei != -1
    body = src[di: src.find("\n\n\ndef ", di)]
    assert "_reset_order_circuit()" in body, "run_daily must reset the circuit at the top of every run"
    # the exits-only path has no entry orders — it must NOT reference the recorder
    exits_body = src[ei: di if di > ei else len(src)]
    assert "_record_entry_result" not in exits_body
_run("run_daily calls _reset_order_circuit; run_exits_only doesn't record entries", test_run_daily_resets_circuit)


def test_entry_result_recorded_on_both_paths():
    src = open(os.path.join(BASE_DIR, "forex", "runner.py"), encoding="utf-8").read()
    i = src.find("entry_oid, stop_oid, tp_oid = saxo_order.place_with_stop")
    seg = src[i: i + 3200]
    assert "_record_entry_result(rejected=True" in seg, "rejection must feed the breaker"
    assert "_record_entry_result(rejected=False)" in seg, "a fill must reset the breaker"
    # rejected=True is recorded before the (no-paper-fill) break
    assert seg.find("_record_entry_result(rejected=True") < seg.find("break"), (
        "on a rejection the breaker is fed before the loop breaks"
    )
_run("both a rejection and a fill are reported to the breaker at the order call site",
     test_entry_result_recorded_on_both_paths)


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
print(f"{GREEN}{BOLD}  ALL {len(_results)} TESTS PASSED{RESET}")
sys.exit(0)
