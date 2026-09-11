"""
backtest_blend_by_rank.py
-------------------------
Backtests the US Blend signal by rank position.

For each fortnightly rebalance date (2023-01-01 to today):
  - Regenerates the signal using only data available at that date (no look-ahead)
  - Records each ticker's rank and bucket (offense / defense)
  - Measures actual 14-day forward return

Output: which rank slots (#1-7) and which bucket outperform.

Run: python backtest_blend_by_rank.py
"""
from __future__ import annotations
import sys, warnings, time
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
warnings.filterwarnings("ignore")

# ── Parameters ────────────────────────────────────────────────────────────────
START_DATE      = "2023-01-01"
END_DATE        = "2026-09-10"
FORWARD_DAYS    = 14    # holding period (calendar trading days in index)
REBAL_FREQ      = 14    # fortnightly rebalance (index steps)
HISTORY_NEEDED  = 220   # trading days needed before first valid signal (~11 months)

# Signal params (must match atos/us_momentum.py)
MOM_LOOKBACK    = 120   # ~6-month momentum window
MOM_THRESHOLD   = 0.05  # 5% min 6m return for offense
MOM_N_MAX       = 6     # max offense slots
LOWVOL_N        = 2     # defense slots

# ── Universe ──────────────────────────────────────────────────────────────────
from ibkr_module.ibkr_signals import US_TICKERS


def download_prices(tickers: list[str], start: str, end: str) -> pd.DataFrame:
    import yfinance as yf
    print(f"\nDownloading {len(tickers)} tickers  ({start} -> {end})...")
    print("This takes ~3-5 min on first run; Yahoo rate-limits batch downloads.")
    raw = yf.download(
        tickers, start=start, end=end,
        auto_adjust=True, progress=True, threads=True
    )
    if isinstance(raw.columns, pd.MultiIndex):
        prices = raw["Close"]
    else:
        prices = raw
    prices = prices.dropna(axis=1, how="all")
    print(f"Downloaded {prices.shape[1]} tickers x {prices.shape[0]} trading days.")
    return prices


def signal_at_date(panel: pd.DataFrame, available: list[str]) -> dict | None:
    """Regenerate US Blend signal using only data in `panel` (no look-ahead)."""
    if len(panel) < MOM_LOOKBACK + 10:
        return None

    last   = panel.iloc[-1]
    mom    = panel.iloc[-1] / panel.iloc[-1 - MOM_LOOKBACK] - 1
    vol    = panel.pct_change().rolling(60).std().iloc[-1] * np.sqrt(252)
    ema200 = panel.ewm(span=200, adjust=False).mean().iloc[-1]

    # Risk-off: SPY below its 200d EMA
    if "SPY" in panel.columns:
        if pd.notna(ema200.get("SPY")) and last.get("SPY", 0) < ema200.get("SPY", 0):
            return {"risk_off": True}

    # Stocks above EMA200 with valid vol
    above = [
        t for t in available
        if t in panel.columns
        and pd.notna(ema200.get(t)) and last.get(t, 0) > ema200.get(t, 0)
        and pd.notna(vol.get(t)) and vol.get(t, 0) > 0
    ]

    # Offense: mom > threshold, ranked by mom/vol (Sharpe-like)
    off_elig = [t for t in above if pd.notna(mom.get(t)) and mom.get(t, 0) >= MOM_THRESHOLD]
    offense  = sorted(off_elig, key=lambda t: mom[t] / vol[t], reverse=True)[:MOM_N_MAX]

    # Defense: lowest-vol stocks above EMA200
    defense_pool = [t for t in above if t not in offense]
    defense = sorted(defense_pool, key=lambda t: vol[t])[:LOWVOL_N]

    return {
        "risk_off": False,
        "offense":  offense,
        "defense":  defense,
        "mom":      mom,
        "vol":      vol,
    }


def run() -> pd.DataFrame:
    tickers_dl = list(set(US_TICKERS + ["SPY"]))
    prices = download_prices(tickers_dl, START_DATE, END_DATE)

    available = [t for t in US_TICKERS if t in prices.columns]
    print(f"Tickers with full history: {len(available)}")

    idx = prices.index
    records: list[dict] = []
    risk_off_count = 0

    # Step fortnightly, starting after HISTORY_NEEDED bars
    i = HISTORY_NEEDED
    while i + FORWARD_DAYS < len(idx):
        rebal_date   = idx[i]
        forward_date = idx[i + FORWARD_DAYS]

        panel_slice  = prices.iloc[:i + 1][available]
        future_slice = prices.iloc[i:i + FORWARD_DAYS + 1][available]

        sig = signal_at_date(panel_slice, available)

        if sig is None:
            i += REBAL_FREQ
            continue

        if sig["risk_off"]:
            risk_off_count += 1
            i += REBAL_FREQ
            continue

        offense = sig["offense"]
        defense = sig["defense"]
        targets = list(dict.fromkeys(offense + defense))  # deduped, offense first

        for rank0, ticker in enumerate(targets):
            rank   = rank0 + 1
            bucket = "offense" if ticker in offense else "defense"

            fwd = future_slice[ticker].dropna() if ticker in future_slice.columns else pd.Series()
            if len(fwd) < 2:
                continue

            fwd_ret  = float(fwd.iloc[-1] / fwd.iloc[0] - 1)
            mom_pct  = float(sig["mom"].get(ticker, 0)) * 100
            vol_pct  = float(sig["vol"].get(ticker, 0)) * 100

            records.append({
                "rebal_date":   rebal_date.date(),
                "forward_date": forward_date.date(),
                "ticker":       ticker,
                "rank":         rank,
                "bucket":       bucket,
                "fwd_14d_pct":  round(fwd_ret * 100, 3),
                "mom_6m_pct":   round(mom_pct, 1),
                "vol_ann_pct":  round(vol_pct, 1),
            })

        i += REBAL_FREQ

    df = pd.DataFrame(records)

    print(f"\nRisk-off periods skipped : {risk_off_count}")
    print(f"Total observations       : {len(df)}")
    print(f"Rebalance dates          : {df['rebal_date'].nunique()}")
    print(f"Unique tickers           : {df['ticker'].nunique()}")

    # ── By rank ───────────────────────────────────────────────────────────────
    print("\n" + "=" * 65)
    print("PERFORMANCE BY RANK POSITION  (14-day forward return, %)")
    print("=" * 65)
    rank_grp = df.groupby("rank")["fwd_14d_pct"]
    rank_stats = pd.DataFrame({
        "n":         rank_grp.count(),
        "mean_%":    rank_grp.mean().round(3),
        "median_%":  rank_grp.median().round(3),
        "win_rate%": rank_grp.apply(lambda x: (x > 0).mean() * 100).round(1),
        "ann_sharpe": rank_grp.apply(
            lambda x: (x.mean() / x.std() * np.sqrt(26)) if x.std() > 0 else 0
        ).round(2),
    })
    print(rank_stats.to_string())

    # ── By bucket ─────────────────────────────────────────────────────────────
    print("\n" + "=" * 65)
    print("OFFENSE vs DEFENSE BUCKET")
    print("=" * 65)
    bkt = df.groupby("bucket")["fwd_14d_pct"]
    print(pd.DataFrame({
        "n":         bkt.count(),
        "mean_%":    bkt.mean().round(3),
        "median_%":  bkt.median().round(3),
        "win_rate%": bkt.apply(lambda x: (x > 0).mean() * 100).round(1),
    }).to_string())

    # ── Rank group summary ────────────────────────────────────────────────────
    print("\n" + "=" * 65)
    print("RANK GROUPS  (Top 1-2 / Mid 3-4 / Low 5+)")
    print("=" * 65)
    df["rank_group"] = pd.cut(df["rank"], bins=[0, 2, 4, 99],
                               labels=["Top 1-2", "Mid 3-4", "Low 5+"])
    grp = df.groupby("rank_group", observed=True)["fwd_14d_pct"]
    print(pd.DataFrame({
        "n":         grp.count(),
        "mean_%":    grp.mean().round(3),
        "median_%":  grp.median().round(3),
        "win_rate%": grp.apply(lambda x: (x > 0).mean() * 100).round(1),
        "ann_sharpe": grp.apply(
            lambda x: (x.mean() / x.std() * np.sqrt(26)) if x.std() > 0 else 0
        ).round(2),
    }).to_string())

    # ── Best performing tickers by rank slot ──────────────────────────────────
    print("\n" + "=" * 65)
    print("TOP 10 TICKERS MOST FREQUENTLY APPEARING (any rank)")
    print("=" * 65)
    freq = df.groupby("ticker").agg(
        appearances=("rank", "count"),
        avg_rank=("rank", "mean"),
        avg_fwd_ret=("fwd_14d_pct", "mean"),
        win_rate=("fwd_14d_pct", lambda x: (x > 0).mean() * 100),
    ).sort_values("appearances", ascending=False).head(10).round(2)
    print(freq.to_string())

    # Save
    out = Path("data/backtest_blend_by_rank.csv")
    df.to_csv(out, index=False)
    print(f"\nFull results saved -> {out}")

    return df


if __name__ == "__main__":
    t0 = time.time()
    df = run()
    print(f"\nDone in {time.time()-t0:.0f}s")
