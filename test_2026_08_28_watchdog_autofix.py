"""
Regression test -- 2026-08-28 scheduler_watchdog.py auto-fix-and-confirm.

Real incident: the SIM "ATOS Forex Intraday Scan" task went stale a SECOND
time the same day after being manually restarted once already -- Task
Scheduler's trigger simply stopped re-arming it, with zero error logged
anywhere. Explicit user request: "if it fails the watchdog checks and
report to safeguard and safeguard fix this error and send an confirmation
email. it should not stop."

Added: AUTO_FIX_ELIGIBLE (a hard, name-based "LIVE" exclusion of
INTRADAY_REPEATING_TASKS -- never auto-restarts a real-money task, since
that is the same "trigger the scheduled task instead of me placing the
order" pattern this session has repeatedly refused for LIVE),
_attempt_auto_fix() (Start-ScheduledTask + re-query to CONFIRM Running,
never trust a silent "success"), and _send_autofix_confirmation() (a
third, distinct email flavor alongside the existing failure alert and
heartbeat).

Uses source-inspection for the safety-boundary checks (no live Task
Scheduler calls in a test) plus a live import for the set-membership math,
which is safe/side-effect-free (building AUTO_FIX_ELIGIBLE doesn't touch
Windows Task Scheduler).
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


def _src():
    with open(os.path.join(BASE_DIR, "scheduler_watchdog.py"), encoding="utf-8") as f:
        return f.read()


# ═══════════════════════════════════════════════════════════════════════
section("1. AUTO_FIX_ELIGIBLE never includes a LIVE-named task")
# ═══════════════════════════════════════════════════════════════════════

def test_no_live_task_in_autofix_set():
    import scheduler_watchdog as w
    live_in_set = [n for n in w.AUTO_FIX_ELIGIBLE if "LIVE" in n]
    assert not live_in_set, f"AUTO_FIX_ELIGIBLE must never contain a LIVE-named task, found: {live_in_set}"
_run("No 'LIVE'-named task is ever in AUTO_FIX_ELIGIBLE",
     test_no_live_task_in_autofix_set)


def test_known_live_tasks_excluded():
    import scheduler_watchdog as w
    for live_task in ("Forex LIVE Daily Run", "Forex LIVE EUR Daily Run", "Saxo LIVE Token Keepalive"):
        assert live_task in w.INTRADAY_REPEATING_TASKS, f"expected {live_task} in INTRADAY_REPEATING_TASKS (test assumption stale)"
        assert live_task not in w.AUTO_FIX_ELIGIBLE, f"{live_task} must NEVER be auto-restartable"
_run("Every known LIVE task is present in INTRADAY_REPEATING_TASKS but absent from AUTO_FIX_ELIGIBLE",
     test_known_live_tasks_excluded)


def test_sim_intraday_scan_is_eligible():
    import scheduler_watchdog as w
    assert "Forex Intraday Scan" in w.AUTO_FIX_ELIGIBLE, (
        "the exact task this feature was built for (SIM Forex Intraday Scan) must be auto-fix eligible"
    )
_run("SIM 'Forex Intraday Scan' (the task that motivated this feature) is auto-fix eligible",
     test_sim_intraday_scan_is_eligible)


def test_autofix_eligible_is_subset_of_intraday_repeating():
    import scheduler_watchdog as w
    assert w.AUTO_FIX_ELIGIBLE.issubset(w.INTRADAY_REPEATING_TASKS), (
        "AUTO_FIX_ELIGIBLE must only ever contain tasks already being watched for staleness"
    )
_run("AUTO_FIX_ELIGIBLE is always a subset of INTRADAY_REPEATING_TASKS",
     test_autofix_eligible_is_subset_of_intraday_repeating)


# ═══════════════════════════════════════════════════════════════════════
section("2. _attempt_auto_fix verifies before claiming success")
# ═══════════════════════════════════════════════════════════════════════

def test_autofix_confirms_running_not_just_command_success():
    src = _src()
    idx = src.find("def _attempt_auto_fix")
    assert idx != -1
    body = src[idx: src.find("\ndef ", idx + 10)]
    assert "_query_task_info(task_name)" in body, (
        "must re-query task state after Start-ScheduledTask, not trust the command's own exit"
    )
    assert '"Running"' in body, "must specifically check for the 'Running' state before declaring success"
_run("_attempt_auto_fix re-queries and requires state=='Running' before returning True",
     test_autofix_confirms_running_not_just_command_success)


def test_check_windows_task_only_autofixes_staleness_not_result_errors():
    src = _src()
    stale_idx = src.find("if stale_msg:")
    autofix_call_idx = src.find("_attempt_auto_fix(task_name)")
    result_check_idx = src.find("if result not in (0, TASK_NEVER_RUN):")
    assert stale_idx != -1 and autofix_call_idx != -1 and result_check_idx != -1
    assert stale_idx < autofix_call_idx < result_check_idx, (
        "auto-fix must only trigger on the staleness path, which is checked and returned "
        "from BEFORE the result-code error path -- never blindly restart a task that reported "
        "a real error code"
    )
_run("Auto-fix only applies to the staleness branch, structurally before the result-code error check",
     test_check_windows_task_only_autofixes_staleness_not_result_errors)


# ═══════════════════════════════════════════════════════════════════════
section("3. A confirmed auto-fix is reported via a distinct email, not silently swallowed")
# ═══════════════════════════════════════════════════════════════════════

def test_distinct_autofix_email_function_exists():
    src = _src()
    assert "_send_autofix_confirmation" in src
_run("_send_autofix_confirmation exists as its own function",
     test_distinct_autofix_email_function_exists)


def test_main_sends_autofix_confirmation_when_any_fixed():
    src = _src()
    main_idx = src.find("def main()")
    body = src[main_idx:]
    assert "auto_fixed" in body and "_send_autofix_confirmation(auto_fixed, label)" in body, (
        "main() must collect auto-fixed tasks and send the confirmation email when non-empty"
    )
_run("main() wires auto_fixed_out through and sends the confirmation email",
     test_main_sends_autofix_confirmation_when_any_fixed)


def test_autofix_email_subject_distinct_from_alert_and_heartbeat():
    src = _src()
    fix_idx = src.find("def _send_autofix_confirmation")
    fix_body = src[fix_idx: src.find("\ndef ", fix_idx + 10)]
    assert "auto-fixed" in fix_body, "subject line should clearly say auto-fixed, distinct from an alert or heartbeat"
_run("Auto-fix confirmation email's subject is distinguishable from the alert/heartbeat emails",
     test_autofix_email_subject_distinct_from_alert_and_heartbeat)


# ═══════════════════════════════════════════════════════════════════════
section("4. A failed auto-fix attempt still escalates to a human (never silently drops it)")
# ═══════════════════════════════════════════════════════════════════════

def test_failed_autofix_still_returns_a_failure_string():
    src = _src()
    idx = src.find("if stale_msg:")
    body = src[idx: src.find("\n    if result not in", idx)]
    assert "AUTO-FIX ATTEMPTED AND DID NOT CONFIRM" in body, (
        "when auto-fix is attempted but not confirmed, the original staleness failure must "
        "still propagate (annotated) rather than being swallowed"
    )
    assert "return stale_msg" in body or "stale_msg = stale_msg +" in body
_run("An unconfirmed auto-fix attempt still returns a (annotated) failure -- escalates to the normal alert email",
     test_failed_autofix_still_returns_a_failure_string)


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
