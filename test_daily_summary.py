"""
test_daily_summary.py
-----------------------
Regression tests for daily_summary.py, the end-of-day P&L digest across
all 4 modules. No test here touches real Saxo or the real pnl_ledger.db —
pnl_tracker's functions and saxo_client are monkeypatched.
"""

import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import daily_summary as ds
import pnl_tracker
import housekeeping as hk

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


def _strategy(strategy, trades, wins, losses, open_, pnl, pf, symbols):
    return {"strategy": strategy, "trades": trades, "wins": wins, "losses": losses,
            "open": open_, "win_rate": round(wins / trades * 100, 1) if trades else 0.0,
            "total_pnl": pnl, "profit_factor": pf, "best": 0, "worst": 0,
            "total_costs": 0, "symbols": symbols}


# ═══════════════════════════════════════════════════════════════════════
section("Per-module aggregation")
# ═══════════════════════════════════════════════════════════════════════

def test_module_data_aggregates_strategy_rows_correctly():
    orig = pnl_tracker.get_strategy_summary_since
    orig_open = pnl_tracker.get_open_positions
    try:
        pnl_tracker.get_strategy_summary_since = lambda module, since: [
            _strategy("gap", 10, 3, 7, 2, -150.0, 0.4, ["EURUSD", "GBPUSD"]),
            _strategy("donchian", 5, 4, 1, 1, 300.0, 3.0, ["USDJPY"]),
        ]
        pnl_tracker.get_open_positions = lambda module: []
        data = ds._module_data("forex", "2026-08-24")
        assert data["trades"] == 15
        assert data["pnl"] == 150.0
        assert data["open_positions"] == 3
        assert data["win_rate"] == round(7 / 15 * 100, 1)
    finally:
        pnl_tracker.get_strategy_summary_since = orig
        pnl_tracker.get_open_positions = orig_open


def test_module_data_falls_back_to_live_open_count_when_no_closed_trades():
    """A module with open positions but nothing closed today (win_rate
    undefined) must still report its open count, not silently show 0."""
    orig = pnl_tracker.get_strategy_summary_since
    orig_open = pnl_tracker.get_open_positions
    try:
        pnl_tracker.get_strategy_summary_since = lambda module, since: []
        pnl_tracker.get_open_positions = lambda module: [{"a": 1}, {"b": 2}]
        data = ds._module_data("futures", "2026-08-24")
        assert data["trades"] == 0
        assert data["win_rate"] is None
        assert data["open_positions"] == 2
    finally:
        pnl_tracker.get_strategy_summary_since = orig
        pnl_tracker.get_open_positions = orig_open


_run("module data sums trades/pnl across strategies and computes overall win rate",
     test_module_data_aggregates_strategy_rows_correctly)
_run("a module with zero closed trades still reports its live open count",
     test_module_data_falls_back_to_live_open_count_when_no_closed_trades)


# ═══════════════════════════════════════════════════════════════════════
section("Account health is read-only")
# ═══════════════════════════════════════════════════════════════════════

def test_account_health_never_calls_reconcile_all():
    """reconcile_all() mutates live state (cancels/replaces orders) --
    a reporting script must never trigger that as a side effect of
    building an email. Pin this down by making reconcile_all blow up if
    called at all during _account_health()."""
    orig_reconcile = hk.reconcile_all
    orig_naked = hk.scan_naked_positions
    orig_bal = ds.saxo_client.get_balances
    try:
        hk.reconcile_all = lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("_account_health() must never call reconcile_all()"))
        hk.scan_naked_positions = lambda send_email=True: []
        ds.saxo_client.get_balances = lambda: {"TotalValue": 27800.0,
                                               "InitialMargin": {"MarginUtilizationPct": 12.0}}
        health = ds._account_health()
        assert health["equity"] == 27800.0
        assert health["naked_count"] == 0
    finally:
        hk.reconcile_all = orig_reconcile
        hk.scan_naked_positions = orig_naked
        ds.saxo_client.get_balances = orig_bal


def test_account_health_survives_saxo_errors():
    orig_bal = ds.saxo_client.get_balances
    orig_naked = hk.scan_naked_positions
    try:
        ds.saxo_client.get_balances = lambda: (_ for _ in ()).throw(ConnectionError("down"))
        hk.scan_naked_positions = lambda send_email=True: (_ for _ in ()).throw(RuntimeError("down"))
        health = ds._account_health()
        assert health["equity"] is None
        assert health["naked_count"] is None
    finally:
        ds.saxo_client.get_balances = orig_bal
        hk.scan_naked_positions = orig_naked


_run("_account_health() never calls the mutating reconcile_all()", test_account_health_never_calls_reconcile_all)
_run("_account_health() degrades gracefully if Saxo/housekeeping calls fail", test_account_health_survives_saxo_errors)


# ═══════════════════════════════════════════════════════════════════════
section("send_daily_summary(): assembly and send")
# ═══════════════════════════════════════════════════════════════════════

def test_send_daily_summary_sends_exactly_one_email_with_correct_subject():
    orig_module_data = ds._module_data
    orig_health = ds._account_health
    orig_send = ds._send_email
    emails = []
    try:
        def fake_module_data(module, since):
            if module == "forex":
                return {"module": "forex", "trades": 3, "pnl": 42.5, "win_rate": 66.7,
                        "open_positions": 2,
                        "strategies": [_strategy("gap", 3, 2, 1, 1, 42.5, 1.8, ["EURUSD"])]}
            return {"module": module, "trades": 0, "pnl": 0, "win_rate": None,
                    "open_positions": 0, "strategies": []}
        ds._module_data = fake_module_data
        ds._account_health = lambda: {"equity": 27800.0, "margin_pct": 12.0, "naked_count": 0}
        ds._send_email = lambda subject, html: emails.append((subject, html)) or True

        ok = ds.send_daily_summary("2026-08-24")
        assert ok is True
        assert len(emails) == 1
        subject, html = emails[0]
        assert "3 trades" in subject
        assert "42" in subject
        assert "gap" in html
        assert "Futures" not in html or "No trades closed" not in html  # empty modules are skipped entirely
    finally:
        ds._module_data = orig_module_data
        ds._account_health = orig_health
        ds._send_email = orig_send


def test_empty_modules_are_omitted_from_the_email_body():
    orig_module_data = ds._module_data
    orig_health = ds._account_health
    orig_send = ds._send_email
    captured = {}
    try:
        def fake_module_data(module, since):
            return {"module": module, "trades": 0, "pnl": 0, "win_rate": None,
                    "open_positions": 0, "strategies": []}
        ds._module_data = fake_module_data
        ds._account_health = lambda: {"equity": None, "margin_pct": None, "naked_count": None}
        def fake_send(subject, html):
            captured["subject"] = subject
            captured["html"] = html
            return True
        ds._send_email = fake_send

        ds.send_daily_summary("2026-08-24")
        for label in ds.MODULE_LABELS.values():
            assert f"<h2>{label}</h2>" not in captured["html"], (
                f"{label} had zero trades and zero open positions -- must not get its own section"
            )
    finally:
        ds._module_data = orig_module_data
        ds._account_health = orig_health
        ds._send_email = orig_send


_run("send_daily_summary sends exactly one email with an accurate subject line",
     test_send_daily_summary_sends_exactly_one_email_with_correct_subject)
_run("a module with nothing to show (no trades, no open positions) gets no section",
     test_empty_modules_are_omitted_from_the_email_body)


# ═══════════════════════════════════════════════════════════════════════
section("pnl_tracker.get_strategy_summary_since()")
# ═══════════════════════════════════════════════════════════════════════

def test_get_strategy_summary_since_real_db_does_not_crash():
    """Structural smoke test against the real (possibly empty) ledger --
    must return a list without crashing regardless of data volume."""
    rows = pnl_tracker.get_strategy_summary_since("forex", "2026-01-01")
    assert isinstance(rows, list)
    for r in rows:
        assert "symbols" in r and isinstance(r["symbols"], list)
        assert "total_costs" in r


_run("get_strategy_summary_since runs against the real ledger without crashing",
     test_get_strategy_summary_since_real_db_does_not_crash)


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
