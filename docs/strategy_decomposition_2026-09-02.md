# Strategy edge decomposition — 2026-09-02

A sweep of every forex swing strategy (plus the US Blend momentum sleeve) to
answer one question the user asked repeatedly: **is there a filterable subset
of "top" trades inside each strategy, and if so what defines it?**

## Method

Replay each strategy's *real* `generate_signals` + exit stack on Yahoo daily
bars — 49 CORE pairs, ~11–13 years — computing per-trade **R** (R = the
initial ATR stop distance). Then bucket trades by entry-context features and
keep only buckets that are:

1. **positive in BOTH halves** (pre-2020/21 and after) — not a single-regime
   artifact, and
2. **bootstrap 95% CI (5000×) excludes zero**.

Yahoo daily is backtest-only (Saxo-Only-Live-Prices rule). Absolute P&L in
the SIM `pnl_ledger` is mixed-currency and unusable for cross-pair
comparison — R-normalised or win% only.

## Results

| Strategy | Base edge (R/trade) | Stable? | Verdict |
|---|---|---|---|
| **`ema`** | +0.036 | ✗ (CI spans zero) | ✅ **filterable** → shipped `ema_trend` |
| **`bb`** | +0.048 | ✓ | ✅ **filterable** → shipped `bb_quality` |
| **`rsi`** | +0.021 | ✗ (≈0 pre-2020) | ✅ **filterable** → shipped `rsi_trend` (2026-09-02, PR #7) |
| **`zscore`** | +0.002 | ✗ (CI spans zero) | ✅ **filterable** → shipped `zscore_quality` (`|+DI−−DI| ≤ 14` = +0.132 R stable, same filter as `bb`); 19/49 pairs stable-positive |
| `gap` | ≈0 | — | ⚠️ partial — `weekly` gaps survive (+0.10 R); session legs (`newyork`/`london`) net-losing |
| `rsi_confirm` | −0.11 control / −0.25 delayed | — | ❌ built + backtested + retired same day — delay enters after the reversion |
| **US Blend momentum** | +1.16%/pick, 57% win | ✓ | ✅ works — **don't concentrate**; see below |
| `donchian` (+`donchian_quality`) | negative | — | ✗ retire — no rescuing filter |
| `pullback` | negative | — | ✗ retire |
| `ml` | −0.046 (CI negative) | — | ✗ retire |
| `supertrend` | negative 10/12 years | — | ✗ retire |

### `ema` → `ema_trend`

`ema`'s edge concentrates entirely in crossovers that are still **fresh** and
show a real **+DI/−DI gap**:

| filter | n | avg R | 1st / 2nd half | PF | max DD |
|---|---|---|---|---|---|
| base | 2,259 | +0.036 | +0.064 / +0.010 | 1.09 | −47 R |
| fresh crossover (age ≤ 3 bars) | 778 | +0.103 | +0.163 / +0.056 | 1.28 | −15 R |
| DI spread ≥ median | 1,130 | +0.110 | +0.120 / +0.099 | 1.30 | −32 R |
| **both** | **242** | **+0.298** | **+0.356 / +0.250** | **1.97** | **−6 R** |

Shipped: [`forex/strategy_ema_trend.py`](../forex/strategy_ema_trend.py) —
`ema` kept only if `crossover_age ≤ 3` **and** `|+DI−−DI| ≥ 15`.
[Doc.](forex_ema_trend_strategy.md)

### `bb` → `bb_quality`

`bb` is already stable-positive but gives most of it back on signals fired
into a trend:

| filter | n | avg R | 1st / 2nd half | PF |
|---|---|---|---|---|
| base | 4,741 | +0.048 | +0.038 / +0.057 | 1.17 |
| `|+DI−−DI|` ≤ p25 (non-directional) | 1,186 | **+0.219** | **+0.247 / +0.200** | **2.07** |
| `|+DI−−DI|` ≤ median | 2,371 | +0.148 | +0.154 / +0.144 | 1.66 |

Shipped: [`forex/strategy_bb_quality.py`](../forex/strategy_bb_quality.py) —
`bb` kept only if `|+DI−−DI| ≤ 14`. [Doc.](forex_bb_quality_strategy.md)

### `gap` — session legs are dead

R-normalised (232 closed SIM trades):

| gap_type | n | avg R | win% |
|---|---|---|---|
| `weekly` (Monday open) | 149 | **+0.095** | 43% |
| `newyork` (session) | 78 | **−0.099** | 42% |
| `london` (session) | 5 | −0.63 | 0% |

Weekday / direction / "with-trend" cuts wash out under R-normalisation.
Recommendation: disable the session-gap legs, run `gap` weekly-only on SIM.
Not yet actioned (needs user sign-off — it edits live entry behaviour).

### US Blend momentum — don't concentrate

407-name universe, 208 rebalances, 2017–2026. Per offense pick: **+1.16%**
over each 14-day hold, **57% win rate**. But:

- **No alpha gradient by rank.** Ranks 3, 6, 7, 10, 11 all stable-positive;
  ranks **1–2 are the weakest**. Cutting to "top 5" drops the profitable
  rank-6 slot for nothing.
- **Portfolio Sharpe rises with breadth**: N=3 → 0.28, N=6 (live) → 0.44,
  N=8 → 0.58, and drawdown shrinks. Concentration is the wrong direction.
- What *does* separate picks (all stable): `momentum/vol` score in the
  **0.75–2.05** band (>2.36 is dead — and the strategy sorts *descending* by
  this score, so it preferentially grabs the weak high-score names);
  6-mo momentum **77–133%** (>133% parabolic fades); regime
  **TRENDING_BULLISH / RANGING** (HIGH_VOLATILITY −0.78%).

No code change — these are core-selection observations for the user's review.

### Rebalance mechanics (answering a user question)

A still-trending US Blend name is **not force-sold at 14 days**.
[`plan_rebalance`](../atos/us_momentum.py) only sells a ticker when it drops
out of the recomputed target set (falls below EMA200, momentum < 5%, or
outranked). A persistent winner is held for as many consecutive 14-day
cycles as it keeps ranking — only a >10% weight drift triggers a small
top-up/trim.

## Build queue status

1. ✅ `ema_trend` — shipped (this branch)
2. ✅ `bb_quality` — shipped (this branch)
3. ❌ `rsi_confirm` — built, **backtested, RETIRED same day**. The
   confirmation delay systematically enters *after* the mean reversion:
   control (enter on signal) −0.106 R/trade / win 56%; delayed −0.24 to
   −0.27 R/trade / win 42% at every K (1/2/3 observation bars), both halves,
   and in TRENDING_BULLISH-only too. Module kept unwired as the negative
   result. [Doc.](forex_rsi_confirm_strategy.md)
4. ✅ `zscore_quality` — **shipped (this branch).** `zscore` base is a
   coin-flip (+0.002 R, CI spans zero) but `|+DI−−DI| ≤ 14` (non-directional
   market) → **+0.132 R, stable both halves** (+0.120 / +0.144) — the *exact
   same* filter that works for `bb`. Also: 19/49 pairs are stable-positive
   both halves; trading only those → +0.084 R, PF 1.43, CI [+0.049, +0.120].
   The NZD pairs + GBP-commodity crosses lose in both halves (thin,
   trend-prone — bad for mean reversion). Per-pair whitelist NOT baked into
   the module — a SIM-universe config lever to consider. [Doc.](forex_zscore_quality_strategy.md)

## Retired (actioned 2026-09-02)

`donchian`, `donchian_quality`, `pullback`, `ml`, `supertrend` — net-negative
over 12 years with no filter that survives the two-halves + bootstrap test.
Now in `RETIRED_STRATEGIES` (`forex/runner.py`): still registered so open
positions keep full exit management, but excluded from the default entry
rotation — they open nothing new. `--strategy <name>` still runs one (with a
warning) for research. Their `advanced_*` A/B twins (`advanced_pullback_master`,
`advanced_ml`) are separate designs and stay active for now — retire them too
if their own forward data confirms.

Still on the table: the `gap` session-leg disable (`newyork`/`london` legs
are net-losing; `weekly` survives) — that edits `gap`'s live entry behaviour,
so it needs its own explicit go-ahead.

Scratch backtests:
`~/.claude/.../scratchpad/{rsi_*,ema_decompose,st_bb_decompose,donchian_decompose,pullback_decompose,ml_decompose,gap_ledger_analysis,blend_momentum_decompose,composite_verify,zscore_decompose,rsi_confirm_backtest}.py`.
