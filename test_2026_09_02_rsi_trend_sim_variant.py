"""
2026-09-02 -- rsi_trend: a SIM-only A/B twin of "rsi" that gates entries
on the regime classifier (Buy only in TRENDING_BULLISH, Sell only in
TRENDING_BEARISH).

Hypothesis (from an 11y / 49-CORE-pair decomposition):
  RSI(2) by regime label at entry --
    TRENDING_BULLISH +0.088 R (stable both halves)
    TRENDING_BEARISH +0.040 R (stable)
    RANGING          +0.011 R (unstable -- 2014-20 -0.029)
  -> the stable edge is entirely "buy the dip IN A TREND".

These tests lock the A/B design: rsi_trend must be IDENTICAL to rsi except
the entry gate, SIM-only, and can never invent a signal rsi wouldn't fire.
"""

import ast
import inspect
import os
import subprocess
import sys

import numpy as np
import pandas as pd

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


import forex.strategy_rsi as base
import forex.strategy_rsi_trend as rt
import forex.runner as runner
from ai.regime.classifier import classify_regime


# ── 1. registration / A-B wiring ──────────────────────────────────────────

def test_registered_as_sim_only_ab_twin():
    assert runner.STRATEGIES.get("rsi_trend") is rt
    assert "rsi_trend" in runner.SLOTS_PER_STRATEGY
    assert runner.SLOTS_PER_STRATEGY["rsi_trend"] == runner.SLOTS_PER_STRATEGY["rsi"]
    # SIM ONLY -- never in either LIVE allowlist
    assert "rsi_trend" not in runner.LIVE_ALLOWED_STRATEGIES
    assert "rsi_trend" not in runner.LIVE_EUR_ALLOWED_STRATEGIES


def test_shares_rsis_exit_management_for_a_clean_ab():
    # both arms use the profit ladder -> the ONLY difference is the entry gate
    assert "rsi_trend" in runner.PROFIT_LADDER_STRATEGIES
    assert "rsi" in runner.PROFIT_LADDER_STRATEGIES
    assert runner._HEAT_LIMIT_BY_STRATEGY.get("rsi_trend") == runner._HEAT_LIMIT_BY_STRATEGY.get("rsi")
    # exempt from the momentum pre-filter, same as "rsi" (it has its own gate)
    src = inspect.getsource(runner)
    i = src.index("_NO_MOMENTUM_FILTER = (")
    assert '"rsi_trend"' in src[i:i + 400]


def test_original_rsi_module_is_untouched():
    """strategy_rsi.py must be byte-identical to HEAD -- the A/B control."""
    try:
        head = subprocess.run(["git", "show", "HEAD:forex/strategy_rsi.py"],
                               capture_output=True, text=True, cwd=BASE, timeout=15).stdout
        disk = open(os.path.join(BASE, "forex", "strategy_rsi.py"), encoding="utf-8").read()
        assert head and head == disk, "forex/strategy_rsi.py has been modified"
    except FileNotFoundError:
        pass  # git not on PATH in this env -- skip


# ── 2. identical-by-delegation ───────────────────────────────────────────

def test_exit_sizing_trailing_are_the_SAME_objects():
    assert rt.should_exit is base.should_exit
    assert rt.size_position is base.size_position
    assert rt.trailing_stop_update is base.trailing_stop_update


def test_constants_mirror_rsi():
    for k in ("RSI_PERIOD", "RSI_OVERSOLD", "RSI_OVERBOUGHT", "RSI_EXIT_LONG",
              "RSI_EXIT_SHORT", "TREND_EMA", "ATR_STOP_MULT", "RISK_PCT",
              "MAX_POSITIONS", "TIME_STOP_DAYS", "LOT_ROUND", "MIN_BARS"):
        assert getattr(rt, k) == getattr(base, k), f"{k} diverged from rsi"


def test_module_has_no_new_thresholds_or_params():
    """rt must not introduce its own numeric knobs -- it's rsi + one gate."""
    src = inspect.getsource(rt)
    tree = ast.parse(src)
    assigns = [n.targets[0].id for n in ast.walk(tree)
               if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
               and n.targets[0].id.isupper()]
    # only re-exports of rsi's constants + the direction->regime-label map
    allowed = {"RSI_PERIOD", "RSI_OVERSOLD", "RSI_OVERBOUGHT", "RSI_EXIT_LONG",
               "RSI_EXIT_SHORT", "TREND_EMA", "ATR_PERIOD", "ATR_STOP_MULT",
               "RISK_PCT", "MAX_POSITIONS", "TIME_STOP_DAYS", "LOT_ROUND", "MIN_BARS",
               "_TREND_FOR_SIDE"}
    extra = set(assigns) - allowed
    assert not extra, f"rsi_trend introduced its own constant(s): {extra}"
    # and _TREND_FOR_SIDE must be exactly the two trend labels, nothing tunable
    assert rt._TREND_FOR_SIDE == {"Buy": "TRENDING_BULLISH", "Sell": "TRENDING_BEARISH"}


# ── 3. the gate itself ───────────────────────────────────────────────────

def _series(kind, n=320):
    idx = pd.date_range("2022-01-01", periods=n, freq="D")
    rng = np.random.default_rng(7)
    if kind == "uptrend":
        base_px = np.linspace(100, 150, n) + rng.normal(0, 0.6, n)
        base_px[-3:] = base_px[-4] * np.array([0.985, 0.972, 0.96])   # sharp dip -> RSI2 oversold
    elif kind == "downtrend":
        base_px = np.linspace(150, 100, n) + rng.normal(0, 0.6, n)
        base_px[-3:] = base_px[-4] * np.array([1.015, 1.03, 1.045])   # sharp spike -> RSI2 overbought
    else:  # range
        base_px = 120 + 4 * np.sin(np.arange(n) / 7.0) + rng.normal(0, 0.5, n)
        base_px[-3:] = base_px[-4] - np.array([1.5, 3.0, 4.2])
    h = base_px + np.abs(rng.normal(0, 0.4, n))
    l = base_px - np.abs(rng.normal(0, 0.4, n))
    return pd.DataFrame({"Open": base_px, "High": h, "Low": l, "Close": base_px}, index=idx)


def test_generate_signals_is_a_subset_of_rsi():
    md = {"UP": _series("uptrend"), "DN": _series("downtrend"), "RG": _series("range")}
    b = {(s["symbol"], s["direction"]) for s in base.generate_signals(md)}
    t = {(s["symbol"], s["direction"]) for s in rt.generate_signals(md)}
    assert t <= b, f"rsi_trend produced signals rsi did not: {t - b}"


def test_kept_signals_match_their_regime():
    md = {"UP": _series("uptrend"), "DN": _series("downtrend"), "RG": _series("range")}
    want = {"Buy": "TRENDING_BULLISH", "Sell": "TRENDING_BEARISH"}
    for sig in rt.generate_signals(md):
        lbl = classify_regime(md[sig["symbol"]])["label"]
        assert sig.get("regime_at_entry") == lbl == want[sig["direction"]], \
            f"{sig['symbol']} {sig['direction']}: kept under regime {lbl}"


def test_ranging_pair_signal_is_dropped():
    df = _series("range")
    assert classify_regime(df)["label"] != "TRENDING_BULLISH"
    md = {"RG": df}
    # if rsi fires here at all, rsi_trend must NOT
    if base.generate_signals(md):
        assert rt.generate_signals(md) == []


def test_classify_failure_drops_the_signal_never_raises(monkeypatch=None):
    md = {"UP": _series("uptrend")}
    import ai.regime.classifier as clf
    real = clf.classify_regime
    try:
        clf.classify_regime = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        out = rt.generate_signals(md)          # must not raise
        assert out == []                       # UNKNOWN label -> nothing kept
    finally:
        clf.classify_regime = real


def test_runner_parses():
    ast.parse(inspect.getsource(runner))


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
