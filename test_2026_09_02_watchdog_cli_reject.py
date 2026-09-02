"""
Regression test -- 2026-09-02 watchdog must catch a scheduled runner that
exits non-zero because it rejected its OWN command line.

INCIDENT: the 2026-09-01 SEK consolidation emptied
LIVE_EUR_ALLOWED_STRATEGIES, but the two EUR .bat files still passed
`--strategy rsi`. `forex/runner.py --account live_eur` then called
`ap.error(...)` -> exit code 2 on every scheduled run for ~2 days. The
LIVE EUR exit check IS in WINDOWS_TASKS, but the watchdog's result-code
path trusts a "fresh" log over a non-zero code -- and run_hidden.vbs kept
appending the argparse reject to the big append-mode scheduler log, whose
mtime stayed perfectly fresh. `_log_content_failure`'s size gate never
fires on a large log. So the watchdog reported "healthy" the whole time.

FIX: `_log_tail_failure()` -- no size gate -- scans the log tail for CLI
-reject / crash signatures. On the non-zero-result-code path a fresh log
is only trusted when the tail is ALSO clean.
"""

import inspect
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

G, R, Y, X, B = "\033[92m", "\033[91m", "\033[93m", "\033[0m", "\033[1m"
_res = []


def _run(n, f):
    try:
        f()
        _res.append((n, True, None))
    except Exception as e:
        import traceback
        _res.append((n, False, f"{type(e).__name__}: {e}\n{traceback.format_exc()}"))


import scheduler_watchdog as w


def _tmp_log(text):
    d = tempfile.mkdtemp()
    p = os.path.join(d, "sched.log")
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    w.DATA_DIR = d
    return "sched.log"


def test_tail_failure_flags_argparse_reject_in_a_large_log():
    big = "2026-09-02 10:00 INFO some earlier healthy run\n" * 400  # >> size gate
    log = _tmp_log(big + "\nusage: runner.py [-h] [--live] [--exits-only]\n"
                         "runner.py: error: --account live_eur only allows [] -- got ['rsi']\n")
    hit = w._log_tail_failure(log)
    assert hit is not None and "error:" in hit, hit


def test_tail_failure_none_on_a_healthy_log():
    log = _tmp_log("banner\n" + "scan line\n" * 500 + "  EXITS-ONLY complete — Closed: 0\n")
    assert w._log_tail_failure(log) is None


def test_tail_failure_still_catches_the_old_crash_signatures():
    log = _tmp_log("x\n" * 500 + "Traceback (most recent call last):\n  ...\nImportError: boom\n")
    assert w._log_tail_failure(log) is not None


def test_cli_reject_signatures_cover_the_incident():
    sigs = w._CLI_REJECT_SIGNATURES
    assert any("runner.py: error:" in s for s in sigs)
    assert any("only allows" in s for s in sigs)
    assert any("usage: runner.py" in s for s in sigs)


def test_result_code_path_consults_tail_failure():
    src = inspect.getsource(w._check_windows_task)
    i = src.index("if result not in (0, TASK_NEVER_RUN):")
    block = src[i:i + 2000]
    assert "_log_tail_failure(" in block, "result-code path must call _log_tail_failure"
    # the early 'return None' must now also require NOT tail_fail
    assert "and not tail_fail" in block


def test_tail_failure_defined_before_use():
    src = inspect.getsource(w)
    assert src.index("def _log_tail_failure") < src.index("tail_fail = _log_tail_failure(")


def test_module_parses():
    import ast
    ast.parse(inspect.getsource(w))


for _n, _f in list(globals().items()):
    if _n.startswith("test_") and callable(_f):
        _run(_n, _f)

print(f"\n{B}{'=' * 66}{X}")
bad = [(n, e) for n, ok, e in _res if not ok]
for n, ok, e in _res:
    print(f"  [{G}PASS{X}]" if ok else f"  [{R}FAIL{X}]", n)
    if e:
        print(f"      {Y}{e}{X}")
print(f"{B}{'=' * 66}{X}")
if bad:
    print(f"{R}{B}  {len(bad)} / {len(_res)} FAILED{X}")
    sys.exit(1)
print(f"{G}{B}  ALL {len(_res)} TESTS PASSED{X}")
sys.exit(0)
