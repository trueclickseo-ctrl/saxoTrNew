"""
Regression test -- 2026-09-02 US Blend / US Reversion duplicate-trade churn.

Bug (confirmed live in data/atos_live.db, DELL 2026-09-02, MTB 2026-09-01):
run_us_momentum() (US Blend) built its working position set from
    {t for t in open_trades if t.get("market_group") == "US Equities"}
which also swept in every US Reversion position (same market_group, different
strategy). The delta rebalance then saw Reversion's dip-buys as Blend holdings
that were NOT in the momentum / low-vol target and emitted Sell orders for
them. _place_us("Sell", cur_trade=<reversion row>) closed the Reversion DB row
tagged exit_reason="momentum_rebalance"; the next Reversion scan re-bought the
freed slot at the same price -> a buy/sell churn loop, pure commission + spread
bleed (~10 SEK/cycle; ~12 cycles on MTB + one real -97 SEK adverse round-trip).

Fixes in atos_runner.py:
  * run_us_momentum: us_open scoped by strategy == "US Blend" (matches how
    run_us_reversion already scopes its own set);
  * run_us_momentum: rev_held names excluded from the buy `priority` list;
  * run_us_reversion (daily): candidates exclude names US Blend holds;
  * run_intraday_cycle: already_held includes US Blend holdings too.
"""

import ast
import inspect
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


import atos_runner as ar


# A realistic mixed open-book: Blend holds MSFT/NVDA, Reversion holds DELL/MTB.
MIXED_BOOK = [
    {"ticker": "MSFT", "strategy": "US Blend",     "market_group": "US Equities", "shares": 4},
    {"ticker": "NVDA", "strategy": "US Blend",     "market_group": "US Equities", "shares": 2},
    {"ticker": "DELL", "strategy": "US Reversion", "market_group": "US Equities", "shares": 3},
    {"ticker": "MTB",  "strategy": "US Reversion", "market_group": "US Equities", "shares": 6},
]


# ── the actual scoping expression used by run_us_momentum ────────────────
def test_blend_working_set_excludes_reversion_positions():
    us_open = {t["ticker"]: t for t in MIXED_BOOK if t.get("strategy") == "US Blend"}
    assert set(us_open) == {"MSFT", "NVDA"}, us_open
    assert "DELL" not in us_open and "MTB" not in us_open


def test_rev_held_set_is_reversion_only():
    rev_held = {t["ticker"] for t in MIXED_BOOK if t.get("strategy") == "US Reversion"}
    assert rev_held == {"DELL", "MTB"}, rev_held


# ── source guarantees: no regression back to market_group scoping ────────
def test_run_us_momentum_scopes_by_strategy_not_market_group():
    src = inspect.getsource(ar.run_us_momentum)
    assert 'if t.get("strategy") == "US Blend"' in src, \
        "us_open must be scoped to Blend's own rows"
    # the old, buggy expression must be gone from this function
    assert 'if t.get("market_group") == "US Equities"' not in src, \
        "market_group scoping re-introduced -- Blend will stomp Reversion again"


def test_run_us_momentum_skips_reversion_held_names_on_buy():
    src = inspect.getsource(ar.run_us_momentum)
    assert "rev_held" in src
    i_priority = src.index("priority = []")
    i_approve  = src.index("USM.plan_rebalance(")
    block = src[i_priority:i_approve]
    assert "if tk in rev_held:" in block and "continue" in block, \
        "buy priority list must skip names US Reversion already holds"


def test_run_us_reversion_excludes_blend_held_names():
    src = inspect.getsource(ar.run_us_reversion)
    assert 'if t.get("strategy") == "US Blend"' in src
    assert "blend_held" in src
    i_scan = src.index("USR.scan(")
    tail = src[i_scan:i_scan + 400]
    assert "blend_held" in tail, "candidate filter must drop Blend-held names"


def test_intraday_already_held_includes_blend():
    src = inspect.getsource(ar.run_intraday_cycle)
    i = src.index("already_held")
    block = src[i:i + 300]
    assert 'strategy") == "US Blend"' in block, \
        "intraday reversion must also skip names US Blend holds"


# ── exit_reason 'momentum_rebalance' is still Blend-only (no Reversion) ──
def test_momentum_rebalance_close_only_from_place_us():
    # _place_us is the only place that writes this exit_reason; if Blend is
    # correctly scoped it can only ever pass a cur_trade that is its own row.
    place_us_src = inspect.getsource(ar._place_us)
    assert '"momentum_rebalance"' in place_us_src
    rev_src = inspect.getsource(ar.run_us_reversion)
    assert "momentum_rebalance" not in rev_src


def test_atos_runner_ast_parses():
    ast.parse(inspect.getsource(ar))


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
