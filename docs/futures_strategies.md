# Futures Trading — Strategy Playbook

**Module**: `futures/runner.py`  
**Markets**: 13 across 5 asset classes and 3 currencies (see [Market Overview](#market-overview))  
**7 strategies × 5 slots = 35 max open positions**  
**Risk per trade**: 1% of `risk_equity_eur` (config), ATR-based sizing  
**Scheduled**: daily at 06:15 PKT (01:15 UTC) via `run_futures_daily.bat`  
**Last updated**: 2026-08-19

---

## Audit — 2026-08-19

| # | Finding | Severity | Status |
|---|---------|----------|--------|
| 1 | Dry runs deleted live position state, orphaning positions at the broker | **Critical** | Fixed `a34ffd0` |
| 2 | Sizing ran off 957,732 EUR of SIM demo credit (~34× intended capital) | **High** | Fixed `a34ffd0` |
| 3 | `max(1, …)` contract floor silently over-risked small accounts | **High** | Fixed `a34ffd0` |
| 4 | **6 of 7 strategies have no backtest at all** | **High** | Open |
| 5 | Backtest uses ETF proxies, not the live instruments; ZB proxied by TLT | Medium | Open |
| 6 | No reconciliation between local state and actual broker positions | Medium | Fixed `852eac2` |
| 7 | Documented parameters did not match code (`ATR_STOP_MULT` 5.0 vs 1.5) | Medium | Fixed here |
| 8 | Sharpe documented as 1.62; measured 0.750 | Medium | Fixed here |
| 9 | Universe grew 5 → 13 markets; 8 of them never backtested, 3 currencies | **High** | Measured — see 12 |
| 10 | Equity in EUR, ATR/`contract_size` in instrument currency, no FX conversion | Medium | Open |
| 11 | Source files contain mojibake (`â€"`) in comments/docstrings | Low | Open |
| 12 | **Expanding 5 → 13 markets turns every strategy negative** | **Critical** | Open — needs your call |

> **Findings 4 and 6 are now closed.** All 7 strategies are backtested
> (`backtest_futures_all.py`) and state reconciles against the broker
> (`--reconcile`). Finding 12 is what those two pieces of work uncovered.

### Finding 1 — dry runs destroyed state (critical)

`run_daily()` simulates exits into the in-memory `positions` dict in dry-run mode
exactly as in live mode, then called `_save_state()` unconditionally. Any dry run
whose exit rules fired wrote an **emptied** positions dict over
`data/futures_state.json`.

The positions remain open at the broker — the runner simply forgets them. No stop
management, no time stop, no exit, no record. Combined with Finding 6 (nothing
reconciles against broker positions) they are orphaned permanently.

This explains the live `macd:CL` and `macd:ZB` positions that filled 2026-08-18
18:16 (order ids `5039683399`/`5039683400`, **7,972 contracts short ZB**) and were
absent from state by 06:15 the next morning, with `Exits: 0` and no exit logged.

Reproduced during this audit — a dry run wiped `donchian:GC` and `donchian:NQ`.
Both restored. Dry runs no longer write state.

### Finding 2 — sizing ran off demo credit

`_account()` read Saxo `TotalValue` uncapped: **957,732 EUR** of SIM credit, not
real capital. The broker rejected the resulting orders outright:

```
[macd]    SKIP CL: 400 order size 1311 exceeds broker max
[squeeze] SKIP ZB: 400 order size 5227 exceeds broker max
```

So MACD and Squeeze could never place a trade. Sizing is now capped at
`strategies.futures.risk_equity_eur` (27,800 EUR ≈ 300,000 SEK).

> **Confirm this number.** 27,800 EUR assumes futures gets the *entire* stated
> account. Lower it if futures should share capital with the ATOS equity sleeves.

### Finding 3 — the 1-contract floor

`size_position()` returns `max(1, …)`. Where the risk-correct size is below one
contract, that floor turns "too small to trade" into "far over-risked" — one CL
contract risks 1.5 × 2.5 × 1000 = $3,750, i.e. **12.5%** of a 27,800 EUR account,
not 1%. `MAX_RISK_OVERSHOOT` (1.5×) now skips such signals with an explicit
reason. Several instruments (ZC, DAX, NQ) are simply too large at this equity —
now visible in the log rather than silently over-risked.

### Finding 4 — six strategies were unvalidated (CLOSED 2026-08-20)

`backtest_futures.py` imported only `futures.strategy` (Donchian), so RSI, EMA,
MACD, Squeeze, MA Cross and Trend MA had no backtest anywhere in the repo.

**Now measured** — see [Backtest Results](#backtest-results--all-7-strategies-2026-08-20).
`backtest_futures_all.py` drives the **real strategy modules** (their own
`generate_signals` / `should_exit` / `size_position`) rather than reimplementing
the rules, so what it measures is what `runner.py` executes.

The results overturned the previous assumptions. Donchian — the only strategy
that had ever been "validated" — **fails**, while Trend MA, which had never been
tested, is the only one that passes.

### Finding 6 — broker/state reconciliation (CLOSED 2026-08-20)

Nothing compared local state to the broker, so any state loss left positions open
with no stop management and no record. `reconcile_positions()` now surfaces both
directions:

- **ORPHAN** — open at the broker, absent from state: unmanaged risk
- **STALE** — in state, gone at the broker: phantom position holding a slot

Two details decide whether it works:

1. **Rolled contracts.** CL/ZB/NG/ZC/ZW/ZS roll monthly, so the UIC traded last
   month is not the UIC in today's universe. Matching only current UICs missed
   exactly the position most likely to be orphaned — the first version reported a
   clean "OK" while a real orphan sat on a rolled-out contract. The UIC set is now
   the current universe **∪ every UIC ever ordered**.
2. **Attribution.** `futures_orders.json` records strategy, entry and stop per
   order, so an orphan is re-attributed from its order-log entry and `--adopt`
   restores the *original* entry and stop rather than assuming current price.

```bash
python futures/runner.py --reconcile           # report only
python futures/runner.py --reconcile --adopt   # import orphans, drop stale
```

Also runs report-only inside `run_daily()` before trading.

**Applied to the live account:** found `macd:CL` (uic 55278986, 1 contract) open
and untracked since 2026-08-18, attributed it to `[macd]`, adopted it with its
original entry 84.86 / stop 77.33. It previously had no stop of any kind. The
7,972-contract ZB short from that same session was confirmed **not** open.

### Backtest Results — all 7 strategies (2026-08-20)

`python backtest_futures_all.py` — 5 years, ETF proxies, next-open fills, 0.05%
commission per side, 5 slots per strategy, `MAX_RISK_OVERSHOOT` guard applied.
Thresholds: Sharpe ≥ 0.70, WR ≥ 35%, MaxDD < 30%, N ≥ 30.

#### Core 5 markets (ES, NQ, GC, CL, ZB)

| Strategy | Trades | Sharpe | WR | MaxDD | CAGR | Hold | Verdict |
|----------|-------:|-------:|---:|------:|-----:|-----:|---------|
| **trend_ma** | 323 | **0.935** | 40.9% | 13.3% | **10.4%** | 12.2d | ✅ **PASS** |
| rsi | 110 | 0.477 | 60.9% | 6.1% | 2.6% | 3.9d | ❌ FAIL |
| donchian | 183 | 0.397 | 43.2% | 12.2% | 3.8% | 12.4d | ❌ FAIL |
| macd | 72 | 0.275 | 48.6% | 5.1% | 1.2% | 11.2d | ❌ FAIL |
| ema | 47 | 0.220 | 38.3% | 6.3% | 0.8% | 17.3d | ❌ FAIL |
| ma_cross | 10 | −0.102 | 30.0% | 4.4% | −0.2% | 38.5d | ⚠️ too few trades |
| squeeze | 22 | −0.947 | 13.6% | 10.7% | −2.0% | 8.4d | ⚠️ too few trades |

#### All 13 markets — every strategy fails

| Strategy | Trades | Sharpe | WR | MaxDD | CAGR | Verdict |
|----------|-------:|-------:|---:|------:|-----:|---------|
| rsi | 343 | 0.291 | 59.8% | 15.4% | 2.5% | ❌ FAIL |
| ema | 129 | 0.218 | 41.1% | 14.3% | 1.4% | ❌ FAIL |
| donchian | 496 | −0.021 | 37.9% | 36.6% | −2.4% | ❌ FAIL |
| trend_ma | 563 | −0.096 | 38.5% | **42.1%** | −3.4% | ❌ FAIL |
| macd | 184 | −0.160 | 37.0% | 13.6% | −1.5% | ❌ FAIL |
| ma_cross | 41 | −0.359 | 26.8% | 11.4% | −1.9% | ❌ FAIL |
| squeeze | 98 | −0.875 | 30.6% | 20.4% | −4.2% | ❌ FAIL |

#### Harness validation

The new harness reproduces the original `backtest_futures.py` trade generation
under matched assumptions — **249 trades vs its 248** — which is what confirms the
strategy modules are being driven correctly. Isolating each change to Donchian:

| Configuration | Sharpe | MaxDD | N |
|---------------|-------:|------:|--:|
| 5 mkts, close fill, no risk guard (≈ original) | 0.528 | 25.8% | 249 |
| + `MAX_RISK_OVERSHOOT` guard | 0.452 | 11.7% | 184 |
| + realistic next-open fills | 0.397 | 12.2% | 183 |
| **+ expand to 13 markets** | **−0.021** | **36.6%** | 496 |

The residual gap to the originally-reported 0.750 is the date window (this harness
includes ~10 extra months) and equity-curve construction; the matching trade count
is the meaningful check.

### 🔴 Finding 12 — the universe expansion destroyed the edge (2026-08-20)

The single most damaging line above: **going from 5 to 13 markets takes Donchian
from Sharpe 0.397 to −0.021 and triples drawdown 12.2% → 36.6%.** Trend MA falls
even harder, 0.935 → −0.096 with a 42.1% drawdown.

This is not dilution by a couple of weak markets — it is every strategy turning
negative at once. The 8 added markets (YM, DAX, HK50, SI, NG, ZC, ZW, ZS) are
lower-quality trend vehicles, and adding them multiplied correlated equity-index
exposure (ES/NQ/YM/DAX/HK50 are five expressions of one trade) while the grain
complex added noise.

**Recommendation, in priority order:**

1. **Restrict `load_universe()` to ES, NQ, GC, CL, ZB.** This is the highest-value
   single change in the module — it is the difference between a losing system and
   a marginal-to-good one.
2. **Keep `trend_ma`** — the only strategy passing thresholds (Sharpe 0.935).
3. **Retire `squeeze` and `ma_cross`** — negative Sharpe, and only 22 and 10 trades
   in 5 years. They cannot be evaluated and are not earning their slot.
4. **Demote `donchian` from primary.** It fails at 0.397 and is the strategy the
   module was built around. Its documented 1.62 Sharpe never existed.
5. `rsi`, `macd`, `ema` are mildly positive but below threshold — keep only if you
   want breadth; none is carrying the portfolio.

> These results are on ETF proxies, not live instruments (Finding 5), so treat the
> *ranking* as more reliable than the absolute numbers.

### Finding 9 — universe grew 5 → 13 markets (open)

The docs described 5 markets (ES, NQ, GC, CL, ZB). `futures/universe.py` defines
**13**, and all 13 have cached UICs, so all are live. Added: YM, DAX, HK50, SI, NG,
ZC, ZW, ZS.

This compounds Finding 4. The validation gap is not just "6 of 7 strategies
unbacktested" — it is that **8 of 13 markets have no backtest data either**, so
most strategy/market combinations in production have never been measured. The
2026-08-19 dry run generated live signals on ZC and DAX, neither of which appears
anywhere in `backtest_futures.py`.

It also introduced two currencies the sizing code does not handle (Finding 10) and
new correlation exposure — the grain complex `{ZC, ZW, ZS}` and `{GC, SI}` are
handled by `CORRELATED_PAIRS`, but that list must be maintained by hand as markets
are added.

**Before expanding further:** extend the backtest to cover every traded market, or
restrict `load_universe()` to the validated set.

### Finding 10 — no FX conversion in sizing (open)

`_account()` returns equity in the account currency (**EUR**), while `sig["atr"]`
and `contract_size` are denominated in the *instrument's* currency.
`futures/runner.py` imports no FX module and performs no conversion, so:

| Instruments | Currency | Sizing error |
|-------------|----------|--------------|
| DAX | EUR | none — matches account |
| ES, NQ, YM, GC, SI, CL, NG, ZB, ZC, ZW, ZS | USD | ~1.08× oversized |
| HK50 | HKD | ~9× oversized |

HK50 is the one that matters. Fixing this means converting the risk budget into
the instrument currency before dividing by `risk_per_contract`.

### Verified as correct

Not everything was broken. Confirmed sound by inspection:

- **ADX** — textbook Wilder implementation (correct `+DM`/`−DM` selection, RMA
  smoothing; the `1/period` factors correctly cancel between DI numerator and ATR
  denominator).
- **ATR** — correct Wilder RMA (`ewm(alpha=1/period, adjust=False)`), consistent
  across all modules.
- **Donchian lookback** — `closes.iloc[-(N+1):-1]` correctly **excludes the
  current bar**, so there is no lookahead bias in the breakout test.
- **Stop comparison** — hard stops compare against the intraday low/high rather
  than the close, which is the conservative choice.
- **Portfolio guards** — correlation groups, portfolio heat cap (6%), drawdown
  circuit breaker (10%) and the daily loss limit (−3%) are all implemented and
  wired into the entry path.

---

## Market Overview

All 13 are defined in `futures/universe.py` and all 13 have cached UICs — the
module trades the full set, not a subset.

| Symbol | Name | Saxo asset type | Ccy | `contract_size` | Direction |
|--------|------|-----------------|-----|-----------------|-----------|
| ES | S&P 500 Index CFD | CfdOnIndex | USD | 1 | Long only |
| NQ | NASDAQ-100 Index CFD | CfdOnIndex | USD | 1 | Long only |
| YM | Dow Jones 30 CFD | CfdOnIndex | USD | 1 | Long only |
| DAX | Germany 40 CFD | CfdOnIndex | **EUR** | 1 | Long only |
| HK50 | Hang Seng 50 CFD | CfdOnIndex | **HKD** | 1 | Long only |
| GC | Gold Spot (XAU/USD) | FxSpot | USD | 1 | Bidirectional |
| SI | Silver Spot (XAG/USD) | FxSpot | USD | 1 | Bidirectional |
| CL | WTI Crude Oil | ContractFutures | USD | 1,000 | Long only |
| NG | Natural Gas | ContractFutures | USD | 10,000 | Long only |
| ZB | 30-Year T-Bond | ContractFutures | USD | 1,000 | Bidirectional |
| ZC | Corn | ContractFutures | USD | 50 | Bidirectional |
| ZW | Wheat | ContractFutures | USD | 50 | Bidirectional |
| ZS | Soybeans | ContractFutures | USD | 50 | Bidirectional |

**Regime filter**: longs on **all five equity indices** (ES, NQ, YM, DAX, HK50) are
blocked when ES < SMA(200) — `EQUITY_FUTURES` in each strategy module, not just
ES/NQ as previously documented.

**Correlation groups** (`runner.py`) — never the same direction in more than one
per group: `{ES, NQ, YM}`, `{GC, SI}`, `{CL, NG}`, `{ZC, ZW, ZS}`.

> **Three currencies, no FX conversion.** DAX settles in EUR (the account
> currency, so correct) but HK50 settles in **HKD** — roughly a 9× sizing error,
> and USD instruments are off by ~1.08×. See [Position Sizing](#position-sizing).

> **8 of these 13 markets have never been backtested.** `backtest_futures.py`
> covers only ES, NQ, GC, CL and ZN-via-TLT. YM, DAX, HK50, SI, NG, ZC, ZW and ZS
> are traded live on strategies validated (where validated at all) against a
> different, smaller market set. See [Finding 9](#finding-9--universe-grew-5--13-markets-open).

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
**Markets**: All 13 (long only for equity indices + CL/NG; bidirectional for GC/SI/ZB/ZC/ZW/ZS)

### Concept
Price breaking above the highest high of the past 30 days signals a genuine breakout from established resistance. Momentum traders flood in, creating the trend. The opposite for shorts.

### Entry
- **Long**: Close > 30-day high
- **Short** (GC/ZB only): Close < 30-day low
- Regime filter applies to ES/NQ

### Exit (first condition)
1. **Donchian trailing**: close below the 5-day lowest close (longs) / above the 5-day highest close (shorts)
2. 1.5×ATR hard stop — compared against the intraday low/high, not the close
3. 30-day time stop

### Parameters
```
BREAKOUT_PERIOD = 30     # entry: close above N-day highest close
EXIT_PERIOD     = 5      # exit:  close below N-day lowest close (trailing)
ATR_PERIOD      = 14
ATR_STOP_MULT   = 1.5
TIME_STOP_DAYS  = 30
RISK_PCT        = 0.01
```

### Measured results (`python backtest_futures.py`, 2026-08-19)

| Metric | Value |
|--------|-------|
| Sharpe | **0.750** |
| Win rate | 43.1% |
| Max drawdown | 9.8% |
| CAGR | 8.7% |
| Trades | 248 (5y, 5 markets) |

Passes the enable thresholds (Sharpe ≥ 0.70, WR ≥ 35%, MaxDD < 30%, N ≥ 30).

> **This is the only futures strategy with a backtest.** See
> [Audit](#audit--2026-08-19). The backtest also runs on **ETF proxies**
> (SPY/QQQ/GLD/USO/TLT), not the CFD/FxSpot/ContractFutures instruments actually
> traded live, and proxies ZB with TLT — so even this figure is indicative, not
> a measurement of the live system.

---

## Strategy 2 — RSI(5) Pullback

**Type**: Mean reversion / pullback within trend  
**Markets**: All 13

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

### Expected results — ⚠️ UNVALIDATED (no backtest exists)
~25-35 signals/yr | WR ~58-64% | Avg hold ~6 days

---

## Strategy 3 — EMA(5/20) Crossover

**Type**: Trend following / momentum  
**Markets**: All 13

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
FAST_EMA        = 5
SLOW_EMA        = 20
ADX_MIN         = 20
ATR_STOP_MULT   = 2.0
TIME_STOP_DAYS  = 25    # code value (docs previously said 20)
SIGNAL_LOOKBACK = 3     # bars; code value (docs previously said 2)
RISK_PCT        = 0.01
```

### Expected results — ⚠️ UNVALIDATED (no backtest exists)
~30-40 signals/yr | WR ~50-56% | Avg hold ~9 days

---

## Strategy 4 — MACD(12,26,9) Momentum Crossover

**Type**: Momentum / crossover  
**Markets**: All 13

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

### Expected results — ⚠️ UNVALIDATED (no backtest exists)
~12-18 signals/yr | WR ~52-58% | Avg hold ~14 days  
Edge: catches momentum inflection points earlier than price crossovers

---

## Strategy 5 — Bollinger Band Squeeze Breakout

**Type**: Volatility breakout  
**Markets**: All 13 (GC/SI/ZB/ZC/ZW/ZS bidirectional)

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

### Expected results — ⚠️ UNVALIDATED (no backtest exists)
~10-15 signals/yr | WR ~60-65% | Avg hold ~8 days  
Edge: enters at the start of a volatility expansion — tight stop, large potential move relative to risk

---

## Strategy 6 — SMA(50/200) Golden/Death Cross

**Type**: Long-term trend confirmation  
**Markets**: All 13 (GC/SI/ZB/ZC/ZW/ZS bidirectional)

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

### Expected results — ⚠️ UNVALIDATED (no backtest exists)
~4-8 signals/yr | WR ~65-70% | Avg hold ~25 days  
Edge: highest signal quality of all 6 strategies — classic large winners (oil 2022, gold 2023, bonds 2020)

---

## Strategy 7 — MA(20/100) Medium-Term Trend

**File**: `futures/strategy_trend_ma.py`  
**Type**: Medium-term trend following with volatility filter  
**Markets**: All 13 (long only for equity indices + CL/NG; bidirectional for GC/SI/ZB/ZC/ZW/ZS)

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

### Expected Results — ⚠️ UNVALIDATED (no backtest exists)

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

> **Every figure in this table except Donchian's is an estimate, not a
> measurement** — all seven are now measured; see
> [Backtest Results](#backtest-results--all-7-strategies-2026-08-20).
> Only Donchian has a backtest, and it measured Sharpe 0.750, not the 1.62
> previously documented.

---

## Adding More Strategies — no; subtract instead (updated 2026-08-20)

All seven are now measured, and the results say the module needs **subtraction,
not addition**.

On the full 13-market universe every strategy is negative. On the core 5, exactly
one clears the thresholds. Adding an 8th to a system where six of seven fail would
add noise to a portfolio already losing to breadth.

**Order of work:**

1. **Restrict the universe to ES, NQ, GC, CL, ZB** (Finding 12). Highest-value
   single change — it decides whether anything works at all.
2. **Retire `squeeze` and `ma_cross`** — negative Sharpe on 22 and 10 trades in
   5 years. Not evaluable, not earning their slots.
3. **Re-measure the survivors *together*.** Every table here treats each strategy
   in isolation with its own 5 slots. Run concurrently they compete for capital and
   take correlated positions in the same 5 markets, so portfolio Sharpe is *not*
   the average of the individual Sharpes. This is the biggest remaining unknown and
   no number in this document answers it.
4. Validate on the **live instruments** rather than ETF proxies (Finding 5).
5. Only then consider an 8th strategy.

**On profitability:** on the core 5 markets `trend_ma` is genuinely good — Sharpe
0.935, CAGR 10.4%, MaxDD 13.3% over 323 trades. `rsi` is interesting for a
different reason: a 60.9% win rate at a 3.9-day hold, the only mean-reversion
profile in the set, so it may diversify the trend strategies even while below
threshold.

Everything else is at or below noise. Donchian — the strategy this module was built
around, and the only one previously believed validated — measures **0.397**. Its
documented 1.62 Sharpe never existed.

---

## Position Sizing

All strategies use ATR-based position sizing:
```
risk_equity   = min(broker TotalValue, risk_equity_eur)   # config cap
risk_amount   = risk_equity × 1%
stop_distance = ATR_STOP_MULT × ATR × contract_size
quantity      = max(1, int(risk_amount / stop_distance))

# Guard: skip the signal entirely when even 1 contract over-risks
if stop_distance > risk_amount × MAX_RISK_OVERSHOOT:  # 1.5×
    skip
```

This automatically sizes down in high-volatility markets and sizes up in low-volatility ones.

**Two caps matter here.** `risk_equity_eur` stops SIM demo credit from inflating
sizes (Finding 2). `MAX_RISK_OVERSHOOT` stops the `max(1, …)` floor from taking a
position that risks far more than 1% when the correct size is under one contract
(Finding 3).

> **Known gap:** equity is in EUR while `ATR`/`contract_size` are in the
> instrument's currency, with no conversion — see
> [Finding 10](#finding-10--no-fx-conversion-in-sizing-open).

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
