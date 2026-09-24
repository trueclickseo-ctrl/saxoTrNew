# IBKR Live Quant Bot — Complete Reference

**Account:** U28013794 (ISK, SEK-denominated)  
**Platform:** Interactive Brokers via IB Gateway  
**Status:** LIVE — real money  
**Last verified:** 2026-09-25  
**Paper account docs:** [ibkr_paper_bot.md](ibkr_paper_bot.md)

---

## Table of Contents

1. [Account & Gateway Setup](#1-account--gateway-setup)
2. [Architecture Overview](#2-architecture-overview)
3. [Module Map](#3-module-map)
4. [Live Strategies](#4-live-strategies)
5. [Stop Order System](#5-stop-order-system)
6. [Database (ibkr_live_stocks.db)](#6-database-ibkr_live_stocksdb)
7. [Configuration (ibkr_config.json)](#7-configuration-ibkr_configjson)
8. [Task Scheduler (Automated Schedule)](#8-task-scheduler-automated-schedule)
9. [Commands & CLI Reference](#9-commands--cli-reference)
10. [Safety Rules](#10-safety-rules)
11. [Test Suite](#11-test-suite)
12. [Incidents & Bugs Fixed (History)](#12-incidents--bugs-fixed-history)
13. [Adding a New Strategy](#13-adding-a-new-strategy)
14. [Current Live Positions (2026-09-25)](#14-current-live-positions-2026-09-25)
15. [Evolver Gate — Live Trade Counts](#15-evolver-gate--live-trade-counts)
16. [AI Copilot — Observation Layer (Live)](#16-ai-copilot--observation-layer-live)

---

## 1. Account & Gateway Setup

| Parameter       | Value |
|-----------------|-------|
| Account ID      | `U28013794` |
| Gateway port    | `4001` |
| Currency        | SEK (ISK account) |
| DB file         | `data/ibkr_live_stocks.db` |
| Gateway config  | `config_live.ini` |
| Gateway startup | `StartGatewayLive.bat` |

> Paper account (DUR952126, port 4002) is documented separately: [ibkr_paper_bot.md](ibkr_paper_bot.md)

### Gateway Requirements

- IB Gateway must be running before any task fires.
- **Read-Only API must be unchecked** in Gateway settings.
- `AutoRestartTime` in `config_live.ini`: set via Gateway UI, NOT `config.ini`.  
  `ColdRestartTime` must be **blank** (autofill leaves old 2FA token, which blocks restart).
- Gateway auto-restarts at ~02:15 PKT each night and is fully online by 03:15 PKT.

### Environment Variable

The DB file is controlled by the `IBKR_DB_PATH` environment variable:

```
set IBKR_DB_PATH=data/ibkr_live_stocks.db
```

The `--live` flag in `run_ibkr_stocks.py` sets this env var automatically. `ibkr_state._conn()` re-reads it on every call so it takes effect even after modules are already imported.

---

## 2. Architecture Overview

```
Yahoo Finance (signal only)
        |
        v
ibkr_signals.py           <-- screens US universe, ranks candidates
        |
        v
ibkr_executor.py          <-- strategy logic, order placement
        |        \
        |         ibkr_client.py    <-- raw IB Gateway API (ib_insync)
        |
        v
ibkr_state.py             <-- SQLite ledger (ibkr_live_stocks.db)
        |
        v
run_ibkr_stocks.py        <-- CLI entry point (--live flag sets env)
```

**Data flow rule:**  
- **Yahoo Finance** = screening and signal generation ONLY (RSI, dip %, SMA, momentum ranks)  
- **IBKR live prices** = all execution sizing and order placement  
- Never use Yahoo prices for live order sizing or stop calculation

---

## 3. Module Map

| File | Role |
|------|------|
| `ibkr_module/ibkr_executor.py` | Strategy executors: blend, reversion, trail_stops, heal_missing_stops, penny, signals, scorer |
| `ibkr_module/ibkr_client.py` | Raw IB Gateway API wrapper (connect, place_order, get_prices, cancel_order, is_market_open) |
| `ibkr_module/ibkr_state.py` | SQLite ledger: record_order, mark_filled, mark_cancelled, update_stop, close_buy_position, get_open_positions |
| `ibkr_module/ibkr_signals.py` | Signal generators: blend_targets, reversion_candidates, intraday_candidates, reversion_exit_indicators |
| `ibkr_module/ibkr_scorer.py` | ATOS 500 scoring engine (SIM only) |
| `ibkr_module/config/ibkr_config.json` | All strategy parameters, account IDs, ports, client IDs |
| `run_ibkr_stocks.py` | CLI entry point; pass `--live` to target the live account |
| `ibkr_dashboard.py` | Live terminal dashboard (refreshes every 15s) |
| `test_ibkr_live_module.py` | 39-test comprehensive suite (Sections A, B, C) |

---

## 4. Live Strategies

### 4.1 US Blend (Cross-Sectional Momentum)

**Purpose:** Holds the top-N momentum stocks from the US universe. Rebalances fortnightly.

**Signal generation (`ibkr_signals.blend_targets`):**
- Downloads ~424 US tickers via Yahoo Finance (8-hour disk cache at `data/ibkr_price_cache.pkl`)
- Ranks by cross-sectional momentum score
- Returns ordered target list + `risk_off` flag (moves to defensive when SPY regime bearish)

**Execution flow:**
1. Pre-generate signal (Yahoo, ~30s) before connecting to Gateway
2. Connect to Gateway, fetch IBKR live prices for targets + held symbols
3. `_compute_plan()` produces `(buys, sells)` — whole shares, equal slot-weight
4. **SELL phase:** For each position leaving the target set:
   - Connect as `clientId=0` (master) via `cancel_stops_as_master()` to cancel ALL SELL orders for that symbol (needed because stops are placed by `clientId=13`)
   - Also cancel the DB-tracked stop ID via current client (belt-and-suspenders)
   - Poll up to 20s until all SELL orders confirmed cancelled
   - Place market SELL, confirm fill via polling
5. **BUY phase:** Skipped if any sell failed (`failed_sells` guard)
   - Place market BUY, confirm fill
   - Place GTC stop via `_place_stop_submitted()` — waits up to 15s for Submitted state
   - Email notification on fill

**Config (live):**
- Budget: `$2,000 USD`
- Max positions: `5`
- Stop: `8%` fixed below fill price
- Rebalance guard: `14 days` (skips if portfolio built < 14 days ago)
- Fractional shares: `false`

**ATR Chandelier exits:**
- Blend is listed in `atr_strategies` for trail_stops
- Stop = `trail_high - 2.5 × ATR(14)` instead of fixed %
- Falls back to fixed 8% if ATR is unavailable

**Splits:**
- `--sells-only`: runs only the sell phase (task at 19:30 PKT)
- `--buys-only`: runs only the buy phase (task at 21:00 PKT)
- This allows cash to settle between sells and buys

---

### 4.2 US Reversion (Mean Reversion)

**Purpose:** Buys short-term dip candidates from the US universe. Exits on RSI recovery or after max hold period.

**Signal generation (`ibkr_signals.reversion_candidates`):**
- Screens from `REVERSION_TICKERS` universe via Yahoo Finance
- Ranks candidates by: RSI(2) < 38, dip ≥ 5%, volume spike ≥ 1.5x
- Returns ordered list with `{ticker, price, rsi, dip_pct, vol_ratio}`

**Execution flow (entries):**
1. Check open reversion slots in DB; skip if all `max_slots` used
2. Pre-generate candidates (Yahoo) before connecting to Gateway
3. Connect, fetch IBKR live prices for new candidates
4. For each candidate with a live IBKR price:
   - Size = `budget_usd / max_slots / price` (whole shares)
   - Stop = `fill_price × (1 - 3%)`
   - Place market BUY, confirm fill
   - Place GTC stop via `_place_stop_submitted()` — waits up to 15s
   - Email on fill
5. If buy fails after 2 attempts: email alert, skip to next candidate

**Exit conditions (`run_reversion_exits`):**
- Exit triggered by `us_reversion.should_exit()`:
  - RSI(2) recovered above 60
  - Price crossed back above SMA(20)
  - Held for `max_hold_days` (10 business days)
- Always uses IBKR live price for the exit check
- Cancels GTC stop before selling

**Config (live):**
- Budget: `$2,000 USD`  
- Max slots: `3`  
- Stop: `3%` fixed below fill  
- Max hold: `10` business days  
- RSI entry threshold: `< 38`  
- RSI exit threshold: `> 60`  
- Minimum dip: `5%`  

---

### 4.3 Trail Stops (Nightly Ratchet)

**Purpose:** Ratchets GTC stop-loss orders up as positions profit. Runs nightly for all open positions across all strategies.

**Algorithm:**
1. `heal_missing_stops()` first — re-places any stop that disappeared from IB (see §5)
2. For each open position:
   - `new_high = max(trailing_high, current_price)`
   - For blend/blend_v2: `new_stop = new_high - 2.5 × ATR(14)` (Chandelier)
   - For all others: `new_stop = new_high × (1 - stop_pct)`
   - Only ratchet if improvement > `$1.00` (prevents order churn)
3. Place new stop FIRST, then cancel old stop after new one reaches Submitted
4. If new stop stays PreSubmitted: cancel it immediately and keep the old stop (avoids frozen PreSubmitted order)

**Client ID:** `clientId=13` (trail_live=20 for live)

---

## 5. Stop Order System

### The PreSubmitted Problem

GTC stop orders placed by a session that disconnects too quickly freeze in `PreSubmitted` state permanently. They cannot be cancelled via API (require manual GUI cancel) and IBKR counts them against the position limit.

**Root cause:** `place_stop_order()` returns immediately after IB acknowledges the API call — the order is not yet acknowledged by the exchange. If the session disconnects before the exchange ACK arrives, the order is stuck.

### Fix: `_place_stop_submitted()`

```python
def _place_stop_submitted(ib, account_id, sym, qty, stop_price, strategy=""):
    trade = ic.place_stop_order(ib, account_id, sym, qty, stop_price)
    for _w in range(15):   # poll up to 15s
        ib.sleep(1.0)
        status = trade.orderStatus.status
        if status == "Submitted":
            return trade, True
        if status in ("Filled", "Cancelled", "ApiCancelled", "Inactive"):
            return trade, False
    # After 15s: warn, leave in place, DB updated; heal_missing_stops will re-verify
    if status != "Submitted":
        print(f"  WARNING [{strategy}]: stop for {sym} is '{status}' after 15s ...")
    return trade, status == "Submitted"
```

Applied to **every buy-side stop placement**:
- `run_rebalance` (blend buy)
- `run_blend_v2` (blend_v2 buy)
- `run_reversion_entries` (reversion buy)
- `run_penny_entries` (penny buy)

Trail_stops uses the same 15s wait pattern directly (pre-dates the helper).

### heal_missing_stops()

Runs at the start of every `trail_stops` pass.

For each DB position:
1. Check if `stop_order_id` exists in IB's open orders
2. If stop is missing AND position still at broker: re-place the stop (recovery)
3. If stop is missing AND position is gone from broker: GTC stop triggered → record exit in DB, email notification
4. If stop is missing AND broker position gone AND no fill price: close DB position anyway

This ensures the DB stays consistent with the broker even after Gateway restarts or network drops.

### cancel_stops_as_master()

Used before blend sells to clear existing GTC stops regardless of which `clientId` placed them.

```python
ic.cancel_stops_as_master(sell_symbols, account_id, port=4001)
```

Connects briefly as `clientId=0` (IB "master client") which can cancel any order. Regular `clientId=10` can only cancel orders it placed — stops placed by `clientId=13` (trail) cannot be cancelled by `clientId=10`.

---

## 6. Database (ibkr_live_stocks.db)

**Path:** `data/ibkr_live_stocks.db` (set via `IBKR_DB_PATH` env var)

**Schema:**

```sql
CREATE TABLE trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id      TEXT    NOT NULL,        -- IB order ID (string)
    symbol        TEXT    NOT NULL,
    side          TEXT    NOT NULL,        -- BUY or SELL
    qty           REAL    NOT NULL,
    limit_price   REAL,                   -- requested price (NULL for market orders)
    fill_price    REAL,                   -- confirmed fill price
    stop_price    REAL,                   -- current stop level
    stop_order_id TEXT,                   -- IB order ID of the active GTC stop
    trailing_high REAL,                   -- highest price seen since entry (for ratchet)
    status        TEXT    NOT NULL DEFAULT 'PENDING',  -- PENDING | FILLED | SOLD | CANCELLED
    created_at    TEXT    NOT NULL,        -- UTC ISO timestamp
    filled_at     TEXT,                   -- UTC ISO timestamp when filled
    strategy      TEXT    NOT NULL DEFAULT 'blend'
);
```

**Key state functions:**

| Function | When called |
|----------|-------------|
| `record_order(order_id, sym, side, qty, ...)` | Immediately after placing any order |
| `mark_filled(order_id, fill_price, side)` | After `confirm_fill()` returns a price |
| `mark_cancelled(order_id)` | After `confirm_fill()` returns None |
| `update_stop(sym, stop_price, stop_order_id, trailing_high, strategy)` | After placing/ratcheting a stop |
| `close_buy_position(sym, strategy)` | After a SELL fill is confirmed |
| `get_open_positions(strategy=None)` | Returns all FILLED BUY rows not yet SOLD |

**Strategy isolation:** Each strategy (blend, reversion, signals, scorer) writes to the same table but queries are filtered by `strategy` column. This prevents blend rebalancer from seeing reversion positions and vice versa.

---

## 7. Configuration (ibkr_config.json)

**Path:** `ibkr_module/config/ibkr_config.json`

```json
{
  "paper": true,               // Set to false for live
  "live_account_id": "U28013794",
  "paper_account_id": "DUR952126",
  "port_live": 4001,
  "port_paper": 4002,
  "client_ids": {
    "blend":              10,
    "reversion":          11,
    "reversion_v2":       18,
    "intraday":           12,
    "trail":              13,
    "trail_live":         20,
    "reversion_live":     22,
    "reversion_live_exits": 23,
    "info":               14,
    "positions":          14,
    "dashboard":          96,
    "signals":            16,
    "scorer":             17,
    "penny":              19,
    "bagger":             21
  }
}
```

**Critical:** Each strategy uses a unique `clientId`. IB Gateway allows only one connection per clientId. If a task is already running on clientId=10, a second connection on clientId=10 will fail. Each scheduled task must use its own dedicated clientId.

**Live strategy config (subset):**
```json
"live_blend": {
    "budget_usd": 2000,
    "max_positions": 5,
    "stop_pct": 0.08,
    "rebal_days": 14,
    "fractional": false
},
"live_reversion": {
    "budget_usd": 2000,
    "max_slots": 3,
    "stop_pct": 0.03,
    "max_hold_days": 10,
    "rsi_entry": 38,
    "rsi_exit": 60,
    "dip_pct_min": 0.05
}
```

---

## 8. Task Scheduler (Automated Schedule)

All tasks run via Windows Task Scheduler. The scheduler fires `.bat` files which pass `--live --auto` flags (no human prompts).

**Daily schedule (PKT = UTC+5):**

| Time (PKT) | Task | clientId | Notes |
|------------|------|----------|-------|
| 19:30 | Blend sells-only | 10 | Frees cash from exiting positions |
| 20:35 | Reversion exits | 23 | Checks all open reversion positions |
| 20:35 | Reversion entries | 22 | Scans for new dip candidates |
| 21:00 | Trail stops | 20 | Ratchets all stops; heal_missing_stops runs first |
| 21:00 | Blend buys-only | 10 | Deploys cash from earlier sells |
| 03:15 | Gateway auto-restart | — | Nightly reboot; Gateway online by 03:30 |

**US market hours (ET):** 09:30–16:00 ET = 14:30–21:00 UTC = 19:30–02:00 PKT

**Market closed guard:** Every `run_rebalance` and `run_reversion_entries` call checks `ic.is_market_open()` and returns without placing orders if market is closed. Trail stops and exits can run any time.

**`--auto` flag:** Eliminates all `input("Confirm? [y/N]:")` prompts. Required for unattended Task Scheduler execution. Without `--auto`, the task will hang waiting for keyboard input.

---

## 9. Commands & CLI Reference

```bash
# Live account -- always pass --live
python run_ibkr_stocks.py --live                              # blend dry-run
python run_ibkr_stocks.py --live --execute                   # blend execute (confirm each)
python run_ibkr_stocks.py --live --execute --auto            # blend execute (no prompts)
python run_ibkr_stocks.py --live --execute --auto --sells-only  # sells phase only
python run_ibkr_stocks.py --live --execute --auto --buys-only   # buys phase only

python run_ibkr_stocks.py --live --strategy reversion         # reversion entries dry-run
python run_ibkr_stocks.py --live --strategy reversion --execute --auto
python run_ibkr_stocks.py --live --strategy reversion --exits  # exit check dry-run
python run_ibkr_stocks.py --live --strategy reversion --exits --execute --auto

python run_ibkr_stocks.py --live --trail-stops                # trail stops dry-run
python run_ibkr_stocks.py --live --trail-stops --execute      # trail stops live

python run_ibkr_stocks.py --live --positions                  # show broker positions
python run_ibkr_stocks.py --live --info                       # account summary

# Dashboard (refreshes every 15s)
python ibkr_dashboard.py --live
python ibkr_dashboard.py --live --once --client-id 88        # print once and exit

# Test suite (no --live flag -- reads live DB via env var)
python -X utf8 test_ibkr_live_module.py
```

---

## 10. Safety Rules

1. **Claude never runs `--live --execute`** — Claude never places real IBKR trades. The user runs all `--execute` commands themselves.
2. **Dry-run first** — Before any new live task or strategy change, always verify with `dry_run=True`. The task scheduler uses `--auto` but the human must review the dry-run output first.
3. **`--auto` flag required for Task Scheduler** — All scheduled `.bat` files must include `--auto`. Without it, the task hangs on `input()` prompts.
4. **Yahoo Finance = screening only** — IBKR live price must be fetched and used for every live order. If IBKR returns $0 for a symbol, that symbol is skipped entirely.
5. **Market hours gate** — Entries and rebalance buys are blocked outside 09:30–16:00 ET. Trail stops and exit checks run any time.
6. **failed_sells blocks buys** — If any sell fails during blend rebalance, the buy phase is entirely skipped. This prevents deploying cash from positions that weren't freed.
7. **DB must match broker** — Before any live run after a Gateway restart or incident, verify `B04` in the test suite passes (DB positions match broker positions).
8. **PreSubmitted stops** — After any Gateway restart, stops already placed may show as PreSubmitted. They self-resolve once the Gateway is stable; `heal_missing_stops` handles any that don't.

---

## 11. Test Suite

**File:** `test_ibkr_live_module.py`  
**Run:** `python -X utf8 test_ibkr_live_module.py` (from project root)  
**Total tests:** 39 (25 unit + 14 integration + 1 regression section)

The `-X utf8` flag is required on Windows to prevent `UnicodeEncodeError` from non-ASCII characters in output.

### Section A — Unit Tests (no Gateway, 25 tests)

| Test | What it verifies |
|------|-----------------|
| A01 | All 4 modules import without crash |
| A02 | `_compute_plan` normal rebalance — correct buys/sells |
| A03 | `_compute_plan` hold unchanged portfolio |
| A04 | `_compute_plan` zero price skips that symbol |
| A05 | `sells_only`/`buys_only` flags clear the right list |
| A06 | `failed_sells` guard blocks all buys |
| A07 | `_email_ibkr_alert()` never raises (fire-and-forget) |
| A08 | `is_market_open()` returns a bool without crashing |
| A09 | `cancel_stops_as_master()` is non-fatal on connect failure |
| A10 | `cancel_stops_as_master()` filters only SELL orders for target symbols |
| A11 | Trail stop PreSubmitted → keeps old stop (no naked position) |
| A12 | Trail stop Submitted → proceeds to cancel old stop |
| A13 | Reversion slots full → returns early without placing orders |
| A14 | `blend_targets()` works offline (uses 8h cache) |
| A15 | `reversion_candidates()` works offline |
| A16 | DB round-trip via temp DB (IBKR_DB_PATH override) |
| A17 | `_print_plan()` doesn't crash with any input shape |
| A18 | Market closed gate blocks `run_rebalance` execution |
| A19 | `dry_run=True` prints plan, places zero orders |
| A20 | `auto=True` eliminates all human prompts |
| A21 | No IBKR prices → execution blocked gracefully |
| A22 | `run_reversion_exits` with no positions exits cleanly |
| A23 | `_email_ibkr_fill()` never raises |
| A24 | Pre-sell verify loop exits immediately when no SELL orders |
| A25 | `rebal_days` guard skips fresh portfolio |

### Section B — Integration Tests (live Gateway, dry-run, 14 tests)

These tests connect to the real live Gateway at port 4001. All are `dry_run=True` — no orders placed.

| Test | What it verifies |
|------|-----------------|
| B01 | Gateway reachable on port 4001 |
| B02 | Account U28013794 accessible |
| B03 | Account summary returns numeric net liq / cash / P&L values |
| B04 | DB positions exactly match broker positions (IBKR_DB_PATH=live DB) |
| B05 | Open orders visible via `reqAllOpenOrders()` |
| B06 | Checks for PreSubmitted SELL orders (warns during market hours; normal during close) |
| B07 | All DB stop_order_ids are present at broker |
| B08 | Blend dry-run completes end-to-end without crash |
| B09 | Reversion dry-run completes end-to-end without crash |
| B10 | Trail stops dry-run completes end-to-end without crash |
| B11 | `heal_missing_stops` dry-run places 0 stops (all stops healthy) |
| B12 | IBKR can return live prices for all currently held positions |
| B13 | Blend `sells_only` dry-run skips buy phase |
| B14 | Blend `buys_only` dry-run skips sell phase |

### Section C — Regression

Runs existing `test_ibkr_copilot_hooks.py` — verifies AI copilot APPROVE/MODIFY/REJECT paths for all 7 strategies.

**Re-run triggers:**
- Any change to `ibkr_executor.py`, `ibkr_client.py`, or `ibkr_state.py`
- After a Gateway incident or account reset
- Before promoting a new strategy to live

---

## 12. Incidents & Bugs Fixed (History)

### 2026-09-25: PreSubmitted Stop Bug (all buy-side placements)

**Symptom:** HPE stock was bought; its GTC stop order froze in `PreSubmitted` state. When later trying to sell HPE, `cancel_stops_as_master()` could not remove the frozen stop, blocking the sell.

**Root cause:** All buy-side stop placements (blend, blend_v2, reversion, penny) slept only 1 second after calling `place_stop_order()`. If the Gateway session closed within that 1 second, the exchange acknowledgement never arrived and the order froze.

**Fix:** Introduced `_place_stop_submitted()` helper — waits up to 15 seconds polling for `Submitted` status before returning. Applied to every buy-side stop. If still not `Submitted` after 15s, logs a warning and leaves the order in place (DB still updated); `heal_missing_stops` will verify on the next run.

**Commit:** `cae00e5`

### 2026-09-22: IBKR Paper Account Reset

**Symptom:** Paper account reset by IBKR → order-ID collisions corrupted DB, 10 orphan short positions.

**Fix:** Added `AND status='PENDING'` guard to `mark_filled()` to prevent double-processing; cleanup procedure documented.

### 2026-09-23: Gateway 2FA Fix

**Symptom:** Gateway was started with `StartGateway.bat` (wrong config file) → used `config.ini` (paper) → 2FA prompts every morning.

**Fix:** Task Scheduler now starts `StartGatewayLive.bat` + `config_live.ini`. `ColdRestartTime` blanked in `config_live.ini`. Zero 2FA prompts from 03:15 AM 2026-09-25 onwards.

---

## 13. Adding a New Strategy

Follow these steps to add a new strategy to the live module:

### Step 1: Signal generator (ibkr_signals.py)

Add a function that returns a list of candidates using Yahoo Finance only:
```python
def my_strategy_candidates() -> list[dict]:
    # Download / screen tickers
    # Return [{ticker, price, rsi, ...}, ...]
```

### Step 2: Executor (ibkr_executor.py)

Add `run_my_strategy_entries()` and `run_my_strategy_exits()` functions. Follow the existing reversion pattern:
- Accept `ib, account_id, cfg, dry_run=True, auto=False`
- Get open positions via `st.get_open_positions("my_strategy")`
- Fetch IBKR live prices for all candidate tickers
- Check `ic.is_market_open()` before placing orders
- Place market BUY via `ic.place_market_order()`
- Place GTC stop via `_place_stop_submitted()` — **do not skip the 15s wait**
- Call `st.record_order()`, `st.mark_filled()`, `st.update_stop()` in sequence
- Email fill via `_email_ibkr_fill()`

### Step 3: Config (ibkr_config.json)

Add a new strategy block and assign a unique `clientId`:
```json
"client_ids": {
    "my_strategy": 30
},
"strategies": {
    "my_strategy": {
        "budget_usd": 2000,
        "max_slots": 3,
        "stop_pct": 0.05
    }
}
```

### Step 4: CLI hook (run_ibkr_stocks.py)

Add the `--strategy my_strategy` branch to the `main()` function.

### Step 5: Tests (test_ibkr_live_module.py)

Add to Section A:
- Slots-full guard
- Dry-run places zero orders
- Failed fill path

Add to Section B:
- Dry-run end-to-end against live Gateway

### Step 6: Task Scheduler

Create a `.bat` file:
```bat
python run_ibkr_stocks.py --live --strategy my_strategy --execute --auto
```

Schedule it with a unique time and add it to the schedule table in this document.

---

## 14. Current Live Positions (2026-09-25)

Account state as of 2026-09-25 00:13 PKT:

| Symbol | Strategy | Qty | Entry | Stop | Days Held |
|--------|----------|-----|-------|------|-----------|
| ROKU | US Blend | 4 | $153.79 | $156.54 | 14 |
| DELL | US Blend | 1 | $539.28 | $540.76 | 10 |
| ABNB | US Reversion | 4 | $149.97 | $145.47 | 0 |
| BMY | US Reversion | 10 | $60.80 | $58.97 | 0 |

**Account summary:**
- Net Liquidation: SEK 24,506
- Cash: SEK 1,024 (~$99 USD)
- Realized P&L: +SEK 364

**Note:** ROKU stop ($156.54) is above current price ($153.19) — ROKU is at a loss and would trigger a stop-loss sell if breached. The stop was placed at entry and has not yet been ratcheted below entry (meaning it has not gone up enough to move the stop).

---

---

## 15. Evolver Gate — Live Trade Counts



The AI Strategy Evolver requires **30 closed trades** per strategy. Live is accumulating slowly (real trades only).

As of **2026-09-25:**

| Strategy | Closed | Gate (30) | Status |
|----------|--------|-----------|--------|
| blend | 2 | 30 | need 28 more |
| reversion | 0 | 30 | no closed trades yet |

The evolver will not be run against the live account until each strategy crosses 30 closed trades. In the meantime, evolver improvements derived from the paper account carry over to live parameters once validated.

See the paper account evolver section for the full picture: [ibkr_paper_bot.md §9](ibkr_paper_bot.md#9-evolver-gate--trade-counts)

---

## 16. AI Copilot — Observation Layer (Live)

The AI layer observes every live trade and feeds the outcomes into the shared learning pipeline. **It never acts on live — IBKR live (`ibkr_live`) is permanently excluded from `_AI_ACTING_ACCOUNTS` in `ai/config.py`.**

### 16.1 What the AI Records on Live

Every BUY fill and every SELL fill on the live account writes to `data/stock_observation_cards.jsonl` (the same file all other accounts use, identified by `account_env="ibkr_live"`).

| Fill event | What is written |
|------------|----------------|
| BUY confirmed | Entry card: symbol, entry price, stop price, shares, risk_usd, strategy |
| SELL confirmed | Exit card: exit price, exit_reason, net_pnl_usd, r_multiple, holding_hours |

**Card ID format:** `ibkr_live:us_blend:ROKU:2026-09-25`

This is deterministic — the sell-side process reconstructs it from the trade's DB `entry_date`, not from a stored card ID.

### 16.2 How Live Cards Feed the Learning Model

```
Live fill → ibkr_executor.py
    ↓
ai/features/stock_cards.py (log_stock_entry_card / log_stock_exit_card)
    ↓
data/stock_observation_cards.jsonl
    ↓
ai/models/stock_outcome_predictor.py (train, daily at 22:00 PKT)
    ↓
data/stock_outcome_model/model.pkl
    ↓
stock_top_win_prob on every new buy proposal
```

The model trains on cards from **all** account environments together (sim + live_stocks + ibkr_paper + ibkr_live). Live trades are the most valuable — real money fills at real prices with real slippage. They carry extra weight by virtue of being real vs. simulated.

### 16.3 Hardcoded Autonomy Boundaries

| Action | Live account |
|--------|-------------|
| Observe trades | ✅ Always |
| Write observation cards | ✅ Always (while `stocks.enabled: true`) |
| Score proposals with model | ✅ (once model trains — log-only) |
| APPROVE / REJECT / MODIFY a proposal | 🔒 Log only — never applied |
| Resize order quantity | ❌ Code hard-wall |
| Skip an entry | ❌ Code hard-wall |
| Place any order | ❌ Claude never runs `--live --execute` |

The hard wall is in `ai/config.py`:
```python
_AI_ACTING_ACCOUNTS = {"sim", "ai_sim"}  # IBKR accounts not listed here
```
No config flag can override this. Promoting the live account to AI-acted requires:
1. A written go/no-go decision by the account owner
2. Explicit addition of `"ibkr_live"` to `_AI_ACTING_ACCOUNTS`
3. Code change + test + commit review

### 16.4 AI Config Flags That Apply to Live

All in `config/ai.json`, re-read every scan cycle (no restart needed):

| Flag | Path | Effect on live |
|------|------|----------------|
| `stocks.enabled` | `stocks → enabled` | `true` = write cards; `false` = no card writing |
| `stock_outcome_predictor.enabled` | top-level | Trains model from all cards including live |
| `stocks_live.enabled` | `stocks_live → enabled` | `true` = feed live trades to AI Journal |
| `stocks_live.basket_ranker_blend` | `stocks_live → basket_ranker_blend` | LLM shadow-ranks the live US Blend basket (1 call/rebalance, log-only) |

Current state (2026-09-25):
- `stocks.enabled = true` → cards active on live since commit `0114a06`
- `stocks_live.enabled = true` → live trades feed AI Journal
- `stock_outcome_predictor.enabled = true` → model will train once 50 cards close (currently ~43)

### 16.5 Checking Live AI State

Card counts for the live account:
```bash
python -X utf8 -c "
import json
cards = [json.loads(l) for l in open('data/stock_observation_cards.jsonl')]
live  = [c for c in cards if c.get('account_env') == 'ibkr_live']
print(f'ibkr_live total: {len(live)}')
entries = [c for c in live if c.get('event') == 'entry']
exits   = [c for c in live if c.get('event') == 'exit']
print(f'  entries: {len(entries)}, exits: {len(exits)}')
"
```

Predictor status (gate check):
```bash
python ai_stock_outcome_predictor.py --status
```

Full AI Copilot architecture: [ibkr_paper_bot.md §11](ibkr_paper_bot.md#11-ai-copilot--stock-learning-module)

---

*Document maintained by the bot author. Update the "Current Live Positions" section after each strategy change or significant account event.*
