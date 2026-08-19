"""
preview_us_reversion.py — dry-run scan for mean-reversion entry signals.
Run from E:\SaxoTrNew\SaxoTrNew:
    python preview_us_reversion.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import yfinance as yf
from atos.universe import ATOS_UNIVERSE
from atos.us_reversion import scan, RSI_ENTRY, DIP_PCT, VOL_MULT, MAX_POSITIONS
from instrument_map import load_instrument_map

imap = load_instrument_map()
tickers = [t for t in ATOS_UNIVERSE if t in imap]
print(f"Downloading 2y daily data for {len(tickers)} tickers (may take ~60s)...")

raw = yf.download(
    tickers, period="2y", interval="1d",
    auto_adjust=True, progress=False, group_by="ticker"
)

feat_data = {}
for t in tickers:
    try:
        df = raw[t].dropna(how="all")
        if len(df) >= 220:
            feat_data[t] = df
    except Exception:
        pass

print(f"Data loaded for {len(feat_data)} tickers\n")
print(f"Entry criteria: RSI(14) < {RSI_ENTRY}  |  Dip >= {DIP_PCT*100:.0f}% below SMA20  |  Volume >= {VOL_MULT}x 20d avg  |  Price > EMA200")
print()

hits = scan(feat_data, list(feat_data.keys()))

if not hits:
    print("No mean-reversion signals today.")
else:
    print(f"Mean-reversion candidates ({len(hits)} found):")
    print(f"  {'Ticker':<8}  {'Price':>8}  {'RSI':>5}  {'Dip%':>6}  {'VolX':>5}  Score  {'Status'}")
    print("  " + "-" * 65)
    for i, h in enumerate(hits):
        flag = "  <-- BUY" if i < MAX_POSITIONS else ""
        print(
            f"  {h['ticker']:<8}  ${h['price']:>7.2f}  {h['rsi']:>5.1f}"
            f"  {h['dip_pct']:>5.1f}%  {h['vol_ratio']:>4.1f}x  {h['score']:.4f}{flag}"
        )
    print()
    print(f"Max concurrent positions: {MAX_POSITIONS}")
    print(f"Top {min(MAX_POSITIONS, len(hits))} would be traded if reversion strategy is enabled.")
