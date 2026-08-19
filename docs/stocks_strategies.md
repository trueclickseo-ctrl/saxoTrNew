# US Equities Strategy Playbook

**Module**: `atos/` + `atos_runner.py`  
**Universe**: 108 S&P 500 blue-chip stocks  
**Strategies**: 2 concurrent (Momentum Blend + Mean Reversion)  
**Capital**: 85% of live SIM account (split 50/50 between strategies)  
**Scheduled**: 06:00 PKT daily (main) + 5× intraday (19:00–00:30 PKT)  
**Last updated**: 2026-08-19  

---

## Universe

**108 S&P 500 blue-chip stocks** — defined in `atos/universe.py` (`US_TICKERS`).

### Selection criteria
- Daily dollar volume > $200M (sufficient liquidity for the position sizes we trade)
- Market cap > $30B (avoids small/micro-cap volatility)
- Established companies with at least 5 years of price history

### Excluded categories (deliberate)
| Category | Reason |
|----------|--------|
| REITs | Different tax/distribution mechanics; dividend timing conflicts with momentum logic |
| Small E&P (oil & gas) | Extreme commodity exposure; correlates with CL futures we already trade |
| Speculative biotech | Binary FDA events cause 40–80% overnight gaps; untradeable with rule-based stops |
| Penny stocks / low volume | Slippage kills edge at our sizing |

### Sector breakdown (approximate)
Technology, Comm Services, Consumer Discretionary, Consumer Staples, Financials, Healthcare, Industrials, Energy (majors only), Semiconductors, Materials, Utilities.

---

## Capital Allocation

```
US Equities total:   85% of account
  ├─ US Blend:       50% of 85% = 42.5% of account
  │    8 slots (6 offense + 2 defense)
  │    Each position ≈ 6.25% of Blend sleeve
  └─ US Reversion:   50% of 85% = 42.5% of account
       6 max slots (10% × 108 stocks = max 10 universe %)
       Each position ≈ 8.3% of Reversion sleeve
```

All percentages live in `config/capital.json` — the **single source of truth**. Never edit strategy code to change allocation; change only the JSON.

---

## Strategy 1 — US Momentum Blend (LIVE)

**File**: `atos/us_momentum.py`  
**Type**: Cross-sectional momentum + low-volatility defensive blend  
**Status**: LIVE — running since 2026-08-07  
**Rebalance**: Every 14 calendar days (`REBAL_DAYS = 14`) — see [Rebalance Cadence](#rebalance-cadence-why-14-days) below  

### Concept
Two uncorrelated factors are blended in one portfolio sleeve:
1. **Momentum factor (offense)**: top-6 stocks by 6-month risk-adjusted return (momentum/volatility ratio) — captures stocks already trending strongly
2. **Low-volatility factor (defense)**: 2 stocks with lowest 60-day realized volatility (above their EMA200) — provides ballast when momentum stocks correct

Factor correlation: ~0.44 — low enough that the blend outperforms either factor alone on a risk-adjusted basis.

### Entry
| Type | Logic |
|------|-------|
| **Offense (6 slots)** | Top-6 stocks by `return_120d / vol_60d`, where return > 5% AND price > EMA(200) |
| **Defense (2 slots)** | Lowest-vol 2 stocks with price > EMA(200) (no momentum minimum required) |

### Risk-Off Gate
Daily check: if SPY/QQQ index closes below its 200-day SMA, all Blend positions are sold and cash is held. Re-enters when index recovers above the SMA.

### Exit
Triggered by the fortnightly rebalance: if a stock is no longer in the top-6 momentum or top-2 defense selection, it is sold and replaced. No individual stop-loss — the rebalance is the exit mechanism.

### Position Sizing
Equal-weight within the sleeve. Budget = 50% of live SIM cash / 8 slots.

### Backtested Performance (10y, 2016–2026)
| Metric | Value |
|--------|-------|
| CAGR | 24.4% |
| Sharpe | 1.30 |
| Max Drawdown | 21.3% |
| Universe | 108 stocks |

### Parameters
| Param | Value |
|-------|-------|
| Universe | 108 stocks (`atos/universe.py`) |
| Momentum lookback | 120 trading days (≈ 6 months) |
| Offense slots | 6 |
| Defense slots | 2 |
| Min momentum return | 5% |
| Trend filter | Price > EMA(200) |
| Rebalance period | 14 calendar days |
| Capital | 50% of live SIM cash |

### Rebalance Cadence — why 14 days

Changed from 7 → 14 on **2026-08-19**. Swept 4d / 7d / 10d / 15d / 21d / monthly /
quarterly through the production engine (`backtest_us_momentum.py`) on the 10-year
panel — 385 names, TOPN=8, daily regime overlay + vol targeting.

**Full sample:**

| Interval | CAGR | Sharpe | MaxDD |
|----------|------|--------|-------|
| 4 days | 15.6% | 0.75 | 37.7% |
| 7 days (old) | 21.4% | 0.98 | 33.6% |
| **15 days** | **25.9%** | **1.13** | **30.6%** |
| 21 days | 20.5% | 0.94 | 37.1% |
| monthly | 14.1% | 0.68 | 48.8% |
| quarterly | 8.8% | 0.47 | 44.9% |

A single-sample peak is usually curve-fit, so it was re-scored by **mean rank across
8 independent tests** (3 sub-periods × 5 TOPN settings):

| Interval | Mean rank | Worst rank | Verdict |
|----------|-----------|------------|---------|
| 15 days | 2.31 | 4 | robust |
| 7 days | 2.56 | 6 | unstable |
| 21 days | 2.56 | 4 | robust |
| 10 days | 3.50 | 6 | unstable |
| monthly | 5.31 | 7 | avoid |
| quarterly | 6.31 | 7 | avoid |

The top three are a **statistical tie** (0.25 rank spread = noise). The tiebreak is
**trading cost** — weekly decays far faster as costs rise:

| Interval | 0.11%/side | 0.20% | 0.35% | 0.50% |
|----------|-----------|-------|-------|-------|
| 7 days | 0.98 | 0.84 | 0.62 | 0.39 |
| **15 days** | **1.13** | **1.04** | **0.90** | **0.75** |
| 21 days | 0.94 | 0.88 | 0.78 | 0.67 |
| monthly | 0.68 | 0.63 | 0.55 | 0.46 |

At ~37,500 SEK per slot, Saxo's real all-in cost lands near 0.15–0.25%/side — squarely
in the band where weekly starts bleeding and the fortnightly cadence does not. It also
halves commission drag and portfolio churn.

**14 rather than 15** so rebalances land on the same weekday each time instead of
drifting through the week.

> Monthly and slower ranked 5th–7th in **every** test. Do not extend past ~21 days.

---

## Strategy 2 — US Mean Reversion (LIVE ON SIM)

**File**: `atos/us_reversion.py`  
**Type**: Short-term dip buying on oversold blue-chips  
**Status**: LIVE ON SIM — enabled 2026-08-08; observe 6–8 weeks before real capital  

### Concept
Short-term price dips in quality companies — measured by RSI < 33 (now 38 after recalibration) and a price decline > 5% below SMA(20) — statistically revert to the mean within 3–10 trading days. The EMA(200) filter ensures we only buy dips in companies in a long-term uptrend, not falling knives.

The edge: quality companies with temporary oversold conditions in an uptrend have a strong baseline of institutional buyers who step in on dips. We ride that reversion.

### Entry Conditions (all required)
| Condition | Value | Purpose |
|-----------|-------|---------|
| RSI(14) | < 38 | Oversold momentum |
| Price vs SMA(20) | > 5% below | Meaningful dip, not noise |
| Volume | > 1.5× 20-day average | Confirms selling climax |
| Price vs EMA(200) | Above | In long-term uptrend |
| Sleeve drawdown | < 10% | Pause if sleeve down 10% |
| Daily loss cap | < 3% account loss today | Hard safety gate |

### Exit Conditions (first hit)
| Condition | Meaning |
|-----------|---------|
| RSI(14) > 60 | Recovery complete |
| Price ≥ SMA(20) | Target hit (dip filled) |
| -4% hard stop | Entry stop (capital.json) |
| 10 trading days | Time-stop |

### Intraday Extension
The reversion scanner also runs **5 times per session** (19:00, 20:30, 22:00, 23:30, 00:30 PKT) via `atos_runner.py intraday`. This version adds:
- **Opening gap filter**: skip if today's gap-down > 8% (likely earnings/scandal)
- **Total drop filter**: skip if total intraday drop > 15%
- **News keyword filter**: scan last 24h headlines for bankruptcy/fraud/scandal keywords → skip
- Uses live 5-minute bars from yfinance, with volume scaled to elapsed session fraction

### Honest Out-of-Sample Validation (2026-08-08)

| Period | Sharpe | WR | MaxDD | Trades | CAGR |
|--------|--------|-----|-------|--------|------|
| IS: Apr 2024 – Jun 2025 | 2.08 | 66% | 12.5% | 64 | 30% |
| **OOS: Jun 2025 – Aug 2026** | **2.39** | **70%** | **5.9%** | **23** | **47%** |

OOS was never touched during parameter selection. Verdict: **5/5 — edge survives clean OOS test.**

### Position Sizing
- Budget: 50% of live SIM cash
- Max slots: `max_universe_pct (10%) × 108 stocks = 10 max` (practical limit: 6 due to budget)
- Each position: `budget / max_slots`

### Parameters
| Param | Value | Source |
|-------|-------|--------|
| RSI entry | < 38 | Recalibrated from 33 (doubles trade frequency, same Sharpe) |
| Dip | > 5% below SMA(20) | IS-validated |
| Volume | > 1.5× 20d avg | IS-validated |
| Stop | -4% | capital.json |
| Time stop | 10 trading days | capital.json |
| Sleeve DD cap | 10% | capital.json |
| Max slots | 10% of universe = 10 | capital.json |
| Capital | 50% of live SIM cash | capital.json |

---

## Stop-Loss Architecture

The system uses a **three-layer stop hierarchy** (managed by `intraday_monitor.py`):

```
Layer 1 — Entry stop:   price from entry in signal (fixed)
Layer 2 — Trailing:     -12% from peak (follows price up)
Layer 3 — Hard floor:   -15% from entry (never exceeded)
```

The 1-second monitor (`intraday_monitor.py`) runs during US market hours (09:30–16:00 ET = 19:30–02:00 PKT). It checks Saxo prices every second and sends sell orders when any layer is breached.

**Circuit breaker**: If price data is unavailable for > 180 seconds, CRITICAL alert fires.

---

## Email Notifications

All notifications go to `heyitskaxhif@gmail.com` automatically.

| Event | Trigger |
|-------|---------|
| **Blend rebalance** | Fortnightly — targets list, offense/defense split, risk-off status |
| **Reversion entry signal** | Per scan — RSI, dip%, vol, BUY vs QUEUED per ticker |
| **Reversion exit** | Per exit — P&L %, P&L SEK, hold days, exit reason |
| **BUY executed** | Per order — strategy, shares, price, value SEK, account balance |
| **SELL executed** | Per order — strategy, shares, price, P&L SEK, account balance |
| **Weekly P&L report** | Fridays — equity, week P&L, open positions, SVG equity chart |

---

## Corporate Events Filter

`atos/corporate_events.py` automatically:
- **Ex-dividend**: exits position 3 days before ex-div date (avoids ex-div gap)
- **Earnings**: skips new entries 2 days before earnings report (avoids binary risk)

Data source: yfinance (free tier, ~75% accuracy on earnings dates).

---

## Key Files

| File | Purpose |
|------|---------|
| `atos_runner.py` | Daily orchestrator — `run_cycle()` + `run_intraday_cycle()` |
| `atos/universe.py` | 108-stock universe (`US_TICKERS` list) |
| `atos/us_momentum.py` | Blend strategy: momentum scoring, rebalance logic, risk-off gate |
| `atos/us_reversion.py` | Reversion strategy: RSI/dip signal, exits, sleeve drawdown cap |
| `atos/intraday_reversion.py` | Intraday scanner: live 5-min bars + bad-news filters |
| `atos/capital_config.py` | Loads `config/capital.json`, typed getters for all allocation values |
| `atos/corporate_events.py` | Ex-dividend + earnings date checker |
| `atos/risk.py` | Kill switch, daily loss cap, ATR sizing, heat gates |
| `atos/notifier.py` | Email notification module — all 6 email types |
| `atos/features.py` | Technical indicators: EMA, ATR, RSI, MACD, Bollinger, Donchian |
| `atos/database.py` | SQLite CRUD for `data/atos_live.db` |
| `atos/learner.py` | Magnitude-aware detector weight updater |
| `config/capital.json` | **Single source of truth for all capital allocation** |
| `intraday_monitor.py` | 1-second stop-loss watchdog during US market hours |
| `atos_dashboard.py` | Live dashboard — http://localhost:8070 |

---

## Backtest Results Summary

### US Momentum Blend — LIVE
```
Universe:  108 stocks (S&P 500 blue-chip, market cap >$30B, daily vol >$200M)
Rebalance: Fortnightly (REBAL_DAYS=14)
Config:    Top-6 momentum + 2 low-vol, daily risk-off, vol-target 15%
CAGR:      24.4% | Sharpe: 1.30 | MaxDD: 21.3%
VERDICT:   LIVE
```

### US Mean Reversion — LIVE ON SIM
```
Universe:  108 stocks (same as Blend)
Hold:      3-10 trading days
Config:    RSI<38, Dip>5%, Vol>1.5x, Stop4%, 6 max slots (10% of universe)
IS (2024-2025): Sharpe 2.08, WR 66%, MaxDD 12.5%
OOS (2025-2026): Sharpe 2.39, WR 70%, MaxDD 5.9% — clean OOS pass
VERDICT:   LIVE ON SIM — watch 6-8 weeks before real capital
```

---

## Rejected Strategies (Do Not Revisit)

| Strategy | Why rejected |
|----------|-------------|
| OMX30 / CPH25 momentum | 3y Sharpe 1.81 was bull mirage; 10y Sharpe 0.24, MaxDD 44% |
| US Breakout (per-instrument) | ~1 trade per 2 years per stock; not enough signals |
| ML probability model | Walk-forward OOS AUC 0.52 = coin flip; no edge |
| Residual momentum | Same return as raw momentum; no added value in blend |
| Plain momentum / mom252 / 52-week-high | All beaten by risk-adjusted momentum |

---

## Quick Reference

```powershell
# Run daily cycle
python atos_runner.py

# Run intraday reversion scan (during US market hours only)
python atos_runner.py intraday

# Start intraday stop-loss monitor
python intraday_monitor.py

# Start dashboard
python atos_dashboard.py   # → http://localhost:8070

# Refresh Saxo token (expires every ~24h)
python set_token.py

# Emergency stop
New-Item -Path "STOP_TRADING" -ItemType File
# Resume:
Remove-Item "STOP_TRADING"
```
