# Forex RSI(2) Pullback Strategy

**File**: [`forex/strategy_rsi.py`](../forex/strategy_rsi.py)
**Runner key**: `"rsi"`
**Type**: mean-reversion (buy-the-dip inside a trend)
**Where it runs**: **SIM** *and* **both** real-money accounts as of 2026-08-31 — the **EUR account** (`LIVE_EUR_ALLOWED_STRATEGIES = {"rsi"}`, 49 CORE pairs) and the **SEK account** (`LIVE_ALLOWED_STRATEGIES = {"rsi"}`, 17 HIGH_VOLUME pairs). The 17 HIGH_VOLUME pairs are traded on both accounts — every signal on those is taken twice (once per account), ~2× per-signal real-money exposure on the shared Saxo margin pool. Sizing stays per account (SEK off the 15,000 SEK cap, EUR off the 8,000 EUR cap); the 8% RSI heat cap is per account (~16% combined worst case).
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

Plus a broker-side take-profit at `2.0 × R` (`DEFAULT_TP_RR`) resting from entry,
and — every scheduled cycle, in `_run_exits` — the stop is tightened by:

- **`trailing_stop_update()`** — `stop = max(stop, close − 1.5 × ATR)` (ratchet, active from the first cycle). *(The old "no trailing stop" note in this doc was stale — the function is defined and called.)*
- **`_apply_breakeven_stop()`** — one-shot move to exact `entry_price` once unrealised profit ≥ `1.0 × ATR_at_entry` (≈ +0.67 R).

### Opt-in profit-protection ladder (2026-08-31, OFF by default)

`forex/runner.py` `_profit_ladder_target_stop()` — an alternative to the two
lines above for the RSI book, staging the stop instead of one jump to entry:

| Unrealised profit | Stop moves to |
|---|---|
| ≥ 0.75 R | `entry + 0.10 R` (breakeven + costs) |
| ≥ 1.00 R | `entry + 0.50 R` (locked profit) |
| ≥ 1.25 R | `max(entry + 0.50 R, close − 1.0 × ATR)` — trailing starts here, not before |

Ratchet only; primary exits (RSI recovery / 2R TP / 12-day / hard stop) unchanged.
When active it **replaces** both `trailing_stop_update` and `_apply_breakeven_stop`
for RSI positions so the two systems never fight. New positions store
`initial_stop_price` as the frozen R reference.

**Gating:** `PROFIT_LADDER_ACCOUNTS` (empty ⇒ off everywhere) × `PROFIT_LADDER_STRATEGIES = {"rsi"}`.
Set `PROFIT_LADDER_ACCOUNTS = {"live_eur"}` to switch on. Backtest first:
`python backtests/rsi_exit_ladder_backtest.py`.

2026-08-31 run — 17 pairs, 12 y, 2,365 trades, cost = 0.03 R/trade:

| | Current | Ladder | Δ |
|---|---|---|---|
| Win rate | 61.6% | 62.7% | **+1.0 pp** |
| Avg R / trade | −0.013 | −0.010 | +0.003 |
| Total R | −30.1 | −23.3 | +6.9 R |
| Max drawdown | 34.4 R | 30.8 R | **−3.6 R** |
| Avg give-back | 0.279 R | 0.283 R | ~0 (unchanged) |

Small net improvement, but it does **not** fix give-back: avg MFE is only
≈ 0.51 R (avg hold ≈ 4 days), well below the 0.75 R first rung, so most
RSI(2) trades never trigger a ladder step. Both policies also show slightly
negative net expectancy at a 3%-of-R cost assumption (this replay omits the
`signal_filter` / consensus / momentum context the live path applies).
Flag stays **OFF** pending: (a) a re-run with lower rungs (~0.4 / 0.6 / 0.8 R)
matched to this strategy's actual MFE profile, and (b) `--cost-r` set to the
real per-lot Saxo commission.

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
