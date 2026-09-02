"""
2026-09-02 -- ema_trend + bb_quality + zscore_quality: SIM-only A/B twins of
"ema" / "bb" / "zscore" that gate entries on features a 12y/49-CORE-pair
decomposition showed carry the stable edge.

  ema_trend      = "ema"    kept only if crossover age <= 3 bars AND
                             |plus_di - minus_di| >= 15
  bb_quality     = "bb"     kept only if |plus_di - minus_di| <= 14 (non-directional)
  zscore_quality = "zscore" kept only if |plus_di - minus_di| <= 14 (non-directional)

These tests lock the A/B design: each twin must be IDENTICAL to its parent
except the entry gate, SIM-only, delegate all exit management, and can never
invent a signal the parent wouldn't fire.
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


import forex.strategy as ema_base
import forex.strategy_bb as bb_base
import forex.strategy_zscore as zs_base
import forex.strategy_ema_trend as et
import forex.strategy_bb_quality as bq
import forex.strategy_zscore_quality as zq
import forex.runner as runner


# ── 1. registration / A-B wiring ─────────────────────────────────────────

def test_registered_as_sim_only_ab_twins():
    assert runner.STRATEGIES.get("ema_trend") is et
    assert runner.STRATEGIES.get("bb_quality") is bq
    assert runner.STRATEGIES.get("zscore_quality") is zq
    for k, parent in (("ema_trend", "ema"), ("bb_quality", "bb"), ("zscore_quality", "zscore")):
        assert runner.SLOTS_PER_STRATEGY[k] == runner.SLOTS_PER_STRATEGY[parent], f"{k} slots != {parent}"
        assert runner.SLOTS_PER_STRATEGY[k] == runner._SWING_SLOTS, f"{k} not full-universe"
        assert k not in runner.LIVE_ALLOWED_STRATEGIES, f"{k} leaked into LIVE allowlist"
        assert k not in runner.LIVE_EUR_ALLOWED_STRATEGIES, f"{k} leaked into LIVE_EUR allowlist"


def test_momentum_filter_membership_matches_parent_nature():
    src = inspect.getsource(runner)
    i = src.index("_NO_MOMENTUM_FILTER = (")
    block = src[i:src.index("_edata = market_data", i)]
    # bb_quality / zscore_quality are mean-reversion (twin bb / zscore, both exempt) -> exempt
    assert '"bb_quality"' in block
    assert '"zscore_quality"' in block
    # ema_trend is trend-following (twins ema, which is NOT exempt) -> momentum-filtered
    assert '"ema_trend"' not in block


def test_not_in_profit_ladder():
    # ema / bb / zscore don't use the RSI(2) profit ladder -> neither do their twins
    assert "ema_trend" not in runner.PROFIT_LADDER_STRATEGIES
    assert "bb_quality" not in runner.PROFIT_LADDER_STRATEGIES
    assert "zscore_quality" not in runner.PROFIT_LADDER_STRATEGIES


def test_parent_modules_are_untouched():
    for rel in ("forex/strategy.py", "forex/strategy_bb.py", "forex/strategy_zscore.py"):
        try:
            head = subprocess.run(["git", "show", f"HEAD:{rel}"],
                                  capture_output=True, text=True, cwd=BASE, timeout=15).stdout
            disk = open(os.path.join(BASE, *rel.split("/")), encoding="utf-8").read()
            assert head and head == disk, f"{rel} has been modified"
        except FileNotFoundError:
            pass


# ── 2. identical-by-delegation ──────────────────────────────────────────

def test_exit_sizing_trailing_are_the_SAME_objects():
    assert et.should_exit is ema_base.should_exit
    assert et.size_position is ema_base.size_position
    assert et.trailing_stop_update is ema_base.trailing_stop_update
    assert bq.should_exit is bb_base.should_exit
    assert bq.size_position is bb_base.size_position
    assert bq.trailing_stop_update is bb_base.trailing_stop_update
    assert zq.should_exit is zs_base.should_exit
    assert zq.size_position is zs_base.size_position
    assert not hasattr(zq, "trailing_stop_update")   # zscore has none -> twin has none (clean A/B)


def test_constants_mirror_parents():
    for k in ("FAST_EMA", "SLOW_EMA", "ADX_MIN", "ATR_STOP_MULT", "RISK_PCT",
              "MAX_POSITIONS", "TIME_STOP_DAYS", "LOT_ROUND", "MIN_BARS"):
        assert getattr(et, k) == getattr(ema_base, k), f"ema_trend.{k} diverged"
    for k in ("BB_PERIOD", "BB_STD", "RSI_OB", "RSI_OS", "ATR_STOP_MULT", "RISK_PCT",
              "MAX_POSITIONS", "TIME_STOP_DAYS", "LOT_ROUND", "MIN_BARS"):
        assert getattr(bq, k) == getattr(bb_base, k), f"bb_quality.{k} diverged"
    for k in ("LOOKBACK", "Z_ENTRY", "Z_EXIT", "EMA_TREND", "ATR_STOP_MULT",
              "RISK_PCT", "TIME_STOP_DAYS", "LOT_ROUND", "MIN_BARS"):
        assert getattr(zq, k) == getattr(zs_base, k), f"zscore_quality.{k} diverged"


def test_only_the_gate_thresholds_are_new():
    et_src, bq_src, zq_src = inspect.getsource(et), inspect.getsource(bq), inspect.getsource(zq)
    def _upper_assigns(src):
        return {n.targets[0].id for n in ast.walk(ast.parse(src))
                if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
                and n.targets[0].id.isupper()}
    et_allowed = {"FAST_EMA", "SLOW_EMA", "ADX_PERIOD", "ADX_MIN", "ATR_PERIOD",
                  "ATR_STOP_MULT", "RISK_PCT", "MAX_POSITIONS", "TIME_STOP_DAYS",
                  "LOT_ROUND", "MIN_BARS", "MAX_CROSSOVER_AGE", "DI_SPREAD_MIN"}
    bq_allowed = {"BB_PERIOD", "BB_STD", "RSI_PERIOD", "RSI_OB", "RSI_OS", "ATR_PERIOD",
                  "ATR_STOP_MULT", "RISK_PCT", "MAX_POSITIONS", "TIME_STOP_DAYS",
                  "LOT_ROUND", "MIN_BARS", "DI_SPREAD_MAX"}
    zq_allowed = {"LOOKBACK", "Z_ENTRY", "Z_EXIT", "EMA_TREND", "ATR_PERIOD",
                  "ATR_STOP_MULT", "RISK_PCT", "TIME_STOP_DAYS", "LOT_ROUND",
                  "MIN_BARS", "DI_SPREAD_MAX"}
    assert _upper_assigns(et_src) - et_allowed - {"_BASE_SIGNAL_LOOKBACK"} == set()
    assert _upper_assigns(bq_src) - bq_allowed == set()
    assert _upper_assigns(zq_src) - zq_allowed == set()
    # the two ema gates + one bb gate + one zscore gate, exact values
    assert et.MAX_CROSSOVER_AGE == 3 and et.DI_SPREAD_MIN == 15.0
    assert bq.DI_SPREAD_MAX == 14.0
    assert zq.DI_SPREAD_MAX == 14.0


def test_no_orders_or_io_in_either_module():
    for src in (inspect.getsource(et), inspect.getsource(bq), inspect.getsource(zq)):
        for bad in ("saxo", "open(", "requests", "_place", "insert_trade",
                    "cancel_order", ".to_csv", "json.dump"):
            assert bad not in src, f"{bad!r} in a pure strategy twin"


# ── 3. the gate itself: subset of the parent, and it actually filters ────

def _synth(kind, n=420, seed=3):
    idx = pd.date_range("2021-01-01", periods=n, freq="D")
    rng = np.random.default_rng(seed)
    if kind == "clean_up":       # strong late uptrend -> fresh EMA cross, wide DI
        px = np.concatenate([120 + rng.normal(0, 0.4, n - 40),
                             120 + np.linspace(0, 12, 40)]) + rng.normal(0, 0.3, n)
    elif kind == "chop":         # sideways -> stale/again crossovers, thin DI
        px = 120 + 3 * np.sin(np.arange(n) / 5.0) + rng.normal(0, 0.9, n)
    elif kind == "bb_spike_trend":   # big spike inside a strong trend (high DI)
        px = 120 + np.linspace(0, 25, n) + rng.normal(0, 0.4, n)
        px[-1] += 6
    else:                        # bb_spike_flat: spike with no trend (low DI)
        px = 120 + rng.normal(0, 0.5, n)
        px[-1] += 5
    h = px + np.abs(rng.normal(0, 0.5, n))
    l = px - np.abs(rng.normal(0, 0.5, n))
    return pd.DataFrame({"Open": px, "High": h, "Low": l, "Close": px}, index=idx)


def test_ema_trend_is_a_subset_of_ema():
    md = {"A": _synth("clean_up"), "B": _synth("chop"), "C": _synth("clean_up", seed=9)}
    b = {(s["symbol"], s["direction"]) for s in ema_base.generate_signals(md)}
    t = {(s["symbol"], s["direction"]) for s in et.generate_signals(md)}
    assert t <= b, f"ema_trend produced signals ema did not: {t - b}"


def test_bb_quality_is_a_subset_of_bb():
    md = {"A": _synth("bb_spike_trend"), "B": _synth("bb_spike_flat"),
          "C": _synth("bb_spike_flat", seed=11)}
    b = {(s["symbol"], s["direction"]) for s in bb_base.generate_signals(md)}
    t = {(s["symbol"], s["direction"]) for s in bq.generate_signals(md)}
    assert t <= b, f"bb_quality produced signals bb did not: {t - b}"


def test_zscore_quality_is_a_subset_of_zscore():
    md = {"A": _synth("bb_spike_trend"), "B": _synth("bb_spike_flat"),
          "C": _synth("bb_spike_flat", seed=13), "D": _synth("chop", seed=4)}
    b = {(s["symbol"], s["direction"]) for s in zs_base.generate_signals(md)}
    t = {(s["symbol"], s["direction"]) for s in zq.generate_signals(md)}
    assert t <= b, f"zscore_quality produced signals zscore did not: {t - b}"
    for sig in zq.generate_signals(md):
        assert sig["di_spread"] <= zq.DI_SPREAD_MAX


def test_ema_trend_kept_signals_obey_both_gates():
    md = {s: _synth("clean_up", seed=i) for i, s in enumerate(["P1", "P2", "P3", "P4"])}
    for sig in et.generate_signals(md):
        assert sig["crossover_age"] <= et.MAX_CROSSOVER_AGE
        assert sig["di_spread"] >= et.DI_SPREAD_MIN


def test_bb_quality_kept_signals_obey_the_gate():
    md = {s: _synth("bb_spike_flat", seed=i) for i, s in enumerate(["P1", "P2", "P3", "P4"])}
    for sig in bq.generate_signals(md):
        assert sig["di_spread"] <= bq.DI_SPREAD_MAX


def test_gates_are_not_no_ops_on_real_history():
    """On 2 years of real-ish trending data at least one parent signal must be
    dropped by each twin -- otherwise the gate does nothing."""
    import forex.universe as U
    try:
        import yfinance as yf
    except Exception:
        return
    syms = [p for p in U.PAIRS if p["symbol"] in U.CORE_SYMBOLS][:12]
    md = {}
    for p in syms:
        try:
            d = yf.download(p["yf_ticker"], period="3y", interval="1d",
                            progress=False, auto_adjust=True)
            if isinstance(d.columns, pd.MultiIndex):
                d.columns = d.columns.get_level_values(0)
            if len(d) > 260:
                md[p["symbol"]] = d[["Open", "High", "Low", "Close"]].dropna()
        except Exception:
            pass
    if len(md) < 4:
        return
    for parent, twin, name in ((ema_base, et, "ema_trend"), (bb_base, bq, "bb_quality"),
                               (zs_base, zq, "zscore_quality")):
        pb = len(parent.generate_signals(md))
        tw = len(twin.generate_signals(md))
        assert tw <= pb, f"{name}: {tw} > parent {pb}"


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
