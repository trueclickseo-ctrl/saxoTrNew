# Forex London / NY Session-Breakout Strategy

**File**: [`forex/strategy_london_breakout.py`](../forex/strategy_london_breakout.py)
**Runner key**: `"london_breakout"`
**Type**: intraday **day-trading** — session-range breakout, no overnight holds
**Where it runs**: **SIM only**, and **only via the dedicated LBO scheduled tasks** — it is stripped from the generic `--strategy all` daily/London scans (`run_lbo_london.bat` / `run_lbo_ny.bat` / `run_lbo_close.bat`).
**Data**: H1 bars only (`NEEDS_H1_DATA = True`).
**Capital**: a **separate day-trading book** — `config.forex.lbo_capital_eur` (≈15,000 SEK / 1,390 EUR), **not** the swing account.
**A/B sibling**: [`london_breakout_v2`](forex_london_breakout_v2_strategy.md) — runs on the same schedule; this one is untouched.

> Not to be changed. Described exactly as it runs today.

---

## Concept

Liquidity compresses during Asia / London-morning, then releases directionally
when institutional flow arrives at the session open. Trade the **break of that
compression range** on an H1 close, stop on the far side of the range, target
2× the range.

| Session | Reference range (UTC) | Entry window (UTC) |
|---|---|---|
| London open | Asian range 00:00–06:59 | 07:00–10:00 |
| NY open | London-morning range 09:00–12:59 | 13:00–15:00 |
| Force close | — | **20:00 UTC** — all LBO positions closed before Asia |

---

## Parameters

| Param | Value | Constant |
|---|---|---|
| Min / max range | 10 / 120 pips | `MIN_RANGE_PIPS` / `MAX_RANGE_PIPS` |
| ATR confirm | require H1 ATR > ~5 pips | `ATR_CONFIRM` |
| Risk per trade | **1.5% of the LBO book** | `RISK_PCT = 0.015` |
| Take-profit | **2.0 × range size** | `TP_RATIO` |
| Max units / min units | 50,000 / 1,000 | `MAX_UNITS` / `MIN_UNITS` |
| Session close | 20:00 UTC | `SESSION_CLOSE` |
| Pairs | 28 (majors + liquid crosses; excludes 6 illiquid Scandi/exotic) | `PAIRS` |
| Runner slots | `SLOTS_PER_STRATEGY["london_breakout"] = 28` (one per pair) | |

Worst-case concurrent exposure: 28 × 1.5% = **42% of the LBO book** (much of it correlated FX-cross exposure — this is exactly what `london_breakout_v2` cuts).

---

## Entry

| | Buy | Sell |
|---|---|---|
| Trigger | H1 **close** > `range_high` | H1 close < `range_low` |
| Range gate | `MIN_RANGE_PIPS ≤ range ≤ MAX_RANGE_PIPS` | same |

- Enters at the latest H1 close.
- `stop_price` = the opposite range boundary (hard stop).
- `tp` = entry ± `2.0 × range`.
- Sizing is **pre-computed in the signal** (`units`), off the LBO book equity
  converted to each pair's quote currency. A signal whose correct size is
  below `MIN_UNITS` is **SKIPPED**, not floored up.

## Exit

| Condition | |
|---|---|
| Take-profit | 2.0 × range hit |
| Stop | opposite range boundary hit |
| Time stop | position closed by 20:00 UTC (`run_lbo_close.bat`) |

## Scheduled tasks

| Task | Trigger | Command |
|---|---|---|
| ATOS LBO London Open | 07:00 UTC / 12:00 PKT | `--strategy london_breakout,london_breakout_v2 --live` |
| ATOS LBO NY Open | 13:00 UTC / 18:00 PKT | same |
| ATOS LBO Force Close | 20:00 UTC / 01:00 PKT | `--strategy london_breakout,london_breakout_v2 --exits-only --live` |

(Mon–Fri only. `--live` = real Saxo **SIM** orders — the LBO tasks run against SIM.)

## History

The zero-signal bug (2026-08-22 HourUTC/index issue) was fixed and confirmed
live. Universe widened 7 → 28 pairs (2026-08-20). See `memory/forex_london_breakout.md`.

## Inspect

`python forex/runner.py --scan` → `[LBO]` panel (range / hi / lo / breakout /
tradeable per pair).
