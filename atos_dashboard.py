import os
import sqlite3
import json
import threading
import webbrowser
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
from datetime import datetime

DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
# The ATOS engine (atos/database.py) writes to atos_live.db — the dashboard MUST
# read the same file or every DB-backed panel (signals, trades, weights, stats,
# equity curve) shows up empty.
DB_PATH = os.path.join(DB_DIR, 'atos_live.db')
PORT = 8070

def init_db():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.executescript("""
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

        CREATE TABLE IF NOT EXISTS market_allocation (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alloc_date TEXT NOT NULL,
            market_group TEXT NOT NULL,
            allocated_pct REAL NOT NULL,
            capital_sek REAL NOT NULL,
            win_rate REAL,
            profit_factor REAL,
            note TEXT,
            UNIQUE(alloc_date, market_group)
        );
    """)
    conn.commit()
    conn.close()

# ── Saxo API Integration ──────────────────────────────────────────
TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'saxo_token.json')
SIM_BASE = 'https://gateway.saxobank.com/sim/openapi/'

def _load_saxo_token():
    """Load access token from saxo_token.json if valid."""
    import time as _time
    if not os.path.exists(TOKEN_FILE):
        return None
    try:
        with open(TOKEN_FILE) as f:
            data = json.load(f)
        token = data.get('access_token', '')
        obtained = float(data.get('obtained_at', 0))
        expires_in = int(data.get('expires_in', 1200))
        if _time.time() > obtained + expires_in - 60:
            return None  # expired
        return token
    except Exception:
        return None

def _saxo_headers(token):
    return {'Authorization': f'Bearer {token}'}

def _saxo_get_balance(token):
    """Get live account balance from Saxo."""
    import requests
    try:
        r = requests.get(SIM_BASE + 'port/v1/balances/me',
                        headers=_saxo_headers(token), timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None

def _saxo_get_positions(token):
    """Get live open positions from Saxo."""
    import requests
    try:
        r = requests.get(SIM_BASE + 'port/v1/positions/me',
                        headers=_saxo_headers(token),
                        params={'FieldGroups': 'PositionBase,PositionView,DisplayAndFormat'},
                        timeout=10)
        if r.status_code == 200:
            return r.json().get('Data', [])
    except Exception:
        pass
    return []

HTML_CONTENT = """<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ATOS Dashboard</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
    <style>
        :root[data-theme="dark"] {
            --bg-color: #0f1117;
            --surface-color: rgba(26, 29, 46, 0.7);
            --surface-solid: #1a1d2e;
            --text-primary: #e2e8f0;
            --text-secondary: #94a3b8;
            --border-color: rgba(255, 255, 255, 0.1);
            --accent-gradient: linear-gradient(135deg, #8b5cf6, #3b82f6);
            --accent-color: #6366f1;
            --success-color: #10b981;
            --danger-color: #ef4444;
            --warning-color: #f59e0b;
            --neutral-color: #64748b;
        }

        :root[data-theme="light"] {
            --bg-color: #f8fafc;
            --surface-color: rgba(255, 255, 255, 0.8);
            --surface-solid: #ffffff;
            --text-primary: #1e293b;
            --text-secondary: #475569;
            --border-color: rgba(0, 0, 0, 0.1);
            --accent-gradient: linear-gradient(135deg, #6d28d9, #2563eb);
            --accent-color: #4f46e5;
            --success-color: #059669;
            --danger-color: #dc2626;
            --warning-color: #d97706;
            --neutral-color: #94a3b8;
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
            transition: background-color 0.3s, color 0.3s, border-color 0.3s;
        }

        body {
            font-family: 'Inter', sans-serif;
            background-color: var(--bg-color);
            color: var(--text-primary);
            line-height: 1.5;
            padding-bottom: 40px;
        }

        .container {
            max-width: 1400px;
            margin: 0 auto;
            padding: 0 20px;
        }

        header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 20px 0;
            border-bottom: 1px solid var(--border-color);
            margin-bottom: 30px;
            animation: fadeIn 0.5s ease-out;
        }

        .logo {
            font-size: 24px;
            font-weight: 700;
            background: var(--accent-gradient);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            display: flex;
            align-items: center;
            gap: 10px;
        }

        .status-dot {
            width: 10px;
            height: 10px;
            background-color: var(--success-color);
            border-radius: 50%;
            display: inline-block;
            box-shadow: 0 0 10px var(--success-color);
            animation: pulse 2s infinite;
        }

        .header-actions {
            display: flex;
            align-items: center;
            gap: 20px;
        }

        .last-updated {
            font-size: 14px;
            color: var(--text-secondary);
        }

        .theme-toggle {
            background: none;
            border: none;
            color: var(--text-primary);
            cursor: pointer;
            font-size: 20px;
            padding: 8px;
            border-radius: 50%;
            background-color: var(--surface-solid);
            border: 1px solid var(--border-color);
            display: flex;
            align-items: center;
            justify-content: center;
        }

        .glass-card {
            background: var(--surface-color);
            backdrop-filter: blur(10px);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 20px;
            margin-bottom: 24px;
            box-shadow: 0 4px 6px rgba(0, 0, 0, 0.05);
            animation: fadeInUp 0.5s ease-out backwards;
        }

        /* KPI Row */
        .kpi-row {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }

        .kpi-card {
            text-align: center;
        }

        .kpi-card h3 {
            font-size: 14px;
            color: var(--text-secondary);
            font-weight: 500;
            margin-bottom: 8px;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }

        .kpi-value {
            font-size: 32px;
            font-weight: 700;
            margin-bottom: 4px;
        }

        .kpi-sub {
            font-size: 14px;
            color: var(--text-secondary);
        }

        .text-success { color: var(--success-color); }
        .text-danger { color: var(--danger-color); }

        /* Two Column Layout */
        .two-col {
            display: grid;
            grid-template-columns: 2fr 1fr;
            gap: 24px;
        }
        
        @media (max-width: 900px) {
            .two-col {
                grid-template-columns: 1fr;
            }
        }

        .section-title {
            font-size: 18px;
            font-weight: 600;
            margin-bottom: 20px;
            display: flex;
            align-items: center;
            gap: 10px;
        }

        /* Tables */
        .table-container {
            overflow-x: auto;
        }

        table {
            width: 100%;
            border-collapse: collapse;
            text-align: left;
            font-size: 14px;
        }

        th, td {
            padding: 12px 16px;
            border-bottom: 1px solid var(--border-color);
        }

        th {
            color: var(--text-secondary);
            font-weight: 500;
            cursor: pointer;
            white-space: nowrap;
        }

        th:hover {
            color: var(--text-primary);
        }

        tbody tr:hover {
            background-color: rgba(255, 255, 255, 0.02);
        }

        .badge {
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 12px;
            font-weight: 600;
            text-transform: uppercase;
        }

        .badge.buy { background-color: rgba(16, 185, 129, 0.1); color: var(--success-color); border: 1px solid rgba(16, 185, 129, 0.2); }
        .badge.exit { background-color: rgba(239, 68, 68, 0.1); color: var(--danger-color); border: 1px solid rgba(239, 68, 68, 0.2); }
        .badge.blocked { background-color: rgba(245, 158, 11, 0.1); color: var(--warning-color); border: 1px solid rgba(245, 158, 11, 0.2); }

        /* Score Pills */
        .score-pill {
            display: inline-block;
            width: 28px;
            height: 20px;
            line-height: 20px;
            text-align: center;
            border-radius: 4px;
            font-size: 11px;
            font-weight: 600;
            margin-right: 2px;
            color: white;
        }
        
        .score-row {
            display: flex;
            align-items: center;
            gap: 2px;
        }

        /* Weight Bars */
        .weight-item {
            margin-bottom: 15px;
        }
        
        .weight-header {
            display: flex;
            justify-content: space-between;
            font-size: 14px;
            margin-bottom: 6px;
        }

        .weight-bar-bg {
            height: 8px;
            background-color: var(--border-color);
            border-radius: 4px;
            overflow: hidden;
        }

        .weight-bar-fill {
            height: 100%;
            background: var(--accent-gradient);
            border-radius: 4px;
            transition: width 1s ease-out;
        }

        footer {
            text-align: center;
            padding: 40px 0 20px;
            color: var(--text-secondary);
            font-size: 14px;
            border-top: 1px solid var(--border-color);
            margin-top: 40px;
        }

        .empty-state {
            text-align: center;
            padding: 40px;
            color: var(--text-secondary);
        }

        /* Animations */
        @keyframes pulse {
            0% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7); }
            70% { transform: scale(1); box-shadow: 0 0 0 6px rgba(16, 185, 129, 0); }
            100% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0); }
        }

        @keyframes fadeIn {
            from { opacity: 0; }
            to { opacity: 1; }
        }

        @keyframes fadeInUp {
            from { opacity: 0; transform: translateY(20px); }
            to { opacity: 1; transform: translateY(0); }
        }

        .delay-1 { animation-delay: 0.1s; }
        .delay-2 { animation-delay: 0.2s; }
        .delay-3 { animation-delay: 0.3s; }
        .delay-4 { animation-delay: 0.4s; }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div class="logo">
                <span class="status-dot"></span>
                ATOS <span style="font-weight: 400; font-size: 18px; color: var(--text-secondary)">Algorithmic Trading OS</span>
            </div>
            <div class="header-actions">
                <span class="last-updated" id="lastUpdated">Updating...</span>
                <button class="theme-toggle" id="themeToggle" title="Toggle Theme">
                    🌙
                </button>
            </div>
        </header>

        <div class="kpi-row delay-1">
            <div class="glass-card kpi-card">
                <h3>Total Equity</h3>
                <div class="kpi-value" id="kpiEquity">--- SEK</div>
                <div class="kpi-sub" id="kpiEquitySub">---</div>
            </div>
            <div class="glass-card kpi-card">
                <h3>Today's P&L</h3>
                <div class="kpi-value" id="kpiTodayPnl">---</div>
                <div class="kpi-sub">Realized & Unrealized</div>
            </div>
            <div class="glass-card kpi-card">
                <h3>Open Positions</h3>
                <div class="kpi-value" id="kpiPositions">0/10</div>
                <div class="kpi-sub">Active Trades</div>
            </div>
            <div class="glass-card kpi-card">
                <h3>Algorithm Stats</h3>
                <div class="kpi-value" id="kpiWinRate">---%</div>
                <div class="kpi-sub" id="kpiProfitFactor">PF: --- | Trades: 0</div>
            </div>
        </div>

        <div class="glass-card" style="margin-bottom:30px;">
            <div class="section-title">Market Status</div>
            <div id="marketStatus" style="display:flex;flex-wrap:wrap;gap:16px;">
                <div class="empty-state">Loading...</div>
            </div>
        </div>

        <div class="glass-card" style="margin-bottom:30px;">
            <div class="section-title">Strategy Leaderboard (per market)</div>
            <div class="table-container">
                <table id="leaderboardTable">
                    <thead><tr>
                        <th>Strategy</th><th>Instrument</th><th>P&amp;L (SEK)</th><th>Sharpe</th><th>Max DD</th>
                        <th>Win rate</th><th>Trades</th><th>Open</th><th>Status</th>
                    </tr></thead>
                    <tbody><tr><td colspan="9" class="empty-state">Loading...</td></tr></tbody>
                </table>
            </div>
        </div>

        <div class="glass-card" style="margin-bottom:30px;">
            <div class="section-title">US Momentum — Schedule &amp; Holdings</div>
            <div id="usMomentum" class="kpi-sub">Loading...</div>
        </div>

        <div class="two-col delay-2">
            <div class="glass-card">
                <div class="section-title">Equity Curve (90 Days)</div>
                <div style="height: 300px; position: relative;">
                    <canvas id="equityChart"></canvas>
                </div>
            </div>
            
            <div class="glass-card">
                <div class="section-title">Algorithm Brain Weights</div>
                <div id="weightsContainer">
                    <div class="empty-state">Loading...</div>
                </div>
            </div>
        </div>

        <div class="two-col delay-3" style="margin-top: 24px;">
            <div class="glass-card" style="grid-column: 1 / -1;">
                <div class="section-title">Today's Signals</div>
                <div class="table-container">
                    <table id="signalsTable">
                        <thead>
                            <tr>
                                <th>Action</th>
                                <th>Ticker</th>
                                <th>Market</th>
                                <th>Score</th>
                                <th>D1-D5 Breakdown</th>
                                <th>Reason</th>
                            </tr>
                        </thead>
                        <tbody><tr><td colspan="6" class="empty-state">Loading...</td></tr></tbody>
                    </table>
                </div>
            </div>
        </div>

        <div class="glass-card delay-4">
            <div class="section-title">Tradeable Universe (stock-only: US · OMX30 · CPH25)</div>
            <div id="universeContainer"><div class="empty-state">Loading...</div></div>
        </div>

        <div class="glass-card delay-4">
            <div class="section-title">Open Positions (Portfolio)</div>
            <div class="table-container">
                <table id="openPositionsTable">
                    <thead>
                        <tr>
                            <th>Ticker</th>
                            <th>Exchange</th>
                            <th>Shares</th>
                            <th>Entry Price</th>
                            <th>Entry Date</th>
                            <th>P&L</th>
                            <th>Description</th>
                        </tr>
                    </thead>
                    <tbody><tr><td colspan="7" class="empty-state">Loading...</td></tr></tbody>
                </table>
            </div>
        </div>

        <div class="glass-card delay-4">
            <div class="section-title">Recent Trades</div>
            <div class="table-container">
                <table id="recentTradesTable">
                    <thead><tr>
                        <th>Date</th><th>Strategy</th><th>Ticker</th><th>Action</th>
                        <th>Shares</th><th>Price</th><th>P&amp;L</th>
                    </tr></thead>
                    <tbody><tr><td colspan="7" class="empty-state">Loading...</td></tr></tbody>
                </table>
            </div>
        </div>

        <div class="glass-card delay-4">
            <div class="section-title">Trade History (Closed)</div>
            <div class="table-container">
                <table id="tradeHistoryTable">
                    <thead>
                        <tr>
                            <th>Ticker</th>
                            <th>Market</th>
                            <th>Entry Date</th>
                            <th>Exit Date</th>
                            <th>Shares</th>
                            <th>P&L</th>
                            <th>Exit Reason</th>
                            <th>D1-D5 Breakdown</th>
                        </tr>
                    </thead>
                    <tbody><tr><td colspan="8" class="empty-state">Loading...</td></tr></tbody>
                </table>
            </div>
        </div>

        <footer>
            ATOS v3 &middot; Saxo SIM (Live) &middot; localhost:8070
        </footer>
    </div>

    <script>
        // Charts initialization (must be declared before setTheme)
        let equityChartInstance = null;

        // Theme Management
        const themeToggle = document.getElementById('themeToggle');
        const htmlElement = document.documentElement;
        
        function setTheme(theme) {
            htmlElement.setAttribute('data-theme', theme);
            localStorage.setItem('theme', theme);
            themeToggle.innerHTML = theme === 'dark' ? '☀️' : '🌙';
            updateChartTheme();
        }

        const savedTheme = localStorage.getItem('theme') || 'dark';
        setTheme(savedTheme);

        themeToggle.addEventListener('click', () => {
            const currentTheme = htmlElement.getAttribute('data-theme');
            setTheme(currentTheme === 'dark' ? 'light' : 'dark');
        });

        function updateChartTheme() {
            const isDark = htmlElement.getAttribute('data-theme') === 'dark';
            const textColor = isDark ? '#94a3b8' : '#475569';
            const gridColor = isDark ? 'rgba(255,255,255,0.1)' : 'rgba(0,0,0,0.1)';
            
            if (equityChartInstance) {
                equityChartInstance.options.scales.x.ticks.color = textColor;
                equityChartInstance.options.scales.y.ticks.color = textColor;
                equityChartInstance.options.scales.x.grid.color = gridColor;
                equityChartInstance.options.scales.y.grid.color = gridColor;
                equityChartInstance.update();
            }
        }

        function initEquityChart(labels, data) {
            const ctx = document.getElementById('equityChart').getContext('2d');
            
            if (equityChartInstance) {
                equityChartInstance.destroy();
            }

            if (!labels || labels.length === 0) {
                return;
            }

            const isDark = htmlElement.getAttribute('data-theme') === 'dark';
            
            const gradient = ctx.createLinearGradient(0, 0, 0, 300);
            gradient.addColorStop(0, 'rgba(99, 102, 241, 0.5)');
            gradient.addColorStop(1, 'rgba(99, 102, 241, 0.0)');

            equityChartInstance = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: labels,
                    datasets: [{
                        label: 'Total Equity (SEK)',
                        data: data,
                        borderColor: '#6366f1',
                        backgroundColor: gradient,
                        borderWidth: 2,
                        pointRadius: 0,
                        pointHoverRadius: 4,
                        fill: true,
                        tension: 0.4
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { display: false },
                        tooltip: { mode: 'index', intersect: false }
                    },
                    scales: {
                        x: {
                            grid: { color: isDark ? 'rgba(255,255,255,0.1)' : 'rgba(0,0,0,0.1)' },
                            ticks: { color: isDark ? '#94a3b8' : '#475569', maxTicksLimit: 10 }
                        },
                        y: {
                            grid: { color: isDark ? 'rgba(255,255,255,0.1)' : 'rgba(0,0,0,0.1)' },
                            ticks: { color: isDark ? '#94a3b8' : '#475569' }
                        }
                    }
                }
            });
        }

        // Helper functions
        function formatNumber(num) {
            if (num === null || num === undefined) return '---';
            return parseFloat(num).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        }

        function getScoreColor(val) {
            if (val === null || val === undefined) return '#64748b';
            if (val > 0.5) return '#10b981';
            if (val > 0) return '#34d399';
            if (val < -0.5) return '#ef4444';
            if (val < 0) return '#f87171';
            return '#64748b';
        }

        function generateScorePills(d1, d2, d3, d4, d5) {
            const vals = [d1, d2, d3, d4, d5];
            return `<div class="score-row">` + vals.map(v => 
                `<span class="score-pill" style="background-color: ${getScoreColor(v)}">${v !== null ? v.toFixed(1) : '-'}</span>`
            ).join('') + `</div>`;
        }

        // Fetch and Update Data
        async function fetchDashboardData() {
            try {
                document.getElementById('lastUpdated').textContent = 'Updating...';

                const [summary, equity, open, closed, signals, weights, livePositions] = await Promise.all([
                    fetch('/api/summary').then(r => r.json()).catch(e => ({ error: e.message })),
                    fetch('/api/equity').then(r => r.json()).catch(e => ({ data: [] })),
                    fetch('/api/trades/open').then(r => r.json()).catch(e => ({ data: [] })),
                    fetch('/api/trades/closed').then(r => r.json()).catch(e => ({ data: [] })),
                    fetch('/api/signals').then(r => r.json()).catch(e => ({ data: [] })),
                    fetch('/api/weights').then(r => r.json()).catch(e => ({ current: null })),
                    fetch('/api/positions/live').then(r => r.json()).catch(e => ({ data: [] }))
                ]);

                const livePos = (livePositions && livePositions.data) || [];
                const openData = (open && open.data) || [];
                const closedData = (closed && closed.data) || [];
                const signalData = (signals && signals.data) || [];
                
                // Use live position count if available, otherwise DB count
                const positionCount = livePos.length > 0 ? livePos.length : openData.length;

                if (summary && !summary.error) {
                    updateKPIs(summary, positionCount, livePositions && livePositions.summary);
                }
                
                if (equity && equity.data && equity.data.length > 0) {
                    const labels = equity.data.map(d => d.snap_date);
                    const data = equity.data.map(d => d.total_equity_sek);
                    initEquityChart(labels, data);
                }

                updateWeights(weights ? weights.current : null);
                
                // Update tables - use live positions if available
                if (livePos.length > 0) {
                    updateLivePositions(livePositions);
                } else {
                    updateTables(openData, closedData, signalData);
                }
                // Always update signals and closed trades from DB
                updateSignalsTable(signalData);
                updateClosedTradesTable(closedData);
                fetch('/api/leaderboard').then(r => r.json()).then(d => updateLeaderboard(d.data)).catch(() => {});
                fetch('/api/trades/recent').then(r => r.json()).then(d => updateRecentTrades(d.data)).catch(() => {});

                document.getElementById('lastUpdated').textContent = `Last updated: ${new Date().toLocaleTimeString()}`;
            } catch (err) {
                console.error('Dashboard fetch error:', err);
                document.getElementById('lastUpdated').textContent = `Error: ${err.message}`;
            }
        }

        function updateKPIs(data, openCount, liveSummary) {
            if (!data) return;
            const currency = data.currency || 'SEK';
            const cash = data.cash_balance;
            // Mark-to-market equity = available cash + CURRENT market value of the
            // positions (so unrealized profit/loss shows and moves the number).
            let eq = data.total_equity;
            let invested = null;
            if (cash !== null && cash !== undefined && liveSummary && liveSummary.total_value_sek != null) {
                eq = cash + liveSummary.total_value_sek;
                invested = liveSummary.total_value_sek;
            } else if (cash !== null && cash !== undefined && eq != null) {
                invested = eq - cash;
            }
            document.getElementById('kpiEquity').textContent = formatNumber(eq) + ' ' + currency;

            const startingCapital = 15000;
            const pct = eq ? ((eq - startingCapital) / startingCapital) * 100 : 0;
            const sign = pct >= 0 ? '+' : '';
            const cls = pct >= 0 ? 'text-success' : 'text-danger';

            let subText = `<span class="${cls}">${sign}${formatNumber(pct)}%</span>`;
            if (invested !== null) subText += ` | Invested: ${formatNumber(invested)} ${currency}`;
            if (cash !== null && cash !== undefined) subText += ` | Cash: ${formatNumber(cash)} ${currency}`;
            if (data.saxo_total_eur !== null && data.saxo_total_eur !== undefined) {
                subText += `<br><span style="color:var(--text-secondary)">Saxo account (live): ${formatNumber(data.saxo_total_eur)} ${data.saxo_currency} · cash ${formatNumber(data.saxo_cash_eur)} ${data.saxo_currency}</span>`;
            }
            document.getElementById('kpiEquitySub').innerHTML = subText;

            const pnl = data.today_pnl || 0;
            const pnlCls = pnl >= 0 ? 'text-success' : 'text-danger';
            document.getElementById('kpiTodayPnl').innerHTML = `<span class="${pnlCls}">${pnl > 0 ? '+' : ''}${formatNumber(pnl)} ${currency}</span>`;
            document.getElementById('kpiPositions').textContent = `${openCount}/10`;
            document.getElementById('kpiWinRate').textContent = data.win_rate ? formatNumber(data.win_rate) + '%' : '---%';
            document.getElementById('kpiProfitFactor').textContent = `PF: ${data.profit_factor ? formatNumber(data.profit_factor) : '---'} | Trades: ${data.trades_count || 0}`;
        }

        function updateLeaderboard(rows) {
            const body = document.querySelector('#leaderboardTable tbody');
            if (!rows || rows.length === 0) { body.innerHTML = '<tr><td colspan="9" class="empty-state">No data yet</td></tr>'; return; }
            body.innerHTML = rows.map(r => {
                const pnlCls = r.pnl_sek >= 0 ? 'text-success' : 'text-danger';
                const statusColor = r.status === 'Active' ? 'var(--success-color)' : 'var(--neutral-color)';
                return `<tr>
                    <td><strong>${r.strategy || r.market}</strong></td>
                    <td>${r.instrument || ''}</td>
                    <td class="${pnlCls}">${r.pnl_sek >= 0 ? '+' : ''}${formatNumber(r.pnl_sek)}</td>
                    <td>${r.sharpe != null ? r.sharpe.toFixed(2) : '—'}</td>
                    <td class="${r.max_dd_pct > 0 ? 'text-danger' : ''}">${r.max_dd_pct ? '-' + formatNumber(r.max_dd_pct) + '%' : '0.0%'}</td>
                    <td>${r.trades ? formatNumber(r.win_rate) + '%' : '—'}</td>
                    <td>${r.trades}</td>
                    <td>${r.open_positions}</td>
                    <td><span class="badge" style="background:rgba(16,185,129,0.1);color:${statusColor};border:1px solid ${statusColor}">${r.status}</span></td>
                </tr>`;
            }).join('');
        }

        function updateRecentTrades(rows) {
            const body = document.querySelector('#recentTradesTable tbody');
            if (!rows || rows.length === 0) { body.innerHTML = '<tr><td colspan="7" class="empty-state">No trades yet</td></tr>'; return; }
            body.innerHTML = rows.map(t => {
                const closed = !!t.exit_date;
                const action = closed ? 'SELL' : 'BUY';
                const badge = closed ? 'exit' : 'buy';
                const price = closed ? (t.exit_price != null ? t.exit_price : '-') : (t.entry_price != null ? t.entry_price : '-');
                const pnl = t.pnl_sek;
                const pnlHtml = (closed && pnl != null)
                    ? `<span class="${pnl >= 0 ? 'text-success' : 'text-danger'}">${pnl >= 0 ? '+' : ''}${formatNumber(pnl)} SEK</span>` : '—';
                return `<tr>
                    <td>${(t.exit_date || t.entry_date) || '-'}</td>
                    <td>${t.strategy || t.market_group}</td>
                    <td><strong>${t.ticker}</strong></td>
                    <td><span class="badge ${badge}">${action}</span></td>
                    <td>${t.shares}</td>
                    <td>${typeof price === 'number' ? price.toFixed(2) : price}</td>
                    <td>${pnlHtml}</td>
                </tr>`;
            }).join('');
        }

        function updateWeights(w) {
            const container = document.getElementById('weightsContainer');
            if (!w) {
                container.innerHTML = '<div class="empty-state">No weights learned yet</div>';
                return;
            }
            
            const maxW = 2.5; // based on prompt req
            const names = ['w_trend', 'w_momentum', 'w_breakout', 'w_mean_revert', 'w_volume'];
            const labels = ['Trend (D1)', 'Momentum (D2)', 'Breakout (D3)', 'Mean Revert (D4)', 'Volume (D5)'];
            
            let html = '';
            names.forEach((name, i) => {
                const val = w[name] || 1.0;
                const pct = Math.min((val / maxW) * 100, 100);
                html += `
                    <div class="weight-item">
                        <div class="weight-header">
                            <span>${labels[i]}</span>
                            <strong>${val.toFixed(2)}</strong>
                        </div>
                        <div class="weight-bar-bg">
                            <div class="weight-bar-fill" style="width: ${pct}%"></div>
                        </div>
                    </div>
                `;
            });
            container.innerHTML = html;
        }

        function updateLivePositions(liveData) {
            const openBody = document.querySelector('#openPositionsTable tbody');
            const positions = (liveData && liveData.data) || [];
            
            if (positions.length === 0) {
                // Don't overwrite if we already have DB positions
                return;
            }
            
            let rows = positions.map(p => {
                const pnlCls = p.pnl >= 0 ? 'text-success' : 'text-danger';
                return `
                    <tr>
                        <td><strong>${p.ticker}</strong></td>
                        <td>${p.market_group}</td>
                        <td>${p.shares}</td>
                        <td>${p.entry_price ? p.entry_price.toFixed(2) : '-'}</td>
                        <td>${p.entry_date ? p.entry_date.substring(0, 10) : '-'}</td>
                        <td class="${pnlCls}">${p.pnl >= 0 ? '+' : ''}${formatNumber(p.pnl)} ${p.currency}</td>
                        <td>${p.description}</td>
                    </tr>
                `;
            }).join('');
            const s = liveData && liveData.summary;
            if (s) {
                const pnlCls = s.total_pnl_sek >= 0 ? 'text-success' : 'text-danger';
                const pnlPct = s.total_invested_sek > 0 ? (s.total_pnl_sek / s.total_invested_sek * 100) : 0;
                rows += `
                    <tr style="border-top:2px solid var(--border-color);font-weight:700;">
                        <td>TOTAL</td>
                        <td></td>
                        <td>${s.count}</td>
                        <td colspan="2">Invested: ${formatNumber(s.total_invested_sek)} SEK</td>
                        <td class="${pnlCls}">${s.total_pnl_sek >= 0 ? '+' : ''}${formatNumber(s.total_pnl_sek)} SEK (${pnlPct >= 0 ? '+' : ''}${formatNumber(pnlPct)}%)</td>
                        <td>Market value: ${formatNumber(s.total_value_sek)} SEK</td>
                    </tr>`;
            }
            openBody.innerHTML = rows;
        }

        function updateSignalsTable(signals) {
            const sigBody = document.querySelector('#signalsTable tbody');
            if (!signals || signals.length === 0) {
                sigBody.innerHTML = '<tr><td colspan="6" class="empty-state">No signals today</td></tr>';
            } else {
                sigBody.innerHTML = signals.map(s => {
                    const badgeClass = s.action === 'BUY' ? 'buy' : s.action === 'EXIT' ? 'exit' : 'blocked';
                    return `
                        <tr>
                            <td><span class="badge ${badgeClass}">${s.action}</span></td>
                            <td><strong>${s.ticker}</strong></td>
                            <td>${s.market_group}</td>
                            <td>${s.final_score ? s.final_score.toFixed(2) : '-'}</td>
                            <td>${generateScorePills(s.d1_trend, s.d2_momentum, s.d3_breakout, s.d4_mean_revert, s.d5_volume)}</td>
                            <td>${s.block_reason || '-'}</td>
                        </tr>
                    `;
                }).join('');
            }
        }

        function updateClosedTradesTable(closed) {
            const histBody = document.querySelector('#tradeHistoryTable tbody');
            if (!closed || closed.length === 0) {
                histBody.innerHTML = '<tr><td colspan="8" class="empty-state">No closed trades yet</td></tr>';
            } else {
                histBody.innerHTML = closed.map(t => {
                    const pnlCls = t.pnl_sek >= 0 ? 'text-success' : 'text-danger';
                    return `
                        <tr>
                            <td><strong>${t.ticker}</strong></td>
                            <td>${t.market_group}</td>
                            <td>${t.entry_date}</td>
                            <td>${t.exit_date}</td>
                            <td>${t.shares}</td>
                            <td class="${pnlCls}">${t.pnl_sek > 0 ? '+' : ''}${formatNumber(t.pnl_sek)}</td>
                            <td>${t.exit_reason || '-'}</td>
                            <td>${generateScorePills(t.d1_trend, t.d2_momentum, t.d3_breakout, t.d4_mean_revert, t.d5_volume)}</td>
                        </tr>
                    `;
                }).join('');
            }
        }

        function updateTables(open, closed, signals) {
            // Signals
            const sigBody = document.querySelector('#signalsTable tbody');
            if (!signals || signals.length === 0) {
                sigBody.innerHTML = '<tr><td colspan="6" class="empty-state">No signals today</td></tr>';
            } else {
                sigBody.innerHTML = signals.map(s => {
                    const badgeClass = s.action === 'BUY' ? 'buy' : s.action === 'EXIT' ? 'exit' : 'blocked';
                    return `
                        <tr>
                            <td><span class="badge ${badgeClass}">${s.action}</span></td>
                            <td><strong>${s.ticker}</strong></td>
                            <td>${s.market_group}</td>
                            <td>${s.final_score ? s.final_score.toFixed(2) : '-'}</td>
                            <td>${generateScorePills(s.d1_trend, s.d2_momentum, s.d3_breakout, s.d4_mean_revert, s.d5_volume)}</td>
                            <td>${s.block_reason || '-'}</td>
                        </tr>
                    `;
                }).join('');
            }

            // Open Positions
            const openBody = document.querySelector('#openPositionsTable tbody');
            if (!open || open.length === 0) {
                openBody.innerHTML = '<tr><td colspan="7" class="empty-state">No open positions</td></tr>';
            } else {
                openBody.innerHTML = open.map(p => `
                    <tr>
                        <td><strong>${p.ticker}</strong></td>
                        <td>${p.market_group}</td>
                        <td>${p.shares}</td>
                        <td>${p.entry_price}</td>
                        <td>${p.entry_date}</td>
                        <td>${p.entry_score ? p.entry_score.toFixed(2) : '-'}</td>
                        <td>${generateScorePills(p.d1_trend, p.d2_momentum, p.d3_breakout, p.d4_mean_revert, p.d5_volume)}</td>
                    </tr>
                `).join('');
            }

            // History
            const histBody = document.querySelector('#tradeHistoryTable tbody');
            if (!closed || closed.length === 0) {
                histBody.innerHTML = '<tr><td colspan="8" class="empty-state">No closed trades yet</td></tr>';
            } else {
                histBody.innerHTML = closed.map(t => {
                    const pnlCls = t.pnl_sek >= 0 ? 'text-success' : 'text-danger';
                    return `
                        <tr>
                            <td><strong>${t.ticker}</strong></td>
                            <td>${t.market_group}</td>
                            <td>${t.entry_date}</td>
                            <td>${t.exit_date}</td>
                            <td>${t.shares}</td>
                            <td class="${pnlCls}">${t.pnl_sek > 0 ? '+' : ''}${formatNumber(t.pnl_sek)}</td>
                            <td>${t.exit_reason || '-'}</td>
                            <td>${generateScorePills(t.d1_trend, t.d2_momentum, t.d3_breakout, t.d4_mean_revert, t.d5_volume)}</td>
                        </tr>
                    `;
                }).join('');
            }
        }

        function updateUniverse(resp) {
            const c = document.getElementById('universeContainer');
            if (!resp || !resp.groups) { c.innerHTML = '<div class="empty-state">Unavailable</div>'; return; }
            let html = '';
            resp.groups.forEach(g => {
                html += `<div style="margin-bottom:18px;">
                    <div class="weight-header">
                        <span><strong>${g.market_group}</strong></span>
                        <span class="kpi-sub">${g.tradable}/${g.count} tradable</span>
                    </div>
                    <div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:8px;">` +
                    g.tickers.map(t =>
                        `<span class="score-pill" title="${t.mapped ? 'mapped — tradable' : 'not mapped yet'}"
                               style="width:auto;padding:0 8px;background-color:${t.mapped ? '#10b981' : '#64748b'}">${t.ticker}</span>`
                    ).join('') +
                    `</div></div>`;
            });
            c.innerHTML = html;
        }

        function marketOpenNow(tz, o, c) {
            const parts = new Intl.DateTimeFormat('en-US', {timeZone: tz, weekday: 'short',
                hour: '2-digit', minute: '2-digit', hour12: false}).formatToParts(new Date());
            const wd = parts.find(p => p.type === 'weekday').value;
            if (wd === 'Sat' || wd === 'Sun') return false;
            let h = parseInt(parts.find(p => p.type === 'hour').value);
            const m = parseInt(parts.find(p => p.type === 'minute').value);
            if (h === 24) h = 0;
            const cur = h * 60 + m;
            return cur >= (o[0] * 60 + o[1]) && cur < (c[0] * 60 + c[1]);
        }
        function updateMarketStatus() {
            const mkts = [
                {name: 'US (NYSE / Nasdaq)', tz: 'America/New_York', o: [9,30], c: [16,0], hours: '09:30–16:00 ET'},
                {name: 'OMX30 (Stockholm)',  tz: 'Europe/Stockholm', o: [9,0],  c: [17,30], hours: '09:00–17:30 CET'},
                {name: 'CPH25 (Copenhagen)', tz: 'Europe/Copenhagen', o: [9,0], c: [17,0],  hours: '09:00–17:00 CET'},
            ];
            document.getElementById('marketStatus').innerHTML = mkts.map(mk => {
                const open = marketOpenNow(mk.tz, mk.o, mk.c);
                const color = open ? 'var(--success-color)' : 'var(--neutral-color)';
                return `<div style="flex:1;min-width:220px;padding:14px;border:1px solid var(--border-color);border-radius:10px;">
                    <div style="display:flex;justify-content:space-between;align-items:center;">
                        <strong>${mk.name}</strong>
                        <span class="badge" style="background:${open?'rgba(16,185,129,0.12)':'rgba(100,116,139,0.12)'};color:${color};border:1px solid ${color}">${open ? 'OPEN' : 'CLOSED'}</span>
                    </div>
                    <div class="kpi-sub" style="margin-top:6px;">${mk.hours}</div>
                </div>`;
            }).join('');
        }

        // Start
        fetchDashboardData();
        setInterval(fetchDashboardData, 60000);
        function updateUsMomentum(d) {
            const el = document.getElementById('usMomentum');
            if (!d) { el.textContent = 'Unavailable'; return; }
            const names = (d.holdings || []).map(h => `${h.ticker} (${h.shares})`).join(', ') || 'none yet';
            el.innerHTML = `Last rebalance: <strong>${d.last_rebalance || 'never'}</strong> &nbsp;·&nbsp; ` +
                `Next rebalance: <strong>${d.next_rebalance}</strong> (first trading day of month) &nbsp;·&nbsp; ` +
                `Holdings (${d.holdings_count}): ${names}`;
        }

        // Universe rarely changes — fetch once at load.
        fetch('/api/universe').then(r => r.json()).then(updateUniverse).catch(() => {});
        fetch('/api/us_momentum').then(r => r.json()).then(updateUsMomentum).catch(() => {});
        setInterval(() => fetch('/api/us_momentum').then(r => r.json()).then(updateUsMomentum).catch(() => {}), 60000);
        updateMarketStatus();
        setInterval(updateMarketStatus, 30000);
    </script>
</body>
</html>"""

def get_db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

class DashboardHandler(BaseHTTPRequestHandler):
    def send_json(self, data, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode('utf-8'))

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == '/':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(HTML_CONTENT.encode('utf-8'))
            return

        if not path.startswith('/api/'):
            self.send_error(404)
            return

        try:
            conn = get_db_conn()
            cursor = conn.cursor()

            if path == '/api/summary':
                # ATOS STRATEGY equity (10,000 SEK baseline) — deliberately NOT
                # the raw Saxo demo balance (~€999k), which would read +9,899%.
                cursor.execute("SELECT total_equity_sek FROM equity_curve ORDER BY snap_date DESC LIMIT 1")
                eq_row = cursor.fetchone()
                atos_equity = eq_row['total_equity_sek'] if eq_row else 15000.0

                # Cash from the ATOS risk-state file (same source the engine sizes off)
                atos_cash = None
                try:
                    _rs = os.path.join(DB_DIR, 'atos_risk_state.json')
                    if os.path.exists(_rs):
                        with open(_rs) as _f:
                            atos_cash = json.load(_f).get('available_cash_sek')
                except Exception:
                    atos_cash = None

                # Today's realized P&L (closed trades) in SEK
                cursor.execute("SELECT SUM(pnl_sek) as tpnl FROM trades WHERE exit_date LIKE ?", (datetime.now().strftime('%Y-%m-%d') + '%',))
                tpnl_row = cursor.fetchone()
                today_pnl = tpnl_row['tpnl'] if tpnl_row and tpnl_row['tpnl'] else 0.0

                cursor.execute("SELECT COUNT(*) as cnt, SUM(CASE WHEN was_profitable = 1 THEN 1 ELSE 0 END) as wins FROM trades WHERE exit_date IS NOT NULL")
                stats_row = cursor.fetchone()
                trades_count = stats_row['cnt'] if stats_row else 0
                wins = stats_row['wins'] if stats_row and stats_row['wins'] else 0
                win_rate = (wins / trades_count * 100) if trades_count > 0 else 0

                cursor.execute("SELECT SUM(CASE WHEN pnl_sek > 0 THEN pnl_sek ELSE 0 END) as gross_profit, SUM(CASE WHEN pnl_sek < 0 THEN ABS(pnl_sek) ELSE 0 END) as gross_loss FROM trades WHERE exit_date IS NOT NULL")
                pf_row = cursor.fetchone()
                gp = pf_row['gross_profit'] if pf_row and pf_row['gross_profit'] else 0
                gl = pf_row['gross_loss'] if pf_row and pf_row['gross_loss'] else 0
                profit_factor = (gp / gl) if gl > 0 else (gp if gp > 0 else 0)

                # Real Saxo broker account (EUR) — shown SEPARATELY so the actual
                # account balance is visible, distinct from the ATOS 10k SEK sleeve.
                saxo_total = saxo_cash = None
                saxo_cur = 'EUR'
                _tok = _load_saxo_token()
                if _tok:
                    _bal = _saxo_get_balance(_tok)
                    if _bal:
                        saxo_total = _bal.get('TotalValue')
                        saxo_cash = _bal.get('CashBalance')
                        saxo_cur = _bal.get('Currency', 'EUR')

                self.send_json({
                    "total_equity": atos_equity,
                    "today_pnl": today_pnl,
                    "trades_count": trades_count,
                    "win_rate": win_rate,
                    "profit_factor": profit_factor,
                    "cash_balance": atos_cash,
                    "currency": "SEK",
                    "source": "atos",
                    "saxo_total_eur": saxo_total,
                    "saxo_cash_eur": saxo_cash,
                    "saxo_currency": saxo_cur,
                })

            elif path == '/api/equity':
                cursor.execute("SELECT snap_date, total_equity_sek FROM equity_curve ORDER BY snap_date DESC LIMIT 90")
                rows = [dict(r) for r in cursor.fetchall()]
                rows.reverse() # chronological
                self.send_json({"data": rows})

            elif path == '/api/trades/open':
                cursor.execute("SELECT * FROM trades WHERE exit_date IS NULL ORDER BY entry_date DESC")
                rows = [dict(r) for r in cursor.fetchall()]
                self.send_json({"data": rows})

            elif path == '/api/trades/closed':
                cursor.execute("SELECT * FROM trades WHERE exit_date IS NOT NULL ORDER BY exit_date DESC LIMIT 100")
                rows = [dict(r) for r in cursor.fetchall()]
                self.send_json({"data": rows})

            elif path == '/api/signals':
                cursor.execute("SELECT * FROM signals ORDER BY id DESC LIMIT 200")
                rows = [dict(r) for r in cursor.fetchall()]
                self.send_json({"data": rows})

            elif path == '/api/weights':
                cursor.execute("SELECT * FROM detector_weights ORDER BY id DESC LIMIT 1")
                current = cursor.fetchone()
                self.send_json({"current": dict(current) if current else None})

            elif path == '/api/positions/live':
                saxo_token = _load_saxo_token()
                if saxo_token:
                    positions = _saxo_get_positions(saxo_token)
                    formatted = []
                    for p in positions:
                        disp = p.get('DisplayAndFormat', {})
                        pbase = p.get('PositionBase', {})
                        pview = p.get('PositionView', {})
                        # Derive market from Saxo symbol suffix
                        sym = disp.get('Symbol', '?')
                        if ':xome' in sym:
                            mkt = 'OMX30'
                        elif ':xams' in sym:
                            mkt = 'EU'
                        elif ':xnas' in sym or ':xnys' in sym:
                            mkt = 'US'
                        elif ':xetr' in sym:
                            mkt = 'DAX'
                        else:
                            mkt = disp.get('Currency', '?')
                        formatted.append({
                            'ticker': sym,
                            'description': disp.get('Description', '?'),
                            'market_group': mkt,
                            'shares': pbase.get('Amount', 0),
                            'entry_price': pbase.get('OpenPrice', 0),
                            'entry_date': pbase.get('ExecutionTimeOpen', '?'),
                            'current_price': pview.get('CurrentPrice', 0),
                            'pnl': pview.get('ProfitLossOnTrade', 0),
                            'pnl_pct': pview.get('ProfitLossOnTradeInPercentage', 0),
                            'currency': disp.get('Currency', '?'),
                            'market_value': pview.get('MarketValue', 0),
                        })
                    # Portfolio totals, each position converted to SEK by its currency.
                    import sys as _sys
                    _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
                    try:
                        import fx as _fx
                    except Exception:
                        _fx = None
                    t_inv = t_pnl = t_val = 0.0
                    for _p in formatted:
                        _rate = 1.0
                        if _fx is not None:
                            try:
                                _rate = _fx.get_rate_to_sek(_p.get('currency') or 'SEK')
                            except Exception:
                                _rate = 1.0
                        t_inv += (_p.get('shares') or 0) * (_p.get('entry_price') or 0) * _rate
                        t_pnl += (_p.get('pnl') or 0) * _rate
                        t_val += (_p.get('market_value') or 0) * _rate
                    # Market value = cost basis + unrealized P&L (robust even when
                    # Saxo omits MarketValue, which it sometimes does on SIM).
                    self.send_json({'data': formatted, 'source': 'saxo_live',
                                    'summary': {'total_invested_sek': round(t_inv, 2),
                                                'total_pnl_sek': round(t_pnl, 2),
                                                'total_value_sek': round(t_inv + t_pnl, 2),
                                                'count': len(formatted)}})
                else:
                    self.send_json({'data': [], 'source': 'unavailable', 'error': 'Token expired or missing'})

            elif path == '/api/universe':
                import sys as _sys
                _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
                from atos.universe import MARKET_GROUPS
                try:
                    from instrument_map import load_instrument_map
                    imap = load_instrument_map()
                except Exception:
                    imap = {}
                groups = []
                for g, tickers in MARKET_GROUPS.items():
                    items = [{'ticker': t, 'mapped': t in imap} for t in sorted(tickers)]
                    groups.append({
                        'market_group': g,
                        'count': len(items),
                        'tradable': sum(1 for i in items if i['mapped']),
                        'tickers': items,
                    })
                self.send_json({'groups': groups,
                                'total': sum(gr['count'] for gr in groups)})

            elif path == '/api/leaderboard':
                import math
                markets = ['US Equities', 'OMX30', 'CPH25']
                instruments = {'US Equities': 'US · S&P500 + Nasdaq100',
                               'OMX30': 'OMXS30', 'CPH25': 'CPH25'}
                strategies = {'US Equities': 'US Blend', 'OMX30': 'OMX (paused)', 'CPH25': 'CPH (paused)'}
                rows = []
                for mg in markets:
                    closed = [r['pnl_sek'] for r in cursor.execute(
                        "SELECT pnl_sek FROM trades WHERE market_group=? AND exit_date IS NOT NULL AND pnl_sek IS NOT NULL ORDER BY exit_date, id",
                        (mg,)).fetchall()]
                    openc = cursor.execute("SELECT COUNT(*) c FROM trades WHERE market_group=? AND exit_date IS NULL",
                                           (mg,)).fetchone()['c']
                    n = len(closed); pnl = sum(closed); wins = sum(1 for p in closed if p > 0)
                    cum = peak = maxdd = 0.0
                    for p in closed:
                        cum += p
                        peak = max(peak, cum)
                        maxdd = max(maxdd, peak - cum)
                    sharpe = None
                    if n >= 2:
                        mean = pnl / n
                        sd = math.sqrt(sum((p - mean) ** 2 for p in closed) / (n - 1))
                        if sd > 0:
                            sharpe = round(mean / sd * math.sqrt(252), 2)
                    rows.append({
                        'market': mg, 'strategy': strategies.get(mg, mg),
                        'instrument': instruments.get(mg, mg),
                        'pnl_sek': round(pnl, 2), 'trades': n,
                        'win_rate': round((wins / n * 100) if n else 0, 1),
                        'max_dd_pct': round(maxdd / 10000 * 100, 1),
                        'sharpe': sharpe, 'open_positions': openc,
                        'status': 'Active' if openc or n else 'Idle',
                    })
                self.send_json({'data': rows})

            elif path == '/api/trades/recent':
                rows = [dict(r) for r in cursor.execute(
                    "SELECT strategy, ticker, market_group, direction, entry_date, exit_date, "
                    "entry_price, exit_price, shares, pnl_sek, was_profitable "
                    "FROM trades ORDER BY COALESCE(exit_date, entry_date) DESC, id DESC LIMIT 20"
                ).fetchall()]
                self.send_json({'data': rows})

            elif path == '/api/us_momentum':
                from datetime import date as _date
                state_file = os.path.join(DB_DIR, 'us_momentum_state.json')
                last = None
                try:
                    with open(state_file) as _f:
                        last = json.load(_f).get('last_rebalance')
                except Exception:
                    last = None
                today = _date.today()
                ny = today.year + (1 if today.month == 12 else 0)
                nm = 1 if today.month == 12 else today.month + 1
                next_rebal = _date(ny, nm, 1).isoformat() if last else "next engine run (first rebalance)"
                cursor.execute("SELECT ticker, shares, entry_price FROM trades "
                               "WHERE strategy='US Blend' AND exit_date IS NULL ORDER BY ticker")
                holds = [dict(r) for r in cursor.fetchall()]
                self.send_json({"last_rebalance": last, "next_rebalance": next_rebal,
                                "holdings": holds, "holdings_count": len(holds)})

            else:
                self.send_error(404)

        except Exception as e:
            self.send_json({"error": str(e)}, status=500)
        finally:
            conn.close()

def run_server():
    server_address = ('', PORT)
    httpd = HTTPServer(server_address, DashboardHandler)
    print(f"ATOS Dashboard running at http://localhost:{PORT}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    httpd.server_close()

if __name__ == '__main__':
    init_db()
    
    # Run server in a thread so we can open browser
    server_thread = threading.Thread(target=run_server)
    server_thread.daemon = True
    server_thread.start()
    
    time.sleep(0.5)
    webbrowser.open(f'http://localhost:{PORT}')
    
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Shutting down...")
