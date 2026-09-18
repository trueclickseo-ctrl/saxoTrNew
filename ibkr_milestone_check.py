"""
ibkr_milestone_check.py
-----------------------
Counts valid closed trades per IBKR paper strategy (fill_price > 0 on both
sides). Every time a strategy crosses a new multiple of 20 closed trades, it:
  1. Computes full P&L stats for that strategy.
  2. Calls Claude (Haiku) for a parameter-tuning suggestion.
  3. Writes the proposal to data/ibkr_strategy_proposals.json for human review.
  4. Logs to data/ai_strategy_evolution.jsonl.
  5. Sends an email notification.

Proposals are NEVER auto-applied to ibkr_config.json — user reviews and
decides manually.

Usage:
    python ibkr_milestone_check.py            # check + trigger if milestone hit
    python ibkr_milestone_check.py --status   # print current counts only
    python ibkr_milestone_check.py --dry-run  # detect milestone but don't call Claude or email
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

_ROOT = os.path.dirname(os.path.abspath(__file__))
_DB   = os.path.join(_ROOT, "data", "ibkr_stocks.db")
_CFG  = os.path.join(_ROOT, "ibkr_module", "config", "ibkr_config.json")
_STATE_FILE     = os.path.join(_ROOT, "data", "ibkr_milestone_state.json")
_PROPOSALS_FILE = os.path.join(_ROOT, "data", "ibkr_strategy_proposals.json")
_EVOLUTION_LOG  = os.path.join(_ROOT, "data", "ai_strategy_evolution.jsonl")

_MILESTONE_STEP = 20   # trigger every N closed trades

# ── Strategy metadata ──────────────────────────────────────────────────────────

_STRATEGIES = [
    # (db_key, display_name, config_path_in_json)
    ("scorer_swing",    "Scorer Swing",     "strategies.scorer.swing"),
    ("scorer_portfolio","Scorer Portfolio",  "strategies.scorer.portfolio"),
    ("reversion",       "US Reversion",      "strategies.reversion"),
    ("blend",           "US Blend",          "strategies.blend"),
    ("US Ensemble",     "US Ensemble",       "strategies.signals"),
    ("US Momentum",     "US Momentum",       "strategies.signals"),
    ("US RSI Reversal", "US RSI Reversal",   "strategies.signals"),
    ("US SMA Crossover","US SMA Crossover",  "strategies.signals"),
]

# Tunable parameters per strategy — proposals stay within these bounds
_BOUNDS: dict[str, dict[str, tuple]] = {
    "scorer_swing": {
        "stop_pct":      (0.02, 0.08),
        "min_score":     (55.0, 80.0),
        "max_positions": (8,    20),
    },
    "scorer_portfolio": {
        "stop_pct":      (0.02, 0.08),
        "min_score":     (55.0, 80.0),
        "max_positions": (10,   25),
    },
    "reversion": {
        "stop_pct":      (0.01, 0.06),
        "rsi_entry":     (25,   45),
        "rsi_exit":      (55,   75),
        "max_hold_days": (5,    20),
    },
    "blend": {
        "stop_pct":   (0.04, 0.12),
        "rebal_days": (7,    30),
    },
    "US Ensemble":      {"hard_stop_pct": (0.02, 0.08), "max_hold_days": (15, 45)},
    "US Momentum":      {"hard_stop_pct": (0.02, 0.08), "max_hold_days": (15, 45)},
    "US RSI Reversal":  {"hard_stop_pct": (0.02, 0.08), "max_hold_days": (15, 45)},
    "US SMA Crossover": {"hard_stop_pct": (0.02, 0.08), "max_hold_days": (15, 45)},
}

_SYSTEM = """You are a quant analyst reviewing IBKR paper-trading strategy performance.
Your job: read the strategy's closed-trade history and current config, then propose
bounded parameter adjustments that may improve future performance.

RULES
1. Proposals must stay within the bounds table — never exceed them.
2. Only change one or two parameters per run. Conservative is better.
3. If WR > 55% AND PF > 1.2 AND sample >= 20, keep most params unchanged.
4. Always provide brief evidence-based rationale.
5. If sample < 20 reliable trades, set confidence to "low" and be very conservative.
6. These are proposals for HUMAN REVIEW — they are not auto-applied.

OUTPUT FORMAT — respond ONLY with this JSON object, no markdown, no fences:
{
  "strategy": "<db_key>",
  "proposed_params": {"PARAM": value, ...},
  "rationale": "<1-3 sentences>",
  "confidence": "<low|medium|high>",
  "sample_note": "<comment on data quality / sample size>"
}
"""


# ── DB helpers ─────────────────────────────────────────────────────────────────

def _get_closed_stats(db_path: str) -> dict[str, dict]:
    """Return per-strategy closed-trade stats (only valid fill_price > 0 rows)."""
    if not os.path.exists(db_path):
        return {}
    out: dict[str, dict] = {}
    try:
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        rows = con.execute("""
            SELECT s.strategy,
                   COUNT(*)  AS n,
                   SUM(CASE WHEN (s.fill_price - b.fill_price) > 0 THEN 1 ELSE 0 END) AS wins,
                   SUM((s.fill_price - b.fill_price) * s.qty)       AS net_pnl,
                   SUM(CASE WHEN (s.fill_price - b.fill_price) > 0
                       THEN (s.fill_price - b.fill_price) * s.qty ELSE 0 END) AS gross_profit,
                   SUM(CASE WHEN (s.fill_price - b.fill_price) <= 0
                       THEN ABS((s.fill_price - b.fill_price) * s.qty) ELSE 0 END) AS gross_loss,
                   AVG(JULIANDAY(s.filled_at) - JULIANDAY(b.filled_at)) AS avg_hold,
                   GROUP_CONCAT(
                       s.symbol || ':buy=' || b.fill_price ||
                       ',sell=' || s.fill_price || ',qty=' || s.qty ||
                       ',pnl=' || ROUND((s.fill_price-b.fill_price)*s.qty,2)
                   ) AS trade_detail
            FROM trades s
            JOIN trades b ON s.symbol    = b.symbol
                          AND b.side     = 'BUY'
                          AND b.status  IN ('FILLED', 'SOLD')
                          AND b.strategy = s.strategy
            WHERE s.side='SELL' AND s.status='FILLED'
              AND s.fill_price > 0 AND b.fill_price > 0
            GROUP BY s.strategy
        """).fetchall()
        con.close()
        for r in rows:
            n  = int(r["n"])
            gp = float(r["gross_profit"] or 0)
            gl = float(r["gross_loss"] or 0)
            wins = int(r["wins"])
            out[str(r["strategy"])] = {
                "closed":       n,
                "wins":         wins,
                "losses":       n - wins,
                "win_rate_pct": round(wins / n * 100, 1) if n else 0,
                "profit_factor": round(gp / gl, 2) if gl else None,
                "net_pnl_usd":  round(float(r["net_pnl"] or 0), 2),
                "avg_hold_days": round(float(r["avg_hold"] or 0), 1),
                "trade_detail": str(r["trade_detail"] or ""),
            }
    except Exception as e:
        print(f"[milestone] DB error: {e}")
    return out


# ── State management ───────────────────────────────────────────────────────────

def _load_state() -> dict:
    if os.path.exists(_STATE_FILE):
        try:
            with open(_STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_state(state: dict) -> None:
    with open(_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


# ── Config reader ──────────────────────────────────────────────────────────────

def _read_config_section(db_key: str) -> dict:
    """Return the relevant config section for this strategy."""
    try:
        with open(_CFG, encoding="utf-8") as f:
            cfg = json.load(f)
        strats = cfg.get("strategies", {})
        if db_key in ("scorer_swing", "scorer_portfolio"):
            subkey = db_key.replace("scorer_", "")
            return strats.get("scorer", {}).get(subkey, {})
        return strats.get(db_key, {})
    except Exception:
        return {}


# ── Claude call ────────────────────────────────────────────────────────────────

def _call_claude(prompt: str) -> dict | None:
    try:
        import anthropic
    except ImportError:
        print("[milestone] anthropic SDK not installed")
        return None
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("[milestone] ANTHROPIC_API_KEY not set")
        return None
    client = anthropic.Anthropic(api_key=api_key)
    t0 = time.monotonic()
    try:
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            timeout=60.0,
        )
        latency_ms = round((time.monotonic() - t0) * 1000)
        text = next((b.text.strip() for b in (msg.content or []) if hasattr(b, "text")), "")
        parsed = json.loads(text)
        parsed["_meta"] = {"latency_ms": latency_ms, "model": "claude-haiku-4-5-20251001"}
        return parsed
    except json.JSONDecodeError as e:
        print(f"[milestone] JSON parse error: {e}")
        return None
    except Exception as e:
        print(f"[milestone] Claude call failed: {e}")
        return None


# ── Milestone analysis ─────────────────────────────────────────────────────────

def _analyse_strategy(db_key: str, display: str, stats: dict, dry_run: bool) -> dict | None:
    """Call Claude for a parameter proposal. Returns proposal dict or None."""
    bounds   = _BOUNDS.get(db_key, {})
    cfg_now  = _read_config_section(db_key)

    prompt = f"""Strategy: {db_key} ({display})

Current config params:
{json.dumps(cfg_now, indent=2)}

Parameter bounds you must stay within:
{json.dumps({k: {"min": lo, "max": hi} for k, (lo, hi) in bounds.items()}, indent=2)}

Closed-trade stats ({stats['closed']} trades, fill_price > 0 on both sides):
  Win rate:      {stats['win_rate_pct']}%
  Profit factor: {stats['profit_factor']}
  Net P&L (USD): {stats['net_pnl_usd']:+.2f}
  Avg hold:      {stats['avg_hold_days']}d
  Wins / Losses: {stats['wins']} / {stats['losses']}

Individual trades (symbol:buy=X,sell=Y,qty=Z,pnl=P):
{stats['trade_detail']}

Based on the above, propose parameter adjustments (if any) within the bounds.
Return ONLY the JSON object.
"""

    if dry_run:
        print(f"  [milestone] [{db_key}] DRY-RUN — would call Claude here")
        return None

    return _call_claude(prompt)


# ── Email ──────────────────────────────────────────────────────────────────────

def _send_email(subject: str, html: str) -> bool:
    try:
        from forex.notifier import _send
        ok = bool(_send(subject, html))
        print(f"[milestone] email {'sent' if ok else 'FAILED'}: {subject}")
        return ok
    except Exception as e:
        print(f"[milestone] email error: {e}")
        return False


def _build_email(triggered: list[dict]) -> tuple[str, str]:
    now   = datetime.now()
    subj  = f"[IBKR] Trade milestone reached — {now:%Y-%m-%d}"
    rows  = ""
    for item in triggered:
        db_key  = item["db_key"]
        display = item["display"]
        stats   = item["stats"]
        prop    = item.get("proposal") or {}
        params  = prop.get("proposed_params", {})
        rationale = prop.get("rationale", "(no Claude response)")
        confidence = prop.get("confidence", "—")
        sample_note = prop.get("sample_note", "")

        param_rows = "".join(
            f"<tr><td style='padding:2px 8px'>{k}</td>"
            f"<td style='padding:2px 8px;font-weight:bold'>{v}</td></tr>"
            for k, v in params.items()
        ) if params else "<tr><td colspan=2 style='padding:2px 8px;color:#888'>no changes suggested</td></tr>"

        rows += f"""
        <h3 style='margin-bottom:4px'>{display}
          <span style='font-size:12px;background:#1565c0;color:#fff;
            padding:2px 6px;border-radius:3px;margin-left:8px'>
            {stats['closed']} closed trades milestone
          </span>
        </h3>
        <p style='margin:2px 0;font-size:13px'>
          <b>WR:</b> {stats['win_rate_pct']}% &nbsp;
          <b>PF:</b> {stats['profit_factor'] or '—'} &nbsp;
          <b>Net P&L:</b> ${stats['net_pnl_usd']:+.2f} &nbsp;
          <b>Avg hold:</b> {stats['avg_hold_days']}d
        </p>
        <p style='margin:2px 0;font-size:13px'><b>AI Suggestion ({confidence} confidence):</b></p>
        <table style='border-collapse:collapse;font-size:13px;margin:4px 0'>
          <tr style='color:#888'><th align='left' style='padding:2px 8px'>Param</th>
            <th align='left' style='padding:2px 8px'>Proposed value</th></tr>
          {param_rows}
        </table>
        <p style='margin:4px 0;font-size:13px;color:#444'><b>Rationale:</b> {rationale}</p>
        <p style='margin:0 0 12px;font-size:12px;color:#888'>{sample_note}</p>
        <p style='margin:0 0 16px;font-size:12px;color:#c0392b'>
          ⚠️ PROPOSALS ARE FOR REVIEW ONLY — not auto-applied to ibkr_config.json.
          Check data/ibkr_strategy_proposals.json, then apply manually if you agree.
        </p>
        <hr style='border:none;border-top:1px solid #eee'>
        """

    html = f"""<!DOCTYPE html><html><body style='font-family:sans-serif;color:#222;max-width:640px'>
    <h2>IBKR Strategy Milestone Report</h2>
    <p style='color:#888;font-size:12px'>{now:%Y-%m-%d %H:%M} PKT &nbsp;·&nbsp;
       ibkr_milestone_check.py</p>
    {rows}
    <p style='font-size:11px;color:#aaa'>
      Proposals saved to data/ibkr_strategy_proposals.json<br>
      Re-run: python ibkr_milestone_check.py
    </p>
    </body></html>"""
    return subj, html


# ── Main ───────────────────────────────────────────────────────────────────────

def main(dry_run: bool = False, status_only: bool = False) -> None:
    stats_all = _get_closed_stats(_DB)
    state     = _load_state()

    print(f"[milestone] closed trade counts ({datetime.now():%Y-%m-%d %H:%M})")
    print(f"  {'Strategy':<22} {'Closed':>7} {'Next milestone':>15} {'WR%':>6} {'PF':>6} {'Net P&L':>10}")
    print("  " + "-" * 72)

    for db_key, display, _ in _STRATEGIES:
        s = stats_all.get(db_key, {})
        closed = s.get("closed", 0)
        nxt    = (closed // _MILESTONE_STEP + 1) * _MILESTONE_STEP
        wr     = f"{s['win_rate_pct']}%" if s else "--"
        pf     = str(s.get("profit_factor") or "--") if s else "--"
        pnl    = f"${s['net_pnl_usd']:+.2f}" if s else "--"
        print(f"  {display:<22} {closed:>7} {nxt:>15} {wr:>6} {pf:>6} {pnl:>10}")

    if status_only:
        return

    # ── Detect milestones ──────────────────────────────────────────────────────
    triggered: list[dict] = []
    proposals_all: dict   = {}

    try:
        existing_proposals = json.load(open(_PROPOSALS_FILE, encoding="utf-8")) \
            if os.path.exists(_PROPOSALS_FILE) else {}
    except Exception:
        existing_proposals = {}

    for db_key, display, _ in _STRATEGIES:
        s = stats_all.get(db_key, {})
        closed = s.get("closed", 0)
        if closed == 0:
            continue

        prev_milestone = state.get(db_key, {}).get("last_milestone", 0)
        curr_milestone = (closed // _MILESTONE_STEP) * _MILESTONE_STEP

        if curr_milestone <= prev_milestone:
            continue  # no new milestone crossed

        print(f"\n[milestone] {display}: {closed} closed trades → milestone {curr_milestone}!")

        proposal = _analyse_strategy(db_key, display, s, dry_run)

        if proposal:
            print(f"  proposed params: {proposal.get('proposed_params', {})}")
            print(f"  rationale: {proposal.get('rationale', '—')}")
            print(f"  confidence: {proposal.get('confidence', '—')}")
            proposals_all[db_key] = {
                "strategy":      db_key,
                "display":       display,
                "milestone":     curr_milestone,
                "stats":         {k: v for k, v in s.items() if k != "trade_detail"},
                "proposed_params": proposal.get("proposed_params", {}),
                "rationale":     proposal.get("rationale", ""),
                "confidence":    proposal.get("confidence", ""),
                "sample_note":   proposal.get("sample_note", ""),
                "generated_at":  datetime.now(timezone.utc).isoformat(),
            }
        else:
            proposals_all[db_key] = existing_proposals.get(db_key, {})

        triggered.append({
            "db_key":   db_key,
            "display":  display,
            "stats":    s,
            "proposal": proposal,
        })

        # Log to evolution log
        log_entry = {
            "ts":       datetime.now(timezone.utc).isoformat(),
            "source":   "ibkr_milestone_check",
            "strategy": db_key,
            "milestone": curr_milestone,
            "stats":    {k: v for k, v in s.items() if k != "trade_detail"},
            "proposal": proposal,
            "dry_run":  dry_run,
        }
        try:
            with open(_EVOLUTION_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(log_entry) + "\n")
        except Exception as e:
            print(f"[milestone] log write error: {e}")

        # Update state
        if not dry_run:
            if db_key not in state:
                state[db_key] = {}
            state[db_key]["last_milestone"] = curr_milestone
            state[db_key]["checked_at"]     = datetime.now(timezone.utc).isoformat()

    # ── Save proposals + state ─────────────────────────────────────────────────
    if triggered and not dry_run:
        merged = {**existing_proposals, **proposals_all}
        with open(_PROPOSALS_FILE, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2)
        print(f"\n[milestone] proposals saved → {_PROPOSALS_FILE}")
        _save_state(state)

        subj, html = _build_email(triggered)
        _send_email(subj, html)

    if not triggered:
        print("\n[milestone] no new milestones reached")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="detect milestones but skip Claude + email")
    parser.add_argument("--status",  action="store_true", help="print current counts and exit")
    args = parser.parse_args()
    main(dry_run=args.dry_run, status_only=args.status)
