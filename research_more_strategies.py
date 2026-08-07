"""
research_more_strategies.py   (RESEARCH ONLY)
---------------------------------------------
Hunt for a SMALL SET of validated, low-correlation US strategies the engine can run
together safely. Diversification only helps if strategies don't move together, so we
measure correlation and test a blend.

Strategies (same portfolio engine: top-N above EMA200, monthly, daily risk-off, costs):
  MOMENTUM  risk-adjusted momentum (return / vol)   -- offense (our validated winner)
  LOWVOL    lowest 60-day volatility                -- defense
  REVERSAL  biggest recent losers (short-term mean reversion, held monthly)
  BLEND     50% momentum + 50% low-vol

Reports Sharpe / DD / CAGR, the correlation matrix, and whether the blend wins.

    python research_more_strategies.py
"""
import warnings; warnings.filterwarnings("ignore")
import sys, os; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd, yfinance as yf
from atos.universe import US_TICKERS

HISTORY, REBAL, TOPN, COST, START = "10y", 21, 5, 0.0011, 252


def load_prices():
    ts = sorted(set(US_TICKERS))
    raw = yf.download(ts, period=HISTORY, interval="1d", group_by="ticker",
                      auto_adjust=True, threads=True, progress=False)
    cols = {}
    for t in ts:
        try:
            s = raw[t]["Close"].dropna()
            if len(s) > START + REBAL:
                cols[t] = s
        except Exception:
            pass
    return pd.DataFrame(cols).ffill()


def rank_signals(px):
    vol = px.pct_change().rolling(60).std() * np.sqrt(252)
    return {
        "MOMENTUM": (px / px.shift(120) - 1) / vol.replace(0, np.nan),
        "LOWVOL":   -vol,
        "REVERSAL": -(px / px.shift(10) - 1),   # biggest 10-day losers first
    }


def portfolio_daily_returns(px, rank_df, ema200, idx, idx_sma):
    n = len(px); cap = 100000.0; shares = {}; equity = []; rets = []; last = -10 ** 9
    for d in range(START, n):
        price = px.iloc[d]
        val = cap + sum(sh * price[t] for t, sh in shares.items())
        if equity and equity[-1] > 0:
            rets.append((val - equity[-1]) / equity[-1])
        equity.append(val)
        if shares and pd.notna(idx_sma.iloc[d]) and idx.iloc[d] < idx_sma.iloc[d]:
            cap = val - COST * sum(sh * price[t] for t, sh in shares.items()); shares = {}
        if d - last >= REBAL:
            last = d
            ok = pd.isna(idx_sma.iloc[d]) or idx.iloc[d] > idx_sma.iloc[d]
            if ok:
                sig = rank_df.iloc[d]
                elig = [t for t in px.columns if pd.notna(sig[t]) and pd.notna(ema200[t].iloc[d]) and price[t] > ema200[t].iloc[d]]
                ranked = sorted(elig, key=lambda t: sig[t], reverse=True)[:TOPN]
            else:
                ranked = []
            inv_old = sum(sh * price[t] for t, sh in shares.items())
            cap += inv_old
            per = val / len(ranked) if ranked else 0
            shares = {t: per / price[t] for t in ranked}
            cap -= sum(sh * price[t] for t, sh in shares.items())
            cap -= COST * (inv_old + (val if ranked else 0))
    return np.array(rets)


def stats(r):
    eq = np.cumprod(1 + r); yrs = len(r) / 252
    cagr = (eq[-1] ** (1 / yrs) - 1) * 100 if yrs > 0 and eq[-1] > 0 else 0
    sharpe = r.mean() / r.std() * np.sqrt(252) if r.std() > 0 else 0
    peak = 1; mdd = 0
    for e in eq:
        peak = max(peak, e); mdd = max(mdd, (peak - e) / peak)
    return cagr, sharpe, mdd * 100


def main():
    print(f"Multi-strategy research ({HISTORY}, top {TOPN}, monthly, daily risk-off)\n")
    px = load_prices()
    ema200 = px.ewm(span=200, adjust=False).mean()
    idx = (px / px.iloc[0]).mean(axis=1); idx_sma = idx.rolling(200).mean()
    sigs = rank_signals(px)

    daily = {name: portfolio_daily_returns(px, df, ema200, idx, idx_sma) for name, df in sigs.items()}
    L = min(len(v) for v in daily.values())
    daily = {k: v[-L:] for k, v in daily.items()}
    daily["BLEND"] = 0.5 * daily["MOMENTUM"] + 0.5 * daily["LOWVOL"]

    print(f"{'Strategy':10} {'CAGR':>7} {'Sharpe':>7} {'MaxDD':>7}")
    print("-" * 36)
    for name in ("MOMENTUM", "LOWVOL", "REVERSAL", "BLEND"):
        c, s, d = stats(daily[name])
        print(f"{name:10} {c:>6.1f}% {s:>7.2f} {d:>6.1f}%")

    print("\nCorrelation of daily returns (lower = better diversification):")
    names = ["MOMENTUM", "LOWVOL", "REVERSAL"]
    df = pd.DataFrame({n: daily[n] for n in names})
    corr = df.corr()
    print("           " + "".join(f"{n:>10}" for n in names))
    for n in names:
        print(f"{n:>10} " + "".join(f"{corr.loc[n,m]:>10.2f}" for m in names))
    print("\nRead: if MOMENTUM & LOWVOL correlation is low AND the BLEND's Sharpe beats")
    print("both, running the two together is genuinely safer than either alone.")


if __name__ == "__main__":
    main()
