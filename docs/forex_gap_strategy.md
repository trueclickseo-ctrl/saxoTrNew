# Forex Gap-Fill Strategy (weekly + session)

**File**: [`forex/strategy_gap.py`](../forex/strategy_gap.py)
**Runner key**: `"gap"`
**Type**: mean-reversion — fade a session-open gap back to its reference price
**Where it runs**: **SIM only**. `"gap"` is never in either LIVE allowlist.
**Momentum pre-filter**: **no** (gap needs every pair — the gap% filter selects).
**`NEEDS_LIVE_PRICES = True`** — the runner fetches the current open price via Saxo infoprices before calling it.
**A/B sibling**: [`gap_weekend`](forex_gapfill_weekend_strategy.md) — a rebuilt variant; this one is left untouched.

> Not to be changed. Described exactly as it runs today.

---

## Concept

FX gaps between one session's close and the next session's open tend to fill.
Gap **up** → short (fade down to the reference). Gap **down** → long (fade up).
The strategy runs only inside defined session windows (`_detect_gap_session()`
in the runner: weekly / london / newyork / tokyo).

The take-profit is placed as a **real resting Limit order at the reference
price** at entry (`saxo_order.place_with_stop`); `should_exit()`'s target check
is a redundant safety net.

---

## Sessions

| Session | Window (runner) | Reference | Gap band | Stop | Time stop | Risk |
|---|---|---|---|---|---|---|
| **weekly** | Sun 22:00 → Mon 06:00 UTC | Friday daily close | 0.10%–2.00% (`MIN/MAX_GAP_PCT`) | 1.5 × gap | **7 days** (`TIME_STOP_DAYS`) | 0.25% |
| **london** | 07:00–08:30 UTC | H1 bar closing 06:00 UTC | 0.05%–0.40% | 2.0 × gap | **8 hours** | 0.25% |
| **newyork** | 12:00–13:30 UTC | H1 bar closing 11:00 UTC | 0.05%–0.40% | 2.0 × gap | **6 hours** | 0.25% |
| **tokyo** | Mon-Fri 00:00–01:30 UTC (skipped Monday — covered by weekly) | H1 bar closing 23:00 UTC | 0.04%–0.30% | 2.0 × gap | **7 hours** | 0.25% |

Session gaps carry `risk_pct_override` in the signal so the runner sizes them at
their session's own `risk_pct` (all currently 0.25%). Session gaps also carry a
`gap_type` (`"london"` / `"newyork"` / `"tokyo"`), weekly carries `"weekly"`.

Reference-bar lookup: `_find_ref_bar_close()` uses the `HourUTC` column the
runner populates; falls back to the last completed bar.

---

## Entry (all sessions)

`sunday_open` (or `session_open`) vs `reference_close`:

| Gap | Direction | Stop |
|---|---|---|
| open **above** reference | **Sell** | `open + mult × gap_size` |
| open **below** reference | **Buy** | `open − mult × gap_size` |

- `score` = `gap_pct` (biggest gap first). `atr` field carries `gap_size`.
- Skipped if `sym in open_symbols` or `sym in exhausted_symbols` (already traded a gap this week — the runner's cooldown file).

## Exit — `should_exit()`, first hit

| # | Condition | Reason |
|---|---|---|
| A | session gap: hours held ≥ `time_stop_hours`; weekly: `days_held ≥ 7` | `time_stop (…)` |
| B | **target hit — checked on `cur_close`, NOT the day's high/low** | `gap_filled (target=…)` |
| C | Long: `low ≤ stop` / Short: `high ≥ stop` | `hard_stop (px)` |

> The `cur_close` check on B is a real bug fix (2026-08-24): using the day's
> cumulative high/low made the check "sticky" — one wick through the target and
> every later run that day fired a market close at whatever price then existed.
> 18/18 weekly exits that day were mislabelled `gap_filled` while 16 were real
> losses (−345 EUR). Now it only fires while price is *still* at/beyond target.

## Sizing

`size_position(equity, gap_size, min_units, risk_pct=RISK_PCT, block_below_min=False)`
— stop distance = `1.5 × gap_size`.

## Inspect

`python forex/runner.py --scan` → `[GAP]` panel (`*** GAP UP → SHORT ***` etc.,
needs live prices).
