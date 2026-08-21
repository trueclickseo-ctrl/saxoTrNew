# Scheduling Reference — Every Strategy, Every Trigger

**Purpose**: a single ground-truth map of what runs, when, and how — for any
agent picking up this project cold. Verified directly against Windows Task
Scheduler on 2026-08-21 (not reconstructed from memory or old docs).

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

## 1. Forex module (`forex/runner.py`) — 11 strategies

### 1a. Core scan/entry schedule (Windows Task Scheduler)

| Task name | Fires | Command | What it actually does |
|---|---|---|---|
| `ATOS Forex Daily Run` | 06:20 PKT (01:20 UTC), daily | `run_forex_daily.bat` → `runner.py --live --session asian` | **All strategies except LBO** (see §1c) scan the Asian-session pair set for entries; exits checked on the full universe. |
| `ATOS Forex Gap Monday Early` | Mon 03:00 PKT | same `run_forex_daily.bat` | Same "all except LBO" run — timed so `gap` catches the Sun 22:00 UTC weekly-gap window (see §1b). |
| `ATOS Forex Gap London` | weekdays 12:00 PKT (07:00 UTC) | same `run_forex_daily.bat` | Same "all except LBO" run, timed for `gap`'s london-session window. |
| `ATOS Forex Gap NewYork` | weekdays 17:00 PKT (12:00 UTC) | same `run_forex_daily.bat` | Same "all except LBO" run, timed for `gap`'s newyork-session window. |
| `ATOS Forex Gap Fill` | **Mon 03:00 PKT** (= Sun 22:00 UTC) | `run_forex_gap.bat` → `runner.py --strategy gap --live` | The one task that calls `gap` directly. **Retimed 2026-08-21** — was Sun 22:00 PKT (17:00 UTC), 5 hours before the weekly gap window actually opens; now correctly fires right when it does. |
| `ATOS Forex London Run` | 18:00 PKT (13:00 UTC), daily | `run_forex_london.bat` → `runner.py --live --session london` | "All except LBO" run over the London pair set. |
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
| `ATOS PnL Sync` | 23:00 PKT, daily | `run_pnl_sync.bat` → `pnl_tracker.py --sync` | Syncs open/closed trades from all module state files into `data/pnl_ledger.db`. |
| `ATOS Scheduler Watchdog` | every 30 min | `python scheduler_watchdog.py` | See §6. |
| ~~`ATOS Dashboard Start`~~ | ~~18:30 PKT~~ | ~~missing path~~ | Disabled 2026-08-20 — same missing-path problem as Daily Scan. Non-financial, lower urgency. |

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

This document is a snapshot from 2026-08-21. Task Scheduler and the bat
files are the actual source of truth if anything here looks stale —
`Get-ScheduledTask | Where TaskName -like "ATOS*"` and `cat run_*.bat` will
always tell you what's really configured.
