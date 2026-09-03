"""
ai/features/research_analyst.py -- the AI Research Analyst (roadmap #19).

Turns the trade record into a triaged backlog of SPECIFIED, testable
strategy-improvement hypotheses:

  1. build_research_digest()  -- pure aggregation over the closed-trade
     record + the AI Trading Journal + the decomposition-harness cache.
  2. propose_hypotheses()     -- one batched LLM call: "here is how each
     strategy behaves by regime / pair-tier / feature; propose concrete
     entry/exit/sizing filters worth backtesting." Never proposes to trade.
  3. auto_gate()              -- for any hypothesis carrying a decompose
     spec, run the cheap deterministic decomposition gate and attach the
     verdict.
  4. the backlog              -- data/ai_research_hypotheses.jsonl, append-
     only, deduped by claim hash, status lifecycle proposed -> gate_* ->
     (human) backtesting -> validated/falsified/shelped -> shipped.

READ-ONLY, by hard design -- same contract as ai/features/trade_journal.py.
This module imports only json / os / hashlib / datetime / statistics /
ai.config / ai.research.decompose / ai.features.trade_journal (all
read-only) and, lazily, anthropic. It never imports forex.runner /
saxo_* / pnl_tracker mutators / housekeeping, never places/amends/cancels
an order, never mutates a position or a stop, never edits a strategy file.
Output is {hypothesis, evidence}; a human writes the deterministic gate and
ships it as a SIM A/B twin. Enforced by test_ai_research_analyst.py.

    python ai_research_analyst.py            # digest -> propose -> auto-gate -> backlog
    python ai_research_analyst.py --report   # the ranked backlog
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from statistics import mean

import ai.config as ai_config
import ai.features.trade_journal as tj
from ai.research import decompose as D

_DATA_DIR = tj._DATA_DIR
HYPOTHESES_LOG = os.path.join(_DATA_DIR, "ai_research_hypotheses.jsonl")
DIGEST_PATH    = os.path.join(_DATA_DIR, "ai_research_digest.json")

_ROSTER = D.ROSTER
_RECENT_DAYS_FOR_THEMES = 21
_STATUSES = {"proposed", "gate_passed", "gate_failed", "backtesting",
             "validated", "falsified", "shelved", "shipped"}
EVAL_TIMEOUT_S = 120.0
MAX_TOKENS = 8000

_SYSTEM = """You are ATOS's quant Research Analyst. ATOS is a systematic FX bot that \
runs 5 daily-bar strategies (rsi = RSI(2) pullback with trend filter; rsi_trend = \
rsi gated to TRENDING regimes; ema_trend = EMA(5/30) crossover, fresh + DI-backed; \
bb_quality = Bollinger reversion in non-directional markets; zscore_quality = \
z-score reversion, low DI-spread) across 184 currency pairs on a SIM paper account.

You are given a digest of how each strategy has actually behaved -- win rate and \
average R by market regime and by pair-tier, give-back (how much of the favourable \
excursion is handed back), recurring lessons from the trade journal, and the \
results of a decomposition harness that replays each strategy over ~13 years and \
tells you which entry-context buckets carry a statistically stable edge.

Your job: propose a SHORT list of concrete, testable improvements -- an entry gate, \
an exit rule, or a sizing tweak -- each falsifiable by the decomposition harness. \
You do NOT trade, write code, or touch any live order. A human reads your \
hypothesis, writes the deterministic gate, and forward-tests it as an isolated SIM \
A/B twin.

Rules for every hypothesis:
- It must name ONE strategy, ONE feature, and a CONCRETE rule (e.g. "keep a \
bb_quality entry only when adx < 20").
- expected_effect_r: your estimate of the change in average R per trade, with a \
sign. Be honest; small is fine.
- decompose_spec: {"strategy": ..., "feature": one of \
[regime, di_spread, adx, atr_pctile, crossover_age, dist_ema200_atr, dow]}. \
The harness will bucket the real replayed trades by that feature and check whether \
your proposed bucket is positive in both halves of history with a bootstrap CI \
that excludes zero.
- Prefer hypotheses the digest's evidence already points at. Do not invent \
features that aren't in the list above.
- No more than the requested count. If the evidence is thin, propose fewer.

Return ONLY JSON:
{"hypotheses": [
  {"claim": "<one sentence>",
   "strategy": "<name>",
   "kind": "entry_gate" | "exit_gate" | "sizing",
   "feature": "<feature>",
   "rule": "<concrete, e.g. keep entry only if adx < 20>",
   "expected_effect_r": <float>,
   "rationale": "<2-3 sentences citing the digest>",
   "evidence_refs": ["<short strings pointing at digest rows>"],
   "decompose_spec": {"strategy": "<name>", "feature": "<feature>"}}
]}
"""


# ── 1. digest ──────────────────────────────────────────────────────────

def _trade_rows() -> list[dict]:
    """Lightweight per-closed-trade rows: strategy / symbol / regime / R /
    give-back. Reuses trade_journal's readers (all read-only)."""
    ai_idx = tj._ai_by_trade_id()
    rows = []
    for t in tj._closed_trades():
        if (t.get("market") or "fx") != "fx":
            continue
        r = t.get("r_multiple")
        if not isinstance(r, (int, float)):
            continue
        ai = ai_idx.get(tj._trade_id_from_card(t), {})
        mfe = t.get("mfe_eur")
        net = t.get("net_pnl_eur")
        give_back = None
        if isinstance(mfe, (int, float)) and mfe > 0 and isinstance(net, (int, float)):
            give_back = round(max(0.0, (mfe - max(net, 0.0)) / mfe), 3)
        rows.append({
            "day": str(t.get("exit_timestamp") or t.get("timestamp") or "")[:10],
            "strategy": t.get("strategy"),
            "symbol": t.get("symbol"),
            "tier": _tier(t.get("symbol")),
            "regime": ai.get("regime") or t.get("regime_at_entry"),
            "r_multiple": float(r),
            "give_back": give_back,
            "exit_reason": t.get("exit_reason"),
        })
    return rows


def _tier(symbol) -> str:
    try:
        from forex.universe import get_tier
        return get_tier(str(symbol))
    except Exception:
        return "?"


def _agg(rows: list[dict], key) -> list[dict]:
    groups: dict = defaultdict(list)
    for row in rows:
        groups[key(row)].append(row)
    out = []
    for k, g in sorted(groups.items(), key=lambda kv: str(kv[0])):
        rs = [x["r_multiple"] for x in g]
        gb = [x["give_back"] for x in g if isinstance(x["give_back"], (int, float))]
        out.append({
            "key": k, "n": len(g),
            "win_rate": round(sum(1 for r in rs if r > 0) / len(rs) * 100, 1),
            "avg_r": round(mean(rs), 3),
            "avg_give_back": round(mean(gb), 3) if gb else None,
        })
    return out


def _journal_themes(since_day: str) -> dict:
    rows = tj._load_jsonl(tj.JOURNAL_LOG)
    tags = Counter()
    lessons: list[str] = []
    day_summaries: list[str] = []
    for r in rows:
        if (r.get("day") or "") < since_day:
            continue
        if r.get("event") == "trade":
            for tag in (r.get("tags") or []):
                tags[str(tag)] += 1
            les = r.get("lesson")
            if les and str(les).lower() not in ("none", ""):
                lessons.append(f"[{r.get('strategy')}] {les}")
        elif r.get("event") == "day_summary" and r.get("summary"):
            day_summaries.append(f"{r.get('day')}: {r['summary']}")
    return {
        "top_tags": tags.most_common(15),
        "recent_lessons": lessons[-40:],
        "recent_day_summaries": day_summaries[-14:],
    }


def build_research_digest(since: str | None = None) -> dict:
    """Pure aggregation, no LLM. Never raises."""
    try:
        rows = _trade_rows()
    except Exception as exc:                            # pragma: no cover - defensive
        rows = []
        err = f"trade_rows: {exc}"
    else:
        err = None
    if since:
        rows = [r for r in rows if r["day"] >= since]

    since_day = since or _n_days_ago(_RECENT_DAYS_FOR_THEMES)
    roster_rows = [r for r in rows if r["strategy"] in _ROSTER]

    digest = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "window_since": since or "all",
        "n_closed_trades": len(rows),
        "by_strategy": _agg(roster_rows, lambda r: r["strategy"]),
        "by_strategy_regime": _agg(
            [r for r in roster_rows if r["regime"]],
            lambda r: f'{r["strategy"]} / {r["regime"]}'),
        "by_strategy_tier": _agg(roster_rows, lambda r: f'{r["strategy"]} / {r["tier"]}'),
        "journal": _journal_themes(since_day),
        "decomposition_cache": _decomp_summary(),
    }
    if err:
        digest["_warning"] = err

    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        tmp = DIGEST_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(digest, f, indent=2, default=str)
        os.replace(tmp, DIGEST_PATH)
    except Exception:
        pass
    return digest


def _decomp_summary() -> dict:
    """Compact view of the decomposition-harness cache (data/
    ai_research_decomp_cache.json), refreshed by the weekly --sweep."""
    cache = D.cached_verdicts()
    out = {}
    for strat, blob in cache.items():
        if "error" in blob:
            out[strat] = {"error": blob["error"], "ts": blob.get("ts")}
            continue
        passing = []
        for v in blob.get("verdicts", []):
            for b in v.get("buckets", []):
                if b.get("gate_pass"):
                    passing.append({
                        "feature": v["feature"], "bucket": b["label"], "n": b["n"],
                        "avg_r": b["avg_r"], "pf": b.get("pf"),
                        "halves": [b["first_half_avg_r"], b["second_half_avg_r"]],
                    })
        out[strat] = {"ts": blob.get("ts"), "base_by_feature":
                      {v["feature"]: v["base_avg_r"] for v in blob.get("verdicts", [])},
                      "gate_passing_buckets": passing}
    return out


def _n_days_ago(n: int) -> str:
    o = datetime.now(timezone.utc).date().toordinal() - max(0, n)
    return datetime.fromordinal(o).strftime("%Y-%m-%d")


# ── 2. propose ─────────────────────────────────────────────────────────

def _claim_hash(h: dict) -> str:
    basis = f'{h.get("strategy")}|{h.get("kind")}|{h.get("feature")}|{h.get("rule","").strip().lower()}'
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]


def propose_hypotheses(digest: dict, max_n: int | None = None) -> dict:
    """One LLM call. Returns {"hypotheses": [...], "_agent": {...}}. Never raises."""
    model = ai_config.research_analyst_cfg().get("model") or ai_config.agent_model()
    max_n = max_n or int(ai_config.research_analyst_cfg().get("max_hypotheses_per_run", 8))
    t0 = time.time()
    try:
        import anthropic
    except Exception:
        return {"hypotheses": [], "_agent": {"ok": False, "error": "anthropic SDK not installed",
                                             "model": model, "latency_ms": 0}}
    payload = {"max_hypotheses": max_n, "digest": digest}
    try:
        client = anthropic.Anthropic().with_options(timeout=EVAL_TIMEOUT_S, max_retries=1)
        resp = client.messages.create(
            model=model, max_tokens=MAX_TOKENS,
            system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": json.dumps(payload, default=str)}],
        )
    except Exception as exc:
        return {"hypotheses": [], "_agent": {"ok": False,
                "error": f"{type(exc).__name__}: {str(exc)[:160]}", "model": model,
                "latency_ms": round((time.time() - t0) * 1000, 1)}}

    latency = round((time.time() - t0) * 1000, 1)
    text = "".join(b.text for b in getattr(resp, "content", []) if getattr(b, "type", "") == "text")
    raw = tj._extract_json(text) if hasattr(tj, "_extract_json") else None
    if not isinstance(raw, dict):
        try:
            raw = json.loads(tj._strip_fence(text))
        except Exception:
            raw = None
    if not isinstance(raw, dict):
        return {"hypotheses": [], "_agent": {"ok": False, "error": f"unparseable: {text[:120]!r}",
                                             "model": model, "latency_ms": latency}}

    clean = []
    for h in (raw.get("hypotheses") or [])[:max_n]:
        if not isinstance(h, dict) or not h.get("claim") or not h.get("strategy"):
            continue
        spec = h.get("decompose_spec") or {}
        clean.append({
            "claim": str(h.get("claim"))[:300],
            "strategy": str(h.get("strategy"))[:40],
            "kind": str(h.get("kind", "entry_gate"))[:20],
            "feature": str(h.get("feature", ""))[:40],
            "rule": str(h.get("rule", ""))[:300],
            "expected_effect_r": _num(h.get("expected_effect_r")),
            "rationale": str(h.get("rationale", ""))[:600],
            "evidence_refs": [str(x)[:120] for x in (h.get("evidence_refs") or [])][:6],
            "decompose_spec": {"strategy": str(spec.get("strategy") or h.get("strategy"))[:40],
                               "feature": str(spec.get("feature") or h.get("feature"))[:40]},
        })
    return {"hypotheses": clean,
            "_agent": {"ok": True, "error": None, "model": model, "latency_ms": latency,
                       "stop_reason": getattr(resp, "stop_reason", None)}}


def _num(x):
    try:
        return round(float(x), 3)
    except (TypeError, ValueError):
        return None


# ── 3. auto-gate ───────────────────────────────────────────────────────

def auto_gate(hyp: dict, trades_by_strategy: dict | None = None,
              years: int = 13) -> dict:
    """Run the decomposition gate for one hypothesis. Returns
    {"status": "gate_passed"|"gate_failed"|"gate_skipped", "verdict": {...}}.
    Deterministic, read-only, never raises."""
    spec = hyp.get("decompose_spec") or {}
    strat, feature = spec.get("strategy"), spec.get("feature")
    if strat not in D.MODULE_IMPORT or not feature:
        return {"status": "gate_skipped", "verdict": None,
                "note": f"no runnable spec ({strat}/{feature})"}
    try:
        if trades_by_strategy is not None and strat in trades_by_strategy:
            trades = trades_by_strategy[strat]
        else:
            trades = D.replay_trades(strat, years=years, core_only=True)
        verdict = D.bucket_and_gate(trades, feature)
    except Exception as exc:                            # pragma: no cover - defensive
        return {"status": "gate_skipped", "verdict": None, "note": f"replay failed: {exc}"}

    passing = verdict.passing
    return {
        "status": "gate_passed" if passing else "gate_failed",
        "verdict": D._verdict_to_dict(verdict),
        "note": (f"{len(passing)} bucket(s) stable-positive: "
                 f"{[b.label for b in passing]}" if passing
                 else "no bucket positive in both halves with CI excluding zero"),
    }


# ── 4. backlog ─────────────────────────────────────────────────────────

def _load_backlog() -> list[dict]:
    return tj._load_jsonl(HYPOTHESES_LOG)


def _latest_by_id() -> dict[str, dict]:
    """Fold the append-only log into the current state per hypothesis id."""
    state: dict[str, dict] = {}
    for row in _load_backlog():
        hid = row.get("id")
        if not hid:
            continue
        cur = state.get(hid, {})
        cur.update({k: v for k, v in row.items() if v is not None})
        state[hid] = cur
    return state


def _append(row: dict) -> None:
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        with open(HYPOTHESES_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")
    except Exception:
        pass


def set_status(hid: str, status: str, note: str = "") -> bool:
    """Human status transition. Appends an event row (log is append-only)."""
    if status not in _STATUSES:
        raise ValueError(f"status must be one of {sorted(_STATUSES)}")
    if hid not in _latest_by_id():
        return False
    _append({"event": "status", "id": hid, "status": status, "note": note,
             "ts": datetime.now(timezone.utc).isoformat()})
    return True


# ── orchestration ──────────────────────────────────────────────────────

def run(since: str | None = None, sweep_first: bool = False) -> dict:
    """digest -> propose -> auto-gate -> append new hypotheses. Gated by
    config/ai.json research_analyst.enabled. Read-only w.r.t. trading
    state; writes only its own two files. Never raises."""
    if not ai_config.research_analyst_enabled():
        return {"status": "disabled", "proposed": 0, "gate_passed": 0}

    cfg = ai_config.research_analyst_cfg()
    years = int(cfg.get("sweep_years", 13))
    if sweep_first:
        try:
            D.refresh_cache(years=years)
        except Exception:
            pass

    digest = build_research_digest(since=since)
    prop = propose_hypotheses(digest)
    if not (prop.get("_agent") or {}).get("ok"):
        return {"status": "digest_only", "proposed": 0, "gate_passed": 0,
                "error": (prop.get("_agent") or {}).get("error")}

    have = _latest_by_id()
    have_hashes = {r.get("claim_hash") for r in have.values()}
    replay_cache: dict = {}
    added = 0
    gate_passed = 0
    now = datetime.now(timezone.utc).isoformat()

    for h in prop["hypotheses"]:
        ch = _claim_hash(h)
        if ch in have_hashes:
            continue
        hid = f"H{now[:10].replace('-', '')}-{ch[:6]}"
        _append({"event": "proposed", "id": hid, "claim_hash": ch, "ts": now,
                 "status": "proposed", "_agent": prop["_agent"], **h})
        added += 1

        gate = auto_gate(h, trades_by_strategy=replay_cache, years=years)
        _append({"event": "gate", "id": hid, "ts": now,
                 "status": gate["status"], "note": gate.get("note"),
                 "verdict": gate.get("verdict")})
        if gate["status"] == "gate_passed":
            gate_passed += 1

    return {"status": "ok", "proposed": added, "gate_passed": gate_passed,
            "n_hypotheses_seen": len(prop["hypotheses"])}


def backlog_view() -> list[dict]:
    """Current state of every hypothesis, ranked: gate_passed first, then by
    expected_effect_r desc. For the --report renderer + the digest email."""
    state = list(_latest_by_id().values())
    rank = {"gate_passed": 0, "backtesting": 1, "validated": 1, "proposed": 2,
            "gate_failed": 3, "shelved": 4, "falsified": 4, "shipped": 5}
    state.sort(key=lambda r: (rank.get(r.get("status"), 9),
                              -(r.get("expected_effect_r") or 0)))
    return state
