"""
backtest_momentum.py
--------------------
Cross-sectional (portfolio) momentum backtest — the well-documented edge
(Jegadeesh-Titman / Clenow / Antonacci). Per market:
  * every REBAL bars (monthly), rank instruments by LOOKBACK-day return
  * keep only those with positive momentum AND price > EMA200 (absolute filter)
  * hold the top N equal-weighted until the next rebalance; else hold cash
  * charge turnover cost (commission + slippage) when holdings change

Reports portfolio CAGR / Sharpe / maxDD vs an equal-weight buy&hold benchmark.

    py -3 -X utf8 backtest_momentum.py
"""
import warnings; warnings.filterwarnings('ignore')
import sys, os; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd, yfinance as yf
from atos.universe import MARKET_GROUPS

HISTORY  = "3y"
LOOKBACK = 120     # ~6 months momentum
REBAL    = 21      # monthly rebalance
COST     = 0.0011  # 0.08% commission + 0.03% slippage, per side
TOPN     = {"US Equities": 5, "OMX30": 3, "CPH25": 3}


def _load_prices(tickers):
    raw = yf.download(tickers, period=HISTORY, interval="1d", group_by="ticker",
                      auto_adjust=True, threads=True, progress=False)
    cols = {}
    for t in tickers:
        try:
            s = raw[t]["Close"].dropna()
            if len(s) > LOOKBACK + REBAL:
                cols[t] = s
        except Exception:
            pass
    px = pd.DataFrame(cols).ffill().dropna(how="all")
    return px


def _stats(monthly_rets):
    mr = np.array(monthly_rets, dtype=float)
    if len(mr) == 0:
        return 0, 0, 0, 0
    eq = np.cumprod(1 + mr)
    total = (eq[-1] - 1) * 100
    yrs = len(mr) / 12
    cagr = (eq[-1] ** (1 / yrs) - 1) * 100 if yrs > 0 and eq[-1] > 0 else 0
    sharpe = mr.mean() / mr.std() * np.sqrt(12) if mr.std() > 0 else 0
    peak = eq[0]; maxdd = 0
    for e in eq:
        peak = max(peak, e); maxdd = max(maxdd, (peak - e) / peak)
    return total, cagr, sharpe, maxdd * 100


def backtest_market(market):
    px = _load_prices(sorted(MARKET_GROUPS[market]))
    if px.shape[1] < 4:
        print(f"{market}: not enough instruments with data"); return
    ema200 = px.ewm(span=200, adjust=False).mean()
    topn = TOPN.get(market, 3)
    idxs = list(range(LOOKBACK, len(px) - 1, REBAL))

    mom_rets, bh_rets, holds = [], [], []
    prev = set()
    for i in idxs:
        j = min(i + REBAL, len(px) - 1)
        mom = px.iloc[i] / px.iloc[i - LOOKBACK] - 1
        elig = [t for t in px.columns
                if pd.notna(mom[t]) and mom[t] > 0 and px[t].iloc[i] > ema200[t].iloc[i]]
        ranked = sorted(elig, key=lambda t: mom[t], reverse=True)[:topn]
        holds.append(len(ranked))
        # benchmark: equal-weight all instruments, same month
        bh = np.nanmean([px[t].iloc[j] / px[t].iloc[i] - 1 for t in px.columns])
        bh_rets.append(bh)
        if ranked:
            gross = np.mean([px[t].iloc[j] / px[t].iloc[i] - 1 for t in ranked])
            newh = set(ranked)
            turnover = len(newh.symmetric_difference(prev)) / max(len(newh), 1)
            mom_rets.append(gross - COST * turnover)
            prev = newh
        else:
            mom_rets.append(0.0); prev = set()

    tot, cagr, sh, dd = _stats(mom_rets)
    btot, bcagr, bsh, bdd = _stats(bh_rets)
    print(f"\n{'='*70}\n{market}  —  cross-sectional momentum (top {topn}, {HISTORY})\n{'='*70}")
    print(f"  Rebalances: {len(idxs)}  |  avg holdings: {np.mean(holds):.1f}")
    print(f"  MOMENTUM  : CAGR {cagr:+.1f}%  Sharpe {sh:.2f}  maxDD {dd:.1f}%  total {tot:+.1f}%")
    print(f"  Buy&Hold  : CAGR {bcagr:+.1f}%  Sharpe {bsh:.2f}  maxDD {bdd:.1f}%  total {btot:+.1f}%")
    verdict = "STRONG" if sh >= 1.0 else ("PROMISING" if sh >= 0.6 else "WEAK")
    edge = "beats" if sh > bsh else "trails"
    print(f"  -> {verdict}; {edge} buy&hold on Sharpe ({sh:.2f} vs {bsh:.2f})")
    return sh


def main():
    print(f"Cross-sectional momentum backtest ({HISTORY}, {LOOKBACK}d lookback, "
          f"monthly rebalance, EMA200 filter, cost {COST*100:.2f}%/side)")
    for m in ("US Equities", "OMX30", "CPH25"):
        backtest_market(m)


if __name__ == "__main__":
    main()
