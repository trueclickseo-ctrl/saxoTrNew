"""
ATOS Dashboard Server v2
========================
Fixed version:  ThreadingHTTPServer (handles 6 parallel API fetches from browser)
                SO_REUSEADDR (no port conflict on restart)
                Silent logs (clean terminal)
                Seeded starting equity if DB is empty

Run:  python -X utf8 atos_server.py
Open: http://localhost:8070
"""
import os
import sqlite3
import json
import socket
import threading
import webbrowser
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from datetime import datetime, date

DB_DIR  = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
# atos_live.db = SEO-owned, writable. atos.db has Kashif-owned WAL lock (use fix_permissions.bat to fix)
DB_PATH = os.path.join(DB_DIR, 'atos_live.db')
PORT    = 8070

# ─── Database ────────────────────────────────────────────────────────────────

def init_db():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy TEXT NOT NULL DEFAULT 'ATOS_v1',
            market_group TEXT NOT NULL,
            ticker TEXT NOT NULL,
            direction TEXT NOT NULL DEFAULT 'BUY',
            entry_date TEXT NOT NULL,
            exit_date TEXT,
            entry_price REAL NOT NULL,
            exit_price REAL,
            shares REAL NOT NULL,
            pnl_sek REAL,
            commission_sek REAL DEFAULT 0,
            entry_score REAL,
            d1_trend REAL,
            d2_momentum REAL,
            d3_breakout REAL,
            d4_mean_revert REAL,
            d5_volume REAL,
            exit_reason TEXT,
            was_profitable INTEGER,
            stop_price REAL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_date TEXT NOT NULL,
            market_group TEXT NOT NULL,
            ticker TEXT NOT NULL,
            final_score REAL NOT NULL,
            d1_trend REAL,
            d2_momentum REAL,
            d3_breakout REAL,
            d4_mean_revert REAL,
            d5_volume REAL,
            action TEXT NOT NULL,
            executed INTEGER NOT NULL DEFAULT 0,
            block_reason TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS detector_weights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            updated_at TEXT NOT NULL,
            num_trades_used INTEGER NOT NULL DEFAULT 0,
            w_trend REAL NOT NULL DEFAULT 1.0,
            w_momentum REAL NOT NULL DEFAULT 1.0,
            w_breakout REAL NOT NULL DEFAULT 1.0,
            w_mean_revert REAL NOT NULL DEFAULT 1.0,
            w_volume REAL NOT NULL DEFAULT 1.0,
            note TEXT
        );
        CREATE TABLE IF NOT EXISTS equity_curve (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snap_date TEXT NOT NULL UNIQUE,
            total_equity_sek REAL NOT NULL,
            us_equity_sek REAL DEFAULT 0,
            omx30_equity_sek REAL DEFAULT 0,
            dax_equity_sek REAL DEFAULT 0,
            commodities_sek REAL DEFAULT 0,
            forex_sek REAL DEFAULT 0,
            open_positions INTEGER DEFAULT 0,
            trades_today INTEGER DEFAULT 0
        );
    """)
    conn.commit()

    # Seed starting state if DB is brand new
    c.execute("SELECT COUNT(*) FROM equity_curve")
    if c.fetchone()[0] == 0:
        c.execute("""INSERT OR IGNORE INTO equity_curve
                     (snap_date, total_equity_sek, open_positions, trades_today)
                     VALUES (?, 10000.0, 0, 0)""", (date.today().isoformat(),))
        conn.commit()
        print("  DB: seeded starting equity 10,000 SEK")

    c.execute("SELECT COUNT(*) FROM detector_weights")
    if c.fetchone()[0] == 0:
        c.execute("""INSERT INTO detector_weights
                     (updated_at, num_trades_used, w_trend, w_momentum, w_breakout, w_mean_revert, w_volume, note)
                     VALUES (?, 0, 1.0, 1.0, 1.0, 1.0, 1.0, 'initial — no trades yet')""",
                  (datetime.now().isoformat(),))
        conn.commit()
        print("  DB: seeded initial weights (all 1.0)")

    conn.close()


def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


# ─── HTML ────────────────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ATOS — Algorithmic Trading OS</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
    <style>
        :root[data-theme="dark"] {
            --bg: #0f1117; --surface: rgba(26,29,46,0.8); --surface-solid: #1a1d2e;
            --text: #e2e8f0; --sub: #94a3b8; --border: rgba(255,255,255,0.1);
            --grad: linear-gradient(135deg,#8b5cf6,#3b82f6); --accent: #6366f1;
            --ok: #10b981; --bad: #ef4444; --warn: #f59e0b;
        }
        :root[data-theme="light"] {
            --bg: #f8fafc; --surface: rgba(255,255,255,0.9); --surface-solid: #fff;
            --text: #1e293b; --sub: #475569; --border: rgba(0,0,0,0.1);
            --grad: linear-gradient(135deg,#6d28d9,#2563eb); --accent: #4f46e5;
            --ok: #059669; --bad: #dc2626; --warn: #d97706;
        }
        *{box-sizing:border-box;margin:0;padding:0;transition:background-color .3s,color .3s}
        body{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text);padding-bottom:40px}
        .wrap{max-width:1400px;margin:0 auto;padding:0 24px}
        header{display:flex;justify-content:space-between;align-items:center;padding:20px 0;
               border-bottom:1px solid var(--border);margin-bottom:30px}
        .logo{font-size:22px;font-weight:700;background:var(--grad);
              -webkit-background-clip:text;-webkit-text-fill-color:transparent;display:flex;align-items:center;gap:10px}
        .dot{width:10px;height:10px;background:var(--ok);border-radius:50%;
             box-shadow:0 0 10px var(--ok);animation:pulse 2s infinite}
        .head-right{display:flex;align-items:center;gap:16px}
        #lastUpdated{font-size:13px;color:var(--sub)}
        .btn-theme{background:var(--surface-solid);border:1px solid var(--border);
                   color:var(--text);cursor:pointer;font-size:18px;padding:8px 10px;border-radius:8px}
        .kpi-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:20px;margin-bottom:28px}
        .card{background:var(--surface);backdrop-filter:blur(10px);border:1px solid var(--border);
              border-radius:12px;padding:22px;box-shadow:0 4px 6px rgba(0,0,0,.08)}
        .kpi h3{font-size:12px;color:var(--sub);text-transform:uppercase;letter-spacing:.06em;margin-bottom:10px}
        .kpi-val{font-size:30px;font-weight:700;margin-bottom:4px}
        .kpi-sub{font-size:13px;color:var(--sub)}
        .ok{color:var(--ok)} .bad{color:var(--bad)}
        .two{display:grid;grid-template-columns:2fr 1fr;gap:22px;margin-bottom:22px}
        @media(max-width:900px){.two{grid-template-columns:1fr}}
        .sec-title{font-size:17px;font-weight:600;margin-bottom:18px}
        table{width:100%;border-collapse:collapse;font-size:13px}
        th,td{padding:11px 14px;border-bottom:1px solid var(--border);text-align:left}
        th{color:var(--sub);font-weight:500}
        tbody tr:hover{background:rgba(255,255,255,.02)}
        .badge{padding:3px 7px;border-radius:4px;font-size:11px;font-weight:700;text-transform:uppercase}
        .badge.buy{background:rgba(16,185,129,.1);color:var(--ok);border:1px solid rgba(16,185,129,.2)}
        .badge.exit{background:rgba(239,68,68,.1);color:var(--bad);border:1px solid rgba(239,68,68,.2)}
        .badge.blocked{background:rgba(245,158,11,.1);color:var(--warn);border:1px solid rgba(245,158,11,.2)}
        .pill{display:inline-block;width:32px;height:20px;line-height:20px;text-align:center;
              border-radius:3px;font-size:11px;font-weight:600;color:#fff;margin-right:2px}
        .pills{display:flex;align-items:center;gap:2px}
        .witem{margin-bottom:14px}
        .whead{display:flex;justify-content:space-between;font-size:13px;margin-bottom:5px}
        .wbg{height:7px;background:var(--border);border-radius:4px;overflow:hidden}
        .wfill{height:100%;background:var(--grad);border-radius:4px;transition:width 1s ease-out}
        .empty{text-align:center;padding:32px;color:var(--sub);font-size:13px}
        footer{text-align:center;padding:30px 0 10px;color:var(--sub);font-size:13px;
               border-top:1px solid var(--border);margin-top:36px}
        @keyframes pulse{
            0%{transform:scale(.95);box-shadow:0 0 0 0 rgba(16,185,129,.7)}
            70%{transform:scale(1);box-shadow:0 0 0 6px rgba(16,185,129,0)}
            100%{transform:scale(.95);box-shadow:0 0 0 0 rgba(16,185,129,0)}
        }
    </style>
</head>
<body>
<div class="wrap">
    <header>
        <div class="logo"><span class="dot"></span> ATOS <span style="font-weight:400;font-size:16px;color:var(--sub)">Algorithmic Trading OS</span></div>
        <div class="head-right">
            <span id="lastUpdated">Loading...</span>
            <button class="btn-theme" id="themeBtn" title="Toggle theme">🌙</button>
        </div>
    </header>

    <div class="kpi-row">
        <div class="card kpi"><h3>Total Equity</h3><div class="kpi-val" id="kpiEq">---</div><div class="kpi-sub" id="kpiEqSub">---</div></div>
        <div class="card kpi"><h3>Today's P&L</h3><div class="kpi-val" id="kpiPnl">---</div><div class="kpi-sub">Realized today</div></div>
        <div class="card kpi"><h3>Open Positions</h3><div class="kpi-val" id="kpiPos">0/10</div><div class="kpi-sub" id="kpiRegime">Regime: ---</div></div>
        <div class="card kpi"><h3>Algorithm Stats</h3><div class="kpi-val" id="kpiWin">---%</div><div class="kpi-sub" id="kpiPf">PF: --- | Trades: 0</div></div>
    </div>

    <div class="two">
        <div class="card">
            <div class="sec-title">Equity Curve (90 Days)</div>
            <div style="height:290px;position:relative"><canvas id="eqChart"></canvas></div>
        </div>
        <div class="card">
            <div class="sec-title">Algorithm Brain Weights</div>
            <div id="weightsBox"><div class="empty">Loading...</div></div>
        </div>
    </div>

    <div class="card" style="margin-bottom:22px">
        <div class="sec-title">Today's Signals</div>
        <div style="overflow-x:auto">
            <table id="tblSig">
                <thead><tr><th>Action</th><th>Ticker</th><th>Market</th><th>Score</th><th>Regime</th><th>D1-D8</th><th>Note</th></tr></thead>
                <tbody><tr><td colspan="7" class="empty">Loading...</td></tr></tbody>
            </table>
        </div>
    </div>

    <div class="card" style="margin-bottom:22px">
        <div class="sec-title">Open Positions</div>
        <div style="overflow-x:auto">
            <table id="tblOpen">
                <thead><tr><th>Ticker</th><th>Market</th><th>Shares</th><th>Entry Price</th><th>Entry Date</th><th>Score</th><th>Regime</th><th>D1-D8</th></tr></thead>
                <tbody><tr><td colspan="8" class="empty">Loading...</td></tr></tbody>
            </table>
        </div>
    </div>

    <div class="card">
        <div class="sec-title">Trade History (Closed)</div>
        <div style="overflow-x:auto">
            <table id="tblHist">
                <thead><tr><th>Ticker</th><th>Market</th><th>Entry</th><th>Exit</th><th>Shares</th><th>P&L</th><th>Reason</th><th>D1-D8</th></tr></thead>
                <tbody><tr><td colspan="8" class="empty">Loading...</td></tr></tbody>
            </table>
        </div>
    </div>

    <footer>ATOS v2 &middot; 8 Adaptive Detectors &middot; Saxo SIM (Paper Money) &middot; localhost:8070</footer>
</div>

<script>
    let chart = null;

    // Theme
    const html = document.documentElement;
    const btn  = document.getElementById('themeBtn');
    function setTheme(t) {
        html.setAttribute('data-theme', t);
        localStorage.setItem('atos-theme', t);
        btn.textContent = t === 'dark' ? '☀️' : '🌙';
        if (chart) updateChartTheme();
    }
    setTheme(localStorage.getItem('atos-theme') || 'dark');
    btn.addEventListener('click', () => setTheme(html.getAttribute('data-theme') === 'dark' ? 'light' : 'dark'));

    function updateChartTheme() {
        const dark = html.getAttribute('data-theme') === 'dark';
        const tc = dark ? '#94a3b8' : '#475569';
        const gc = dark ? 'rgba(255,255,255,.08)' : 'rgba(0,0,0,.08)';
        chart.options.scales.x.ticks.color = tc;
        chart.options.scales.y.ticks.color = tc;
        chart.options.scales.x.grid.color  = gc;
        chart.options.scales.y.grid.color  = gc;
        chart.update();
    }

    function fmt(n) {
        if (n === null || n === undefined || n === '') return '---';
        return parseFloat(n).toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2});
    }

    function pillColor(v) {
        if (v === null || v === undefined) return '#475569';
        if (v > 50)  return '#10b981';
        if (v > 20)  return '#34d399';
        if (v > 0)   return '#6ee7b7';
        if (v > -20) return '#fca5a5';
        return '#ef4444';
    }

    function pills(d1,d2,d3,d4,d5,d6,d7,d8) {
        const names = ['T','M','B','MR','V','SM','MQ','R'];
        const vals = [d1,d2,d3,d4,d5,d6,d7,d8];
        return `<div class="pills">`+vals.map((v,i)=>
            `<span class="pill" style="background:${pillColor(v)}" title="${names[i]}: ${v !== null ? v : '-'}">${v !== null && v !== undefined ? Math.round(v) : '-'}</span>`
        ).join('')+`</div>`;
    }

    function drawChart(labels, data) {
        const ctx = document.getElementById('eqChart').getContext('2d');
        if (chart) chart.destroy();
        if (!labels || !labels.length) return;
        const dark = html.getAttribute('data-theme') === 'dark';
        const g = ctx.createLinearGradient(0,0,0,290);
        g.addColorStop(0,'rgba(99,102,241,.45)');
        g.addColorStop(1,'rgba(99,102,241,0)');
        chart = new Chart(ctx, {
            type:'line',
            data:{labels, datasets:[{
                label:'Equity (SEK)', data,
                borderColor:'#6366f1', backgroundColor:g,
                borderWidth:2, pointRadius:0, pointHoverRadius:4, fill:true, tension:.4
            }]},
            options:{
                responsive:true, maintainAspectRatio:false,
                plugins:{legend:{display:false}, tooltip:{mode:'index',intersect:false}},
                scales:{
                    x:{grid:{color:dark?'rgba(255,255,255,.08)':'rgba(0,0,0,.08)'},
                       ticks:{color:dark?'#94a3b8':'#475569', maxTicksLimit:10}},
                    y:{grid:{color:dark?'rgba(255,255,255,.08)':'rgba(0,0,0,.08)'},
                       ticks:{color:dark?'#94a3b8':'#475569'}}
                }
            }
        });
    }

    async function load() {
        document.getElementById('lastUpdated').textContent = 'Updating...';
        try {
            const [sum, eq, open, closed, sigs, wts] = await Promise.all([
                fetch('/api/summary').then(r=>r.json()).catch(()=>null),
                fetch('/api/equity').then(r=>r.json()).catch(()=>({data:[]})),
                fetch('/api/trades/open').then(r=>r.json()).catch(()=>({data:[]})),
                fetch('/api/trades/closed').then(r=>r.json()).catch(()=>({data:[]})),
                fetch('/api/signals').then(r=>r.json()).catch(()=>({data:[]})),
                fetch('/api/weights').then(r=>r.json()).catch(()=>({current:null}))
            ]);

            const openD   = (open   && open.data)   || [];
            const closedD = (closed && closed.data)  || [];
            const sigD    = (sigs   && sigs.data)    || [];

            // KPIs
            if (sum && !sum.error) {
                const eq2 = sum.total_equity || 10000;
                document.getElementById('kpiEq').textContent = fmt(eq2) + ' SEK';
                const pct = ((eq2 - 10000) / 10000) * 100;
                document.getElementById('kpiEqSub').innerHTML =
                    `<span class="${pct>=0?'ok':'bad'}">${pct>=0?'+':''}${fmt(pct)}%</span> from 10,000 SEK start`;

                const pnl = sum.today_pnl || 0;
                document.getElementById('kpiPnl').innerHTML =
                    `<span class="${pnl>=0?'ok':'bad'}">${pnl>0?'+':''}${fmt(pnl)} SEK</span>`;

                document.getElementById('kpiPos').textContent = openD.length + '/10';
                document.getElementById('kpiWin').textContent =
                    sum.trades_count > 0 ? fmt(sum.win_rate)+'%' : '---%';
                document.getElementById('kpiPf').textContent =
                    `PF: ${sum.trades_count > 0 ? fmt(sum.profit_factor) : '---'} | Trades: ${sum.trades_count || 0}`;
            }

            // Chart
            if (eq && eq.data && eq.data.length) {
                drawChart(eq.data.map(d=>d.snap_date), eq.data.map(d=>d.total_equity_sek));
            }

            // Weights
            const wb = document.getElementById('weightsBox');
            const w  = wts && wts.current;
            if (w) {
                const names  = ['w_trend','w_momentum','w_breakout','w_mean_revert','w_volume','w_smart_money','w_mom_quality','w_regime'];
                const labels = ['Trend (D1)','Momentum (D2)','Breakout (D3)','Mean Revert (D4)','Volume (D5)','Smart Money (D6)','Mom Quality (D7)','Regime (D8)'];
                const colors = ['#6366f1','#8b5cf6','#a855f7','#ec4899','#f59e0b','#10b981','#06b6d4','#f97316'];
                wb.innerHTML = names.map((n,i)=>{
                    const v = w[n]||1; const p = Math.min((v/2.5)*100,100);
                    return `<div class="witem">
                        <div class="whead"><span>${labels[i]}</span><strong>${v.toFixed(2)}</strong></div>
                        <div class="wbg"><div class="wfill" style="width:${p}%;background:${colors[i]}"></div></div>
                    </div>`;
                }).join('') + `<div style="font-size:12px;color:var(--sub);margin-top:12px">Trades learned from: ${w.num_trades_used || 0}</div>`;
            } else {
                wb.innerHTML = '<div class="empty">No weights yet</div>';
            }

            // Signals table
            const sb = document.querySelector('#tblSig tbody');
            sb.innerHTML = sigD.length
                ? sigD.map(s=>`<tr>
                    <td><span class="badge ${s.action==='BUY'?'buy':s.action==='EXIT'?'exit':'blocked'}">${s.action}</span></td>
                    <td><strong>${s.ticker}</strong></td><td>${s.market_group}</td>
                    <td>${s.final_score!=null?s.final_score.toFixed(1):'-'}</td>
                    <td><span class="badge" style="font-size:10px;padding:2px 6px;background:${s.regime==='BULL'?'#10b981':s.regime==='BEAR'?'#ef4444':s.regime==='SIDEWAYS'?'#f59e0b':'#6366f1'}">${s.regime||'---'}</span></td>
                    <td>${pills(s.d1_trend,s.d2_momentum,s.d3_breakout,s.d4_mean_revert,s.d5_volume,s.d6_smart_money,s.d7_mom_quality,s.d8_regime)}</td>
                    <td style="font-size:12px;color:var(--sub)">${s.block_reason||''}</td>
                </tr>`).join('')
                : '<tr><td colspan="7" class="empty">No signals recorded today — run atos_runner.py to scan markets</td></tr>';

            // Open positions
            const ob = document.querySelector('#tblOpen tbody');
            ob.innerHTML = openD.length
                ? openD.map(p=>`<tr>
                    <td><strong>${p.ticker}</strong></td><td>${p.market_group}</td>
                    <td>${p.shares}</td><td>${fmt(p.entry_price)}</td><td>${p.entry_date}</td>
                    <td>${p.entry_score!=null?p.entry_score.toFixed(1):'-'}</td>
                    <td><span class="badge" style="font-size:10px;padding:2px 6px;background:${p.regime_at_entry==='BULL'?'#10b981':p.regime_at_entry==='BEAR'?'#ef4444':p.regime_at_entry==='SIDEWAYS'?'#f59e0b':'#6366f1'}">${p.regime_at_entry||'---'}</span></td>
                    <td>${pills(p.d1_trend,p.d2_momentum,p.d3_breakout,p.d4_mean_revert,p.d5_volume,p.d6_smart_money,p.d7_mom_quality,p.d8_regime)}</td>
                </tr>`).join('')
                : '<tr><td colspan="8" class="empty">No open positions</td></tr>';

            // History
            const hb = document.querySelector('#tblHist tbody');
            hb.innerHTML = closedD.length
                ? closedD.map(t=>`<tr>
                    <td><strong>${t.ticker}</strong></td><td>${t.market_group}</td>
                    <td>${t.entry_date}</td><td>${t.exit_date||'-'}</td><td>${t.shares}</td>
                    <td class="${(t.pnl_sek||0)>=0?'ok':'bad'}">${(t.pnl_sek||0)>0?'+':''}${fmt(t.pnl_sek)}</td>
                    <td style="font-size:12px">${t.exit_reason||'-'}</td>
                    <td>${pills(t.d1_trend,t.d2_momentum,t.d3_breakout,t.d4_mean_revert,t.d5_volume,t.d6_smart_money,t.d7_mom_quality,t.d8_regime)}</td>
                </tr>`).join('')
                : '<tr><td colspan="8" class="empty">No closed trades yet</td></tr>';

            document.getElementById('lastUpdated').textContent =
                'Last updated: ' + new Date().toLocaleTimeString();

        } catch(e) {
            document.getElementById('lastUpdated').textContent = 'Error: ' + e.message;
            console.error(e);
        }
    }

    load();
    setInterval(load, 60000);  // refresh every minute
</script>
</body>
</html>"""


# ─── HTTP Handler ─────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # silent

    def send_json(self, data, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(data, default=str).encode())

    def do_GET(self):
        path = urlparse(self.path).path

        if path == '/':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(HTML.encode('utf-8'))
            return

        if not path.startswith('/api/'):
            self.send_error(404)
            return

        conn = None
        try:
            conn = get_conn()
            c    = conn.cursor()

            if path == '/api/summary':
                c.execute("SELECT total_equity_sek FROM equity_curve ORDER BY snap_date DESC LIMIT 1")
                eq_row    = c.fetchone()
                total_eq  = eq_row['total_equity_sek'] if eq_row else 10000.0

                today = datetime.now().strftime('%Y-%m-%d')
                c.execute("SELECT SUM(pnl_sek) as p FROM trades WHERE exit_date LIKE ?", (today+'%',))
                r = c.fetchone(); today_pnl = (r['p'] or 0.0) if r else 0.0

                c.execute("SELECT COUNT(*) as cnt, SUM(CASE WHEN was_profitable=1 THEN 1 ELSE 0 END) as wins FROM trades WHERE exit_date IS NOT NULL")
                r = c.fetchone(); cnt = r['cnt'] or 0; wins = r['wins'] or 0
                win_rate = (wins/cnt*100) if cnt > 0 else 0.0

                c.execute("SELECT SUM(CASE WHEN pnl_sek>0 THEN pnl_sek ELSE 0 END) as gp, SUM(CASE WHEN pnl_sek<0 THEN ABS(pnl_sek) ELSE 0 END) as gl FROM trades WHERE exit_date IS NOT NULL")
                r  = c.fetchone(); gp = r['gp'] or 0; gl = r['gl'] or 0
                pf = (gp/gl) if gl > 0 else (gp if gp > 0 else 0.0)

                self.send_json({'total_equity': total_eq, 'today_pnl': today_pnl,
                                'trades_count': cnt, 'win_rate': win_rate, 'profit_factor': pf})

            elif path == '/api/equity':
                c.execute("SELECT snap_date, total_equity_sek FROM equity_curve ORDER BY snap_date DESC LIMIT 90")
                rows = [dict(r) for r in c.fetchall()]
                rows.reverse()
                self.send_json({'data': rows})

            elif path == '/api/trades/open':
                c.execute("SELECT * FROM trades WHERE exit_date IS NULL ORDER BY entry_date DESC")
                self.send_json({'data': [dict(r) for r in c.fetchall()]})

            elif path == '/api/trades/closed':
                c.execute("SELECT * FROM trades WHERE exit_date IS NOT NULL ORDER BY exit_date DESC LIMIT 100")
                self.send_json({'data': [dict(r) for r in c.fetchall()]})

            elif path == '/api/signals':
                today = datetime.now().strftime('%Y-%m-%d')
                c.execute("SELECT * FROM signals WHERE signal_date LIKE ? ORDER BY final_score DESC", (today+'%',))
                self.send_json({'data': [dict(r) for r in c.fetchall()]})

            elif path == '/api/weights':
                c.execute("SELECT * FROM detector_weights ORDER BY id DESC LIMIT 1")
                row = c.fetchone()
                self.send_json({'current': dict(row) if row else None})

            else:
                self.send_error(404)

        except Exception as e:
            self.send_json({'error': str(e)}, 500)
        finally:
            if conn:
                conn.close()


# ─── Main ─────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print("ATOS Dashboard Server v2")
    print(f"  DB: {DB_PATH}")
    init_db()

    server = ThreadingHTTPServer(('', PORT), Handler)
    server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    print(f"  Running at http://localhost:{PORT}")
    print("  Press Ctrl+C to stop\n")

    t = threading.Thread(target=server.serve_forever)
    t.daemon = True
    t.start()

    time.sleep(0.8)
    webbrowser.open(f'http://localhost:{PORT}')

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()
