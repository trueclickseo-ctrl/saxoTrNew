"""
scripts/regime_analysis.py
--------------------------
Regime breakdown of all Binance strategy backtest data.

Re-runs simulations internally and classifies every trade by two independent
regime definitions to answer: "Are the strategies fundamentally bad, or does
the problem concentrate in correlated crypto bear markets?"

Strategies analysed:
  - Rotation v1:       all 72 grid combos pooled (most data coverage)
  - Momentum Trend v1: best combo (RSI55-65, Break25d, Exit7d, Stop5%, Hold60d)

Regime definitions (tested in parallel):
  A. BTC 50-day SMA gate (proposed v2 filter):
       SMA_BULL = BTC close > BTC 50d SMA at trade entry date
       SMA_BEAR = BTC close <= BTC 50d SMA at trade entry date

  B. Calendar-period buckets (user-defined):
       BULL: 2020, 2021, 2023, 2024
       BEAR: 2022, 2026-YTD
       CHOP: 2025 (neither clearly bull nor bear)

Run from project root:
    python scripts/regime_analysis.py
"""

from __future__ import annotations

import sys
import os
from itertools import product

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

import numpy as np
import pandas as pd

# ── Imports from existing backtests ──────────────────────────────────────────

from backtest_binance_rotation import (
    SYMBOLS,
    WARMUP_BARS as ROT_WARMUP,
    load_data,
    _precompute as rot_precompute,
    simulate as rot_simulate,
    GRID as ROT_GRID,
    _profit_factor,
)

from backtest_binance_momentum import (
    _precompute as mom_precompute,
    simulate as mom_simulate,
    WARMUP_BARS as MOM_WARMUP,
)

# ── Regime classification ─────────────────────────────────────────────────────

BULL_YEARS = {2020, 2021, 2023, 2024}
BEAR_YEARS = {2022, 2026}
CHOP_YEARS = {2025}


def _build_btc_sma_regime(data: dict) -> dict:
    """
    Returns a dict: date -> "SMA_BULL" | "SMA_BEAR"
    based on BTC close vs BTC 50-day SMA at that date.
    Dates before 50 bars of BTC history are labeled SMA_BULL by default.
    """
    btc_df     = data["BTCUSDT"]
    btc_idx    = list(btc_df.index)
    btc_closes = btc_df["close"].to_numpy(dtype=float)
    n          = len(btc_closes)

    cs      = np.concatenate(([0.0], np.cumsum(btc_closes)))
    sma50   = np.full(n, np.nan)
    if n >= 50:
        sma50[49:] = (cs[50:] - cs[:-50]) / 50

    regime: dict = {}
    for i, d in enumerate(btc_idx):
        if np.isnan(sma50[i]):
            regime[d] = "SMA_BULL"
        else:
            regime[d] = "SMA_BULL" if btc_closes[i] > sma50[i] else "SMA_BEAR"
    return regime


def cal_regime(d) -> str:
    yr = d.year if hasattr(d, "year") else int(str(d)[:4])
    if yr in BULL_YEARS:  return "BULL"
    if yr in BEAR_YEARS:  return "BEAR"
    if yr in CHOP_YEARS:  return "CHOP"
    return "OTHER"


# ── Per-regime stats ──────────────────────────────────────────────────────────

def _regime_stats(trades: list, regime_key: str) -> dict:
    """Compute WR, PF, avg_pct, total_pnl for a group of trades."""
    if not trades:
        return {"n": 0, "wr": float("nan"), "pf": float("nan"),
                "avg_pct": float("nan"), "total_pnl": 0.0}
    wins  = [t for t in trades if t["pnl_net"] > 0]
    loss  = [t for t in trades if t["pnl_net"] <= 0]
    gross_win  = sum(t["pnl_net"] for t in wins)
    gross_loss = abs(sum(t["pnl_net"] for t in loss))
    return {
        "n":         len(trades),
        "wr":        len(wins) / len(trades),
        "pf":        gross_win / gross_loss if gross_loss > 0 else float("inf"),
        "avg_pct":   sum(t["pnl_pct"] for t in trades) / len(trades),
        "total_pnl": sum(t["pnl_net"] for t in trades),
    }


def analyse(trades: list, btc_regime: dict) -> dict[str, dict[str, dict]]:
    """Split trades by both regime definitions; return nested dicts of stats."""
    by_sma: dict[str, list]  = {"SMA_BULL": [], "SMA_BEAR": []}
    by_cal: dict[str, list]  = {"BULL": [], "BEAR": [], "CHOP": [], "OTHER": []}

    for t in trades:
        entry_d = t["entry_date"]
        # SMA regime
        sma_reg = btc_regime.get(entry_d, "SMA_BULL")
        by_sma[sma_reg].append(t)
        # Calendar regime
        by_cal[cal_regime(entry_d)].append(t)

    return {
        "sma": {k: _regime_stats(v, k) for k, v in by_sma.items()},
        "cal": {k: _regime_stats(v, k) for k, v in by_cal.items()},
    }


# ── Printing ─────────────────────────────────────────────────────────────────

def _fmt_stats(label: str, s: dict, width: int = 10) -> str:
    if s["n"] == 0:
        return f"  {label:<{width}}  N=0  (no trades)"
    wr  = f"{s['wr']*100:.0f}%"
    pf  = f"{s['pf']:.2f}" if s["pf"] != float("inf") else "inf"
    avg = f"{s['avg_pct']:+.1f}%"
    pnl = f"${s['total_pnl']:+,.0f}"
    return (f"  {label:<{width}}  N={s['n']:>5}  "
            f"WR={wr:>5}  PF={pf:>5}  "
            f"AvgPnL={avg:>7}  TotalPnL={pnl:>12}")


def _print_section(title: str, breakdown: dict, order: list) -> None:
    print(f"\n  {title}")
    print("  " + "-" * 76)
    for k in order:
        s = breakdown.get(k)
        if s is None or s["n"] == 0:
            continue
        print(_fmt_stats(k, s))


def _verdict(sma_stats: dict) -> str:
    bull = sma_stats.get("SMA_BULL", {})
    bear = sma_stats.get("SMA_BEAR", {})

    if bull.get("n", 0) == 0 or bear.get("n", 0) == 0:
        return "INCONCLUSIVE — insufficient trades in one regime."

    bull_pf = bull["pf"] if bull["pf"] != float("inf") else 99.0
    bear_pf = bear["pf"] if bear["pf"] != float("inf") else 99.0

    pf_gap    = bull_pf - bear_pf
    bear_wins = bear["wr"] < 0.42
    bull_ok   = bull_pf >= 1.2 and bull["wr"] >= 0.40

    if pf_gap >= 0.40 and bear_wins and bull_ok:
        return (
            "REGIME IS THE PRIMARY PROBLEM. "
            f"SMA_BULL PF={bull_pf:.2f} WR={bull['wr']*100:.0f}% vs "
            f"SMA_BEAR PF={bear_pf:.2f} WR={bear['wr']*100:.0f}%. "
            "Excluding bear-regime entries would materially improve results. "
            "Proceed with regime-gated v2."
        )
    elif pf_gap < 0.20 and bull_pf < 1.3:
        return (
            "STRATEGIES ARE FUNDAMENTALLY WEAK. "
            f"Even in SMA_BULL regime PF={bull_pf:.2f} and WR={bull['wr']*100:.0f}%. "
            "A regime gate would filter bad trades but not expose a hidden edge. "
            "Do not proceed to v2 on this basis alone."
        )
    else:
        return (
            f"MIXED. SMA_BULL PF={bull_pf:.2f} WR={bull['wr']*100:.0f}% vs "
            f"SMA_BEAR PF={bear_pf:.2f} WR={bear['wr']*100:.0f}%. "
            "Regime matters but bull-regime performance is not clearly profitable. "
            "Proceed cautiously — v2 gate would help but is not sufficient alone."
        )


# ── Simulation helpers ────────────────────────────────────────────────────────

def _all_rotation_trades(data: dict, rot_ind: dict) -> list:
    """Run all 72 rotation combos and pool their trades."""
    keys   = list(ROT_GRID.keys())
    combos = list(product(*[ROT_GRID[k] for k in keys]))
    all_trades: list = []
    n      = len(combos)
    print(f"  Running {n} rotation combos...", end="", flush=True)
    for i, vals in enumerate(combos, 1):
        params = dict(zip(keys, vals))
        r      = rot_simulate(data, rot_ind, params)
        all_trades.extend(r["trades"])
        if i % 12 == 0:
            print(f" {i}/{n}", end="", flush=True)
    print(f" {n}/{n}  ({len(all_trades)} total trades)")
    return all_trades


def _momentum_best_trades(data: dict, mom_ind: dict) -> list:
    """Run the best Momentum v1 combo and return its trades."""
    best = {
        "RSI_LOW": 55, "RSI_HIGH": 65,
        "BREAKOUT_DAYS": 25, "EXIT_DAYS": 7,
        "STOP_PCT": 0.05, "MAX_HOLD": 60,
    }
    print("  Running Momentum v1 best combo...", end="", flush=True)
    r = mom_simulate(data, mom_ind, best)
    print(f" {len(r['trades'])} trades")
    return r["trades"]


# ── BTC SMA fraction during each period ───────────────────────────────────────

def _regime_calendar_pct(btc_regime: dict, data: dict) -> None:
    """Print what fraction of trading days were SMA_BEAR in each calendar year."""
    btc_df  = data["BTCUSDT"]
    by_year: dict[int, dict] = {}
    for d, reg in btc_regime.items():
        if d not in btc_df.index:
            continue
        yr = d.year
        if yr not in by_year:
            by_year[yr] = {"bear": 0, "total": 0}
        by_year[yr]["total"] += 1
        if reg == "SMA_BEAR":
            by_year[yr]["bear"] += 1

    print(f"\n  BTC 50d SMA regime by year (fraction of days below SMA):")
    print("  " + "-" * 50)
    for yr in sorted(by_year):
        b = by_year[yr]
        frac = b["bear"] / b["total"] if b["total"] > 0 else 0.0
        bar  = "#" * int(frac * 30)
        print(f"  {yr}  {frac*100:>5.0f}% BEAR  {bar}")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 80)
    print("  BINANCE STRATEGY REGIME ANALYSIS")
    print("  Question: Is the problem 'bad strategies' or 'correlated bear markets'?")
    print("=" * 80)

    # ── Data & indicators ─────────────────────────────────────────────────────
    print("\nLoading data and pre-computing indicators...")
    data       = load_data()
    common_idx = sorted(set.intersection(*[set(data[s].index) for s in SYMBOLS]))
    print(f"  {len(SYMBOLS)} symbols  |  {len(common_idx)} common daily bars")

    btc_regime = _build_btc_sma_regime(data)
    _regime_calendar_pct(btc_regime, data)

    print("\nPre-computing rotation indicators...")
    rot_ind = rot_precompute(data)

    print("Pre-computing momentum indicators...")
    mom_ind = mom_precompute(data)

    # ── Simulations ───────────────────────────────────────────────────────────
    print("\nRunning simulations...")
    rot_trades = _all_rotation_trades(data, rot_ind)
    mom_trades = _momentum_best_trades(data, mom_ind)

    # ── Regime breakdown ──────────────────────────────────────────────────────
    rot_breakdown = analyse(rot_trades, btc_regime)
    mom_breakdown = analyse(mom_trades, btc_regime)

    print()
    print("=" * 80)
    print("  RESULTS")
    print("=" * 80)

    # ─── Rotation ─────────────────────────────────────────────────────────────
    print(f"\n  ROTATION v1 — all 72 combos pooled, {len(rot_trades):,} trade-instances")
    print(f"  (Each trade may appear across multiple combos; N reflects aggregate exposure)")
    print()
    print(f"  [A] BTC 50-day SMA regime at entry:")
    _print_section("A", rot_breakdown["sma"], ["SMA_BULL", "SMA_BEAR"])
    print()
    print(f"  [B] Calendar-period regime at entry:")
    _print_section("B", rot_breakdown["cal"], ["BULL", "BEAR", "CHOP", "OTHER"])

    # ─── Momentum ─────────────────────────────────────────────────────────────
    print(f"\n  MOMENTUM TREND v1 — best combo only, {len(mom_trades)} trades")
    print()
    print(f"  [A] BTC 50-day SMA regime at entry:")
    _print_section("A", mom_breakdown["sma"], ["SMA_BULL", "SMA_BEAR"])
    print()
    print(f"  [B] Calendar-period regime at entry:")
    _print_section("B", mom_breakdown["cal"], ["BULL", "BEAR", "CHOP", "OTHER"])

    # ─── What happens if we gate on SMA_BULL only ─────────────────────────────
    print()
    print("=" * 80)
    print("  HYPOTHETICAL: Rotation v1 restricted to SMA_BULL entries only")
    print("=" * 80)
    bull_only = [t for t in rot_trades
                 if btc_regime.get(t["entry_date"], "SMA_BULL") == "SMA_BULL"]
    bear_only = [t for t in rot_trades
                 if btc_regime.get(t["entry_date"], "SMA_BULL") == "SMA_BEAR"]
    s_bull = _regime_stats(bull_only, "SMA_BULL")
    s_bear = _regime_stats(bear_only, "SMA_BEAR")
    s_all  = _regime_stats(rot_trades, "ALL")

    print(f"\n  All trades:          {_fmt_stats('ALL',      s_all,  6)}")
    print(f"  SMA_BULL trades:     {_fmt_stats('SMA_BULL', s_bull, 6)}")
    print(f"  SMA_BEAR trades:     {_fmt_stats('SMA_BEAR', s_bear, 6)}")

    pnl_recovery_pct = (
        (s_bull["total_pnl"] - s_all["total_pnl"]) / abs(s_all["total_pnl"]) * 100
        if s_all["total_pnl"] != 0 else 0.0
    )
    print()
    print(f"  Removing SMA_BEAR entries would have shifted total PnL by "
          f"${s_bear['total_pnl'] * -1:+,.0f} "
          f"({pnl_recovery_pct:+.1f}% vs current baseline).")

    # ─── Verdict ──────────────────────────────────────────────────────────────
    print()
    print("=" * 80)
    print("  VERDICT")
    print("=" * 80)

    # Use pooled rotation data for the verdict (more data = more reliable signal)
    verdict = _verdict(rot_breakdown["sma"])
    # Cross-check with momentum
    mom_verdict_check = _verdict(mom_breakdown["sma"])

    print(f"\n  Rotation (72 combos pooled):   {verdict}")
    print(f"\n  Momentum cross-check:          {mom_verdict_check}")

    # Summary of key numbers for quick reading
    rs  = rot_breakdown["sma"]
    ms  = mom_breakdown["sma"]
    print()
    print(f"  Key numbers (Rotation pooled):")
    for reg in ["SMA_BULL", "SMA_BEAR"]:
        s = rs.get(reg, {})
        if s.get("n", 0) == 0: continue
        pf  = f"{s['pf']:.2f}" if s["pf"] != float("inf") else "inf"
        print(f"    {reg:<10}  N={s['n']:>6}  WR={s['wr']*100:.0f}%  PF={pf}  "
              f"AvgPnL={s['avg_pct']:+.2f}%  TotalPnL=${s['total_pnl']:+,.0f}")
    print()
    print(f"  Key numbers (Momentum best combo):")
    for reg in ["SMA_BULL", "SMA_BEAR"]:
        s = ms.get(reg, {})
        if s.get("n", 0) == 0: continue
        pf  = f"{s['pf']:.2f}" if s["pf"] != float("inf") else "inf"
        print(f"    {reg:<10}  N={s['n']:>6}  WR={s['wr']*100:.0f}%  PF={pf}  "
              f"AvgPnL={s['avg_pct']:+.2f}%  TotalPnL=${s['total_pnl']:+,.0f}")

    print()
    print("=" * 80)
