# IBKR Stocks Module

Paper-trading sleeve running all three ATOS SIM strategies via Interactive Brokers IB Gateway.
Fully isolated from Saxo and Avanza — Yahoo Finance for signals, IBKR for execution.

**Safety constraint:** Claude never runs `--execute` or places IBKR trades.
Every trade requires an interactive `y` confirmation at the terminal.

---

## Strategies

| Strategy | Signal source | Budget | Slots | Stop | Schedule |
|---|---|---|---|---|---|
| **US Blend** | `atos.us_momentum.compute_targets()` | $50,000 | 10 max | 8% trail | Fortnightly (Thu 16:00 PKT) |
| **US Reversion** | `atos.us_reversion.scan()` | $50,000 | 15 max | 4% hard | Daily 16:00 PKT (07:00 ET, pre-open) |
| **Intraday Reversion** | `atos.intraday_reversion.intraday_scan()` | shared with reversion | 15 max | 4% hard | 5×/day US session (19:00–00:30 PKT) |

Per-slot sizing (reversion): `$50,000 / 15 = $3,333/slot`
Per-slot sizing (blend): `$50,000 × 0.95 buffer / 10 = $4,750/slot`

All strategy logic is the same validated code that runs the ATOS SIM books — imported
directly from the pure `atos/` modules which have zero Saxo/IBKR I/O.

---

## Architecture

### Signal flow

```
Yahoo Finance (yf.download, 424 tickers, ~30s)
    └─ 8-hour disk cache: data/ibkr_price_cache.pkl
           └─ ibkr_signals.py
                  ├─ blend_targets()           → ibkr_executor.run_rebalance()
                  ├─ reversion_candidates()    → ibkr_executor.run_reversion_entries()
                  ├─ intraday_candidates()     → ibkr_executor.run_reversion_entries(intraday=True)
                  └─ reversion_exit_indicators() → ibkr_executor.run_reversion_exits()
```

**Signals are pre-generated BEFORE connecting to IB Gateway.** This limits the
IBKR connection to ~3-5 seconds, preventing "client id already in use" (Error 326)
on rapid back-to-back runs.

### Per-strategy client IDs

| clientId | Used for |
|---|---|
| 10 | blend |
| 11 | reversion |
| 12 | intraday |
| 13 | trail stops |
| 14 | info / positions |
| 15 | dashboard |

Override with `--client-id N` if Gateway still holds a slot after a crash.

---

## Files

| File | Purpose |
|---|---|
| `ibkr_module/ibkr_signals.py` | Yahoo Finance signal generators (blend_targets, reversion_candidates, intraday_candidates, reversion_exit_indicators) with 8-hour disk cache |
| `ibkr_module/ibkr_client.py` | ib_insync wrapper — connect, get_positions, get_prices, place_market_order, place_stop_order, confirm_fill, cancel_order |
| `ibkr_module/ibkr_state.py` | SQLite ledger `data/ibkr_stocks.db`; strategy column per trade; record_order / mark_filled / mark_cancelled / update_stop / get_open_positions / count_open |
| `ibkr_module/ibkr_executor.py` | run_rebalance, run_reversion_entries, run_reversion_exits, trail_stops |
| `ibkr_module/config/ibkr_config.json` | paper=true, port_paper=4002, client_ids block, strategies.blend + strategies.reversion |
| `run_ibkr_stocks.py` | CLI entry point |

---

## CLI usage

```bash
# Dry-run scans (no orders)
python run_ibkr_stocks.py                                  # blend signal + plan
python run_ibkr_stocks.py --strategy reversion             # reversion entry scan
python run_ibkr_stocks.py --strategy reversion --exits     # check open positions for exit
python run_ibkr_stocks.py --strategy intraday              # intraday scan (US hours only)

# Execute (interactive y/N per order)
python run_ibkr_stocks.py --execute                        # blend rebalance
python run_ibkr_stocks.py --strategy reversion --execute   # reversion entries

# Utilities
python run_ibkr_stocks.py --positions                      # show IBKR positions
python run_ibkr_stocks.py --info                           # account summary
python run_ibkr_stocks.py --trail-stops                    # dry-run trail stop check
python run_ibkr_stocks.py --trail-stops --execute          # ratchet stops
python run_ibkr_stocks.py --dashboard                      # live dashboard (30s refresh)

# Client ID override (if Gateway holds slot after crash)
python run_ibkr_stocks.py --strategy blend --client-id 16
```

---

## Scheduled tasks

All tasks are dry-run — they log the signal but do not place orders.

| Task name | Schedule (PKT) | Bat file | Log |
|---|---|---|---|
| ATOS IBKR Blend Rebalance | Every 2 weeks, Thu 16:00 | `run_ibkr_blend_rebalance.bat` | `data/ibkr_blend_rebalance.log` |
| ATOS IBKR Reversion Entries | Daily 16:00 (07:00 ET) | `run_ibkr_reversion_entries.bat` | `data/ibkr_reversion_entries.log` |
| ATOS IBKR Reversion Exits | Daily 09:00 (00:00 ET) | `run_ibkr_reversion_exits.bat` | `data/ibkr_reversion_exits.log` |
| ATOS IBKR Intraday Reversion | 19:00 / 20:30 / 22:00 / 23:30 / 00:30 | `run_ibkr_intraday.bat` | `data/ibkr_intraday.log` |
| ATOS IBKR Trail Stops | Daily 21:00 | `run_ibkr_trail_stops.bat` | `data/ibkr_trail_stops.log` |

### Register / re-register tasks

```powershell
powershell -ExecutionPolicy Bypass -File setup_scheduler_ibkr_blend.ps1
powershell -ExecutionPolicy Bypass -File setup_scheduler_ibkr_reversion.ps1
powershell -ExecutionPolicy Bypass -File setup_scheduler_ibkr_intraday.ps1
```

---

## Prerequisites

```bash
pip install ib_insync yfinance
```

- IB Gateway running on this machine (paper port 4002 / live port 4001)
- **Uncheck Read-Only API** in Gateway → Settings → API
- Paper account login uses the paper-trading username (separate from main login)
- Account ID auto-detected; or set `IBKR_ACCOUNT_ID` in `.env.ibkr`

---

## State

- `data/ibkr_stocks.db` — SQLite ledger (gitignored, machine-local)
- `data/ibkr_price_cache.pkl` — Yahoo Finance 8-hour cache (gitignored)

---

## US Blend detail

Cross-sectional momentum rebalance (same logic as ATOS SIM / Avanza ISK):
- Universe: 424 US tickers (`atos.universe.US_TICKERS`)
- Offense: top 6 by 12-1 month return / volatility (risk-adjusted momentum)
- Defense: lowest 2 by volatility that are above EMA(200)
- Risk-off: if SPY below EMA(200), shift to full defense / cash
- Rebalance: every ~14 days (Thursday), log signal, confirm manually

## US Reversion detail

Mean reversion on daily bars:
- Entry: RSI(14) < 38, price > EMA(200), price ≥ 5% below SMA(20), volume ≥ 1.5× 20d avg
- Exit: RSI(14) > 60 OR price returns to SMA(20) OR 4% stop hit OR 10 trading days held
- 15 slots, $3,333/slot, hard 4% stop placed as GTC stop order at IBKR

## Intraday Reversion detail

Same exit logic; entry uses live 5-min yfinance bars:
- Bad-news filter: skip if recent headlines contain distress keywords
- Catastrophic-drop filter: skip if intraday move > 15%
- 5 scans per US session (09:30–16:00 ET = 18:30–01:00 PKT)
