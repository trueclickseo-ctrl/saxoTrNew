# IBKR Stocks Module

Paper-trading sleeve running five ATOS strategies via Interactive Brokers IB Gateway.
Fully isolated from Saxo and Avanza — Yahoo Finance for signals, IBKR for execution.

**Safety constraint:** Claude never runs `--execute` or places IBKR trades.
The `--auto` flag bypasses interactive prompts; it is hard-blocked on any live account.

---

## Strategies

| Strategy | DB key | Signal source | Budget | Slots | Stop | Schedule (PKT) |
|---|---|---|---|---|---|---|
| **Scorer Swing** | `scorer_swing` | `ibkr_scorer.run_scorer()` — top ROC/ADX/RS-vs-SPY | $50,000 | 8 max | 8% trail | Mon–Fri 19:00 |
| **Scorer Portfolio** | `scorer_portfolio` | `ibkr_scorer.run_scorer()` — SMA200 quality + trend | $50,000 | 10 max | 8% trail | Mon–Fri 19:00 |
| **US Blend** | `blend` | `atos.us_momentum.compute_targets()` | $50,000 | 10 max | 8% trail | Fortnightly (every 2 Thu, 19:00) |
| **US Reversion** | `reversion` | `atos.us_reversion.scan()` | $50,000 | 15 max | 4% hard | Daily 19:30 entries / 23:00 exits |
| **US Intraday Rev.** | `intraday` | `atos.intraday_reversion.intraday_scan()` | shared | 15 max | 4% hard | 5×/day US session (19:00–00:30) |
| **US SMA Crossover** | `US SMA Crossover` | `atos.us_signals` — 50/200 SMA golden cross | $50,000 | 5 max | 8% trail | Daily 16:00 entries / 23:00 exits |
| **US RSI Reversal** | `US RSI Reversal` | `atos.us_signals` — RSI oversold + volume | $50,000 | 5 max | 8% trail | Daily 16:00 entries / 23:00 exits |
| **US Momentum** | `US Momentum` | `atos.us_signals` — 52-week high breakout | $50,000 | 5 max | 8% trail | Daily 16:00 entries / 23:00 exits |
| **US Ensemble** | `US Ensemble` | `atos.us_signals` — SMA + RSI + momentum triple | $50,000 | 5 max | 8% trail | Daily 16:00 entries / 23:00 exits |

Mirror schedule vs Saxo: Saxo LIVE scan 19:20 PKT → IBKR Scorer 19:00 PKT (20 min earlier).

---

## Universe

**File:** `config/atos_us_500_universe.csv` — **363 quality/growth tickers** (down from 482 raw)

Quality filter applied 2026-09-05 (two-pass):

| Pass | Method | Result |
|---|---|---|
| 1 — Curated sector exclusions | Known ETFs, crypto miners, gold miners, commodity energy, utilities, REITs, speculative biotech, legacy pharma, non-growth banks, consumer staples, legacy telecom, high-vol meme stocks | 482 → 394 |
| 2 — yfinance market-cap/sector screen | Sector= Utilities / Real Estate; market cap < $2B; price < $5 | 394 → 363 |

**Removed categories:** ETFs (GLD), crypto (CLSK, COIN), gold miners (NEM), energy (XOM, CVX, VLO, MPC), utilities (NEE, SO, DUK, EIX, CEG), REITs (AMT, EQIX, CBRE, IRM, GLPI, VICI, SPG + 17 more), speculative biotech (CRSP, GMAB, HIMS), legacy pharma (MRK, PFE, ABBV, JNJ), non-growth banks (WFC, BAC, C, USB, etc.), consumer staples (MO, PM, TAP, KDP), legacy telecom (T, VZ), meme/volatile (SMCI, HOOD), small-cap < $2B (LCID, GLOB, TRIP, DAVA).

**Backup:** `config/atos_us_500_universe.csv.bak_20260905`

Only `ibkr_module/ibkr_scorer.py` reads this file; `atos/universe.py` (Saxo/ATOS) is unchanged.

---

## Architecture

### Signal flow

```
Yahoo Finance (yf.download, 363 tickers, ~30s)
    └─ 8-hour disk cache: data/ibkr_price_cache.pkl
           └─ ibkr_signals.py  /  ibkr_scorer.py  /  scoring_engine.py
                  ├─ run_scorer()              → ibkr_executor.run_scorer_strategy()
                  │       (scorer_swing + scorer_portfolio — ranked by ROC/ADX/RS)
                  ├─ blend_targets()           → ibkr_executor.run_rebalance()
                  ├─ reversion_candidates()    → ibkr_executor.run_reversion_entries()
                  ├─ intraday_candidates()     → ibkr_executor.run_reversion_entries(intraday=True)
                  ├─ reversion_exit_indicators() → ibkr_executor.run_reversion_exits()
                  └─ us_signals_data()         → ibkr_executor.run_us_signals_entries/exits()
```

**Signals are pre-generated BEFORE connecting to IB Gateway.** This limits the
IBKR connection to ~3–5 seconds, preventing "client id already in use" (Error 326)
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
| 16 | scorer |
| 17 | signals entries |
| 18 | signals exits |

Override with `--client-id N` if Gateway still holds a slot after a crash.

---

## Files

| File | Purpose |
|---|---|
| `ibkr_module/ibkr_signals.py` | Yahoo Finance signal generators: blend_targets, reversion_candidates, intraday_candidates, reversion_exit_indicators, us_signals_data; 8-hour disk cache |
| `ibkr_module/ibkr_scorer.py` | Scorer strategy: reads universe CSV, fetches features, delegates to scoring_engine, returns ranked Swing + Portfolio candidate lists |
| `ibkr_module/scoring_engine.py` | Pure-Python momentum/quality scoring: ROC(63), ADX(14), RS-vs-SPY, SMA200 filter, trend quality composite |
| `ibkr_module/ibkr_client.py` | ib_insync wrapper — connect, get_positions, get_prices, place_market_order, place_stop_order, confirm_fill, cancel_order; all ib_insync loggers silenced at CRITICAL before connect |
| `ibkr_module/ibkr_state.py` | SQLite ledger `data/ibkr_stocks.db`; strategy column per trade; record_order / mark_filled / mark_cancelled / update_stop / get_open_positions / count_open |
| `ibkr_module/ibkr_executor.py` | run_rebalance (blend), run_scorer_strategy, run_reversion_entries/exits, run_us_signals_entries/exits, trail_stops; AI observation cards logged on every fill |
| `ibkr_module/config/ibkr_config.json` | paper=true, port_paper=4002, client_ids block, per-strategy budgets/slots |
| `ai/features/ibkr_stock_cards.py` | AI entry/exit observation cards for IBKR fills → `data/stock_observation_cards.jsonl` (same file Saxo SIM feeds) |
| `run_ibkr_stocks.py` | CLI entry point (`--strategy blend/reversion/intraday/scorer/signals/all`) |
| `ibkr_dashboard.py` | Live dashboard — open positions by strategy, IBKR live prices (Yahoo fallback), WR/PF stats section for closed trades |
| `scripts/ibkr_scan_signals.py` | Dry-run scanner + email for US Signals strategies |

---

## CLI usage

```bash
# Dry-run scans (no orders)
python run_ibkr_stocks.py                                  # blend signal + plan
python run_ibkr_stocks.py --strategy scorer                # scorer scan (swing + portfolio)
python run_ibkr_stocks.py --strategy reversion             # reversion entry scan
python run_ibkr_stocks.py --strategy reversion --exits     # check open positions for exit
python run_ibkr_stocks.py --strategy intraday              # intraday scan (US hours only)
python run_ibkr_stocks.py --strategy signals               # US Signals entries scan
python run_ibkr_stocks.py --strategy signals --exits       # US Signals exits scan
python run_ibkr_stocks.py --strategy all                   # run all strategies

# Execute (scheduled tasks use --auto; manual use prompts interactively)
python run_ibkr_stocks.py --strategy scorer --execute --auto
python run_ibkr_stocks.py --strategy blend --execute --auto
python run_ibkr_stocks.py --strategy reversion --execute --auto

# Dashboard
python ibkr_dashboard.py                                   # 10s refresh
python ibkr_dashboard.py --once                            # print once
python ai_dashboard.py --once                              # AI twin + IBKR paper section

# Utilities
python run_ibkr_stocks.py --positions                      # show IBKR positions
python run_ibkr_stocks.py --info                           # account summary
python run_ibkr_stocks.py --trail-stops                    # dry-run trail stop check
python run_ibkr_stocks.py --trail-stops --execute          # ratchet stops

# Client ID override (if Gateway holds slot after crash)
python run_ibkr_stocks.py --strategy blend --client-id 16
```

---

## Scheduled tasks (Windows Task Scheduler)

| Task name | Schedule (PKT) | Bat file | Log |
|---|---|---|---|
| ATOS IBKR Scorer | Mon–Fri 19:00 | `run_ibkr_scorer.bat` | `data/ibkr_scorer.log` |
| ATOS IBKR Blend Rebalance | Every 2 Thu 19:00 | `run_ibkr_blend_rebalance.bat` | `data/ibkr_blend_rebalance.log` |
| ATOS IBKR Reversion Entries | Daily 19:30 | `run_ibkr_reversion_entries.bat` | `data/ibkr_reversion_entries.log` |
| ATOS IBKR Reversion Exits | Daily 23:00 | `run_ibkr_reversion_exits.bat` | `data/ibkr_reversion_exits.log` |
| ATOS IBKR Intraday Reversion | 19:00/20:30/22:00/23:30/00:30 | `run_ibkr_intraday.bat` | `data/ibkr_intraday.log` |
| ATOS IBKR Signals Entries | Daily 16:00 | `run_ibkr_signals.bat` | `data/ibkr_signals.log` |
| ATOS IBKR Signals Exits | Daily 23:00 | `run_ibkr_signals.bat --exits` | `data/ibkr_signals_exits.log` |
| ATOS IBKR Trail Stops | Mon–Fri 21:00 | `run_ibkr_trail_stops.bat` | `data/ibkr_trail_stops.log` |

---

## Prerequisites

```bash
pip install ib_insync yfinance pandas ta-lib
```

- IB Gateway running on this machine (paper port 4002 / live port 4001)
- **Uncheck Read-Only API** in Gateway → Settings → API
- Paper account: DUR952126 (SEK-denominated)
- Account ID auto-detected; or set `IBKR_ACCOUNT_ID` in `.env.ibkr`

---

## State

- `data/ibkr_stocks.db` — SQLite ledger (gitignored, machine-local)
  - `trades` table: order_id, symbol, side, qty, limit_price, fill_price, stop_price, stop_order_id, trailing_high, status, created_at, filled_at, **strategy**
- `data/ibkr_price_cache.pkl` — Yahoo Finance 8-hour cache (gitignored)

---

## AI observation layer

Every confirmed fill (entry + exit) is logged to `data/stock_observation_cards.jsonl`
via `ai/features/ibkr_stock_cards.py`. Cards use `account_env="ibkr_paper"` so they
are distinct from Saxo SIM rows but flow into the same Stock Outcome Predictor and
AI Trading Journal pipelines — no extra plumbing needed.

The AI dashboard (`ai_dashboard.py`) shows an **IBKR PAPER** section with total
open/closed, WR, PF, P&L (USD), per-strategy open breakdown, and the running count
of observation cards logged.

**Log-only — no AI decisions applied to IBKR.** The `can_apply_decision()` gate is
hardcoded False for `ibkr_paper`; acting on IBKR would require an explicit code change
and a separate written go/no-go.

---

## Scorer strategy detail

Two sub-strategies from a single scoring run against the 363-ticker quality universe:

**Scorer Swing** (8 slots, `scorer_swing`):
- Screen: price > $10, ADX(14) > 20, 63-day ROC > 5%
- Rank: composite of ROC(63), ADX momentum, RS-vs-SPY
- Entry: market order at US open; 8% trailing stop

**Scorer Portfolio** (10 slots, `scorer_portfolio`):
- Screen: price > $10, above SMA(200), trend quality > threshold
- Rank: composite of SMA200 distance, trend strength, 63-day ROC
- Entry: market order at US open; 8% trailing stop

---

## US Blend detail

Cross-sectional momentum rebalance (same logic as ATOS SIM / Avanza ISK):
- Universe: 363 quality US tickers (`config/atos_us_500_universe.csv`)
- Offense: top ranked by 12-1 month return / volatility (risk-adjusted momentum)
- Defense: lowest volatility names above EMA(200)
- Risk-off: if SPY below EMA(200), shift to full defense / cash
- Rebalance: every ~14 days (fortnightly Thursdays)

**Important:** `run_rebalance()` reads current blend holdings from the **local DB**
(`st.get_open_positions(strategy="blend")`), not from IBKR broker positions.
This prevents the rebalancer from selling positions owned by other strategies
(scorer, reversion, signals) that happen to be in the same IBKR account.

---

## US Reversion detail

Mean reversion on daily bars:
- Entry: RSI(14) < 38, price > EMA(200), price ≥ 5% below SMA(20), volume ≥ 1.5× 20d avg
- Exit: RSI(14) > 60 OR price returns to SMA(20) OR 4% stop hit OR 10 trading days held
- 15 slots, $3,333/slot, hard 4% stop placed as GTC stop order at IBKR

---

## Known incidents

| Date | Incident | Fix |
|---|---|---|
| 2026-09-04 | Blend rebalancer found scorer positions in IBKR account, sold all 17 on first run | Fixed `run_rebalance()` to use `st.get_open_positions(strategy="blend")` not `ic.get_positions()` |
| 2026-09-04 | 34 stale rows deleted from `ibkr_stocks.db` (17 scorer BUYs + 17 orphan blend SELLs) | DB backup at `data/ibkr_stocks.db.bak_20260905_010846`; orphan stop orders cancelled in TWS |
