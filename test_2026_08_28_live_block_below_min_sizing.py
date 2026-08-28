"""
Regression tests -- 2026-08-28 LIVE/LIVE_EUR "block, don't force" sizing.

Explicit user proposal (verbatim pseudocode):
    risk_budget = equity x 0.25%
    raw_units = risk_budget / stop_distance
    quantity = floor(raw_units / 1,000) x 1,000
    if quantity < 1,000:
        BLOCK TRADE

Confirmed via real Saxo data before implementing: at current LIVE pilot
capital (6,000 SEK / 500 EUR, and even the EUR account's full 900 EUR
balance), 0/34 (pair x strategy) combinations on the 17-pair
HIGH_VOLUME_SYMBOLS universe naturally clear 1,000 units -- so this is a
deliberate, accepted near-total-halt tradeoff (confirmed with the user
via AskUserQuestion: "Implement it anyway, accept zero trades for now",
scoped to "LIVE and LIVE_EUR only").

size_position() in every forex strategy module gained a new
`block_below_min: bool = False` parameter -- default False preserves
the historical floor-up-to-min_units behavior everywhere (SIM
unaffected). forex/runner.py passes block_below_min=True only when
ACCOUNT_ENV is "live" or "live_eur", and skips the entry entirely
(logs, continues) when size_position() returns 0.
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


STRATEGY_MODULES = ["strategy", "strategy_bb", "strategy_cnn_lstm", "strategy_donchian",
                    "strategy_gap", "strategy_ml", "strategy_pullback", "strategy_rsi",
                    "strategy_supertrend", "strategy_zscore"]


# ═══════════════════════════════════════════════════════════════════════
section("1. size_position() -- block_below_min param on every strategy module")
# ═══════════════════════════════════════════════════════════════════════

def test_every_module_accepts_block_below_min():
    import importlib
    for modname in STRATEGY_MODULES:
        mod = importlib.import_module(f"forex.{modname}")
        # A tiny equity + a large ATR guarantees raw < min_units for any
        # module's own RISK_PCT/ATR_STOP_MULT -- exercises the "should
        # block" branch uniformly regardless of each module's own constants.
        result = mod.size_position(10.0, 100.0, 1000, block_below_min=True)
        assert result == 0, f"forex.{modname}.size_position(block_below_min=True) should return 0, got {result}"
        result_default = mod.size_position(10.0, 100.0, 1000)
        assert result_default == 1000, (
            f"forex.{modname}.size_position() default (block_below_min=False) must still floor "
            f"up to min_units for backward compatibility, got {result_default}"
        )
_run("every forex strategy module's size_position() accepts block_below_min and defaults to the old floor-up behavior",
     test_every_module_accepts_block_below_min)


def test_rsi_and_bb_block_vs_floor_agree_above_threshold():
    import forex.strategy_rsi as rsi_mod
    import forex.strategy_bb as bb_mod
    # A large equity where raw naturally clears min_units -- block_below_min
    # must be a no-op here (both branches return the identical real size).
    for mod in (rsi_mod, bb_mod):
        blocked = mod.size_position(2_000_000, 0.005, 1000, risk_pct=0.0025, block_below_min=True)
        floored = mod.size_position(2_000_000, 0.005, 1000, risk_pct=0.0025, block_below_min=False)
        assert blocked == floored and blocked > 1000, (
            f"{mod.__name__}: block_below_min must not change the result when raw already clears min_units "
            f"(got blocked={blocked}, floored={floored})"
        )
_run("size_position(block_below_min=True) is a no-op once raw units already clear the minimum",
     test_rsi_and_bb_block_vs_floor_agree_above_threshold)


# ═══════════════════════════════════════════════════════════════════════
section("2. forex/runner.py -- LIVE/LIVE_EUR pass block_below_min=True, SIM doesn't")
# ═══════════════════════════════════════════════════════════════════════

def test_runner_sets_block_below_min_only_for_live_accounts():
    import forex.runner as r
    import inspect
    src = inspect.getsource(r)
    assert 'rp_kw["block_below_min"] = True' in src, (
        "expected forex/runner.py to set block_below_min=True somewhere in its entry-sizing path"
    )
    assert 'ACCOUNT_ENV in ("live", "live_eur")' in src, (
        "expected the block_below_min assignment to be gated on ACCOUNT_ENV in ('live', 'live_eur')"
    )
_run("forex/runner.py gates block_below_min=True on ACCOUNT_ENV in ('live', 'live_eur') only",
     test_runner_sets_block_below_min_only_for_live_accounts)


def test_runner_skips_entry_on_zero_quantity():
    import forex.runner as r
    import inspect
    src = inspect.getsource(r)
    assert "if qty <= 0:" in src, "expected an explicit qty<=0 skip check after size_position() is called"
_run("forex/runner.py has an explicit skip when size_position() returns 0 (not passed through to order placement)",
     test_runner_skips_entry_on_zero_quantity)


# ═══════════════════════════════════════════════════════════════════════
section("3. Sanity -- current LIVE pilot capital genuinely can't clear the threshold")
# ═══════════════════════════════════════════════════════════════════════

def test_current_live_caps_block_every_high_volume_pair():
    # Documents the real, verified consequence of this change at today's
    # capital levels -- if this ever starts passing (i.e. some pair/
    # strategy combo naturally clears 1,000 units), that's a sign capital
    # or RISK_PCT has grown enough that this is worth re-visiting with
    # the user, not a test to "fix" by loosening.
    import atos.capital_config as cc
    import forex.strategy_rsi as rsi_mod
    import forex.strategy_bb as bb_mod
    sek_cap = cc.forex_live_risk_equity_sek()
    eur_cap = cc.forex_live_eur_risk_equity_eur()
    assert sek_cap <= 10_000, f"expected the SEK pilot cap to still be small (~6,000), got {sek_cap}"
    assert eur_cap <= 1_000, f"expected the EUR pilot cap to still be small (~500), got {eur_cap}"
    # A representative wide-stop pair (~0.0044 EURUSD-scale ATR, rsi's
    # 1.5x multiplier) at the SEK cap converted at a plausible ~10 SEK/EUR
    # rate -- comfortably above any real ATR seen on the 17 HIGH_VOLUME
    # pairs (confirmed live 2026-08-28, max raw_units was ~288 of 1,000
    # needed, EURGBP/rsi at the SEK cap).
    eq_quote_plausible_upper_bound = (sek_cap / 8.0)  # generous SEK/EUR floor
    qty = rsi_mod.size_position(eq_quote_plausible_upper_bound, 0.0027, 1000,
                                 risk_pct=0.0025, block_below_min=True)
    assert qty == 0, (
        f"expected the SEK cap to still be far below what's needed to clear 1,000 units "
        f"on a representative pair, got qty={qty} -- if this now passes, capital has "
        f"genuinely grown enough to revisit this with the user"
    )
_run("current LIVE pilot capital (SEK ~6,000 / EUR ~500) still can't naturally clear 1,000 units on a representative pair",
     test_current_live_caps_block_every_high_volume_pair)


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
