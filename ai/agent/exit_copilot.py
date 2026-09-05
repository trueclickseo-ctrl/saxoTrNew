"""
ai/agent/exit_copilot.py — Phase C: AI exit timing agent.

Evaluates open positions that have reached ≥1R MFE and decides whether
to EXIT_NOW or HOLD. Ships SHADOW-ONLY (logs only, never acts) until
config/ai.json exit_copilot.shadow_mode is flipped to false after Phase B
evidence matures.

v1 scope (locked):
  * Two actions: HOLD or EXIT_NOW. TIGHTEN_STOP is Phase C v2.
  * Evaluates only positions with r_now ≥ min_mfe_r (default 1.0).
  * Never touches an order, stop, or position itself.
  * Any failure → HOLD. Must not raise.
  * Dedup: one evaluation per position_key per day.

Integration in forex/runner.py (_run_exits):
  After the exit_advisor block, once per profitable position:
    dec = ai_exit_copilot.evaluate_position(pos, sym, strat_name, adv,
                                             close_price, account_env)
    if dec["action"] == "EXIT_NOW" and can_apply:
        ... close the position ...

Log: data/ai_exit_decisions.jsonl — includes est_pnl_eur_at_decision so
report_phase_c_counterfactual.py can compute give-back saved vs actual exit.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone

import ai.config as ai_config

logger = logging.getLogger(__name__)

_BASE        = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CONFIG_PATH = os.path.join(_BASE, "config", "ai.json")
_LOG_PATH    = os.path.join(_BASE, "data", "ai_exit_decisions.jsonl")

EVAL_TIMEOUT_S = 20.0
MAX_TOKENS     = 1024

_DEFAULTS = {
    "enabled":            True,
    "shadow_mode":        True,   # ships shadow-only; flip after Phase B proves out
    "min_mfe_r":          1.0,    # only evaluate positions that reached ≥1R profit
    "model":              "claude-sonnet-5",
}

_SYSTEM = """You are ATOS's Exit Copilot. You evaluate open FX positions that are \
currently in profit and decide whether to exit NOW or hold for more.

THE BOT'S NORMAL EXIT MECHANISM
The bot already has hard exits: a fixed stop-loss, a profit ladder that trails the \
stop up after 1R, an RSI-recovery exit (for mean-reversion strategies), and a \
time-stop. Those run regardless of your call. Your job is to recognise the early \
warning pattern of a trade that HAS reached meaningful profit but is giving it back \
fast — and call EXIT_NOW before the normal exits catch it at a much worse price.

YOU ARE NOT DECIDING WHETHER THE ORIGINAL ENTRY WAS RIGHT. The trade is already \
open. You decide: given where price is NOW relative to where it peaked, should we \
take what we have?

WHEN TO CALL EXIT_NOW
Exit NOW when you see ALL of these:
  1. Price has given back ≥40% of its peak profit (giveback_frac ≥ 0.40).
  2. The give-back is still in progress (r_now is noticeably below mfe_r).
  3. Either: (a) RSI is NOT heading to the strategy's natural exit level, so
     the bot's own recovery exit is not imminent; OR (b) the position is late
     in its life (time_frac > 0.65) and has already retraced to near entry.
  4. The exit_advisor already rates this EXIT or TIGHTEN (score ≥ 45).

WHEN TO HOLD
  - r_now is still near mfe_r (give-back < 20%) — let the winner run.
  - RSI is heading toward its own natural exit level — the bot will catch it.
  - The give-back happened on one noisy candle and the position is young.
  - Score < 45 — the deterministic scorer is not alarmed, neither should you be.

RESPOND WITH ONLY this JSON, nothing else:
{
  "action": "HOLD" | "EXIT_NOW",
  "comment": "<=150 chars, terse — the single main reason"
}"""


def _cfg() -> dict:
    try:
        if os.path.exists(_CONFIG_PATH):
            with open(_CONFIG_PATH, encoding="utf-8") as f:
                data = json.load(f)
            block = data.get("exit_copilot")
            if isinstance(block, dict):
                cfg = dict(_DEFAULTS)
                cfg.update({k: block[k] for k in _DEFAULTS if k in block})
                return cfg
    except Exception:
        pass
    return dict(_DEFAULTS)


def enabled() -> bool:
    return bool(_cfg().get("enabled", True)) and ai_config.ai_enabled_for("sim")


def shadow_mode() -> bool:
    return bool(_cfg().get("shadow_mode", True))


def can_apply(account_env: str) -> bool:
    """True only when shadow_mode is off AND account is SIM."""
    return (not shadow_mode()) and account_env in ("sim",)


def min_mfe_r() -> float:
    return float(_cfg().get("min_mfe_r", 1.0))


# ── Dedup: one evaluation per position_key per UTC day ───────────────────────

_evaluated_today: set[str] | None = None


def _load_evaluated_today() -> set[str]:
    today = datetime.now(timezone.utc).date().isoformat()
    seen: set[str] = set()
    try:
        if os.path.exists(_LOG_PATH):
            with open(_LOG_PATH, encoding="utf-8") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln:
                        continue
                    row = json.loads(ln)
                    if str(row.get("ts", ""))[:10] == today and row.get("position_key"):
                        seen.add(row["position_key"])
    except Exception:
        pass
    return seen


def _already_evaluated(position_key: str) -> bool:
    global _evaluated_today
    if _evaluated_today is None:
        _evaluated_today = _load_evaluated_today()
    return position_key in _evaluated_today


def _mark_evaluated(position_key: str) -> None:
    global _evaluated_today
    if _evaluated_today is None:
        _evaluated_today = _load_evaluated_today()
    _evaluated_today.add(position_key)


# ── LLM call ─────────────────────────────────────────────────────────────────

def _hold(reason: str, latency_ms: float = 0.0) -> dict:
    return {
        "action": "HOLD",
        "comment": f"agent unavailable: {reason}",
        "_agent": {"ok": False, "error": reason, "latency_ms": round(latency_ms, 1)},
    }


def _coerce(raw: dict, latency_ms: float) -> dict:
    action = str(raw.get("action", "")).upper()
    if action not in ("HOLD", "EXIT_NOW"):
        return _hold(f"bad action {action!r}", latency_ms)
    return {
        "action": action,
        "comment": str(raw.get("comment", ""))[:200],
        "_agent": {"ok": True, "error": None, "latency_ms": round(latency_ms, 1)},
    }


def _extract_json(text: str) -> dict | None:
    text = (text or "").strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    a, b = text.find("{"), text.rfind("}")
    if 0 <= a < b:
        try:
            return json.loads(text[a:b + 1])
        except Exception:
            pass
    # salvage action at minimum
    m = re.search(r'"action"\s*:\s*"([A-Za-z_]+)"', text or "")
    if m:
        return {"action": m.group(1), "comment": "(salvaged)"}
    return None


def _call_llm(user_prompt: str, model: str) -> tuple[dict, float]:
    t0 = time.time()
    try:
        import anthropic
    except Exception:
        return _hold("anthropic SDK not installed", (time.time() - t0) * 1000), (time.time() - t0) * 1000

    try:
        client = anthropic.Anthropic().with_options(timeout=EVAL_TIMEOUT_S, max_retries=1)
        resp = client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=[{"type": "text", "text": _SYSTEM,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_prompt}],
        )
        latency_ms = (time.time() - t0) * 1000
        text = resp.content[0].text if resp.content else ""
        raw = _extract_json(text)
        if raw is None:
            return _hold("unparseable response", latency_ms), latency_ms
        return _coerce(raw, latency_ms), latency_ms
    except Exception as exc:
        latency_ms = (time.time() - t0) * 1000
        return _hold(str(exc)[:120], latency_ms), latency_ms


# ── Public API ────────────────────────────────────────────────────────────────

def evaluate_position(
    pos: dict,
    sym: str,
    strat_name: str,
    adv: dict | None,
    close_price: float,
    account_env: str,
) -> dict:
    """Evaluate one open position. Returns decision dict. Never raises.

    adv is the exit_advisor.score() result (may be None for underwater positions).
    close_price is the current bar close used for est_pnl_eur computation.
    """
    if not enabled():
        return _hold("exit_copilot disabled")

    is_long  = pos.get("direction", "Buy") == "Buy"
    entry    = float(pos.get("entry_price", 0) or 0)
    risk_eur = float(pos.get("risk_eur_at_entry") or 0)

    # Only evaluate if position has real profit reference
    if adv is None or adv.get("r_now", 0) < min_mfe_r():
        return _hold(f"r_now {adv.get('r_now', 0) if adv else 0:.2f} < min {min_mfe_r():.1f}R")

    position_key = f"{account_env}:{strat_name}:{sym}:{datetime.now(timezone.utc).date()}"
    if _already_evaluated(position_key):
        return _hold("already evaluated today")

    r_now        = adv["r_now"]
    mfe_r        = adv["mfe_r"]
    score        = adv["score"]
    rec          = adv["recommendation"]
    signals      = adv.get("signals", {})
    giveback     = signals.get("giveback_frac", 0.0)
    rsi_toward   = signals.get("rsi_toward_exit", False)
    time_frac    = signals.get("time_frac", 0.0)
    dist_stop    = signals.get("dist_to_stop_r", 9.9)
    atr_expansion = signals.get("atr_expansion", 1.0)

    # Estimate realized P&L if we exit now (for the counterfactual log)
    init_stop = pos.get("initial_stop_price")
    R_price   = abs(entry - float(init_stop)) if init_stop else 0.0
    est_pnl_eur = r_now * risk_eur if risk_eur else 0.0

    model = _cfg().get("model", "claude-sonnet-5")

    user_prompt = (
        f"Position: {sym} {'BUY' if is_long else 'SELL'}  strategy={strat_name}\n"
        f"r_now={r_now:.2f}R  mfe_r={mfe_r:.2f}R  giveback_frac={giveback:.2f}\n"
        f"rsi_toward_exit={rsi_toward}  time_frac={time_frac:.2f}  "
        f"atr_expansion={atr_expansion:.2f}  dist_to_stop_r={dist_stop:.2f}\n"
        f"exit_advisor score={score:.1f}  recommendation={rec}\n"
        f"est_pnl_if_exit_now={est_pnl_eur:.1f} EUR  risk_eur={risk_eur:.1f}\n\n"
        f"Should we EXIT_NOW or HOLD?"
    )

    decision, latency_ms = _call_llm(user_prompt, model)
    _mark_evaluated(position_key)

    _log(
        position_key=position_key,
        account=account_env,
        strategy=strat_name,
        symbol=sym,
        action=decision["action"],
        r_now=r_now,
        mfe_r=mfe_r,
        giveback_frac=giveback,
        exit_advisor_score=score,
        exit_advisor_rec=rec,
        entry_price=entry,
        close_price=close_price,
        risk_eur=risk_eur,
        est_pnl_eur_at_decision=round(est_pnl_eur, 2),
        applied=False,   # shadow: never applied at log time
        comment=decision.get("comment", ""),
    )

    logger.info(
        f"  [exit-copilot:{account_env}] {sym} {decision['action']} "
        f"({r_now:.1f}R / peak {mfe_r:.1f}R / give-back {giveback:.0%}) "
        f"— {decision.get('comment','')[:80]}"
    )
    return decision


def _log(**kwargs) -> None:
    row = {"ts": datetime.now(timezone.utc).isoformat(), **kwargs}
    try:
        os.makedirs(os.path.join(_BASE, "data"), exist_ok=True)
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")
    except Exception:
        pass
