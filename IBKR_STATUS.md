# IBKR Status — What Works, What Doesn't, What's Tested

Living document. Last updated 2026-08-21. For the "why" behind the design
see [IBKR_ARCHITECTURE.md](IBKR_ARCHITECTURE.md); for migration-by-migration
history see [MIGRATION_GUIDE.md](MIGRATION_GUIDE.md).

Everything below marked "tested live" was actually run against the real
paper Gateway (account `DUR952126`, port 4002) during this migration, not
just code-reviewed. Everything marked "not tested" is implemented but
unverified end-to-end.

## Working, verified live

| Capability | Status | Evidence |
|---|---|---|
| Gateway connect/reconnect | ✅ | `test_connection()` returns account list; retry-with-backoff on connect failure |
| Account balances/equity | ✅ | `get_balances()` — TotalValue, CashAvailableForTrading, CashBalance alias, Currency all correct |
| Positions | ✅ | `get_positions()` — flattened shape confirmed against real (empty, then post-test) positions |
| Instrument resolution | ✅ | Plain US tickers, FX pairs, explicit `SYM:SECTYPE:EXCH:CCY` form, ambiguous futures (auto-picks nearest month) all confirmed |
| Historical bars | ✅ | Daily and hourly, both FX (after market data fix) and stocks |
| Live prices | ✅ | FX, stocks, ETFs all confirmed returning real quotes after market data sharing was enabled |
| Bracket order construction | ✅ | Entry+stop+TP transmit as one linked group, confirmed via `get_open_orders()` mid-flight |
| Order amend | ✅ | `amend_stop()` repriced a live resting stop, confirmed |
| Order cancel | ✅ | `cancel_order()` confirmed on both bracket legs |
| Rejection detection | ✅ | A rejected entry now raises with the broker's reason, instead of silently returning fake success (see "Bugs found" below) |
| Orphaned-leg cleanup | ✅ | A rejected entry's stop/TP children get cancelled too, with retry (see "Bugs found") |
| **atos_runner.py full cycle** | not run end-to-end | Individual functions (balances, orders, instrument map) verified; the full daily-cycle script itself wasn't executed live during this migration |
| **ETF executor** | not run end-to-end | Code migrated and reviewed twice (once to fix a bug), individual IBKR calls it uses are all independently verified, but `run_etf_bot.py` itself wasn't executed live (needs `saxo_etf_strategy/data/etf_universe.json`, a Saxo-catalog cache not present in the dev worktree) |
| **forex/runner.py full cycle** | ✅ | Full `python forex/runner.py` dry-run executed successfully: connected, read equity (1,000,000 SEK correctly capped to 300,000 SEK), fetched daily bars for all 34 pairs, live prices for all 34, ran all 10 strategies, generated a real signal (EMA/CHFJPY), sized it correctly, blocked further entries at the 6% heat cap, London Breakout book capital showed correctly as 15,000 SEK |

## Blocked — account-side, not code

### 1. Market data subscriptions — RESOLVED 2026-08-21
Paper accounts can't subscribe to market data independently; only work by
*sharing* the live account's subscriptions (off by default, and doesn't
apply to an already-connected Gateway session even after saving in
Client Portal — needs a full logout/login). Fixed and re-verified: FX,
stock, and ETF live prices and historical bars all confirmed working
after enabling sharing + Gateway restart.

### 2. FX cross-currency-pair trading — **NOT RESOLVED, actively blocking**
**This is the current blocker.** Every FX order rejects with:
```
Error 201: Order rejected - reason: FX trade would expose account to currency leverage.
```
**Confirmed pattern** (tested directly, not inferred): pairs including the
account's base currency (SEK) fill instantly — USDSEK, EURSEK, NOKSEK all
tested successfully. Every pair that doesn't touch SEK is rejected
identically — EURUSD, USDJPY, GBPJPY, EURGBP, AUDNZD all tested, all
rejected, at every size tried (100 to 20,000 units) and both market and
limit order types.

**Practical impact: only 2 of the 34 pairs in `forex/universe.py`
(USDSEK, EURSEK) can actually execute live right now.** The other 32 —
which is most of the strategy universe — will place-and-immediately-reject
every time (correctly detected and cleaned up as of the 2026-08-21 fix,
so no phantom positions or orphaned orders, but no fills either).

**Ruled out as the cause:**
- Order type (limit orders rejected identically to market)
- Order size (100 units rejected same as 20,000)
- Trading Permissions (Currency/Forex already enabled on the live account;
  confirmed via IBKR's own docs that permissions mirror to paper
  automatically, unlike market data)
- Stale session (retested immediately after a full Gateway logout/login —
  same rejection)

**Attempted fix that didn't resolve it:** submitted a Trading Permissions
request under Currency/Forex → "locations" → checked "All Global" +
"Currency Conversion" (previously unchecked), which showed "approved
successfully." Retested after — same rejection, both immediately and
after a Gateway restart.

**Best-informed hypothesis** (from web research, not IBKR confirmation):
matches a documented restriction on IBKR's EU-regulated entities (e.g.
Interactive Brokers Central Europe) — an ESMA/MiFID II retail-protection
leverage guard specifically on cross-currency pairs that don't touch base
currency. Likely fix: **Professional Client status** (2 of 3 MiFID II
criteria — 10+ significant trades/quarter, portfolio >€500k, or 1+ year
professional trading experience), which removes ESMA retail restrictions.
Not confirmed — needs IBKR support to verify. Draft support-ticket text
was sent separately.

**What NOT to conclude from this:** this doesn't mean the code is wrong,
and it doesn't mean the forex migration failed — the full pipeline (data,
signals, sizing, risk gates, order construction) all work correctly, right
up to the point IBKR's own account-level restriction rejects the fill.
Once that's resolved (or if it can't be), no code changes should be
needed on this side.

## Bugs found and fixed during this migration

Found by actually connecting and testing, not by reading code — listed
here because they indicate the kind of thing worth re-checking if new
IBKR functions get added later without live verification.

1. `find_instrument()` never parsed the documented `SYMBOL:SECTYPE:
   EXCHANGE:CURRENCY` explicit form — every non-US stock and every
   futures market would have failed to resolve.
2. `find_instrument()` crashed outright (`AttributeError`) on ambiguous
   futures (no expiry pinned) — the normal case for every futures market
   this codebase trades.
3. `ibkr_price_service.py` drove the (not thread-safe) IB connection from
   an 8-thread pool.
4. No market data type was ever requested, so IBKR defaulted to live
   (type 1) data, which silently returns nothing without a paid
   subscription.
5. `ibkr_client.get_balances()` didn't include a `"CashBalance"` key —
   every real balance consumer in this codebase reads that exact key,
   some via bare `balances["CashBalance"]` (would raise `KeyError`).
6. A pre-existing (not IBKR-migration-related) bug in `atos_runner.py`:
   a `place_order(uic=uic, buy_sell="Buy", quantity=shares)` call used
   kwargs that never existed on either broker client. Fixed while
   touching that line.
7. `saxo_etf_strategy/core/etf_executor.py`: after renaming a parameter
   from `uic` to `key` mid-edit, two references to the old name were
   missed — would have raised `NameError` the first time a live
   stop-loss/take-profit exit fired. Caught on re-review, not by
   anything that ran it — worth remembering this class of bug is easy to
   introduce and easy to miss without either running the code or a
   careful second pass.
8. `ibkr_order.place_with_stop()` returned "success" (valid order ids)
   even when IBKR rejected the entry order — `ib.placeOrder()` doesn't
   raise for broker-side rejections, which arrive asynchronously. Every
   caller would have recorded a position that was never actually opened.
9. A rejected entry's stop/TP legs aren't automatically cancelled by
   IBKR — found live (both legs showed up in `get_open_orders()` after a
   parent rejection). First fix attempt (cancel immediately) missed one
   of the two legs due to a timing race; needed a retry loop re-checking
   `ib.openTrades()` across a few rounds.
10. Runner-driven exits (a time/trailing stop, not the broker-side
    bracket firing) didn't cancel the original entry's resting stop/TP
    legs before closing — same orphaned-order risk as #9, different
    trigger. Fixed in both `forex/runner.py` and the ETF executor.
11. `forex/runner.py` couldn't even *import* without `torch` installed —
    `strategy_cnn_lstm.py` needs it unconditionally, and `runner.py`
    imports every strategy module at load time regardless of which
    `--strategy` is selected. Unrelated to the IBKR migration itself,
    just uncovered by finally running the file end-to-end; `torch` added
    to `requirements.txt`.

## Not yet tested / open items

- **futures/runner.py**: not touched, stays on Saxo. No IBKR contract-roll
  logic exists; needs a design decision before any migration work starts
  here.
- **atos_runner.py / ETF executor full end-to-end runs**: individual IBKR
  calls are verified, but the top-level scripts themselves (`python
  atos_runner.py`, `python run_etf_bot.py`) weren't executed live during
  this migration — only `forex/runner.py` got a full dry-run.
- **Existing `data/forex_state.json` positions**, if any exist on the
  user's live working copy (not present in this dev worktree): would
  carry stale Saxo Uics under the old key scheme, same issue the ETF
  state file had. Needs the same backup-and-reset treatment before going
  live here — not yet done, since there was nothing to do it *to* in this
  worktree.
- **Futures contract-month selection**: `find_instrument()` resolves
  symbol+exchange but not a specific expiry for futures. Not relevant
  until `futures/runner.py` migration is revisited.
- **Position `unrealised_pnl`**: not populated in `get_positions()` —
  needs a separate `reqPnLSingle` subscription per position. Not blocking
  anything currently, but a dashboard integration would need it.
- **Order pacing**: no explicit rate-limiting added for tight
  historical-data-request loops (e.g. `_fetch_history()` across all 34
  forex pairs). The full dry-run completed without hitting a pacing
  violation, but that's one successful run, not a guarantee under all
  conditions (e.g. running multiple sessions/strategies back-to-back).
