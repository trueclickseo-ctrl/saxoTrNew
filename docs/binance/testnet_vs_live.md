# Binance: Testnet vs Live — Readiness Checklist

This document is a running list of every testnet-specific assumption in the
Binance bot. **Review every item here before ever pointing this bot at a real
Binance account.** Items are grouped by what must change vs what stays the same.

Current status: TESTNET ONLY. Do not proceed with live until all items are checked.

---

## Testnet quirks discovered during live testing

**Pre-loaded asset balances (2026-08-09)**
Binance testnet accounts start with non-zero balances in every major asset:
BTC(1), ETH(1), BNB(1), SOL(6), ADA(2631) — all universe symbols.
These are NOT positions we opened and must not count against our slot limit.

- `get_positions()` (balance-based) sees all 5 as open positions -> slots=0 always.
  Fixed by using `get_my_trades()` instead, which only returns trades made with
  our specific API key. Pre-loaded assets have no trade history for our key.
- Before going live: confirm the live account has no pre-existing balances that
  would trigger the same false-positive. If it does, `get_my_trades()` will still
  correctly show only trades we placed, so this fix holds for live too.

**"Invalid symbol" from get_positions() (2026-08-09)**
Some pre-loaded assets do not have a USDT pair on testnet (e.g. obscure tokens).
Calling `get_symbol_ticker(asset + "USDT")` throws `APIError(code=-1121)`.
Fixed by wrapping the per-asset ticker call in a try/except and skipping silently.
This is not a real error -- it just means that balance cannot be priced.

**Position state reconstruction durability gap**
`_open_slots()` and `_total_open_notional()` derive open positions from trade
history (last 100 trades per symbol). This works for testnet but will drift if:
- Bot restarts mid-position and old fills fall outside the 100-trade window
- A manual trade is placed on the same API key during testing
- A position is partially filled across multiple orders

**Backlog item (not urgent for testnet):** Replace trade-history reconstruction
with a proper position ledger in a database (e.g. `data/binance_positions.json`
or a SQLite table). Write on every fill, read at scan start. This is the
correct architecture for a production bot.

---

## Items that MUST change for live

### 1. Base URLs
```yaml
# Testnet (current)
BINANCE_TESTNET_BASE_URL=https://testnet.binance.vision/api

# Live (do NOT add until ready)
# BINANCE_API_KEY=
# BINANCE_API_SECRET=
# BINANCE_BASE_URL=https://api.binance.com
```
Action: Create a separate `.env.live` (gitignored). Never mix testnet and live keys.
In `binance_client.py`, the SDK is initialised with `testnet=True` — change to `False`
and update `API_URL`.

### 2. API key pair
Testnet keys (from testnet.binance.vision) DO NOT work on mainnet.
You need a separate real API key from the live Binance dashboard.

Before creating a live key:
- [ ] Enable IP allowlist on the key (whitelist only the machine running the bot)
- [ ] Enable only "Spot Trading" permission — no Withdrawal, no Futures, no Transfer
- [ ] Store in `.env.live`, never in `.env.binance` or `.env`

### 3. Account balance
Testnet gives you fake USDT (typically 10,000-100,000 depending on the cycle).
Live balance is your real deposited USDT. Position sizing is calculated as a
percentage of free USDT — recheck `capital.position_size_pct` before going live.

### 4. Pakistan account specific
- [ ] Confirm Binance is accessible from Pakistan (VPN may be needed; check current regulations)
- [ ] Confirm KYC is completed on the live account
- [ ] Check withdrawal limits and whether futures trading is available in the jurisdiction
- [ ] If regulatory access is restricted, consider Binance.com vs Binance alternatives

### 5. Minimum notional
Testnet MIN_NOTIONAL is typically 5 USDT per order. Mainnet minimums vary by symbol
(usually 10-15 USDT for major pairs). With a small initial balance, the bot may fail
to place orders if position_size_pct × balance falls below MIN_NOTIONAL.

Action: Add a pre-order check in `BinanceAdapter.place_order()` that reads the
PRICE_FILTER / MIN_NOTIONAL filter and skips the order with a warning if notional < minimum.

---

## Items that stay the same

- Signal logic (`strategies/binance/mean_reversion.py`) — no changes needed
- `binance_adapter.py` — broker-agnostic except for the `testnet=True` flag in client
- Config structure (`binance_testnet_config.yaml`) — copy and adjust values; don't rename
- Logging — `logs/binance_*.log` is the same namespace
- `.gitignore` — `.env.binance` already gitignored; `.env.live` must also be added

---

## Pre-live checklist

Work through this in order. Do not skip steps.

- [ ] Strategy has been running on testnet for at least 4 weeks with signals observed
- [ ] At least 5 completed trades (entry + exit) recorded in testnet logs
- [ ] Backtest run on historical crypto data (see `docs/binance/strategy_notes.md`)
- [ ] Backtest Sharpe > 1.0 and Win Rate > 55% confirmed
- [ ] `execution.dry_run: true` has been tested and prints correct signal summaries
- [ ] `--execute` has been run on testnet and orders confirmed filled in dashboard
- [ ] Exit logic (stop-loss, max-hold, take-profit) implemented and tested on testnet
- [ ] API key for live account created with IP allowlist and Spot-only permissions
- [ ] `.env.live` created (gitignored) with live key/secret and mainnet URL
- [ ] `run_binance_bot.py` updated to load `.env.live` (or a `--env` flag added)
- [ ] Pakistan regulatory check completed and access confirmed
- [ ] Initial deposit into Binance live account is an amount you can afford to lose entirely
- [ ] Email notification system wired to Binance bot (currently only Saxo bot has email)
- [ ] First live run uses `execution.dry_run: true` to verify signal pipeline end-to-end
- [ ] Only after all above: set `dry_run: false` and start with one position max (`max_slots: 1`)

---

## Risk reminder

The Binance bot is completely separate from the Saxo SIM bot.
The Saxo bot is paper money. The Binance bot, once live, uses real funds.
The two bots run in separate processes and share no state. There is no
combined kill-switch — each must be stopped independently.

Never run both bots in the same Python process or combine their configs.
