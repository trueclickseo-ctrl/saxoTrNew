# Binance Strategy Notes — Crypto Mean Reversion

Strategy file: `strategies/binance/mean_reversion.py`
Config: `binance/config/binance_testnet_config.yaml` under `strategy:`

---

## Why this strategy is Binance-specific

This strategy cannot be shared with the Saxo bot's universe without modifications:

1. **24/7 market** — no market-hours gate needed. Saxo strategies have time-of-day
   guards for US market open (15:30 PKT); crypto runs all the time.

2. **USDT sizing** — position size is a percentage of free USDT.
   Saxo uses SEK (Swedish krona) converted via fx.py. Keeping them separate avoids
   FX assumptions leaking between the two adapters.

3. **Lot-size precision** — BTC requires 5 decimal places, ETH 4, BNB 2.
   The Saxo instrument map (data/instrument_map.csv) does not apply here.

4. **Crypto volatility** — RSI oversold threshold is 35 (vs Saxo's 33) and stop-loss
   is 4% (same as Saxo reversion). Crypto makes larger daily moves than S&P stocks
   so these thresholds are deliberately calibrated separately.

---

## Signal conditions (all 4 must fire)

```
1. RSI-14 < 35           -- oversold momentum (Wilder RSI, same algorithm as Saxo)
2. Price >= 5% below SMA-20  -- meaningful pullback, not just normal noise
3. 24h volume > 1.5x 20d avg -- real sell-off driven by volume (panic, not drift)
4. Price > EMA-200       -- long-term uptrend intact (don't catch falling knives)
```

The 4-condition gate is the same structural logic as the Saxo US Reversion strategy.
It was ported conceptually, not by copy-paste, because the implementation details differ.

---

## Universe (default)

| Symbol | Rationale |
|---|---|
| BTCUSDT | Highest liquidity on any exchange, 24/7 |
| ETHUSDT | Second by liquidity; moves semi-independently of BTC |
| BNBUSDT | Exchange token, liquid, different correlation |
| SOLUSDT | Higher beta — catches bigger dips, higher reward/risk |
| ADAUSDT | Different L1 narrative; useful for diversification |

All are USDT pairs (quote currency consistent with account). Do not add BTC-quoted
pairs unless you adjust the position sizing logic to account for BTC exposure.

---

## Position sizing

```
position_size_pct = 25%    (of free USDT balance per position)
max_slots         = 3       (maximum simultaneous open positions)
max_account_risk  = 10%     (never risk more than 10% of total equity at once)
```

With 3 slots at 25% each, 75% of free USDT is deployed when fully loaded.
The remaining 25% acts as a liquidity buffer.

---

## Stop-loss & exit

| Rule | Value |
|---|---|
| Hard stop | -4% unrealised loss per position |
| Max hold | 10 trading days (calendar days for crypto) |
| Take profit | 8% (configurable; set 0 to disable) |

Exit logic is not yet automated in the bot entry point (`bots/run_binance_bot.py`).
The current version scans for entries only. Exit management is the next step:
- On each scan, check open positions
- If any position is past max_hold_days or below stop_loss_pct, place a SELL order

---

## Backtesting (to do)

No backtest has been run on this strategy yet against historical crypto data.
Before enabling `--execute` with real testnet orders:

1. Pull 2 years of daily OHLCV for each symbol via:
   ```python
   adapter.get_ohlcv("BTCUSDT", "1d", limit=730)
   ```
2. Run the same `scan()` function in a loop over historical windows
3. Track simulated trades: entry on signal, exit on stop/target/max_hold
4. Compute Sharpe, Win Rate, Max Drawdown (same criteria as Saxo backtest)
5. Target: Sharpe > 1.0, WR > 55%, MaxDD < 20%

A `backtest_binance_reversion.py` script (analogous to the Saxo version) is
a recommended next step for the next agent session.

---

## What to watch during testnet

- **Signal frequency**: Crypto is more volatile than US equities so signals should
  fire more often (expect 1-3 per month per symbol in normal conditions).
- **Volume spikes**: Crypto volume data from Binance is base-currency volume
  (BTC, ETH, etc.), not USD-notional. The vol_ratio comparison is self-consistent
  but not directly comparable to the Saxo USD-notional volume filter.
- **Correlated moves**: BTC and ETH often dip together. If both trigger on the
  same day, both get slots — that is intentional (both individually passed all
  4 criteria). Monitor whether this concentrates risk.

---

## ARCHIVED — Mean Reversion v1 backtest verdict (2026-08-09)

**Status: archived. Do not use for live trading.**

Two grid searches were run against historical Binance kline data.

### Run 1 — 5 symbols, 2-year history, 2,916 combos

Universe: BTCUSDT, ETHUSDT, BNBUSDT, SOLUSDT, ADAUSDT  
RSI range: 30–38. Dip range: 4–7%.

Result: **0 robust combos (N≥20)**. The best results had N=4 (3 wins, 1 loss)
and reported Sharpe 1.26 / WR 75% — pure noise at that sample size, correctly
flagged as overfit risk and discarded.

### Run 2 — 10 symbols, max history (~3,280 bars BTC), 8,505 combos

Universe: above + XRPUSDT, DOGEUSDT, AVAXUSDT, DOTUSDT, LINKUSDT  
RSI range: 30–45. Dip range: 2–6%.

Result: **0 robust combos (N≥20)**. The only combos to reach N≥20 were at the
loosest RSI settings (RSI<35) and returned Sharpe 0.45, WR 38%. That is a
net-losing strategy — below 50% win rate means the exits cannot overcome the
losses even with tight stops.

### Root cause

The four-condition gate (RSI + dip + volume spike + EMA-200) is simultaneously
too rare to fire enough times to be measurable, and when it does fire the
signal quality is insufficient. Crypto's trend persistence means sharp dips
with volume spikes often continue falling rather than reverting.

The strategy has no viable parameter region in the tested space. Parameter
tweaks cannot fix a structural signal-quality problem.

### Next strategy

`strategies/binance/momentum_trend.py` — BTC Momentum Trend (trend-following,
not mean-reversion). Donchian channel breakout (close above N-day high) with
RSI momentum confirmation (50–70 zone). Exit on close below M-day trailing low.

---

## NOT READY — Momentum Trend v1 backtest verdict (2026-08-09)

**Status: not ready. Three checks failed. Do not wire into live bot.**

Strategy file: `strategies/binance/momentum_trend.py`  
Backtest: `backtest_binance_momentum.py`  
Pass criteria used: Sharpe≥1.0, PF≥2.0, MaxDD<40%, N≥20 (trend-following criteria —
WR excluded by design since breakout strategies structurally have low hit rates).

### Grid search (729 combos, 10 symbols, max history)

Best combo: `RSI[55,65] Break>25d Exit<7d Stop5% Hold60d`  
Full-sample: Sharpe=1.41, PF=1.93, WR=41%, DD=36.8%, CAGR=103%, N=174

Formally fails PF<2.0 (1.93 vs 2.0 target). Three robustness checks were run
before accepting any result, and all three raised concerns.

### Check 1 — Does `max_account_risk_pct` cap real-money drawdown?

**No.** The guard in `run_binance_bot.py::_execute_buys()` is an entry-time
exposure cap: it blocks new orders when open notional exceeds the threshold,
but does not close or constrain positions already held. Once positions enter
cleanly, they can lose whatever they lose. The 36–64% backtest drawdowns would
still materialise in live trading.

**Fix built:** `binance_bot/equity_stop.py` — portfolio-level drawdown stop,
checked every scan, halts new entries (and optionally flattens positions) if
equity falls more than a configurable threshold from peak. Wired into
`run_binance_bot.py`. Config key: `risk.equity_stop_drawdown_pct` (default 15%).

### Check 2 — Does PF survive removing the 3 best trades?

**No. It collapses from 1.93 to 1.29.**

The 3 removed trades:
- XRPUSDT Nov–Dec 2024: +298%, $+239k — the Trump-election XRP pump
- ETHUSDT Jul 2025: +41%, $+88k
- XRPUSDT Jul 2025: +39%, $+81k

Those 3 trades account for $408k of $592k total profit (69% of gains in 3/174
trades). The strategy is structurally dependent on rare outlier events that are
not repeatable signals — the XRP pump was a one-off political catalyst, not a
breakout pattern. Ex-top-3 PF=1.29 is below even the 1.5 threshold for "weak
edge," let alone the 2.0 target.

### Check 3 — Is the edge spread across years?

**No. Concentrated in 2021.**

| Year | Return | MaxDD | N |
|------|--------|-------|---|
| 2020 | +60%   | 20%   | 5 |
| **2021** | **+1009%** | 21% | 29 |
| 2022 | -20%   | 26%   | 29 |
| 2023 | +47%   | 19%   | 30 |
| 2024 | +170%  | 20%   | 32 |
| 2025 | +37%   | 17%   | 25 |
| 2026 | -22%   | 27%   | 24 |

The 2021 +1009% single year is what makes CAGR=103% look compelling. Without
it the strategy earns 40–170% in bull years and loses 20% in bear years — an
undifferentiated crypto-beta trade, not an exploitable alpha signal.

### What to try in v2

Before building a new strategy version, the portfolio equity stop must be
validated in testnet first (see above). Candidate improvements for v2:

- **Per-symbol position sizing based on volatility** (ATR) — would have reduced
  XRP position to a level where the outlier trade doesn't dominate results.
- **Universe filtering** — ADAUSDT and AVAXUSDT had 18–20% win rates and
  dragged overall PF below 2.0. Excluding low-quality symbols may help.
- **Entry confirmation** — require 2 consecutive closes above the breakout
  level to reduce false breakouts. Would lower N but raise WR.

Do not start on v2 until the equity stop has run at least one full testnet
session without error.

---

## NOT READY — Cross-Sectional Momentum Rotation v1 backtest verdict (2026-08-09)

**Status: not ready. 0 of 72 combos passed any criterion. Do not wire into live bot.**

Strategy file: `strategies/binance/` (not created — strategy archived before implementation)
Backtest: `backtest_binance_rotation.py`
Pass criteria used: Sharpe≥1.0, PF≥2.0, MaxDD<40%, N≥20, best-single-trade≤15% of profit, best-3-trades≤35% of profit.

### Grid search (72 combos, 10 symbols, April 2021 – Aug 2026)

Grid: Lookback [30/60/90d] × TrendMA [100/200d] × K [2/3] × Rebal [7/14d] × Stop [8/12/15%]

Result: **0 robust combos. 0 outlier-risk combos. 0 overfit-risk combos.** Nothing came close.

Best combo by Sharpe: `Lb=60d MA=200d K=2 Rb=7d Stop=15%`  
Full-sample (net of 0.1%/side fees): Sharpe=0.68, PF=1.33, WR=43%, DD=82.6%, CAGR=40%, N=122

That is a clean fail on every single criterion simultaneously. The best Sharpe in the
entire grid is 0.68 against a 1.0 bar. The best PF is 1.33 against a 2.0 bar. The best
MaxDD is 68.1% against a 40% bar. No parameter region — including the loosest stop,
widest SMA, and smallest K — came close to the threshold on all criteria together.

### Year-by-year (best combo)

| Year | Return  | MaxDD | N  |
|------|---------|-------|----|
| 2021 | +198.6% | 66.1% | 25 |
| 2022 | -74.7%  | 75.3% | 15 |
| 2023 | +292.8% | 35.2% | 26 |
| 2024 | +89.1%  | 40.1% | 31 |
| 2025 | -12.4%  | 44.5% | 24 |
| 2026 | -3.8%   |  6.9% |  1 |

The 2022 bear year (-74.7%) is structural: the portfolio was equally split across 2 crypto
assets during an asset-class-wide collapse. Rotating to the "least-bad" two symbols when
the entire universe is falling just means holding two different falling knives. The
SMA-200 filter did not protect because crypto moved faster than the 200-day lag —
positions were opened during the SMA transition and then fell through stops.

### 2022-onward out-of-sample test

Re-running only from 2022-01-01: Sharpe=0.44, PF=1.22, MaxDD=78.0%, CAGR=15%.

**The strategy does not survive without the 2021 bull run.** Excluding that year, the
remaining 4.6 years produce well below the Sharpe and PF thresholds. This is a worse
result than Momentum Trend v1, which at least had Sharpe=1.41 on full-sample even if
the edge collapsed on the ex-top-3 check.

### Outlier concentration (best combo)

Best single trade: 21.6% of total profit (ETHUSDT Jul–Sep 2025, +72% in 71 days)  
Best 3 trades: 45.6% of total profit

Exceeds both concentration limits even on the best combo in the grid.

### Root cause

The strategy assumption is that cross-sectional momentum rotation works because some
assets in the universe are always going up and the ranking correctly selects them.
This holds in diversified multi-asset universes (equities across sectors, or equities
plus bonds plus commodities). It breaks when the universe is a single asset class
(crypto) and that class enters a coordinated bear market — which it did in 2022 and
again partially in 2025-2026.

Three specific structural problems:

1. **Correlated drawdowns.** All 10 symbols are USDT-quoted crypto with BTC correlation
   typically 0.6–0.9. In a bear market they fall together. The trend filter (SMA-200)
   uses a 200-day lag, which is too slow to react to a fast reversal. The filter kept
   the strategy invested through most of 2022's decline.

2. **Equal-weight amplifies concentration.** K=2 means 50%/50% in two assets. When the
   top-2 by momentum are both in drawdown, the portfolio draws down as fast as the
   assets. There is no defensive leg.

3. **Fees on weekly churn.** Rebalancing weekly on 10 symbols at 0.1%/side means ~20
   round-trips per year even on a quiet portfolio. Fee drag at Sharpe=0.68 gross would
   be meaningful, though it's already included in these numbers since fees were applied
   to every simulated trade.

### What this strategy needs that wasn't built here

- A **defensive allocation** (cash or stablecoins) when fewer than K symbols pass the
  trend filter, and the ability to hold 0 positions rather than always being invested in
  the least-bad option during a bear market. This was already in the spec ("hold fewer
  positions or go to cash if fewer than K pass the trend filter") but the core problem
  persists: the SMA-200 filter is too slow to get to cash before large drawdowns develop.
- A **faster defensive gate** — e.g. exit the whole portfolio if BTC itself is below its
  50-day SMA, as a market-regime filter. Would miss some early-bull gains but would have
  largely avoided 2022.
- A **different universe** — mixing crypto with non-correlated assets (e.g. gold, equity
  index ETFs if tradeable on Binance) so the rotation always has a non-crypto leg when
  the whole crypto market falls. Not available cleanly on Binance USDT pairs without
  synthetic exposure.

Do not build a v2 of this strategy without addressing the correlated-drawdown problem
structurally. Tweaking lookback, K, or stop-loss within the same architecture will not
fix a 74.7% bear-year that applies uniformly across the whole universe.

---

## NOT READY — Rotation v2 + Momentum v2 backtest verdict (2026-08-09)

Backtest scripts: `backtest_binance_rotation_v2.py`, `backtest_binance_momentum_v2.py`

BTC 50d SMA regime gate added to both strategies. Gate logic: at bar[i] close, if
BTC close <= BTC 50d SMA → regime=BEAR. Rotation: sets target portfolio to empty
(all positions exit at bar[i+1] open). Momentum: skips entry scan (existing positions
exit naturally via stop/donchian/maxhold). Gate uses only data known at bar[i] close;
execution at bar[i+1] open. Zero lookahead confirmed by code audit (0 bear-regime
entries found in both simulations).

IS/OOS split: IS = backtest start → 2023-12-31. OOS = 2024-01-01 → 2026-08-09.
Gate threshold (50d) specified a priori from regime analysis; NOT fitted to IS data.

### Rotation v2 — NOT READY

Grid: 72 combos (Lookback×TrendMA×K×Rebal×Stop). Result: 0 of 72 passed.

Best combo by Sharpe: `Lb=90d MA=100d K=2 Rb=7d Stop=8%`

| Period       | Sharpe | PF   | MaxDD | CAGR | N  | Verdict             |
|--------------|--------|------|-------|------|----|---------------------|
| Full-sample  | 1.06   | 1.58 | 50.7% | 66%  | 95 | FAIL (PF, DD, conc) |
| IS 2021-2023 | 1.44   | 2.14 | 31.9% | 100% | 43 | PASS                |
| OOS 2024-26  | 0.42   | 1.47 | 50.7% | 14%  | 52 | FAIL (Sh, PF, DD)   |

Gate effect on 2022: Year return improved from -74.7% (v1) to -16.4% (v2) — gate
clearly works for the correlated bear market that broke v1. But 2024-2026 OOS
performance degrades severely (Sharpe 1.44 IS → 0.42 OOS). The IS/OOS improvement
does NOT hold up: IS passes but OOS fails.

Root cause of OOS failure: 2024 bull market with frequent rebalances produced 50.7%
MaxDD (the 2024 year had +70% return but peak drawdown of 50.7%). The rotation
architecture is sensitive to whipsaw at weekly rebalance frequency in high-volatility
bull phases. The regime gate only solves the bear-market problem, not the bull-market
volatility problem.

**Rotation v3 would need:** Longer rebalance interval in high-volatility regimes, or
a volatility-scaled position size, or an intra-bull drawdown brake. Not worth pursuing
until Momentum v2 is resolved first.

### Momentum v2 — BORDERLINE (1 trade from passing)

Grid: 729 combos. 49 combos pass Sharpe/PF/DD/N but fail concentration caps.
0 combos pass all 6 criteria. Best combo by Sharpe: `RSI[45,65] Break>25d Exit<7d Stop5% Hold60d`

| Period       | Sharpe | PF   | MaxDD | CAGR  | N   | Verdict                      |
|--------------|--------|------|-------|-------|-----|------------------------------|
| Full-sample  | 1.45   | 2.41 | 31.3% | 105%  | 151 | FAIL (concentration only)    |
| IS 2021-2023 | 1.59   | 2.37 | 31.3% | 144%  | 83  | PASS (b1=9.8%, b3=27.7%)     |
| OOS 2024-26  | 1.18   | 2.43 | 28.9% | 58%   | 68  | PASS on Sh+PF; conc fails OOS |

OOS verdict: HOLDS UP — both IS and OOS clear Sharpe≥1.0 and PF≥2.0. MaxDD 31.3%
(full) / 28.9% (OOS) — both well under 40% limit. IS/OOS OOS degradation is moderate
and acceptable (Sh 1.59 → 1.18, PF 2.37 → 2.43).

Gate improvement vs v1: MaxDD 36.8%→31.3% (now under 40%); PF 1.93→2.41 (now over 2.0);
Sharpe 1.41→1.45. The gate fixed both v1 blockers (PF and DD).

Concentration blocker:
- Full sample: best1=20.7%, best3=35.6% — barely over limits (15%, 35%)
- IS 2020-2023: best1=9.8%, best3=27.7% — clean, both under limits
- OOS 2024-2026: best1=29.4%, best3=50.4% — driven entirely by 1 trade:
  XRPUSDT Nov 8 – Dec 9, 2024: +298%, $+217k (Trump-election XRP pump)
  This single trade = 20.7% of total gross profit over 6 years.

Ex-top-3 PF collapses to 1.55 (same structural problem as v1). The strategy has
positive expectancy in aggregate but structural dependence on rare catalyst events.

Year-by-year:
| Year | Return   | MaxDD | N  |
|------|----------|-------|----|
| 2020 | +51.8%   | 22.8% | 4  |
| 2021 | +1030.0% | 21.3% | 28 |
| 2022 | -24.5%   | 29.4% | 23 |
| 2023 | +46.0%   | 18.1% | 28 |
| 2024 | +172.2%  | 18.9% | 31 |
| 2025 | +48.9%   | 11.5% | 17 |
| 2026 | -18.2%   | 23.5% | 20 |

2022 bear year: -24.5% (v1 was -20% — similar, gate mostly helps by avoiding entries
rather than stopping losses on positions already held). MaxDD in 2022: 29.4% — acceptable.

Exit reasons: Donchian exit 107 trades WR=57% P&L=+$835k; hard stop 42 trades WR=0%
P&L=-$258k. The strategy wins through Donchian exits and loses through stops.

**What is needed to pass:** Fix the concentration cap without changing the signal.
Options, in order of preference:
1. ATR-based position sizing: size each position inversely proportional to its 14d
   ATR. XRP's ATR in Nov 2024 was elevated; smaller notional → smaller $ impact.
   Reduces XRPUSDT's outsized contribution without changing entry/exit logic.
2. Hard per-position notional cap: never let any single position exceed X% of portfolio.
   Simple to implement; crude but effective. Cap would need to be ~12% to bring b1 under 15%.
3. Universe modification: remove XRPUSDT (it drives 3 of the 5 largest trades including
   the $217k outlier). Would likely push b1 under 15% at cost of N reduction.

Do not wire Momentum v2 into live bot until concentration is addressed by one of the
above methods. The signal quality is there; the position sizing is not.

### Momentum v3 — NOT READY (cap fixes concentration, exposes weak OOS edge)

Backtest: `backtest_binance_momentum_v3.py`

Change from v2: single line — `slot_size = min(current_total / MAX_POSITIONS, current_total * 0.12)`.
All other logic (BTC 50d SMA gate, exits, grid, pass criteria) is unchanged.

Effect: at entry, no position exceeds 12% of portfolio equity. With 3 slots, max
deployment is 36% of portfolio at any time. Verified: avg entry cost = 12.0% of equity,
max observed = 12.3% (rounding artefact on first bar).

Grid result: **438 of 729 combos pass all 6 criteria.** Concentration caps are resolved.

Best combo: `RSI[55,70] Break>25d Exit<15d Stop10% Hold60d`

| Period       | Sharpe | PF   | MaxDD | CAGR | N   | b1    | b3    | ex3PF | Verdict       |
|--------------|--------|------|-------|------|-----|-------|-------|-------|---------------|
| Full-sample  | 1.39   | 2.70 | 19.5% | 44%  | 123 | 10.4% | 27.8% | 1.95  | **PASS**      |
| IS 2021-2023 | 1.87   | 4.80 | 19.5% | 80%  | 56  | 17.5% | 40.9% | 2.83  | PASS (std)    |
| OOS 2024-26  | 0.40   | 1.73 | 17.7% | 7%   | 67  | 23.9% | 45.6% | 0.94  | **FAIL**      |

OOS verdict: **DEGRADES.** IS clears all standard criteria; OOS fails Sharpe and PF.

Year-by-year (with 12% cap):
| Year | Return   | MaxDD | N  |
|------|----------|-------|----|
| 2020 | +21.3%   | 11.6% | 2  |
| 2021 | +320.9%  | 12.5% | 20 |
| 2022 | -13.4%   | 14.0% | 14 |
| 2023 | +56.1%   |  7.8% | 20 |
| 2024 | +20.2%   | 17.7% | 31 |
| 2025 | +6.2%    |  6.5% | 20 |
| 2026 | -7.3%    | 10.1% | 16 |

Ex-top-3 stress test: PF drops from 2.70 to 1.95 — "WEAKENS but acceptable" (above 1.5).
The top 3 trades (DOGE +148%, SOL +202%, ADA +605%) each account for 7-10% of gross profit —
well within concentration caps. No single outlier dominates by construction.

Root cause of OOS degradation: The 12% cap removed the amplified contribution of the
Nov 2024 XRP +298% trade ($217k in v2 → ~$24k in v3 due to cap). The v2 OOS Sharpe of
1.18 was primarily driven by that one political-catalyst trade. With it capped, the genuine
OOS signal (Donchian breakouts in 2024-2026) contributes Sharpe 0.40 — a real but weak edge.

What this means: the strategy has genuine IS edge (trending crypto bull markets respond
well to Donchian breakouts). But the OOS period (2024-2026) — a higher-volatility,
more sideways market punctuated by political catalysts — doesn't reward the signal strongly
enough to clear the thresholds at 12% position size.

Next option (if pursued): ATR-based position sizing (v4). Size each position inversely
proportional to its 14d ATR rather than a flat cap. This would give larger positions in
low-volatility trending markets (where the signal is strongest) and smaller positions
during volatile periods — an adaptive version of the cap that may preserve more upside
in IS while still controlling concentration. The 12% flat cap is too conservative across
all market regimes.

**Do not wire Momentum v3 into the live bot.** Full-sample passes but IS/OOS discipline
shows the OOS edge is insufficient after removing outlier amplification.
