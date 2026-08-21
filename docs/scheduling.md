# Scheduling Reference — Every Strategy, Every Trigger

**Purpose**: a single ground-truth map of what runs, when, and how — for any
agent picking up this project cold. Verified directly against Windows Task
Scheduler and the Claude-native scheduler on 2026-08-20 (not reconstructed
from memory or old docs, which had drifted from reality — see "Known gaps"
at the bottom).

Two independent scheduling systems exist. Both matter:

1. **Windows Task Scheduler** — runs `.bat` files via `run_hidden.vbs`
   (silent launcher) or directly. Covers forex swing strategies, futures,
   ETF, stocks/ATOS, PnL sync, intraday monitor.
2. **Claude-native scheduled tasks** (`.claude/scheduled-tasks/*/SKILL.md`,
   managed via the `schedule` skill) — currently only the 3 London Breakout
   (LBO) day-trading tasks. Runs inside a Claude Code session, not a bare
   Windows process.

All times below are PKT (UTC+5) unless marked UTC. "Session window" for a
strategy means the UTC-hour range in which it will actually act on a signal
— being *scheduled* to run and being *inside its window* are different
things (see FX Gap and LBO below, both of which self-gate on UTC hour
regardless of when the task fires).

---

## 1. Forex module (`forex/runner.py`) — 11 strategies

### 1a. Core scan/entry schedule (Windows Task Scheduler)

| Task name | Fires | Command | What it actually does |
|---|---|---|---|
| `ATOS Forex Daily Run` | 06:20 PKT (01:20 UTC), daily | `run_forex_daily.bat` → `runner.py --live --session asian` | **All strategies except LBO** (see §1c) scan the Asian-session pair set (14 pairs) for entries; exits checked on all 34. |
| `ATOS Forex Gap Monday Early` | Mon 03:00 PKT | same `run_forex_daily.bat` | Same "all except LBO" run — timed so `gap` catches the Sun 22:00 UTC weekly-gap window (see §1b). |
| `ATOS Forex Gap Weekly` | Mon 03:00 PKT | same `run_forex_daily.bat` | **Duplicate trigger of the row above** — identical time, identical command. Not yet reconciled; see "Known gaps." |
| `ATOS Forex Gap London` | weekdays 12:00 PKT (07:00 UTC) | same `run_forex_daily.bat` | Same "all except LBO" run, timed for `gap`'s london-session window. |
| `ATOS Forex Gap NewYork` | weekdays 17:00 PKT (12:00 UTC) | same `run_forex_daily.bat` | Same "all except LBO" run, timed for `gap`'s newyork-session window. |
| `ATOS Forex Gap Fill` | Sun 22:00 PKT (17:00 UTC) | `run_forex_gap.bat` → `runner.py --strategy gap --live` | The one task that calls `gap` directly rather than through the general run. Trigger time doesn't line up with the code's weekly window (see "Known gaps"). |
| `ATOS Forex London Run` | 18:00 PKT (13:00 UTC), daily | `run_forex_london.bat` → `runner.py --live --session london` | "All except LBO" run over the London pair set (13 pairs). |
| `ATOS Forex Exit Check` | 14:00 PKT (09:00 UTC), daily | `run_forex_exits.bat` → `runner.py --exits-only --live` | Stop/time-stop checks only, **all 11 strategies including LBO** (safe — never opens new positions). No new entries anywhere. |

**Net effect**: the "all except LBO" combo (ema, rsi, donchian, bb, pullback,
gap, supertrend, zscore, ml, cnn_lstm) actually gets invoked far more than
once or twice a day — at minimum 06:20, Mon 03:00 (×2 duplicate), weekdays
12:00, weekdays 17:00, and 18:00 PKT. Each invocation independently checks
entry criteria against current prices; existing open positions and currency
exposure limits prevent literal re-entry into an already-held symbol, but
this is a materially more frequent scan cadence than the "3 daily runs"
older documentation described.

### 1b. `gap` strategy's own session self-gating (independent of the table above)

`gap` only acts if `_detect_gap_session()` (forex/runner.py:378) says the
current UTC hour is inside one of:
- **weekly**: Sun 22:00 UTC → Mon 06:00 UTC
- **tokyo**: Mon(skip)-Fri 00:00-01:30 UTC
- **london**: Mon-Fri 07:00-08:30 UTC
- **newyork**: Mon-Fri 12:00-13:30 UTC

Outside those windows, `gap` logs "not in a gap session window" and does
nothing, no matter how often the surrounding task fires.

### 1c. London Breakout (LBO) — separate day-trading book, separate schedule

LBO (`forex/strategy_london_breakout.py`) has its **own** capital
(`forex.lbo_capital_eur`, ~1,390 EUR / 15,000 SEK), own slot cap (10, up
from 7 as of 2026-08-20), and is explicitly excluded from the "all
strategies" runs above (`forex/runner.py:1726`, fixed 2026-08-20 — see
"Known gaps" for why this exclusion had to be added).

**CORRECTION (2026-08-21):** this section originally described a planned
"Claude-native" scheduling mechanism (`.claude/scheduled-tasks/*/SKILL.md`,
`lbo-london-open`/`lbo-ny-open`/`lbo-force-close`) as the active path, with
the Windows Task Scheduler equivalents (`ATOS LBO London Open`, `ATOS LBO
Force Close`) disabled on 2026-08-20 as "duplicates." **That Claude-native
mechanism does not actually exist** — no `.claude/scheduled-tasks/`
directory, no registered cron jobs, checked directly on 2026-08-21. Whoever
disabled the Windows tasks was very likely setting up for a migration to
that mechanism that never got finished, or it was torn down separately.
Net effect: LBO had **zero working schedule** from 2026-08-20 until the
Windows tasks were re-enabled on 2026-08-21 — this is exactly what the
watchdog "LBO Force Close"/"LBO London Open" silent-since-2026-08-20 alerts
were reporting, correctly.

**Current, verified-real schedule (2026-08-21):**

| Task | Mechanism | State | Fires | Command |
|---|---|---|---|---|
| `ATOS LBO London Open` | Windows Task Scheduler | Enabled | 12:00 PKT (07:00 UTC), weekly Mon-Fri via daily trigger | `run_lbo_london.bat` → `runner.py --strategy london_breakout --live` |
| `ATOS LBO Force Close` | Windows Task Scheduler | Enabled | 01:00 PKT (20:00 UTC), daily | `run_lbo_close.bat` → `runner.py --strategy london_breakout --exits-only --live` |
| `ATOS LBO NY Open` | Windows Task Scheduler | Enabled | 18:00 PKT (13:00 UTC), weekly Mon-Fri | `run_lbo_ny.bat` → `runner.py --strategy london_breakout --live` — **created 2026-08-21**, closing the gap noted above. Already covered by `scheduler_watchdog.py`'s `CLAUDE_TASKS["LBO NY Open"]` entry, which checks `lbo_ny.log` freshness against the expected UTC time independent of which mechanism fires it — no watchdog registry change was needed. |

If a genuine Claude-native scheduling migration is wanted later, set it up
properly via the `schedule` skill *before* disabling the Windows-side
tasks, and verify with `CronList`/checking `.claude/scheduled-tasks/` that
it actually exists — don't repeat this gap.

LBO scans 28 pairs (expanded from 7 on 2026-08-20). Its own session logic
(independent of any `--session` flag): London entries 07:00-10:00 UTC
(break of the 00:00-07:00 UTC Asian range), NY entries 13:00-15:00 UTC
(break of the 09:00-13:00 UTC London-morning range). All positions force-closed
by 20:00 UTC — no overnight holds.

### 1d. CNN-LSTM — trained once, never retrained, near-inert

`cnn_lstm` runs inside every "all strategies" invocation above (§1a) like
any other strategy — it is **not** sleeping in the scheduling sense. But:
- Model trained once, 2026-08-19 02:16 (`data/cnn_lstm/model.pt`). No
  scheduled retraining job exists anywhere (`forex/cnn_lstm_trainer.py` is
  manual-only: `python -m forex.cnn_lstm_trainer --train`).
- Walk-forward validation accuracy: **36.9%** mean across 5 folds (`data/cnn_lstm/report.json`)
  — barely above the 33% random baseline for a 3-class Buy/Sell/Hold
  problem, and **0.0% signal_rate in every fold** at its evaluation
  threshold. Live threshold is lower (`CONFIDENCE_THRESHOLD=0.45` vs the
  report's 0.58) so it isn't literally guaranteed to never fire, but in
  every live run observed so far it has logged "No signals today."
- Net effect: technically active, practically inert. Needs a retrain (and
  probably a feature/architecture review) before it should be trusted, not
  just a scheduling fix.

---

## 2. Futures (`futures/runner.py`) — 7 strategies

| Task | Fires | Command |
|---|---|---|
| `ATOS Futures Daily Run` | 06:15 PKT, daily | `run_futures_daily.bat` → `futures\runner.py --live` (after US close) |
| `ATOS Futures Discover` | 08:00 PKT, monthly-ish (next fire 2026-09-01) | contract-discovery/roll job |

All 7 strategies (Donchian, RSI(5), EMA(5/20), MACD, BB Squeeze, MA Cross,
Trend MA) run together in the one daily invocation. See
[futures_strategies.md](futures_strategies.md) for per-strategy detail —
note that doc's own audit found only 2-5 of 13 markets are actually
tradeable at current capital.

## 3. ETF (`saxo_etf_strategy/`)

| Task | Fires | Command |
|---|---|---|
| `ATOS ETF Daily Run` | 06:30 PKT, daily | `saxo_etf_strategy\run_etf_daily.bat` |

Active strategy selected via `etf_config.py` (`strategy_name`) — default is
Sector Rotation. See [etf_strategies.md](etf_strategies.md).

## 4. Stocks / ATOS core

| Task | Fires | Command | Notes |
|---|---|---|---|
| `ATOS Daily Run` | 06:00 PKT, daily | `python daily_run.py` → `atos_runner.run_cycle()` | Working correctly. Rebalances US Blend on the month's first trading day; otherwise holds. |
| ~~`ATOS Daily Scan`~~ | ~~02:00 PKT~~ | ~~`E:\saxobackup\...\run_atos_daily.bat`~~ | **Disabled 2026-08-20** — pointed at a file that no longer exists (`E:\saxobackup\SaxoTrader\files_kwaseem\` is gone). Failing every day, unrelated to any of today's other fixes. |

## 5. Support / monitoring tasks

| Task | Fires | Command | Notes |
|---|---|---|---|
| `ATOS Intraday Monitor` | 18:25 PKT, daily | `python intraday_monitor.py` | Stop-loss / position monitor. |
| `ATOS PnL Sync` | 23:00 PKT, daily | `run_pnl_sync.bat` → `pnl_tracker.py --sync` | Syncs open/closed trades from all module state files into `data/pnl_ledger.db`. |
| ~~`ATOS Dashboard Start`~~ | ~~18:30 PKT~~ | ~~`E:\saxobackup\...\start_dashboard.bat`~~ | **Disabled 2026-08-20** — same missing-path problem as Daily Scan. Non-financial (just launches a local dashboard web page), so lower urgency, but was never actually working. |

---

## 6. How to check "did the scheduler actually run" yourself

Task Scheduler's `LastTaskResult=0` **used to be unreliable** for anything
launched through `run_hidden.vbs` (fixed 2026-08-20 — see
[forex_module.md notes](../.) equivalent in project memory). The trustworthy
check is always: **does the corresponding log file have a fresh
modification time close to the task's scheduled fire time?**

```powershell
Get-ScheduledTaskInfo -TaskName "ATOS Forex Daily Run" | Select LastRunTime, LastTaskResult
```
```bash
ls -la data/forex_scheduler.log   # mtime should be within minutes of LastRunTime
```

`scheduler_watchdog.py` (added 2026-08-20, runs every 30 min via
`ATOS Scheduler Watchdog`) automates exactly this check across every task
above and emails an alert the first time any task's log goes stale relative
to its expected schedule. See that script's docstring for the registry of
watched tasks and how to add a new one.

---

## Known gaps / open questions (flag before assuming fixed)

- **`ATOS Forex Gap Monday Early` and `ATOS Forex Gap Weekly`** fire at the
  literal same time with the literal same command — one is redundant but
  neither has been removed yet (lower urgency than the LBO duplication
  since neither has its own capital book — worst case is a slightly wasted
  extra scan, not a duplicate trade).
- **`ATOS Forex Gap Fill`**'s trigger (Sun 22:00 *PKT*, i.e. 17:00 UTC) does
  not obviously line up with the code's weekly gap window (22:00 UTC-06:00
  UTC Mon) — but Windows Task Scheduler's stored trigger time zone for this
  specific task wasn't fully disambiguated (some tasks show an explicit
  `+05:00` offset, this one doesn't), so treat this as "worth verifying,"
  not "confirmed broken."
- **Futures/ETF/stocks watchdog coverage** is registered in
  `scheduler_watchdog.py` but wasn't stress-tested as thoroughly as the
  forex path today — if you're debugging a futures/ETF scheduling issue,
  verify the watchdog's registry entry for it still matches the real bat
  file/log path.

This document is a snapshot from 2026-08-20. Task Scheduler and the bat
files are the actual source of truth if anything here looks stale —
`Get-ScheduledTask | Where TaskName -like "ATOS*"` and `cat run_*.bat` will
always tell you what's really configured.
