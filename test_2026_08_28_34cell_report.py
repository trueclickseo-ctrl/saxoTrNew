"""
Regression tests -- 2026-08-28 34-cell bb/rsi HIGH_VOLUME economics report
(report_34cell_bb_rsi_high_volume.py).

Explicit user spec: per (strategy, pair) cell, historical (pre-2026-08-27
ledger) and forward (2026-08-27+ observation-card) periods reported
SEPARATELY, never merged -- the forward cards record measurements
(real cost, R-multiple, holding-hours) the historical ledger rows never
captured. Decision hierarchy is Net P&L -> Net R -> Profit Factor ->
sample size -> WR, not WR-first.

Also covers the exclusion of historical trades closed via the pre-
2026-08-28 intraday_monitor.py P&L bug (see
test_2026_08_28_intraday_monitor_pnl_bug.py) -- those rows carry
unconverted/uncosted P&L and must never be silently included in the
economics aggregates.
"""

import os
import subprocess
import sys
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


def section(title):
    print(f"\n{BOLD}{CYAN}{'-'*70}{RESET}")
    print(f"{BOLD}{CYAN}  {title}{RESET}")
    print(f"{BOLD}{CYAN}{'-'*70}{RESET}")


import report_34cell_bb_rsi_high_volume as rpt


# ═══════════════════════════════════════════════════════════════════════
section("1. _finalize() -- per-cell aggregate math")
# ═══════════════════════════════════════════════════════════════════════

def test_finalize_computes_pf_and_wr_correctly():
    cell = rpt._empty_cell()
    cell["trades"] = 3
    cell["wins"] = 2
    cell["losses"] = 1
    cell["net"] = [100.0, 50.0, -40.0]
    cell["gross"] = [100.0, 50.0, -40.0]
    cell["cost"] = [0.0, 0.0, 0.0]
    cell["r"] = [2.0, 1.0, -0.8]
    cell["hold_days"] = [1.0, 2.0, 3.0]
    out = rpt._finalize(cell)
    assert abs(out["wr"] - 66.66666666) < 1e-4, f"expected WR~66.67%, got {out['wr']}"
    assert abs(out["pf"] - (150.0 / 40.0)) < 1e-9, f"expected PF=3.75, got {out['pf']}"
    assert out["net_sum"] == 110.0
    assert out["best"] == 100.0 and out["worst"] == -40.0
    assert abs(out["avg_r"] - (2.0 + 1.0 - 0.8) / 3) < 1e-9
    assert out["med_hold_days"] == 2.0
_run("report._finalize() computes WR/PF/net_sum/best/worst/avg_r/median_hold correctly",
     test_finalize_computes_pf_and_wr_correctly)


def test_finalize_pf_is_infinite_with_zero_losses():
    cell = rpt._empty_cell()
    cell["trades"] = 2; cell["wins"] = 2; cell["losses"] = 0
    cell["net"] = [10.0, 20.0]; cell["gross"] = cell["net"]; cell["cost"] = [0.0, 0.0]
    cell["r"] = [1.0, 2.0]; cell["hold_days"] = [1.0, 1.0]
    out = rpt._finalize(cell)
    assert out["pf"] == float("inf"), "an all-winners cell must show PF=inf, not a crash or None"
_run("report._finalize() shows PF=inf (not a crash) for an all-winners cell",
     test_finalize_pf_is_infinite_with_zero_losses)


def test_finalize_empty_cell_is_all_none_not_a_crash():
    out = rpt._finalize(rpt._empty_cell())
    assert out["trades"] == 0
    assert out["wr"] is None and out["pf"] is None and out["avg_r"] is None
_run("report._finalize() on an empty (0-trade) cell returns None fields, not a crash/zero-division",
     test_finalize_empty_cell_is_all_none_not_a_crash)


# ═══════════════════════════════════════════════════════════════════════
section("2. _load_historical() -- buggy intraday_monitor rows excluded")
# ═══════════════════════════════════════════════════════════════════════

def test_historical_excludes_intraday_monitor_buggy_rows():
    fake_trades = [
        {"strategy": "bb", "symbol": "EURUSD", "exit_reason": "STOP-LOSS hit @ 1.1000 (stop=1.1000)",
         "realized_pnl": 999.0, "timestamp_close": "2026-08-20T10:00:00", "timestamp_open": "2026-08-19T10:00:00",
         "entry_price": 1.10, "exit_price": 1.11, "quantity": 1000, "stop_price": 1.09, "direction": "Buy"},
        {"strategy": "bb", "symbol": "EURUSD", "exit_reason": "hard_stop (1.09000)",
         "realized_pnl": 50.0, "timestamp_close": "2026-08-21T10:00:00", "timestamp_open": "2026-08-20T10:00:00",
         "entry_price": 1.10, "exit_price": 1.105, "quantity": 1000, "stop_price": 1.09, "direction": "Buy"},
    ]
    with patch.object(rpt.pnl_tracker, "get_closed_trades", return_value=fake_trades):
        cells = rpt._load_historical()
    cell = cells[("bb", "EURUSD")]
    assert cell["trades"] == 1, f"expected only the non-buggy row counted, got {cell['trades']}"
    assert cell["excluded_buggy"] == 1, f"expected the STOP-LOSS-hit row flagged excluded, got {cell['excluded_buggy']}"
    assert cell["net_sum"] == 50.0, f"the buggy row's 999.0 must not leak into net_sum, got {cell['net_sum']}"
_run("report._load_historical() excludes intraday_monitor-bug rows from aggregates, counts them separately",
     test_historical_excludes_intraday_monitor_buggy_rows)


def test_historical_ignores_rows_outside_scope():
    fake_trades = [
        {"strategy": "donchian", "symbol": "EURUSD", "exit_reason": "hard_stop", "realized_pnl": 100.0,
         "timestamp_close": "2026-08-20T10:00:00", "timestamp_open": "2026-08-19T10:00:00",
         "entry_price": 1.10, "exit_price": 1.11, "quantity": 1000, "stop_price": 1.09, "direction": "Buy"},
        {"strategy": "bb", "symbol": "EURTRY", "exit_reason": "hard_stop", "realized_pnl": 100.0,
         "timestamp_close": "2026-08-20T10:00:00", "timestamp_open": "2026-08-19T10:00:00",
         "entry_price": 1.10, "exit_price": 1.11, "quantity": 1000, "stop_price": 1.09, "direction": "Buy"},
    ]
    with patch.object(rpt.pnl_tracker, "get_closed_trades", return_value=fake_trades):
        cells = rpt._load_historical()
    assert all(c["trades"] == 0 for c in cells.values()), (
        "a non-bb/rsi strategy and a non-HIGH_VOLUME pair must contribute to NO cell"
    )
_run("report._load_historical() ignores trades outside the bb/rsi x HIGH_VOLUME_SYMBOLS scope",
     test_historical_ignores_rows_outside_scope)


def test_historical_excludes_rows_at_or_after_forward_cutoff():
    fake_trades = [
        {"strategy": "rsi", "symbol": "EURUSD", "exit_reason": "rsi_recovery", "realized_pnl": 100.0,
         "timestamp_close": "2026-08-27T10:00:00", "timestamp_open": "2026-08-26T10:00:00",
         "entry_price": 1.10, "exit_price": 1.11, "quantity": 1000, "stop_price": 1.09, "direction": "Buy"},
    ]
    with patch.object(rpt.pnl_tracker, "get_closed_trades", return_value=fake_trades):
        cells = rpt._load_historical()
    assert cells[("rsi", "EURUSD")]["trades"] == 0, (
        "a trade closed ON/AFTER the forward cutoff must not leak into the historical period"
    )
_run("report._load_historical() excludes trades at/after the forward-observation cutoff date",
     test_historical_excludes_rows_at_or_after_forward_cutoff)


# ═══════════════════════════════════════════════════════════════════════
section("3. _load_forward() -- observation-card period, entry/exit pairing")
# ═══════════════════════════════════════════════════════════════════════

def test_forward_pairs_entry_and_exit_cards_by_card_id():
    cards = [
        {"card_id": "c1", "event": "entry", "strategy": "rsi", "symbol": "EURUSD"},
        {"card_id": "c1", "event": "exit", "net_pnl_eur": 42.0, "gross_pnl_eur": 50.0,
         "commission_eur": 8.0, "r_multiple": 1.5, "holding_hours": 48.0},
    ]
    with patch.object(rpt, "_load_jsonl", side_effect=lambda p: cards if p == rpt.TRADE_CARDS_LOG else []):
        cells, still_open = rpt._load_forward()
    cell = cells[("rsi", "EURUSD")]
    assert cell["trades"] == 1 and cell["net_sum"] == 42.0
    assert cell["cost_sum"] == 8.0 and cell["gross_sum"] == 50.0
    assert cell["avg_r"] == 1.5
    assert cell["med_hold_days"] == 2.0
    assert still_open == 0
_run("report._load_forward() correctly pairs entry+exit cards by card_id and derives holding days",
     test_forward_pairs_entry_and_exit_cards_by_card_id)


def test_forward_counts_still_open_positions_separately():
    cards = [{"card_id": "c2", "event": "entry", "strategy": "bb", "symbol": "GBPUSD"}]
    with patch.object(rpt, "_load_jsonl", side_effect=lambda p: cards if p == rpt.TRADE_CARDS_LOG else []):
        cells, still_open = rpt._load_forward()
    assert cells[("bb", "GBPUSD")]["trades"] == 0, "an entry with no matching exit must not count as closed"
    assert still_open == 1
_run("report._load_forward() counts an entry-only (still-open) card separately, not as a closed trade",
     test_forward_counts_still_open_positions_separately)


def test_forward_cost_gate_counts_pass_and_blocked():
    gate_rows = [
        {"strategy": "bb", "symbol": "EURUSD", "decision": "PASS"},
        {"strategy": "bb", "symbol": "EURUSD", "decision": "BLOCKED"},
        {"strategy": "bb", "symbol": "EURUSD", "decision": "BLOCKED"},
    ]
    with patch.object(rpt, "_load_jsonl", side_effect=lambda p: gate_rows if p == rpt.COST_GATE_LOG else []):
        cells, _ = rpt._load_forward()
    cell = cells[("bb", "EURUSD")]
    assert cell["gate_pass"] == 1 and cell["gate_blocked"] == 2
_run("report._load_forward() tallies cost-gate PASS/BLOCKED decisions per cell",
     test_forward_cost_gate_counts_pass_and_blocked)


# ═══════════════════════════════════════════════════════════════════════
section("4. Blackbox -- the script runs cleanly end-to-end via a real subprocess")
# ═══════════════════════════════════════════════════════════════════════

def test_script_runs_cleanly_and_separates_periods():
    proc = subprocess.run(
        [sys.executable, "report_34cell_bb_rsi_high_volume.py"],
        cwd=BASE_DIR, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    assert proc.returncode == 0, f"expected a clean exit(0), got {proc.returncode}: {proc.stderr}"
    out = proc.stdout
    assert "HISTORICAL PERIOD" in out and "FORWARD PERIOD" in out
    assert "34 cells" in out
    assert "must never be summed or averaged together" in out
_run("report_34cell_bb_rsi_high_volume.py runs cleanly via a real subprocess, sections clearly separated",
     test_script_runs_cleanly_and_separates_periods)


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
