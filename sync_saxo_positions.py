import sys, csv, os, json
from datetime import datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import saxo_client as sc
from atos import database as db

DB_PATH = os.path.join('data', 'atos.db')


# ── Load instrument map (UIC → ticker) ────────────────────────────
uic_map = {}
with open('data/instrument_map.csv', newline='') as f:
    for row in csv.DictReader(f):
        uic = int(row.get('uic', 0) or 0)
        if uic:
            uic_map[uic] = row

# ── Fetch live positions from Saxo SIM ────────────────────────────
print("Fetching open positions from Saxo SIM...")
pos_data = sc.get_positions()
positions = pos_data.get('Data', [])
print(f"Found {len(positions)} open positions on Saxo SIM\n")

# ── Check what's already in ATOS DB ───────────────────────────────
db.init_db()
existing = db.get_open_trades()
already_tracked = {t['ticker'] for t in existing}
print(f"Already in ATOS DB: {already_tracked or 'none'}\n")

# ── Sync each position ────────────────────────────────────────────
synced = 0
skipped = 0

for p in positions:
    pb  = p['PositionBase']
    pv  = p['PositionView']
    uic = pb['Uic']

    inst = uic_map.get(uic)
    if not inst:
        print(f"  SKIP UIC {uic} — not in instrument_map.csv")
        skipped += 1
        continue

    ticker      = inst['yahoo_ticker']
    currency    = inst['currency']
    exchange    = inst['exchange']
    shares      = float(pb['Amount'])
    entry_price = float(pb['OpenPrice'])
    entry_date  = pb['ExecutionTimeOpen'][:10]
    pnl_open    = float(pv.get('ProfitLossOnTrade', 0) or 0)

    mkt_map = {'SSE': 'OMX30', 'CPH': 'OMX30', 'HEL': 'OMX30',
               'XETRA': 'DAX40', 'FWB': 'DAX40',
               'NYSE': 'US',  'NMS': 'US', 'NGM': 'US',
               'AMS': 'EU_OTHER', 'LSE': 'EU_OTHER'}
    market_group = mkt_map.get(exchange, 'US')

    if ticker in already_tracked:
        print(f"  SKIP {ticker} ({uic}) — already in ATOS DB")
        skipped += 1
        continue

    db.insert_trade({
        'strategy':      'ATOS_v1',
        'market_group':  market_group,
        'ticker':        ticker,
        'direction':     'BUY',
        'entry_date':    entry_date,
        'entry_price':   entry_price,
        'shares':        shares,
        'entry_score':   None,
        'd1_trend':      None,
        'd2_momentum':   None,
        'd3_breakout':   None,
        'd4_mean_revert':None,
        'd5_volume':     None,
        'stop_price':    None,
        'commission_sek':0.0,
    })

    print(f"  SYNCED  {ticker:<14} UIC {uic:<10} {shares:>5.0f} shares @ {entry_price:.2f} {currency}"
          f"  P&L: {pnl_open:+.2f} EUR  Market: {market_group}")
    synced += 1

# ── Summary ───────────────────────────────────────────────────────
open_now = db.get_open_trades()
print(f"\nSync complete: {synced} imported, {skipped} skipped")
print(f"Open positions in ATOS DB now: {len(open_now)}")
for t in open_now:
    print(f"  {t['ticker']:<14} {t['shares']} shares @ {t['entry_price']} | {t['entry_date']}")

print()
print("Refresh your browser: http://localhost:8070")

