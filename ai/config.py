"""
ai/config.py -- the single AI kill switch.

Every AI touchpoint anywhere in the codebase calls ai_enabled_for(env)
before doing anything. Flip config/ai.json's "enabled_sim" to false (or
delete the file) and every AI hook goes inert on the next cycle, without
touching forex/runner.py.

Fail-safe contract (Sprint 0 test gate):
  * missing config/ai.json            -> disabled
  * malformed JSON                    -> disabled (logged, never a crash)
  * "live" / "live_eur"               -> ALWAYS disabled, hardcoded here,
                                        regardless of what the file says.
                                        LIVE is not reachable from the
                                        current implementation plan at all.
"""

from __future__ import annotations

import json
import os

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONFIG_PATH = os.path.join(_BASE_DIR, "config", "ai.json")

# Accounts the AI layer is even *allowed* to touch. LIVE accounts are
# excluded in CODE, not just config -- promoting AI to a LIVE account
# requires its own separate written decision (see the roadmap's governance
# rules) and a change here, not a config flip.
_AI_ALLOWED_ACCOUNTS = {"sim"}

_DEFAULTS = {
    "enabled_sim": False,
    "shadow_mode": True,     # even when enabled, only observe -- until a sprint flips it
}


def _load() -> dict:
    """config/ai.json merged over defaults. Any failure -> defaults
    (i.e. disabled). Never raises."""
    cfg = dict(_DEFAULTS)
    try:
        if os.path.exists(_CONFIG_PATH):
            with open(_CONFIG_PATH, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for k in _DEFAULTS:
                    if k in data:
                        cfg[k] = data[k]
    except Exception as exc:  # malformed JSON, permissions, anything
        try:
            print(f"[ai.config] could not read {_CONFIG_PATH}: {exc} -- AI disabled", flush=True)
        except Exception:
            pass
    return cfg


def ai_enabled_for(account_env: str) -> bool:
    """True only if the AI layer may run for this account. Hard-off for any
    account not in _AI_ALLOWED_ACCOUNTS (all LIVE), and for SIM only when
    config/ai.json explicitly enables it."""
    if account_env not in _AI_ALLOWED_ACCOUNTS:
        return False
    return bool(_load().get("enabled_sim", False))


def shadow_mode(account_env: str = "sim") -> bool:
    """True = AI observes/logs only, never changes an order. Defaults True
    and only a later sprint (with its own evidence gate) flips it. If AI
    isn't enabled at all, this is moot but still returns True (safe)."""
    if not ai_enabled_for(account_env):
        return True
    return bool(_load().get("shadow_mode", True))


def config_path() -> str:
    return _CONFIG_PATH
