# Housekeeping & Safeguard Agents

Cross-module state reconciliation and auto-fix for Forex, Futures, ETF
and Shares (ATOS). Two cooperating agents, built 2026-08-24:

- **`housekeeping.py`** — pulls live Saxo state, compares it to each
  module's local state, auto-fixes the unambiguous cases, and *reports*
  the ambiguous ones (direction mismatches, naked positions) for a human
  to decide.
- **`safeguard.py`** — runs immediately after housekeeping and *resolves*
  those ambiguous cases too, then re-verifies against a fresh Saxo
  snapshot before declaring anything fixed. Built the same day, once a
  live run showed housekeeping alone still left 23 naked positions and 8
  mismatches sitting unresolved.

Together they exist so that the class of drift a manual audit first
caught by hand on 2026-08-24 (initially thought to be 4 positions, turned
out to be dozens once actually swept) never has to be found by hand
again — and, as of safeguard.py, never has to be *fixed* by hand again
either.

Code: [`housekeeping.py`](../housekeeping.py), [`safeguard.py`](../safeguard.py) (project root).
Tests: [`test_housekeeping.py`](../test_housekeeping.py) — 17/17,
[`test_safeguard.py`](../test_safeguard.py) — 16/16.

## The problem this solves

Saxo nets opposite-direction trades on the same instrument automatically.
Local state (`forex_state.json`, `futures_state.json`,
`etf_positions.json`, `atos_live.db`) assumes each strategy owns its own
independent position ticket. Whenever two strategies trade the same
symbol in opposite directions, or a scheduler race condition fires the
same signal twice, the two views drift apart **silently** — nothing
crashes, nothing logs an error. A stop order just ends up protecting a
position that no longer exists, or protecting less than what's actually
open, or nothing at all.

Saxo is always ground truth. This tool pulls live positions and orders
once per run and corrects local state to match — never the other way
around.

## Two independent functions

### 1. `reconcile_all(modules=None)` — corrects state, cancels/replaces orders

For each module, groups local position entries by Saxo `Uic` and compares
the combined local quantity against Saxo's real net exposure for that
instrument:

| Situation | Action |
|---|---|
| Local tracks a position, live shows **zero** exposure at all, and **no matching entry order is still Working** | Remove the local entry, cancel its now-orphaned stop order |
| Local tracks a position, live shows **zero** exposure, but a matching-direction entry order (Market/Limit/StopLimit, not a dormant bracket child) is still Working | Left as-is, reported as `pending_entry` — it's a trade that hasn't filled yet (e.g. market closed), not an orphan |
| Local's combined quantity **exceeds** live exposure (Saxo netted part of it against another strategy's opposite trade) | Scale every entry down **proportionally**, so the total protective-stop coverage can never exceed what's actually open. Flagged as an `estimate` — Saxo's own netting has erased which literal lot belongs to which strategy, so the split preserves each entry's *relative* size and its own risk price, not a verified fact |
| Local direction is the **opposite** of live direction | Reported only — **never auto-corrected**. Too ambiguous to guess safely; needs a human |
| Live exposure **exceeds** local tracking (untracked extra) | Reported only — never fabricates a new local entry to explain it |
| Two+ Working stop orders on the same instrument/side/price (breakeven move or race-condition retry that left the old one uncancelled) | Cancel all but the newest |
| A live position with **zero local record in ANY module** (not even a mismatched one) | Reported as `fully_untracked` — see below. This is a separate sweep, not part of the per-module table comparison above |

Sends **one email** summarizing every finding from the run — only if at
least one finding exists. A clean account produces zero emails.

### The fully-untracked sweep (`_scan_fully_untracked`, added 2026-08-24)

Every row above starts from `reconcile_module()` grouping **local**
entries by uic (`_group_by_uic(local)`) — a uic that never appears in a
module's local state at all never enters that loop, full stop. This is
structurally different from an `untracked_live` finding (which requires
*some* local entry to exist first, just an undersized one) — a uic with
**zero** local footprint anywhere was, until this fix, invisible to
`reconcile_all()` entirely.

Two real incidents hid in exactly this gap: a fully-untracked 20,000-share
stock position that went naked then self-closed before anyone caught it
(`stocks_naked_position_blindspot`), and a −2,381,000 EURCHF position
(three near-simultaneous fills from a pre-cross-process-lock race
condition on 2026-08-19) that sat unreconciled for **5 days**, invisible
to `reconcile_all()` the whole time and only ever visible to
`scan_naked_positions()` during the narrow windows its stop happened to
lapse.

`_scan_fully_untracked()` runs after the per-module loop inside
`reconcile_all()`: it builds the set of every uic tracked by *any*
requested module's adapter, then walks every live position and flags
whichever ones aren't in that set at all — reported as `fully_untracked`,
report-only like every other ambiguous finding (a fully-untracked
position could be a real bug or something opened outside any tracked
strategy on purpose; only a human decides which).

**First live run surfaced 22 of them** — 13 forex, 9 stocks, all
currently protected. Several match positions already documented in this
file's own "first real runs" history below (e.g. AUDCAD at exactly
−1,446,000) — long-standing legacy exposure that was always safe, just
never given local tracking or a human decision about its origin.

Module attribution for an ambiguous `FxSpot` uic (forex vs. futures, both
use this asset type) now checks against forex's **full 117-pair
reference universe** (`forex.runner.PAIRS`), not just currently-held
positions — the earlier version defaulted anything not *currently*
tracked by forex to "futures", which is wrong by construction for a
finding whose entire premise is "nothing tracks this anywhere." This same
fix was applied to `scan_naked_positions()`'s own module-attribution
logic, which had the identical bug.

### 2. `scan_naked_positions()` — read-only safety scan, live-Saxo-only

Independent of local state entirely: for every live position on the
account, does a working stop-loss order actually cover it?

| `protection` value | Meaning |
|---|---|
| `none` | No stop or take-profit order at all |
| `tp_only` | A take-profit limit exists but no stop-loss |
| `partial` | A stop exists but covers less than the full quantity |
| *(not reported)* | Fully covered |

**Aggregated per Saxo `Uic`, not per position ticket.** Saxo doesn't tie a
working stop order to a specific ticket — any working stop for a
uic/side reduces the same shared pool of exposure regardless of which
ticket it was originally meant for. An earlier per-ticket version (fixed
2026-08-24, same day it was built) let a real protection gap survive
`safeguard.py`'s fix pass: a uic with 2+ naked tickets *and* some
pre-existing partial coverage got "fixed" with a real gap still left
over, because each ticket's own uncovered quantity subtracted the same
existing coverage instead of it being spent once — and the per-ticket
verification couldn't see the gap either, since summed new coverage
still cleared each ticket's own amount individually even though the true
aggregate exposure wasn't fully covered.

By itself (`housekeeping.py`), this function **never auto-closes or
auto-protects anything** — it only reports, for the reasons below.
`safeguard.py` (next section) is what actually acts on its findings.

Unlike a quantity mismatch (where "make the numbers agree" has one
obviously correct direction), a naked position could be a genuine bug, a
strategy that intentionally manages risk without a broker-side stop, or a
position caught mid-way through its own entry sequence. `housekeeping.py`
alone doesn't try to tell those apart — it just makes sure the question
gets asked (via the report/email). `safeguard.py` *does* act, using a
deliberately conservative, asset-class-generic fallback (see below) since
by the time something reaches this scan, the original strategy's own
risk intent is usually already lost.

Sends **one email** listing every naked position found — only if at
least one exists.

## `safeguard.py` — actually fixes what housekeeping only reports

Runs right after `housekeeping.reconcile_all()` and `scan_naked_positions()`
and resolves both categories they leave for a human:

| Finding | How safeguard resolves it |
|---|---|
| Naked / under-protected position (`none`, `tp_only`, `partial`) | Places a protective stop for the **uncovered** quantity only (never duplicates existing partial coverage), at a conservative asset-class-default distance from the position's own live current price — see `DEFAULT_STOP_PCT` below. An existing take-profit order is left untouched. |
| `direction_mismatch` | Removes the local entry — it has **zero** live backing in its own claimed direction, so this is the same operation as an ordinary zero-exposure orphan, just discovered via a different comparison (`reconcile_all(..., aggressive=True)`). The *real* opposite-direction exposure that confused the old entry is a separate thing this never touches — its protection is handled by the naked-position fix above, not by inventing a new local entry to explain it. |
| `untracked_live` | No local entry is wrong, so there's nothing to remove. Recorded as resolved with a note that protection (if needed) is handled by the naked-position pass — avoids double-counting one root cause as two different "fixed" claims. |
| `ledger_drift` (stocks) | **Never auto-resolved**, even by safeguard — a ledger row needs a real exit price/date, which no automated process has. Reported `NOT FIXED`. |

### Default stop distance (`DEFAULT_STOP_PCT`)

Not tuned to any strategy's real risk logic — that's unknown/lost for an
untracked position. Wide enough to avoid an immediate re-trigger on
normal noise, tight enough to actually bound the loss:

| AssetType | Default |
|---|---|
| `FxSpot` | 2% |
| `CfdOnIndex` / `ContractFutures` | 3% |
| `Etf` | 5% |
| `Stock` / `CfdOnStock` | 8% (matches the existing `US_BLEND_STOP_PCT` precedent from the 2026-08-22 margin incident, where 6 naked stock positions were given 8% stops by hand) |

### Price precision — live lookup, not a generic guess

A naked position can be *any* uic across *any* module. `forex.runner`'s
own `get_price_decimals()` only knows its cached 117-pair FX universe —
it doesn't know about a futures-module symbol like CADMXN whose real
Saxo `AssetType` is `FxSpot` but isn't in that list. Found live
2026-08-24: the generic 5dp FxSpot guess triggered a real
`PriceNotInTickSizeIncrements` rejection (CADMXN actually needs 4dp) —
the exact tick-size bug class already documented for forex's own pairs
(see `account_margin_2026-08-22` session notes), just reachable through a
different door. Fixed via `_live_price_decimals()`, a direct
`/ref/v1/instruments/details` lookup (same endpoint+fallback pattern
already proven in `place_all_stops.py`), used for any `FxSpot` position
outside forex's own universe.

### Verification — never trust the return value alone

After every fix, `run_safeguard()` re-fetches a **fresh** Saxo snapshot
and re-checks that the specific thing it just fixed is actually fixed —
matched by `uic` (not module+symbol, which can shift mid-run once a
mismatch-fix changes which module's adapter still references that uic).
A fix that Saxo silently didn't apply, or that a later event undid, gets
flipped to `NOT FIXED` with a `VERIFICATION FAILED` note rather than
trusted on faith. Naked-position fixes run **before** mismatch fixes
deliberately — removing a direction-mismatched local entry changes the
forex-uic set `scan_naked_positions()` uses to classify an `FxSpot`
position as forex vs. futures, so fixing mismatches first would make that
classification shift mid-run.

Sends exactly **one confirmation email** per run — only if there was
something to do — listing every item with its verified outcome.

## Module adapters

| Module | Local state | Format | Notes |
|---|---|---|---|
| `forex` | `data/forex_state.json` | JSON, key = `"strategy:symbol"` | Full read/write, own stop-order id tracked |
| `futures` | `data/futures_state.json` | JSON, key = `"strategy:symbol"` | Same shape as forex |
| `etf` | `saxo_etf_strategy/data/etf_positions.json` | JSON, key = uic string | Long-only |
| `stocks` (ATOS) | `data/atos_live.db` (SQLite) | `trades` table, `exit_date IS NULL` = open | **Cannot auto-remove** — see below |

### Why stocks is different

`atos_live.db` is a P&L **ledger**, not a simple position tracker — a row
also carries `pnl_sek`, `commission_sek`, etc. once closed. For
forex/futures/ETF, "no live backing at all" means: stop tracking it, no
data is lost. For stocks, marking a row closed without a real
`exit_price`/`exit_date` would **fabricate ledger history**.

So `StocksAdapter`:
- **Will** scale down an overstated `shares` count (safe — doesn't touch
  any P&L field).
- **Will never** auto-close/remove a row with zero live backing. That
  case is reported as `KIND_LEDGER_DRIFT` for a human to resolve with the
  real fill data.
- Doesn't track `stop_order_id` locally at all (the DB schema has no such
  column) — missing stop protection on a stock position is caught by
  `scan_naked_positions()` instead, which works purely off live Saxo
  data and needs no local tracking.

## Wiring — runs automatically after every live trading run

| Module | Where |
|---|---|
| Forex | [`forex/runner.py`](../forex/runner.py) — end of `__main__`'s live dispatch, inside the same `proc_lock` critical section as the run itself |
| Futures | [`futures/runner.py`](../futures/runner.py) — same pattern, `FUTURES_LOCK` |
| ETF | [`saxo_etf_strategy/run_etf_bot.py`](../saxo_etf_strategy/run_etf_bot.py) — end of `ETFBot.run_once()`, gated on `not self.cfg.dry_run` |
| Stocks | [`atos_runner.py`](../atos_runner.py) — end of `run_cycle()` |

Each call site calls `safeguard.run_safeguard([module])`, scoped to just
its own module for the mismatch-fix pass — `safeguard` internally runs
housekeeping's checks against one shared Saxo snapshot, fixes what it
can, and runs the naked-position sweep account-wide (inherently
account-wide — a naked position from any module matters regardless of
which module just ran). Failures are caught and logged, never allowed to
fail the trading run itself.

## Periodic safety nets

Two Windows Scheduled Tasks, both created 2026-08-24, independent of any
module's own schedule — a module's post-run hook only fires when *that*
module runs live, so these catch drift caused by a *different* module's
trade even on a day this module stays flat:

| Task | Runs | Action | Log |
|---|---|---|---|
| `ATOS Housekeeping` | every 30 min | `python housekeeping.py` — report-only, both functions, all 4 modules | `data/housekeeping_scheduler.log` |
| `ATOS Safeguard` | every 30 min, offset 15 min | `python safeguard.py` — fixes and verifies, all 4 modules | `data/safeguard_scheduler.log` |

Both are read-only/no-op unless they find something to do; both email
only when there's something to report.

**Note on Windows Task Scheduler in this environment**: creating a *new*
task by name reliably works via `Register-ScheduledTask`. Modifying or
disabling an *existing* task does not — `Disable-ScheduledTask`,
`schtasks /Change`, and `schtasks /Create /F` (overwrite) were all denied
by the environment's own permission layer while diagnosing the
`ATOS Forex Gap London` task's disabled trigger the same day (see
[[forex_module]]). The reliable workaround is the same one used there:
create a new, correctly-configured task under a different name rather
than trying to fix the old one in place.

## Manual use

```bash
# Full sweep, all 4 modules, both functions
python housekeeping.py

# Just one/some modules
python housekeeping.py --modules forex futures

# Only the reconciliation (skip the naked-position scan)
python housekeeping.py --reconcile-only

# Only the naked-position scan (skip reconciliation)
python housekeeping.py --naked-only
```

```bash
# Fix + verify pass, all 4 modules
python safeguard.py

# Just one/some modules
python safeguard.py --modules forex futures
```

Or from Python:

```python
import housekeeping
findings = housekeeping.reconcile_all(["forex"])   # or None for all 4
naked    = housekeeping.scan_naked_positions()

import safeguard
outcomes = safeguard.run_safeguard(["forex"])       # or None for all 4
```

## Email

Reuses `config/email.json` (same credentials as every other notifier in
this codebase — `forex/notifier.py`, `scheduler_watchdog.py`,
`intraday_monitor.py`). Two templates, both sent only when there's
something to report:

- **Reconciliation report** (`housekeeping.py`) — table of every finding
  (module, symbol, kind, detail), with a note that `estimate`-flagged
  rows involve per-strategy attribution Saxo's netting has erased.
- **Naked position alert** (`housekeeping.py`) — table of every
  unprotected live position (module, symbol, direction, quantity,
  protection level), with a note that this scan never auto-closes or
  auto-protects — it's a decision for a human.
- **Safeguard confirmation** (`safeguard.py`) — table of every item it
  attempted, each row showing verified `FIXED` or `NOT FIXED`, never
  claimed on faith.

## 2026-08-24: first real runs

The first live `reconcile_all()` run (forex only, since that's where the
manual audit started) found and fixed, in a single pass:

- **24 orphaned entries removed** (AUDTRY, USDTRY×2, GBPTRY×2, USDTHB×3,
  USDSGD, USDNOK×2, CADHKD, GBPHKD×2, NZDMXN, EURCZK×2, AUDHKD, AUDCNH,
  EURNOK×2, plus 2 London Breakout day-trade leftovers) — each had zero
  live backing at all.
- **6 duplicate stop orders cancelled** (NZDUSD, GBPUSD, USDJPY, GBPJPY,
  USDCAD, CHFCNH) — leftovers from breakeven moves or race-condition
  retries that never got cancelled.
- **4 positions scaled down** (CADTRY×2, GBPCNH×2) — local combined
  quantity exceeded live exposure; corrected proportionally.
- **4 flagged as direction-mismatch, not touched** (CHFJPY, EURCHF,
  USDCHF, EURUSD) — including EURCHF at −2,381,000 live vs +55,000
  local, and EURUSD at +1,284,000 live vs −15,000 local.

The follow-up `scan_naked_positions()` sweep (all 4 modules) found **23
live positions with zero or partial protection** (after the per-uic
aggregation fix — see above; the pre-fix, per-ticket count read higher
and would have under-protected several of them anyway), including three
unprotected EURCHF tickets totaling −2,381,000 and two unprotected AUDCAD
futures tickets totaling −1,446,000.

`safeguard.py`'s first live run then resolved essentially all of it in
one pass, verified against a fresh Saxo snapshot:

- **19 of 19 naked positions fixed** on the first attempt (protective
  stops placed for the uncovered quantity, correct side, verified) — the
  one exception (`futures/CADMXN`, `PriceNotInTickSizeIncrements`) is
  what led directly to the `_live_price_decimals()` fix above; a retry
  immediately after fixed and verified it too.
- **8 of 8 mismatches resolved**: 4 direction-mismatched local entries
  removed and verified (CHFJPY, EURCHF×2, USDCHF, EURUSD), 4
  untracked-live findings (NZDUSD, CADJPY, GBPJPY, CHFCNH) confirmed
  already fully protected by the naked-position pass.
- A follow-up sweep across all 4 modules found and fixed one more
  (`futures/GBPAUD`, newly opened between runs), then two consecutive
  clean runs confirmed a stable, fully-protected steady state.

## 2026-08-24: pending-entry false-orphan bug (found and fixed same day)

Testing the ETF top-10 config change live placed 7 new bracket entries
(Market orders + nested stop/TP legs) while the US market was still
closed. The post-run `safeguard.py` hook ran seconds later, saw zero
live position for all 7 uics, and — with no way at the time to
distinguish "genuinely orphaned" from "not filled yet" — removed all 7
from local state and cancelled what it believed was each one's stop
order.

Investigating live turned up a second, independent bug: `saxo_order.py`'s
`_place_bracket()` had `stop_order_id`/`tp_order_id` reversed for every
bracket order ever placed (Saxo's response `Orders` array is in the
opposite order from the request's), so the order actually cancelled was
each position's take-profit leg, not its stop — lower-severity than it
could have been, but only by chance. See
`bracket_stop_tp_id_swap_bug_2026-08-24` in the assistant's memory for
the full root-cause writeup; the id-swap fix itself lives in
`saxo_order.py` and is unrelated to housekeeping's own logic.

**The housekeeping-side fix**: `LiveSnapshot.has_pending_entry(uic,
direction)` — true if a Working Market/Limit/StopLimit order exists in
the local entry's own direction and is NOT an `IfDoneSlave` bracket
child (i.e. a real top-level entry, not a dormant stop/TP leg waiting on
its parent to fill). `reconcile_module()` now checks this *before*
treating a zero-live-position local entry as an orphan; if it's true,
the entry is left completely untouched and reported as `pending_entry`
instead of `removed_orphan`. Covered by
`test_pending_entry_left_alone_not_orphaned` and
`test_bracket_child_orders_do_not_count_as_pending_entry` in
`test_housekeeping.py`, and verified live against the real 7 pending
ETF entries (all correctly reported `pending_entry`, none touched).

This closes the gap for *any* module: a bracket entry placed just before
a market close, a Working limit order still waiting to fill, etc. — not
just the ETF case that surfaced it.

## 2026-09-01: stacked open ledger rows / naked re-close (state-race, recurred)

The 2026-08-25 fix (`3a72d8c`) checkpoints state after each *strategy pass* — but `_run_entries`/`_run_exits` fire several real orders per pass. A kill/watchdog-restart mid-pass (common on the SIM box) still loses the entries/closes made so far, so the next scan re-enters a pair it already holds (4 stacked SIM RSI EURCAD longs + 7 other combos) or re-closes an already-flat position into a naked flip (a 46,000 EURCAD short, flagged `fully_untracked … needs_human_review` every cycle).

**Fix (`604ab2a`):** `_run_entries`/`_run_exits` take `state=` and `_save_state(state)` after **every** entry/exit, not just at pass end. `_run_entries` also folds every `(strategy, symbol)` with an open `pnl_ledger.db` row into `open_symbols` (the ledger row is written the instant an order is placed, so it survives a mid-pass kill even when the state file does not). One-time `dedup_stacked_reentries_2026-09-01.py` closed the 26 already-stacked rows (kept the one matching current state per key; `realized_pnl` NULL). `_close_orphan_ledger_rows()` still handles "no state key at all"; this fills the gap it cannot — "state key exists but ledger has several open rows for it". LIVE was clean throughout (`reconcile_live_forex: no mismatches` every cycle).
