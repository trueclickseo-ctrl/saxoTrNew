"""
backtest_strategies.py
-----------------------
Validate the three per-market strategies before trusting them to trade.
Runs each market's assigned strategy across ALL its instruments over ~2 years of
daily bars, using atos/backtester.py (proper commission + slippage + ATR sizing,
no look-ahead), and aggregates the results per strategy.

The strategies have FIXED parameters (nothing is fitted to this data), so a full
in-sample run is a fair out-of-sample-style test of their edge.

    py -3 -X utf8 backtest_strategies.py
"""
import warnings; warnings.filterwarnings('ignore')
import sys, os; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import yfinance as yf

from atos.features import add_all
from atos.backtester import Backtester
from atos.strategies import S3_MeanReversion, S4_BreakoutVol, S5_MomentumAccel
from atos.universe import MARKET_GROUPS

HISTORY = "2y"
STRAT = {
    "US Equities": ("US Breakout",       S4_BreakoutVol),
    "OMX30":       ("OMX Momentum",      S5_MomentumAccel),
    "CPH25":       ("CPH Mean Reversion", S3_MeanReversion),
}


def backtest_market(market, name, cls):
    tickers = sorted(MARKET_GROUPS[market])
    raw = yf.download(tickers, period=HISTORY, interval="1d", group_by="ticker",
                      auto_adjust=True, threads=True, progress=False)
    per = []
    trades = closed = wins = 0
    gp = gl = 0.0
    rets, sharpes, dds = [], [], []
    for t in tickers:
        try:
            df = raw[t].dropna(how="all")
            if len(df) < 150:
                continue
            df = add_all(df)
        except Exception:
            continue
        r = Backtester(cls()).run(df)
        if r.num_trades == 0:
            continue
        trades += r.num_trades
        rets.append(r.total_return_pct)
        if r.sharpe_ratio:
            sharpes.append(r.sharpe_ratio)
        dds.append(r.max_drawdown_pct)
        for tr in r.trade_log:
            closed += 1
            wins += 1 if tr["profitable"] else 0
            if tr["pnl"] > 0: gp += tr["pnl"]
            else: gl += abs(tr["pnl"])
        per.append((t, r.num_trades, r.total_return_pct, r.sharpe_ratio, r.win_rate))

    wr = wins / closed * 100 if closed else 0.0
    pf = gp / gl if gl > 0 else (gp if gp > 0 else 0.0)
    print(f"\n{'='*70}\n{name}  ({market})  —  strategy '{cls().name}'\n{'='*70}")
    print(f"  Instruments that traded : {len(per)} / {len(tickers)}")
    print(f"  Total trades            : {trades}")
    print(f"  Win rate                : {wr:.0f}%   |   Profit factor: {pf:.2f}")
    print(f"  Avg return / instrument : {np.mean(rets) if rets else 0:+.1f}%  (over {HISTORY})")
    print(f"  Avg Sharpe              : {np.mean(sharpes) if sharpes else 0:.2f}")
    print(f"  Avg max drawdown        : {np.mean(dds) if dds else 0:.1f}%")
    if per:
        per.sort(key=lambda x: x[2], reverse=True)
        print("  Best / worst instruments:")
        for t, n, ret, sh, w in per[:3]:
            print(f"     {t:12} {n:3} trades  {ret:+7.1f}%  Sharpe {sh:5.2f}  WR {w:.0f}%")
        for t, n, ret, sh, w in per[-2:]:
            print(f"     {t:12} {n:3} trades  {ret:+7.1f}%  Sharpe {sh:5.2f}  WR {w:.0f}%")
    return {"name": name, "trades": trades, "win_rate": wr, "pf": pf,
            "avg_ret": np.mean(rets) if rets else 0, "avg_sharpe": np.mean(sharpes) if sharpes else 0,
            "avg_dd": np.mean(dds) if dds else 0, "instruments": len(per)}


def main():
    print(f"Backtesting 3 strategies over {HISTORY} of daily bars "
          f"(commission 0.08% + slippage 0.03%, ATR sizing)...")
    summary = [backtest_market(m, name, cls) for m, (name, cls) in STRAT.items()]
    print(f"\n{'='*70}\nVERDICT (rule of thumb: Sharpe>1 good, PF>1.3 tradeable, WR context-dependent)\n{'='*70}")
    for s in summary:
        verdict = "LOOKS TRADEABLE" if (s["avg_sharpe"] >= 0.8 and s["pf"] >= 1.2 and s["trades"] >= 10) \
                  else ("THIN / INCONCLUSIVE" if s["trades"] < 10 else "WEAK — needs work")
        print(f"  {s['name']:20} Sharpe {s['avg_sharpe']:5.2f} | PF {s['pf']:5.2f} | "
              f"WR {s['win_rate']:3.0f}% | {s['trades']:3} trades -> {verdict}")


if __name__ == "__main__":
    main()
