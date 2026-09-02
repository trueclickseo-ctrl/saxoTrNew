# `ema_trend` — EMA(5/30) crossover, clean-crossover-gated (SIM-only A/B twin of `ema`)

**Module:** [`forex/strategy_ema_trend.py`](../forex/strategy_ema_trend.py)
**Added:** 2026-09-02 · **Runs on:** SIM only (never in `LIVE_ALLOWED_STRATEGIES`
/ `LIVE_EUR_ALLOWED_STRATEGIES`)

## What it is

`ema` ([`forex/strategy.py`](../forex/strategy.py)) with **two extra entry
filters and nothing else**. Every EMA(5/30) crossover signal
`strategy.generate_signals` produces is kept only if **both**:

| Gate | Value | Base strategy |
|---|---|---|
| Crossover age | `≤ MAX_CROSSOVER_AGE` (**3 bars**) | scans back up to 15 |
| Directional conviction | `|+DI − −DI| ≥ DI_SPREAD_MIN` (**15.0**) | DI *alignment* only (`+DI > −DI`) |

`should_exit`, `size_position`, `trailing_stop_update` and every constant
(`FAST_EMA=5`, `SLOW_EMA=30`, `ADX_MIN=25`, `ATR_STOP_MULT=1.5`,
`TIME_STOP_DAYS=45`, `RISK_PCT=0.0025`, …) are **delegated straight to
`forex.strategy`** — the two can't drift. The original `forex/strategy.py` is
byte-unchanged (the A/B control). `ema` and `ema_trend` share identical exit
management, so on SIM the *only* difference between the two arms is the entry
gate. `ema_trend` is trend-following, so it **is** momentum-pre-filtered like
its parent (not in `_NO_MOMENTUM_FILTER`).

## Why (the hypothesis)

A 12-year / 49-CORE-pair backtest decomposition (2026-09-02). `ema`'s raw
edge is **+0.036 R/trade and unstable** — +0.064 first half, +0.010 second,
bootstrap 95% CI `[−0.019, +0.090]` spans zero. Broken out by entry context:

| Filter | n | avg R | 1st half / 2nd half | PF | max DD |
|---|---|---|---|---|---|
| base (all signals) | 2,259 | +0.036 | +0.064 / +0.010 | 1.09 | −47 R |
| fresh crossover (age ≤ 3) | 778 | +0.103 | +0.163 / +0.056 | 1.28 | −15 R |
| DI spread ≥ sample median | 1,130 | +0.110 | +0.120 / +0.099 | 1.30 | −32 R |
| **both (fresh + DI)** | **242** | **+0.298** | **+0.356 / +0.250** | **1.97** | **−6 R** |

A stale crossover (price already ran) or a marginal +DI/−DI gap (the
"scissors" chop the ADX filter alone doesn't catch) is where `ema` bleeds.
The fresh, high-conviction subset ran ~8× the avg R, PF ~2, positive in
**both** halves, bootstrap CI `[+0.145, +0.459]`, and the worst
peak-to-trough drawdown fell from −47 R to −6 R.

Adding a third filter (`|close − EMA30| ≤ 1.5 ATR`, "enter near the EMA")
pushed avg R higher (+0.39, n=89) but the sample gets fragile — left out for
now; revisit from the SIM forward data.

## Status / next

**Governance:** the *deterministic-code* step. Forward-test on SIM next to
the untouched `ema`, plus a proper walk-forward (rolling train/test, full
184-pair universe, per-pair cost). Compare via `pnl_ledger` rows
(`strategy='ema_trend'` vs `'ema'`) + the AI journal / give-back reports. No
LIVE consideration until that clears.

Scratch backtests: `~/.claude/.../scratchpad/{ema_decompose,composite_verify}.py`.
