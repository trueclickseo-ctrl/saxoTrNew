# ATOS Forex LIVE — Scheduler Reference

Everything that runs automatically for the real-money LIVE account: what fires when, what each run actually does, how it avoids colliding with SIM, and how to verify/change it.

---

## The five LIVE-specific Windows Scheduled Tasks

Covers both real-money accounts under this Saxo login — the SEK account (donchian/ema/rsi, 34 core pairs) and the EUR account (rsi only, 83 exotic pairs, added 2026-08-26). They share the token keepalive task (same OAuth login) but have entirely separate confirmation gates, state, and P&L.

| Task | Trigger(s) | Command | Places real orders? |
|---|---|---|---|
| **ATOS Forex LIVE Daily Run** | every 45 min, `06:00-03:00` PKT | `python forex\runner.py --account live --strategy donchian,ema,rsi --live` | Yes — the only SEK-account task that can open a new position. Also checks exits every run. |
| **ATOS Forex LIVE Exit Check** | 1x/day: `14:00` | `python forex\runner.py --account live --strategy donchian,ema,rsi --exits-only --live` | SEK account. Manages existing positions only. Backstop only. |
| **ATOS Forex LIVE EUR Daily Run** | every 45 min, `06:00-03:00` PKT | `python forex\runner.py --account live_eur --strategy rsi --live` | Yes — the only EUR-account task that can open a new position. Also checks exits every run. Requires `SAXO_LIVE_EUR_CONFIRMED=1` (separate from the SEK account's gate). |
| **ATOS Forex LIVE EUR Exit Check** | 1x/day: `14:00` | `python forex\runner.py --account live_eur --strategy rsi --exits-only --live` | EUR account. Manages existing positions only. Backstop only. |
| **ATOS Saxo LIVE Token Keepalive** | every 15 min, all day | `python saxo_live_token_keepalive.py` | No — read-only token refresh, no trading logic. Shared by both accounts (same OAuth login — `saxo_auth._cfg()` normalizes `"live_eur"` to `"live"`). |

**Moved from 9 fixed times/day to every 45 min, 2026-08-25** — explicit user request for tighter checking. `forex/runner.py`'s `run_daily()` (what Daily Run calls) already handles both new-entry scanning and exit checking together every time it runs, so this one task alone covers "scan for new trades and exit on stop-loss/profit target."

**Window extended 06:00-22:00 → 06:00-03:00 PKT, 2026-08-26** — the FX trading day doesn't actually roll over until ~17:00 New York time (5pm ET, the industry-standard daily-candle convention), which lands at ~02:00 AM PKT during EDT — 4 hours after the old 22:00 cutoff. The old window was missing the tail end of the NY session; 03:00 PKT gives an hour of buffer past the real rollover point so the last scan of the day reliably sees the fully-closed candle.

Daily Run/Exit Check invoke `run_forex_live_daily.bat` / `run_forex_live_exits.bat` via `run_hidden.vbs` (silent, no console window), logging to `data/forex_live_scheduler.log` — a separate log from SIM's much noisier `forex_scheduler.log`, so a LIVE failure is never masked by SIM's own activity. Keepalive logs to `data/saxo_live_keepalive.log`.

**Registration scripts**: `setup_scheduler_live.ps1` (SEK Daily Run + Exit Check), `setup_scheduler_live_eur.ps1` (EUR Daily Run + Exit Check, added 2026-08-26), and `setup_saxo_live_keepalive.ps1` (Keepalive, shared) — all run as Administrator; safe to re-run any time the schedule needs to change (`-Force`/`Register-ScheduledTask -Force` cleanly replaces the existing triggers).

EUR Daily Run/Exit Check invoke `run_forex_live_eur_daily.bat` / `run_forex_live_eur_exits.bat`, logging to `data/forex_live_eur_scheduler.log` — its own log, separate from both SIM's `forex_scheduler.log` and the SEK account's `forex_live_scheduler.log`.

---

## Why the token keepalive task exists

Found 2026-08-25: the real-money LIVE Saxo app issues an access_token good for only 20 min and a refresh_token good for only 1 hour — both far shorter than SIM's (SIM's access_token lasts a full 24h; SIM never hits this problem). With the old 9-fixed-times schedule (gaps of ~2h between runs), the refresh_token was already dead before the next scheduled run even started — every run past the first hour post-login failed with a `TOKEN EXPIRED` alert email and was skipped entirely (no scan, no orders placed, real or otherwise).

`saxo_live_token_keepalive.py` calls `saxo_auth.get_valid_access_token(env="live")` every 15 min — comfortably inside the 20-min access-token window — so the refresh chain never goes fully cold between real trading runs. It does **not** replace the one-time interactive login itself (`python saxo_auth.py --live`, browser + your Saxo credentials) — only you can do that; this just keeps an already-valid login alive indefinitely afterward.

---

## Why every 45 min (not session-specific times anymore)

The 45-min cadence checks all 34 core pairs on every run regardless of what session PKT time falls in — no per-time pair filtering. This is simpler than the old 9-times/3-sessions design and matches the same reasoning that motivated it in the first place: a Donchian breakout / EMA crossover / RSI pullback signal reads "today's still-forming daily candle," so more frequent checks catch it sooner as the day develops, rather than waiting for one of a handful of fixed times.

For reference, the 34 core pairs' session liquidity (unchanged, just no longer used to pick specific scan times):

| Session | UTC | PKT (UTC+5) | Best for (of the 34 core pairs) |
|---|---|---|---|
| Asian (Tokyo/Sydney) | 00:00–09:00 | 05:00–14:00 | JPY, AUD, NZD (14 of 34) |
| London | 07:00–16:00 | 12:00–21:00 | EUR, GBP, CHF, NOK, SEK, DKK (20 of 34) |
| London–NY overlap | 12:00–16:00 | 17:00–21:00 | Deepest liquidity globally — covers the bulk of the pair list at once |

Verified in code: `SESSION_PAIRS["asian"]` (14) + `SESSION_PAIRS["london"]` (20) exactly equal all 34 `CORE_SYMBOLS` — no gaps, no overlap.

---

## Safety gates that apply at every trigger, regardless of time

1. **Strategy allowlist** — hard CLI error if anything outside `{donchian, ema, rsi}` is ever passed.
2. **`SAXO_LIVE_CONFIRMED=1`** — a second, separate confirmation env var required before `--live` can place a real order. Currently **set** (armed).
3. **Pair filter** — every scan is intersected with `CORE_SYMBOLS` before a signal can fire.
4. **Currency-matched account selection** — the LIVE login controls 3 sub-accounts (SEK/EUR/USD); every run explicitly resolves the SEK one by currency, hard-erroring rather than guessing if ever ambiguous.
5. **Cross-process lock** — `proc_lock.FOREX_LIVE_LOCK`, entirely separate from SIM's `FOREX_LOCK`, so a LIVE run never contends with SIM's `intraday_monitor.py` (which re-acquires the SIM lock every minute).
6. **Post-run reconciliation + auto-fix** — `safeguard_live.run_safeguard_live()` (its own file, `housekeeping_live.py`/`safeguard_live.py` — never SIM's `housekeeping.py`/`safeguard.py`) runs after every LIVE invocation: places a protective stop on any naked position found, resolves state mismatches, then re-fetches and verifies each fix before reporting it. See [forex_live_account.md](forex_live_account.md)'s "Housekeeping & safeguard" section for the full architecture.
7. **Portfolio heat / margin caps** — 6% combined open risk, 50% margin utilization — shared reasoning with SIM but computed against LIVE's own equity/positions only.

**EUR account note**: the same 7 gates apply, substituting `{rsi}` for the strategy allowlist, `EXOTIC_SYMBOLS` for the pair filter, `SAXO_LIVE_EUR_CONFIRMED` for the confirmation var, and `safeguard_live_eur.py`/`housekeeping_live_eur.py` for the post-run fix pass. One caveat the SEK account doesn't have: Saxo pools margin/balance AND positions/orders across all 3 sub-accounts under this Client (confirmed empirically, see [forex_live_account.md](forex_live_account.md)'s EUR section) — the 500 EUR cap and the housekeeping tier-filtering are both code-level compensations for that, not something Saxo enforces on its own.

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

**Resolved 2026-08-25**: the 3 `ATOS ETF Test Run N 2026-08-24` tasks flagged above (including the one at 22:00) were confirmed to be one-time, already-fired leftover test triggers from an earlier test session — removed entirely via `fix_stocks_etf_futures_hourly.ps1`, not an active task anymore.

**Superseded 2026-08-25**: LIVE's Daily Run moved from the 9 fixed times above to every 45 min (see "Why every 45 min" above) — this table's specific collision list is now historical (the exact minutes involved have changed), but the underlying principle (LIVE's schedule is the fixed reference point, SIM/other modules shift around it) still applies to any future collision.

---

## 2026-08-25 — first real trade

23:08 PKT, manual invocation (not yet via the scheduled task — that ran later once the schedule fix was applied): `donchian` opened **EURNOK short** (1,000 @ 10.86975, stop 10.98368, TP 10.52803) and **GBPUSD long** (1,000 @ 1.36466, stop 1.35165, TP 1.39047). Both bracket orders verified correct against Saxo's own web trader (Orders tab, Order Blotter, push notifications) — direction, stop/TP placement, and OCO linkage all exactly right. `housekeeping_live`/`safeguard_live` ran immediately after and confirmed clean.

---

## Verifying the current live schedule

```powershell
Get-ScheduledTask | Where-Object {$_.TaskName -like "ATOS Forex LIVE*" -or $_.TaskName -like "*LIVE Token Keepalive*"} | ForEach-Object {
    Get-ScheduledTaskInfo -TaskName $_.TaskName | Select-Object @{N='Name';E={$_.TaskName}}, LastRunTime, NextRunTime, LastTaskResult
}
```

Confirming the repeating trigger itself is actually correct (not just a one-time occurrence — see the 2026-08-25 findings in [scheduling.md](scheduling.md) for why this specific check matters):
```powershell
schtasks /query /tn "ATOS Forex LIVE Daily Run" /xml | Select-String -Pattern "ScheduleByDay|Repetition|Interval|Duration"
```
Should show a `CalendarTrigger` with both `ScheduleByDay` (real daily recurrence) and a nested `Repetition` block (the 45-min sub-daily interval) — if `ScheduleByDay` is missing, the trigger only fires once, ever.

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
