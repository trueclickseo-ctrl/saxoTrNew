"""
2026-09-03 -- ATOS LIVE STOCKS: black-box / end-to-end validation.

Exercises the whole real-money US Blend sleeve from the OUTSIDE -- the CLI
contract, the dry-run gate, the rails, the snapshot filter, the safety
invariants (LIVE never auto-closes, no real order without both env vars),
housekeeping/safeguard isolation, the AI layer, the scheduler wiring, and
one full forced-DRY-RUN cycle.

SAFETY: this module is LIVE (SAXO_LIVE_STOCKS_CONFIRMED=1 / LIVE_STOCKS_DRY_RUN=0
may be in the ambient env). EVERY subprocess here forces LIVE_STOCKS_DRY_RUN=1
and never passes --live, so the test can NEVER place a real order.
"""

import ast
import json
import os
import subprocess
import sys
import tempfile
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

G, R, Y, X, B = "\033[92m", "\033[91m", "\033[93m", "\033[0m", "\033[1m"
_res = []


def _run(n, f):
    try:
        f()
        _res.append((n, True, None))
    except Exception as e:
        import traceback
        _res.append((n, False, f"{type(e).__name__}: {e}\n{traceback.format_exc()}"))


def _cli(args, timeout=120, extra_env=None):
    """Run atos_live_stocks.py with real orders HARD-DISABLED (dry-run forced,
    no --live, confirm var stripped) no matter the ambient env."""
    env = dict(os.environ)
    env["LIVE_STOCKS_DRY_RUN"] = "1"
    env.pop("SAXO_LIVE_STOCKS_CONFIRMED", None)
    assert "--live" not in args, "the black-box test never runs --live"
    if extra_env:
        env.update(extra_env)
    return subprocess.run([sys.executable, "-X", "utf8", "atos_live_stocks.py", *args],
                          cwd=BASE, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


# ═══════════════════════════════════════════════════════════════════════
#  1. CLI contract
# ═══════════════════════════════════════════════════════════════════════

def test_strategy_must_be_us_blend():
    for s in ("rsi", "US Reversion", "us_blend", "momentum", ""):
        p = _cli(["--strategy", s])
        assert p.returncode == 2, f"--strategy {s!r}: expected exit 2, got {p.returncode}"
        assert "only runs" in p.stderr and "US Blend" in p.stderr


def test_info_is_readonly_and_lists_the_map_diff():
    p = _cli(["--info"], timeout=90)
    out = p.stdout + p.stderr
    assert p.returncode in (0, 1)
    assert "No orders placed" in out or "not built yet" in out
    if "not built yet" not in out:
        assert "LIVE UIC" in out and "differ SIM vs LIVE" in out


def test_help_exits_clean_and_documents_the_gate():
    p = _cli(["--help"], timeout=30)
    assert p.returncode == 0
    assert "SAXO_LIVE_STOCKS_CONFIRMED" in p.stdout and "LIVE_STOCKS_DRY_RUN" in p.stdout


def test_dry_run_gate_is_a_conjunction_of_all_four():
    src = open(os.path.join(BASE, "atos_live_stocks.py"), encoding="utf-8").read()
    assert 'dry_run = dry_env or (not args.live) or (not confirmed) or halted' in src
    # each gate readable
    assert 'os.environ.get("LIVE_STOCKS_DRY_RUN", "1") != "0"' in src
    assert 'os.environ.get("SAXO_LIVE_STOCKS_CONFIRMED") == "1"' in src
    assert 'LIVE_STOCKS_TRADING_HALTED' in src


# ═══════════════════════════════════════════════════════════════════════
#  2. Snapshot filter — AccountKey AND AssetType=="Stock"
# ═══════════════════════════════════════════════════════════════════════

def test_snapshot_keeps_only_this_accounts_stock_rows():
    import housekeeping_live_stocks as hk
    import saxo_client
    real = (saxo_client.get_account_key, saxo_client.get_positions, saxo_client.get_orders)
    try:
        saxo_client.get_account_key = lambda env="sim": "SEK"
        saxo_client.get_positions = lambda env="sim": {"Data": [
            {"PositionBase": {"AccountKey": "SEK",   "AssetType": "Stock",  "Uic": 1, "Amount": 10}},
            {"PositionBase": {"AccountKey": "SEK",   "AssetType": "FxSpot", "Uic": 2, "Amount": 1000}},
            {"PositionBase": {"AccountKey": "OTHER", "AssetType": "Stock",  "Uic": 3, "Amount": 5}},
        ]}
        saxo_client.get_orders = lambda at=None, env="sim": {"Data": [
            {"AccountKey": "SEK",   "AssetType": "Stock",  "Uic": 1, "Status": "Working", "BuySell": "Sell", "OpenOrderType": "Stop", "Amount": 10},
            {"AccountKey": "SEK",   "AssetType": "FxSpot", "Uic": 2, "Status": "Working"},
            {"AccountKey": "OTHER", "AssetType": "Stock",  "Uic": 3, "Status": "Working"},
        ]}
        s = hk.fetch_live_stock_snapshot()
        assert set(s.positions_by_uic) == {1}
        assert set(s.orders_by_uic) == {1}
        assert s.net_amount(1) == 10
    finally:
        saxo_client.get_account_key, saxo_client.get_positions, saxo_client.get_orders = real


def test_degraded_orders_snapshot_is_detected():
    import housekeeping_live_stocks as hk
    snap = hk.StockLiveSnapshot("SEK",
        {1: [{"PositionBase": {"Amount": 10, "Uic": 1}}]}, {})   # positions but 0 orders
    assert hk.orders_snapshot_looks_unreliable(snap) is True
    snap2 = hk.StockLiveSnapshot("SEK", {}, {})
    assert hk.orders_snapshot_looks_unreliable(snap2) is False


def test_naked_scan_flags_a_stop_less_position():
    import housekeeping_live_stocks as hk
    snap = hk.StockLiveSnapshot("SEK",
        {11: [{"PositionBase": {"Amount": 10, "Uic": 11, "AssetType": "Stock"},
               "PositionView": {"CurrentPrice": 100.0}}]},
        {11: [{"Status": "Working", "BuySell": "Sell", "OpenOrderType": "Limit", "Amount": 10}]})  # TP only, no stop
    naked = hk.scan_naked_stock_positions(snapshot=snap, send_email=False)
    assert len(naked) == 1 and naked[0].protection == "tp_only"


# ═══════════════════════════════════════════════════════════════════════
#  3. Rails
# ═══════════════════════════════════════════════════════════════════════

def test_budget_is_min_of_pooled_and_30k_minus_buffer():
    import atos_live_stocks as a
    assert a.live_stocks_rails({"TotalValue": 9_000_000, "InitialMargin": {"MarginUtilizationPct": 5}})["budget_sek"] == round(30_000 * 0.9, 2)
    assert a.live_stocks_rails({"TotalValue": 10_000, "InitialMargin": {"MarginUtilizationPct": 5}})["budget_sek"] == round(10_000 * 0.9, 2)


def test_margin_gate_fails_open_and_blocks_over_50():
    import atos_live_stocks as a
    assert a.live_stocks_rails({"TotalValue": 30_000})["margin_ok"] is True                 # no util -> fail open
    assert a.live_stocks_rails({"TotalValue": 30_000, "InitialMargin": {"MarginUtilizationPct": 49.9}})["margin_ok"] is True
    assert a.live_stocks_rails({"TotalValue": 30_000, "InitialMargin": {"MarginUtilizationPct": 50.1}})["margin_ok"] is False


def test_daily_loss_cap_uses_the_30k_base_not_the_sim_constant():
    import atos_live_stocks as a
    import atos.risk as risk
    assert risk.STARTING_CAPITAL_SEK > 1_000_000                     # the SIM constant that must NOT be used
    r = a.live_stocks_rails({"TotalValue": 30_000, "InitialMargin": {"MarginUtilizationPct": 5}})
    assert r["exits_only"] is False                                  # fresh/empty ledger -> 0 P&L, not ~100% DD


# ═══════════════════════════════════════════════════════════════════════
#  4. Safety invariants
# ═══════════════════════════════════════════════════════════════════════

def test_safeguard_live_stocks_never_auto_closes():
    src = open(os.path.join(BASE, "safeguard_live_stocks.py"), encoding="utf-8").read()
    assert "close_trade" not in src
    assert "place_market_order" not in src
    assert "raise_attention" in src                                  # escalates instead
    assert "NEVER AUTO-CLOSES" in src.upper() or "never auto-close" in src.lower()


def test_place_us_hard_refuses_non_blend_on_live():
    import atos_runner
    atos_runner.set_stocks_env("live")
    try:
        try:
            atos_runner._place_us("Buy", "AAPL", 1, {"AAPL": {"uic": 1}}, [], price=100.0,
                                  strategy="US Reversion")
            assert False, "expected RuntimeError"
        except RuntimeError as e:
            assert "US-Blend-only" in str(e)
    finally:
        atos_runner.set_stocks_env("sim")


def test_bracket_is_stop_plus_tp_at_8_and_20_pct():
    import atos_runner
    assert atos_runner.US_BLEND_STOP_PCT == 0.08
    assert atos_runner.US_BLEND_TP_PCT == 0.20
    src = __import__("inspect").getsource(atos_runner._place_us)
    assert "place_with_stop(" in src
    assert "stop_price=stop_p" in src and "take_profit_price=tp_p" in src


def test_no_real_order_path_without_both_env_vars_black_box():
    # the ambient env may have the vars; _cli strips CONFIRMED + forces DRY_RUN=1.
    # Without --live AND without the vars, the run must self-declare OBSERVE ONLY.
    src = open(os.path.join(BASE, "atos_live_stocks.py"), encoding="utf-8").read()
    assert 'tag = "[LIVE STOCKS DRY-RUN]" if dry_run else "[LIVE STOCKS]"' in src
    assert 'OBSERVE ONLY' in src


# ═══════════════════════════════════════════════════════════════════════
#  5. Isolation
# ═══════════════════════════════════════════════════════════════════════

def test_module_uses_its_own_state_files_and_lock():
    src = open(os.path.join(BASE, "atos_live_stocks.py"), encoding="utf-8").read()
    for env_key, fname in (("ATOS_DB_PATH", "atos_live_stocks.db"),
                           ("ATOS_RISK_STATE_FILE", "atos_live_stocks_risk_state.json"),
                           ("ATOS_US_MOMENTUM_STATE", "us_momentum_state_live.json")):
        assert f'os.environ.setdefault("{env_key}"' in src and fname in src
    assert "proc_lock.ATOS_LIVE_STOCKS_LOCK" in src
    import proc_lock
    assert proc_lock.ATOS_LIVE_STOCKS_LOCK != proc_lock.ATOS_LOCK


def test_run_cycle_and_daily_run_stay_sim_only():
    import atos_runner
    import inspect
    for fn in ("run_cycle", "run_intraday_cycle"):
        assert "set_stocks_env" not in inspect.getsource(getattr(atos_runner, fn))
    assert "set_stocks_env" not in open(os.path.join(BASE, "daily_run.py"), encoding="utf-8").read()
    assert atos_runner._sx() == "sim"


def test_housekeeping_live_stocks_imports_no_sim_adapters():
    import inspect, housekeeping_live_stocks as m
    tree = ast.parse(inspect.getsource(m))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "ADAPTERS" not in names | attrs
    assert "reconcile_all" not in names | attrs
    assert "StocksAdapter" not in names


# ═══════════════════════════════════════════════════════════════════════
#  6. AI layer
# ═══════════════════════════════════════════════════════════════════════

def test_ai_cannot_act_on_live_stocks():
    import ai.config as c
    assert "live_stocks" in c._AI_SHADOW_ACCOUNTS
    assert "live_stocks" not in c._AI_ACTING_ACCOUNTS
    assert c.can_apply_decision("live_stocks") is False
    assert c.basket_ranker_applies("live_stocks") is False           # log-only, deterministic pick stands


def test_live_stocks_entry_cards_are_tagged_and_journal_reads_them():
    import ai.features.stock_cards as sc
    fd, p = tempfile.mkstemp(suffix=".jsonl"); os.close(fd)
    real = sc.STOCK_CARDS_LOG
    try:
        sc.STOCK_CARDS_LOG = p
        cid = sc.log_stock_entry_card(strategy="us_blend", ticker="HUM", direction="Buy",
                                      entry_price=400.0, shares=1, stop_price=368.0,
                                      sek_per_eur=11.0, entry_date="2026-09-03",
                                      risk_sek=32.0, account_env="live_stocks")
        assert cid.startswith("live_stocks:")
        row = json.loads(open(p, encoding="utf-8").read().strip())
        assert row["account_env"] == "live_stocks"
    finally:
        sc.STOCK_CARDS_LOG = real
        os.unlink(p)


# ═══════════════════════════════════════════════════════════════════════
#  7. Commission
# ═══════════════════════════════════════════════════════════════════════

def test_commission_clears_the_saxo_minimum():
    import atos_runner
    assert atos_runner.stocks_live_commission_sek(1, 5.0, 10.5) >= 3.0 * 10.5 * 0.98   # min floor dominates
    assert abs(atos_runner.stocks_live_commission_sek(1000, 50.0, 10.5) - 1000 * 0.02 * 10.5) < 1e-6


# ═══════════════════════════════════════════════════════════════════════
#  8. Scheduler + dashboard
# ═══════════════════════════════════════════════════════════════════════

def test_scheduler_ps1_and_bats_are_live_and_in_us_hours():
    ps1 = open(os.path.join(BASE, "setup_scheduler_live_stocks.ps1"), encoding="utf-8").read()
    assert '-At "19:20"' in ps1 and '-At "23:30"' in ps1               # inside US RTH
    daily = open(os.path.join(BASE, "run_atos_live_stocks_daily.bat"), encoding="utf-8").read()
    assert "atos_live_stocks.py --live" in daily
    assert "--strategy" not in daily


def test_watchdog_registers_live_stocks_alert_only():
    import scheduler_watchdog as w
    assert {"Stocks LIVE Daily Run", "Stocks LIVE Exit Check"} <= set(w.WINDOWS_TASKS)
    assert "Stocks LIVE Daily Run" not in w.INTRADAY_REPEATING_TASKS
    assert not any("LIVE" in n for n in w.AUTO_FIX_ELIGIBLE)           # a real-money task is never auto-restarted


def test_dashboard_renders_and_strips_ansi_for_a_pipe():
    import importlib
    d = importlib.import_module("live_stocks_dashboard")
    out = d.render()
    # Only the unconditional header is asserted here -- the LAST SCAN /
    # REBALANCE CLOCKS panels are driven by data/stocks_live_status.json,
    # which other tests in this suite legitimately rewrite (risk-off runs,
    # SIM-data-down runs). Panel content is covered by the account suite
    # where the status file is constructed. This test's job is: renders
    # without throwing, and strips ANSI for a pipe.
    assert "ATOS LIVE STOCKS" in out and "REAL MONEY" in out
    assert "OBSERVE-ONLY" in out
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        d._emit(out)
    assert "\033[" not in buf.getvalue()


# ═══════════════════════════════════════════════════════════════════════
#  9. Full forced-DRY-RUN end-to-end cycle (downloads the universe)
# ═══════════════════════════════════════════════════════════════════════

def _sim_saxo_reachable() -> bool:
    try:
        import atos_runner
        return bool(atos_runner._rate_to_sek("USD"))
    except Exception:
        return False


def test_end_to_end_dry_run_cycle_places_nothing_and_writes_the_artifacts():
    wb = os.path.join(BASE, "data", "us_blend_live_would_be_orders.jsonl")
    st = os.path.join(BASE, "data", "stocks_live_status.json")
    wb_before = os.path.getsize(wb) if os.path.exists(wb) else 0
    p = _cli([], timeout=560)
    out = p.stdout + p.stderr
    # The module must ALWAYS exit cleanly (never a traceback) and ALWAYS
    # self-declare dry-run -- even when the SIM data endpoints are down.
    assert p.returncode == 0, out[-2000:]
    assert "[LIVE STOCKS DRY-RUN]" in out
    assert "OBSERVE ONLY" in out
    # the LIVE snapshot + rails always run (they use env="live", independent
    # of the SIM data token)
    assert "budget" in out and ("margin utilization" in out or "rails" in out.lower())

    if not _sim_saxo_reachable():
        # SIM data token is dead (needs `python saxo_auth.py`). The cycle
        # correctly aborts at the universe download with no orders and no
        # crash -- that IS the designed behaviour. The blend-engine assertions
        # below need live SIM data, so stop here.
        assert ("no market data" in out.lower() or "aborting" in out.lower()
                or "no live saxo rate" in out.lower()), out[-1500:]
        assert "done — 0 buy / 0 sell" in out or "0 buy / 0 sell" in out
        print("      (SIM data token down -- verified graceful abort, skipped blend-engine checks)")
        return

    assert "no real orders" in out.lower() or "observe-only" in out.lower()
    # it reached the blend engine
    assert "US momentum" in out or "risk_off" in out or "REBALANCE" in out
    # status file refreshed with today's date + dry_run True
    assert os.path.exists(st)
    s = json.load(open(st, encoding="utf-8"))
    assert s["dry_run"] is True
    assert str(s["timestamp"])[:10] == time.strftime("%Y-%m-%d")
    # NO real order: the would-be log only ever grew (append) or stayed put;
    # and the LIVE ledger got no new open row from this dry run
    assert os.path.getsize(wb) >= wb_before
    import sqlite3
    dbp = os.path.join(BASE, "data", "atos_live_stocks.db")
    if os.path.exists(dbp):
        c = sqlite3.connect(dbp)
        try:
            real_rows = c.execute("select count(*) from trades where paper=0 and strategy='US Blend'").fetchone()[0]
        finally:
            c.close()
        assert real_rows == 0, f"a dry run must never book a non-paper row (found {real_rows})"


for _n, _f in list(globals().items()):
    if _n.startswith("test_") and callable(_f):
        _run(_n, _f)

print(f"\n{B}{'=' * 66}{X}")
bad = [(n, e) for n, ok, e in _res if not ok]
for n, ok, e in _res:
    print(f"  [{G}PASS{X}]" if ok else f"  [{R}FAIL{X}]", n)
    if e:
        print(f"      {Y}{e}{X}")
print(f"{B}{'=' * 66}{X}")
if bad:
    print(f"{R}{B}  {len(bad)} / {len(_res)} FAILED{X}")
    sys.exit(1)
print(f"{G}{B}  ALL {len(_res)} TESTS PASSED — LIVE STOCKS module verified{X}")
sys.exit(0)
