"""
Sprint 0 test gate -- ai/config.py, the AI kill switch.

Contract: ships OFF; missing/malformed config -> disabled (never a crash);
LIVE accounts are hard-off in code regardless of config content.
"""

import json
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

GREEN, RED, YELLOW, RESET, BOLD = "\033[92m", "\033[91m", "\033[93m", "\033[0m", "\033[1m"
_results = []


def _run(name, fn):
    try:
        fn()
        _results.append((name, True, None))
    except Exception as e:
        import traceback
        _results.append((name, False, f"{type(e).__name__}: {e}\n{traceback.format_exc()}"))


import ai.config as aic

_REAL = aic._CONFIG_PATH
_TMP = os.path.join(BASE_DIR, "config", "_test_ai.json")


def _point_at(content: str | None):
    aic._CONFIG_PATH = _TMP
    if content is None:
        if os.path.exists(_TMP):
            os.remove(_TMP)
    else:
        with open(_TMP, "w", encoding="utf-8") as f:
            f.write(content)


def _restore():
    aic._CONFIG_PATH = _REAL
    if os.path.exists(_TMP):
        os.remove(_TMP)


def test_killswitch_defaults_are_off_in_code():
    # The FAIL-SAFE DEFAULTS in code must stay OFF, independent of whatever
    # the (deliberately mutable) committed config/ai.json currently sets --
    # since 2026-08-31 that file is ENABLED for the live shadow study. The
    # contract that must never regress is: no config, or a config missing a
    # key, falls back to disabled + shadow.
    assert aic._DEFAULTS["enabled_sim"] is False
    assert aic._DEFAULTS["enabled_live_shadow"] is False
    assert aic._DEFAULTS.get("enabled_ai_sim") is False
    assert aic._DEFAULTS["agent_enabled"] is False
    assert aic._DEFAULTS["shadow_mode"] is True
    # NO real-money account can ever be an acting account, no matter the
    # config. The acting set is SIM paper only: "sim" (gated by shadow_mode)
    # and "ai_sim" (the AI-decision paper twin, 2026-09-03).
    for _live in ("live", "live_eur", "live_stocks"):
        assert _live not in aic._AI_ACTING_ACCOUNTS
        assert aic.can_apply_decision(_live) is False
    assert aic._AI_ACTING_ACCOUNTS == {"sim", "ai_sim"}
_run("kill-switch fail-safe defaults are OFF in code (config file is mutable)",
     test_killswitch_defaults_are_off_in_code)


def test_missing_file_is_disabled():
    try:
        _point_at(None)
        assert aic.ai_enabled_for("sim") is False
        assert aic.shadow_mode("sim") is True
    finally:
        _restore()
_run("missing config/ai.json -> disabled (and shadow_mode stays True)", test_missing_file_is_disabled)


def test_malformed_json_is_disabled_not_a_crash():
    try:
        _point_at("{ this is not valid json ")
        assert aic.ai_enabled_for("sim") is False   # no exception
    finally:
        _restore()
_run("malformed config -> disabled, no crash", test_malformed_json_is_disabled_not_a_crash)


def test_enabled_sim_true_enables_only_sim():
    try:
        _point_at(json.dumps({"enabled_sim": True, "shadow_mode": True}))
        assert aic.ai_enabled_for("sim") is True
        assert aic.ai_enabled_for("live") is False, "LIVE must never be enabled from config"
        assert aic.ai_enabled_for("live_eur") is False
        assert aic.ai_enabled_for("futures") is False
    finally:
        _restore()
_run("enabled_sim:true -> ON for sim only; live/live_eur stay OFF", test_enabled_sim_true_enables_only_sim)


def test_live_can_shadow_but_never_act():
    try:
        # config turns on LIVE shadow AND tries to take it out of shadow_mode
        # AND enables the agent -- LIVE must still be log-only.
        _point_at(json.dumps({"enabled_live_shadow": True, "shadow_mode": False,
                              "agent_enabled": True}))
        # observe/log: allowed
        assert aic.ai_enabled_for("live") is True
        assert aic.ai_enabled_for("live_eur") is True
        # act: NEVER, hardcoded -- not in _AI_ACTING_ACCOUNTS
        assert aic.shadow_mode("live") is True, "LIVE is always shadow, config cannot flip it"
        assert aic.shadow_mode("live_eur") is True
        assert aic.can_apply_decision("live") is False
        assert aic.can_apply_decision("live_eur") is False
        assert "sim" in aic._AI_ACTING_ACCOUNTS
        assert "live" not in aic._AI_ACTING_ACCOUNTS and "live_eur" not in aic._AI_ACTING_ACCOUNTS
        # a totally unknown account is off for everything
        assert aic.ai_enabled_for("futures") is False
        assert aic.can_apply_decision("futures") is False
    finally:
        _restore()
_run("LIVE may shadow-log (enabled_live_shadow) but can NEVER act -- hardcoded", test_live_can_shadow_but_never_act)


def test_live_shadow_off_by_default():
    try:
        _point_at(json.dumps({"enabled_sim": True}))   # enabled_live_shadow omitted
        assert aic.ai_enabled_for("live") is False
        assert aic.ai_enabled_for("live_eur") is False
    finally:
        _restore()
_run("enabled_live_shadow defaults False -- LIVE shadow is opt-in", test_live_shadow_off_by_default)


def test_can_apply_decision_sim_gating():
    try:
        # sim, agent on, shadow off -> the ONLY True case
        _point_at(json.dumps({"enabled_sim": True, "agent_enabled": True, "shadow_mode": False}))
        assert aic.can_apply_decision("sim") is True
        # shadow back on -> False
        _point_at(json.dumps({"enabled_sim": True, "agent_enabled": True, "shadow_mode": True}))
        assert aic.can_apply_decision("sim") is False
        # agent off -> False even out of shadow
        _point_at(json.dumps({"enabled_sim": True, "agent_enabled": False, "shadow_mode": False}))
        assert aic.can_apply_decision("sim") is False
    finally:
        _restore()
_run("can_apply_decision(sim) is True only with agent on AND shadow_mode off", test_can_apply_decision_sim_gating)


def test_agent_cost_controls():
    try:
        _point_at(json.dumps({"enabled_sim": True}))   # defaults
        assert aic.agent_strategy_allowed("rsi") is True
        assert aic.agent_strategy_allowed("ema") is False, "default scope is rsi only"
        assert aic.agent_dedup_enabled() is True
        _point_at(json.dumps({"enabled_sim": True, "agent_strategies": ["*"], "agent_dedup": False}))
        assert aic.agent_strategy_allowed("ema") is True
        assert aic.agent_dedup_enabled() is False
        _point_at(json.dumps({"enabled_sim": True, "agent_strategies": ["rsi", "bb"]}))
        assert aic.agent_strategy_allowed("bb") is True and aic.agent_strategy_allowed("gap") is False
    finally:
        _restore()
_run("agent_strategies scope + agent_dedup switch behave", test_agent_cost_controls)


def test_shadow_mode_defaults_true_when_enabled():
    try:
        _point_at(json.dumps({"enabled_sim": True}))   # shadow_mode omitted
        assert aic.shadow_mode("sim") is True, "shadow_mode must default True"
        _point_at(json.dumps({"enabled_sim": True, "shadow_mode": False}))
        assert aic.shadow_mode("sim") is False, "an explicit shadow_mode:false is honoured for sim"
    finally:
        _restore()
_run("shadow_mode defaults True; explicit false is honoured (for sim only)", test_shadow_mode_defaults_true_when_enabled)


def test_package_imports_clean():
    import ai, ai.regime, ai.features, ai.agent  # noqa: F401
_run("ai / ai.regime / ai.features / ai.agent import cleanly", test_package_imports_clean)


print(f"\n{BOLD}{'='*66}{RESET}")
failed = [(n, e) for n, ok, e in _results if not ok]
for name, ok, err in _results:
    print(f"  [{GREEN}PASS{RESET}]" if ok else f"  [{RED}FAIL{RESET}]", name)
    if err:
        print(f"      {YELLOW}{err}{RESET}")
print(f"{BOLD}{'='*66}{RESET}")
if failed:
    print(f"{RED}{BOLD}  {len(failed)} / {len(_results)} FAILED{RESET}")
    sys.exit(1)
print(f"{GREEN}{BOLD}  ALL {len(_results)} TESTS PASSED{RESET}")
sys.exit(0)
