"""
Regression test -- 2026-09-01 SIM per-trade notional cap.

config/capital.json account.sim_max_trade_notional_eur (=10,000, user):
SIM is for testing strategies, not size. Even at 0.25% risk, low-ATR forex
pairs sized to ~EUR 180k notional on a ~EUR 27,800 base (196,000x USDHKD);
forex+futures margin exposure hit EUR 5.4M. A SIM entry whose NOTIONAL
exceeds the cap is scaled down to it, then floored to the instrument
minimum -- the strategy's risk math is untouched. Forex + stocks + ETF.
FUTURES ARE EXEMPT (1 contract is already the floor).
"""

import inspect
import json
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

GREEN, RED, YELLOW, RESET, BOLD = "\033[92m", "\033[91m", "\033[93m", "\033[0m", "\033[1m"
_results = []


def _run(name, fn):
    try:
        fn()
        _results.append((name, True, None))
    except Exception as e:
        import traceback
        _results.append((name, False, f"{type(e).__name__}: {e}\n{traceback.format_exc()}"))


import atos.capital_config as CC
import atos_runner as ar


# ── config accessor ──────────────────────────────────────────────────────
def test_config_has_the_cap():
    cfg = json.load(open(os.path.join(BASE_DIR, "config", "capital.json")))
    assert cfg["account"]["sim_max_trade_notional_eur"] == 10000
    assert CC.sim_max_trade_notional_eur() == 10000.0


def test_accessor_handles_absent_or_zero(monkeypatch=None):
    real = CC._load
    try:
        CC._load = lambda: {"account": {}}
        assert CC.sim_max_trade_notional_eur() == 0.0
        CC._load = lambda: {"account": {"sim_max_trade_notional_eur": 0}}
        assert CC.sim_max_trade_notional_eur() == 0.0
        CC._load = lambda: {"account": {"sim_max_trade_notional_eur": None}}
        assert CC.sim_max_trade_notional_eur() == 0.0
    finally:
        CC._load = real


# ── stocks: _sim_cap_shares ──────────────────────────────────────────────
def _with_rate(fn):
    real = ar._rate_to_sek
    ar._rate_to_sek = lambda ccy: {"EUR": 11.5, "USD": 10.8}.get(ccy, 1.0)
    try:
        return fn()
    finally:
        ar._rate_to_sek = real


def test_stocks_cap_leaves_small_orders_alone():
    def _t():
        # PYPL: 26 sh @ $53.66, fx 10.8 -> ~15,065 SEK ~= EUR 1,310  (well under 10k)
        assert ar._sim_cap_shares(26, 53.66, 10.8) == 26
        # a 1-share order
        assert ar._sim_cap_shares(1, 500.0, 10.8) == 1
    _with_rate(_t)


def test_stocks_cap_scales_down_a_big_order():
    def _t():
        # 500 sh @ $166, fx 10.8 -> 896,400 SEK.  cap 10,000 EUR * 11.5 = 115,000 SEK
        # -> 115,000 / (166*10.8) ~= 64 shares
        capped = ar._sim_cap_shares(500, 166.0, 10.8)
        assert 60 <= capped <= 66, capped
        assert capped * 166.0 * 10.8 <= 10000 * 11.5 + 1
    _with_rate(_t)


def test_stocks_cap_disabled_is_a_noop():
    real = CC.sim_max_trade_notional_eur
    try:
        ar.CAP.sim_max_trade_notional_eur = lambda: 0.0
        assert ar._sim_cap_shares(999999, 166.0, 10.8) == 999999
    finally:
        ar.CAP.sim_max_trade_notional_eur = real


def test_stocks_cap_never_returns_zero():
    def _t():
        # even a single ultra-expensive share stays at 1 (can't go lower)
        assert ar._sim_cap_shares(1, 999999.0, 10.8) == 1
    _with_rate(_t)


def test_stocks_cap_bad_inputs_safe():
    for args in [(10, 0, 10.8), (10, 100, 0), (0, 100, 10.8), (10, None, 10.8)]:
        out = ar._sim_cap_shares(*args)
        assert isinstance(out, int)


def test_stocks_cap_wired_into_all_three_buy_paths():
    src = inspect.getsource(ar)
    assert src.count("_sim_cap_shares(") >= 4   # def + 3 call sites
    assert "_sim_cap_shares(int(slot_sek" in src          # US Reversion daily
    assert "_sim_cap_shares(int(slot_sek / (price_usd" in src  # intraday
    place_us = inspect.getsource(ar._place_us)
    assert '_sim_cap_shares(shares, price, _rate_to_sek("USD"))' in place_us
    # buy-side only
    assert 'if side == "Buy":\n        shares = _sim_cap_shares' in place_us


# ── forex runner hook ────────────────────────────────────────────────────
def test_forex_cap_hook_present_and_sim_only():
    import forex.runner as r
    src = inspect.getsource(r._run_entries)
    assert 'ACCOUNT_ENV == "sim"' in src and "sim_max_trade_notional_eur()" in src
    i_ai = src.index("_ai_apply_decision_to_qty(")
    i_cap = src.index("sim_max_trade_notional_eur()")
    i_cost = src.index("_round_trip_cost_quote_ccy(")
    assert i_ai < i_cap < i_cost, "cap must sit after the AI sizing hook and before the cost gate"
    block = src[i_cap - 400: i_cost]
    assert 'pair_info["min_units"]' in block   # floors to the instrument minimum
    assert "_eur_per_unit(" in block            # notional computed in EUR


def test_forex_cap_math_reproduction():
    # the inline logic: capped = max(int(cap_eur / eur_per_base), min_units)
    cap_eur, eur_per_base, min_units, qty = 10000.0, 0.92, 1000, 196000   # USDHKD-ish
    notional = qty * eur_per_base
    assert notional > cap_eur
    capped = max(int(cap_eur / eur_per_base), int(min_units))
    assert capped == 10869
    assert capped * eur_per_base <= cap_eur + eur_per_base


def test_futures_is_exempt():
    import futures.runner as fr
    src = inspect.getsource(fr)
    assert "sim_max_trade_notional_eur" not in src, "futures must NOT apply the notional cap"


# ── ETF ──────────────────────────────────────────────────────────────────
def test_etf_cap_in_enter_position():
    src = open(os.path.join(BASE_DIR, "saxo_etf_strategy", "core", "etf_executor.py"),
               encoding="utf-8").read()
    i_def = src.index("def _enter_position(")
    i_price = src.index("price    = signal.last_price", i_def)
    block = src[i_def:i_price]
    assert "sim_max_trade_notional_eur" in block
    assert "budget_ccy = _cap" in block


for _n, _f in list(globals().items()):
    if _n.startswith("test_") and callable(_f):
        _run(_n, _f)

print(f"\n{BOLD}{'='*66}{RESET}")
failed = [(n, e) for n, ok, e in _results if not ok]
for name, ok, err in _results:
    print(f"  [{GREEN}PASS{RESET}]" if ok else f"  [{RED}FAIL{RESET}]", name)
    if err:
        print(f"      {YELLOW}{err}{RESET}")
print(f"{BOLD}{'='*66}{RESET}")
if failed:
    print(f"{RED}{BOLD}  {len(failed)} / {len(_results)} FAILED{RESET}")
    sys.exit(1)
print(f"{GREEN}{BOLD}  ALL {len(_results)} TESTS PASSED{RESET}")
sys.exit(0)
