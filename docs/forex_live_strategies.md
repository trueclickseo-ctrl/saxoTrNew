# ATOS Forex LIVE — Strategy Playbook

**Account**: real-money Saxo LIVE, sub-account `1070996INET`, SEK-denominated, 6,000 SEK opening balance.
**Strategies**: exactly 3 of the 11 available in `forex/` — hard-restricted in code (`forex/runner.py`'s `LIVE_ALLOWED_STRATEGIES`), not just by convention. Any other strategy passed under `--account live` is a CLI hard-error.
**Universe**: exactly the 34 `CORE_SYMBOLS` pairs (no exotic) — hard-filtered before a signal can ever fire.
**Why these 3, why these 34 pairs**: the SIM dashboard's Core-vs-Exotic split showed core pairs dramatically outperforming exotic overall (profit factor 8.19 vs 0.01, win rate 34.6% vs 3.1%). Of SIM's 11 strategies, donchian/ema/rsi had the strongest, most consistent track record specifically on core pairs. This account is the real-money expression of that finding.

---

## 1. Donchian Breakout (`donchian`)

**Type**: trend-following breakout, ~30-day channel.

**Entry**:
- Close breaks above the prior 30-day high (long) or below the prior 30-day low (short)
- AND price is on the trend side of EMA(200) (above for longs, below for shorts)
- AND ADX(14) ≥ 25 (confirmed trend strength — filters out ranging/choppy markets)

**Exit** (first condition met wins):
1. 15-day opposite-channel break (close breaks the *other* side of a tighter 15-day channel)
2. Hard stop: 2.0× ATR from entry
3. Time-stop: 30 calendar days held

**Risk**: 0.25% of equity per trade (~15 SEK on 6,000 SEK). Take-profit: 2:1 reward:risk, resting Limit order at entry ± 2× stop distance.

**Track record so far (core-34, all-time, small sample)**: 2 closed trades, 2W/0L, 100% win rate — the largest realized gains of the 3 live strategies.

---

## 2. EMA Trend (`ema`)

**Type**: trend-following moving-average crossover.

**Entry**:
- EMA(5) crosses EMA(30) — checked over the last 15 bars (not just the exact crossover session), so an already-established trend still qualifies
- AND ADX(14) ≥ 25

**Exit** (first condition met wins):
1. Opposite EMA(5)/EMA(30) crossover
2. Hard stop: 1.5× ATR from entry
3. Time-stop: 45 calendar days held (the longest hold window of the 3 — a genuine trend-following strategy)

**Risk**: 0.25% of equity per trade. Take-profit: 2:1 reward:risk.

**Track record so far (core-34, all-time, small sample)**: 2 closed trades, 2W/0L, 100% win rate.

---

## 3. RSI Pullback (`rsi`)

**Type**: mean-reversion *within* a trend — not a reversal call.

**Naming note**: dashboards label this strategy "RSI Pullback." A completely separate strategy, `pullback` ("EMA Pullback ★" — EMA(20)-in-EMA(50)), also exists in the codebase and shares the word "Pullback" by coincidence. Only `rsi` is live; the standalone `pullback` strategy is SIM-only and never trades on this account.

**Entry**:
- RSI(2) ≤ 10 (oversold) while price is in an EMA(200) uptrend → long
- RSI(2) ≥ 90 (overbought) while price is in an EMA(200) downtrend → short
- No ADX filter — this strategy deliberately trades short-term dips/rallies within an established trend, not breakouts

**Exit** (first condition met wins):
1. RSI reverts past its own exit threshold ("rsi_recovery")
2. Hard stop: 1.5× ATR from entry
3. Time-stop: **12 calendar days** — much shorter than the other two; RSI(2) is a fast-turnover signal by design

**Risk**: 0.25% of equity per trade. Take-profit: 2:1 reward:risk.

**Track record so far (core-34, all-time, small sample)**: 3 closed trades, 3W/0L, 100% win rate — smaller individual gains than Donchian/EMA, but the fastest turnover.

---

## Shared mechanics across all 3

| | Value | Applies to |
|---|---|---|
| Risk per trade | 0.25% of current equity | All 3, identical |
| Position sizing | `risk_amount / (ATR_STOP_MULT × ATR)`, floored at 1,000 units | All 3 |
| Take-profit ratio | Fixed 2:1 reward:risk | All 3 |
| Stop-loss placement | Genuine Saxo-side protective stop, placed atomically with entry | All 3 |
| Signal basis | Daily bars only — no intraday/tick signals | All 3 |
| Portfolio heat cap | 6% of equity in combined open risk (~360 SEK) | Shared across all 3 |
| Margin cap | Never uses more than 50% of available broker margin | Shared across all 3 |

**"Best strategy" — honestly, still too early to call.** All three show 100% win rate so far, but on only 2-3 closed trades each. Donchian and EMA have the largest individual gains; RSI has the fastest turnover (12-day max hold vs 30/45 days) but smaller wins. Revisit this once each strategy has accumulated a real sample (10+ closed trades).

---

## What is deliberately NOT live

The other 8 SIM strategies (`bb`, `pullback`, `gap`, `supertrend`, `zscore`, `ml`, `cnn_lstm`, `london_breakout`) continue running on SIM only, across all 117 pairs (34 core + 83 exotic), for continued evaluation. None of them are hard-blocked from ever going live later — but doing so requires the same explicit-decision process this account itself went through (a real track record on core pairs specifically, then a deliberate user choice), not an automatic promotion.
