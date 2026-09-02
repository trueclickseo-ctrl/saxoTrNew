# `rsi_confirm` — RSI(2) with a confirmation delay + conviction single-slot

**Module:** [`forex/strategy_rsi_confirm.py`](../forex/strategy_rsi_confirm.py)
**Added:** 2026-09-02 · **Status: RETIRED 2026-09-02 — backtest-falsified,
never scanned.** Unwired from `forex/runner.py` (not in `STRATEGIES`); kept
only as the documented negative result.

## Backtest verdict (why it was retired)

`scratchpad/rsi_confirm_backtest.py` — 12,702 real `strategy_rsi` signals, 49
CORE pairs, 12y Yahoo daily, 0.12 R/trade cost. A 2×2 of {enter now vs. delay
+ confirm} × {rsi's own exit vs. fast +0.6 ATR exit}:

| arm | n | R/trade | win% | PF | 1st/2nd half |
|---|---|---|---|---|---|
| **A** immediate + rsi-exit (control) | 12,702 | **−0.106** | 56% | 0.65 | −0.13 / −0.09 |
| **B** immediate + fast-TP | 12,702 | −0.130 | 55% | 0.53 | −0.15 / −0.11 |
| **C** delayed + rsi-exit | 10,423 | −0.242 | 42% | 0.32 | −0.26 / −0.22 |
| **D** delayed + fast-TP (as shipped) | 10,423 | **−0.252** | 45% | 0.32 | −0.28 / −0.23 |

TRENDING_BULLISH only (RSI's stable-edge zone): A −0.041 → D −0.234 — the
delay wrecks it there too.

**Why:** RSI(2) is a *mean-reversion* signal; the edge is buying the instant
of maximum oversold. Waiting 6–30h for a "dip-then-recover" confirmation
means the reversion has already happened — you buy back the bounce at a worse
price, win rate collapses 56% → 42%. The 82% confirmation rate means you
still enter almost always, just later and worse; the 18% that never confirm
include the ones that went straight up (the best trades). The fast +0.6 ATR
exit is also a mild negative on its own — it clips winners RSI's recovery
exit would have banked more of. **No knob fixes "don't wait to fade an
extreme."**

Governance working as designed: hypothesis → code → backtest → **failed
validation** → not shipped to forward-test.

---

## Original design (kept for the record)

**Was:** SIM only (never in `LIVE_ALLOWED_STRATEGIES` /
`LIVE_EUR_ALLOWED_STRATEGIES`, not in `PROFIT_LADDER_STRATEGIES`).

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

Scratch: `~/.claude/.../scratchpad/rsi_confirm_backtest.py`.
