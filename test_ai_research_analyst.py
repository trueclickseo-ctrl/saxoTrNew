"""
test_ai_research_analyst.py -- unit tests for ai/features/research_analyst.py.

Tests:
  1. AST forbidden-import guard (no forex.runner, saxo_*, order calls)
  2. build_research_digest() structure and graceful empty case
  3. run() is gated by research_analyst_enabled()
  4. propose_hypotheses() degrades safely without ANTHROPIC_API_KEY
  5. backlog_view() structure
  6. set_status() return value for unknown id
  7. _claim_hash() deduplication logic (case-insensitive, strategy-sensitive)
  8. auto_gate() with injected trades (no yfinance download)
  9. auto_gate() gate_skipped when no decompose_spec

Nothing here downloads live market data or places orders.
"""

import ast
import json
import os
import sys
import tempfile
from unittest.mock import patch

import pytest

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import ai.features.research_analyst as RA
import ai.config as ai_config
from ai.research.decompose import Trade


# ─── helpers ──────────────────────────────────────────────────────────────────

def _fake_trades_bb(n: int = 100) -> list[Trade]:
    """All-positive bb trades evenly split into halves, di_spread=10 (<=14 → PASS)."""
    return [
        Trade("bb", "EURUSD", "core", "Buy", f"2020-01-{i % 28 + 1:02d}",
              1.1, 0.01, 1.0, 1.0, -0.3, 3, "tp",
              half=1 if i < n // 2 else 2, di_spread=10.0)
        for i in range(n)
    ]


# ─── 1. forbidden imports ─────────────────────────────────────────────────────

_FORBIDDEN = frozenset({
    "forex.runner", "saxo_api", "pnl_tracker", "housekeeping",
    "pnl_ledger", "place_order", "cancel_order", "amend_order",
})


def test_forbidden_imports_research_analyst():
    src = os.path.join(_ROOT, "ai", "features", "research_analyst.py")
    with open(src, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module)
    for bad in _FORBIDDEN:
        assert bad not in imported, f"research_analyst.py must not import {bad!r}"


# ─── 2. build_research_digest ─────────────────────────────────────────────────

def test_build_research_digest_expected_keys():
    digest = RA.build_research_digest()
    assert isinstance(digest, dict)
    for key in ("generated", "n_closed_trades", "by_strategy",
                "by_strategy_regime", "by_strategy_tier",
                "journal", "decomposition_cache"):
        assert key in digest, f"digest missing key {key!r}"


def test_build_research_digest_n_closed_trades_is_int():
    digest = RA.build_research_digest()
    assert isinstance(digest["n_closed_trades"], int)
    assert digest["n_closed_trades"] >= 0


def test_build_research_digest_never_raises_on_empty_trades():
    with patch.object(RA.tj, "_closed_trades", return_value=[]):
        digest = RA.build_research_digest()
    assert digest["n_closed_trades"] == 0
    assert isinstance(digest["by_strategy"], list)


def test_build_research_digest_journal_keys():
    digest = RA.build_research_digest()
    journal = digest["journal"]
    assert "top_tags" in journal
    assert "recent_lessons" in journal
    assert "recent_day_summaries" in journal


# ─── 3. run() gate ────────────────────────────────────────────────────────────

def test_run_returns_disabled_when_config_off():
    with patch.object(ai_config, "research_analyst_enabled", return_value=False):
        result = RA.run()
    assert result["status"] == "disabled"
    assert result.get("proposed") == 0


def test_run_disabled_never_calls_propose(monkeypatch):
    called = []
    monkeypatch.setattr(RA, "propose_hypotheses",
                        lambda *a, **kw: called.append(1) or {})
    with patch.object(ai_config, "research_analyst_enabled", return_value=False):
        RA.run()
    assert not called, "propose_hypotheses must not be called when disabled"


# ─── 4. propose_hypotheses degrades safely ────────────────────────────────────

def test_propose_hypotheses_returns_hypotheses_key():
    result = RA.propose_hypotheses({"by_strategy": [], "by_strategy_regime": []})
    assert "hypotheses" in result
    assert isinstance(result["hypotheses"], list)


def test_propose_hypotheses_no_api_key_never_raises():
    """When anthropic raises on import (not installed), returns empty gracefully."""
    import builtins
    real_import = builtins.__import__

    def _raise_on_anthropic(name, *args, **kwargs):
        if name == "anthropic":
            raise ImportError("no module named anthropic")
        return real_import(name, *args, **kwargs)

    with patch.object(builtins, "__import__", side_effect=_raise_on_anthropic):
        result = RA.propose_hypotheses({"by_strategy": []})
    assert isinstance(result["hypotheses"], list)
    assert "_agent" in result
    assert result["_agent"]["ok"] is False



# ─── 5. backlog_view ──────────────────────────────────────────────────────────

def test_backlog_view_returns_list():
    result = RA.backlog_view()
    assert isinstance(result, list)


def test_backlog_view_gate_passed_before_proposed():
    """gate_passed hypotheses rank before proposed ones."""
    gate_hyp = {"id": "H1", "status": "gate_passed", "expected_effect_r": 0.05}
    prop_hyp  = {"id": "H2", "status": "proposed",    "expected_effect_r": 0.20}
    # Inject directly into the state dict returned by _latest_by_id
    with patch.object(RA, "_latest_by_id",
                      return_value={"H1": gate_hyp, "H2": prop_hyp}):
        result = RA.backlog_view()
    assert result[0]["status"] == "gate_passed", (
        "gate_passed should appear before proposed regardless of expected_effect_r")


# ─── 6. set_status ────────────────────────────────────────────────────────────

def test_set_status_unknown_id_returns_false():
    result = RA.set_status("nonexistent_hypothesis_id_xyz", "shelved")
    assert result is False, "set_status with unknown id must return False, not raise"


def test_set_status_invalid_status_raises():
    with pytest.raises(ValueError, match="status must be one of"):
        RA.set_status("any_id", "invalid_status_xyz")


# ─── 7. _claim_hash deduplication ────────────────────────────────────────────

def test_claim_hash_case_insensitive_on_rule():
    h1 = {"strategy": "bb", "kind": "entry_gate", "feature": "di_spread",
          "rule": "Keep entry only if DI_SPREAD < 14"}
    h2 = {"strategy": "bb", "kind": "entry_gate", "feature": "di_spread",
          "rule": "keep entry only if di_spread < 14"}
    assert RA._claim_hash(h1) == RA._claim_hash(h2)


def test_claim_hash_differs_by_strategy():
    base = {"kind": "entry_gate", "feature": "di_spread", "rule": "di < 14"}
    h_bb  = {**base, "strategy": "bb"}
    h_rsi = {**base, "strategy": "rsi"}
    assert RA._claim_hash(h_bb) != RA._claim_hash(h_rsi)


def test_claim_hash_differs_by_rule():
    base = {"strategy": "bb", "kind": "entry_gate", "feature": "di_spread"}
    h1 = {**base, "rule": "di < 14"}
    h2 = {**base, "rule": "di < 20"}
    assert RA._claim_hash(h1) != RA._claim_hash(h2)


# ─── 8. auto_gate with injected trades ───────────────────────────────────────

def test_auto_gate_with_injected_trades_passes():
    """Gate should pass when injected trades show stable positive edge in <=14 bucket."""
    trades = _fake_trades_bb(n=200)  # all di=10, all R=+1, both halves positive
    hyp = {
        "strategy": "bb", "kind": "entry_gate", "feature": "di_spread",
        "rule": "keep entry only if di_spread <= 14",
        "decompose_spec": {"strategy": "bb", "feature": "di_spread"},
    }
    result = RA.auto_gate(hyp, trades_by_strategy={"bb": trades})
    assert result["status"] in ("gate_passed", "gate_failed", "gate_skipped")
    assert "verdict" in result
    # with all-positive trades in <=14, should pass
    assert result["status"] == "gate_passed"


def test_auto_gate_with_injected_negative_trades_fails():
    """Gate must fail when trades are mixed (avg R ≈ 0, CI includes zero)."""
    import numpy as np
    rng = np.random.default_rng(42)
    rs = rng.choice([-1.0, 1.0], size=120).tolist()
    trades = [
        Trade("bb", "EURUSD", "core", "Buy", "2020-01-01", 1.1, 0.01,
              r, 1.0, -0.3, 3, "tp",
              half=1 if i < 60 else 2, di_spread=10.0)
        for i, r in enumerate(rs)
    ]
    hyp = {
        "strategy": "bb", "kind": "entry_gate", "feature": "di_spread",
        "rule": "keep entry only if di_spread <= 14",
        "decompose_spec": {"strategy": "bb", "feature": "di_spread"},
    }
    result = RA.auto_gate(hyp, trades_by_strategy={"bb": trades})
    assert result["status"] in ("gate_failed", "gate_skipped")


def test_auto_gate_skipped_when_no_decompose_spec():
    hyp = {
        "strategy": "bb", "kind": "entry_gate", "feature": "di_spread",
        "rule": "some rule without a spec",
    }
    result = RA.auto_gate(hyp)
    assert result["status"] == "gate_skipped"
    assert result["verdict"] is None


def test_auto_gate_skipped_for_unknown_strategy():
    hyp = {
        "strategy": "fake_strategy", "kind": "entry_gate", "feature": "di_spread",
        "rule": "some rule",
        "decompose_spec": {"strategy": "fake_strategy", "feature": "di_spread"},
    }
    result = RA.auto_gate(hyp)
    assert result["status"] == "gate_skipped"
