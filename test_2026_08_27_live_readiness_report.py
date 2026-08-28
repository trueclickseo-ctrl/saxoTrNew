"""
Regression tests -- 2026-08-27/28 LIVE readiness report verdict logic.

Covers reports/live_readiness_report.py's compute_stats()/verdict()
functions (the actual decision logic behind the 17-pair x 3-strategy
matrix) -- these are pure functions, testable without openpyxl or a
live Saxo connection, unlike the rest of that script.
"""

import os
import sys
import importlib.util

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


def _load_report_functions():
    # Import just the pure logic without triggering openpyxl/forex.runner
    # imports at module load time -- load the file as source and exec only
    # the two function defs we need, sandboxed.
    path = os.path.join(BASE_DIR, "reports", "live_readiness_report.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    start = src.index("def compute_stats(")
    end = src.index("VERDICT_FILL = {")
    ns = {}
    exec(src[start:end], ns)
    return ns["compute_stats"], ns["verdict"]


def test_zero_trades_is_no_data():
    compute_stats, verdict = _load_report_functions()
    s = compute_stats([])
    assert s["n"] == 0
    assert verdict(s) == "NO DATA"
_run("live_readiness_report: an empty cell (0 trades) verdicts as NO DATA, "
     "never a false green/red", test_zero_trades_is_no_data)


def test_thin_sample_is_insufficient_regardless_of_result():
    compute_stats, verdict = _load_report_functions()
    ts = [{"gross_pnl_eur": 100, "commission_eur": 5, "net_pnl_eur": 95}] * 3
    s = compute_stats(ts)
    assert s["n"] == 3
    assert verdict(s) == "INSUFFICIENT SAMPLE", (
        "even a great-looking 3-trade sample must not verdict as LIVE candidate "
        "-- not enough trades to trust yet")
_run("live_readiness_report: fewer than 5 trades verdicts as INSUFFICIENT "
     "SAMPLE even when every trade won", test_thin_sample_is_insufficient_regardless_of_result)


def test_net_positive_low_cost_is_live_candidate():
    compute_stats, verdict = _load_report_functions()
    ts = [{"gross_pnl_eur": 100, "commission_eur": 5, "net_pnl_eur": 95}] * 6
    s = compute_stats(ts)
    assert s["cost_pct_gross"] == round(5/100*100, 1)
    assert verdict(s) == "LIVE candidate"
_run("live_readiness_report: net-positive with cost <30% of gross, 6+ "
     "trades, verdicts as LIVE candidate", test_net_positive_low_cost_is_live_candidate)


def test_net_positive_high_cost_is_sim_only():
    compute_stats, verdict = _load_report_functions()
    # gross winners are real, but cost eats too much -- the exact
    # EURPLN/NZDCHF pattern this whole investigation started from
    ts = [{"gross_pnl_eur": 10, "commission_eur": 5, "net_pnl_eur": 5}] * 6
    s = compute_stats(ts)
    assert s["net"] > 0
    assert s["cost_pct_gross"] == 50.0
    assert verdict(s) == "SIM only (cost heavy)"
_run("live_readiness_report: net-positive but cost >=30% of gross verdicts "
     "as SIM only, not a false LIVE candidate", test_net_positive_high_cost_is_sim_only)


def test_net_negative_is_do_not_trade_live():
    compute_stats, verdict = _load_report_functions()
    ts = [{"gross_pnl_eur": 10, "commission_eur": 5.16, "net_pnl_eur": -1.29}] * 6
    s = compute_stats(ts)
    assert verdict(s) == "DO NOT TRADE LIVE"
_run("live_readiness_report: net-negative (the EURPLN pattern) verdicts as "
     "DO NOT TRADE LIVE, gross profitability never overrides this",
     test_net_negative_is_do_not_trade_live)


def test_win_rate_and_profit_factor_computed_correctly():
    compute_stats, verdict = _load_report_functions()
    ts = [
        {"gross_pnl_eur": 100, "commission_eur": 5, "net_pnl_eur": 95},
        {"gross_pnl_eur": 100, "commission_eur": 5, "net_pnl_eur": 95},
        {"gross_pnl_eur": -50, "commission_eur": 5, "net_pnl_eur": -55},
        {"gross_pnl_eur": -20, "commission_eur": 5, "net_pnl_eur": -25},
    ]
    s = compute_stats(ts)
    assert s["win_rate"] == 50.0
    assert s["pf"] == round((95+95) / (55+25), 2)
_run("live_readiness_report: compute_stats() gets win rate and profit "
     "factor right against a hand-checked example",
     test_win_rate_and_profit_factor_computed_correctly)


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
