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


def _wrap(title: str, body_html: str) -> str:
    now = datetime.now().strftime("%d %b %Y  %H:%M PKT")
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>{_STYLE}</style></head><body><div class="wrap">
  <div class="header">
    <div class="logo">FX AUTOPILOT</div>
    <div class="sub">9 Strategies · 34 Pairs · Saxo SIM &nbsp;·&nbsp; {now}</div>
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

def send_token_expired(scheduled_time: str = "") -> None:
    """
    Send email when the Saxo token has expired and the run was skipped.
    Call this before exiting from the runner on a 401 error.
    """
    now      = datetime.now()
    run_time = scheduled_time or now.strftime("%H:%M")
    today    = now.strftime("%Y-%m-%d")

    body = f"""
    <span class="badge warn">⚠ TOKEN EXPIRED</span>
    <p style="color:#d29922; margin-top:14px">
      The scheduled run at <strong>{run_time} PKT</strong> was <strong>SKIPPED</strong>
      because the Saxo SIM token has expired. No orders were placed or checked.
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

    subject = f"FX Autopilot ⚠ TOKEN EXPIRED — run {run_time} SKIPPED [{today}]"
    _send(subject, _wrap("Saxo Token Expired — Refresh Required", body))


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
        <div class="lbl">Holdings</div>
        <div class="val">{holdings}</div>
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
    subject = (f"FX Autopilot — {action} | {holdings} open | "
               f"${equity:,.0f} equity | {time_} PKT")
    _send(subject, _wrap(f"Run Complete — {session.upper()} Session", body))


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
) -> None:
    """Immediate win/loss alert when ANY forex strategy closes a position.

    Was LBO-only (send_lbo_trade_closed) — the other 9 strategies' exits only
    ever showed up in the batched run-summary email, which has no P&L column,
    so a closed trade's win/loss was invisible unless you read the raw log.
    Generalized 2026-08-21 so every strategy's exit gets the same immediate,
    color-coded win/loss email LBO always had.
    """
    label   = STRATEGY_LABELS.get(strategy, strategy)
    now     = datetime.now()
    time_   = now.strftime("%H:%M")
    today   = now.strftime("%Y-%m-%d")
    won     = pnl_pct > 0
    cls     = "buy" if won else "sell"
    result  = "WIN ✓" if won else "LOSS ✗"
    col     = "#3fb950" if won else "#f85149"
    sign    = "+" if pnl_pct >= 0 else ""
    # P&L in the pair's own quote currency (last 3 chars, standard FX convention:
    # EURUSD -> USD, USDJPY -> JPY, GBPCHF -> CHF) — NOT a fixed SEK rate, which
    # only ever made sense for LBO's mostly-EUR-quoted book and would silently
    # show a wrong number once every strategy/pair calls this.
    quote_ccy = symbol[-3:] if len(symbol) >= 6 else ""
    pnl_native = round((exit_px - entry) * units if direction == "Buy"
                       else (entry - exit_px) * units, 0)
    pnl_sgn = "+" if pnl_native >= 0 else ""
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
        <div class="lbl">P&L ({quote_ccy})</div>
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
    subject = (f"{label} {'✅' if won else '❌'} {symbol} {result} {sign}{pnl_pct:.2f}% "
               f"({pnl_sgn}{pnl_native:,.0f} {quote_ccy}) [{time_} PKT]")
    _send(subject, _wrap(f"{label} Closed — {symbol} {result}", body))


def send_lbo_trade_closed(**kwargs) -> None:
    """Backward-compatible alias — send_trade_closed() replaced this 2026-08-21."""
    send_trade_closed(strategy="london_breakout", **kwargs)
