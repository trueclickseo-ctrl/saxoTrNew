# Forex Strategy Playbook

**Module**: `forex/`  
**Universe**: 117 FX pairs — 34 majors/crosses/Scandi (live trading candidates) + 83
EM/exotic crosses added 2026-08-21 for **SIM-only** broad testing (see Audit below).
LBO trades a separate, smaller 28-pair universe — majors/crosses only, EM/exotic
pairs deliberately excluded (wider spreads don't suit a tight 2:1 RR day-trade).  
**Strategies**: 11 active (9 rule-based swing + 1 deep learning swing + 1 day-trading breakout)  
**Max slots**: swing strategies scan and can hold a position in every pair in the
active universe (no artificial cap below universe size) + **28 day-trading** (LBO,
independent book, one slot per LBO pair)  
**Swing risk per trade**: 0.5% of account equity (cut from 1% 2026-08-22 —
see Audit below; margin relief, not a strategy change)  
**Day-trading capital**: 15,000 SEK dedicated, 1.5% risk per trade  
**Stop-loss + take-profit**: every strategy places BOTH as native Saxo orders
atomically at entry (2026-08-22) — a true OCO bracket via
`saxo_order.place_with_stop()`, not dependent on a scheduled run to add
protection later. See "Strategy Comparison" below for each strategy's
take-profit rule.  
**Risk gates — SIM-testing state, NOT the intended live config**: portfolio
heat cap and currency-exposure cap are both currently disabled (raised to
effectively unlimited) for full SIM testing across the expanded universe.
**Both must be reinstated with real values before trading live capital** —
see Audit 2026-08-22.  
**Price source**: live SIM orders, position sizing, and `forex_dashboard.py`
use **Saxo's own live quotes only** (2026-08-22, explicit user direction) —
`forex/runner.py`'s and `forex_dashboard.py`'s `_eur_per_unit()` triangulate
EUR conversion rates via a Saxo-traded EUR{ccy}/USD{ccy} pair, with no
Yahoo fallback. Yahoo (`fx.py`, `yfinance`) is used only for historical
data — `backtest_forex_universe.py` and similar.  
**Concurrency**: every `--live` invocation of `forex/runner.py` (and
`futures/runner.py`, and `intraday_monitor.py`'s forex/futures checks)
serializes through a shared file lock (`proc_lock.py`) — two overlapping
scheduled triggers now wait for each other instead of racing on
`forex_state.json`. Added 2026-08-24 after a real race caused duplicate
closes and orphaned phantom positions (see Audit below).  
**Margin gate**: every new entry (all strategies, including LBO) checks
Saxo's own live margin utilization first and refuses to place an order
above 50% utilization, reserving headroom for every other strategy/module
sharing the account. Added 2026-08-24 — see Audit below.

---

## Audit — 2026-08-24

Fourth pass, triggered by the user reviewing a batch of "Gap Fill" trade
emails and asking for a deep audit. Found a chain of related, serious
issues — not cosmetic.

| # | Finding | Severity | Status |
|---|---------|----------|--------|
| 1 | `strategy_gap.py`'s `should_exit()` checked the day's cumulative High/Low against `gap_target` instead of the current price — once price wicked through the target for an instant (common at the thin Monday reopen), every later check that day still saw it as "filled" even after price fully reverted, closing at a stale, often-losing price while logging `gap_filled` (a win) | **Critical** | Fixed — checks current close instead; 18/18 same-morning trades had been mislabeled this way |
| 2 | `ATOS Forex Gap Fill` and `ATOS Forex Gap Monday Early` fire at the exact same instant (Mon 03:00 PKT) with no coordination — two processes could both load stale state and both act on the same position | **Critical** | Fixed — `proc_lock.py`, shared file lock around every live run |
| 3 | `intraday_monitor.py` (a separate process) independently reads/writes the same `forex_state.json`/`futures_state.json` as the runners — invisible to fix #2's forex-runner-only lock. Confirmed live: this caused the SAME real gap-fill position to be "closed" 3 times by overlapping runs; the 2nd/3rd closes had nothing left to close and opened brand-new untracked phantom positions instead (CHFMXN x2, GBPMXN x2, zero stop/TP) | **Critical** | Fixed — `proc_lock.py` extended into `intraday_monitor.py` and `futures/runner.py`; the 4 live phantom positions closed manually |
| 4 | 10 legacy positions from 2026-08-17–21 (before the capital-sizing cap existed) were still open — ~24M EUR notional against an intended ~27,800 EUR trading capital — pushing real Saxo margin utilization to 98.56%, which would have blocked LBO and every other module from trading, independent of any of the above | **Critical** | Closed live (froze margin at 32.7%); new `_margin_allows_entry()` gate in forex+futures prevents recurrence for any future strategy/module |

**Root-cause pattern**: none of #2–#4 were visible from forex's own
self-computed telemetry (heat, currency exposure) — they only show up by
checking Saxo's own live account state directly. Same lesson as
[[saxo_api_verification]], now demonstrated at the account-margin level
too, not just individual price/UIC lookups.

---

## Audit — 2026-08-22

Third pass, prompted by the user asking to build out historical backtest
coverage and spotting a real position-conflict pattern live on the
dashboard. All items fixed unless marked Open.

| # | Finding | Severity | Status |
|---|---------|----------|--------|
| 1 | London Breakout had never produced a real signal since inception — `_session_range()` read the session-hour window off `df.index.hour`; the real H1 data carries the hour in a separate `HourUTC` column instead, so the mask matched ~0 rows on every call, forever | **Critical** | Fixed — 13 real signals produced live post-fix where it previously produced 0 |
| 2 | A single rejected order used to crash the ENTIRE scheduled run — `saxo_order._place_entry_then_stop()` had no exception handling, so every strategy queued after the failure silently never ran that cycle | **Critical** | Fixed |
| 3 | Wrong tick-size rounding on TRY/CNH-quoted pairs (4dp, not the 5dp default) caused a live `PriceNotInTickSizeIncrements` rejection on a stop order while the Market entry still went through — position briefly held with no stop-loss | **High** | Fixed, in all 4 duplicated locations |
| 4 | Cross-strategy opposite-direction stacking — different strategies independently held both Long and Short on the same pair (NZDUSD, USDTHB, USDCZK) simultaneously, since each strategy only ever checks its own open positions | **Medium** | Fixed — new entries opposing another strategy's existing position are now blocked; same-direction stacking deliberately left alone |
| 5 | `pnl_tracker.sync_etf_from_json()`: SQLite doesn't support `ORDER BY`/`LIMIT` on `UPDATE` — silently failed the first time a real ETF sell was ever synced; a second bug in the same path would have marked a partially-sold position fully closed, dropping the remaining shares from the ledger | **High** | Fixed |
| 6 | TRY/MXN/CNH had no fallback FX rate, and their live Yahoo lookup structurally 404s (confirmed, not a bad-data-day) — every pair quoted in one of these was silently unsizable, permanently | Medium | Fixed |
| 7 | Account-wide margin exhaustion — stocks/ETF/forex share one Saxo margin pool; disabling the heat cap ran usable margin to 5,546 EUR available (99.22% utilization), blocking new entries and protective stops alike | Medium | Mitigated — sold ~half of every stock/ETF position, cut swing `RISK_PCT` 1%→0.5%; margin now 55,774 EUR available (92.69%) |
| 8 | `atos_live.db` held 4 phantom/stale rows not matching live Saxo (one entirely fictional position) | Medium | Reconciled — closed with honest unknown P&L, not a guessed number; new standing rule added (state must always match live Saxo, every module) |
| 9 | Only 1 of 10 forex strategies (EMA) had ever been backtested, and only on 7 G7 majors — the 83-pair EM/exotic expansion and 9 of 10 strategies had zero historical validation before live signals started firing on them | **High** | Closed — see "Backtesting" section below. Real finding, not just a validation formality: `ema`, `donchian`, `pullback`, `supertrend` show weak/negative historical edge on the CORE universe too, not only the new exotic pairs |
| 10 | `ATOS Dashboard Start` scheduled task was never actually disabled despite `docs/scheduling.md` claiming so since 2026-08-20 (same broken path as `ATOS Daily Scan`) — would have failed its next fire | Low | Flagged — needs an elevated `Disable-ScheduledTask`, this session has no admin rights |
| 11 | Repeated real "LBO win" emails with byte-identical values turned out to be a test-suite side effect (`test_london_breakout.py` calling the real notifier unconditionally, not properly mocked) — not a mislabeled real trade, confirmed via exhaustive cross-check against the ledger, state file, dashboard, and Saxo's own account history | Low | Fixed — SMTP now mocked in those tests |

**Root-cause pattern across #1–#3**: none of these were caught by any
existing test until this session, because every prior test used
conveniently-shaped synthetic data (a real `DatetimeIndex`, majors-only
5dp pricing) that never exercised the actual shape production code
produces (`HourUTC` column + `RangeIndex`, 4dp exotic pairs). 15 new
regression tests added in `test_2026_08_22_session_fixes.py`, each
reproducing the runner's real data shape, not a convenient one.

**Backtesting**: see the new "Backtesting" section further down for
`backtest_forex_universe.py`'s methodology and results.

---

## Audit — 2026-08-21

Second full pass, triggered by a live watchdog alert investigation that expanded
into a broader pre-go-live audit. All items below are fixed unless marked Open.

| # | Finding | Severity | Status |
|---|---------|----------|--------|
| 1 | `run_hidden.vbs` never propagated the child process's real exit code — `LastTaskResult` was a false positive even when the wrapped command failed or never ran | **Critical** | Fixed |
| 2 | LBO's double-wrap launch chain (`vbs → bat → vbs#2 → python`) passed a "program + arguments" string through the same quote-stripping logic a second time — only works for a bare file path, so the inner python call silently never ran on any of the 3 LBO tasks | **Critical** | Fixed |
| 3 | 13 of 20 ATOS scheduled tasks (incl. Forex Daily Run, Intraday Scan, LBO ×3) had `DisallowStartIfOnBatteries: True` — silently skipped with zero log trace any time the machine ran on battery | **Critical** | Fixed |
| 4 | `ATOS Forex Gap Fill` fired at Sun 22:00 **PKT** (17:00 UTC) — 5 hours before the weekly gap window actually opens (22:00 **UTC**) | **High** | Fixed |
| 5 | `ATOS Forex Intraday Scan` was a one-time trigger with a 16h repetition window, not a real daily-recurring trigger — worked for exactly one day then went permanently dormant | **High** | Fixed |
| 6 | Momentum pre-filter (ranks pairs by *directional trend strength*) was applied to `rsi`/`bb`/`zscore` — all three are mean-reversion strategies, so the filter suppressed exactly the low-momentum/choppy setups they're designed to catch | **High** | Fixed |
| 7 | Realized P&L stored raw, unconverted quote-currency amounts labeled uniformly `currency='USD'` — a JPY pair's P&L number is its true EUR value inflated ~150×, summed directly into NOK/CHF/CAD pairs' numbers as if all one currency | **Critical** | Fixed |
| 8 | 4 of 34 universe UICs (USDNOK, USDSEK, USDDKK, USDMXN) were never-verified sequential-numbering guesses — all 4 pointed at the wrong Saxo instrument (USDNOK's UIC was actually USDCZK; USDMXN's guess was off by over 1000) | **Critical** | Fixed |
| 9 | Stocks (`atos_runner.py`) placed bare market orders with no stop-loss/take-profit attached — `stop_price` was hardcoded to `0` in the US Blend DB record. Unlike forex/futures/ETF (which already used `saxo_order.place_with_stop()`), a position sat fully unprotected at the broker until the next scheduled cycle noticed | **Critical** | Fixed |
| 10 | `_post`/`_patch` had no 429 (rate-limit) backoff and no `x-request-id` header, so an identical retry within Saxo's 15s dedup window would be silently rejected as a duplicate (409) | Medium | Fixed |
| 11 | Live trading requires a **separate Saxo app registration** — SIM and LIVE app keys/secrets are not shared, and LIVE requires the full OAuth Authorization Code Grant (`saxo_auth.py` is currently hardcoded to `sim.logonvalidation.net`). Not a code bug — a manual step only the account owner can do via Saxo's developer portal, worth starting well before a target go-live date | **Critical** | Open (manual, not code) |

**Root-cause pattern across #1–#5**: every scheduled-task bug this pass was a
Windows Task Scheduler / launch-chain problem, not a strategy-logic problem — the
underlying signal code was already correct once it actually got to run.

**Root-cause pattern across #7–#8**: neither the P&L ledger nor 4 of the UICs had
ever been verified against Saxo's own live data — both were fixed by querying
Saxo's API directly (`/port/v1/positions/me` → `ProfitLossOnTradeInBaseCurrency`,
`/ref/v1/instruments` → real UICs) instead of trusting internally-computed or
guessed values. Applied as a standing practice: any account-currency conversion or
UIC now gets checked against a live Saxo call before being trusted.

**Universe expansion (2026-08-21)**: grew from 34 to 117 pairs — every FxSpot pair
Saxo offers among the 8 majors + 13 EM/exotic currencies (TRY, ZAR, MXN, PLN, HUF,
CZK, RON, THB, ILS, AED, CNH, HKD, SGD), minus inverse duplicates (e.g. CADUSD,
already covered by USDCAD) and precious metals (XAU/XAG/XPT — a different asset
class, already covered by the futures module's Gold market). All 83 new pairs'
UIC/pip_size/min_units were pulled live from `/ref/v1/instruments/details`, not
guessed — pip_size = `10 ** -(decimals - 1)` from Saxo's own `Format.Decimals`.
**SIM-only for now** — the plan is to define a narrower, deliberately-chosen
universe before going live, then expand it later based on what SIM testing shows.

**Live trading cost note**: spread-checked a sample of the new pairs directly
against Saxo's live SIM quotes rather than trusting generic web spread tables.
Majors run ~0.01–0.02% spread (EURUSD 0.017%, USDJPY 0.013%). Most new EM/exotic
pairs run ~0.02–0.09% (USDZAR 0.034%, EURPLN 0.058%, USDHUF 0.090% — 2–5× wider,
not the "20+ pips" web estimates suggested) — some (USDTRY 0.002%, USDCNH 0.022%)
were actually as tight as or tighter than majors. Real numbers move; day-trading
strategies (LBO) are most spread-sensitive, which is exactly why LBO stays on the
narrower 28-pair majors-only universe regardless of what SIM testing shows here.

---

## Audit — 2026-08-20

| # | Finding | Severity | Status |
|---|---------|----------|--------|
| 1 | **Position sizing ignored the quote currency — 447× risk spread** | **Critical** | Fixed `5bf8a5f` |
| 2 | Dry runs permanently deleted live position tracking | **Critical** | Fixed `5bf8a5f` |
| 3 | Sizing ran off ~945,000 EUR of SIM demo credit (~33× intended) | **High** | Fixed `5bf8a5f` |
| 4 | **10 of 11 strategies have no backtest** | **High** | Open |
| 5 | **CNN-LSTM emits zero signals and is ~chance accuracy** | **High** | Open |
| 6 | Signals computed on the incomplete, still-forming daily bar | Medium | Open |
| 7 | No broker/state reconciliation — orphaned positions undetectable | Medium | Open |
| 8 | No file logging — unattended runs leave no audit trail | Medium | Open |
| 9 | Bar timestamps discarded on fetch — stale data undetectable | Low | Open |
| 10 | **London Breakout: sized off the whole account, not its 15,000 SEK book** | **High** | Fixed `0b855b2` |
| 11 | **LBO: hardcoded `/10.7` "USDSEK" rate on a EUR account** | **High** | Fixed `0b855b2` |
| 12 | **LBO: `MAX_UNITS` cap was doing the sizing on 5 of 7 pairs** | **High** | Fixed `0b855b2` |
| 13 | LBO `_atr()` uses a simple mean, not Wilder — inconsistent with every other module | Low | Open |

### Per-strategy formula audit

Every strategy's indicators and signal construction were read. Results:

| # | Strategy | Indicators | Lookahead | Sizing | Verdict |
|---|----------|-----------|-----------|--------|---------|
| 1 | EMA + ADX | ✅ Wilder | ✅ none | fixed `5bf8a5f` | OK |
| 2 | RSI(2) | ✅ Wilder | ✅ none | fixed `5bf8a5f` | OK |
| 3 | Donchian | ✅ Wilder | ✅ `[-(N+1):-1]` excludes current bar | fixed `5bf8a5f` | OK |
| 4 | Bollinger | ✅ `ddof=0` | ✅ none | fixed `5bf8a5f` | OK |
| 5 | Pullback | ✅ Wilder | ✅ none | fixed `5bf8a5f` | OK |
| 6 | Gap Fill | ✅ n/a | ✅ none | fixed `5bf8a5f` | OK |
| 7 | SuperTrend | ✅ correct ratcheting | ✅ none | fixed `5bf8a5f` | OK |
| 8 | Z-Score | ✅ `ddof=1` | ✅ none | fixed `5bf8a5f` | OK |
| 9 | ML (logistic) | ✅ correct | ✅ **verified none** | fixed `5bf8a5f` | OK |
| 10 | CNN-LSTM | ✅ correct | ✅ walk-forward safe | n/a | ⚠️ **no edge, never fires** |
| 11 | London Breakout | ⚠️ SMA not Wilder | ✅ none | ❌ **4 defects** | fixed `0b855b2` |

The indicator maths is sound across the board — the defects were all in **capital
and currency handling**, not in the signal logic.

### 🔴 Findings 10–12 — London Breakout sizing (fixed)

LBO is the day-trading book and was the last strategy audited. Four defects sat in
the same six lines:

1. **Wrong book.** It is documented as a separate book with 15,000 SEK and 1.5%
   risk. `runner.py` passed `account_equity=equity` — the *whole account* — so the
   `15_000.0` default was always overridden. Off the uncapped SIM equity that was
   ~14,185 EUR of risk per trade.
2. **Hardcoded wrong-currency rate.** `equity_usd = account_equity / 10.7  #
   approximate USDSEK`, on an account denominated in **EUR**. Wrong constant, wrong
   pair, and nothing updates it.
3. **Quote-currency error, and the Finding 1 fix did not reach it.** LBO returns
   precomputed `units` in its signal, and the runner uses `sig["units"]` directly —
   bypassing the conversion added in `5bf8a5f`. LBO trades USDJPY and GBPJPY, so
   those were sized against a JPY stop distance using an unconverted budget.
4. **The cap was doing the sizing.** With inflated equity, **5 of 7 pairs pinned at
   `MAX_UNITS = 50,000`** — position size was set by the clamp, not by risk. And
   `max(MIN_UNITS, …)` floored small sizes *up*, silently over-risking any trade
   whose correct size was below one lot; those are now skipped instead.

**Verified across all 7 LBO pairs** at representative session ranges:

| | Before | After |
|---|---|---|
| Risk range | 7 – 107 EUR | 20.85 EUR flat |
| Spread | **15×** (5 of 7 pinned at cap) | **1.00×** |
| Per trade | uncontrolled | exactly 1.5% of the 1,390 EUR book |

Book capital now lives in `strategies.forex.lbo_capital_eur` (1,390 EUR ≈ 15,000 SEK).

### 🔴 Finding 1 — sizing ignored the quote currency (fixed)

`size_position()` computes `units = (equity × RISK_PCT) / (mult × ATR)`. ATR — and
therefore stop distance — is denominated in the pair's **quote currency**, while
equity is in **EUR**. Nothing converted between them, so realised risk scaled with
the *numeric size of the quote currency*.

Measured on the 14 live open positions, each converted to EUR at live rates:

| Position | Units | Risk (EUR) | % of account |
|----------|------:|-----------:|-------------:|
| ema:USDJPY | 5,000 | 24 | 0.1% |
| pullback:EURJPY | 5,000 | 46 | 0.2% |
| ml:AUDJPY | 5,000 | 46 | 0.2% |
| ml:CADJPY | 6,000 | 51 | 0.2% |
| pullback:EURNOK | 81,000 | 872 | 3.1% |
| ema:GBPUSD | 958,000 | 3,587 | 12.9% |
| donchian:EURUSD | 974,000 | 4,547 | 16.4% |
| donchian:GBPUSD | 722,000 | 6,018 | 21.6% |
| ema:USDCAD | 1,223,000 | 6,180 | 22.2% |
| donchian:AUDUSD | 1,063,000 | 8,538 | 30.7% |
| **pullback:NZDCHF** | 1,602,000 | **10,674** | **38.4%** |

Every one of those is nominally a **1% risk** trade. Actual spread: **447×**.
JPY-quoted pairs were effectively not trading (0.1% risk); USD/CAD/CHF pairs risked
up to 38% of the account on a single position.

**Fix:** restate the risk budget in the quote currency before sizing
(`_equity_in_quote` / `_eur_per_unit`, using the existing `fx` module). Applied at
the single sizing call site in the runner, so all 11 strategies inherit it without
touching each module. If a quote currency has no rate the signal is now **skipped**
rather than sized without conversion.

**Verified after the fix** — same 14 positions, same stops:

| | Before | After |
|---|---|---|
| Risk range | 24 – 10,674 EUR | 269 – 278 EUR |
| Spread | **447×** | **1.0×** |
| Per trade | 0.1% – 38.4% | 0.97% – 1.00% |

Residual variation is `min_units` rounding only.

### Finding 2 — dry runs deleted live positions (fixed)

Same defect found in futures (`a34ffd0`), and worse here. `_process_exits()` does
`del positions[key]` **unconditionally** — outside the `if not dry_run` block — and
both `run_daily()` and `run_exits_only()` called `_save_state()` unconditionally.

Any dry run whose exit rules fired would have permanently wiped tracking for real
open positions, leaving all 14 open at the broker with no stop management and no
record. Found by reading before running, so it was never triggered here.

### Finding 3 — sizing ran off demo credit (fixed)

`_account()` read `TotalValue` uncapped — `forex_peak_equity.json` confirms
**945,669 EUR**, roughly 33× the intended 300,000 SEK. Now capped by
`strategies.forex.risk_equity_eur`.

Unlike futures, this does **not** reduce what can be traded: FX deals in 1,000-unit
increments, so positions scale down cleanly rather than whole pairs becoming
untradeable.

### Finding 4 — 10 of 11 strategies are unvalidated (open)

`backtest_forex.py` covers **only Strategy 1 (EMA + ADX)**, and reimplements the
rules inline rather than importing `forex.strategy` — so it validates a *copy*, not
the code that trades. RSI, Donchian, BB, Pullback, Gap, SuperTrend, Z-Score, ML and
London Breakout have **no backtest anywhere in the repo**.

The win rates quoted in each strategy section below are therefore assertions, not
measurements. `backtest_futures_all.py` shows the pattern for fixing this: drive the
real modules over truncated data. The forex modules share a uniform
`generate_signals` / `should_exit` / `size_position` interface, so the same approach
ports directly.

### 🔴 Finding 5 — CNN-LSTM never fires (open)

Strategy 10 is marked ★★★ in this document. Its own walk-forward report
(`data/cnn_lstm/report.json`) says otherwise:

| Fold | Train end | Accuracy | Buy prec. | Sell prec. | **Signal rate** |
|-----:|-----------|---------:|----------:|-----------:|----------------:|
| 1 | 2023-12-06 | 36.8% | 40.6% | 34.1% | **0.0** |
| 2 | 2024-05-22 | 39.0% | 39.5% | 38.0% | **0.0** |
| 3 | 2024-11-06 | 37.7% | 39.7% | 36.7% | **0.0** |
| 4 | 2025-04-28 | 35.6% | 35.5% | 34.2% | **0.0** |
| 5 | 2025-10-13 | 35.6% | 35.9% | 32.0% | **0.0** |
| | **mean** | **36.9%** | 38.2% | — | **0.0** |

Two independent problems:

1. **Accuracy is chance.** This is a 3-class problem (Buy / Sell / Hold), so random
   is 33.3%. 36.9% is not a demonstrated edge.
2. **Signal rate is 0.0 in every fold.** At the configured `CONFIDENCE = 0.58`
   threshold the model never produced a single tradeable signal in five folds of
   walk-forward testing. It holds no live positions either.

The model trains, loads and runs correctly — it simply never emits anything. It is
carrying a PyTorch dependency, a training pipeline and a ★★★ rating for zero output.
**Either lower the confidence threshold and re-validate, or retire it.**

> The *engineering* here is sound — the trainer does proper walk-forward with a
> `max_date` cutoff and never shuffles time-series data. The model just has no edge.

### Finding 6 — signals use the incomplete current bar (open)

`_fetch_history()` returns every bar the chart API gives, **including the current,
still-forming daily bar**. The scheduled runs fire at 01:20, 09:00 and 13:00 UTC —
all mid-session — so `closes.iloc[-1]` is a partial bar, not a close.

Two consequences:

- Daily-bar strategies were designed and (where backtested) validated on *completed*
  bars. Acting on a partial bar is a different strategy.
- The runner fires 3× per day, so the same forming bar is evaluated three times with
  different values. A Donchian breakout or RSI extreme can trigger on an intraday
  spike the daily close never confirms.

This is a live-vs-backtest mismatch affecting **all 11 strategies**. Fixing it means
dropping the last bar (signal on yesterday's close, trade today) — a material change
to signal timing, so it needs a deliberate decision rather than a silent edit.

### Findings 7–9 — operational gaps (open)

- **No reconciliation.** Nothing compares state to broker positions; forex has the
  same orphaning exposure futures had. `futures/runner.py --reconcile` (`852eac2`)
  is the working template.
- **No file logging.** `logging.basicConfig()` with no `FileHandler` — console only.
  Every other module writes `logs/*.log`. Unattended runs (5 scheduled tasks/day)
  leave no trace, which is why no forex log exists to audit.
- **Timestamps discarded.** `_fetch_history()` builds rows of OHLC only, dropping the
  bar `Time`, so stale or gapped data cannot be detected.

### Verified as correct

Not everything was broken. Confirmed sound by inspection:

- **RSI** — proper Wilder smoothing (`ewm(alpha=1/period)`) on separated gains and
  losses, in both `strategy_rsi.py` and `strategy_bb.py`.
- **ATR** — correct Wilder RMA, consistent across all modules.
- **ADX** — textbook Wilder `+DM`/`−DM` selection and smoothing.
- **Bollinger** — `ddof=0` (population std), the standard convention.
- **Z-Score** — `ddof=1` rolling mean/std; uses only past and current bars.
- **SuperTrend** — correct band ratcheting and direction-flip logic, with NaN
  handling before the first valid ATR.
- **ML (logistic regression)** — **no lookahead**: labels use `shift(-1)` correctly,
  the training window ends at `len-2` so the last label is knowable today, and the
  normalisation `mu`/`sigma` are fitted on the training window only, never including
  the prediction row.
- **CNN-LSTM trainer** — time-series-safe walk-forward with `max_date` cutoff and no
  shuffling.

---

## Daily Schedule

| Task (Task Scheduler)       | Time PKT       | UTC           | Session        | Pairs |
|-----------------------------|----------------|---------------|----------------|-------|
| ATOS Forex Daily Run        | 06:20 Mon–Fri  | 01:20         | Asian          | 14 (JPY/AUD/NZD crosses) |
| ATOS Forex Exit Check       | 14:00 Mon–Fri  | 09:00         | All            | 34 (stops only — no new entries) |
| ATOS Forex London Run       | 18:00 Mon–Fri  | 13:00         | London         | 20 (EUR/GBP/USD + Scandi/CAD) |
| ATOS Forex Gap Fill         | 22:00 Sunday   | 17:00 Sun     | All            | 34 (gap fill entries only) |
| **LBO London Open**         | **12:00 Mon–Fri** | **07:00**  | **London open** | **7 majors (Asian range break)** |
| **LBO NY Open**             | **18:00 Mon–Fri** | **13:00**  | **NY open**    | **7 majors (London morning break)** |
| **LBO Force Close**         | **01:00 daily**   | **20:00**  | **Session end** | **7 (close all LBO positions)** |

---

## Strategy 1 — EMA(5/30) Crossover + ADX

**File**: `forex/strategy.py`  
**Type**: Trend-Following  
**Win Rate**: ~55%  
**Slots**: 4  

### Concept
The foundational trend strategy. EMA(5) crossing EMA(30) signals a momentum shift; ADX(14) ≥ 25 confirms a genuine trend exists, filtering out choppy sideways sessions where crossovers whipsaw.

### Entry
| Direction | Conditions |
|-----------|-----------|
| **LONG**  | EMA(5) crossed above EMA(30) within last 15 bars + ADX ≥ 25 + +DI > -DI |
| **SHORT** | EMA(5) crossed below EMA(30) within last 15 bars + ADX ≥ 25 + -DI > +DI |

### Exit (first condition hit)
- **A** — Opposite EMA crossover (trend reversal)
- **B** — 1.5×ATR(14) hard stop from entry
- **C** — 45-day time stop

### Parameters
| Param | Value |
|-------|-------|
| Fast EMA | 5 |
| Slow EMA | 30 |
| ADX period | 14 |
| ADX minimum | 25 |
| ATR stop mult | 1.5× |
| Time stop | 45 days |
| Signal lookback | 15 bars |
| Backtested Sharpe | 1.619 |

---

## Strategy 2 — RSI(2) Pullback in Trend

**File**: `forex/strategy_rsi.py`  
**Type**: Mean-Reversion within Trend  
**Win Rate**: ~60%  
**Slots**: 34  

### Concept
Uses EMA(200) to lock in the major trend direction, then fires only when RSI(2) hits an extreme. A 2-day exhaustion move against the trend statistically snaps back within 3–8 sessions. Very fast in, very fast out.

### Entry
| Direction | Conditions |
|-----------|-----------|
| **LONG**  | close > EMA(200) AND RSI(2) < 10 |
| **SHORT** | close < EMA(200) AND RSI(2) > 90 |

### Exit (first condition hit)
- **A** — RSI(2) recovers: ≥ 55 for longs / ≤ 45 for shorts
- **B** — 1.5×ATR(14) hard stop
- **C** — 12-day time stop

### Parameters
| Param | Value |
|-------|-------|
| RSI period | 2 |
| Trend EMA | 200 |
| Entry long | RSI(2) < 10 |
| Entry short | RSI(2) > 90 |
| Exit long | RSI(2) ≥ 55 |
| Exit short | RSI(2) ≤ 45 |
| ATR stop mult | 1.5× |
| Time stop | 12 days |

> **Note**: RSI(2) moves extremely fast — can shift from 9 to 81 within a single session. Signals are only valid immediately after the scheduled scan fires.

---

## Strategy 3 — Donchian Channel Breakout (Strict Mode)

**File**: `forex/strategy_donchian.py`  
**Type**: Momentum  
**Win Rate**: ~50% (edge from large winners, not high WR)  
**Slots**: 4  

### Concept
Turtle Trading adapted for FX. A 30-day high/low breakout captures the start of a sustained move. Now includes mandatory EMA(200) trend filter AND ADX(14) ≥ 25 to eliminate counter-trend entries and false breakouts in ranging markets.

### Entry — all three conditions required
| Direction | Conditions |
|-----------|-----------|
| **LONG**  | close > 30-day highest close AND close > EMA(200) AND ADX ≥ 25 |
| **SHORT** | close < 30-day lowest close AND close < EMA(200) AND ADX ≥ 25 |

### Exit (first condition hit)
- **A** — 15-day channel reversal (close crosses 15-day opposite channel side)
- **B** — 2.0×ATR(14) hard stop
- **C** — 30-day time stop

### Parameters
| Param | Value |
|-------|-------|
| Entry channel | 30 days |
| Exit channel | 15 days |
| Trend EMA | 200 |
| ADX minimum | 25 |
| ATR stop mult | 2.0× |
| Time stop | 30 days |

> **Change log**: Channel widened 20→30 days; EMA(200) + ADX(25) gate added to fix counter-trend entries that caused the original strategy to lose.

---

## Strategy 4 — Bollinger Band Reversion

**File**: `forex/strategy_bb.py`  
**Type**: Mean-Reversion  
**Win Rate**: ~60%  
**Slots**: 4  

### Concept
Fades 2-sigma price extremes. BB(20,2) outer band touch = statistical overextension; RSI(14) confirms momentum exhaustion. Targets reversion back to the 20-day mean (BB midline). Short-term hold — 8-day time stop keeps capital free.

### Entry
| Direction | Conditions |
|-----------|-----------|
| **SHORT** | close > BB upper AND RSI(14) > 65 (overbought excursion) |
| **LONG**  | close < BB lower AND RSI(14) < 35 (oversold excursion) |

### Exit (first condition hit)
- **A** — Close crosses back through BB mid (20-day SMA)
- **B** — 2.0×ATR(14) hard stop
- **C** — 8-day time stop

### Parameters
| Param | Value |
|-------|-------|
| BB period | 20 |
| BB std dev | 2.0σ |
| RSI period | 14 |
| RSI overbought | 65 |
| RSI oversold | 35 |
| ATR stop mult | 2.0× |
| Time stop | 8 days |

---

## Strategy 5 — Trend Pullback to EMA(20) ★

**File**: `forex/strategy_pullback.py`  
**Type**: Trend Continuation — Pullback Entry  
**Win Rate**: ~70%+ (highest win rate among trend strategies)  
**Slots**: 34  

### Concept
Enters the same trend as EMA crossover, but at a far better price. Instead of chasing the crossover, it waits for the market to pull back and touch EMA(20) — the dynamic support in an uptrend — then confirms the bounce with a close back in the trend direction.

**Triple confirmation** (trend + ADX + bounce) is what drives the elevated win rate. Because the entry is near EMA support, the stop is tighter → more units can be sized for the same 1% risk → larger profit on the same move.

### Entry — all three conditions must be true simultaneously
| Direction | Conditions |
|-----------|-----------|
| **LONG**  | close > EMA(50) AND ADX(14) ≥ 25 AND low touched EMA(20) within last 3 bars AND current close > EMA(20) |
| **SHORT** | close < EMA(50) AND ADX(14) ≥ 25 AND high touched EMA(20) within last 3 bars AND current close < EMA(20) |

### Exit (first condition hit)
- **A** — Trend break: close < EMA(50) for longs / close > EMA(50) for shorts
- **B** — 1.5×ATR(14) hard stop (tight because entry is near EMA support)
- **C** — 25-day time stop

### Parameters
| Param | Value |
|-------|-------|
| Trend EMA | 50 |
| Pullback EMA | 20 |
| ADX period | 14 |
| ADX minimum | 25 |
| Pullback lookback | 3 bars |
| ATR stop mult | 1.5× |
| Time stop | 25 days |

---

## Strategy 6 — Weekend Gap Fill ★★

**File**: `forex/strategy_gap.py`  
**Type**: Statistical Mean-Reversion (Structural Edge)  
**Win Rate**: ~80–85% (highest win rate of all strategies)  
**Slots**: 34  
**Runner flag**: `NEEDS_LIVE_PRICES = True` — runner fetches live Sunday open prices before calling this strategy  

### Concept
FX markets close Friday ~22:00 GMT and reopen Sunday ~22:00 GMT. Price frequently gaps between the Friday close and the Sunday open due to weekend news, central bank statements, or geopolitical events.

Approximately **80–85% of these gaps fill within 5 trading days**. The edge is structural, not technical:
1. Market makers immediately quote back toward Friday's close
2. Algorithmic desks are programmed to fade weekend gaps
3. Retail traders close weekend positions at Sunday open

We enter the **fade direction** on Sunday night and target a full gap fill.

### Entry — Sunday 22:00 PKT
| Direction | Conditions |
|-----------|-----------|
| **SHORT** | Sunday open > Friday close (gap up) AND gap 0.10%–2.00% |
| **LONG**  | Sunday open < Friday close (gap down) AND gap 0.10%–2.00% |

### Exit (first condition hit)
- **A** — Gap filled: price reaches Friday close level
- **B** — Hard stop: 1.5 × gap size against position
- **C** — 7-day time stop (≈ 5 trading days Mon–Fri)

### Parameters
| Param | Value |
|-------|-------|
| Min gap size | 0.10% of price |
| Max gap size | 2.00% of price |
| Stop mult | 1.5× gap size |
| Time stop | 7 calendar days |

---

## Strategy 7 — SuperTrend(10,3) Trend-Following

**File**: `forex/strategy_supertrend.py`  
**Type**: Trend-Following  
**Win Rate**: ~65%  
**Slots**: 20  

### Concept
SuperTrend generates a dynamic ATR-based support/resistance band. When price crosses above the band the trend flips bullish; below = bearish. EMA(200) acts as a macro filter — only trade in the direction of the dominant long-term trend. Fresh crossovers only (within last 3 bars) to avoid chasing stale signals.

### Entry — all conditions required
| Direction | Conditions |
|-----------|-----------|
| **LONG**  | SuperTrend direction flipped to +1 within last 3 bars AND close > EMA(200) |
| **SHORT** | SuperTrend direction flipped to -1 within last 3 bars AND close < EMA(200) |

SuperTrend bands:
- `upper = HL/2 + 3.0 × ATR(10)` — resistance in downtrend
- `lower = HL/2 − 3.0 × ATR(10)` — support in uptrend
- Bands ratchet: upper can only decrease, lower can only increase

### Exit (first condition hit)
- **A** — SuperTrend direction reverses (band crossover)
- **B** — 2.0×ATR(10) hard stop
- **C** — 40-day time stop

### Parameters
| Param | Value |
|-------|-------|
| ATR period | 10 |
| Multiplier | 3.0 |
| Trend EMA | 200 |
| Signal lookback | 3 bars |
| ATR stop mult | 2.0× |
| Time stop | 40 days |

---

## Strategy 8 — Z-Score Mean Reversion

**File**: `forex/strategy_zscore.py`  
**Type**: Mean-Reversion  
**Win Rate**: ~63%  
**Slots**: 20  

### Concept
When price deviates more than 2 standard deviations from its 20-day mean, it is statistically overextended and reverts. More rigorous than Bollinger Band reversion — uses the actual z-score (normalized in σ units) rather than a fixed band. EMA(200) prevents fading a genuine macro trend breakout.

### Entry
| Direction | Conditions |
|-----------|-----------|
| **LONG**  | z-score < −2.0 AND close > EMA(200) × 0.99 (not in extreme downtrend) |
| **SHORT** | z-score > +2.0 AND close < EMA(200) × 1.01 (not in extreme uptrend) |

### Exit (first condition hit)
- **A** — Z-score reverts to within ±0.3 (returned to mean)
- **B** — 2.5×ATR(14) hard stop
- **C** — 12-day time stop

### Parameters
| Param | Value |
|-------|-------|
| Z-score window | 20 days |
| Entry threshold | ±2.0σ |
| Exit threshold | ±0.3σ |
| Trend EMA | 200 |
| ATR stop mult | 2.5× |
| Time stop | 12 days |

---

## Strategy 9 — Machine Learning Signals (Logistic Regression)

**File**: `forex/strategy_ml.py`  
**Type**: ML — Data-Driven  
**Win Rate**: ~57–62% (varies by market regime)  
**Slots**: 20  

### Concept
Trains a logistic regression model on the last 126 daily bars (6 months) per pair. Seven normalized technical features capture trend, momentum, volatility, and mean-reversion simultaneously. Only trades when model confidence exceeds the threshold. Pure numpy implementation — no sklearn dependency.

### Features (7 normalized inputs)
| Feature | Description |
|---------|-------------|
| RSI(14) / 100 | Momentum oscillator (0–1) |
| ADX(14) / 100 | Trend strength (0–1) |
| BB %B (20,2) | Price position within BB band (0–1) |
| EMA(5)/EMA(20) − 1 | Fast/slow EMA spread |
| EMA(20)/EMA(50) − 1 | Medium-term trend |
| Price/EMA(200) − 1 | Macro trend bias |
| ATR(14)/close | Normalized volatility |

**Target**: next-day close > today's close → 1 (up), else 0 (down)

### Entry
| Direction | Conditions |
|-----------|-----------|
| **LONG**  | model probability ≥ 0.58 AND ADX(14) ≥ 20 |
| **SHORT** | model probability ≤ 0.42 AND ADX(14) ≥ 20 |

### Exit (first condition hit)
- **A** — Model prediction flips direction with confidence ≥ 0.58
- **B** — 2.0×ATR(14) hard stop
- **C** — 20-day time stop

### Parameters
| Param | Value |
|-------|-------|
| Training window | 126 bars (6 months) |
| Min bars required | 336 (EMA200 + lookback + buffer) |
| Confidence threshold | 0.58 |
| ADX minimum | 20 |
| ATR stop mult | 2.0× |
| Time stop | 20 days |
| Learning rate | 0.05 |
| Epochs | 200 |

> **Note**: ML strategy requires 336+ daily bars per pair. Pairs with fewer bars are silently skipped. Retrains on every signal check — no state persisted between runs.

---

## Strategy 10 — CNN-LSTM Deep Learning ⚠️ NOT TRADING

**Files**: `forex/strategy_cnn_lstm.py` (inference) · `forex/cnn_lstm_trainer.py` (training)  
**Type**: Deep Learning — Multi-scale CNN + Bidirectional LSTM + Self-Attention  
**Measured**: walk-forward accuracy **36.9%** on a 3-class problem (chance = 33.3%);
buy precision 35–41%, sell precision 32–38%; **signal rate 0.0 in all 5 folds**  
**Slots**: 20 (none ever used)  

> **This section previously claimed ★★★ and "walk-forward precision typically
> 55–65%". Its own report — `data/cnn_lstm/report.json` — contradicts both.** The
> model has never emitted a signal at `CONFIDENCE = 0.58` and holds no positions.
> See [Finding 5](#-finding-5--cnn-lstm-never-fires-open). Either lower the
> threshold and re-validate, or retire the strategy.


### Concept
A production-quality deep learning model trained on **5 years of daily bars across all 34 pairs simultaneously** (≈40,000 sequences). It learns patterns at three different timescales in parallel and uses an attention mechanism to weight which recent bars matter most for the prediction.

**Why it's better than a naive Conv1D→LSTM:**

| Naive approach | This model |
|---|---|
| Single Conv kernel=3 | 3 parallel branches (k=3/7/14) — daily, weekly, bi-weekly patterns |
| Random train/val shuffle | Walk-forward cross-validation — never shuffles time series |
| No look-ahead bias protection | Causal convolutions — output at day t uses only data ≤ t |
| Standard LSTM | Bidirectional LSTM — processes sequence both directions |
| None | Self-attention pooling — learns which bars matter most |
| Binary target (up/down) | ATR-normalized 3-class target — only labels significant moves |
| Per-pair model | Single global model across all 34 pairs (12× more training data) |

### Architecture
```
Input: (batch, 60 days, 16 features)
  ↓
Multi-Scale CNN  [3 parallel branches, causal padding]:
  Branch-A  Conv1D(k=3) × 2 → BatchNorm → ReLU   (3-day / daily patterns)
  Branch-B  Conv1D(k=7)     → BatchNorm → ReLU   (weekly patterns)
  Branch-C  Conv1D(k=14)    → BatchNorm → ReLU   (bi-weekly patterns)
  → Concat(192ch) → Dropout(0.2) → MaxPool(2)
  ↓
Bidirectional LSTM(128 × 2 = 256) → Dropout(0.3)
  ↓
Self-Attention pooling → context vector(256)
  ↓
Dense: 256→128 (BN, ReLU) → Dropout(0.25) → 64 (ReLU) → 3 (Softmax)
  ↓
Output: [P(Sell), P(Hold), P(Buy)]   — 409,220 parameters
```

### Features (16 per time step, 60-bar lookback)
| # | Feature | Captures |
|---|---------|---------|
| 1 | log return (1d) | Recent momentum |
| 2 | log return (5d) | Weekly trend |
| 3 | log return (20d) | Monthly momentum |
| 4 | EMA(5)/EMA(20) − 1 | Fast trend |
| 5 | EMA(20)/EMA(50) − 1 | Medium trend |
| 6 | Price/EMA(200) − 1 | Macro trend bias |
| 7 | RSI(14) − 0.5 | Momentum oscillator (centred) |
| 8 | ADX(14)/100 | Trend strength |
| 9 | ATR(14)/close | Normalized volatility |
| 10 | ATR/avg-ATR(20) − 1 | Relative volatility (regime) |
| 11 | BB(20,2) %B | Mean-reversion band position |
| 12 | Z-score(20) | Standard deviations from mean |
| 13 | Donchian position | 30-day channel location (0–1) |
| 14 | MACD histogram/ATR | Normalized momentum divergence |
| 15 | sin(2π × DoW/5) | Cyclical day-of-week |
| 16 | cos(2π × DoW/5) | Cyclical day-of-week |

### Target labels
A signal is only labelled **Buy** or **Sell** if the 5-day forward return exceeds ±0.5 × ATR%. Otherwise it is **Hold** (no trade). This avoids training the model to trade noise.

| Label | Condition |
|-------|-----------|
| **Buy** (2) | fwd\_return\_5d > +0.5 × ATR% |
| **Hold** (1) | within ±0.5 × ATR% band |
| **Sell** (0) | fwd\_return\_5d < −0.5 × ATR% |

### Entry
| Direction | Conditions |
|-----------|-----------|
| **LONG**  | P(Buy) ≥ 0.60 AND ADX(14) ≥ 15 |
| **SHORT** | P(Sell) ≥ 0.60 AND ADX(14) ≥ 15 |

### Exit (first condition hit)
- **A** — Model flips: P(Sell) ≥ 0.60 for longs / P(Buy) ≥ 0.60 for shorts
- **B** — 2.5×ATR(14) hard stop
- **C** — 15-day time stop

### Parameters
| Param | Value |
|-------|-------|
| Lookback window | 60 bars |
| Features | 16 |
| Training data | 5 years, 34 pairs (yfinance) |
| Prediction horizon | 5 days |
| Confidence threshold | 0.60 |
| ADX minimum | 15 |
| ATR stop mult | 2.5× |
| Time stop | 15 days |
| Model size | 409,220 parameters |
| Training time (CPU) | ~15–30 min |
| Min bars for inference | 280 |

### Training & maintenance
```bash
# First-time training (run once before first live use)
python -m forex.cnn_lstm_trainer --train

# Check model status and walk-forward performance
python -m forex.cnn_lstm_trainer --status

# Retrain on specific pairs only
python -m forex.cnn_lstm_trainer --train --pairs EURUSD GBPUSD USDJPY

# Walk-forward backtest without retraining final model
python -m forex.cnn_lstm_trainer --backtest
```

Model artifacts are saved to `data/cnn_lstm/` (gitignored — regenerate with `--train`):
- `model.pt` — PyTorch state dict
- `scaler.json` — per-feature mean/std (fitted on training fold only)
- `config.json` — hyperparameters used
- `report.json` — walk-forward accuracy, Buy/Sell precision per fold

If no trained model exists, the strategy silently emits no signals — safe to have in the registry before training.

> **Retrain schedule**: Recommend monthly retraining as new market data accumulates. Add to Task Scheduler: `python -m forex.cnn_lstm_trainer --train` on the 1st of each month.

---

## Strategy Comparison

**Win rates below are original design targets, not measured results** — see
the Backtesting section immediately after this table for what's actually
been historically validated so far (only EMA, on 7 majors, before
2026-08-22). Slot counts corrected 2026-08-22 — the table below had been
stale since before the universe expansion.

| # | Strategy | Type | Win Rate (design target) | Key Indicators | Stop | Take-Profit | Time Stop | Slots | Book |
|---|----------|------|----------|---------------|------|------|-----------|-------|------|
| 1 | EMA Crossover | Trend | ~55% | EMA(5/30) + ADX(14) | 1.5×ATR | 2.0×R | 45d | **117** | Swing |
| 2 | RSI(2) Pullback | Reversion-in-trend | ~60% | RSI(2) + EMA(200) | 1.5×ATR | 2.0×R | 12d | **117** | Swing |
| 3 | Donchian Break | Momentum | ~50% | 30d High/Low + EMA(200) + ADX | 2.0×ATR | 2.0×R | 30d | **117** | Swing |
| 4 | BB Reversion | Mean-reversion | ~60% | BB(20,2) + RSI(14) | 2.0×ATR | 2.0×R | 8d | **117** | Swing |
| 5 | **Pullback-to-EMA** ★ | Trend continuation | **~70%+** | EMA(20/50) + ADX(14) | 1.5×ATR | 2.0×R | 25d | **117** | Swing |
| 6 | **Weekend Gap Fill** ★★ | Structural mean-rev | **~80–85%** | Gap % + live price | 1.5×gap | Gap target | 7d | **117** | Swing |
| 7 | SuperTrend | Trend | ~65% | ST(10,3) + EMA(200) | 2.0×ATR | 2.0×R | 40d | **117** | Swing |
| 8 | Z-Score Rev | Mean-reversion | ~63% | 20d z-score + EMA(200) | 2.5×ATR | 2.0×R | 12d | **117** | Swing |
| 9 | ML Signals | ML / Logistic Reg | ~57–62% | 7 features, per-pair retrain | 2.0×ATR | 2.0×R | 20d | **117** | Swing |
| 10 | **CNN-LSTM** ★★★ | Deep Learning | **~55–65%** | 16 features, global model, attention | 2.5×ATR | 2.0×R | 15d | **117** | Swing |
| **11** | **London Breakout** ★★ | **Day Trading** | **~58–63%** | **H1 Asian/London range + session clock** | **Range boundary** | **2.0× range** | **20:00 UTC** | **28** | **Day** |

**"2.0×R" = `DEFAULT_TP_RR` (`forex/runner.py`)** — 2× that strategy's own
stop distance (entry→stop), on the profit side. Added 2026-08-22: before
this, strategies 1–5 and 7–10 only ever placed a stop-loss at entry —
profit-taking depended entirely on the next scheduled `run_exits_only()`
catching `should_exit()`. Every strategy's own stop/time-stop/trailing-stop
logic is unchanged; the take-profit is an *additional* resting order at the
broker (a true OCO bracket alongside the stop, via `saxo_order.place_with_stop`)
so a winning trade is captured even if a scheduled run is late or skipped —
per explicit user direction not to depend on the scheduler for this. Gap and
London Breakout already had their own session-range-derived target and are
unaffected by the default.

---

## Backtesting

**Coverage before 2026-08-22**: `backtest_forex.py` — a 5-year grid-search
backtest, but **only for the EMA strategy**, and **only on the 7 original
G7 majors** (EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD, NZDUSD, USDCHF). The
other 9 strategies, and the 83-pair EM/exotic universe expansion, had
**zero historical validation** before live signals started firing on them
— a rule-based strategy generates a signal whenever its mathematical
condition is met (a crossover, an RSI threshold) on ANY price series fed
to it; that a signal fires is not evidence it has positive expectancy on
that instrument. ML and CNN-LSTM are a sharper version of the same gap:
both are trained models, and there's no record of what pairs their
training data actually covered — running them on a currency the model
never saw during training is closer to unvalidated extrapolation than
inference.

**`backtest_forex_universe.py`** (added 2026-08-22) closes this gap for the
8 daily-bar strategies (ema, rsi, donchian, bb, pullback, supertrend,
zscore, ml). `gap` and `london_breakout` are excluded — both need intraday
H1 session data, which Yahoo Finance doesn't reliably carry history for.

- **Drives each strategy's REAL production code** —
  `generate_signals()`/`should_exit()`/`size_position()` imported directly
  from `forex/strategy_*.py`, walked forward day-by-day. This is not a
  reimplementation that could silently diverge from what actually runs
  live.
- **Data**: yfinance daily bars, one ticker per currency's USD leg (23
  currencies, e.g. `EURUSD=X`, `USDTRY=X`). Cross pairs not directly
  listed on Yahoo with usable history (confirmed empirically: e.g.
  `AUDTRY=X`/`EURCNH=X` return ~1 bar of data vs 780 for USD-leg tickers)
  are synthesized via triangulation: `pair_price = usd_value(base) /
  usd_value(quote)`. Verified against a real live Saxo quote before
  trusting it: `AUDUSD 0.717 / (1/USDTRY 48.06) = 34.46`, matching the
  real AUDTRY quote exactly. High/Low for a synthesized pair are
  approximated by combining each leg's own relative daily range as though
  roughly independent — adequate for validation-level ATR/Donchian/BB
  calculations, not a substitute for real historical H/L data. CNH uses
  `USDCNY=X` as a proxy (no direct `USDCNH` history on Yahoo; onshore and
  offshore yuan trade very closely).
- **Usage**:
  ```bash
  python backtest_forex_universe.py                  # all 8 strategies, all 117 pairs, 3y
  python backtest_forex_universe.py --strategy ema donchian
  python backtest_forex_universe.py --pairs-only-core # skip the 83 exotic pairs
  ```
  Results: `data/forex_universe_backtest.csv` (one row per strategy×pair)
  and `data/forex_universe_backtest_summary.csv` (aggregated per
  strategy×tier — core vs. exotic — so a strategy's core-universe edge can
  be compared directly against its exotic-universe edge).

**Results (3-year run, completed 2026-08-22)** — 936 strategy×pair
combinations with ≥5 trades. Aggregated per strategy×tier:

| Strategy | Tier | Pairs | Avg trades | Avg WR | Avg PF | % pairs PF>1 |
|---|---|--:|--:|--:|--:|--:|
| **zscore** | core | 34 | 10.6 | 67.6% | 2.59 | **73.5%** |
| **zscore** | exotic | 82 | 10.5 | 66.4% | 2.94 | **69.5%** |
| **rsi** | core | 34 | 25.5 | 66.5% | 1.58 | **73.5%** |
| **rsi** | exotic | 82 | 24.6 | 67.1% | 2.87 | **73.2%** |
| **bb** | core | 34 | 18.0 | 52.9% | 1.41 | **67.6%** |
| **bb** | exotic | 83 | 18.6 | 54.5% | 1.91 | **67.5%** |
| ml | core | 34 | 10.4 | 44.6% | 1.22 | 50.0% |
| ml | exotic | 83 | 13.1 | 50.1% | 2.39 | 56.6% |
| pullback | core | 26 | 9.9 | 22.4% | 0.88 | 42.3% |
| pullback | exotic | 69 | 11.1 | 28.9% | 1.20 | 37.7% |
| ema | core | 14 | 6.6 | 27.3% | 0.96 | 35.7% |
| ema | exotic | 31 | 7.1 | 22.9% | 0.68 | **19.4%** |
| donchian | core | 20 | 6.8 | 26.7% | 0.74 | 35.0% |
| donchian | exotic | 64 | 7.3 | 33.4% | 2.21 | 40.6% |
| supertrend | core | 27 | 7.0 | 23.2% | 0.54 | **18.5%** |
| supertrend | exotic | 46 | 7.5 | 28.8% | 0.83 | 26.1% |

**Read this as a genuine finding, not just an exotic-universe validation
check**: `zscore`, `rsi`, and `bb` show real, consistent historical edge on
BOTH the core and exotic universes (65–75% of pairs individually
profitable, healthy profit factors, no core→exotic dropoff). But
`ema`, `donchian`, `pullback`, and especially `supertrend` show weak or
outright negative average profit factor **on the core universe they've
already been live-trading on**, not just the new exotic pairs — this isn't
only an "exotic pairs are unvalidated" gap, it's evidence that 4 of the 10
currently-live strategies may not have real edge even where they're
already running. `ema`'s exotic-tier drop to 19.4% of pairs profitable is
the sharpest core→exotic decline in the whole table. Avg PF can be pulled
upward by one or two outlier pairs (e.g. donchian-exotic's 2.21 average
sits oddly next to only 40.6% of pairs actually being profitable) — the
"% pairs PF>1" column is the more honest single number per strategy.

**Caveats, read before acting on this table**:
- Exotic-pair (and some core cross-pair, e.g. EURGBP/EURJPY) prices are
  synthesized via USD-leg triangulation, not independently sourced —
  approximated High/Low specifically could distort ATR-dependent exits
  (affects `donchian`, `supertrend`, `pullback`, `ema` more than
  RSI/BB/z-score, which lean less on ATR-based stops).
- This tests each strategy's RAW per-pair signal quality in isolation — it
  does not replicate the live portfolio's slot competition,
  `signal_filter`'s consensus/ML meta-filter, or the momentum pre-filter
  applied in production, all of which could move real results in either
  direction.
- Per-pair trade counts are thin for some strategies (donchian/ema/
  supertrend average 6-8 trades per pair over 3 years) — aggregated across
  many pairs the total sample is reasonable, but any single pair's number
  should not be over-trusted.
- `gap` and `london_breakout` are not in this table — both need intraday
  H1 data this backtest can't source historically.

**Recommendation**: treat `ema`, `donchian`, `pullback`, and `supertrend`
as needing a closer look (parameter re-tuning or reconsidering whether
they belong in the live rotation at all) before trusting them with
materially more capital — `zscore`, `rsi`, and `bb` are the three with
the most consistent historical support across both universes.

---

## Universe — 34 Core Pairs (+ 83 SIM-only exotic, see Audit above)

**Correction (2026-08-21)**: the "Asian Session — 14 pairs" / "London Session
— 20 pairs" labels below describe the *names* of the scheduled tasks, not an
actual pair filter — `run_forex_daily.bat` and `run_forex_london.bat` stopped
passing `--session asian`/`--session london` on 2026-08-20 (confirmed by
reading the live `.bat` files directly), so both tasks scan the **full
universe** (117 pairs as of 2026-08-21) for entries and exits, every time
they fire. The lists below are kept for reference (they still define
`SESSION_PAIRS` in code, used only when a `--session` flag is explicitly
passed manually) but do not reflect what the scheduled tasks actually do.

### "Asian Session" pair grouping (14 pairs) — not currently used to filter live runs
`USDJPY` `EURJPY` `GBPJPY` `AUDJPY` `CADJPY` `NZDJPY` `CHFJPY`  
`AUDUSD` `NZDUSD` `AUDCAD` `AUDCHF` `AUDNZD` `NZDCAD` `NZDCHF`

### "London Session" pair grouping (20 pairs) — not currently used to filter live runs
`EURUSD` `GBPUSD` `USDCAD` `USDCHF`  
`EURGBP` `EURAUD` `EURNZD` `EURCAD` `EURCHF`  
`GBPAUD` `GBPCAD` `GBPCHF` `GBPNZD`  
`CADCHF` `EURNOK` `EURSEK` `USDNOK` `USDSEK` `USDDKK` `USDMXN`

### Gap Fill — all 117 pairs (Mon 03:00 PKT / Sun 22:00 UTC)
All pairs scanned; only those showing a 0.10%–2.00% gap receive entries.

### UICs (Saxo SIM)
| Pair | UIC | Status | Pair | UIC | Status |
|------|-----|--------|------|-----|--------|
| EURUSD | 21 | ✓ confirmed | EURGBP | 17 | ✓ confirmed |
| GBPUSD | 31 | ✓ confirmed | EURJPY | 18 | ✓ confirmed |
| USDJPY | 42 | ✓ confirmed | GBPJPY | 26 | ✓ confirmed |
| AUDUSD | 4  | ✓ confirmed | AUDJPY | 2  | ✓ confirmed |
| USDCAD | 38 | ✓ confirmed | CADJPY | 6  | ✓ confirmed |
| NZDUSD | 37 | ✓ confirmed | CHFJPY | 8  | ✓ confirmed |
| USDCHF | 39 | ✓ confirmed | NZDJPY | 36 | ✓ confirmed |
| AUDCAD | 1  | ✓ confirmed | NZDCAD | 33 | ✓ confirmed |
| AUDCHF | 5027 | ✓ confirmed | NZDCHF | 34 | ✓ confirmed |
| AUDNZD | 3  | ✓ confirmed | EURAUD | 12 | ✓ confirmed |
| EURNZD | 2072 | ✓ confirmed | EURCAD | 13 | ✓ confirmed |
| EURCHF | 14 | ✓ confirmed | GBPAUD | 22 | ✓ confirmed |
| GBPCAD | 23 | ✓ confirmed | GBPCHF | 24 | ✓ confirmed |
| GBPNZD | 28 | ✓ confirmed | | | |
| CADCHF | 7  | inferred (verify) | EURNOK | 19 | ✓ confirmed 2026-08-21 |
| EURSEK | 20 | ✓ confirmed 2026-08-21 | USDNOK | 43 | ✓ **corrected** 2026-08-21 (was 40 — that's USDCZK) |
| USDSEK | 44 | ✓ **corrected** 2026-08-21 (was 41) | USDDKK | 41 | ✓ **corrected** 2026-08-21 (was 43) |
| USDMXN | 1348 | ✓ **corrected** 2026-08-21 (was 44 — off by >1300) | | | |

> **2026-08-21**: 4 of these 6 "inferred" UICs (USDNOK, USDSEK, USDDKK, USDMXN)
> turned out wrong when actually checked against Saxo's `/ref/v1/instruments` —
> they were never verified, just guessed from sequential numbering. Corrected in
> `forex/universe.py` and above. Lesson: never trust an "inferred, verify with
> --info" UIC in production without actually running that verification.
>
> The universe also expanded to 117 pairs total (2026-08-21, SIM-only) — see the
> Audit section at the top of this doc. This table covers only the original 34;
> `forex/universe.py` is the source of truth for the full list.

> **Verify new UICs**: run `python forex/runner.py --info`

---

## CLI Reference

```bash
# Run all 10 swing strategies — all pairs
python forex/runner.py --live

# Session-aware runs (as used by Task Scheduler)
python forex/runner.py --live --session asian    # 06:20 PKT
python forex/runner.py --live --session london   # 18:00 PKT
python forex/runner.py --exits-only --live       # 14:00 PKT (stops only)

# London Breakout day-trading strategy (runs INDEPENDENTLY)
python forex/runner.py --strategy london_breakout --live           # entries (auto-detects London vs NY)
python forex/runner.py --strategy london_breakout --exits-only --live   # force-close
python forex/runner.py --strategy london_breakout --scan               # session range dashboard

# Single swing strategy
python forex/runner.py --live --strategy pullback
python forex/runner.py --live --strategy gap        # Sunday 22:00 PKT
python forex/runner.py --live --strategy supertrend
python forex/runner.py --live --strategy zscore
python forex/runner.py --live --strategy ml
python forex/runner.py --live --strategy cnn_lstm   # requires trained model

# Diagnostics
python forex/runner.py --scan      # 11-panel market snapshot (all strategies)
python forex/runner.py --status    # open positions + currency exposure
python forex/runner.py --info      # verify UICs live via Saxo API

# LBO test suite
python test_london_breakout.py     # 57 tests — run before any LBO change

# CNN-LSTM model management
python -m forex.cnn_lstm_trainer --train          # train on all 34 pairs (5y data)
python -m forex.cnn_lstm_trainer --train --pairs EURUSD GBPUSD  # specific pairs
python -m forex.cnn_lstm_trainer --status         # show walk-forward accuracy
python -m forex.cnn_lstm_trainer --backtest       # re-run validation without retraining
python -m forex.cnn_lstm_trainer --train --epochs 50  # quick test (fewer epochs)
```

---

---

## Strategy 11 — London Breakout (Day Trading) ★★

**File**: `forex/strategy_london_breakout.py`  
**Type**: Day Trading — Session Range Breakout  
**Win Rate**: ~58–63%  
**Capital**: 15,000 SEK dedicated book (independent from swing positions)  
**Slots**: 7 (one per pair, all 7 majors eligible per session)  
**No overnight holds** — all positions closed by 20:00 UTC (01:00 PKT)  

### Concept
FX markets compress during low-liquidity sessions, then release directionally when institutional flows kick in at session opens. We trade the **first directional break** of the compression range at London open (07:00 UTC) and NY open (13:00 UTC).

The stop is on the **opposite boundary of the reference range** — the level that invalidates the breakout thesis. The target is **2× the range size** — giving a 2:1 R/R on every trade. Range size filters (10–120 pips) eliminate thin sessions and chaotic sessions.

**This strategy is isolated from the swing book.** It bypasses portfolio heat checks and the swing drawdown gate via the `DAY_TRADE_STRATEGIES` set in the runner. Only the hard daily loss limit (–3% equity) applies to both books.

### Session Logic

| Session | Entry Window | Reference Range | Target Pairs |
|---------|-------------|-----------------|-------------|
| **London** | 07:00–10:00 UTC | Asian session H1 high/low (00:00–06:59 UTC) | EURUSD, GBPUSD, USDJPY, EURGBP, GBPJPY, AUDUSD, USDCAD |
| **NY** | 13:00–15:00 UTC | London morning H1 high/low (09:00–12:59 UTC) | Same 7 pairs |

### Entry — H1 bar confirmation required
| Direction | Conditions |
|-----------|-----------|
| **BUY**  | H1 close > range_high AND range is 10–120 pips |
| **SELL** | H1 close < range_low AND range is 10–120 pips |

### Exit (first condition hit)
- **A — Take profit**: price hits entry ± 2.0 × range size  
- **B — Stop loss**: price hits opposite range boundary  
- **C — Time stop**: 20:00 UTC hard close (no overnight holds)

### Parameters
| Param | Value |
|-------|-------|
| Pairs | EURUSD, GBPUSD, USDJPY, EURGBP, GBPJPY, AUDUSD, USDCAD |
| London entry window | 07:00–10:00 UTC |
| NY entry window | 13:00–15:00 UTC |
| Asian range hours | 00:00–06:59 UTC (H1 bars) |
| London morning range | 09:00–12:59 UTC (H1 bars) |
| Min range | 10 pips |
| Max range | 120 pips |
| TP ratio | 2.0 × range |
| Stop | Opposite range boundary |
| Time stop | 20:00 UTC |
| Risk per trade | 1.5% of equity |
| Capital | 15,000 SEK dedicated |
| Data | H1 bars (last 48 hours via Saxo API) |

### Position Sizing (15,000 SEK book)
```
risk_SEK = 15,000 × 0.015 = 225 SEK ≈ $21 USD
stop_distance = range_size (in price)
units = risk_USD / stop_distance
        → capped at 1,000 min / 50,000 max
```

### Scheduled Tasks (Task Scheduler)
| Task name | Trigger | Action |
|-----------|---------|--------|
| `lbo-london-open` | Mon–Fri 12:00 PKT (07:00 UTC) | `run_lbo_london.bat` — entries |
| `lbo-ny-open` | Mon–Fri 18:00 PKT (13:00 UTC) | `run_lbo_ny.bat` — entries |
| `lbo-force-close` | Daily 01:00 PKT (20:00 UTC) | `run_lbo_close.bat` — exits only |

### Email Alerts
Every open and close fires an immediate email via `forex/notifier.py`:
- **Open alert**: pair, direction, entry, stop, TP, R/R, risk in SEK, session name
- **Close alert**: WIN/LOSS badge, P&L % and SEK, exit reason (TP / SL / time_stop)

### Key Files
| File | Purpose |
|------|---------|
| `forex/strategy_london_breakout.py` | Strategy: signal generation, exit logic, scan summary |
| `run_lbo_london.bat` | London open launcher (hidden window via VBS) |
| `run_lbo_ny.bat` | NY open launcher |
| `run_lbo_close.bat` | Force-close launcher (exits only) |
| `test_london_breakout.py` | 57-test suite (unit / functional / blackbox / edge) |

### Test Suite
```powershell
python test_london_breakout.py
# → 57/57 PASS  (unit, functional, exit, scan, blackbox, edge cases)
```

---

## Currency Exposure Filter

The runner enforces `MAX_CURRENCY_EXPOSURE = 3` — at most **±3 net positions** per currency across all strategies simultaneously.

**Example**: If you already have 3 long positions involving USD (EURUSD short, GBPUSD short, USDJPY long), any new signal that would add a 4th USD long or short is **skipped** with a log message.

This prevents correlated drawdowns where 4+ strategies all lose simultaneously on the same currency move.
