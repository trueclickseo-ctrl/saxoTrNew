import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from atos import database as db
from datetime import date

today = str(date.today())
all_trades = db.get_all_trades() if hasattr(db, "get_all_trades") else []

# Fallback: query closed trades from DB directly
import sqlite3
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "atos_live.db")
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

print(f"=== TRADES TODAY ({today}) ===\n")

# Closed today
cur.execute("SELECT * FROM trades WHERE exit_date=? ORDER BY created_at", (today,))
closed = cur.fetchall()
print(f"SELLS (closed today): {len(closed)}")
for t in closed:
    pnl = t['pnl_sek'] or 0
    sign = "+" if pnl >= 0 else ""
    print(f"  SELL  {t['ticker']:<6}  {int(t['shares'] or 0):>5} shrs  "
          f"entry=${t['entry_price']:.2f}  exit=${t['exit_price'] or 0:.2f}  "
          f"P&L={sign}{pnl:.0f} SEK  reason={t['exit_reason']}")

print()

# Opened today
cur.execute("SELECT * FROM trades WHERE entry_date=? AND exit_date IS NULL ORDER BY created_at", (today,))
opened = cur.fetchall()
print(f"BUYS (open today): {len(opened)}")
for t in opened:
    print(f"  BUY   {t['ticker']:<6}  {int(t['shares'] or 0):>5} shrs  "
          f"entry=${t['entry_price']:.2f}  strategy={t['strategy']}")

conn.close()
