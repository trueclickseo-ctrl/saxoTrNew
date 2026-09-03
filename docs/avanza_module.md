# Avanza ISK Module

Semi-manual execution sleeve that mirrors the ATOS US Blend stock signal onto a Swedish Avanza ISK account. Runs independently of Saxo — separate broker API, separate SQLite ledger, separate scheduler task.

**Status (2026-09-04):** funds not yet deposited; first `--execute` run pending after transfer.

---

## Architecture

```
data/stocks_live_status.json   ← US Blend signal (written by atos_live_stocks.py)
        │
        ▼
avanza_module/avanza_executor.py   rebalance() / trail_stops()
        │
        ├── avanza_client.py        Avanza broker API (auth, prices, orders, stop-losses)
        ├── avanza_instrument_cache.py   ticker → orderBookId (disk cache)
        └── avanza_state.py         SQLite ledger  data/avanza_trades.db
```

Every trade requires an interactive `y/n` prompt — no unattended execution.

---

## Files

| File | Purpose |
|---|---|
| `avanza_module/avanza_client.py` | Avanza API wrapper: auth, positions, prices, place/cancel orders, stop-loss CRUD, `confirm_fill()` |
| `avanza_module/avanza_instrument_cache.py` | Ticker → `orderBookId` disk cache (`data/avanza_instrument_cache.json`) |
| `avanza_module/avanza_state.py` | SQLite ledger (`data/avanza_trades.db`): OPEN / FILLED / CANCELLED row lifecycle |
| `avanza_module/avanza_executor.py` | `rebalance()` (BUY/SELL/HOLD plan) + `trail_stops()` (ratchet only) |
| `avanza_module/config/avanza_config.json` | `budget_sek`, `max_positions`, `stop_pct` |
| `run_avanza.py` | CLI entry point |
| `run_avanza_trail_stops.bat` | Scheduled bat launcher for trail-stops |
| `setup_scheduler_avanza_trail_stops.ps1` | Registers "ATOS Avanza Trail Stops" in Task Scheduler (run as Admin once) |
| `test_2026_09_04_avanza_module.py` | 39 pure-logic tests (no live API) |
| `.env.avanza` | Credentials (gitignored) |

---

## Configuration

`avanza_module/config/avanza_config.json`:

```json
{
  "budget_sek": 36000,
  "max_positions": 10,
  "stop_pct": 0.08
}
```

`.env.avanza` (gitignored, never committed):

```
AVANZA_USERNAME=...
AVANZA_PASSWORD=...
AVANZA_TOTP_SECRET=...
AVANZA_ACCOUNT_ID=5834714
AVANZA_SEK_USD_RATE=10.5
```

Account: Swing ISK, account ID 5834714.

---

## CLI Commands

```bash
# Dry-run: show rebalance plan without placing any orders
python run_avanza.py

# Resolve tickers to Avanza orderBookIds (clears old cache entries)
python run_avanza.py --resolve-tickers HUM DELL STT HPE U FTNT AES PEN

# Execute rebalance (interactive y/n per trade — never automated)
python run_avanza.py --execute

# Trail-stop ratchet only (run by scheduler; only raises stops, never lowers)
python run_avanza.py --trail-stops --execute
```

**Safety rule: Claude never runs `--execute` or places Avanza trades.**

---

## Execution Flow (`--execute`)

1. Load US Blend signal from `data/stocks_live_status.json`
2. Fetch live Avanza positions + cash balance
3. Compute per-ticker action: BUY / SELL / HOLD / SKIP (equal-weight, `budget_sek / max_positions` per slot)
4. For each BUY:
   - Resolve `orderBookId` via instrument cache (or live search + cache)
   - Show plan; user types `y` to proceed
   - Place market/limit buy order
   - `confirm_fill()`: polls `get_open_orders()` every 10 s; order gone → fill confirmed; read actual fill price from `get_positions().averageAcquiredPrice`
   - On timeout (120 s): cancel order, skip this ticker
   - On fill: `mark_filled(actual_fill)`, place stop-loss at `fill × (1 − stop_pct)`
   - Record `stop_order_id` in ledger
5. For each SELL: place limit sell → `confirm_fill()` → `mark_filled()` → cancel associated stop-loss

---

## Trail-Stop Logic

`trail_stops()` runs daily (21:00 PKT / 12:00 ET):

```
new_stop = max(trailing_stop_high, current_price) × (1 − stop_pct)
```

- Only ever raises the stop, never lowers it.
- Cancel + replace the native Avanza stop-loss order on each ratchet.
- No positions opened; purely protective.

---

## Scheduler

| Task Name | Schedule | Launcher | Log |
|---|---|---|---|
| ATOS Avanza Trail Stops | Daily 21:00 PKT | `run_avanza_trail_stops.bat` | `data/avanza_trail_stops.log` |

**Register once (as Admin):**

```powershell
powershell -ExecutionPolicy Bypass -File "E:\SaxoTrNew\SaxoTrNew\setup_scheduler_avanza_trail_stops.ps1"
```

Watchdog monitors `"Avanza Trail Stops"` with `max_log_age_hours=26`.

---

## Avanza API Quirks

| Quirk | Detail |
|---|---|
| Monetary fields | Every monetary field is `{"value": x, "unit": "SEK", ...}` — use `_mv()` helper, never plain `float()` |
| ISK account type | `INVESTERINGSSPARKONTO` (not "ISK") |
| Search result ticker | Embedded in `title` as `"Dell Technologies C (DELL)"` — parse with `_extract_ticker_from_title()` |
| Search result ID field | `orderBookId` (capital B) — not `id` or `orderbookId` |
| Quote price field | `quote["last"]` (plain float) — not `latest` / `lastPrice` / `currentPrice` |
| Stop-loss placement | `place_stop_loss_order(parent_stop_loss_id="0", ...)` + `StopLossTrigger(LESS_OR_EQUAL, MONETARY, 365-day GTC)` + `StopLossOrderEvent(SELL, 1% slippage)` |

---

## Resolved Order Book IDs (2026-09-04)

| Ticker | orderBookId |
|---|---|
| HUM | 3691 |
| DELL | 918953 |
| STT | 4471 |
| HPE | 605658 |
| U | 1139014 |
| FTNT | 242850 |
| AES | 4204 |
| PEN | 595617 |

Cached in `data/avanza_instrument_cache.json` (gitignored). If cache is stale, clear it and re-run `--resolve-tickers`.

---

## First-Time Operator Runbook

After funds arrive in Avanza account 5834714:

1. Verify cash balance in Avanza app
2. Dry-run to confirm plan:
   ```bash
   python run_avanza.py
   ```
3. Resolve tickers if cache was cleared:
   ```bash
   python run_avanza.py --resolve-tickers HUM DELL STT HPE U FTNT AES PEN
   ```
4. Place orders interactively:
   ```bash
   python run_avanza.py --execute
   ```
   Approve each BUY one at a time (`y`/`n`)
5. Register trail-stops scheduler (as Admin, once):
   ```powershell
   powershell -ExecutionPolicy Bypass -File "E:\SaxoTrNew\SaxoTrNew\setup_scheduler_avanza_trail_stops.ps1"
   ```
6. Confirm task appears in Task Scheduler as "ATOS Avanza Trail Stops"

---

## Tests

```bash
python test_2026_09_04_avanza_module.py
```

39 tests covering: instrument cache scoring/deduplication, state lifecycle (OPEN/FILLED/CANCELLED), executor BUY/SELL/HOLD plan logic, `confirm_fill()` timeout + cancel path, stop-loss placement, trail-stop ratchet. No live API calls.
