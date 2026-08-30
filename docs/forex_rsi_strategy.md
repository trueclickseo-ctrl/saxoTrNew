# Forex RSI(2) Pullback Strategy

**File**: [`forex/strategy_rsi.py`](../forex/strategy_rsi.py)
**Runner key**: `"rsi"`
**Type**: mean-reversion (buy-the-dip inside a trend)
**Where it runs**: **SIM** *and* the real-money **LIVE_EUR account** (`LIVE_EUR_ALLOWED_STRATEGIES = {"rsi"}`). Not on the SEK LIVE account.
**Momentum pre-filter**: **no** (in `_NO_MOMENTUM_FILTER`) — RSI is a reversal strategy, so it scans the full universe, not just trending pairs.

> Not to be changed. This is the strategy as it runs today, including the
> LIVE-only gates layered on top of it by `forex/runner.py`.

---

## Concept

Classic Connors RSI(2): in an established trend (price vs EMA200), a 2-period
RSI hitting an extreme is a short-lived pullback that snaps back. Buy the dip
in an uptrend, sell the rip in a downtrend. Very short holding period.

Acts on `iloc[-1]` — the **latest (still-forming) daily bar** — so a signal can
fire intraday on a move that later reverses. No multi-bar confirmation.

---

## Parameters

| Param | Value | Constant |
|---|---|---|
| RSI period | 2 | `RSI_PERIOD` |
| Oversold / Overbought | (see `RSI_OVERSOLD` / `RSI_OVERBOUGHT = 90`) | |
| Exit RSI (long / short) | 55 / 45 | `RSI_EXIT_LONG` / `RSI_EXIT_SHORT` |
| Trend EMA | 200 | `TREND_EMA` |
| ATR period / stop multiple | 14 / **1.5×** | `ATR_PERIOD` / `ATR_STOP_MULT` |
| Risk per trade | **0.25%** SIM (`RISK_PCT = 0.0025`); **0.75%** on LIVE_EUR via `LIVE_RISK_PCT_OVERRIDE` | |
| Time stop | 12 calendar days | `TIME_STOP_DAYS` |
| Lot round | 1,000 | `LOT_ROUND` |
| `MAX_POSITIONS` | 4 | **dead code** — the runner never reads it (slot cap is `_SWING_SLOTS` = 184). The real cap on concurrent RSI positions on LIVE_EUR is the **RSI-only 8% portfolio-heat cap** (`_HEAT_LIMIT_BY_STRATEGY`, 2026-08-30) ≈ **10 positions** at 0.75% risk each. |

---

## Entry

| | Long | Short |
|---|---|---|
| Trend | `close > EMA200` | `close < EMA200` |
| Trigger | `RSI(2) ≤ oversold` | `RSI(2) ≥ 90` |

- `score` = distance past the threshold (most extreme first).
- `stop_price` = `close ∓ 1.5 × ATR(14)`.
- One position per pair (`sym in open_symbols` skipped).

## Exit — `should_exit()`, first hit

| # | Condition | Reason |
|---|---|---|
| A | `days_held ≥ 12` | `time_stop (Nd)` |
| B | Long: `RSI(2) ≥ 55` / Short: `RSI(2) ≤ 45` | `rsi_recovery (…)` |
| C | Long: `low ≤ stop` / Short: `high ≥ stop` | `hard_stop (px)` |

No trailing stop (`trailing_stop_update` not defined — RSI holds are too short).

## Sizing (SIM)

`units = floor(equity_in_quote × RISK_PCT / (1.5 × ATR) / 1000) × 1000`,
`risk_pct` overridable, `block_below_min` supported.

## LIVE_EUR-only gates layered on top (in `forex/runner.py`, not this file)

RSI is the only strategy currently placing real-money orders, so it carries
every LIVE gate:

| Gate | Effect |
|---|---|
| `LIVE_RISK_PCT_OVERRIDE = 0.0075` | sizes off 0.75%, not 0.25% |
| `risk_equity_eur = 8000` cap | sizing base is €8,000 (2026-08-30, raised from 6,000 ahead of an 18k SEK deposit). The real pooled Saxo balance is **~15,800 SEK ≈ €1,400** (→ ~€3,050 post-deposit), so €8,000 is ~2.5× — a deliberate leverage choice. |
| **10k–100k lot ladder** (`_snap_rsi_live_lot`) | risk-sized qty snapped to the nearest 10,000, clamped [10k, 100k] |
| `LIVE_MAX_CURRENCY_EXPOSURE = 5` | ≤ 5 net positions per currency |
| Cost gate `MIN_EDGE_TO_COST_RATIO = 3.0` | skip if target < 3× Saxo round-trip commission |
| Weekend market-hours gate | no new entries Fri ~22:00 → Sun ~22:00 UTC; signals still emailed |
| **RSI-only 8% portfolio-heat cap** (`_HEAT_LIMIT_BY_STRATEGY`) | ≈ 10 concurrent RSI positions; every other strategy + the SEK account keep 6% |
| 50% Saxo margin cap | shared hard backstop across all books |

See [forex_live_account.md](forex_live_account.md) for the full LIVE picture.

## Inspect

`python forex/runner.py --scan` → `[RSI]` panel.
