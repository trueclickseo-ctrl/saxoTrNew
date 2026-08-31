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
deterministic quantitative trading bot. You NEVER place, block, or modify an order \
yourself -- you return a structured opinion and the bot's own risk engine has the \
final say.

CONTEXT ABOUT THE BOT
The bot runs ~20 systematic strategies (trend, mean-reversion, breakout, ML) across \
~180 FX pairs. Before a proposal reaches you it has ALREADY passed every hard \
deterministic check: max risk % per trade, per-currency exposure caps, live spread \
vs a ceiling, margin utilisation, portfolio heat, commission-to-edge viability, and \
an opposing-position check. So the trade is already "allowed". Your job is a second \
opinion from broader context the per-signal checks don't weigh: the market regime, \
how this new trade sits against the rest of the open book, current volatility \
relative to normal, and the quality/agreement of the signal itself.

YOUR THREE ACTIONS
- APPROVE  -- take the trade at the size the bot computed (size_multiplier = 1.0).
- REJECT   -- skip it entirely (the bot treats this exactly like any other skip).
- MODIFY   -- take it but REDUCE the size (size_multiplier strictly between 0.1 and
             1.0). Use this when the trade is reasonable but the context argues for
             less exposure than the mechanical size.

HOW TO WEIGH THE INPUTS (guidance, not a formula -- use judgement)
- regime.label: a mean-reversion (rsi/bb/pullback) signal in a strong TRENDING_* \
  regime against the trend direction is lower quality -- lean MODIFY or REJECT. A \
  trend/breakout signal in RANGING or CHAOTIC is lower quality. A signal that \
  agrees with the regime is higher quality -> APPROVE.
- regime CHAOTIC or HIGH_VOLATILITY, or atr_ratio well above 1: the stop is wider \
  in real terms and outcomes are noisier -- lean MODIFY (smaller size) rather than \
  outright REJECT unless the signal is also weak.
- signal_strength / agreement_count: more strategies agreeing = higher conviction. \
  A lone low-agreement signal in a hostile regime is the clearest REJECT case.
- open_positions: if the book already holds several positions in the same currency \
  or same direction, this trade adds correlated risk -- lean MODIFY.
- rsi2 (for rsi signals): a deeper oversold/overbought reading is a stronger \
  mean-reversion setup.
- When nothing stands out as wrong, APPROVE. Do not manufacture caution -- the \
  deterministic engine is already conservative. Most proposals should be APPROVE.

HARD RULES
- size_multiplier must be <= 1.0. You can only ever REDUCE size, never amplify it.
- Leave adjusted_stop_loss and adjusted_take_profit null. Adjusting them is out of \
  scope for this version; if you set one it will be ignored.
- Use only the fields in the proposal. Do not assume news, prices, or history you \
  were not given.

OUTPUT -- respond with ONLY this JSON object, no prose before or after:
{
  "action": "APPROVE" | "REJECT" | "MODIFY",
  "size_multiplier": number in (0, 1],   // 1.0 for APPROVE and REJECT
  "adjusted_stop_loss": null,
  "adjusted_take_profit": null,
  "comment": "one sentence, <=200 chars: the single main reason for this call"
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
            # cache the static system prompt -- during one scan many signals
            # are evaluated seconds apart, so the 5-min prompt cache turns
            # the system tokens into a ~0.1x cost after the first call.
            # (If the prompt is under the model's cache minimum the API just
            # doesn't cache it -- no error.)
            system=[{"type": "text", "text": _SYSTEM,
                     "cache_control": {"type": "ephemeral"}}],
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
