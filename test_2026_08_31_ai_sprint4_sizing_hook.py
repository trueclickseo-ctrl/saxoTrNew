"""
AI Sprint 4 test gate -- the Trading Copilot's decision wired into SIM
sizing (Level 2, SIM only).

Contract:
  * FLOOR = 0.25 (D1, user-decided 2026-08-31) -- a MODIFY can cut to at
    most a quarter; smaller than that is a REJECT.
  * _ai_apply_decision_to_qty: REJECT -> (0, reason); MODIFY -> scaled &
    floored to min_units, multiplier clamped to [FLOOR, 1.0]; APPROVE/HOLD
    -> unchanged.
  * SHIPS INERT: under the committed config/ai.json (shadow_mode:true)
    can_apply_decision("sim") is False, so the runner hook is a no-op --
    exactly like the Sprint 2/3 hooks shipped.
  * LIVE can NEVER reach the hook (can_apply_decision hardcoded False for
    live/live_eur).
  * the hook sits after size_position and before the cost gate; REJECT
    uses the same `continue` skip shape as every deterministic gate.
"""

import inspect
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


import ai.agent.trading_copilot as tc
import ai.config as aic
import ai.features.trade_proposal as tp
import forex.runner as r


# ── D1: FLOOR = 0.25 ──────────────────────────────────────────────────────
def test_floor_is_quarter():
    assert tc.MULTIPLIER_FLOOR == 0.25
    # the coerce path clamps a too-small multiplier up to the floor
    d = tc._coerce_decision({"action": "MODIFY", "size_multiplier": 0.02, "comment": "x"}, "m", 1)
    assert d["size_multiplier"] == 0.25


# ── _ai_apply_decision_to_qty ─────────────────────────────────────────────
def test_apply_reject_returns_zero():
    q, note = r._ai_apply_decision_to_qty(10000, {"action": "REJECT", "comment": "hostile regime"}, 1000)
    assert q == 0 and "REJECT" in note


def test_apply_modify_scales_and_floors():
    q, note = r._ai_apply_decision_to_qty(10000, {"action": "MODIFY", "size_multiplier": 0.4}, 1000)
    assert q == 4000 and note and "4,000" in note
    # scaled result below the pair minimum is floored back up to min_units
    q2, _ = r._ai_apply_decision_to_qty(1200, {"action": "MODIFY", "size_multiplier": 0.25}, 1000)
    assert q2 == 1000


def test_apply_modify_multiplier_clamped_to_floor():
    q, _ = r._ai_apply_decision_to_qty(10000, {"action": "MODIFY", "size_multiplier": 0.01},
                                       1000, floor=0.25)
    assert q == 2500


def test_apply_approve_hold_unchanged():
    for act in ("APPROVE", "HOLD", "WEIRD"):
        q, note = r._ai_apply_decision_to_qty(10000, {"action": act}, 1000)
        assert q == 10000 and note is None


def test_apply_modify_at_one_is_unchanged():
    q, note = r._ai_apply_decision_to_qty(10000, {"action": "MODIFY", "size_multiplier": 1.0}, 1000)
    assert q == 10000 and note is None


def test_apply_never_amplifies():
    # a model that somehow returns >1 must never enlarge the position
    q, _ = r._ai_apply_decision_to_qty(10000, {"action": "MODIFY", "size_multiplier": 3.0}, 1000)
    assert q == 10000


def test_apply_bad_multiplier_is_safe():
    for bad in (None, "abc", float("nan")):
        q, _ = r._ai_apply_decision_to_qty(10000, {"action": "MODIFY", "size_multiplier": bad}, 1000)
        assert q == 10000, bad


# ── ships inert under the committed config ────────────────────────────────
def test_hook_is_inert_on_main_today():
    # committed config/ai.json has shadow_mode:true -> the Sprint 4 hook
    # cannot fire on any account.
    assert aic.can_apply_decision("sim") is False, "shadow_mode must gate Sprint 4 off"
    assert aic.can_apply_decision("live") is False
    assert aic.can_apply_decision("live_eur") is False


def test_can_apply_only_sim_and_only_out_of_shadow():
    real = aic._CONFIG_PATH
    tmp = os.path.join(BASE_DIR, "config", "_test_sprint4.json")
    aic._CONFIG_PATH = tmp
    try:
        # sim + agent on + shadow OFF -> the ONLY True case
        with open(tmp, "w") as f:
            json.dump({"enabled_sim": True, "agent_enabled": True, "shadow_mode": False}, f)
        assert aic.can_apply_decision("sim") is True
        # LIVE stays False even with the exact same flags + enabled_live_shadow
        with open(tmp, "w") as f:
            json.dump({"enabled_live_shadow": True, "agent_enabled": True, "shadow_mode": False}, f)
        assert aic.can_apply_decision("live") is False
        assert aic.can_apply_decision("live_eur") is False
    finally:
        aic._CONFIG_PATH = real
        if os.path.exists(tmp):
            os.remove(tmp)


# ── runner wiring (source inspection) ─────────────────────────────────────
def test_runner_hook_placement_and_shape():
    src = inspect.getsource(r._run_entries)
    # the Sprint 4 apply block exists and is gated on can_apply_decision
    assert "ai_config.can_apply_decision(ACCOUNT_ENV)" in src
    assert "_ai_apply_decision_to_qty(" in src
    # it sits AFTER sizing and BEFORE the cost gate
    i_size = src.index("strat_mod.size_position(")
    i_hook = src.index("_ai_apply_decision_to_qty(")
    i_cost = src.index("_round_trip_cost_quote_ccy(")
    assert i_size < i_hook < i_cost, "hook must run after sizing, before the cost gate"
    # REJECT -> continue (same skip shape as the deterministic gates)
    hook = src[i_hook: i_cost]
    assert "continue" in hook
    # when the agent may act, it is evaluated every rescan (dedup bypass)
    assert "_ai_acting" in src and "can_apply_decision(ACCOUNT_ENV)" in src


def test_shadow_log_records_applied_flag():
    p = os.path.join(BASE_DIR, "data", "_test_sprint4_shadow.jsonl")
    old = tp.SHADOW_DECISIONS_LOG
    tp.SHADOW_DECISIONS_LOG = p
    try:
        if os.path.exists(p):
            os.remove(p)
        prop = {"ts": "2026-08-31T12:00:00+00:00", "account_env": "sim",
                "strategy_name": "rsi", "symbol": "EURUSD", "side": "BUY",
                "regime": {"label": "RANGING"}, "signal_strength": 0.1}
        dec = {"action": "MODIFY", "size_multiplier": 0.25, "comment": "c",
               "_agent": {"ok": True}}
        tp.log_shadow_decision(prop, dec, entered=True, applied=True)
        tp.log_shadow_decision(prop, dec, entered=False)   # default applied=False
        rows = [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]
        assert rows[0]["applied"] is True
        assert rows[1]["applied"] is False
    finally:
        tp.SHADOW_DECISIONS_LOG = old
        if os.path.exists(p):
            os.remove(p)


def test_system_prompt_reflects_quarter_floor():
    assert "0.25" in tc._SYSTEM
    assert "0.1 and 1.0" not in tc._SYSTEM  # the old bound text is gone


for _n, _f in list(globals().items()):
    if _n.startswith("test_") and callable(_f):
        _run(_n, _f)

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
