"""
preview_eligible.py — show how many stocks qualify for momentum rebalance.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import yfinance as yf, numpy as np, pandas as pd
from atos.universe import ATOS_UNIVERSE
from atos.us_momentum import MOM_THRESHOLD, MOM_N_MAX, LOWVOL_N, compute_targets
from instrument_map import load_instrument_map

imap = load_instrument_map()
tickers = [t for t in ATOS_UNIVERSE if t in imap]
print(f"Universe: {len(ATOS_UNIVERSE)} | With UICs: {len(tickers)}")
print(f"Downloading data...")

raw = yf.download(tickers, period="1y", interval="1d",
                  auto_adjust=True, progress=False, group_by="ticker")

feat_data = {}
for t in tickers:
    try:
        df = raw[t].dropna(how="all")
        if len(df) >= 130:
            feat_data[t] = df
    except Exception:
        pass

print(f"Data loaded: {len(feat_data)} tickers with enough history\n")

# Replicate the filter logic from compute_targets
above_ema = []
mom_eligible = []
mom_vals = {}
vol_vals = {}

for t, df in feat_data.items():
    close = df["Close"].dropna()
    if len(close) < 130:
        continue
    ema200 = float(close.ewm(span=200, adjust=False).mean().iloc[-1])
    price  = float(close.iloc[-1])
    ret6m  = float(close.iloc[-1] / close.iloc[-126] - 1) if len(close) >= 126 else None
    vol60  = float(close.pct_change().rolling(60).std().iloc[-1] * (252**0.5)) if len(close) >= 60 else None

    if price > ema200:
        above_ema.append(t)
        if ret6m is not None:
            mom_vals[t] = ret6m
        if vol60 is not None and vol60 > 0:
            vol_vals[t] = vol60
        if ret6m is not None and ret6m >= MOM_THRESHOLD:
            mom_eligible.append(t)

# Offense: ranked by return/vol
ranked = sorted(mom_eligible, key=lambda t: mom_vals.get(t, 0) / vol_vals.get(t, 1), reverse=True)
offense = ranked[:MOM_N_MAX]

# Defense: lowest vol among above-EMA
defense_pool = [t for t in above_ema if t in vol_vals]
defense = sorted(defense_pool, key=lambda t: vol_vals[t])[:LOWVOL_N]

targets = list(dict.fromkeys(offense + defense))

print(f"=== MOMENTUM REBALANCE ELIGIBILITY ===")
print(f"  Total above EMA200:           {len(above_ema):>4}")
print(f"  6-month return > {MOM_THRESHOLD*100:.0f}% (offense): {len(mom_eligible):>4}")
print(f"  Selected offense (top {MOM_N_MAX}):      {len(offense):>4}")
print(f"  Selected defense (top {LOWVOL_N} low-vol): {len(defense):>4}")
print(f"  Final targets (deduped):      {len(targets):>4}")
print()
print(f"OFFENSE picks (ranked by return/vol):")
for i, t in enumerate(offense, 1):
    print(f"  {i}. {t:<8}  6m={mom_vals.get(t,0)*100:+6.1f}%  vol={vol_vals.get(t,1)*100:.0f}%  score={mom_vals.get(t,0)/vol_vals.get(t,1):.2f}")
print()
print(f"DEFENSE picks (lowest volatility above EMA200):")
for i, t in enumerate(defense, 1):
    flag = " (also in offense)" if t in offense else ""
    print(f"  {i}. {t:<8}  vol={vol_vals.get(t,1)*100:.0f}%{flag}")
print()
print(f"FINAL TARGETS: {targets}")
print(f"Top {len(mom_eligible)} offense-eligible tickers (all qualifying):")
print(f"  {'Rank':<5} {'Ticker':<8} {'6m Ret':>7} {'Ann.Vol':>8} {'Score':>7}")
print("  " + "-"*40)
for i, t in enumerate(ranked[:20], 1):
    sel = "<-- selected" if t in offense else ""
    print(f"  {i:<5} {t:<8} {mom_vals.get(t,0)*100:>+6.1f}%  {vol_vals.get(t,1)*100:>6.0f}%  {mom_vals.get(t,0)/vol_vals.get(t,1):>7.2f}  {sel}")
if len(ranked) > 20:
    print(f"  ... and {len(ranked)-20} more qualifying tickers")
