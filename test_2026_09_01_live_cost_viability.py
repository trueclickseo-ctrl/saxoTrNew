"""
Regression test -- 2026-09-01 LIVE trade-viability gate.

MXNUSD closed a real -EUR3.05 LIVE loss whose only fault was size:
+EUR2.13 gross price move, -EUR5.19 flat round-trip commission (a legacy
1,000-lot trade whose R had collapsed to ~EUR5). On real money the cost
gate is now:
  * edge-to-cost ratio 5x, not 3x on the 2R target (_min_edge_ratio);
  * RECOVERY-vs-COST (RSI): a realistic partial recovery
    (RSI_LIVE_ASSUMED_EXIT_R of R) must clear RSI_LIVE_MIN_RECOVERY_MULT x
    the all-in transaction cost (commission + spread + slippage) -- else
    the signal is REJECTED ("recovery_below_cost_margin"), never resized
    up. ONE pair-independent rule; replaced BOTH the generic
    MIN_LIVE_NOTIONAL_EUR and the pair-specific LIVE_RSI_MIN_UNITS table
    (at fixed EUR45 risk the economics are the same for every pair).
  * an UNKNOWN round-trip cost OR missing FX rate blocks on LIVE
    (SIM still passes on unknown cost -- forward-test continuity).
No signal/entry/exit change -- these only ever REMOVE marginal trades.
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


import forex.runner as fr


def test_min_edge_ratio_is_stricter_on_live():
    fr.set_account_env("live");     assert fr._min_edge_ratio() == 5.0
    fr.set_account_env("live_eur"); assert fr._min_edge_ratio() == 5.0
    fr.set_account_env("sim");      assert fr._min_edge_ratio() == 3.0
    assert fr.MIN_LIVE_EDGE_TO_COST_RATIO == 5.0
    assert fr.MIN_EDGE_TO_COST_RATIO == 3.0     # SIM untouched


def test_generic_notional_floor_and_units_table_are_gone():
    # 2026-09-01: BOTH the generic MIN_LIVE_NOTIONAL_EUR and the pair-
    # specific LIVE_RSI_MIN_UNITS table were replaced by ONE pair-
    # independent recovery-vs-cost gate.
    assert not hasattr(fr, "MIN_LIVE_NOTIONAL_EUR")
    assert not hasattr(fr, "LIVE_RSI_MIN_UNITS")
    assert not hasattr(fr, "_live_rsi_min_units")
    src = inspect.getsource(fr._run_entries)
    assert "below_min_notional" not in src and "below_live_min_quantity" not in src


def test_recovery_gate_constants():
    assert fr.RSI_LIVE_ASSUMED_EXIT_R == 0.5       # provisional; journal replaces
    assert fr.RSI_LIVE_MIN_RECOVERY_MULT == 3.0    # user's safety margin
    assert fr.RSI_LIVE_SLIPPAGE_PIPS == 0.5
    # financing is NOT in the gate (holding cost, sign not fixed)
    assert "RSI_LIVE_ASSUMED_HOLD_DAYS" not in dir(fr)


def test_gate_block_logic_in_source():
    src = inspect.getsource(fr._run_entries)
    assert 'block_reason = True, "cost_not_cleared"' in src
    assert '"cost_unknown_live"' in src
    # the recovery gate: 0.5R * realised_R < 3.0 * all_in_cost -> reject
    assert "RSI_LIVE_ASSUMED_EXIT_R * _realised_r_eur" in src
    assert "RSI_LIVE_MIN_RECOVERY_MULT * _all_in_cost_eur" in src
    assert '"recovery_below_cost_margin"' in src
    # RSI-scoped (the only LIVE strategy; a future one needs its own logic)
    i = src.index('"recovery_below_cost_margin"')
    assert 'strat_name == "rsi"' in src[i-400:i]
    # realised R is the RISK-BASED qty's stop distance -- never a bumped qty
    assert 'abs(sig["close"] - sig["stop_price"]) * qty * eur_rate_for_log' in src
    # LIVE now also blocks when the FX rate is missing (can't evaluate)
    assert "round_trip_cost is None or eur_rate_for_log is None" in src


def test_reject_never_resizes_up():
    src = inspect.getsource(fr._run_entries)
    # nowhere does the gate bump qty toward a minimum
    assert "qty = _live_min_units" not in src
    i = src.index('block_reason == "recovery_below_cost_margin"')
    assert "rejecting (not resizing up)" in src[i:i + 600]
    assert "continue" in src[i:i + 1600]


def test_all_in_cost_helper():
    # pure helper: commission + spread crossing + slippage, all in EUR
    base = fr._live_all_in_cost_eur(commission_eur=5.18, spread_pct=None,
                                    entry_px=1.10, notional_eur=None, quote_ccy="USD")
    assert base == 5.18                                   # commission only when notional unknown
    full = fr._live_all_in_cost_eur(commission_eur=5.18, spread_pct=0.01,
                                    entry_px=1.10, notional_eur=7000.0, quote_ccy="USD")
    # spread term 0.01% of 7,000 = 0.70 ; slippage 0.5 pip round-trip
    assert abs((full - 5.18) - (0.70 + (0.5 * 0.0001 / 1.10) * 7000)) < 1e-6
    # JPY pip is 0.01
    jpy = fr._live_all_in_cost_eur(commission_eur=5.18, spread_pct=None,
                                   entry_px=185.0, notional_eur=6000.0, quote_ccy="JPY")
    assert abs((jpy - 5.18) - (0.5 * 0.01 / 185.0) * 6000) < 1e-6
    # unknown commission -> None (don't guess the dominant term)
    assert fr._live_all_in_cost_eur(None, 0.01, 1.1, 7000.0, "USD") is None


def test_at_e45_all_pairs_clear_the_gate():
    # sanity from the live analysis: at EUR45 risk every HIGH_VOLUME pair's
    # 0.5R recovery clears 3x the all-in cost (ratios were 3.1-4.0x).
    import json, os
    p = os.path.join(BASE, "data", "live_pair_commission_analysis.json")
    if not os.path.exists(p):
        return
    d = json.load(open(p, encoding="utf-8"))
    live = [r for r in d["rows"] if str(r.get("tier", "")).startswith("HIGH_VOLUME")]
    assert live, "analysis json has no HIGH_VOLUME rows"
    for r in live:
        R = r.get("realised_risk_eur"); c = r.get("commission_eur_roundtrip")
        if R and c:
            assert 0.5 * R >= 3.0 * c, f"{r['pair']}: 0.5R {0.5*R:.1f} < 3x comm {3*c:.1f}"


def test_cost_gate_log_carries_recovery_fields():
    import forex.forward_observation as fo
    sig = inspect.signature(fo.log_cost_gate_decision)
    assert {"notional_eur", "realised_r_eur", "all_in_cost_eur"} <= set(sig.parameters)
    src = inspect.getsource(fr._run_entries)
    assert "min_edge_to_cost_ratio=_edge_ratio" in src
    assert "realised_r_eur=_realised_r_eur" in src and "all_in_cost_eur=_all_in_cost_eur" in src


def test_modules_parse():
    ast.parse(inspect.getsource(fr))
    import forex.forward_observation as fo
    ast.parse(inspect.getsource(fo))


for _n, _f in list(globals().items()):
    if _n.startswith("test_") and callable(_f):
        _run(_n, _f)
fr.set_account_env("sim")

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
