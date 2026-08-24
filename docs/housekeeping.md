# Housekeeping Agent

Cross-module state reconciliation for Forex, Futures, ETF and Shares
(ATOS). Built 2026-08-24 after a manual audit of 4 suspected mismatched
positions turned into a full account sweep that found **24 orphaned
local entries, 6 duplicate stop orders, 4 overstated positions, 4
untracked live positions, and 21 completely unprotected live
positions** — the manual fix only caught the tip of it. This tool exists
so that never has to be found by hand again.

Code: [`housekeeping.py`](../housekeeping.py) (project root).
Tests: [`test_housekeeping.py`](../test_housekeeping.py) — 16/16 passing.

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
| Local tracks a position, live shows **zero** exposure at all | Remove the local entry, cancel its now-orphaned stop order |
| Local's combined quantity **exceeds** live exposure (Saxo netted part of it against another strategy's opposite trade) | Scale every entry down **proportionally**, so the total protective-stop coverage can never exceed what's actually open. Flagged as an `estimate` — Saxo's own netting has erased which literal lot belongs to which strategy, so the split preserves each entry's *relative* size and its own risk price, not a verified fact |
| Local direction is the **opposite** of live direction | Reported only — **never auto-corrected**. Too ambiguous to guess safely; needs a human |
| Live exposure **exceeds** local tracking (untracked extra) | Reported only — never fabricates a new local entry to explain it |
| Two+ Working stop orders on the same instrument/side/price (breakeven move or race-condition retry that left the old one uncancelled) | Cancel all but the newest |

Sends **one email** summarizing every finding from the run — only if at
least one finding exists. A clean account produces zero emails.

### 2. `scan_naked_positions()` — read-only safety scan, live-Saxo-only

Independent of local state entirely: for every live position on the
account, does a working stop-loss order actually cover it?

| `protection` value | Meaning |
|---|---|
| `none` | No stop or take-profit order at all |
| `tp_only` | A take-profit limit exists but no stop-loss |
| `partial` | A stop exists but covers less than the full quantity |
| *(not reported)* | Fully covered |

**Deliberately never auto-closes or auto-protects anything.** Unlike a
quantity mismatch (where "make the numbers agree" has one obviously
correct direction), a naked position could be a genuine bug, a strategy
that intentionally manages risk without a broker-side stop, or a position
caught mid-way through its own entry sequence. Only a human — or that
strategy's own code — should decide whether "no stop" means "bug" or "by
design." This function's job is only to make sure that decision actually
gets made, via the report/email, not to guess.

Sends **one email** listing every naked position found — only if at
least one exists.

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

Each call site scopes `reconcile_all()` to just its own module (fast,
targeted) and always runs the full `scan_naked_positions()` sweep
afterward (inherently account-wide — a naked position from any module
matters regardless of which module just ran). Failures are caught and
logged, never allowed to fail the trading run itself.

## Periodic safety net

`ATOS Housekeeping` — a Windows Scheduled Task created 2026-08-24, runs
**every 30 minutes**, any day, independent of any module's own schedule.
Exists because a module's post-run reconciliation only fires when *that*
module runs live — this catches drift caused by a *different* module's
trade even on a day this module stays flat.

- Action: `run_housekeeping.bat` → `python housekeeping.py` (both
  `reconcile_all()` and `scan_naked_positions()`, all 4 modules)
- Log: `data/housekeeping_scheduler.log`
- Read-only unless it finds something to fix; sends email only on a
  mismatch/naked finding, same as the per-run wiring.

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

Or from Python:

```python
import housekeeping
findings = housekeeping.reconcile_all(["forex"])   # or None for all 4
naked    = housekeeping.scan_naked_positions()
```

## Email

Reuses `config/email.json` (same credentials as every other notifier in
this codebase — `forex/notifier.py`, `scheduler_watchdog.py`,
`intraday_monitor.py`). Two templates, both sent only when there's
something to report:

- **Reconciliation report** — table of every finding (module, symbol,
  kind, detail), with a note that `estimate`-flagged rows involve
  per-strategy attribution Saxo's netting has erased.
- **Naked position alert** — table of every unprotected live position
  (module, symbol, direction, quantity, protection level), with a note
  that this scan never auto-closes or auto-protects — it's a decision
  for a human.

## 2026-08-24 first real run

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
  local, and EURUSD at +1,284,000 live vs −15,000 local. These need
  manual investigation, not an automated guess.

The follow-up `scan_naked_positions()` sweep (all 4 modules) found **21
live positions with zero or partial protection**, including three
unprotected EURCHF shorts totaling −2,381,000 and two unprotected AUDCAD
futures shorts totaling −1,446,000 — see the session notes for the full
list and what was decided about each.
