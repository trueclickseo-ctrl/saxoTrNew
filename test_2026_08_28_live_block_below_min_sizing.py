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
section("3. Sanity -- LIVE pilot capital unlocks SOME cells, not all (2026-08-28 raise)")
# ═══════════════════════════════════════════════════════════════════════

def test_live_caps_reflect_the_2026_08_28_pooled_balance_raise():
    # 2026-08-28: confirmed live via Saxo's own /port/v1/balances/me that
    # the SEK/EUR/USD sub-accounts share ONE real pooled cash balance
    # (~15,770 SEK that day) -- there was never a separate 6,000 SEK +
    # 500 EUR, both were artificial software slices of the same real
    # total. Explicit user decision (AskUserQuestion): raise both caps to
    # reflect that real pool (15,000 SEK / 1,350 EUR, both strategies
    # sharing it), paired with LIVE_RISK_PCT_OVERRIDE=0.75%.
    # 2026-08-29: EUR cap raised again 1,350 -> 6,000 (explicit user
    # decision) -- 1,000-unit minimum-lot trades were commission-dominated
    # (flat ~5 EUR round-trip vs ~10 EUR risk budget). See capital.json's
    # forex_live_eur comment. SEK cap unchanged; risk % unchanged at 0.75%.
    import atos.capital_config as cc
    sek_cap = cc.forex_live_risk_equity_sek()
    eur_cap = cc.forex_live_eur_risk_equity_eur()
    assert sek_cap == 15_000.0, f"expected the 2026-08-28 15,000 SEK cap, got {sek_cap}"
    assert eur_cap == 6_000.0, f"expected the 2026-08-29 6,000 EUR cap, got {eur_cap}"
_run("atos.capital_config: LIVE caps are 15,000 SEK / 6,000 EUR (2026-08-29 EUR raise)",
     test_live_caps_reflect_the_2026_08_28_pooled_balance_raise)


def test_some_but_not_all_cells_clear_both_gates_at_new_caps():
    # This is the actual point of the 2026-08-28 raise: unlock SOME of the
    # 34 (17 HIGH_VOLUME_SYMBOLS pairs x rsi/bb) cells, not all of them --
    # a deliberate moderate step (0.75% risk, not 1.00%'s 28/34). Uses
    # real live Saxo ATR/cost, so the exact count drifts day to day as
    # market conditions move (11/34 confirmed the day this was written) --
    # asserting a loose range here, not an exact count, so normal ATR
    # movement doesn't make this test flaky. If this ever shows 0/34
    # (the raise stopped working) or 34/34 (capital/risk grew enough for
    # full coverage), that's worth a fresh conversation with the user,
    # not a silent "fix" to this test.
    import math
    import forex.runner as r
    from forex.strategy import _atr
    from forex.universe import get_pair, HIGH_VOLUME_SYMBOLS
    import forex.strategy_rsi as rsi_mod
    import forex.strategy_bb as bb_mod
    import atos.capital_config as cc

    r.set_account_env("sim")
    sek_cap = cc.forex_live_risk_equity_sek()
    risk_pct = 0.0075
    tp_rr = r.DEFAULT_TP_RR
    min_ratio = r.MIN_EDGE_TO_COST_RATIO
    strat_mult = {"rsi": (1.5, rsi_mod), "bb": (2.0, bb_mod)}

    both_pass = 0
    total = 0
    for sym in sorted(HIGH_VOLUME_SYMBOLS):
        pinfo = get_pair(sym)
        quote = sym[3:6]
        uic = pinfo["uic"]
        min_units = pinfo["min_units"]
        df = r._fetch_history(uic, count=60)
        if df is None or len(df) < 20:
            continue
        atr = float(_atr(df["High"], df["Low"], df["Close"]).iloc[-1])
        sek_rate = r._sek_per_unit(quote)
        if not sek_rate:
            continue
        eq_sek_quote = sek_cap / sek_rate
        cost_quote = r._round_trip_cost_quote_ccy(uic, 1000, None)
        if cost_quote is None:
            continue
        for strat_name, (mult, mod) in strat_mult.items():
            total += 1
            qty = mod.size_position(eq_sek_quote, atr, min_units, risk_pct=risk_pct, block_below_min=True)
            if qty <= 0:
                continue
            stop_dist = mult * atr
            target_dist = stop_dist * tp_rr
            needed_qty = max(min_units, math.ceil((min_ratio * cost_quote) / target_dist / min_units) * min_units)
            if qty >= needed_qty:
                both_pass += 1

    assert total >= 30, f"expected to successfully evaluate most of the 34 cells, only got {total}"
    assert both_pass > 0, (
        f"expected at least 1/{total} cells to clear both gates at the new caps -- "
        f"if this is 0, the 2026-08-28 capital raise isn't achieving its purpose, worth a fresh look"
    )
    assert both_pass < total, (
        f"expected LESS than all {total} cells to clear -- this was a deliberate moderate raise "
        f"(0.75% risk, not 1.00%), not meant for full 34/34 coverage; if this is now {total}/{total}, "
        f"capital/risk has grown enough to revisit the decision with the user"
    )
_run("Some (not all) of the 34 cells clear both gates at the new 15,000 SEK / 0.75% risk setting (real live data)",
     test_some_but_not_all_cells_clear_both_gates_at_new_caps)


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
