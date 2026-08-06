"""
atos/dashboard_gen.py
----------------------
Generates a self-contained static HTML dashboard file.
No server required — open locally or upload to namazic.com/atos/

All charts use Chart.js (loaded from CDN when online, graceful fallback offline).
All data is baked directly into the HTML — no database connection from browser.
"""

import json
import os
from datetime import datetime

from . import database as db
from .learner import format_weight_bar
from .universe import MARKET_GROUPS

DASHBOARD_DIR  = os.path.join(os.path.dirname(os.path.dirname(__file__)), "dashboard")
DASHBOARD_FILE = os.path.join(DASHBOARD_DIR, "index.html")


def _pct(value: float, reference: float) -> str:
    if not reference:
        return "0.00%"
    p = (value - reference) / reference * 100
    sign = "+" if p >= 0 else ""
    return f"{sign}{p:.2f}%"


def _color(value: float) -> str:
    return "#4ade80" if value >= 0 else "#f87171"


def generate(
    todays_actions: list[dict],
    open_trades:    list[dict],
    run_summary:    dict,
):
    """
    Build the HTML dashboard.

    Parameters
    ----------
    todays_actions : list of dicts with keys:
        action, ticker, market_group, score, shares, price, reason, pnl_sek
    open_trades    : list of open trade dicts from DB
    run_summary    : dict with keys:
        total_equity_sek, day_start_equity, trades_today, errors
    """
    os.makedirs(DASHBOARD_DIR, exist_ok=True)

    weights      = db.get_current_weights()
    equity_curve = db.get_equity_curve(days=90)
    closed       = db.get_all_closed_trades()
    weight_hist  = db.get_weight_history()

    starting_cap = 10_000  # from risk.py STARTING_CAPITAL_SEK
    total_equity = run_summary.get("total_equity_sek", starting_cap)
    day_start    = run_summary.get("day_start_equity", total_equity)
    today_pnl    = total_equity - day_start
    num_trades   = weights.get("num_trades", 0)
    now_str      = datetime.now().strftime("%A %Y-%m-%d  %H:%M PKT")

    # ── Compute stats ──────────────────────────────────────────────
    total_pnl_pct = _pct(total_equity, starting_cap)
    day_pnl_color = _color(today_pnl)
    total_color   = _color(total_equity - starting_cap)

    win_rate = profit_factor = 0.0
    if closed:
        wins   = [t for t in closed if (t.get("pnl_sek") or 0) > 0]
        losses = [t for t in closed if (t.get("pnl_sek") or 0) <= 0]
        win_rate = len(wins) / len(closed) * 100 if closed else 0
        avg_win  = sum(t["pnl_sek"] for t in wins)   / max(len(wins),   1)
        avg_loss = abs(sum(t["pnl_sek"] for t in losses)) / max(len(losses), 1)
        profit_factor = avg_win / avg_loss if avg_loss else 0

    # ── Chart data ─────────────────────────────────────────────────
    eq_labels = [r["snap_date"] for r in equity_curve]
    eq_values = [round(r["total_equity_sek"], 2) for r in equity_curve]

    wh_labels   = [f"#{r['num_trades_used']}" for r in weight_hist]
    wh_trend    = [r["w_trend"]       for r in weight_hist]
    wh_momentum = [r["w_momentum"]    for r in weight_hist]
    wh_breakout = [r["w_breakout"]    for r in weight_hist]
    wh_meanrev  = [r["w_mean_revert"] for r in weight_hist]
    wh_volume   = [r["w_volume"]      for r in weight_hist]

    # ── Per-market equity ──────────────────────────────────────────
    mkt_equity = {}
    for t in open_trades:
        mkt = t.get("market_group", "Unknown")
        val = (t.get("shares", 0) or 0) * (t.get("entry_price", 0) or 0)
        mkt_equity[mkt] = mkt_equity.get(mkt, 0) + val

    # ── Build actions HTML ─────────────────────────────────────────
    action_rows = ""
    for a in todays_actions:
        action  = a.get("action", "")
        ticker  = a.get("ticker", "")
        score   = a.get("score", 0)
        shares  = a.get("shares", 0)
        price   = a.get("price", 0)
        reason  = a.get("reason", "")
        pnl     = a.get("pnl_sek")
        mkt     = a.get("market_group", "")

        if action == "BUY":
            badge = '<span class="badge badge-buy">BUY</span>'
        elif action == "EXIT":
            pnl_str = f'<span style="color:{_color(pnl or 0)}">{("+" if (pnl or 0)>=0 else "")}{(pnl or 0):.0f} SEK</span>'
            badge = f'<span class="badge badge-exit">EXIT</span>'
        elif action == "BLOCKED":
            badge = '<span class="badge badge-blocked">BLOCKED</span>'
        else:
            badge = f'<span class="badge">{action}</span>'

        pnl_cell = ""
        if pnl is not None:
            pnl_cell = f'<td style="color:{_color(pnl)}">{("+" if pnl>=0 else "")}{pnl:.0f} SEK</td>'
        else:
            pnl_cell = "<td>—</td>"

        action_rows += f"""
        <tr>
          <td>{badge}</td>
          <td><strong>{ticker}</strong></td>
          <td class="muted">{mkt}</td>
          <td><span class="score-pill" style="background:{_score_color(score)}">{score}</span></td>
          <td>{shares} sh @ {price:.2f}</td>
          {pnl_cell}
          <td class="muted small">{reason}</td>
        </tr>"""

    if not action_rows:
        action_rows = '<tr><td colspan="7" class="muted" style="text-align:center">No actions today</td></tr>'

    # ── Open positions HTML ────────────────────────────────────────
    pos_rows = ""
    for t in open_trades:
        ticker     = t.get("ticker", "")
        entry      = t.get("entry_price", 0) or 0
        stop       = t.get("stop_price", 0) or 0
        shares     = t.get("shares", 0) or 0
        entry_date = t.get("entry_date", "")
        mkt        = t.get("market_group", "")
        score      = t.get("entry_score", 0) or 0
        pos_rows += f"""
        <tr>
          <td><strong>{ticker}</strong></td>
          <td class="muted">{mkt}</td>
          <td>{shares}</td>
          <td>{entry:.3f}</td>
          <td style="color:#f87171">{stop:.3f}</td>
          <td class="muted">{entry_date}</td>
          <td><span class="score-pill" style="background:{_score_color(score)}">{score}</span></td>
        </tr>"""

    if not pos_rows:
        pos_rows = '<tr><td colspan="7" class="muted" style="text-align:center">No open positions</td></tr>'

    # ── Weight bars ────────────────────────────────────────────────
    def wbar(name, key, emoji):
        w = weights.get(key, 1.0)
        pct = min(int((w / 2.5) * 100), 100)
        return f"""
        <div class="weight-row">
          <span class="weight-label">{emoji} {name}</span>
          <div class="weight-bar-bg">
            <div class="weight-bar-fill" style="width:{pct}%"></div>
          </div>
          <span class="weight-val">{w:.3f}</span>
        </div>"""

    weight_bars = (
        wbar("Trend",       "w_trend",       "📈") +
        wbar("Momentum",    "w_momentum",    "⚡") +
        wbar("Breakout",    "w_breakout",    "🚀") +
        wbar("Mean Revert", "w_mean_revert", "🔄") +
        wbar("Volume",      "w_volume",      "📊")
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>ATOS — Algo Trading Dashboard | namazic.com</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    :root {{
      --bg:       #0f1117;
      --surface:  #1a1d2e;
      --surface2: #252840;
      --border:   #2e3150;
      --text:     #e2e8f0;
      --muted:    #8892aa;
      --green:    #4ade80;
      --red:      #f87171;
      --blue:     #60a5fa;
      --purple:   #a78bfa;
      --yellow:   #fbbf24;
      --accent:   #7c3aed;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif;
            font-size: 14px; line-height: 1.6; min-height: 100vh; }}
    .header {{
      background: linear-gradient(135deg, #1a1d2e 0%, #252840 50%, #1a1d2e 100%);
      border-bottom: 1px solid var(--border);
      padding: 20px 32px;
      display: flex; align-items: center; justify-content: space-between;
    }}
    .header h1 {{ font-size: 22px; font-weight: 700; color: var(--text); letter-spacing: -0.5px; }}
    .header h1 span {{ color: var(--purple); }}
    .header .subtitle {{ color: var(--muted); font-size: 12px; margin-top: 2px; }}
    .timestamp {{ color: var(--muted); font-size: 12px; text-align: right; }}
    .main {{ padding: 24px 32px; max-width: 1400px; margin: 0 auto; }}
    .grid-4 {{ display: grid; grid-template-columns: repeat(4,1fr); gap: 16px; margin-bottom: 20px; }}
    .grid-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 20px; }}
    .grid-3 {{ display: grid; grid-template-columns: 2fr 1fr; gap: 16px; margin-bottom: 20px; }}
    .card {{
      background: var(--surface); border: 1px solid var(--border);
      border-radius: 12px; padding: 20px;
    }}
    .card-title {{ color: var(--muted); font-size: 11px; font-weight: 600;
                   text-transform: uppercase; letter-spacing: 1px; margin-bottom: 8px; }}
    .metric {{ font-size: 28px; font-weight: 700; }}
    .metric-sub {{ font-size: 13px; color: var(--muted); margin-top: 2px; }}
    .green {{ color: var(--green); }}
    .red   {{ color: var(--red); }}
    .blue  {{ color: var(--blue); }}
    .muted {{ color: var(--muted); }}
    .small {{ font-size: 12px; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th {{ color: var(--muted); font-size: 11px; font-weight: 600; text-transform: uppercase;
          letter-spacing: 0.5px; padding: 8px 12px; border-bottom: 1px solid var(--border);
          text-align: left; }}
    td {{ padding: 10px 12px; border-bottom: 1px solid rgba(46,49,80,0.5); vertical-align: middle; }}
    tr:last-child td {{ border-bottom: none; }}
    tr:hover td {{ background: rgba(255,255,255,0.02); }}
    .badge {{ display: inline-block; padding: 2px 10px; border-radius: 20px;
              font-size: 11px; font-weight: 700; letter-spacing: 0.5px; }}
    .badge-buy     {{ background: rgba(74,222,128,0.15); color: var(--green); border: 1px solid rgba(74,222,128,0.3); }}
    .badge-exit    {{ background: rgba(248,113,113,0.15); color: var(--red);   border: 1px solid rgba(248,113,113,0.3); }}
    .badge-blocked {{ background: rgba(251,191,36,0.15);  color: var(--yellow);border: 1px solid rgba(251,191,36,0.3); }}
    .score-pill {{ display: inline-block; padding: 2px 8px; border-radius: 12px;
                   font-size: 12px; font-weight: 700; color: #fff; }}
    .weight-row {{ display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }}
    .weight-label {{ width: 110px; font-size: 13px; color: var(--text); }}
    .weight-bar-bg {{ flex: 1; background: var(--surface2); border-radius: 6px; height: 8px; }}
    .weight-bar-fill {{ height: 8px; border-radius: 6px;
                        background: linear-gradient(90deg, var(--purple), var(--blue)); }}
    .weight-val {{ width: 46px; text-align: right; font-size: 13px;
                   font-weight: 700; color: var(--blue); font-family: monospace; }}
    canvas {{ max-height: 280px; }}
    .trade-count {{ display: inline-block; background: var(--surface2);
                    border-radius: 20px; padding: 2px 10px; font-size: 12px; color: var(--muted); }}
    .status-dot {{ display: inline-block; width: 8px; height: 8px; border-radius: 50%;
                   background: var(--green); margin-right: 6px;
                   box-shadow: 0 0 6px var(--green); animation: pulse 2s infinite; }}
    @keyframes pulse {{ 0%,100%{{opacity:1}} 50%{{opacity:.4}} }}
    .section-title {{ font-size: 15px; font-weight: 700; margin-bottom: 14px;
                      color: var(--text); display: flex; align-items: center; gap: 8px; }}
    @media(max-width:900px){{.grid-4,.grid-2,.grid-3{{grid-template-columns:1fr;}}}}
  </style>
</head>
<body>

<div class="header">
  <div>
    <h1><span>ATOS</span> — Algorithmic Trading OS</h1>
    <div class="subtitle"><span class="status-dot"></span>Saxo SIM Paper Trading &nbsp;·&nbsp; namazic.com/atos</div>
  </div>
  <div class="timestamp">{now_str}<br><span class="muted">Auto-generated each daily run</span></div>
</div>

<div class="main">

  <!-- KPI Row -->
  <div class="grid-4">
    <div class="card">
      <div class="card-title">Total Equity</div>
      <div class="metric" style="color:{_color(total_equity - starting_cap)}">{total_equity:,.0f} SEK</div>
      <div class="metric-sub" style="color:{_color(total_equity - starting_cap)}">{total_pnl_pct} from start</div>
    </div>
    <div class="card">
      <div class="card-title">Today's P&L</div>
      <div class="metric" style="color:{day_pnl_color}">{("+" if today_pnl>=0 else "")}{today_pnl:,.0f} SEK</div>
      <div class="metric-sub">{run_summary.get("trades_today",0)} trades executed today</div>
    </div>
    <div class="card">
      <div class="card-title">Open Positions</div>
      <div class="metric blue">{len(open_trades)}</div>
      <div class="metric-sub">of 10 max slots used</div>
    </div>
    <div class="card">
      <div class="card-title">Algorithm Progress</div>
      <div class="metric" style="color:var(--purple)">{num_trades}</div>
      <div class="metric-sub">trades learned from &nbsp;·&nbsp;
        WR {win_rate:.0f}% &nbsp;·&nbsp; PF {profit_factor:.2f}</div>
    </div>
  </div>

  <!-- Equity Curve + Weights -->
  <div class="grid-3">
    <div class="card">
      <div class="section-title">📈 Equity Curve (90 days)</div>
      <canvas id="equityChart"></canvas>
    </div>
    <div class="card">
      <div class="section-title">🧠 Algorithm Weights
        <span class="trade-count">{num_trades} trades</span>
      </div>
      {weight_bars}
      <div style="margin-top:16px; padding-top:14px; border-top:1px solid var(--border);">
        <div class="card-title">Weight Evolution</div>
        <canvas id="weightChart"></canvas>
      </div>
    </div>
  </div>

  <!-- Today's Actions -->
  <div class="card" style="margin-bottom:20px">
    <div class="section-title">⚡ Today's Actions</div>
    <table>
      <thead><tr>
        <th>Action</th><th>Ticker</th><th>Market</th>
        <th>Score</th><th>Size</th><th>P&L</th><th>Reason</th>
      </tr></thead>
      <tbody>{action_rows}</tbody>
    </table>
  </div>

  <!-- Open Positions -->
  <div class="card" style="margin-bottom:20px">
    <div class="section-title">📂 Open Positions ({len(open_trades)})</div>
    <table>
      <thead><tr>
        <th>Ticker</th><th>Market</th><th>Shares</th>
        <th>Entry Price</th><th>Stop Loss</th><th>Entry Date</th><th>Score at Entry</th>
      </tr></thead>
      <tbody>{pos_rows}</tbody>
    </table>
  </div>

  <!-- Footer -->
  <div style="text-align:center; color:var(--muted); font-size:12px; padding: 20px 0 10px;">
    ATOS v1 &nbsp;·&nbsp; Saxo SIM (Paper Money Only) &nbsp;·&nbsp;
    namazic.com &nbsp;·&nbsp; Not financial advice
  </div>

</div>

<script>
const eq_labels  = {json.dumps(eq_labels)};
const eq_values  = {json.dumps(eq_values)};
const wh_labels  = {json.dumps(wh_labels)};
const wh_trend   = {json.dumps(wh_trend)};
const wh_mom     = {json.dumps(wh_momentum)};
const wh_break   = {json.dumps(wh_breakout)};
const wh_mr      = {json.dumps(wh_meanrev)};
const wh_vol     = {json.dumps(wh_volume)};

// Equity chart
if (eq_labels.length > 1) {{
  new Chart(document.getElementById('equityChart'), {{
    type: 'line',
    data: {{
      labels: eq_labels,
      datasets: [{{
        label: 'Equity (SEK)',
        data: eq_values,
        borderColor: '#7c3aed',
        backgroundColor: 'rgba(124,58,237,0.08)',
        fill: true,
        tension: 0.3,
        pointRadius: 0,
        borderWidth: 2,
      }}]
    }},
    options: {{
      responsive: true, maintainAspectRatio: true,
      plugins: {{ legend: {{ display: false }} }},
      scales: {{
        x: {{ ticks: {{ color:'#8892aa', maxTicksLimit:8 }}, grid: {{ color:'rgba(255,255,255,0.04)' }} }},
        y: {{ ticks: {{ color:'#8892aa' }}, grid: {{ color:'rgba(255,255,255,0.04)' }} }}
      }}
    }}
  }});
}}

// Weight evolution chart
if (wh_labels.length > 1) {{
  new Chart(document.getElementById('weightChart'), {{
    type: 'line',
    data: {{
      labels: wh_labels,
      datasets: [
        {{ label:'Trend',      data:wh_trend,  borderColor:'#7c3aed', tension:0.3, pointRadius:0, borderWidth:2 }},
        {{ label:'Momentum',   data:wh_mom,    borderColor:'#60a5fa', tension:0.3, pointRadius:0, borderWidth:2 }},
        {{ label:'Breakout',   data:wh_break,  borderColor:'#4ade80', tension:0.3, pointRadius:0, borderWidth:2 }},
        {{ label:'Mean Rev',   data:wh_mr,     borderColor:'#fbbf24', tension:0.3, pointRadius:0, borderWidth:2 }},
        {{ label:'Volume',     data:wh_vol,    borderColor:'#f87171', tension:0.3, pointRadius:0, borderWidth:2 }},
      ]
    }},
    options: {{
      responsive: true, maintainAspectRatio: true,
      plugins: {{ legend: {{ labels: {{ color:'#8892aa', boxWidth:10, font:{{size:11}} }} }} }},
      scales: {{
        x: {{ ticks: {{ color:'#8892aa', maxTicksLimit:6 }}, grid: {{ color:'rgba(255,255,255,0.04)' }} }},
        y: {{ ticks: {{ color:'#8892aa' }}, grid: {{ color:'rgba(255,255,255,0.04)' }}, min:0, max:2.6 }}
      }}
    }}
  }});
}}
</script>
</body>
</html>"""

    with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
        f.write(html)

    return DASHBOARD_FILE


def _score_color(score: float) -> str:
    if score >= 70:
        return "#16a34a"
    elif score >= 55:
        return "#2563eb"
    elif score >= 0:
        return "#78716c"
    else:
        return "#dc2626"
