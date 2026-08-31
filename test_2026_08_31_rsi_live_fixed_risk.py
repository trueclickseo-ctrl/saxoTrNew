"""
Regression test -- 2026-08-31 RSI real-money FIXED per-trade risk.

User: "one pair minimum 45 Euro" -- each RSI trade on the real-money
accounts should risk AT LEAST ~EUR45 if the stop is hit, uniform across
pairs regardless of stop width, instead of the equity-% + 10k-lot-ladder
combo (which gave ~EUR8 on MXNUSD vs ~EUR73 on GBPUSD).

- forex/runner.py: RSI_LIVE_FIXED_RISK_EUR = 45.0. When set, the entry
  loop passes strategy_rsi.size_position(risk_amount=<EUR45 in quote ccy>)
  and skips _snap_rsi_live_lot (only RSI_LIVE_LOT_MAX still caps).
- forex/strategy_rsi.py: size_position gains a `risk_amount` param; in
  that mode it rounds the qty UP to the 1,000-unit lot increment so the
  realised risk is >= the budget, never systematically under it.
- SIM is never affected (it passes neither risk_amount nor the live gate).
"""

import inspect
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

GREEN, RED, YELLOW, CYAN, RESET, BOLD = (
    "\033[92m", "\033[91m", "\033[96m", "\033[93m", "\033[0m", "\033[1m"
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


import forex.strategy_rsi as srsi
import forex.runner as r

ATR_MULT = srsi.ATR_STOP_MULT  # 1.5


# ═══════════════════════════════════════════════════════════════════════
section("1. size_position(risk_amount=...) targets the absolute budget, rounds UP")
# ═══════════════════════════════════════════════════════════════════════

def test_risk_amount_hits_budget_and_never_under():
    # quote-ccy budget 45, atr 0.006 -> stop_dist 0.009 -> raw 5000
    qty = srsi.size_position(0, 0.006, min_units=1000, risk_amount=45.0)
    realised = ATR_MULT * 0.006 * qty
    assert qty % 1000 == 0
    assert realised >= 45.0 - 1e-9, f"realised risk {realised} must be >= the 45 budget"
    assert realised < 45.0 + ATR_MULT * 0.006 * 1000, "should not overshoot by more than one lot"
_run("risk_amount mode: realised risk >= budget, within one lot increment", test_risk_amount_hits_budget_and_never_under)


def test_risk_amount_rounds_up_not_down():
    # raw = 45 / (1.5 * 0.007) = 4285.7 -> must round UP to 5000, not down to 4000
    qty = srsi.size_position(0, 0.007, min_units=1000, risk_amount=45.0)
    assert qty == 5000, f"expected ceil to 5000, got {qty}"
_run("risk_amount mode rounds the lot UP (a floor would under-risk the trade)", test_risk_amount_rounds_up_not_down)


def test_risk_amount_ignores_equity_and_pct():
    a = srsi.size_position(999_999, 0.006, min_units=1000, risk_amount=45.0, risk_pct=0.5)
    b = srsi.size_position(1, 0.006, min_units=1000, risk_amount=45.0)
    assert a == b, "risk_amount must override both account_equity and risk_pct"
_run("risk_amount overrides account_equity and risk_pct entirely", test_risk_amount_ignores_equity_and_pct)


def test_wide_stop_still_clears_one_lot():
    # very wide stop: raw < 1000 -> still returns the 1,000 minimum, never 0
    qty = srsi.size_position(0, 5.0, min_units=1000, risk_amount=45.0, block_below_min=True)
    assert qty == 1000
_run("risk_amount mode floors at the 1,000-unit Saxo minimum (never 0, never below)", test_wide_stop_still_clears_one_lot)


def test_sim_default_sizing_unchanged():
    # no risk_amount -> old behaviour exactly (floors down)
    q_old = srsi.size_position(100_000, 0.006, min_units=1000, risk_pct=0.0025)
    raw = 100_000 * 0.0025 / (ATR_MULT * 0.006)
    assert q_old == int(raw / 1000) * 1000, "SIM/default path must still floor, not ceil"
_run("default (no risk_amount) sizing is byte-for-byte unchanged -- still floors", test_sim_default_sizing_unchanged)


# ═══════════════════════════════════════════════════════════════════════
section("2. runner wiring: fixed-risk for live RSI, ladder is the fallback")
# ═══════════════════════════════════════════════════════════════════════

def test_constant_present_and_set():
    assert hasattr(r, "RSI_LIVE_FIXED_RISK_EUR")
    assert r.RSI_LIVE_FIXED_RISK_EUR == 45.0
_run("RSI_LIVE_FIXED_RISK_EUR is 45.0", test_constant_present_and_set)


def test_entry_loop_uses_fixed_risk_before_sizing():
    src = inspect.getsource(r._run_entries)
    assert 'RSI_LIVE_FIXED_RISK_EUR' in src
    assert 'rp_kw["risk_amount"] = RSI_LIVE_FIXED_RISK_EUR / _eur_per' in src
    # the fixed-risk block must sit before size_position() is called
    fr_at = src.index('rp_kw["risk_amount"] = RSI_LIVE_FIXED_RISK_EUR')
    size_at = src.index('strat_mod.size_position(')
    assert fr_at < size_at
_run("entry loop sets risk_amount from RSI_LIVE_FIXED_RISK_EUR before size_position()", test_entry_loop_uses_fixed_risk_before_sizing)


def test_ladder_is_the_else_branch_now():
    src = inspect.getsource(r._run_entries)
    # _snap_rsi_live_lot only runs when RSI_LIVE_FIXED_RISK_EUR is falsy
    seg = src[src.index('if RSI_LIVE_FIXED_RISK_EUR:'): src.index('_snap_rsi_live_lot(qty)') + 40]
    assert 'else:' in seg, "the 10k ladder must be the RSI_LIVE_FIXED_RISK_EUR-is-None fallback"
    assert 'min(qty, RSI_LIVE_LOT_MAX)' in seg, "fixed-risk mode still applies the max-lot ceiling"
_run("_snap_rsi_live_lot is now the fallback branch; fixed-risk mode still caps at RSI_LIVE_LOT_MAX", test_ladder_is_the_else_branch_now)


def test_fixed_risk_gated_to_live_rsi_only():
    src = inspect.getsource(r._run_entries)
    block = src[src.index('fixed ~EUR45 per-trade risk'): src.index('strat_mod.size_position(')]
    assert 'ACCOUNT_ENV in ("live", "live_eur")' in block
    assert 'strat_name == "rsi"' in block
_run("fixed-risk sizing is gated on live/live_eur AND strat_name=='rsi' (SIM + other strategies untouched)", test_fixed_risk_gated_to_live_rsi_only)


# ═══════════════════════════════════════════════════════════════════════
section("3. end-to-end: uniform ~EUR45 risk across a spread of pairs")
# ═══════════════════════════════════════════════════════════════════════

def test_uniform_risk_across_pairs():
    # (eur_per_quote_unit, atr_quote) for a spread of real pairs
    cases = {
        "EURUSD": (0.92, 0.0060), "GBPUSD": (0.92, 0.0075), "USDJPY": (0.0059, 0.90),
        "EURGBP": (1.17, 0.0035), "GBPJPY": (0.0059, 1.45), "EURCHF": (1.06, 0.0030),
        "GBPAUD": (0.60, 0.0130),
    }
    risks = []
    for sym, (epq, atr) in cases.items():
        budget_quote = 45.0 / epq
        qty = srsi.size_position(0, atr, min_units=1000, risk_amount=budget_quote, block_below_min=True)
        qty = min(qty, r.RSI_LIVE_LOT_MAX)
        realised_eur = ATR_MULT * atr * qty * epq
        risks.append((sym, realised_eur))
        assert 45.0 <= realised_eur <= 60.0, f"{sym}: realised EUR risk {realised_eur:.1f} outside [45, 60]"
    spread = max(x for _, x in risks) - min(x for _, x in risks)
    assert spread < 12.0, f"per-trade EUR risk spread across pairs is {spread:.1f}, expected tight (<12)"
_run("realised per-trade EUR risk is uniform (all in [45, 60], spread < EUR12) across a pair spread", test_uniform_risk_across_pairs)


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
