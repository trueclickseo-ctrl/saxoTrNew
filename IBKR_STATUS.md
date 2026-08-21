# IBKR Status — What Works, What Doesn't, What's Tested

Living document. Last updated 2026-08-21. For the "why" behind the design
see [IBKR_ARCHITECTURE.md](IBKR_ARCHITECTURE.md); for migration-by-migration
history see [MIGRATION_GUIDE.md](MIGRATION_GUIDE.md).

> **Headline (2026-08-21):** stocks, ETFs, market data, historical bars and
> the whole forex code pipeline all work. **Spot FX cross-pairs are blocked
> by an IBKR Ireland regulatory restriction — confirmed by IBKR support in
> writing, not fixable in code or by settings.** Forex CFDs are IBKR's
> supported alternative and 34/34 pairs are reachable, but CFDs can't be
> held in a Swedish ISK — which reintroduces the K4 paperwork this
> migration was meant to avoid. See §2/§2b for the decision.

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

### 2. FX cross-currency-pair trading — **CONFIRMED HARD BLOCK by IBKR**
Every spot FX order on a pair without a SEK leg rejects with:
```
Error 201: Order rejected - reason: FX trade would expose account to currency leverage.
```

**IBKR Client Services confirmed this in writing (2026-08-21):** it is a
*hard regulatory restriction* under **IBKR Ireland (IBIE)**. As a retail
client of that entity, leveraged FX transactions that would create or
increase a negative balance in either component currency are not
supported. Only three FX scenarios are permitted:
1. **FX Conversion** — converting a positive currency balance into base (SEK)
2. **Debt Reduction** — reducing the net of negative currency balances
3. **Debt Consolidation** — trading a negative non-base balance into base (SEK)

They explicitly stated the "All Global" permission change could not fix
this ("the restriction is regulatory, not a permissions configuration
problem"), and that they **cannot confirm Professional Client status
removes it** for IBIE accounts.

**Independent testing matched their rule exactly**, before their reply
arrived. The decisive evidence — same instrument, opposite directions,
opposite outcomes, while holding a long USD balance:
| Order | Effect | Result |
|---|---|---|
| **BUY** USDJPY | long USD, short **JPY** (none held) | **rejected** |
| **SELL** USDJPY | short **USD** (45,000 held), long JPY | **filled** |

So the rule is not "the pair must contain SEK" as first assumed — it is
**"the order must not create or increase a short balance in a currency
you don't already hold."** Pairs containing SEK always work because
shorting the base currency is always permitted. Confirmed across
USDSEK/EURSEK/NOKSEK (fill) vs EURUSD/USDJPY/GBPJPY/EURGBP/AUDNZD
(reject), at every size (100–20,000 units) and both order types. EURUSD
*did* fill once a USD balance was on hand — proving the pair itself was
never the problem.

**Ruled out as causes:** order type, order size, Trading Permissions,
stale Gateway session, market data.

**Practical impact: spot FX on IBKR cannot support this strategy.** The
strategies go both long and short across 34 pairs; pre-funding every
quote currency in both directions isn't workable (it would require
holding meaningful balances in all 8+ currencies simultaneously, and a
short entry still needs the base currency held).

### 2b. The Forex CFD alternative — works technically, but see the ISK problem
IBKR support's recommended route for leveraged cross-pair FX exposure
under IBIE is **Forex CFDs**, requestable via Client Portal → Settings →
Trading Permissions.

**Tested — the contracts resolve cleanly.** Using
`CFD(base, currency=quote)` (i.e. `CFD('EUR', currency='USD')`):
**33 of 34 pairs in `forex/universe.py` resolve immediately.** The single
failure, EURAUD, is an *ambiguity* (two CFD contracts match, `tradingClass`
`EUR.AUD` vs `EUR`), not a missing instrument — fixable by pinning
`tradingClass`, so effectively **34/34 are reachable**. The alternative
formulation (`secType="CFD", symbol="EUR.USD"`) does *not* work — returns
"No security definition found."

**But two serious caveats before taking this route:**

1. **CFDs cannot be held in a Swedish ISK account.** Derivatives —
   including CFDs — are excluded from ISK by law; ISK is limited to
   instruments admitted to trading on a regulated market. Trading FX CFDs
   would require a regular taxable account (*aktie- och fondkonto*/depå),
   taxed at 30% on gains **with self-reported declarations — i.e. the K4
   paperwork this whole migration was meant to avoid**, at least for the
   forex book. (Stocks/ETFs/funds in the ISK are unaffected.)
2. **CFDs are economically different from the spot FX these strategies
   were built and backtested on** — wider spreads plus overnight
   financing charges. That matters most for the swing strategies (ema,
   rsi, donchian, bb, pullback, supertrend, zscore, ml, cnn_lstm) which
   hold positions for days; `london_breakout` closes same-day so is far
   less affected. Backtested edges would need re-validating against CFD
   cost assumptions before trusting them.

**Tax-wrapper question — settled 2026-08-21: there is no Swedish
tax-wrapped account that can hold forex.**
- **ISK**: only instruments admitted to trading on a regulated market or
  trading platform. Pure currency positions are treated as speculative
  trading and fall outside the ISK framework; derivatives/CFDs likewise
  excluded. Currency *exchange to settle a securities trade* is fine and
  automatic — that's settlement, not an FX position.
- **KF (kapitalförsäkring)**: more permissive than ISK for some
  derivatives (warrants, turbo warrants, Mini Futures, certain
  certificates) but **CFDs and forex are excluded from KF too**. Also
  moot here: IBKR does not offer KF (it requires a Swedish insurance
  company as legal owner; IBKR Sweden offers only ISK + general
  investment accounts).

Consequence: **forex requires a taxable account and K4 reporting at any
Swedish broker, regardless of instrument or venue.** The ISK/K4
motivation behind this migration only ever applied to stocks and ETFs —
which are migrated and working. Tax is therefore *neutral* between Saxo
and IBKR for forex, leaving execution quality as the only real
differentiator.

*Possible future angle:* exchange-listed **currency futures** (CME
6E/6J/6B/6A/6C/6N/6S or Micro M6E etc.) are regulated-market instruments,
and IBKR now supports futures inside Swedish ISKs — the only route that
could put FX exposure in a tax wrapper. Not a port of the current
strategy though: contract sizes are large relative to the books here
(M6E ≈ 12,500 EUR ≈ 140,000 SEK notional vs the 15,000 SEK LBO book),
coverage is ~7 USD-crosses rather than 34 pairs, and it needs the same
contract-roll logic `futures/runner.py` is parked on. Would be a new,
smaller strategy, not a migration.

**Options, honestly stated:**
- **Keep forex on Saxo** (recommended default) — spot FX there already
  works and is what the strategies were validated on; migrate only the
  instruments that genuinely benefit from the ISK. Costs nothing but
  leaves forex on the old broker.
- **Forex CFDs on IBKR in a non-ISK account** — technically ready
  (34/34 pairs reachable, needs CFD permission + a code change to build
  CFD contracts instead of `Forex` ones), but reintroduces K4 for forex
  and needs strategy re-validation for CFD costs.
- **Spot FX limited to SEK-leg pairs** — only 2 of 34 pairs; not viable
  as a strategy.
- **Professional Client status** — IBKR could not confirm this lifts the
  restriction, so it's a gamble, not a plan.

**What this does NOT mean:** the forex code migration isn't wrong or
wasted. The full pipeline — market data, historical bars, all 10
strategies, sizing, risk gates, bracket-order construction, exits,
healing — is implemented and verified working against the live Gateway.
Only the final fill is refused, by broker policy. If the CFD route is
taken, the change needed is contained: build CFD contracts in
`_ibkr_uic()`/`find_instrument()` instead of `Forex` contracts.

**Note on the paper account after testing:** the FX tests left two virtual
FX positions (USD.SEK +45,000, USD.JPY −20,000) that can't be unwound,
because closing them is itself blocked by the same restriction. Net
liquidation is intact (999,862 SEK vs 1,000,000 start — the difference is
spread cost). These are FX-Portfolio tracking entries, not risk exposure,
and the cleanest way to clear them is a paper-account reset from Client
Portal if a pristine starting state is wanted.

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
