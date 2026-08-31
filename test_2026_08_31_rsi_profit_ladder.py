"""
Regression test — 2026-08-31 RSI(2) profit-protection ladder (opt-in).

An OPT-IN alternative to the always-on trailing + one-shot breakeven for
the RSI(2) book (user-specified design):

  >= 0.75 R  -> stop to entry + 0.10 R  (breakeven + costs)
  >= 1.00 R  -> stop to entry + 0.50 R  (lock +0.5 R)
  >= 1.25 R  -> stop to max(lock level, close - 1.0 x ATR_now)   [trail on]

R = initial entry-to-stop distance. Ratchet only. Primary exit (RSI
recovery / 2R broker TP / 12d time / hard stop) is unchanged.

OFF by default: PROFIT_LADDER_ACCOUNTS is empty, so no account's behaviour
changes until it's explicitly switched on after the backtest.
"""

import os
import sys
import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

GREEN, RED, YELLOW, CYAN, RESET, BOLD = (
    "\033[92m", "\033[91m", "\033[93m", "\033[96m", "\033[0m", "\033[1m"
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


import forex.runner as r

R_PRICE = 0.015          # entry 1.0, initial stop 0.985
LONG = {"direction": "Buy", "entry_price": 1.0, "initial_stop_price": 0.985,
        "stop_price": 0.985, "atr_at_entry": 0.01}
SHORT = {"direction": "Sell", "entry_price": 1.0, "initial_stop_price": 1.015,
         "stop_price": 1.015, "atr_at_entry": 0.01}


def _df(close, atr_half=0.001):
    n = 30
    return pd.DataFrame({"High": [close + atr_half] * n,
                         "Low":  [close - atr_half] * n,
                         "Close": [close] * n})


def _target(pos, r_mult):
    is_long = pos["direction"] == "Buy"
    close = pos["entry_price"] + (r_mult * R_PRICE if is_long else -r_mult * R_PRICE)
    return r._profit_ladder_target_stop(dict(pos), _df(close), "rsi")


# ═══════════════════════════════════════════════════════════════════════
section("1. Default OFF — no account behaviour changes")
# ═══════════════════════════════════════════════════════════════════════

def test_off_by_default():
    assert r.PROFIT_LADDER_ACCOUNTS == set(), "ladder must ship OFF for every account"
    assert not r._profit_ladder_active("rsi")
_run("PROFIT_LADDER_ACCOUNTS is empty and _profit_ladder_active('rsi') is False", test_off_by_default)


def test_activation_is_account_and_strategy_scoped():
    r.PROFIT_LADDER_ACCOUNTS = {"live_eur"}
    old_env = r.ACCOUNT_ENV
    try:
        r.ACCOUNT_ENV = "live_eur"
        assert r._profit_ladder_active("rsi")
        assert not r._profit_ladder_active("bb"), "only the rsi book, never another strategy"
        r.ACCOUNT_ENV = "sim"
        assert not r._profit_ladder_active("rsi"), "only the enabled account"
    finally:
        r.ACCOUNT_ENV = old_env
        r.PROFIT_LADDER_ACCOUNTS = set()
_run("activation is gated on BOTH account and strategy", test_activation_is_account_and_strategy_scoped)


# ═══════════════════════════════════════════════════════════════════════
section("2. Ladder rungs (long)")
# ═══════════════════════════════════════════════════════════════════════

def test_below_first_rung_no_change():
    assert _target(LONG, 0.50) is None
    assert _target(LONG, 0.74) is None
_run("< 0.75R -> no ladder stop yet", test_below_first_rung_no_change)


def test_breakeven_rung():
    t = _target(LONG, 0.80)
    assert abs(t - (1.0 + 0.10 * R_PRICE)) < 1e-9, f"expected entry + 0.10R, got {t}"
    assert t > 1.0, "breakeven rung must be strictly above entry (covers costs)"
_run("0.75–1.0R -> stop to entry + 0.10R (breakeven + costs)", test_breakeven_rung)


def test_lock_rung():
    t = _target(LONG, 1.05)
    assert abs(t - (1.0 + 0.50 * R_PRICE)) < 1e-9, f"expected entry + 0.50R, got {t}"
_run("1.0–1.25R -> stop to entry + 0.50R (locked profit)", test_lock_rung)


def test_trail_rung_never_below_lock():
    # deep in profit, ATR tiny -> trail sits well above the lock level
    t = _target(LONG, 3.0)
    assert t >= 1.0 + 0.50 * R_PRICE - 1e-9, "trail must never drop below the +0.5R lock"
    # right at activation with a wide ATR, lock is the floor
    pos = dict(LONG)
    close = 1.0 + 1.26 * R_PRICE
    wide = r._profit_ladder_target_stop(pos, _df(close, atr_half=0.02), "rsi")
    assert abs(wide - (1.0 + 0.50 * R_PRICE)) < 1e-9, "wide ATR at +1.25R -> floored at the lock level"
_run("≥1.25R -> trail = max(+0.5R lock, close − 1×ATR)", test_trail_rung_never_below_lock)


# ═══════════════════════════════════════════════════════════════════════
section("3. Ladder rungs (short) mirror long")
# ═══════════════════════════════════════════════════════════════════════

def test_short_side():
    assert _target(SHORT, 0.5) is None
    assert abs(_target(SHORT, 0.8) - (1.0 - 0.10 * R_PRICE)) < 1e-9
    assert abs(_target(SHORT, 1.05) - (1.0 - 0.50 * R_PRICE)) < 1e-9
    assert _target(SHORT, 3.0) <= 1.0 - 0.50 * R_PRICE + 1e-9
_run("short positions ladder symmetrically below entry", test_short_side)


# ═══════════════════════════════════════════════════════════════════════
section("4. _apply_profit_ladder_stop ratchets, never loosens")
# ═══════════════════════════════════════════════════════════════════════

def test_apply_ratchets_and_moves_local_stop():
    pos = dict(LONG)
    pos["stop_price"] = 0.985
    close = 1.0 + 1.05 * R_PRICE
    moved = r._apply_profit_ladder_stop("rsi:EURUSD", pos, _df(close), "rsi",
                                        akey="k", dry_run=True)
    assert moved is True
    assert abs(pos["stop_price"] - (1.0 + 0.50 * R_PRICE)) < 1e-6
_run("_apply_profit_ladder_stop moves the local stop_price up to the rung (dry_run)",
     test_apply_ratchets_and_moves_local_stop)


def test_apply_never_loosens():
    pos = dict(LONG)
    pos["stop_price"] = 1.0 + 0.90 * R_PRICE     # already tighter than the 1.05R lock rung (0.5R)
    close = 1.0 + 1.05 * R_PRICE
    moved = r._apply_profit_ladder_stop("rsi:EURUSD", pos, _df(close), "rsi",
                                        akey="k", dry_run=True)
    assert moved is False, "a rung that would LOOSEN the stop must be ignored"
    assert abs(pos["stop_price"] - (1.0 + 0.90 * R_PRICE)) < 1e-9
_run("a rung that would loosen the stop is a no-op", test_apply_never_loosens)


def test_missing_R_reference_is_safe():
    pos = {"direction": "Buy", "entry_price": 1.0, "stop_price": 0.985}  # no initial_stop_price, no atr_at_entry
    assert r._profit_ladder_target_stop(pos, _df(1.02), "rsi") is None
    assert r._apply_profit_ladder_stop("rsi:X", pos, _df(1.02), "rsi", akey="k", dry_run=True) is False
_run("no R reference (old position) -> ladder safely does nothing", test_missing_R_reference_is_safe)


# ═══════════════════════════════════════════════════════════════════════
section("5. Wiring: ladder replaces the generic trail + breakeven for rsi")
# ═══════════════════════════════════════════════════════════════════════

def test_run_exits_gates_the_generic_blocks():
    src = open(os.path.join(BASE_DIR, "forex", "runner.py"), encoding="utf-8").read()
    i = src.find("def _run_exits(")
    body = src[i: src.find("\n\ndef ", i + 10)]
    assert "_ladder_active = _profit_ladder_active(strat_name)" in body
    assert "if not _ladder_active and df is not None and hasattr(strat_mod, \"trailing_stop_update\")" in body, (
        "the generic trailing block must be skipped when the ladder is active"
    )
    assert "if _ladder_active:\n            _apply_profit_ladder_stop(" in body, (
        "breakeven call site must branch to the ladder when active"
    )
_run("_run_exits skips generic trail + breakeven when the ladder is active", test_run_exits_gates_the_generic_blocks)


def test_entry_records_initial_stop_price():
    src = open(os.path.join(BASE_DIR, "forex", "runner.py"), encoding="utf-8").read()
    assert '"initial_stop_price": sig["stop_price"]' in src, (
        "new positions must freeze the initial stop so R stays stable as stop_price ratchets"
    )
_run("_run_entries freezes initial_stop_price on every new position", test_entry_records_initial_stop_price)


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
