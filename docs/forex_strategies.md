# Forex Strategy Playbook

**Module**: `forex/`  
**Universe**: 34 FX pairs — 7 G7 majors + 27 crosses (incl. Scandinavian & EM)  
**Strategies**: 4 × 4 + 2 × 25 = **66 max open positions**  
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

### Concept
Uses EMA(200) to lock in the major trend direction, then fires only when RSI(2) hits an extreme. A 2-day exhaustion move against the trend statistically snaps back within 3–8 sessions. Very fast in, very fast out.

### Entry
| Direction | Conditions |
|-----------|-----------|
| **LONG**  | close > EMA(200) AND RSI(2) < 10 |
| **SHORT** | close < EMA(200) AND RSI(2) > 90 |

### Exit (first condition hit)
- **A** — RSI(2) recovers: > 55 for longs / < 45 for shorts
- **B** — 2.0×ATR(14) hard stop
- **C** — 10-day time stop

### Parameters
| Param | Value |
|-------|-------|
| RSI period | 2 |
| Trend EMA | 200 |
| Entry long | RSI(2) < 10 |
| Entry short | RSI(2) > 90 |
| ATR stop mult | 2.0× |
| Time stop | 10 days |

> **Note**: RSI(2) moves extremely fast — can shift from 9 to 81 within a single session. Signals are only valid immediately after the scheduled scan fires.

---

## Strategy 3 — Donchian Channel Breakout

**File**: `forex/strategy_donchian.py`  
**Type**: Momentum  
**Win Rate**: ~50% (by design — edge comes from large winners, not high win rate)  

### Concept
Turtle Trading adapted for FX. A 20-day high/low breakout captures the start of a sustained move. Lower win rate but large winners — the few big trends more than compensate for small losses.

### Entry
| Direction | Conditions |
|-----------|-----------|
| **LONG**  | close > 20-day high (upside breakout) |
| **SHORT** | close < 20-day low (downside breakdown) |

### Exit (first condition hit)
- **A** — 10-day channel reversal (close crosses opposite channel side)
- **B** — 2.0×ATR(14) hard stop
- **C** — 60-day time stop

### Parameters
| Param | Value |
|-------|-------|
| Entry channel | 20 days |
| Exit channel | 10 days |
| ATR stop mult | 2.0× |
| Time stop | 60 days |

---

## Strategy 4 — Bollinger Band Reversion

**File**: `forex/strategy_bb.py`  
**Type**: Mean-Reversion  
**Win Rate**: ~60%  

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
**Win Rate**: ~70%+ (highest of all 5 strategies)  

### Concept
Enters the same trend as EMA crossover, but at a far better price. Instead of chasing the crossover, it waits for the market to pull back and touch EMA(20) — the dynamic support in an uptrend — then confirms the bounce with a close back in the trend direction.

**Triple confirmation** (trend + ADX + bounce) is what drives the elevated win rate. Because the entry is near EMA support, the stop is tighter → more units can be sized for the same 1% risk → larger profit on the same move.

### Entry — all three conditions must be true simultaneously
| Direction | Conditions |
|-----------|-----------|
| **LONG**  | close > EMA(50) AND ADX(14) ≥ 25 AND low touched EMA(20) within last 3 bars AND current close > EMA(20) (bounce confirmed) |
| **SHORT** | close < EMA(50) AND ADX(14) ≥ 25 AND high touched EMA(20) within last 3 bars AND current close < EMA(20) (breakdown confirmed) |

### Exit (first condition hit)
- **A** — Trend break: close < EMA(50) for longs / close > EMA(50) for shorts
- **B** — 1.5×ATR(14) hard stop (tight because entry is near EMA support)
- **C** — 25-day time stop (bounce should resolve quickly)

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

### Why the edge
- Enters at EMA support = tighter stop = larger position size for same 1% risk
- EMA crossover fires once per trend; pullback can fire 2–4× during the same trend
- Three simultaneous filters eliminate most false entries before they happen

---

## Strategy 6 — Weekend Gap Fill ★★

**File**: `forex/strategy_gap.py`  
**Type**: Statistical Mean-Reversion (Structural Edge)  
**Win Rate**: ~80–85% (highest of all strategies)  
**Runner flag**: `NEEDS_LIVE_PRICES = True` — runner fetches live Sunday open prices before calling this strategy

### Concept
FX markets close Friday ~22:00 GMT and reopen Sunday ~22:00 GMT. Price frequently gaps between the Friday close and the Sunday open due to weekend news, central bank statements, or geopolitical events.

Approximately **80–85% of these gaps fill within 5 trading days** — meaning price returns to Friday's close level. The edge is structural, not technical:
1. Market makers immediately quote back toward Friday's close
2. Algorithmic desks are programmed to fade weekend gaps
3. Retail traders close weekend positions at Sunday open

We enter the **fade direction** on Sunday night and target a full gap fill.

### Entry — Sunday 22:00 PKT (after FX market reopens)
| Direction | Conditions |
|-----------|-----------|
| **SHORT** | Sunday open > Friday close (gap up) AND gap % between 0.10% and 2.00% |
| **LONG**  | Sunday open < Friday close (gap down) AND gap % between 0.10% and 2.00% |

Gap filters:
- **Min gap 0.10%** — eliminates spread noise (gaps below this are statistical noise)
- **Max gap 2.00%** — extreme gaps (news events) fill less reliably, skipped

### Exit (first condition hit)
- **A** — Gap filled: price reaches Friday close level  
  Long exits when `cur_high ≥ friday_close` / Short exits when `cur_low ≤ friday_close`
- **B** — Hard stop: 1.5 × gap size against position  
  (If gap was 20 pips, stop is 30 pips away — i.e., price moved further from fill)
- **C** — 7-day time stop (≈ 5 trading days Mon–Fri)  
  Gaps that don't fill within a week are unlikely to fill at all

### Parameters
| Param | Value |
|-------|-------|
| Min gap size | 0.10% of price |
| Max gap size | 2.00% of price |
| ATR stop mult | 1.5× gap size |
| Time stop | 7 calendar days |
| Risk per trade | 1% equity |
| Live prices required | Yes (fetched at Sunday run) |

### Sizing note
Stop distance = 1.5 × gap size. Because the gap itself defines the volatility measure, position sizing is automatic — larger gaps get smaller positions, preserving the 1% risk rule.

### Frequency
Approximately 3–8 signals per Sunday across 34 pairs. Gap fills occur most often in JPY, AUD, NZD, and Scandinavian pairs where weekend news has the largest impact. USDNOK and USDSEK gap frequently on oil/macro news; USDMXN gaps on EM risk events.

### Why ~80–85% win rate
This is the genuine ceiling for a legitimate FX strategy. The edge is market microstructure, not pattern recognition — gap fading is built into how banks quote on Sunday open. The 7-day time stop keeps losing trades small, so the risk:reward remains sound even at this high win rate.

---

## Strategy Comparison

| # | Strategy | Type | Win Rate | Indicators | Stop | Time Stop | Slots |
|---|----------|------|----------|-----------|------|-----------|-------|
| 1 | EMA Crossover | Trend | ~55% | EMA(5/30) + ADX(14) | 1.5×ATR | 45d | 4 |
| 2 | RSI(2) Pullback | Reversion in trend | ~60% | RSI(2) + EMA(200) | 2.0×ATR | 10d | 4 |
| 3 | Donchian Breakout | Momentum | ~50% | 20d High/Low channel | 2.0×ATR | 60d | 4 |
| 4 | BB Reversion | Mean-reversion | ~60% | BB(20,2) + RSI(14) | 2.0×ATR | 8d | 4 |
| 5 | **Pullback-to-EMA** ★ | Trend continuation | **~70%+** | EMA(20/50) + ADX(14) | 1.5×ATR | 25d | **25** |
| 6 | **Weekend Gap Fill** ★★ | Structural mean-rev | **~80–85%** | Gap % + live price | 1.5×gap | 7d | **25** |

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
All pairs are scanned; only those showing a 0.10%–2.00% gap receive entries.

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

> **Verify new UICs**: run `python forex/runner.py --info` — invalid UICs return no data and the pair is silently skipped, no harm done.

---

## CLI Reference

```bash
# Run all strategies — all pairs
python forex/runner.py --live

# Session-aware runs (as used by Task Scheduler)
python forex/runner.py --live --session asian    # 06:20 PKT
python forex/runner.py --live --session london   # 18:00 PKT
python forex/runner.py --exits-only --live       # 14:00 PKT (stops only)

# Single strategy
python forex/runner.py --live --strategy pullback
python forex/runner.py --live --strategy gap     # Sunday 22:00 PKT
python forex/runner.py --live --strategy ema

# Diagnostics
python forex/runner.py --scan      # 6-panel market snapshot (all strategies)
python forex/runner.py --status    # open positions + currency exposure
python forex/runner.py --info      # verify UICs live via Saxo API
```

---

## Currency Exposure Filter

The runner enforces `MAX_CURRENCY_EXPOSURE = 3` — at most **±3 net positions** per currency across all strategies simultaneously.

**Example**: If you already have 3 long positions involving USD (EURUSD short, GBPUSD short, USDJPY long), any new signal that would add a 4th USD long or short is **skipped** with a log message.

This prevents correlated drawdowns where 4+ strategies all lose simultaneously on the same currency move. Exposure is checked per-entry and updated in real time within each run.
