# ETF Strategy Playbook

**Module**: `saxo_etf_strategy/`  
**Universe**: Up to 8,924 US-listed ETFs (cached 24h, sourced from Saxo API)  
**Strategies**: 5 (switch via `etf_config.py` `strategy_name`)  
**Capital**: 15% of account balance — separate from stocks (85%) and forex/futures  
**Max positions**: 5 open at once, 3% of account per position  
**Stop-loss**: 8% | **Take-profit**: 20%  
**Currency**: EUR (SIM account currency)  
**Scheduled**: 06:30 PKT daily via `saxo_etf_strategy/run_etf_daily.bat`  
**Status**: LIVE (`dry_run = False`) — flipped 2026-08-15, first real SIM orders placed 2026-08-17 (XLV, XLF, XLE)  
**Last updated**: 2026-08-20 — see [Audit Log](#audit-log-2026-08-20) at bottom for corrections made today  

---

## Capital Allocation

```
Account total:       100%
  ├─ US Equities:    85%  (atos_runner.py — Blend 50% + Reversion 50%)
  └─ ETF Module:     15%  (saxo_etf_strategy/ — independent process)
       ├─ Max 5 positions
       └─ 3% of account per position (= ~20% of ETF sleeve)
```

The ETF module imports nothing from the US equities code. They are completely isolated processes.

---

## Strategy 1 — Sector Rotation ✓ (default)

**File**: `saxo_etf_strategy/core/etf_strategy.py` → `SectorRotationStrategy`  
**Type**: Momentum-based sector allocation  

### Concept
Rank all 11 US sector SPDR ETFs by their 3-month (63-day) price return. Hold the top 3 ranked sectors. Rebalance weekly — sell any sector no longer in the top 3, buy whatever enters the top 3.

The 3-month lookback captures intermediate momentum (the "sweet spot" — short enough to be responsive, long enough to filter noise). Sector rotation historically generates 2–4% annual alpha over static allocation.

### Universe (11 sector ETFs)
| ETF | Sector |
|-----|--------|
| XLB | Materials |
| XLC | Communication Services |
| XLE | Energy |
| XLF | Financials |
| XLI | Industrials |
| XLK | Technology |
| XLP | Consumer Staples |
| XLRE | Real Estate |
| XLU | Utilities |
| XLV | Health Care |
| XLY | Consumer Discretionary |

### Entry/Exit
- **Buy**: ETF is in the top-3 by 3-month return AND not already in portfolio
- **Sell**: hits 8% stop OR hits 20% TP only — **rank-drop exit is NOT implemented** (see Known Limitations). A sector that falls out of the top 3 is currently held until it stops out or takes profit, not sold on the rank change.
- **Rebalance**: the bot runs once daily (Task Scheduler, not a weekly job) and buys whatever newly enters the top-3 into free slots. Without a rank-drop exit, this is closer to "buy-and-hold with daily top-up" than true weekly rebalancing.

### Parameters
| Param | Value |
|-------|-------|
| Lookback | 63 trading days (≈ 3 months) |
| Top N | 3 sectors |
| Position size | 3% of account |
| Stop-loss | 8% |
| Take-profit | 20% |
| Rebalance | Weekly |

---

## Strategy 2 — Risk-Off Switching

**File**: `saxo_etf_strategy/core/etf_strategy.py` → `RiskOffStrategy`  
**Type**: Regime-conditional allocation  

### Concept
A simple regime filter: hold equity ETFs in bull regimes, switch to defensive ETFs in bear regimes. Uses SPY's 200-day SMA as the regime indicator — clean, widely-followed, hard to game.

### Logic
| Regime | Condition | Holdings |
|--------|-----------|----------|
| **Bull** | SPY close > SMA(200) | SPY (50%) + QQQ (50%) |
| **Bear** | SPY close ≤ SMA(200) | TLT (50%) + GLD (50%) |

### Parameters
| Param | Value |
|-------|-------|
| Regime indicator | SPY vs SMA(200) |
| Bull holdings | SPY, QQQ |
| Bear holdings | TLT (Long Treasuries), GLD (Gold) |
| Position size | 3% of account per ETF |
| Stop-loss | 8% |
| Take-profit | 20% |

---

## Strategy 3 — Mean Reversion

**File**: `saxo_etf_strategy/core/etf_strategy.py` → `MeanReversionStrategy`  
**Type**: Oversold bounce on broad ETFs  

### Concept
Fades oversold readings on 5 broad market ETFs. RSI < 30 + price ≥ 5% below SMA(20) = statistical overextension in a liquid, diversified instrument — reversion probability is high.

Only fires when both conditions are met simultaneously. Tight 8% stop limits losses when the reversion doesn't materialize.

### Universe (5 broad ETFs)
| ETF | Description |
|-----|-------------|
| SPY | S&P 500 |
| QQQ | Nasdaq 100 |
| IWM | Russell 2000 |
| EFA | International Developed |
| EEM | Emerging Markets |

### Entry/Exit
- **Buy**: RSI(14) < 30 AND price ≥ 5% below SMA(20)
- **Sell**: Price recovers to SMA(20) OR -8% stop OR +20% TP

### Parameters
| Param | Value |
|-------|-------|
| RSI threshold | < 30 |
| Dip threshold | ≥ 5% below SMA(20) |
| RSI period | 14 |
| Stop-loss | 8% |
| Take-profit | 20% |

---

## Strategy 4 — Dual Moving Average

**File**: `saxo_etf_strategy/core/etf_strategy.py` → `DualMAStrategy`  
**Type**: Trend-following on curated ETF universe  

### Concept
20d MA crossing above 100d MA signals a confirmed intermediate uptrend on an ETF. Scans a curated list of 50 ETFs (thematic + sector + factor) to catch the start of sustained moves.

The curated universe avoids illiquid or inverse/leveraged ETFs. 20/100 MA separation is wide enough to filter noise but responsive enough to catch multi-month trends early.

### Universe (50 curated ETFs)
Includes: ARKG, XBI, IBB, SOXX, SMH, ARKK, ARKW, XLK, CIBR, BOTZ, ROBO, ICLN, TAN, FAN, BLOK, BETZ, HERO, ESPO, NERD, CLOU, BUG, HACK, UFO, MOON, METV, MSOS, YOLO, MJ, TOKE, POTX, JETS, AWAY, PEJ, KBWB, KRE, BKLN, HYG, EMB, LEMB, PCY, ANGL, SJNK, JNK, BNDX, VXUS, VEA, VWO, EFA, EEM, MCHI

### Entry/Exit
- **Buy**: 20d MA is currently above 100d MA AND not already in portfolio — this is a *state* check, not a crossover-event detector (no lookback window tracks when the cross actually happened, so it fires every day the condition holds, not just at the cross)
- **Sell**: -8% stop OR +20% TP only (no MA-cross-down exit is implemented)

### Parameters
| Param | Value |
|-------|-------|
| Fast MA | 20 days |
| Slow MA | 100 days |
| Universe size | 50 ETFs |
| Stop-loss | 8% |
| Take-profit | 20% |

---

## Strategy 5 — Momentum Scan (full universe)

**File**: `saxo_etf_strategy/core/etf_strategy.py` → `MomentumScanStrategy`  
**Type**: Full-universe momentum screen  

### Concept
Scans the entire ~8,924-ETF universe (not a curated list) for the strongest 3-month momentum among liquid, non-leveraged US ETFs.

1. **Filter**: NYSE Arca / NASDAQ only, base ticker ≤ 5 chars (liquidity proxy), excludes leveraged/inverse keywords (ULTRA, 2X, 3X, BEAR, SHORT, INVERSE, etc.) — pre-filters to top 200 candidates by ticker length.
2. **Score**: for each candidate, requires price above SMA(20) and a positive 63-day return; score = 63-day return.
3. **Select**: top `max_candidates_per_run` by score.

Runtime is ~3–5 minutes (200 sequential history API calls) — intended for a weekly/off-hours run, not the daily cycle.

### Parameters
| Param | Value |
|-------|-------|
| Lookback | 63 trading days |
| SMA confirmation | 20 days (price must be above it) |
| Pre-filter pool | 200 candidates |
| Stop-loss | 8% |
| Take-profit | 20% |

Not currently selected by default (`strategy_name = "sector_rotation"`). Not documented until the 2026-08-20 audit — see Audit Log.

---

## Strategy Comparison

> **DECISION (2026-08-26, closed — do not re-run this comparison):** user
> asked which strategy is most profitable; all 4 backtestable strategies
> were compared on real 10-year data (table below) and Sector Rotation
> won on both CAGR and Sharpe. User's explicit call: **"just keep that
> one is good"** — stay on `sector_rotation`, do not switch, do not
> re-litigate this unless something changes (new years of data, a
> strategy's code changes, or the user raises it again themselves).

**Real 10-year backtest added 2026-08-26** (`backtest_etf.py` for Sector
Rotation, `backtest_etf_other_strategies.py` for the other 3) -- entry
logic transcribed exactly from each strategy's real production code in
`saxo_etf_strategy/core/etf_strategy.py`, same shared 8% SL / 20% TP exit
rule for all (confirmed uniform across every strategy via
`etf_executor.review_exits()` -- no strategy has its own distinct exit).
Equal-weight position sizing across up to 10 slots for every strategy, so
the numbers below are directly comparable to each other.

| Strategy | Type | Best regime | CAGR | Sharpe | MaxDD | WR% | Trades/yr |
|----------|------|-------------|------|--------|-------|-----|-----------|
| **Sector Rotation** ✓ default | Momentum | All regimes | **+12.4%** | **0.84** | -31.6% | 57.1% | 5.0 |
| **Dual MA** | Trend following (state-based) | Trending markets | +9.1% | 0.63 | -24.5% | 41.8% | 30.7 |
| **Mean Reversion** | Oversold bounce | Volatile markets | +3.0% | 0.44 | -15.6% | 48.3% | 6.2 |
| **Risk-Off** | Regime switching | Bear markets | +2.8% | 0.76 | **-11.7%** | 45.8% | 6.4 |
| **Momentum Scan** | Full-universe momentum | All regimes | not backtestable | — | — | — | — |

**Sector Rotation remains the best choice on both CAGR and Sharpe** — none
of the other 3 beat it on risk-adjusted return, only Risk-Off comes close
on Sharpe (0.76) while trading much less drawdown (-11.7% vs -31.6%) for
much less return (+2.8% vs +12.4%). If capital preservation ever becomes
the priority over growth, Risk-Off is the one to reconsider; otherwise
the current default is validated by real data, not just left in place by
default.

Momentum Scan's numbers are missing because it isn't practically
backtestable: it scans Saxo's live ~8,924-instrument full universe with
exchange/ticker-length/description filtering at run time, and no
equivalent historical dataset (past exchange listings, past descriptions)
exists to replay accurately — any attempt would carry real survivorship
bias. This is also already the least-mature of the 5 per the note below
(never selected by default, undocumented until the 2026-08-20 audit).

---

## How to Run

```powershell
# From E:\SaxoTrNew\SaxoTrNew (always run from parent dir)
python saxo_etf_strategy\run_etf_bot.py

# With explicit strategy (overrides etf_config.py):
python saxo_etf_strategy\run_etf_bot.py --strategy sector_rotation
python saxo_etf_strategy\run_etf_bot.py --strategy risk_off
python saxo_etf_strategy\run_etf_bot.py --strategy mean_reversion
python saxo_etf_strategy\run_etf_bot.py --strategy dual_ma
```

---

## Going Live

```python
# saxo_etf_strategy/config/etf_config.py
dry_run: bool = False   # flip to place real SIM orders after dry-run review
```

**Keep `dry_run = True` until you've reviewed at least 3–4 weeks of dry-run signals.**  
The log is at `saxo_etf_strategy/logs/etf_strategy.log`.

---

## Key Files

| File | Purpose |
|------|---------|
| `saxo_etf_strategy/run_etf_bot.py` | Entry point — wires auth, runs daily cycle |
| `saxo_etf_strategy/run_etf_daily.bat` | Task Scheduler launcher (sets CWD correctly) |
| `saxo_etf_strategy/config/etf_config.py` | All config: capital, strategy name, risk, paths |
| `saxo_etf_strategy/core/etf_strategy.py` | 4 strategy classes + dispatcher |
| `saxo_etf_strategy/core/etf_executor.py` | Order execution + SL/TP exit review |
| `saxo_etf_strategy/core/etf_universe.py` | ETF universe builder (8,922 ETFs, cached 24h) |
| `saxo_etf_strategy/core/etf_state.py` | JSON position state store |
| `saxo_etf_strategy/core/saxo_client.py` | HTTP client with rate-limit backoff |
| `saxo_etf_strategy/data/etf_positions.json` | Live position state + order log |
| `saxo_etf_strategy/data/etf_universe.json` | Cached ETF universe (24h TTL) |
| `saxo_etf_strategy/logs/etf_strategy.log` | Daily run log |

---

## Isolation

The ETF module is completely isolated from all other modules:
```powershell
# Verify no cross-imports:
Select-String -Path saxo_etf_strategy\ -Recurse -Pattern "import atos"  # → no matches
Select-String -Path atos\ -Recurse -Pattern "etf_strategy"              # → no matches
```

No shared state files, no shared imports, no shared capital allocation code.

---

## Known Limitations

- `sector_rotation` exit-on-rank-drop not yet implemented — currently only SL/TP exit triggers a sell when a sector falls out of the top 3. A future improvement would add a rank-based exit trigger.
- `dual_ma` has no crossover-event detection — it's a same-day state check (fast MA > slow MA), so it will keep re-signaling every day the condition holds rather than only at the moment of the cross. A future improvement would track the prior day's MA relationship to detect the actual cross.
- ETF universe cache (`etf_universe.json`) can grow large — it is gitignored.
- `dry_run = False` since 2026-08-15 — this module places real SIM orders on every scheduled run.

---

## Audit Log — 2026-08-20

A full read-through of this doc against the live code (`etf_strategy.py`, `etf_executor.py`, `etf_config.py`) turned up several places where the doc had drifted from — or never matched — the implementation. Recorded here so future sessions don't re-introduce the same mistakes or re-trust the stale claims:

| Issue | Was | Now |
|-------|-----|-----|
| Live-trading status | Doc said `dry_run = True` / "review for weeks" | Corrected — module has been live since 2026-08-15, real orders since 2026-08-17 |
| Sector universe bug | Code's `SECTORS` list had `SPY` (not a sector) instead of `XLC` (Communication Services) — confirmed live in logs, XLC was never once a candidate | **Fixed in code** (`etf_strategy.py`): `SPY` → `XLC` |
| Lookback window bug | `_history()` fetched `count+5` bars but never trimmed back to `count`, so 3-month (63-day) returns for `sector_rotation` and `momentum_scan` were actually measured over up to ~68 days | **Fixed in code** (`etf_strategy.py`): result is now sliced to `closes[-count:]` |
| Rank-drop exit | Doc claimed sectors are sold when they drop out of top-3 | This was never implemented — doc corrected to state the actual behavior (SL/TP only). Left as a known limitation rather than silently added, since implementing it changes live trading behavior. |
| Dual MA crossover | Doc claimed a "crosses within last 5 bars" signal | No such event detection exists — it's a same-day state check. Doc corrected; left as a known limitation (dual_ma isn't the active default strategy). |
| Undocumented strategy | `momentum_scan` existed in code, unmentioned in docs | Added as Strategy 5 |

Position sizing (3% per position, 15%/5 slots), RSI/SMA windowing, mean-reversion, and risk-off logic were all verified correct against the code and did not need changes.
