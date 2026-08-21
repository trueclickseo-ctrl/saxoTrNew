# IBKR Architecture

How the IBKR integration is put together, why it's split the way it is,
and how the pieces connect. For migration history/rationale see
[MIGRATION_GUIDE.md](MIGRATION_GUIDE.md); for current test status and open
problems see [IBKR_STATUS.md](IBKR_STATUS.md).

## Why TWS API, not Web API

IBKR exposes three API surfaces: **TWS API** (socket connection to a
running, logged-in IB Gateway/TWS process — what this codebase uses, via
the `ib_async` wrapper), **Web API / Client Portal API** (OAuth + REST,
needs recurring session refresh, ~24h sessions), and **FIX** (institutional).
TWS API is the standard choice for unattended algo trading: no token
refresh loop to maintain, full order-type/contract coverage, and it's what
every serious quant-trading integration with IBKR uses. The Web API's
recurring re-auth requirement is a poor fit for a scheduled bot that needs
to run unattended via Windows Task Scheduler.

## Broker split: not everything runs on IBKR

This is a **mixed-broker system**, not a full cutover. Each module made an
independent choice based on what IBKR can and can't do:

| Module | Broker | Why |
|---|---|---|
| `atos_runner.py` (US stocks) | IBKR paper | No blocker — straightforward migration |
| `saxo_etf_strategy/` execution | IBKR paper | — |
| `saxo_etf_strategy/` universe discovery | **Saxo** | IBKR has no "list every ETF you offer" endpoint to page through; Saxo's instrument catalog is the only source for building the tradeable universe |
| `forex/runner.py` | IBKR paper | — |
| `futures/runner.py` | **Saxo** | IBKR has no continuous/non-expiring product like Saxo's CfdOnIndex — only real futures with contract-month rollover, and this codebase has no roll logic. Deliberately left on Saxo pending a decision on how to handle expiries. |

The practical consequence: `saxo_auth.py`, `saxo_client.py`, and Saxo's
own API are all still live dependencies for this system — not legacy code
to be deleted. `saxo_etf_strategy/core/etf_universe.py` and
`etf_strategy.py` (signal generation) call Saxo directly; only
`etf_executor.py` (orders, balances, positions, exits) calls IBKR.

## The IBKR module set

All new files live flat at the project root, mirroring the existing
`saxo_client.py` / `saxo_order.py` / `price_service.py` pattern rather
than introducing a class-based abstraction layer — this matches how every
real call site in this codebase already imports brokers (`import
saxo_client`, not `from core.broker_interface import BrokerInterface`).

```
ibkr_client.py           account/balances/positions/orders/instrument lookup
ibkr_order.py             native stop-loss + take-profit bracket orders
ibkr_price_service.py     live mid-price lookups
ibkr_history.py           historical OHLC bars (new capability, forex-only need)
lookup_instruments_ibkr.py    stocks/forex/futures universes -> conId CSVs
resolve_etf_universe_ibkr.py  ETF universe -> conId CSV (candidate-list approach)
```

### `ibkr_client.py` — connection and account primitives

Holds one module-level `IB()` connection (`_client()`), lazily connected
on first use and reused for the life of the process — mirrors
`saxo_client.py`'s "callers never construct a client themselves" pattern,
except there's no token: the socket just connects to an already-running,
already-logged-in IB Gateway/TWS process (`IBKR_HOST`/`IBKR_PORT`/
`IBKR_CLIENT_ID` env vars, default `127.0.0.1:4002` = paper Gateway).

On connect, it also negotiates market data type
(`reqMarketDataType`, default 3/delayed — see IBKR_STATUS.md for why) and
caches resolved `Contract` objects by conId (`_resolve_by_conid()`) so
repeated calls against the same instrument don't re-qualify it every time.

Key functions: `test_connection()`, `get_account_key()`, `get_balances()`,
`get_positions()`, `get_open_orders()`, `find_instrument()`,
`place_market_order()`, `place_order()`, `cancel_order()`.

### The identifier problem: Saxo Uic → IBKR conId

Saxo identifies instruments by an internal integer **Uic**, looked up once
via `find_instrument()` and cached in CSVs (`instrument_map.csv`,
`forex_map_ibkr.csv`, etc.). IBKR has no equivalent pre-registered id — it
resolves a `Contract` from symbol + exchange + currency + asset type, and
*that* resolution produces IBKR's own id, the **conId**.

`ibkr_client.find_instrument(symbol, asset_type)` returns the same
list-of-dicts shape Saxo's version does, with the `"Uic"` key now holding
IBKR's `conId`. Every `ibkr_client`/`ibkr_order` function that takes a
`uic` parameter accepts that same conId back — the round-trip is
consistent even though the underlying meaning changed.

Symbol resolution rules in `_build_contract()`:
- 6-letter A–Z string (`"EURUSD"`) → `Forex` contract
- Plain ticker (`"AAPL"`) → `Stock(symbol, "SMART", IBKR_CURRENCY)`
- Explicit `SYMBOL:SECTYPE:EXCHANGE:CURRENCY` (`"VOD:STK:LSE:GBP"`,
  `"ES:FUT:CME:USD"`) → parsed directly, for anything ambiguous
- Futures with no expiry pinned → resolved via `reqContractDetails()`,
  picks the nearest not-yet-expired contract month (still flagged for
  manual review downstream — this doesn't know which month you *want*,
  just which one exists soonest)

Each broker-touching module keeps its own resolution strategy fitted to
how it already worked:
- **atos_runner.py**: `instrument_map.py`'s `load_instrument_map(broker="ibkr")`
  loads a pre-built CSV (`data/instrument_map_ibkr.csv`, built once via
  `lookup_instruments_ibkr.py`)
- **ETF executor**: resolves live at order time via `find_instrument()`,
  cached per-run in `self._ibkr_uic_cache` — no pre-built CSV dependency,
  since `ETFSignal.uic` is a Saxo Uic from the untouched discovery
  pipeline and needs translating anyway
- **forex/runner.py**: same live-resolution pattern, via a module-level
  `_ibkr_uic(symbol)` helper cached in `_IBKR_UIC_CACHE` — `PAIRS` in
  `forex/universe.py` still carries Saxo Uics (kept for backward-compat/
  logging only), every broker call site uses `_ibkr_uic()` instead

### `ibkr_order.py` — order construction

**Bracket orders** (`place_with_stop()`): entry (parent, `transmit=False`)
+ stop-loss + optional take-profit (children, linked via `parentId`, last
leg `transmit=True`). IBKR only submits the whole group once the final
order transmits — this is the direct equivalent of Saxo's
`IfDoneSlave`/OCO relation, and unlike Saxo's version it's atomic (no
fallback-to-separate-orders dance needed, since IBKR either transmits the
whole linked group or none of it).

Per-asset-type rules (duration/TIF, price rounding, JPY 3dp handling,
long-only close-side logic, StopLimit vs. plain Stop) are ported from
`saxo_order.py` verbatim so risk-management behavior matches.

Waits for the entry's definitive order status before returning (added
2026-08-21 — see IBKR_STATUS.md) — `ib.placeOrder()` doesn't raise for a
broker-side rejection, so without this check a rejected entry would look
identical to a successful one to the caller. On rejection, also cancels
the stop/TP children (IBKR doesn't do this automatically — a rejected
parent doesn't take its children down with it) with a retry loop, since a
single cancel attempt immediately after detecting rejection isn't
reliably fast enough to catch both legs.

**Standalone orders** (`place_stop()`, `place_limit()`, `amend_stop()`):
added for forex's heal-missing-stop/TP logic and breakeven stop moves,
which don't go through the bracket flow (they attach a stop/TP to a
position whose entry already filled some other way, or reprice an
existing resting stop). `amend_stop()` resubmits under the same orderId
to reprice in place — IBKR's equivalent of Saxo's `PATCH
/trade/v2/orders/{id}`.

### `ibkr_price_service.py` / `ibkr_history.py` — market data

Both are careful about `ib_async`'s single-threaded requirement: **the IB
connection is not thread-safe**, every call must happen on the thread
that connected. `fetch_prices()` subscribes to every requested instrument
first, waits once, then reads and unsubscribes — all on the calling
thread — rather than fanning out across a thread pool (an earlier draft
did this and it's unsafe under `ib_async`).

Price fallback order: bid/ask mid → last trade → close. IBKR's delayed
data often doesn't populate top-of-book even when subscribed correctly;
`close` is the last resort, same "some price beats none" reasoning as
`price_service.py`'s `LastTraded` fallback. Also filters out IBKR's `-1`
"field not available" sentinel, which a naive truthy/NaN check would
otherwise read as a real price.

`ibkr_history.py` wraps `reqHistoricalData()` with `formatDate=2` (UTC-aware
timestamps, unambiguous for session-bucketing logic) and returns plain
Open/High/Low/Close(/Volume) DataFrames — no broker-specific fields, so
callers written against Saxo's bar-fetch functions need a call-site swap,
not a data-shape change.

## Per-module data flow

### atos_runner.py (US stocks)
```
atos_runner.py --(import ibkr_client as saxo_client)--> ibkr_client.py --> IB Gateway
instrument_map.py --(broker="ibkr")--> data/instrument_map_ibkr.csv
```
Straightforward: every call site already went through `saxo_client.py`'s
functions, so aliasing the import was most of the work.

### saxo_etf_strategy/ (hybrid)
```
core/etf_universe.py  ---> Saxo catalog (unchanged, pages /ref/v1/instruments)
core/etf_strategy.py  ---> Saxo chart data (unchanged, /chart/v3/charts)
        |
        v  ETFSignal (carries a Saxo Uic, never used for trading)
core/etf_executor.py  ---> ibkr_client.find_instrument(signal.symbol) --> IBKR conId
                       ---> ibkr_order.place_with_stop() --> IB Gateway
                       ---> ibkr_price_service / ibkr_client.get_positions() for exits
```
Position state (`etf_positions.json`) is keyed by IBKR conId, not Saxo
Uic — a structural change from the original, since the executor now needs
to correlate its own tracked positions against `ibkr_client.get_positions()`
results, which report conIds.

### forex/runner.py (full rewrite, not an import swap)
```
forex/universe.py PAIRS (Saxo Uics, kept for backward-compat only)
        |
        v symbol
_ibkr_uic(symbol) --(ibkr_client.find_instrument, cached)--> IBKR conId
        |
        +--> ibkr_history.get_bars()      daily + hourly OHLC, all 10 strategies
        +--> ibkr_price_service.fetch_prices()   live mid, gap strategy
        +--> ibkr_order.place_with_stop() entries
        +--> ibkr_client.place_market_order()    runner-driven exits (cancels
        |                                        resting bracket legs first)
        +--> ibkr_order.place_stop()/place_limit()   heal missing stop/TP
        +--> ibkr_order.amend_stop()      breakeven stop moves
```
This was hand-rolling its own Saxo REST client before (`_get`/`_post`/
`_patch` against Saxo endpoints directly) rather than using
`saxo_client.py`/`saxo_order.py` — every one of those ~50 call sites got
rewritten, not just the import line. Equity accounting also moved from
EUR (Saxo's account currency) to SEK (IBKR's paper account currency)
natively — see `atos/capital_config.py`'s `forex_risk_equity_sek()` /
`forex_lbo_capital_sek()`.

### futures/runner.py — untouched
Still 100% Saxo (`_get`/`_post` against Saxo's REST API, `saxo_order.py`
for brackets). No IBKR code path exists for this module.

## Position state schema changes

Every migrated module's position-tracking JSON now keys positions by
**IBKR conId** where it used to key by Saxo Uic (`etf_positions.json`,
and `forex_state.json` if/when it's reset — see IBKR_STATUS.md). This
isn't a compatible change: a pre-migration state file's keys are
meaningless post-migration, since they're integers from a different
identifier space that happen to look the same shape (both are plain
integers). Any existing state file needs to be backed up and reset before
a module's first IBKR-executed run, not merged forward.

## Capital/risk config: EUR → SEK

`config/capital.json`'s `forex.risk_equity_eur` / `forex.lbo_capital_eur`
were derived caps for Saxo's EUR-denominated account (27,800 EUR ≈
300,000 SEK, 1,390 EUR ≈ 15,000 SEK, both at ~10.8 SEK/EUR). Since IBKR's
paper account reports natively in SEK, `risk_equity_sek: 300000` /
`lbo_capital_sek: 15000` were added as the *native* values — same real
caps, no FX round-trip. The EUR fields are untouched and still used by
whatever hasn't migrated (`futures_risk_equity_eur()` for
`futures/runner.py`, still on Saxo).

## What's deliberately NOT abstracted

There's no `BrokerInterface`/adapter layer unifying Saxo and IBKR calls
behind a common API, even though `core/broker_interface.py` exists in this
codebase (used by `binance_bot/`). The original `IBKR.zip` draft included
a second, class-based `ibkr_bot/` package built against that interface —
discarded, because none of the real call sites in this codebase (`atos_runner.py`,
`forex/runner.py`, `futures/runner.py`, `saxo_etf_strategy/core/etf_executor.py`)
use `broker_interface.py` at all; they call `saxo_client.py`/`saxo_order.py`'s
flat functions directly. Building an abstraction layer nothing would call
into wasn't worth the indirection. If a genuine broker-agnostic layer is
wanted later, it would need every existing call site rewritten to use it —
a much larger, separate project.
