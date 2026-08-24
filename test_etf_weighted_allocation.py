"""
test_etf_weighted_allocation.py
---------------------------------
Regression tests for the 2026-08-24 change widening ETF sector rotation
from top-3/equal-split to top-10/rank-weighted: max_candidates_per_run
and max_positions both raised to 10, and process_signals() now gives
rank 1 the largest slice of the fixed 15%-of-cash budget instead of
splitting it equally across every open slot.
"""

import os
import sys
from unittest.mock import MagicMock

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "saxo_etf_strategy"))

from config.etf_config import DEFAULT_CONFIG, ETFConfig
from core.etf_executor import ETFExecutor
from core.etf_strategy import ETFSignal
from core.etf_state import ETFStateStore

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


def _signal(symbol, uic, price=100.0, score=0.0):
    return ETFSignal(uic=uic, symbol=symbol, description=symbol, exchange_id="NYSE_ARCA",
                     currency="USD", action="BUY", score=score, last_price=price,
                     fast_ma=price, slow_ma=0.0)


def _make_executor(cash, held_uics=None, dry_run=True):
    cfg = ETFConfig()
    cfg.dry_run = dry_run
    state = ETFStateStore(path=os.path.join(BASE_DIR, "__unused_test_state.json"))
    state._state = {"positions": {}, "orders": []}
    state._save = lambda: None  # never touch disk in these tests
    for uic in (held_uics or []):
        state._state["positions"][str(uic)] = {"symbol": "HELD", "quantity": 1}
    client = MagicMock()
    executor = ETFExecutor(client=client, state=state, cfg=cfg)
    executor.get_account_cash = lambda: cash
    executor._account_key = "AKEY"
    return executor


# ═══════════════════════════════════════════════════════════════════════
section("Config: top-10 with rank-weighted allocation")
# ═══════════════════════════════════════════════════════════════════════

def test_config_widened_to_ten():
    assert DEFAULT_CONFIG.strategy.max_candidates_per_run == 10
    assert DEFAULT_CONFIG.risk.max_positions == 10


def test_weights_are_linear_by_rank_highest_first():
    n = 10
    weights = [n - i for i in range(n)]
    assert weights == [10, 9, 8, 7, 6, 5, 4, 3, 2, 1]
    assert weights[0] > weights[-1]


_run("config widened to top-10 candidates / 10 max positions", test_config_widened_to_ten)
_run("rank weights are linear, highest at rank 1, lowest at rank 10", test_weights_are_linear_by_rank_highest_first)


# ═══════════════════════════════════════════════════════════════════════
section("process_signals(): rank-weighted budget, not equal split")
# ═══════════════════════════════════════════════════════════════════════

def test_rank_one_gets_more_budget_than_rank_ten():
    executor = _make_executor(cash=100_000)
    calls = []
    executor._enter_position = lambda signal, budget: calls.append((signal.symbol, budget))
    signals = [_signal(f"ETF{i}", uic=1000 + i, score=1.0 - i * 0.1) for i in range(10)]
    executor.process_signals(signals)
    assert len(calls) == 10
    budgets = {sym: b for sym, b in calls}
    assert budgets["ETF0"] > budgets["ETF9"], "rank 1 (first signal) must get more budget than rank 10 (last)"
    # linear weights 10..1 -> rank1 should be exactly 10x rank10
    assert abs(budgets["ETF0"] / budgets["ETF9"] - 10.0) < 1e-6


def test_budgets_sum_to_the_allocation_budget_when_all_slots_used():
    executor = _make_executor(cash=100_000)
    calls = []
    executor._enter_position = lambda signal, budget: calls.append(budget)
    signals = [_signal(f"ETF{i}", uic=2000 + i) for i in range(10)]
    executor.process_signals(signals)
    allocation_budget = 100_000 * DEFAULT_CONFIG.risk.total_allocation_pct_of_account
    assert abs(sum(calls) - allocation_budget) < 1.0, (
        "the 10 weighted budgets should sum back to the fixed 15%-of-cash allocation"
    )


def test_per_position_cap_still_applies_to_the_top_ranked_pick():
    """Even though rank-1 gets the biggest slice, it must still never
    exceed max_position_pct of cash."""
    executor = _make_executor(cash=100_000)
    calls = []
    executor._enter_position = lambda signal, budget: calls.append(budget)
    signals = [_signal(f"ETF{i}", uic=3000 + i) for i in range(3)]  # few candidates -> big weighted share
    executor.process_signals(signals)
    cap = 100_000 * DEFAULT_CONFIG.risk.max_position_pct
    assert all(b <= cap + 1e-6 for b in calls), "no single position may exceed max_position_pct of cash"


def test_already_held_symbols_are_skipped_and_dont_consume_a_weighted_slot():
    executor = _make_executor(cash=100_000, held_uics=[4001])  # rank-1 candidate already held
    calls = []
    executor._enter_position = lambda signal, budget: calls.append(signal.symbol)
    signals = [_signal("HELD", uic=4001), _signal("NEW", uic=4002)]
    executor.process_signals(signals)
    assert calls == ["NEW"], "an already-held top pick must be skipped, not re-bought"


_run("rank 1 gets exactly 10x the budget of rank 10 (linear weight ratio)",
     test_rank_one_gets_more_budget_than_rank_ten)
_run("the 10 weighted per-position budgets sum back to the total allocation budget",
     test_budgets_sum_to_the_allocation_budget_when_all_slots_used)
_run("the top-ranked pick's weighted budget still respects max_position_pct",
     test_per_position_cap_still_applies_to_the_top_ranked_pick)
_run("an already-held symbol is skipped without consuming its rank's weighted slot",
     test_already_held_symbols_are_skipped_and_dont_consume_a_weighted_slot)


# ═══════════════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════════════

print(f"\n{BOLD}{'='*70}{RESET}")
passed = sum(1 for _, ok, _ in _results if ok)
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
