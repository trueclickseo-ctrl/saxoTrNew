# Forex LIVE Account — Real Money

**Status**: TWO real-money accounts now live under the same Saxo login.

1. **SEK account** (`--account live`) — fully armed (`SAXO_LIVE_CONFIRMED=1` set, scheduled tasks registered) since 2026-08-25. **First real trade placed 2026-08-25, 23:08 PKT**: `donchian` opened EURNOK short (1,000 @ 10.86975, stop 10.98368, TP 10.52803) and GBPUSD long (1,000 @ 1.36466, stop 1.35165, TP 1.39047) — both bracket orders verified correct against Saxo's own web trader, `housekeeping_live`/`safeguard_live` confirmed clean immediately after.
2. **EUR account** (`--account live_eur`) — fully armed (`SAXO_LIVE_EUR_CONFIRMED=1` set, scheduled tasks registered) since 2026-08-26. RSI Pullback only. As of 2026-08-28, trades the **same 17-pair `HIGH_VOLUME_SYMBOLS` universe as the SEK account** (no exotic pairs live any longer) — safe because Saxo's pooled position/order records carry a genuine per-record `AccountKey` (verified live 2026-08-28), so `housekeeping_live_eur.py` attributes each record to the correct account directly instead of relying on non-overlapping pair sets. Legacy open EXOTIC-pair positions from this account's original design are still tracked/protected the same way. Sized off a 500 EUR code-level cap. See "Second real-money account: EUR sub-account" below for the full design and important findings about how Saxo's API behaves for this multi-currency Client.

The rest of this document describes the SEK account in depth; the EUR account section below cross-references it rather than repeating shared concepts.

**See also**: [forex_live_strategies.md](forex_live_strategies.md) (entry/exit rules, in depth) and [forex_live_scheduler.md](forex_live_scheduler.md) (every scheduled task, exact trigger times, SIM-conflict history).

---

## Current configuration (authoritative — updated 2026-08-31)

Much of the prose below this section predates the 2026-08-28 two-account
redesign and the 2026-08-29/30/31 tuning. Where they disagree, this table wins.

| Setting | SEK account (`--account live`) | EUR account (`--account live_eur`) |
|---|---|---|
| Strategy | `rsi` only (`LIVE_ALLOWED_STRATEGIES`, changed `{"bb"}`→`{"rsi"}` on 2026-08-31 — both accounts run RSI now) | `rsi` only (`LIVE_EUR_ALLOWED_STRATEGIES`) — **active** |
| Universe | 17-pair `HIGH_VOLUME_SYMBOLS` (deliberately **not** expanded to 49 — caps the extra exposure from running the same strategy twice to the highest-liquidity pairs) | 49-pair `CORE_SYMBOLS` |
| Overlap | The 17 HIGH_VOLUME pairs are traded on **both** accounts → every signal on those is taken twice, ~2× per-signal real-money exposure on the shared Saxo margin pool (user-confirmed 2026-08-31). Attribution stays clean via per-record `AccountKey`. | |
| Scheduler | `ATOS Forex LIVE Daily Run` / `Exit Check` — **enabled 2026-08-31** (Windows quota error on the enable was flaky, state changed anyway) | `ATOS Forex LIVE EUR Daily Run` / `Exit Check` — active |
| Legacy positions | 4 open `donchian:` positions — **now fully exit-managed by donchian's own rules** (`2aa38c0`+, `_legacy_exit_strategies()` runs `_run_exits("donchian", …)` in both `run_daily` and `run_exits_only` for any held strategy outside the entry allowlist; entries stay `rsi`-only). Donchian 15-day channel-break exit, ATR trail, `TIME_STOP_DAYS`, and broker GTC bracket all apply. (Before 2026-09-01: frozen entry-day broker bracket only — "close manually".) | legacy EXOTIC `rsi:` positions (e.g. GBPPLN) ARE now exit-managed — `_add_held_position_history()` (2026-08-31) fetches their history even though they sit outside the 49-pair CORE universe, so the RSI ladder / recovery / 12-day time stop apply. NB: some exotics have a distorted Ask on Saxo's chart endpoint (GBPPLN CloseAsk 5.13 vs CloseBid 5.01) so the mid used for exit checks runs ~0.5% optimistic — acceptable vs no management; the actual close order uses a fresh live quote. |
| Sizing cap | `risk_equity_sek: 15000` | `risk_equity_eur: 8000` (1,350 → 6,000 on 2026-08-29 → **8,000 on 2026-08-30**, ahead of an 18k SEK deposit). Real pooled balance is ~15,800 SEK ≈ €1,400, so €8,000 ≈ 2.5× — a deliberate leverage choice. |
| Risk per trade | **RSI: fixed ~€45 loss-if-stopped** (`RSI_LIVE_FIXED_RISK_EUR = 45.0`, 2026-08-31), uniform across pairs — overrides the 0.75% for RSI. `LIVE_RISK_PCT_OVERRIDE = 0.0075` still applies to any non-RSI strategy. | same |
| Trading halt | `LIVE_TRADING_HALTED = False` (lifted 2026-08-28 by explicit go-ahead) | same |

**Gates between a signal and a real order (LIVE only; SIM has none of these):**

- **Weekend market-hours gate** (2026-08-29) — no new entries while FX is
  closed (`_fx_market_open()`: Fri ~22:00 → Sun ~22:00 UTC). Exits and
  stop-management still run every cycle. Signals that fire during the
  closure are still generated and **emailed** (`send_signals_detected`,
  `market_closed=True`) so nothing is silently swallowed; they are
  re-evaluated on fresh data at reopen, never left as resting orders.
- **Per-currency exposure cap** — `LIVE_MAX_CURRENCY_EXPOSURE = 5` (was 1;
  raised 2026-08-29 because the cap of 1 let one position consume a whole
  currency slot and blocked nearly every subsequent RSI signal). SIM stays
  unlimited (999).
- **Cost-clearance gate** — a signal whose own target can't clear
  `MIN_EDGE_TO_COST_RATIO = 3.0 ×` Saxo's real round-trip commission is
  skipped. Bigger position size (see the RSI lot ladder) makes more
  signals clear this.
- **RSI fixed per-trade risk** (`RSI_LIVE_FIXED_RISK_EUR = 45.0`,
  2026-08-31) — RSI on both real-money accounts sizes for a **uniform
  ~€45 loss if the stop is hit**, on every pair regardless of stop width.
  qty rounds **up** to Saxo's 1,000-unit increment (realised risk ≥ €45,
  typically €45–55), capped at 100,000 units. Replaced the 2026-08-29
  equity-% + `_snap_rsi_live_lot` 10k-ladder combo, which gave wildly
  uneven realised risk (~€8 on MXNUSD vs ~€73 on GBPUSD). At €45 risk /
  €90 target the flat ~€10 round-trip commission is ~11% of the target —
  well clear of the cost gate. SIM untouched. Set the constant to `None`
  to restore the 10k ladder (`_snap_rsi_live_lot` is unchanged, just the
  fallback now).
- **Portfolio heat cap** — 6% of the sizing base in combined open risk
  (re-enabled for LIVE/LIVE_EUR 2026-08-28). **2026-08-30: `rsi` gets an
  8% cap** (`_HEAT_LIMIT_BY_STRATEGY = {"rsi": 0.08}`) ≈ 10 concurrent RSI
  positions; every other strategy + the SEK account stay at 6%. Known gap:
  each account's heat check sees only its own positions, not the other
  account's against the same pooled Saxo balance — Saxo's real 50% margin
  cap is the shared hard backstop.
- **Margin cap** — never uses more than 50% of available broker margin.

**Emails per LIVE run:** the run-summary email (`send_run_summary`) **plus**
a signals-detected email whenever that scan produced signals. The
run-summary "Positions" metric counts `strategy:symbol` keys; it also
shows the distinct-pair count so it reconciles with the dashboard header
(`forex_dashboard.py`: "N positions in P/total pairs").

---

**Module**: `forex/runner.py --account live` (same codebase as SIM, account-scoped via `set_account_env()`)
**Account**: Saxo LIVE, sub-account `1070996INET`, SEK-denominated, opened with 6,000 SEK
**Strategies**: as of 2026-08-28, `bb` only (history: `donchian`/`ema`/`rsi` -> `bb`/`rsi` -> briefly `bb`/`rsi`/`pullback` -> `bb`/`rsi` -> `bb` only once `rsi` moved exclusively to the EUR account) -- hard-restricted in code via `LIVE_ALLOWED_STRATEGIES`, not just by convention
**Universe**: the 17-pair `HIGH_VOLUME_SYMBOLS` subset of `CORE_SYMBOLS` (narrowed 2026-08-27 from all 34 core pairs) -- no exotic (hard-filtered in code via `_filter_pairs_for_account()`)
**Separate from SIM**: own Saxo app/login, own state/orders files, own pnl_tracker module (`forex_live`), own strategy-learner weights, own signal-filter/ML training data, own cross-process lock, own capital cap. Nothing is shared with the SIM account except the source code itself.

---

## Why this exists

SIM (`forex/runner.py` default, no `--account` flag) runs all 11 strategies across 117 pairs (34 core + 83 exotic) for continued testing. The dashboard's Core-vs-Exotic split (`forex_dashboard.py`) showed core pairs dramatically outperforming exotic (profit factor 8.19 vs 0.01, win rate 34.6% vs 3.1%, all-strategies blended). Of the 11 strategies, `donchian`/`ema`/`rsi` had the strongest, most consistent track record on core pairs specifically. This account is the real-money expression of that finding — deliberately narrow (3 strategies, 34 pairs, small capital) rather than a full mirror of SIM.

---

## Risk & sizing

- **Risk per trade**: 0.25% of current equity (`RISK_PCT = 0.0025`, identical across all 3 strategies). On 6,000 SEK, ~15 SEK risked per trade if the stop is hit.
- **Position size**: `risk_amount / (ATR_STOP_MULT × ATR)`, floored at a 1,000-unit minimum per pair. Not a fixed lot size — scales with each pair's own volatility.
- **Capital does NOT split across simultaneous signals.** Each signal is sized independently off the same 0.25% budget; multiple positions can be open at once. What actually caps concurrent exposure:
  - **Portfolio heat cap**: 6% of equity in combined open risk (~360 SEK) pauses new entries once hit — room for roughly 24 simultaneous 0.25% positions before this binds.
  - **Margin cap**: never uses more than 50% of available broker margin (`_margin_allows_entry()`).
  - Note: the SIM-only daily-loss-limit/drawdown circuit breaker is present in the code but explicitly disabled from blocking entries (a 2026-08-24 decision for SIM fairness-of-sampling) — it still computes and logs, it just doesn't gate. This applies to LIVE too since it's the same code path; worth revisiting if LIVE's risk posture should differ from SIM's here.
- **Profit target**: fixed 2:1 reward:risk for all three strategies (`DEFAULT_TP_RR = 2.0`), placed as a resting broker-side Limit order at entry ± 2× the stop's own distance. The *ratio* is fixed; the absolute SEK amount varies by pair (ATR-driven).
- **Stop-loss**: every entry gets a genuine Saxo-side protective stop placed atomically with the entry order (`saxo_order.place_with_stop()`), not dependent on a later scheduled run to add protection.

---

## Entry & exit criteria (all daily-bar-based — no intraday/tick strategies here)

Full detail (per-strategy sections, shared mechanics table, what's deliberately not live) in [forex_live_strategies.md](forex_live_strategies.md). Summary:

| Strategy | Entry | Exit | Time-stop |
|---|---|---|---|
| **Donchian Breakout** (`donchian`) | Close breaks above/below the prior 30-day high/low, AND price is on the trend side of EMA(200), AND ADX(14) ≥ 25 | 15-day opposite-channel break, or hard stop (2.0× ATR) | 30 calendar days |
| **EMA Trend** (`ema`) | EMA(5)/EMA(30) crossover within the last 15 bars (not just the exact crossover day), with ADX(14) ≥ 25 | Opposite crossover, or hard stop (1.5× ATR) | 45 calendar days |
| **RSI Pullback** (`rsi`) | RSI(2) ≤ 10 within an EMA(200) uptrend (long) or RSI(2) ≥ 90 within a downtrend (short) — a pullback *within* a trend, not a reversal call | RSI reverts past its own exit threshold ("rsi_recovery"), or hard stop (1.5× ATR) | 12 calendar days (much faster turnover than the other two) |

**Naming note**: "RSI Pullback" (dashboard label for the `rsi` strategy) and the separate `pullback` strategy ("EMA Pullback ★", EMA(20)-in-EMA(50) pullback) are two different things that happen to share a word. Only `rsi` is live; the standalone `pullback` strategy stays SIM-only.

**Performance so far (small samples — 2-3 closed trades each on core pairs)**: Donchian and EMA both 100% win rate (2W/0L) with the largest gains; RSI Pullback also 100% (3W/0L) with smaller individual wins but much faster turnover. Genuinely too early to call a "best" strategy.

---

## Scheduling

Full detail (including the SIM wall-clock conflict resolution) in [forex_live_scheduler.md](forex_live_scheduler.md). Summary:

Three Windows Scheduled Tasks:

- **`ATOS Forex LIVE Daily Run`** (created via `setup_scheduler_live.ps1`, Administrator) — **every 45 min, 06:00-03:00 PKT** (moved from 9 fixed times/day 2026-08-25, explicit user request; window extended from 22:00 to 03:00 PKT 2026-08-26 to cover the tail of the NY session, which doesn't actually roll over until ~17:00 ET / ~02:00 PKT). Checks all 34 core pairs every run, for both new entries AND exits (stop-loss/TP/time-stop) together — the reasoning is that a signal reads "today's still-forming daily candle," which keeps updating through the day as price moves, so frequent re-checks catch a breakout/crossover as it develops.
- **`ATOS Forex LIVE Exit Check`** — once daily at `14:00`. Backstop only — Daily Run above already checks exits every 45 min.
- **`ATOS Saxo LIVE Token Keepalive`** (created via `setup_saxo_live_keepalive.ps1`, Administrator) — **every 10 min, all day, runs as SYSTEM**. Calls `saxo_auth.get_valid_access_token(env="live")` to keep the refresh-token chain alive; added 2026-08-25 after finding LIVE's app issues a 20-min access token / 1-hour refresh token — far shorter than SIM's 24h token — which the old 9-times/2h-gap schedule couldn't keep alive between runs. **Hardened 2026-08-30** (it kept dying on every reboot/sleep gap → manual browser re-login): SYSTEM principal + `StartWhenAvailable` + 15→10 min + 3× restart-on-failure, so a reboot landing before auto-logon can't take it offline. SYSTEM needs `SAXO_LIVE_APP_KEY` as a **Machine** env var (it's a public client id, not a secret — no `SAXO_LIVE_APP_SECRET` exists). Doesn't replace the one-time interactive login (`python saxo_auth.py --live`) itself.

Both run `run_forex_live_daily.bat` / `run_forex_live_exits.bat`, which call:
```
python forex\runner.py --account live --strategy donchian,ema,rsi --live [--exits-only]
```
Logs to `data/forex_live_scheduler.log` (separate from SIM's much noisier `forex_scheduler.log`).

### FX session reference (for context on the 9 scan times)
| Session | UTC | PKT (UTC+5) | Best for |
|---|---|---|---|
| Asian (Tokyo/Sydney) | 00:00–09:00 | 05:00–14:00 | JPY, AUD, NZD pairs (14 of the 34 core) |
| London | 07:00–16:00 | 12:00–21:00 | EUR, GBP, CHF, NOK, SEK, DKK (20 of the 34 core) |
| London–NY overlap | 12:00–16:00 | 17:00–21:00 | Deepest liquidity globally; covers the bulk of the pair list at once |

Verified: `SESSION_PAIRS["asian"]` (14) + `SESSION_PAIRS["london"]` (20) exactly equal all 34 `CORE_SYMBOLS` — no gaps, no overlap. There is no separate "NY-only" pair subset in this codebase; USD-crosses are already grouped under "london."

---

## Safety rails (hard-coded, not just convention)

1. **Strategy allowlist**: `--account live` with any strategy outside `{donchian, ema, rsi}` is a hard CLI error (`ap.error()`, exit 2) — not a warning, a refusal to run.
2. **Explicit confirmation gate**: `--account live --live` also requires `SAXO_LIVE_CONFIRMED=1` set in the environment, checked *separately* from `--live` itself — a copied/scheduled command can't silently place a real order on a machine that hasn't deliberately opted in.
3. **Pair filter**: every strategy's scan list is intersected with `CORE_SYMBOLS` before signal generation under `--account live` — an exotic pair can never reach a live signal.
4. **Currency-aware account selection**: the LIVE login controls 3 sub-accounts (SEK/EUR/USD) — `saxo_client.get_account_key()` and `forex/runner.py`'s `_account()` explicitly match `Currency == "SEK"` rather than trusting list order, and hard-error if ambiguous rather than guessing.
5. **Separate cross-process lock** (`proc_lock.FOREX_LIVE_LOCK`) — LIVE runs never contend with SIM's `intraday_monitor.py` (which re-acquires the SIM lock every minute).
6. **Post-run reconciliation + auto-fix**: `safeguard_live.run_safeguard_live()` runs after every live invocation — fetches a fresh LIVE Saxo snapshot, places a protective stop on any naked position it finds, resolves state mismatches, then **re-fetches and verifies** each fix actually took before reporting it as fixed. See "Housekeeping & safeguard" below — this is now a real auto-fix agent, not just a report, built proactively on 2026-08-25 before any real trade happened.

---

## What's fully separate from SIM (and why)

| Concern | SIM | LIVE | Why separated |
|---|---|---|---|
| Saxo app/login | `saxo_token.json`, SIM app key | `saxo_token_live.json`, `SAXO_LIVE_APP_KEY` | Different Saxo environments entirely |
| Gateway | `sim/openapi` | `openapi` (no `/sim/`) | Real vs simulated orders |
| State/orders | `forex_state.json` | `forex_live_state.json` | Different account, different positions |
| P&L ledger | `pnl_tracker` module `"forex"` | module `"forex_live"` | So a blended `get_summary()` call never mixes real SEK P&L with SIM's demo credit |
| Strategy learning weights | `data/forex_strategy_weights.json` | `data/forex_live_strategy_weights.json` | LIVE starts neutral (1.0x every strategy), not biased by SIM's 117-pair/117-trade history |
| Consensus/ML signal filter | `fx_signal_log.csv`, `fx_meta_model.pkl` | `forex_live_signal_log.csv`, `forex_live_meta_model.pkl` | Same reasoning — LIVE's own training data, not SIM's |
| Capital cap | `risk_equity_eur: 27800` | `risk_equity_sek: 6000` | Different currency, different real account size |
| Cross-process lock | `forex_runner.lock` | `forex_live_runner.lock` | SIM's every-minute intraday monitor never blocks a live run |
| Dashboard | `forex_dashboard.py` | `forex_live_dashboard.py` | LIVE's SEK-based P&L conversion (`_sek_per_unit`), not SIM's EUR-based one |

Deliberate exception: source code (strategy logic, `saxo_order.py`'s bracket-order placement, etc.) is fully shared — the point was to reuse the SIM-hardened, bug-fixed trading logic, not rewrite it.

---

## Housekeeping & safeguard (own module, built 2026-08-25 — before any real trade)

SIM's `housekeeping.py`/`safeguard.py` reconcile local state against a live Saxo snapshot and auto-fix naked positions; they were built *reactively* on 2026-08-24 after a live SIM run surfaced 23 unprotected positions and 8 state mismatches in one day. LIVE gets the equivalent safety net *proactively*, before that kind of incident has any chance to happen for real money — but per explicit user direction ("do not use any of SIM account, always build new for ATOS live"), it is **two entirely separate files**, not a parameter or subclass hanging off SIM's:

- **`housekeeping_live.py`** — `ForexLiveAdapter` (inherits only from the generic `housekeeping.BaseAdapter`, never `housekeeping.ForexAdapter`), `fetch_live_snapshot()` (always `env="live"`, its own function — not `housekeeping.fetch_live_snapshot()` with a parameter), `reconcile_live_forex()`, `scan_naked_positions_live()`, `_scan_fully_untracked()` (LIVE's own zero-local-footprint sweep), and `[LIVE]`-tagged email helpers.
- **`safeguard_live.py`** — `run_safeguard_live()`: fetches one live snapshot, places a conservative protective stop (2% from current price) on every naked position found, resolves mismatches via `reconcile_live_forex(aggressive=True)`, then **re-fetches a fresh snapshot and verifies** each fix actually stuck before calling it "fixed" — a fix that looks successful but fails verification is downgraded to NOT FIXED, never reported as done on faith. Sends one `[LIVE]`-tagged summary email only if there was something to do.

What's reused from SIM's `housekeeping.py`: only generic, account-agnostic building blocks — the `LocalPosition`/`Finding`/`LiveSnapshot` dataclasses, the `reconcile_module()` diffing algorithm (a pure function, no SIM-specific behavior), and `_symbol_hint()`. Never `housekeeping.ADAPTERS`, `reconcile_all()`, `scan_naked_positions()`, or `ForexAdapter` — those stay SIM-only, unchanged, untouched by any of this.

`forex/runner.py` dispatches to `safeguard_live.run_safeguard_live()` after every `--account live` invocation, and to SIM's own `safeguard.py` after every SIM invocation — never the other way around (source-level checked by the test suite, see below).

---

## Known issues found & fixed during setup (2026-08-25)

| # | Finding | Severity | Status |
|---|---------|----------|--------|
| 1 | Multi-sub-account ambiguity: the LIVE login controls SEK/EUR/USD sub-accounts; code blindly took `Data[0]` from the accounts list | **Critical** — could misdirect a real order | Fixed — explicit `Currency == "SEK"` match, hard-errors if ambiguous |
| 2 | `_equity_in_quote()` unconditionally used SIM's EUR-basis conversion (`_eur_per_unit`) — for LIVE this treated 6,000 SEK equity as if it were 6,000 EUR | **Critical** — ~11x oversizing on every non-SEK-quoted pair | Fixed — new `_sek_per_unit()`, selected via `ACCOUNT_ENV`. No real order was ever placed with the wrong size (caught before any signal fired). |
| 3 | Daily-loss-limit check hardcoded `module="forex"` (SIM) instead of the account-aware module | High — produced a nonsensical "-38,049 SEK today loss" warning on an account with zero trades ever | Fixed |
| 4 | `strategy_learner`/`signal_filter` had no LIVE/SIM separation at all (see table above) | High — LIVE's strategy weighting and ML training data would have silently mixed with SIM's | Fixed |
| 5 | `intraday_monitor.py` (SIM-only) shares a cross-process lock with LIVE despite touching entirely different accounts/files — a live run sat polling against an unrelated process for several minutes | Medium (operational, not financial) | Fixed — separate `FOREX_LIVE_LOCK` |
| 6 | `SAXO_LIVE_APP_KEY`/`SAXO_LIVE_CONFIRMED` env-var propagation: `SetEnvironmentVariable(...,"User")` does not retroactively update already-running processes (including Explorer, which every Start-Menu-launched terminal inherits from) | Operational | Resolved via reboot with the value already set beforehand; documented for future reference |
| 7 | `saxo_client._account_key_cache` was a single global value/None, not per-environment | Would have risked a SIM AccountKey leaking into a LIVE order | Fixed — cache keyed by env |
| 8 | `sync_futures_from_json()` had the exact duplicate-open-row bug already fixed for forex's sync on 2026-08-21, never applied to futures | High (data integrity, not this account, but same class) | Fixed |
| 9 | `pnl_tracker.log_close()` never multiplied by contract_size for ContractFutures instruments (e.g. ZC at $50/point) | High (futures P&L understated ~35x) | Fixed going forward; historical rows not retroactively audited |
| 10 | `intraday_monitor.py` closed positions correctly (forex + futures) but never logged them to `trade_logger`/`pnl_tracker` — invisible to every strategy-wise P&L report | High (data integrity) | Fixed |
| 11 | `forex_dashboard.py` and `forex_live_dashboard.py` had no UTF-8 stdout safeguard (`futures_dashboard.py` already did) — either would crash under redirected/piped output or a non-UTF-8 console codepage | Medium (would surface as an unexplained crash if ever invoked non-interactively) | Fixed — found via the property-based/blackbox testing pass below, not a live incident |
| 12 | While building `housekeeping_live.py`: removing the old in-`housekeeping.py` `ForexLiveAdapter`/`reconcile_live_forex()` accidentally deleted the *generic* `_scan_fully_untracked()` helper too — still called by SIM's own `reconcile_all()` — leaving an undefined-name bug in SIM's reconciliation path | **Critical** (SIM-affecting, caught via `pyflakes`, not a live incident) | Fixed — restored the generic function; all 34 SIM `test_housekeeping.py` tests re-verified passing |
| 13 | 3 tests in the account-level suite still asserted the *old* location (`housekeeping.ForexLiveAdapter`/`housekeeping.reconcile_live_forex`) after the split into `housekeeping_live.py` | Test-only (would have been a false regression signal) | Fixed — updated to assert the new module boundary instead |
| 14 | LIVE's app issues a 20-min access token / 1-hour refresh token (SIM's lasts 24h) — with the old 9-fixed-times/~2h-gap schedule, every run past the first hour post-login failed with `TOKEN EXPIRED` and was skipped entirely | High (real operational gap — zero scans could happen for hours at a time; no financial loss since a skipped run places no order, correct or otherwise) | Fixed — `saxo_live_token_keepalive.py`, every 15 min |
| 15 | LIVE's schedule moved to every 45 min the same day, but the `New-ScheduledTaskTrigger` construction needed for a real daily-recurring sub-daily-repeating trigger took 3 attempts to get right (PowerShell's `ScheduledTasks` module rejects `-Daily` + repetition params together) | Operational (would have silently only fired once, like the SIM bug below) | Fixed — see [scheduling.md](scheduling.md)'s 2026-08-25 section for the full account |
| 16 | (SIM, not LIVE, but same-day and same root cause class) An unrelated schedule-conflict-avoidance fix silently deleted SIM's "ATOS Forex Intraday Scan" every-30-min repetition, and `scheduler_watchdog.py` never had a registry entry for that task at all, so nothing alerted for ~2.5h of zero SIM scans | High (SIM signal-detection gap, not LIVE) | Fixed — trigger restored, watchdog registry gap closed, plus 2 real false-positive bugs found and fixed in the watchdog's own new checks along the way |

None of these caused a real financial loss — all were caught by direct questioning/verification/testing, either before the first live trade (1-13) or the same day it happened (14-16).

## Known issues found & fixed post-launch

| # | Finding | Severity | Status |
|---|---------|----------|--------|
| 17 | **Fill price never confirmed** (2026-09-01, `2aa38c0`) — Saxo's order POST returns only an `OrderId`, no fill and no price. `forex/runner.py` (and `atos_runner.py`) recorded every position at `sig["close"]`, the scan chart's last bar close, routinely 10–60 min stale. Confirmed live: MXNUSD LIVE EUR booked entry 0.058876 / exit 0.0588435 vs the real Saxo fills 0.058687 / 0.058811 — a 0.32% entry error (a third of that trade's stop) that flipped the recorded P&L sign and poisoned the R-multiple / MAE-MFE / observation cards / P2 give-back. Same class as the accepted-but-unfilled phantom-position bug on SIM stocks. | High (data integrity — no financial loss; the trades themselves executed fine, only the *recorded* prices were wrong) | Fixed — `_confirm_entry_fill` / `_confirm_exit_fill` poll `positions/me` / `closedpositions/me` for the true average fill via `PositionBase.SourceOrderId` (fallback: same-Uic position opened <180s ago), bounded retries, never raise. `_run_entries` records the real fill; a **LIVE** entry that never fills is **cancelled (entry + both bracket legs) and NOT recorded** — missing a trade beats a phantom. `_run_exits` records the true `ClosingPrice`. Stocks: an unconfirmed SIM entry is cancelled + booked paper. One-time `fix_live_fill_prices_2026-09-01.py` rewrote the 7 open LIVE positions + the MXNUSD round-trip from the live API (state files, `pnl_ledger`, observation cards; marker `entry_price_corrected="saxo-fill-truth-2026-09-01"`). `test_2026_09_01_fill_confirmation.py` (14 ✅). |
| 18 | Duplicate open `GBPUSD` rows in `pnl_ledger.db` for `forex_live_eur` (ids 1820 + 2047) — a re-open recorded a second open row without closing the first | Low (P&L double-count risk in ledger roll-ups only; state files + live Saxo are correct) | Open — flagged 2026-09-01, needs a ledger-hygiene pass |
| 19 | Orphan `CHFAUD` (uic 7) Buy **OCO entry** pair on the SEK account (buy-stop 1.76185 / buy-limit 1.67628, GTC, placed 2026-08-26, never triggered, no position, pair outside the current SEK universe) — **invisible to all current reconciliation**: `reconcile_module()` only examines uics with a local position, `_scan_fully_untracked()` and `scan_naked_positions_live()` only look at live *positions*, so nothing scans for a working *order* on a uic with no position and no local record. Left un-triggered it would eventually fire an unmanaged real-money position. | Medium (dormant, but a real un-managed-entry risk) | Cancelled manually 2026-09-01 (orders `5437227302`/`303`). **Gap remains**: `housekeeping_live.py` needs an orphan-working-order scan (no position + no local key → cancel). Not yet built. |

---

## Second real-money account: EUR sub-account (added 2026-08-26)

**Why this exists**: user wanted to test RSI Pullback, and ONLY RSI Pullback, on the 83 EXOTIC pairs with real money — a focused single-strategy/single-tier experiment, deliberately NOT an addition to the SEK account's existing CORE coverage (that would just duplicate RSI's already-running core signals in a second account). Uses the EUR sub-account under the same Saxo LIVE login that has always controlled 3 sub-accounts (SEK/EUR/USD, see finding #1 in the table above) — the EUR one had simply never been used for trading before this.

**Module**: `forex/runner.py --account live_eur` (same codebase, same `set_account_env()` mechanism as the SEK account)
**Account**: Saxo LIVE, sub-account `1076635INET`, EUR-denominated
**Strategy**: exactly 1 — `rsi` (`LIVE_EUR_ALLOWED_STRATEGIES = {"rsi"}`, hard-restricted in code)
**Universe**: as of 2026-08-28, the same 17-pair `HIGH_VOLUME_SYMBOLS` universe as the SEK account (changed from the original 83 `EXOTIC_SYMBOLS` pairs -- explicit user decision, see finding #2 below for why sharing pairs with the SEK account is safe)
**Sizing cap**: 500 EUR (`atos.capital_config.forex_live_eur_risk_equity_eur()`) — of the 900 EUR actually sitting in that sub-account, only 500 is used as the sizing base
**Separate from both SIM and the SEK account**: own state/orders files (`forex_live_eur_*.json`), own pnl_tracker module (`forex_live_eur`), own strategy-learner weights, own signal-filter/ML training data, own confirmation gate (`SAXO_LIVE_EUR_CONFIRMED`, independent of the SEK account's `SAXO_LIVE_CONFIRMED`). Shares the SEK account's Saxo LOGIN/OAuth token (see finding below) but nothing else.

### Two critical findings about this Client's Saxo API behavior

Both discovered empirically while building this account — worth knowing before extending Saxo API access here further:

1. **Margin/balance is pooled across all 3 sub-accounts, not per-account.** `/port/v1/balances/me` returns the exact same pooled, SEK-denominated Client-Group total (~15,786 SEK) regardless of whether it's queried scoped by this account's own AccountKey, by ClientKey, or unscoped. There is no broker-enforced wall keeping this experiment's risk at exactly 500 EUR — only the code-level `_risk_equity()` cap provides that discipline (the same protection model the SEK account's own 6,000 SEK cap already relies on). `forex_live_eur_dashboard.py` deliberately does NOT label this pooled figure as "this account's equity" — doing so would be a real money-figure error on a real-money dashboard.
2. **Positions/orders are ALSO pooled** — but each record still carries its own correct account attribution. `/port/v1/positions/me` and `/port/v1/orders/me` return the SEK account's real positions even when passing the EUR account's own AccountKey as a QUERY PARAM (that filter simply doesn't work server-side). However, every returned row DOES carry its own genuine `PositionBase.AccountKey` / top-level `AccountKey` field (verified live 2026-08-28) identifying which sub-account actually owns it. `housekeeping_live.py` / `housekeeping_live_eur.py`'s `fetch_live_snapshot()` now filter the raw pooled snapshot by matching that field against `saxo_client.get_account_key(env=...)` for their own account, immediately after fetch, before any reconciliation logic runs. (2026-08-26 through 2026-08-27, before this was found: filtered by pair-tier instead — `EXOTIC_SYMBOLS` for this account, `HIGH_VOLUME_SYMBOLS`/`CORE_SYMBOLS` for the SEK account — which only worked as long as the two accounts' pair sets never overlapped. The AccountKey filter is strictly better and is what makes it safe for both accounts to now trade the identical 17-pair `HIGH_VOLUME_SYMBOLS` universe.)

### Scheduling

Two Windows Scheduled Tasks, registered via `setup_scheduler_live_eur.ps1` (run once, Administrator):

- **`ATOS Forex LIVE EUR Daily Run`** — every 45 min, 06:00-03:00 PKT (same window as the SEK account, correct from day one — see the SEK account's own scheduling section above for why 03:00 not 22:00). Runs `run_forex_live_eur_daily.bat` → `python forex\runner.py --account live_eur --strategy rsi --live`.
- **`ATOS Forex LIVE EUR Exit Check`** — once daily at 14:00, backstop only (Daily Run already checks exits every 45 min). Runs `run_forex_live_eur_exits.bat`.

Both tasks have `WakeToRun` enabled and no battery restriction from creation (matching the fix applied to the SEK account's tasks the same day, after tracing a real multi-hour token-keepalive gap to a laptop sleep/standby event). Logs to `data/forex_live_eur_scheduler.log`. Both registered in `scheduler_watchdog.py`'s `WINDOWS_TASKS` and `INTRADAY_REPEATING_TASKS`.

Uses the SAME `saxo_live_token_keepalive.py` task as the SEK account — no separate keepalive needed, since `saxo_auth._cfg()` normalizes `"live_eur"` to `"live"` (same OAuth login/token, only the trading sub-account AccountKey differs).

### Housekeeping & safeguard

`housekeeping_live_eur.py` / `safeguard_live_eur.py` — same relationship and design as the SEK account's `housekeeping_live.py`/`safeguard_live.py` (see that section above), built proactively before this account's first real trade. The only structural difference is the positions/orders pooling workaround described above. `forex/runner.py`'s post-run hook dispatches to `safeguard_live_eur.run_safeguard_live_eur()` when `ACCOUNT_ENV == "live_eur"` — a dedicated branch, never falls through to SIM's `safeguard.py` or the SEK account's `safeguard_live.py`.

### Dashboard

`forex_live_eur_dashboard.py` — thinner than the SEK account's dashboard since this account is EUR-denominated, the same currency basis SIM's own dashboard uses. Reuses `forex_dashboard.py`'s `_eur_per_unit()`/`_fx_conversion_instruments()`/`_positions_section()`/`_strategy_breakdown_table()` directly rather than rebuilding SEK-style triangulation. Shows the 500 EUR sizing cap prominently and the pooled balance figure honestly labelled as pooled (see finding #1 above).

### Testing

Verified end-to-end against the real Saxo LIVE API (read-only + dry-run only, `--live` never invoked by Claude — see the safety-rail note above): `--account live_eur --info` returns real quotes for exactly 83 pairs; `--account live_eur --strategy donchian` correctly hard-errors; a full dry run correctly resolves the EUR sub-account's own distinct AccountKey and sizes at 500 (capped from the pooled ~15,786 raw figure); `housekeeping_live_eur.py`/`safeguard_live_eur.py` both run clean with zero false positives against the real account (which held 5 real SEK positions at the time — proving the tier-filtering fix works). Full regression suite green: `test_2026_08_25_live_forex_account.py` 61/61 (grew to cover both accounts), `test_housekeeping.py` 34/34, `test_2026_08_25_live_housekeeping_safeguard.py` 26/26, `saxo_client_engine_black_box_test.py` 29/29.

---

## Testing — methodology, checklist, current status

Built and applied deliberately, not ad hoc: **(1)** an explicit checklist of functions/edge-cases/error-paths written before adding test code, **(2)** deterministic tools (static analysis, property-based fuzzing) rather than only hand-picked examples, **(3)** coverage measurement as an objective stopping condition for closing gaps, **(4)** full regression re-run after every fix, reasoning through blast radius rather than only re-testing the bug in isolation.

**Test file**: `test_2026_08_25_live_forex_account.py` — 61 tests, organized into 16 sections. Read its module docstring for the full written checklist (functions in scope, edge cases covered, explicit out-of-scope list).

**Housekeeping/safeguard test file**: `test_2026_08_25_live_housekeeping_safeguard.py` — 26 tests covering: module independence (source-level check, via `tokenize`-stripped code, that `housekeeping_live.py`/`safeguard_live.py` never reference `housekeeping.ADAPTERS`/`reconcile_all`/`scan_naked_positions`/`ForexAdapter`, or SIM's `safeguard.py`), snapshot-fetch env correctness, adapter load/save/replace_stop/cancel_stop, every naked-position classification (none/tp_only/partial/fully-covered/non-FxSpot-skip/zero-net-skip), the fully-untracked scan, `[LIVE]` email tagging, `_fix_naked_position_live()` (direction-correct stop pricing both sides, no-price edge case, Saxo-rejection handling), and `run_safeguard_live()`'s verification loop — including the specific case of a fix that *looks* successful but fails post-verification, which must be downgraded to NOT FIXED rather than reported on faith.

**Tools used** (installed to `./.devtools`, never system site-packages — see `.coveragerc`):
- **pyflakes** (static analysis) across every file touched this session — zero new issues found; every flagged item pre-dates this work (verified against the prior commit).
- **Hypothesis** (property-based testing) — 6 properties run against hundreds of generated examples each, targeting the real-money sizing/conversion math specifically:
  - `size_position()` never sizes below the 1,000-unit floor, is monotonic in equity, and returns exactly the documented floor when ATR≤0 (across all 3 live strategies)
  - `_equity_in_quote()` scales exactly linearly with equity for any positive scale factor
  - `_sek_per_unit()`'s triangulation satisfies `rate == usdsek / usd_ccy` exactly — this specifically guards against the *inverted-ratio* class of mistake that caused the 11x oversizing bug (#2 above)
  - `_risk_equity()` never lets sizing scale off more than the configured cap, for any broker-reported balance
- **coverage.py**, with subprocess tracking enabled (`COVERAGE_PROCESS_START` + `.coveragerc`), run across all 4 test suites combined. Raw combined number (27%) is not meaningful on its own — most of the instrumented files' code is SIM-strategy execution logic outside this account's scope. Used instead to find *specific* untested lines inside the LIVE-added functions, then closed each one: `set_account_env()`'s invalid-input path, `_equity_in_quote()`'s two `None`-return branches, `_risk_equity()` under LIVE, its config-read-failure fallback, and its misconfigured-cap fallback.

**Blackbox tests**: real subprocess invocations (not mocked) of `forex/runner.py`'s CLI and `forex_live_dashboard.py` — confirms the hard rails (disallowed strategy, missing confirmation env var) and the dashboard actually render/exit correctly end-to-end, not just that the underlying functions return the right value in isolation.

**Full regression status** (re-run after the `housekeeping_live.py`/`safeguard_live.py` build): 61/61 in the LIVE-account suite, 26/26 in the housekeeping/safeguard suite, 34/34 in SIM's own `test_housekeeping.py`, 29/29 in `saxo_client_engine_black_box_test.py`. The main session suite (`test_2026_08_22_session_fixes.py`, 95 tests) intermittently shows 1-2 failures traced to a pre-existing environment flake — a live scheduled task holding `logs/monitor_*.log` open causes a transient `PermissionError` on import, plus stale `.lock` files from interrupted subprocess tests — not a code regression; deleting stale lock files before a run and re-running confirms it.

**Re-running**:
```
python test_2026_08_25_live_forex_account.py
python test_2026_08_25_live_housekeeping_safeguard.py
```
Requires `hypothesis` (installed to `./.devtools`, auto-added to `sys.path` by the test file itself — no manual `PYTHONPATH` needed).

**Re-running coverage** (optional, for future gap-closing passes):
```
rm -f .coverage .coverage.*
PYTHONPATH=.devtools COVERAGE_PROCESS_START="$(pwd)/.coveragerc" python -m coverage run --source=forex.runner,forex.signal_filter,saxo_client,saxo_auth,proc_lock,strategy_learner,housekeeping,pnl_tracker test_2026_08_25_live_forex_account.py
# ...repeat for the other 3 suites, no --source flag needed after the first...
python -m coverage combine
python -m coverage report -m
```

---

## Operational reference

**Manual dry-run** (no orders, any account):
```
python forex\runner.py --account live --strategy donchian,ema,rsi
```

**Manual live run** (places real orders if a signal fires):
```
python forex\runner.py --account live --strategy donchian,ema,rsi --live
```

**Re-verify live UICs** (do this if the Saxo LIVE app is ever re-registered, or before trusting a new pair):
```
python forex\runner.py --account live --info
```
Confirmed 2026-08-25: all 34 core pairs' UICs are identical between SIM and LIVE for this account (not guaranteed in general — Saxo often assigns different IDs per environment — but empirically true here).

**Dashboard**:
```
python forex_live_dashboard.py --once      # one-time snapshot
python forex_live_dashboard.py             # refresh every 60s
```

**One-time login** (only needed again if the refresh token expires or the app is disconnected):
```
python saxo_auth.py --live
```

**Emails**: every run (signal found or not) sends a `[LIVE]`-tagged summary email; every close sends an immediate `[LIVE]`-tagged win/loss alert. Token-expiry alerts are also `[LIVE]`-tagged.

**Turning it off**: remove `SAXO_LIVE_CONFIRMED` (or set to any value other than `"1"`) to stop real order placement while leaving the scheduled tasks in place, or unregister the tasks entirely:
```powershell
Unregister-ScheduledTask -TaskName "ATOS Forex LIVE Daily Run" -Confirm:$false
Unregister-ScheduledTask -TaskName "ATOS Forex LIVE Exit Check" -Confirm:$false
```
