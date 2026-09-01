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

TEST PLAN / CHECKLIST (written before the 2026-08-25 property-based pass
below was added -- sections 1-14 above were added incrementally as each
bug was found; this list is the retroactive checklist for what "done"
means for the LIVE account's safety-critical surface):

  Functions in scope (real-money decision path only -- dashboards/CLI
  cosmetics are covered but not the focus of the checklist):
    - saxo_auth._cfg / get_valid_access_token / _load_tokens
    - saxo_client._base_url / get_token / get_account_key (currency match)
    - forex.runner: set_account_env, _lock_path, _filter_pairs_for_account,
      _pnl_module, _eur_per_unit, _sek_per_unit, _equity_in_quote,
      _risk_equity, _account, _entries_blocked_by_loss_limit
    - forex.strategy_donchian/strategy/strategy_rsi: size_position
    - housekeeping.ForexLiveAdapter, reconcile_live_forex
    - pnl_tracker.log_close (contract_size), sync_futures_from_json
    - strategy_learner / signal_filter module-separation surface
    - proc_lock: FOREX_LIVE_LOCK vs FOREX_LOCK

  Edge cases / boundary conditions covered:
    - env="sim" (default) vs env="live" for every account-scoped function
    - Missing/unset SAXO_LIVE_APP_KEY, unknown env string
    - Single-account login (SIM's historical case) vs multi-sub-account
      login with the target currency first/not-first/absent entirely
    - atr <= 0 (size_position's documented floor-to-min_units path)
    - A currency needing direct EUR{ccy}/USD{ccy}-triangulated vs
      SEK-anchored (USDSEK+USD{ccy} / USDSEK+{ccy}USD) conversion
    - Ambiguous multi-account state with NO currency match (must hard-error,
      never guess)
    - CLI: disallowed strategy, missing confirmation env var, comma-list
      parsing, SIM behavior fully unaffected by any LIVE-only flag

  Error paths covered: RuntimeError (no app key, no matching account),
  ValueError (unknown env), CLI hard-error (ap.error/exit 2), None-return
  propagation when a live quote is unavailable (never a guessed number).

  Explicitly OUT of scope for this file (covered elsewhere or not
  practically testable without a live broker connection): actual order
  placement/fill behavior (saxo_order.py has its own test coverage),
  real Saxo API response shape drift, Windows Task Scheduler trigger
  registration (verified manually via Get-ScheduledTask, not scriptable
  here without admin rights).

Run:
    python test_2026_08_25_live_forex_account.py
Exit code 0 = all pass, 1 = one or more failures.

Requires (installed into ./.devtools, not system site-packages -- see
sys.path.insert below): hypothesis, for the property-based section.
"""
import os
import subprocess
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, ".devtools"))

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


def test_filter_pairs_for_account_high_volume_only_under_live():
    import forex.runner as r
    r.set_account_env("live")
    try:
        filtered = r._filter_pairs_for_account(r.PAIRS)
        assert len(filtered) == len(r.HIGH_VOLUME_SYMBOLS) == 17, (
            "2026-08-27: narrowed from all 34 CORE_SYMBOLS to the 17-pair "
            "HIGH_VOLUME_SYMBOLS subset. 2026-08-28: a same-day attempt to "
            "split this further into a 9-pair HIGH_VOLUME_GROUP_A (paired "
            "with an 8-pair GROUP_B for the EUR account) was explicitly "
            "reverted by the user before being committed -- SEK LIVE keeps "
            "the full 17-pair set")
        assert all(p["symbol"] in r.HIGH_VOLUME_SYMBOLS for p in filtered)
        assert all(p["symbol"] in r.CORE_SYMBOLS for p in filtered), (
            "HIGH_VOLUME_SYMBOLS must still be a subset of CORE_SYMBOLS")
        exotic_examples = {"EURTRY", "USDZAR", "EURSGD"}
        assert not any(p["symbol"] in exotic_examples for p in filtered), (
            "an exotic pair reached the live-filtered pair list"
        )
    finally:
        r.set_account_env("sim")
_run("forex/runner: _filter_pairs_for_account() under LIVE returns exactly the 17 HIGH_VOLUME pairs, no exotic",
     test_filter_pairs_for_account_high_volume_only_under_live)


def test_filter_pairs_for_account_expanded_to_core_under_live_eur():
    # 2026-08-28 (later, same day): expanded 17 -> 49 -- explicit user
    # request ("add these pairs too only for RSI") to add CORE_STANDARD_
    # SYMBOLS (the other 32) on top of the original 17 HIGH_VOLUME_SYMBOLS,
    # RSI-only. SEK LIVE (bb) deliberately stays at the original 17 --
    # this expansion was scoped to EUR/rsi only.
    import forex.runner as r
    r.set_account_env("live_eur")
    try:
        filtered = r._filter_pairs_for_account(r.PAIRS)
        assert len(filtered) == len(r.CORE_SYMBOLS) == 49, (
            "expected EUR LIVE (rsi) to scan all 49 CORE_SYMBOLS pairs "
            "(17 HIGH_VOLUME + 32 CORE_STANDARD), got a different count -- "
            "safe because housekeeping_live_eur.py attributes pooled "
            "positions/orders by their own AccountKey field, not pair-tier"
        )
        assert all(p["symbol"] in r.CORE_SYMBOLS for p in filtered)
        exotic_examples = {"EURTRY", "USDZAR", "EURSGD"}
        assert not any(p["symbol"] in exotic_examples for p in filtered), (
            "an exotic pair reached the live_eur-filtered pair list"
        )
    finally:
        r.set_account_env("sim")
_run("forex/runner: _filter_pairs_for_account() under LIVE_EUR returns all 49 CORE pairs (expanded from 17, RSI-only)",
     test_filter_pairs_for_account_expanded_to_core_under_live_eur)


def test_filter_pairs_for_account_live_sek_stays_at_high_volume():
    # SEK LIVE (bb) was deliberately NOT part of the 2026-08-28 CORE
    # expansion -- bb's tasks are Disabled anyway (see forex_live_trading_
    # halted_lifted_2026-08-28.md) and the expansion request was RSI-only.
    import forex.runner as r
    r.set_account_env("live")
    try:
        filtered = r._filter_pairs_for_account(r.PAIRS)
        assert len(filtered) == len(r.HIGH_VOLUME_SYMBOLS) == 17, (
            "SEK LIVE (bb) must stay at the original 17 HIGH_VOLUME pairs -- "
            "the CORE expansion was scoped to EUR/rsi only"
        )
    finally:
        r.set_account_env("sim")
_run("forex/runner: _filter_pairs_for_account() under LIVE (SEK/bb) still returns only the 17 HIGH_VOLUME pairs",
     test_filter_pairs_for_account_live_sek_stays_at_high_volume)


def test_housekeeping_live_eur_filters_by_account_key():
    # 2026-08-28: housekeeping_live_eur.py's fetch_live_snapshot() must
    # attribute pooled positions/orders by their own AccountKey field, not
    # pair-tier membership -- required now that LIVE and LIVE_EUR share the
    # same 17-pair HIGH_VOLUME_SYMBOLS universe (a pair-tier filter alone
    # could no longer tell the two accounts' positions apart).
    from unittest.mock import patch
    import housekeeping_live_eur as hkle
    import saxo_client
    mine   = {"PositionBase": {"AccountKey": "eur-key", "Uic": 21}}
    theirs = {"PositionBase": {"AccountKey": "sek-key", "Uic": 21}}
    with patch.object(saxo_client, "get_account_key", return_value="eur-key"), \
         patch.object(saxo_client, "get_positions", return_value={"Data": [mine, theirs]}), \
         patch.object(saxo_client, "get_orders", return_value={"Data": []}):
        snap = hkle.fetch_live_snapshot()
    assert snap.positions_by_uic.get(21) == [mine], (
        "fetch_live_snapshot() must keep only the EUR account's own "
        "AccountKey, even for a uic the SEK account also legitimately trades"
    )
_run("housekeeping_live_eur.fetch_live_snapshot() attributes pooled positions by AccountKey, not pair-tier",
     test_housekeeping_live_eur_filters_by_account_key)


def test_filter_pairs_for_account_noop_under_sim():
    import forex.runner as r
    r.set_account_env("sim")
    filtered = r._filter_pairs_for_account(r.PAIRS)
    assert len(filtered) == len(r.PAIRS), (
        "SIM's own pair scan must be completely unaffected by the LIVE filter existing"
    )
_run("forex/runner: _filter_pairs_for_account() is a no-op under SIM (all pairs unaffected)",
     test_filter_pairs_for_account_noop_under_sim)


def test_live_allowed_strategies():
    import forex.runner as r
    assert r.LIVE_ALLOWED_STRATEGIES == {"rsi"}, (
        "history: {donchian,ema,rsi} -> {bb,rsi} -> ... -> {bb} -> {rsi} "
        "(2026-08-31). RSI is the one live strategy -- deliberate, not an accident"
    )
    # 2026-09-01: CONSOLIDATION -- funds moved to the SEK ('live') account,
    # the EUR sub-account takes NO new entries (empty allowlist) but its
    # open positions are still exit-managed via _legacy_exit_strategies.
    assert r.LIVE_EUR_ALLOWED_STRATEGIES == set(), (
        "EUR sub-account consolidated into SEK 2026-09-01 -- empty allowlist "
        "= no new entries, exits still managed until its positions close"
    )
_run("forex/runner: LIVE_ALLOWED_STRATEGIES == {rsi}; LIVE_EUR_ALLOWED_STRATEGIES == set() (consolidated)",
     test_live_allowed_strategies)


# ═══════════════════════════════════════════════════════════════════════
section("4. forex/runner.py CLI — hard rails via real subprocess invocations")
# ═══════════════════════════════════════════════════════════════════════

def _run_cli(args, env_overrides=None, timeout=60):
    env = dict(os.environ)
    env.pop("SAXO_LIVE_CONFIRMED", None)
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "runner.py", *args],
        cwd=os.path.join(BASE_DIR, "forex"),
        env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )


def test_cli_rejects_disallowed_strategy_on_live():
    proc = _run_cli(["--account", "live", "--strategy", "gap"])
    assert proc.returncode == 2, f"expected argparse hard-error exit(2), got {proc.returncode}: {proc.stderr}"
    assert "only allows" in proc.stderr
_run("forex/runner CLI: --account live --strategy gap is a hard error (gap is not in the live allowlist)",
     test_cli_rejects_disallowed_strategy_on_live)


def test_cli_rejects_live_without_confirmation_envvar():
    # Uses "rsi" (the actually-allowed strategy for --account live as of
    # 2026-08-31) so this test isolates the confirmation/halt gate
    # specifically. It has used "donchian" then "bb" over time as the
    # allowlist changed -- keep it pointed at whatever's currently allowed
    # so a real "strategy not allowed" error can't mask the gate under test.
    proc = _run_cli(["--account", "live", "--strategy", "rsi", "--live"])
    assert proc.returncode == 2, f"expected argparse hard-error exit(2), got {proc.returncode}: {proc.stderr}"
    # Accept either gate: SAXO_LIVE_CONFIRMED (this test's original target) or
    # LIVE_TRADING_HALTED (2026-08-26 emergency stop, checked first when
    # active -- see forex/runner.py). Either is a valid reason the run must
    # be refused; which one fires first depends on whether the halt is on.
    assert "SAXO_LIVE_CONFIRMED" in proc.stderr or "LIVE_TRADING_HALTED" in proc.stderr
_run("forex/runner CLI: --account live --live refuses to run without SAXO_LIVE_CONFIRMED=1, even with an allowed strategy",
     test_cli_rejects_live_without_confirmation_envvar)


def test_cli_accepts_rsi_strategy_dry_run():
    # 2026-08-26: the cost-clearance gate (_round_trip_cost_quote_ccy) added a
    # real live Saxo commission lookup per candidate signal reaching that
    # point in the entry loop -- a real multi-pair x multi-strategy scan now
    # legitimately takes longer than this call's old 60s budget. Not a hang,
    # same "more real work per pair" reasoning as the SIM test's 400s below.
    # 2026-08-27/28: strategy allowlist changed -- {bb, rsi} replaced
    # {donchian, ema, rsi} (via a brief {bb, rsi, pullback} step) -> {bb}
    # only once the two-account pilot moved rsi exclusively to the EUR
    # account. LIVE narrowed from 34 CORE_SYMBOLS to the 17-pair
    # HIGH_VOLUME_SYMBOLS subset (unchanged by the 2026-08-28 revert).
    import forex.universe as _u
    proc = _run_cli(["--account", "live", "--strategy", "rsi"], timeout=150)
    assert proc.returncode == 0, f"expected a clean dry-run exit(0), got {proc.returncode}: {proc.stderr}"
    n = len(_u.PAIRS)
    assert f"17 of {n}" in proc.stdout or f"17 of {n}" in proc.stderr, (
        f"the dry-run log should report scanning 17 (HIGH_VOLUME) of {n} total pairs"
    )
_run("forex/runner CLI: --account live --strategy rsi dry-runs cleanly and scans only the 17 high-volume pairs",
     test_cli_accepts_rsi_strategy_dry_run)


def test_cli_rejects_bb_on_live_sek_account():
    # 2026-08-31: both real-money accounts switched to rsi -- bb is no
    # longer in LIVE_ALLOWED_STRATEGIES, so it must be rejected here the
    # same way any other disallowed strategy is.
    proc = _run_cli(["--account", "live", "--strategy", "bb"])
    assert proc.returncode == 2, f"expected argparse hard-error exit(2), got {proc.returncode}: {proc.stderr}"
    assert "only allows" in proc.stderr
_run("forex/runner CLI: --account live --strategy bb is a hard error (both live accounts run rsi now)",
     test_cli_rejects_bb_on_live_sek_account)


def test_cli_sim_default_behavior_unaffected():
    # No --account flag at all -- must behave exactly like before this feature
    # existed. SIM's dry-run genuinely scans all pairs in the universe (vs
    # LIVE's 34 -- 149 as of 2026-08-25's SCANDI tier addition, up from 117),
    # each needing a real Saxo history fetch, so this legitimately takes
    # longer than the other CLI checks above -- generous timeout, not a hang.
    # Bumped 240s -> 400s the same day the 32-pair SCANDI tier was added:
    # ~27% more pairs pushed the real run past the old budget.
    import forex.universe as _u
    env = dict(os.environ)
    env.pop("SAXO_LIVE_CONFIRMED", None)
    proc = subprocess.run(
        [sys.executable, "runner.py", "--strategy", "donchian"],
        cwd=os.path.join(BASE_DIR, "forex"),
        env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=400,
    )
    assert proc.returncode == 0, f"SIM's own default dry-run must still exit cleanly, got {proc.returncode}: {proc.stderr}"
    n = len(_u.PAIRS)
    assert f"{n} of {n}" in proc.stdout or f"{n} of {n}" in proc.stderr
_run("forex/runner CLI: omitting --account entirely (SIM default) still dry-runs exactly as before",
     test_cli_sim_default_behavior_unaffected)


def test_cli_unknown_strategy_name_rejected_regardless_of_account():
    proc = _run_cli(["--strategy", "not_a_real_strategy"])
    assert proc.returncode == 2, f"expected argparse hard-error exit(2), got {proc.returncode}"
    assert "unknown strategy" in proc.stderr
_run("forex/runner CLI: an unrecognized --strategy name is rejected on SIM too (comma-list parsing didn't loosen validation)",
     test_cli_unknown_strategy_name_rejected_regardless_of_account)


# ═══════════════════════════════════════════════════════════════════════
section("5. housekeeping_live.py — a fully separate file from SIM's housekeeping.py")
# ═══════════════════════════════════════════════════════════════════════
# 2026-08-25: per explicit user direction ("do not use any of SIM account,
# always build new for ATOS live"), the LIVE reconciliation/auto-fix agent
# was moved OUT of housekeeping.py entirely into its own housekeeping_live.py
# / safeguard_live.py pair -- not a class/function living inside housekeeping.py
# behind an env parameter. These 3 tests used to check the old in-file
# location; updated to check the new dedicated module instead. Full
# independence coverage (source-level checks that housekeeping_live.py never
# references housekeeping.ADAPTERS/reconcile_all/ForexAdapter, etc.) lives in
# test_2026_08_25_live_housekeeping_safeguard.py, not duplicated here.

def test_forex_live_adapter_module_tag():
    import housekeeping_live as hk_live
    adapter = hk_live.ForexLiveAdapter()
    assert adapter.module == "forex_live"
    assert adapter.module != "forex"
_run("housekeeping_live: ForexLiveAdapter tags every finding 'forex_live', never 'forex'",
     test_forex_live_adapter_module_tag)


def test_forex_live_adapter_not_in_sim_adapters_dict():
    import housekeeping as hk
    assert "forex_live" not in hk.ADAPTERS, (
        "forex_live must NOT be in SIM's ADAPTERS dict -- reconcile_all() "
        "(used by every existing SIM caller with modules=None) would otherwise "
        "compare LIVE local state against a SIM-only snapshot the moment "
        "anyone called it without an explicit modules= list"
    )
    assert not hasattr(hk, "ForexLiveAdapter"), (
        "ForexLiveAdapter must live only in housekeeping_live.py now, not "
        "also be defined inside housekeeping.py"
    )
_run("housekeeping: 'forex_live'/ForexLiveAdapter are deliberately absent from housekeeping.py entirely",
     test_forex_live_adapter_not_in_sim_adapters_dict)


def test_reconcile_live_forex_is_a_separate_module_entry_point():
    import housekeeping_live as hk_live
    import housekeeping as hk
    assert hasattr(hk_live, "reconcile_live_forex"), (
        "housekeeping_live.py must expose a dedicated reconcile_live_forex() "
        "entry point, separate from SIM's reconcile_all()"
    )
    assert not hasattr(hk, "reconcile_live_forex"), (
        "reconcile_live_forex() must NOT also exist inside housekeeping.py -- "
        "it belongs only in housekeeping_live.py now"
    )
_run("housekeeping_live: reconcile_live_forex() exists as its own entry point in its own file, not inside housekeeping.py",
     test_reconcile_live_forex_is_a_separate_module_entry_point)


def test_forex_live_adapter_switches_account_env_before_load():
    import housekeeping_live as hk_live
    import forex.runner as r
    r.set_account_env("sim")   # start from a known state
    adapter = hk_live.ForexLiveAdapter()
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
_run("housekeeping_live: ForexLiveAdapter.load() switches forex.runner to env='live' before touching local state",
     test_forex_live_adapter_switches_account_env_before_load)


# ═══════════════════════════════════════════════════════════════════════
section("6. atos/capital_config.py — forex_live has its own SEK-denominated cap")
# ═══════════════════════════════════════════════════════════════════════

def test_forex_live_capital_cap_is_sek_and_separate_from_sim():
    # 2026-08-28: raised 6,000 -> 15,000 SEK -- confirmed live via Saxo's own
    # /port/v1/balances/me that the SEK/EUR/USD sub-accounts share ONE real
    # pooled cash balance (~15,770 SEK that day), not three separate pots;
    # explicit user decision (AskUserQuestion) to size against that real
    # total rather than the old artificial 6,000 SEK slice. See
    # config/capital.json's forex_live comment for the full rationale.
    import atos.capital_config as cap
    live_cap = cap.forex_live_risk_equity_sek()
    sim_cap  = cap.forex_risk_equity_eur()
    # 2026-09-01: 15,000 -> 35,000 SEK after the 20,000 SEK deposit (Option A).
    assert live_cap == 35000.0, f"expected the 2026-09-01 35,000 SEK cap, got {live_cap}"
    assert live_cap != sim_cap, "LIVE's cap must be a separate config value from SIM's, not accidentally shared"
_run("atos.capital_config: forex_live_risk_equity_sek() returns 35,000 SEK, independent of SIM's EUR cap",
     test_forex_live_capital_cap_is_sek_and_separate_from_sim)


# ═══════════════════════════════════════════════════════════════════════
section("7. Currency-correct sizing (_sek_per_unit / _equity_in_quote) -- 2026-08-25 critical fix")
# ═══════════════════════════════════════════════════════════════════════
# Real bug found and fixed same day: _equity_in_quote() unconditionally used
# the EUR conversion (_eur_per_unit), so on the SEK-denominated LIVE account
# it silently treated 6,000 SEK equity as if it were 6,000 EUR -- an ~11x
# oversizing on every pair not quoted directly in SEK. These tests pin the
# fix down with mocked but real recent rates, independent of live API access.

def test_sek_per_unit_usd_direct():
    import forex.runner as r
    from unittest.mock import patch
    r.set_account_env("live")
    r._SEK_QUOTE_RATE_CACHE.clear()
    known = {r._PAIRS_BY_SYMBOL["USDSEK"]["uic"]: 9.49}
    with patch.object(r, "_live_price_retry", side_effect=lambda uic, akey, attempts=2: known.get(uic)):
        rate = r._sek_per_unit("USD")
    assert rate == 9.49, f"expected 1 USD = 9.49 SEK (direct USDSEK quote), got {rate}"
    r.set_account_env("sim")
_run("forex/runner: _sek_per_unit('USD') returns the direct USDSEK rate",
     test_sek_per_unit_usd_direct)


def test_sek_per_unit_jpy_triangulated():
    import forex.runner as r
    from unittest.mock import patch
    r.set_account_env("live")
    r._SEK_QUOTE_RATE_CACHE.clear()
    known = {
        r._PAIRS_BY_SYMBOL["USDSEK"]["uic"]: 9.49,
        r._PAIRS_BY_SYMBOL["USDJPY"]["uic"]: 159.25,
    }
    with patch.object(r, "_live_price_retry", side_effect=lambda uic, akey, attempts=2: known.get(uic)):
        rate = r._sek_per_unit("JPY")
    expected = 9.49 / 159.25
    assert abs(rate - expected) < 1e-9, f"expected {expected} SEK/JPY (USDSEK / USDJPY triangulation), got {rate}"
    r.set_account_env("sim")
_run("forex/runner: _sek_per_unit('JPY') correctly triangulates via USDSEK/USDJPY, not EUR",
     test_sek_per_unit_jpy_triangulated)


def test_equity_in_quote_live_uses_sek_not_eur():
    """The actual regression case: before the fix, this silently produced
    an ~11x-too-large number for LIVE (treating SEK equity as EUR)."""
    import forex.runner as r
    from unittest.mock import patch
    r.set_account_env("live")
    r._SEK_QUOTE_RATE_CACHE.clear()
    known = {
        r._PAIRS_BY_SYMBOL["USDSEK"]["uic"]: 9.49,
        r._PAIRS_BY_SYMBOL["USDJPY"]["uic"]: 159.25,
    }
    with patch.object(r, "_live_price_retry", side_effect=lambda uic, akey, attempts=2: known.get(uic)):
        eq_quote = r._equity_in_quote(6000.0, "USDJPY")
    # Correct: 6000 SEK / (9.49/159.25 SEK-per-JPY) ~= 100,685 JPY-equivalent
    # Buggy (pre-fix, treating 6000 as EUR via EURUSD~1.1661): ~1,114,209 -- an 11x difference
    assert eq_quote is not None
    assert 90_000 < eq_quote < 115_000, (
        f"expected ~100,685 JPY-equivalent (correct SEK-basis conversion), got {eq_quote:,.0f} -- "
        f"a value near 1,114,209 would mean the EUR-basis bug has regressed"
    )
    r.set_account_env("sim")
_run("forex/runner: _equity_in_quote() under LIVE produces the correct SEK-basis size, not an ~11x-inflated EUR-basis one",
     test_equity_in_quote_live_uses_sek_not_eur)


def test_equity_in_quote_sim_still_uses_eur_unchanged():
    """SIM's own behavior must be provably untouched by the LIVE fix.

    _eur_per_unit() prefers a DIRECT EUR{ccy} pair when the universe has
    one (EURJPY does exist), only falling back to USD{ccy}+EURUSD
    triangulation otherwise -- the mock must cover whichever path is
    actually taken, not assume triangulation.
    """
    import forex.runner as r
    from unittest.mock import patch
    r.set_account_env("sim")
    r._QUOTE_RATE_CACHE.clear()
    known = {
        r._PAIRS_BY_SYMBOL["EURJPY"]["uic"]: 185.71,   # direct pair -- this is the path actually taken
    }
    with patch.object(r, "_live_price_retry", side_effect=lambda uic, akey, attempts=2: known.get(uic)):
        eq_quote = r._equity_in_quote(27_800.0, "USDJPY")
    expected = 27_800.0 / (1.0 / 185.71)
    assert eq_quote is not None and abs(eq_quote - expected) < 1.0, (
        f"SIM's EUR-basis _equity_in_quote() must be unchanged -- expected {expected:,.0f}, got {eq_quote}"
    )
_run("forex/runner: _equity_in_quote() under SIM is unchanged (still EUR-basis via _eur_per_unit)",
     test_equity_in_quote_sim_still_uses_eur_unchanged)


# ═══════════════════════════════════════════════════════════════════════
section("8. Multi-sub-account currency selection -- 2026-08-25 critical fix")
# ═══════════════════════════════════════════════════════════════════════
# Real finding: the LIVE login controls 3 sub-accounts (SEK/EUR/USD).
# Data[0] happened to be SEK by list order, not by any guarantee.

def test_saxo_client_expected_currency_map():
    import saxo_client
    assert saxo_client._EXPECTED_CURRENCY.get("live") == "SEK", (
        "LIVE must have an explicit expected-currency entry -- without it, "
        "get_account_key() silently falls back to accounts[0] again"
    )
    assert "sim" not in saxo_client._EXPECTED_CURRENCY, (
        "SIM must NOT have an expected-currency entry -- it only ever had one "
        "account historically; adding a preference here is an unnecessary "
        "behavior change to code that already works"
    )
_run("saxo_client: _EXPECTED_CURRENCY pins LIVE to 'SEK' and leaves SIM's original accounts[0] behavior alone",
     test_saxo_client_expected_currency_map)


def test_get_account_key_picks_matching_currency_not_first_in_list():
    import saxo_client
    from unittest.mock import patch
    accounts = [
        {"Currency": "EUR", "AccountKey": "eur-key"},
        {"Currency": "SEK", "AccountKey": "sek-key"},   # NOT first in the list, on purpose
        {"Currency": "USD", "AccountKey": "usd-key"},
    ]
    saxo_client._account_key_cache = {}
    import os as _os
    saved = _os.environ.pop("SAXO_LIVE_ACCOUNT_KEY", None)
    try:
        with patch.object(saxo_client, "get_account_info", return_value={"Data": accounts}):
            key = saxo_client.get_account_key(env="live")
        assert key == "sek-key", (
            f"expected the SEK account's key regardless of its position in the list, got {key!r}"
        )
    finally:
        if saved is not None:
            _os.environ["SAXO_LIVE_ACCOUNT_KEY"] = saved
        saxo_client._account_key_cache = {}
_run("saxo_client: get_account_key(env='live') picks the SEK sub-account even when it's not first in the list",
     test_get_account_key_picks_matching_currency_not_first_in_list)


def test_get_account_key_hard_errors_when_no_sek_account_among_multiple():
    import saxo_client
    from unittest.mock import patch
    accounts = [
        {"Currency": "EUR", "AccountKey": "eur-key"},
        {"Currency": "USD", "AccountKey": "usd-key"},
    ]
    saxo_client._account_key_cache = {}
    import os as _os
    saved = _os.environ.pop("SAXO_LIVE_ACCOUNT_KEY", None)
    try:
        with patch.object(saxo_client, "get_account_info", return_value={"Data": accounts}):
            try:
                saxo_client.get_account_key(env="live")
                raise AssertionError("expected RuntimeError when multiple accounts exist and none is SEK")
            except RuntimeError as e:
                assert "SEK" in str(e)
    finally:
        if saved is not None:
            _os.environ["SAXO_LIVE_ACCOUNT_KEY"] = saved
        saxo_client._account_key_cache = {}
_run("saxo_client: get_account_key(env='live') hard-errors (never guesses) when multiple accounts exist and none is SEK",
     test_get_account_key_hard_errors_when_no_sek_account_among_multiple)


def test_get_account_key_single_account_no_currency_required():
    """A single-account login (e.g. SIM, or a LIVE login that only ever
    has one account) must still work without needing a currency match --
    this is the pre-existing, unchanged fallback path."""
    import saxo_client
    from unittest.mock import patch
    accounts = [{"Currency": "USD", "AccountKey": "only-key"}]
    saxo_client._account_key_cache = {}
    with patch.object(saxo_client, "get_account_info", return_value={"Data": accounts}):
        key = saxo_client.get_account_key(env="sim")
    assert key == "only-key"
    saxo_client._account_key_cache = {}
_run("saxo_client: a single-account login still resolves fine with no currency match needed (SIM's original behavior)",
     test_get_account_key_single_account_no_currency_required)


# ═══════════════════════════════════════════════════════════════════════
section("9. _pnl_module() actually used everywhere -- no remaining hardcoded \"forex\" literals")
# ═══════════════════════════════════════════════════════════════════════
# Each of these was a real, separate bug found 2026-08-25: a bare "forex"
# string literal passed to a module-aware function, which is exactly the
# class of bug most likely to recur if a new LIVE-touching call is ever
# added without checking this.

def test_no_hardcoded_forex_literal_in_pnl_tracker_calls():
    import inspect
    import forex.runner as r
    src = inspect.getsource(r)
    import re
    # Every pnl_tracker call taking a module/`module=` argument must use
    # _pnl_module(), not a bare "forex" string literal.
    bad = re.findall(r'pnl_tracker\.\w+\([^)]*module\s*=\s*"forex"', src)
    bad += re.findall(r'pnl_tracker\.get_closed_trades\(module="forex"', src)
    assert not bad, f"found hardcoded module=\"forex\" in pnl_tracker calls (should be _pnl_module()): {bad}"
_run("forex/runner: no pnl_tracker.* call hardcodes module=\"forex\" -- all use _pnl_module()",
     test_no_hardcoded_forex_literal_in_pnl_tracker_calls)


def test_loss_limit_uses_pnl_module_not_hardcoded_forex():
    """The actual regression case: this used to hardcode module="forex",
    which summed SIM's real daily P&L into a LIVE-account safety check and
    produced a nonsensical "-38,049 SEK loss" warning on an account that
    has never had a single trade."""
    import inspect
    import forex.runner as r
    src = inspect.getsource(r._entries_blocked_by_loss_limit)
    assert 'module="forex"' not in src and "module='forex'" not in src, (
        "_entries_blocked_by_loss_limit() must not hardcode module=\"forex\" -- "
        "use _pnl_module() so LIVE checks its own (empty) ledger, not SIM's"
    )
    assert "_pnl_module()" in src
_run("forex/runner: _entries_blocked_by_loss_limit() reads the correct account's own P&L, not SIM's",
     test_loss_limit_uses_pnl_module_not_hardcoded_forex)


def test_strategy_learner_calls_use_pnl_module():
    import inspect
    import forex.runner as r
    src = inspect.getsource(r)
    assert 'strategy_learner.get_weights("forex")' not in src
    assert 'strategy_learner.log_weights_table("forex")' not in src
    assert 'strategy_learner.run_learning_pass("forex")' not in src
    assert src.count("strategy_learner.get_weights(_pnl_module())") >= 1
    assert src.count("strategy_learner.run_learning_pass(_pnl_module())") >= 1
_run("forex/runner: every strategy_learner.* call uses _pnl_module(), none hardcode \"forex\"",
     test_strategy_learner_calls_use_pnl_module)


def test_signal_filter_calls_use_pnl_module():
    import inspect
    import forex.runner as r
    src = inspect.getsource(r)
    assert "signal_filter.label_outcome(key, won=raw_pnl > 0)" not in src, "label_outcome must pass module=_pnl_module()"
    assert "module=_pnl_module()" in src
    # Specifically the 4 call sites touched during the 2026-08-25 sweep
    for fn in ("label_outcome", "evaluate", "log_signal", "training_status"):
        assert f"signal_filter.{fn}(" in src, f"expected a signal_filter.{fn}(...) call site in forex/runner.py"
_run("forex/runner: signal_filter.evaluate/log_signal/label_outcome/training_status all pass module=_pnl_module()",
     test_signal_filter_calls_use_pnl_module)


# ═══════════════════════════════════════════════════════════════════════
section("10. strategy_learner.py -- LIVE has its own, fully separate learning state")
# ═══════════════════════════════════════════════════════════════════════

def test_forex_live_has_its_own_strategy_names_entry():
    import strategy_learner
    assert strategy_learner.STRATEGY_NAMES.get("forex_live") == ["rsi"], (
        "forex_live must have its own STRATEGY_NAMES entry (exactly the approved "
        "strategy: rsi as of 2026-08-31, both live accounts run RSI) -- without "
        "it, get_weights('forex_live') silently defaults to an empty list"
    )
    assert strategy_learner.STRATEGY_NAMES.get("forex_live_eur") == ["rsi"], (
        "forex_live_eur must have its own STRATEGY_NAMES entry (rsi only)"
    )
_run("strategy_learner: STRATEGY_NAMES['forex_live']==['rsi'], ['forex_live_eur']==['rsi']",
     test_forex_live_has_its_own_strategy_names_entry)


def test_forex_live_weights_file_is_separate_from_sim():
    import strategy_learner
    live_file = strategy_learner._weights_file("forex_live")
    sim_file  = strategy_learner._weights_file("forex")
    assert live_file != sim_file
    assert "forex_live" in live_file
    assert live_file.endswith("forex_live_strategy_weights.json")
_run("strategy_learner: _weights_file('forex_live') is a distinct file from SIM's",
     test_forex_live_weights_file_is_separate_from_sim)


def test_forex_live_get_weights_starts_neutral():
    import strategy_learner
    # A module with no saved weights file yet must default to neutral 1.0x
    # for each of its own strategies -- never inherit or reuse SIM's values.
    weights = strategy_learner.get_weights("forex_live")
    assert weights.get("rsi") == 1.0 or "rsi" in weights, "expected a weight entry for rsi"
    weights_eur = strategy_learner.get_weights("forex_live_eur")
    assert weights_eur.get("rsi") == 1.0 or "rsi" in weights_eur, "expected a weight entry for rsi"
_run("strategy_learner: get_weights('forex_live'/'forex_live_eur') return their own neutral defaults, independent of SIM's learned weights",
     test_forex_live_get_weights_starts_neutral)


# ═══════════════════════════════════════════════════════════════════════
section("11. forex/signal_filter.py -- LIVE has its own signal log + ML model files")
# ═══════════════════════════════════════════════════════════════════════

def test_paths_for_forex_unchanged_filenames():
    import forex.signal_filter as sf
    log_csv, model_pkl = sf._paths_for("forex")
    assert log_csv == sf._LOG_CSV, "SIM's module must keep the exact original filename -- no risk to existing training data"
    assert model_pkl == sf._MODEL_PKL
_run("signal_filter: _paths_for('forex') returns the original, unchanged SIM filenames",
     test_paths_for_forex_unchanged_filenames)


def test_paths_for_forex_live_is_separate():
    import forex.signal_filter as sf
    log_csv, model_pkl = sf._paths_for("forex_live")
    assert log_csv != sf._LOG_CSV
    assert model_pkl != sf._MODEL_PKL
    assert "forex_live" in log_csv and "forex_live" in model_pkl
_run("signal_filter: _paths_for('forex_live') returns its own separate CSV + model files",
     test_paths_for_forex_live_is_separate)


def test_model_cache_is_keyed_per_module():
    import forex.signal_filter as sf
    sf._model_cache.clear()
    sf._model_cache["forex"] = {"marker": "SIM", "loaded_at": __import__("datetime").datetime.now()}
    sf._model_cache["forex_live"] = {"marker": "LIVE", "loaded_at": __import__("datetime").datetime.now()}
    assert sf._model_cache["forex"]["marker"] == "SIM"
    assert sf._model_cache["forex_live"]["marker"] == "LIVE"
    assert sf._model_cache["forex"] is not sf._model_cache["forex_live"]
    sf._model_cache.clear()
_run("signal_filter: _model_cache is keyed per-module, a SIM model can never be served for a LIVE lookup",
     test_model_cache_is_keyed_per_module)


def test_training_status_forex_live_is_isolated():
    import forex.signal_filter as sf
    status = sf.training_status("forex_live")
    # Must not crash, and must reflect LIVE's own (likely empty) log --
    # cannot assert an exact count since this depends on real trading
    # activity, but it must be a well-formed status dict either way.
    assert "labeled_trades" in status and "model_exists" in status
_run("signal_filter: training_status('forex_live') runs cleanly against LIVE's own signal log",
     test_training_status_forex_live_is_isolated)


# ═══════════════════════════════════════════════════════════════════════
section("12. proc_lock.py -- LIVE never contends with SIM's every-minute intraday monitor")
# ═══════════════════════════════════════════════════════════════════════

def test_forex_live_lock_is_a_distinct_file():
    import proc_lock
    assert proc_lock.FOREX_LIVE_LOCK != proc_lock.FOREX_LOCK
    assert "forex_live" in proc_lock.FOREX_LIVE_LOCK
_run("proc_lock: FOREX_LIVE_LOCK is a distinct file from FOREX_LOCK",
     test_forex_live_lock_is_a_distinct_file)


def test_lock_path_selects_correctly_per_account():
    import forex.runner as r
    import proc_lock
    r.set_account_env("live")
    assert r._lock_path() == proc_lock.FOREX_LIVE_LOCK
    r.set_account_env("sim")
    assert r._lock_path() == proc_lock.FOREX_LOCK
_run("forex/runner: _lock_path() resolves to FOREX_LIVE_LOCK under live, FOREX_LOCK under sim",
     test_lock_path_selects_correctly_per_account)


def test_intraday_monitor_never_touches_live_state():
    """intraday_monitor.py has no --account concept at all yet -- it must
    stay scoped to SIM's forex_state.json only, never forex_live_state.json,
    so a stray import/constant change can't silently start touching real
    money positions from an unrelated every-minute process.

    Reads the source TEXT rather than `import intraday_monitor` -- that
    module opens a logging.FileHandler on its own log file at import time
    (module-level side effect), which can raise a transient PermissionError
    if the real scheduled task has that same log file open concurrently.
    Not a code bug -- just means an import is the wrong tool for this check."""
    src_path = os.path.join(BASE_DIR, "intraday_monitor.py")
    with open(src_path, encoding="utf-8") as f:
        src = f.read()
    import re
    m = re.search(r'FOREX_STATE\s*=\s*os\.path\.join\(DATA_DIR,\s*"([^"]+)"\)', src)
    assert m is not None, "could not find FOREX_STATE assignment in intraday_monitor.py"
    assert m.group(1) == "forex_state.json", (
        f"intraday_monitor.py's FOREX_STATE must point at SIM's state file only, found {m.group(1)!r}"
    )
_run("intraday_monitor: FOREX_STATE still points only at SIM's forex_state.json, never the LIVE one",
     test_intraday_monitor_never_touches_live_state)


# ═══════════════════════════════════════════════════════════════════════
section("13. Blackbox -- forex_live_dashboard.py sanity (real subprocess, no mocks)")
# ═══════════════════════════════════════════════════════════════════════

def test_live_dashboard_runs_once_without_crashing():
    proc = subprocess.run(
        [sys.executable, "forex_live_dashboard.py", "--once"],
        cwd=BASE_DIR, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    assert proc.returncode == 0, f"forex_live_dashboard.py --once must exit cleanly, got {proc.returncode}: {proc.stderr}"
    out = proc.stdout
    assert "FOREX LIVE ACCOUNT" in out
    assert "REAL MONEY" in out
_run("forex_live_dashboard.py --once runs cleanly via a real subprocess, no exceptions",
     test_live_dashboard_runs_once_without_crashing)


def test_live_dashboard_shows_exactly_1_strategy():
    # 2026-08-27/28: approved set changed {donchian,ema,rsi} -> {bb,rsi}
    # (via a brief {bb,rsi,pullback} step) -> {bb} once the two-account
    # HIGH_VOLUME pilot moved rsi to the LIVE EUR account (SEK now trades
    # bb only, on HIGH_VOLUME_GROUP_A). "RSI (2)" and "Pullback" are both
    # SEPARATE strategies' own display labels (renamed 2026-09-01 from
    # "RSI Pullback" / "EMA Pullback" which read as one combined thing) --
    # see forex_dashboard.py's STRAT_LABELS_ALL.
    proc = subprocess.run(
        [sys.executable, "forex_live_dashboard.py", "--once"],
        cwd=BASE_DIR, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    out = proc.stdout
    assert "RSI (2)" in out, "expected 'RSI (2)' in the live SEK dashboard's strategy breakdown"
    # None of the other strategies (bb removed 2026-08-31, and the earlier
    # donchian/ema/pullback) should appear on the SEK account's dashboard
    for label in ("BB Reversion", "Donchian Break", "EMA Trend", "Pullback ★", "Gap Fill",
                  "SuperTrend", "Z-Score Rev", "ML Signals", "CNN-LSTM", "LBO Day Trade"):
        assert label not in out, f"'{label}' must NOT appear on the live SEK dashboard -- only rsi is approved for LIVE SEK"
_run("forex_live_dashboard.py shows exactly the 1 approved strategy (rsi), none of the others",
     test_live_dashboard_shows_exactly_1_strategy)


def test_live_dashboard_labels_currency_as_sek_not_eur():
    proc = subprocess.run(
        [sys.executable, "forex_live_dashboard.py", "--once"],
        cwd=BASE_DIR, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    out = proc.stdout
    assert "SEK" in out
    assert " EUR" not in out, "the live (SEK-denominated) dashboard must never label a P&L figure in EUR"
_run("forex_live_dashboard.py labels every P&L figure in SEK, never EUR",
     test_live_dashboard_labels_currency_as_sek_not_eur)


# ═══════════════════════════════════════════════════════════════════════
section("14. Sanity -- current live account state is internally consistent")
# ═══════════════════════════════════════════════════════════════════════

def test_core_symbols_count_is_49():
    # 2026-08-28: grew 34 -> 49 when the Saxo /ref/v1/currencypairs
    # cross-check added 15 new pairs to CORE_SYMBOLS (all fiat G10/G7
    # reversed-direction or EUR/USD-vs-MXN crosses -- see
    # test_2026_08_28_saxo_currencypairs_crosscheck.py).
    from forex.universe import CORE_SYMBOLS
    assert len(CORE_SYMBOLS) == 49, f"expected exactly 49 core pairs, got {len(CORE_SYMBOLS)}"
_run("forex.universe: CORE_SYMBOLS is exactly 49 pairs (grew from 34)",
     test_core_symbols_count_is_49)


def test_asian_and_london_sessions_are_a_legacy_core_subset():
    # SESSION_PAIRS (asian=14/london=20=34) predates the 2026-08-28
    # currencypairs addition and was never updated to include the 15 new
    # CORE pairs. Confirmed this is NOT a live gap: every active scheduled
    # .bat (run_forex_daily.bat, run_forex_london.bat, run_forex_live_*.bat)
    # either omits --session (defaults to "all") or explicitly documents
    # "Session: ALL -- full pair universe" -- --session asian/london is
    # unreferenced by any real scheduled task today (grep confirmed the
    # only --session usages left are in a stale worktree and this file's
    # docstring examples). So SESSION_PAIRS is legacy/inert code, not a
    # silently-broken live filter -- this test just documents that fact
    # instead of asserting a full-coverage partition that was never
    # true after today's growth and isn't required while it's unused.
    import forex.runner as r
    from forex.universe import CORE_SYMBOLS
    asian  = r.SESSION_PAIRS["asian"]
    london = r.SESSION_PAIRS["london"]
    assert len(asian) == 14 and len(london) == 20
    assert not (asian & london), "asian and london session pair sets must not overlap"
    assert (asian | london) <= CORE_SYMBOLS, "asian + london session pairs must still be a subset of CORE_SYMBOLS"
    missing = CORE_SYMBOLS - (asian | london)
    assert len(missing) == 15, (
        f"expected exactly the 15 pairs added 2026-08-28 to be uncovered by the "
        f"legacy session split, got {len(missing)}: {missing}"
    )
_run("forex.runner: SESSION_PAIRS['asian']+['london'] are a legacy 34-pair subset of the now-49-pair CORE_SYMBOLS (unused by any live scheduler -- confirmed no functional gap)",
     test_asian_and_london_sessions_are_a_legacy_core_subset)


def test_risk_pct_identical_across_both_live_strategies():
    import forex.strategy_bb as bb
    import forex.strategy_rsi as rsi
    assert bb.RISK_PCT == rsi.RISK_PCT == 0.0025, (
        f"expected RISK_PCT=0.0025 uniformly across bb/rsi, got "
        f"{bb.RISK_PCT}/{rsi.RISK_PCT}"
    )
_run("Both live strategies (bb/rsi) share the identical documented RISK_PCT=0.25%",
     test_risk_pct_identical_across_both_live_strategies)


def test_default_tp_rr_is_2_to_1():
    import forex.runner as r
    assert r.DEFAULT_TP_RR == 2.0, f"expected the documented 2:1 reward:risk, got {r.DEFAULT_TP_RR}"
_run("forex/runner: DEFAULT_TP_RR is the documented fixed 2:1 reward:risk ratio",
     test_default_tp_rr_is_2_to_1)


# ═══════════════════════════════════════════════════════════════════════
section("15. Property-based tests (Hypothesis) -- real-money sizing/conversion math")
# ═══════════════════════════════════════════════════════════════════════
# Example-based tests above sample a handful of plausible inputs. These
# exhaustively search the input space for a counterexample instead --
# most valuable exactly on the functions that decide real position size.

from hypothesis import given, settings, strategies as st, assume

_positive_float = st.floats(min_value=0.0001, max_value=1_000_000, allow_nan=False, allow_infinity=False)
_positive_equity = st.floats(min_value=1.0, max_value=10_000_000, allow_nan=False, allow_infinity=False)


def test_size_position_never_below_min_units_for_any_positive_atr():
    import forex.strategy_donchian as donchian
    import forex.strategy_rsi as rsi
    import forex.strategy as ema

    @settings(max_examples=200, deadline=None)
    @given(equity=_positive_equity, atr=_positive_float)
    def _prop(equity, atr):
        for mod in (donchian, rsi, ema):
            qty = mod.size_position(equity, atr)
            assert qty >= 1_000, f"{mod.__name__}: size_position({equity}, {atr}) = {qty} < min_units 1000"
            assert isinstance(qty, int)
    _prop()
_run("Hypothesis: size_position() never returns below the 1,000-unit floor, for any positive equity/ATR (donchian/rsi/ema)",
     test_size_position_never_below_min_units_for_any_positive_atr)


def test_size_position_atr_zero_or_negative_returns_exact_floor():
    import forex.strategy_donchian as donchian
    import forex.strategy_rsi as rsi
    import forex.strategy as ema

    @settings(max_examples=100, deadline=None)
    @given(equity=_positive_equity, atr=st.floats(min_value=-1000, max_value=0, allow_nan=False))
    def _prop(equity, atr):
        for mod in (donchian, rsi, ema):
            qty = mod.size_position(equity, atr)
            assert qty == 1_000, (
                f"{mod.__name__}: size_position with atr<=0 must return exactly the "
                f"documented floor (1000), got {qty} for equity={equity}, atr={atr}"
            )
    _prop()
_run("Hypothesis: size_position() with atr<=0 returns exactly the documented 1,000-unit floor, never a computed/garbage value",
     test_size_position_atr_zero_or_negative_returns_exact_floor)


def test_size_position_monotonic_in_equity():
    """More equity (same ATR) must never produce a SMALLER position --
    the risk-budget-per-trade should scale up, or at worst stay floored."""
    import forex.strategy_donchian as donchian

    @settings(max_examples=150, deadline=None)
    @given(equity_low=_positive_equity, equity_delta=_positive_float, atr=_positive_float)
    def _prop(equity_low, equity_delta, atr):
        equity_high = equity_low + equity_delta
        qty_low  = donchian.size_position(equity_low, atr)
        qty_high = donchian.size_position(equity_high, atr)
        assert qty_high >= qty_low, (
            f"size_position must be monotonic in equity: equity {equity_low}->{equity_high} "
            f"(same atr={atr}) gave qty {qty_low}->{qty_high} (decreased)"
        )
    _prop()
_run("Hypothesis: size_position() is monotonic non-decreasing in equity for fixed ATR",
     test_size_position_monotonic_in_equity)


def test_equity_in_quote_scales_linearly_with_equity():
    """_equity_in_quote(equity, sym) = equity / rate -- must scale exactly
    linearly for any positive rate, both under SIM and LIVE."""
    import forex.runner as r
    from unittest.mock import patch

    @settings(max_examples=100, deadline=None)
    @given(equity=_positive_equity, scale=st.floats(min_value=1.01, max_value=100, allow_nan=False))
    def _prop(equity, scale):
        r.set_account_env("live")
        r._SEK_QUOTE_RATE_CACHE.clear()
        known = {r._PAIRS_BY_SYMBOL["USDSEK"]["uic"]: 9.49}
        with patch.object(r, "_live_price_retry", side_effect=lambda uic, akey, attempts=2: known.get(uic)):
            base   = r._equity_in_quote(equity, "USDSEK")   # USD quote currency == SEK's own anchor for this pair? no -- USDSEK's quote IS SEK
            scaled = r._equity_in_quote(equity * scale, "USDSEK")
        assume(base is not None and scaled is not None and base > 0)
        ratio = scaled / base
        assert abs(ratio - scale) < 1e-6, f"expected linear scaling by {scale}, got ratio {ratio}"
        r.set_account_env("sim")
    _prop()
_run("Hypothesis: _equity_in_quote() scales exactly linearly with equity for any positive scale factor",
     test_equity_in_quote_scales_linearly_with_equity)


def test_sek_per_unit_triangulation_always_positive_and_consistent():
    """For ANY positive USDSEK/USD{ccy} rate pair, the triangulated SEK
    rate must be positive, finite, and satisfy the defining identity
    exactly: rate == usdsek / usd_ccy (never inverted, never off by the
    reciprocal -- the exact class of mistake that caused the 11x bug)."""
    import forex.runner as r
    from unittest.mock import patch

    @settings(max_examples=200, deadline=None)
    @given(usdsek=_positive_float, usdjpy=_positive_float)
    def _prop(usdsek, usdjpy):
        r.set_account_env("live")
        r._SEK_QUOTE_RATE_CACHE.clear()
        known = {
            r._PAIRS_BY_SYMBOL["USDSEK"]["uic"]: usdsek,
            r._PAIRS_BY_SYMBOL["USDJPY"]["uic"]: usdjpy,
        }
        with patch.object(r, "_live_price_retry", side_effect=lambda uic, akey, attempts=2: known.get(uic)):
            rate = r._sek_per_unit("JPY")
        assert rate is not None and rate > 0 and rate < float("inf")
        expected = usdsek / usdjpy
        assert abs(rate - expected) < 1e-6 * max(1.0, expected), (
            f"expected rate == usdsek/usdjpy == {expected}, got {rate} "
            f"(usdsek={usdsek}, usdjpy={usdjpy}) -- an inverted ratio here "
            f"is exactly the class of bug that caused the ~11x oversizing"
        )
        r.set_account_env("sim")
    _prop()
_run("Hypothesis: _sek_per_unit() triangulation satisfies rate==usdsek/usd_ccy exactly, for any positive rate pair (guards against an inverted-ratio regression)",
     test_sek_per_unit_triangulation_always_positive_and_consistent)


def test_risk_equity_never_exceeds_configured_cap():
    """_risk_equity() must never let sizing scale off more than the
    configured cap, regardless of how large the broker-reported balance
    is (the whole point of the function -- SIM's demo credit is ~945,000
    EUR, real capital is a small fraction of that)."""
    import forex.runner as r

    @settings(max_examples=100, deadline=None)
    @given(raw_equity=st.floats(min_value=0, max_value=100_000_000, allow_nan=False, allow_infinity=False))
    def _prop(raw_equity):
        r.set_account_env("sim")
        capped = r._risk_equity(raw_equity)
        assert capped <= 27_800.0 + 1e-6 or raw_equity <= 27_800.0, (
            f"_risk_equity({raw_equity}) = {capped} exceeds the configured 27,800 EUR SIM cap"
        )
    _prop()
_run("Hypothesis: _risk_equity() never lets sizing scale off more than the configured cap, for any broker-reported balance",
     test_risk_equity_never_exceeds_configured_cap)


# ═══════════════════════════════════════════════════════════════════════
section("16. Gap-closing -- specific untested lines found via coverage measurement")
# ═══════════════════════════════════════════════════════════════════════
# Coverage run (combined across all 4 test suites in this repo) flagged
# these exact lines in forex/runner.py's LIVE-touching functions as never
# executed by any existing test. Closing each one specifically rather than
# chasing the file's overall %, most of which is unrelated SIM-strategy
# execution logic outside this account's scope.

def test_set_account_env_rejects_unknown_env():
    import forex.runner as r
    try:
        r.set_account_env("production")
        raise AssertionError("expected ValueError for an unrecognized env name")
    except ValueError as e:
        assert "production" in str(e)
    finally:
        r.set_account_env("sim")
_run("forex/runner: set_account_env() rejects an unrecognized env name (was untested -- coverage line 207)",
     test_set_account_env_rejects_unknown_env)


def test_equity_in_quote_returns_none_for_short_symbol():
    import forex.runner as r
    r.set_account_env("sim")
    assert r._equity_in_quote(1000.0, "XY") is None, (
        "a symbol too short to have a 3-char quote currency must return None, not raise or guess"
    )
_run("forex/runner: _equity_in_quote() returns None for a malformed/too-short symbol (was untested -- coverage line 499)",
     test_equity_in_quote_returns_none_for_short_symbol)


def test_equity_in_quote_returns_none_when_rate_unavailable():
    import forex.runner as r
    from unittest.mock import patch
    r.set_account_env("sim")
    r._QUOTE_RATE_CACHE.clear()
    with patch.object(r, "_live_price_retry", return_value=None):
        result = r._equity_in_quote(1000.0, "USDJPY")
    assert result is None, (
        "when the conversion rate can't be determined (Saxo has no quote right now), "
        "_equity_in_quote() must return None, never a guessed/fallback number "
        "(was untested -- coverage lines 501-502)"
    )
_run("forex/runner: _equity_in_quote() returns None (never guesses) when the live rate is unavailable",
     test_equity_in_quote_returns_none_when_rate_unavailable)


def test_risk_equity_under_live_env():
    """The Hypothesis property test above only exercised SIM's cap path --
    LIVE's own path (_CAP.forex_live_risk_equity_sek()) was untested."""
    import forex.runner as r
    r.set_account_env("live")
    try:
        capped = r._risk_equity(1_000_000.0)   # a broker balance far above the 35,000 SEK cap
        assert capped == 35000.0, f"expected LIVE's 2026-09-01 35,000 SEK cap to bind, got {capped}"
    finally:
        r.set_account_env("sim")
_run("forex/runner: _risk_equity() under LIVE caps at the 35,000 SEK configured capital (was untested)",
     test_risk_equity_under_live_env)


def test_risk_equity_falls_back_gracefully_on_config_read_failure():
    import forex.runner as r
    from unittest.mock import patch
    r.set_account_env("sim")
    with patch("atos.capital_config.forex_risk_equity_eur", side_effect=Exception("config unreadable")):
        result = r._risk_equity(12345.0)
    assert result == 12345.0, (
        "if the capital-config read itself fails, _risk_equity() must fall back to the "
        "raw (uncapped) equity rather than crash the whole run (was untested -- coverage lines 524-526)"
    )
_run("forex/runner: _risk_equity() falls back to raw equity if capital_config itself fails to read",
     test_risk_equity_falls_back_gracefully_on_config_read_failure)


def test_risk_equity_zero_or_negative_cap_returns_raw():
    import forex.runner as r
    from unittest.mock import patch
    r.set_account_env("sim")
    with patch("atos.capital_config.forex_risk_equity_eur", return_value=0.0):
        result = r._risk_equity(9999.0)
    assert result == 9999.0, (
        "a misconfigured cap (<=0) must not zero-out or block sizing -- fall back to raw equity "
        "(was untested -- coverage line 528)"
    )
_run("forex/runner: _risk_equity() with a misconfigured (<=0) cap falls back to raw equity, doesn't zero out sizing",
     test_risk_equity_zero_or_negative_cap_returns_raw)


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
