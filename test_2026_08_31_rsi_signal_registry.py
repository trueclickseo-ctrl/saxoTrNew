"""
Regression test -- 2026-08-31 RSI signal registry (threshold study).

forex/rsi_signal_registry.py logs every RSI(2) trigger in the study band
(<=15 long / >=85 short) SIM + LIVE, incl. the 11-15 the live threshold
(RSI_OVERSOLD=10) rejects, then forward-resolves each against the daily
bars. Observe-only -- the live entry threshold is NEVER changed.
report_rsi_thresholds.py buckets the resolved rows by expectancy.
"""

import inspect
import os
import sys

import numpy as np
import pandas as pd

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


import forex.rsi_signal_registry as reg
import forex.strategy_rsi as srsi

_TMP = os.path.join(BASE_DIR, "data", "_test_rsi_registry.jsonl")
reg.REGISTRY = _TMP


def _clean():
    for p in (_TMP, _TMP + ".tmp"):
        if os.path.exists(p):
            os.remove(p)


def _uptrend_then_dip(final_rsi_target_low=True):
    """200 bars rising (so close > EMA200), last few bars a sharp dip to
    drive RSI(2) low."""
    n = 210
    base = 1.0 + np.linspace(0, 0.05, n)
    base[-3:] -= 0.010   # sharp 3-bar drop -> RSI(2) collapses, still > EMA200
    return pd.DataFrame({"High": base + 0.0015, "Low": base - 0.0015, "Close": base})


# ═══════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}1. observe(): logs study-band triggers, dedups per bar{RESET}")
# ═══════════════════════════════════════════════════════════════════════

def test_observe_logs_a_low_rsi_dip():
    _clean()
    df = _uptrend_then_dip()
    rsi_now = float(srsi._rsi(df["Close"]).iloc[-1])
    assert rsi_now <= reg.STUDY_MAX_LONG, f"test fixture should trigger the study band, rsi={rsi_now}"
    n = reg.observe("sim", {"EURUSD": df}, fired_syms=set(), taken_syms=set())
    assert n == 1
    rows = reg._load_all()
    assert rows[0]["symbol"] == "EURUSD" and rows[0]["direction"] == "Buy"
    assert rows[0]["rsi2"] <= reg.STUDY_MAX_LONG
    assert rows[0]["resolved"] is None
    assert abs(rows[0]["r_price"] - srsi.ATR_STOP_MULT * rows[0]["atr"]) < 1e-6
_run("observe() logs a study-band RSI dip with rsi2 / stop / 1R price", test_observe_logs_a_low_rsi_dip)


def test_observe_is_idempotent_per_bar():
    _clean()
    df = _uptrend_then_dip()
    reg.observe("sim", {"EURUSD": df})
    n2 = reg.observe("sim", {"EURUSD": df})   # same day, same bar
    assert n2 == 0, "re-running the same scan must not duplicate the row"
    assert len(reg._load_all()) == 1
_run("observe() dedups by (account, symbol, bar_date, direction)", test_observe_is_idempotent_per_bar)


def test_observe_ignores_pairs_outside_the_band():
    _clean()
    flat = pd.DataFrame({"High": [1.0] * 210, "Low": [1.0] * 210, "Close": [1.0] * 210})
    assert reg.observe("sim", {"EURUSD": flat}) == 0
    assert reg._load_all() == []
_run("observe() skips pairs whose RSI(2) isn't in the study band", test_observe_ignores_pairs_outside_the_band)


# ═══════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}2. resolve(): first-touch outcome from the daily bars{RESET}")
# ═══════════════════════════════════════════════════════════════════════

def test_resolve_marks_a_stop_hit():
    _clean()
    df = _uptrend_then_dip()
    reg.observe("sim", {"EURUSD": df})
    row = reg._load_all()[0]
    # a follow-on bar that gaps below the stop
    df2 = pd.concat([df, pd.DataFrame({
        "High": [row["stop"] + 0.001], "Low": [row["stop"] - 0.002], "Close": [row["stop"] - 0.001],
    })], ignore_index=True)
    # backdate the row so days_elapsed > 0
    rows = reg._load_all()
    from datetime import date, timedelta
    rows[0]["bar_date"] = (date.today() - timedelta(days=2)).isoformat()
    reg._rewrite(rows)
    reg.resolve("sim", {"EURUSD": df2})
    res = reg._load_all()[0]["resolved"]
    assert res and res["outcome"] == "stop", res
    assert res["r_multiple"] < 0
_run("resolve() records a stop hit with a negative R", test_resolve_marks_a_stop_hit)


def test_resolve_leaves_still_open_unresolved():
    _clean()
    df = _uptrend_then_dip()
    reg.observe("sim", {"EURUSD": df})
    reg.resolve("sim", {"EURUSD": df})   # same bars, nothing new happened
    assert reg._load_all()[0]["resolved"] is None
_run("resolve() leaves a position that hasn't hit anything as still-open", test_resolve_leaves_still_open_unresolved)


# ═══════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}3. wiring + guarantees{RESET}")
# ═══════════════════════════════════════════════════════════════════════

def test_wired_into_run_entries_observe_only():
    import forex.runner as r
    src = inspect.getsource(r._run_entries)
    assert "rsi_signal_registry.observe(" in src and "rsi_signal_registry.resolve(" in src
    assert 'strat_name == "rsi"' in src
_run("_run_entries calls registry.observe + resolve for the rsi strategy", test_wired_into_run_entries_observe_only)


def test_live_entry_threshold_is_untouched():
    # the whole point: observing a wider band must NOT change what fires live
    assert srsi.RSI_OVERSOLD == 10 and srsi.RSI_OVERBOUGHT == 90
    src = inspect.getsource(srsi.generate_signals)
    assert "RSI_OVERSOLD" in src and "STUDY_MAX_LONG" not in src, (
        "strategy_rsi.generate_signals must still gate on RSI_OVERSOLD, not the study band"
    )
_run("live RSI entry threshold (RSI_OVERSOLD=10) is unchanged -- registry only observes",
     test_live_entry_threshold_is_untouched)


def test_report_runs_with_no_data():
    import subprocess
    p = subprocess.run([sys.executable, "report_rsi_thresholds.py"], cwd=BASE_DIR,
                       capture_output=True, text=True, timeout=30)
    assert p.returncode == 0
_run("report_rsi_thresholds.py exits cleanly with no data", test_report_runs_with_no_data)


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
