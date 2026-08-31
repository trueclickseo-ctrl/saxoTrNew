"""
Regression test -- 2026-08-31 exit advisor, Stage A (shadow-mode
give-back-risk scorer).

- forex/exit_advisor.py: pure, deterministic score() -> HOLD/TIGHTEN/EXIT.
- forex/runner.py: EXIT_ADVISOR_MODE="shadow"; _run_exits logs
  forward_observation.log_exit_advisor_shadow() per open position per
  cycle and NEVER acts (no "active" path exists).
- forex/forward_observation.py: log_exit_advisor_shadow() appends to
  data/exit_advisor_shadow.jsonl.
"""

import inspect
import os
import sys

import pandas as pd

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


import forex.exit_advisor as ea
import forex.runner as r


def _df(closes, hi=None, lo=None):
    n = len(closes)
    return pd.DataFrame({
        "High":  hi or [c * 1.004 for c in closes],
        "Low":   lo or [c * 0.996 for c in closes],
        "Close": closes,
    })


def _pos(**kw):
    p = {"direction": "Buy", "entry_price": 1.0, "initial_stop_price": 0.985,
         "stop_price": 0.985, "atr_at_entry": 0.01, "entry_date": "2026-08-25",
         "risk_eur_at_entry": 100.0}
    p.update(kw)
    return p


# ═══════════════════════════════════════════════════════════════════════
section("1. score(): the give-back case fires EXIT, healthy trades HOLD")
# ═══════════════════════════════════════════════════════════════════════

def test_underwater_or_flat_returns_none():
    assert ea.score(_pos(), _df([0.99] * 20), "rsi") is None, "not in profit -> None"
    assert ea.score(_pos(), None, "rsi") is None, "no df -> None"
    assert ea.score(_pos(entry_price=0), _df([1.1] * 20), "rsi") is None
_run("score() returns None for a flat/underwater position or missing data", test_underwater_or_flat_returns_none)


def test_big_giveback_recommends_exit():
    # peaked at +1.8R (mfe_eur 180 on risk 100), now back to +0.3R, ATR flat,
    # RSI mid, well into the trade's life
    closes = [1.006] * 18 + [1.0045] * 2   # ~+0.3R now (R=0.015)
    pos = _pos(mfe_eur=180.0)
    out = ea.score(pos, _df(closes), "rsi")
    assert out is not None
    assert out["recommendation"] == "EXIT", out
    assert out["signals"]["giveback_frac"] >= 0.7
_run("a position that gave back most of a >1R peak -> EXIT", test_big_giveback_recommends_exit)


def test_healthy_runner_holds():
    # still near its highs: mfe 90, now +0.85R, small give-back
    closes = [1.0125] * 19 + [1.0128]
    pos = _pos(mfe_eur=95.0)
    out = ea.score(pos, _df(closes), "rsi")
    assert out is not None and out["recommendation"] == "HOLD", out
_run("a position holding near its highs -> HOLD", test_healthy_runner_holds)


def test_score_is_bounded_and_deterministic():
    pos = _pos(mfe_eur=200.0)
    d = _df([1.003] * 20)
    a = ea.score(pos, d, "rsi")
    b = ea.score(dict(pos), d, "rsi")
    assert a == b, "pure function -> identical output for identical input"
    assert 0.0 <= a["score"] <= 100.0
_run("score is bounded [0,100] and deterministic", test_score_is_bounded_and_deterministic)


def test_short_side_symmetric():
    # short entered at 1.0, stop 1.015 -> R=0.015; peaked deep, now +0.3R back
    pos = _pos(direction="Sell", initial_stop_price=1.015, stop_price=1.015, mfe_eur=170.0)
    out = ea.score(pos, _df([0.9955] * 20), "rsi")
    assert out is not None and out["recommendation"] == "EXIT", out
_run("short positions score symmetrically", test_short_side_symmetric)


# ═══════════════════════════════════════════════════════════════════════
section("2. runner wiring: SHADOW ONLY, never acts")
# ═══════════════════════════════════════════════════════════════════════

def test_mode_is_shadow_and_no_active_path():
    assert r.EXIT_ADVISOR_MODE == "shadow"
    src = inspect.getsource(r._run_exits)
    assert 'EXIT_ADVISOR_MODE == "shadow"' in src
    # isolate the advisor block: from its guard to the should_exit() call
    blk = src[src.index('if EXIT_ADVISOR_MODE == "shadow"'):
              src.index("exit_flag, reason = strat_mod.should_exit")]
    assert "log_exit_advisor_shadow" in blk
    assert 'pos["stop_price"] =' not in blk, "shadow block must not mutate the stop"
    assert "_post(" not in blk and "_amend_stop_order" not in blk and "_replace_stop_order" not in blk, \
        "shadow block must not touch orders"
_run("EXIT_ADVISOR_MODE is 'shadow'; the _run_exits block only logs, never mutates a stop or an order",
     test_mode_is_shadow_and_no_active_path)


def test_log_helper_writes_jsonl(tmp_path=None):
    import forex.forward_observation as fo
    import json, tempfile
    p = os.path.join(BASE_DIR, "data", "_test_exit_advisor_shadow.jsonl")
    old = fo.EXIT_ADVISOR_LOG
    fo.EXIT_ADVISOR_LOG = p
    try:
        if os.path.exists(p):
            os.remove(p)
        fo.log_exit_advisor_shadow(account_env="sim", strategy="rsi", symbol="EURUSD",
                                   card_id="c1", score=72.0, recommendation="EXIT",
                                   r_now=0.4, mfe_r=1.8, signals={"giveback_frac": 0.78},
                                   cur_stop=1.14)
        rec = json.loads(open(p).read().strip())
        assert rec["recommendation"] == "EXIT" and rec["symbol"] == "EURUSD"
        assert rec["signals"]["giveback_frac"] == 0.78
    finally:
        fo.EXIT_ADVISOR_LOG = old
        if os.path.exists(p):
            os.remove(p)
_run("log_exit_advisor_shadow appends a well-formed row", test_log_helper_writes_jsonl)


print(f"\n{BOLD}{'='*70}{RESET}")
passed = sum(1 for _, ok, _ in _results if ok)
failed = [(n, e) for n, ok, e in _results if not ok]
for name, ok, err in _results:
    print(f"  [{GREEN}PASS{RESET}]" if ok else f"  [{RED}FAIL{RESET}]", name)
    if err:
        print(f"         {YELLOW}{err}{RESET}")
print(f"{BOLD}{'='*70}{RESET}")
if failed:
    print(f"{RED}{BOLD}  {len(failed)} / {len(_results)} FAILED{RESET}")
    sys.exit(1)
print(f"{GREEN}{BOLD}  ALL {len(_results)} TESTS PASSED{RESET}")
sys.exit(0)
