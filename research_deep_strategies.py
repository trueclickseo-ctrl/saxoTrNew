"""
research_deep_strategies.py   (RESEARCH ONLY)
---------------------------------------------
Deep-research candidates vs our current blend. Headline candidate: RESIDUAL MOMENTUM
(beta-adjusted momentum) — documented to roughly DOUBLE the Sharpe of raw momentum by
stripping out market beta (Blitz/Huij/Martens; Quantpedia).

Same portfolio engine as the rest (top-N above EMA200, monthly, daily risk-off, real
costs), 10y. Reports Sharpe/DD/CAGR, correlation to our blend, and whether a
residual-momentum + low-vol blend beats the current momentum + low-vol blend.

    python research_deep_strategies.py
"""
import warnings; warnings.filterwarnings("ignore")
import sys, os; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd, yfinance as yf
from atos.universe import US_TICKERS

HISTORY, REBAL, TOPN, COST, START, LB = "10y", 21, 5, 0.0011, 252, 120


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


def signals(px):
    ret = px.pct_change()
    mkt_ret = ret.mean(axis=1)                       # equal-weight market
    mkt_idx = (px / px.iloc[0]).mean(axis=1)
    vol = ret.rolling(60).std() * np.sqrt(252)
    mom = px / px.shift(LB) - 1
    mkt_mom = mkt_idx / mkt_idx.shift(LB) - 1
    # rolling beta of each stock vs the market
    mvar = mkt_ret.rolling(LB).var()
    beta = pd.DataFrame({c: ret[c].rolling(LB).cov(mkt_ret) / mvar for c in px.columns})
    resid_mom = mom.sub(beta.mul(mkt_mom, axis=0))    # market-neutral momentum
    return {
        "MOMENTUM":     mom / vol.replace(0, np.nan),   # our current offense (risk-adj mom)
        "RESID_MOM":    resid_mom,
        "LOWVOL":       -vol,
    }


def port_daily(px, rank_df, ema200, idx, idx_sma):
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
            inv_old = sum(sh * price[t] for t, sh in shares.items()); cap += inv_old
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
    print(f"Deep-research strategies ({HISTORY}, top {TOPN}, monthly, daily risk-off)\n")
    px = load_prices()
    ema200 = px.ewm(span=200, adjust=False).mean()
    idx = (px / px.iloc[0]).mean(axis=1); idx_sma = idx.rolling(200).mean()
    sigs = signals(px)
    daily = {k: port_daily(px, v, ema200, idx, idx_sma) for k, v in sigs.items()}
    L = min(len(v) for v in daily.values()); daily = {k: v[-L:] for k, v in daily.items()}
    daily["BLEND_MOM+LV (current)"] = 0.5 * daily["MOMENTUM"] + 0.5 * daily["LOWVOL"]
    daily["BLEND_RESID+LV (new?)"]  = 0.5 * daily["RESID_MOM"] + 0.5 * daily["LOWVOL"]

    print(f"{'Strategy':24} {'CAGR':>7} {'Sharpe':>7} {'MaxDD':>7}")
    print("-" * 50)
    for name in ("MOMENTUM", "RESID_MOM", "LOWVOL", "BLEND_MOM+LV (current)", "BLEND_RESID+LV (new?)"):
        c, s, d = stats(daily[name])
        print(f"{name:24} {c:>6.1f}% {s:>7.2f} {d:>6.1f}%")

    corr = np.corrcoef(daily["MOMENTUM"], daily["RESID_MOM"])[0, 1]
    print(f"\nCorrelation MOMENTUM vs RESID_MOM: {corr:.2f}")
    print("If RESID_MOM Sharpe > MOMENTUM, and the RESID+LV blend beats the current blend,")
    print("residual momentum is the upgrade.")


if __name__ == "__main__":
    main()
