# Forex LIVE Account — Real Money

**Status**: live, fully armed (`SAXO_LIVE_CONFIRMED=1` set, scheduled tasks registered) as of 2026-08-25. **First real trade placed 2026-08-25, 23:08 PKT**: `donchian` opened EURNOK short (1,000 @ 10.86975, stop 10.98368, TP 10.52803) and GBPUSD long (1,000 @ 1.36466, stop 1.35165, TP 1.39047) — both bracket orders verified correct against Saxo's own web trader, `housekeeping_live`/`safeguard_live` confirmed clean immediately after.

**See also**: [forex_live_strategies.md](forex_live_strategies.md) (entry/exit rules for each of the 3 strategies, in depth) and [forex_live_scheduler.md](forex_live_scheduler.md) (every scheduled task, exact trigger times, SIM-conflict history).

**Module**: `forex/runner.py --account live` (same codebase as SIM, account-scoped via `set_account_env()`)
**Account**: Saxo LIVE, sub-account `1070996INET`, SEK-denominated, opened with 6,000 SEK
**Strategies**: exactly 3 of the 11 available — `donchian`, `ema`, `rsi` (hard-restricted in code, not just by convention)
**Universe**: exactly the 34 `CORE_SYMBOLS` pairs — no exotic (hard-filtered in code)
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
- **`ATOS Saxo LIVE Token Keepalive`** (created via `setup_saxo_live_keepalive.ps1`, Administrator) — every 15 min, all day. Calls `saxo_auth.get_valid_access_token(env="live")` to keep the refresh-token chain alive; added 2026-08-25 after finding LIVE's app issues a 20-min access token / 1-hour refresh token — far shorter than SIM's 24h token — which the old 9-times/2h-gap schedule couldn't keep alive between runs. Doesn't replace the one-time interactive login (`python saxo_auth.py --live`) itself.

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
