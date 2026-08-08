"""
atos/dashboard_gen.py
----------------------
Generates a self-contained static HTML dashboard file.
No server required — open locally or upload to namazic.com/atos/

All charts use Chart.js (loaded from CDN when online, graceful fallback offline).
All data is baked directly into the HTML — no database connection from browser.
"""

import csv
import json
import os
from datetime import datetime

from . import database as db
from .learner import format_weight_bar
from .universe import MARKET_GROUPS

DASHBOARD_DIR  = os.path.join(os.path.dirname(os.path.dirname(__file__)), "dashboard")
DASHBOARD_FILE = os.path.join(DASHBOARD_DIR, "index.html")
TRADE_LOG_CSV  = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "trade_log.csv")


def _pct(value: float, reference: float) -> str:
    if not reference:
        return "0.00%"
    p = (value - reference) / reference * 100
    sign = "+" if p >= 0 else ""
    return f"{sign}{p:.2f}%"


def _color(value: float) -> str:
    return "#4ade80" if value >= 0 else "#f87171"


def _read_trade_log(n: int = 30) -> list[dict]:
    """Read last n rows from data/trade_log.csv. Returns [] if file missing."""
    if not os.path.exists(TRADE_LOG_CSV):
        return []
    try:
        with open(TRADE_LOG_CSV, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        return rows[-n:][::-1]  # most recent first
    except Exception:
        return []


def _strategy_badge(strategy: str) -> str:
    s = strategy or "ATOS"
    if "Reversion" in s:
        return f'<span class="badge badge-rev">{s}</span>'
    elif "Blend" in s or "Momentum" in s:
        return f'<span class="badge badge-blend">{s}</span>'
    return f'<span class="badge badge-atos">{s}</span>'


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
        action, ticker, market_group, strategy, score, shares, price, reason, pnl_sek
    open_trades    : list of open trade dicts from DB
    run_summary    : dict with keys:
        total_equity_sek, day_start_equity, trades_today, errors
    """
    os.makedirs(DASHBOARD_DIR, exist_ok=True)

    weights      = db.get_current_weights()
    equity_curve = db.get_equity_curve(days=90)
    closed       = db.get_all_closed_trades()
    weight_hist  = db.get_weight_history()
    trade_log    = _read_trade_log(30)

    starting_cap = 10_000  # from risk.py STARTING_CAPITAL_SEK
    total_equity = run_summary.get("total_equity_sek", starting_cap)
    day_start    = run_summary.get("day_start_equity", total_equity)
    today_pnl    = total_equity - day_start
    num_trades   = weights.get("num_trades", 0)
    now_str      = datetime.now().strftime("%A %Y-%m-%d  %H:%M PKT")

    # ── Compute stats ──────────────────────────────────────────────
    total_pnl_pct = _pct(total_equity, starting_cap)
    day_pnl_color = _color(today_pnl)

    win_rate = profit_factor = 0.0
    if closed:
        wins   = [t for t in closed if (t.get("pnl_sek") or 0) > 0]
        losses = [t for t in closed if (t.get("pnl_sek") or 0) <= 0]
        win_rate = len(wins) / len(closed) * 100 if closed else 0
        avg_win  = sum(t["pnl_sek"] for t in wins)   / max(len(wins),   1)
        avg_loss = abs(sum(t["pnl_sek"] for t in losses)) / max(len(losses), 1)
        profit_factor = avg_win / avg_loss if avg_loss else 0

    # ── Per-strategy stats ─────────────────────────────────────────
    def _strat_stats(name_fragment):
        trades = [t for t in closed if name_fragment in (t.get("strategy") or "")]
        if not trades:
            return dict(n=0, wr=0.0, pnl=0.0, avg_win=0.0, avg_loss=0.0, best=0.0, worst=0.0)
        wins   = [t for t in trades if (t.get("pnl_sek") or 0) > 0]
        losses = [t for t in trades if (t.get("pnl_sek") or 0) <= 0]
        pnl_list = [(t.get("pnl_sek") or 0) for t in trades]
        return dict(
            n     = len(trades),
            wr    = len(wins) / len(trades) * 100,
            pnl   = sum(pnl_list),
            avg_win  = sum(t["pnl_sek"] for t in wins)   / max(len(wins), 1),
            avg_loss = sum(t["pnl_sek"] for t in losses)  / max(len(losses), 1),
            best  = max(pnl_list) if pnl_list else 0,
            worst = min(pnl_list) if pnl_list else 0,
        )

    blend_s = _strat_stats("Blend")
    rev_s   = _strat_stats("Reversion")
    blend_n, blend_wr, blend_pnl = blend_s["n"], blend_s["wr"], blend_s["pnl"]
    rev_n,   rev_wr,   rev_pnl   = rev_s["n"],   rev_s["wr"],   rev_s["pnl"]

    # ── Cumulative P&L per strategy from trade_log.csv ────────────
    all_log = _read_trade_log(500)   # all available history
    def _cumulative_pnl(name_fragment):
        rows = [r for r in reversed(all_log)   # all_log is newest-first
                if name_fragment in (r.get("strategy") or "")
                and r.get("action") == "SELL"]
        dates, cum = [], []
        running = 0.0
        for r in rows:
            try:
                running += float(r.get("pnl_sek") or 0)
                dates.append(r.get("date", ""))
                cum.append(round(running, 2))
            except (ValueError, TypeError):
                pass
        return dates, cum

    blend_pnl_dates, blend_pnl_cum = _cumulative_pnl("Blend")
    rev_pnl_dates,   rev_pnl_cum   = _cumulative_pnl("Reversion")

    # Sleeve status
    rev_open   = [t for t in open_trades if "Reversion" in (t.get("strategy") or "")]
    blend_open = [t for t in open_trades if "Blend" in (t.get("strategy") or "")
                  or t.get("market_group") == "US Equities"
                  and "Reversion" not in (t.get("strategy") or "")]
    rev_slots_used = len(rev_open)

    # ── Chart data ─────────────────────────────────────────────────
    eq_labels = [r["snap_date"] for r in equity_curve]
    eq_values = [round(r["total_equity_sek"], 2) for r in equity_curve]

    wh_labels   = [f"#{r['num_trades_used']}" for r in weight_hist]
    wh_trend    = [r["w_trend"]       for r in weight_hist]
    wh_momentum = [r["w_momentum"]    for r in weight_hist]
    wh_breakout = [r["w_breakout"]    for r in weight_hist]
    wh_meanrev  = [r["w_mean_revert"] for r in weight_hist]
    wh_volume   = [r["w_volume"]      for r in weight_hist]

    # ── Build actions HTML ─────────────────────────────────────────
    action_rows = ""
    for a in todays_actions:
        action   = a.get("action", "")
        ticker   = a.get("ticker", "")
        score    = a.get("score", 0)
        shares   = a.get("shares", 0)
        price    = a.get("price", 0)
        reason   = a.get("reason", "")
        pnl      = a.get("pnl_sek")
        mkt      = a.get("market_group", "")
        strategy = a.get("strategy", "ATOS")

        if action == "BUY":
            badge = '<span class="badge badge-buy">BUY</span>'
        elif action in ("EXIT", "SELL"):
            badge = '<span class="badge badge-exit">SELL</span>'
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
          <td>{_strategy_badge(strategy)}</td>
          <td class="muted">{mkt}</td>
          <td><span class="score-pill" style="background:{_score_color(score)}">{score}</span></td>
          <td>{shares} sh @ {price:.2f}</td>
          {pnl_cell}
          <td class="muted small">{reason}</td>
        </tr>"""

    if not action_rows:
        action_rows = '<tr><td colspan="8" class="muted" style="text-align:center">No actions today</td></tr>'

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
        strategy   = t.get("strategy", "ATOS")
        pos_rows += f"""
        <tr>
          <td><strong>{ticker}</strong></td>
          <td>{_strategy_badge(strategy)}</td>
          <td class="muted">{mkt}</td>
          <td>{shares}</td>
          <td>{entry:.3f}</td>
          <td style="color:#f87171">{f'{stop:.3f}' if stop else '—'}</td>
          <td class="muted">{entry_date}</td>
          <td><span class="score-pill" style="background:{_score_color(score)}">{score}</span></td>
        </tr>"""

    if not pos_rows:
        pos_rows = '<tr><td colspan="8" class="muted" style="text-align:center">No open positions</td></tr>'

    # ── Trade history from CSV ─────────────────────────────────────
    hist_rows = ""
    for row in trade_log:
        action   = row.get("action", "")
        ticker   = row.get("ticker", "")
        strategy = row.get("strategy", "")
        shares   = row.get("shares", "")
        price    = row.get("price_usd", "")
        val      = row.get("value_sek", "")
        pnl_raw  = row.get("pnl_sek", "")
        reason   = row.get("reason", "")
        date_s   = row.get("date", "")
        held     = row.get("days_held", "")

        if action == "BUY":
            abadge = '<span class="badge badge-buy">BUY</span>'
        else:
            abadge = '<span class="badge badge-exit">SELL</span>'

        try:
            pnl_f = float(pnl_raw) if pnl_raw else None
        except ValueError:
            pnl_f = None

        if pnl_f is not None:
            pnl_cell = f'<td style="color:{_color(pnl_f)}">{("+" if pnl_f>=0 else "")}{pnl_f:,.0f} SEK</td>'
        else:
            pnl_cell = "<td>—</td>"

        try:
            val_fmt = f"{float(val):,.0f}" if val else "—"
        except ValueError:
            val_fmt = val

        hist_rows += f"""
        <tr>
          <td class="muted small">{date_s}</td>
          <td>{abadge}</td>
          <td><strong>{ticker}</strong></td>
          <td>{_strategy_badge(strategy)}</td>
          <td class="muted">{shares} sh @ ${price}</td>
          <td class="muted">{val_fmt} SEK</td>
          {pnl_cell}
          <td class="muted small">{held}d</td>
          <td class="muted small">{reason[:40]}</td>
        </tr>"""

    if not hist_rows:
        hist_rows = '<tr><td colspan="9" class="muted" style="text-align:center">No trade history yet</td></tr>'

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

    # ── Strategy sleeve cards ──────────────────────────────────────
    blend_pnl_s = f'{"+" if blend_pnl>=0 else ""}{blend_pnl:,.0f} SEK'
    rev_pnl_s   = f'{"+" if rev_pnl>=0 else ""}{rev_pnl:,.0f} SEK'

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
      --orange:   #fb923c;
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
    .badge-buy     {{ background: rgba(74,222,128,0.15);  color: var(--green);  border: 1px solid rgba(74,222,128,0.3); }}
    .badge-exit    {{ background: rgba(248,113,113,0.15); color: var(--red);    border: 1px solid rgba(248,113,113,0.3); }}
    .badge-blocked {{ background: rgba(251,191,36,0.15);  color: var(--yellow); border: 1px solid rgba(251,191,36,0.3); }}
    .badge-blend   {{ background: rgba(96,165,250,0.15);  color: var(--blue);   border: 1px solid rgba(96,165,250,0.3); }}
    .badge-rev     {{ background: rgba(251,146,60,0.15);  color: var(--orange); border: 1px solid rgba(251,146,60,0.3); }}
    .badge-atos    {{ background: rgba(167,139,250,0.15); color: var(--purple); border: 1px solid rgba(167,139,250,0.3); }}
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
    .sleeve-dot-blue   {{ background: var(--blue);   box-shadow: 0 0 6px var(--blue); }}
    .sleeve-dot-orange {{ background: var(--orange); box-shadow: 0 0 6px var(--orange); }}
    @keyframes pulse {{ 0%,100%{{opacity:1}} 50%{{opacity:.4}} }}
    .section-title {{ font-size: 15px; font-weight: 700; margin-bottom: 14px;
                      color: var(--text); display: flex; align-items: center; gap: 8px; }}
    .sleeve-bar-bg {{ background: var(--surface2); border-radius: 6px; height: 6px; margin-top: 8px; }}
    .sleeve-bar-fill-blue   {{ height: 6px; border-radius: 6px; background: var(--blue); }}
    .sleeve-bar-fill-orange {{ height: 6px; border-radius: 6px; background: var(--orange); }}
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
      <div class="metric-sub">{len(blend_open)} blend &nbsp;·&nbsp; {len(rev_open)} reversion</div>
    </div>
    <div class="card">
      <div class="card-title">Algorithm Progress</div>
      <div class="metric" style="color:var(--purple)">{num_trades}</div>
      <div class="metric-sub">trades learned &nbsp;·&nbsp; WR {win_rate:.0f}% &nbsp;·&nbsp; PF {profit_factor:.2f}</div>
    </div>
  </div>

  <!-- Strategy Sleeve Status -->
  <div class="grid-2">
    <div class="card">
      <div class="section-title">
        <span class="status-dot sleeve-dot-blue"></span>
        US Blend — Momentum Strategy
      </div>
      <div style="display:flex; gap:32px;">
        <div>
          <div class="card-title">Closed Trades</div>
          <div style="font-size:22px; font-weight:700; color:var(--blue)">{blend_n}</div>
        </div>
        <div>
          <div class="card-title">Win Rate</div>
          <div style="font-size:22px; font-weight:700; color:{'var(--green)' if blend_wr>=50 else 'var(--red)'}">
            {blend_wr:.0f}%</div>
        </div>
        <div>
          <div class="card-title">Total P&L</div>
          <div style="font-size:22px; font-weight:700; color:{_color(blend_pnl)}">{blend_pnl_s}</div>
        </div>
        <div>
          <div class="card-title">Open Positions</div>
          <div style="font-size:22px; font-weight:700; color:var(--blue)">{len(blend_open)}</div>
        </div>
      </div>
      <div class="sleeve-bar-bg" style="margin-top:14px">
        <div class="sleeve-bar-fill-blue" style="width:{min(len(blend_open)/8*100,100):.0f}%"></div>
      </div>
      <div class="muted small" style="margin-top:4px">~1,095,000 SEK sleeve &nbsp;·&nbsp; weekly rebalance</div>
    </div>
    <div class="card">
      <div class="section-title">
        <span class="status-dot sleeve-dot-orange"></span>
        US Reversion — Mean Reversion Strategy
      </div>
      <div style="display:flex; gap:32px;">
        <div>
          <div class="card-title">Closed Trades</div>
          <div style="font-size:22px; font-weight:700; color:var(--orange)">{rev_n}</div>
        </div>
        <div>
          <div class="card-title">Win Rate</div>
          <div style="font-size:22px; font-weight:700; color:{'var(--green)' if rev_wr>=50 else 'var(--red)'}">
            {rev_wr:.0f}%</div>
        </div>
        <div>
          <div class="card-title">Total P&L</div>
          <div style="font-size:22px; font-weight:700; color:{_color(rev_pnl)}">{rev_pnl_s}</div>
        </div>
        <div>
          <div class="card-title">Slots Used</div>
          <div style="font-size:22px; font-weight:700; color:var(--orange)">{rev_slots_used}/2</div>
        </div>
      </div>
      <div class="sleeve-bar-bg" style="margin-top:14px">
        <div class="sleeve-bar-fill-orange" style="width:{rev_slots_used/2*100:.0f}%"></div>
      </div>
      <div class="muted small" style="margin-top:4px">300,000 SEK sleeve &nbsp;·&nbsp; RSI&lt;33 dip-buy &nbsp;·&nbsp; max 10d hold</div>
    </div>
  </div>

  <!-- Strategy Head-to-Head -->
  <div class="card" style="margin-bottom:20px">
    <div class="section-title">⚔️ Strategy Comparison</div>
    <div style="display:grid; grid-template-columns: 1fr 1fr; gap: 24px;">
      <div>
        <table>
          <thead><tr>
            <th>Metric</th>
            <th style="color:var(--blue)">US Blend</th>
            <th style="color:var(--orange)">US Reversion</th>
            <th>Better</th>
          </tr></thead>
          <tbody>
            {_cmp_row("Closed Trades", blend_s["n"], rev_s["n"], higher=True, fmt="{:.0f}")}
            {_cmp_row("Win Rate", blend_s["wr"], rev_s["wr"], higher=True, fmt="{:.1f}%")}
            {_cmp_row("Total P&L (SEK)", blend_s["pnl"], rev_s["pnl"], higher=True, fmt="{:+,.0f}")}
            {_cmp_row("Avg Win (SEK)", blend_s["avg_win"], rev_s["avg_win"], higher=True, fmt="{:+,.0f}")}
            {_cmp_row("Avg Loss (SEK)", blend_s["avg_loss"], rev_s["avg_loss"], higher=False, fmt="{:,.0f}")}
            {_cmp_row("Best Trade (SEK)", blend_s["best"], rev_s["best"], higher=True, fmt="{:+,.0f}")}
            {_cmp_row("Worst Trade (SEK)", blend_s["worst"], rev_s["worst"], higher=False, fmt="{:,.0f}")}
          </tbody>
        </table>
      </div>
      <div>
        <div class="card-title">Cumulative P&L per Strategy</div>
        <canvas id="pnlChart" style="max-height:220px"></canvas>
      </div>
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
        <th>Action</th><th>Ticker</th><th>Strategy</th><th>Market</th>
        <th>Score</th><th>Size</th><th>P&L</th><th>Signal</th>
      </tr></thead>
      <tbody>{action_rows}</tbody>
    </table>
  </div>

  <!-- Open Positions -->
  <div class="card" style="margin-bottom:20px">
    <div class="section-title">📂 Open Positions ({len(open_trades)})</div>
    <table>
      <thead><tr>
        <th>Ticker</th><th>Strategy</th><th>Market</th><th>Shares</th>
        <th>Entry Price</th><th>Stop Loss</th><th>Entry Date</th><th>Score</th>
      </tr></thead>
      <tbody>{pos_rows}</tbody>
    </table>
  </div>

  <!-- Trade History -->
  <div class="card" style="margin-bottom:20px">
    <div class="section-title">📋 Trade History (last 30)</div>
    <table>
      <thead><tr>
        <th>Date</th><th>Action</th><th>Ticker</th><th>Strategy</th>
        <th>Size</th><th>Value</th><th>P&L</th><th>Held</th><th>Reason</th>
      </tr></thead>
      <tbody>{hist_rows}</tbody>
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
const blend_pnl_dates = {json.dumps(blend_pnl_dates)};
const blend_pnl_cum   = {json.dumps(blend_pnl_cum)};
const rev_pnl_dates   = {json.dumps(rev_pnl_dates)};
const rev_pnl_cum     = {json.dumps(rev_pnl_cum)};

// Cumulative P&L per strategy
if (blend_pnl_dates.length > 0 || rev_pnl_dates.length > 0) {{
  const pnlDatasets = [];
  if (blend_pnl_cum.length > 0) pnlDatasets.push({{
    label: 'US Blend', data: blend_pnl_cum, borderColor: '#60a5fa',
    backgroundColor: 'rgba(96,165,250,0.08)', fill: true,
    tension: 0.3, pointRadius: 3, borderWidth: 2,
  }});
  if (rev_pnl_cum.length > 0) pnlDatasets.push({{
    label: 'US Reversion', data: rev_pnl_cum, borderColor: '#fb923c',
    backgroundColor: 'rgba(251,146,60,0.08)', fill: true,
    tension: 0.3, pointRadius: 3, borderWidth: 2,
  }});
  // Merge + sort labels
  const allDates = [...new Set([...blend_pnl_dates, ...rev_pnl_dates])].sort();
  new Chart(document.getElementById('pnlChart'), {{
    type: 'line',
    data: {{ labels: allDates, datasets: pnlDatasets }},
    options: {{
      responsive: true, maintainAspectRatio: true,
      plugins: {{ legend: {{ labels: {{ color:'#8892aa', boxWidth:12, font:{{size:11}} }} }} }},
      scales: {{
        x: {{ ticks: {{ color:'#8892aa', maxTicksLimit:6 }}, grid: {{ color:'rgba(255,255,255,0.04)' }} }},
        y: {{ ticks: {{ color:'#8892aa', callback: v => v.toLocaleString() + ' SEK' }},
              grid: {{ color:'rgba(255,255,255,0.04)' }} }}
      }}
    }}
  }});
}}

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


def _cmp_row(label: str, a: float, b: float, higher: bool, fmt: str = "{:.2f}") -> str:
    """Single comparison table row: label | blend value | rev value | winner badge."""
    a_wins = (a > b) if higher else (a < b)
    b_wins = (b > a) if higher else (b < a)
    a_col  = "var(--green)" if a_wins else ("var(--red)" if b_wins else "var(--muted)")
    b_col  = "var(--green)" if b_wins else ("var(--red)" if a_wins else "var(--muted)")
    winner = ("🔵 Blend" if a_wins else ("🟠 Reversion" if b_wins else "—"))
    return (f'<tr><td class="muted small">{label}</td>'
            f'<td style="color:{a_col};font-weight:700">{fmt.format(a)}</td>'
            f'<td style="color:{b_col};font-weight:700">{fmt.format(b)}</td>'
            f'<td class="small">{winner}</td></tr>')


def _score_color(score: float) -> str:
    if score >= 70:
        return "#16a34a"
    elif score >= 55:
        return "#2563eb"
    elif score >= 0:
        return "#78716c"
    else:
        return "#dc2626"
