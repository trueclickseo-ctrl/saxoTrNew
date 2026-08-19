# Futures Trading — Strategy Playbook

**Module**: `futures/runner.py`  
**Markets**: ES, NQ, GC, CL, ZB  (5 CME futures via Saxo SIM)  
**7 strategies × 5 slots = 35 max open positions**  
**Risk per trade**: 1% of equity, ATR-based sizing  
**Scheduled**: daily at 06:15 PKT (01:15 UTC) via `run_futures_daily.bat`  
**Last updated**: 2026-08-19

---

## Market Overview

| Symbol | Name                  | Type         | Direction     |
|--------|-----------------------|--------------|---------------|
| ES     | S&P 500 E-mini        | Equity Index | Long only     |
| NQ     | Nasdaq 100 E-mini     | Equity Index | Long only     |
| GC     | Gold                  | Commodity    | Bidirectional |
| CL     | Crude Oil             | Commodity    | Long only     |
| ZB     | 30-Year T-Bond        | Fixed Income | Bidirectional |

**Regime filter (ES/NQ)**: Equity index longs blocked when ES < SMA(200) — avoids buying in confirmed bear markets.

---

## Strategy Schedule

| Strategy    | Run                    | Signal frequency  | Hold period  |
|-------------|------------------------|-------------------|--------------|
| Donchian    | Daily after close      | ~20-30 / yr       | 5-15 days    |
| RSI(5)      | Daily after close      | ~25-35 / yr       | 3-8 days     |
| EMA(5/20)   | Daily after close      | ~30-40 / yr       | 5-12 days    |
| MACD(12/26) | Daily after close      | ~12-18 / yr       | 10-20 days   |
| BB Squeeze  | Daily after close      | ~10-15 / yr       | 5-10 days    |
| MA Cross    | Daily after close      | ~4-8 / yr         | 15-40 days   |
| Trend MA    | Daily after close      | ~15-25 / yr       | 18-35 days   |

---

## Strategy 1 — Donchian Channel Breakout

**Type**: Trend following / breakout  
**Markets**: All 5 (long only for ES/NQ/CL, bidirectional for GC/ZB)

### Concept
Price breaking above the highest high of the past 30 days signals a genuine breakout from established resistance. Momentum traders flood in, creating the trend. The opposite for shorts.

### Entry
- **Long**: Close > 30-day high
- **Short** (GC/ZB only): Close < 30-day low
- Regime filter applies to ES/NQ

### Exit (first condition)
1. 5×ATR hard stop
2. 30-day time stop

### Parameters
```
BREAKOUT_PERIOD = 30
ATR_STOP_MULT   = 5.0
TIME_STOP_DAYS  = 30
RISK_PCT        = 0.01
```

### Expected results
~20-30 signals/yr | WR ~45-55% | Avg hold ~12 days | **Sharpe ~1.62** (grid-optimal)

---

## Strategy 2 — RSI(5) Pullback

**Type**: Mean reversion / pullback within trend  
**Markets**: All 5

### Concept
In a bull trend (price > 50d SMA), short-term RSI oversold readings (RSI < 30) signal a temporary dip, not a trend reversal. Buy the dip, sell the rip.

### Entry
- **Long**: RSI(5) < 30 AND close > SMA(50) (bull trend)
- **Short** (GC/ZB): RSI(5) > 70 AND close < SMA(50) (bear trend)
- Regime filter applies to ES/NQ

### Exit (first condition)
1. RSI(5) > 60 (for longs) / RSI(5) < 40 (for shorts) — exit when momentum exhausted
2. 2×ATR hard stop
3. 10-day time stop

### Parameters
```
RSI_PERIOD     = 5
RSI_OVERSOLD   = 30
RSI_OVERBOUGHT = 70
RSI_EXIT_LONG  = 60
RSI_EXIT_SHORT = 40
ATR_STOP_MULT  = 2.0
TIME_STOP_DAYS = 10
RISK_PCT       = 0.01
```

### Expected results
~25-35 signals/yr | WR ~58-64% | Avg hold ~6 days

---

## Strategy 3 — EMA(5/20) Crossover

**Type**: Trend following / momentum  
**Markets**: All 5

### Concept
When the fast EMA(5) crosses above slow EMA(20) with ADX confirming a trend (ADX ≥ 20), it signals fresh momentum. Complementary to Donchian — catches medium-term trend shifts rather than breakouts.

### Entry
- **Long**: EMA(5) crosses above EMA(20) within last 2 bars AND ADX ≥ 20
- **Short** (GC/ZB): EMA(5) crosses below EMA(20) AND ADX ≥ 20
- Regime filter applies to ES/NQ

### Exit (first condition)
1. EMA(5) crosses back through EMA(20)
2. 2×ATR hard stop
3. 20-day time stop

### Parameters
```
FAST_EMA       = 5
SLOW_EMA       = 20
ADX_MIN        = 20
ATR_STOP_MULT  = 2.0
TIME_STOP_DAYS = 20
RISK_PCT       = 0.01
```

### Expected results
~30-40 signals/yr | WR ~50-56% | Avg hold ~9 days

---

## Strategy 4 — MACD(12,26,9) Momentum Crossover

**Type**: Momentum / crossover  
**Markets**: All 5

### Concept
MACD measures the difference between EMA(12) and EMA(26). When the MACD line crosses above its signal line with the histogram turning positive AND MACD > 0 (above zero line), short-term momentum is accelerating in the trend direction. The zero-line filter removes counter-trend entries.

Complementary to EMA(5/20): MACD uses longer periods and requires histogram confirmation — fewer signals but higher quality.

### Entry
- **Long**: MACD crosses above signal (within 2 bars) AND MACD > 0 AND ADX ≥ 18
- **Short** (GC/ZB): MACD crosses below signal AND MACD < 0 AND ADX ≥ 18
- Regime filter applies to ES/NQ

### Exit (first condition)
1. MACD crosses back through signal (momentum reversal)
2. 2×ATR hard stop
3. 20-day time stop

### Parameters
```
MACD_FAST      = 12
MACD_SLOW      = 26
MACD_SIGNAL    = 9
ADX_MIN        = 18
ATR_STOP_MULT  = 2.0
TIME_STOP_DAYS = 20
RISK_PCT       = 0.01
```

### Expected results
~12-18 signals/yr | WR ~52-58% | Avg hold ~14 days  
Edge: catches momentum inflection points earlier than price crossovers

---

## Strategy 5 — Bollinger Band Squeeze Breakout

**Type**: Volatility breakout  
**Markets**: All 5 (GC/ZB bidirectional)

### Concept
Markets alternate between compression (low volatility) and expansion (high volatility). A "squeeze" occurs when Bollinger Bands (BB, 20d, 2σ) contract *inside* Keltner Channels (KC, 20d EMA ± 1.5×ATR). When BB eventually expands back outside KC, a directional breakout is imminent — volatility is releasing.

Based on John Carter's TTM Squeeze. Direction is determined by the TTM momentum oscillator: close minus midpoint of the 20-day high/low range.

### Entry
- Squeeze just released (BB was inside KC on previous bar, not now)
- **Long**: momentum > 0 AND close > EMA(20)
- **Short** (GC/ZB): momentum < 0 AND close < EMA(20)
- Regime filter applies to ES/NQ

### Exit (first condition)
1. Momentum reverses sign (histogram crosses zero)
2. 2×ATR hard stop
3. 15-day time stop (squeezes resolve fast — cut stale signals early)

### Parameters
```
BB_PERIOD      = 20
BB_STD         = 2.0
KC_EMA_PERIOD  = 20
KC_ATR_MULT    = 1.5
ATR_STOP_MULT  = 2.0
TIME_STOP_DAYS = 15
RISK_PCT       = 0.01
```

### Expected results
~10-15 signals/yr | WR ~60-65% | Avg hold ~8 days  
Edge: enters at the start of a volatility expansion — tight stop, large potential move relative to risk

---

## Strategy 6 — SMA(50/200) Golden/Death Cross

**Type**: Long-term trend confirmation  
**Markets**: All 5 (GC/ZB bidirectional)

### Concept
When the 50d SMA crosses above the 200d SMA ("Golden Cross"), it confirms a long-term trend shift from bear to bull. The signal is rare (2-4 per market per year) but extremely high quality — by the time the cross occurs, the trend is well-established and committed.

Note: the cross is a **lagging** signal by design. You miss the first part of the move in exchange for much higher signal quality. The wide stop (2.5×ATR) and long time stop (60 days) give the trend room to develop.

The MA cross itself is the regime filter — no separate ES/SMA(200) check needed.

### Entry
- **Long** (all markets): SMA(50) crosses above SMA(200) within last 3 bars AND SMA(50) > SMA(200) AND ADX ≥ 15
- **Short** (GC/ZB only): SMA(50) crosses below SMA(200) within last 3 bars AND SMA(50) < SMA(200) AND ADX ≥ 15

### Exit (first condition)
1. MAs re-cross in opposite direction (confirmed reversal)
2. 2.5×ATR hard stop (wider — trend needs room to breathe)
3. 60-day time stop

### Parameters
```
FAST_MA        = 50
SLOW_MA        = 200
ADX_MIN        = 15
ATR_STOP_MULT  = 2.5
TIME_STOP_DAYS = 60
RISK_PCT       = 0.01
SIGNAL_LOOKBACK = 3
```

### Expected results
~4-8 signals/yr | WR ~65-70% | Avg hold ~25 days  
Edge: highest signal quality of all 6 strategies — classic large winners (oil 2022, gold 2023, bonds 2020)

---

## Strategy 7 — MA(20/100) Medium-Term Trend

**File**: `futures/strategy_trend_ma.py`  
**Type**: Medium-term trend following with volatility filter  
**Markets**: All 5 (long only for ES/NQ/CL, bidirectional for GC/ZB)

### Concept

MA(20) vs MA(100) sits between the fast EMA(5/20) and the slow SMA(50/200), capturing multi-week trends that last 1–6 weeks — the sweet spot for liquid futures. It generates more signals than the Golden Cross (which waits for 50/200 separation) but is more committed than the EMA crossover (which fires on 1-week moves).

**Trend Strength (TS)** normalises the MA gap by price: only trade when `|TS| > 0.3%`. This filters weak crossovers where the MAs have barely separated and the signal is noise.

**Volatility regime filter**: skip new entries when the 20-day realised vol is in the top 80th percentile of its own 252-day history. High-vol regimes widen ATR stops and cause frequent whipsaws — sit out.

**Trailing stop follows MA(50) ± 1.5×ATR**, ratcheting in the favourable direction. This keeps winners running through normal pullbacks while a structural trend reversal (MA50 breaking below the bar) exits the trade.

**Daily loss limit**: new entries are blocked for the rest of the day if realised P&L falls below −3% of equity.

### Entry

| Direction | Condition                                                                 |
|-----------|---------------------------------------------------------------------------|
| Long      | MA20 > MA100 AND TS > +0.003 AND vol < 80th pct AND not risk-off equity  |
| Short     | MA20 < MA100 AND TS < −0.003 AND vol < 80th pct (GC/ZB only)             |

Equity index longs (ES/NQ) are additionally blocked when ES < SMA(200) (risk-off regime).

### Exit (first condition hit)

| Condition | Rule                                      |
|-----------|-------------------------------------------|
| A         | MA50 ± 1.5×ATR trailing stop (ratchets)  |
| B         | 2×ATR hard stop from entry               |
| C         | 60 calendar-day time stop                |

### Parameters

```python
FAST_MA        = 20     # fast moving average
SLOW_MA        = 100    # slow moving average
TRAIL_MA       = 50     # MA used for trailing stop
ATR_PERIOD     = 20     # ATR period (one trading month)
ATR_STOP_MULT  = 2.0    # initial hard stop: 2×ATR from entry
TRAIL_MULT     = 1.5    # trailing stop band: MA50 ± 1.5×ATR
TS_THRESHOLD   = 0.003  # minimum trend strength (0.3% of price)
RISK_PCT       = 0.01   # 1% equity per trade
TIME_STOP_DAYS = 60     # 60 calendar days
VOL_LOOKBACK   = 252    # 1 year vol history
VOL_BLOCK_PCT  = 0.80   # block when vol > 80th percentile
```

### Daily Loss Limit

`runner.py` checks realised P&L from `data/futures_orders.json` before any entry loop. If today's P&L as a fraction of account equity ≤ −3%, ALL strategy entries are blocked until the next calendar day. Exits are never blocked.

```python
DAILY_LOSS_LIMIT_PCT = -3.0  # in runner.py
```

### Expected Results

~15–25 signals/yr | WR ~50–55% | Avg hold ~28 days  
Win rate is moderate — trend-following edge comes from large winners, not high WR.  
Fills the gap between EMA(5/20) [too fast, 9d avg] and SMA(50/200) [too slow, 25d avg].

---

## Strategy Comparison

| Strategy      | Signals/yr | Win Rate   | Hold Time   | Stop             | Direction      |
|---------------|------------|------------|-------------|------------------|----------------|
| Donchian      | 20-30      | 45-55%     | ~12d        | 5×ATR            | L + GC/ZB short|
| RSI Pullback  | 25-35      | 58-64%     | ~6d         | 2×ATR            | L + GC/ZB short|
| EMA Crossover | 30-40      | 50-56%     | ~9d         | 2×ATR            | L + GC/ZB short|
| MACD Momentum | 12-18      | 52-58%     | ~14d        | 2×ATR            | L + GC/ZB short|
| BB Squeeze    | 10-15      | 60-65%     | ~8d         | 2×ATR            | L + GC/ZB short|
| MA Cross ★    | 4-8        | 65-70%     | ~25d        | 2.5×ATR          | L + GC/ZB short|
| Trend MA ◆    | 15-25      | 50-55%     | ~28d        | 2×ATR + MA50 trail| L + GC/ZB short|

★ MA Cross = highest quality, lowest frequency  
◆ Trend MA = fills medium-term gap between EMA(5/20) and SMA(50/200)

---

## Position Sizing

All strategies use ATR-based position sizing:
```
risk_amount   = account_equity × 1%
stop_distance = ATR_STOP_MULT × ATR × contract_size
quantity      = max(1, int(risk_amount / stop_distance))
```

This automatically sizes down in high-volatility markets and sizes up in low-volatility ones.

---

## Running the Futures Module

```bash
# Dry run (default) — no real orders, logs to console
python futures/runner.py

# Live mode — places real Saxo orders (runs all 7 strategies)
python futures/runner.py --live

# Run a single strategy only
python futures/runner.py --strategy donchian --live
python futures/runner.py --strategy macd --live
python futures/runner.py --strategy trend_ma --live

# 7-panel market snapshot (no orders)
python futures/runner.py --scan

# Show open positions
python futures/runner.py --status

# Discover and cache fresh UICs (CL/ZB change monthly)
python futures/runner.py --discover
```

### UICs (Saxo SIM)

| Symbol | Instrument type | UIC note |
|--------|----------------|----------|
| ES | CdfOnIndex | Stable UIC — does not change |
| NQ | CdfOnIndex | Stable UIC — does not change |
| GC | FxSpot | Stable UIC — does not change |
| CL | ContractFutures | Changes monthly — run `--discover` |
| ZB | ContractFutures | Changes monthly — run `--discover` |

Cache stored in `data/futures_uic_cache.json`. Run `--discover` at the start of each month.

### Scan panel descriptions
- **DONCHIAN**: 30d high/low levels vs current price
- **RSI**: RSI(5) values with trend direction
- **EMA**: EMA(5/20) gap % and ADX
- **MACD**: MACD line, signal, histogram, zone (bull/bear)
- **SQUEEZE**: BB width, TTM momentum, squeeze status
- **MA CROSS**: SMA(50/200) levels, gap %, regime (BULL/BEAR)
- **TREND MA**: MA(20/100) trend strength, vol percentile, bias (BULL/BEAR/flat)

### Daily Loss Limit
Runner checks realized P&L before each entry loop. If today's P&L ≤ −3% of equity, ALL new entries are blocked for the rest of the day. Exits are never blocked.

### State files
| File | Purpose |
|------|---------|
| `data/futures_state.json` | Open positions (keyed `strategy:symbol`, e.g. `donchian:GC`) |
| `data/futures_orders.json` | Order log (last 500 entries) |
| `data/futures_uic_cache.json` | CL/ZB UIC cache (refresh monthly with `--discover`) |
