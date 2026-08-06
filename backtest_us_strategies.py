"""
backtest_us_strategies.py
-------------------------
Hunt for the best US swing strategy. Tests several RANKING SIGNALS head-to-head
using the same portfolio engine (top-10 above EMA200, monthly rebalance, daily
market risk-off, real costs), so the only thing that varies is the signal.

Signals tested:
  mom120        6-month return (our current winner)
  mom252        12-month return (classic momentum)
  mom_riskadj   6-month return / volatility (risk-adjusted momentum)
  w52high       proximity to 52-week high (breakout anomaly)
  lowvol        lowest 60-day volatility (defensive factor)

vs Buy&Hold (equal-weight all US names). 10y daily, fractional shares (isolates the
signal edge; whole-share constraint is a separate live-sizing issue).

    py -3 -X utf8 backtest_us_strategies.py
"""
import warnings; warnings.filterwarnings('ignore')
import sys, os; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd, yfinance as yf
from atos.universe import US_TICKERS

HISTORY = "10y"
REBAL   = 21
TOPN    = 10
COST    = 0.0011
START   = 252   # warm-up for the longest signal


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
    vol = px.pct_change().rolling(60).std() * np.sqrt(252)
    return {
        "mom120":      px / px.shift(120) - 1,
        "mom252":      px / px.shift(252) - 1,
        "mom_riskadj": (px / px.shift(120) - 1) / vol.replace(0, np.nan),
        "w52high":     px / px.rolling(252).max(),
        "lowvol":      -vol,
    }


def portfolio_run(px, rank_df, ema200, idx, idx_sma):
    n = len(px); cap = 100000.0; shares = {}; equity = []; prets = []; last = -10 ** 9
    for d in range(START, n):
        price = px.iloc[d]
        val = cap + sum(sh * price[t] for t, sh in shares.items())
        if equity and equity[-1] > 0:
            prets.append((val - equity[-1]) / equity[-1])
        equity.append(val)
        # daily risk-off
        if shares and pd.notna(idx_sma.iloc[d]) and idx.iloc[d] < idx_sma.iloc[d]:
            cap = val - COST * sum(sh * price[t] for t, sh in shares.items()); shares = {}
        if d - last >= REBAL:
            last = d
            regime_ok = pd.isna(idx_sma.iloc[d]) or idx.iloc[d] > idx_sma.iloc[d]
            if regime_ok:
                sig = rank_df.iloc[d]
                elig = [t for t in px.columns
                        if pd.notna(sig[t]) and pd.notna(ema200[t].iloc[d]) and price[t] > ema200[t].iloc[d]]
                ranked = sorted(elig, key=lambda t: sig[t], reverse=True)[:TOPN]
            else:
                ranked = []
            invested_old = sum(sh * price[t] for t, sh in shares.items())
            cap += invested_old
            per = val / len(ranked) if ranked else 0
            shares = {t: per / price[t] for t in ranked}
            cap -= sum(sh * price[t] for t, sh in shares.items())
            cap -= COST * (invested_old + (val if ranked else 0))
    return _stats(np.array(equity), np.array(prets))


def buyhold(px):
    rets = px.pct_change().dropna()
    port = rets.mean(axis=1)
    eq = 100000 * np.cumprod(1 + port.values)
    return _stats(eq, port.values)


def _stats(eq, prets):
    yrs = len(eq) / 252
    cagr = ((eq[-1] / eq[0]) ** (1 / yrs) - 1) * 100 if eq[0] > 0 and yrs > 0 else 0
    sharpe = prets.mean() / prets.std() * np.sqrt(252) if prets.std() > 0 else 0
    peak = eq[0]; mdd = 0
    for e in eq:
        peak = max(peak, e); mdd = max(mdd, (peak - e) / peak)
    return {"cagr": cagr, "sharpe": sharpe, "maxdd": mdd * 100}


def main():
    print(f"US swing strategy hunt ({HISTORY}, top {TOPN}, monthly, daily risk-off, cost {COST*100:.2f}%/side)\n")
    px = load_prices()
    ema200 = px.ewm(span=200, adjust=False).mean()
    idx = (px / px.iloc[0]).mean(axis=1); idx_sma = idx.rolling(200).mean()
    sigs = signals(px)
    print(f"{'Signal':14} {'CAGR':>7} {'Sharpe':>7} {'MaxDD':>7}")
    print("-" * 40)
    rows = []
    for name, df in sigs.items():
        r = portfolio_run(px, df, ema200, idx, idx_sma)
        rows.append((name, r))
        print(f"{name:14} {r['cagr']:>6.1f}% {r['sharpe']:>7.2f} {r['maxdd']:>6.1f}%")
    bh = buyhold(px)
    print(f"{'BUY&HOLD':14} {bh['cagr']:>6.1f}% {bh['sharpe']:>7.2f} {bh['maxdd']:>6.1f}%")
    best = max(rows, key=lambda x: x[1]["sharpe"])
    print(f"\nBest by Sharpe: {best[0]} (Sharpe {best[1]['sharpe']:.2f}, "
          f"DD {best[1]['maxdd']:.1f}%, CAGR {best[1]['cagr']:.1f}%)  vs buy&hold Sharpe {bh['sharpe']:.2f}")


if __name__ == "__main__":
    main()
