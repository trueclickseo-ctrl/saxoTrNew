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
