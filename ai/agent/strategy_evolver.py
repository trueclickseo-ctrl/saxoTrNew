"""
ai/agent/strategy_evolver.py — AI-owned strategy parameter evolver.
---------------------------------------------------------------------
Reads the deterministic strategy SOURCE CODE + the AI SIM trade history,
calls Claude to propose bounded parameter adjustments, validates them, and
writes them to atos/ai_variants/. These overrides are picked up by the
ai_sim run path (run_us_blend_ai, run_us_reversion account_env=ai_sim).

GOVERNANCE
  * The main codebase (atos/) is NEVER modified.
  * Evolver can only propose params within the hard bounds in atos/ai_variants/__init__.py.
  * Every proposal is logged to data/ai_strategy_evolution.jsonl with the full
    reasoning so changes are auditable.
  * Phase 2 (code modification) is listed in docs/atos_ai_tracker.md — not yet built.

Usage:
    python -m ai.agent.strategy_evolver           # evolve all strategies
    python -m ai.agent.strategy_evolver --dry-run  # print proposal, don't write
    python -m ai.agent.strategy_evolver --strategy us_blend
    python -m ai.agent.strategy_evolver --strategy us_reversion
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


def _call_claude(prompt_user: str) -> dict | None:
    """Call Claude API with the evolver system prompt. Returns parsed JSON or None."""
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
            max_tokens=1024,
            system=_SYSTEM,
            messages=[{"role": "user", "content": prompt_user}],
            timeout=60.0,
        )
        latency_ms = round((time.monotonic() - t0) * 1000)
        text = msg.content[0].text.strip() if msg.content else ""
        parsed = json.loads(text)
        parsed["_meta"] = {"ok": True, "latency_ms": latency_ms,
                           "model": "claude-sonnet-5"}
        return parsed
    except json.JSONDecodeError as e:
        print(f"  [evolver] JSON parse error: {e}\n  raw: {text[:300]}")
        return None
    except Exception as e:
        print(f"  [evolver] Claude call failed: {e}")
        return None


def evolve_strategy(strategy: str, dry_run: bool = False) -> dict | None:
    """Run the evolver for one strategy. Returns the accepted proposal or None."""
    print(f"\n[evolver] ── {strategy.upper()} ─────────────────────────────")

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


def main(argv=None):
    ap = argparse.ArgumentParser(description="AI strategy param evolver")
    ap.add_argument("--strategy", choices=["us_blend", "us_reversion", "all"],
                    default="all")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print proposals without writing variant files")
    ap.add_argument("--email", action="store_true",
                    help="Send email report after running (implies real run, not dry-run)")
    args = ap.parse_args(argv)

    if args.email:
        args.dry_run = False  # email implies real run

    targets = (list(_BOUNDS.keys()) if args.strategy == "all"
               else [args.strategy])

    print(f"[evolver] running {datetime.now():%Y-%m-%d %H:%M:%S}  "
          f"dry_run={args.dry_run}  email={args.email}  strategies={targets}")

    results = []
    for s in targets:
        results.append(evolve_strategy(s, dry_run=args.dry_run))

    if args.email:
        send_email_report(results)

    print(f"\n[evolver] done. Log: {EVOLUTION_LOG}")


if __name__ == "__main__":
    main()
