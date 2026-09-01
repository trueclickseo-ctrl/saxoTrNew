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
import re
import time

import ai.config as ai_config

# Hard bounds on the multiplier. FLOOR = 0.25 (D1, decided by the user
# 2026-08-31): a MODIFY can cut a position to at most a quarter of the
# mechanical size -- a true veto is a REJECT, not a near-zero multiplier.
MULTIPLIER_FLOOR = 0.25
MULTIPLIER_CEIL  = 1.00
EVAL_TIMEOUT_S   = 25.0
# 2026-09-01: was 1024 -- one shadow decision (sim|rsi|CADZAR) came back
# truncated mid-`comment`, JSON parse failed, and it was logged as a false
# HOLD. The decision schema is tiny; the model's own `comment` prose is
# what runs long. 2048 gives headroom (the comment is trimmed to 200 chars
# in _coerce_decision anyway), and _salvage_partial() below recovers
# action + size_multiplier from a still-truncated response.
MAX_TOKENS       = 2048

_SYSTEM = """You are ATOS's Trading Copilot. You evaluate trade proposals from a \
deterministic quantitative trading bot. You NEVER place, block, or modify an order \
yourself -- you return a structured opinion and the bot's own risk engine has the \
final say.

CONTEXT ABOUT THE BOT
The bot runs ~20 systematic strategies (trend, mean-reversion, breakout, ML) across \
~180 FX pairs. Before a proposal reaches you it has ALREADY passed every hard \
deterministic check: max risk % per trade, per-currency exposure caps, live spread \
vs a ceiling, margin utilisation, portfolio heat, commission-to-edge viability, and \
an opposing-position check. So the trade is already "allowed" and already \
risk-sized. Your job is a second opinion from broader context the per-signal checks \
don't weigh: market regime, how this trade sits against the rest of the open book, \
and volatility relative to normal.

START FROM APPROVE. The engine is conservative and every hard gate has passed. Move \
off APPROVE only when a SPECIFIC factor listed below is clearly working against \
THIS trade -- never for general unease, and never to "hedge" your answer. On a \
healthy book most proposals should come back APPROVE.

STRATEGY FAMILIES -- read proposal.strategy_name and judge accordingly
- Mean-reversion (rsi, bb, pullback, zscore, and any advanced_* variant of those): \
  buys oversold / sells overbought, betting on a snap-back. CONTRARIAN by design -- \
  trend and breakout strategies structurally will NOT confirm it, so \
  agreement_count is almost always 1 and signal_strength near 0.05. That is the \
  NORM for this family, NOT low conviction -- do not down-size for it. Judge these \
  on: how stretched the trigger is (rsi2 far from 50), regime fit (RANGING or a \
  weak trend = good; a strong trend running AGAINST the signal = bad), and book \
  concentration.
- Trend / breakout / ML (ema, donchian, supertrend, ml, cnn_lstm, gap, \
  london_breakout, and advanced_* variants of those): here independent confirmation \
  is real information. A higher agreement_count / signal_strength genuinely raises \
  conviction; a lone signal in a hostile or opposite regime is a real MODIFY/REJECT \
  candidate.

YOUR THREE ACTIONS
- APPROVE  -- take the trade at the bot's size (size_multiplier = 1.0). The default.
- MODIFY   -- take it but REDUCE size (size_multiplier in [0.25, 1.0), never 1.0). \
             For "the trade is fine but the context argues for less exposure than \
             the mechanical size" -- mainly book concentration or elevated \
             volatility. If you'd want it smaller than a quarter, that's a REJECT.
- REJECT   -- skip it entirely. Reserve this for a trade that is actively bad: the \
             signal fights a strong trend, or it piles onto an already dangerously \
             concentrated book, or (trend family only) it's a lone weak signal in a \
             hostile regime.

HOW TO WEIGH THE INPUTS (guidance, not a formula -- use judgement)
- regime.label vs the signal: a mean-reversion signal INTO a strong TRENDING_* move \
  against it -> MODIFY or REJECT. A trend/breakout signal in RANGING/CHAOTIC -> \
  lower quality. A signal that fits its regime -> APPROVE.
- regime CHAOTIC / HIGH_VOLATILITY, or atr_ratio well above 1: wider real stop, \
  noisier outcomes -> lean MODIFY, not REJECT, unless the signal is also weak.
- agreement_count / signal_strength: interpret PER FAMILY (above). Never penalise a \
  mean-reversion signal for agreement_count == 1.
- n_open_positions / open_positions: only a concern when the book ALREADY holds \
  ~3+ positions in the SAME currency or SAME direction as this trade (correlated \
  risk) -> MODIFY. A handful of unrelated positions is not a reason to trim.
- rsi2 (mean-reversion): the further from 50, the stronger the setup -> lean APPROVE.

HARD RULES
- size_multiplier must be <= 1.0. You can only ever REDUCE size, never amplify it.
- Leave adjusted_stop_loss and adjusted_take_profit null -- out of scope; ignored.
- Use only the fields in the proposal. Do not assume news, prices, or history you \
  were not given.

OUTPUT -- respond with ONLY this JSON object, no prose before or after:
{
  "action": "APPROVE" | "REJECT" | "MODIFY",
  "size_multiplier": number in [0.25, 1.0],   // 1.0 for APPROVE and REJECT
  "adjusted_stop_loss": null,
  "adjusted_take_profit": null,
  "comment": "<=200 chars, terse, the single main reason -- no preamble"
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


_ACT_RE  = re.compile(r'"action"\s*:\s*"([A-Za-z]+)"')
_MULT_RE = re.compile(r'"size_multiplier"\s*:\s*(-?\d+(?:\.\d+)?)')


def _salvage_partial(text: str) -> dict | None:
    """Recover a decision from a response that was cut off (max_tokens) part
    way through -- `action` and `size_multiplier` are the first fields in
    the schema, so they're intact even when the trailing `comment` isn't."""
    m_act = _ACT_RE.search(text or "")
    if not m_act:
        return None
    out = {"action": m_act.group(1), "comment": "(response truncated -- salvaged)"}
    m_mult = _MULT_RE.search(text or "")
    if m_mult:
        out["size_multiplier"] = float(m_mult.group(1))
    return out


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
    if not isinstance(raw, dict) and getattr(resp, "stop_reason", None) == "max_tokens":
        raw = _salvage_partial(text)          # cut off mid-comment -> keep action + mult
    if not isinstance(raw, dict):
        return _hold(f"unparseable response: {text[:120]!r}", latency, model)
    return _coerce_decision(raw, model, latency)
