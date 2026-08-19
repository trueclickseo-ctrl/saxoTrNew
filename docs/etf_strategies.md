# ETF Strategy Playbook

**Module**: `saxo_etf_strategy/`  
**Universe**: Up to 8,922 US-listed ETFs (cached 24h, sourced from Saxo API)  
**Strategies**: 4 (switch via `etf_config.py` `strategy_name`)  
**Capital**: 15% of account balance — separate from stocks (85%) and forex/futures  
**Max positions**: 5 open at once, 3% of account per position  
**Stop-loss**: 8% | **Take-profit**: 20%  
**Currency**: EUR (SIM account currency)  
**Scheduled**: 06:30 PKT daily via `saxo_etf_strategy/run_etf_daily.bat`  
**Status**: DRY RUN (`dry_run = True`) — review signals for several weeks before live  
**Last updated**: 2026-08-19  

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
- **Sell**: ETF drops out of top-3 (rank > 3) OR hits 8% stop OR hits 20% TP
- **Rebalance**: weekly (checked each run; buys/sells as needed)

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
- **Buy**: 20d MA crosses above 100d MA (within last 5 bars) AND not already in portfolio
- **Sell**: 20d MA crosses below 100d MA OR -8% stop OR +20% TP

### Parameters
| Param | Value |
|-------|-------|
| Fast MA | 20 days |
| Slow MA | 100 days |
| Signal lookback | 5 bars |
| Universe size | 50 ETFs |
| Stop-loss | 8% |
| Take-profit | 20% |

---

## Strategy Comparison

| Strategy | Type | Signal frequency | Best regime |
|----------|------|-----------------|-------------|
| **Sector Rotation** ✓ default | Momentum | Weekly rebalance | All regimes |
| **Risk-Off** | Regime switching | Low (only on SPY SMA cross) | Bear markets |
| **Mean Reversion** | Oversold bounce | Low (requires extreme dip) | Volatile markets |
| **Dual MA** | Trend following | Medium (~10-20/yr) | Trending markets |

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
- ETF universe cache (`etf_universe.json`) can grow large — it is gitignored.
- `dry_run = True` by default — production orders require explicit flip.
