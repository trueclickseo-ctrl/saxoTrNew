"""
research_technical_analysis.py   (RESEARCH ONLY)
------------------------------------------------
Definitive head-to-head of CLASSIC technical-analysis signals vs our momentum, in the
same rigorous portfolio engine (top-N above EMA200, monthly, daily risk-off, real
costs, 10y). Answers: "does traditional TA beat what we have?"

Signals (ranked high->low):
  MA_CROSS   50/200 SMA golden-cross strength (trend)
  MACD_HIST  MACD histogram (trend/momentum)
  RSI_REV    oversold RSI  (mean reversion; buy the dip)
  BB_REV     near lower Bollinger band (mean reversion)
  MOMENTUM   risk-adjusted momentum (our current offense)  -- benchmark

    python research_technical_analysis.py
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


def ta_signals(px):
    sma50, sma200 = px.rolling(50).mean(), px.rolling(200).mean()
    # RSI(14)
    delta = px.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rsi = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    # MACD histogram
    macd = px.ewm(span=12, adjust=False).mean() - px.ewm(span=26, adjust=False).mean()
    hist = macd - macd.ewm(span=9, adjust=False).mean()
    # Bollinger %b
    m20, s20 = px.rolling(20).mean(), px.rolling(20).std()
    pctb = (px - (m20 - 2 * s20)) / ((m20 + 2 * s20) - (m20 - 2 * s20))
    vol = px.pct_change().rolling(60).std() * np.sqrt(252)
    return {
        "MA_CROSS":  sma50 / sma200 - 1,
        "MACD_HIST": hist / px,
        "RSI_REV":   -rsi,
        "BB_REV":    -pctb,
        "MOMENTUM":  (px / px.shift(120) - 1) / vol.replace(0, np.nan),
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
    print(f"Classic technical-analysis head-to-head ({HISTORY}, top {TOPN}, monthly, risk-off)\n")
    px = load_prices()
    ema200 = px.ewm(span=200, adjust=False).mean()
    idx = (px / px.iloc[0]).mean(axis=1); idx_sma = idx.rolling(200).mean()
    print(f"{'TA signal':12} {'CAGR':>7} {'Sharpe':>7} {'MaxDD':>7}")
    print("-" * 38)
    for name, df in ta_signals(px).items():
        c, s, d = stats(port_daily(px, df, ema200, idx, idx_sma))
        print(f"{name:12} {c:>6.1f}% {s:>7.2f} {d:>6.1f}%")
    print("\n(Everything gets the same EMA200 trend filter + risk-off. If no classic TA")
    print("signal beats MOMENTUM, traditional single-signal TA has no edge here.)")


if __name__ == "__main__":
    main()
