"""
ai/agent/trading_copilot.py -- AI Sprint 3: the consolidated Trading
Copilot. Takes one trade proposal (ai/features/trade_proposal.py), returns
a structured decision. In-process call, not a microservice.

v1 mandate (locked, do not re-litigate -- docs/atos_ai_roadmap.md):
  * The agent may APPROVE / REJECT / MODIFY and set a size_multiplier in
    (0, 1] -- it can only ever REDUCE size, never exceed the Risk Engine.
  * It must NEVER adjust the stop-loss or take-profit in v1. The schema
    carries those fields but this module forces them to null and, if the
    model tries to set one, downgrades the decision.
  * It NEVER places, blocks, or touches an order. ATOS's deterministic
    gates and Risk Engine are the only thing between any decision and Saxo.

Resilience (governance principle #6): ANY failure -- SDK missing, no
credentials, network down, timeout, malformed JSON, schema violation --
returns action "HOLD" (a no-op the caller ignores), logged as an agent
failure. This function MUST NOT raise.
"""

from __future__ import annotations

import json
import time

import ai.config as ai_config

# Hard bounds on the multiplier. FLOOR is the one number Sprint 4 leaves
# open (a business call) -- until it's decided, the agent may propose down
# to this, and the report will show whether it ever wanted to go lower.
MULTIPLIER_FLOOR = 0.10
MULTIPLIER_CEIL  = 1.00
EVAL_TIMEOUT_S   = 25.0
MAX_TOKENS       = 1024

_SYSTEM = """You are ATOS's Trading Copilot. You evaluate trade proposals from a \
deterministic quantitative bot -- you NEVER place, block, or modify an order yourself.

The bot has already run every hard risk check (max risk %, exposure caps, spread, \
margin, portfolio heat, commission viability). Your job is a second opinion using \
broader context: the market regime, how the new trade sits against the open book, \
volatility, signal quality.

You MAY:
- APPROVE the trade as sized
- REJECT it (the bot will skip it -- same as any other skip)
- MODIFY it by REDUCING the position size (size_multiplier between 0.1 and 1.0)

You MUST NOT:
- increase size (size_multiplier must be <= 1.0)
- adjust the stop-loss or take-profit (leave both null -- not in scope for this version)
- reference anything outside the proposal you were given

Respond with ONLY a JSON object, no prose around it:
{
  "action": "APPROVE" | "REJECT" | "MODIFY",
  "size_multiplier": number in (0, 1],   // 1.0 for APPROVE/REJECT
  "adjusted_stop_loss": null,
  "adjusted_take_profit": null,
  "comment": "one sentence, <=200 chars, the reason"
}"""


def _hold(reason: str, latency_ms: float = 0.0, model: str = "") -> dict:
    return {
        "action": "HOLD",              # a no-op the caller must ignore
        "size_multiplier": 1.0,
        "adjusted_stop_loss": None,
        "adjusted_take_profit": None,
        "comment": f"agent unavailable/failed: {reason}",
        "_agent": {"ok": False, "error": reason, "model": model,
                   "latency_ms": round(latency_ms, 1)},
    }


def _coerce_decision(raw: dict, model: str, latency_ms: float) -> dict:
    """Validate + clamp the model's JSON into the decision schema. A schema
    violation is downgraded, never trusted."""
    notes = []
    action = str(raw.get("action", "")).upper()
    if action not in ("APPROVE", "REJECT", "MODIFY"):
        return _hold(f"bad action {action!r}", latency_ms, model)

    mult = raw.get("size_multiplier", 1.0)
    try:
        mult = float(mult)
    except (TypeError, ValueError):
        mult = 1.0
        notes.append("non-numeric size_multiplier -> 1.0")
    if mult > MULTIPLIER_CEIL:
        notes.append(f"multiplier {mult} > {MULTIPLIER_CEIL} clamped")
        mult = MULTIPLIER_CEIL
    if mult < MULTIPLIER_FLOOR:
        notes.append(f"multiplier {mult} < {MULTIPLIER_FLOOR} clamped")
        mult = MULTIPLIER_FLOOR
    if action in ("APPROVE", "REJECT") and abs(mult - 1.0) > 1e-9:
        notes.append("multiplier ignored for APPROVE/REJECT")
        mult = 1.0

    # v1: SL/TP adjustment is out of scope. If the model tried, drop it and
    # downgrade a MODIFY-that-was-only-SL/TP to APPROVE.
    tried_sltp = raw.get("adjusted_stop_loss") is not None or raw.get("adjusted_take_profit") is not None
    if tried_sltp:
        notes.append("adjusted_stop_loss/take_profit set by model -- forced to null (out of scope for v1)")
        if action == "MODIFY" and abs(mult - 1.0) <= 1e-9:
            action = "APPROVE"

    comment = str(raw.get("comment", ""))[:200]
    if notes:
        comment = (comment + "  [" + "; ".join(notes) + "]")[:400]

    return {
        "action": action,
        "size_multiplier": round(mult, 3),
        "adjusted_stop_loss": None,
        "adjusted_take_profit": None,
        "comment": comment,
        "_agent": {"ok": True, "error": None, "model": model,
                   "latency_ms": round(latency_ms, 1)},
    }


def _extract_json(text: str) -> dict | None:
    text = (text or "").strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    # tolerate a fenced block or leading/trailing prose
    a, b = text.find("{"), text.rfind("}")
    if 0 <= a < b:
        try:
            return json.loads(text[a:b + 1])
        except Exception:
            return None
    return None


def evaluate_proposal(proposal: dict) -> dict:
    """Return a decision dict for `proposal`. Never raises. Any failure ->
    action 'HOLD'."""
    model = ai_config.agent_model()
    t0 = time.time()
    try:
        import anthropic
    except Exception:
        return _hold("anthropic SDK not installed", (time.time() - t0) * 1000, model)

    try:
        client = anthropic.Anthropic().with_options(timeout=EVAL_TIMEOUT_S, max_retries=1)
        resp = client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=_SYSTEM,
            messages=[{"role": "user", "content": json.dumps(proposal, default=str)}],
        )
    except Exception as exc:  # auth, network, timeout, rate limit, anything
        return _hold(f"{type(exc).__name__}: {str(exc)[:160]}", (time.time() - t0) * 1000, model)

    latency = (time.time() - t0) * 1000
    if getattr(resp, "stop_reason", None) == "refusal":
        return _hold("model refusal", latency, model)

    text = "".join(b.text for b in getattr(resp, "content", []) if getattr(b, "type", "") == "text")
    raw = _extract_json(text)
    if not isinstance(raw, dict):
        return _hold(f"unparseable response: {text[:120]!r}", latency, model)
    return _coerce_decision(raw, model, latency)
