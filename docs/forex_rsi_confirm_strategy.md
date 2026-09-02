# `rsi_confirm` — RSI(2) with a confirmation delay + conviction single-slot (SIM-only)

**Module:** [`forex/strategy_rsi_confirm.py`](../forex/strategy_rsi_confirm.py)
**Added:** 2026-09-02 · **Runs on:** SIM only (never in `LIVE_ALLOWED_STRATEGIES`
/ `LIVE_EUR_ALLOWED_STRATEGIES`, not in `PROFIT_LADDER_STRATEGIES`)
**Status:** hypothesis + code shipped for a SIM forward-test. **Backtest is
the next step** (built first at the user's request).

## The idea (user's, 2026-09-02)

> "When an RSI signal triggers we should NOT buy immediately. Keep it in a
> 'LIVE CANDIDATE' bucket and observe ~8h–1 day: usually the price first goes
> against us, then starts climbing. Only enter once that turn is confirmed.
> And for a high-conviction setup, put ONE concentrated position on at a time
> (~600–800 EUR) and sell after a small profitable move."

## Lifecycle

| Stage | What happens | Where |
|---|---|---|
| **1. Queue** | Every fresh `strategy_rsi` signal (not already queued/open) → a *candidate* `{direction, signal_px, signal_ts, signal_rsi, regime, best_adverse_px}`. **Nothing is traded.** | `update_candidates()` |
| **2. Observe** | Each cycle refreshes `best_adverse_px` (worst excursion against the signal so far). A candidate older than `OBSERVE_MAX_HOURS` (30h) with no confirmation is dropped. | `update_candidates()` |
| **3. Confirm & enter** | After `OBSERVE_MIN_HOURS` (6h): enter iff the turn is confirmed. Entry at the **current** price (not the stale signal price), stop `ATR_STOP_MULT × ATR`, tight TP `FAST_TP_ATR × ATR`. | `generate_signals()` |
| **4. Exit** | `fast_tp` (+0.6 ATR) → short time stop (4d) → then rsi's own hard-stop / RSI-recovery exit. | `should_exit()` |

**Confirmation rule (Buy; Sell mirrors):**
- `(dipped ≥ MIN_DIP_ATR below signal) AND (recovered ≥ MIN_RECOVERY_ATR off that low, back to/above it)`
- **OR** `immediate follow-through ≥ MIN_FOLLOW_ATR in our favour` **AND** RSI(2) hasn't already blown past 65 (that path only — a post-bounce RSI spike is *expected* and fine).

**Conviction slot:** `SLOTS_PER_STRATEGY["rsi_confirm"] = 1` — one position at a
time. Size targets `CONVICTION_NOTIONAL_QUOTE` (750) of quote-currency
notional, which on SIM resolves to the **1,000-unit minimum lot** for
virtually every pair (~600–1,000 EUR base notional) — the smallest
concentrated position, by design.

## Knobs (all starting points — the backtest tunes them)

| | value | |
|---|---|---|
| `OBSERVE_MIN_HOURS` / `OBSERVE_MAX_HOURS` | 6 / 30 | observation window |
| `MIN_DIP_ATR` | 0.15 | must actually have gone against us |
| `MIN_RECOVERY_ATR` | 0.35 | …then climbed back off the extreme |
| `MIN_FOLLOW_ATR` | 0.25 | OR: never dipped, ran our way this far |
| `FAST_TP_ATR` | 0.60 | tight take-profit |
| `CONVICTION_TIME_STOP_DAYS` | 4 | short leash (rsi's own is 12) |
| `CONVICTION_NOTIONAL_QUOTE` | 750 | → 1,000-unit min lot on SIM |

## Architecture

The strategy module is **pure**. The candidate bucket is the **runner's**
state — `data/rsi_confirm_candidates.json`, loaded/refreshed/saved each cycle
by `_run_entries` (`_load_rsi_confirm_candidates` / `_save_…`), exactly like
the gap-cooldown and lbo-v2-session files. `strategy_rsi.py` is untouched.

## Next

1. **Backtest** — replay `strategy_rsi` signals on daily bars, apply the
   observation window + confirmation rule + fast-TP, compare expectancy /
   PF / drawdown / trade count against plain `rsi` and against `rsi_trend`.
   Does the 1-day delay + reversal confirmation beat entering on the signal?
   Does the fast +0.6 ATR exit beat the full RSI-recovery exit?
2. If it validates → keep on SIM forward-test; tune the knobs from the data.
   No LIVE consideration until a walk-forward clears it.

Scratch: (backtest script to be added next.)
