# Forex Strategy Playbook

**Module**: `forex/`  
**Universe**: 34 FX pairs — 7 G7 majors + 27 crosses (incl. Scandinavian & EM)  
**Strategies**: 11 active (9 rule-based swing + 1 deep learning swing + 1 day-trading breakout)  
**Max slots**: 4+4+4+4+34+34+20+20+20+20 = **164 swing** + **7 day-trading** (independent book)  
**Swing risk per trade**: 1% of account equity  
**Day-trading capital**: 15,000 SEK dedicated, 1.5% risk per trade  

---

## Audit — 2026-08-20

| # | Finding | Severity | Status |
|---|---------|----------|--------|
| 1 | **Position sizing ignored the quote currency — 447× risk spread** | **Critical** | Fixed `5bf8a5f` |
| 2 | Dry runs permanently deleted live position tracking | **Critical** | Fixed `5bf8a5f` |
| 3 | Sizing ran off ~945,000 EUR of SIM demo credit (~33× intended) | **High** | Fixed `5bf8a5f` |
| 4 | **10 of 11 strategies have no backtest** | **High** | Open |
| 5 | **CNN-LSTM emits zero signals and is ~chance accuracy** | **High** | Open |
| 6 | Signals computed on the incomplete, still-forming daily bar | Medium | Open |
| 7 | No broker/state reconciliation — orphaned positions undetectable | Medium | Open |
| 8 | No file logging — unattended runs leave no audit trail | Medium | Open |
| 9 | Bar timestamps discarded on fetch — stale data undetectable | Low | Open |
| 10 | **London Breakout: sized off the whole account, not its 15,000 SEK book** | **High** | Fixed `0b855b2` |
| 11 | **LBO: hardcoded `/10.7` "USDSEK" rate on a EUR account** | **High** | Fixed `0b855b2` |
| 12 | **LBO: `MAX_UNITS` cap was doing the sizing on 5 of 7 pairs** | **High** | Fixed `0b855b2` |
| 13 | LBO `_atr()` uses a simple mean, not Wilder — inconsistent with every other module | Low | Open |

### Per-strategy formula audit

Every strategy's indicators and signal construction were read. Results:

| # | Strategy | Indicators | Lookahead | Sizing | Verdict |
|---|----------|-----------|-----------|--------|---------|
| 1 | EMA + ADX | ✅ Wilder | ✅ none | fixed `5bf8a5f` | OK |
| 2 | RSI(2) | ✅ Wilder | ✅ none | fixed `5bf8a5f` | OK |
| 3 | Donchian | ✅ Wilder | ✅ `[-(N+1):-1]` excludes current bar | fixed `5bf8a5f` | OK |
| 4 | Bollinger | ✅ `ddof=0` | ✅ none | fixed `5bf8a5f` | OK |
| 5 | Pullback | ✅ Wilder | ✅ none | fixed `5bf8a5f` | OK |
| 6 | Gap Fill | ✅ n/a | ✅ none | fixed `5bf8a5f` | OK |
| 7 | SuperTrend | ✅ correct ratcheting | ✅ none | fixed `5bf8a5f` | OK |
| 8 | Z-Score | ✅ `ddof=1` | ✅ none | fixed `5bf8a5f` | OK |
| 9 | ML (logistic) | ✅ correct | ✅ **verified none** | fixed `5bf8a5f` | OK |
| 10 | CNN-LSTM | ✅ correct | ✅ walk-forward safe | n/a | ⚠️ **no edge, never fires** |
| 11 | London Breakout | ⚠️ SMA not Wilder | ✅ none | ❌ **4 defects** | fixed `0b855b2` |

The indicator maths is sound across the board — the defects were all in **capital
and currency handling**, not in the signal logic.

### 🔴 Findings 10–12 — London Breakout sizing (fixed)

LBO is the day-trading book and was the last strategy audited. Four defects sat in
the same six lines:

1. **Wrong book.** It is documented as a separate book with 15,000 SEK and 1.5%
   risk. `runner.py` passed `account_equity=equity` — the *whole account* — so the
   `15_000.0` default was always overridden. Off the uncapped SIM equity that was
   ~14,185 EUR of risk per trade.
2. **Hardcoded wrong-currency rate.** `equity_usd = account_equity / 10.7  #
   approximate USDSEK`, on an account denominated in **EUR**. Wrong constant, wrong
   pair, and nothing updates it.
3. **Quote-currency error, and the Finding 1 fix did not reach it.** LBO returns
   precomputed `units` in its signal, and the runner uses `sig["units"]` directly —
   bypassing the conversion added in `5bf8a5f`. LBO trades USDJPY and GBPJPY, so
   those were sized against a JPY stop distance using an unconverted budget.
4. **The cap was doing the sizing.** With inflated equity, **5 of 7 pairs pinned at
   `MAX_UNITS = 50,000`** — position size was set by the clamp, not by risk. And
   `max(MIN_UNITS, …)` floored small sizes *up*, silently over-risking any trade
   whose correct size was below one lot; those are now skipped instead.

**Verified across all 7 LBO pairs** at representative session ranges:

| | Before | After |
|---|---|---|
| Risk range | 7 – 107 EUR | 20.85 EUR flat |
| Spread | **15×** (5 of 7 pinned at cap) | **1.00×** |
| Per trade | uncontrolled | exactly 1.5% of the 1,390 EUR book |

Book capital now lives in `strategies.forex.lbo_capital_eur` (1,390 EUR ≈ 15,000 SEK).

### 🔴 Finding 1 — sizing ignored the quote currency (fixed)

`size_position()` computes `units = (equity × RISK_PCT) / (mult × ATR)`. ATR — and
therefore stop distance — is denominated in the pair's **quote currency**, while
equity is in **EUR**. Nothing converted between them, so realised risk scaled with
the *numeric size of the quote currency*.

Measured on the 14 live open positions, each converted to EUR at live rates:

| Position | Units | Risk (EUR) | % of account |
|----------|------:|-----------:|-------------:|
| ema:USDJPY | 5,000 | 24 | 0.1% |
| pullback:EURJPY | 5,000 | 46 | 0.2% |
| ml:AUDJPY | 5,000 | 46 | 0.2% |
| ml:CADJPY | 6,000 | 51 | 0.2% |
| pullback:EURNOK | 81,000 | 872 | 3.1% |
| ema:GBPUSD | 958,000 | 3,587 | 12.9% |
| donchian:EURUSD | 974,000 | 4,547 | 16.4% |
| donchian:GBPUSD | 722,000 | 6,018 | 21.6% |
| ema:USDCAD | 1,223,000 | 6,180 | 22.2% |
| donchian:AUDUSD | 1,063,000 | 8,538 | 30.7% |
| **pullback:NZDCHF** | 1,602,000 | **10,674** | **38.4%** |

Every one of those is nominally a **1% risk** trade. Actual spread: **447×**.
JPY-quoted pairs were effectively not trading (0.1% risk); USD/CAD/CHF pairs risked
up to 38% of the account on a single position.

**Fix:** restate the risk budget in the quote currency before sizing
(`_equity_in_quote` / `_eur_per_unit`, using the existing `fx` module). Applied at
the single sizing call site in the runner, so all 11 strategies inherit it without
touching each module. If a quote currency has no rate the signal is now **skipped**
rather than sized without conversion.

**Verified after the fix** — same 14 positions, same stops:

| | Before | After |
|---|---|---|
| Risk range | 24 – 10,674 EUR | 269 – 278 EUR |
| Spread | **447×** | **1.0×** |
| Per trade | 0.1% – 38.4% | 0.97% – 1.00% |

Residual variation is `min_units` rounding only.

### Finding 2 — dry runs deleted live positions (fixed)

Same defect found in futures (`a34ffd0`), and worse here. `_process_exits()` does
`del positions[key]` **unconditionally** — outside the `if not dry_run` block — and
both `run_daily()` and `run_exits_only()` called `_save_state()` unconditionally.

Any dry run whose exit rules fired would have permanently wiped tracking for real
open positions, leaving all 14 open at the broker with no stop management and no
record. Found by reading before running, so it was never triggered here.

### Finding 3 — sizing ran off demo credit (fixed)

`_account()` read `TotalValue` uncapped — `forex_peak_equity.json` confirms
**945,669 EUR**, roughly 33× the intended 300,000 SEK. Now capped by
`strategies.forex.risk_equity_eur`.

Unlike futures, this does **not** reduce what can be traded: FX deals in 1,000-unit
increments, so positions scale down cleanly rather than whole pairs becoming
untradeable.

### Finding 4 — 10 of 11 strategies are unvalidated (open)

`backtest_forex.py` covers **only Strategy 1 (EMA + ADX)**, and reimplements the
rules inline rather than importing `forex.strategy` — so it validates a *copy*, not
the code that trades. RSI, Donchian, BB, Pullback, Gap, SuperTrend, Z-Score, ML and
London Breakout have **no backtest anywhere in the repo**.

The win rates quoted in each strategy section below are therefore assertions, not
measurements. `backtest_futures_all.py` shows the pattern for fixing this: drive the
real modules over truncated data. The forex modules share a uniform
`generate_signals` / `should_exit` / `size_position` interface, so the same approach
ports directly.

### 🔴 Finding 5 — CNN-LSTM never fires (open)

Strategy 10 is marked ★★★ in this document. Its own walk-forward report
(`data/cnn_lstm/report.json`) says otherwise:

| Fold | Train end | Accuracy | Buy prec. | Sell prec. | **Signal rate** |
|-----:|-----------|---------:|----------:|-----------:|----------------:|
| 1 | 2023-12-06 | 36.8% | 40.6% | 34.1% | **0.0** |
| 2 | 2024-05-22 | 39.0% | 39.5% | 38.0% | **0.0** |
| 3 | 2024-11-06 | 37.7% | 39.7% | 36.7% | **0.0** |
| 4 | 2025-04-28 | 35.6% | 35.5% | 34.2% | **0.0** |
| 5 | 2025-10-13 | 35.6% | 35.9% | 32.0% | **0.0** |
| | **mean** | **36.9%** | 38.2% | — | **0.0** |

Two independent problems:

1. **Accuracy is chance.** This is a 3-class problem (Buy / Sell / Hold), so random
   is 33.3%. 36.9% is not a demonstrated edge.
2. **Signal rate is 0.0 in every fold.** At the configured `CONFIDENCE = 0.58`
   threshold the model never produced a single tradeable signal in five folds of
   walk-forward testing. It holds no live positions either.

The model trains, loads and runs correctly — it simply never emits anything. It is
carrying a PyTorch dependency, a training pipeline and a ★★★ rating for zero output.
**Either lower the confidence threshold and re-validate, or retire it.**

> The *engineering* here is sound — the trainer does proper walk-forward with a
> `max_date` cutoff and never shuffles time-series data. The model just has no edge.

### Finding 6 — signals use the incomplete current bar (open)

`_fetch_history()` returns every bar the chart API gives, **including the current,
still-forming daily bar**. The scheduled runs fire at 01:20, 09:00 and 13:00 UTC —
all mid-session — so `closes.iloc[-1]` is a partial bar, not a close.

Two consequences:

- Daily-bar strategies were designed and (where backtested) validated on *completed*
  bars. Acting on a partial bar is a different strategy.
- The runner fires 3× per day, so the same forming bar is evaluated three times with
  different values. A Donchian breakout or RSI extreme can trigger on an intraday
  spike the daily close never confirms.

This is a live-vs-backtest mismatch affecting **all 11 strategies**. Fixing it means
dropping the last bar (signal on yesterday's close, trade today) — a material change
to signal timing, so it needs a deliberate decision rather than a silent edit.

### Findings 7–9 — operational gaps (open)

- **No reconciliation.** Nothing compares state to broker positions; forex has the
  same orphaning exposure futures had. `futures/runner.py --reconcile` (`852eac2`)
  is the working template.
- **No file logging.** `logging.basicConfig()` with no `FileHandler` — console only.
  Every other module writes `logs/*.log`. Unattended runs (5 scheduled tasks/day)
  leave no trace, which is why no forex log exists to audit.
- **Timestamps discarded.** `_fetch_history()` builds rows of OHLC only, dropping the
  bar `Time`, so stale or gapped data cannot be detected.

### Verified as correct

Not everything was broken. Confirmed sound by inspection:

- **RSI** — proper Wilder smoothing (`ewm(alpha=1/period)`) on separated gains and
  losses, in both `strategy_rsi.py` and `strategy_bb.py`.
- **ATR** — correct Wilder RMA, consistent across all modules.
- **ADX** — textbook Wilder `+DM`/`−DM` selection and smoothing.
- **Bollinger** — `ddof=0` (population std), the standard convention.
- **Z-Score** — `ddof=1` rolling mean/std; uses only past and current bars.
- **SuperTrend** — correct band ratcheting and direction-flip logic, with NaN
  handling before the first valid ATR.
- **ML (logistic regression)** — **no lookahead**: labels use `shift(-1)` correctly,
  the training window ends at `len-2` so the last label is knowable today, and the
  normalisation `mu`/`sigma` are fitted on the training window only, never including
  the prediction row.
- **CNN-LSTM trainer** — time-series-safe walk-forward with `max_date` cutoff and no
  shuffling.

---

## Daily Schedule

| Task (Task Scheduler)       | Time PKT       | UTC           | Session        | Pairs |
|-----------------------------|----------------|---------------|----------------|-------|
| ATOS Forex Daily Run        | 06:20 Mon–Fri  | 01:20         | Asian          | 14 (JPY/AUD/NZD crosses) |
| ATOS Forex Exit Check       | 14:00 Mon–Fri  | 09:00         | All            | 34 (stops only — no new entries) |
| ATOS Forex London Run       | 18:00 Mon–Fri  | 13:00         | London         | 20 (EUR/GBP/USD + Scandi/CAD) |
| ATOS Forex Gap Fill         | 22:00 Sunday   | 17:00 Sun     | All            | 34 (gap fill entries only) |
| **LBO London Open**         | **12:00 Mon–Fri** | **07:00**  | **London open** | **7 majors (Asian range break)** |
| **LBO NY Open**             | **18:00 Mon–Fri** | **13:00**  | **NY open**    | **7 majors (London morning break)** |
| **LBO Force Close**         | **01:00 daily**   | **20:00**  | **Session end** | **7 (close all LBO positions)** |

---

## Strategy 1 — EMA(5/30) Crossover + ADX

**File**: `forex/strategy.py`  
**Type**: Trend-Following  
**Win Rate**: ~55%  
**Slots**: 4  

### Concept
The foundational trend strategy. EMA(5) crossing EMA(30) signals a momentum shift; ADX(14) ≥ 25 confirms a genuine trend exists, filtering out choppy sideways sessions where crossovers whipsaw.

### Entry
| Direction | Conditions |
|-----------|-----------|
| **LONG**  | EMA(5) crossed above EMA(30) within last 15 bars + ADX ≥ 25 + +DI > -DI |
| **SHORT** | EMA(5) crossed below EMA(30) within last 15 bars + ADX ≥ 25 + -DI > +DI |

### Exit (first condition hit)
- **A** — Opposite EMA crossover (trend reversal)
- **B** — 1.5×ATR(14) hard stop from entry
- **C** — 45-day time stop

### Parameters
| Param | Value |
|-------|-------|
| Fast EMA | 5 |
| Slow EMA | 30 |
| ADX period | 14 |
| ADX minimum | 25 |
| ATR stop mult | 1.5× |
| Time stop | 45 days |
| Signal lookback | 15 bars |
| Backtested Sharpe | 1.619 |

---

## Strategy 2 — RSI(2) Pullback in Trend

**File**: `forex/strategy_rsi.py`  
**Type**: Mean-Reversion within Trend  
**Win Rate**: ~60%  
**Slots**: 34  

### Concept
Uses EMA(200) to lock in the major trend direction, then fires only when RSI(2) hits an extreme. A 2-day exhaustion move against the trend statistically snaps back within 3–8 sessions. Very fast in, very fast out.

### Entry
| Direction | Conditions |
|-----------|-----------|
| **LONG**  | close > EMA(200) AND RSI(2) < 10 |
| **SHORT** | close < EMA(200) AND RSI(2) > 90 |

### Exit (first condition hit)
- **A** — RSI(2) recovers: ≥ 55 for longs / ≤ 45 for shorts
- **B** — 1.5×ATR(14) hard stop
- **C** — 12-day time stop

### Parameters
| Param | Value |
|-------|-------|
| RSI period | 2 |
| Trend EMA | 200 |
| Entry long | RSI(2) < 10 |
| Entry short | RSI(2) > 90 |
| Exit long | RSI(2) ≥ 55 |
| Exit short | RSI(2) ≤ 45 |
| ATR stop mult | 1.5× |
| Time stop | 12 days |

> **Note**: RSI(2) moves extremely fast — can shift from 9 to 81 within a single session. Signals are only valid immediately after the scheduled scan fires.

---

## Strategy 3 — Donchian Channel Breakout (Strict Mode)

**File**: `forex/strategy_donchian.py`  
**Type**: Momentum  
**Win Rate**: ~50% (edge from large winners, not high WR)  
**Slots**: 4  

### Concept
Turtle Trading adapted for FX. A 30-day high/low breakout captures the start of a sustained move. Now includes mandatory EMA(200) trend filter AND ADX(14) ≥ 25 to eliminate counter-trend entries and false breakouts in ranging markets.

### Entry — all three conditions required
| Direction | Conditions |
|-----------|-----------|
| **LONG**  | close > 30-day highest close AND close > EMA(200) AND ADX ≥ 25 |
| **SHORT** | close < 30-day lowest close AND close < EMA(200) AND ADX ≥ 25 |

### Exit (first condition hit)
- **A** — 15-day channel reversal (close crosses 15-day opposite channel side)
- **B** — 2.0×ATR(14) hard stop
- **C** — 30-day time stop

### Parameters
| Param | Value |
|-------|-------|
| Entry channel | 30 days |
| Exit channel | 15 days |
| Trend EMA | 200 |
| ADX minimum | 25 |
| ATR stop mult | 2.0× |
| Time stop | 30 days |

> **Change log**: Channel widened 20→30 days; EMA(200) + ADX(25) gate added to fix counter-trend entries that caused the original strategy to lose.

---

## Strategy 4 — Bollinger Band Reversion

**File**: `forex/strategy_bb.py`  
**Type**: Mean-Reversion  
**Win Rate**: ~60%  
**Slots**: 4  

### Concept
Fades 2-sigma price extremes. BB(20,2) outer band touch = statistical overextension; RSI(14) confirms momentum exhaustion. Targets reversion back to the 20-day mean (BB midline). Short-term hold — 8-day time stop keeps capital free.

### Entry
| Direction | Conditions |
|-----------|-----------|
| **SHORT** | close > BB upper AND RSI(14) > 65 (overbought excursion) |
| **LONG**  | close < BB lower AND RSI(14) < 35 (oversold excursion) |

### Exit (first condition hit)
- **A** — Close crosses back through BB mid (20-day SMA)
- **B** — 2.0×ATR(14) hard stop
- **C** — 8-day time stop

### Parameters
| Param | Value |
|-------|-------|
| BB period | 20 |
| BB std dev | 2.0σ |
| RSI period | 14 |
| RSI overbought | 65 |
| RSI oversold | 35 |
| ATR stop mult | 2.0× |
| Time stop | 8 days |

---

## Strategy 5 — Trend Pullback to EMA(20) ★

**File**: `forex/strategy_pullback.py`  
**Type**: Trend Continuation — Pullback Entry  
**Win Rate**: ~70%+ (highest win rate among trend strategies)  
**Slots**: 34  

### Concept
Enters the same trend as EMA crossover, but at a far better price. Instead of chasing the crossover, it waits for the market to pull back and touch EMA(20) — the dynamic support in an uptrend — then confirms the bounce with a close back in the trend direction.

**Triple confirmation** (trend + ADX + bounce) is what drives the elevated win rate. Because the entry is near EMA support, the stop is tighter → more units can be sized for the same 1% risk → larger profit on the same move.

### Entry — all three conditions must be true simultaneously
| Direction | Conditions |
|-----------|-----------|
| **LONG**  | close > EMA(50) AND ADX(14) ≥ 25 AND low touched EMA(20) within last 3 bars AND current close > EMA(20) |
| **SHORT** | close < EMA(50) AND ADX(14) ≥ 25 AND high touched EMA(20) within last 3 bars AND current close < EMA(20) |

### Exit (first condition hit)
- **A** — Trend break: close < EMA(50) for longs / close > EMA(50) for shorts
- **B** — 1.5×ATR(14) hard stop (tight because entry is near EMA support)
- **C** — 25-day time stop

### Parameters
| Param | Value |
|-------|-------|
| Trend EMA | 50 |
| Pullback EMA | 20 |
| ADX period | 14 |
| ADX minimum | 25 |
| Pullback lookback | 3 bars |
| ATR stop mult | 1.5× |
| Time stop | 25 days |

---

## Strategy 6 — Weekend Gap Fill ★★

**File**: `forex/strategy_gap.py`  
**Type**: Statistical Mean-Reversion (Structural Edge)  
**Win Rate**: ~80–85% (highest win rate of all strategies)  
**Slots**: 34  
**Runner flag**: `NEEDS_LIVE_PRICES = True` — runner fetches live Sunday open prices before calling this strategy  

### Concept
FX markets close Friday ~22:00 GMT and reopen Sunday ~22:00 GMT. Price frequently gaps between the Friday close and the Sunday open due to weekend news, central bank statements, or geopolitical events.

Approximately **80–85% of these gaps fill within 5 trading days**. The edge is structural, not technical:
1. Market makers immediately quote back toward Friday's close
2. Algorithmic desks are programmed to fade weekend gaps
3. Retail traders close weekend positions at Sunday open

We enter the **fade direction** on Sunday night and target a full gap fill.

### Entry — Sunday 22:00 PKT
| Direction | Conditions |
|-----------|-----------|
| **SHORT** | Sunday open > Friday close (gap up) AND gap 0.10%–2.00% |
| **LONG**  | Sunday open < Friday close (gap down) AND gap 0.10%–2.00% |

### Exit (first condition hit)
- **A** — Gap filled: price reaches Friday close level
- **B** — Hard stop: 1.5 × gap size against position
- **C** — 7-day time stop (≈ 5 trading days Mon–Fri)

### Parameters
| Param | Value |
|-------|-------|
| Min gap size | 0.10% of price |
| Max gap size | 2.00% of price |
| Stop mult | 1.5× gap size |
| Time stop | 7 calendar days |

---

## Strategy 7 — SuperTrend(10,3) Trend-Following

**File**: `forex/strategy_supertrend.py`  
**Type**: Trend-Following  
**Win Rate**: ~65%  
**Slots**: 20  

### Concept
SuperTrend generates a dynamic ATR-based support/resistance band. When price crosses above the band the trend flips bullish; below = bearish. EMA(200) acts as a macro filter — only trade in the direction of the dominant long-term trend. Fresh crossovers only (within last 3 bars) to avoid chasing stale signals.

### Entry — all conditions required
| Direction | Conditions |
|-----------|-----------|
| **LONG**  | SuperTrend direction flipped to +1 within last 3 bars AND close > EMA(200) |
| **SHORT** | SuperTrend direction flipped to -1 within last 3 bars AND close < EMA(200) |

SuperTrend bands:
- `upper = HL/2 + 3.0 × ATR(10)` — resistance in downtrend
- `lower = HL/2 − 3.0 × ATR(10)` — support in uptrend
- Bands ratchet: upper can only decrease, lower can only increase

### Exit (first condition hit)
- **A** — SuperTrend direction reverses (band crossover)
- **B** — 2.0×ATR(10) hard stop
- **C** — 40-day time stop

### Parameters
| Param | Value |
|-------|-------|
| ATR period | 10 |
| Multiplier | 3.0 |
| Trend EMA | 200 |
| Signal lookback | 3 bars |
| ATR stop mult | 2.0× |
| Time stop | 40 days |

---

## Strategy 8 — Z-Score Mean Reversion

**File**: `forex/strategy_zscore.py`  
**Type**: Mean-Reversion  
**Win Rate**: ~63%  
**Slots**: 20  

### Concept
When price deviates more than 2 standard deviations from its 20-day mean, it is statistically overextended and reverts. More rigorous than Bollinger Band reversion — uses the actual z-score (normalized in σ units) rather than a fixed band. EMA(200) prevents fading a genuine macro trend breakout.

### Entry
| Direction | Conditions |
|-----------|-----------|
| **LONG**  | z-score < −2.0 AND close > EMA(200) × 0.99 (not in extreme downtrend) |
| **SHORT** | z-score > +2.0 AND close < EMA(200) × 1.01 (not in extreme uptrend) |

### Exit (first condition hit)
- **A** — Z-score reverts to within ±0.3 (returned to mean)
- **B** — 2.5×ATR(14) hard stop
- **C** — 12-day time stop

### Parameters
| Param | Value |
|-------|-------|
| Z-score window | 20 days |
| Entry threshold | ±2.0σ |
| Exit threshold | ±0.3σ |
| Trend EMA | 200 |
| ATR stop mult | 2.5× |
| Time stop | 12 days |

---

## Strategy 9 — Machine Learning Signals (Logistic Regression)

**File**: `forex/strategy_ml.py`  
**Type**: ML — Data-Driven  
**Win Rate**: ~57–62% (varies by market regime)  
**Slots**: 20  

### Concept
Trains a logistic regression model on the last 126 daily bars (6 months) per pair. Seven normalized technical features capture trend, momentum, volatility, and mean-reversion simultaneously. Only trades when model confidence exceeds the threshold. Pure numpy implementation — no sklearn dependency.

### Features (7 normalized inputs)
| Feature | Description |
|---------|-------------|
| RSI(14) / 100 | Momentum oscillator (0–1) |
| ADX(14) / 100 | Trend strength (0–1) |
| BB %B (20,2) | Price position within BB band (0–1) |
| EMA(5)/EMA(20) − 1 | Fast/slow EMA spread |
| EMA(20)/EMA(50) − 1 | Medium-term trend |
| Price/EMA(200) − 1 | Macro trend bias |
| ATR(14)/close | Normalized volatility |

**Target**: next-day close > today's close → 1 (up), else 0 (down)

### Entry
| Direction | Conditions |
|-----------|-----------|
| **LONG**  | model probability ≥ 0.58 AND ADX(14) ≥ 20 |
| **SHORT** | model probability ≤ 0.42 AND ADX(14) ≥ 20 |

### Exit (first condition hit)
- **A** — Model prediction flips direction with confidence ≥ 0.58
- **B** — 2.0×ATR(14) hard stop
- **C** — 20-day time stop

### Parameters
| Param | Value |
|-------|-------|
| Training window | 126 bars (6 months) |
| Min bars required | 336 (EMA200 + lookback + buffer) |
| Confidence threshold | 0.58 |
| ADX minimum | 20 |
| ATR stop mult | 2.0× |
| Time stop | 20 days |
| Learning rate | 0.05 |
| Epochs | 200 |

> **Note**: ML strategy requires 336+ daily bars per pair. Pairs with fewer bars are silently skipped. Retrains on every signal check — no state persisted between runs.

---

## Strategy 10 — CNN-LSTM Deep Learning ⚠️ NOT TRADING

**Files**: `forex/strategy_cnn_lstm.py` (inference) · `forex/cnn_lstm_trainer.py` (training)  
**Type**: Deep Learning — Multi-scale CNN + Bidirectional LSTM + Self-Attention  
**Measured**: walk-forward accuracy **36.9%** on a 3-class problem (chance = 33.3%);
buy precision 35–41%, sell precision 32–38%; **signal rate 0.0 in all 5 folds**  
**Slots**: 20 (none ever used)  

> **This section previously claimed ★★★ and "walk-forward precision typically
> 55–65%". Its own report — `data/cnn_lstm/report.json` — contradicts both.** The
> model has never emitted a signal at `CONFIDENCE = 0.58` and holds no positions.
> See [Finding 5](#-finding-5--cnn-lstm-never-fires-open). Either lower the
> threshold and re-validate, or retire the strategy.


### Concept
A production-quality deep learning model trained on **5 years of daily bars across all 34 pairs simultaneously** (≈40,000 sequences). It learns patterns at three different timescales in parallel and uses an attention mechanism to weight which recent bars matter most for the prediction.

**Why it's better than a naive Conv1D→LSTM:**

| Naive approach | This model |
|---|---|
| Single Conv kernel=3 | 3 parallel branches (k=3/7/14) — daily, weekly, bi-weekly patterns |
| Random train/val shuffle | Walk-forward cross-validation — never shuffles time series |
| No look-ahead bias protection | Causal convolutions — output at day t uses only data ≤ t |
| Standard LSTM | Bidirectional LSTM — processes sequence both directions |
| None | Self-attention pooling — learns which bars matter most |
| Binary target (up/down) | ATR-normalized 3-class target — only labels significant moves |
| Per-pair model | Single global model across all 34 pairs (12× more training data) |

### Architecture
```
Input: (batch, 60 days, 16 features)
  ↓
Multi-Scale CNN  [3 parallel branches, causal padding]:
  Branch-A  Conv1D(k=3) × 2 → BatchNorm → ReLU   (3-day / daily patterns)
  Branch-B  Conv1D(k=7)     → BatchNorm → ReLU   (weekly patterns)
  Branch-C  Conv1D(k=14)    → BatchNorm → ReLU   (bi-weekly patterns)
  → Concat(192ch) → Dropout(0.2) → MaxPool(2)
  ↓
Bidirectional LSTM(128 × 2 = 256) → Dropout(0.3)
  ↓
Self-Attention pooling → context vector(256)
  ↓
Dense: 256→128 (BN, ReLU) → Dropout(0.25) → 64 (ReLU) → 3 (Softmax)
  ↓
Output: [P(Sell), P(Hold), P(Buy)]   — 409,220 parameters
```

### Features (16 per time step, 60-bar lookback)
| # | Feature | Captures |
|---|---------|---------|
| 1 | log return (1d) | Recent momentum |
| 2 | log return (5d) | Weekly trend |
| 3 | log return (20d) | Monthly momentum |
| 4 | EMA(5)/EMA(20) − 1 | Fast trend |
| 5 | EMA(20)/EMA(50) − 1 | Medium trend |
| 6 | Price/EMA(200) − 1 | Macro trend bias |
| 7 | RSI(14) − 0.5 | Momentum oscillator (centred) |
| 8 | ADX(14)/100 | Trend strength |
| 9 | ATR(14)/close | Normalized volatility |
| 10 | ATR/avg-ATR(20) − 1 | Relative volatility (regime) |
| 11 | BB(20,2) %B | Mean-reversion band position |
| 12 | Z-score(20) | Standard deviations from mean |
| 13 | Donchian position | 30-day channel location (0–1) |
| 14 | MACD histogram/ATR | Normalized momentum divergence |
| 15 | sin(2π × DoW/5) | Cyclical day-of-week |
| 16 | cos(2π × DoW/5) | Cyclical day-of-week |

### Target labels
A signal is only labelled **Buy** or **Sell** if the 5-day forward return exceeds ±0.5 × ATR%. Otherwise it is **Hold** (no trade). This avoids training the model to trade noise.

| Label | Condition |
|-------|-----------|
| **Buy** (2) | fwd\_return\_5d > +0.5 × ATR% |
| **Hold** (1) | within ±0.5 × ATR% band |
| **Sell** (0) | fwd\_return\_5d < −0.5 × ATR% |

### Entry
| Direction | Conditions |
|-----------|-----------|
| **LONG**  | P(Buy) ≥ 0.60 AND ADX(14) ≥ 15 |
| **SHORT** | P(Sell) ≥ 0.60 AND ADX(14) ≥ 15 |

### Exit (first condition hit)
- **A** — Model flips: P(Sell) ≥ 0.60 for longs / P(Buy) ≥ 0.60 for shorts
- **B** — 2.5×ATR(14) hard stop
- **C** — 15-day time stop

### Parameters
| Param | Value |
|-------|-------|
| Lookback window | 60 bars |
| Features | 16 |
| Training data | 5 years, 34 pairs (yfinance) |
| Prediction horizon | 5 days |
| Confidence threshold | 0.60 |
| ADX minimum | 15 |
| ATR stop mult | 2.5× |
| Time stop | 15 days |
| Model size | 409,220 parameters |
| Training time (CPU) | ~15–30 min |
| Min bars for inference | 280 |

### Training & maintenance
```bash
# First-time training (run once before first live use)
python -m forex.cnn_lstm_trainer --train

# Check model status and walk-forward performance
python -m forex.cnn_lstm_trainer --status

# Retrain on specific pairs only
python -m forex.cnn_lstm_trainer --train --pairs EURUSD GBPUSD USDJPY

# Walk-forward backtest without retraining final model
python -m forex.cnn_lstm_trainer --backtest
```

Model artifacts are saved to `data/cnn_lstm/` (gitignored — regenerate with `--train`):
- `model.pt` — PyTorch state dict
- `scaler.json` — per-feature mean/std (fitted on training fold only)
- `config.json` — hyperparameters used
- `report.json` — walk-forward accuracy, Buy/Sell precision per fold

If no trained model exists, the strategy silently emits no signals — safe to have in the registry before training.

> **Retrain schedule**: Recommend monthly retraining as new market data accumulates. Add to Task Scheduler: `python -m forex.cnn_lstm_trainer --train` on the 1st of each month.

---

## Strategy Comparison

| # | Strategy | Type | Win Rate | Key Indicators | Stop | Time Stop | Slots | Book |
|---|----------|------|----------|---------------|------|-----------|-------|------|
| 1 | EMA Crossover | Trend | ~55% | EMA(5/30) + ADX(14) | 1.5×ATR | 45d | 4 | Swing |
| 2 | RSI(2) Pullback | Reversion-in-trend | ~60% | RSI(2) + EMA(200) | 1.5×ATR | 12d | **34** | Swing |
| 3 | Donchian Break | Momentum | ~50% | 30d High/Low + EMA(200) + ADX | 2.0×ATR | 30d | 4 | Swing |
| 4 | BB Reversion | Mean-reversion | ~60% | BB(20,2) + RSI(14) | 2.0×ATR | 8d | 4 | Swing |
| 5 | **Pullback-to-EMA** ★ | Trend continuation | **~70%+** | EMA(20/50) + ADX(14) | 1.5×ATR | 25d | **34** | Swing |
| 6 | **Weekend Gap Fill** ★★ | Structural mean-rev | **~80–85%** | Gap % + live price | 1.5×gap | 7d | **34** | Swing |
| 7 | SuperTrend | Trend | ~65% | ST(10,3) + EMA(200) | 2.0×ATR | 40d | 20 | Swing |
| 8 | Z-Score Rev | Mean-reversion | ~63% | 20d z-score + EMA(200) | 2.5×ATR | 12d | 20 | Swing |
| 9 | ML Signals | ML / Logistic Reg | ~57–62% | 7 features, per-pair retrain | 2.0×ATR | 20d | 20 | Swing |
| 10 | **CNN-LSTM** ★★★ | Deep Learning | **~55–65%** | 16 features, global model, attention | 2.5×ATR | 15d | 20 | Swing |
| **11** | **London Breakout** ★★ | **Day Trading** | **~58–63%** | **H1 Asian/London range + session clock** | **Range boundary** | **20:00 UTC** | **7** | **Day** |

---

## Universe — 34 Pairs

### Asian Session — 14 pairs (06:20 PKT)
`USDJPY` `EURJPY` `GBPJPY` `AUDJPY` `CADJPY` `NZDJPY` `CHFJPY`  
`AUDUSD` `NZDUSD` `AUDCAD` `AUDCHF` `AUDNZD` `NZDCAD` `NZDCHF`

### London Session — 20 pairs (18:00 PKT)
`EURUSD` `GBPUSD` `USDCAD` `USDCHF`  
`EURGBP` `EURAUD` `EURNZD` `EURCAD` `EURCHF`  
`GBPAUD` `GBPCAD` `GBPCHF` `GBPNZD`  
`CADCHF` `EURNOK` `EURSEK` `USDNOK` `USDSEK` `USDDKK` `USDMXN`

### Gap Fill — all 34 pairs (22:00 Sunday)
All pairs scanned; only those showing a 0.10%–2.00% gap receive entries.

### UICs (Saxo SIM)
| Pair | UIC | Status | Pair | UIC | Status |
|------|-----|--------|------|-----|--------|
| EURUSD | 21 | ✓ confirmed | EURGBP | 17 | ✓ confirmed |
| GBPUSD | 31 | ✓ confirmed | EURJPY | 18 | ✓ confirmed |
| USDJPY | 42 | ✓ confirmed | GBPJPY | 26 | ✓ confirmed |
| AUDUSD | 4  | ✓ confirmed | AUDJPY | 2  | ✓ confirmed |
| USDCAD | 38 | ✓ confirmed | CADJPY | 6  | ✓ confirmed |
| NZDUSD | 37 | ✓ confirmed | CHFJPY | 8  | ✓ confirmed |
| USDCHF | 39 | ✓ confirmed | NZDJPY | 36 | ✓ confirmed |
| AUDCAD | 1  | ✓ confirmed | NZDCAD | 33 | ✓ confirmed |
| AUDCHF | 5027 | ✓ confirmed | NZDCHF | 34 | ✓ confirmed |
| AUDNZD | 3  | ✓ confirmed | EURAUD | 12 | ✓ confirmed |
| EURNZD | 2072 | ✓ confirmed | EURCAD | 13 | ✓ confirmed |
| EURCHF | 14 | ✓ confirmed | GBPAUD | 22 | ✓ confirmed |
| GBPCAD | 23 | ✓ confirmed | GBPCHF | 24 | ✓ confirmed |
| GBPNZD | 28 | ✓ confirmed | | | |
| CADCHF | 7  | inferred (verify) | EURNOK | 19 | inferred (verify) |
| EURSEK | 20 | inferred (verify) | USDNOK | 40 | inferred (verify) |
| USDSEK | 41 | inferred (verify) | USDDKK | 43 | inferred (verify) |
| USDMXN | 44 | inferred (verify) | | | |

> **Verify new UICs**: run `python forex/runner.py --info`

---

## CLI Reference

```bash
# Run all 10 swing strategies — all pairs
python forex/runner.py --live

# Session-aware runs (as used by Task Scheduler)
python forex/runner.py --live --session asian    # 06:20 PKT
python forex/runner.py --live --session london   # 18:00 PKT
python forex/runner.py --exits-only --live       # 14:00 PKT (stops only)

# London Breakout day-trading strategy (runs INDEPENDENTLY)
python forex/runner.py --strategy london_breakout --live           # entries (auto-detects London vs NY)
python forex/runner.py --strategy london_breakout --exits-only --live   # force-close
python forex/runner.py --strategy london_breakout --scan               # session range dashboard

# Single swing strategy
python forex/runner.py --live --strategy pullback
python forex/runner.py --live --strategy gap        # Sunday 22:00 PKT
python forex/runner.py --live --strategy supertrend
python forex/runner.py --live --strategy zscore
python forex/runner.py --live --strategy ml
python forex/runner.py --live --strategy cnn_lstm   # requires trained model

# Diagnostics
python forex/runner.py --scan      # 11-panel market snapshot (all strategies)
python forex/runner.py --status    # open positions + currency exposure
python forex/runner.py --info      # verify UICs live via Saxo API

# LBO test suite
python test_london_breakout.py     # 57 tests — run before any LBO change

# CNN-LSTM model management
python -m forex.cnn_lstm_trainer --train          # train on all 34 pairs (5y data)
python -m forex.cnn_lstm_trainer --train --pairs EURUSD GBPUSD  # specific pairs
python -m forex.cnn_lstm_trainer --status         # show walk-forward accuracy
python -m forex.cnn_lstm_trainer --backtest       # re-run validation without retraining
python -m forex.cnn_lstm_trainer --train --epochs 50  # quick test (fewer epochs)
```

---

---

## Strategy 11 — London Breakout (Day Trading) ★★

**File**: `forex/strategy_london_breakout.py`  
**Type**: Day Trading — Session Range Breakout  
**Win Rate**: ~58–63%  
**Capital**: 15,000 SEK dedicated book (independent from swing positions)  
**Slots**: 7 (one per pair, all 7 majors eligible per session)  
**No overnight holds** — all positions closed by 20:00 UTC (01:00 PKT)  

### Concept
FX markets compress during low-liquidity sessions, then release directionally when institutional flows kick in at session opens. We trade the **first directional break** of the compression range at London open (07:00 UTC) and NY open (13:00 UTC).

The stop is on the **opposite boundary of the reference range** — the level that invalidates the breakout thesis. The target is **2× the range size** — giving a 2:1 R/R on every trade. Range size filters (10–120 pips) eliminate thin sessions and chaotic sessions.

**This strategy is isolated from the swing book.** It bypasses portfolio heat checks and the swing drawdown gate via the `DAY_TRADE_STRATEGIES` set in the runner. Only the hard daily loss limit (–3% equity) applies to both books.

### Session Logic

| Session | Entry Window | Reference Range | Target Pairs |
|---------|-------------|-----------------|-------------|
| **London** | 07:00–10:00 UTC | Asian session H1 high/low (00:00–06:59 UTC) | EURUSD, GBPUSD, USDJPY, EURGBP, GBPJPY, AUDUSD, USDCAD |
| **NY** | 13:00–15:00 UTC | London morning H1 high/low (09:00–12:59 UTC) | Same 7 pairs |

### Entry — H1 bar confirmation required
| Direction | Conditions |
|-----------|-----------|
| **BUY**  | H1 close > range_high AND range is 10–120 pips |
| **SELL** | H1 close < range_low AND range is 10–120 pips |

### Exit (first condition hit)
- **A — Take profit**: price hits entry ± 2.0 × range size  
- **B — Stop loss**: price hits opposite range boundary  
- **C — Time stop**: 20:00 UTC hard close (no overnight holds)

### Parameters
| Param | Value |
|-------|-------|
| Pairs | EURUSD, GBPUSD, USDJPY, EURGBP, GBPJPY, AUDUSD, USDCAD |
| London entry window | 07:00–10:00 UTC |
| NY entry window | 13:00–15:00 UTC |
| Asian range hours | 00:00–06:59 UTC (H1 bars) |
| London morning range | 09:00–12:59 UTC (H1 bars) |
| Min range | 10 pips |
| Max range | 120 pips |
| TP ratio | 2.0 × range |
| Stop | Opposite range boundary |
| Time stop | 20:00 UTC |
| Risk per trade | 1.5% of equity |
| Capital | 15,000 SEK dedicated |
| Data | H1 bars (last 48 hours via Saxo API) |

### Position Sizing (15,000 SEK book)
```
risk_SEK = 15,000 × 0.015 = 225 SEK ≈ $21 USD
stop_distance = range_size (in price)
units = risk_USD / stop_distance
        → capped at 1,000 min / 50,000 max
```

### Scheduled Tasks (Task Scheduler)
| Task name | Trigger | Action |
|-----------|---------|--------|
| `lbo-london-open` | Mon–Fri 12:00 PKT (07:00 UTC) | `run_lbo_london.bat` — entries |
| `lbo-ny-open` | Mon–Fri 18:00 PKT (13:00 UTC) | `run_lbo_ny.bat` — entries |
| `lbo-force-close` | Daily 01:00 PKT (20:00 UTC) | `run_lbo_close.bat` — exits only |

### Email Alerts
Every open and close fires an immediate email via `forex/notifier.py`:
- **Open alert**: pair, direction, entry, stop, TP, R/R, risk in SEK, session name
- **Close alert**: WIN/LOSS badge, P&L % and SEK, exit reason (TP / SL / time_stop)

### Key Files
| File | Purpose |
|------|---------|
| `forex/strategy_london_breakout.py` | Strategy: signal generation, exit logic, scan summary |
| `run_lbo_london.bat` | London open launcher (hidden window via VBS) |
| `run_lbo_ny.bat` | NY open launcher |
| `run_lbo_close.bat` | Force-close launcher (exits only) |
| `test_london_breakout.py` | 57-test suite (unit / functional / blackbox / edge) |

### Test Suite
```powershell
python test_london_breakout.py
# → 57/57 PASS  (unit, functional, exit, scan, blackbox, edge cases)
```

---

## Currency Exposure Filter

The runner enforces `MAX_CURRENCY_EXPOSURE = 3` — at most **±3 net positions** per currency across all strategies simultaneously.

**Example**: If you already have 3 long positions involving USD (EURUSD short, GBPUSD short, USDJPY long), any new signal that would add a 4th USD long or short is **skipped** with a log message.

This prevents correlated drawdowns where 4+ strategies all lose simultaneously on the same currency move.
