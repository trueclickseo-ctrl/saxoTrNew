# ATOS Forex LIVE — Scheduler Reference

Everything that runs automatically for the real-money LIVE account: what fires when, what each run actually does, how it avoids colliding with SIM, and how to verify/change it.

---

## The five LIVE-specific Windows Scheduled Tasks

Covers both real-money accounts under this Saxo login. **As of the 2026-09-01 consolidation (`c6af3c7`) the SEK account is the only one that opens new positions** — `LIVE_ALLOWED_STRATEGIES = {"rsi"}` (RSI on the 17 HIGH_VOLUME pairs). The EUR account is **exits-only**: `LIVE_EUR_ALLOWED_STRATEGIES = set()` — it still holds `rsi:*` positions from before and manages them out, but takes no new entries. They share the token keepalive task (same OAuth login) but have entirely separate confirmation gates, state, and P&L.

| Task | Trigger(s) | Command | Places real orders? |
|---|---|---|---|
| **ATOS Forex LIVE Daily Run** | every 45 min, `06:00-03:00` PKT | `python forex\runner.py --account live --live` | Yes — the only task that opens a new position. `--strategy` **omitted** so it resolves to `LIVE_ALLOWED_STRATEGIES` (`{rsi}`) itself — see the 2026-08-28 fix below. Also checks exits every run. |
| **ATOS Forex LIVE Exit Check** | 1x/day: `14:00` | `python forex\runner.py --account live --exits-only --live` | SEK account. Manages existing positions only. Backstop only. |
| **ATOS Forex LIVE EUR Daily Run** | every 45 min, `06:00-03:00` PKT | `python forex\runner.py --account live_eur --live` | **No** (exits-only account). `--strategy` omitted → resolves to `LIVE_EUR_ALLOWED_STRATEGIES` (empty). Runs an exits/reconcile pass for the held `rsi:*` positions. Requires `SAXO_LIVE_EUR_CONFIRMED=1` for a real close order. Redundant with the Exit Check task — a candidate to disable. |
| **ATOS Forex LIVE EUR Exit Check** | 1x/day: `14:00` | `python forex\runner.py --account live_eur --exits-only --live` | EUR account. Manages existing positions only. Backstop only. |
| **ATOS Saxo LIVE Token Keepalive** | every 10 min, all day, **as SYSTEM** | `python saxo_live_token_keepalive.py` | No — read-only token refresh, no trading logic. Shared by both accounts (same OAuth login — `saxo_auth._cfg()` normalizes `"live_eur"` to `"live"`). Hardened 2026-08-30 (SYSTEM + `StartWhenAvailable` + 3× restart) so a reboot/sleep gap can't kill the 1h refresh token. |

**2026-08-28 / 2026-09-02 — never hard-code `--strategy` in a LIVE `.bat`.** `forex/runner.py --account live[_eur]` validates every `--strategy` value against `LIVE_ALLOWED_STRATEGIES` / `LIVE_EUR_ALLOWED_STRATEGIES` and **`ap.error()`s (exit code 2) on any mismatch — before it checks exits.** The SEK exit `.bat` hit this on 2026-08-28 (hard-coded `donchian,ema,rsi`, none still allowed); **the two EUR `.bat` files hit it on 2026-09-01** — the consolidation emptied `LIVE_EUR_ALLOWED_STRATEGIES` but they still passed `--strategy rsi`, so the EUR exit-check exited 2 on **every scheduled run for ~2 days** (no trailing stops / time-stops / reconciliation on the open EUR positions — broker OCO brackets kept them safe, but nothing else ran). Fixed 2026-09-02 (`1ec531c`): omit `--strategy` everywhere and let the allowlist resolve it, so a future allowlist change can never desync a `.bat` again. The watchdog was also blind to it — see the exit-guard section below.

**Moved from 9 fixed times/day to every 45 min, 2026-08-25** — explicit user request for tighter checking. `forex/runner.py`'s `run_daily()` (what Daily Run calls) already handles both new-entry scanning and exit checking together every time it runs, so this one task alone covers "scan for new trades and exit on stop-loss/profit target."

**Window extended 06:00-22:00 → 06:00-03:00 PKT, 2026-08-26** — the FX trading day doesn't actually roll over until ~17:00 New York time (5pm ET, the industry-standard daily-candle convention), which lands at ~02:00 AM PKT during EDT — 4 hours after the old 22:00 cutoff. The old window was missing the tail end of the NY session; 03:00 PKT gives an hour of buffer past the real rollover point so the last scan of the day reliably sees the fully-closed candle.

Daily Run/Exit Check invoke `run_forex_live_daily.bat` / `run_forex_live_exits.bat` via `run_hidden.vbs` (silent, no console window), logging to `data/forex_live_scheduler.log` — a separate log from SIM's much noisier `forex_scheduler.log`, so a LIVE failure is never masked by SIM's own activity. Keepalive logs to `data/saxo_live_keepalive.log`.

**Registration scripts**: `setup_scheduler_live.ps1` (SEK Daily Run + Exit Check), `setup_scheduler_live_eur.ps1` (EUR Daily Run + Exit Check, added 2026-08-26), and `setup_saxo_live_keepalive.ps1` (Keepalive, shared) — all run as Administrator; safe to re-run any time the schedule needs to change (`-Force`/`Register-ScheduledTask -Force` cleanly replaces the existing triggers).

EUR Daily Run/Exit Check invoke `run_forex_live_eur_daily.bat` / `run_forex_live_eur_exits.bat`, logging to `data/forex_live_eur_scheduler.log` — its own log, separate from both SIM's `forex_scheduler.log` and the SEK account's `forex_live_scheduler.log`.

---

## Why the token keepalive task exists

Found 2026-08-25: the real-money LIVE Saxo app issues an access_token good for only 20 min and a refresh_token good for only 1 hour — both far shorter than SIM's (SIM's access_token lasts a full 24h; SIM never hits this problem). With the old 9-fixed-times schedule (gaps of ~2h between runs), the refresh_token was already dead before the next scheduled run even started — every run past the first hour post-login failed with a `TOKEN EXPIRED` alert email and was skipped entirely (no scan, no orders placed, real or otherwise).

`saxo_live_token_keepalive.py` calls `saxo_auth.get_valid_access_token(env="live")` every 10 min — comfortably inside the 20-min access-token window — so the refresh chain never goes fully cold between real trading runs. It does **not** replace the one-time interactive login itself (`python saxo_auth.py --live`, browser + your Saxo credentials) — only you can do that; this just keeps an already-valid login alive indefinitely afterward.

**Hardened 2026-08-30**: the task kept dying whenever the machine rebooted or slept with nobody logged in — the interactive-user task simply didn't run, and one gap > 60 min kills the refresh token permanently (→ manual browser re-login). It now runs as **SYSTEM** (no logon needed), with `StartWhenAvailable` (a missed run fires on catch-up instead of being dropped), a 10-min cadence, and 3× restart-on-failure. SYSTEM can't read user env vars, so `SAXO_LIVE_APP_KEY` was promoted to a **Machine** env var — it's a public OAuth client id, not a secret (and the LIVE app is PKCE, so there is no `SAXO_LIVE_APP_SECRET`). Re-run `setup_saxo_live_keepalive.ps1` **as Administrator** to apply.

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

## 2026-09-02 — exit-guard against stale local state (`1ec531c`)

**Incident.** After the EUR `.bat` was fixed (above) the exit-check was triggered *before local state had been reconciled*. It saw a ~2-day-stale `rsi:NZDCAD` (marked open; actually stopped out broker-side hours earlier), fired `hard_stop`, and `_run_exits` sent a market `Sell 9,000`. **FX spot has no reduce-only** — a Sell against a flat position is a new short, so Saxo *opened* a 9,000 NZDCAD short. safeguard_live_eur stopped it and escalated; it was closed manually (~€8.70 cost).

**Fix — `forex/runner.py _live_position_open(uic, qty, direction, n_tracked)`** → `"open" | "gone" | "unknown"`. `_run_exits` calls it before every real LIVE close order:

- **`"gone"`** (a healthy positions snapshot with no matching row) → book the close from Saxo's own `ClosedPosition` record, send **no order**, raise a low-severity `attention` item. `_reconcile_closed_vs_saxo()` corrects the exact price/P&L afterwards.
- **`"unknown"`** (the lookup failed, or 0 FxSpot rows came back while several positions are tracked = a degraded fetch) → fall through to the **normal** close, so a genuine exit is never suppressed by a transient API problem.
- **`"open"`** — an exact signed-size match, **or** (2026-09-02 RSI-audit follow-up) a same-Uic **same-direction** position whose broker `Amount` merely *differs* from `pos["quantity"]` (aggregation / a partial manual close / a stop-heal that re-placed a different lot). The earlier code returned `"gone"` there and phantom-booked the close, stranding the real live position **naked**. A residual after a normal close is only a reconcile nit; a naked position is a real hazard. An opposite-direction position at any size is still `"gone"` (netted out).

Never runs on a dry-run, a paper position, or SIM. Tests: `test_2026_09_02_exit_guard_stale_position.py` (11).

**Operational rule this encodes:** never trigger a LIVE run (or a scheduled LIVE task) until local state has been reconciled against Saxo — `python reconcile_closed_trades_vs_saxo.py`, or a dry `--exits-only` run eyeballed against `saxo_client.get_positions()`. The guard is a backstop, not a licence to skip that.

### Watchdog blind spot that hid the 2-day outage (`1ec531c`)

`scheduler_watchdog.py`'s result-code path trusted a *fresh log* over a non-zero `LastTaskResult` (a non-zero code is often just a mid-run reading). But `run_hidden.vbs` kept appending the argparse reject to the big append-mode `forex_live_eur_scheduler.log` every run, so its mtime stayed perfectly fresh. `_log_content_failure`'s 200-byte size gate never fires on a large log. Fix: **`_log_tail_failure()`** (no size gate) scans the log tail for `_CLI_REJECT_SIGNATURES` (`"runner.py: error:"`, `"usage: runner.py"`, `"only allows"`, …) and the existing crash signatures; on the non-zero-code path a fresh log is trusted **only when the tail is also clean.** Tests: `test_2026_09_02_watchdog_cli_reject.py` (7).

## 2026-09-02 — `torch` no longer aborts the whole runner (RSI audit)

`forex/runner.py` imported `forex.strategy_cnn_lstm` unconditionally, and that module does a top-level `import torch`. On any interpreter without torch (the `.bat` files call bare `pythonw` off `PATH`; the two-phase performance tracker deliberately runs its build step under `py -3.12`, which has no torch) the import raised at module load — **the entire runner died before entries or exits ran**, taking the LIVE RSI book down with it.

Fix: the two `cnn_lstm` imports are wrapped in `try/except` → `None`, `STRATEGIES` is filtered to drop any strategy whose module failed to load (so `_VALID_STRATS` and `--strategy` validation stay honest), and the `--scan` CNN-LSTM panel guards for it. Verified: `import forex.runner` succeeds on system Python (no torch, 18 strategies) and is unchanged under `.venv` (torch 2.13, 20 strategies). No `--strategy cnn_lstm` / `advanced_cnn_lstm_master` on any account without torch — those two are SIM-only and never in a LIVE allowlist anyway.

## 2026-09-02 — data-folder write permissions (`fix_data_permissions.ps1`)

The scheduled tasks run with **RunLevel HIGHEST**. A file an elevated task creates via `os.replace()` is owned by `BUILTIN\Administrators` and only carries the folder's *inheritable* ACEs — and `data/` granted the user FullControl on the folder with `InheritanceFlags = None`, so that never reached the files. Result: every state / orders / weights / observation-card file last written by a scheduled run was **readable but not writable from a normal (non-elevated) shell** — a manual `python forex\runner.py --live` died with `PermissionError` in `_save_state()`.

`fix_data_permissions.ps1` (no elevation needed): adds an **inheritable** `(OI)(CI)` Modify ACE for the current user to `data/`, `logs/`, `saxo_etf_strategy/data/` — so every file created there from now on is user-writable regardless of which process made it — then rewrites each currently-locked file in place (temp + `os.replace`, using the folder's `DELETE_CHILD` right) so it picks up the new inherited ACE. Re-run any time; files a task is actively holding open are skipped and self-heal on their next atomic write.

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
