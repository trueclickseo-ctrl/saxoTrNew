"""
preview_us_momentum.py
----------------------
Show EXACTLY the orders the US momentum rebalance would place — WITHOUT placing
any order and WITHOUT needing a Saxo token (it only downloads prices + reads the
local DB). Run this before the first live run to see what the engine will do.

    py -3 -X utf8 preview_us_momentum.py
"""
import warnings; warnings.filterwarnings("ignore")
import sys, os; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import yfinance as yf
from atos.features import add_all
from atos.universe import ATOS_UNIVERSE
from atos import database as db
import atos_runner as R

print(f"Downloading {len(ATOS_UNIVERSE)} instruments for the preview...")
raw = yf.download(ATOS_UNIVERSE, period=f"{R.HISTORY_DAYS}d", interval="1d",
                  group_by="ticker", auto_adjust=True, threads=True, progress=False)
feat = {}
for t in ATOS_UNIVERSE:
    try:
        d = raw[t].dropna(how="all")
        if len(d) >= 200:
            feat[t] = add_all(d)
    except Exception:
        pass

print(f"Loaded {len(feat)} instruments with data.\n")
print("=" * 64)
print("  US MOMENTUM — DRY-RUN PREVIEW (no orders placed)")
print("=" * 64)
R.run_us_momentum(feat, db.get_open_trades(), [], dry_run=True)
print("=" * 64)
print("Nothing was placed. To trade for real, run the engine with a valid token.")
