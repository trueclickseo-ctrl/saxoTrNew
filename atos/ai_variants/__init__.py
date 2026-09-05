"""
atos/ai_variants/__init__.py
----------------------------
AI-owned parameter variants for the ATOS SIM paper twin.

Written exclusively by ai/agent/strategy_evolver.py — never by hand.
The main deterministic code never imports from here.
Only the ai_sim run path (run_us_blend_ai, run_us_reversion account_env=ai_sim)
reads these params and applies them via context managers.

Phase 1 (params only): evolver proposes bounded parameter overrides.
Phase 2 (future): evolver may write variant .py files for logic changes.
"""
from __future__ import annotations

import contextlib
import json
import os
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))

# ── Hard bounds ─────────────────────────────────────────────────────────────
# Evolver cannot write values outside these ranges. Applied at load time.
BLEND_BOUNDS: dict[str, tuple] = {
    "LOOKBACK":      (60,   180),
    "MOM_THRESHOLD": (0.02, 0.15),
    "TARGET_VOL":    (0.10, 0.25),
    "REBAL_DAYS":    (7,    30),
}
REVERSION_BOUNDS: dict[str, tuple] = {
    "RSI_ENTRY":     (25,  45),
    "RSI_EXIT":      (55,  75),
    "DIP_PCT":       (0.02, 0.10),
    "VOL_MULT":      (1.0,  3.0),
    "MAX_HOLD_DAYS": (5,   20),
}


def _load(fname: str) -> dict:
    path = os.path.join(_HERE, fname)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _validate(raw: dict, bounds: dict[str, tuple]) -> dict[str, Any]:
    """Return only params within their allowed range."""
    out: dict = {}
    for k, v in raw.items():
        if k not in bounds:
            continue
        lo, hi = bounds[k]
        try:
            v = float(v) if isinstance(v, str) else v
            if lo <= v <= hi:
                out[k] = v
        except (TypeError, ValueError):
            pass
    return out


def load_blend_params() -> dict:
    """Return validated AI blend param overrides (empty = no change)."""
    return _validate(_load("us_blend_params.json"), BLEND_BOUNDS)


def load_reversion_params() -> dict:
    """Return validated AI reversion param overrides (empty = no change)."""
    return _validate(_load("us_reversion_params.json"), REVERSION_BOUNDS)


@contextlib.contextmanager
def _patch_module(module, overrides: dict):
    """Temporarily set module attributes, restore on exit."""
    saved: dict = {}
    for k, v in overrides.items():
        if hasattr(module, k):
            saved[k] = getattr(module, k)
            setattr(module, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(module, k, v)


def blend_context():
    """Context manager: applies validated AI blend param overrides to us_momentum module."""
    from atos import us_momentum as USM  # noqa: PLC0415
    params = load_blend_params()
    if params:
        print(f"  [ai_variants] blend overrides active: {params}")
    return _patch_module(USM, params)


def reversion_context():
    """Context manager: applies validated AI reversion param overrides to us_reversion module."""
    from atos import us_reversion as USR  # noqa: PLC0415
    params = load_reversion_params()
    if params:
        print(f"  [ai_variants] reversion overrides active: {params}")
    return _patch_module(USR, params)
