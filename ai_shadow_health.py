"""
ai_shadow_health.py -- monitor the AI shadow study's data pipeline.

Why this exists
---------------
The AI advisory layer (ai/, Sprint 3) is deliberately wrapped in
try/except everywhere in forex/runner.py: if the paid agent call fails --
ANTHROPIC_API_KEY gone, monthly spend cap hit, network blip, timeout,
model refusal, a broken import -- ai.agent.trading_copilot.evaluate_proposal()
silently returns {"action": "HOLD", ...} and a shadow row is STILL written
to data/ai_shadow_decisions.jsonl. So the file keeps growing at the normal
rate even when nothing is actually being evaluated.

scheduler_watchdog.py only checks that the *scan task* ran (result code +
log freshness). It has no visibility into the AI sub-layer inside that
run. This module closes that gap.

What it checks (all read-only)
------------------------------
1. Degraded rate -- of the shadow decisions in the last 7 days, what share
   came back HOLD/degraded (no real LLM verdict). Over 30% on a real
   sample => something is broken, not just the model being cautious.
2. Silent while active -- agent-eligible signals ARE being logged as
   proposals but NONE of them ever get a shadow decision => the agent
   hook isn't running at all (agent_enabled flipped off, import failing,
   an exception every call).
3. Total silence -- neither file has been written in over 96h while the
   study is switched on => the SIM scan stopped feeding the AI hook, or
   config/ai.json got disabled.

check() returns a list of human-readable problem strings (empty ==
healthy). scheduler_watchdog.py calls it once per pass and folds the
result into its existing alert / dedup / email path. Also runnable by
hand:  python ai_shadow_health.py
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")

# Overridable in tests.
DECISIONS = os.path.join(DATA_DIR, "ai_shadow_decisions.jsonl")
PROPOSALS = os.path.join(DATA_DIR, "ai_trade_proposals.jsonl")
CONFIG_PATH = os.path.join(BASE_DIR, "config", "ai.json")

# ── thresholds ──────────────────────────────────────────────────────────────
DEGRADED_WINDOW_DAYS = 7
DEGRADED_MIN_SAMPLE = 8      # need at least this many decisions to judge a rate
DEGRADED_MAX_FRAC = 0.30     # > this share degraded => alert

SILENT_WINDOW_H = 48        # look back this far for "proposals but no decisions"
SILENT_GRACE_H = 2          # ignore proposals newer than this (same-scan race)
SILENT_MIN_PROPOSALS = 3    # need this many distinct eligible signals to judge

TOTAL_SILENCE_H = 96        # neither file touched in this long => alert


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


def _parse_ts(row: dict) -> datetime | None:
    raw = row.get("ts")
    if not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _load_config() -> dict:
    """config/ai.json, or ai.config's merged view if importable. Any failure
    -> {} (treated as study-off, so this monitor stays quiet)."""
    try:
        import ai.config as ai_config           # noqa: WPS433
        return dict(ai_config._load())          # already merged with defaults
    except Exception:
        pass
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _study_on(cfg: dict) -> bool:
    return bool(cfg.get("agent_enabled")
                and (cfg.get("enabled_sim") or cfg.get("enabled_live_shadow")))


def _eligible_strategies(cfg: dict) -> set[str] | None:
    """None == all strategies (config had ["*"])."""
    lst = cfg.get("agent_strategies") or ["rsi"]
    if not isinstance(lst, list):
        return {"rsi"}
    if "*" in lst:
        return None
    return set(lst)


def _tid(row: dict) -> str:
    """account | strategy | symbol | UTC-date -- same shape as
    ai.features.trade_proposal.trade_id / the shadow log's trade_id."""
    strat = row.get("strategy_name") or row.get("strategy")
    return "|".join((str(row.get("account_env")), str(strat),
                     str(row.get("symbol")), str(row.get("ts", ""))[:10]))


def _is_ok(dec: dict) -> bool:
    return (dec.get("agent_meta") or {}).get("ok") is True


def _newest_mtime(*paths: str) -> float | None:
    times = [os.path.getmtime(p) for p in paths if os.path.exists(p)]
    return max(times) if times else None


def check(now: datetime | None = None, cfg: dict | None = None) -> list[str]:
    """Return a list of problem descriptions (empty == healthy).
    Never raises."""
    try:
        return _check(now, cfg)
    except Exception as exc:                    # pragma: no cover - defensive
        return [f"ai_shadow_health check itself errored: {type(exc).__name__}: {exc}"]


def _check(now: datetime | None, cfg: dict | None) -> list[str]:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cfg = _load_config() if cfg is None else cfg

    if not _study_on(cfg):
        return []   # study is off on purpose -- nothing to monitor

    problems: list[str] = []
    decs = _load_jsonl(DECISIONS)
    props = _load_jsonl(PROPOSALS)

    # ── 3. total silence ────────────────────────────────────────────────────
    mtime = _newest_mtime(DECISIONS, PROPOSALS)
    if mtime is None:
        problems.append(
            "AI shadow study is switched on (config/ai.json agent_enabled + "
            "enabled_*) but neither data/ai_shadow_decisions.jsonl nor "
            "data/ai_trade_proposals.jsonl exists yet -- the AI hook in "
            "forex/runner.py has never produced a row. Check the scan log for "
            "'[ai] advisory layer unavailable' or '[ai] advisory hook failed'.")
        return problems   # nothing else to check without files
    age_h = (now.timestamp() - mtime) / 3600
    if age_h > TOTAL_SILENCE_H:
        problems.append(
            f"AI shadow study: no proposal or decision written in {age_h:.0f}h "
            f"(> {TOTAL_SILENCE_H}h). Across the RSI universe on the 30-min SIM "
            f"cadence at least one signal was expected by now -- the scan is "
            f"likely not reaching the AI hook, or config/ai.json was disabled.")

    # ── 1. degraded rate ───────────────────────────────────────────────────
    window_start = now - timedelta(days=DEGRADED_WINDOW_DAYS)
    recent = [d for d in decs
              if (_parse_ts(d) or datetime.min.replace(tzinfo=timezone.utc)) >= window_start]
    if len(recent) >= DEGRADED_MIN_SAMPLE:
        degraded = [d for d in recent if not _is_ok(d)]
        frac = len(degraded) / len(recent)
        if frac > DEGRADED_MAX_FRAC:
            errs = Counter(((d.get("agent_meta") or {}).get("error") or "unknown")
                           for d in degraded)
            top = ", ".join(f"{e} x{n}" for e, n in errs.most_common(3))
            problems.append(
                f"AI agent degraded: {len(degraded)}/{len(recent)} shadow "
                f"decisions in the last {DEGRADED_WINDOW_DAYS}d came back HOLD "
                f"with no real LLM verdict ({frac:.0%}, over the "
                f"{DEGRADED_MAX_FRAC:.0%} line). Top causes: {top}. Nothing is "
                f"being learned while this persists -- check ANTHROPIC_API_KEY, "
                f"the Anthropic console spend cap, and network reachability.")

    # ── 2. silent while active ─────────────────────────────────────────────
    elig = _eligible_strategies(cfg)
    lo = now - timedelta(hours=SILENT_WINDOW_H)
    hi = now - timedelta(hours=SILENT_GRACE_H)
    recent_prop_tids: set[str] = set()
    for p in props:
        ts = _parse_ts(p)
        if ts is None or not (lo <= ts <= hi):
            continue
        strat = p.get("strategy_name") or p.get("strategy")
        if elig is not None and strat not in elig:
            continue
        recent_prop_tids.add(_tid(p))
    all_dec_tids = {d.get("trade_id") for d in decs}
    never_evaluated = {t for t in recent_prop_tids if t not in all_dec_tids}
    if (len(recent_prop_tids) >= SILENT_MIN_PROPOSALS
            and len(never_evaluated) == len(recent_prop_tids)):
        problems.append(
            f"AI agent not running: {len(recent_prop_tids)} distinct "
            f"agent-eligible signal(s) were logged as proposals in the last "
            f"{SILENT_WINDOW_H}h and NOT ONE received a shadow decision. "
            f"data/ai_trade_proposals.jsonl is growing, "
            f"data/ai_shadow_decisions.jsonl is not -- the agent hook is off "
            f"(config agent_enabled?) or throwing every call (check the scan "
            f"log for '[ai] advisory hook failed').")

    # corroborating env hint -- only when something else already fired
    if problems and cfg.get("agent_enabled") and not os.environ.get("ANTHROPIC_API_KEY"):
        problems.append(
            "Note: ANTHROPIC_API_KEY is not set in this process's environment. "
            "If the scheduled scans share it, that is the likely root cause -- "
            "set it (User scope) and reboot so Task Scheduler picks it up.")

    return problems


def heartbeat_html() -> tuple[str, str]:
    """(subject, html_body) -- a POSITIVE 'the AI bot is up and green'
    status (or the problems if not). For `--email`, schedulable as often
    as wanted; the watchdog still only emails on problems."""
    problems = check()
    now = datetime.now(timezone.utc)
    decs, props = _load_jsonl(DECISIONS), _load_jsonl(PROPOSALS)

    def _within(rows, hrs):
        cut = now - timedelta(hours=hrs)
        return [r for r in rows if (_parse_ts(r) or datetime.min.replace(tzinfo=timezone.utc)) >= cut]

    d24, p24 = _within(decs, 24), _within(props, 24)
    d7 = _within(decs, 24 * 7)
    ok7 = sum(1 for d in d7 if _is_ok(d))
    last_dec = max((_parse_ts(d) for d in decs if _parse_ts(d)), default=None)
    last_ago = f"{(now - last_dec).total_seconds() / 3600:.1f}h ago" if last_dec else "never"
    acts = Counter((d.get("agent_action") or d.get("action") or "-") for d in d7)

    if problems:
        subj = f"[AI] shadow study — {len(problems)} problem(s)"
        rows = "".join(f"<li>{p}</li>" for p in problems)
        head = f"<h2 style='color:#c0392b'>&#9679; AI shadow study NOT healthy</h2><ul>{rows}</ul>"
    else:
        subj = "[AI] shadow study — healthy ✓"
        head = ("<h2 style='color:#1e8449'>&#9679; AI shadow study HEALTHY</h2>"
                "<p>The AI bot is up, scoring signals and logging decisions.</p>")
    body = f"""<!DOCTYPE html><html><body style="font-family:sans-serif;color:#222">
    {head}
    <table cellpadding="6" style="border-collapse:collapse;margin-top:8px">
      <tr><td>Decisions (24h)</td><td><b>{len(d24)}</b></td></tr>
      <tr><td>Proposals (24h)</td><td><b>{len(p24)}</b></td></tr>
      <tr><td>LLM ok (7d)</td><td><b>{ok7}/{len(d7)}</b></td></tr>
      <tr><td>Total decisions logged</td><td><b>{len(decs)}</b></td></tr>
      <tr><td>Last decision</td><td><b>{last_ago}</b></td></tr>
      <tr><td>7d verdicts</td><td><b>{', '.join(f'{k} {v}' for k, v in acts.most_common()) or '-'}</b></td></tr>
    </table>
    <p style="color:#888;font-size:12px">ai_shadow_health.py --email · the watchdog still alerts separately on problems only.</p>
    </body></html>"""
    return subj, body


def send_heartbeat() -> bool:
    try:
        from forex.notifier import _send
        subj, html = heartbeat_html()
        return bool(_send(subj, html))
    except Exception as exc:
        print(f"ai_shadow_health: heartbeat email failed: {exc}")
        return False


def main() -> int:
    if "--email" in sys.argv:
        ok = send_heartbeat()
        print("heartbeat email sent" if ok else "heartbeat email FAILED")
        return 0 if ok else 1
    problems = check()
    if not problems:
        print("ai_shadow_health: OK -- shadow study pipeline looks healthy "
              "(or the study is switched off).")
        return 0
    print(f"ai_shadow_health: {len(problems)} problem(s)\n")
    for p in problems:
        print(f"  - {p}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
