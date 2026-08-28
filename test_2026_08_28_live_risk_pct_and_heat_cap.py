"""
Regression tests -- 2026-08-28 LIVE RISK_PCT raised to 0.75% + portfolio
heat cap re-enabled for LIVE/LIVE_EUR.

Explicit user decision, made after a real 34-cell minimum-account-size
analysis (17 HIGH_VOLUME_SYMBOLS pairs x rsi/bb, real Saxo ATR/cost)
showed 0.25%/0.50% clear BOTH the risk gate (block_below_min) and cost
gate (MIN_EDGE_TO_COST_RATIO=3.0x) together on 0/34 cells at any
realistic LIVE capital level -- 0.75% clears 14/34, chosen via
AskUserQuestion as the deliberate middle ground (not 1.00%'s 28/34,
to avoid inflating per-trade risk further than necessary).

Paired same-day: the portfolio-wide EUR-risk heat cap
(_heat_allows_entry(), limit 6%) had been a no-op since 2026-08-21
(always returned True, contrary to its own docstring's "should be
reinstated before trading live capital") -- re-enabled for LIVE/
LIVE_EUR only, specifically to guard against the correlated-position
risk that a higher RISK_PCT reintroduces. SIM stays disabled, unchanged
(original 2026-08-21 full-testing-breadth rationale untouched).
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
        result = fn()
        if result is None:
            result = True
        _results.append((name, bool(result), None))
    except Exception as e:
        _results.append((name, False, f"{type(e).__name__}: {e}"))


def section(title):
    print(f"\n{BOLD}{CYAN}{'-'*70}{RESET}")
    print(f"{BOLD}{CYAN}  {title}{RESET}")
    print(f"{BOLD}{CYAN}{'-'*70}{RESET}")


# ═══════════════════════════════════════════════════════════════════════
section("1. LIVE_RISK_PCT_OVERRIDE -- 0.75%, LIVE/LIVE_EUR only, SIM unaffected")
# ═══════════════════════════════════════════════════════════════════════

def test_live_risk_pct_override_is_075_pct():
    import forex.runner as r
    assert r.LIVE_RISK_PCT_OVERRIDE == 0.0075, (
        f"expected LIVE_RISK_PCT_OVERRIDE == 0.0075 (0.75%), got {r.LIVE_RISK_PCT_OVERRIDE}"
    )
_run("forex.runner.LIVE_RISK_PCT_OVERRIDE is exactly 0.0075 (0.75%)",
     test_live_risk_pct_override_is_075_pct)


def test_live_risk_pct_applies_only_to_live_accounts():
    import forex.runner as r
    r.ACCOUNT_ENV = "live"
    assert r._live_risk_pct() == 0.0075, "expected 0.0075 under ACCOUNT_ENV='live'"
    r.ACCOUNT_ENV = "live_eur"
    assert r._live_risk_pct() == 0.0075, "expected 0.0075 under ACCOUNT_ENV='live_eur'"
    r.ACCOUNT_ENV = "sim"
    assert r._live_risk_pct() is None, "SIM must be unaffected -- expected None (module's own RISK_PCT applies)"
_run("_live_risk_pct() returns 0.0075 for live/live_eur, None for sim (unchanged)",
     test_live_risk_pct_applies_only_to_live_accounts)


def test_bb_and_rsi_module_constants_unchanged():
    # The override lives in forex/runner.py, not the strategy modules --
    # bb.RISK_PCT/rsi.RISK_PCT themselves must still read the original
    # 0.25% (SIM's own sizing, and the value the override is layered on
    # top of via the risk_pct= kwarg, are unaffected).
    import forex.strategy_bb as bb
    import forex.strategy_rsi as rsi
    assert bb.RISK_PCT == rsi.RISK_PCT == 0.0025, (
        f"expected the module-level RISK_PCT constants to stay 0.0025, got bb={bb.RISK_PCT}, rsi={rsi.RISK_PCT}"
    )
_run("forex.strategy_bb.RISK_PCT / forex.strategy_rsi.RISK_PCT module constants are untouched (still 0.0025)",
     test_bb_and_rsi_module_constants_unchanged)


# ═══════════════════════════════════════════════════════════════════════
section("2. Portfolio heat cap -- re-enabled for LIVE/LIVE_EUR, SIM still disabled")
# ═══════════════════════════════════════════════════════════════════════

def test_heat_cap_blocks_for_live():
    import forex.runner as r
    # Monkeypatch _eur_per_unit to a fixed rate -- avoids any live network
    # call and the real ACCOUNT_ENV/token mismatch that occurs if
    # ACCOUNT_ENV is mutated directly without a full set_account_env('live')
    # (saxo_auth.get_valid_access_token(env=ACCOUNT_ENV) would try to use a
    # LIVE token against whatever BASE_URL is currently set, a genuine
    # 401-producing inconsistency confirmed earlier this session -- this
    # test only needs _heat_allows_entry()'s ACCOUNT_ENV branching, not a
    # real conversion rate).
    orig_rate_fn = r._eur_per_unit
    orig_env = r.ACCOUNT_ENV
    r._eur_per_unit = lambda ccy, akey=None: 0.86
    r.ACCOUNT_ENV = "live"
    try:
        # |entry-stop|*qty = 0.10*100,000 = 10,000 USD -> *0.86 = 8,600 EUR
        # heat = 8,600 / 100 equity = 8,600% -- far over the 6% limit.
        positions = {
            "bb:EURUSD": {"entry_price": 1.10, "stop_price": 1.00, "quantity": 100_000,
                          "sized_under_cap": True},
        }
        assert r._heat_allows_entry(positions, 100.0) is False, (
            "expected _heat_allows_entry() to return False (block) for LIVE once heat exceeds the 6% limit"
        )
    finally:
        r._eur_per_unit = orig_rate_fn
        r.ACCOUNT_ENV = orig_env
_run("_heat_allows_entry() actually blocks (returns False) for LIVE once heat >= PORTFOLIO_HEAT_LIMIT",
     test_heat_cap_blocks_for_live)


def test_heat_cap_stays_disabled_for_sim():
    import forex.runner as r
    orig_rate_fn = r._eur_per_unit
    orig_env = r.ACCOUNT_ENV
    r._eur_per_unit = lambda ccy, akey=None: 0.86
    r.ACCOUNT_ENV = "sim"
    try:
        positions = {
            "bb:EURUSD": {"entry_price": 1.10, "stop_price": 1.00, "quantity": 100_000,
                          "sized_under_cap": True},
        }
        assert r._heat_allows_entry(positions, 100.0) is True, (
            "SIM must stay unaffected -- expected True (not blocking) even with heat far over the limit"
        )
    finally:
        r._eur_per_unit = orig_rate_fn
        r.ACCOUNT_ENV = orig_env
_run("_heat_allows_entry() still returns True (never blocks) for SIM, unchanged from 2026-08-21",
     test_heat_cap_stays_disabled_for_sim)


def test_heat_cap_allows_entry_when_under_limit():
    import forex.runner as r
    orig_env = r.ACCOUNT_ENV
    r.ACCOUNT_ENV = "live"
    try:
        # No positions at all -- heat is 0%, must not block (no network
        # call needed here since _portfolio_heat_pct's loop never runs).
        assert r._heat_allows_entry({}, 1_441.0) is True, (
            "expected _heat_allows_entry() to allow entries (True) when heat is 0%"
        )
    finally:
        r.ACCOUNT_ENV = orig_env
_run("_heat_allows_entry() still allows entries for LIVE when heat is genuinely under the 6% limit",
     test_heat_cap_allows_entry_when_under_limit)


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
