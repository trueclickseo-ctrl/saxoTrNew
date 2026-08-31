"""
Regression test -- 2026-08-31 ai_shadow_health.py

Monitors the AI shadow study's data pipeline (data/ai_shadow_decisions.jsonl
+ data/ai_trade_proposals.jsonl) for the failure the try/except-everywhere
AI layer would otherwise hide: the paid agent call degrading silently to
HOLD (dead API key / spend cap / import break) while shadow rows keep
getting written at the normal rate.

check() is read-only, never raises, and returns [] when the study is off.
Wired into scheduler_watchdog.py's main pass under one dedup key.
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

GREEN, RED, YELLOW, RESET, BOLD = "\033[92m", "\033[91m", "\033[93m", "\033[0m", "\033[1m"
_results = []


def _run(name, fn):
    try:
        fn()
        _results.append((name, True, None))
    except Exception as e:
        import traceback
        _results.append((name, False, f"{type(e).__name__}: {e}\n{traceback.format_exc()}"))


import ai_shadow_health as h

_DEC = os.path.join(BASE_DIR, "data", "_test_ai_health_decisions.jsonl")
_PROP = os.path.join(BASE_DIR, "data", "_test_ai_health_proposals.jsonl")
h.DECISIONS = _DEC
h.PROPOSALS = _PROP

_STUDY_ON = {"agent_enabled": True, "enabled_sim": True, "agent_strategies": ["rsi"]}
_NOW = datetime(2026, 8, 31, 18, 0, tzinfo=timezone.utc)


def _clean():
    for p in (_DEC, _PROP):
        if os.path.exists(p):
            os.remove(p)


def _iso(dt):
    return dt.isoformat()


def _write(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _dec(hrs_ago, ok=True, action="APPROVE", strat="rsi", sym="EURUSD", err=None):
    ts = _NOW - timedelta(hours=hrs_ago)
    return {
        "trade_id": f"sim|{strat}|{sym}|{ts.date().isoformat()}",
        "ts": _iso(ts), "account_env": "sim", "strategy": strat, "symbol": sym,
        "agent_action": action,
        "agent_meta": {"ok": ok, "error": err, "model": "claude-sonnet-5"},
    }


def _prop(hrs_ago, strat="rsi", sym="EURUSD"):
    ts = _NOW - timedelta(hours=hrs_ago)
    return {"ts": _iso(ts), "account_env": "sim", "strategy_name": strat, "symbol": sym}


# ── study off => always silent ─────────────────────────────────────────────
def test_study_off_returns_empty():
    _clean()
    _write(_DEC, [_dec(1, ok=False) for _ in range(20)])
    assert h.check(now=_NOW, cfg={"agent_enabled": False, "enabled_sim": True}) == []
    assert h.check(now=_NOW, cfg={}) == []


# ── degraded rate ─────────────────────────────────────────────────────────
def test_all_ok_is_healthy():
    _clean()
    _write(_DEC, [_dec(i + 1, ok=True) for i in range(15)])
    _write(_PROP, [_prop(i + 1) for i in range(15)])
    assert h.check(now=_NOW, cfg=_STUDY_ON) == []


def test_small_sample_not_judged():
    _clean()
    # 5 decisions, all degraded -- below DEGRADED_MIN_SAMPLE, no degraded-rate alert
    _write(_DEC, [_dec(i + 1, ok=False, action="HOLD") for i in range(5)])
    _write(_PROP, [_prop(i + 1) for i in range(5)])
    probs = h.check(now=_NOW, cfg=_STUDY_ON)
    assert not any("degraded" in p for p in probs), probs


def test_high_degraded_rate_alerts():
    _clean()
    rows = [_dec(i + 1, ok=True) for i in range(6)] + \
           [_dec(i + 1, ok=False, action="HOLD", err="no ANTHROPIC_API_KEY") for i in range(6)]
    _write(_DEC, rows)
    _write(_PROP, [_prop(i + 1) for i in range(12)])
    probs = h.check(now=_NOW, cfg=_STUDY_ON)
    assert any("degraded" in p and "6/12" in p for p in probs), probs
    assert any("no ANTHROPIC_API_KEY" in p for p in probs), probs


def test_old_degraded_outside_window_ignored():
    _clean()
    # all degraded but 10-40 days ago -- outside the 7d window
    _write(_DEC, [_dec(24 * (10 + i), ok=False) for i in range(15)])
    _write(_PROP, [_prop(1)])
    probs = h.check(now=_NOW, cfg=_STUDY_ON)
    assert not any("degraded" in p for p in probs), probs


# ── silent while active ───────────────────────────────────────────────────
def test_proposals_but_no_decisions_alerts():
    _clean()
    _write(_PROP, [_prop(6, sym="EURUSD"), _prop(7, sym="GBPUSD"),
                   _prop(8, sym="AUDUSD"), _prop(9, sym="USDJPY")])
    _write(_DEC, [])
    probs = h.check(now=_NOW, cfg=_STUDY_ON)
    assert any("not running" in p and "shadow decision" in p for p in probs), probs


def test_proposals_recently_evaluated_is_healthy():
    _clean()
    # same 3 signals appear as proposals AND each has a decision (ok) -- fine
    syms = ["EURUSD", "GBPUSD", "AUDUSD"]
    _write(_PROP, [_prop(6, sym=s) for s in syms])
    _write(_DEC, [_dec(6, ok=True, sym=s) for s in syms])
    probs = h.check(now=_NOW, cfg=_STUDY_ON)
    assert not any("not running" in p for p in probs), probs


def test_fresh_proposals_within_grace_ignored():
    _clean()
    # proposals all < SILENT_GRACE_H old -- the same-scan race, don't alarm
    _write(_PROP, [_prop(0.5, sym="EURUSD"), _prop(0.5, sym="GBPUSD"),
                   _prop(0.5, sym="AUDUSD")])
    _write(_DEC, [])
    probs = h.check(now=_NOW, cfg=_STUDY_ON)
    assert not any("not running" in p for p in probs), probs


def test_non_eligible_strategy_proposals_ignored():
    _clean()
    # bb is not in agent_strategies -- proposals for it don't count
    _write(_PROP, [_prop(6, strat="bb", sym="EURUSD"), _prop(7, strat="bb", sym="GBPUSD"),
                   _prop(8, strat="bb", sym="AUDUSD")])
    _write(_DEC, [])
    probs = h.check(now=_NOW, cfg=_STUDY_ON)
    assert not any("not running" in p for p in probs), probs


# ── total silence ─────────────────────────────────────────────────────────
def test_total_silence_alerts():
    _clean()
    _write(_DEC, [_dec(200, ok=True)])
    _write(_PROP, [_prop(200)])
    old = _NOW.timestamp() - 200 * 3600
    os.utime(_DEC, (old, old))
    os.utime(_PROP, (old, old))
    probs = h.check(now=_NOW, cfg=_STUDY_ON)
    assert any("no proposal or decision written" in p for p in probs), probs


def test_no_files_alerts_when_on():
    _clean()
    probs = h.check(now=_NOW, cfg=_STUDY_ON)
    assert any("never produced a row" in p for p in probs), probs


def test_no_files_silent_when_off():
    _clean()
    assert h.check(now=_NOW, cfg={"agent_enabled": False}) == []


# ── robustness ────────────────────────────────────────────────────────────
def test_malformed_jsonl_does_not_raise():
    _clean()
    with open(_DEC, "w", encoding="utf-8") as f:
        f.write("not json\n")
        f.write(json.dumps(_dec(1)) + "\n")
        f.write("{partial\n")
    with open(_PROP, "w", encoding="utf-8") as f:
        f.write("garbage\n")
    probs = h.check(now=_NOW, cfg=_STUDY_ON)  # must not raise
    assert isinstance(probs, list)


def test_check_never_raises_on_bad_config():
    _clean()
    for bad in (None, [], "x", {"agent_enabled": True, "enabled_sim": True,
                                "agent_strategies": "notalist"}):
        try:
            h.check(now=_NOW, cfg=bad if isinstance(bad, dict) else _STUDY_ON)
        except Exception as e:
            raise AssertionError(f"check raised on cfg={bad!r}: {e}")


def test_naive_now_is_accepted():
    _clean()
    _write(_DEC, [_dec(i + 1, ok=True) for i in range(10)])
    _write(_PROP, [_prop(i + 1) for i in range(10)])
    naive = _NOW.replace(tzinfo=None)
    assert isinstance(h.check(now=naive, cfg=_STUDY_ON), list)


# ── watchdog wiring ───────────────────────────────────────────────────────
def test_watchdog_imports_and_references_check():
    import inspect
    import scheduler_watchdog as sw
    src = inspect.getsource(sw.main)
    assert "ai_shadow_health" in src
    assert "AI Shadow Health" in src
    # the AI block must be inside the `if not args.only_forex:` section
    assert src.index("ai_shadow_health") > src.index("if not args.only_forex")


for _n, _f in list(globals().items()):
    if _n.startswith("test_") and callable(_f):
        _run(_n, _f)

_clean()
print(f"\n{BOLD}{'='*66}{RESET}")
failed = [(n, e) for n, ok, e in _results if not ok]
for name, ok, err in _results:
    print(f"  [{GREEN}PASS{RESET}]" if ok else f"  [{RED}FAIL{RESET}]", name)
    if err:
        print(f"      {YELLOW}{err}{RESET}")
print(f"{BOLD}{'='*66}{RESET}")
if failed:
    print(f"{RED}{BOLD}  {len(failed)} / {len(_results)} FAILED{RESET}")
    sys.exit(1)
print(f"{GREEN}{BOLD}  ALL {len(_results)} TESTS PASSED{RESET}")
sys.exit(0)
