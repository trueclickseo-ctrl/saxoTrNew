# Forex Bollinger Band Mean-Reversion Strategy

**File**: [`forex/strategy_bb.py`](../forex/strategy_bb.py)
**Runner key**: `"bb"`
**Type**: mean-reversion (fade the excursion)
**Where it runs**: **SIM** *and* the real-money **SEK LIVE account** (`LIVE_ALLOWED_STRATEGIES = {"bb"}`) — though the SEK LIVE task is currently **Disabled** at the scheduler.
**Momentum pre-filter**: **no** (in `_NO_MOMENTUM_FILTER`) — reversion strategy, scans the full universe.

> Not to be changed. Described exactly as it runs today.

---

## Concept

Price closing outside a Bollinger(20, 2) band is a ~2σ excursion — statistically
it snaps back to the 20-day mean within a few sessions. RSI(14) at an extreme is
required as confirmation, which filters out trending "band walks" where the move
just keeps going.

Uses **population** std (`ddof=0`) for the bands.

---

## Parameters

| Param | Value | Constant |
|---|---|---|
| BB period / std | 20 / 2.0 | `BB_PERIOD` / `BB_STD` |
| RSI period | 14 | `RSI_PERIOD` |
| RSI overbought → short | **65** | `RSI_OB` |
| RSI oversold → long | **35** | `RSI_OS` |
| ATR period / stop multiple | 14 / **2.0×** | `ATR_PERIOD` / `ATR_STOP_MULT` |
| Risk per trade | **0.25%** SIM (`RISK_PCT = 0.0025`); **0.75%** on SEK LIVE via `LIVE_RISK_PCT_OVERRIDE` | |
| Time stop | **8 calendar days** (shortest of the swing strategies) | `TIME_STOP_DAYS` |
| Min bars | `20 + 14 + 5` = 39 | `MIN_BARS` |
| `MAX_POSITIONS` | 4 | declared; runner slot cap is `_SWING_SLOTS` = 184 |

---

## Entry

| | Long | Short |
|---|---|---|
| Excursion | `close < BB_lower` | `close > BB_upper` |
| RSI confirm | `RSI(14) < 35` | `RSI(14) > 65` |

- `score` = distance past the band in ATR units (most extreme first).
- `stop_price` = `close ∓ 2.0 × ATR(14)`.
- `bb_target` = the 20-day SMA (BB mid) — the reversion target.

## Exit — `should_exit()`, first hit

| # | Condition | Reason |
|---|---|---|
| A | `days_held ≥ 8` | `time_stop (Nd)` |
| B | Long: `close ≥ BB_mid` / Short: `close ≤ BB_mid` (reverted to mean) | `bb_mid_reversion (…)` |
| C | Long: `low ≤ stop` / Short: `high ≥ stop` | `hard_stop (px)` |

## Trailing stop

`trailing_stop_update()` — 2.0×ATR ratchet, called generically by the runner.

## Sizing

`size_position(equity, atr, min_units, risk_pct=None, block_below_min=False)` —
`risk_pct` override supported (used for the 0.75% LIVE value), `block_below_min`
supported (LIVE returns 0 instead of flooring up).

## SEK LIVE gates (in `forex/runner.py`, not this file)

Same LIVE gate stack as RSI: currency-exposure cap 5, 3× cost gate, 6% heat cap,
50% margin cap, weekend market-hours gate, `block_below_min`. **No lot ladder**
— the 10k–100k ladder is RSI-only. The SEK LIVE task is Disabled right now, so
`bb` trades on SIM only in practice.

## Inspect

`python forex/runner.py --scan` → `[BB]` panel.
