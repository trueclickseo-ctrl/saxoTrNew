"""
create_fresh_db.py  —  Run once to create a clean database owned by SEO
and import the 4 open positions from Saxo SIM.
"""
import sys, sqlite3, csv, os
from datetime import datetime, date
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import saxo_client as sc

FRESH_DB = os.path.join('data', 'atos_live.db')
os.makedirs('data', exist_ok=True)

conn = sqlite3.connect(FRESH_DB)
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
        d1_trend REAL, d2_momentum REAL, d3_breakout REAL,
        d4_mean_revert REAL, d5_volume REAL,
        exit_reason TEXT, was_profitable INTEGER, stop_price REAL,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_date TEXT NOT NULL, market_group TEXT NOT NULL,
        ticker TEXT NOT NULL, final_score REAL NOT NULL,
        d1_trend REAL, d2_momentum REAL, d3_breakout REAL,
        d4_mean_revert REAL, d5_volume REAL,
        action TEXT NOT NULL, executed INTEGER NOT NULL DEFAULT 0,
        block_reason TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS detector_weights (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        updated_at TEXT NOT NULL, num_trades_used INTEGER NOT NULL DEFAULT 0,
        w_trend REAL NOT NULL DEFAULT 1.0, w_momentum REAL NOT NULL DEFAULT 1.0,
        w_breakout REAL NOT NULL DEFAULT 1.0, w_mean_revert REAL NOT NULL DEFAULT 1.0,
        w_volume REAL NOT NULL DEFAULT 1.0, note TEXT
    );
    CREATE TABLE IF NOT EXISTS equity_curve (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        snap_date TEXT NOT NULL UNIQUE, total_equity_sek REAL NOT NULL,
        us_equity_sek REAL DEFAULT 0, omx30_equity_sek REAL DEFAULT 0,
        dax_equity_sek REAL DEFAULT 0, commodities_sek REAL DEFAULT 0,
        forex_sek REAL DEFAULT 0, open_positions INTEGER DEFAULT 0,
        trades_today INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS market_allocation (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        alloc_date TEXT NOT NULL, market_group TEXT NOT NULL,
        allocated_pct REAL NOT NULL, capital_sek REAL NOT NULL,
        win_rate REAL, profit_factor REAL, note TEXT,
        UNIQUE(alloc_date, market_group)
    );
""")

# Seed starting equity
c.execute(
    "INSERT OR IGNORE INTO equity_curve (snap_date, total_equity_sek, open_positions, trades_today) VALUES (?,10000.0,0,0)",
    (date.today().isoformat(),)
)

# Seed initial weights
c.execute(
    "INSERT INTO detector_weights (updated_at, num_trades_used, w_trend, w_momentum, w_breakout, w_mean_revert, w_volume, note) VALUES (?,0,1.0,1.0,1.0,1.0,1.0,'initial weights — no trades yet')",
    (datetime.now().isoformat(),)
)
conn.commit()

# Load instrument map (UIC → ticker)
uic_map = {}
with open('data/instrument_map.csv', newline='') as f:
    for row in csv.DictReader(f):
        uic = int(row.get('uic', 0) or 0)
        if uic:
            uic_map[uic] = row

# Fetch live open positions from Saxo SIM
print("Fetching open positions from Saxo SIM...")
pos_data   = sc.get_positions()
positions  = pos_data.get('Data', [])
print(f"Found {len(positions)} positions\n")

mkt_map = {
    'SSE': 'OMX30', 'CPH': 'OMX30', 'HEL': 'OMX30',
    'XETRA': 'DAX40', 'FWB': 'DAX40',
    'NYSE': 'US', 'NMS': 'US', 'NGM': 'US',
    'AMS': 'EU_OTHER', 'LSE': 'EU_OTHER',
}

for p in positions:
    pb   = p['PositionBase']
    pv   = p['PositionView']
    uic  = pb['Uic']
    inst = uic_map.get(uic)

    if not inst:
        print(f"  SKIP UIC {uic} — not in instrument_map.csv")
        continue

    ticker       = inst['yahoo_ticker']
    exchange     = inst['exchange']
    currency     = inst['currency']
    market_group = mkt_map.get(exchange, 'US')
    shares       = float(pb['Amount'])
    entry_price  = float(pb['OpenPrice'])
    entry_date   = pb['ExecutionTimeOpen'][:10]
    pnl_eur      = float(pv.get('ProfitLossOnTrade', 0) or 0)

    c.execute(
        """INSERT INTO trades
           (strategy, market_group, ticker, direction, entry_date,
            entry_price, shares, commission_sek, created_at)
           VALUES (?,?,?,?,?,?,?,0.0,?)""",
        ('ATOS_v1', market_group, ticker, 'BUY',
         entry_date, entry_price, shares, datetime.now().isoformat())
    )
    conn.commit()
    print(f"  SYNCED  {ticker:<14}  {shares:>5.0f} shares @ {entry_price:.2f} {currency}"
          f"  P&L: {pnl_eur:+.2f} EUR  Market: {market_group}")

# Final summary
c.execute("SELECT COUNT(*) FROM trades")
n_trades = c.fetchone()[0]
conn.close()

print(f"\n{'='*50}")
print(f"Fresh DB created: {FRESH_DB}")
print(f"Trades imported:  {n_trades}")
print(f"{'='*50}")
print("\nNEXT STEP: The server is already pointing at atos_live.db")
print("Refresh http://localhost:8070 to see your positions.")
