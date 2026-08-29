# Forex Strategies — Per-Strategy Documents

One document per strategy, for review. Each describes the strategy **exactly as
it runs today** — parameters, entry/exit rules, sizing, where it runs (SIM /
LIVE), and any known stale docstring notes. **None of these describe changes** —
the strategies run as-is.

The 20 strategies are the keys of `STRATEGIES` in [`forex/runner.py`](../forex/runner.py).
The `advanced_*` / `*_master` ones are SIM-only A/B experiments running in
parallel with their untouched originals — none of them can place a real-money order.

## Swing strategies (daily bars, SIM scan)

| Strategy | Doc | Type | Runs on |
|---|---|---|---|
| `ema` | [forex_ema_strategy.md](forex_ema_strategy.md) | EMA(5/30) crossover + ADX | SIM |
| `advanced_ema` | [forex_advanced_ema_strategy.md](forex_advanced_ema_strategy.md) | `ema` + EMA50 confirm + rising-ADX + vol-percentile + recent-cross-only (A/B vs `ema`) | SIM |
| `rsi` | [forex_rsi_strategy.md](forex_rsi_strategy.md) | RSI(2) pullback (mean-reversion) | SIM **+ LIVE_EUR (real money)** |
| `advanced_rsi_master` | [forex_advanced_rsi_master_strategy.md](forex_advanced_rsi_master_strategy.md) | `rsi` + EMA50/200 alignment + slope + distance + reversal-confirm (A/B vs `rsi`) | SIM |
| `donchian` | [forex_donchian_strategy.md](forex_donchian_strategy.md) | 30-day channel breakout (strict) | SIM |
| `donchian_quality` | [forex_donchian_quality_strategy.md](forex_donchian_quality_strategy.md) | `donchian` + breakout-quality filters (A/B) | SIM |
| `bb` | [forex_bb_strategy.md](forex_bb_strategy.md) | Bollinger(20,2) + RSI reversion | SIM **+ SEK LIVE** (task Disabled) |
| `advanced_bb_master` | [forex_advanced_bb_master_strategy.md](forex_advanced_bb_master_strategy.md) | `bb` + ADX ceiling + band-width + excursion + reversal-confirm (A/B vs `bb`) | SIM |
| `pullback` | [forex_pullback_strategy.md](forex_pullback_strategy.md) | trend pullback to EMA(20) | SIM |
| `advanced_pullback_master` | [forex_advanced_pullback_master_strategy.md](forex_advanced_pullback_master_strategy.md) | `pullback` + EMA5>20>50 structure + vol-percentile + DI + same-day bounce (A/B vs `pullback`) | SIM |
| `supertrend` | [forex_supertrend_strategy.md](forex_supertrend_strategy.md) | SuperTrend(10,3) + EMA(200) | SIM |
| `zscore` | [forex_zscore_strategy.md](forex_zscore_strategy.md) | z-score(20) ±2σ reversion | SIM |
| `ml` | [forex_ml_strategy.md](forex_ml_strategy.md) | per-pair logistic regression, 7 features | SIM |
| `advanced_ml` | [forex_advanced_ml_strategy.md](forex_advanced_ml_strategy.md) | regularized logistic reg + regime + trend filters (A/B vs `ml`) | SIM |
| `cnn_lstm` | [forex_cnn_lstm_strategy.md](forex_cnn_lstm_strategy.md) | CNN + BiLSTM + attention (⚠️ barely fires) | SIM |
| `advanced_cnn_lstm_master` | [forex_advanced_cnn_lstm_master_strategy.md](forex_advanced_cnn_lstm_master_strategy.md) | stricter selection wrapper on the **same** CNN-LSTM model, no retrain (A/B vs `cnn_lstm`) | SIM |

## Gap strategies (session windows, need live prices)

| Strategy | Doc | Type | Runs on |
|---|---|---|---|
| `gap` | [forex_gap_strategy.md](forex_gap_strategy.md) | weekly + session gap-fade | SIM |
| `gap_weekend` | [forex_gapfill_weekend_strategy.md](forex_gapfill_weekend_strategy.md) | rebuilt gap-fade, **weekly window only** right now (A/B) | SIM |

## Day-trading book (H1 bars, own capital, dedicated LBO tasks — Mon–Fri)

| Strategy | Doc | Type | Runs on |
|---|---|---|---|
| `london_breakout` | [forex_london_breakout_strategy.md](forex_london_breakout_strategy.md) | Asian/London-morning range breakout | SIM (LBO tasks) |
| `london_breakout_v2` | [forex_london_breakout_v2_strategy.md](forex_london_breakout_v2_strategy.md) | reworked breakout, 9 fixes, 0.5% risk / 4-cap (A/B) | SIM (LBO tasks) |

## Cross-cutting

- **Real-money LIVE accounts + all LIVE-only gates**: [forex_live_account.md](forex_live_account.md)
- **Full strategy comparison table + audits**: [forex_strategies.md](forex_strategies.md)
- **Currency-exposure cap, cost gate, weekend gate, heat cap** — applied by
  `forex/runner.py`, not the strategy files; documented in `forex_live_account.md`.

## Notes that apply to almost every swing strategy

- **`RISK_PCT = 0.0025` (0.25%)** — most module docstrings still say "1%"; that
  is stale (cut 1% → 0.5% on 2026-08-22, → 0.25% on 2026-08-24). LIVE overrides
  to 0.75% via `LIVE_RISK_PCT_OVERRIDE`.
- **`MAX_POSITIONS = 4`** in a module is usually **not enforced** — the runner's
  real cap is `SLOTS_PER_STRATEGY[name]`, which is `_SWING_SLOTS` (= 184) for
  most. The exceptions that *do* enforce a real 4-cap: `donchian_quality`,
  `london_breakout_v2`.
- **Win-rate figures in docstrings are estimates**, not measured in this
  codebase — judge from the live SIM dashboard's per-strategy P&L.
- Signals act on `iloc[-1]` = the **current, still-forming daily bar** (no
  look-ahead, but a signal can fire intraday and later reverse).
