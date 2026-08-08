"""
atos/notifier.py
-----------------
Email notifications for ATOS trading signals.

Reads credentials from config/email.json (gitignored — never committed).
If that file is missing, notifications are silently skipped — the trading
system continues normally without email.

Setup:
  1. Enable 2-Factor Authentication on your Google account
  2. Go to: https://myaccount.google.com/apppasswords
  3. Create an App Password for "ATOS Trading"
  4. Fill in config/email.json with that 16-char password
"""
import smtplib
import json
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import date, datetime

_CFG = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "email.json")

# Track last sent to prevent duplicate emails on the same calendar day
_last_blend_date: str = ""
_last_blend_targets: list = []


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
        msg["From"] = f"ATOS Trading <{cfg['sender_email']}>"
        msg["To"]   = cfg["recipient_email"]
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"]) as s:
            s.starttls()
            s.login(cfg["sender_email"], cfg["sender_password"])
            s.sendmail(cfg["sender_email"], cfg["recipient_email"], msg.as_string())
        print(f"  [notifier] email sent: {subject}")
        return True
    except Exception as e:
        print(f"  [notifier] email failed: {e}")
        return False


# ── Email templates ───────────────────────────────────────────────────────────

_STYLE = """
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background:#0f0f13; color:#e2e8f0; margin:0; padding:24px; }
  .wrap { max-width:620px; margin:0 auto; }
  .header { background:linear-gradient(135deg,#1a1a2e,#16213e);
            border-radius:12px 12px 0 0; padding:24px 28px; border-bottom:2px solid #2563eb; }
  .logo  { font-size:22px; font-weight:700; color:#60a5fa; letter-spacing:2px; }
  .strat { font-size:13px; color:#94a3b8; margin-top:4px; }
  .body  { background:#1a1a2e; border-radius:0 0 12px 12px; padding:24px 28px; }
  .badge { display:inline-block; padding:4px 12px; border-radius:20px;
           font-size:12px; font-weight:700; letter-spacing:1px; }
  .buy   { background:#16a34a22; color:#4ade80; border:1px solid #16a34a; }
  .sell  { background:#dc262622; color:#f87171; border:1px solid #dc2626; }
  .watch { background:#d9770622; color:#fb923c; border:1px solid #d97706; }
  .blend { background:#1d4ed822; color:#60a5fa; border:1px solid #1d4ed8; }
  table  { width:100%; border-collapse:collapse; margin:16px 0; }
  th     { background:#0f172a; color:#94a3b8; font-size:12px;
           text-transform:uppercase; letter-spacing:.5px; padding:10px 12px;
           text-align:left; }
  td     { padding:10px 12px; border-bottom:1px solid #1e293b; font-size:14px; }
  tr:last-child td { border-bottom:none; }
  .ticker { font-weight:700; font-size:16px; color:#f1f5f9; }
  .muted  { color:#64748b; font-size:12px; }
  .footer { text-align:center; color:#334155; font-size:11px; margin-top:16px; }
  .divider { border:none; border-top:1px solid #1e293b; margin:16px 0; }
  .metric-row { display:flex; gap:24px; margin:12px 0; flex-wrap:wrap; }
  .metric     { background:#0f172a; border-radius:8px; padding:12px 16px; flex:1; min-width:100px; }
  .metric .label { font-size:11px; color:#64748b; text-transform:uppercase; letter-spacing:.5px; }
  .metric .value { font-size:20px; font-weight:700; color:#f1f5f9; margin-top:4px; }
  a { color:#60a5fa; }
"""


def _wrap(title: str, body_html: str) -> str:
    now = datetime.now().strftime("%A %d %b %Y  %H:%M PKT")
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>{_STYLE}</style></head><body><div class="wrap">
  <div class="header">
    <div class="logo">ATOS</div>
    <div class="strat">Adaptive Trading Operations System &nbsp;·&nbsp; {now}</div>
  </div>
  <div class="body">
    <h2 style="margin:0 0 16px; font-size:18px; color:#f1f5f9">{title}</h2>
    {body_html}
    <hr class="divider">
    <div class="footer">ATOS Trading &nbsp;·&nbsp; Auto-generated &nbsp;·&nbsp; Do not reply</div>
  </div>
</div></body></html>"""


# ── Public API ────────────────────────────────────────────────────────────────

def notify_blend_targets(
    targets: list,        # tickers to hold this week, e.g. ["NVDA","AAPL","NEE","SO"]
    risk_off: bool,
    reason: str,
    momentum_tickers: list,   # offense sleeve
    lowvol_tickers: list,     # defense sleeve
    sleeve_sek: float,
) -> None:
    """Send weekly Blend rebalance email when targets change."""
    global _last_blend_date, _last_blend_targets

    today = date.today().isoformat()
    if today == _last_blend_date and sorted(targets) == sorted(_last_blend_targets):
        return  # already notified today with the same targets

    _last_blend_date    = today
    _last_blend_targets = list(targets)

    if risk_off:
        status_badge = '<span class="badge sell">RISK OFF — CASH</span>'
        status_note  = f"<p style='color:#94a3b8'>{reason}</p>"
        tickers_html = "<p style='color:#94a3b8'>No positions. System is in cash.</p>"
    else:
        status_badge = '<span class="badge blend">REBALANCING</span>'
        status_note  = f"<p style='color:#94a3b8'>{reason}</p>"

        def _rows(tklist, label, color):
            if not tklist:
                return ""
            rows = "".join(f"<tr><td class='ticker'>{t}</td>"
                           f"<td><span class='badge' style='background:{color}22;color:{color};border:1px solid {color}'>"
                           f"{label}</span></td></tr>" for t in tklist)
            return rows

        tickers_html = f"""
        <table>
          <thead><tr><th>Ticker</th><th>Sleeve</th></tr></thead>
          <tbody>
            {_rows(momentum_tickers, "OFFENSE", "#60a5fa")}
            {_rows(lowvol_tickers,   "DEFENSE", "#a78bfa")}
          </tbody>
        </table>"""

    body = f"""
    {status_badge}
    {status_note}
    {tickers_html}
    <div class="metric-row">
      <div class="metric">
        <div class="label">Sleeve</div>
        <div class="value">{sleeve_sek:,.0f} SEK</div>
      </div>
      <div class="metric">
        <div class="label">Positions</div>
        <div class="value">{len(targets)}</div>
      </div>
      <div class="metric">
        <div class="label">Risk Mode</div>
        <div class="value" style="color:{'#f87171' if risk_off else '#4ade80'}">
          {'OFF' if risk_off else 'ON'}
        </div>
      </div>
    </div>
    <p class="muted">Weekly Blend rebalances every Friday. Check the dashboard for order details.</p>
    """

    tickers_str = ", ".join(targets[:4])
    subject = f"ATOS Blend — {'RISK OFF (cash)' if risk_off else f'Targets: {tickers_str}'} [{today}]"
    _send(subject, _wrap("Weekly Blend — Rebalance Signal", body))


def notify_reversion_signal(
    candidates: list,   # list of dicts from USR.scan(): ticker, rsi, dip_pct, vol_ratio, price
    slots_free: int,
    sleeve_sek: float,
) -> None:
    """Send email when Mean Reversion entry signals fire."""
    if not candidates:
        return

    today = date.today().isoformat()
    tickers = [c["ticker"] for c in candidates]

    rows = ""
    for c in candidates:
        rsi      = c.get("rsi", 0)
        dip      = c.get("dip_pct", 0) * 100
        vol      = c.get("vol_ratio", 0)
        price    = c.get("price", 0)
        badge    = '<span class="badge buy">BUY SIGNAL</span>' if candidates.index(c) < slots_free else '<span class="badge watch">QUEUED</span>'
        rows += f"""<tr>
          <td class="ticker">{c['ticker']}</td>
          <td>{badge}</td>
          <td>${price:.2f}</td>
          <td style="color:#f87171">{rsi:.1f}</td>
          <td style="color:#fb923c">{dip:.1f}%</td>
          <td style="color:#4ade80">{vol:.1f}x</td>
        </tr>"""

    body = f"""
    <span class="badge buy">ENTRY SIGNAL</span>
    <p style="color:#94a3b8">
      {len(candidates)} stock(s) triggered the Mean Reversion entry conditions.<br>
      {slots_free} slot(s) available — top {min(len(candidates), slots_free)} will be executed at next market open.
    </p>
    <table>
      <thead>
        <tr>
          <th>Ticker</th><th>Action</th><th>Price</th>
          <th>RSI</th><th>Dip%</th><th>Vol Ratio</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
    <div class="metric-row">
      <div class="metric">
        <div class="label">Sleeve</div>
        <div class="value">{sleeve_sek:,.0f} SEK</div>
      </div>
      <div class="metric">
        <div class="label">Signals</div>
        <div class="value" style="color:#4ade80">{len(candidates)}</div>
      </div>
      <div class="metric">
        <div class="label">Executing</div>
        <div class="value" style="color:#60a5fa">{min(len(candidates), slots_free)}</div>
      </div>
    </div>
    <p class="muted">
      Entry criteria: Price above EMA200 &nbsp;·&nbsp; RSI &lt; 33 &nbsp;·&nbsp;
      Dip &gt; 5% from SMA20 &nbsp;·&nbsp; Volume &gt; 1.5x average
    </p>
    """

    subject = f"ATOS Reversion SIGNAL — {', '.join(tickers[:3])} [{today}]"
    _send(subject, _wrap("Mean Reversion — Entry Signal", body))


def notify_reversion_exit(ticker: str, pnl_sek: float, reason: str, hold_days: int) -> None:
    """Send email when a Mean Reversion position exits."""
    today    = date.today().isoformat()
    pnl_col  = "#4ade80" if pnl_sek >= 0 else "#f87171"
    pnl_sign = "+" if pnl_sek >= 0 else ""

    body = f"""
    <span class="badge {'buy' if pnl_sek >= 0 else 'sell'}">
      {'PROFIT' if pnl_sek >= 0 else 'LOSS'}
    </span>
    <div class="metric-row" style="margin-top:16px">
      <div class="metric">
        <div class="label">Ticker</div>
        <div class="value">{ticker}</div>
      </div>
      <div class="metric">
        <div class="label">P&amp;L</div>
        <div class="value" style="color:{pnl_col}">{pnl_sign}{pnl_sek:,.0f} SEK</div>
      </div>
      <div class="metric">
        <div class="label">Hold</div>
        <div class="value">{hold_days}d</div>
      </div>
    </div>
    <p style="color:#94a3b8; margin-top:12px">Exit reason: <strong>{reason}</strong></p>
    """

    subject = f"ATOS Reversion EXIT — {ticker}  {pnl_sign}{pnl_sek:,.0f} SEK [{today}]"
    _send(subject, _wrap("Mean Reversion — Position Closed", body))
