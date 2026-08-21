# IBKR Migration Guide

Five new files, mirroring your existing Saxo modules 1:1 by role. Nothing
existing was touched — `saxo_client.py`, `saxo_order.py`, `price_service.py`,
and every runner are exactly as they were (until you choose to wire a
runner to the IBKR files instead — see "Per-runner notes" below).

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

**Still an open account-side gap, not a code bug:** on the verified paper
account, FX (`IDEALPRO`) market data errors outright ("Error 10089 …
requires additional subscription for API"), and even delayed US-stock
bid/ask stayed unavailable — only `close` (previous close) came through,
and only after ~3-4s. `fetch_prices()` now falls back bid/ask → last →
close and waits 2.5s, but a permanently-unavailable bid/ask for an
instrument means the *account* needs a (free) market data subscription
added in Client Portal → Settings → **User Settings → Market Data
Subscriptions** — not something fixable from this code. Do this before
relying on `ibkr_price_service.py` for anything that sizes a stop off a
live price.

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

...**not** a rewrite, for any code that only calls the functions below.

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

**`atos_runner.py` (US stocks)**
Calls `saxo_client.get_balances()`, `place_market_order(uic, asset_type,
buy_sell, amount)`, `place_order(uic=..., side=..., qty=..., asset_type=...)`
— all present in `ibkr_client.py` with identical signatures. `asset_type`
stays `"Stock"` throughout; IBKR resolves it to `STK`/SMART/USD.

**`forex/` runners**
FX pairs like `"EURUSD"` are auto-detected (any 6-letter A–Z string →
`Forex` contract) — no explicit asset_type wrangling needed beyond what
you already pass (`"FxSpot"`). `saxo_order.place_with_stop()` →
`ibkr_order.place_with_stop()` keeps the same JPY-3dp and stop-price
rounding rules, ported as-is. **This one call site is not a pure import
swap**: `forex/runner.py`, `futures/runner.py`, and
`saxo_etf_strategy/core/etf_executor.py` all call
`saxo_order.place_with_stop(post_fn=_post, ...)` — `ibkr_order`'s version
has no `post_fn` parameter (there's no JSON body to inject; it talks to
`ibkr_client`'s connection directly), so that one keyword argument needs
to be dropped at each of those three call sites.

**`futures/` runners**
`asset_type="ContractFutures"` (CL, NG, ZB, ZC, ZW, ZS) → IBKR `FUT` on
NYMEX/CBOT directly. `ES/NQ/YM/DAX/HK50/GC/SI` are Saxo's continuous
index-CFDs and spot-like metals — `lookup_instruments_ibkr.py` resolves
these too, as real CME/CBOT/EUREX/HKFE/COMEX futures on the same ticker
(not skipped), but every row needs a manual contract-month check since
`find_instrument()` doesn't pin an expiry — and roll risk/sizing genuinely
differs from Saxo's continuous wrappers, so don't treat "resolved" as
"ready to trade" for these without checking the CSV's `needs_review` notes.

**`saxo_etf_strategy/`**
Same as `atos_runner.py` but `asset_type="Etf"` → IBKR `STK` (IBKR treats
ETFs as plain stock contracts, no separate type).

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
