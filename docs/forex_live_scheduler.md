# ATOS Forex LIVE — Scheduler Reference

Everything that runs automatically for the real-money LIVE account: what fires when, what each run actually does, how it avoids colliding with SIM, and how to verify/change it.

---

## The two LIVE-specific Windows Scheduled Tasks

| Task | Trigger(s) | Command | Places real orders? |
|---|---|---|---|
| **ATOS Forex LIVE Daily Run** | 9x/day: `06:00, 08:00, 10:00, 12:30, 14:30, 16:30, 18:00, 20:00, 22:00` | `python forex\runner.py --account live --strategy donchian,ema,rsi --live` | Yes — the only task that can open a new position |
| **ATOS Forex LIVE Exit Check** | 1x/day: `14:00` | `python forex\runner.py --account live --strategy donchian,ema,rsi --exits-only --live` | Manages existing positions only — never opens new ones |

Both invoke `run_forex_live_daily.bat` / `run_forex_live_exits.bat` via `run_hidden.vbs` (silent, no console window), logging to `data/forex_live_scheduler.log` — a separate log from SIM's much noisier `forex_scheduler.log`, so a LIVE failure is never masked by SIM's own activity.

**Registration script**: `setup_scheduler_live.ps1` (run as Administrator; safe to re-run any time the schedule needs to change — `-Force` cleanly replaces the existing triggers).

---

## Why these 9 times

3 scan times within each of the 3 FX sessions, so a Donchian breakout / EMA crossover / RSI pullback signal — all of which read "today's still-forming daily candle" — has multiple chances to be caught as it develops through the day, rather than only at one fixed check.

| Session | UTC | PKT (UTC+5) | Scan times chosen | Best for (of the 34 core pairs) |
|---|---|---|---|---|
| Asian (Tokyo/Sydney) | 00:00–09:00 | 05:00–14:00 | 06:00, 08:00, 10:00 | JPY, AUD, NZD (14 of 34) |
| London | 07:00–16:00 | 12:00–21:00 | 12:30, 14:30, 16:30 | EUR, GBP, CHF, NOK, SEK, DKK (20 of 34) |
| London–NY overlap | 12:00–16:00 | 17:00–21:00 | 18:00, 20:00, 22:00 | Deepest liquidity globally — covers the bulk of the pair list at once |

Verified in code: `SESSION_PAIRS["asian"]` (14) + `SESSION_PAIRS["london"]` (20) exactly equal all 34 `CORE_SYMBOLS` — no gaps, no overlap. Every scan still checks all 34 pairs regardless of which "session" label the time falls under — there's no separate pair filter per scan time, only the schedule reasoning is session-based.

---

## Safety gates that apply at every trigger, regardless of time

1. **Strategy allowlist** — hard CLI error if anything outside `{donchian, ema, rsi}` is ever passed.
2. **`SAXO_LIVE_CONFIRMED=1`** — a second, separate confirmation env var required before `--live` can place a real order. Currently **set** (armed).
3. **Pair filter** — every scan is intersected with `CORE_SYMBOLS` before a signal can fire.
4. **Currency-matched account selection** — the LIVE login controls 3 sub-accounts (SEK/EUR/USD); every run explicitly resolves the SEK one by currency, hard-erroring rather than guessing if ever ambiguous.
5. **Cross-process lock** — `proc_lock.FOREX_LIVE_LOCK`, entirely separate from SIM's `FOREX_LOCK`, so a LIVE run never contends with SIM's `intraday_monitor.py` (which re-acquires the SIM lock every minute).
6. **Post-run reconciliation + auto-fix** — `safeguard_live.run_safeguard_live()` (its own file, `housekeeping_live.py`/`safeguard_live.py` — never SIM's `housekeeping.py`/`safeguard.py`) runs after every LIVE invocation: places a protective stop on any naked position found, resolves state mismatches, then re-fetches and verifies each fix before reporting it. See [forex_live_account.md](forex_live_account.md)'s "Housekeeping & safeguard" section for the full architecture.
7. **Portfolio heat / margin caps** — 6% combined open risk, 50% margin utilization — shared reasoning with SIM but computed against LIVE's own equity/positions only.

---

## Relationship to SIM's schedule (conflict check, 2026-08-25)

LIVE and SIM share **zero** state — separate Saxo accounts, separate lock files, separate state/orders files, separate `pnl_tracker` module (`forex_live` vs `forex`). There is no *functional* conflict possible between them. There was, however, a **wall-clock scheduling collision** — several SIM tasks happened to fire at the exact same minute as LIVE's schedule. Resolved by shifting the SIM tasks a few minutes later (LIVE's times are the fixed reference point, never moved):

| SIM task | Was | Now | Collided with |
|---|---|---|---|
| ATOS Forex Intraday Scan | 06:00 | 06:05 | LIVE Daily Run's 06:00 |
| ATOS Futures Discover | 06:00 | 06:10 | same |
| ATOS Forex Exit Check | 14:00 | 14:05 | LIVE Exit Check |
| ATOS Forex London Run | 18:00 | 18:05 | LIVE Daily Run's 18:00 |

Fix script: `fix_sim_schedule_conflicts.ps1` (run as Administrator).

**Not touched**: `ATOS LBO NY Open` also sits at 18:00, deliberately — LBO's own code auto-detects the real NY session boundary rather than blindly trading at trigger time, and the forex "all strategies" scan already explicitly excludes `london_breakout` from its own run (documented in `forex/runner.py`) — a pre-existing, understood coexistence, not a real collision.

**Flagged but not changed**: `ATOS ETF Test Run 2 2026-08-24` also sits at 22:00 — its name suggests a temporary one-off test task, not a permanent fixture; left alone pending confirmation it's still needed.

---

## Verifying the current live schedule

```powershell
Get-ScheduledTask | Where-Object {$_.TaskName -like "ATOS Forex LIVE*"} | ForEach-Object {
    [PSCustomObject]@{ Name = $_.TaskName; State = $_.State; Times = ($_.Triggers.StartBoundary -join ", ") }
}
```

Confirming the confirmation gate is still set:
```powershell
[System.Environment]::GetEnvironmentVariable("SAXO_LIVE_CONFIRMED","User")
```

## Turning automation off (without deleting anything)

```powershell
[System.Environment]::SetEnvironmentVariable("SAXO_LIVE_CONFIRMED", $null, "User")
```
Leaves the scheduled tasks in place (still dry-running/logging) but stops any real order from being placed. To remove the tasks entirely instead:
```powershell
Unregister-ScheduledTask -TaskName "ATOS Forex LIVE Daily Run" -Confirm:$false
Unregister-ScheduledTask -TaskName "ATOS Forex LIVE Exit Check" -Confirm:$false
```
