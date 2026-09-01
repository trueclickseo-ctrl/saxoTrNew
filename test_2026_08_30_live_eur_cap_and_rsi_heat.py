"""
Regression tests -- 2026-08-30.

Explicit user decisions (AskUserQuestion) ahead of an 18,000 SEK deposit on
Mon 2026-09-01, after verifying live that the real POOLED balance is
~15,800 SEK (~EUR 1,400), reported in SEK, shared across all 3 LIVE
sub-accounts ('Equity 15,867 -> sizing off 6,000' in both scheduled-run
logs) -- NOT ~15,800 EUR.

  1. forex_live_eur risk_equity_eur: 6,000 -> 8,000 EUR  (config/capital.json)
     ~2.5x the post-deposit real balance -- deliberate leverage for bigger
     natural size / better commission ratio. User declined 1:1 (3,000) and
     5x (15,000). Risk % unchanged at 0.75%.

  2. RSI-only portfolio-heat cap: 8% instead of the shared 6%
     (_HEAT_LIMIT_BY_STRATEGY in forex/runner.py). Lets the RSI(2) pullback
     book run ~10 concurrent positions at 0.75% risk each. Every other
     strategy -- and the SEK LIVE account (bb) on the same pooled balance --
     keeps 6%. Saxo's real 50% margin gate is still the hard backstop.
"""

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
        r = fn()
        _results.append((name, True if r is None else bool(r), None))
    except Exception as e:
        _results.append((name, False, f"{type(e).__name__}: {e}"))


def section(t):
    print(f"\n{BOLD}{CYAN}{'-'*70}{RESET}\n{BOLD}{CYAN}  {t}{RESET}\n{BOLD}{CYAN}{'-'*70}{RESET}")


# ═══════════════════════════════════════════════════════════════════════
section("1. forex_live_eur risk_equity_eur = 8,000 EUR")
# ═══════════════════════════════════════════════════════════════════════

def test_config_value_is_8000():
    import json
    d = json.load(open(os.path.join(BASE_DIR, "config", "capital.json")))
    assert d["strategies"]["forex_live_eur"]["risk_equity_eur"] == 8000
_run("config/capital.json: forex_live_eur.risk_equity_eur == 8000", test_config_value_is_8000)


def test_loader_returns_8000():
    import atos.capital_config as c
    assert c.forex_live_eur_risk_equity_eur() == 8000.0
_run("atos.capital_config.forex_live_eur_risk_equity_eur() == 8000.0", test_loader_returns_8000)


def test_sek_account_cap():
    # 2026-08-30 this asserted the SEK cap "must not move" (the EUR-only raise).
    # 2026-09-01: after the 20,000 SEK deposit the SEK cap WAS moved
    # 15,000 -> 35,000 by explicit user decision (Option A) to size off the
    # now-real ~35,800 SEK pool. EUR cap left at 8,000.
    import atos.capital_config as c
    assert c.forex_live_risk_equity_sek() == 35000.0, "SEK LIVE cap should be 35,000 (2026-09-01 deposit)"
_run("forex_live (SEK) risk_equity_sek == 35,000 (2026-09-01 deposit)", test_sek_account_cap)


def test_live_risk_pct_still_075():
    import forex.runner as r
    assert r.LIVE_RISK_PCT_OVERRIDE == 0.0075, "risk % must stay 0.75% -- only the equity base moved"
_run("LIVE_RISK_PCT_OVERRIDE unchanged at 0.75%", test_live_risk_pct_still_075)


# ═══════════════════════════════════════════════════════════════════════
section("2. RSI-only 8% portfolio-heat cap")
# ═══════════════════════════════════════════════════════════════════════

def test_heat_override_table():
    import forex.runner as r
    assert r._HEAT_LIMIT_BY_STRATEGY == {"rsi": 0.08}, r._HEAT_LIMIT_BY_STRATEGY
    assert r.PORTFOLIO_HEAT_LIMIT == 0.06, "the shared default must stay 6%"
_run("_HEAT_LIMIT_BY_STRATEGY == {'rsi': 0.08}, PORTFOLIO_HEAT_LIMIT still 0.06",
     test_heat_override_table)


def _heat_env(monkey_rate=0.86):
    """Context: LIVE env + a fixed EUR conversion rate, no network."""
    import forex.runner as r
    st = {"rate": r._eur_per_unit, "env": r.ACCOUNT_ENV}
    r._eur_per_unit = lambda ccy, akey=None: monkey_rate
    r.ACCOUNT_ENV = "live_eur"
    return r, st


def _restore(r, st):
    r._eur_per_unit = st["rate"]
    r.ACCOUNT_ENV = st["env"]


def test_rsi_allowed_between_6_and_8_pct():
    r, st = _heat_env()
    try:
        # One position, |entry-stop|*qty*rate / equity = 0.10*100k*0.86/122000
        #   = 8600 / 122000 = 7.05% -- over 6%, under 8%.
        positions = {"rsi:EURUSD": {"entry_price": 1.10, "stop_price": 1.00,
                                    "quantity": 100_000, "sized_under_cap": True}}
        eq = 122_000.0
        assert r._portfolio_heat_pct(positions, eq) > 0.06
        assert r._portfolio_heat_pct(positions, eq) < 0.08
        assert r._heat_allows_entry(positions, eq, "rsi") is True, (
            "RSI must still be allowed at ~7% heat (its cap is 8%)")
    finally:
        _restore(r, st)
_run("_heat_allows_entry(..., 'rsi') allows a new entry at ~7% heat (6% < h < 8%)",
     test_rsi_allowed_between_6_and_8_pct)


def test_non_rsi_blocked_at_same_7_pct():
    r, st = _heat_env()
    try:
        positions = {"bb:EURUSD": {"entry_price": 1.10, "stop_price": 1.00,
                                   "quantity": 100_000, "sized_under_cap": True}}
        eq = 122_000.0
        assert r._heat_allows_entry(positions, eq, "bb") is False, (
            "bb keeps the 6% cap -- must be blocked at ~7% heat")
        # and the default (no strat_name) also keeps 6%
        assert r._heat_allows_entry(positions, eq) is False
    finally:
        _restore(r, st)
_run("_heat_allows_entry(..., 'bb') and the default still block at ~7% heat (6% cap)",
     test_non_rsi_blocked_at_same_7_pct)


def test_rsi_still_blocked_above_8_pct():
    r, st = _heat_env()
    try:
        positions = {"rsi:EURUSD": {"entry_price": 1.10, "stop_price": 1.00,
                                    "quantity": 100_000, "sized_under_cap": True}}
        eq = 90_000.0   # 8600/90000 = 9.6% -- over the RSI 8% cap too
        assert r._portfolio_heat_pct(positions, eq) > 0.08
        assert r._heat_allows_entry(positions, eq, "rsi") is False, (
            "even RSI must be blocked once heat clears its own 8% cap")
    finally:
        _restore(r, st)
_run("_heat_allows_entry(..., 'rsi') still blocks once heat exceeds 8%",
     test_rsi_still_blocked_above_8_pct)


def test_call_site_passes_strat_name():
    import inspect
    import forex.runner as r
    src = inspect.getsource(r._run_entries)
    assert "_heat_allows_entry(positions, equity, strat_name)" in src, (
        "the _run_entries call site must thread strat_name through so the "
        "RSI override actually takes effect")
_run("_run_entries passes strat_name into _heat_allows_entry", test_call_site_passes_strat_name)


def test_sim_still_never_blocks_even_for_rsi():
    import forex.runner as r
    st = {"rate": r._eur_per_unit, "env": r.ACCOUNT_ENV}
    r._eur_per_unit = lambda ccy, akey=None: 0.86
    r.ACCOUNT_ENV = "sim"
    try:
        positions = {"rsi:EURUSD": {"entry_price": 1.10, "stop_price": 1.00,
                                    "quantity": 100_000, "sized_under_cap": True}}
        assert r._heat_allows_entry(positions, 90_000.0, "rsi") is True, (
            "SIM stays fully disabled regardless of strategy -- unchanged from 2026-08-21")
    finally:
        r._eur_per_unit = st["rate"]
        r.ACCOUNT_ENV = st["env"]
_run("SIM heat cap still never blocks, RSI included", test_sim_still_never_blocks_even_for_rsi)


print(f"\n{BOLD}{'='*70}{RESET}")
passed = sum(1 for _, ok, _ in _results if ok)
for name, ok, err in _results:
    icon = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
    print(f"  [{icon}] {name}" + (f"\n         {YELLOW}{err}{RESET}" if err else ""))
print(f"{BOLD}{'='*70}{RESET}")
if passed == len(_results):
    print(f"{GREEN}{BOLD}  ALL {passed} TESTS PASSED{RESET}")
    sys.exit(0)
print(f"{RED}{BOLD}  {len(_results)-passed} / {len(_results)} FAILED{RESET}")
sys.exit(1)
