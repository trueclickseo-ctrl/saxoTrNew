# Daily Summary Email

End-of-day P&L digest across all 4 modules (forex, futures, etf, stocks),
built 2026-08-24. One email, sent once daily, covering everything that
happened that trading day.

Code: [`daily_summary.py`](../daily_summary.py) (project root).
Tests: [`test_daily_summary.py`](../test_daily_summary.py) — 7/7 passing.
Scheduled: `ATOS Daily Summary` Windows task, daily at 23:30 PKT, via
[`run_daily_summary.bat`](../run_daily_summary.bat) → `python daily_summary.py`.

## What's in it

Per module (only shown if something happened — trades closed or
positions currently open):

- Trades closed today, win rate, day P&L, positions currently open
- Per-strategy breakdown: strategy name, trades, **symbols traded**,
  win rate, **profit factor**, P&L, open count

Account-level, once at the top:

- Total trades today, day P&L, account equity, margin utilization
- **Naked position count** (live, read-only check via
  `housekeeping.scan_naked_positions()`) — a quick pre-live health signal

Deliberately **not** included: a "mismatch count" from
`housekeeping.reconcile_all()`. That function mutates live state
(cancels/replaces orders) as it corrects what it finds — the right
behavior for its own scheduled/post-run role, but not something a
reporting script should trigger as a side effect of building an email.
See the Housekeeping/Safeguard emails (every 30 min) for that signal
instead.

## Data source

`pnl_tracker.get_strategy_summary_since(module, since)` — a new
date-scoped sibling of the existing `get_strategy_summary()` (used by
the weekly report), same SQL shape (correctly excludes open positions
from win-rate/P&L math, computes open-count from its own query rather
than diluting the closed-trade rowset), plus a `symbols` list per
strategy for the window.

## Why win rate / profit factor might look worse than expected right now

Three real bugs were found and fixed the same day this report was built,
all specific to the `gap` strategy's session-based sub-strategies
(London/NY/Tokyo — as opposed to the weekly variant, which was
unaffected):

1. **Session gaps never actually traded before 2026-08-24.** A
   consensus-filter bug (see `docs/housekeeping.md`'s sibling notes, or
   `forex/signal_filter.py`'s `evaluate()`) silently rejected every
   London/NY/Tokyo gap signal since that filter was introduced — fixed
   the same day, confirmed live (5 real signals executed within the hour).
2. **The breakeven-stop logic used a stale, "sticky" high/low check**
   (`forex/runner.py`'s `_apply_breakeven_stop()`) that moved a gap
   position's stop to breakeven on a brief intrabar wick even if price
   immediately reverted — then a tiny bit of ordinary noise a few
   minutes later stopped it out at (near) breakeven for a small loss.
   Confirmed live: **all 19** of that day's `hard_stop` gap exits had a
   logged stop price exactly equal to entry price, for a combined
   **−€1,530.64**.
3. **Exits closed on a stale `should_exit()` decision.** The decision
   (`gap_filled`/`hard_stop`) is based on an H1/D1 bar close, possibly
   minutes old by the time dozens of positions have been checked in one
   sweep — but the actual closing market order executes at a separately
   fetched, genuinely fresh live price. If price moved between those two
   lookups, a position closed on a label the fresh price no longer
   supported, at a worse price than the label implied. Confirmed live:
   **42** `gap_filled` exits that day, only **3** real wins, net
   **−€2,179.19** — most never actually reached their target at the live
   execution price. Fixed by re-validating against the same live price
   used for execution and skipping (not force-closing) when it disagrees
   — the resting Stop+Limit bracket order already on Saxo remains the
   real protection either way, so nothing is left unprotected by skipping.

None of this was real strategy underperformance — **all three** are now
fixed. Combined day-total impact from bugs #2 and #3: **−€3,710.83**
across 61 trades (61 of gap's 61 closes that day; only 3 were real wins).
Because session gaps were structurally blocked until bug #1's fix landed
the same day, bugs #2 and #3's real-world impact was invisible until
then — they'd been latent in code paths that had (almost) never actually
run in production before. All three fixes are in place; trade counts
closed *after* they landed reflect the corrected behavior.
