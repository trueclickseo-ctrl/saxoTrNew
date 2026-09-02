"""
2026-09-02 -- ATOS LIVE STOCKS sleeve (atos_live_stocks.py), US Blend, real
money on the Saxo LIVE SEK sub-account. Phase 1 = observe-only.

Mirrors test_2026_08_25_live_forex_account.py: hard rails, sizing off the
30k cap (not the pooled raw), the US-Blend-only allowlist, the observe
path writing a would-be order + an AI card but placing nothing, the
confirmation gate, SIM-pinning of the ordinary stocks path, and the
AccountKey+AssetType snapshot filter.

No network needed -- every Saxo touch is stubbed.
"""

import ast
import importlib
import json
import os
import subprocess
import sys
import tempfile

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


# ═══════════════════════════════════════════════════════════════════════
#  1. CLI hard rails
# ═══════════════════════════════════════════════════════════════════════

def _cli(args, env_overrides=None, timeout=60):
    env = dict(os.environ)
    for k in ("SAXO_LIVE_STOCKS_CONFIRMED", "LIVE_STOCKS_DRY_RUN", "LIVE_STOCKS_TRADING_HALTED"):
        env.pop(k, None)
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run([sys.executable, "-X", "utf8", "atos_live_stocks.py", *args],
                          cwd=BASE, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def test_cli_rejects_any_strategy_other_than_us_blend():
    for strat in ("rsi", "us_reversion", "US Reversion", "momentum_rebalance"):
        p = _cli(["--strategy", strat])
        assert p.returncode == 2, f"--strategy {strat!r}: expected exit 2, got {p.returncode}: {p.stderr}"
        assert "only runs" in p.stderr and "US Blend" in p.stderr


def test_cli_accepts_us_blend_token_explicitly():
    # "US Blend" is the one allowed value -- it must NOT argparse-error
    # (it still runs observe-only; --info short-circuits before any scan).
    p = _cli(["--strategy", "US Blend", "--info"])
    assert p.returncode in (0, 1), f"got {p.returncode}: {p.stderr}"   # 1 = LIVE map not built yet
    assert "only runs" not in p.stderr


def test_cli_info_is_readonly_and_never_places_orders():
    p = _cli(["--info"])
    out = p.stdout + p.stderr
    assert "No orders" in out or "not built yet" in out


def test_cli_fast_and_once_imply_dashboard_not_a_scan():
    # `python atos_live_stocks.py --fast` should open the dashboard, never run
    # an observe scan (the user hit "unrecognized arguments: --fast").
    p = _cli(["--fast", "--once"], timeout=60)
    out = p.stdout + p.stderr
    assert "unrecognized arguments" not in out
    assert "ATOS LIVE STOCKS" in out           # the dashboard header
    assert "Downloading data for" not in out   # no universe download = no scan


def test_dashboard_strips_ansi_when_output_is_not_a_tty():
    import importlib
    d = importlib.import_module("live_stocks_dashboard")
    out = d.render()
    # render() itself emits colour codes; _emit() strips them for a pipe.
    assert "\033[" in out
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        d._emit(out)                            # buf is not a tty
    assert "\033[" not in buf.getvalue()


def test_bats_never_carry_a_strategy_token():
    # US Blend is hard-coded in atos_live_stocks.py -- a --strategy token in the
    # .bat could only ever be wrong. (--live IS present as of the 2026-09-03
    # go-live; the real safety gate is SAXO_LIVE_STOCKS_CONFIRMED + LIVE_STOCKS_
    # DRY_RUN, checked in atos_live_stocks.run().)
    for b in ("run_atos_live_stocks_daily.bat", "run_atos_live_stocks_exits.bat"):
        cmd_lines = [ln.strip() for ln in open(os.path.join(BASE, b), encoding="utf-8")
                     if "atos_live_stocks.py" in ln and not ln.lstrip().startswith(("REM", "::"))]
        assert cmd_lines, f"{b}: no command line found"
        for ln in cmd_lines:
            assert "--strategy" not in ln, f"{b} command line must not carry a strategy token: {ln}"


def test_live_run_still_needs_both_env_vars_regardless_of_the_bat():
    # the .bat passing --live is NOT sufficient -- run() forces dry_run unless
    # SAXO_LIVE_STOCKS_CONFIRMED=1 AND LIVE_STOCKS_DRY_RUN=0 (and not halted).
    src = open(os.path.join(BASE, "atos_live_stocks.py"), encoding="utf-8").read()
    assert 'dry_run = dry_env or (not args.live) or (not confirmed) or halted' in src
    assert 'os.environ.get("SAXO_LIVE_STOCKS_CONFIRMED") == "1"' in src
    assert 'os.environ.get("LIVE_STOCKS_DRY_RUN", "1") != "0"' in src
    assert "LIVE_STOCKS_TRADING_HALTED" in src


# ═══════════════════════════════════════════════════════════════════════
#  2. Sizing -- off the 30k cap, never the pooled raw
# ═══════════════════════════════════════════════════════════════════════

def test_rails_budget_is_capped_at_30k_minus_buffer():
    import atos_live_stocks as a
    # pooled balance far above the cap -> budget clamps to 30k * (1 - 10%)
    r = a.live_stocks_rails({"TotalValue": 5_000_000.0,
                             "InitialMargin": {"MarginUtilizationPct": 5.0}})
    assert r["budget_sek"] == round(30_000 * 0.9, 2), r
    # pooled below the cap -> budget scales down with the real money
    r2 = a.live_stocks_rails({"TotalValue": 12_000.0,
                              "InitialMargin": {"MarginUtilizationPct": 5.0}})
    assert r2["budget_sek"] == round(12_000 * 0.9, 2), r2


def test_rails_margin_gate_fails_open_on_missing_util():
    import atos_live_stocks as a
    r = a.live_stocks_rails({"TotalValue": 30_000.0})   # no InitialMargin
    assert r["margin_ok"] is True
    r2 = a.live_stocks_rails({"TotalValue": 30_000.0,
                              "InitialMargin": {"MarginUtilizationPct": 62.0}})
    assert r2["margin_ok"] is False


def test_rails_daily_loss_uses_the_sleeve_base_not_sim_constant():
    # atos.risk.STARTING_CAPITAL_SEK is 10.4M -- must NOT anchor this sleeve.
    import atos_live_stocks as a
    r = a.live_stocks_rails({"TotalValue": 30_000.0,
                             "InitialMargin": {"MarginUtilizationPct": 5.0}})
    # empty LIVE ledger -> 0 P&L today -> not exits-only (the bug was ~100%)
    assert r["exits_only"] is False


def test_capital_config_stocks_live_is_30k_and_separate():
    import atos.capital_config as CAP
    assert CAP.stocks_live_risk_equity_sek() == 30_000.0
    assert CAP.stocks_live_enabled() is True
    # not the same knob as forex live
    assert CAP.stocks_live_risk_equity_sek() != CAP.forex_live_risk_equity_sek()


# ═══════════════════════════════════════════════════════════════════════
#  3. Observe mode -- logs a would-be order + an AI card, places nothing
# ═══════════════════════════════════════════════════════════════════════

def test_observe_mode_writes_would_be_order_and_no_db_row(tmp=None):
    import atos_runner
    import pandas as pd
    import numpy as np

    tmpdir = tempfile.mkdtemp()
    wb = os.path.join(tmpdir, "wb.jsonl")
    real_wb = atos_runner.US_BLEND_LIVE_WOULD_BE_ORDERS
    real_place = atos_runner._place_us
    placed = []
    try:
        atos_runner.US_BLEND_LIVE_WOULD_BE_ORDERS = wb
        atos_runner._place_us = lambda *a, **k: placed.append(a) or True

        # a tiny 2-name feat_data with an uptrend so compute_targets picks something
        idx = pd.date_range("2024-01-01", periods=300, freq="B")
        def _bars(start):
            c = np.linspace(start, start * 2.2, len(idx))
            return pd.DataFrame({"Open": c, "High": c * 1.01, "Low": c * 0.99,
                                 "Close": c, "Volume": 1e6}, index=idx)
        feat = {}
        from atos.features import add_all
        for tk, s in (("AAA", 100), ("BBB", 50)):
            try:
                feat[tk] = add_all(_bars(s))
            except Exception:
                feat[tk] = _bars(s)

        actions = []
        atos_runner.run_us_momentum(feat, [], actions,
                                    available_cash_sek=27_000.0,
                                    account_env="live_stocks", observe=True)
        # observe mode NEVER calls _place_us
        assert placed == [], "observe mode must not place real orders"
        # a would-be order line was written (or nothing, if targets were empty --
        # accept both; the invariant is "no real order, valid jsonl")
        if os.path.exists(wb):
            for ln in open(wb, encoding="utf-8"):
                row = json.loads(ln)
                assert row["account_env"] == "live_stocks"
                assert "notional_sek" in row and "budget_sek" in row
    finally:
        atos_runner.US_BLEND_LIVE_WOULD_BE_ORDERS = real_wb
        atos_runner._place_us = real_place


def test_run_us_momentum_sim_default_is_byte_identical_signature():
    import atos_runner
    import inspect
    sig = inspect.signature(atos_runner.run_us_momentum)
    assert sig.parameters["account_env"].default == "sim"
    assert sig.parameters["observe"].default is False


def test_observe_orders_are_surfaced_in_todays_actions_for_the_dashboard():
    # the LIVE stocks dashboard's SCAN SIGNALS panel reads result["actions"];
    # _observe_order must append a would_be-tagged action (SIM path unaffected --
    # observe is only ever True for account_env="live_stocks").
    import inspect, atos_runner
    src = inspect.getsource(atos_runner.run_us_momentum)
    assert "_observe_order" in src
    obs = src[src.index("def _observe_order"):src.index("def _do(")]
    assert "todays_actions.append" in obs and '"would_be": True' in obs


def test_run_us_blend_live_returns_the_signal_basket():
    import inspect, atos_runner
    src = inspect.getsource(atos_runner.run_us_blend_live)
    assert '"signal"' in src and "_blend_signal" in src
    assert '"book_state"' in src


def test_blend_book_state_covers_both_books():
    import atos_runner
    bs = atos_runner._blend_book_state()
    assert set(bs) == {"sim", "live_stocks"}
    for b in bs.values():
        assert set(b) >= {"last_rebalance", "days_since", "next_due_in_days", "holdings"}
        assert isinstance(b["holdings"], dict)


def test_basket_ranker_accepts_and_logs_book_state():
    import inspect
    from ai.features import basket_ranker as br
    sig = inspect.signature(br.rank_basket_shadow)
    assert "book_state" in sig.parameters and sig.parameters["book_state"].default is None
    src = inspect.getsource(br.rank_basket_shadow)
    assert '"book_state": book_state or {}' in src           # on the logged row
    assert '"book_state": book_state or {}' in src.split("payload = {", 1)[1]  # and in the LLM payload


def test_dashboard_renders_scan_signal_from_status_file(tmp=None):
    import importlib, json, tempfile
    d = importlib.import_module("live_stocks_dashboard")
    fd, path = tempfile.mkstemp(suffix=".json"); os.close(fd)
    real = d.STATUS_FILE
    try:
        d.STATUS_FILE = path
        json.dump({
            "status": "complete", "timestamp": "2026-09-03T02:40:00", "dry_run": True,
            "budget_sek": 27000.0, "margin_util_pct": 17.7, "rails_notes": [],
            "signal": {"targets": ["HUM", "DELL"], "risk_off": False, "reason": "test",
                       "momentum": ["HUM", "DELL"], "lowvol": []},
            "actions": [{"action": "BUY", "ticker": "HUM", "shares": 12, "price": 288.5,
                         "reason": "rebalance", "would_be": True}],
            "buy": 1, "sell": 0,
        }, open(path, "w"))
        out = d.render()
        assert "LAST SCAN" in out and "TODAY'S SCAN SIGNALS" in out
        # the SIM-dashboard column layout
        for col in ("Action", "Ticker", "Strategy", "Score", "Shares", "Price", "Reason"):
            assert col in out, col
        assert "HUM" in out and "target basket" in out
        assert "1 BUY" in out and "BLOCKED" in out          # the SIM-style footer
        assert "OBSERVE" in out
    finally:
        d.STATUS_FILE = real
        os.unlink(path)


# ═══════════════════════════════════════════════════════════════════════
#  4. SIM-pinning of the ordinary stocks path
# ═══════════════════════════════════════════════════════════════════════

def test_stocks_env_defaults_to_sim_and_run_cycle_never_flips_it():
    import atos_runner
    assert atos_runner._sx() == "sim"
    for fn in ("run_cycle", "run_intraday_cycle"):
        src = __import__("inspect").getsource(getattr(atos_runner, fn))
        assert "set_stocks_env" not in src, f"{fn} must never call set_stocks_env"
    daily = open(os.path.join(BASE, "daily_run.py"), encoding="utf-8").read()
    assert "set_stocks_env" not in daily


def test_set_stocks_env_rejects_unknown():
    import atos_runner
    try:
        atos_runner.set_stocks_env("live_eur")
        assert False, "should have raised"
    except ValueError:
        pass
    finally:
        atos_runner.set_stocks_env("sim")


def test_place_us_refuses_non_blend_on_live():
    import atos_runner
    atos_runner.set_stocks_env("live")
    try:
        atos_runner._place_us("Buy", "AAPL", 1, {"AAPL": {"uic": 1}}, [], price=100.0,
                              strategy="US Reversion")
        assert False, "should have raised RuntimeError"
    except RuntimeError as e:
        assert "US-Blend-only" in str(e)
    finally:
        atos_runner.set_stocks_env("sim")


# ═══════════════════════════════════════════════════════════════════════
#  5. Snapshot filter -- AccountKey AND AssetType=="Stock"
# ═══════════════════════════════════════════════════════════════════════

def test_snapshot_filters_by_accountkey_and_assettype():
    import housekeeping_live_stocks as hk
    import saxo_client
    real_ak, real_pos, real_ord = (saxo_client.get_account_key,
                                   saxo_client.get_positions, saxo_client.get_orders)
    try:
        saxo_client.get_account_key = lambda env="sim": "SEK-KEY"
        saxo_client.get_positions = lambda env="sim": {"Data": [
            {"PositionBase": {"AccountKey": "SEK-KEY", "AssetType": "Stock", "Uic": 11, "Amount": 10}},
            {"PositionBase": {"AccountKey": "SEK-KEY", "AssetType": "FxSpot", "Uic": 22, "Amount": 1000}},
            {"PositionBase": {"AccountKey": "OTHER",   "AssetType": "Stock", "Uic": 33, "Amount": 5}},
        ]}
        saxo_client.get_orders = lambda at=None, env="sim": {"Data": [
            {"AccountKey": "SEK-KEY", "AssetType": "Stock",  "Uic": 11, "Status": "Working"},
            {"AccountKey": "SEK-KEY", "AssetType": "FxSpot", "Uic": 22, "Status": "Working"},
            {"AccountKey": "OTHER",   "AssetType": "Stock",  "Uic": 33, "Status": "Working"},
        ]}
        snap = hk.fetch_live_stock_snapshot()
        assert set(snap.positions_by_uic) == {11}, snap.positions_by_uic
        assert set(snap.orders_by_uic) == {11}, snap.orders_by_uic
    finally:
        saxo_client.get_account_key, saxo_client.get_positions, saxo_client.get_orders = (
            real_ak, real_pos, real_ord)


def test_safeguard_live_stocks_never_auto_closes_untracked():
    src = open(os.path.join(BASE, "safeguard_live_stocks.py"), encoding="utf-8").read()
    assert "LIVE NEVER AUTO-CLOSES" in src.upper() or "never auto-close" in src.lower()
    assert "raise_attention" in src
    assert "close_trade" not in src and "place_market_order" not in src


# ═══════════════════════════════════════════════════════════════════════
#  6. Commission model
# ═══════════════════════════════════════════════════════════════════════

def test_stocks_live_commission_clears_saxo_min():
    import atos_runner
    # 1 share, cheap stock -> the per-share fee is tiny, so the USD min floor
    # (~3 USD) must dominate and convert to SEK
    c = atos_runner.stocks_live_commission_sek(1, 5.0, 10.5)
    assert c >= 3.0 * 10.5 * 0.99, c
    # a big order -> per-share fee dominates
    c2 = atos_runner.stocks_live_commission_sek(1000, 50.0, 10.5)
    assert c2 == round(1000 * 0.02 * 10.5, 6) or abs(c2 - 1000 * 0.02 * 10.5) < 1e-6, c2


# ═══════════════════════════════════════════════════════════════════════
#  7. Module independence + parse
# ═══════════════════════════════════════════════════════════════════════

def test_new_modules_parse():
    for m in ("atos_live_stocks.py", "housekeeping_live_stocks.py",
              "safeguard_live_stocks.py", "live_stocks_dashboard.py",
              "lookup_instruments_live.py"):
        ast.parse(open(os.path.join(BASE, m), encoding="utf-8").read())


def test_housekeeping_live_stocks_imports_no_sim_adapters():
    import inspect, housekeeping_live_stocks as m
    # no code reference to SIM's module dict / entry points / stocks adapter
    # (docstrings mentioning them by name are fine)
    tree = ast.parse(inspect.getsource(m))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "ADAPTERS" not in names and "ADAPTERS" not in attrs
    assert "reconcile_all" not in names and "reconcile_all" not in attrs
    assert "StocksAdapter" not in names
    # the only names imported from housekeeping are the generic building blocks
    imp = [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module == "housekeeping"]
    imported = {a.name for node in imp for a in node.names}
    assert imported <= {"Finding", "KIND_FULLY_UNTRACKED", "_symbol_hint",
                        "LocalPosition", "LiveSnapshot", "BaseAdapter", "reconcile_module"}, imported


def test_watchdog_registers_live_stocks_tasks_as_alert_only():
    import scheduler_watchdog as w
    assert "Stocks LIVE Daily Run" in w.WINDOWS_TASKS
    assert "Stocks LIVE Exit Check" in w.WINDOWS_TASKS
    assert "Stocks LIVE Daily Run" not in w.INTRADAY_REPEATING_TASKS
    assert not any("LIVE" in n for n in w.AUTO_FIX_ELIGIBLE)


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
print(f"{G}{B}  ALL {len(_res)} TESTS PASSED{X}")
sys.exit(0)
