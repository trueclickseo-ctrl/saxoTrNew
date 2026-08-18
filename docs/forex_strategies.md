# Forex Strategy Playbook

**Module**: `forex/`  
**Universe**: 34 FX pairs — 7 G7 majors + 27 crosses (incl. Scandinavian & EM)  
**Strategies**: 9 active  
**Max slots**: 4+4+4+4+34+34+20+20+20 = **144 theoretical max** (currency exposure filter limits practical concurrency)  
**Risk per trade**: 1% of account equity  

---

## Daily Schedule

| Task (Task Scheduler)  | Time PKT     | Session       | Pairs |
|------------------------|--------------|---------------|-------|
| ATOS Forex Daily Run   | 06:20 Mon–Fri | Asian        | 14 (JPY/AUD/NZD crosses) |
| ATOS Forex Exit Check  | 14:00 Mon–Fri | All          | 34 (stops only — no new entries) |
| ATOS Forex London Run  | 18:00 Mon–Fri | London       | 20 (EUR/GBP/USD + Scandi/CAD) |
| ATOS Forex Gap Fill    | 22:00 Sunday  | All          | 34 (gap fill entries only) |

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

## Strategy Comparison

| # | Strategy | Type | Win Rate | Key Indicators | Stop | Time Stop | Slots |
|---|----------|------|----------|---------------|------|-----------|-------|
| 1 | EMA Crossover | Trend | ~55% | EMA(5/30) + ADX(14) | 1.5×ATR | 45d | 4 |
| 2 | RSI(2) Pullback | Reversion-in-trend | ~60% | RSI(2) + EMA(200) | 1.5×ATR | 12d | **34** |
| 3 | Donchian Break | Momentum | ~50% | 30d High/Low + EMA(200) + ADX | 2.0×ATR | 30d | 4 |
| 4 | BB Reversion | Mean-reversion | ~60% | BB(20,2) + RSI(14) | 2.0×ATR | 8d | 4 |
| 5 | **Pullback-to-EMA** ★ | Trend continuation | **~70%+** | EMA(20/50) + ADX(14) | 1.5×ATR | 25d | **34** |
| 6 | **Weekend Gap Fill** ★★ | Structural mean-rev | **~80–85%** | Gap % + live price | 1.5×gap | 7d | **34** |
| 7 | SuperTrend | Trend | ~65% | ST(10,3) + EMA(200) | 2.0×ATR | 40d | 20 |
| 8 | Z-Score Rev | Mean-reversion | ~63% | 20d z-score + EMA(200) | 2.5×ATR | 12d | 20 |
| 9 | ML Signals | ML / Data-driven | ~57–62% | 7 features, logistic reg | 2.0×ATR | 20d | 20 |

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
# Run all 9 strategies — all pairs
python forex/runner.py --live

# Session-aware runs (as used by Task Scheduler)
python forex/runner.py --live --session asian    # 06:20 PKT
python forex/runner.py --live --session london   # 18:00 PKT
python forex/runner.py --exits-only --live       # 14:00 PKT (stops only)

# Single strategy
python forex/runner.py --live --strategy pullback
python forex/runner.py --live --strategy gap        # Sunday 22:00 PKT
python forex/runner.py --live --strategy supertrend
python forex/runner.py --live --strategy zscore
python forex/runner.py --live --strategy ml

# Diagnostics
python forex/runner.py --scan      # 9-panel market snapshot (all strategies)
python forex/runner.py --status    # open positions + currency exposure
python forex/runner.py --info      # verify UICs live via Saxo API
```

---

## Currency Exposure Filter

The runner enforces `MAX_CURRENCY_EXPOSURE = 3` — at most **±3 net positions** per currency across all strategies simultaneously.

**Example**: If you already have 3 long positions involving USD (EURUSD short, GBPUSD short, USDJPY long), any new signal that would add a 4th USD long or short is **skipped** with a log message.

This prevents correlated drawdowns where 4+ strategies all lose simultaneously on the same currency move.
