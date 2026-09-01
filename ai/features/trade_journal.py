"""
ai/features/trade_journal.py -- AI Trading Journal (roadmap #18).

An LLM-written retrospective on each CLOSED trade: was the entry good, was
the exit good, why did it win or lose, and the one lesson worth keeping.
Plus a short daily pattern summary across the day's trades.

READ-ONLY, by hard design. This module:
  * imports only json / os / datetime / ai.config / (lazily) anthropic;
  * READS   data/trade_observation_cards.jsonl (entry+exit cards),
            data/ai_trade_proposals.jsonl, data/ai_shadow_decisions.jsonl,
            data/exit_advisor_shadow.jsonl;
  * WRITES  data/ai_trade_journal.jsonl (its own file) and nothing else.
It never imports forex.runner / saxo_* / pnl_tracker / housekeeping, never
places, amends or cancels an order, never mutates a position or a stop,
never influences a strategy or sizing decision. It runs entirely after a
trade has already closed. Enforced by test_2026_08_31_ai_trade_journal.py.

Covers ALL forex accounts -- SIM and both real-money LIVE accounts (live,
live_eur) -- since every forex trade writes an observation card regardless
of account. There is no account filter; the account is on each journal row.

Cost: batched LLM calls per trading day (a big day is split into
CHUNK_SIZE-trade calls; ~1 call per 8 closed trades), not one call per
trade. A truncated response is salvaged for whatever complete trade
objects it contains; a fully failed chunk logs nothing and is retried next
run (dedup by card_id). Gated by config/ai.json `journal_enabled` (default
false) -- independent of the shadow study.

    python ai_trade_journal.py            # generate entries for un-journaled closed trades
    python ai_trade_journal.py --report   # print the journal + roll-ups
"""

from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from datetime import datetime, timezone

import ai.config as ai_config

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "data")

CARDS_LOG        = os.path.join(_DATA_DIR, "trade_observation_cards.jsonl")
PROPOSALS_LOG    = os.path.join(_DATA_DIR, "ai_trade_proposals.jsonl")
SHADOW_LOG       = os.path.join(_DATA_DIR, "ai_shadow_decisions.jsonl")
EXIT_ADVISOR_LOG = os.path.join(_DATA_DIR, "exit_advisor_shadow.jsonl")
JOURNAL_LOG      = os.path.join(_DATA_DIR, "ai_trade_journal.jsonl")

EVAL_TIMEOUT_S = 120.0   # once-daily batch job -- latency doesn't matter, completeness does
MAX_TOKENS     = 16000
CHUNK_SIZE     = 8       # trades per LLM call; a big day is split into several

_SYSTEM = """You are ATOS's Trading Journal -- a post-trade analyst for a systematic \
FX trading bot. Every trade you see has ALREADY closed. You do not place, modify, or \
advise on any live order; you write an honest retrospective so the operator can learn.

You receive a JSON object: "narrate" is the list of closed trades to write up in \
detail; "all_trades_today" (when present) is a compact list of every trade that day \
for the day_summary; "partial_batch": true means narrate only, day_summary = null. \
Each trade in "narrate" has: the account (account_env -- "sim" is paper, \
"live"/"live_eur" are REAL money; weigh lessons on live trades more heavily), the \
strategy, \
symbol, direction, entry/stop/target prices, ATR at entry, position size, the market \
REGIME at entry (from a deterministic classifier), any AI Copilot verdict on the \
signal (APPROVE/REJECT/MODIFY + size multiplier, shadow-only -- it did not change the \
trade), how many times the shadow Exit Advisor said EXIT or TIGHTEN while the trade \
was open, and the outcome: net P&L in EUR, R-multiple, exit reason, holding hours, \
and MAE/MFE in EUR (worst/best unrealised P&L seen). MAE/MFE quality: if \
"mae_mfe_note" is set the value was nulled (a known data bug) -- ignore MAE/MFE for \
that trade; if "mae_mfe_coarse" is true it's a loose upper bound from a single daily \
bar (intraday strategy) -- use it directionally only. Otherwise if MAE/MFE still \
looks wildly inconsistent with net P&L and risk, say so and don't over-read it.

COST HEALTH (Saxo charges a flat ~EUR5.18 round-trip commission, so a small \
position is commission-dominated): "recovery_to_cost_ratio" is a realistic 0.5R \
recovery divided by the all-in cost (commission + spread + slippage). LIVE rejects \
anything below 3.0. "recovery_thin": true means this RSI signal WOULD have been \
rejected on LIVE -- on SIM it ran anyway because SIM tests all 184 pairs at full \
breadth. Treat a recovery_thin trade as a marginal setup: a small win on one is not \
evidence the pair is good, and a loss on one is partly a sizing/cost problem, not a \
signal problem. In the day_summary, call out if the thin trades cluster on \
particular pairs or tiers.

For EACH trade return:
- entry_quality: "excellent" | "good" | "fair" | "poor"  -- judged on regime fit, \
  signal/regime alignment, volatility, and how the trade sat against the book.
- exit_quality:  "excellent" | "good" | "fair" | "poor"  -- did it capture the move? \
  Compare net P&L to MFE (give-back), look at the exit reason and the Exit Advisor \
  signals. A trade stopped out for a small loss can still be a "good" exit.
- why_result: ONE sentence -- the single main reason it won or lost.
- lesson: ONE sentence -- a concrete, testable takeaway, or "none" if nothing stands out.
- tags: 2-5 short kebab-case tags, e.g. "counter-trend", "high-vol-entry", \
  "early-exit", "let-winner-run", "regime-mismatch", "gave-back-profit", "clean-trend".

Then return day_summary: 2-4 sentences on the PATTERNS across the day -- what the \
winners had in common, what the losers had in common, and anything the operator \
should watch. Be specific and quantitative where you can. No hedging boilerplate. \
If the user message says this is a PARTIAL batch, set day_summary to null and only \
narrate the trades given. If it includes an "all_trades_today" list, use that whole \
list for the day_summary even though you only narrate the detailed ones.

Keep every field terse: why_result and lesson are ONE sentence each, under 200 \
characters. Output ONLY raw JSON -- no code fence, no prose before or after:
{
  "trades": [
    {"card_id": "<echoed exactly>", "entry_quality": "...", "exit_quality": "...",
     "why_result": "...", "lesson": "...", "tags": ["..."]}
  ],
  "day_summary": "..." | null
}"""


# ── loading / joining (all read-only) ──────────────────────────────────────

def _load_jsonl(path: str) -> list[dict]:
    out: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    row = json.loads(ln)
                    if isinstance(row, dict):
                        out.append(row)
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return out


def _closed_trades() -> list[dict]:
    """Pair entry+exit observation cards by card_id. Returns one merged dict
    per closed trade (exit fields win), sorted by exit timestamp."""
    entry: dict[str, dict] = {}
    exits: list[dict] = []
    for c in _load_jsonl(CARDS_LOG):
        if c.get("event") == "entry" and c.get("card_id"):
            entry[c["card_id"]] = c
        elif c.get("event") == "exit" and c.get("card_id"):
            exits.append(c)
    trades = []
    for x in exits:
        if x.get("pnl_suspect"):
            continue                       # SIM P&L feed proven bad for this trade
        e = entry.get(x["card_id"])
        if e:
            # exit fields win on merge, but keep the ENTRY timestamp under its
            # own key -- the AI proposal/shadow trade_id is dated by entry day,
            # and `timestamp` becomes the exit time after the merge.
            merged = {**e, **x, "entry_timestamp": e.get("timestamp"),
                      "exit_timestamp": x.get("timestamp")}
            trades.append(merged)
    trades.sort(key=lambda t: t.get("exit_timestamp") or "")
    return trades


def _trade_id_from_card(card: dict) -> str:
    """account | strategy | symbol | UTC-date-of-ENTRY -- matches
    ai.features.trade_proposal.trade_id / the shadow log's trade_id (which
    is dated by the signal/entry day, not the exit day)."""
    return "|".join((
        str(card.get("account_env")), str(card.get("strategy")),
        str(card.get("symbol")),
        str(card.get("entry_timestamp") or card.get("timestamp") or "")[:10],
    ))


def _ai_by_trade_id() -> dict[str, dict]:
    """{trade_id: {"regime":..., "action":..., "size_multiplier":..., "comment":...}}
    from the shadow-decision log (falls back to the proposal log for regime)."""
    out: dict[str, dict] = {}
    for p in _load_jsonl(PROPOSALS_LOG):
        tid = "|".join((str(p.get("account_env")), str(p.get("strategy_name")),
                        str(p.get("symbol")), str(p.get("ts", ""))[:10]))
        out.setdefault(tid, {})["regime"] = (p.get("regime") or {}).get("label")
    for d in _load_jsonl(SHADOW_LOG):
        tid = d.get("trade_id")
        if not tid:
            continue
        rec = out.setdefault(tid, {})
        rec["regime"] = d.get("regime") or rec.get("regime")
        rec["action"] = d.get("agent_action")
        rec["size_multiplier"] = d.get("agent_size_multiplier")
        rec["comment"] = d.get("agent_comment")
    return out


def _exit_advisor_by_card() -> dict[str, dict]:
    """{card_id: {"exit": n, "tighten": n, "cycles": n}} -- how often the
    shadow Exit Advisor flagged this trade while it was open."""
    agg: dict[str, dict] = defaultdict(lambda: {"exit": 0, "tighten": 0, "cycles": 0})
    for r in _load_jsonl(EXIT_ADVISOR_LOG):
        cid = r.get("card_id")
        if not cid:
            continue
        a = agg[cid]
        a["cycles"] += 1
        rec = str(r.get("recommendation", "")).upper()
        if rec == "EXIT":
            a["exit"] += 1
        elif rec == "TIGHTEN":
            a["tighten"] += 1
    return dict(agg)


def journaled_card_ids() -> set[str]:
    return {r["card_id"] for r in _load_jsonl(JOURNAL_LOG)
            if r.get("event") != "day_summary" and r.get("card_id")}


def _days_with_summary() -> set[str]:
    return {r["day"] for r in _load_jsonl(JOURNAL_LOG)
            if r.get("event") == "day_summary" and r.get("day") and _real_summary(r.get("summary"))}


def _real_summary(s) -> str | None:
    """The model sometimes returns the literal string 'None'/'null'/'' for a
    partial batch -- treat those as no summary."""
    s = (str(s).strip() if s is not None else "")
    return s if s and s.lower() not in ("none", "null", "n/a") else None


# ── dossier assembly ──────────────────────────────────────────────────────

def build_dossiers(since: str | None = None, limit: int | None = None) -> list[dict]:
    """Closed trades not yet journaled, each with its AI + exit-advisor
    context attached. `since` = 'YYYY-MM-DD' lower bound on the exit date."""
    done = journaled_card_ids()
    ai_idx = _ai_by_trade_id()
    ea_idx = _exit_advisor_by_card()
    limit = limit or ai_config.journal_max_trades_per_run()

    dossiers = []
    for t in _closed_trades():
        cid = t.get("card_id")
        if not cid or cid in done:
            continue
        exit_day = str(t.get("timestamp") or "")[:10]
        if since and exit_day < since:
            continue
        ai = ai_idx.get(_trade_id_from_card(t), {})
        ea = ea_idx.get(cid, {})
        dossiers.append({
            "card_id": cid,
            "day": exit_day,
            "account_env": t.get("account_env"),
            "strategy": t.get("strategy"),
            "symbol": t.get("symbol"),
            "direction": t.get("direction"),
            "entry_price": t.get("entry_price"),
            "stop_at_entry": t.get("current_stop"),
            "atr_at_entry": t.get("atr_at_entry"),
            "quantity": t.get("quantity"),
            "risk_eur": t.get("risk_eur"),
            # cost health AT ENTRY -- the flat Saxo commission dominates, so
            # a small position (or a tight-stop pair) can be cost-dominated.
            # `recovery_to_cost_ratio` is 0.5R / all-in cost (LIVE rejects
            # < 3.0); `recovery_thin` True == this RSI signal would have been
            # REJECTED on LIVE -- on SIM it ran anyway (full 184-pair
            # breadth), so weigh it as a marginal setup, not a clean one.
            "cost_eur": t.get("cost_eur"),
            "all_in_cost_eur": t.get("all_in_cost_eur"),
            "cost_to_edge_ratio": t.get("cost_to_edge_ratio"),
            "recovery_to_cost_ratio": t.get("recovery_to_cost_ratio"),
            "recovery_thin": bool(t.get("recovery_thin")),
            "regime_at_entry": ai.get("regime"),
            "ai_action": ai.get("action"),
            "ai_size_multiplier": ai.get("size_multiplier"),
            "ai_comment": ai.get("comment"),
            "exit_advisor_exit_flags": ea.get("exit", 0),
            "exit_advisor_tighten_flags": ea.get("tighten", 0),
            "exit_price": t.get("exit_price"),
            "exit_reason": t.get("exit_reason"),
            "net_pnl_eur": t.get("net_pnl_eur"),
            "gross_pnl_eur": t.get("gross_pnl_eur"),
            "r_multiple": t.get("r_multiple"),
            "mae_eur": t.get("mae_eur"),
            "mfe_eur": t.get("mfe_eur"),
            # MAE/MFE quality flags (2026-09-01): coarse == taken from a
            # single daily bar (intraday strategy, sub-day hold -> loose
            # upper bound); invalidated == pre-fix corrupted value, nulled.
            "mae_mfe_coarse": bool(t.get("mae_mfe_coarse")),
            "mae_mfe_note": t.get("mae_mfe_invalidated"),
            "holding_hours": t.get("holding_hours"),
        })
        if len(dossiers) >= limit:
            break
    return dossiers


# ── the LLM call ──────────────────────────────────────────────────────────

def _strip_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else text[3:]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()


def _salvage_trade_objects(text: str) -> list[dict]:
    """Pull every COMPLETE {...} object nested one level inside the top
    object (i.e. the elements of the "trades" array) out of a response that
    was truncated mid-array. A brace scanner that respects strings/escapes."""
    out: list[dict] = []
    depth = start = 0
    have_start = in_str = esc = False
    for i, ch in enumerate(text):
        if esc:
            esc = False
            continue
        if in_str:
            if ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
            if depth == 2:
                start, have_start = i, True
        elif ch == "}":
            if depth == 2 and have_start:
                try:
                    o = json.loads(text[start:i + 1])
                    if isinstance(o, dict) and o.get("card_id"):
                        out.append(o)
                except Exception:
                    pass
                have_start = False
            depth = max(0, depth - 1)
    return out


def _extract_json(text: str) -> dict | None:
    """Parse the model's JSON. Tolerates a ```json fence, leading/trailing
    prose, and -- critically -- a response TRUNCATED mid-array (max_tokens
    or a dropped stream): in that case salvage every complete trade object
    plus a trailing day_summary if it's intact."""
    text = _strip_fence(text or "")
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    a = text.find("{")
    if a >= 0:
        try:
            obj = json.loads(text[a:text.rfind("}") + 1])
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    salvaged = _salvage_trade_objects(text)
    if salvaged:
        ds = None
        m = text.rfind('"day_summary"')
        if m >= 0:
            try:
                ds = json.loads("{" + text[m:].split("}", 1)[0].rstrip().rstrip(",") + "}").get("day_summary")
            except Exception:
                ds = None
        return {"trades": salvaged, "day_summary": ds, "_truncated": True}
    return None


def _day_context_row(d: dict) -> dict:
    """Compact one-liner for the day_summary context (not the detailed narrate list)."""
    return {"account": d.get("account_env"), "symbol": d.get("symbol"),
            "strategy": d.get("strategy"), "direction": d.get("direction"),
            "regime": d.get("regime_at_entry"),
            "net_pnl_eur": d.get("net_pnl_eur"), "r_multiple": d.get("r_multiple"),
            "exit_reason": d.get("exit_reason"),
            "recovery_thin": bool(d.get("recovery_thin"))}


def generate(dossiers: list[dict], day_context: list[dict] | None = None) -> dict:
    """One LLM call. Narrates `dossiers`; if `day_context` (compact rows for
    the whole day) is given, also returns a day_summary spanning it.
    Returns {"trades": {card_id: {...}}, "day_summary": str|None, "_agent": {...}}.
    NEVER raises -- any failure returns empty narratives + an error note."""
    model = ai_config.journal_model()
    t0 = time.time()
    if not dossiers:
        return {"trades": {}, "day_summary": None,
                "_agent": {"ok": False, "error": "no dossiers", "model": model, "latency_ms": 0}}
    try:
        import anthropic
    except Exception:
        return {"trades": {}, "day_summary": None,
                "_agent": {"ok": False, "error": "anthropic SDK not installed",
                           "model": model, "latency_ms": 0}}
    if day_context:
        payload = {"partial_batch": False, "narrate": dossiers, "all_trades_today": day_context}
    else:
        payload = {"partial_batch": True, "narrate": dossiers}
    try:
        client = anthropic.Anthropic().with_options(timeout=EVAL_TIMEOUT_S, max_retries=1)
        resp = client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": json.dumps(payload, default=str)}],
        )
    except Exception as exc:
        return {"trades": {}, "day_summary": None,
                "_agent": {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}",
                           "model": model, "latency_ms": round((time.time() - t0) * 1000, 1)}}

    latency = round((time.time() - t0) * 1000, 1)
    stop = getattr(resp, "stop_reason", None)
    if stop == "refusal":
        return {"trades": {}, "day_summary": None,
                "_agent": {"ok": False, "error": "model refusal", "model": model, "latency_ms": latency}}

    text = "".join(b.text for b in getattr(resp, "content", []) if getattr(b, "type", "") == "text")
    raw = _extract_json(text)
    if not isinstance(raw, dict):
        why = "hit max_tokens" if stop == "max_tokens" else "unparseable"
        return {"trades": {}, "day_summary": None,
                "_agent": {"ok": False, "error": f"{why}: {text[:120]!r}",
                           "model": model, "latency_ms": latency, "stop_reason": stop}}

    by_card: dict[str, dict] = {}
    for row in (raw.get("trades") or []):
        if isinstance(row, dict) and row.get("card_id"):
            by_card[str(row["card_id"])] = {
                "entry_quality": str(row.get("entry_quality", ""))[:20] or None,
                "exit_quality": str(row.get("exit_quality", ""))[:20] or None,
                "why_result": str(row.get("why_result", ""))[:300] or None,
                "lesson": str(row.get("lesson", ""))[:300] or None,
                "tags": [str(t)[:40] for t in (row.get("tags") or [])][:6],
            }
    if not by_card:
        return {"trades": {}, "day_summary": None,
                "_agent": {"ok": False, "error": f"no usable trade objects (stop={stop})",
                           "model": model, "latency_ms": latency, "stop_reason": stop}}
    return {"trades": by_card,
            "day_summary": (str(raw.get("day_summary") or "")[:1500] or None),
            "_agent": {"ok": True, "error": None, "model": model, "latency_ms": latency,
                       "stop_reason": stop, "truncated": bool(raw.get("_truncated"))}}


# ── writing our own file (the only thing this module mutates) ──────────────

def _append(row: dict) -> None:
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        with open(JOURNAL_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")
    except Exception:
        pass


def _log_day(day: str, dossiers: list[dict], result: dict,
             day_totals: list[dict] | None = None) -> int:
    """Log one chunk of a day's trades + (if this chunk produced one AND the
    day has no summary yet) the day_summary. `day_totals` is the whole day's
    dossiers, used only for the summary row's n_trades/net_eur so a chunked
    day still totals correctly."""
    narr = result.get("trades", {})
    meta = result.get("_agent", {})
    now = datetime.now(timezone.utc).isoformat()
    have_summary = _days_with_summary()
    written = 0
    for d in dossiers:
        n = narr.get(d["card_id"], {})
        _append({
            "event": "trade",
            "card_id": d["card_id"], "ts": now, "day": day,
            "account_env": d["account_env"], "strategy": d["strategy"],
            "symbol": d["symbol"], "direction": d["direction"],
            "net_pnl_eur": d["net_pnl_eur"], "r_multiple": d["r_multiple"],
            "exit_reason": d["exit_reason"], "holding_hours": d["holding_hours"],
            "regime_at_entry": d["regime_at_entry"],
            "ai_action": d["ai_action"], "ai_size_multiplier": d["ai_size_multiplier"],
            "entry_quality": n.get("entry_quality"),
            "exit_quality": n.get("exit_quality"),
            "why_result": n.get("why_result"),
            "lesson": n.get("lesson"),
            "tags": n.get("tags", []),
            "narrated": bool(n),
            "_agent": meta,
        })
        written += 1
    summary = _real_summary(result.get("day_summary"))
    if summary and day not in have_summary:
        span = day_totals or dossiers
        _append({
            "event": "day_summary", "ts": now, "day": day,
            "n_trades": len(span),
            "net_eur": round(sum((d.get("net_pnl_eur") or 0) for d in span), 2),
            "summary": summary,
            "_agent": meta,
        })
    return written


# ── orchestration ────────────────────────────────────────────────────────

def run(since: str | None = None) -> dict:
    """Generate + log journal entries for un-journaled closed trades. Gated
    by config/ai.json `journal_enabled`. Read-only w.r.t. all trading
    state; writes only data/ai_trade_journal.jsonl. Never raises."""
    if not ai_config.journal_enabled():
        return {"status": "disabled", "journaled": 0, "days": 0}
    try:
        dossiers = build_dossiers(since=since)
    except Exception as exc:
        return {"status": "error", "error": f"build_dossiers: {exc}", "journaled": 0, "days": 0}
    if not dossiers:
        return {"status": "nothing_new", "journaled": 0, "days": 0}

    by_day: dict[str, list[dict]] = defaultdict(list)
    for d in dossiers:
        by_day[d["day"]].append(d)

    total = 0
    days_with_output: set[str] = set()
    errors = []
    for day in sorted(by_day):
        day_dossiers = by_day[day]
        # split a big day into chunks so one LLM call never has to narrate
        # too many trades at once (it times out / truncates). The FIRST chunk
        # also carries the whole-day context and produces the day_summary.
        chunks = [day_dossiers[i:i + CHUNK_SIZE]
                  for i in range(0, len(day_dossiers), CHUNK_SIZE)]
        day_context = [_day_context_row(d) for d in day_dossiers]
        for idx, chunk in enumerate(chunks):
            try:
                result = generate(chunk, day_context=day_context if idx == 0 else None)
            except Exception as exc:                    # pragma: no cover - defensive
                result = {"trades": {}, "day_summary": None,
                          "_agent": {"ok": False, "error": f"generate: {exc}"}}
            if not (result.get("_agent") or {}).get("ok"):
                # this chunk failed -- log nothing for it, next run retries
                # exactly these trades (dedup is by card_id).
                errors.append(f"{day} chunk {idx + 1}/{len(chunks)}: "
                              f"{(result.get('_agent') or {}).get('error')}")
                continue
            n = _log_day(day, chunk, result, day_totals=day_dossiers)
            total += n
            if n:
                days_with_output.add(day)

    return {"status": "ok", "journaled": total, "days": len(days_with_output),
            "errors": errors}
