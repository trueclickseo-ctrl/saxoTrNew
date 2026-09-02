"""
Regression tests -- 2026-08-30 four user-supplied "advanced_*_master"
strategies added as SIM-only A/B tests against their originals:

  advanced_rsi_master       <-> rsi        (mean-reversion, momentum-exempt)
  advanced_bb_master        <-> bb         (mean-reversion, momentum-exempt)
  advanced_pullback_master  <-> pullback   (trend-continuation)
  advanced_cnn_lstm_master  <-> cnn_lstm   (selection wrapper on the same model)

Originals are UNTOUCHED. All SIM-only (not in either LIVE allowlist).
"""

import inspect
import os
import sys

import numpy as np
import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

GREEN, RED, YELLOW, CYAN, RESET, BOLD = (
    "\033[92m", "\033[91m", "\033[93m", "\033[96m", "\033[0m", "\033[1m"
)
_results = []

_NEW = [
    "advanced_rsi_master", "advanced_bb_master",
    "advanced_pullback_master", "advanced_cnn_lstm_master",
]


def _run(name, fn):
    try:
        fn()
        _results.append((name, True, None))
    except Exception as e:
        _results.append((name, False, f"{type(e).__name__}: {e}"))


def _mod(key):
    return __import__(f"forex.strategy_{key}", fromlist=["x"])


def _synthetic(n=560, seed=1, trend=0.0):
    rng = np.random.default_rng(seed)
    ret = rng.normal(trend, 0.005, n)
    close = 1.20 * np.exp(ret.cumsum())
    h = close * (1 + np.abs(rng.normal(0, 0.002, n)))
    l = close * (1 - np.abs(rng.normal(0, 0.002, n)))
    return pd.DataFrame({"High": h, "Low": l, "Close": close})


def test_all_registered_sim_only():
    import forex.runner as r
    for k in _NEW:
        assert k in r.STRATEGIES, f"{k} missing from STRATEGIES"
        assert r.SLOTS_PER_STRATEGY.get(k) == r._SWING_SLOTS, f"{k} slots wrong"
        assert k not in r.LIVE_ALLOWED_STRATEGIES, f"{k} leaked into LIVE allowlist"
        assert k not in r.LIVE_EUR_ALLOWED_STRATEGIES, f"{k} leaked into LIVE_EUR allowlist"
    # 24 as of 2026-09-02: original 20 + rsi_trend + ema_trend + bb_quality
    # + zscore_quality. (rsi_confirm was built + backtested + RETIRED the same
    # day -- unwired, not counted.)
    assert len(r.STRATEGIES) == 24
_run("forex/runner: all 4 advanced_*_master registered, uncapped slots, SIM-only; STRATEGIES == 24",
     test_all_registered_sim_only)


def test_originals_untouched():
    import forex.strategy_rsi as rsi
    import forex.strategy_bb as bb
    import forex.strategy_pullback as pb
    import forex.strategy_cnn_lstm as cl
    assert rsi.RSI_PERIOD == 2 and rsi.TREND_EMA == 200
    assert not hasattr(rsi, "FAST_EMA")          # the master adds EMA50; original rsi has none
    assert bb.RSI_OB == 65 and bb.RSI_OS == 35 and not hasattr(bb, "ADX_MAX")
    assert not hasattr(pb, "FAST_CONFIRM_EMA")
    assert cl.CONFIDENCE_THRESHOLD == 0.45       # unchanged
_run("originals (rsi / bb / pullback / cnn_lstm) are untouched", test_originals_untouched)


def test_mean_reversion_variants_are_momentum_exempt():
    import forex.runner as r
    src = inspect.getsource(r.run_daily)
    start = src.index("_NO_MOMENTUM_FILTER =")
    end = src.index("_edata =", start)          # the line right after the tuple closes
    block = src[start:end]
    assert '"advanced_rsi_master"' in block, block
    assert '"advanced_bb_master"' in block, block
    # trend-following variants must NOT be exempt
    assert '"advanced_pullback_master"' not in block
    assert '"advanced_cnn_lstm_master"' not in block
_run("forex/runner: advanced_rsi_master + advanced_bb_master are momentum-exempt; pullback/cnn_lstm masters are not",
     test_mean_reversion_variants_are_momentum_exempt)


def test_min_bars_fit_chart_bars():
    import forex.runner as r
    for k in _NEW:
        m = _mod(k)
        assert m.MIN_BARS <= r.CHART_BARS, f"{k} MIN_BARS {m.MIN_BARS} > CHART_BARS {r.CHART_BARS}"
_run("all 4: MIN_BARS fit inside CHART_BARS (500) -- no silent never-signals", test_min_bars_fit_chart_bars)


def test_public_interface_and_standard_hooks():
    for k in _NEW:
        m = _mod(k)
        for fn in ("generate_signals", "should_exit", "size_position",
                   "scan_summary", "trailing_stop_update"):
            assert hasattr(m, fn), f"{k} missing {fn}"
        # none define the non-standard update_stop_price hook -> no runner exit-loop change needed
        assert not hasattr(m, "update_stop_price"), f"{k} defines update_stop_price (unexpected)"
_run("all 4: compatible public interface + standard trailing_stop_update hook (no runner exit change)",
     test_public_interface_and_standard_hooks)


def test_generate_signals_runs_clean_and_wellformed():
    # synthetic uptrend + downtrend + flat; the strategies are highly
    # selective so 0 signals is acceptable -- the check is "no crash,
    # correct shape when it does fire".
    md = {
        "UPUSD":  _synthetic(seed=1, trend=0.0010),
        "DNUSD":  _synthetic(seed=2, trend=-0.0010),
        "FLATUSD": _synthetic(seed=3, trend=0.0),
        "SHORT":  _synthetic(n=100, seed=4),
    }
    for k in _NEW:
        m = _mod(k)
        sigs = m.generate_signals(md)
        assert isinstance(sigs, list)
        for s in sigs:
            assert s["direction"] in ("Buy", "Sell")
            assert "score" in s and "stop_price" in s and "close" in s
            assert s.get("strategy", k).startswith("advanced_")
            if s["direction"] == "Buy":
                assert s["stop_price"] < s["close"]
            else:
                assert s["stop_price"] > s["close"]
            assert s["symbol"] != "SHORT"     # too-short series never signals
        # scan_summary must not raise and must not report 'error' rows
        rows = m.scan_summary(md)
        assert not any(r.get("status") == "error" for r in rows), f"{k} scan_summary produced error rows"
_run("all 4: generate_signals + scan_summary run clean on synthetic data, signals well-formed",
     test_generate_signals_runs_clean_and_wellformed)


def test_rsi_master_wilder_edge_cases():
    import forex.strategy_advanced_rsi_master as m
    up = pd.Series(np.arange(1, 60, dtype=float))        # pure up -> RSI 100
    dn = pd.Series(np.arange(60, 1, -1, dtype=float))    # pure down -> RSI 0
    flat = pd.Series(np.full(60, 5.0))                   # flat -> RSI 50
    assert m._rsi(up).iloc[-1] == 100.0
    assert m._rsi(dn).iloc[-1] == 0.0
    assert m._rsi(flat).iloc[-1] == 50.0
_run("forex/strategy_advanced_rsi_master: _rsi handles pure-up/pure-down/flat without NaN",
     test_rsi_master_wilder_edge_cases)


def test_cnn_lstm_master_uses_same_model_no_retrain():
    import forex.strategy_advanced_cnn_lstm_master as m
    src = inspect.getsource(m)
    assert "MODEL_PATH" in src and "SCALER_PATH" in src
    assert "train" not in src.lower().replace("strainer", "").replace("trained", "") or "does NOT retrain" in src
    # decision gate: confidence + class margin + hold ceiling
    assert m.CONFIDENCE_THRESHOLD == 0.52 and m.MIN_CLASS_MARGIN == 0.08 and m.MAX_HOLD_PROB == 0.38
_run("forex/strategy_advanced_cnn_lstm_master: reuses the trained model (no retrain), stricter selection gates",
     test_cnn_lstm_master_uses_same_model_no_retrain)


print(f"\n{BOLD}{'='*70}{RESET}")
passed = sum(1 for _, ok, _ in _results)
failed = [(nm, e) for nm, ok, e in _results if not ok]
for nm, ok, err in _results:
    icon = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
    print(f"  [{icon}] {nm}")
    if err:
        print(f"         {YELLOW}{err}{RESET}")
print(f"{BOLD}{'='*70}{RESET}")
if failed:
    print(f"{RED}{BOLD}  {len(failed)} / {len(_results)} TESTS FAILED{RESET}")
    sys.exit(1)
else:
    print(f"{GREEN}{BOLD}  ALL {len(_results)} TESTS PASSED{RESET}")
    sys.exit(0)
