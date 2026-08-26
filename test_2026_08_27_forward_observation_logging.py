"""
Regression tests -- 2026-08-27 forward-SIM observation logging.

Per the decision to freeze the architecture and let SIM generate forward
evidence instead of tuning further against a tiny sample: this adds
structured, append-only logging for (1) every cost-gate decision (PASS
and BLOCKED, not just skip counts -- the counterfactual question needs
what was let through too) and (2) one currency-exposure snapshot per
run cycle (count-based and EUR-notional side by side). Pure observation
-- these tests confirm logging never changes what the gate decides or
what a run does, only that it's recorded.
"""

import os
import sys
import json
import tempfile
from unittest.mock import patch

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


def test_cost_gate_decision_logs_both_pass_and_blocked():
    import forex.forward_observation as fo
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "cost_gate.jsonl")
        with patch.object(fo, "COST_GATE_LOG", path):
            fo.log_cost_gate_decision(
                account_env="sim", strategy="rsi", symbol="EURUSD", direction="Buy",
                entry_price=1.1, stop_price=1.09, tp_price=1.12, qty=1000,
                expected_target_profit_quote=20.0, round_trip_cost_quote=3.0,
                expected_target_profit_eur=20.0, round_trip_cost_eur=3.0,
                min_edge_to_cost_ratio=3.0, decision="PASS", reason="")
            fo.log_cost_gate_decision(
                account_env="sim", strategy="rsi", symbol="EURPLN", direction="Buy",
                entry_price=4.29, stop_price=4.27, tp_price=4.31, qty=1000,
                expected_target_profit_quote=5.0, round_trip_cost_quote=11.1,
                expected_target_profit_eur=1.2, round_trip_cost_eur=2.6,
                min_edge_to_cost_ratio=3.0, decision="BLOCKED", reason="cost_not_cleared")
        with open(path) as f:
            lines = [json.loads(l) for l in f]
    assert len(lines) == 2
    assert lines[0]["decision"] == "PASS"
    assert lines[1]["decision"] == "BLOCKED"
    assert lines[1]["ratio_actual"] == round(5.0 / 11.1, 2)
_run("forward_observation: log_cost_gate_decision() records both PASS "
     "and BLOCKED signals, not just skip counts",
     test_cost_gate_decision_logs_both_pass_and_blocked)


def test_exposure_snapshot_includes_both_measures_and_pct_of_equity():
    import forex.forward_observation as fo
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "exposure.jsonl")
        with patch.object(fo, "EXPOSURE_LOG", path):
            fo.log_exposure_snapshot(
                account_env="sim", count_exposure={"AUD": 2, "CHF": -2},
                notional_exposure_eur={"AUD": 1000.0, "CHF": -1000.0}, equity_eur=10000.0)
        with open(path) as f:
            row = json.loads(f.readline())
    assert row["count_exposure"] == {"AUD": 2, "CHF": -2}
    assert row["notional_exposure_eur"] == {"AUD": 1000.0, "CHF": -1000.0}
    assert row["pct_of_equity"] == {"AUD": 10.0, "CHF": 10.0}
    assert row["top_currency_by_notional"] in ("AUD", "CHF")
_run("forward_observation: log_exposure_snapshot() records count-based "
     "AND EUR-notional exposure side by side, plus %-of-equity",
     test_exposure_snapshot_includes_both_measures_and_pct_of_equity)


def test_logging_failure_never_raises():
    import forex.forward_observation as fo
    # An unwritable path must not raise -- observation logging must never
    # break a live trading run.
    with patch.object(fo, "COST_GATE_LOG", "/this/path/cannot/possibly/exist/x.jsonl"):
        fo.log_cost_gate_decision(
            account_env="sim", strategy="rsi", symbol="EURUSD", direction="Buy",
            entry_price=1.1, stop_price=1.09, tp_price=1.12, qty=1000,
            expected_target_profit_quote=20.0, round_trip_cost_quote=3.0,
            expected_target_profit_eur=20.0, round_trip_cost_eur=3.0,
            min_edge_to_cost_ratio=3.0, decision="PASS", reason="")
_run("forward_observation: a logging failure (bad path) never raises -- "
     "observation must not be able to break a live run",
     test_logging_failure_never_raises)


def test_cost_gate_wired_into_entry_loop_for_both_outcomes():
    import inspect
    import forex.runner as r
    src = inspect.getsource(r._run_entries)
    assert "forward_observation.log_cost_gate_decision(" in src
    assert 'decision="BLOCKED" if blocked else "PASS"' in src
_run("forex/runner: _run_entries() logs the cost-gate decision for "
     "every signal, tagging PASS vs BLOCKED",
     test_cost_gate_wired_into_entry_loop_for_both_outcomes)


def test_trade_entry_card_wired_and_gated_to_not_dry_run():
    import inspect
    import forex.runner as r
    src = inspect.getsource(r._run_entries)
    assert "forward_observation.log_trade_entry_card(" in src
    # must live inside the `if not dry_run:` block -- a dry run never
    # actually opens a position, logging a card for it would be a phantom
    idx_card = src.index("forward_observation.log_trade_entry_card(")
    idx_not_dry_run = src.index("if not dry_run:")
    assert idx_not_dry_run < idx_card, (
        "trade entry card must be logged only for real (non-dry-run) opens")
_run("forex/runner: trade entry card is only logged for real opens, "
     "never for a dry-run preview", test_trade_entry_card_wired_and_gated_to_not_dry_run)


def test_donchian_structural_hybrid_only_computed_for_donchian():
    import inspect
    import forex.runner as r
    src = inspect.getsource(r._run_entries)
    assert 'if strat_name == "donchian":' in src
    assert "structural_stop = hybrid_stop = None" in src
_run("forex/runner: structural/hybrid stop candidates are only computed "
     "for donchian (its own channel data), None elsewhere -- not a "
     "generic guess applied to every strategy",
     test_donchian_structural_hybrid_only_computed_for_donchian)


def test_mae_mfe_update_reuses_existing_daily_bars_no_extra_api_call():
    import inspect
    import forex.runner as r
    src = inspect.getsource(r._run_exits)
    assert "forward_observation.update_mae_mfe(pos, worst_pnl_eur)" in src
    assert "forward_observation.update_mae_mfe(pos, best_pnl_eur)" in src
    # must reuse `df` (already fetched for should_exit/trailing-stop), not
    # call _live_price() again for every open position every cycle
    mae_block_start = src.index("MAE/MFE from the daily bars")
    mae_block_end = src.index("exit_flag, reason = strat_mod.should_exit")
    mae_block = src[mae_block_start:mae_block_end]
    assert "_live_price(" not in mae_block, (
        "MAE/MFE tracking must not add a live price call per open position "
        "per cycle -- SIM alone has ~97 open positions, that's expensive; "
        "reuse the daily bars already fetched for should_exit()")
_run("forex/runner: MAE/MFE tracking reuses already-fetched daily bars, "
     "doesn't add a live price call per open position per cycle",
     test_mae_mfe_update_reuses_existing_daily_bars_no_extra_api_call)


def test_exposure_snapshot_wired_once_per_run_not_per_strategy():
    import inspect
    import forex.runner as r
    src = inspect.getsource(r.run_daily)
    assert "forward_observation.log_exposure_snapshot(" in src
    entries_src = inspect.getsource(r._run_entries)
    assert "forward_observation.log_exposure_snapshot(" not in entries_src, (
        "must be logged once per run_daily() cycle, not once per strategy "
        "inside _run_entries() -- exposure barely moves within one cycle")
_run("forex/runner: exposure snapshot is logged once per run_daily() "
     "cycle, not once per strategy",
     test_exposure_snapshot_wired_once_per_run_not_per_strategy)


def test_exit_card_wired_and_skips_positions_without_entry_card():
    import inspect
    import forex.runner as r
    src = inspect.getsource(r._run_exits)
    assert "forward_observation.log_trade_exit_card(" in src
    assert 'card_id = pos.get("observation_card_id")' in src
    assert "if card_id:" in src, (
        "must skip logging an exit card for positions opened before this "
        "logging existed (no matching entry card), not log a half-record")
_run("forex/runner: exit card is logged only when a matching entry card "
     "exists, referencing it by card_id", test_exit_card_wired_and_skips_positions_without_entry_card)


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
