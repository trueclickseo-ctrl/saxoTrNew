"""
forex/notifier.py
-----------------
Email notifications for the FX Autopilot runner.

Reads credentials from config/email.json (gitignored).
If that file is missing, all functions return silently — the runner continues.

Functions:
    send_run_summary()       — after each live run (entries, exits, holdings, P&L)
    send_token_expired()     — when Saxo token fails at startup
    send_weekly_report()     — call Friday; per-strategy P&L + equity curve
"""

import json
import os
import smtplib
import sys
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CFG  = os.path.join(_ROOT, "config", "email.json")

# ── Email core ────────────────────────────────────────────────────────────────

def _load_cfg() -> dict | None:
    if not os.path.exists(_CFG):
        return None
    try:
        with open(_CFG) as f:
            return json.load(f)
    except Exception:
        return None


def _send(subject: str, html: str) -> bool:
    cfg = _load_cfg()
    if not cfg:
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"FX Autopilot <{cfg['sender_email']}>"
        msg["To"]      = cfg["recipient_email"]
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"]) as s:
            s.starttls()
            s.login(cfg["sender_email"], cfg["sender_password"])
            s.sendmail(cfg["sender_email"], cfg["recipient_email"], msg.as_string())
        print(f"  [fx_notifier] email sent: {subject}", file=sys.stderr)
        return True
    except Exception as exc:
        print(f"  [fx_notifier] email FAILED: {exc}", file=sys.stderr)
        return False


_STYLE = """
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       background: #0d1117; color: #e6edf3; margin: 0; padding: 20px; }
.wrap { max-width: 640px; margin: 0 auto; }
.header { background: linear-gradient(135deg, #161b22, #1a2332);
          border-radius: 10px 10px 0 0; padding: 20px 24px;
          border-bottom: 2px solid #1f6feb; }
.logo  { font-size: 20px; font-weight: 700; color: #58a6ff; letter-spacing: 2px; }
.sub   { font-size: 12px; color: #8b949e; margin-top: 4px; }
.body  { background: #161b22; border-radius: 0 0 10px 10px; padding: 20px 24px; }
h2 { margin: 0 0 16px; font-size: 17px; color: #f0f6fc; }
h3 { margin: 20px 0 8px; font-size: 14px; color: #8b949e;
     text-transform: uppercase; letter-spacing: .5px; }
.badge { display: inline-block; padding: 3px 10px; border-radius: 20px;
         font-size: 11px; font-weight: 700; letter-spacing: .5px; }
.buy   { background: #1a7f3722; color: #3fb950; border: 1px solid #2ea043; }
.sell  { background: #da363322; color: #f85149; border: 1px solid #da3633; }
.warn  { background: #d2992022; color: #d29922; border: 1px solid #9e6a03; }
.info  { background: #1f6feb22; color: #58a6ff; border: 1px solid #1f6feb; }
table  { width: 100%; border-collapse: collapse; margin: 10px 0; font-size: 13px; }
th { background: #0d1117; color: #8b949e; font-size: 11px; text-transform: uppercase;
     letter-spacing: .4px; padding: 8px 10px; text-align: left; }
td { padding: 8px 10px; border-bottom: 1px solid #21262d; }
tr:last-child td { border-bottom: none; }
.sym  { font-weight: 700; color: #f0f6fc; }
.muted { color: #8b949e; font-size: 11px; }
.pos  { color: #3fb950; font-weight: 700; }
.neg  { color: #f85149; font-weight: 700; }
.metric-row { display: flex; gap: 12px; margin: 14px 0; flex-wrap: wrap; }
.metric { background: #0d1117; border-radius: 8px; padding: 12px 14px; flex: 1; min-width: 90px; }
.metric .lbl { font-size: 10px; color: #8b949e; text-transform: uppercase; letter-spacing: .4px; }
.metric .val { font-size: 18px; font-weight: 700; color: #f0f6fc; margin-top: 4px; }
hr { border: none; border-top: 1px solid #21262d; margin: 16px 0; }
.footer { text-align: center; color: #484f58; font-size: 11px; margin-top: 14px; }
"""


def _wrap(title: str, body_html: str, subtitle: str | None = None) -> str:
    now = datetime.now().strftime("%d %b %Y  %H:%M PKT")
    # subtitle defaults kept generic -- callers that know the real
    # strategy/pair/venue counts (send_run_summary) pass an accurate one so
    # this line can't silently go stale the way the old hardcoded literal
    # strategy+pair+venue string did (it drifted to a wrong pair count).
    sub = subtitle or "FX Autopilot"
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>{_STYLE}</style></head><body><div class="wrap">
  <div class="header">
    <div class="logo">FX AUTOPILOT</div>
    <div class="sub">{sub} &nbsp;·&nbsp; {now}</div>
  </div>
  <div class="body">
    <h2>{title}</h2>
    {body_html}
    <hr>
    <div class="footer">FX Autopilot &nbsp;·&nbsp; Auto-generated &nbsp;·&nbsp;
      Only the token needs manual refresh</div>
  </div>
</div></body></html>"""


# ── Public API ────────────────────────────────────────────────────────────────

def send_token_expired(scheduled_time: str = "", live: bool = False) -> None:
    """
    Send email when the Saxo token has expired and the run was skipped.
    Call this before exiting from the runner on a 401 error.
    """
    now      = datetime.now()
    run_time = scheduled_time or now.strftime("%H:%M")
    today    = now.strftime("%Y-%m-%d")
    env_name = "LIVE (real money)" if live else "SIM"

    body = f"""
    <span class="badge warn">⚠ TOKEN EXPIRED</span>
    <p style="color:#d29922; margin-top:14px">
      The scheduled run at <strong>{run_time} PKT</strong> was <strong>SKIPPED</strong>
      because the Saxo {env_name} token has expired. No orders were placed or checked.
    </p>
    <div class="metric-row" style="margin-top:14px">
      <div class="metric">
        <div class="lbl">Skipped Run</div>
        <div class="val" style="font-size:14px">{run_time} PKT</div>
      </div>
      <div class="metric">
        <div class="lbl">Date</div>
        <div class="val" style="font-size:14px">{today}</div>
      </div>
    </div>
    <h3>Action Required</h3>
    <ol style="color:#8b949e; line-height:1.8; margin:0 0 0 16px; font-size:13px">
      <li>Open a terminal in the project directory</li>
      <li>Run: <code style="background:#0d1117;padding:2px 6px;border-radius:4px;
          color:#58a6ff">python set_token.py</code></li>
      <li>Paste the new token from Saxo when prompted</li>
    </ol>
    <p class="muted" style="margin-top:12px">
      Token is the only manual step — everything else runs automatically.<br>
      Open positions are protected by Saxo GTC stop orders (active 24/7).
    </p>
    """

    tag = "[LIVE] " if live else ""
    subject = f"{tag}FX Autopilot ⚠ TOKEN EXPIRED — run {run_time} SKIPPED [{today}]"
    _send(subject, _wrap(f"{'LIVE — ' if live else ''}Saxo Token Expired — Refresh Required", body))


def send_order_venue_down(account_env: str = "sim", consecutive: int = 0,
                          saxo_error: str = "", blocked: list | None = None,
                          paper_fill: bool = False) -> None:
    """Sent once per run when the order-venue circuit breaker trips: Saxo has
    rejected `consecutive` entry orders in a row (the "CouldNotCompleteRequest
    (90)" outage pattern, 2026-08-28 / 2026-08-31). `saxo_error` is the real
    error string from the last rejection; `blocked` is
    [(strategy, sym, direction, paper_filled), ...] for every signal Saxo
    couldn't fill this run; `paper_fill` = SIM booked them locally instead of
    dropping them. Best-effort — never raises."""
    try:
        now      = datetime.now()
        run_time = now.strftime("%H:%M")
        today    = now.strftime("%Y-%m-%d")
        env_name = {"live": "LIVE (real money)", "live_eur": "LIVE EUR (real money)"}.get(
            account_env, "SIM")
        blocked = blocked or []
        n_paper = sum(1 for b in blocked if len(b) > 3 and b[3])
        n_drop  = len(blocked) - n_paper

        if paper_fill:
            headline = (f"Saxo's SIM order engine rejected every entry this run "
                        f"(<code>{saxo_error or 'CouldNotCompleteRequest'}</code>). "
                        f"ATOS <strong>paper-filled {n_paper} signal(s)</strong> locally at the "
                        f"live quote so the forward-test keeps running — they are managed by "
                        f"ATOS's own stop/TP/exit logic, no broker order exists for them.")
        else:
            headline = (f"Saxo rejected <strong>{consecutive} entry orders in a row</strong> on the "
                        f"{env_name} account (<code>{saxo_error or 'CouldNotCompleteRequest'}</code>). "
                        f"The scan <strong>stopped placing new entries</strong> for the rest of this "
                        f"run; {n_drop} signal(s) were not taken.")

        rows = "".join(
            f"<tr><td class='sym'>{s}</td><td>{sy}</td><td>{d}</td>"
            f"<td class='{'pos' if (len(b) > 3 and b[3]) else 'neg'}'>"
            f"{'PAPER-FILLED' if (len(b) > 3 and b[3]) else 'not taken'}</td></tr>"
            for b in blocked for (s, sy, d) in [(b[0], b[1], b[2])]
        ) or "<tr><td colspan='4' class='muted'>(none recorded)</td></tr>"

        body = f"""
        <span class="badge warn">⚠ ORDER VENUE DOWN</span>
        <p style="color:#d29922; margin-top:14px">{headline}</p>
        <div class="metric-row" style="margin-top:14px">
          <div class="metric"><div class="lbl">Run</div>
            <div class="val" style="font-size:14px">{run_time} PKT · {today}</div></div>
          <div class="metric"><div class="lbl">Consecutive rejects</div>
            <div class="val" style="font-size:14px">{consecutive}</div></div>
          <div class="metric"><div class="lbl">Signals this run</div>
            <div class="val" style="font-size:14px">{n_paper} paper · {n_drop} dropped</div></div>
        </div>
        <h3>Signals Saxo couldn't fill</h3>
        <table><thead><tr><th>Strategy</th><th>Pair</th><th>Side</th><th>Outcome</th></tr></thead>
        <tbody>{rows}</tbody></table>
        <h3>What still ran</h3>
        <p class="muted">Exits and stop-loss healing run every cycle — open positions
          stay managed. The watchdog re-fires the scan ahead of its normal cadence so
          real fills resume as soon as Saxo answers.</p>
        <h3>Action</h3>
        <p class="muted">Usually nothing — clears when Saxo's order endpoint recovers.
          {'Paper positions convert to normal SIM once fills resume (new entries only; existing paper trades keep being managed locally).' if paper_fill else ''}</p>
        """
        tag = "[LIVE] " if account_env in ("live", "live_eur") else ""
        subject = (f"{tag}FX Autopilot ⚠ ORDER VENUE DOWN — "
                   f"{n_paper} paper-filled [{today} {run_time}]" if paper_fill
                   else f"{tag}FX Autopilot ⚠ ORDER VENUE DOWN — entries paused [{today} {run_time}]")
        _send(subject, _wrap(f"{env_name} — Saxo Order Venue Down", body))
    except Exception as exc:  # notifier must never crash the runner
        print(f"  [fx_notifier] send_order_venue_down FAILED: {exc}", file=sys.stderr)


def send_run_summary(
    session:       str,
    entries:       int,
    exits:         int,
    holdings:      int,
    equity:        float,
    today_trades:  list,     # from trade_logger.tail("forex") filtered to today
    strategy_stats: list,    # from pnl_tracker.get_strategy_summary("forex")
    healed_stops:  int = 0,
    healed_tp:     int = 0,
    live:          bool = False,   # True = the real-money LIVE account, not SIM
    pairs_trading: int = 0,        # distinct pairs held (holdings counts strategy:symbol keys)
    strategy_count: int = 0,       # strategies that ran this cycle
    pair_count:    int = 0,        # pairs in the scanned universe
    venue:         str = "",       # "Saxo SIM" / "Saxo LIVE" / "Saxo LIVE_EUR"
) -> None:
    """
    Send run summary email after each live execution.

    today_trades: list of dicts with keys: strategy, symbol, side, quantity,
                  price, stop_price, order_id, notes
    strategy_stats: list from pnl_tracker.get_strategy_summary("forex")
    """
    now   = datetime.now()
    today = now.strftime("%Y-%m-%d")
    time_ = now.strftime("%H:%M")

    # ── Badge ──
    if entries > 0 and exits > 0:
        badge = f'<span class="badge buy">{entries} ENTERED</span> &nbsp; <span class="badge sell">{exits} EXITED</span>'
    elif entries > 0:
        badge = f'<span class="badge buy">{entries} ENTERED</span>'
    elif exits > 0:
        badge = f'<span class="badge sell">{exits} EXITED</span>'
    else:
        badge = '<span class="badge info">NO TRADES</span>'

    # ── Metrics ──
    heal_html = ""
    if healed_stops or healed_tp:
        heal_html = (f'<div class="metric"><div class="lbl">Healed</div>'
                     f'<div class="val" style="color:#d29922; font-size:14px">'
                     f'{healed_stops} stop{"s" if healed_stops!=1 else ""}'
                     f'{f", {healed_tp} TP" if healed_tp else ""}</div></div>')

    metrics = f"""
    <div class="metric-row" style="margin-top:14px">
      <div class="metric">
        <div class="lbl">Equity</div>
        <div class="val" style="color:#58a6ff">${equity:,.0f}</div>
      </div>
      <div class="metric">
        <div class="lbl">Positions</div>
        <div class="val">{holdings}</div>
        <div class="muted">{f"in {pairs_trading} pair{'s' if pairs_trading != 1 else ''}" if pairs_trading else "&nbsp;"}</div>
      </div>
      <div class="metric">
        <div class="lbl">Entries</div>
        <div class="val" style="color:#3fb950">{entries}</div>
      </div>
      <div class="metric">
        <div class="lbl">Exits</div>
        <div class="val" style="color:#f85149">{exits}</div>
      </div>
      {heal_html}
    </div>"""

    # ── Today's trades table ──
    if today_trades:
        rows = ""
        for t in today_trades:
            side  = t.get("side", "")
            cls   = "buy" if side == "Buy" else "sell"
            stop  = t.get("stop_price", "")
            try:
                stop = f"{float(stop):.5f}" if stop else "—"
            except Exception:
                stop = "—"
            price = t.get("price", "")
            try:
                price = f"{float(price):.5f}"
            except Exception:
                pass
            qty = t.get("quantity", "")
            try:
                qty = f"{int(float(qty)):,}"
            except Exception:
                pass
            notes = t.get("notes", "")
            rows += f"""<tr>
              <td><span class="badge {cls}">{side}</span></td>
              <td class="sym">{t.get('symbol','')}</td>
              <td class="muted">{t.get('strategy','')}</td>
              <td>{qty}</td>
              <td>{price}</td>
              <td class="muted">{stop}</td>
              <td class="muted">{notes[:25] if notes else '—'}</td>
            </tr>"""
        trades_html = f"""
        <h3>Today's Trades ({len(today_trades)})</h3>
        <table>
          <thead><tr>
            <th>Side</th><th>Pair</th><th>Strategy</th>
            <th>Qty</th><th>Price</th><th>Stop</th><th>Notes</th>
          </tr></thead>
          <tbody>{rows}</tbody>
        </table>"""
    else:
        trades_html = "<h3>Trades</h3><p class='muted'>No trades executed this run.</p>"

    # ── Strategy P&L table ──
    if strategy_stats:
        srows = ""
        for s in strategy_stats:
            pnl   = s.get("total_pnl", 0)
            col   = "pos" if pnl >= 0 else "neg"
            sign  = "+" if pnl >= 0 else ""
            pf    = s.get("profit_factor")
            pf_s  = f"{pf:.2f}" if pf else "—"
            wr    = s.get("win_rate", 0)
            n     = s.get("trades", 0)
            op    = s.get("open", 0)
            srows += f"""<tr>
              <td class="sym">{s.get('strategy','').upper()}</td>
              <td>{n} closed{f" / {op} open" if op else ""}</td>
              <td>{wr:.0f}%</td>
              <td>{pf_s}</td>
              <td class="{col}">{sign}${pnl:,.2f}</td>
            </tr>"""
        strat_html = f"""
        <h3>Strategy P&L (All-Time)</h3>
        <table>
          <thead><tr>
            <th>Strategy</th><th>Trades</th><th>Win Rate</th>
            <th>Prof. Factor</th><th>P&L (USD)</th>
          </tr></thead>
          <tbody>{srows}</tbody>
        </table>"""
    else:
        strat_html = ""

    body = f"""
    {badge}
    {metrics}
    {trades_html}
    {strat_html}
    <p class="muted" style="margin-top:12px">
      Session: {session.upper()} &nbsp;·&nbsp; Run: {time_} PKT &nbsp;·&nbsp; {today}<br>
      Stop orders active 24/7 in Saxo. Next run per schedule.
    </p>
    """

    action = (f"{entries}E/{exits}X" if (entries or exits) else "no trades")
    tag = "[LIVE] " if live else ""
    pos_txt = f"{holdings} pos" + (f"/{pairs_trading} pairs" if pairs_trading else "")
    subject = (f"{tag}FX Autopilot — {action} | {pos_txt} | "
               f"${equity:,.0f} equity | {time_} PKT")
    _sub = " · ".join(x for x in [
        f"{strategy_count} Strateg{'ies' if strategy_count != 1 else 'y'}" if strategy_count else "",
        f"{pair_count} Pairs" if pair_count else "",
        venue or ("Saxo LIVE" if live else "Saxo SIM"),
    ] if x)
    _send(subject, _wrap(f"{'LIVE — ' if live else ''}Run Complete — {session.upper()} Session",
                         body, subtitle=_sub))


def send_signals_detected(
    strategy:       str,
    signals:        list,          # raw dicts from strat.generate_signals()
    entered:        list,          # symbols actually entered this run
    account_env:    str,           # "live" | "live_eur"
    market_closed:  bool = False,  # True = FX weekend, entries were gated
) -> None:
    """Real-money accounts only: email the strategy signals a LIVE scan
    produced, with whether each was entered. Exists so a signal that fires
    while the FX market is closed for the weekend (entries gated by
    _fx_market_open) -- or one blocked by any other gate -- is still
    visible, not silently swallowed. SIM never calls this.
    """
    if not signals:
        return

    now   = datetime.now()
    today = now.strftime("%Y-%m-%d")
    time_ = now.strftime("%H:%M")
    entered_set = set(entered or [])
    venue = "LIVE (real money)" if account_env == "live" else "LIVE EUR (real money)"

    if market_closed:
        headline = (f'<span class="badge warn">FX MARKET CLOSED</span>'
                    f'<p style="color:#d29922;margin-top:14px">'
                    f'{len(signals)} {strategy.upper()} signal(s) detected but '
                    f'<strong>NOT entered</strong> — the FX market is closed for the '
                    f'weekend. Entries resume Sunday ~22:00 UTC; these will be '
                    f're-evaluated on fresh data then, not carried over as resting '
                    f'orders.</p>')
    else:
        n_in = sum(1 for s in signals if s.get("symbol") in entered_set)
        headline = (f'<span class="badge info">{len(signals)} SIGNAL(S)</span>'
                    f'<p class="muted" style="margin-top:14px">{n_in} entered, '
                    f'{len(signals) - n_in} not taken (blocked by an entry gate — '
                    f'exposure cap / cost / spread / slots / heat; see the run log '
                    f'for the exact reason on each).</p>')

    rows = ""
    for s in signals:
        sym  = s.get("symbol", "")
        did  = sym in entered_set
        dirn = s.get("direction", "")
        cls  = "buy" if dirn == "Buy" else "sell"
        rsi  = s.get("rsi", s.get("score", ""))
        try:
            rsi = f"{float(rsi):.1f}"
        except Exception:
            rsi = "—"
        stop = s.get("stop_price", "")
        try:
            stop = f"{float(stop):.5f}"
        except Exception:
            stop = "—"
        status = ('<span class="badge buy">ENTERED</span>' if did
                  else '<span class="muted">not entered</span>')
        rows += (f"<tr><td><span class='badge {cls}'>{dirn}</span></td>"
                 f"<td class='sym'>{sym}</td><td class='muted'>RSI {rsi}</td>"
                 f"<td class='muted'>{stop}</td><td>{status}</td></tr>")

    body = f"""
    {headline}
    <table>
      <thead><tr><th>Side</th><th>Pair</th><th>Signal</th><th>Stop</th><th>Status</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
    <p class="muted" style="margin-top:12px">
      Strategy: {strategy.upper()} &nbsp;·&nbsp; {venue} &nbsp;·&nbsp; {time_} PKT &nbsp;·&nbsp; {today}
    </p>
    """

    n_entered = sum(1 for s in signals if s.get("symbol") in entered_set)
    if market_closed:
        subj = f"[LIVE] {strategy.upper()} — {len(signals)} signal(s) detected, market closed [{today}]"
    else:
        subj = (f"[LIVE] {strategy.upper()} — {len(signals)} signal(s), "
                f"{n_entered} entered [{today}]")
    _send(subj, _wrap(f"LIVE — {strategy.upper()} Signals Detected", body,
                      subtitle=f"{venue} · {time_} PKT"))


def send_weekly_report(
    strategy_stats: list,
    equity:         float,
    total_realized: float,
    open_count:     int,
    week_closed:    list,    # closed trades this week: dicts with symbol, strategy, pnl
) -> None:
    """Send Friday weekly P&L report with per-strategy breakdown."""
    now   = datetime.now()
    today = now.strftime("%Y-%m-%d")

    eq_col  = "#58a6ff"
    pnl_col = "#3fb950" if total_realized >= 0 else "#f85149"
    pnl_sgn = "+" if total_realized >= 0 else ""

    metrics = f"""
    <div class="metric-row">
      <div class="metric">
        <div class="lbl">Account Equity</div>
        <div class="val" style="color:{eq_col}">${equity:,.0f}</div>
      </div>
      <div class="metric">
        <div class="lbl">Total Realized P&L</div>
        <div class="val" style="color:{pnl_col}">{pnl_sgn}${total_realized:,.2f}</div>
      </div>
      <div class="metric">
        <div class="lbl">Open Positions</div>
        <div class="val">{open_count}</div>
      </div>
      <div class="metric">
        <div class="lbl">Week Closed</div>
        <div class="val">{len(week_closed)}</div>
      </div>
    </div>"""

    # Strategy table
    if strategy_stats:
        srows = ""
        for s in strategy_stats:
            pnl  = s.get("total_pnl", 0)
            col  = "pos" if pnl >= 0 else "neg"
            sgn  = "+" if pnl >= 0 else ""
            pf   = s.get("profit_factor")
            pf_s = f"{pf:.2f}" if pf else "—"
            srows += f"""<tr>
              <td class="sym">{s.get('strategy','').upper()}</td>
              <td>{s.get('trades', 0)}</td>
              <td>{s.get('win_rate', 0):.0f}%</td>
              <td>{pf_s}</td>
              <td class="{col}">{sgn}${pnl:,.2f}</td>
              <td class="muted">${s.get('best', 0):+,.0f} / ${s.get('worst', 0):+,.0f}</td>
            </tr>"""
        strat_html = f"""
        <h3>Strategy P&L (All-Time)</h3>
        <table>
          <thead><tr>
            <th>Strategy</th><th>Trades</th><th>Win Rate</th>
            <th>Prof. Factor</th><th>P&L (USD)</th><th>Best / Worst</th>
          </tr></thead>
          <tbody>{srows}</tbody>
        </table>"""
    else:
        strat_html = "<p class='muted'>No closed trades yet.</p>"

    # This week's closed trades
    if week_closed:
        wrows = ""
        for t in week_closed:
            p   = t.get("realized_pnl", 0) or 0
            col = "pos" if p >= 0 else "neg"
            sgn = "+" if p >= 0 else ""
            wrows += f"""<tr>
              <td class="sym">{t.get('symbol','')}</td>
              <td class="muted">{t.get('strategy','')}</td>
              <td class="muted">{(t.get('timestamp_close') or '')[:10]}</td>
              <td class="muted">{t.get('exit_reason','—')}</td>
              <td class="{col}">{sgn}${p:,.2f}</td>
            </tr>"""
        week_html = f"""
        <h3>Closed This Week ({len(week_closed)})</h3>
        <table>
          <thead><tr>
            <th>Pair</th><th>Strategy</th><th>Date</th><th>Reason</th><th>P&L</th>
          </tr></thead>
          <tbody>{wrows}</tbody>
        </table>"""
    else:
        week_html = "<p class='muted'>No trades closed this week.</p>"

    body = f"""
    {metrics}
    {strat_html}
    {week_html}
    <p class="muted" style="margin-top:12px">
      Weekly report generated {today} &nbsp;·&nbsp; Next report next Friday
    </p>
    """

    subject = (f"FX Autopilot Weekly — ${total_realized:,.0f} realized | "
               f"{open_count} open | {today}")
    _send(subject, _wrap("Weekly Performance Report", body))


def send_lbo_trade_opened(
    symbol:    str,
    direction: str,
    entry:     float,
    stop:      float,
    tp:        float,
    units:     int,
    session:   str,
    range_pips: float = 0,
) -> None:
    """Immediate alert when a London Breakout trade is opened."""
    now    = datetime.now()
    time_  = now.strftime("%H:%M")
    today  = now.strftime("%Y-%m-%d")
    cls    = "buy" if direction == "Buy" else "sell"
    tag    = "LONG" if direction == "Buy" else "SHORT"
    rr     = round(abs(tp - entry) / abs(entry - stop), 1) if abs(entry - stop) > 0 else 0
    risk_sek = round(abs(entry - stop) * units * 10.7, 0)

    body = f"""
    <span class="badge {cls}">&#9650; {direction.upper()} OPENED — {tag}</span>
    <div class="metric-row" style="margin-top:14px">
      <div class="metric">
        <div class="lbl">Pair</div>
        <div class="val" style="font-size:20px">{symbol}</div>
      </div>
      <div class="metric">
        <div class="lbl">Session</div>
        <div class="val" style="font-size:16px; color:#58a6ff">{session}</div>
      </div>
      <div class="metric">
        <div class="lbl">Units</div>
        <div class="val" style="font-size:16px">{units:,}</div>
      </div>
      <div class="metric">
        <div class="lbl">R:R</div>
        <div class="val" style="font-size:16px">{rr}:1</div>
      </div>
    </div>
    <table style="margin-top:10px">
      <thead><tr>
        <th>Entry</th><th>Stop Loss</th><th>Take Profit</th>
        <th>Range</th><th>Risk (SEK)</th>
      </tr></thead>
      <tbody><tr>
        <td class="sym">{entry:.5f}</td>
        <td class="neg">{stop:.5f}</td>
        <td class="pos">{tp:.5f}</td>
        <td class="muted">{range_pips:.0f} pips</td>
        <td class="neg">~{risk_sek:,.0f} SEK</td>
      </tr></tbody>
    </table>
    <p class="muted" style="margin-top:12px">
      London Breakout · {session} open · {time_} PKT · {today}<br>
      Position closes automatically by 01:00 PKT (20:00 UTC) if TP/SL not hit.
    </p>
    """
    subject = f"LBO 📈 {direction.upper()} {symbol} @ {entry:.5f} — {session} open [{time_} PKT]"
    _send(subject, _wrap(f"Day Trade Opened — {symbol} {tag}", body))


STRATEGY_LABELS = {
    "ema":             "EMA Trend",
    "rsi":             "RSI Pullback",
    "donchian":        "Donchian Break",
    "bb":              "BB Reversion",
    "pullback":        "EMA Pullback",
    "gap":             "Gap Fill",
    "supertrend":      "SuperTrend",
    "zscore":          "Z-Score Rev",
    "ml":              "ML Signals",
    "cnn_lstm":        "CNN-LSTM",
    "london_breakout": "London Breakout",
}


def send_trade_closed(
    strategy:  str,
    symbol:    str,
    direction: str,
    entry:     float,
    exit_px:   float,
    pnl_pct:   float,
    units:     int,
    reason:    str,
    session:   str = "",
    live:      bool = False,   # True = a real-money LIVE account, not SIM
    net_pnl_native: float | None = None,  # true P&L (price + broker cost), pair's own quote ccy
) -> None:
    """Immediate win/loss alert when ANY forex strategy closes a position.

    Was LBO-only (send_lbo_trade_closed) — the other 9 strategies' exits only
    ever showed up in the batched run-summary email, which has no P&L column,
    so a closed trade's win/loss was invisible unless you read the raw log.
    Generalized 2026-08-21 so every strategy's exit gets the same immediate,
    color-coded win/loss email LBO always had.

    net_pnl_native: when the caller has Saxo's own net (price + cost) P&L for
    this exact close (forex/runner.py's _position_net_pnl_quote_ccy()), pass
    it here so the WIN/LOSS badge reflects what actually happened to the
    account balance. Without it, WIN/LOSS falls back to pnl_pct (raw price
    move only) — confirmed live 2026-08-26: a live_eur RSI Pullback close
    showed WIN/+0.41% here (raw price moved in its favor) while Saxo's own
    web trader recorded a net loss once its ~5 EUR round-trip commission was
    included — commission this codebase had never subtracted anywhere the
    email could see it, on a small enough gain that it flipped the sign.
    """
    label   = STRATEGY_LABELS.get(strategy, strategy)
    now     = datetime.now()
    time_   = now.strftime("%H:%M")
    today   = now.strftime("%Y-%m-%d")
    # P&L in the pair's own quote currency (last 3 chars, standard FX convention:
    # EURUSD -> USD, USDJPY -> JPY, GBPCHF -> CHF) — NOT a fixed SEK rate, which
    # only ever made sense for LBO's mostly-EUR-quoted book and would silently
    # show a wrong number once every strategy/pair calls this.
    quote_ccy = symbol[-3:] if len(symbol) >= 6 else ""
    raw_pnl_native = round((exit_px - entry) * units if direction == "Buy"
                           else (entry - exit_px) * units, 0)
    pnl_native = round(net_pnl_native, 0) if net_pnl_native is not None else raw_pnl_native
    won     = (net_pnl_native > 0) if net_pnl_native is not None else (pnl_pct > 0)
    cls     = "buy" if won else "sell"
    result  = "WIN ✓" if won else "LOSS ✗"
    col     = "#3fb950" if won else "#f85149"
    sign    = "+" if pnl_pct >= 0 else ""
    pnl_sgn = "+" if pnl_native >= 0 else ""
    net_note = (" (net of broker cost)" if net_pnl_native is not None
                and (net_pnl_native > 0) != (raw_pnl_native > 0) else "")
    session_line = f" · {session} session" if session else ""

    body = f"""
    <span class="badge {cls}">{result}</span>
    <div class="metric-row" style="margin-top:14px">
      <div class="metric">
        <div class="lbl">Pair</div>
        <div class="val" style="font-size:20px">{symbol}</div>
      </div>
      <div class="metric">
        <div class="lbl">P&L %</div>
        <div class="val" style="color:{col}">{sign}{pnl_pct:.2f}%</div>
      </div>
      <div class="metric">
        <div class="lbl">P&L ({quote_ccy}){net_note}</div>
        <div class="val" style="color:{col}">{pnl_sgn}{pnl_native:,.0f}</div>
      </div>
      <div class="metric">
        <div class="lbl">Units</div>
        <div class="val" style="font-size:16px">{units:,}</div>
      </div>
    </div>
    <table style="margin-top:10px">
      <thead><tr>
        <th>Direction</th><th>Entry</th><th>Exit</th><th>Exit Reason</th>
      </tr></thead>
      <tbody><tr>
        <td><span class="badge {'buy' if direction=='Buy' else 'sell'}">{direction}</span></td>
        <td>{entry:.5f}</td>
        <td class="sym">{exit_px:.5f}</td>
        <td class="muted">{reason}</td>
      </tr></tbody>
    </table>
    <p class="muted" style="margin-top:12px">
      {label}{session_line} · {time_} PKT · {today}
    </p>
    """
    tag = "[LIVE] " if live else ""
    subject = (f"{tag}{label} {'✅' if won else '❌'} {symbol} {result} {sign}{pnl_pct:.2f}% "
               f"({pnl_sgn}{pnl_native:,.0f} {quote_ccy}) [{time_} PKT]")
    _send(subject, _wrap(f"{'LIVE — ' if live else ''}{label} Closed — {symbol} {result}", body))


def send_lbo_trade_closed(**kwargs) -> None:
    """Backward-compatible alias — send_trade_closed() replaced this 2026-08-21."""
    send_trade_closed(strategy="london_breakout", **kwargs)
