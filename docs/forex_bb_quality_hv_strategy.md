# `bb_quality_hv` — BB-quality + HIGH_VOLATILITY regime gate (SIM-only A/B twin of `bb_quality`)

**Module:** [`forex/strategy_bb_quality_hv.py`](../forex/strategy_bb_quality_hv.py)
**Added:** 2026-09-04 · **Hypothesis:** H20260904-37779e · **Status:** backtesting
**Runs on:** SIM only (never in `LIVE_ALLOWED_STRATEGIES` / `LIVE_EUR_ALLOWED_STRATEGIES`)

## What it is

`bb_quality` ([`forex/strategy_bb_quality.py`](../forex/strategy_bb_quality.py)) with **one
extra entry filter and nothing else**. Every BB(20,2) + RSI(14) + DI-spread(≤14)
signal `strategy_bb_quality.generate_signals` produces is kept only if the AI
regime classifier labels the pair **HIGH_VOLATILITY** at the signal bar.

| Gate | Value |
|---|---|
| `\|+DI − −DI\| ≤ DI_SPREAD_MAX` | **14.0** (inherited from `bb_quality`) |
| `classify_regime(df).label == HIGH_VOLATILITY` | regime check at signal bar |

`should_exit`, `size_position`, `trailing_stop_update` and every constant
(`BB_STD=2.0`, `RSI_OB=65`, `RSI_OS=35`, `ATR_STOP_MULT=2.0`,
`TIME_STOP_DAYS=8`, `RISK_PCT=0.0025`, …) are **delegated straight to
`forex.strategy_bb_quality`** — the twin cannot drift from its parent. The
original `strategy_bb_quality.py` is byte-unchanged (the A/B control).

The `HIGH_VOLATILITY` label is computed by `ai/regime/classifier.py`'s
`classify_regime(df)`. If the classifier fails for any reason (missing data,
import error), `_regime_label()` returns `"UNKNOWN"` and the signal is
**filtered out** (fail-safe: a regime-gated entry never fires on an unknown
regime).

## Why (the hypothesis)

AI Research Analyst decomposition of `bb_quality` by regime-at-entry
(2026-09-04, H20260904-37779e). `bb_quality`'s base avg_r is +0.024 — near-flat,
dominated by commission on close-to-cost signals. Broken out by regime:

| Regime at entry | n | avg R | 1st half / 2nd half | PF |
|---|---|---|---|---|
| HIGH_VOLATILITY | 33 | **+0.247** | **+0.215 / +0.348** | **2.70** |
| RANGING | (majority) | ~flat | | |
| others | | | | |

Both halves independently positive, bootstrap 95% CI excludes 0. The intuition:
BB mean-reversion captures the best moves specifically when the market is making
large swings — an oversold signal during HIGH_VOLATILITY reflects a genuine
over-extension with mean-reversion energy, rather than a slow drift in quiet
conditions where the excursion can simply continue.

## SIM active roster

`bb_quality_hv` is the **8th strategy** in `SIM_ACTIVE_STRATEGIES`. The parent
`bb_quality` runs alongside it (the A/B control, 5th strategy). Both compete for
the same `_SWING_SLOTS` cap.

## Parent doc

[`forex_bb_quality_strategy.md`](forex_bb_quality_strategy.md) — describes the
DI-spread gate, the decomposition methodology, and the status/next steps.
