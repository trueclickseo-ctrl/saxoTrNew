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


def test_ships_off_on_clean_checkout():
    # the real committed config/ai.json must have AI disabled
    with open(_REAL, encoding="utf-8") as f:
        cfg = json.load(f)
    assert cfg.get("enabled_sim") is False, "config/ai.json must ship with enabled_sim=false"
    assert aic.ai_enabled_for("sim") is False, "AI must be OFF for sim on a clean checkout"
_run("ships OFF: committed config/ai.json disables AI for sim", test_ships_off_on_clean_checkout)


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


def test_live_hard_off_even_if_config_tries():
    try:
        # a config that maliciously/accidentally tries to turn LIVE on
        _point_at(json.dumps({"enabled_sim": True, "enabled_live": True, "enabled_live_eur": True}))
        assert aic.ai_enabled_for("live") is False
        assert aic.ai_enabled_for("live_eur") is False
        assert "sim" in aic._AI_ALLOWED_ACCOUNTS and "live" not in aic._AI_ALLOWED_ACCOUNTS
    finally:
        _restore()
_run("LIVE is hard-off in code -- config cannot enable it", test_live_hard_off_even_if_config_tries)


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
