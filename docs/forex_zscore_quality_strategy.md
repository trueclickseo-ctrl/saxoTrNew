# `zscore_quality` — Z-Score reversion, non-directional-market-gated (SIM-only A/B twin of `zscore`)

**Module:** [`forex/strategy_zscore_quality.py`](../forex/strategy_zscore_quality.py)
**Added:** 2026-09-02 · **Runs on:** SIM only (never in `LIVE_ALLOWED_STRATEGIES`
/ `LIVE_EUR_ALLOWED_STRATEGIES`)

## What it is

`zscore` ([`forex/strategy_zscore.py`](../forex/strategy_zscore.py)) with
**one extra entry filter and nothing else**. Every 2σ-excursion signal
`strategy_zscore.generate_signals` produces is kept only if the market is
**not strongly directional** at the signal bar:

| Gate | Value |
|---|---|
| `|+DI − −DI| ≤ DI_SPREAD_MAX` | **14.0** (≈ the 12y sample p25 — same `_adx` / pairs as `bb_quality`) |

`zscore` has no ADX/DI of its own; `zscore_quality` computes +DI/−DI via
`forex.strategy._adx`. `should_exit` and `size_position` and every constant
(`Z_ENTRY=2.0`, `Z_EXIT=0.3`, `ATR_STOP_MULT=2.5`, `TIME_STOP_DAYS=12`,
`RISK_PCT=0.0025`, …) are **delegated straight to `forex.strategy_zscore`**.
Neither module has a `trailing_stop_update` — clean A/B, identical exit
management. The original `strategy_zscore.py` is byte-unchanged (the
control). `zscore_quality` is mean-reversion, so it stays exempt from the
momentum pre-filter, same as `zscore`.

## Why (the hypothesis)

A 12-year / 49-CORE-pair backtest decomposition (2026-09-02). `zscore` as a
whole is a **coin flip**: +0.002 R/trade, bootstrap 95% CI `[−0.020, +0.024]`
(spans zero), first half −0.028 / second +0.027, PF 1.01. But split by
directional conviction it is the same story as `bb`:

| filter | n | avg R | 1st / 2nd half |
|---|---|---|---|
| base | 2,760 | +0.002 | −0.028 / +0.027 |
| `|+DI − −DI|` bottom quartile | 690 | **+0.132** | **+0.120 / +0.144** |
| Q2 | 690 | +0.001 | |
| Q3 | 690 | +0.002 | |
| top quartile | 690 | −0.128 | −0.171 / −0.097 |

Fading a 2σ excursion works when no strong trend is pushing price further
from the mean, and loses when +DI/−DI show a real directional move.
`zscore`'s only filter — close within ±1% of EMA200 — is far too loose to
catch this. Gating on a low DI spread lifted avg R from ~0 to +0.13, PF
~1.0 → ~1.4, positive in **both** halves.

**Per-pair (the "better tradeable pairs" question):** the decomposition also
found **19/49 CORE pairs stable-positive in both halves** — mostly
EUR/USD/CHF/AUD/CAD crosses (AUDCAD, EURGBP, EURUSD, EURJPY, USDCHF, …).
Trading only those → +0.084 R, PF 1.43, CI `[+0.049, +0.120]`. The losers
are the **NZD pairs** (NZDUSD, NZDCAD, EURNZD, GBPNZD, …) and GBP-commodity
crosses — thin, trend-prone currencies, bad for mean reversion. A per-pair
whitelist is a separate, more curve-fit lever; `zscore_quality` uses only
the mechanical DI gate. Scoping the SIM universe to the stable set is a
config change to consider from the forward data.

## Status / next

**Governance:** the *deterministic-code* step. Forward-test on SIM next to
the untouched `zscore`, plus a proper walk-forward (rolling train/test, full
184-pair universe, per-pair cost). Compare via `pnl_ledger` rows
(`strategy='zscore_quality'` vs `'zscore'`) + the AI journal / give-back
reports. No LIVE consideration until that clears.

**Note:** `bb` and `zscore` are the same idea (fade a σ-extreme) and respond
to the *exact same* filter (low DI spread) — worth consolidating in the
eventual strategy cleanup.

Scratch backtest: `~/.claude/.../scratchpad/zscore_decompose.py`.

## Regime-gated A/B twin

**`zscore_quality_tb`** ([`forex/strategy_zscore_quality_tb.py`](../forex/strategy_zscore_quality_tb.py)) —
added 2026-09-04 (H20260904-c9e606). Keeps only signals where the AI regime
classifier labels the pair `TRENDING_BULLISH` at the signal bar. Gate evidence:
base avg_r +0.004; TRENDING_BULLISH bucket n=54, avg_r +0.150, PF 2.23, both
halves positive (+0.231 / +0.119). Bootstrap 95% CI excludes 0. Note: RANGING
(the nominal design intention) failed the gate — `TRENDING_BULLISH` is the
empirical winner, not the prior. Status: `backtesting`.
See [`forex_zscore_quality_tb_strategy.md`](forex_zscore_quality_tb_strategy.md).
