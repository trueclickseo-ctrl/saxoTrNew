# Forex Donchian Breakout Strategy (strict)

**File**: [`forex/strategy_donchian.py`](../forex/strategy_donchian.py)
**Runner key**: `"donchian"`
**Type**: breakout / trend-following
**Where it runs**: **SIM only**.
**Momentum pre-filter**: **yes**.
**A/B sibling**: [`donchian_quality`](forex_donchian_quality_strategy.md) — a separate, filtered variant. This one is left untouched.

> Not to be changed. Described exactly as it runs today.

---

## Concept

A 30-day channel breakout with **two mandatory confirmations**: the break must
be in the direction of the EMA(200) macro trend, and ADX(14) must be ≥ 25.
That combination removes counter-trend entries and dead-range false breakouts.
Exit trails a proportional 15-day channel.

The channel uses the **prior 30 closes** (`c.iloc[-(31):-1]`), i.e. today's bar
is compared against a channel that excludes it.

---

## Parameters

| Param | Value | Constant |
|---|---|---|
| Breakout channel | 30 days | `BREAKOUT_PERIOD` |
| Exit channel | 15 days | `EXIT_PERIOD` |
| Trend EMA | 200 | `EMA_TREND` |
| ADX period / minimum | 14 / **25** | `ADX_PERIOD` / `ADX_MIN` |
| ATR period / stop multiple | 14 / **2.0×** | `ATR_PERIOD` / `ATR_STOP_MULT` |
| Risk per trade | **0.25%** (`RISK_PCT = 0.0025`; docstring says "1%" — stale) | |
| Time stop | 30 calendar days | `TIME_STOP_DAYS` |
| Min bars | `200 + 14 + 5` = 219 | `MIN_BARS` |
| `MAX_POSITIONS` | **4 — declared but NEVER enforced.** The runner uses `SLOTS_PER_STRATEGY["donchian"] = _SWING_SLOTS` (184). This is a known, deliberate gap — the [`donchian_quality`](forex_donchian_quality_strategy.md) variant *does* enforce its 4-cap. | |

---

## Entry — all three required

| | Long | Short |
|---|---|---|
| Breakout | `close > max(prior 30 closes)` | `close < min(prior 30 closes)` |
| Macro trend | `close > EMA200` | `close < EMA200` |
| Trend strength | `ADX(14) ≥ 25` | same |

- `score` = `(close − breakout_level) / ATR` — how far past the channel, in ATR units.
- `stop_price` = `close ∓ 2.0 × ATR(14)`.

## Exit — `should_exit()`, first hit

| # | Condition | Reason |
|---|---|---|
| A | `days_held ≥ 30` | `time_stop (Nd)` |
| B | Long: `low ≤ stop` / Short: `high ≥ stop` | `hard_stop (px)` |
| C | Long: `close ≤ min(prior 15 closes)` / Short: `close ≥ max(prior 15 closes)` | `donchian_exit (15d …)` |

## Trailing stop

`trailing_stop_update()` — ratchets toward price at 2.0×ATR distance. Called generically by the runner.

## Sizing

`units = floor(equity_in_quote × 0.0025 / (2.0 × ATR) / 1000) × 1000`.

## Notes on the live history

Donchian ran the SEK LIVE account earlier (2026-08-25 → 27) before the
two-account redesign moved LIVE to `bb`/`rsi` only. It is SIM-only now.

## Inspect

`python forex/runner.py --scan` → `[DONCHIAN]` panel.
