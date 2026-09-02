# US Equities Strategy Playbook

> **Real money — ATOS LIVE STOCKS sleeve (LIVE since 2026-09-03).**
> A separate module — `atos_live_stocks.py` — runs **US Blend only** with
> **30,000 SEK** on the Saxo LIVE SEK sub-account (the same account forex LIVE
> used; forex LIVE entries are now stopped). It shares only that broker login;
> its own ledger / risk state / lock / housekeeping / safeguard / dashboard /
> scheduler / AI env (`live_stocks`). Real orders gate on
> `SAXO_LIVE_STOCKS_CONFIRMED=1` + `LIVE_STOCKS_DRY_RUN=0` (both set) + `--live`
> + not `LIVE_STOCKS_TRADING_HALTED`. See the "Real money" section below and
> [atos_ai_tracker.md](atos_ai_tracker.md).

> **AI observation layer (2026-09-02 SIM, 2026-09-03 live_stocks — shadow, log-only).**
> The forex AI layer also covers this module: an LLM **Trading Journal**
> retrospective on each closed trade, a **shadow Trading Copilot** score on
> every US Reversion entry (SIM), and a **shadow basket-ranker** that logs a
> re-ranked US Blend offense basket next to the deterministic pick. All
> OBSERVE/LOG only — nothing changes a stocks trade, the rebalance basket, or
> any sizing; `can_apply_decision` is never called for stocks (AST-enforced).
> SIM: `config/ai.json` `"stocks": {"enabled": …}`. Real-money sleeve:
> `"stocks_live": {…}` (own block, ARMED). See [atos_ai_tracker.md](atos_ai_tracker.md).

**Module**: `atos/` + `atos_runner.py` (SIM) · `atos_live_stocks.py` (real money, US Blend only)  
**Universe**: **424** US stocks spanning all 3 major indices — Dow-30 (30/30),
Nasdaq-100, and the large/mid-cap core of the S&P 500 (`atos/universe.py`
`US_TICKERS` = `SP500_TICKERS` + `HIGH_GROWTH_TICKERS` + `NASDAQ100_DOW_TICKERS`)  
**Strategies**: 2 concurrent on SIM (Momentum Blend + Mean Reversion); **US Blend only** on the real-money sleeve  
**Capital**: SIM = 85% of SIM account (split 50/50); real money = 30,000 SEK (US Blend sleeve, capped)  
**Scheduled**: 06:00 PKT daily (main) — `ATOS Daily Run` → `daily_run.py` →
`atos_runner.run_cycle()`, the only Task Scheduler entry point confirmed
live 2026-08-22. `run_intraday_cycle()` exists in `atos_runner.py` but no
scheduled task calls it (`python atos_runner.py intraday` is manual-only) —
this doc's older "5× intraday" note describes an intent, not current
Task Scheduler reality; re-verify before relying on it.  
**Price source**: Saxo's own live quotes only (2026-08-22, explicit user
direction) — `download_universe()` and every `_rate_to_sek()` call fetch
from Saxo's `/chart/v3/charts` and FX quotes via `saxo_history.py`/
`saxo_fx.py`, not Yahoo. A `data_loader.py` comment claiming Saxo's SIM
has no historical stock data was stale/never re-verified — confirmed live
it does. Yahoo remains correct for `data_loader.py`/`backtest_*.py`
(genuinely historical/backtest code) and `k4_export.py` (tax export).
Saxo **SIM won't quote stocks** via `/trade/v1/infoprices` or return stock
positions (NoAccess), so `stocks_dashboard.py` (SIM) backfills the **last
daily close** from `saxo_history.fetch_daily_bars` for any position Saxo
won't price (paper fills etc.) — labelled as such; not real-time but a real
number. LIVE has real stock quotes.  
**Last updated**: 2026-09-03 (LIVE STOCKS live + go-live schedule + book_state
tracking + SIM dashboard price backfill; 3-index universe; price-source note
2026-08-22; original audit 2026-08-19)  

---

## Audit — 2026-08-19

Full review of all stock strategies. **US Blend is healthy. US Reversion had two live
defects**, both from the same root cause: a value *derived* from something that later
changed underneath it, with no ceiling and no test guarding the result.

| # | Finding | Severity | Status |
|---|---------|----------|--------|
| 1 | Intraday reversion budget was never capped — daily path capped, intraday path not | **High** | Fixed `9af26d3` |
| 2 | Reversion slot count scaled with universe → 38 slots vs the 2–3 validated | **High** | Fixed `9af26d3` |
| 3 | US Blend rebalanced weekly; cost-sensitivity favours fortnightly | Medium | Fixed `7e7a9bb` |
| 4 | Aug 14 positions oversized (239,788 SEK vs 135,000 cap) | Medium | Resizes at Aug 21 rebalance |
| 5 | `us_reversion.py` docstring wrong on every parameter incl. enabled/disabled | Low | Fixed `9af26d3` |
| 6 | Universe documented as 108 *and* 61; actually 385 | Low | Fixed `9af26d3` |
| 7 | Slot-math test re-implemented the formula it was meant to guard | Low | Fixed `9af26d3` |

### Finding 1 — intraday budget was uncapped

`starting_capital_sek` capping was added to the daily path but not to
`run_intraday_cycle()`, which runs **5× per session**. It used raw
`cash_sek × allocation_pct`, so inflated SIM demo cash could oversize entries — the
same defect that produced the 670-share UNH position:

| SIM cash | Daily path | Intraday path (before) |
|---|---|---|
| 300,000 SEK | 135,000 | 150,000 (1.1×) |
| 1,000,000 SEK | 135,000 | 500,000 (3.7×) |
| 10,400,000 SEK | 135,000 | **5,200,000 (38×)** |

Both paths now use `min(cash × pct, starting_capital × max_deploy × pct)`.

**Lesson:** the original fix changed two call sites and missed a third. A cap belongs in
one function that every path calls, not repeated at each site.

### Finding 2 — slot count scaled with the universe

See [Why `max_slots` exists](#why-max_slots-exists-added-2026-08-19). Worst part was the
failure mode: the 13% of the universe that became unbuyable was dropped at *order* time,
not signal time, so it never appeared in any log, dashboard, or rejected-signal list.

**Lesson:** a derived value needs a ceiling whenever what it derives from can grow.
`max_universe_pct` alone cannot express "never shrink a slot below tradeable size".

### Still open

- **US Reversion has zero closed trades** since going live 2026-08-08. Not a bug — the
  scanner genuinely finds no signals (verified 2026-08-19), and entry needs four
  conditions at once. But it means the strategy is **completely unvalidated live**, and
  until Finding 2 was fixed, 13% of its universe was unreachable.
- **Live params drift from the validated winner** — see
  [Validated vs live parameters](#validated-vs-live-parameters).

---

## Universe

**424 US stocks** — defined in `atos/universe.py` (`US_TICKERS`), the
deduplicated union of three source lists:

| List | ~Count | What |
|---|---|---|
| `SP500_TICKERS` | 385 | hand-curated large/mega-cap S&P 500 core, sector-organised |
| `HIGH_GROWTH_TICKERS` | +22 net-new | high-volume growth names (2026-08-28, user request) |
| `NASDAQ100_DOW_TICKERS` | +17 net-new | Nasdaq-100 gap-fill + DOW Inc (2026-09-03, user request) |

Coverage: **Dow Jones Industrial Average 30/30**, the **Nasdaq-100**, and the
large/mid-cap core of the **S&P 500** (the small-cap S&P tail is deliberately
not carried). Deliberately excluded: `GOOG` (dual-class duplicate of `GOOGL` —
would double-weight Alphabet in the rank-weighted Blend sleeve) and `EA` (went
private 2026-08-04, delisted).

> The universe grew 61 → 385 → 424. Anything that *derives* a value from
> `len(US_TICKERS)` scales with it — the reversion slot count is capped at
> `max_slots = 6` so it does **not** (see the slot ceiling below).

After any `US_TICKERS` edit, run `lookup_missing.py` (SIM UICs → `data/instrument_map.csv`)
**and** `lookup_instruments_live.py` (LIVE UICs → `data/instrument_map_live.csv`,
operator-only) — SIM and LIVE UICs are re-derived independently.

### Selection criteria
- Daily dollar volume > $200M (sufficient liquidity for the position sizes we trade)
- Market cap > $30B (avoids small/micro-cap volatility)
- Established companies with at least 5 years of price history

### Excluded categories (deliberate)
| Category | Reason |
|----------|--------|
| REITs | Different tax/distribution mechanics; dividend timing conflicts with momentum logic |
| Small E&P (oil & gas) | Extreme commodity exposure; correlates with CL futures we already trade |
| Speculative biotech | Binary FDA events cause 40–80% overnight gaps; untradeable with rule-based stops |
| Penny stocks / low volume | Slippage kills edge at our sizing |

### Sector breakdown (approximate)
Technology, Comm Services, Consumer Discretionary, Consumer Staples, Financials, Healthcare, Industrials, Energy (majors only), Semiconductors, Materials, Utilities.

---

## Capital Allocation

```
US Equities total:   85% of account
  ├─ US Blend:       50% of 85% = 42.5% of account
  │    8 slots (6 offense + 2 defense)
  │    Each position ≈ 6.25% of Blend sleeve
  └─ US Reversion:   50% of 85% = 42.5% of account
       6 max slots (10% of universe, clamped to max_slots = 6)
       Each position ≈ 8.3% of Reversion sleeve
```

All percentages live in `config/capital.json` — the **single source of truth**. Never edit strategy code to change allocation; change only the JSON.

---

## Real money — ATOS LIVE STOCKS sleeve

**Entry point**: `atos_live_stocks.py` (2026-09-03)  
**Strategy**: **US Blend only** — `LIVE_STOCKS_ALLOWED_STRATEGIES = {"US Blend"}`,
never a CLI arg. `atos_runner._place_us` raises if `_sx()=="live"` and the tag
isn't "US Blend". US Reversion / intraday / the legacy per-market engine never
run here.  
**Capital**: 30,000 SEK — `config/capital.json` `strategies.stocks_live.risk_equity_sek`.
Budget each cycle = `min(pooled Saxo TotalValue, 30k) × (1 − 10% cash buffer)` —
never the pooled raw.  
**Account**: the Saxo LIVE **SEK sub-account** (`1070996INET`) — the same one
forex LIVE used. Forex LIVE entries are now stopped (`LIVE_ALLOWED_STRATEGIES =
set()`); its 5 open positions wind down on their stops. Saxo pools margin +
positions across sub-accounts, so **every snapshot is filtered by AccountKey
AND `AssetType=="Stock"`**.

### Separate module — shares only the broker login

| Concern | SIM stocks (`atos_runner`) | LIVE stocks (`atos_live_stocks`) |
|---|---|---|
| Ledger | `data/atos_live.db` | `data/atos_live_stocks.db` (`ATOS_DB_PATH`) |
| Risk state | `data/atos_risk_state.json` | `data/atos_live_stocks_risk_state.json` |
| Blend clock | `data/us_momentum_state.json` | `data/us_momentum_state_live.json` |
| Process lock | `proc_lock.ATOS_LOCK` | `proc_lock.ATOS_LIVE_STOCKS_LOCK` |
| Instrument map | `data/instrument_map.csv` | `data/instrument_map_live.csv` (USD-only) |
| Housekeeping / safeguard | `housekeeping.py` / `safeguard.py` | `housekeeping_live_stocks.py` / `safeguard_live_stocks.py` |
| Dashboard | `stocks_dashboard.py` | `live_stocks_dashboard.py` |
| Scheduler | `ATOS Daily Run` | `ATOS Stocks LIVE Daily Run` (19:20 PKT) + `ATOS Stocks LIVE Exit Check` (23:30 PKT) — both inside US market hours |
| AI env | `sim` | `live_stocks` (own `stocks_live` config block) |

### Safety rails (mirror forex LIVE)

- **50% pooled-margin gate** on `MarginUtilizationPct` — fails **open** on a lookup miss.
- **Daily-loss cap** (~3% ≈ 900 SEK/day) computed against the **30k base + the
  sleeve's own ledger** — *not* `atos.risk.STARTING_CAPITAL_SEK` (that 10.4M SIM
  constant reads ~100% drawdown on a fresh empty LIVE risk file). Breach → exits-only.
- `housekeeping_live_stocks` — 2-snapshot agreement gate, degraded-`/orders`
  detection, orphan-working-order scan.
- `safeguard_live_stocks` — conservative 8% protective stop on a naked stock,
  re-verified against a fresh snapshot. **LIVE never auto-closes** an untracked
  position — it escalates via `attention.raise_attention("live_stocks:…")`.

### Rollout

**Went LIVE 2026-09-03** (explicit user instruction, Phase 1 observe cut short —
the first observe scan on the full 424-ticker universe produced a clean 6-name
basket). Both `.bat` wrappers now pass `--live`; `SAXO_LIVE_STOCKS_CONFIRMED=1`
and `LIVE_STOCKS_DRY_RUN=0` set as User env vars.

`run()` still forces `dry_run` unless **all** of: `--live` **and**
`SAXO_LIVE_STOCKS_CONFIRMED=1` **and** `LIVE_STOCKS_DRY_RUN=0` **and** not
`LIVE_STOCKS_TRADING_HALTED` — so the env vars remain the real gate, not the
`.bat`. Kill switch (any one): `setx LIVE_STOCKS_DRY_RUN 1`, remove
`SAXO_LIVE_STOCKS_CONFIRMED`, create a `STOP_TRADING` file, disable the Scheduler
task, or set `LIVE_STOCKS_TRADING_HALTED=1`.

Not done before go-live (the user chose to skip): a manual 1-share test order,
and the ~2-week AI shadow-evidence review (`stocks_live` AI observing started
the same day). The 8% broker stop + 20% TP bracket, 50% margin gate, 10% cash
buffer, daily-loss cap and `safeguard_live_stocks` are the live backstops.

Earlier observe mode (still the behaviour without the env vars):
`run_us_momentum(observe=True)` fires the AI hooks + blend-target notification
and appends every would-be order to `data/us_blend_live_would_be_orders.jsonl`
with a would-be AI entry card — zero real orders, no DB row, no `last_rebalance`
stamp.

### AI (log-only, governance unchanged)

`ai/config.py` `_AI_SHADOW_ACCOUNTS` includes `"live_stocks"`; `_AI_ACTING_ACCOUNTS`
does **not** → `can_apply_decision("live_stocks")` is `False` forever (AST-checked).
Own `config/ai.json` `stocks_live` block (journal + basket-ranker, **armed**) —
LIVE stocks trades feed the AI Trading Journal (weighed as real money) and the
shadow basket-ranker, separate rows from the SIM study.

**Both-books context (2026-09-03):** every rebalance scan (SIM *and* LIVE) hands
the basket-ranker a `book_state` — for each of `sim` and `live_stocks`: its
`last_rebalance`, `days_since`, `next_due_in_days`, and current `holdings`
(`atos_runner._blend_book_state()`, from each book's own state file + ledger).
Logged on the `data/ai_basket_shadow.jsonl` row and in the LLM payload; the
`_SYSTEM` prompt tells the AI to note when the two books have drifted materially
(different names, or one overdue) — **observe only, it never changes the pick**.
The two books run on independent 14-day rebalance clocks (`us_momentum_state.json`
vs `us_momentum_state_live.json`).

### Commands

```powershell
python atos_live_stocks.py --info          # SIM vs LIVE Uic diff -- only the rows that differ / are missing (no orders)
python atos_live_stocks.py                 # cycle (real if --live + both env vars; else observe-only)
python atos_live_stocks.py --live          # real orders (needs SAXO_LIVE_STOCKS_CONFIRMED=1 + LIVE_STOCKS_DRY_RUN=0, US market open)
python atos_live_stocks.py --exits-only    # manage open positions only, no new buys
python atos_live_stocks.py --dashboard     # live view (refreshes 30s); add --fast for 5s
python lookup_instruments_live.py          # OPERATOR: build/refresh the LIVE instrument map (hits LIVE ref-data); re-run after any US_TICKERS change
powershell -ExecutionPolicy Bypass -File setup_scheduler_live_stocks.ps1   # OPERATOR: (re)register the 2 tasks -- Daily 19:20 PKT, Exit 23:30 PKT
```

---

## Strategy 1 — US Momentum Blend (LIVE)

**File**: `atos/us_momentum.py`  
**Type**: Cross-sectional momentum + low-volatility defensive blend  
**Status**: LIVE — running since 2026-08-07  
**Rebalance**: Every 14 calendar days (`REBAL_DAYS = 14`) — see [Rebalance Cadence](#rebalance-cadence-why-14-days) below  

### Concept
Two uncorrelated factors are blended in one portfolio sleeve:
1. **Momentum factor (offense)**: top-6 stocks by 6-month risk-adjusted return (momentum/volatility ratio) — captures stocks already trending strongly
2. **Low-volatility factor (defense)**: 2 stocks with lowest 60-day realized volatility (above their EMA200) — provides ballast when momentum stocks correct

Factor correlation: ~0.44 — low enough that the blend outperforms either factor alone on a risk-adjusted basis.

### Entry
| Type | Logic |
|------|-------|
| **Offense (6 slots)** | Top-6 stocks by `return_120d / vol_60d`, where return > 5% AND price > EMA(200) |
| **Defense (2 slots)** | Lowest-vol 2 stocks with price > EMA(200) (no momentum minimum required) |

### Risk-Off Gate
Daily check: if SPY/QQQ index closes below its 200-day SMA, all Blend positions are sold and cash is held. Re-enters when index recovers above the SMA.

### Exit
Triggered by the fortnightly rebalance: if a stock is no longer in the top-6 momentum or top-2 defense selection, it is sold and replaced. No individual stop-loss — the rebalance is the exit mechanism.

### Position Sizing
Equal-weight within the sleeve. Budget = 50% of live SIM cash / 8 slots.

### Backtested Performance (10y, 2016–2026)
| Metric | Value |
|--------|-------|
| CAGR | 24.4% |
| Sharpe | 1.30 |
| Max Drawdown | 21.3% |
| Universe | 385 stocks |

### Live Performance (2026-08-07 → 2026-08-19, SIM)

| Metric | All trades | Excluding the oversized UNH trade |
|--------|-----------|---------------|
| Closed trades | 31 | 30 |
| Win rate | 29.0% | 30.0% |
| Gross profit | 77,057 SEK | 77,057 SEK |
| Gross loss | −87,930 SEK | −20,831 SEK |
| Net P&L | **−10,872 SEK** | **+56,226 SEK** |
| Profit factor | 0.88× | **3.7×** |
| Worst trade | −67,099 SEK (UNH) | −9,683 SEK (AMD) |
| Best trade | +41,340 SEK | +41,340 SEK |

The single oversized UNH fill accounts for **76% of all gross losses**. The next worst
loss is −9,683 SEK — an order of magnitude smaller, and in line with normal position
sizing. (A second, correctly-sized UNH trade lost only −3,786 SEK.)

**Read this carefully before concluding the strategy is losing.** The entire net loss is
one trade. UNH was sized at 670 shares on a 300,000 SEK account because Saxo SIM's
`CashBalance` included the full demo credit and `blend_budget = cash × 0.5` was computed
off it. That was a **sizing bug, not a signal failure** — fixed in `9c2482d`.

A 29% win rate is also normal for cross-sectional momentum: the strategy is designed to
take many small losses and a few large wins. Judge it on profit factor, not win rate.

> Live sample is 12 days and 31 trades — far too small to conclude anything either way.
> The backtest figures above remain the better estimate of expected behaviour.

### Parameters
| Param | Value |
|-------|-------|
| Universe | 385 stocks (`atos/universe.py`) |
| Momentum lookback | 120 trading days (≈ 6 months) |
| Offense slots | 6 |
| Defense slots | 2 |
| Min momentum return | 5% |
| Trend filter | Price > EMA(200) |
| Rebalance period | 14 calendar days |
| Capital | 50% of live SIM cash, capped at 135,000 SEK |

### Rebalance Cadence — why 14 days

Changed from 7 → 14 on **2026-08-19**. Swept 4d / 7d / 10d / 15d / 21d / monthly /
quarterly through the production engine (`backtest_us_momentum.py`) on the 10-year
panel — 385 names, TOPN=8, daily regime overlay + vol targeting.

**Full sample:**

| Interval | CAGR | Sharpe | MaxDD |
|----------|------|--------|-------|
| 4 days | 15.6% | 0.75 | 37.7% |
| 7 days (old) | 21.4% | 0.98 | 33.6% |
| **15 days** | **25.9%** | **1.13** | **30.6%** |
| 21 days | 20.5% | 0.94 | 37.1% |
| monthly | 14.1% | 0.68 | 48.8% |
| quarterly | 8.8% | 0.47 | 44.9% |

A single-sample peak is usually curve-fit, so it was re-scored by **mean rank across
8 independent tests** (3 sub-periods × 5 TOPN settings):

| Interval | Mean rank | Worst rank | Verdict |
|----------|-----------|------------|---------|
| 15 days | 2.31 | 4 | robust |
| 7 days | 2.56 | 6 | unstable |
| 21 days | 2.56 | 4 | robust |
| 10 days | 3.50 | 6 | unstable |
| monthly | 5.31 | 7 | avoid |
| quarterly | 6.31 | 7 | avoid |

The top three are a **statistical tie** (0.25 rank spread = noise). The tiebreak is
**trading cost** — weekly decays far faster as costs rise:

| Interval | 0.11%/side | 0.20% | 0.35% | 0.50% |
|----------|-----------|-------|-------|-------|
| 7 days | 0.98 | 0.84 | 0.62 | 0.39 |
| **15 days** | **1.13** | **1.04** | **0.90** | **0.75** |
| 21 days | 0.94 | 0.88 | 0.78 | 0.67 |
| monthly | 0.68 | 0.63 | 0.55 | 0.46 |

At ~37,500 SEK per slot, Saxo's real all-in cost lands near 0.15–0.25%/side — squarely
in the band where weekly starts bleeding and the fortnightly cadence does not. It also
halves commission drag and portfolio churn.

**14 rather than 15** so rebalances land on the same weekday each time instead of
drifting through the week.

> Monthly and slower ranked 5th–7th in **every** test. Do not extend past ~21 days.

---

## Strategy 2 — US Mean Reversion (LIVE ON SIM)

**File**: `atos/us_reversion.py`  
**Type**: Short-term dip buying on oversold blue-chips  
**Status**: LIVE ON SIM — enabled 2026-08-08; observe 6–8 weeks before real capital  

### Concept
Short-term price dips in quality companies — measured by RSI < 33 (now 38 after recalibration) and a price decline > 5% below SMA(20) — statistically revert to the mean within 3–10 trading days. The EMA(200) filter ensures we only buy dips in companies in a long-term uptrend, not falling knives.

The edge: quality companies with temporary oversold conditions in an uptrend have a strong baseline of institutional buyers who step in on dips. We ride that reversion.

### Entry Conditions (all required)
| Condition | Value | Purpose |
|-----------|-------|---------|
| RSI(14) | < 38 | Oversold momentum |
| Price vs SMA(20) | > 5% below | Meaningful dip, not noise |
| Volume | > 1.5× 20-day average | Confirms selling climax |
| Price vs EMA(200) | Above | In long-term uptrend |
| Sleeve drawdown | < 10% | Pause if sleeve down 10% |
| Daily loss cap | < 3% account loss today | Hard safety gate |

### Exit Conditions (first hit)
| Condition | Meaning |
|-----------|---------|
| RSI(14) > 60 | Recovery complete |
| Price ≥ SMA(20) | Target hit (dip filled) |
| -4% hard stop | Entry stop (capital.json) |
| 10 trading days | Time-stop |

### Intraday Extension
The reversion scanner also runs **5 times per session** (19:00, 20:30, 22:00, 23:30, 00:30 PKT) via `atos_runner.py intraday`. This version adds:
- **Opening gap filter**: skip if today's gap-down > 8% (likely earnings/scandal)
- **Total drop filter**: skip if total intraday drop > 15%
- **News keyword filter**: scan last 24h headlines for bankruptcy/fraud/scandal keywords → skip
- Uses live 5-minute bars from yfinance, with volume scaled to elapsed session fraction

### Honest Out-of-Sample Validation (2026-08-08)

| Period | Sharpe | WR | MaxDD | Trades | CAGR |
|--------|--------|-----|-------|--------|------|
| IS: Apr 2024 – Jun 2025 | 2.08 | 66% | 12.5% | 64 | 30% |
| **OOS: Jun 2025 – Aug 2026** | **2.39** | **70%** | **5.9%** | **23** | **47%** |

OOS was never touched during parameter selection. Verdict: **5/5 — edge survives clean OOS test.**

### Validated vs live parameters

The OOS result above validated a *specific* parameter set. Live has drifted from it on
four axes — worth knowing when judging live results against that Sharpe 2.39:

| Param | Validated winner | Live now | Note |
|-------|-----------------|----------|------|
| RSI entry | 33 | **38** | Deliberate: extended grid top-1 (120 trades, Sharpe 1.73, WR 60%) — looser entry, more trades, lower Sharpe |
| Dip | 5% | 5% | ✓ matches |
| Volume | 1.5× | 1.5× | ✓ matches |
| Stop | 5% | **4%** | From `capital.json`; tighter than validated |
| Sleeve DD cap | 15% | **10%** | From `capital.json`; more conservative |
| Concurrent positions | 3 (grid was `[2,3]`) | **6** | Was **38** before 2026-08-19 — see Finding 2 |

The stop and DD-cap changes are conservative (they can only reduce risk), and the RSI
change was a deliberate, separately-backtested trade-off. **The position count was not
deliberate** — it drifted silently when the universe grew. 6 is still 2× the validated
3; it was chosen to keep slot size tradeable rather than to match the grid. If live
results disappoint, this is the first parameter to bring back toward 3.

### Live status (as of 2026-08-19)

**Zero closed trades. Zero open positions.** Live since 2026-08-08 (11 days).

This is *not* a malfunction — running the scanner on 2026-08-19 produced no signals at
all. Entry requires RSI<38 **and** a 5% dip **and** 1.5× volume **and** price above
EMA200 simultaneously, which is rare in a strong uptrend (exactly the regime that has
been feeding the momentum Blend). Backtest trade frequency implies roughly 2–5 trades
per month, so a quiet 11 days sits within the expected range.

What it does mean: **the strategy has no live validation whatsoever.** Nothing about its
real-world fills, slippage, or commission drag has been observed. Treat the OOS Sharpe
as an expectation, not a track record — and note that until the Finding 2 fix, 13% of
its universe could not have been bought even if it had signalled.

### Position Sizing
- Budget: 50% of live SIM cash, **capped at `starting_capital_sek × max_deploy_pct`** so SIM demo credit cannot inflate sizes (135,000 SEK today)
- Max slots: `CAP.reversion_slots(universe_size)` = `min_slots (2) ≤ round(universe × 10%) ≤ max_slots (6)` → **6 slots**
- Each position: `budget / slots` = **22,500 SEK**

#### Why `max_slots` exists (added 2026-08-19)

Slot count used to derive from universe size alone. When the universe grew 61 → 385,
10% silently became **38 concurrent slots** against the **2–3** the strategy was
validated at. Each slot fell to ~3,550 SEK (~$370), with two consequences:

| | 38 slots (before) | 6 slots (now) |
|---|---|---|
| Slot size | 3,553 SEK (~$370) | 22,500 SEK (~$2,344) |
| Universe unbuyable at 1 share | **50 tickers (13%)** | 2 tickers (1%) |
| Broker minimum commission | large fraction of a $370 position | negligible |

The unbuyable names — NVR, AZO, ASML, LLY, BLK, GS, COST… — were dropped at order
time (`slot too small for 1 share — skip`), never surfacing as rejected signals.
A ceiling is the fix; the 10% rule alone cannot express "don't shrink below tradeable".

### Parameters
| Param | Value | Source |
|-------|-------|--------|
| RSI entry | < 38 | Recalibrated from 33 (doubles trade frequency, same Sharpe) |
| Dip | > 5% below SMA(20) | IS-validated |
| Volume | > 1.5× 20d avg | IS-validated |
| Stop | -4% | capital.json |
| Time stop | 10 trading days | capital.json |
| Sleeve DD cap | 10% | capital.json |
| Max slots | `min 2 ≤ 10% of universe ≤ max 6` → 6 | capital.json (`max_slots`) |
| Capital | 50% of live SIM cash, capped at 135,000 SEK | capital.json |

---

## Stop-Loss Architecture

The system uses a **three-layer stop hierarchy** (managed by `intraday_monitor.py`):

```
Layer 1 — Entry stop:   price from entry in signal (fixed)
Layer 2 — Trailing:     -12% from peak (follows price up)
Layer 3 — Hard floor:   -15% from entry (never exceeded)
```

The 1-second monitor (`intraday_monitor.py`) runs during US market hours (09:30–16:00 ET = 19:30–02:00 PKT). It checks Saxo prices every second and sends sell orders when any layer is breached.

**Circuit breaker**: If price data is unavailable for > 180 seconds, CRITICAL alert fires.

---

## Email Notifications

All notifications go to `heyitskaxhif@gmail.com` automatically.

| Event | Trigger |
|-------|---------|
| **Blend rebalance** | Fortnightly — targets list, offense/defense split, risk-off status |
| **Reversion entry signal** | Per scan — RSI, dip%, vol, BUY vs QUEUED per ticker |
| **Reversion exit** | Per exit — P&L %, P&L SEK, hold days, exit reason |
| **BUY executed** | Per order — strategy, shares, price, value SEK, account balance |
| **SELL executed** | Per order — strategy, shares, price, P&L SEK, account balance |
| **Weekly P&L report** | Fridays — equity, week P&L, open positions, SVG equity chart |

---

## Corporate Events Filter

`atos/corporate_events.py` automatically:
- **Ex-dividend**: exits position 3 days before ex-div date (avoids ex-div gap)
- **Earnings**: skips new entries 2 days before earnings report (avoids binary risk)

Data source: yfinance (free tier, ~75% accuracy on earnings dates).

---

## Key Files

| File | Purpose |
|------|---------|
| `atos_runner.py` | SIM daily orchestrator — `run_cycle()` + `run_intraday_cycle()`; `run_us_blend_live()` wrapper for the real-money sleeve |
| `atos_live_stocks.py` | **Real-money US Blend sleeve** (30k SEK, SEK LIVE account, Phase 1 observe-only) |
| `housekeeping_live_stocks.py` / `safeguard_live_stocks.py` | LIVE stocks reconcile + auto-fix (AccountKey+AssetType filter; never auto-closes) |
| `live_stocks_dashboard.py` | Real-money sleeve dashboard — capital/margin, **LAST SCAN** (blend target basket = offense/defense/target — the "signal"), **REBALANCE CLOCKS** (LIVE vs SIM: last rebalance, days since / next due, current holdings — the two books run on independent 14-day cycles), **SCAN SIGNALS** (this scan's would-be orders, SIM-dashboard layout), open positions, would-be-order history, AI basket-ranker shadow. Reads `data/stocks_live_status.json` (the LIVE analogue of `data/atos_status.json`). `--fast` (5s) / `--once`; ANSI auto-strips when piped. |
| `lookup_instruments_live.py` | Operator: build `data/instrument_map_live.csv` (LIVE Uics, USD-only) |
| `atos/universe.py` | **424-stock** universe (`US_TICKERS` = SP500 + HIGH_GROWTH + NASDAQ100_DOW) |
| `atos/us_momentum.py` | Blend strategy: momentum scoring, rebalance logic, risk-off gate |
| `atos/us_reversion.py` | Reversion strategy: RSI/dip signal, exits, sleeve drawdown cap |
| `atos/intraday_reversion.py` | Intraday scanner: live 5-min bars + bad-news filters |
| `atos/capital_config.py` | Loads `config/capital.json`, typed getters for all allocation values |
| `atos/corporate_events.py` | Ex-dividend + earnings date checker |
| `atos/risk.py` | Kill switch, daily loss cap, ATR sizing, heat gates |
| `atos/notifier.py` | Email notification module — all 6 email types |
| `atos/features.py` | Technical indicators: EMA, ATR, RSI, MACD, Bollinger, Donchian |
| `atos/database.py` | SQLite CRUD for `data/atos_live.db` |
| `atos/learner.py` | Magnitude-aware detector weight updater |
| `config/capital.json` | **Single source of truth for all capital allocation** |
| `intraday_monitor.py` | 1-second stop-loss watchdog during US market hours |
| `atos_dashboard.py` | Live dashboard — http://localhost:8070 |

---

## Backtest Results Summary

### US Momentum Blend — LIVE
```
Universe:  424 stocks (Dow-30 + Nasdaq-100 + S&P 500 large/mid-cap core)
Rebalance: Fortnightly (REBAL_DAYS=14)
Config:    Top-6 momentum + 2 low-vol, daily risk-off, vol-target 15%
CAGR:      24.4% | Sharpe: 1.30 | MaxDD: 21.3%
VERDICT:   LIVE
```

### US Mean Reversion — LIVE ON SIM
```
Universe:  385 stocks (same as Blend)
Hold:      3-10 trading days
Config:    RSI<38, Dip>5%, Vol>1.5x, Stop4%, 6 slots (10% of universe, capped)
IS (2024-2025): Sharpe 2.08, WR 66%, MaxDD 12.5%
OOS (2025-2026): Sharpe 2.39, WR 70%, MaxDD 5.9% — clean OOS pass
LIVE:      0 closed trades as of 2026-08-19 — no live validation yet
VERDICT:   LIVE ON SIM — watch 6-8 weeks before real capital
```

---

## Rejected Strategies (Do Not Revisit)

| Strategy | Why rejected |
|----------|-------------|
| OMX30 / CPH25 momentum | 3y Sharpe 1.81 was bull mirage; 10y Sharpe 0.24, MaxDD 44% |
| US Breakout (per-instrument) | ~1 trade per 2 years per stock; not enough signals |
| ML probability model | Walk-forward OOS AUC 0.52 = coin flip; no edge |
| Residual momentum | Same return as raw momentum; no added value in blend |
| Plain momentum / mom252 / 52-week-high | All beaten by risk-adjusted momentum |

---

## Adding a Third Strategy — not yet (2026-08-19)

**Recommendation: do not add a new stock strategy right now.** Reasons, in order:

1. **Only one of the two existing strategies has produced any live data.** US Reversion
   has zero closed trades. Adding a third means debugging three strategies concurrently
   while the second is still unproven.
2. **Both existing strategies changed this week** — Blend cadence 7d→14d and budget cap;
   Reversion slot ceiling and intraday budget cap. None of those changes has been
   observed through even one full cycle yet.
3. **The audit found two live sizing defects in 11 days of operation.** That rate says
   the priority is hardening what exists, not widening the surface area.

### Revisit when

- US Reversion has **~20 closed trades**, so its live Sharpe can be compared to the 2.39 OOS figure
- Blend has run **3+ rebalances** on the 14-day cadence with correct sizing
- Both have run a month without a sizing or config defect

### What the actual gap is, when that time comes

Every current stock strategy is **long-only**. Blend goes to cash in risk-off; Reversion
simply stops finding entries. So in a sustained downturn all US equity exposure converges
to the same place — flat — and none of it profits. The genuine diversifier is **short or
inverse exposure**, not a third long strategy that would correlate with the other two
precisely when it matters least.

A second candidate is a **different holding period**: Blend is fortnightly, Reversion is
3–10 days. Nothing occupies the multi-month horizon.

> Do not add a strategy because the system feels idle. Idle is a position.

---

## Quick Reference

```powershell
# Run daily cycle
python atos_runner.py

# Run intraday reversion scan (during US market hours only)
python atos_runner.py intraday

# Start intraday stop-loss monitor
python intraday_monitor.py

# Start dashboard
python atos_dashboard.py   # → http://localhost:8070

# ── Real-money US Blend sleeve (Phase 1 observe-only) ──
python atos_live_stocks.py              # observe-only cycle (no real orders)
python atos_live_stocks.py --info       # SIM vs LIVE Uic diff
python atos_live_stocks.py --dashboard  # live_stocks_dashboard.py
python lookup_instruments_live.py       # OPERATOR: refresh the LIVE instrument map after a universe change

# Refresh Saxo token (expires every ~24h)
python set_token.py

# Emergency stop
New-Item -Path "STOP_TRADING" -ItemType File
# Resume:
Remove-Item "STOP_TRADING"
```
