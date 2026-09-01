"""
Sprint 3 test gate -- ai/agent/trading_copilot.py + the shadow hook.

Contract:
  * valid JSON -> parsed & clamped decision
  * malformed JSON / timeout / no SDK / refusal -> action "HOLD", NEVER raises
  * a model that sets adjusted_stop_loss/take_profit -> forced null (v1 scope)
  * size_multiplier clamped to [FLOOR, 1.0]; forced 1.0 for APPROVE/REJECT
  * the _run_entries hook is guarded, try/excepted, and never touches order flow
  * RESILIENCE DRILL: an unreachable endpoint -> the entry loop is byte-for-byte
    what it would be with AI disabled
"""

import inspect
import json
import os
import sys
import types

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


import ai.agent.trading_copilot as tc
import ai.config as aic


# ── a fake `anthropic` module we can swap in ──────────────────────────────
class _FakeBlock:
    type = "text"
    def __init__(self, text): self.text = text

class _FakeResp:
    def __init__(self, text, stop_reason="end_turn"):
        self.content = [_FakeBlock(text)]
        self.stop_reason = stop_reason

def _install_fake_anthropic(*, reply=None, raise_exc=None, stop_reason="end_turn"):
    mod = types.ModuleType("anthropic")
    class _Msgs:
        def create(self, **kw):
            if raise_exc:
                raise raise_exc
            return _FakeResp(reply, stop_reason)
    class _Client:
        def with_options(self, **kw): return self
        messages = _Msgs()
    mod.Anthropic = lambda *a, **k: _Client()
    sys.modules["anthropic"] = mod

def _uninstall_fake():
    sys.modules.pop("anthropic", None)


_PROP = {"symbol": "EURUSD", "side": "BUY", "strategy_name": "rsi", "account_env": "sim",
         "entry_price": 1.15, "regime": {"label": "TRENDING_BULLISH"}}


# ═══════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}1. evaluate_proposal(): happy path + every failure -> HOLD{RESET}")
# ═══════════════════════════════════════════════════════════════════════

def test_valid_response_parses():
    _install_fake_anthropic(reply=json.dumps({
        "action": "MODIFY", "size_multiplier": 0.6,
        "adjusted_stop_loss": None, "adjusted_take_profit": None,
        "comment": "high vol regime, trim size"}))
    try:
        d = tc.evaluate_proposal(_PROP)
        assert d["action"] == "MODIFY" and d["size_multiplier"] == 0.6
        assert d["adjusted_stop_loss"] is None and d["_agent"]["ok"] is True
    finally:
        _uninstall_fake()
_run("valid MODIFY response parses with its multiplier", test_valid_response_parses)


def test_json_wrapped_in_prose_still_parses():
    _install_fake_anthropic(reply='Sure! Here is my call:\n```json\n{"action":"APPROVE","size_multiplier":1.0,"comment":"ok"}\n```')
    try:
        assert tc.evaluate_proposal(_PROP)["action"] == "APPROVE"
    finally:
        _uninstall_fake()
_run("JSON fenced/wrapped in prose is still extracted", test_json_wrapped_in_prose_still_parses)


def test_malformed_json_is_hold():
    _install_fake_anthropic(reply="I think you should probably approve this one.")
    try:
        d = tc.evaluate_proposal(_PROP)
        assert d["action"] == "HOLD" and d["_agent"]["ok"] is False
    finally:
        _uninstall_fake()
_run("unparseable response -> HOLD, no exception", test_malformed_json_is_hold)


def test_timeout_is_hold():
    _install_fake_anthropic(raise_exc=TimeoutError("read timed out"))
    try:
        d = tc.evaluate_proposal(_PROP)
        assert d["action"] == "HOLD" and "TimeoutError" in d["comment"]
    finally:
        _uninstall_fake()
_run("an API exception (timeout) -> HOLD, no exception", test_timeout_is_hold)


def test_refusal_is_hold():
    _install_fake_anthropic(reply='{"action":"APPROVE","size_multiplier":1.0,"comment":"x"}',
                            stop_reason="refusal")
    try:
        assert tc.evaluate_proposal(_PROP)["action"] == "HOLD"
    finally:
        _uninstall_fake()
_run("a model refusal -> HOLD", test_refusal_is_hold)


def test_truncated_response_is_salvaged_not_a_false_hold():
    # 2026-09-01: MAX_TOKENS=1024 cut a real MODIFY off mid-`comment`,
    # json.loads failed, and it was logged as a false HOLD. With
    # stop_reason='max_tokens' the prefix's action + size_multiplier are
    # recovered instead.
    partial = ('{"action": "MODIFY", "size_multiplier": 0.5, '
               '"adjusted_stop_loss": null, "comment": "Deep oversold but the book already')
    _install_fake_anthropic(reply=partial, stop_reason="max_tokens")
    try:
        d = tc.evaluate_proposal(_PROP)
        assert d["action"] == "MODIFY", d
        assert abs(d["size_multiplier"] - 0.5) < 1e-9
    finally:
        _uninstall_fake()
_run("a max_tokens-truncated MODIFY is salvaged, not a false HOLD", test_truncated_response_is_salvaged_not_a_false_hold)


def test_truncated_with_no_action_still_holds():
    _install_fake_anthropic(reply='{"size_multiplier": 0.5, "comment": "blah', stop_reason="max_tokens")
    try:
        assert tc.evaluate_proposal(_PROP)["action"] == "HOLD"
    finally:
        _uninstall_fake()
_run("a truncated response with no action field -> HOLD", test_truncated_with_no_action_still_holds)


def test_max_tokens_headroom():
    assert tc.MAX_TOKENS >= 2048


_run("MAX_TOKENS raised to >= 2048", test_max_tokens_headroom)


def test_prompt_does_not_penalise_mean_reversion_for_lone_agreement():
    # 2026-09-01: the live shadow log was 21/21 MODIFY -- the agent was
    # told a low agreement_count is "the clearest REJECT case", but for
    # rsi/bb/pullback (contrarian) agreement_count is structurally ~1.
    s = tc._SYSTEM
    assert "STRATEGY FAMILIES" in s
    assert "agreement_count is almost always 1" in s or "agreement_count == 1" in s
    assert "Never penalise a mean-reversion signal for agreement_count" in s
    assert "START FROM APPROVE" in s
    # the old blanket rule must be gone
    assert "lone low-agreement signal in a hostile regime is the clearest REJECT" not in s


_run("prompt: mean-reversion not penalised for structural low agreement", test_prompt_does_not_penalise_mean_reversion_for_lone_agreement)


def test_no_sdk_is_hold():
    # force `import anthropic` to fail even if the real SDK is installed
    _uninstall_fake()
    sys.modules["anthropic"] = None
    try:
        d = tc.evaluate_proposal(_PROP)
        assert d["action"] == "HOLD" and "not installed" in d["comment"]
    finally:
        _uninstall_fake()
_run("anthropic SDK not installed -> HOLD", test_no_sdk_is_hold)


# ═══════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}2. v1 scope is enforced on the model's output{RESET}")
# ═══════════════════════════════════════════════════════════════════════

def test_multiplier_clamped_and_forced():
    assert tc._coerce_decision({"action": "APPROVE", "size_multiplier": 1.7, "comment": "x"}, "m", 1)["size_multiplier"] == 1.0
    assert tc._coerce_decision({"action": "MODIFY", "size_multiplier": 0.01, "comment": "x"}, "m", 1)["size_multiplier"] == tc.MULTIPLIER_FLOOR
    assert tc._coerce_decision({"action": "REJECT", "size_multiplier": 0.3, "comment": "x"}, "m", 1)["size_multiplier"] == 1.0
_run("size_multiplier: clamped to [FLOOR,1.0]; forced 1.0 for APPROVE/REJECT", test_multiplier_clamped_and_forced)


def test_sltp_adjustment_is_stripped():
    d = tc._coerce_decision({"action": "MODIFY", "size_multiplier": 1.0,
                             "adjusted_stop_loss": 1.14, "adjusted_take_profit": 1.20,
                             "comment": "widen the stop before NFP"}, "m", 1)
    assert d["adjusted_stop_loss"] is None and d["adjusted_take_profit"] is None
    assert d["action"] == "APPROVE", "a MODIFY that only touched SL/TP downgrades to APPROVE"
    assert "out of scope" in d["comment"]
_run("model setting adjusted_stop_loss/take_profit -> forced null, decision downgraded", test_sltp_adjustment_is_stripped)


def test_bad_action_is_hold():
    assert tc._coerce_decision({"action": "PANIC_SELL", "comment": "x"}, "m", 1)["action"] == "HOLD"
_run("an action outside APPROVE/REJECT/MODIFY -> HOLD", test_bad_action_is_hold)


# ═══════════════════════════════════════════════════════════════════════
print(f"\n{BOLD}3. runner wiring — inert, guarded, never blocks the loop{RESET}")
# ═══════════════════════════════════════════════════════════════════════

def test_hook_is_agent_gated_and_downstream_inert():
    import forex.runner as r
    src = inspect.getsource(r._run_entries)
    assert "ai_config.agent_enabled_for(ACCOUNT_ENV)" in src, "agent call must be behind its own switch"
    assert "ai_trading_copilot.evaluate_proposal(" in src
    # decision is stashed, then logged after the loop -- never applied
    assert "_ai_shadow_pending.append(" in src and "_ai_log_shadow(" in src
    hook = src[src.index("ai_config.ai_enabled_for(ACCOUNT_ENV):"): src.index("if not _currency_ok")]
    for forbidden in ("place_with_stop", "_post(", "size_position", "positions[pos_key]",
                      "entries += 1", "pos_record", "_amend_stop_order"):
        assert forbidden not in hook, f"advisory hook must not contain {forbidden!r}"
    assert "try:" in hook and "except Exception" in hook
_run("_run_entries: agent behind agent_enabled_for, decision logged not applied, hook is inert",
     test_hook_is_agent_gated_and_downstream_inert)


def test_agent_default_off():
    # "default" = the fail-safe when config/ai.json is absent, NOT whatever
    # the live committed file sets (it is ENABLED now for the shadow study).
    _real = aic._CONFIG_PATH
    aic._CONFIG_PATH = os.path.join(BASE_DIR, "config", "_test_copilot_missing.json")
    try:
        assert aic.agent_enabled_for("sim") is False
        assert aic.agent_enabled_for("live") is False and aic.agent_enabled_for("live_eur") is False
    finally:
        aic._CONFIG_PATH = _real
_run("agent_enabled_for is False when config/ai.json is absent (fail-safe)", test_agent_default_off)


def test_shadow_decision_row_shape():
    import ai.features.trade_proposal as tp
    p = os.path.join(BASE_DIR, "data", "_test_shadow_dec.jsonl")
    old = tp.SHADOW_DECISIONS_LOG
    tp.SHADOW_DECISIONS_LOG = p
    try:
        if os.path.exists(p):
            os.remove(p)
        dec = {"action": "MODIFY", "size_multiplier": 0.5, "comment": "trim",
               "_agent": {"ok": True, "model": "claude-sonnet-5", "latency_ms": 812}}
        tp.log_shadow_decision({**_PROP, "ts": "2026-08-31T12:00:00+00:00"}, dec, entered=True)
        row = json.loads(open(p).read().strip())
        assert row["agent_action"] == "MODIFY" and row["entered_by_atos"] is True
        assert row["trade_id"] == "sim|rsi|EURUSD|2026-08-31"
    finally:
        tp.SHADOW_DECISIONS_LOG = old
        if os.path.exists(p):
            os.remove(p)
_run("log_shadow_decision writes proposal+decision+entered, keyed by a joinable trade_id",
     test_shadow_decision_row_shape)


def test_resilience_drill_unreachable_endpoint():
    # the agent pointed at a broken SDK for a whole 'run' must produce HOLD
    # every time and never raise -- i.e. the entry loop is unaffected.
    _install_fake_anthropic(raise_exc=ConnectionError("Name or service not known"))
    try:
        for _ in range(20):
            d = tc.evaluate_proposal(_PROP)
            assert d["action"] == "HOLD"
    finally:
        _uninstall_fake()
_run("resilience drill: 20 calls to an unreachable endpoint -> 20x HOLD, 0 exceptions",
     test_resilience_drill_unreachable_endpoint)


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
