"""
check_ai_gates.py — Weekly Phase B + C gate checker.

Computes the three gate metrics and sends an email + exits 0 when all pass.
Scheduled every Monday at 09:00 PKT via Windows Task Scheduler.

Exit codes:
  0 = all gates pass (email sent)
  1 = one or more gates not yet ready
  2 = data error / not enough trades

Gates checked:
  B1: net counterfactual EUR > 0  (REJECT/MODIFY would have helped)
  B2: APPROVE avg P&L > REJECT avg P&L  (agent quality)
  C:  >= 10 matched EXIT_NOW closed trades AND net exit delta > 0

Usage:
    python check_ai_gates.py [--account sim]
"""
from __future__ import annotations

import json
import os
import sys
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
DECISIONS_LOG  = os.path.join(BASE, "data", "ai_shadow_decisions.jsonl")
EXIT_LOG       = os.path.join(BASE, "data", "ai_exit_decisions.jsonl")
LEDGER_DB      = os.path.join(BASE, "data", "pnl_ledger.db")
GATE_LOG       = os.path.join(BASE, "data", "ai_gate_check.log")

_MODULE_MAP = {"sim": "forex", "ai_sim": "forex_ai",
               "live": "forex_live", "live_eur": "forex_live_eur"}

MIN_EXIT_TRADES = 10
MIN_B_TRADES    = 10   # minimum matched REJECT+MODIFY before Gate B2 is meaningful


# ── data readers ─────────────────────────────────────────────────────────────

def _load_decisions(account: str) -> list[dict]:
    if not os.path.exists(DECISIONS_LOG):
        return []
    rows = []
    with open(DECISIONS_LOG, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                row = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if row.get("account_env") == account:
                rows.append(row)
    return rows


def _load_exits(account: str) -> list[dict]:
    if not os.path.exists(EXIT_LOG):
        return []
    rows = []
    with open(EXIT_LOG, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                row = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if row.get("account") == account:
                rows.append(row)
    return rows


def _closed_trades(account: str) -> dict:
    """{(strategy, symbol, date): [pnl, ...]}"""
    if not os.path.exists(LEDGER_DB):
        return {}
    module = _MODULE_MAP.get(account, "forex")
    con = sqlite3.connect(LEDGER_DB)
    rows = con.execute(
        "SELECT strategy, symbol, realized_pnl, timestamp_open "
        "FROM trades WHERE module=? AND exit_price IS NOT NULL",
        (module,),
    ).fetchall()
    con.close()
    by_key: dict = defaultdict(list)
    for strat, sym, pnl, t_open in rows:
        by_key[(strat, sym, (t_open or "")[:10])].append(float(pnl or 0))
    return by_key


def _match_pnl(dec: dict, closed: dict) -> float | None:
    key = (dec.get("strategy"), dec.get("symbol"), (dec.get("ts") or "")[:10])
    hits = closed.get(key)
    return hits[0] if hits else None


# ── gate computations ─────────────────────────────────────────────────────────

def gate_b(account: str) -> dict:
    """Returns {b1_pass, b2_pass, net_eur, approve_avg, reject_avg, n_matched}."""
    decs   = _load_decisions(account)
    closed = _closed_trades(account)

    approve_pnls: list[float] = []
    reject_pnls:  list[float] = []
    actionable_deltas: list[float] = []

    for dec in decs:
        action = dec.get("agent_action", "HOLD")
        if action not in ("APPROVE", "REJECT", "MODIFY"):
            continue
        entered    = bool(dec.get("entered_by_atos"))
        multiplier = float(dec.get("agent_size_multiplier") or 1.0)
        pnl        = _match_pnl(dec, closed)
        if pnl is None:
            continue

        if action == "APPROVE":
            approve_pnls.append(pnl)
        elif action == "REJECT" and entered:
            reject_pnls.append(pnl)
            actionable_deltas.append(-pnl)
        elif action == "MODIFY" and entered:
            m = min(max(multiplier, 0.0), 1.0)
            actionable_deltas.append(pnl * (m - 1.0))

    net_eur      = sum(actionable_deltas)
    n_matched    = len(actionable_deltas)
    approve_avg  = (sum(approve_pnls) / len(approve_pnls)) if approve_pnls else None
    reject_avg   = (sum(reject_pnls)  / len(reject_pnls))  if reject_pnls  else None

    b1_pass = net_eur > 0 and n_matched >= MIN_B_TRADES
    b2_pass = (approve_avg is not None and reject_avg is not None
               and approve_avg > reject_avg
               and len(approve_pnls) >= 5)

    return {
        "b1_pass":    b1_pass,
        "b2_pass":    b2_pass,
        "net_eur":    round(net_eur, 1),
        "n_matched":  n_matched,
        "approve_avg": round(approve_avg, 2) if approve_avg is not None else None,
        "reject_avg":  round(reject_avg,  2) if reject_avg  is not None else None,
        "n_approve":   len(approve_pnls),
        "n_reject":    len(reject_pnls),
    }


def gate_c(account: str) -> dict:
    """Returns {c_pass, n_matched, net_delta, good_call_pct}."""
    exits  = _load_exits(account)
    closed = _closed_trades(account)

    matched: list[dict] = []
    for row in exits:
        if row.get("action") != "EXIT_NOW":
            continue
        key = (row.get("strategy"), row.get("symbol"), str(row.get("ts", ""))[:10])
        hits = closed.get(key)
        if not hits:
            continue
        actual = hits[0]
        est    = float(row.get("est_pnl_eur_at_decision") or 0)
        matched.append({"est": est, "actual": actual, "delta": est - actual})

    n_matched    = len(matched)
    net_delta    = sum(r["delta"] for r in matched)
    good_calls   = sum(1 for r in matched if r["delta"] > 0)
    good_pct     = round(100 * good_calls / n_matched, 1) if n_matched else 0

    c_pass = n_matched >= MIN_EXIT_TRADES and net_delta > 0 and good_pct >= 60

    return {
        "c_pass":       c_pass,
        "n_matched":    n_matched,
        "net_delta":    round(net_delta, 1),
        "good_call_pct": good_pct,
    }


# ── email ─────────────────────────────────────────────────────────────────────

def _send_ready_email(b: dict, c: dict) -> None:
    try:
        from atos.notifier import _send
    except ImportError:
        print("  [gates] notifier not available — email skipped")
        return

    subject = "ATOS AI - Copilot Ready to Trade"
    html = f"""
<html><body style="font-family:sans-serif;background:#0f0f13;color:#e2e8f0;padding:24px;">
<h2 style="color:#60a5fa;">AI Copilot Gates Passed ✅</h2>
<p>All Phase B + C gates have passed. The Copilot is ready to apply
decisions to SIM trades. Set <code>exit_copilot.shadow_mode=false</code>
and <code>stocks.shadow_mode=false</code> when ready.</p>

<h3 style="color:#93c5fd;">Phase B (Entry Copilot)</h3>
<table style="border-collapse:collapse;">
  <tr><td style="padding:4px 12px 4px 0;color:#94a3b8;">Gate B1 (Counterfactual)</td>
      <td style="color:#4ade80;">✅ +{b['net_eur']} EUR ({b['n_matched']} trades)</td></tr>
  <tr><td style="padding:4px 12px 4px 0;color:#94a3b8;">Gate B2 (APPROVE quality)</td>
      <td style="color:#4ade80;">✅ APPROVE avg {b['approve_avg']} &gt; REJECT avg {b['reject_avg']}
          (n={b['n_approve']} vs {b['n_reject']})</td></tr>
</table>

<h3 style="color:#93c5fd;">Phase C (Exit Copilot)</h3>
<table style="border-collapse:collapse;">
  <tr><td style="padding:4px 12px 4px 0;color:#94a3b8;">Matched EXIT_NOW trades</td>
      <td style="color:#4ade80;">✅ {c['n_matched']} / {MIN_EXIT_TRADES} required</td></tr>
  <tr><td style="padding:4px 12px 4px 0;color:#94a3b8;">Net exit delta</td>
      <td style="color:#4ade80;">✅ +{c['net_delta']} EUR</td></tr>
  <tr><td style="padding:4px 12px 4px 0;color:#94a3b8;">Good-call rate</td>
      <td style="color:#4ade80;">✅ {c['good_call_pct']}% (≥60% required)</td></tr>
</table>

<p style="margin-top:20px;color:#64748b;font-size:12px;">
Run: <code>python report_phase_b_counterfactual.py sim</code> and
<code>python report_phase_c_counterfactual.py sim</code> for full detail.</p>
</body></html>"""
    _send(subject, html)


def _send_status_email(b: dict, c: dict) -> None:
    try:
        from atos.notifier import _send
    except ImportError:
        return

    b1 = "✅" if b["b1_pass"] else "❌"
    b2 = "✅" if b["b2_pass"] else "❌"
    c_  = "✅" if c["c_pass"]  else "❌"

    approve_str = f"{b['approve_avg']} (n={b['n_approve']})" if b["approve_avg"] is not None else "n/a"
    reject_str  = f"{b['reject_avg']} (n={b['n_reject']})"  if b["reject_avg"]  is not None else "n/a"

    subject = f"ATOS AI - Weekly Gate Check ({datetime.now().strftime('%Y-%m-%d')})"
    html = f"""
<html><body style="font-family:sans-serif;background:#0f0f13;color:#e2e8f0;padding:24px;">
<h2 style="color:#60a5fa;">AI Copilot Gate Status</h2>
<table style="border-collapse:collapse;">
  <tr><td style="padding:4px 12px 4px 0;color:#94a3b8;">B1 Counterfactual</td>
      <td>{b1} {b['net_eur']:+.1f} EUR ({b['n_matched']} trades)</td></tr>
  <tr><td style="padding:4px 12px 4px 0;color:#94a3b8;">B2 APPROVE quality</td>
      <td>{b2} APPROVE {approve_str} vs REJECT {reject_str}</td></tr>
  <tr><td style="padding:4px 12px 4px 0;color:#94a3b8;">C Exit matched trades</td>
      <td>{c_} {c['n_matched']}/{MIN_EXIT_TRADES} trades, delta {c['net_delta']:+.1f} EUR,
          {c['good_call_pct']}% good calls</td></tr>
</table>
<p style="margin-top:16px;color:#64748b;font-size:12px;">
Next check: next Monday 09:00 PKT</p>
</body></html>"""
    _send(subject, html)


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    account = "sim"
    for arg in sys.argv[1:]:
        if not arg.startswith("-"):
            account = arg

    b = gate_b(account)
    c = gate_c(account)

    ts = datetime.now(timezone.utc).isoformat()
    all_pass = b["b1_pass"] and b["b2_pass"] and c["c_pass"]

    summary = (
        f"{ts}  account={account}  "
        f"B1={'PASS' if b['b1_pass'] else 'FAIL'}({b['net_eur']:+.1f}EUR,n={b['n_matched']})  "
        f"B2={'PASS' if b['b2_pass'] else 'FAIL'}"
        f"(app={b['approve_avg']},rej={b['reject_avg']},n={b['n_approve']}/{b['n_reject']})  "
        f"C={'PASS' if c['c_pass'] else 'FAIL'}"
        f"(n={c['n_matched']},delta={c['net_delta']:+.1f},good={c['good_call_pct']}%)  "
        f"OVERALL={'ALL_PASS' if all_pass else 'NOT_READY'}"
    )

    print(summary)

    # append to gate log
    try:
        with open(GATE_LOG, "a", encoding="utf-8") as f:
            f.write(summary + "\n")
    except Exception:
        pass

    if all_pass:
        _send_ready_email(b, c)
        return 0
    else:
        _send_status_email(b, c)
        return 1


if __name__ == "__main__":
    sys.exit(main())
