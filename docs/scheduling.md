# Scheduling Reference — Every Strategy, Every Trigger

**Purpose**: a single ground-truth map of what runs, when, and how — for any
agent picking up this project cold. Verified directly against Windows Task
Scheduler on 2026-08-21, re-verified 2026-08-22 (not reconstructed from
memory or old docs).

**Everything below runs through Windows Task Scheduler.** An earlier version
of this doc described a planned "Claude-native" scheduling mechanism for LBO
(`.claude/scheduled-tasks/*/SKILL.md`) as the active path — that mechanism
never actually existed (checked directly: no such directory, no registered
cron jobs). All 3 LBO tasks are now real Windows Scheduled Tasks like
everything else, and `scheduler_watchdog.py` checks them the same way.

All times below are PKT (UTC+5) unless marked UTC. "Session window" for a
strategy means the UTC-hour range in which it will actually act on a signal
— being *scheduled* to run and being *inside its window* are different
things (see FX Gap and LBO below, both of which self-gate on UTC hour
regardless of when the task fires).

---

## 2026-08-21 fixes — read this before trusting anything below as "working"

Three separate bug classes were found and fixed this pass. Any task that
looks like it "ran successfully" in Task Scheduler before this date should
not be trusted without checking the corresponding log file's actual mtime —
see §6.

1. **`run_hidden.vbs` never propagated the real exit code.** `LastTaskResult`
   was a false positive even when the wrapped command failed or never ran.
   Fixed: it now captures `objShell.Run`'s return value and calls
   `WScript.Quit rc`.
2. **LBO's double-wrap launch chain was broken.** `Task Scheduler → vbs →
   .bat → vbs#2 → python "args"` passed a "program + arguments" string
   through the same quote-stripping logic a second time — that only works
   for a bare file path with no embedded spaces, so the inner python call
   silently never ran for any of the 3 LBO tasks. Fixed by removing the
   double-wrap: each LBO `.bat` now calls `pythonw` directly (matching the
   pattern every other task already used), with the outer `run_hidden.vbs`
   wrap providing the single redirect.
3. **13 of 20 ATOS tasks had `DisallowStartIfOnBatteries: True`.** On a
   laptop that isn't always plugged in, this silently skips the task with
   *zero* log trace — Task Scheduler doesn't even record a launch attempt.
   This was very likely a real contributor to several "mystery" misses
   found this session. Fixed for all 13 (`DisallowStartIfOnBatteries` and
   `StopIfGoingOnBatteries` both set `False`).

---

## 2026-08-22 fixes and findings

1. **London Breakout has never produced a real signal since inception** —
   found and fixed. `_session_range()` tried to read the session-hour window
   off `df.index.hour`; the real H1 data (`_fetch_history_h1()`) carries a
   plain integer index with the hour in a separate `HourUTC` column instead.
   The mask matched ~0 rows on every call, every pair, every session,
   forever — not a "quiet market," a total signal-detection failure.
   `strategy_gap.py` already used the correct pattern; LBO now matches it.
   Verified live: 13 real signals produced on the same data that previously
   produced 0. **First real live test is the next natural trigger** — see
   §1c below.
2. **A rejected order used to crash the entire scheduled run.**
   `saxo_order._place_entry_then_stop()` (the path 9 of 10 forex strategies
   use) had no exception handling around the entry POST — one `400` killed
   every strategy queued after it, silently, for that whole cycle. Now
   logs, skips, continues.
3. **Wrong tick-size rounding left positions with no stop-loss.** Stop/TP
   rounding only special-cased JPY crosses (3dp) vs. a flat 5dp default —
   wrong for any TRY- or CNH-quoted pair (actually 4dp). Caused a real
   `PriceNotInTickSizeIncrements` rejection on a stop order while the
   Market entry still went through, leaving the position briefly
   unprotected. Fixed via `forex.universe.price_decimals()`, derived from
   each pair's own Saxo-reported precision, in all 4 places that had
   independently duplicated the wrong guess (entry, stop-heal, TP-heal,
   breakeven-amend).
4. **Cross-strategy opposite-direction stacking blocked.** Each strategy's
   `open_symbols` only ever sees its OWN positions (`prefix =
   f"{strat_name}:"`) — found live on the dashboard: NZDUSD held Long
   (donchian, pullback) AND Short (bb, ml) simultaneously, same on USDTHB
   and USDCZK. That combination has no upside ever (pays spread/commission
   on both legs for a smaller net position, zero diversification benefit).
   New entries that would oppose another strategy's existing position on
   the same pair are now blocked. Same-direction stacking is deliberately
   left alone — multiple strategies agreeing isn't a conflict.
5. **Account-wide margin exhaustion.** Confirmed live that stocks/ETF/forex
   share ONE Saxo margin pool, not siloed. Disabling the forex heat cap (for
   broader SIM testing) ran usable margin down to 5,546 EUR available
   (99.22% utilization), which then blocked new entries *and* protective
   stops on already-open positions. Relieved by selling ~half of every
   stock/ETF position and cutting forex `RISK_PCT` 1%→0.5% across all 10
   swing strategies (not LBO, which has its own capital book) — margin
   available went to 55,774 EUR (92.69% utilization).
6. **Stock/ETF position sizing capped at 50 shares/name** — a flat dollar
   budget alone was sizing cheap names (XLF, XLE, AES, U) into 100-500+
   share positions, tying up disproportionate margin for their dollar
   value. Applied in both `atos/us_momentum.py`'s `plan_rebalance()` and
   `saxo_etf_strategy/core/etf_executor.py`'s `_enter_position()`.
7. **`atos_live.db` didn't match live Saxo** — 4 phantom/stale rows (one
   entirely fictional CRWD position, excess lots on DELL/FTNT/PANW) traced
   to the very first 2026-08-14 rebalance, reconciled with an honest
   unknown/null P&L rather than a guessed number. **New standing rule**:
   every module's local state must always match live Saxo exactly — see
   the `state_reconciliation` memory note.
8. **Historical strategy validation gap identified and being closed.** Only
   1 of 10 forex strategies (EMA) had ever been backtested, and only on 7
   G7 majors — the 83-pair EM/exotic expansion and the other 9 strategies
   had zero historical validation before live signals started firing on
   them. New `backtest_forex_universe.py` walks all 8 daily-bar strategies
   forward through 3 years of real data across the full 117-pair universe,
   using each strategy's actual production `generate_signals`/
   `should_exit`/`size_position` code (not a reimplementation). `gap` and
   `london_breakout` are excluded (both need intraday H1 data Yahoo doesn't
   carry history for). See the "Backtesting" section of
   [forex_strategies.md](forex_strategies.md) for results.
9. **`ATOS Dashboard Start` was never actually disabled**, despite this doc
   claiming since 2026-08-20 that it was (same broken path as `ATOS Daily
   Scan` — confirmed live, `E:\saxobackup\SaxoTrader\files_kwaseem\` no
   longer exists at all). Would have failed at its next fire (Monday
   18:30 PKT). PowerShell's `Disable-ScheduledTask` was denied without
   admin rights, but the older `schtasks /Change /TN "ATOS Dashboard
   Start" /DISABLE` CLI succeeded under the same non-elevated session —
   worth remembering as the fallback when the ScheduledTasks PowerShell
   module refuses. Confirmed disabled via `Get-ScheduledTask` afterward.

---

## 2026-08-25 fixes and findings

1. **"ATOS Forex Intraday Scan" went silently once-daily for ~2.5h, zero
   emails, zero watchdog alert.** Root cause: an unrelated same-day fix
   (`fix_sim_schedule_conflicts.ps1`, shifting 4 tasks' start times to
   avoid a wall-clock collision with LIVE's new schedule) used
   `Set-ScheduledTask -Trigger (New-ScheduledTaskTrigger -Daily -At X)`.
   That REPLACES the entire trigger set — fine for 3 genuinely once-daily
   tasks in that script, but this task needs its every-30-min repetition,
   which got silently deleted. Confirmed via Windows' own Task Scheduler
   event log: fired correctly on every 30-min mark through 20:00, was
   modified at 20:20:08 (exactly when the conflict-fix script ran), never
   fired again.
2. **Watchdog never actually watched this task.** `WINDOWS_TASKS` had no
   entry for "ATOS Forex Intraday Scan" at all — a pure coverage gap, not
   a detection-logic bug (every other forex task was already correctly
   registered). Added.
3. **Fixing the trigger itself took 3 attempts** before landing on
   something that actually works, because PowerShell's `ScheduledTasks`
   module has a real, undocumented-feeling limitation: `-Daily -At X
   -RepetitionInterval Y -RepetitionDuration Z` together is REJECTED
   outright ("Parameter set cannot be resolved") — those repetition
   params are only valid alongside `-Once`, which then never recurs
   daily at all (attempt 1's mistake, and the SAME mistake as the
   original bug). Directly assigning `$trigger.Repetition.Interval` on a
   plain `-Daily` trigger also fails ("property cannot be found") since
   `.Repetition` is `$null` until given a real CIM instance (attempt 2).
   **Working approach**: build a real `MSFT_TaskRepetitionPattern` via
   `New-CimInstance -ClientOnly`, set `.Interval`/`.Duration` as ISO8601
   duration strings (`"PT30M"`/`"PT16H"`, not a `TimeSpan` or its
   `.ToString()`), assign that object to the `-Daily` trigger's
   `.Repetition` property, then pass to `Set-ScheduledTask`. Verified via
   raw XML afterward: a genuine `CalendarTrigger` with both
   `<ScheduleByDay>` (real daily recurrence) and a nested `<Repetition>`
   (sub-daily interval) — the combination missing both prior attempts.
   See `fix_intraday_scan_trigger.ps1` for the full working script.
4. **The watchdog's own new "hasn't fired recently enough" check then
   false-alarmed itself**, twice. First: it didn't account for a
   repeating intraday task's legitimate overnight "off" window (22:05 →
   next day 06:05, ~8h) — flagged the just-fixed task as broken again
   purely because `last_run` was hours behind "now", even though
   `NextRunTime` correctly showed a healthy near-term tomorrow-morning
   occurrence. Fixed: only escalate when `NextRunTime` ALSO looks
   unhealthy (missing, or >12h out) — exactly the signature a genuinely
   broken/disabled trigger leaves. Second: the check used `grace_min <=
   60` as a proxy for "repeats multiple times a day", which is not a safe
   proxy — several genuinely once-daily tasks (Forex Exit Check, PnL
   Sync, both LBO session-open tasks) also use a tight `grace_min` for
   unrelated reasons and were briefly false-flagged too. Replaced with an
   explicit `INTRADAY_REPEATING_TASKS` set.
5. **LIVE's real-money Saxo app issues short-lived tokens**: access_token
   good for 20 min, refresh_token good for only 1 hour — both far shorter
   than SIM's (SIM's access_token lasts a full 24h). LIVE's own schedule
   at the time ran every ~2h, so the refresh_token was already dead
   before the next scheduled run even started — every run past the first
   hour post-login failed with a `TOKEN EXPIRED` alert email and was
   skipped entirely (no scan, no orders). New `saxo_live_token_keepalive.py`
   + `ATOS Saxo LIVE Token Keepalive` task keeps the refresh chain alive
   between real trading runs. **2026-08-30**: hardened to run as **SYSTEM**
   + `StartWhenAvailable` + every 10 min + 3× restart-on-failure, after it
   kept dying on every reboot/sleep gap (interactive-user task didn't run
   with nobody logged in → one >60-min gap killed the 1h refresh token).
   `SAXO_LIVE_APP_KEY` moved to a Machine env var so SYSTEM can read it
   (public client id, not a secret). Does not replace the one-time
   interactive login itself (`python saxo_auth.py --live`)
   — that still requires a real browser + Saxo credentials.
6. **LIVE's schedule moved from 9 fixed times/day to every 45 min,
   06:00-22:00 PKT** (~22 runs/day) — explicit user request. Verified
   first that `forex/runner.py`'s `run_daily()` (what this task calls)
   already handles both new-entry scanning AND exit checking
   (stop-loss/take-profit/time-stop) together every time it runs, so one
   task fully covers "scan for new trades and exit on stop-loss/profit
   target" without needing a second dedicated task.
7. **Stocks/ETF/Futures moved from once/day to hourly** (00:00-23:00,
   all day) — explicit user request ("no need every minute" but also no
   need to wait a whole day). Verified first that all 3 already combine
   entry AND exit checking in one pass, same as forex:
   `atos_runner.py`'s `should_exit()`, `run_etf_bot.py`'s `run_once()` →
   `review_exits()`, `futures/runner.py`'s `run_daily()` → `should_exit()`.
   Also removed 3 leftover one-time "ATOS ETF Test Run N 2026-08-24"
   tasks (already fired, never recur — stale clutter, not an active
   scanner).
8. **"ATOS Intraday Monitor" mystery, unresolved.** This task's own
   registered trigger is a plain once-daily/weekday `CalendarTrigger` at
   18:25 (confirmed via raw XML) — yet `logs/monitor_{date}.log` shows it
   genuinely running every ~1-2 min in real time, all day. No
   `pythonw.exe`/`python.exe` process matching it was found running in a
   snapshot check, and CIM couldn't retrieve other processes' command
   lines to identify what's actually invoking it that often. Left as an
   open question for the user (possibly a manually-run `--watch`-mode
   console window, e.g. via `ATOS_Monitor.bat`) rather than guessed at
   further. The watchdog now correctly flags this task's OWN trigger as
   unhealthy (its `NextRunTime` genuinely is 24h out), even though
   whatever is actually running the script appears to be working fine in
   practice — a single point of failure if that mechanism ever stops.
9. **LIVE placed its first-ever real trade this same day** (2026-08-25,
   23:08 PKT, manual invocation): `donchian` opened EURNOK short (1,000 @
   10.86975, stop 10.98368, TP 10.52803) and GBPUSD long (1,000 @
   1.36466, stop 1.35165, TP 1.39047). Both bracket orders confirmed
   correct against Saxo's own web trader notifications and Orders tab —
   direction, stop/TP placement, and OCO linkage all exactly right.
   `housekeeping_live`/`safeguard_live` ran immediately after and
   confirmed clean ("everything protected", "nothing to fix").

---

## 1. Forex module (`forex/runner.py`) — 11 strategies

### 1a. Core scan/entry schedule (Windows Task Scheduler)

| Task name | Fires | Command | What it actually does |
|---|---|---|---|
| `ATOS Forex Daily Run` | 06:20 PKT (01:20 UTC), daily | `run_forex_daily.bat` → `runner.py --live` | **All strategies except LBO** (see §1c) scan the **full 117-pair universe** for entries and exits — widened from the Asian-session-only 14-pair set on 2026-08-20 (the `.bat` no longer passes `--session`, defaults to `all`). |
| `ATOS Forex Gap Monday Early` | Mon 03:00 PKT | same `run_forex_daily.bat` | Same "all except LBO" run — timed so `gap` catches the Sun 22:00 UTC weekly-gap window (see §1b). |
| `ATOS Forex Gap London` | weekdays 12:00 PKT (07:00 UTC) | same `run_forex_daily.bat` | Same "all except LBO" run, timed for `gap`'s london-session window. |
| `ATOS Forex Gap NewYork` | weekdays 17:00 PKT (12:00 UTC) | same `run_forex_daily.bat` | Same "all except LBO" run, timed for `gap`'s newyork-session window. |
| `ATOS Forex Gap Tokyo` | weekdays 05:00 PKT (00:00 UTC) | same `run_forex_daily.bat` | **Added 2026-08-26** — the Tokyo gap window (00:00-01:30 UTC, Tue-Fri; Monday's is subsumed by the weekly window) previously had NO dedicated task and fell almost entirely inside the Intraday Scan's own 03:00-06:00 PKT dead zone, so most Tokyo gap opportunities were silently missed. This closes that gap. Verified in code: `_detect_gap_session()` correctly resolves to `"tokyo"` Tue-Fri and `"weekly"` on Monday at this exact time, so firing it daily (including Monday) is safe. |
| `ATOS Forex Gap Fill` | **Mon 03:00 PKT** (= Sun 22:00 UTC) | `run_forex_gap.bat` → `runner.py --strategy gap --live` | The one task that calls `gap` directly. **Retimed 2026-08-21** — was Sun 22:00 PKT (17:00 UTC), 5 hours before the weekly gap window actually opens; now correctly fires right when it does. |
| `ATOS Forex London Run` | 18:00 PKT (13:00 UTC), daily | `run_forex_london.bat` → `runner.py --live` | "All except LBO" run over the **full 117-pair universe** — widened from the London-session-only 20-pair set on 2026-08-20 (no `--session` flag, defaults to `all`). |
| `ATOS Forex Exit Check` | 14:00 PKT (09:00 UTC), daily | `run_forex_exits.bat` → `runner.py --exits-only --live` | Stop/time-stop checks only, **all 11 strategies including LBO** (safe — never opens new positions). |
| `ATOS Forex Intraday Scan` | every 30 min, 06:05-03:00 PKT, daily | `run_forex_intraday.bat` → `runner.py --live` | **Fixed 2026-08-21, broken and re-fixed 2026-08-25**; window extended 22:05→03:00 PKT 2026-08-26 to cover the tail of the NY session (~17:00 ET / ~02:00 PKT rollover) — see the 2026-08-25 section above for the earlier repetition-drop story. |

`ATOS Forex Gap Weekly` (a duplicate of Gap Monday Early, same time/command)
was deleted 2026-08-21 — no longer in this list or in the watchdog registry.

**Net effect**: the "all except LBO" combo (ema, rsi, donchian, bb, pullback,
gap, supertrend, zscore, ml, cnn_lstm) gets invoked at minimum 06:20, every
30 min 06:00-03:00 (Intraday Scan), Mon 03:00, weekdays 05:00, weekdays
12:00, weekdays 17:00, and 18:00 PKT. Existing open positions and currency
exposure limits prevent literal re-entry into an already-held symbol.

### 1b. `gap` strategy's own session self-gating (independent of the table above)

`gap` only acts if `_detect_gap_session()` (forex/runner.py) says the
current UTC hour is inside one of:
- **weekly**: Sun 22:00 UTC → Mon 06:00 UTC — dedicated task: Gap Monday Early / Gap Fill (both 03:00 PKT)
- **tokyo**: Mon(skip, subsumed by weekly)-Fri 00:00-01:30 UTC — dedicated task: Gap Tokyo (05:00 PKT), **added 2026-08-26**, previously uncovered
- **london**: Mon-Fri 07:00-08:30 UTC — dedicated task: Gap London (12:00 PKT)
- **newyork**: Mon-Fri 12:00-13:30 UTC — dedicated task: Gap NewYork (17:00 PKT)

Outside those windows, `gap` logs "not in a gap session window" and does
nothing, no matter how often the surrounding task fires. All 4 sessions now
have a dedicated task firing at (or very near) the start of their window,
in addition to whatever regular scans happen to overlap it.

### 1c. London Breakout (LBO) — separate day-trading book, separate schedule

LBO (`forex/strategy_london_breakout.py`) has its own capital
(`forex.lbo_capital_eur`, ~1,390 EUR / 15,000 SEK), own slot cap (**28**, one
per pair — raised from 10 on 2026-08-21 so a multi-pair breakout day is
never capped below what the pair list can offer; max concurrent exposure is
now 28 × 1.5% = 42% of the LBO book if every slot fills, vs 15% before), and
is explicitly excluded from the "all strategies" runs above.

**Current, verified-real schedule (2026-08-21):**

| Task | State | Fires | Command |
|---|---|---|---|
| `ATOS LBO London Open` | Enabled | 12:00 PKT (07:00 UTC), weekdays | `run_lbo_london.bat` → `runner.py --strategy london_breakout --live` |
| `ATOS LBO Force Close` | Enabled | 01:00 PKT (20:00 UTC), daily | `run_lbo_close.bat` → `runner.py --strategy london_breakout --exits-only --live` |
| `ATOS LBO NY Open` | Enabled | 18:00 PKT (13:00 UTC), weekdays | `run_lbo_ny.bat` → `runner.py --strategy london_breakout --live` |

All 3 `.bat` files were fixed 2026-08-21 (see double-wrap bug above) and are
now registered in `scheduler_watchdog.py`'s `WINDOWS_TASKS` (checked the same
precise way as every other task — LastRunTime/LastTaskResult + log-freshness
— not the old hardcoded-schedule guess it used before real Task Scheduler
entries existed).

LBO trades a separate, smaller **28-pair** universe — majors/crosses only.
The main forex universe expanded to 117 pairs on 2026-08-21 (see
[forex_strategies.md](forex_strategies.md)) but LBO deliberately was **not**
expanded — the added EM/exotic pairs' wider spreads don't suit LBO's tight
2:1 RR day-trade structure. Session logic (independent of any `--session`
flag): London entries 07:00-10:00 UTC (break of the 00:00-07:00 UTC Asian
range), NY entries 13:00-15:00 UTC (break of the 09:00-12:00 UTC
London-morning range, `LONDON_RANGE_END` in `forex/strategy_london_
breakout.py` — checked 1h after that range closes, not at the instant it
does). All positions force-closed by 20:00 UTC (`SESSION_CLOSE` in the
same file) — no overnight holds.

### 1d. CNN-LSTM — trained once, never retrained, near-inert

`cnn_lstm` runs inside every "all strategies" invocation above (§1a) like
any other strategy — it is **not** sleeping in the scheduling sense. But:
- Model trained once, 2026-08-19 02:16 (`data/cnn_lstm/model.pt`). No
  scheduled retraining job exists anywhere.
- Walk-forward validation accuracy: 36.9% mean across 5 folds — barely above
  the 33% random baseline for a 3-class problem.
- Net effect: technically active, practically inert.

---

## 1e. Forex LIVE — the real-money account (separate from everything above)

Full detail in [forex_live_account.md](forex_live_account.md) and
[forex_live_scheduler.md](forex_live_scheduler.md) — this is only a
pointer/summary so this doc stays a complete map of every scheduled task.

| Task | Fires | Command | Notes |
|---|---|---|---|
| `ATOS Forex LIVE Daily Run` | every 45 min, 06:00-03:00 PKT | `run_forex_live_daily.bat` → `runner.py --account live --strategy donchian,ema,rsi --live` | **Moved from 9 fixed times/day 2026-08-25**, window extended 22:00→03:00 PKT 2026-08-26. Handles both entry scanning and exit checking (stop-loss/TP/time-stop) every run. |
| `ATOS Forex LIVE Exit Check` | 14:00 PKT, daily | `run_forex_live_exits.bat` → `runner.py --account live --exits-only --live` | Backstop only — Daily Run above already checks exits every 45 min. |
| `ATOS Forex LIVE EUR Daily Run` | every 45 min, 06:00-03:00 PKT | `run_forex_live_eur_daily.bat` → `runner.py --account live_eur --strategy rsi --live` | **Added 2026-08-26.** Second real-money account (EUR sub-account) — rsi only, 83 EXOTIC pairs only. Own confirmation gate (`SAXO_LIVE_EUR_CONFIRMED`). |
| `ATOS Forex LIVE EUR Exit Check` | 14:00 PKT, daily | `run_forex_live_eur_exits.bat` → `runner.py --account live_eur --exits-only --live` | Backstop only — EUR Daily Run above already checks exits every 45 min. |
| `ATOS Saxo LIVE Token Keepalive` | every 10 min, all day, **as SYSTEM** | `run_saxo_live_keepalive.bat` → `saxo_live_token_keepalive.py` | **Added 2026-08-25; hardened 2026-08-30** (SYSTEM + `StartWhenAvailable` + 3× restart, `SAXO_LIVE_APP_KEY` → Machine env var). Keeps the LIVE refresh-token chain alive between real trading runs — shared by both accounts (same OAuth login) — see the 2026-08-25 findings above. |

Real money, separate Saxo login/app/state/lock/capital-cap from SIM in
every respect. Hard rails enforced in code (never just convention): SEK
account allows only `donchian`/`ema`/`rsi` on the 34 `CORE_SYMBOLS` pairs;
EUR account allows only `rsi` on the 83 `EXOTIC_SYMBOLS` pairs. Each
requires its own confirmation var (`SAXO_LIVE_CONFIRMED` / `SAXO_LIVE_EUR_
CONFIRMED`) set separately from `--live` itself. **First real trade placed
2026-08-25, 23:08 PKT** (SEK account) — see the 2026-08-25 findings above.
Full EUR-account detail, including two findings about how Saxo pools
margin/balance AND positions/orders across all 3 sub-accounts under this
Client, in [forex_live_account.md](forex_live_account.md).

---

## 2. Futures (`futures/runner.py`) — 7 strategies

| Task | Fires | Command |
|---|---|---|
| `ATOS Futures Daily Run` | every 1 hour, 00:00-23:00, daily | `run_futures_daily.bat` → `futures\runner.py --live` |
| `ATOS Futures Discover` | **06:00 PKT, daily** | `run_futures_discover.bat` → `futures\runner.py --discover` |

**Both fixed 2026-08-21**, `Daily Run` moved from once/day to **hourly
2026-08-25** (explicit user request — already combines entry and exit
checking in one pass, see the 2026-08-25 findings above). `run_futures_daily.bat` had the same
double-redirect race as the forex `.bat` files (outer `run_hidden.vbs` wrap
+ an inner `>> log 2>&1` fighting over the same file handle) — that's very
likely why the dashboard was reporting "no signals since day one" for
futures. `ATOS Futures Discover` (refreshes CL/ZB front-month contract
UICs) was monthly (next-fire-only-on-the-1st) — widened to **daily**,
15 minutes before the main run, so a contract nearing expiry (the Sep 2026
WTI contract that caused a stale-position bug expired 2026-08-19, 2 days
before the old monthly refresh would ever have caught it) gets rolled
promptly instead of waiting up to a month.

See [futures_strategies.md](futures_strategies.md) for per-strategy detail.

## 3. ETF (`saxo_etf_strategy/`)

| Task | Fires | Command |
|---|---|---|
| `ATOS ETF Daily Run` | every 1 hour, 00:00-23:00, daily | `saxo_etf_strategy\run_etf_daily.bat` |

**Fixed 2026-08-21** (same double-redirect race as futures/forex) — the
module's own internal log (`saxo_etf_strategy/logs/etf_strategy.log`,
written directly by Python logging, independent of the shell redirect
chain) hadn't been touched since 2026-08-18 despite Task Scheduler
reporting daily success — proof the run was dying before doing any real
work. This is why the rotation was frozen on its original 2026-08-17 picks
for 3+ days with the 2 remaining slots never filled. **Moved from
once/day to hourly 2026-08-25** — explicit user request, already combines
entry and exit checking in one pass (`run_once()` → `review_exits()`), see
the 2026-08-25 findings above. 3 leftover one-time "ATOS ETF Test Run N
2026-08-24" tasks removed same day (already fired, never recur — stale
clutter from an earlier test session).

See [etf_strategies.md](etf_strategies.md).

## 4. Stocks / ATOS core

| Task | Fires | Command | Notes |
|---|---|---|---|
| `ATOS Daily Run` | every 1 hour, 00:00-23:00, daily | `python daily_run.py` → `atos_runner.run_cycle()` | US Blend rebalance logic itself is still 14-day cadence internally; the *task* moved from once/day to hourly 2026-08-25 (explicit user request — already combines entry/exit checking via `should_exit()`, see the 2026-08-25 findings above) so the position check itself doesn't wait a full day to run. |
| ~~`ATOS Daily Scan`~~ | ~~02:00 PKT~~ | ~~missing path~~ | Disabled 2026-08-20 — pointed at a file that no longer exists. |

**US Blend rebalance logic fixed 2026-08-21**: was doing a full
liquidate-and-rebuy every cycle (sell 100% of holdings, rebuy the fresh
target list from scratch) even when a ticker stayed in the target list
across two rebalances — paying commission/spread to sell and immediately
rebuy the same position for no reason. Now uses proper delta-based
rebalancing (only trades what's actually changed). A broker-reconciliation
step also now runs before every rebalance, closing out any DB row that
claims to be open but the broker doesn't actually hold (was causing
duplicate "open" rows for the same ticker after a failed sell).

**Stop-loss/take-profit fixed 2026-08-21**: stock entries previously placed
a bare market order with **no protective stop attached at all**
(`stop_price` was hardcoded to `0` in the DB record for US Blend). Now uses
the same `saxo_order.place_with_stop()` bracket mechanism forex/futures/ETF
already used — a native Saxo GTC stop (and, for US Blend, an 8%/20%
stop/take-profit matching the ETF module's convention) is attached
atomically at entry, enforced by Saxo 24/7 instead of only being checked
when the next scheduled cycle happens to run.

## 5. Support / monitoring tasks

| Task | Fires | Command | Notes |
|---|---|---|---|
| `ATOS Intraday Monitor` | 18:25 PKT, daily (per its own registered trigger) | `python intraday_monitor.py` | Stop-loss / position monitor. **Unresolved mystery flagged 2026-08-25** — the real script clearly runs every ~1-2 min all day (confirmed via `logs/monitor_{date}.log`), but this task's own trigger is only once-daily. Something else appears to be invoking it that often; not identified. See the 2026-08-25 findings above. |
| `ATOS Saxo LIVE Token Keepalive` | every 10 min, all day, **as SYSTEM** | `saxo_live_token_keepalive.py` | **Added 2026-08-25; hardened 2026-08-30.** See §1e above. |
| `ATOS PnL Sync` | 23:00 PKT, daily | `run_pnl_sync.bat` → `pnl_tracker.py --sync` | Syncs open/closed trades from all module state files into `data/pnl_ledger.db`. **Was actually configured as a weekly Sunday-only trigger since creation (2026-08-19) — never fired even once, silently freezing stock P&L data at 2026-08-14. Fixed to real daily 2026-08-21 and backfilled.** |
| `ATOS Scheduler Watchdog` | every 30 min | `python scheduler_watchdog.py` | See §6. |
| `ATOS Daily Chart` | 23:15 PKT, daily | `run_daily_chart.bat` → `daily_chart.py` | **Added 2026-08-21.** Generates a 2-panel per-strategy P&L chart (cumulative + today's) for EACH of the 4 modules **separately** — stock/etf/futures/forex each get their own independent chart file, never combined — from `data/pnl_ledger.db`. Fires 15 min after `ATOS PnL Sync` so every module's data is fully synced first. Saves `data/charts/{module}_strategy_YYYY-MM-DD.png` (permanent daily record) and `data/charts/{module}_strategy_latest.png` (always-current), then emails all of today's charts as inline attachments via `config/email.json` (one section per module in the email body). Skips a module gracefully (chart and email) if it has no closed trades yet — ETF/futures did on the day this was added, will start appearing automatically once they have their first closed trade. |
| `ATOS Forex Performance Tracker` | 23:45 PKT, daily | `run_forex_performance_tracker.bat` → `reports\_gather_daily_sim_data.py` then `py -3.12 reports\pair_group_performance_tracker.py` | **Added 2026-08-28.** Rebuilds `data/forex_performance_tracker.xlsx` in place (Per-Group, Per-Pair, Daily, Weekly, Monthly Performance sheets — PF/WR%/Net P&L, live COUNTIF/SUMIF formulas over a hidden Trade Detail sheet). Two-phase (forex needs live EUR re-pricing across many quote currencies, needs `forex.runner`/torch; the openpyxl build step runs under `py -3.12`, which doesn't have torch). |
| `ATOS Futures Performance Tracker` | 23:50 PKT, daily | `run_futures_performance_tracker.bat` → `py -3.12 reports\module_performance_tracker.py futures Futures USD` | **Added 2026-08-28.** Rebuilds `data/futures_performance_tracker.xlsx` (Per-Strategy, Per-Symbol, Daily, Weekly, Monthly). Single-phase — reads `data/pnl_ledger.db` directly (already stores real dealt `realized_pnl`/`commission` per closed trade, no live re-pricing needed). |
| `ATOS ETF Performance Tracker` | 23:55 PKT, daily | `run_etf_performance_tracker.bat` → `py -3.12 reports\module_performance_tracker.py etf ETF USD` | **Added 2026-08-28.** Same as Futures above, module `etf`. |
| `ATOS Stocks Performance Tracker` | 00:00 PKT, daily | `run_stocks_performance_tracker.bat` → `py -3.12 reports\module_performance_tracker.py stock Stocks SEK` | **Added 2026-08-28.** Same as Futures above, module `stock` (pnl_ledger.db's real module name — the workbook is `data/stock_performance_tracker.xlsx`, singular). |
| ~~`ATOS Dashboard Start`~~ | ~~18:30 PKT~~ | ~~missing path~~ | Same missing-path problem as Daily Scan. **Was claimed disabled since 2026-08-20 but actually never was** (found live 2026-08-22, still `State: Ready`). Fixed the same day via `schtasks /Change /TN "ATOS Dashboard Start" /DISABLE` after PowerShell's `Disable-ScheduledTask` was denied for lack of admin rights — confirmed disabled. |

All 4 Performance Tracker tasks are read-only analytics (never touch a live
signal, gate, stop, or order) and are deliberately **not** registered in
`scheduler_watchdog.py` — same category as `ATOS Housekeeping`/`ATOS
Safeguard`/`ATOS Daily Summary`, none of which are watchdog-monitored
either; a missed run just means stale numbers until the next one, not a
trading gap. Staggered 5 min apart, starting 15 min after `ATOS Daily
Summary` (23:30 PKT), so each module's `pnl_ledger.db` data is settled
before its tracker reads it.

---

## 6. How to check "did the scheduler actually run" yourself

`LastTaskResult=0` is **still not fully trustworthy on its own** even after
the exit-code fix — it can show a transient in-progress code right after a
task starts, and (rarely) other transient codes. The trustworthy check is
always: **does the corresponding log file have a fresh modification time
close to the task's scheduled fire time?**

```powershell
Get-ScheduledTaskInfo -TaskName "ATOS Forex Daily Run" | Select LastRunTime, LastTaskResult
```
```bash
ls -la data/forex_scheduler.log   # mtime should be within minutes of LastRunTime
```

`scheduler_watchdog.py` (runs every 30 min via `ATOS Scheduler Watchdog`)
automates exactly this check across every task above, cross-referencing
`LastTaskResult` against real log freshness before declaring a failure —
**fixed 2026-08-21** to trust a fresh log over a possibly-transient result
code, rather than false-alarming the moment it saw any non-zero code. Every
alert email now includes the exact `Start-ScheduledTask` command to fire
the task manually right away, since a missed run's window is otherwise gone
until the next scheduled occurrence.

**Also fixed 2026-08-31**: a task that is *currently executing* is no
longer mistaken for a failure. Task Scheduler reports `LastTaskResult`
`267009` (`SCHED_S_TASK_RUNNING`, `0x00041301`) — and `0x800710E0`
(`ERROR_TASK_ALREADY_RUNNING`) for a rejected overlapping occurrence — for
the whole duration of an in-progress run, and a slow run's log
legitimately goes stale meanwhile, which defeated the "trust a fresh log"
escape hatch. A transient DNS blip to `gateway.saxobank.com` (every Saxo
call then eating its full 15 s timeout) made all four 12:00/12:05 forex
tasks overrun their windows and the watchdog alerted on every one, though
nothing had failed. The watchdog now treats `state == "Running"` (or
either running-status code) as in-progress and only escalates once a task
has been `Running` past `RUNNING_HANG_CEILING_MIN` (90 min — well beyond
any legitimate run, so this is a genuine hang, and a stuck forex run also
holds `forex_runner.lock` against every other forex task). It likewise no
longer alarms on `267014` (`SCHED_S_TASK_TERMINATED` — Windows killed the
run for exceeding its `ExecutionTimeLimit`; `SaxoTr Intraday Monitor`, 2
min limit + 60 s cadence, was terminated every cycle while Saxo SIM order
placement was slow) as long as the task is still relaunching normally.
Same incident: `proc_lock.acquire()` now clears a lock whose recorded
holder PID is dead instead of waiting out the full 15 min `WAIT_TIMEOUT`
(a crashed `intraday_monitor` had left `forex_runner.lock` held, taxing
every subsequent forex run 15 min).

Underlying trigger for all of the above on 2026-08-31: **Saxo SIM was
rejecting essentially every order operation** (`CouldNotCompleteRequest
(90)`) from ~00:00 PKT onward — a Saxo-side execution outage, same class
as 2026-08-28 — so every scan spent 10–15 s per signal retrying rejected
orders and massively overran its window. Auth/token/read endpoints stayed
healthy throughout; nothing in ATOS was broken.

**Mitigation added 2026-08-31** (`forex/runner.py`): an order-venue circuit
breaker. `_record_entry_result()` counts *consecutive* entry-order
rejections across one run; after `CIRCUIT_BREAKER_MAX_CONSECUTIVE_REJECTS`
(8) it opens, `_run_entries()` returns immediately for every remaining
strategy, and one `send_order_venue_down` email goes out. Exits and
stop-loss healing are deliberately untouched — a protective action is
still worth retrying mid-outage. `run_daily()` calls
`_reset_order_circuit()` so the state is per-run; the next scheduled scan
retries from scratch. This keeps a scan against a dead venue to ~2 min
instead of 60–90 and stops the orphan-order pileup.

**Also fixed 2026-08-21**: a task that has genuinely never run was
previously treated as "nothing to check" unconditionally — which is exactly
how the PnL Sync misconfiguration (weekly instead of daily) went
undetected: "never run yet" looked identical to "legitimately new, hasn't
had its first chance." Now checks the task's own `NextRunTime` against a
per-task `max_first_run_wait_hours` (30h for daily tasks, 78h for
weekday-only, 174h for the two genuinely-weekly ones) — if a never-run
task's next fire is further out than that, the trigger itself is flagged as
likely misconfigured.

The Windows Task Scheduler operational event log (`Microsoft-Windows-
TaskScheduler/Operational`) was disabled by default and is now enabled
(`wevtutil sl "Microsoft-Windows-TaskScheduler/Operational" /e:true`) — any
future miss now has a real forensic trail (`Get-WinEvent -LogName
"Microsoft-Windows-TaskScheduler/Operational"`), which most of this
session's investigations had to work around not having.

---

## Went live — 2026-08-25 (superseded the old "what's still open" note below)

The forex LIVE account (§1e above) is real and has placed real trades.
`saxo_auth.py` supports both `env="sim"` and `env="live"` (separate app
registration, separate OAuth endpoints, separate token file) — no longer
hardcoded to SIM. Everything needed to go live (app registration, PKCE
login, hard strategy/pair allowlists, its own housekeeping/safeguard
agents) is done; see [forex_live_account.md](forex_live_account.md) for
the full architecture and testing record. The one still-open item is the
LIVE app's short token lifetime, addressed by `saxo_live_token_keepalive.py`
(§1e above) rather than anything requiring Saxo's side to change.

This document is a snapshot last verified against live Task Scheduler
state on 2026-08-25. Task Scheduler and the bat files are the actual
source of truth if anything here looks stale —
`Get-ScheduledTask | Where TaskName -like "ATOS*"` and `cat run_*.bat` will
always tell you what's really configured.
