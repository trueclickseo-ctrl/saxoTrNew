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
- **All exit management needs a price bar.** `_run_exits` pulls `df` from `market_data`, which is built for the account's *scanned* universe. A held position on a pair since dropped from that universe (e.g. a legacy exotic on the EUR account, which now trades CORE only) had `df = None` → trailing, breakeven, the ladder, `should_exit` (RSI recovery **and** the 12-day time stop) all silently no-op'd, leaving it on its entry-day broker stop/TP alone. Fixed 2026-08-31: `_add_held_position_history()` tops up `market_data` with daily history for every held symbol, in both `run_daily` and `run_exits_only`.

### Profit-protection ladder (2026-08-31, **ON for `sim` + `live` + `live_eur`**)

`forex/runner.py` `_profit_ladder_target_stop()` — replaces the two
lines above for the RSI book on all three accounts, staging the stop
instead of one jump to entry:

| Unrealised profit | Stop moves to |
|---|---|
| ≥ 0.75 R | `entry + 0.10 R` (breakeven + costs) |
| ≥ 1.00 R | `entry + 0.50 R` (locked profit) |
| ≥ 1.25 R | `max(entry + 0.50 R, close − 1.0 × ATR)` — trailing starts here, not before |

Ratchet only; primary exits (RSI recovery / 2R TP / 12-day / hard stop) unchanged.
When active it **replaces** both `trailing_stop_update` and `_apply_breakeven_stop`
for RSI positions so the two systems never fight. New positions store
`initial_stop_price` as the frozen R reference.

**Gating:** `PROFIT_LADDER_ACCOUNTS = {"sim", "live", "live_eur"}` × `PROFIT_LADDER_STRATEGIES = {"rsi"}`.
Empty the set to revert everything to the plain breakeven + 1.5×ATR trail.
Only the `rsi` strategy is affected — `advanced_rsi_master` (rsi's untouched A/B twin) keeps the plain
policy and is the **control** on SIM. Turned on 2026-08-31 at the user's explicit request after a
GBPPLN position gave back +30 → −24 PLN; the backtest shows only a small net edge and that it
doesn't fully close the give-back (avg MFE ~0.51 R < the 0.75 R first rung), but the ladder only ever
*tightens* a stop.

**Watching it:** every rung move is logged `[PROFIT-LADDER … rung=breakeven+costs|lock+0.5R|trail-1ATR]`
and stamped on the position (`pos["ladder_rung"]`, carried into the exit observation card).
`python report_profit_ladder.py` compares `rsi` (ladder) vs `advanced_rsi_master` (control) per
account — capture ratio (net P&L / MFE), give-back EUR, avg R, exit-reason and rung breakdown.
Backtest: `python backtests/rsi_exit_ladder_backtest.py`.

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
| **€45 per-trade risk CAP** (`RSI_LIVE_FIXED_RISK_EUR = 45.0`, 2026-08-31) | caps loss-if-stopped at a uniform €45 max on every pair regardless of stop width (was: equity-% + a 10k-lot ladder that gave ~€8 on MXNUSD vs ~€73 on GBPUSD). qty rounds **down** to Saxo's 1,000-unit increment so realised risk stays **≤ €45** (typically €38–45); a pair whose stop is so wide that even one min-lot would risk > €45 is **skipped**. `RSI_LIVE_LOT_MAX` remains only as a sanity backstop (never binds at €45). If the €45 can't be converted to the pair's quote currency (no live rate) the trade is skipped — no %-based fallback on real money. The round-trip commission stays a separate edge/cost filter (`MIN_EDGE_TO_COST_RATIO`), not folded into sizing. Set the constant to `None` to restore the old `_snap_rsi_live_lot` 10k-ladder behaviour. (First cut this same day rounded *up* / treated €45 as a minimum; corrected to cap/round-down/skip per explicit user rules.) |
| `LIVE_MAX_CURRENCY_EXPOSURE = 5` | ≤ 5 net positions per currency |
| Cost gate `MIN_EDGE_TO_COST_RATIO = 3.0` | skip if target < 3× Saxo round-trip commission |
| Weekend market-hours gate | no new entries Fri ~22:00 → Sun ~22:00 UTC; signals still emailed |
| **RSI-only 8% portfolio-heat cap** (`_HEAT_LIMIT_BY_STRATEGY`) | ≈ 10 concurrent RSI positions; every other strategy + the SEK account keep 6% |
| 50% Saxo margin cap | shared hard backstop across all books |

See [forex_live_account.md](forex_live_account.md) for the full LIVE picture.

## Inspect

`python forex/runner.py --scan` → `[RSI]` panel.

---

## RSI(2) threshold study — the signal registry (2026-08-31)

`forex/rsi_signal_registry.py` logs **every** RSI(2) trigger (SIM + LIVE) whose
RSI(2) is in the *study band* — `<= 15` in an uptrend / `>= 85` in a downtrend —
including the 11–15 the live threshold (`RSI_OVERSOLD = 10`) rejects. Each row
is then forward-resolved against the daily bars: first-touch of the 1.5×ATR
stop / 2R TP / RSI recovery / 12-day time stop → `{outcome, r_multiple, days_held}`.

**Observe-only.** The live entry threshold is unchanged — the runner still only
acts on `RSI(2) <= 10`. Wired into `_run_entries` for the `rsi` strategy
(`observe()` + `resolve()` once per scan). `data/rsi_signal_registry.jsonl`.

`python report_rsi_thresholds.py [account] [--cost 0.03]` buckets the resolved
rows cumulatively (≤5 / ≤7 / ≤10 / ≤12 / ≤15) and reports, **after a modelled
round-trip cost**: trades · WR · avg win/loss R · profit factor · max DD ·
**expectancy (R/trade)**. The best threshold is the highest-*expectancy* bucket,
not the highest win rate. Needs ~40–80 resolved triggers per account before it's
actionable (paper-fill keeps SIM flowing through Saxo outages). Also feeds AI
roadmap #4 (Trade Probability).

---

## SIM-only A/B twins (this strategy is the control)

`strategy_rsi.py` is deliberately **byte-unchanged** so it's a clean control for
the SIM-only experiments running in parallel with it. None can place a
real-money order.

| Variant | Runner key | What differs | Doc |
|---|---|---|---|
| Advanced RSI Master | `advanced_rsi_master` | robust one-sided RSI, EMA50/200 alignment + slope, min EMA200-distance, ATR-percentile band, post-extreme reversal-confirm bar, DI confirm. Also the **ladder-vs-no-ladder control** — keeps plain breakeven + 1.5×ATR trail (not in `PROFIT_LADDER_STRATEGIES`). | [forex_advanced_rsi_master_strategy.md](forex_advanced_rsi_master_strategy.md) |
| **RSI Trend** | `rsi_trend` | **Identical to `rsi` in every way** — delegates `should_exit` / `size_position` / `trailing_stop_update`, re-exports every constant, **shares the profit ladder** — *except one entry gate*: a Buy is only taken when `ai.regime.classifier` says `TRENDING_BULLISH`, a Sell only when `TRENDING_BEARISH`. So the SIM A/B isolates exactly one variable: the regime filter. `classify_regime` failure → signal dropped, never raises. | [forex_rsi_trend_strategy.md](forex_rsi_trend_strategy.md) |
| ~~RSI Confirm~~ `rsi_confirm` | **RETIRED 2026-09-02** — a confirmation-delay entry model (queue the signal, observe 6–30h, enter only on a confirmed dip-then-recover). A 12,700-signal backtest showed the delay systematically enters *after* the mean reversion: control −0.11 R / win 56% → delayed −0.25 R / win 42%, every K, both halves. Module kept unwired. | [forex_rsi_confirm_strategy.md](forex_rsi_confirm_strategy.md) |

**Why `rsi_trend` (2026-09-02):** an 11y / 49-CORE-pair backtest decomposition
showed RSI(2)'s raw edge (+0.021 R/trade) is *unstable* — ≈0 in 2014–2020,
+0.046 in 2021–2026 — and lives entirely in the TRENDING regime buckets
(`TRENDING_BULLISH` +0.088 R, stable both halves; `RANGING` +0.011, unstable,
= 75% of signals). Gating on `TRENDING_*` lifted avg R to +0.081 (positive in
both halves), PF 1.09 → 1.37, max drawdown 82 R → 18 R. Forward-test on SIM +
a proper walk-forward before any LIVE consideration.

Compare all three via the `pnl_ledger` rows (`strategy` column) and the AI
journal / give-back reports (both break down by strategy);
`python report_profit_ladder.py` covers `rsi` (ladder) vs `advanced_rsi_master`
(control).
