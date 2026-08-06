# ATOS Strategy Lab Notebook

Running log of strategy design, backtests, findings, and dead-ends — so we don't
repeat work. Update this every time a strategy or its parameters change.

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
