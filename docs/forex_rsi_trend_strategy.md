# `rsi_trend` — RSI(2) pullback, regime-gated (SIM-only A/B twin of `rsi`)

**Module:** [`forex/strategy_rsi_trend.py`](../forex/strategy_rsi_trend.py)
**Added:** 2026-09-02 · **Runs on:** SIM only (never in `LIVE_ALLOWED_STRATEGIES`
/ `LIVE_EUR_ALLOWED_STRATEGIES`)

## What it is

`rsi` with **one extra entry filter and nothing else**. Every RSI(2) signal
`strategy_rsi.generate_signals` produces is passed through
`ai.regime.classifier.classify_regime(bars)` and kept only if:

| Signal | Regime label required |
|---|---|
| Buy  | `TRENDING_BULLISH` |
| Sell | `TRENDING_BEARISH` |

`should_exit`, `size_position`, `trailing_stop_update` and every constant
(`RSI_OVERSOLD=10`, `ATR_STOP_MULT=1.5`, `TIME_STOP_DAYS=12`, `RISK_PCT=0.0025`, …)
are **delegated straight to `strategy_rsi`** — the two can't drift. The
original `strategy_rsi.py` is byte-unchanged (the A/B control). Both `rsi`
and `rsi_trend` are in `PROFIT_LADDER_STRATEGIES`, so on SIM the *only*
difference between the two arms is the entry gate.

## Why (the hypothesis)

An 11-year / 49-CORE-pair backtest decomposition (2026-09-02). RSI(2)'s raw
edge is **+0.021 R/trade but unstable** — ~0 in 2014–2020, +0.046 in
2021–2026. Broken out by the regime label at entry:

| Regime at entry | Raw avg R | 2014–20 | 2021–26 | n |
|---|---|---|---|---|
| **TRENDING_BULLISH** | **+0.088** | +0.083 | +0.092 | 669 |
| **TRENDING_BEARISH** | **+0.040** | +0.061 | +0.019 | 715 |
| RANGING | +0.011 | **−0.029** | +0.048 | 4,627 |
| HIGH_VOLATILITY | −0.013 | −0.003 | −0.026 | 144 |

RSI(2) is a mean-reversion signal, but its stable, sizeable edge is
entirely **"buy the dip *in an established trend*"**. RANGING (75% of the
signals) is where the regime-luck lives.

Gating on `TRENDING_*` (backtest, trailing+breakeven exits):

| | Trades | avg R | 2014–20 / 2021–26 | PF | Max DD |
|---|---|---|---|---|---|
| `rsi` (all signals) | 6,192 | +0.022 | −0.003 / +0.046 | 1.09 | 82 R |
| `rsi_trend` (TRENDING only) | 1,065 | **+0.081** | **+0.064 / +0.101** | 1.37 | **18 R** |
| `rsi_trend` + top-3/day | 945 | +0.089 | +0.070 / +0.112 | 1.41 | 11 R |

Net expectancy turns positive at **~€150 risk/trade** (real Saxo cost
€9.89) instead of ~€600, on ~1.6 trades/week.

## Status / next

**Governance:** this is the *deterministic-code* step. Forward-test on SIM
next to the untouched `rsi`, plus a proper walk-forward (rolling train/test,
full 184-pair universe, per-pair cost). Compare via the `pnl_ledger` rows
(`strategy='rsi_trend'` vs `'rsi'`) + the AI journal / give-back reports
(both break down by strategy). No LIVE consideration until that clears.

Scratch backtests: `~/.claude/.../scratchpad/rsi_{expectancy_vs_size,
robustness,regime_decompose,topn}.py`.
