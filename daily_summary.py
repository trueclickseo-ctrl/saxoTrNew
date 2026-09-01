"""
daily_summary.py
-------------------
End-of-day email across all 4 modules (forex, futures, etf, stocks):
per-strategy trade count, symbols traded, win rate, and profit factor —
plus a handful of additional signals worth checking before trading real
capital (see "Extra pre-live signals" below).

Run once daily via the "ATOS Daily Summary" scheduled task:
    python daily_summary.py                 # today's data (default)
    python daily_summary.py --since 2026-08-20   # a specific date forward

Reuses config/email.json (same credentials as every other notifier in
this codebase).
"""

from __future__ import annotations

import smtplib
from datetime import date, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import housekeeping
import pnl_tracker
import saxo_client

MODULES = ["forex", "futures", "etf", "stocks"]
MODULE_LABELS = {"forex": "Forex", "futures": "Futures", "etf": "ETF", "stocks": "Shares"}


def _module_data(module: str, since: str) -> dict:
    strategies = pnl_tracker.get_strategy_summary_since(module, since)
    trades = sum(s["trades"] for s in strategies)
    pnl = sum(s["total_pnl"] for s in strategies)
    wins = sum(s["wins"] for s in strategies)
    losses = sum(s["losses"] for s in strategies)
    gross_win = sum(s["total_pnl"] for s in strategies if s["total_pnl"] > 0)
    open_positions = sum(s["open"] for s in strategies) or len(pnl_tracker.get_open_positions(module))
    return {
        "module": module,
        "strategies": strategies,
        "trades": trades,
        "pnl": round(pnl, 2),
        "win_rate": round(wins / (wins + losses) * 100, 1) if (wins + losses) else None,
        "open_positions": open_positions,
    }


def _account_health() -> dict:
    """Live, read-only signals only — no fixing. housekeeping.reconcile_all()
    is deliberately NOT called here even though it could report a
    "mismatch count": it mutates live state (cancels/replaces orders) as
    it goes, which is the right behavior for its own scheduled/post-run
    role but not something a reporting script should trigger as a side
    effect of generating an email. scan_naked_positions() is safe to call
    here because it's genuinely read-only by design."""
    health = {"equity": None, "margin_pct": None, "naked_count": None}
    try:
        bal = saxo_client.get_balances()
        health["equity"] = bal.get("TotalValue")
        health["margin_pct"] = bal.get("InitialMargin", {}).get("MarginUtilizationPct")
    except Exception:
        pass
    try:
        naked = housekeeping.scan_naked_positions(send_email=False)
        health["naked_count"] = len(naked)
    except Exception:
        pass
    return health


# ── Email ───────────────────────────────────────────────────────────────

_STYLE = """
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       background: #0d1117; color: #e6edf3; margin: 0; padding: 20px; }
.wrap { max-width: 760px; margin: 0 auto; }
.header { background: linear-gradient(135deg, #161b22, #1a2332);
          border-radius: 10px 10px 0 0; padding: 20px 24px;
          border-bottom: 2px solid #1f6feb; }
.logo  { font-size: 20px; font-weight: 700; color: #58a6ff; letter-spacing: 2px; }
.sub   { font-size: 12px; color: #8b949e; margin-top: 4px; }
.body  { background: #161b22; border-radius: 0 0 10px 10px; padding: 20px 24px; }
h2 { margin: 24px 0 10px; font-size: 15px; color: #f0f6fc; }
h2:first-child { margin-top: 0; }
h3 { margin: 0 0 10px; font-size: 12px; color: #8b949e; text-transform: uppercase; letter-spacing: .5px; }
table  { width: 100%; border-collapse: collapse; margin: 6px 0 18px; font-size: 12.5px; }
th { background: #0d1117; color: #8b949e; font-size: 10.5px; text-transform: uppercase;
     letter-spacing: .4px; padding: 7px 9px; text-align: left; }
td { padding: 7px 9px; border-bottom: 1px solid #21262d; }
tr:last-child td { border-bottom: none; }
.sym  { font-weight: 700; color: #f0f6fc; }
.muted { color: #8b949e; font-size: 11px; }
.pos  { color: #3fb950; font-weight: 700; }
.neg  { color: #f85149; font-weight: 700; }
.metric-row { display: flex; gap: 10px; margin: 10px 0 6px; flex-wrap: wrap; }
.metric { background: #0d1117; border-radius: 8px; padding: 10px 12px; flex: 1; min-width: 84px; }
.metric .lbl { font-size: 9.5px; color: #8b949e; text-transform: uppercase; letter-spacing: .4px; }
.metric .val { font-size: 16px; font-weight: 700; color: #f0f6fc; margin-top: 3px; }
hr { border: none; border-top: 1px solid #21262d; margin: 18px 0; }
.footer { text-align: center; color: #484f58; font-size: 11px; margin-top: 14px; }
.warn { color: #d29922; }
"""


def _wrap(title: str, body_html: str) -> str:
    now = datetime.now().strftime("%d %b %Y  %H:%M PKT")
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>{_STYLE}</style></head><body><div class="wrap">
  <div class="header">
    <div class="logo">DAILY SUMMARY</div>
    <div class="sub">Forex · Futures · ETF · Shares &nbsp;·&nbsp; Saxo SIM &nbsp;·&nbsp; {now}</div>
  </div>
  <div class="body">
    <h2 style="margin-top:0">{title}</h2>
    {body_html}
    <hr>
    <div class="footer">Daily Summary &nbsp;·&nbsp; Auto-generated &nbsp;·&nbsp;
      For pre-live validation, not investment advice</div>
  </div>
</div></body></html>"""


def _pf_str(pf) -> str:
    return f"{pf:.2f}" if pf is not None else "—"


def _strategy_rows(strategies: list[dict]) -> str:
    if not strategies:
        return "<p class='muted'>No trades closed in this window.</p>"
    rows = ""
    for s in strategies:
        col = "pos" if s["total_pnl"] >= 0 else "neg"
        sign = "+" if s["total_pnl"] >= 0 else ""
        symbols = ", ".join(s["symbols"][:6]) + ("…" if len(s["symbols"]) > 6 else "")
        rows += f"""<tr>
          <td class="sym">{s['strategy']}</td>
          <td>{s['trades']}</td>
          <td class="muted">{symbols or '—'}</td>
          <td>{s['win_rate']:.0f}%</td>
          <td>{_pf_str(s['profit_factor'])}</td>
          <td class="{col}">{sign}${s['total_pnl']:,.2f}</td>
          <td class="muted">{s['open']}</td>
        </tr>"""
    return f"""<table>
      <thead><tr>
        <th>Strategy</th><th>Trades</th><th>Symbols</th><th>WR</th>
        <th>Profit Factor</th><th>P&amp;L</th><th>Open</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>"""


def _ai_journal_section() -> str:
    """AI Trading Journal roll-up (roadmap #18, added 2026-08-31). Reads
    data/ai_trade_journal.jsonl -- the generation (one LLM call) happens in
    send_daily_summary() before this, best-effort. Read-only, never raises
    into the email."""
    try:
        import ai.features.trade_journal as tj
        rows = tj._load_jsonl(tj.JOURNAL_LOG)
    except Exception:
        return ""
    if not rows:
        return ""
    trades = [r for r in rows if r.get("event") == "trade"]
    if not trades:
        return ""
    # latest real day_summary (re-runs can append; "None" strings are skipped)
    day_map = {}
    for r in sorted((x for x in rows if x.get("event") == "day_summary"),
                    key=lambda x: x.get("ts") or ""):
        if tj._real_summary(r.get("summary")):
            day_map[r.get("day")] = r
    latest = day_map[max(day_map)] if day_map else None
    summary_html = ""
    if latest:
        summary_html = (f"<p style='margin:2px 0 12px'><span class='muted'>"
                        f"{latest.get('day')}</span> &mdash; {latest.get('summary','')}</p>")

    from collections import Counter
    recent = sorted(trades, key=lambda r: r.get("ts") or "")[-8:]
    rows_html = ""
    for t in recent:
        net = t.get("net_pnl_eur") or 0
        col = "pos" if net >= 0 else "neg"
        lesson = t.get("lesson") or ""
        if lesson.lower() == "none":
            lesson = ""
        acct = t.get("account_env") or "?"
        acct_html = (f"<span class='neg'>{acct}</span>" if acct in ("live", "live_eur")
                     else f"<span class='muted'>{acct}</span>")
        rows_html += f"""<tr>
          <td>{acct_html}</td>
          <td class="sym">{t.get('symbol','?')}</td>
          <td class="muted">{t.get('strategy','?')}</td>
          <td class="muted">{t.get('regime_at_entry') or '—'}</td>
          <td>{t.get('entry_quality') or '—'}/{t.get('exit_quality') or '—'}</td>
          <td class="{col}">{'+' if net>=0 else ''}€{net:,.0f}</td>
          <td class="muted">{lesson}</td>
        </tr>"""

    tags = Counter(tag for t in trades for tag in (t.get("tags") or []))
    tag_line = ", ".join(f"{k} ({v})" for k, v in tags.most_common(6))

    return f"""
    <h2>AI Trading Journal</h2>
    {summary_html}
    <table>
      <thead><tr><th>Acct</th><th>Symbol</th><th>Strategy</th><th>Regime</th>
      <th>Entry/Exit</th><th>Net</th><th>Lesson</th></tr></thead>
      <tbody>{rows_html}</tbody>
    </table>
    <p class="muted" style="margin:2px 0 0">Covers SIM + both LIVE forex accounts. Recurring
    themes: {tag_line or '—'} &nbsp;·&nbsp;
    <code>python ai_trade_journal.py --report</code> for the full journal. Read-only retrospective,
    generated after each trade closed &mdash; nothing here influenced a trade.</p>
    """


def _ai_health_section() -> str:
    """AI shadow-study heartbeat -- a GREEN 'the bot is alive' block, not
    just the failure alerts the watchdog sends. Read-only, never raises."""
    try:
        import ai_shadow_health as h
        from datetime import datetime, timezone, timedelta
        cfg = h._load_config()
        if not h._study_on(cfg):
            return ("<h2>AI Shadow Study</h2><p class='muted'>Switched off in "
                    "config/ai.json &mdash; nothing to monitor.</p>")

        problems = h.check()
        now = datetime.now(timezone.utc)
        decs = h._load_jsonl(h.DECISIONS)
        props = h._load_jsonl(h.PROPOSALS)

        def _within(rows, hrs):
            cut = now - timedelta(hours=hrs)
            return [r for r in rows if (h._parse_ts(r) or datetime.min.replace(tzinfo=timezone.utc)) >= cut]

        d24, p24 = _within(decs, 24), _within(props, 24)
        d7 = _within(decs, 24 * 7)
        ok7 = sum(1 for d in d7 if h._is_ok(d))
        ok_rate = f"{ok7}/{len(d7)}" if d7 else "—"
        last_dec = max((h._parse_ts(d) for d in decs if h._parse_ts(d)), default=None)
        last_ago = (f"{(now - last_dec).total_seconds() / 3600:.1f}h ago"
                    if last_dec else "never")

        from collections import Counter
        acts = Counter((d.get("agent_action") or d.get("action") or "—") for d in d7)
        act_line = ", ".join(f"{k} {v}" for k, v in acts.most_common()) or "—"

        if problems:
            banner = ("<div style='background:#3a1212;border-left:4px solid #f85149;"
                      "padding:10px 14px;border-radius:6px;margin:4px 0 12px'>"
                      "<b class='neg'>&#9679; NOT HEALTHY</b><ul style='margin:6px 0 0'>"
                      + "".join(f"<li class='muted'>{p}</li>" for p in problems) + "</ul></div>")
        else:
            banner = ("<div style='background:#12261a;border-left:4px solid #3fb950;"
                      "padding:10px 14px;border-radius:6px;margin:4px 0 12px'>"
                      "<b class='pos'>&#9679; HEALTHY &mdash; the AI bot is up, scoring signals, "
                      "logging decisions</b></div>")

        applied = "yes (SIM sizing)" if cfg.get("shadow_mode") is False else "no (shadow only)"
        strat = ", ".join(cfg.get("agent_strategies") or []) or "all"
        return f"""
        <h2>AI Shadow Study</h2>
        {banner}
        <div class="metric-row">
          <div class="metric"><div class="lbl">Decisions 24h</div><div class="val">{len(d24)}</div></div>
          <div class="metric"><div class="lbl">Proposals 24h</div><div class="val">{len(p24)}</div></div>
          <div class="metric"><div class="lbl">LLM ok (7d)</div><div class="val">{ok_rate}</div></div>
          <div class="metric"><div class="lbl">Total decisions</div><div class="val">{len(decs)}</div></div>
        </div>
        <p class="muted" style="margin:2px 0 0">
          Last decision {last_ago} &nbsp;&middot;&nbsp; 7d verdicts: {act_line} &nbsp;&middot;&nbsp;
          agent strategies: <code>{strat}</code> &nbsp;&middot;&nbsp; acting on trades: {applied}
          &nbsp;&middot;&nbsp; <code>python ai_shadow_report.py</code> for the study.
        </p>
        """
    except Exception:
        return ""


def _account_equity_section() -> str:
    """Real-money account equity: peak / drawdown / return / give-back,
    from account_equity.py's tracked curve (NOT the sizing cap). Best-
    effort -- never raises into the email."""
    try:
        import account_equity
        return "<h2>Account Equity</h2>" + account_equity.render_html()
    except Exception as exc:
        return f"<h2>Account Equity</h2><p class='muted'>unavailable: {exc}</p>"


def _profit_ladder_section() -> str:
    """RSI profit-protection ladder forward-test roll-up (added 2026-08-31).
    Shows ladder (rsi) vs control (advanced_rsi_master) per account and a
    loud flag once the sample is big enough to act on. Best-effort -- never
    raises into the email."""
    try:
        import report_profit_ladder as rpl
        s = rpl.summarize()
    except Exception as exc:
        return f"<h2>RSI Profit Ladder</h2><p class='muted'>roll-up unavailable: {exc}</p>"
    if not s["per"]:
        return ("<h2>RSI Profit Ladder</h2><p class='muted'>No RSI trades have closed "
                "with observation cards yet — nothing to compare.</p>")

    rows = ""
    for env, b in s["per"].items():
        for arm_name, arm in (("ladder (rsi)", b["ladder"]), ("control (adv_rsi)", b["control"])):
            cap = arm["avg_capture"]
            cap_col = "pos" if (cap is not None and cap > 0.5) else ("neg" if cap is not None else "muted")
            rows += f"""<tr>
              <td class="sym">{env}</td>
              <td class="muted">{arm_name}</td>
              <td>{arm['n']}</td>
              <td>{arm['win_rate'] if arm['win_rate'] is not None else '—'}{'%' if arm['win_rate'] is not None else ''}</td>
              <td>{arm['avg_r'] if arm['avg_r'] is not None else '—'}</td>
              <td>{('€' + format(arm['avg_giveback_eur'], ',.0f')) if arm['avg_giveback_eur'] is not None else '—'}</td>
              <td class="{cap_col}">{cap if cap is not None else '—'}</td>
            </tr>"""

    flag = (f"<p class='pos' style='font-weight:700;margin-top:10px'>SAMPLE READY — "
            f"≥{s['ready_threshold']} closed trades per arm in at least one account. "
            f"Compare avg capture / avg R above and decide whether to keep, retune the "
            f"rungs, or turn the ladder off.</p>"
            if s["sample_ready"] else
            f"<p class='muted' style='margin-top:10px'>Sample still building — need "
            f"≥{s['ready_threshold']} closed trades per arm before this comparison is "
            f"worth acting on. (`python report_profit_ladder.py` for the full breakdown.)</p>")

    return f"""
    <h2>RSI Profit Ladder — forward test</h2>
    <p class="muted" style="margin:0 0 8px">capture = net P&amp;L ÷ max favourable excursion
    (1.0 = kept all the profit, ≤0 = gave it all back). Ladder is on for the <code>rsi</code>
    book; <code>advanced_rsi_master</code> keeps the plain policy as the control.</p>
    <table>
      <thead><tr><th>Account</th><th>Arm</th><th>Closed</th><th>WR</th><th>Avg R</th>
      <th>Avg give-back</th><th>Avg capture</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
    {flag}
    """


def _generate_ai_journal() -> None:
    """Best-effort: write AI Trading Journal entries for every closed trade
    not yet journaled (one batched LLM call per day, capped by
    config/ai.json journal_max_trades_per_run), before the email is
    composed. No `since` filter -- dedup is by card_id, so this naturally
    catches late US-session closers and any day a prior run missed. Gated
    by journal_enabled. Read-only w.r.t. all trading state --
    ai/features/trade_journal.py only reads closed-trade logs and writes
    its own file. Never raises into the summary run."""
    try:
        import ai.features.trade_journal as tj
        res = tj.run()
        if res.get("journaled"):
            print(f"[daily_summary] AI journal: {res['journaled']} trade(s), "
                  f"{res['days']} day(s)")
        for e in res.get("errors", []):
            print(f"[daily_summary] AI journal error -- {e}")
    except Exception as exc:
        print(f"[daily_summary] AI journal generation skipped: {exc}")


def send_daily_summary(since: str | None = None) -> bool:
    since = since or date.today().isoformat()
    _generate_ai_journal()
    sections = []
    total_trades = 0
    total_pnl = 0.0

    for module in MODULES:
        data = _module_data(module, since)
        total_trades += data["trades"]
        total_pnl += data["pnl"]
        if not data["strategies"] and data["open_positions"] == 0:
            continue  # nothing happened in this module today -- skip the section
        col = "pos" if data["pnl"] >= 0 else "neg"
        sign = "+" if data["pnl"] >= 0 else ""
        wr = f"{data['win_rate']:.0f}%" if data["win_rate"] is not None else "—"
        sections.append(f"""
        <h2>{MODULE_LABELS[module]}</h2>
        <div class="metric-row">
          <div class="metric"><div class="lbl">Trades Closed</div><div class="val">{data['trades']}</div></div>
          <div class="metric"><div class="lbl">Win Rate</div><div class="val">{wr}</div></div>
          <div class="metric"><div class="lbl">P&amp;L</div><div class="val {col}">{sign}${data['pnl']:,.2f}</div></div>
          <div class="metric"><div class="lbl">Open Now</div><div class="val">{data['open_positions']}</div></div>
        </div>
        {_strategy_rows(data['strategies'])}
        """)

    health = _account_health()
    eq = f"${health['equity']:,.0f}" if health["equity"] is not None else "—"
    mg = f"{health['margin_pct']:.1f}%" if health["margin_pct"] is not None else "—"
    mg_col = "neg" if (health["margin_pct"] or 0) >= 50 else ""
    naked = health["naked_count"]
    naked_col = "neg" if naked else "pos"
    naked_str = str(naked) if naked is not None else "—"

    day_col = "pos" if total_pnl >= 0 else "neg"
    day_sign = "+" if total_pnl >= 0 else ""

    header = f"""
    <div class="metric-row">
      <div class="metric"><div class="lbl">Total Trades</div><div class="val">{total_trades}</div></div>
      <div class="metric"><div class="lbl">Day P&amp;L</div><div class="val {day_col}">{day_sign}${total_pnl:,.2f}</div></div>
      <div class="metric"><div class="lbl">Account Equity</div><div class="val">{eq}</div></div>
      <div class="metric"><div class="lbl">Margin Used</div><div class="val {mg_col}">{mg}</div></div>
    </div>
    <h3 style="margin-top:16px">System Health (pre-live checklist)</h3>
    <div class="metric-row">
      <div class="metric"><div class="lbl">Naked Positions</div><div class="val {naked_col}">{naked_str}</div></div>
    </div>
    <p class="muted" style="margin:2px 0 0">Reconciliation mismatches aren't shown here — checking them would
    mutate live state, which this report doesn't do. See the Housekeeping/Safeguard emails (every 30 min) for that.</p>
    """

    body = (header + "".join(sections) + _account_equity_section() + _ai_health_section()
            + _ai_journal_section() + _profit_ladder_section())
    subject = f"Daily Summary — {total_trades} trades | {day_sign}${total_pnl:,.0f} | {since}"
    html = _wrap(f"Trading Day — {since}", body)
    return _send_email(subject, html)


def _load_email_cfg():
    return housekeeping._load_email_cfg()


def _send_email(subject: str, html: str) -> bool:
    cfg = _load_email_cfg()
    if not cfg:
        print(f"[daily_summary] no config/email.json — would have sent: {subject}")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"Daily Summary <{cfg['sender_email']}>"
        msg["To"]      = cfg["recipient_email"]
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"]) as s:
            s.starttls()
            s.login(cfg["sender_email"], cfg["sender_password"])
            s.sendmail(cfg["sender_email"], cfg["recipient_email"], msg.as_string())
        return True
    except Exception as exc:
        print(f"[daily_summary] email FAILED: {exc}")
        return False


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--since", default=None, help="YYYY-MM-DD (default: today)")
    args = p.parse_args()
    ok = send_daily_summary(args.since)
    print("sent" if ok else "not sent (see message above)")
