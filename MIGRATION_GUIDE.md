# IBKR Migration Guide

## FINAL OUTCOME (2026-08-21)

**Shares on IBKR. Everything else stays on Saxo.**

| Runner | Broker | Notes |
|---|---|---|
| `atos_runner.py` (stocks) | **IBKR** ✅ | Migrated. Ordinary shares verified tradeable (AAPL/MSFT/SAP order-accepted). ISK-eligible → no K4. This is the migration's actual win. |
| `saxo_etf_strategy/` | **Saxo** (reverted) | US-domiciled ETFs (SPY/XLV/XLF/XLE) are **blocked on IBKR by EU PRIIPs/KID rules**. UCITS ETFs work, but re-specifying the sector-rotation universe in UCITS terms is a strategy redesign, not a symbol swap. See IBKR_STATUS.md §3. |
| `forex/runner.py` | **Saxo** (reverted) | Spot FX cross-pairs **blocked by IBKR Ireland regulation** (confirmed by IBKR support in writing). Forex CFDs would work but cost more at these trade sizes, add financing drag, and can't sit in an ISK anyway. See IBKR_STATUS.md §2/§2b. |
| `futures/runner.py` | **Saxo** (never migrated) | IBKR has no continuous/non-expiring product like Saxo's CfdOnIndex, only expiring futures needing contract-roll logic this codebase doesn't have. |

The IBKR forex/ETF migrations were written, tested against the live paper
Gateway, then **deliberately reverted** once broker-side restrictions made
them unusable. The code is preserved in git history if the situation
changes:
- forex → IBKR: commit `93d84bd` (plus `50a0068`, `fe5d5ed` for order fixes)
- ETF → IBKR: commits `d16bbfc`, `1f8fc18`

The `ibkr_*.py` modules stay in the tree — `atos_runner.py` depends on
them, and they're the foundation for any future IBKR work.

Five new files, mirroring your existing Saxo modules 1:1 by role, plus
`ibkr_history.py` (new capability, see forex notes below). Nothing on the
Saxo side was touched — `saxo_client.py`, `saxo_order.py`,
`price_service.py`, `futures/runner.py`, and every dashboard are exactly
as they were.

**Verified working 2026-08-21** against the running paper Gateway
(port 4002, account `DUR952126`, base currency SEK, ~1,000,000 SEK paper
equity). `python ibkr_client.py` connects, reads the account, and pulls
balances; `find_instrument()` resolves plain US tickers, FX pairs, explicit
`SYMBOL:SECTYPE:EXCHANGE:CURRENCY` stocks, and ambiguous futures (auto-picks
nearest unexpired month). Four real bugs from the first draft were fixed in
the process — see "Fixed during verification" below before trusting this
against a live account.

| Saxo file          | IBKR equivalent            | Role                                    |
|---------------------|------------------------------|------------------------------------------|
| `saxo_client.py`    | `ibkr_client.py`             | account/balances/positions/orders        |
| `saxo_order.py`     | `ibkr_order.py`               | native stop-loss + take-profit brackets  |
| `price_service.py`  | `ibkr_price_service.py`       | live mid-price lookups                   |
| `lookup_instruments.py` | `lookup_instruments_ibkr.py` | stocks/forex/futures → conId CSVs    |
| *(new, no Saxo equivalent)* | `resolve_etf_universe_ibkr.py` | ETF universe → conId CSV, see below |

`live_data.py` (yfinance) is untouched and keeps supplying historical bars
for signal generation on both brokers — that was never Saxo-specific.

## Setup

```bash
pip install ib_async
```

Install & log into **IB Gateway** (paper account first — `DU`-prefixed
account number), enable API access under *Settings → API → Enable Socket
Clients*, and leave it running. There's no Python-side login step, unlike
`saxo_auth.py`'s PKCE flow — the socket just connects to an already
logged-in Gateway process.

```bash
IBKR_HOST=127.0.0.1
IBKR_PORT=4002          # paper Gateway. 7497=paper TWS, 4001/7496=live.
IBKR_CLIENT_ID=1
IBKR_CURRENCY=USD       # default currency for plain tickers
```

Test it standalone first, same as `python saxo_client.py`:

```bash
python ibkr_client.py
```

## Fixed during verification (2026-08-21)

The version in the original zip had four real bugs, found by actually
connecting to the paper Gateway rather than reading the code:

1. **`find_instrument()` never parsed the documented explicit
   `SYMBOL:SECTYPE:EXCHANGE:CURRENCY` form** (e.g. `"VOD:STK:LSE:GBP"`,
   `"ES:FUT:CME:USD"`). `lookup_instruments_ibkr.py` sends exactly that
   format for every non-US stock and every futures market — it was being
   passed straight through as a literal (malformed) ticker string, so
   Nordic/German/UK/French/Dutch stocks and all futures would have failed
   to resolve. Fixed in `_build_contract()`.
2. **`find_instrument()` crashed outright on ambiguous futures** — asking
   for `"ES"` with no expiry pinned (the normal case) makes
   `qualifyContracts()` log an "Ambiguous contract" error and leave that
   slot unresolved, which the old code dereferenced without checking,
   raising `AttributeError`. Since every futures market in this codebase
   (ES/NQ/GC/CL/ZB/...) has several live contract months, this would have
   killed `lookup_instruments_ibkr.py`'s futures pass entirely. Fixed by
   resolving via `reqContractDetails()` and picking the nearest
   not-yet-expired month when the expiry isn't pinned — still flagged
   `needs_review` downstream, same as before.
3. **`ibkr_price_service.fetch_prices()` drove `ib`'s asyncio connection
   from an 8-thread `ThreadPoolExecutor`** — `ib_async`/`ib_insync` is not
   thread-safe; every call has to happen on the thread that connected.
   Rewritten to subscribe to every instrument, wait once, then read —
   all on the calling thread. Not slower (one shared wait instead of N).
4. **No market data type was ever requested** — IBKR defaults every new
   connection to live (type 1) data, which silently returns nothing
   without a paid real-time subscription (true for most paper accounts).
   `_client()` now calls `reqMarketDataType(3)` (delayed) by default;
   override with `IBKR_MARKET_DATA_TYPE=1` once you have real-time
   entitlements for what you trade.

**Account-side gap found, then fixed, 2026-08-21:** on the paper account,
FX (`IDEALPRO`) market data initially errored outright ("Error 10089 …
requires additional subscription for API"), and even delayed US-stock
bid/ask stayed unavailable — only `close` came through. Root cause: paper
accounts can't subscribe to market data independently — they only work by
*sharing* the linked live account's subscriptions, off by default. Fixed
via Client Portal (logged into the **live** account) → Settings → Account
Settings → Paper Trading Account → enable market data sharing → pick the
paper username. Takes a full IB Gateway logout/login on the paper side to
apply — a save in Client Portal alone doesn't reach an already-connected
Gateway session. Re-verified working after that: live EURUSD price, daily
bars, and hourly bars all returned real data. `fetch_prices()` still falls
back bid/ask → last → close and waits 2.5s as a safety net for instruments
without a live top-of-book, but that's no longer needed for FX on this
account.

**Second account-side gap, more serious — confirmed structural, not a
setting (2026-08-21):** a live bracket order (`ibkr_order.place_with_stop`,
20,000-unit EURUSD buy) was rejected with *"FX trade would expose account
to currency leverage"*. The bracket mechanics themselves worked correctly
(entry/stop/TP transmitted as a linked group, `amend_stop()` and
`cancel_order()` both confirmed working) — only the entry fill is blocked.

Ruled out one at a time: order type (limit and market both rejected),
order size (100 units rejected same as 20,000), and Trading Permissions
(Currency/Forex already enabled on the live account, and permissions
mirror to paper automatically — confirmed via IBKR's own docs, this was
never the cause). What actually determines it: **whether one leg of the
pair is the account's base currency (SEK).** Tested across 8 pairs —
USDSEK, EURSEK, NOKSEK all filled instantly; EURUSD, USDJPY, GBPJPY,
EURGBP, AUDNZD were all rejected identically, at every size and order type
tried. This matches a documented restriction on IBKR's EU-regulated
entities (e.g. Interactive Brokers Central Europe) — a retail-protection
leverage guard on cross-currency pairs that don't touch base currency, not
a togglable account setting.

**Practical impact: only 2 of the 34 pairs in `forex/universe.py`
(USDSEK, EURSEK) are actually tradeable live on this account today.**
Switching base currency away from SEK doesn't fix this generally — it
just shifts which subset of pairs touches base currency (e.g. EUR base
unlocks 8 pairs, not all 34), since the restriction is "must touch base
currency," not "must be SEK" specifically. The only fix found in research:
IBKR **Professional Client** status (2 of 3 MiFID II criteria: 10+
trades/quarter, portfolio >€500k, or 1+ year professional experience)
removes ESMA retail-protection restrictions including this one.
**Not yet resolved — needs a support ticket to IBKR to confirm whether
this can be lifted for this account at all.** Until then, `forex/runner.py
--live` will only ever fill USDSEK/EURSEK signals; everything else will
place-and-immediately-reject.

## The one identifier change every call site needs

Saxo's `Uic` (an integer you look up once via `find_instrument()` and then
cache) has no IBKR equivalent — IBKR resolves a `Contract` from symbol +
exchange + currency instead. `ibkr_client.find_instrument()` returns the
same shape Saxo's does, but the `"Uic"` field now holds IBKR's `conId`.
Every `ibkr_client`/`ibkr_order` function that takes `uic` accepts that
same `conId` back — so at each call site, the change is:

```python
import saxo_client            →   import ibkr_client as saxo_client
```

...**not** a rewrite, for any code that only calls the functions below and
already used `saxo_client.py`/`saxo_order.py` as an abstraction layer
(true for `atos_runner.py`). It was **not** true for `forex/runner.py`,
which hand-rolled its own Saxo REST client — see "Per-runner notes" below
for what that one actually needed.

## Building the IBKR instrument maps

Two new scripts, run once, mirroring `lookup_instruments.py`:

**`lookup_instruments_ibkr.py`** — resolves your three fixed-list
universes in one run:
- Stocks (`config.ACTIVE_UNIVERSE`) → `data/instrument_map_ibkr.csv`
- Forex (`forex/universe.py` `PAIRS`) → `data/forex_map_ibkr.csv`
- Futures `ContractFutures` entries (`futures/universe.py` `MARKETS`) →
  `data/futures_map_ibkr.csv`

```bash
python lookup_instruments_ibkr.py
```

Same as the original: **open the CSVs and check every `needs_review`
row before trusting it for real order placement.**

**`resolve_etf_universe_ibkr.py`** — deliberately *not* the same shape as
the others. Your `saxo_etf_strategy` builds its ETF universe by paging
Saxo's entire instrument catalog — currently cached at 8,924 instruments,
mostly European UCITS ETFs. IBKR's API has no equivalent "list every ETF"
endpoint to page through, so this reuses your cached Saxo list as a
*candidate* set and resolves a filtered subset against IBKR instead of
attempting all 8,924 (which would be slow, hit pacing limits, and mostly
fail for European-only listings anyway):

```bash
python resolve_etf_universe_ibkr.py --currency USD          # or:
python resolve_etf_universe_ibkr.py --symbols SPY,QQQ,VTI
```

It refuses to run unfiltered on purpose — see the script's docstring if
you want to widen it later.

Needs `saxo_etf_strategy/data/etf_universe.json` to already exist (it's
runtime-generated cache, not checked into git — it wasn't present in this
worktree). Run whatever already builds that cache on the Saxo side first,
or point `CACHE_PATH` at wherever it actually lives on the machine you run
this from.

## Per-runner notes

**`atos_runner.py` (US stocks) — done**
`import ibkr_client as saxo_client` at the top; every call site
(`get_balances()`, `place_market_order(uic, asset_type, buy_sell, amount)`,
`place_order(uic=..., side=..., qty=..., asset_type=...)`) already matched
`ibkr_client.py`'s signatures unchanged. Two real fixes made in the
process: `ibkr_client.get_balances()` gained a `"CashBalance"` alias key
(every real balance consumer in this codebase reads that exact key, some
via bare `balances["CashBalance"]` — would have raised `KeyError`
otherwise), and `instrument_map.py` gained a `broker="ibkr"` parameter so
`load_instrument_map()` can load `instrument_map_ibkr.csv` instead of the
Saxo Uic map.

**`saxo_etf_strategy/` — done**
`etf_executor.py` now calls `ibkr_client`/`ibkr_order`/`ibkr_price_service`
for account cash, order placement (native stop/TP bracket), position sync,
and exit-price checks. `etf_universe.py`/`etf_strategy.py` (signal
generation, paging Saxo's ETF catalog) are untouched — there's no IBKR
equivalent to page, so every `ETFSignal` still carries a Saxo Uic that the
executor never trades on. Instead it resolves `signal.symbol` against IBKR
directly via `find_instrument()` at order time (cached per run), so it
works without ever running `resolve_etf_universe_ibkr.py`. Position state
is now keyed by IBKR conId, not Saxo Uic — the 3 real open Saxo SIM
positions in `etf_positions.json` (XLV/XLF/XLE, opened 2026-08-17) got
backed up to `etf_positions_saxo_backup_2026-08-21.json` and state reset
to empty, since the old key scheme can't carry them forward. **Those 3
Saxo positions are no longer managed by this bot** — close them manually
on Saxo if you want them off the books.

**`forex/runner.py` — done, but a real rewrite, not an import swap**
This one hand-rolled its own Saxo REST client (`_get`/`_post`/`_patch`
against `/port/v1/...`, `/chart/v3/charts`, `/trade/v2/orders` directly)
across ~50 call sites, rather than using `saxo_client.py`/`saxo_order.py`.
Notable pieces:
- **New capability: `ibkr_history.py`** — `forex/runner.py` pulls every
  strategy's daily *and* hourly OHLC bars directly from Saxo's own chart
  endpoint (not yfinance, unlike the stock/ETF modules), so there was
  nothing to reuse. Wraps `ib.reqHistoricalData()`, returns the same
  Open/High/Low/Close(/HourUTC) shape the Saxo fetchers did.
- **conId resolution**: `forex/universe.py`'s `PAIRS` hardcodes Saxo Uics
  in each of 34 pair dicts. Rather than editing that list, `runner.py`
  gained a local `_ibkr_uic(symbol)` resolver (cached per process) used at
  every broker call site instead of `pair["uic"]`.
- **Equity currency**: the Saxo account was EUR-denominated (config had
  `risk_equity_eur`/`lbo_capital_eur` caps, ~27,800 EUR / ~1,390 EUR = the
  real 300,000 SEK / 15,000 SEK caps at ~10.8 SEK/EUR). IBKR's paper
  account is SEK-denominated, so `atos/capital_config.py` gained native
  `forex_risk_equity_sek()`/`forex_lbo_capital_sek()` reading new
  `risk_equity_sek: 300000` / `lbo_capital_sek: 15000` fields in
  `config/capital.json` — the *same* real caps, not a separate budget, now
  expressed without an EUR round-trip. (This incidentally fixed a latent
  unit mismatch: `strategy_london_breakout.py`'s `account_equity` fallback
  already defaulted to `15_000.0` assuming SEK, but the live caller was
  passing the EUR figure.)
- **Bracket orders, healing, breakeven**: `saxo_order.place_with_stop()` →
  `ibkr_order.place_with_stop()` (no `post_fn` — there's no JSON body to
  inject, it talks to `ibkr_client`'s connection directly). The
  heal-missing-stop/TP logic and breakeven stop-amend needed two new
  primitives IBKR didn't have an equivalent for yet: `ibkr_order.place_stop()`
  / `place_limit()` (standalone, non-bracket orders) and
  `ibkr_order.amend_stop()` (resubmit under the same orderId to reprice —
  IBKR's equivalent of Saxo's `PATCH /trade/v2/orders/{id}`), plus
  `ibkr_client.get_open_orders()` and `ibkr_client.cancel_order()`.
- **New safety fix, both forex and ETF**: when a *runner-driven* exit
  closes a position (a time/trailing stop, not the broker-side bracket
  firing), the bracket's resting stop/TP legs don't automatically know the
  position they protected is gone — unlike Saxo's OCO/IfDoneSlave linkage,
  IBKR only auto-cancels a bracket's own legs against *each other*, not
  against an out-of-band close. Left alone, a stale resting stop/TP could
  fill later and open an unintended reverse position. Both `_run_exits()`
  (forex) and `_exit_position()` (ETF) now call `ibkr_client.cancel_order()`
  on both legs before closing.
- **Verified**: `_ibkr_uic()` resolves live (EURUSD, and the compound-form
  fix also covers Scandi/EM crosses); `ibkr_order.place_with_stop()` +
  `amend_stop()` + `cancel_order()` all confirmed working end-to-end
  against the paper Gateway (bracket legs transmitted as a linked group,
  stop reprice worked, both legs cancelled cleanly). The test entry order
  itself was rejected by IBKR — see "Second account-side gap" above; that's
  an account permission, not a code issue.
- Existing `data/forex_state.json` positions (if you have any on your live
  copy) carry Saxo Uics under the old key scheme, same as the ETF file did
  — back that up and reset it before going live here, for the same reason.

**`futures/runner.py` — not migrated (by design)**
See "Current status" table above.

## What doesn't have a clean IBKR equivalent yet

- **Position `unrealised_pnl`** — needs a separate `reqPnLSingle`
  subscription per position; `get_positions()` doesn't include it yet.
- **Futures contract month selection** — `find_instrument()` resolves
  symbol+exchange but not a specific expiry; every row in
  `data/futures_map_ibkr.csv` needs a manual check before trading.
- **Order pacing** — IBKR rate-limits historical/market-data requests more
  aggressively than Saxo. If a runner loops over many symbols calling
  `ibkr_price_service.fetch_prices()` or `find_instrument()` in a tight
  loop, add a short sleep between batches or you'll hit pacing violations.

None of these block getting stocks/ETFs/forex running on paper first —
futures needs the contract-month check resolved before going live there
specifically.
