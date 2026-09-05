"""
ai/agent/strategy_evolver.py — AI-owned strategy parameter + code evolver.
---------------------------------------------------------------------------
Phase 1 (stocks — params): reads strategy SOURCE CODE + AI SIM trade history,
calls Claude to propose bounded JSON parameter adjustments, writes to
atos/ai_variants/. Picked up by the ai_sim run path.

Phase 2 (forex — code): reads forex strategy source + SIM closed-trade record
+ AI copilot shadow decisions, calls Claude to write a Python wrapper module,
AST-validates it, and writes to forex/ai_variants/strategy_<name>_override.py.
The forex --account ai_sim runner imports the override instead of the original;
main forex/strategy_*.py is NEVER modified.

Phase 2 gate: >=50 closed SIM trades per strategy. Below that the evolver
produces a descriptive analysis only (no code written).

GOVERNANCE
  * Main codebases (atos/ and forex/strategy_*.py) are NEVER modified.
  * Every proposal logged to data/ai_strategy_evolution.jsonl (full reasoning).
  * AST security check on every forex override before it is saved.
  * LIVE accounts never use forex overrides (runner gate in forex/runner.py).

Usage:
    python -m ai.agent.strategy_evolver               # evolve all (stocks + forex)
    python -m ai.agent.strategy_evolver --dry-run     # print, don't write
    python -m ai.agent.strategy_evolver --strategy us_blend
    python -m ai.agent.strategy_evolver --strategy donchian
    python -m ai.agent.strategy_evolver --forex-only  # forex strategies only
    python -m ai.agent.strategy_evolver --stocks-only # stocks strategies only
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BASE)

VARIANTS_DIR = os.path.join(BASE, "atos", "ai_variants")
AI_STOCKS_DB = os.path.join(BASE, "data", "atos_ai.db")
EVOLUTION_LOG = os.path.join(BASE, "data", "ai_strategy_evolution.jsonl")

# Source files fed to the AI as context
_STRATEGY_SOURCES = {
    "us_blend": os.path.join(BASE, "atos", "us_momentum.py"),
    "us_reversion": os.path.join(BASE, "atos", "us_reversion.py"),
}
_VARIANT_FILES = {
    "us_blend": os.path.join(VARIANTS_DIR, "us_blend_params.json"),
    "us_reversion": os.path.join(VARIANTS_DIR, "us_reversion_params.json"),
}

_BOUNDS = {
    "us_blend": {
        "LOOKBACK":      (60,   180),
        "MOM_THRESHOLD": (0.02, 0.15),
        "TARGET_VOL":    (0.10, 0.25),
        "REBAL_DAYS":    (7,    30),
    },
    "us_reversion": {
        "RSI_ENTRY":     (25,  45),
        "RSI_EXIT":      (55,  75),
        "DIP_PCT":       (0.02, 0.10),
        "VOL_MULT":      (1.0,  3.0),
        "MAX_HOLD_DAYS": (5,   20),
    },
}

_SYSTEM = """You are the ATOS AI Strategy Evolver — an autonomous quant researcher.
You read the source code of ATOS trading strategies and their forward SIM trade history,
then propose bounded parameter adjustments to improve the AI paper-twin's performance.

KEY RULES
1. You are working on an ISOLATED SIM paper account (atos_ai.db). Changes NEVER affect
   the live deterministic system. The main code (atos/) is never modified.
2. Propose only parameters listed in the bounds table. Stay within the bounds.
3. Make conservative, evidence-based changes — one parameter at a time if the trade
   sample is small (<20 closed trades). More aggressive tuning only with >50 closed trades.
4. If the current performance is already good (WR > 55%, PF > 1.2) keep most params
   unchanged — return the current values unless there is a clear improvement case.
5. Always provide a brief rationale for each proposed change.

OUTPUT FORMAT — respond ONLY with a JSON object, no markdown fences:
{
  "strategy": "<us_blend|us_reversion>",
  "params": {
    "PARAM_NAME": <value>,
    ...
  },
  "rationale": "<1-3 sentences explaining the proposal>",
  "confidence": "<low|medium|high>",
  "sample_note": "<brief comment on data quality / sample size>"
}
"""


def _read_source(strategy: str) -> str:
    path = _STRATEGY_SOURCES.get(strategy, "")
    if not os.path.exists(path):
        return f"(source not found: {path})"
    with open(path, encoding="utf-8") as f:
        return f.read()


def _read_current_params(strategy: str) -> dict:
    path = _VARIANT_FILES.get(strategy, "")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _fetch_trade_history(strategy_name: str) -> dict:
    """Return closed-trade summary from atos_ai.db for the given strategy."""
    label = "US Blend" if strategy_name == "us_blend" else "US Reversion"
    if not os.path.exists(AI_STOCKS_DB):
        return {"error": "atos_ai.db not found", "label": label}
    try:
        con = sqlite3.connect(AI_STOCKS_DB)
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute(
            "SELECT ticker, entry_date, exit_date, entry_price, exit_price, "
            "pnl_sek, exit_reason, shares FROM trades "
            "WHERE strategy=? AND exit_price IS NOT NULL "
            "ORDER BY exit_date DESC LIMIT 100", (label,)
        ).fetchall()]
        open_n = con.execute(
            "SELECT COUNT(*) FROM trades WHERE strategy=? AND exit_price IS NULL",
            (label,)).fetchone()[0]
        con.close()
    except Exception as e:
        return {"error": str(e), "label": label}

    if not rows:
        return {"label": label, "closed": 0, "open": open_n, "trades": []}

    pnls = [r["pnl_sek"] or 0 for r in rows]
    wins = sum(1 for p in pnls if p > 0)
    gp = sum(p for p in pnls if p > 0)
    gl = -sum(p for p in pnls if p < 0)
    exit_counts: dict = {}
    for r in rows:
        k = r["exit_reason"] or "unknown"
        exit_counts[k] = exit_counts.get(k, 0) + 1

    return {
        "label": label,
        "closed": len(rows),
        "open": open_n,
        "win_rate_pct": round(wins / len(rows) * 100, 1),
        "profit_factor": round(gp / gl, 2) if gl else None,
        "total_pnl_sek": round(sum(pnls), 0),
        "avg_pnl_sek": round(sum(pnls) / len(rows), 1),
        "exit_reasons": exit_counts,
        "recent_trades": rows[:20],  # last 20 for pattern inspection
    }


def _validate_params(strategy: str, raw: dict) -> dict:
    """Keep only params within their allowed range."""
    bounds = _BOUNDS.get(strategy, {})
    out: dict = {}
    for k, v in raw.items():
        if k not in bounds:
            continue
        lo, hi = bounds[k]
        try:
            v = float(v) if not isinstance(v, (int, float)) else v
            if lo <= v <= hi:
                out[k] = v
            else:
                print(f"  [evolver] {k}={v} out of bounds [{lo}, {hi}] — dropped")
        except (TypeError, ValueError):
            print(f"  [evolver] {k}={v} not numeric — dropped")
    return out


def _call_claude_stocks(prompt_user: str) -> dict | None:
    """Call Claude with the stocks Phase 1 system prompt."""
    return _call_claude(prompt_user, system=_SYSTEM)


def _call_claude_unused_ref(prompt_user: str) -> dict | None:
    """Original single-system version — kept for git history only, not called."""
    try:
        import anthropic
    except ImportError:
        print("  [evolver] anthropic SDK not installed")
        return None
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("  [evolver] ANTHROPIC_API_KEY not set")
        return None
    client = anthropic.Anthropic(api_key=api_key)
    t0 = time.monotonic()
    try:
        msg = client.messages.create(
            model="claude-sonnet-5", max_tokens=1024, system=_SYSTEM,
            messages=[{"role": "user", "content": prompt_user}], timeout=60.0,
        )
        latency_ms = round((time.monotonic() - t0) * 1000)
        text = next((b.text.strip() for b in (msg.content or []) if hasattr(b, "text")), "")
        parsed = json.loads(text)
        parsed["_meta"] = {"ok": True, "latency_ms": latency_ms, "model": "claude-sonnet-5"}
        return parsed
    except json.JSONDecodeError as e:
        print(f"  [evolver] JSON parse error: {e}\n  raw: {text[:300]}")
        return None
    except Exception as e:
        print(f"  [evolver] Claude call failed: {e}")
        return None


def evolve_strategy(strategy: str, dry_run: bool = False) -> dict | None:
    """Run the evolver for one strategy. Returns the accepted proposal or None."""
    print(f"\n[evolver] -- {strategy.upper()} ----------------------------")

    source = _read_source(strategy)
    current_params = _read_current_params(strategy)
    history = _fetch_trade_history(strategy)
    bounds = _BOUNDS.get(strategy, {})

    prompt = f"""Strategy: {strategy}
Source code:
---
{source[:6000]}
---

Current AI variant params (empty = using deterministic defaults):
{json.dumps(current_params, indent=2) if current_params else "(none — deterministic defaults in effect)"}

Parameter bounds you must stay within:
{json.dumps({k: {"min": lo, "max": hi} for k, (lo, hi) in bounds.items()}, indent=2)}

Trade history from the AI SIM paper twin (atos_ai.db):
{json.dumps(history, indent=2, default=str)}

Based on the strategy code, the trade history, and current params, propose the best
parameter set for the AI twin. Return ONLY the JSON object described in your instructions.
"""

    result = _call_claude(prompt)
    if result is None:
        print(f"  [evolver] no response — {strategy} unchanged")
        return None

    proposed_raw = result.get("params", {})
    proposed = _validate_params(strategy, proposed_raw)
    rationale = result.get("rationale", "")
    confidence = result.get("confidence", "unknown")
    sample_note = result.get("sample_note", "")
    meta = result.get("_meta", {})

    print(f"  proposed ({confidence}): {proposed}")
    print(f"  rationale: {rationale}")
    print(f"  sample_note: {sample_note}")

    log_entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "strategy": strategy,
        "proposed": proposed,
        "previous": current_params,
        "rationale": rationale,
        "confidence": confidence,
        "sample_note": sample_note,
        "trade_summary": {k: v for k, v in history.items()
                          if k not in ("recent_trades",)},
        "dry_run": dry_run,
        "meta": meta,
    }

    if not dry_run and proposed:
        os.makedirs(VARIANTS_DIR, exist_ok=True)
        out_path = _VARIANT_FILES[strategy]
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(proposed, f, indent=2)
        print(f"  written → {out_path}")
    elif dry_run:
        print("  [DRY-RUN] not written")
    else:
        print("  no valid params proposed — file unchanged")

    with open(EVOLUTION_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(log_entry) + "\n")

    return log_entry


_DEFAULTS = {
    "us_blend":     {"LOOKBACK": 120, "MOM_THRESHOLD": 0.05, "TARGET_VOL": 0.15, "REBAL_DAYS": 14},
    "us_reversion": {"RSI_ENTRY": 38, "RSI_EXIT": 60, "DIP_PCT": 0.05, "VOL_MULT": 1.5, "MAX_HOLD_DAYS": 10},
}


def _email_html(results: list[dict]) -> tuple[str, str]:
    """Build (subject, html) email report from a list of evolve_strategy() results."""
    now = datetime.now()
    any_changed = any(r.get("proposed") != _DEFAULTS.get(r.get("strategy"), {})
                      for r in results if r)

    subj = (f"[AI Evolver] params CHANGED — {now:%Y-%m-%d}"
            if any_changed else
            f"[AI Evolver] held defaults — {now:%Y-%m-%d}")

    rows_html = ""
    for r in results:
        if not r:
            continue
        s = r.get("strategy", "?")
        defaults = _DEFAULTS.get(s, {})
        proposed = r.get("proposed", {})
        hist = r.get("trade_summary", {})
        changed = proposed != defaults

        color = "#1e6b2e" if not changed else "#7d4e00"
        badge = ("&#9679; held defaults" if not changed else "&#9650; PARAMS CHANGED")
        badge_color = "#27ae60" if not changed else "#e67e22"

        param_rows = "".join(
            f"<tr><td style='padding:2px 8px'>{k}</td>"
            f"<td style='padding:2px 8px;font-weight:bold;"
            f"color:{'#e67e22' if proposed.get(k) != defaults.get(k) else '#333'}'>"
            f"{proposed.get(k, '—')}</td>"
            f"<td style='padding:2px 8px;color:#888'>{defaults.get(k, '—')}</td></tr>"
            for k in sorted(set(list(defaults) + list(proposed)))
        )

        rows_html += f"""
        <h3 style='margin-bottom:4px;color:{color}'>{s.replace('_',' ').title()}
          <span style='font-size:12px;background:{badge_color};color:#fff;
            padding:2px 6px;border-radius:3px;margin-left:8px'>{badge}</span>
        </h3>
        <table style='border-collapse:collapse;margin-bottom:4px;font-size:13px'>
          <tr style='color:#888'><th align='left' style='padding:2px 8px'>Param</th>
            <th align='left' style='padding:2px 8px'>AI twin</th>
            <th align='left' style='padding:2px 8px'>Default</th></tr>
          {param_rows}
        </table>
        <p style='margin:4px 0;font-size:13px'>
          <b>Closed:</b> {hist.get('closed', 0)} &nbsp;
          <b>WR:</b> {hist.get('win_rate_pct', '—')}% &nbsp;
          <b>PF:</b> {hist.get('profit_factor', '—')} &nbsp;
          <b>P&L:</b> {hist.get('total_pnl_sek', '—')} SEK
        </p>
        <p style='margin:4px 0;font-size:13px;color:#444'>
          <b>Rationale:</b> {r.get('rationale', '—')}
        </p>
        <p style='margin:4px 0 12px;font-size:12px;color:#888'>
          Confidence: {r.get('confidence', '?')} &nbsp;·&nbsp; {r.get('sample_note', '')}
        </p>
        <hr style='border:none;border-top:1px solid #eee'>
        """

    html = f"""<!DOCTYPE html><html><body style='font-family:sans-serif;color:#222;max-width:640px'>
    <h2>AI Strategy Evolver — weekly report</h2>
    <p style='color:#888;font-size:12px'>{now:%Y-%m-%d %H:%M} PKT &nbsp;·&nbsp;
       run_ai_strategy_evolver.bat &nbsp;·&nbsp; atos/ai_variants/</p>
    {rows_html}
    <p style='font-size:11px;color:#aaa'>Re-run manually: python -m ai.agent.strategy_evolver</p>
    </body></html>"""
    return subj, html


def send_email_report(results: list[dict]) -> bool:
    try:
        from forex.notifier import _send
        subj, html = _email_html(results)
        ok = bool(_send(subj, html))
        print(f"[evolver] email {'sent' if ok else 'FAILED'}: {subj}")
        return ok
    except Exception as exc:
        print(f"[evolver] email error: {exc}")
        return False


# ── Phase 2: Forex code evolver ──────────────────────────────────────────────

_FOREX_VARIANTS_DIR = os.path.join(BASE, "forex", "ai_variants")
_FOREX_STRATEGY_SOURCES = {
    "donchian":  os.path.join(BASE, "forex", "strategy_donchian.py"),
    "pullback":  os.path.join(BASE, "forex", "strategy_pullback.py"),
    "rsi":       os.path.join(BASE, "forex", "strategy_rsi.py"),
    "ema_trend": os.path.join(BASE, "forex", "strategy_ema_trend.py"),
}
_FOREX_OVERRIDE_FILES = {
    s: os.path.join(_FOREX_VARIANTS_DIR, f"strategy_{s}_override.py")
    for s in _FOREX_STRATEGY_SOURCES
}
# Minimum closed SIM trades (with non-null realized_pnl) before writing code.
# Lowered 50->30 on 2026-09-05: legacy dedup/reconcile artifacts leave ~35
# quality rows for donchian/pullback even with 70+ total closed rows.
_FOREX_CODE_GATE = 30

_FOREX_SYSTEM = """You are the ATOS AI Strategy Evolver — Phase 2 (forex code writer).
You read a forex strategy's SOURCE CODE and its closed SIM trade record, then write
a Python WRAPPER that the ai_sim runner will use instead of the original.

RULES
1. You MUST import and call the original generate_signals() — you are WRAPPING it, not
   replacing it. Your wrapper adds pre/post filters based on the trade data.
2. Only these imports are allowed: pandas, numpy, math, ta, collections, datetime,
   functools, itertools, statistics, typing, and forex.strategy_<name> (the original).
   Any other import will cause the file to be rejected by the AST validator.
3. Forbidden calls: eval, exec, open, __import__, compile, subprocess, os, sys,
   socket, urllib, requests. The validator will reject them.
4. Your generate_signals() must accept **kwargs and pass them through to the original.
5. Be conservative: add ONE clear filter per run. The best filters come directly from
   the trade data (e.g. "wins cluster in RANGING regime, losses in TRENDING").
6. If the sample is too small (<50 closed trades) or the data shows no clear pattern,
   return the analysis but write NO CODE — output the JSON with "code": null.

OUTPUT FORMAT — respond ONLY with this JSON object, no markdown fences:
{
  "strategy": "<strategy_name>",
  "phase": 2,
  "code": "<full Python override module as a string, or null if gate not met>",
  "rationale": "<1-3 sentences explaining the filter added>",
  "confidence": "<low|medium|high>",
  "sample_note": "<comment on data quality / sample size>",
  "filter_description": "<plain-English description of the filter, for the email report>"
}

The code string must be a complete, valid Python module. Use \\n for newlines inside the JSON string.
Start the code with a comment block: # AI-WRITTEN <date> by claude-sonnet-5 / # Rationale: <one line>
"""


def _fetch_forex_trade_history(strategy_name: str) -> dict:
    """Return closed-trade summary from pnl_ledger.db for a forex strategy."""
    db_path = os.path.join(BASE, "data", "pnl_ledger.db")
    if not os.path.exists(db_path):
        return {"error": "pnl_ledger.db not found", "strategy": strategy_name}
    try:
        import sqlite3 as _sq
        con = _sq.connect(db_path)
        con.row_factory = _sq.Row
        rows = [dict(r) for r in con.execute(
            "SELECT symbol, direction, realized_pnl, exit_reason, timestamp_open, "
            "timestamp_close FROM trades "
            "WHERE strategy=? AND status='closed' AND realized_pnl IS NOT NULL "
            "AND realized_pnl != 0 ORDER BY timestamp_close DESC LIMIT 100",
            (strategy_name,)
        ).fetchall()]
        open_n = con.execute(
            "SELECT COUNT(*) FROM trades WHERE strategy=? AND status='open'",
            (strategy_name,)).fetchone()[0]
        con.close()
    except Exception as e:
        return {"error": str(e), "strategy": strategy_name}

    if not rows:
        return {"strategy": strategy_name, "closed": 0, "open": open_n, "trades": []}

    pnls = [r["realized_pnl"] for r in rows]
    wins = sum(1 for p in pnls if p > 0)
    gp = sum(p for p in pnls if p > 0)
    gl = -sum(p for p in pnls if p < 0)
    exit_counts: dict = {}
    for r in rows:
        k = r["exit_reason"] or "unknown"
        exit_counts[k] = exit_counts.get(k, 0) + 1

    return {
        "strategy": strategy_name,
        "closed": len(rows),
        "open": open_n,
        "win_rate_pct": round(wins / len(rows) * 100, 1),
        "profit_factor": round(gp / gl, 2) if gl else None,
        "total_pnl_eur": round(sum(pnls), 0),
        "avg_pnl_eur": round(sum(pnls) / len(rows), 1),
        "exit_reasons": exit_counts,
        "recent_trades": rows[:30],
    }


def _fetch_copilot_shadow(strategy_name: str) -> dict:
    """Return copilot verdict breakdown from ai_shadow_decisions.jsonl."""
    log = os.path.join(BASE, "data", "ai_shadow_decisions.jsonl")
    if not os.path.exists(log):
        return {"strategy": strategy_name, "decisions": 0}
    verdicts: dict = {}
    regime_wins: dict = {}
    try:
        with open(log, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                    if r.get("strategy") != strategy_name:
                        continue
                    v = r.get("agent_action", "?")
                    verdicts[v] = verdicts.get(v, 0) + 1
                    reg = r.get("regime", "?")
                    regime_wins.setdefault(reg, {"n": 0})
                    regime_wins[reg]["n"] += 1
                except Exception:
                    pass
    except Exception:
        pass
    return {
        "strategy": strategy_name,
        "total_decisions": sum(verdicts.values()),
        "verdicts": verdicts,
        "by_regime": regime_wins,
    }


def _ast_validate_override(code: str) -> str | None:
    """Return error string if code is unsafe, None if OK."""
    import ast as _ast
    _ALLOWED = {"__future__", "pandas", "numpy", "math", "ta", "collections", "datetime",
                "functools", "itertools", "statistics", "typing", "forex"}
    _FORBIDDEN = {"eval", "exec", "open", "compile", "__import__",
                  "subprocess", "socket", "urllib", "requests"}
    try:
        tree = _ast.parse(code)
    except SyntaxError as e:
        return f"SyntaxError: {e}"
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top not in _ALLOWED:
                    return f"disallowed import: {alias.name}"
        if isinstance(node, _ast.ImportFrom):
            if node.module:
                top = node.module.split(".")[0]
                if top not in _ALLOWED:
                    return f"disallowed import from: {node.module}"
        if isinstance(node, _ast.Call):
            func = node.func
            name = (func.id if isinstance(func, _ast.Name) else
                    func.attr if isinstance(func, _ast.Attribute) else None)
            if name and name in _FORBIDDEN:
                return f"disallowed call: {name}"
    top_names = {n.name for n in _ast.walk(tree) if isinstance(n, _ast.FunctionDef)}
    if "generate_signals" not in top_names and "should_exit" not in top_names:
        return "must define generate_signals() or should_exit()"
    return None


def evolve_forex_strategy(strategy: str, dry_run: bool = False) -> dict | None:
    """Run the Phase 2 evolver for one forex strategy."""
    print(f"\n[evolver:forex] -- {strategy.upper()} --------------------------")

    source_path = _FOREX_STRATEGY_SOURCES.get(strategy, "")
    if not os.path.exists(source_path):
        print(f"  source not found: {source_path}")
        return None

    with open(source_path, encoding="utf-8") as f:
        source = f.read()

    history = _fetch_forex_trade_history(strategy)
    shadow  = _fetch_copilot_shadow(strategy)
    closed  = history.get("closed", 0)

    # Check if existing override is present
    existing_override = ""
    ov_path = _FOREX_OVERRIDE_FILES.get(strategy, "")
    if ov_path and os.path.exists(ov_path):
        with open(ov_path, encoding="utf-8") as f:
            existing_override = f.read()

    gate_met = closed >= _FOREX_CODE_GATE
    print(f"  closed trades: {closed}  (gate={_FOREX_CODE_GATE}, gate_met={gate_met})")
    print(f"  copilot decisions: {shadow.get('total_decisions',0)}  verdicts: {shadow.get('verdicts',{})}")

    prompt = f"""Strategy: {strategy} (forex, SIM paper research track)

=== STRATEGY SOURCE CODE ===
{source[:6000]}

=== EXISTING AI OVERRIDE (if any) ===
{existing_override[:2000] if existing_override else "(none — original strategy in use)"}

=== CLOSED TRADE HISTORY (from pnl_ledger.db, SIM paper) ===
{json.dumps(history, indent=2, default=str)}

=== AI COPILOT SHADOW DECISIONS (verdict breakdown) ===
{json.dumps(shadow, indent=2, default=str)}

=== PHASE 2 CODE GATE ===
Minimum closed trades required to write code: {_FOREX_CODE_GATE}
Actual closed trades: {closed}
Gate met: {gate_met}

{"GATE MET: You MAY write a Python wrapper override. Add ONE clear filter backed by the trade data." if gate_met else
 "GATE NOT YET MET: Do NOT write code. Analyse the strategy and shadow data, propose what filter you would add once enough trades accumulate, and set code: null."}

Return the JSON object described in your instructions.
"""

    result = _call_claude(prompt, system=_FOREX_SYSTEM)
    if result is None:
        print(f"  [evolver:forex] no response — {strategy} unchanged")
        return None

    code        = result.get("code")
    rationale   = result.get("rationale", "")
    confidence  = result.get("confidence", "unknown")
    sample_note = result.get("sample_note", "")
    filter_desc = result.get("filter_description", "")
    meta        = result.get("_meta", {})

    print(f"  confidence: {confidence}")
    print(f"  rationale: {rationale}")
    print(f"  filter: {filter_desc}")
    print(f"  code: {'<{} chars>'.format(len(code)) if code else 'null (gate not met or no clear pattern)'}")

    log_entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "module": "forex",
        "phase": 2,
        "strategy": strategy,
        "code_written": bool(code and not dry_run and gate_met),
        "rationale": rationale,
        "confidence": confidence,
        "sample_note": sample_note,
        "filter_description": filter_desc,
        "trade_summary": {k: v for k, v in history.items() if k != "recent_trades"},
        "shadow_summary": shadow,
        "dry_run": dry_run,
        "meta": meta,
    }

    if code and gate_met:
        # Validate before writing
        err = _ast_validate_override(code)
        if err:
            print(f"  AST validation FAILED: {err} — not written")
            log_entry["ast_error"] = err
        elif not dry_run:
            os.makedirs(_FOREX_VARIANTS_DIR, exist_ok=True)
            with open(ov_path, "w", encoding="utf-8") as f:
                f.write(code)
            print(f"  written → {ov_path}")
            log_entry["code_written"] = True
        else:
            print("  [DRY-RUN] override not written")
            print(f"  --- proposed code ---\n{code[:800]}\n  ---")
    else:
        if not gate_met:
            print(f"  gate not met ({closed}/{_FOREX_CODE_GATE} trades) — analysis only, no code written")

    with open(EVOLUTION_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(log_entry) + "\n")

    return log_entry


# ── Phase 3: Forex exit logic evolver ────────────────────────────────────────

_FOREX_EXIT_SYSTEM = """You are the ATOS AI Strategy Evolver -- Phase 3 (forex exit logic).
You read a forex strategy's SOURCE CODE, its closed SIM trade record with exit_reason
breakdown, and the EXISTING Phase 2 entry-filter override module. You produce an UPDATED
override module that ALSO wraps should_exit() with improved exit logic.

RULES
1. Copy the existing generate_signals() wrapper VERBATIM. Do not change it.
2. Wrap should_exit() by importing the original and adding pre/post logic.
   Import pattern: from forex.strategy_<name> import should_exit as _orig_should_exit
   Signature you must match: (position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple
   Must return (bool, str). Call _orig_should_exit first; you may override its decision.
3. Only these imports are allowed: __future__, pandas, numpy, math, ta, collections, datetime,
   functools, itertools, statistics, typing, forex. Any other import will be rejected.
   Do NOT use "from __future__ import annotations" or any other __future__ import unless
   strictly necessary — prefer explicit type annotations without it.
4. Forbidden calls: eval, exec, open, __import__, compile, subprocess, os, sys,
   socket, urllib, requests.
5. The position dict contains at minimum: direction, stop_price, entry_price, symbol.
   df has columns: Open, High, Low, Close (daily bars). calendar_days_held is an int.
6. Add ONE clear exit improvement backed directly by the exit_reason data.
   Good candidates:
   - Confirmation bars: require 2 consecutive closes past the exit trigger (not just one)
   - Breakeven trail: if unrealized_profit > 0.5 * ATR, suggest stop move (return False
     with a note -- stop-moving is the runner's job; you can only block an early exit)
   - Skip exit in clearly wrong regime (e.g. trend_break in RANGING)
7. If the exit data shows no clear pattern, output the existing code UNCHANGED and set
   exit_rationale explaining why (code will still be written to preserve Phase 2).

OUTPUT FORMAT -- respond ONLY with this JSON object, no markdown fences:
{
  "strategy": "<strategy_name>",
  "phase": 3,
  "code": "<full Python module with BOTH generate_signals() AND should_exit() wrappers>",
  "exit_rationale": "<1-3 sentences: what exit rule was added and why>",
  "confidence": "<low|medium|high>",
  "sample_note": "<comment on exit data quality>",
  "exit_description": "<plain-English description of the exit rule, for email report>"
}

The code must be a complete valid Python module. Use \\n for newlines in the JSON string.
Start with:
# AI-WRITTEN Phase 2+3 <date> by claude-sonnet-5
# Entry filter: <one line from Phase 2>
# Exit filter: <one line from Phase 3>
"""


def _fetch_exit_breakdown(strategy_name: str) -> dict:
    """Return exit_reason x wins/losses breakdown from pnl_ledger.db."""
    db_path = os.path.join(BASE, "data", "pnl_ledger.db")
    if not os.path.exists(db_path):
        return {"error": "pnl_ledger.db not found"}
    try:
        import sqlite3 as _sq
        con = _sq.connect(db_path)
        rows = con.execute(
            "SELECT exit_reason, realized_pnl FROM trades "
            "WHERE strategy=? AND status='closed' AND realized_pnl IS NOT NULL AND realized_pnl != 0",
            (strategy_name,)
        ).fetchall()
        con.close()
    except Exception as e:
        return {"error": str(e)}

    buckets: dict = {}
    for reason, pnl in rows:
        key = (reason or "unknown").split("(")[0].strip()  # normalize e.g. "hard_stop (1.35)"
        b = buckets.setdefault(key, {"n": 0, "wins": 0, "total_pnl": 0.0})
        b["n"] += 1
        b["total_pnl"] = round(b["total_pnl"] + pnl, 2)
        if pnl > 0:
            b["wins"] += 1

    for b in buckets.values():
        b["losses"] = b["n"] - b["wins"]
        b["win_rate_pct"] = round(b["wins"] / b["n"] * 100, 1) if b["n"] else 0
        b["avg_pnl"] = round(b["total_pnl"] / b["n"], 1) if b["n"] else 0

    return {"strategy": strategy_name, "total_quality_trades": len(rows), "by_exit_reason": buckets}


def evolve_forex_exit(strategy: str, dry_run: bool = False) -> dict | None:
    """Run Phase 3: add should_exit() wrapper to an existing forex override."""
    print(f"\n[evolver:forex:exit] -- {strategy.upper()} ----------------------")

    source_path = _FOREX_STRATEGY_SOURCES.get(strategy, "")
    ov_path = _FOREX_OVERRIDE_FILES.get(strategy, "")

    if not os.path.exists(source_path):
        print(f"  source not found: {source_path}")
        return None

    with open(source_path, encoding="utf-8") as f:
        source = f.read()
    existing_override = ""
    if ov_path and os.path.exists(ov_path):
        with open(ov_path, encoding="utf-8") as f:
            existing_override = f.read()

    exit_data = _fetch_exit_breakdown(strategy)
    total = exit_data.get("total_quality_trades", 0)
    print(f"  quality closed trades: {total}")
    print(f"  exit breakdown: {exit_data.get('by_exit_reason', {})}")

    has_phase2 = bool(existing_override)
    prompt = f"""Strategy: {strategy} (forex, SIM paper research track)

=== STRATEGY SOURCE CODE ===
{source[:5000]}

=== EXISTING PHASE 2 ENTRY OVERRIDE ===
{existing_override if has_phase2 else "(none -- no entry filter exists yet for this strategy)"}

=== EXIT REASON BREAKDOWN (from pnl_ledger.db) ===
{json.dumps(exit_data, indent=2, default=str)}

=== TASK ===
Total quality closed trades: {total}
{"Preserve the existing generate_signals() wrapper verbatim and ADD a should_exit() wrapper." if has_phase2 else
 "No Phase 2 entry filter exists yet. Write a module with: (1) a pass-through generate_signals() that calls the original unchanged, and (2) a should_exit() wrapper with the exit improvement."}
Focus on exit_reason types with high loss counts and low/zero win rates.

Return the JSON object described in your instructions.
"""

    result = _call_claude(prompt, system=_FOREX_EXIT_SYSTEM)
    if result is None:
        print(f"  [evolver:forex:exit] no response -- {strategy} unchanged")
        return None

    code        = result.get("code")
    rationale   = result.get("exit_rationale", "")
    confidence  = result.get("confidence", "unknown")
    sample_note = result.get("sample_note", "")
    exit_desc   = result.get("exit_description", "")
    meta        = result.get("_meta", {})

    print(f"  confidence: {confidence}")
    print(f"  exit_rationale: {rationale}")
    print(f"  exit_description: {exit_desc}")
    print(f"  code: {'<{} chars>'.format(len(code)) if code else 'null'}")

    log_entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "module": "forex",
        "phase": 3,
        "strategy": strategy,
        "code_written": False,
        "exit_rationale": rationale,
        "confidence": confidence,
        "sample_note": sample_note,
        "exit_description": exit_desc,
        "exit_breakdown": exit_data,
        "dry_run": dry_run,
        "meta": meta,
    }

    if code:
        err = _ast_validate_override(code)
        if err:
            print(f"  AST validation FAILED: {err} -- not written")
            log_entry["ast_error"] = err
        elif not dry_run:
            with open(ov_path, "w", encoding="utf-8") as f:
                f.write(code)
            print(f"  written -> {ov_path}")
            log_entry["code_written"] = True
        else:
            print("  [DRY-RUN] override not written")
            print(f"  --- proposed code (first 800 chars) ---\n{code[:800]}\n  ---")
    else:
        print("  no code returned")

    with open(EVOLUTION_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(log_entry) + "\n")

    return log_entry


def _call_claude(prompt_user: str, system: str = _SYSTEM) -> dict | None:
    """Call Claude API with a given system prompt. Returns parsed JSON or None."""
    try:
        import anthropic
    except ImportError:
        print("  [evolver] anthropic SDK not installed")
        return None

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("  [evolver] ANTHROPIC_API_KEY not set")
        return None

    client = anthropic.Anthropic(api_key=api_key)
    t0 = time.monotonic()
    try:
        msg = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=8192,
            system=system,
            messages=[{"role": "user", "content": prompt_user}],
            timeout=180.0,
        )
        latency_ms = round((time.monotonic() - t0) * 1000)
        # claude-sonnet-5 may return ThinkingBlock + TextBlock — find the text
        text = ""
        _block_types = [type(b).__name__ for b in (msg.content or [])]
        for block in (msg.content or []):
            if hasattr(block, "text"):
                text = block.text.strip()
                break
        # Strip markdown fences if model wrapped the JSON (```json ... ```)
        if text.startswith("```"):
            text = text.split("```", 2)[-1] if text.count("```") >= 2 else text
            # remove optional language tag on first line
            if text.startswith("json"):
                text = text[4:]
            text = text.rstrip("`").strip()
        parsed = json.loads(text)
        parsed["_meta"] = {"ok": True, "latency_ms": latency_ms, "model": "claude-sonnet-5"}
        return parsed
    except json.JSONDecodeError as e:
        print(f"  [evolver] JSON parse error: {e}\n  raw: {text[:300]}")
        return None
    except Exception as e:
        print(f"  [evolver] Claude call failed: {e}")
        return None


_FOREX_STRATEGIES = list(_FOREX_STRATEGY_SOURCES.keys())   # ["donchian", "pullback"]


def main(argv=None):
    ap = argparse.ArgumentParser(description="AI strategy param + code evolver")
    ap.add_argument("--strategy",
                    choices=["us_blend", "us_reversion", "donchian", "pullback",
                             "rsi", "ema_trend", "all"],
                    default="all")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print proposals without writing variant files")
    ap.add_argument("--email", action="store_true",
                    help="Send email report after running (implies real run, not dry-run)")
    ap.add_argument("--forex-only", action="store_true",
                    help="Run forex Phase 2+3 evolver only")
    ap.add_argument("--stocks-only", action="store_true",
                    help="Run stocks Phase 1 evolver only")
    ap.add_argument("--exit-only", action="store_true",
                    help="Run forex Phase 3 (exit logic) only, skip Phase 2 entry evolver")
    args = ap.parse_args(argv)

    if args.email:
        args.dry_run = False

    # Determine which modules to run
    run_stocks = not args.forex_only
    run_forex  = not args.stocks_only

    # Strategy filter
    if args.strategy != "all":
        if args.strategy in _FOREX_STRATEGIES:
            run_stocks = False
        else:
            run_forex = False

    print(f"[evolver] running {datetime.now():%Y-%m-%d %H:%M:%S}  "
          f"dry_run={args.dry_run}  email={args.email}  "
          f"stocks={run_stocks}  forex={run_forex}")

    results = []

    # ── Stocks Phase 1 ────────────────────────────────────────────────────────
    if run_stocks:
        stocks_targets = (list(_BOUNDS.keys()) if args.strategy == "all"
                          else [args.strategy])
        for s in stocks_targets:
            results.append(evolve_strategy(s, dry_run=args.dry_run))

    # ── Forex Phase 2 (entry filters) ─────────────────────────────────────────
    if run_forex and not args.exit_only:
        forex_targets = (_FOREX_STRATEGIES if args.strategy == "all"
                         else [args.strategy])
        for s in forex_targets:
            results.append(evolve_forex_strategy(s, dry_run=args.dry_run))

    # ── Forex Phase 3 (exit logic) ────────────────────────────────────────────
    if run_forex:
        forex_targets = (_FOREX_STRATEGIES if args.strategy == "all"
                         else [args.strategy])
        for s in forex_targets:
            results.append(evolve_forex_exit(s, dry_run=args.dry_run))

    if args.email:
        send_email_report(results)

    print(f"\n[evolver] done. Log: {EVOLUTION_LOG}")


if __name__ == "__main__":
    main()
