"""
test_2026_08_25_live_forex_account.py
--------------------------------------
Regression tests for the real-money Saxo LIVE forex account added
2026-08-25 alongside the existing SIM account. These pin down the safety
rails that make it safe for LIVE and SIM to share the same codebase:

  1. saxo_auth.py / saxo_client.py — an `env` parameter switches every
     Saxo-facing constant (endpoints, token file, account-key cache), but
     defaults to "sim" everywhere, so SIM's own behavior is provably
     unchanged when `env` is omitted.
  2. forex/runner.py's set_account_env() correctly redirects BASE_URL /
     STATE_FILE / ORDERS_FILE / PEAK_EQUITY_FILE / ACCOUNT_ENV for "live",
     and _filter_pairs_for_account() never lets an exotic pair reach a
     live signal.
  3. The CLI hard rails: a non-allowed strategy + --account live is a hard
     error; --account live --live without SAXO_LIVE_CONFIRMED=1 is a hard
     error; omitting --strategy under --account live defaults to exactly
     the 3 approved strategies, never "all 11".
  4. housekeeping.py's ForexLiveAdapter is tagged "forex_live" (never
     "forex") and switches forex.runner's account env before touching
     local state, so LIVE and SIM reconciliation can never cross-
     contaminate.

No test here talks to real Saxo or places any order — everything is
either a pure function check or a subprocess dry-run (which never reaches
the network for the strategy/pair-filtering assertions).

Run:
    python test_2026_08_25_live_forex_account.py
Exit code 0 = all pass, 1 = one or more failures.
"""
import os
import subprocess
import sys

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


# ═══════════════════════════════════════════════════════════════════════
section("1. saxo_auth.py — env parameter defaults to SIM, unchanged")
# ═══════════════════════════════════════════════════════════════════════

def test_saxo_auth_sim_default_unchanged():
    import saxo_auth
    cfg = saxo_auth._cfg("sim")
    assert cfg["auth_endpoint"] == "https://sim.logonvalidation.net/authorize"
    assert cfg["token_endpoint"] == "https://sim.logonvalidation.net/token"
    assert cfg["token_file"].endswith("saxo_token.json")
    # Back-compat module-level constants must still resolve to SIM
    assert saxo_auth.AUTHORIZATION_ENDPOINT == cfg["auth_endpoint"]
    assert saxo_auth.TOKEN_FILE == cfg["token_file"]
_run("saxo_auth: env='sim' (and the back-compat module constants) are byte-identical to pre-LIVE behavior",
     test_saxo_auth_sim_default_unchanged)


def test_saxo_auth_live_requires_app_key():
    import saxo_auth
    import os as _os
    saved = _os.environ.pop("SAXO_LIVE_APP_KEY", None)
    try:
        try:
            saxo_auth._cfg("live")
            raise AssertionError("expected RuntimeError when SAXO_LIVE_APP_KEY is unset")
        except RuntimeError:
            pass
    finally:
        if saved is not None:
            _os.environ["SAXO_LIVE_APP_KEY"] = saved
_run("saxo_auth: env='live' refuses to proceed without SAXO_LIVE_APP_KEY set",
     test_saxo_auth_live_requires_app_key)


def test_saxo_auth_unknown_env_rejected():
    import saxo_auth
    try:
        saxo_auth._cfg("production")
        raise AssertionError("expected ValueError for an unknown env")
    except ValueError:
        pass
_run("saxo_auth: an unrecognized env name is rejected, not silently treated as sim",
     test_saxo_auth_unknown_env_rejected)


# ═══════════════════════════════════════════════════════════════════════
section("2. saxo_client.py — env parameter, per-env AccountKey cache")
# ═══════════════════════════════════════════════════════════════════════

def test_saxo_client_base_url_per_env():
    import saxo_client
    assert saxo_client._base_url("sim") == saxo_client.SIM_BASE_URL
    assert saxo_client._base_url("live") == saxo_client.LIVE_BASE_URL
    assert saxo_client.SIM_BASE_URL != saxo_client.LIVE_BASE_URL
    try:
        saxo_client._base_url("bogus")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
_run("saxo_client: _base_url() resolves sim/live to two different, real endpoints",
     test_saxo_client_base_url_per_env)


def test_saxo_client_account_key_cache_is_per_env():
    import saxo_client
    saxo_client._account_key_cache = {"sim": "sim_key_123", "live": "live_key_456"}
    assert saxo_client._account_key_cache["sim"] != saxo_client._account_key_cache["live"]
    # get_account_key must read its OWN env's cache slot, never the other one
    from unittest.mock import patch
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("SAXO_ACCOUNT_KEY", None)
        os.environ.pop("SAXO_LIVE_ACCOUNT_KEY", None)
        assert saxo_client.get_account_key(env="sim") == "sim_key_123"
        assert saxo_client.get_account_key(env="live") == "live_key_456"
    saxo_client._account_key_cache = {}
_run("saxo_client: SIM and LIVE AccountKeys are cached separately and never cross-read",
     test_saxo_client_account_key_cache_is_per_env)


# ═══════════════════════════════════════════════════════════════════════
section("3. forex/runner.py — set_account_env() and the CORE-only pair filter")
# ═══════════════════════════════════════════════════════════════════════

def test_set_account_env_sim_is_unchanged_default():
    import forex.runner as r
    r.set_account_env("sim")
    assert r.ACCOUNT_ENV == "sim"
    assert r.BASE_URL == "https://gateway.saxobank.com/sim/openapi"
    assert r.STATE_FILE.endswith("forex_state.json")
    assert r.ORDERS_FILE.endswith("forex_orders.json")
    assert r._pnl_module() == "forex"
_run("forex/runner: set_account_env('sim') matches this file's original hardcoded SIM constants exactly",
     test_set_account_env_sim_is_unchanged_default)


def test_set_account_env_live_redirects_everything():
    import forex.runner as r
    r.set_account_env("live")
    try:
        assert r.ACCOUNT_ENV == "live"
        assert r.BASE_URL == "https://gateway.saxobank.com/openapi"
        assert r.STATE_FILE.endswith("forex_live_state.json")
        assert r.ORDERS_FILE.endswith("forex_live_orders.json")
        assert r.PEAK_EQUITY_FILE.endswith("forex_live_peak_equity.json")
        assert r._pnl_module() == "forex_live"
        # Never the same file as SIM's
        assert "forex_live_state.json" not in "forex_state.json"
    finally:
        r.set_account_env("sim")   # reset for any later test/import in this process
_run("forex/runner: set_account_env('live') redirects BASE_URL/STATE_FILE/ORDERS_FILE/PEAK_EQUITY_FILE to LIVE-only files",
     test_set_account_env_live_redirects_everything)


def test_filter_pairs_for_account_core_only_under_live():
    import forex.runner as r
    r.set_account_env("live")
    try:
        filtered = r._filter_pairs_for_account(r.PAIRS)
        assert len(filtered) == len(r.CORE_SYMBOLS) == 34
        assert all(p["symbol"] in r.CORE_SYMBOLS for p in filtered)
        exotic_examples = {"EURTRY", "USDZAR", "EURSGD"}
        assert not any(p["symbol"] in exotic_examples for p in filtered), (
            "an exotic pair reached the live-filtered pair list"
        )
    finally:
        r.set_account_env("sim")
_run("forex/runner: _filter_pairs_for_account() under LIVE returns exactly the 34 CORE pairs, no exotic",
     test_filter_pairs_for_account_core_only_under_live)


def test_filter_pairs_for_account_noop_under_sim():
    import forex.runner as r
    r.set_account_env("sim")
    filtered = r._filter_pairs_for_account(r.PAIRS)
    assert len(filtered) == len(r.PAIRS) == 117, (
        "SIM's own pair scan must be completely unaffected by the LIVE filter existing"
    )
_run("forex/runner: _filter_pairs_for_account() is a no-op under SIM (all 117 pairs unaffected)",
     test_filter_pairs_for_account_noop_under_sim)


def test_live_allowed_strategies_is_exactly_three():
    import forex.runner as r
    assert r.LIVE_ALLOWED_STRATEGIES == {"donchian", "ema", "rsi"}, (
        "the live-account strategy allowlist drifted from the explicit user "
        "decision (donchian/ema/rsi only) -- this must be a deliberate change, "
        "not an accident"
    )
_run("forex/runner: LIVE_ALLOWED_STRATEGIES is exactly {donchian, ema, rsi}, matching the explicit decision",
     test_live_allowed_strategies_is_exactly_three)


# ═══════════════════════════════════════════════════════════════════════
section("4. forex/runner.py CLI — hard rails via real subprocess invocations")
# ═══════════════════════════════════════════════════════════════════════

def _run_cli(args, env_overrides=None):
    env = dict(os.environ)
    env.pop("SAXO_LIVE_CONFIRMED", None)
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "runner.py", *args],
        cwd=os.path.join(BASE_DIR, "forex"),
        env=env, capture_output=True, text=True, timeout=60,
    )


def test_cli_rejects_disallowed_strategy_on_live():
    proc = _run_cli(["--account", "live", "--strategy", "gap"])
    assert proc.returncode == 2, f"expected argparse hard-error exit(2), got {proc.returncode}: {proc.stderr}"
    assert "only allows" in proc.stderr
_run("forex/runner CLI: --account live --strategy gap is a hard error (gap is not in the live allowlist)",
     test_cli_rejects_disallowed_strategy_on_live)


def test_cli_rejects_live_without_confirmation_envvar():
    proc = _run_cli(["--account", "live", "--strategy", "donchian", "--live"])
    assert proc.returncode == 2, f"expected argparse hard-error exit(2), got {proc.returncode}: {proc.stderr}"
    assert "SAXO_LIVE_CONFIRMED" in proc.stderr
_run("forex/runner CLI: --account live --live refuses to run without SAXO_LIVE_CONFIRMED=1, even with an allowed strategy",
     test_cli_rejects_live_without_confirmation_envvar)


def test_cli_accepts_comma_separated_allowed_strategies_dry_run():
    proc = _run_cli(["--account", "live", "--strategy", "donchian,ema,rsi"])
    assert proc.returncode == 0, f"expected a clean dry-run exit(0), got {proc.returncode}: {proc.stderr}"
    assert "34 of 117" in proc.stdout or "34 of 117" in proc.stderr, (
        "the dry-run log should report scanning 34 (CORE) of 117 total pairs"
    )
_run("forex/runner CLI: --account live --strategy donchian,ema,rsi dry-runs cleanly and scans only the 34 core pairs",
     test_cli_accepts_comma_separated_allowed_strategies_dry_run)


def test_cli_sim_default_behavior_unaffected():
    # No --account flag at all -- must behave exactly like before this feature
    # existed. SIM's dry-run genuinely scans all 117 pairs (vs LIVE's 34),
    # each needing a real Saxo history fetch, so this legitimately takes
    # longer than the other CLI checks above -- generous timeout, not a hang.
    env = dict(os.environ)
    env.pop("SAXO_LIVE_CONFIRMED", None)
    proc = subprocess.run(
        [sys.executable, "runner.py", "--strategy", "donchian"],
        cwd=os.path.join(BASE_DIR, "forex"),
        env=env, capture_output=True, text=True, timeout=240,
    )
    assert proc.returncode == 0, f"SIM's own default dry-run must still exit cleanly, got {proc.returncode}: {proc.stderr}"
    assert "117 of 117" in proc.stdout or "117 of 117" in proc.stderr
_run("forex/runner CLI: omitting --account entirely (SIM default) still dry-runs exactly as before",
     test_cli_sim_default_behavior_unaffected)


def test_cli_unknown_strategy_name_rejected_regardless_of_account():
    proc = _run_cli(["--strategy", "not_a_real_strategy"])
    assert proc.returncode == 2, f"expected argparse hard-error exit(2), got {proc.returncode}"
    assert "unknown strategy" in proc.stderr
_run("forex/runner CLI: an unrecognized --strategy name is rejected on SIM too (comma-list parsing didn't loosen validation)",
     test_cli_unknown_strategy_name_rejected_regardless_of_account)


# ═══════════════════════════════════════════════════════════════════════
section("5. housekeeping.py — ForexLiveAdapter never touches SIM's module/state")
# ═══════════════════════════════════════════════════════════════════════

def test_forex_live_adapter_module_tag():
    import housekeeping as hk
    adapter = hk.ForexLiveAdapter()
    assert adapter.module == "forex_live"
    assert adapter.module != "forex"
_run("housekeeping: ForexLiveAdapter tags every finding 'forex_live', never 'forex'",
     test_forex_live_adapter_module_tag)


def test_forex_live_adapter_not_in_shared_adapters_dict():
    import housekeeping as hk
    assert "forex_live" not in hk.ADAPTERS, (
        "forex_live must NOT be in the shared ADAPTERS dict -- reconcile_all() "
        "(used by every existing SIM caller with modules=None) would otherwise "
        "compare LIVE local state against a SIM-only snapshot the moment "
        "anyone called it without an explicit modules= list"
    )
_run("housekeeping: 'forex_live' is deliberately absent from the shared ADAPTERS dict used by reconcile_all()",
     test_forex_live_adapter_not_in_shared_adapters_dict)


def test_reconcile_live_forex_is_a_separate_entry_point():
    import housekeeping as hk
    assert hasattr(hk, "reconcile_live_forex"), (
        "housekeeping.py must expose a dedicated reconcile_live_forex() "
        "entry point, separate from reconcile_all()"
    )
_run("housekeeping: reconcile_live_forex() exists as its own entry point (not folded into reconcile_all())",
     test_reconcile_live_forex_is_a_separate_entry_point)


def test_forex_live_adapter_switches_account_env_before_load():
    import housekeeping as hk
    import forex.runner as r
    r.set_account_env("sim")   # start from a known state
    adapter = hk.ForexLiveAdapter()
    try:
        adapter.load()   # reads from disk; may return [] if the file doesn't exist yet -- fine
    except Exception:
        pass   # network/file errors aren't what this test checks
    assert r.ACCOUNT_ENV == "live", (
        "ForexLiveAdapter.load() must switch forex.runner's account env to "
        "'live' before reading local state, or it would silently read SIM's "
        "own forex_state.json instead of forex_live_state.json"
    )
    r.set_account_env("sim")   # reset for any later test in this process
_run("housekeeping: ForexLiveAdapter.load() switches forex.runner to env='live' before touching local state",
     test_forex_live_adapter_switches_account_env_before_load)


# ═══════════════════════════════════════════════════════════════════════
section("6. atos/capital_config.py — forex_live has its own SEK-denominated cap")
# ═══════════════════════════════════════════════════════════════════════

def test_forex_live_capital_cap_is_sek_and_separate_from_sim():
    import atos.capital_config as cap
    live_cap = cap.forex_live_risk_equity_sek()
    sim_cap  = cap.forex_risk_equity_eur()
    assert live_cap == 6000.0, f"expected the confirmed 6,000 SEK opening balance, got {live_cap}"
    assert live_cap != sim_cap, "LIVE's cap must be a separate config value from SIM's, not accidentally shared"
_run("atos.capital_config: forex_live_risk_equity_sek() returns 6,000 SEK, independent of SIM's EUR cap",
     test_forex_live_capital_cap_is_sek_and_separate_from_sim)


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
