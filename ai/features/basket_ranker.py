"""
ai/features/basket_ranker.py -- SHADOW ranker for the US Blend fortnightly
offense basket (atos/us_momentum.compute_targets). 2026-09-02.

US Blend rebalances its top-N momentum "offense" names every ~14 days.
That is a ranking decision, not a single-trade APPROVE/REJECT -- so it
gets its own shadow module rather than the Trading Copilot.

STRICTLY LOG-ONLY. rank_basket_shadow() makes one LLM call asking for a
re-ranked offense list + a recommended count, writes it to
data/ai_basket_shadow.jsonl next to the deterministic pick, and returns
None. The caller NEVER passes its output to plan_rebalance(). The
deterministic `targets` are untouched. The defense (low-vol) sleeve is a
risk allocation, not an alpha call -- left entirely alone.

Governance: this ACCUMULATES EVIDENCE. A deterministic re-ranking rule
and its backtest come out of the user's review of this log (observe ->
human hypothesis -> deterministic code -> backtest -> deploy), never from
wiring the LLM into the live basket.

Never raises. Any failure (no SDK, no key, bad JSON, timeout) -> a
benign row is still logged with _agent.ok = false, and None is returned.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

import ai.config as ai_config

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "data")
BASKET_SHADOW_LOG = os.path.join(_DATA_DIR, "ai_basket_shadow.jsonl")

EVAL_TIMEOUT_S = 30.0
MAX_TOKENS = 1200

_SYSTEM = """You are ATOS's Basket Ranker -- a shadow analyst for a systematic US \
equity momentum sleeve. Every ~14 days the bot rebalances to hold the top few \
"offense" names by risk-adjusted 6-month momentum (return / 60-day volatility), \
plus a separate low-volatility "defense" sleeve you do NOT touch.

You are given the deterministic pick: the offense tickers (best-first), the defense \
tickers, the market regime, and per-ticker stats (6-month momentum %, annualised \
vol %). The bot's rule takes up to MOM_N_MAX names that clear a momentum threshold, \
ranked by momentum/vol.

Your job: say whether you would RE-RANK or TRIM the OFFENSE list, and why. You may \
reorder it and you may recommend holding FEWER names (never more than the \
deterministic count, never add a ticker that is not already in the list). Think \
about: momentum quality vs a single blow-off move, dispersion between the names, \
regime fit (a narrow/late-cycle tape argues for fewer, higher-conviction names), \
and crowding.

This changes NOTHING -- it is logged and reviewed. Be decisive and specific.

Output ONLY raw JSON, no code fence, no prose:
{
  "ai_offense": ["TICK", ...],      // re-ranked subset of the given offense list, best first
  "ai_count": <int>,                // how many offense names to hold (<= given count)
  "confidence": "high" | "medium" | "low",
  "reasoning": "<= 3 sentences, specific"
}"""


def _append(row: dict) -> None:
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        with open(BASKET_SHADOW_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")
    except Exception:
        pass


def _extract_json(text: str):
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    try:
        return json.loads(text)
    except Exception:
        pass
    # first {...} block
    i, j = text.find("{"), text.rfind("}")
    if 0 <= i < j:
        try:
            return json.loads(text[i:j + 1])
        except Exception:
            return None
    return None


def rank_basket_shadow(*, det_offense: list, det_defense: list, det_count: int,
                       detail: dict, regime_label: str | None,
                       mom_n_max: int, as_of_date: str | None = None,
                       account_env: str = "sim") -> None:
    """One shadow LLM call. Logs deterministic vs AI offense pick to
    data/ai_basket_shadow.jsonl. Returns None. Never raises. The caller must
    already have checked ai_config.stocks_basket_ranker_enabled(). `account_env`
    ("sim" | "live_stocks") tags the row so the real-money US Blend sleeve's
    shadow rebalances are separable from SIM's."""
    det_offense = list(det_offense or [])
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "as_of_date": as_of_date or datetime.now(timezone.utc).date().isoformat(),
        "account_env": account_env,
        "regime": regime_label,
        "mom_n_max": mom_n_max,
        "det_offense": det_offense,
        "det_defense": list(det_defense or []),
        "det_count": det_count,
        "detail": detail or {},
    }
    if not det_offense:
        row["_agent"] = {"ok": False, "note": "no offense names to rank"}
        _append(row)
        return None

    model = ai_config.agent_model()
    t0 = time.time()
    payload = {
        "offense": det_offense, "defense": list(det_defense or []),
        "count": det_count, "MOM_N_MAX": mom_n_max,
        "regime": regime_label, "stats": detail or {},
    }
    try:
        import anthropic
    except Exception:
        row["_agent"] = {"ok": False, "model": model, "note": "anthropic SDK not installed"}
        _append(row)
        return None
    try:
        client = anthropic.Anthropic().with_options(timeout=EVAL_TIMEOUT_S, max_retries=1)
        resp = client.messages.create(
            model=model, max_tokens=MAX_TOKENS,
            system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": json.dumps(payload, default=str)}],
        )
    except Exception as exc:
        row["_agent"] = {"ok": False, "model": model,
                         "note": f"{type(exc).__name__}: {str(exc)[:160]}",
                         "latency_ms": round((time.time() - t0) * 1000)}
        _append(row)
        return None

    latency = round((time.time() - t0) * 1000)
    text = "".join(b.text for b in getattr(resp, "content", [])
                   if getattr(b, "type", "") == "text")
    parsed = _extract_json(text)
    if not isinstance(parsed, dict):
        row["_agent"] = {"ok": False, "model": model, "latency_ms": latency,
                         "note": f"unparseable: {text[:120]!r}"}
        _append(row)
        return None

    # sanitise: subset of det_offense only, count <= det_count
    ai_off = [t for t in (parsed.get("ai_offense") or []) if t in det_offense]
    if not ai_off:
        ai_off = det_offense[:]
    try:
        ai_count = int(parsed.get("ai_count", len(ai_off)))
    except (TypeError, ValueError):
        ai_count = len(ai_off)
    ai_count = max(1, min(ai_count, det_count, len(ai_off)))

    row.update({
        "ai_offense": ai_off[:ai_count],
        "ai_offense_ranked_full": ai_off,
        "ai_count": ai_count,
        "confidence": parsed.get("confidence"),
        "reasoning": parsed.get("reasoning"),
        "changed": ai_off[:ai_count] != det_offense[:det_count],
        "_agent": {"ok": True, "model": model, "latency_ms": latency},
    })
    _append(row)
    return None
