# `bb_quality` — Bollinger-Band reversion, non-directional-market-gated (SIM-only A/B twin of `bb`)

**Module:** [`forex/strategy_bb_quality.py`](../forex/strategy_bb_quality.py)
**Added:** 2026-09-02 · **Runs on:** SIM only (never in `LIVE_ALLOWED_STRATEGIES`
/ `LIVE_EUR_ALLOWED_STRATEGIES`)

## What it is

`bb` ([`forex/strategy_bb.py`](../forex/strategy_bb.py)) with **one extra
entry filter and nothing else**. Every BB(20,2) + RSI(14) excursion signal
`strategy_bb.generate_signals` produces is kept only if the market is
**not strongly directional** at the signal bar:

| Gate | Value |
|---|---|
| `|+DI − −DI| ≤ DI_SPREAD_MAX` | **14.0** (≈ the 12y sample 25th percentile) |

`bb` has no ADX/DI of its own; `bb_quality` computes +DI/−DI via
`forex.strategy._adx`. `should_exit`, `size_position`,
`trailing_stop_update` and every constant (`BB_STD=2.0`, `RSI_OB=65`,
`RSI_OS=35`, `ATR_STOP_MULT=2.0`, `TIME_STOP_DAYS=8`, `RISK_PCT=0.0025`, …)
are **delegated straight to `forex.strategy_bb`** — the two can't drift. The
original `strategy_bb.py` is byte-unchanged (the A/B control). `bb_quality`
is mean-reversion, so it stays exempt from the momentum pre-filter, same as
`bb`.

## Why (the hypothesis)

A 12-year / 49-CORE-pair backtest decomposition (2026-09-02). Unlike `ema` /
`rsi`, `bb`'s raw edge is **already stable-positive**: +0.048 R/trade,
bootstrap 95% CI `[+0.024, +0.073]`, +0.038 first half / +0.057 second, PF
1.18, positive in 11 of 14 years. But it gives most of that edge back on the
signals fired into a trend:

| Filter | n | avg R | 1st half / 2nd half | PF | max DD |
|---|---|---|---|---|---|
| base (all signals) | 4,741 | +0.048 | +0.038 / +0.057 | 1.17 | −20 R |
| `|+DI − −DI| ≤ sample p25` | 1,186 | **+0.219** | **+0.247 / +0.200** | **2.07** | **−8 R** |
| `|+DI − −DI| ≤ sample median` | 2,371 | +0.148 | +0.154 / +0.144 | 1.66 | −10 R |

Fading a 2-sigma band excursion works when there's no strong trend pushing
the band-walk further, and loses when +DI/−DI show a real directional move —
exactly what a mean-reversion strategy should look like. The base RSI(14)
confirmation and the ADX filter `bb` *doesn't* have both miss this. The
low-DI-spread subset ran ~4.5× the avg R, PF ~2, positive in **both** halves
with the tightest half-balance of any cut, on ~100 trades/year. A second
filter (`|close − EMA200| ≥ median`, "already stretched") lifted avg R to
+0.27 but worsened the half-balance and halved the sample — left out; the
single DI gate is cleaner. Revisit from the SIM forward data.

## Status / next

**Governance:** the *deterministic-code* step. Forward-test on SIM next to
the untouched `bb`, plus a proper walk-forward (rolling train/test, full
184-pair universe, per-pair cost). Compare via `pnl_ledger` rows
(`strategy='bb_quality'` vs `'bb'`) + the AI journal / give-back reports. No
LIVE consideration until that clears.

Scratch backtests: `~/.claude/.../scratchpad/{st_bb_decompose,composite_verify}.py`.

## Regime-gated A/B twin

**`bb_quality_hv`** ([`forex/strategy_bb_quality_hv.py`](../forex/strategy_bb_quality_hv.py)) —
added 2026-09-04 (H20260904-37779e). Keeps only signals where the AI regime
classifier labels the pair `HIGH_VOLATILITY` at the signal bar. Gate evidence:
base avg_r +0.024; HIGH_VOLATILITY bucket n=33, avg_r +0.247, PF 2.70, both
halves positive (+0.215 / +0.348). Bootstrap 95% CI excludes 0. Status: `backtesting`.
See [`forex_bb_quality_hv_strategy.md`](forex_bb_quality_hv_strategy.md).
