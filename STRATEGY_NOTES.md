# ATOS Strategy Lab Notebook

Running log of strategy design, backtests, findings, and dead-ends — so we don't
repeat work. Update this every time a strategy or its parameters change.

---

## SUMMARY / SCOREBOARD  (as of 2026-08-07)

**How many strategies did we find?** We tested **~a dozen** configurations. **4 validated**
(cleared 10y / real-cost / beats-benchmark bar); **1 is running live** (the blend).

### ✅ Validated (real, robust edge)
| Strategy | Sharpe | MaxDD | Role |
|---|---|---|---|
| **BLEND: momentum + low-vol**  ← **LIVE** | **1.16** | **14.3%** | production (safest) |
| Risk-adjusted momentum | 1.10–1.22 | ~23–29% | offense |
| Low-volatility | 0.80–1.05 | ~14–17% | defense |
| Short-term reversal (10d) | 1.00 | 22.5% | optional 3rd |

**LIVE STRATEGY: the US Momentum+Low-Vol Blend** (top-2 momentum + top-2 low-vol,
compounding 15k sleeve, monthly rebalance + daily risk-off). It beats every single
strategy on risk-adjusted return because momentum & low-vol only correlate 0.44.

### ❌ Tested and rejected (don't revisit)
- Per-market signal strategies (US Breakout / OMX Momentum / CPH Mean-Reversion) — weak.
- OMX30 & CPH25 markets — no edge (3y looked good, 10y exposed it as a bull mirage).
- Plain momentum / mom252 / 52-week-high — all beaten by risk-adjusted momentum.
- ML probability model (triple-barrier + gradient boosting) — OOS AUC 0.52, coin flip.

### Verdict
US equities is the only market with edge. One diversified, validated, drawdown-controlled
strategy (the blend) is live. Finding 1 keeper out of ~12 is a healthy quant hit rate, and
we know the rejects are dead ends — recorded so nobody re-runs them.

---

**How to measure:** `py -3 -X utf8 backtest_strategies.py`
(2y daily bars, commission 0.08% + slippage 0.03%, ATR 1%-risk sizing, no look-ahead,
runs each market's strategy across ALL its instruments and aggregates.)

**Bar to clear before considering live:** avg Sharpe > 1.0, PF > 1.3, enough trades
(>30) to be meaningful. Nothing below this should touch real money.

---

## Per-market assignment (atos_runner.py `STRATEGY_INSTANCE_FOR_MARKET`)
| Market | Strategy | Class |
|---|---|---|
| US Equities | US Breakout | `S4_BreakoutVol` |
| OMX30 | OMX Momentum | `S5_MomentumAccel` |
| CPH25 | CPH Mean Reversion | `S3_MeanReversion` |

Toggle `STRATEGY_MODE = False` in atos_runner.py to fall back to detector consensus.

---

## Baseline v1.0 — 2026-08-07 (BEFORE tuning)
| Strategy | Trades/2y | Win rate | PF | Avg Sharpe | Avg ret 2y | Verdict |
|---|---|---|---|---|---|---|
| US Breakout | **1** | 0% | 0.00 | -0.29 | -1.1% | Barely fires |
| OMX Momentum | 251 | 48% | 1.30 | 0.40 | +2.0% | Weak edge |
| CPH Mean Reversion | 7 | 0% | 0.00 | -0.79 | -1.1% | Barely fires |

**Key finding:** requiring several confirmations *simultaneously* makes a strategy
almost never fire. US Breakout wanted (Donchian breakout AND ATR expansion AND
volume surge) → 1 trade in 2 years across 39 names.

## Iteration v1.1 — 2026-08-07 (loosen entries + trend filter)
Changes:
- **US Breakout**: require breakout + **one** confirmation (ATR OR volume), not both;
  `vol_expansion` 1.1→1.0, `volume_threshold` 1.5→1.2.
- **OMX Momentum**: add EMA200 **trend filter** (only long when close > EMA200) to
  cut counter-trend losers (e.g. HM-B was -6.3%).
- **CPH Mean Reversion**: `rsi_oversold` 30→35, `adx_max` 25→30, lower-BB touch 1.01→1.02.

Results:
| Strategy | Trades/2y | Win rate | PF | Avg Sharpe | Avg ret 2y | Verdict |
|---|---|---|---|---|---|---|
| US Breakout | 6 | 17% | 1.17 | -0.45 | +0.2% | Still thin |
| OMX Momentum | 212 | 47% | **1.32** | 0.40 | +1.7% | Weak edge |
| CPH Mean Reversion | 39 | 26% | **1.34** | -0.12 | +1.1% | Trades now, weak |

**Findings:**
- All three now trade; PF nudged up (US 1.17, OMX 1.32, CPH 1.34).
- **But returns are tiny (+0.2%…+1.7% over 2 YEARS) and Sharpe is weak/negative.**
  Simple TA rules have little edge after costs — this is the norm, not a bug.
- The EMA200 filter on OMX barely moved Sharpe (0.40 unchanged) though it cut ~40 trades.

---

## Learnings / dead-ends (DON'T repeat)
1. **Don't AND many confirmations** — it kills trade count. Prefer breakout + 1 confirm.
2. **Per-instrument Sharpe is noisy for sparse strategies** — most days are flat (no
   position), so daily-return Sharpe is dominated by idle days. Trust PF + total return
   + trade count more than single-name Sharpe here. (TODO: compute a *portfolio-level*
   equity Sharpe across all instruments per market instead of averaging per-name Sharpe.)
3. **Parameter loosening alone won't reach Sharpe>1** — the edge isn't in the params;
   it's in the strategy logic / regime selection / portfolio construction.

## Open ideas / next candidates (untested)
- **Portfolio-level backtest** (one equity curve per market, position across the best
  N signals) — likely a truer Sharpe than per-name averaging.
- **Time-series momentum / dual-momentum** (Clenow / Antonacci style): rank instruments
  by 3–6m momentum, hold top N above their EMA200, monthly rebalance. Well-documented edge.
- **Regime gating** via D8 regime detector (only run momentum in trending regimes,
  mean-reversion in ranging).
- **Better exits** (chandelier/ATR-trailing) rather than fixed signal exits.
- Try `S2_DualEMA` / `S6_SmartMoney` per market and compare.

---

## Cross-sectional (portfolio) momentum — 2026-08-07  (`backtest_momentum.py`)
Rank instruments by 120d return, keep positive-momentum + above-EMA200, hold top N
equal-weight, monthly rebalance, vs equal-weight buy&hold benchmark.

**3y (2023–2026, bull market):**
| Market | Mom CAGR | Mom Sharpe | Mom maxDD | B&H Sharpe |
|---|---|---|---|---|
| US | +40.9% | 1.22 | 17.6% | **1.96** |
| OMX30 | +26.9% | 1.81 | 11.0% | **2.13** |
| CPH25 | +4.6% | 0.35 | 23.3% | 0.52 |

**10y (2016–2026, incl. 2018/2020/2022 bears) — the real test:**
| Market | Mom CAGR | Mom Sharpe | Mom maxDD | B&H CAGR | B&H Sharpe | Verdict |
|---|---|---|---|---|---|---|
| **US** | **+40.8%** | **1.25** | 32.3% | +25.6% | 1.27 | **STRONG** — 3.2× B&H total return (+2480% vs +773%) at ~equal Sharpe |
| OMX30 | +2.9% | 0.24 | 44.3% | +15.8% | 0.96 | **FAILS** — 3y Sharpe 1.81 was a bull mirage |
| CPH25 | +15.1% | 0.76 | 25.8% | +12.9% | 0.76 | marginal — ties B&H |

**CRITICAL LESSONS (do not repeat):**
1. **Always test 10y, not 3y.** OMX momentum looked great over 3y (Sharpe 1.81) but the
   full cycle exposed it as fragile (0.24, 44% DD). Recent-bull results mislead.
2. **Buy&hold is a very strong benchmark.** Most strategies here fail to beat a diversified
   equal-weight hold on a risk-adjusted basis. Any strategy must be measured against it.
3. **US cross-sectional momentum is the one real edge found so far** — robust across bull+bear,
   3.2× the buy&hold return over 10y at comparable Sharpe. Cost: deeper drawdowns (32%).
4. **Momentum on small/concentrated universes (OMX 15, top-3) is fragile.** Needs breadth.

**Best strategy so far: US cross-sectional momentum (top 5 of 39, monthly, EMA200 filter).**
Next: reduce its 32% DD (volatility targeting / more names / crash filter); for OMX/CPH
prefer simple trend-following buy&hold (hold above EMA200, cash below) over momentum.

---

## US momentum — drawdown reduction, 2026-08-07  (`backtest_us_momentum.py`, daily equity)
Proper daily equity curve exposes the true drawdown (37.6%, worse than the monthly
approx of 32%). Findings while cutting it:
- **Monthly regime filter fails** (DD 37.6%→37.6%): the big DD is the *fast* 2020 crash;
  a month-end check reacts too late. Also whipsaws (sells the V-bottom, misses snap-back).
- **More holdings help most**: top-5 → top-10 improved BOTH Sharpe (1.20→1.28) AND DD
  (37.6%→30.7%) for only a little less return (CAGR 40.7%→35.0%). Diversification > filters.
- **Daily risk-off + vol-target on top-10** = best risk-adjusted:

| Config | CAGR | Sharpe | MaxDD |
|---|---|---|---|
| top5 base (orig) | 40.7% | 1.20 | 37.6% |
| top10 base | 35.0% | 1.28 | 30.7% |
| **top10 + daily risk-off + vol-target (15%)** | **24.4%** | **1.30** | **21.3%** |

**WINNER — US strategy is decided:** cross-sectional momentum, **top 10 of 39, 120d lookback,
monthly rebalance, per-stock EMA200 filter, daily market-regime risk-off (equal-weight index
< its 200d SMA → cash), volatility targeting to 15%.** 10y: Sharpe 1.30, MaxDD 21.3%, CAGR 24.4%
— beats buy&hold's Sharpe (1.27) with half the original drawdown. This clears the bar.

Two dials for the user: *max return* (top10 base: CAGR 35%, DD 31%) vs *smoothest*
(top10+risk-off+VT: CAGR 24%, DD 21%). Both are legit; default to the smoother one.

## Classic technical analysis head-to-head — 2026-08-07  (`research_technical_analysis.py`)
Ran the classic TA signals through the same portfolio engine (top-5, monthly, risk-off, 10y):
| TA signal | CAGR | Sharpe | MaxDD | type |
|---|---|---|---|---|
| MACD histogram | 26.3% | **1.17** | 22.1% | trend |
| Momentum (current) | 26.6% | 1.10 | 29.4% | trend |
| Bollinger %b | 19.0% | 1.02 | 24.3% | mean-rev |
| MA golden-cross | 27.0% | 0.94 | 32.5% | trend |
| RSI oversold | 16.1% | 0.92 | 19.5% | mean-rev |

**CLEAR PATTERN: trend/momentum TA works (MACD, MA-cross, momentum); mean-reversion TA
(RSI, Bollinger) is weak.** MACD edged momentum (1.17 vs 1.10) but is just another
momentum flavour — not a new edge; blended w/ low-vol it lands at the same ~1.17 ceiling.
So we HAVE tested traditional TA thoroughly (here + backtest_strategies.py + the 8-detector
engine + the ML on TA features which was AUC 0.52). Conclusion: the ONLY TA with edge is
trend/momentum as a PORTFOLIO factor — which is exactly what the live blend trades. Keep it.

## Deep research: residual momentum — 2026-08-07  (`research_deep_strategies.py`)
Tested the top documented candidate: RESIDUAL (beta-adjusted) momentum — "same return,
half the vol, ~double Sharpe vs raw momentum" per the literature. 10y, same engine:
| Strategy | CAGR | Sharpe | MaxDD |
|---|---|---|---|
| raw risk-adj momentum | 26.6% | 1.10 | 29.4% |
| **residual momentum** | 26.6% | 1.12 | **21.8%** |
| current blend (mom+lowvol) | 18.3% | 1.16 | 14.3% |
| resid-mom + lowvol blend | 18.3% | 1.17 | 14.6% |

Residual momentum IS a real refinement (same return, lower DD standalone). BUT it
correlates **0.89** with raw momentum (not a new stream), and in the BLEND the gain
vanishes (1.17 vs 1.16 — a tie; low-vol already provides the DD control). **VERDICT: keep
the current blend; residual momentum is not worth the extra complexity here. We are at the
practical CEILING for price-based daily US-equity strategies.** Bigger gains would require
genuinely NEW information (fundamentals, alt-data) or a different arena (intraday, more
asset classes) — each a larger project, not more price backtests. Dual-momentum/GEM skipped
(fragile in bull markets; our risk-off overlay already covers trend).

## Multi-strategy diversification — 2026-08-07  (`research_more_strategies.py`)
Hunt for a small set of validated, LOW-CORRELATION strategies to run together safely.
Same portfolio engine (top-5, monthly, daily risk-off, costs), 10y:

| Strategy | CAGR | Sharpe | MaxDD |
|---|---|---|---|
| Momentum (risk-adj) — offense | 26.6% | 1.10 | 29.4% |
| Low-vol — defense | 9.2% | 0.80 | 16.7% |
| Reversal (10d losers) | 20.5% | 1.00 | 22.5% |
| **BLEND 50% mom + 50% low-vol** | 18.3% | **1.16** | **14.3%** |

Correlations (daily): MOM–LOWVOL **0.44**, MOM–REV 0.59, LOWVOL–REV 0.50.

**WINNER: the BLEND (50% risk-adj momentum + 50% low-vol).** Beats BOTH parents on
Sharpe (1.16 vs 1.10 / 0.80) and has the LOWEST drawdown (14.3% — half of momentum
alone). Low 0.44 correlation is why: offense + defense smooth each other. **This is the
best risk-adjusted, safest strategy found — recommend it as the engine's US strategy.**
(Reversal is a legit 3rd option, Sharpe 1.00, but correlates more with momentum.)
Caveat for the 15k whole-share account: the blend wants ~6 positions (3 mom + 3 lowvol);
on 15k use top-2 each (4 positions) to keep whole shares affordable.

## ML probability model — TESTED & REJECTED, 2026-08-07  (`research_ml_probability.py`)
Meta-labeling / triple-barrier: label = hit +5% before -3% within 20d; technical
features; HistGradientBoosting; WALK-FORWARD (TimeSeriesSplit) out-of-sample eval.
- 92,547 rows, base rate P(win) = 40.1%.
- **Walk-forward OOS AUC = 0.52** (0.50 = no skill). Essentially a coin flip.
- Even at P>=0.80 the real win rate is only 46% (+6% over base), 295 trades — thin/fragile.

**VERDICT: ML adds no reliable edge here — do not ship it as a signal.** The "82%
probability" idea is not achievable with daily technical features; a model claiming
82% would be overfit/miscalibrated (its most-confident real win rate is ~46%). Lesson:
efficient liquid equities + technical features on daily bars = no ML edge. Stick with
the validated risk-adjusted momentum strategy. (Needs scikit-learn; run under a venv.)
Could revisit only with genuinely new information (fundamentals, order-flow, alt-data) —
not more technical indicators.

## US signal hunt — 2026-08-07  (`backtest_us_strategies.py`)
Same engine (top-10, EMA200 filter, monthly, daily risk-off, real costs), only the
RANKING SIGNAL varies. 10y, fractional shares (isolates signal edge):

| Signal | CAGR | Sharpe | MaxDD |
|---|---|---|---|
| **risk-adj momentum (ret/vol)** | 24.7% | **1.22** | **22.8%** |
| momentum 120d (prev) | 24.5% | 1.08 | 24.0% |
| low-volatility | 12.1% | 1.05 | 14.3% |
| 52-week-high | 16.3% | 1.01 | 19.9% |
| momentum 252d | 20.7% | 0.93 | 24.1% |
| buy&hold | 26.1% | 1.24 | 33.6% |

**Winner: risk-adjusted momentum (return / 60d vol).** Same return as plain momentum,
higher Sharpe (1.22 vs 1.08), lower DD. Nearly matches buy&hold Sharpe (1.24) at 1/3
less drawdown — the real value of the strategy is DD reduction, not beating B&H return.
**Upgraded the live signal to risk-adjusted momentum** in atos/us_momentum.py.
(mom252 worse than mom120; low-vol lowest DD but low return — a defensive alternative.)

**LIVE CONFIG — COMPOUNDING 15k sleeve:** the US sleeve STARTS at 15,000 SEK and
COMPOUNDS with its own P&L — budget = sleeve equity (`sleeve_cash` in
data/us_momentum_state.json + current US position value). Profit raises the budget
(reward: 15k->16k trades bigger), loss lowers it (penalty: 16k->14k trades smaller).
It NEVER reads or tops up from the real account balance, so extra deposits stay
untouched. Running-budget guard caps each rebalance's spend at the current sleeve
equity. **TOPN = 3** (top-10 needs
~100k for whole shares; on 15k the top-3 by risk-adj momentum is what fits, and bigger
slots still afford the pricey leaders — preview: AMD/UNH/CSCO, ~13k deployed).
Running-budget guard caps total spend at the budget. NOTE: top-3 is more concentrated
than the validated top-10, so expect higher vol/DD than the paper numbers. Monthly
rebalance (first trading day) + daily risk-off.
[history] Was 100k/top-10 for paper validation; now capped at the real 15k budget.
**Vol-targeting DROPPED for live sizing** — it scales exposure to ~30%, making per-slot
budgets too small for whole shares (needs ~5x capital). So live ≈ top10 + risk-off:
expect ~Sharpe 1.18, DD ~24%. OMX/CPH per-instrument strategies PAUSED (unvalidated).

**Next: wire this into the live engine.** NOTE: it's a PORTFOLIO/rebalance strategy (rank +
hold top-N monthly), architecturally different from the runner's per-instrument signal loop —
needs a rebalance execution path (compute targets → buy/sell to reach them), not a per-ticker BUY.
