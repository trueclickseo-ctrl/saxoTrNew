"""
Regression test -- 2026-09-01 AI shadow-study heartbeat in the daily email.

The watchdog only emails when ai_shadow_health.check() finds a PROBLEM, so
a healthy AI bot is silent -- the user had no positive "it's up and green"
signal. daily_summary._ai_health_section() adds a GREEN/RED banner + live
metrics (decisions 24h, proposals 24h, LLM ok rate, last-decision age, 7d
verdict mix) to the 23:30 digest.
"""

import ast
import inspect
import os
import sys

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


import daily_summary as ds
import ai_shadow_health as h


def test_section_is_wired_into_the_body_before_the_journal():
    src = inspect.getsource(ds.send_daily_summary)
    assert "_ai_health_section()" in src
    assert src.index("_ai_health_section()") < src.index("_ai_journal_section()")


def test_green_banner_when_check_is_clean():
    o = h.check
    h.check = lambda *a, **k: []
    try:
        html = ds._ai_health_section()
        assert "HEALTHY" in html and "NOT HEALTHY" not in html
        assert "3fb950" in html          # green accent
        assert "Decisions 24h" in html and "Proposals 24h" in html
    finally:
        h.check = o


def test_red_banner_lists_the_problems():
    o = h.check
    h.check = lambda *a, **k: ["agent degraded: 9/10 HOLD", "ANTHROPIC_API_KEY not set"]
    try:
        html = ds._ai_health_section()
        assert "NOT HEALTHY" in html
        assert "agent degraded" in html and "ANTHROPIC_API_KEY not set" in html
        assert "f85149" in html          # red accent
    finally:
        h.check = o


def test_section_never_raises_even_if_health_module_explodes():
    o = h._load_config
    h._load_config = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        assert ds._ai_health_section() == ""     # swallowed
    finally:
        h._load_config = o


def test_reports_study_off_cleanly():
    o = h._study_on
    h._study_on = lambda cfg: False
    try:
        html = ds._ai_health_section()
        assert "Switched off" in html
    finally:
        h._study_on = o


def test_metrics_come_from_the_real_logs_shape():
    # feed a tiny synthetic decisions list through the real helpers
    o_dec = h._load_jsonl
    h._load_jsonl = lambda path: (
        [{"ts": "2999-01-01T00:00:00+00:00", "agent_action": "APPROVE",
          "agent_meta": {"ok": True}},
         {"ts": "2999-01-01T00:05:00+00:00", "agent_action": "HOLD",
          "agent_meta": {"ok": False}}]
        if "decisions" in path else
        [{"ts": "2999-01-01T00:00:00+00:00", "strategy_name": "rsi", "symbol": "X"}]
    )
    o_check = h.check
    h.check = lambda *a, **k: []
    try:
        html = ds._ai_health_section()
        assert "APPROVE 1" in html and "HOLD 1" in html
        assert "1/2" in html             # LLM ok rate
    finally:
        h._load_jsonl = o_dec
        h.check = o_check


def test_module_parses():
    ast.parse(inspect.getsource(ds))


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
