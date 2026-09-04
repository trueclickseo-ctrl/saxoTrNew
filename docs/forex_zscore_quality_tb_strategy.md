# `zscore_quality_tb` — Z-Score-quality + TRENDING_BULLISH regime gate (SIM-only A/B twin of `zscore_quality`)

**Module:** [`forex/strategy_zscore_quality_tb.py`](../forex/strategy_zscore_quality_tb.py)
**Added:** 2026-09-04 · **Hypothesis:** H20260904-c9e606 · **Status:** backtesting
**Runs on:** SIM only (never in `LIVE_ALLOWED_STRATEGIES` / `LIVE_EUR_ALLOWED_STRATEGIES`)

## What it is

`zscore_quality` ([`forex/strategy_zscore_quality.py`](../forex/strategy_zscore_quality.py)) with
**one extra entry filter and nothing else**. Every 2σ-excursion + DI-spread(≤14)
signal `strategy_zscore_quality.generate_signals` produces is kept only if the AI
regime classifier labels the pair **TRENDING_BULLISH** at the signal bar.

| Gate | Value |
|---|---|
| `\|+DI − −DI\| ≤ DI_SPREAD_MAX` | **14.0** (inherited from `zscore_quality`) |
| `classify_regime(df).label == TRENDING_BULLISH` | regime check at signal bar |

`should_exit`, `size_position` and every constant (`Z_ENTRY=2.0`, `Z_EXIT=0.3`,
`ATR_STOP_MULT=2.5`, `TIME_STOP_DAYS=12`, `RISK_PCT=0.0025`, …) are **delegated
straight to `forex.strategy_zscore_quality`** — the twin cannot drift from its
parent. The original `strategy_zscore_quality.py` is byte-unchanged (the A/B
control). No `trailing_stop_update` (neither parent nor twin has one).

The `TRENDING_BULLISH` label is computed by `ai/regime/classifier.py`'s
`classify_regime(df)`. If the classifier fails for any reason (missing data,
import error), `_regime_label()` returns `"UNKNOWN"` and the signal is
**filtered out** (fail-safe: a regime-gated entry never fires on an unknown
regime).

## Why (the hypothesis)

AI Research Analyst decomposition of `zscore_quality` by regime-at-entry
(2026-09-04, H20260904-c9e606). `zscore_quality`'s base avg_r is +0.004 — flat,
commission-dominated. Broken out by regime:

| Regime at entry | n | avg R | 1st half / 2nd half | PF |
|---|---|---|---|---|
| TRENDING_BULLISH | 54 | **+0.150** | **+0.231 / +0.119** | **2.23** |
| RANGING | (majority) | ~flat | | |
| others | | | | |

Both halves independently positive, bootstrap 95% CI excludes 0.

**Counterintuitive result:** `zscore_quality` was designed for the RANGING
regime — fade a 2σ-excursion in a directionless market. But RANGING failed the
gate (neither half consistently positive). The empirical winner is
`TRENDING_BULLISH`. The intuition: z-score mean-reversion pullbacks work best
with a mild directional tailwind. In a TRENDING_BULLISH context a 2σ oversold
excursion represents a genuine over-extension — the trend provides a natural
structural attractor that pulls price back to the mean. In pure ranging, the
excursion can walk further without a bias to reverse.

## SIM active roster

`zscore_quality_tb` is the **9th strategy** in `SIM_ACTIVE_STRATEGIES`. The
parent `zscore_quality` runs alongside it (the A/B control, 7th strategy). Both
compete for the same `_SWING_SLOTS` cap.

## Parent doc

[`forex_zscore_quality_strategy.md`](forex_zscore_quality_strategy.md) — describes
the DI-spread gate, the decomposition methodology, and the status/next steps.
