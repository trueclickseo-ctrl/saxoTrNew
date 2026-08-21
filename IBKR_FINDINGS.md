# IBKR Migration — Findings & Final Decision

**Date:** 2026-08-21
**Outcome:** Shares moved to IBKR. Forex, ETFs and futures stay on Saxo.

This is the executive summary. Technical detail lives in
[IBKR_STATUS.md](IBKR_STATUS.md) (test evidence, bugs) and
[IBKR_ARCHITECTURE.md](IBKR_ARCHITECTURE.md) (how the code is built).

---

## 1. What we set out to do

Move the whole trading system from Saxo to IBKR, motivated by:
1. IBKR being better suited to quant/algo trading
2. IBKR's Swedish **ISK** account removing the **K4** tax-reporting burden

Both premises were sound. The second turned out to apply to far less of
the system than expected.

## 2. What actually happened

| Asset class | Result | Reason |
|---|---|---|
| **Shares** | ✅ **Migrated to IBKR** | Works. ISK-eligible. Real benefit. |
| **Forex** | ❌ Stays on Saxo | IBKR Ireland blocks cross-currency spot FX |
| **ETFs** | ❌ Stays on Saxo | EU PRIIPs blocks US-domiciled ETFs |
| **Futures** | ❌ Stays on Saxo | Needs contract-roll logic that doesn't exist yet |

## 3. The three blockers, in detail

### 3.1 Forex — IBKR Ireland regulatory restriction
Every spot FX order on a pair without a SEK leg is rejected:
> `Error 201: FX trade would expose account to currency leverage.`

**IBKR Client Services confirmed in writing** this is a hard regulatory
restriction under IBKR Ireland (IBIE). Retail clients there may only:
convert a positive balance to base currency, reduce debt, or consolidate
debt into base currency. Anything creating a short balance in a
non-base currency is refused. They could **not** confirm that Professional
Client status lifts it.

Testing independently reproduced the exact rule before their reply
arrived. The decisive evidence — same instrument, opposite directions,
while holding a long USD balance:

| Order | Effect | Result |
|---|---|---|
| **BUY** USDJPY | long USD, short JPY (none held) | rejected |
| **SELL** USDJPY | short USD (45,000 held), long JPY | **filled** |

So the rule is *"you may not create or increase a short balance in a
currency you don't hold"* — not "the pair must contain SEK", which was
the first (wrong) hypothesis. Only 2 of the 34 pairs in the strategy
universe involve SEK, so the strategy cannot run.

Ruled out by testing: order type, order size (100 → 20,000 units),
trading permissions, stale Gateway session, market data.

### 3.2 ETFs — EU PRIIPs / missing KID
US-domiciled ETFs are rejected outright:
> `Error 201: Customer Ineligible ... This product does not have a KID in
> English or in a language approved for your country.`

EU retail clients cannot buy packaged products without a Key Information
Document, which US ETFs don't produce. **SPY and XLV both refused.**

UCITS ETFs *are* fine — `IWDA` was accepted. But the strategy's
`sector_rotation` logic is built on the 11 US SPDR sector ETFs
(XLV/XLF/XLE/…), and European sector-ETF coverage is thinner and less
liquid. Porting it means redesigning and re-validating the universe, not
swapping symbols.

### 3.3 Futures — no continuous product
Saxo trades ES/NQ/GC etc. as continuous, non-expiring CFDs. IBKR only
offers real expiring futures, so an IBKR version needs contract-roll
logic the codebase has never needed. Left on Saxo pending a design
decision.

## 4. The tax reality — the key strategic finding

**No Swedish tax wrapper can hold forex, at any broker.**

- **ISK**: limited to instruments admitted to trading on a regulated
  market. Pure currency positions are speculative trading, outside the
  ISK framework. CFDs/derivatives excluded.
- **KF (kapitalförsäkring)**: permits *some* derivatives (warrants, turbo
  warrants, Mini Futures, certificates) but **CFDs and forex are
  excluded too**. Moot regardless — IBKR doesn't offer KF, which requires
  a Swedish insurance company as legal owner.

**Therefore the ISK/no-K4 motivation only ever applied to shares and
ETFs.** Forex was always going to require a taxable account and K4
reporting, whichever broker is used. That single fact reframes the whole
project: the migration's tax benefit was never available for the forex
book, so keeping forex on Saxo costs nothing in tax terms.

## 5. Cost comparison (for the forex decision)

If forex *were* moved to IBKR it would have to be via Forex CFDs
(same commission schedule as spot, plus benchmark ±1.5% financing).

Round-turn cost, EURUSD, using the least favourable Saxo assumption
(Classic 1.1 pips) vs IBKR (0.19 pips + USD 2.00/order minimum):

| Notional | IBKR | Saxo | Cheaper |
|---:|---:|---:|:--|
| $10,000 | $4.19 | $1.10 | **Saxo 3.8×** |
| $25,000 *(typical here)* | $4.48 | $2.75 | **Saxo 1.6×** |
| ~$44,000 | ≈ equal | ≈ equal | — |
| $100,000 | $5.90 | $11.00 | IBKR 1.9× |

**Crossover ≈ $44,000 per trade.** IBKR's headline rate is excellent, but
the **$2/order minimum dominates below ~$100k notional**, and this
strategy sizes at ~$25–30k. Generic "IBKR is cheapest" broker comparisons
rank headline rates and miss this. IBKR only wins if position sizes
roughly double.

### Cost warning worth acting on, independent of broker
Commission drag against the 300,000 SEK (~$31,700) forex book:

| Round turns/month | IBKR CFD | Saxo |
|---:|---:|---:|
| 10 | 1.8%/yr | 0.8%/yr |
| 25 | 4.5%/yr | 1.9%/yr |
| 50 | **9.0%/yr** | 3.8%/yr |
| 100 | **18.0%/yr** | 7.6%/yr |

The config allows **201 concurrent slots** across 10 strategies. At even
moderate turnover, costs consume a large share of returns before
financing or slippage. Worth confirming the backtests modelled per-trade
costs at these levels — a many-small-trades design is exactly where fixed
minimums do most damage.

## 6. Methodological lesson

**Contract resolution proves nothing about tradability.** This caught us
twice — FX and US ETFs both resolved perfectly through
`find_instrument()` and were then refused at order time, for completely
different regulatory reasons.

Any future instrument type must be validated with a **real order-acceptance
test** (a far-from-market limit order that cannot fill is enough), not a
successful symbol lookup.

## 7. Bugs found along the way

11 real bugs were found and fixed, several of which would have caused
silent damage in live trading. The most serious:

- `place_with_stop()` reported **success on rejected orders** —
  `ib.placeOrder()` doesn't raise for broker-side rejections, so every
  caller would have recorded positions that were never opened.
- A **rejected entry left its stop/TP legs live** as orphaned orders that
  could later fill and open an unintended position from nothing.
- Runner-driven exits didn't cancel the original bracket's resting legs —
  same orphan risk, different trigger.
- `find_instrument()` **crashed on ambiguous futures** and silently failed
  on every non-US stock (unparsed explicit symbol form).
- A thread-unsafe price fetcher driving `ib_async` from an 8-thread pool.

Full list in [IBKR_STATUS.md](IBKR_STATUS.md).

## 8. What was kept vs reverted

**Kept:**
- `ibkr_client.py`, `ibkr_order.py`, `ibkr_price_service.py`,
  `ibkr_history.py`, `lookup_instruments_ibkr.py`,
  `resolve_etf_universe_ibkr.py`
- `atos_runner.py` on IBKR; `instrument_map.py`'s `broker=` parameter
- `requirements.txt` additions (`ib_async`, `torch`)

**Reverted to Saxo:** `forex/runner.py`, `forex/notifier.py`,
`saxo_etf_strategy/core/etf_executor.py`, `etf_state.py`,
`etf_positions.json` (the 3 real XLV/XLF/XLE positions restored),
`atos/capital_config.py`, `config/capital.json`.

Reverted work is preserved in git history: forex → `93d84bd`
(+ `50a0068`, `fe5d5ed`); ETF → `d16bbfc`, `1f8fc18`.

## 9. Open items

1. **Nordic ticker mapping** — `VOLV-B` doesn't resolve; IBKR uses
   `VOLV B` (space, not hyphen) for Nordic share classes, while
   `strip_suffix()` emits the Yahoo hyphen form. Affects OMX30 names in
   `data/instrument_map_ibkr.csv`.
2. **`data/instrument_map_ibkr.csv` doesn't exist yet** — must run
   `python lookup_instruments_ibkr.py` (after fixing #1) before
   `atos_runner.py` can place IBKR orders.
3. **`atos_runner.py` end-to-end run** — individual IBKR calls verified,
   full daily cycle never executed live. Do this before scheduling it.
4. **Paper account cleanup** — FX testing left two virtual FX positions
   (USD.SEK +45,000, USD.JPY −20,000) that can't be closed, because
   closing them is blocked by the same restriction. NLV intact
   (999,862 SEK vs 1,000,000 start). A Client Portal paper-account reset
   clears them.
5. **Forex CFD permission** is pending approval. Not needed under the
   current decision; harmless to leave.
