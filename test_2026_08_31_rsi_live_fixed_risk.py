"""
Regression test -- 2026-08-31 RSI real-money FIXED per-trade risk CAP.

User's explicit rules (a correction of the first cut, which treated €45 as
a minimum and rounded up):
  1. €45 = the MAXIMUM risk-if-stopped, uniform across pairs. Never a
     floor or a target.
  2. Round the lot DOWN to Saxo's 1,000-unit increment.
  3. If even one min-lot would risk more than €45 -> SKIP the trade
     (size_position returns 0).
  4. The round-trip commission stays a SEPARATE edge/cost filter
     (MIN_EDGE_TO_COST_RATIO) -- not folded into sizing.

- forex/strategy_rsi.py: size_position(risk_amount=<€45 in quote ccy>)
  rounds DOWN and returns 0 below one lot.
- forex/runner.py: RSI_LIVE_FIXED_RISK_EUR = 45.0; the live-RSI entry path
  converts it to the pair's quote ccy via _eur_per_unit and SKIPS the
  trade if that rate is unavailable (no %-based fallback on real money).
- SIM is never affected (passes neither risk_amount nor the live gate).
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
section("1. size_position(risk_amount=...) is a CEILING: rounds down, skips if a lot exceeds it")
# ═══════════════════════════════════════════════════════════════════════

def test_realised_risk_never_exceeds_the_cap():
    # budget 45 quote-ccy, atr 0.006 -> stop_dist 0.009 -> raw 5000 exactly
    for atr in (0.006, 0.0061, 0.0075, 0.004, 0.011):
        qty = srsi.size_position(0, atr, min_units=1000, risk_amount=45.0)
        realised = ATR_MULT * atr * qty
        assert qty % 1000 == 0
        assert realised <= 45.0 + 1e-9, f"atr={atr}: realised {realised:.2f} exceeds the 45 cap"
_run("risk_amount mode: realised risk is always <= the cap", test_realised_risk_never_exceeds_the_cap)


def test_risk_amount_rounds_down_not_up():
    # raw = 45 / (1.5 * 0.007) = 4285.7 -> must floor to 4000, not ceil to 5000
    qty = srsi.size_position(0, 0.007, min_units=1000, risk_amount=45.0)
    assert qty == 4000, f"expected floor to 4000, got {qty}"
_run("risk_amount mode rounds the lot DOWN (a ceil would breach the cap)", test_risk_amount_rounds_down_not_up)


def test_one_lot_over_cap_is_skipped():
    # wide stop: raw = 45 / (1.5 * 0.05) = 600 < 1000 -> one lot risks
    # 1.5*0.05*1000 = 75 > 45 -> must return 0 (skip), never floor up
    qty = srsi.size_position(0, 0.05, min_units=1000, risk_amount=45.0)
    assert qty == 0, f"a pair whose min lot risks > €45 must be skipped, got {qty}"
_run("risk_amount mode returns 0 when even one min-lot would breach the cap", test_one_lot_over_cap_is_skipped)


def test_exactly_one_lot_fits():
    # raw = 45 / (1.5 * 0.030) = 1000.0 exactly -> one lot, realised == 45
    qty = srsi.size_position(0, 0.030, min_units=1000, risk_amount=45.0)
    assert qty == 1000
    assert abs(ATR_MULT * 0.030 * qty - 45.0) < 1e-9
_run("risk_amount mode keeps a trade whose single lot lands exactly on the cap", test_exactly_one_lot_fits)


def test_risk_amount_ignores_equity_and_pct():
    a = srsi.size_position(999_999, 0.006, min_units=1000, risk_amount=45.0, risk_pct=0.5)
    b = srsi.size_position(1, 0.006, min_units=1000, risk_amount=45.0)
    assert a == b, "risk_amount must override both account_equity and risk_pct"
_run("risk_amount overrides account_equity and risk_pct entirely", test_risk_amount_ignores_equity_and_pct)


def test_sim_default_sizing_unchanged():
    # no risk_amount -> old behaviour exactly (floors down off equity * pct)
    q_old = srsi.size_position(100_000, 0.006, min_units=1000, risk_pct=0.0025)
    raw = 100_000 * 0.0025 / (ATR_MULT * 0.006)
    assert q_old == int(raw / 1000) * 1000, "SIM/default path must still floor off equity*pct"
_run("default (no risk_amount) sizing is byte-for-byte unchanged", test_sim_default_sizing_unchanged)


# ═══════════════════════════════════════════════════════════════════════
section("2. runner wiring: fixed-risk cap for live RSI, ladder is the fallback")
# ═══════════════════════════════════════════════════════════════════════

def test_constant_present_and_set():
    assert hasattr(r, "RSI_LIVE_FIXED_RISK_EUR")
    assert r.RSI_LIVE_FIXED_RISK_EUR == 45.0
_run("RSI_LIVE_FIXED_RISK_EUR is 45.0", test_constant_present_and_set)


def test_entry_loop_sets_risk_amount_before_sizing():
    src = inspect.getsource(r._run_entries)
    assert 'RSI_LIVE_FIXED_RISK_EUR' in src
    assert 'rp_kw["risk_amount"] = RSI_LIVE_FIXED_RISK_EUR / _eur_per' in src
    fr_at = src.index('rp_kw["risk_amount"] = RSI_LIVE_FIXED_RISK_EUR')
    size_at = src.index('strat_mod.size_position(')
    assert fr_at < size_at, "risk_amount must be set before size_position() is called"
_run("entry loop sets risk_amount from RSI_LIVE_FIXED_RISK_EUR before size_position()",
     test_entry_loop_sets_risk_amount_before_sizing)


def test_missing_eur_rate_skips_the_trade():
    src = inspect.getsource(r._run_entries)
    seg = src[src.index('_eur_per = _eur_per_unit('): src.index('rp_kw["risk_amount"] = RSI_LIVE_FIXED_RISK_EUR')]
    assert 'if not _eur_per' in seg and 'continue' in seg, (
        "a missing EUR conversion rate must SKIP the live-RSI trade, not fall back to %-based sizing"
    )
_run("live RSI trade is skipped when the €45 cap can't be converted to the quote currency",
     test_missing_eur_rate_skips_the_trade)


def test_ladder_is_the_fallback_branch():
    src = inspect.getsource(r._run_entries)
    seg = src[src.index('if RSI_LIVE_FIXED_RISK_EUR:'): src.index('_snap_rsi_live_lot(qty)') + 40]
    assert 'else:' in seg, "the 10k ladder must be the RSI_LIVE_FIXED_RISK_EUR-is-None fallback"
    assert 'min(qty, RSI_LIVE_LOT_MAX)' in seg, "fixed-risk mode still keeps the max-lot sanity backstop"
_run("_snap_rsi_live_lot is the fallback branch; fixed-risk mode keeps only the RSI_LIVE_LOT_MAX backstop",
     test_ladder_is_the_fallback_branch)


def test_fixed_risk_gated_to_live_rsi_only():
    src = inspect.getsource(r._run_entries)
    end = src.index('RSI_LIVE_FIXED_RISK_EUR):')
    block = src[src.rindex('if (', 0, end): end + len('RSI_LIVE_FIXED_RISK_EUR):')]
    assert 'ACCOUNT_ENV in ("live", "live_eur")' in block, block
    assert 'strat_name == "rsi"' in block, block
_run("fixed-risk sizing is gated on live/live_eur AND strat_name=='rsi' (SIM + other strategies untouched)",
     test_fixed_risk_gated_to_live_rsi_only)


# ═══════════════════════════════════════════════════════════════════════
section("3. end-to-end: per-trade EUR risk stays at or below €45 across a pair spread")
# ═══════════════════════════════════════════════════════════════════════

def test_risk_capped_across_pairs():
    # (eur_per_quote_unit, atr_quote) for a spread of real pairs
    cases = {
        "EURUSD": (0.92, 0.0060), "GBPUSD": (0.92, 0.0075), "USDJPY": (0.0059, 0.90),
        "EURGBP": (1.17, 0.0035), "GBPJPY": (0.0059, 1.45), "EURCHF": (1.06, 0.0030),
        "GBPAUD": (0.60, 0.0130),
    }
    kept = []
    for sym, (epq, atr) in cases.items():
        budget_quote = 45.0 / epq
        qty = srsi.size_position(0, atr, min_units=1000, risk_amount=budget_quote)
        if qty == 0:
            continue  # legitimately skipped -- one lot would breach the cap
        qty = min(qty, r.RSI_LIVE_LOT_MAX)
        realised_eur = ATR_MULT * atr * qty * epq
        kept.append((sym, realised_eur))
        assert realised_eur <= 45.0 + 1e-6, f"{sym}: realised €{realised_eur:.1f} exceeds the €45 cap"
    assert kept, "at least some pairs should still be tradeable under the cap"
    # kept trades cluster just under the cap (within one lot's worth of risk)
    assert all(x > 20.0 for _, x in kept), f"kept trades should still be meaningful: {kept}"
_run("realised per-trade EUR risk is <= €45 for every kept pair; over-cap pairs are skipped",
     test_risk_capped_across_pairs)


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
