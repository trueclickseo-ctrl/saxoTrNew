"""
2026-09-03 -- AI SIM TWIN: forex/runner.py --account ai_sim + atos_ai_stocks.py.

A SIM PAPER book where the AI's decision IS applied (Copilot resize/skip for
forex; the basket-ranker's re-ranked pick for stocks) -- a live forward A/B
vs the deterministic SIM books, on ai_dashboard.py.

Verifies: the config gates (ai_sim acts, LIVE never does), the forex account
env is isolated + paper-only, basket_ranker returns its decision and degrades
safely, run_us_blend_ai runs only US Blend, the new modules parse, and the
scheduler tasks are SIM-named / auto-fixable.
"""

import ast
import importlib
import inspect
import os
import sys

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


# ── config ──────────────────────────────────────────────────────────────

def test_ai_sim_is_a_paper_acting_account_but_live_is_not():
    import ai.config as c
    assert "ai_sim" in c._AI_SHADOW_ACCOUNTS
    assert "ai_sim" in c._AI_ACTING_ACCOUNTS
    for live in ("live", "live_eur", "live_stocks"):
        assert live not in c._AI_ACTING_ACCOUNTS
        assert c.can_apply_decision(live) is False


def test_ai_sim_gates_flip_with_the_config_and_ignore_shadow_mode():
    import ai.config as c
    real = c._load
    try:
        c._load = lambda: {"enabled_ai_sim": True, "agent_enabled": True, "shadow_mode": True}
        assert c.ai_enabled_for("ai_sim") is True
        assert c.shadow_mode("ai_sim") is False          # not gated by shadow_mode
        assert c.can_apply_decision("ai_sim") is True
        assert c.can_apply_decision("sim") is False       # sim still shadowed
        c._load = lambda: {}                              # all off
        assert c.can_apply_decision("ai_sim") is False
    finally:
        c._load = real


def test_basket_ranker_applies_only_for_ai_sim():
    import ai.config as c
    real = c._load
    try:
        c._load = lambda: {"enabled_ai_sim": True, "agent_enabled": True,
                           "stocks_ai": {"enabled": True, "basket_ranker_blend": True},
                           "stocks": {"enabled": True, "basket_ranker_blend": True},
                           "stocks_live": {"enabled": True, "basket_ranker_blend": True},
                           "enabled_sim": True, "enabled_live_shadow": True}
        assert c.basket_ranker_applies("ai_sim") is True
        assert c.basket_ranker_applies("sim") is False
        assert c.basket_ranker_applies("live_stocks") is False
    finally:
        c._load = real


# ── forex/runner ────────────────────────────────────────────────────────

def test_forex_ai_sim_account_is_isolated_and_paper_only():
    import forex.runner as r
    try:
        r.set_account_env("ai_sim")
        assert r.ACCOUNT_ENV == "ai_sim"
        assert r._pnl_module() == "forex_ai"
        assert r._paper_only_account() is True
        assert r._sim_paper_fill_enabled() is True
        assert os.path.basename(r.STATE_FILE) == "forex_state_ai.json"
        assert "sim/openapi" in r.BASE_URL          # SIM gateway (quotes only)
        import proc_lock
        assert r._lock_path() == proc_lock.FOREX_AI_LOCK
    finally:
        r.set_account_env("sim")


def test_forex_cli_accepts_ai_sim():
    import subprocess
    p = subprocess.run([sys.executable, "-X", "utf8", "runner.py", "--account", "ai_sim", "--help"],
                       cwd=os.path.join(BASE, "forex"), capture_output=True, text=True, timeout=60)
    assert p.returncode == 0
    assert "ai_sim" in p.stdout


def test_saxo_auth_normalises_ai_sim_to_sim():
    import saxo_auth
    assert saxo_auth._cfg("ai_sim")["token_endpoint"] == saxo_auth._cfg("sim")["token_endpoint"]


# ── basket_ranker ───────────────────────────────────────────────────────

def test_rank_basket_shadow_returns_its_decision_and_degrades_safely():
    from ai.features import basket_ranker as br
    # no ANTHROPIC_API_KEY in the test env -> the agent call fails ->
    # the returned row must still carry ai_offense == the deterministic pick
    row = br.rank_basket_shadow(det_offense=["AAA", "BBB", "CCC"], det_defense=["DDD"],
                                det_count=3, detail={}, regime_label="BULL",
                                mom_n_max=6, account_env="ai_sim")
    assert isinstance(row, dict)
    assert row["ai_offense"] == ["AAA", "BBB", "CCC"]
    assert row["changed"] is False
    assert row["account_env"] == "ai_sim"


# ── atos_runner / atos_ai_stocks ────────────────────────────────────────

def test_run_us_blend_ai_runs_only_blend():
    import atos_runner
    src = inspect.getsource(atos_runner.run_us_blend_ai)
    assert 'account_env="ai_sim"' in src
    assert "run_us_momentum(" in src and "run_us_reversion(" not in src
    assert "can_apply_decision(" not in src


def test_run_us_momentum_swaps_the_basket_only_when_it_applies():
    import atos_runner
    src = inspect.getsource(atos_runner.run_us_momentum)
    assert "basket_ranker_applies(account_env)" in src
    assert 'tgt["momentum"] = _ai_off' in src


def test_blend_book_state_includes_the_ai_sim_book():
    import atos_runner
    bs = atos_runner._blend_book_state()
    assert set(bs) == {"sim", "live_stocks", "ai_sim"}


def test_new_modules_parse():
    for m in ("atos_ai_stocks.py", "ai_dashboard.py", "setup_scheduler_ai_twin.ps1",
              "run_forex_ai_scan.bat", "run_atos_ai_stocks.bat"):
        p = os.path.join(BASE, m)
        assert os.path.exists(p), m
        if m.endswith(".py"):
            ast.parse(open(p, encoding="utf-8").read())


def test_ai_dashboard_renders_without_the_twin_books():
    d = importlib.import_module("ai_dashboard")
    out = d.render()
    assert "AI SIM TWIN" in out and "FOREX" in out and "STOCKS" in out
    assert "\033[" in out                                  # colours present
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        d._emit(out)
    assert "\033[" not in buf.getvalue()                   # stripped for a pipe


# ── scheduler ───────────────────────────────────────────────────────────

def test_ai_twin_tasks_are_sim_named_and_autofixable():
    import scheduler_watchdog as w
    assert "Forex AI Twin Scan" in w.WINDOWS_TASKS
    assert "Stocks AI Twin" in w.WINDOWS_TASKS
    assert "Forex AI Twin Scan" in w.INTRADAY_REPEATING_TASKS
    assert "Stocks AI Twin" not in w.INTRADAY_REPEATING_TASKS
    # SIM-named -> AUTO_FIX_ELIGIBLE keeps them; no LIVE task is ever there
    assert "Forex AI Twin Scan" in w.AUTO_FIX_ELIGIBLE
    assert not any("LIVE" in n for n in w.AUTO_FIX_ELIGIBLE)


def test_bats_run_the_twin_not_the_deterministic_books():
    daily = open(os.path.join(BASE, "run_forex_ai_scan.bat"), encoding="utf-8").read()
    assert "--account ai_sim" in daily
    stx = open(os.path.join(BASE, "run_atos_ai_stocks.bat"), encoding="utf-8").read()
    assert "atos_ai_stocks.py" in stx and "atos_runner.py" not in stx


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
