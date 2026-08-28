"""
Regression test -- 2026-08-28 ETF universe narrowed to a real, data-
verified TOP 20 by average daily dollar turnover.

Explicit user request: "limit ETF till TOP 20. which are in high
volume and most traded ETF". Previously DualMAStrategy.UNIVERSE was a
101-symbol AUM-curated list (widened earlier the same day from an
original 50). Ranked all 101 by real 20-trading-day average daily
turnover (Volume x Close, pulled live from Saxo's /chart/v3/charts --
not AUM, not a guessed/recalled "most popular ETFs" list) and narrowed
to the real top 20: SPY, QQQ, IWM, GLD, SMH, SOXX, LQD, EWY, TLT, GDX,
HYG, DIA, XLF, XLE, XLV, XBI, RSP, XLK, VTI, EEM.
"""

import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "saxo_etf_strategy"))

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
section("1. DualMAStrategy.UNIVERSE is the real, verified top 20")
# ═══════════════════════════════════════════════════════════════════════

EXPECTED_TOP20 = {
    "SPY", "QQQ", "IWM", "GLD", "SMH", "SOXX", "LQD", "EWY", "TLT", "GDX",
    "HYG", "DIA", "XLF", "XLE", "XLV", "XBI", "RSP", "XLK", "VTI", "EEM",
}


def test_universe_is_exactly_20():
    from core.etf_strategy import DualMAStrategy
    assert len(DualMAStrategy.UNIVERSE) == 20, (
        f"expected exactly 20 symbols, got {len(DualMAStrategy.UNIVERSE)}"
    )
_run("DualMAStrategy.UNIVERSE has exactly 20 symbols",
     test_universe_is_exactly_20)


def test_universe_matches_the_real_ranked_list():
    from core.etf_strategy import DualMAStrategy
    assert set(DualMAStrategy.UNIVERSE) == EXPECTED_TOP20, (
        f"universe doesn't match the real turnover-ranked top 20 -- "
        f"got {set(DualMAStrategy.UNIVERSE)}"
    )
_run("DualMAStrategy.UNIVERSE matches the real data-verified top 20 by turnover",
     test_universe_matches_the_real_ranked_list)


def test_no_duplicates():
    from core.etf_strategy import DualMAStrategy
    assert len(DualMAStrategy.UNIVERSE) == len(set(DualMAStrategy.UNIVERSE))
_run("No duplicate symbols in the universe",
     test_no_duplicates)


# ═══════════════════════════════════════════════════════════════════════
section("2. Candidate/position caps match the 20-symbol universe")
# ═══════════════════════════════════════════════════════════════════════

def test_max_candidates_per_run_is_20():
    from config.etf_config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG.strategy.max_candidates_per_run == 20, (
        f"expected max_candidates_per_run == 20, got {DEFAULT_CONFIG.strategy.max_candidates_per_run}"
    )
_run("ETFStrategyConfig.max_candidates_per_run == 20",
     test_max_candidates_per_run_is_20)


def test_max_positions_is_20():
    from config.etf_config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG.risk.max_positions == 20, (
        f"expected max_positions == 20, got {DEFAULT_CONFIG.risk.max_positions}"
    )
_run("ETFRiskConfig.max_positions == 20",
     test_max_positions_is_20)


def test_rank_weighted_top_pick_stays_under_position_ceiling():
    # With n=20, weights=[20..1], weight_sum=210 -- rank-1's share of the
    # 15%-of-cash allocation budget is 15% * 20/210 ~= 1.43% of cash,
    # comfortably under max_position_pct=3%. Verifies the narrower
    # universe didn't silently push the top pick's weighted share over
    # its own per-position ceiling.
    from config.etf_config import DEFAULT_CONFIG
    n = DEFAULT_CONFIG.strategy.max_candidates_per_run
    weights = [n - i for i in range(n)]
    weight_sum = sum(weights)
    top_pick_pct_of_cash = DEFAULT_CONFIG.risk.total_allocation_pct_of_account * weights[0] / weight_sum
    assert top_pick_pct_of_cash < DEFAULT_CONFIG.risk.max_position_pct, (
        f"rank-1's weighted share ({top_pick_pct_of_cash:.4f}) exceeds "
        f"max_position_pct ({DEFAULT_CONFIG.risk.max_position_pct}) -- would be silently clipped"
    )
_run("Rank-1's weighted allocation share stays comfortably under max_position_pct",
     test_rank_weighted_top_pick_stays_under_position_ceiling)


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
