import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from atos import database as db
import json

trades = db.get_open_trades()
print(f"ATOS DB — open positions: {len(trades)}")
for t in trades:
    print(f"  {t['ticker']:<8} strategy={t.get('strategy','?'):<14} "
          f"shares={t.get('shares',0):<6} entry=${t.get('entry_price',0):.2f}")

print()
try:
    with open("data/atos_scan_state.json") as f:
        state = json.load(f)
    blend = state.get("strategies", {}).get("US Blend", {})
    print(f"Last scan result:  {blend.get('status','?')}")
    print(f"Targets selected:  {blend.get('targets', [])}")
    print(f"Reason:            {blend.get('reason','')}")
except Exception as e:
    print(f"Could not read scan state: {e}")
