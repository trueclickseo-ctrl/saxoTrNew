"""
2026-09-02 -- rsi_confirm: the user's confirmation-delay + conviction idea,
built for a SIM forward-test (backtest is the NEXT step).

An RSI(2) signal is queued in a candidate bucket, observed ~6-30h, and
entered only on a confirmed dip-then-recover (or immediate follow-through),
as ONE concentrated position at a time with a tight ATR take-profit.

These tests lock: SIM-only wiring, the pure candidate state machine (queue /
age / expire / confirm), the conviction single-slot, the fast-TP exit, and
that strategy_rsi.py is untouched.
"""

import ast
import inspect
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

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
import forex.strategy_rsi_confirm as rc
import forex.runner as runner

T0 = datetime(2022, 10, 20, tzinfo=timezone.utc)


def _bars(px, start="2022-01-01"):
    idx = pd.date_range(start, periods=len(px), freq="D")
    px = np.asarray(px, float)
    return pd.DataFrame({"Open": px, "High": px + 0.4, "Low": px - 0.4, "Close": px}, index=idx)


def _uptrend_then_dip(n=260):
    px = np.linspace(100, 140, n) + np.random.default_rng(1).normal(0, 0.3, n)
    return px


# ── 1. wiring ───────────────────────────────────────────────────────────

def test_registered_sim_only_single_slot():
    assert runner.STRATEGIES.get("rsi_confirm") is rc
    assert runner.SLOTS_PER_STRATEGY["rsi_confirm"] == 1          # conviction: one at a time
    assert "rsi_confirm" not in runner.LIVE_ALLOWED_STRATEGIES
    assert "rsi_confirm" not in runner.LIVE_EUR_ALLOWED_STRATEGIES
    assert "rsi_confirm" not in runner.PROFIT_LADDER_STRATEGIES   # its own fast-TP exit
    src = inspect.getsource(runner)
    i = src.index("_NO_MOMENTUM_FILTER = (")
    assert '"rsi_confirm"' in src[i:src.index("_edata = market_data", i)]


def test_runner_has_bucket_persistence():
    src = inspect.getsource(runner)
    assert "RSI_CONFIRM_CANDIDATES_FILE" in src
    assert "_load_rsi_confirm_candidates" in src and "_save_rsi_confirm_candidates" in src
    # the strategy module itself must be pure -- no file / order I/O
    msrc = inspect.getsource(rc)
    for bad in ("open(", "json.dump", "json.load", "saxo", "_place", "requests",
                "insert_trade", "cancel_order"):
        assert bad not in msrc, f"{bad!r} in the pure strategy module"


def test_strategy_rsi_untouched():
    try:
        head = subprocess.run(["git", "show", "HEAD:forex/strategy_rsi.py"],
                              capture_output=True, text=True, cwd=BASE, timeout=15).stdout
        disk = open(os.path.join(BASE, "forex", "strategy_rsi.py"), encoding="utf-8").read()
        assert head and head == disk, "forex/strategy_rsi.py was modified"
    except FileNotFoundError:
        pass


def test_constants_mirror_rsi_except_the_new_knobs():
    for k in ("RSI_PERIOD", "RSI_OVERSOLD", "RSI_OVERBOUGHT", "ATR_STOP_MULT",
              "RISK_PCT", "LOT_ROUND", "MIN_BARS"):
        assert getattr(rc, k) == getattr(base, k), f"{k} diverged from rsi"
    assert rc.MAX_POSITIONS == 1
    upper = {n.targets[0].id for n in ast.walk(ast.parse(inspect.getsource(rc)))
             if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
             and n.targets[0].id.isupper()}
    new = upper - {getattr(base, "__name__", "")} - {
        "RSI_PERIOD", "RSI_OVERSOLD", "RSI_OVERBOUGHT", "RSI_EXIT_LONG", "RSI_EXIT_SHORT",
        "TREND_EMA", "ATR_PERIOD", "ATR_STOP_MULT", "RISK_PCT", "MAX_POSITIONS",
        "LOT_ROUND", "MIN_BARS"}
    assert new == {
        "OBSERVE_MIN_HOURS", "OBSERVE_MAX_HOURS", "MIN_DIP_ATR", "MIN_RECOVERY_ATR",
        "MIN_FOLLOW_ATR", "RSI_STILL_OK_LONG", "RSI_STILL_OK_SHORT", "FAST_TP_ATR",
        "CONVICTION_TIME_STOP_DAYS", "CONVICTION_NOTIONAL_QUOTE"}, f"unexpected knobs: {new}"


# ── 2. the candidate state machine ──────────────────────────────────────

def test_fresh_rsi_signal_is_queued_not_traded(monkeypatch=None):
    df = _bars(_uptrend_then_dip())
    md = {"EURUSD": df}
    fake = [{"symbol": "EURUSD", "direction": "Buy", "close": float(df["Close"].iloc[-1]),
             "rsi": 4.0, "regime_at_entry": "TRENDING_BULLISH"}]
    real = base.generate_signals
    try:
        base.generate_signals = lambda *a, **k: fake
        cands, logs = rc.update_candidates(md, {}, set(), now=T0)
        assert "EURUSD" in cands and cands["EURUSD"]["direction"] == "Buy"
        assert cands["EURUSD"]["best_adverse_px"] == cands["EURUSD"]["signal_px"]
        # nothing to enter yet -- no time has passed
        assert rc.generate_signals(md, set(), candidates=cands, now=T0) == []
    finally:
        base.generate_signals = real


def test_candidate_expires_unconfirmed_after_max_hours():
    df = _bars(_uptrend_then_dip())
    md = {"EURUSD": df}
    cands = {"EURUSD": {"direction": "Buy", "signal_px": 130.0,
                        "signal_ts": T0.isoformat(), "signal_rsi": 4.0,
                        "regime": None, "best_adverse_px": 130.0}}
    later = T0 + timedelta(hours=rc.OBSERVE_MAX_HOURS + 2)
    out, logs = rc.update_candidates(md, cands, set(), now=later)
    assert "EURUSD" not in out
    assert any("EXPIRED" in l for l in logs)


def test_open_position_clears_its_candidate():
    md = {"EURUSD": _bars(_uptrend_then_dip())}
    cands = {"EURUSD": {"direction": "Buy", "signal_px": 130.0,
                        "signal_ts": T0.isoformat(), "best_adverse_px": 130.0}}
    out, _ = rc.update_candidates(md, cands, {"EURUSD"}, now=T0 + timedelta(hours=10))
    assert "EURUSD" not in out


def test_confirm_requires_dip_then_recovery():
    """A Buy candidate: price must have gone against us, then climbed back."""
    close = 100.0
    # never dipped, never ran -> NOT confirmed
    flat = _bars([100.0] * 260)
    c = {"EURUSD": {"direction": "Buy", "signal_px": 100.0,
                    "signal_ts": T0.isoformat(), "best_adverse_px": 100.0,
                    "regime": "TRENDING_BULLISH"}}
    mid = T0 + timedelta(hours=12)
    assert rc.generate_signals({"EURUSD": flat}, set(), candidates=c, now=mid) == []

    # dipped to 97 then climbed back to 101 -> confirmed
    atr = float(rc._atr_now(flat)) or 1.0
    px = [100.0] * 250 + [98, 97, 97.5, 99, 100.5, 101.0]
    df = _bars(px)
    c2 = {"EURUSD": {"direction": "Buy", "signal_px": 100.0,
                     "signal_ts": T0.isoformat(),
                     "best_adverse_px": 97.0, "regime": "TRENDING_BULLISH"}}
    out = rc.generate_signals({"EURUSD": df}, set(), candidates=c2, now=mid)
    assert len(out) == 1
    s = out[0]
    assert s["stage"] == "confirm" and s["direction"] == "Buy"
    assert s["stop_price"] < s["close"] < s["tp_price"]
    assert s["units"] >= rc.LOT_ROUND
    assert s["adverse_atr"] > 0


def test_not_confirmed_inside_the_observation_floor():
    px = [100.0] * 250 + [98, 97, 97.5, 99, 100.5, 101.0]
    c = {"EURUSD": {"direction": "Buy", "signal_px": 100.0, "signal_ts": T0.isoformat(),
                    "best_adverse_px": 97.0, "regime": None}}
    too_soon = T0 + timedelta(hours=rc.OBSERVE_MIN_HOURS - 1)
    assert rc.generate_signals({"EURUSD": _bars(px)}, set(), candidates=c, now=too_soon) == []


def test_conviction_units_are_small_and_floored():
    assert rc._conviction_units(1.08) == rc.LOT_ROUND          # ~700/1.08 -> min lot
    assert rc._conviction_units(0.0) == rc.LOT_ROUND
    assert rc._conviction_units(150.0) == rc.LOT_ROUND         # JPY-quoted, still min lot
    assert rc._conviction_units(1e-6) >= rc.LOT_ROUND


# ── 3. the fast exit ────────────────────────────────────────────────────

def test_fast_tp_fires_on_a_small_favourable_move():
    pos = {"direction": "Buy", "entry_price": 100.0, "atr_at_entry": 1.0}
    up = _bars([100.0] * 250 + [100.1, 100.3, 100.5, 100.65, 100.7, 100.75])
    ex, reason = rc.should_exit(pos, up, 1)
    assert ex and "fast_tp" in reason

    flat = _bars([100.0] * 256)
    ex2, _ = rc.should_exit(pos, flat, 1)
    assert not ex2   # no gain, not enough days -> hold


def test_short_time_stop():
    pos = {"direction": "Buy", "entry_price": 100.0, "atr_at_entry": 1.0}
    flat = _bars([100.0] * 256)
    ex, reason = rc.should_exit(pos, flat, rc.CONVICTION_TIME_STOP_DAYS)
    assert ex and "time_stop" in reason


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
