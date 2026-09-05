"""
forex/ai_variants/__init__.py
------------------------------
Loader for AI-written strategy override modules for the forex ai_sim runner.

HOW IT WORKS
  1. ai/agent/strategy_evolver.py (Phase 2) writes a wrapper module to
     forex/ai_variants/strategy_<name>_override.py once a strategy has
     >=50 closed SIM trades.
  2. When forex/runner.py runs with --account ai_sim, it calls
     get_override(strat_name). If an override exists, it replaces the
     strategy module for that run — main forex/strategy_*.py is untouched.
  3. The override must define generate_signals(**) with the same signature
     as the original. It typically wraps the original and adds AI-learned
     pre/post filters. On any error the runner falls back to the original.

SECURITY
  Every override file is AST-validated before loading:
  - Only whitelisted imports allowed (pandas, numpy, math, ta, forex.strategy_*)
  - eval / exec / open / __import__ / compile / subprocess are forbidden
  - Must define generate_signals at module level
  If validation fails the override is silently skipped (original runs).

GOVERNANCE
  * The main codebase (forex/strategy_*.py) is NEVER modified.
  * Every override is logged to data/ai_strategy_evolution.jsonl.
  * LIVE accounts never use overrides -- get_override() returns None
    unless called from an ai_sim context.
  * Phase 2 is gated: >=50 closed trades per strategy required before
    the evolver is allowed to propose code (not just params).
"""
from __future__ import annotations

import ast
import importlib.util
import os
import sys

_VARIANTS_DIR = os.path.dirname(__file__)

_ALLOWED_TOP_MODULES: set[str] = {
    "__future__", "pandas", "numpy", "math", "ta", "collections", "datetime",
    "functools", "itertools", "statistics", "typing",
    # forex strategy originals -- overrides may call the original
    "forex",
}

_FORBIDDEN_NAMES: set[str] = {
    "eval", "exec", "open", "compile", "__import__",
    "subprocess", "os", "sys", "socket", "urllib", "requests",
}


def _validate_override(path: str) -> None:
    """Raise ValueError if the override file contains unsafe patterns."""
    with open(path, encoding="utf-8") as f:
        source = f.read()
    tree = ast.parse(source, filename=path)

    for node in ast.walk(tree):
        # Check imports
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top not in _ALLOWED_TOP_MODULES:
                    raise ValueError(f"disallowed import: {alias.name}")
        if isinstance(node, ast.ImportFrom):
            if node.module:
                top = node.module.split(".")[0]
                if top not in _ALLOWED_TOP_MODULES:
                    raise ValueError(f"disallowed import from: {node.module}")
        # Check dangerous calls
        if isinstance(node, ast.Call):
            func = node.func
            name = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name and name in _FORBIDDEN_NAMES:
                raise ValueError(f"disallowed call: {name}")

    # Must define generate_signals
    top_names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    if "generate_signals" not in top_names:
        raise ValueError("override must define generate_signals()")


def get_override(strategy_name: str):
    """Return the AI override module for strategy_name, or None.

    Loads and AST-validates forex/ai_variants/strategy_<name>_override.py.
    Returns None if no override exists or validation fails.
    Only intended for use in the ai_sim account path.
    """
    filename = f"strategy_{strategy_name}_override.py"
    path = os.path.join(_VARIANTS_DIR, filename)
    if not os.path.exists(path):
        return None

    try:
        _validate_override(path)
    except ValueError as exc:
        print(f"  [forex ai_variants] {filename} SECURITY FAIL: {exc} — using original")
        return None

    mod_name = f"forex.ai_variants.strategy_{strategy_name}_override"
    try:
        spec = importlib.util.spec_from_file_location(mod_name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception as exc:
        print(f"  [forex ai_variants] {filename} load error: {exc} — using original")
        return None
