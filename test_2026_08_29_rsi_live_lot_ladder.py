"""
Regression tests -- 2026-08-29 RSI real-money lot ladder.

User instruction: "do not buy 1 quantity for RSI always buy 10, 20, 30,
40, 50, 60, 70, 80, 90, or 100 units" (units = thousands). At the
1,000-unit FX minimum lot, Saxo's flat ~5 EUR round-trip commission
dominated the trade and turned RSI's designed 2:1 reward:risk into ~0.9:1
net. RSI on live/live_eur risk-sized as before, then snapped the result
to the nearest 10,000-unit rung, clamped to [10,000, 100,000].

SUPERSEDED 2026-08-31 as the DEFAULT path (RSI_LIVE_FIXED_RISK_EUR = 45.0,
see test_2026_08_31_rsi_live_fixed_risk.py): the 10k ladder is now only
used when RSI_LIVE_FIXED_RISK_EUR is None. `_snap_rsi_live_lot` itself is
unchanged and still unit-tested here; the entry-loop wiring tests below
assert it is the fallback branch.
"""

import inspect
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

GREEN, RED, YELLOW, CYAN, RESET, BOLD = (
    "\033[92m", "\033[91m", "\033[93m", "\033[96m", "\033[0m", "\033[1m"
)
_results = []


def _run(name, fn):
    try:
        result = fn()
        if result is None:
            result = True
        _results.append((name, bool(result), None))
    except Exception as e:
        _results.append((name, False, f"{type(e).__name__}: {e}"))


def test_snap_clamps_and_rounds_to_10k():
    import forex.runner as r
    # below the floor -> floor
    assert r._snap_rsi_live_lot(0) == 10_000
    assert r._snap_rsi_live_lot(1_000) == 10_000
    assert r._snap_rsi_live_lot(6_000) == 10_000
    # mid-ladder rounds to nearest rung
    assert r._snap_rsi_live_lot(16_000) == 20_000
    assert r._snap_rsi_live_lot(34_000) == 30_000
    # above the ceiling -> ceiling
    assert r._snap_rsi_live_lot(250_000) == 100_000
    assert r._snap_rsi_live_lot(100_001) == 100_000
    # every result is a clean 10k multiple in range
    for q in range(0, 300_000, 137):
        s = r._snap_rsi_live_lot(q)
        assert s % 10_000 == 0 and 10_000 <= s <= 100_000
_run("forex/runner: _snap_rsi_live_lot snaps to nearest 10k, clamped 10k-100k",
     test_snap_clamps_and_rounds_to_10k)


def test_ladder_is_exactly_the_ten_rungs_the_user_listed():
    import forex.runner as r
    rungs = sorted({r._snap_rsi_live_lot(q) for q in range(0, 200_000, 250)})
    assert rungs == [10_000, 20_000, 30_000, 40_000, 50_000,
                     60_000, 70_000, 80_000, 90_000, 100_000], rungs
_run("forex/runner: the reachable rungs are exactly 10k,20k,...,100k",
     test_ladder_is_exactly_the_ten_rungs_the_user_listed)


def test_entry_loop_applies_ladder_only_for_live_rsi_before_cost_gate():
    import forex.runner as r
    src = inspect.getsource(r._run_entries)
    # gated on live/live_eur AND strat_name == 'rsi'
    assert 'strat_name == "rsi"' in src
    assert '_snap_rsi_live_lot(qty)' in src
    # 2026-08-31: the ladder is now the RSI_LIVE_FIXED_RISK_EUR-is-None
    # fallback -- still gated to live RSI, still before the cost gate.
    assert 'if RSI_LIVE_FIXED_RISK_EUR:' in src and 'else:' in src[src.index('if RSI_LIVE_FIXED_RISK_EUR:'):]
    snap_at = src.index("_snap_rsi_live_lot(qty)")
    # anchor on the GATE's own commission lookup (qty-based), not the first
    # "_round_trip_cost" substring in the function -- the AI-proposal block
    # (2026-09-01) calls _round_trip_cost_quote_ccy on a nominal 10k lot
    # earlier, for advisory economics only, not the gate itself.
    cost_at = src.index("round_trip_cost = _round_trip_cost_quote_ccy(uic, qty, akey)")
    assert snap_at < cost_at, "lot sizing (ladder or fixed-risk cap) must run before the cost gate"
_run("forex/runner: _run_entries sizes live RSI lots (fixed-risk primary, ladder fallback) before the cost gate",
     test_entry_loop_applies_ladder_only_for_live_rsi_before_cost_gate)


def test_sim_rsi_is_untouched():
    import forex.runner as r
    src = inspect.getsource(r._run_entries)
    # the snap block is guarded by ACCOUNT_ENV in ("live", "live_eur");
    # SIM never reaches _snap_rsi_live_lot
    block = src[src.index("RSI on a real-money account snaps"):]
    guard_line = block[:block.index("_snap_rsi_live_lot(qty)")]
    assert 'ACCOUNT_ENV in ("live", "live_eur")' in guard_line
_run("forex/runner: SIM RSI sizing is not snapped (guard is live/live_eur only)",
     test_sim_rsi_is_untouched)


def test_other_live_strategies_not_snapped():
    import forex.runner as r
    src = inspect.getsource(r._run_entries)
    block = src[src.index("RSI on a real-money account snaps"):]
    guard_line = block[:block.index("_snap_rsi_live_lot(qty)")]
    # bb / donchian / etc must not hit this path
    assert 'strat_name == "rsi"' in guard_line
_run("forex/runner: only RSI is laddered on live (bb/others keep normal sizing)",
     test_other_live_strategies_not_snapped)


print(f"\n{BOLD}{'='*70}{RESET}")
passed = sum(1 for _, ok, _ in _results)
failed = [(n, e) for n, ok, e in _results if not ok]
for name, ok, err in _results:
    icon = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
    print(f"  [{icon}] {name}")
    if err:
        print(f"         {YELLOW}{err}{RESET}")
print(f"{BOLD}{'='*70}{RESET}")
if failed:
    print(f"{RED}{BOLD}  {len(failed)} / {len(_results)} TESTS FAILED{RESET}")
    sys.exit(1)
else:
    print(f"{GREEN}{BOLD}  ALL {len(_results)} TESTS PASSED{RESET}")
    sys.exit(0)
