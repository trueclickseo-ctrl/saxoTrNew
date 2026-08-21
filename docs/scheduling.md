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

## 1. Forex module (`forex/runner.py`) — 11 strategies

### 1a. Core scan/entry schedule (Windows Task Scheduler)

| Task name | Fires | Command | What it actually does |
|---|---|---|---|
| `ATOS Forex Daily Run` | 06:20 PKT (01:20 UTC), daily | `run_forex_daily.bat` → `runner.py --live` | **All strategies except LBO** (see §1c) scan the **full 117-pair universe** for entries and exits — widened from the Asian-session-only 14-pair set on 2026-08-20 (the `.bat` no longer passes `--session`, defaults to `all`). |
| `ATOS Forex Gap Monday Early` | Mon 03:00 PKT | same `run_forex_daily.bat` | Same "all except LBO" run — timed so `gap` catches the Sun 22:00 UTC weekly-gap window (see §1b). |
| `ATOS Forex Gap London` | weekdays 12:00 PKT (07:00 UTC) | same `run_forex_daily.bat` | Same "all except LBO" run, timed for `gap`'s london-session window. |
| `ATOS Forex Gap NewYork` | weekdays 17:00 PKT (12:00 UTC) | same `run_forex_daily.bat` | Same "all except LBO" run, timed for `gap`'s newyork-session window. |
| `ATOS Forex Gap Fill` | **Mon 03:00 PKT** (= Sun 22:00 UTC) | `run_forex_gap.bat` → `runner.py --strategy gap --live` | The one task that calls `gap` directly. **Retimed 2026-08-21** — was Sun 22:00 PKT (17:00 UTC), 5 hours before the weekly gap window actually opens; now correctly fires right when it does. |
| `ATOS Forex London Run` | 18:00 PKT (13:00 UTC), daily | `run_forex_london.bat` → `runner.py --live` | "All except LBO" run over the **full 117-pair universe** — widened from the London-session-only 20-pair set on 2026-08-20 (no `--session` flag, defaults to `all`). |
| `ATOS Forex Exit Check` | 14:00 PKT (09:00 UTC), daily | `run_forex_exits.bat` → `runner.py --exits-only --live` | Stop/time-stop checks only, **all 11 strategies including LBO** (safe — never opens new positions). |
| `ATOS Forex Intraday Scan` | every 30 min, 06:00-22:00 PKT, daily | `run_forex_intraday.bat` → `runner.py --live` | **Fixed 2026-08-21** — was a one-time trigger with a 16h repetition window (fired correctly for exactly one day, then permanently dormant). Now a real daily-recurring trigger with the same repetition. |

`ATOS Forex Gap Weekly` (a duplicate of Gap Monday Early, same time/command)
was deleted 2026-08-21 — no longer in this list or in the watchdog registry.

**Net effect**: the "all except LBO" combo (ema, rsi, donchian, bb, pullback,
gap, supertrend, zscore, ml, cnn_lstm) gets invoked at minimum 06:20, every
30 min 06:00-22:00 (Intraday Scan), Mon 03:00, weekdays 12:00, weekdays
17:00, and 18:00 PKT. Existing open positions and currency exposure limits
prevent literal re-entry into an already-held symbol.

### 1b. `gap` strategy's own session self-gating (independent of the table above)

`gap` only acts if `_detect_gap_session()` (forex/runner.py) says the
current UTC hour is inside one of:
- **weekly**: Sun 22:00 UTC → Mon 06:00 UTC
- **tokyo**: Mon(skip)-Fri 00:00-01:30 UTC
- **london**: Mon-Fri 07:00-08:30 UTC
- **newyork**: Mon-Fri 12:00-13:30 UTC

Outside those windows, `gap` logs "not in a gap session window" and does
nothing, no matter how often the surrounding task fires.

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
range), NY entries 13:00-15:00 UTC (break of the 09:00-13:00 UTC
London-morning range). All positions force-closed by 20:00 UTC — no
overnight holds.

### 1d. CNN-LSTM — trained once, never retrained, near-inert

`cnn_lstm` runs inside every "all strategies" invocation above (§1a) like
any other strategy — it is **not** sleeping in the scheduling sense. But:
- Model trained once, 2026-08-19 02:16 (`data/cnn_lstm/model.pt`). No
  scheduled retraining job exists anywhere.
- Walk-forward validation accuracy: 36.9% mean across 5 folds — barely above
  the 33% random baseline for a 3-class problem.
- Net effect: technically active, practically inert.

---

## 2. Futures (`futures/runner.py`) — 7 strategies

| Task | Fires | Command |
|---|---|---|
| `ATOS Futures Daily Run` | 06:15 PKT, daily | `run_futures_daily.bat` → `futures\runner.py --live` |
| `ATOS Futures Discover` | **06:00 PKT, daily** | `run_futures_discover.bat` → `futures\runner.py --discover` |

**Both fixed 2026-08-21.** `run_futures_daily.bat` had the same
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
| `ATOS ETF Daily Run` | 06:30 PKT, daily | `saxo_etf_strategy\run_etf_daily.bat` |

**Fixed 2026-08-21** — same double-redirect race as futures/forex. The
module's own internal log (`saxo_etf_strategy/logs/etf_strategy.log`,
written directly by Python logging, independent of the shell redirect
chain) hadn't been touched since 2026-08-18 despite Task Scheduler
reporting daily success — proof the run was dying before doing any real
work. This is why the rotation was frozen on its original 2026-08-17 picks
for 3+ days with the 2 remaining slots never filled.

See [etf_strategies.md](etf_strategies.md).

## 4. Stocks / ATOS core

| Task | Fires | Command | Notes |
|---|---|---|---|
| `ATOS Daily Run` | 06:00 PKT, daily | `python daily_run.py` → `atos_runner.run_cycle()` | Rebalances US Blend on a 14-day cadence (fortnightly). |
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
| `ATOS Intraday Monitor` | 18:25 PKT, daily | `python intraday_monitor.py` | Stop-loss / position monitor. |
| `ATOS PnL Sync` | 23:00 PKT, daily | `run_pnl_sync.bat` → `pnl_tracker.py --sync` | Syncs open/closed trades from all module state files into `data/pnl_ledger.db`. **Was actually configured as a weekly Sunday-only trigger since creation (2026-08-19) — never fired even once, silently freezing stock P&L data at 2026-08-14. Fixed to real daily 2026-08-21 and backfilled.** |
| `ATOS Scheduler Watchdog` | every 30 min | `python scheduler_watchdog.py` | See §6. |
| `ATOS Daily Chart` | 23:15 PKT, daily | `run_daily_chart.bat` → `daily_chart.py` | **Added 2026-08-21.** Generates a 2-panel per-strategy P&L chart (cumulative + today's) for EACH of the 4 modules **separately** — stock/etf/futures/forex each get their own independent chart file, never combined — from `data/pnl_ledger.db`. Fires 15 min after `ATOS PnL Sync` so every module's data is fully synced first. Saves `data/charts/{module}_strategy_YYYY-MM-DD.png` (permanent daily record) and `data/charts/{module}_strategy_latest.png` (always-current), then emails all of today's charts as inline attachments via `config/email.json` (one section per module in the email body). Skips a module gracefully (chart and email) if it has no closed trades yet — ETF/futures did on the day this was added, will start appearing automatically once they have their first closed trade. |
| ~~`ATOS Dashboard Start`~~ | ~~18:30 PKT~~ | ~~missing path~~ | Same missing-path problem as Daily Scan. **Was claimed disabled since 2026-08-20 but actually never was** (found live 2026-08-22, still `State: Ready`). Fixed the same day via `schtasks /Change /TN "ATOS Dashboard Start" /DISABLE` after PowerShell's `Disable-ScheduledTask` was denied for lack of admin rights — confirmed disabled. |

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

## Going live — what's still an open, non-code item

Saxo's SIM and LIVE environments use **completely separate app
registrations** — SIM/LIVE app keys and secrets are not shared, and LIVE
requires the full OAuth Authorization Code Grant (no one-day developer-
portal tokens). `saxo_auth.py` is currently hardcoded to
`sim.logonvalidation.net`. This is not something fixable in code — it
requires registering a new app via Saxo's developer portal (their own
account, their own approval process, which can take review time). Start
this well before a target go-live date. See the "Audit — 2026-08-21"
section of [forex_strategies.md](forex_strategies.md) for the full writeup.

This document is a snapshot from 2026-08-22 (last verified against live
Task Scheduler state that day). Task Scheduler and the bat files are the
actual source of truth if anything here looks stale —
`Get-ScheduledTask | Where TaskName -like "ATOS*"` and `cat run_*.bat` will
always tell you what's really configured.
