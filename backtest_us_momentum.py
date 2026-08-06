"""
backtest_us_momentum.py
-----------------------
Build the US cross-sectional momentum strategy properly and cut its drawdown.
Daily equity curve (proper Sharpe/maxDD), accurate turnover costs. Compares:

  BASE       : top-N by 120d return, each above its EMA200, monthly rebalance
  +REGIME    : + go to CASH when the broad US market is below its 200-day trend
  +REGIME+VT : + volatility targeting (scale exposure to TARGET_VOL)

    py -3 -X utf8 backtest_us_momentum.py
"""
import warnings; warnings.filterwarnings('ignore')
import sys, os; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, pandas as pd, yfinance as yf
from atos.universe import US_TICKERS

HISTORY    = "10y"
LOOKBACK   = 120
REBAL      = 21
TOPN       = 10        # top-10 beat top-5 on both Sharpe and drawdown (diversification)
COST       = 0.0011     # 0.08% commission + 0.03% slippage, per side
TARGET_VOL = 0.15       # annualized, for the vol-targeting variant
START_CAP  = 10_000.0


def load_prices():
    ts = sorted(set(US_TICKERS))
    raw = yf.download(ts, period=HISTORY, interval="1d", group_by="ticker",
                      auto_adjust=True, threads=True, progress=False)
    cols = {}
    for t in ts:
        try:
            s = raw[t]["Close"].dropna()
            if len(s) > LOOKBACK + REBAL:
                cols[t] = s
        except Exception:
            pass
    return pd.DataFrame(cols).ffill().dropna(how="all")


def run(px, use_regime=False, use_voltarget=False, use_daily_regime=False):
    ema200 = px.ewm(span=200, adjust=False).mean()
    idx = (px / px.iloc[0]).mean(axis=1)           # equal-weight US index
    idx_sma = idx.rolling(200).mean()
    n = len(px)

    cap = START_CAP
    shares = {}
    equity = []
    port_rets = []
    invested_days = 0
    last_rebal = -10 ** 9

    for d in range(LOOKBACK, n):
        price = px.iloc[d]
        val = cap + sum(sh * price[t] for t, sh in shares.items())
        if equity and equity[-1] > 0:
            port_rets.append((val - equity[-1]) / equity[-1])
        equity.append(val)
        if shares:
            invested_days += 1

        # Daily risk-off overlay: exit to cash the moment the broad market breaks
        # below its 200d trend (crashes are fast — a monthly check reacts too late).
        # Re-entry only happens at the next monthly rebalance (avoids whipsaw entries).
        if use_daily_regime and shares and pd.notna(idx_sma.iloc[d]) and idx.iloc[d] < idx_sma.iloc[d]:
            traded = sum(sh * price[t] for t, sh in shares.items())
            cap = val - COST * traded
            shares = {}

        if d - last_rebal >= REBAL:
            last_rebal = d
            regime_ok = (not use_regime) or (
                pd.notna(idx_sma.iloc[d]) and idx.iloc[d] > idx_sma.iloc[d])
            mom = px.iloc[d] / px.iloc[d - LOOKBACK] - 1
            if regime_ok:
                elig = [t for t in px.columns
                        if pd.notna(mom[t]) and mom[t] > 0 and price[t] > ema200[t].iloc[d]]
                ranked = sorted(elig, key=lambda t: mom[t], reverse=True)[:TOPN]
            else:
                ranked = []

            scale = 1.0
            if use_voltarget and len(port_rets) >= 20:
                rv = np.std(port_rets[-20:]) * np.sqrt(252)
                if rv > 0:
                    scale = min(1.0, TARGET_VOL / rv)

            invested_target = val * scale if ranked else 0.0
            per = invested_target / len(ranked) if ranked else 0.0
            target = {t: per / price[t] for t in ranked}

            traded = 0.0
            for t in set(shares) | set(target):
                traded += abs(target.get(t, 0.0) - shares.get(t, 0.0)) * price[t]
            cost = COST * traded
            cap = val - sum(sh * price[t] for t, sh in target.items()) - cost
            shares = target

    eq = np.array(equity)
    pr = np.array(port_rets)
    yrs = len(eq) / 252
    cagr = ((eq[-1] / eq[0]) ** (1 / yrs) - 1) * 100 if eq[0] > 0 and yrs > 0 else 0
    sharpe = pr.mean() / pr.std() * np.sqrt(252) if pr.std() > 0 else 0
    peak = eq[0]; mdd = 0
    for e in eq:
        peak = max(peak, e); mdd = max(mdd, (peak - e) / peak)
    return {"cagr": cagr, "sharpe": sharpe, "maxdd": mdd * 100,
            "total": (eq[-1] / eq[0] - 1) * 100, "invested_pct": invested_days / len(eq) * 100}


def main():
    print(f"US momentum ({HISTORY}, top {TOPN}, {LOOKBACK}d lookback, monthly, cost {COST*100:.2f}%/side)\n")
    px = load_prices()
    print(f"Universe with data: {px.shape[1]} US names, {px.shape[0]} bars\n")
    variants = [
        ("BASE            ", dict()),
        ("+MONTHLY REGIME ", dict(use_regime=True)),
        ("+DAILY REGIME   ", dict(use_daily_regime=True)),
        ("+DAILY REG +VT  ", dict(use_daily_regime=True, use_voltarget=True)),
    ]
    print(f"{'Variant':16} {'CAGR':>7} {'Sharpe':>7} {'MaxDD':>7} {'Total':>9} {'InMkt':>6}")
    print("-" * 60)
    for name, kw in variants:
        r = run(px, **kw)
        print(f"{name} {r['cagr']:>6.1f}% {r['sharpe']:>7.2f} {r['maxdd']:>6.1f}% "
              f"{r['total']:>+8.0f}% {r['invested_pct']:>5.0f}%")
    print("\n(Goal: keep CAGR/Sharpe high while MaxDD drops. InMkt = % of days holding positions.)")


if __name__ == "__main__":
    main()
