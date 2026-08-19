# ATOS — Algorithmic Trading Operating System
## Agent Handover & Project State Document
### Last Updated: 2026-08-17 | Updated by: Agent #4 (Kwaseem) — v4

---

> ## SECURITY — STILL PENDING
>
> **`config/deploy.json` (FTP host/user/password for namazic.com) was committed in
> the very first commit (`06ffbc9`) and remains in git history** (it has been untracked
> and gitignored but history was not purged). Required:
> 1. **Rotate the FTP password** in the hosting panel if not done yet.
> 2. Purge from history: `git filter-repo --path config/deploy.json --invert-paths` then force-push.
> 3. File is already gitignored — do NOT re-add it.

> ## MULTI-AGENT PROTOCOL — READ FIRST
>
> **Multiple Claude agents share this project. Before doing ANYTHING:**
>
> 1. `git pull origin main` — get latest code
> 2. Read this entire README
> 3. Do your work
> 4. Update this README with what you did and what's next
> 5. `git add -A && git commit -m "agent#N: <what you did>" && git push origin main`
>
> **Every agent MUST push to git before ending their session. No exceptions.**
>
> **Working directory:**
> - `E:\SaxoTrNew\SaxoTrNew\` — primary working directory (all users can write)

---

## 1. What This Project Is

An **automated paper-money algorithmic trading system** on a **Saxo Bank SIM** (simulation)
account via the official Saxo OpenAPI.

**Paper money only. No real money at risk.**

The system runs **four concurrent trading modules**:

1. **US Equities (ATOS)** — Momentum Blend + Mean Reversion on 108 S&P 500 stocks
2. **ETF Module** — 4-strategy sector rotation / risk-off / mean-reversion on US ETFs
3. **Futures Module** — 3 strategies (Donchian, RSI Pullback, EMA Crossover) on 5 markets
4. **Forex Module** — 11 strategies on 34 FX pairs:
   - 10 swing strategies (EMA+ADX, RSI(2) pullback, Donchian, BB reversion, pullback-to-EMA, weekend gap fill, SuperTrend, Z-score, ML, CNN-LSTM)
   - 1 day-trading strategy: **London Breakout** — session range break at London/NY open, 7 majors, no overnight holds, 15,000 SEK dedicated book

Features: weekly momentum rebalance, intraday mean-reversion scanner, 1-second
stop-loss monitor, and **automatic email notifications** on every trade and weekly P&L report.

---

## 2. Project Location

```
Working dir:  E:\SaxoTrNew\SaxoTrNew\
Git:          https://github.com/trueclickseo-ctrl/saxoTrNew.git
Branch:       main
Dashboard:    python atos_dashboard.py  ->  http://localhost:8070
```

---

## 3. Current System State — 2026-08-19

### Active Strategies

| Strategy | Status | Capital | File |
|---|---|---|---|
| **US Momentum Blend** | LIVE | 50% of live SIM cash | `atos/us_momentum.py` |
| **US Mean Reversion** | LIVE ON SIM | 50% of live SIM cash | `atos/us_reversion.py` |
| **ETF Sector Rotation** | DRY RUN (SIM) | 15% of account | `saxo_etf_strategy/` |
| **Futures (3 strategies)** | LIVE ON SIM | 1% risk/trade (ATR-sized) | `futures/` |
| **Forex Swing (10 strategies, 34 pairs)** | LIVE ON SIM | 1% risk/trade (ATR-sized) | `forex/` |
| **Forex London Breakout (day trading)** | LIVE ON SIM | 15,000 SEK dedicated | `forex/strategy_london_breakout.py` |
| OMX30 / CPH25 | PAUSED | — | `atos_runner.py` |

### Universe (2026-08-08)

**108 S&P 500 blue-chip stocks** — solid companies, daily dollar volume >$200M, market cap >$30B.
Sectors: Tech, Comm, Cons Disc, Cons Staples, Financials, Healthcare, Industrials, Energy, Semis, Materials, Utilities.
REITs, small E&P, small materials, and speculative biotech **removed** (delist/low-volume risk).

### Live Open Positions
Check the dashboard (`python atos_dashboard.py` → http://localhost:8070) for current state.

### Processes

| Process | Trigger | Job |
|---|---|---|
| `atos_runner.py` | Task Scheduler 06:00 PKT | Daily cycle — Blend rebalance + Reversion exits |
| `saxo_etf_strategy\run_etf_bot.py` | Task Scheduler 06:30 PKT | ETF daily scan — signals + exits |
| `futures/runner.py --live` | Task Scheduler 06:15 PKT | Futures daily cycle — 3 strategies, all markets |
| `forex/runner.py --live` | Task Scheduler 06:20 PKT | Forex daily cycle — 34 pairs, 10 swing strategies |
| `run_lbo_london.bat` | Task Scheduler 12:00 PKT Mon–Fri | LBO London open — Asian range break (07:00 UTC) |
| `run_lbo_ny.bat` | Task Scheduler 18:00 PKT Mon–Fri | LBO NY open — London morning range break (13:00 UTC) |
| `run_lbo_close.bat` | Task Scheduler 01:00 PKT daily | LBO force-close — exits all day trades (20:00 UTC) |
| `atos_runner.py intraday` | Task Scheduler ×5 (19:00–00:30 PKT) | Intraday reversion scan |
| `intraday_monitor.py` | Task Scheduler 15:30 PKT | 1-second stop-loss watchdog |
| `terminal_dashboard.py` | Manual | PowerShell live dashboard |

---

## 4. How to Run — Start Here Every Session

### Token refresh (expires every ~24h)
```powershell
python set_token.py
```
Token entered with hidden input — never shown in terminal or committed to git.

### Run daily cycle manually
```powershell
cd E:\SaxoTrNew\SaxoTrNew
python atos_runner.py
```

### Run intraday reversion scan manually
```powershell
python atos_runner.py intraday
```

### Start intraday stop-loss monitor
```powershell
python intraday_monitor.py
```

### Start dashboard
```powershell
python atos_dashboard.py
```
Open http://localhost:8070.

### Emergency stop
```powershell
New-Item -Path "STOP_TRADING" -ItemType File
# Resume:
Remove-Item "STOP_TRADING"
```

### End of session — ALWAYS push
```powershell
git add -A
git commit -m "agent#N: <describe what you did>"
git push origin main
```

---

## 4b. Module Documentation

| Doc | Covers |
|-----|--------|
| [`docs/stocks_strategies.md`](docs/stocks_strategies.md) | US Equities — Momentum Blend + Mean Reversion, universe, capital, stops, backtests |
| [`docs/etf_strategies.md`](docs/etf_strategies.md) | ETF Module — 4 strategies (Sector Rotation, Risk-Off, Mean Reversion, Dual MA) |
| [`docs/futures_strategies.md`](docs/futures_strategies.md) | Futures — 7 strategies on ES/NQ/GC/CL/ZB, UICs, daily loss limit |
| [`docs/forex_strategies.md`](docs/forex_strategies.md) | Forex — 11 strategies (10 swing + London Breakout day trading), 34 pairs, UICs |

---

## 5. Architecture — How the Engine Works

```
Daily cycle (atos_runner.py) — Task Scheduler 06:00 PKT
---------------------------------------------------------------------------
1. Kill switch + daily loss cap check
2. Download 300d daily bars for all 61 US tickers (yfinance)
3. Corporate events — exit 3d before ex-div, skip 2d before earnings
4. US Momentum Blend:
     - Risk-off check: index vs 200d SMA -> cash if bearish
     - If REBAL_DAYS (14) elapsed: sell all -> rebuy top 6 momentum + 2 low-vol
     - Budget: BLEND_CASH_PCT (50%) of live SIM cash
5. US Mean Reversion:
     - Exit checks: RSI>60 / SMA20 hit / -4% stop / 10-day time-stop
     - Scan universe for new dip entries (RSI<33, dip>5%, vol>1.5x, above EMA200)
     - Budget: REV_CASH_PCT (50%) of live SIM cash / max_slots
6. Learning pass (atos/learner.py)
7. Print strategy scorecard (BLEND vs REVERSION head-to-head)
8. Generate HTML dashboard -> data/dashboard.html -> FTP upload (if config set)
9. Log to data/atos_live.db + data/engine_YYYY-MM-DD.log + data/trade_log.csv

Intraday reversion scan (atos_runner.py intraday) — 5 times per session
---------------------------------------------------------------------------
- Runs at 19:00, 20:30, 22:00, 23:30, 00:30 PKT (10:00 AM - 3:30 PM ET)
- Skips first 30 min of session (opening gap volatility)
- Downloads live 5-min bars (yfinance)
- Bad-news filter 1: gap-down >8% at open -> skip (likely earnings/scandal)
- Bad-news filter 2: total drop >15% today -> skip
- Bad-news filter 3: keyword scan on last 24h yfinance headlines -> skip if match
- Volume scaled by session fraction elapsed (fair comparison to 20d daily avg)
- RSI computed from last 13 daily closes + today's live price as 14th point
- Places BUY orders via Saxo API if signal fires

Intraday monitor (intraday_monitor.py) — runs all session, separate process
---------------------------------------------------------------------------
- Saxo API every 1 second during 09:30-16:00 ET
- Stop-loss hierarchy: fixed entry stop -> trailing -12% -> hard floor -15%
- Circuit breaker: CRITICAL if data blind >180s
- Market-closed mode: Yahoo prices every 5 min
```

---

## 6. Capital Allocation — config/capital.json

**All capital percentages live in one file. Edit only this file to change allocation.**

```json
{
  "account": {
    "starting_capital_sek": 300000,
    "max_deploy_pct": 0.90,
    "cash_buffer_pct": 0.10
  },
  "strategies": {
    "us_blend": {
      "allocation_pct": 0.50,
      "offense_slots": 6,
      "defense_slots": 2
    },
    "us_reversion": {
      "allocation_pct": 0.50,
      "max_universe_pct": 0.10,
      "min_slots": 2,
      "stop_pct": 0.04,
      "max_hold_days": 10,
      "sleeve_dd_cap": 0.10,
      "fallback_sleeve_sek": 300000
    }
  }
}
```

Loaded via `atos/capital_config.py`. Printed at startup of every run.

**Position sizing:**
- US Blend: `50% of cash / 8 slots = 6.25% of account per position`
- US Reversion: `50% of cash / (10% × 61 stocks = 6 max slots) = 8.3% per slot`

---

## 7. US Momentum Blend — Full Spec

| Parameter | Value |
|---|---|
| Universe | 108 S&P 500 stocks (`atos/universe.py` -> `US_TICKERS`) |
| Lookback | 120 trading days (~6 months) |
| Offense | Up to 6 stocks: return > 5%, ranked by return/vol ratio |
| Defense | Always 2 stocks: lowest 60d vol above EMA200 |
| Positions | 2-8 dynamic (deduped) |
| Weight | Equal-weight within budget |
| Rebalance | Every 14 calendar days (swept 2026-08-19; see `docs/stocks_strategies.md`) |
| Risk-off | Daily: index < 200d SMA -> full cash |
| Capital | 50% of live SIM cash (from capital.json) |

**Backtest (10y, 2016-2026):** Sharpe 1.30, MaxDD 21.3%, CAGR 24.4%

---

## 8. US Mean Reversion — Full Spec

| Parameter | Value | Source |
|---|---|---|
| Entry: RSI | < 33 | IS-validated, OOS confirmed |
| Entry: Dip | > 5% below SMA20 | IS-validated |
| Entry: Volume | > 1.5x 20d avg | IS-validated |
| Entry: Trend | Price > EMA200 | Avoid falling knives |
| Exit A | RSI > 60 | Recovery complete |
| Exit B | Price >= SMA20 | Target hit |
| Exit C | -4% stop | Hard stop (capital.json) |
| Exit D | 10 trading days | Time-stop (capital.json) |
| Max slots | 10% of universe = 6 | capital.json max_universe_pct |
| DD cap | 10% | Pause new entries if sleeve down 10% |
| Capital | 50% of live SIM cash | capital.json |

**Intraday extension:** scanner runs 5x/session; adds bad-news + keyword filters.

**Honest OOS validation (2026-08-08):**
IS (Apr 2024-Jun 2025): Sharpe 1.60, WR 60%, MaxDD 10.3%, N=20
OOS (Jun 2025-Aug 2026): Sharpe 2.39, WR 70%, MaxDD 5.9%, N=23, P&L +165,750 SEK
Verdict: 5/5 — edge survives clean OOS test.

---

## 9. ETF Strategy Module — `saxo_etf_strategy/`

**Completely separate from the shares strategies.** No shared imports, no shared capital, no shared state files. Runs as an independent process.

### Capital
- **15% of account balance** (separate from stocks' 85%)
- Max 5 positions, 3% of account per position
- Stop-loss: **8%** | Take-profit: **20%**
- All in EUR (SIM account currency)

### 4 Available Strategies (switch via `etf_config.py` `strategy_name`)

| Strategy | Logic | Today's signals |
|---|---|---|
| `sector_rotation` ✓ **(default)** | Top 3 of 11 US sector ETFs by 3-month return | XLV, XLF, XLE |
| `risk_off` | SPY > SMA200 → SPY+QQQ; SPY < SMA200 → TLT+GLD | SPY, QQQ |
| `mean_reversion` | RSI<30 + price ≥5% below SMA20 on 5 broad ETFs | — (no dips today) |
| `dual_ma` | 20d MA > 100d MA on curated 50-ETF list | ARKG, XBI, IBB |

### How to Run

```powershell
# From E:\SaxoTrNew\SaxoTrNew (always run from parent dir)
python saxo_etf_strategy\run_etf_bot.py
```

Task Scheduler job: `\ATOS ETF Daily Run` at 06:30 PKT daily.

### Going Live

```python
# saxo_etf_strategy/config/etf_config.py
dry_run: bool = False        # flip to place real SIM orders
# base_url = "...openapi"   # change sim/ → live URL when ready for real money
```

**Keep `dry_run=True` until several weeks of dry-run signals have been reviewed.**

### Key Files

| File | Purpose |
|---|---|
| `saxo_etf_strategy/run_etf_bot.py` | Entry point — wires Saxo auth, runs daily cycle |
| `saxo_etf_strategy/run_etf_daily.bat` | Task Scheduler launcher (sets CWD correctly) |
| `saxo_etf_strategy/config/etf_config.py` | All config: capital, strategy, risk, paths |
| `saxo_etf_strategy/core/etf_strategy.py` | 4 strategy classes + dispatcher |
| `saxo_etf_strategy/core/etf_executor.py` | Order execution, exit review (SL/TP) |
| `saxo_etf_strategy/core/etf_universe.py` | ETF universe builder (8,922 ETFs, cached 24h) |
| `saxo_etf_strategy/core/etf_state.py` | JSON position state store |
| `saxo_etf_strategy/core/saxo_client.py` | HTTP client with rate-limit backoff |
| `saxo_etf_strategy/data/etf_positions.json` | Live position state + order log |
| `saxo_etf_strategy/data/etf_universe.json` | Cached ETF universe (24h TTL) |
| `saxo_etf_strategy/logs/etf_strategy.log` | Daily run log |

### Isolation Proof
```powershell
# ETF module imports nothing from shares code:
grep -r "import atos" saxo_etf_strategy/   # → no matches
# Shares code knows nothing about ETF:
grep -r "etf_strategy" atos/ run_atos.py   # → no matches
```

---

## 10. File Map

### Core Engine
| File | Purpose |
|---|---|
| `atos_runner.py` | Daily orchestrator — `run_cycle()` + `run_intraday_cycle()` |
| `atos/us_momentum.py` | US Blend strategy logic (pure, no I/O) |
| `atos/us_reversion.py` | US Reversion strategy logic (pure, no I/O) |
| `atos/intraday_reversion.py` | Intraday scanner with bad-news + keyword filters |
| `atos/capital_config.py` | Loads config/capital.json, typed getters for all allocation values |
| `atos/corporate_events.py` | Ex-dividend + earnings checker (yfinance) |
| `atos/universe.py` | 108-stock US blue-chip universe (S&P 500 solid names only) |
| `atos/notifier.py` | Email notification module — all 5 email types |
| `atos/features.py` | Technical indicators: EMA, ATR, RSI, MACD, Bollinger, Donchian |
| `atos/decision_engine.py` | 8-detector consensus engine |
| `atos/risk.py` | Risk gates, ATR sizing, kill switch, daily loss cap |
| `atos/database.py` | SQLite CRUD — `data/atos_live.db` |
| `atos/learner.py` | Magnitude-aware detector weight updater |
| `atos/strategies.py` | 6 strategy classes (S1-S6) |
| `atos/dashboard_gen.py` | Static HTML dashboard generator |

### Config
| File | Purpose |
|---|---|
| `config/capital.json` | **Single source of truth for all capital allocation.** Edit this, not the code. |

### Monitoring & Display
| File | Purpose |
|---|---|
| `intraday_monitor.py` | 1-second stop-loss watchdog during US market hours |
| `atos_dashboard.py` | Live dashboard server — http://localhost:8070 |
| `ATOS_Monitor.bat` | Double-click launcher for intraday monitor |

### Futures Module (`futures/`)
| File | Purpose |
|---|---|
| `futures/__init__.py` | Module marker |
| `futures/universe.py` | 5 markets: ES, NQ (CdfOnIndex), GC (FxSpot), CL, ZB (ContractFutures) with UICs |
| `futures/strategy.py` | Strategy 1 — Donchian Channel 30d breakout (BP=30, EP=5, Mult=1.5) |
| `futures/strategy_rsi.py` | Strategy 2 — RSI(2) Pullback within uptrend (RSI<10, SMA50 trend filter) |
| `futures/strategy_ema.py` | Strategy 3 — EMA(5/20) crossover + ADX(14) ≥ 20 confirmation |
| `futures/runner.py` | Multi-strategy orchestrator — exits, entries, ATR sizing, state I/O |
| `data/futures_state.json` | Open positions — keyed as `strategy:symbol` (e.g. `donchian:GC`) |
| `data/futures_orders.json` | Order log (last 500 entries) |
| `data/futures_uic_cache.json` | ContractFutures UIC cache (refresh monthly with `--discover`) |

### Forex Module (`forex/`)
| File | Purpose |
|---|---|
| `forex/__init__.py` | Module marker |
| `forex/universe.py` | 34 FxSpot pairs across Asian, London, and Scandinavian sessions |
| `forex/runner.py` | Live runner — 11 strategies, bid/ask→mid, ATR sizing, DAY_TRADE_STRATEGIES isolation |
| `forex/strategy.py` | Strategy 1: EMA(5/30) + ADX(14) ≥ 25 swing trend |
| `forex/strategy_rsi.py` | Strategy 2: RSI(2) pullback within EMA(200) trend |
| `forex/strategy_donchian.py` | Strategy 3: 30d Donchian breakout + EMA(200) + ADX |
| `forex/strategy_bb.py` | Strategy 4: Bollinger Band reversion (2σ touch + RSI) |
| `forex/strategy_pullback.py` | Strategy 5: Pullback-to-EMA(20) in confirmed trend |
| `forex/strategy_gap.py` | Strategy 6: Weekend gap fill (Sunday 22:00 PKT) |
| `forex/strategy_supertrend.py` | Strategy 7: SuperTrend(10,3) + EMA(200) filter |
| `forex/strategy_zscore.py` | Strategy 8: Z-score(20) mean-reversion |
| `forex/strategy_ml.py` | Strategy 9: Logistic regression on 7 technical features |
| `forex/strategy_cnn_lstm.py` | Strategy 10: CNN-LSTM deep learning (requires `--train` first) |
| `forex/strategy_london_breakout.py` | **Strategy 11: London Breakout day trading — 7 majors, H1 range break, 15k SEK** |
| `forex/notifier.py` | Email alerts — run summary, token expired, weekly report, LBO trade open/close |
| `run_lbo_london.bat` | LBO London open launcher (12:00 PKT / 07:00 UTC) |
| `run_lbo_ny.bat` | LBO NY open launcher (18:00 PKT / 13:00 UTC) |
| `run_lbo_close.bat` | LBO force-close launcher (01:00 PKT / 20:00 UTC) |
| `test_london_breakout.py` | 57-test suite for LBO strategy (unit/functional/blackbox/edge) |
| `data/forex_state.json` | Open FX swing positions |
| `data/forex_orders.json` | Order log (last 500) |

### Backtests & Research
| File | Purpose |
|---|---|
| `backtest_us_reversion.py` | US mean-reversion backtest + `--grid` parameter search |
| `validate_honest_split.py` | Honest IS/OOS split validation (no data leakage) |
| `preview_us_momentum.py` | Preview today's momentum targets (dry-run, no orders) |
| `backtest_strategies.py` | Per-instrument strategy backtester |
| `backtest_momentum.py` | Cross-sectional momentum backtester |
| `backtest_us_momentum.py` | US momentum daily-equity backtester |
| `backtest_futures.py` | Donchian grid search (180 combos, ETF proxies, 5y) |
| `backtest_forex.py` | EMA+ADX grid search (288 combos, 7 pairs, 5y, mark-to-market) |

### Auth & Infrastructure
| File | Purpose |
|---|---|
| `set_token.py` | Set Saxo token with hidden input (token never shown/committed) |
| `saxo_client.py` | All Saxo API calls |
| `saxo_token.json` | OAuth token — **gitignored, never commit** |
| `config/deploy.json` | FTP credentials — **gitignored, never commit** |
| `config/email.json` | Gmail App Password config — **gitignored, never commit** |
| `config/email.json.template` | Setup template for email.json (no real secrets) |
| `send_test_email.py` | Test email system — run once to verify notifications work |
| `instrument_map.py` | Load `data/instrument_map.csv` (Yahoo ticker -> Saxo UIC) |
| `fx.py` | USD/SEK and other FX rate fetcher |

### Data Files
| File | Purpose |
|---|---|
| `data/atos_live.db` | Main SQLite DB — trades, signals, weights |
| `data/trade_log.csv` | Master trade log — every BUY/SELL from every strategy |
| `data/instrument_map.csv` | Yahoo ticker -> Saxo UIC mapping (committed) |
| `data/engine_YYYY-MM-DD.log` | Daily engine output log — gitignored |
| `data/monitor_log.txt` | Intraday monitor log — gitignored |

---

## 11. Risk Rules

```
Capital:         config/capital.json (single source of truth)
  US Blend:      50% of live cash, 8 slots, 6.25% per position
  US Reversion:  50% of live cash, 6 max slots, 8.3% per slot
Stop-loss:       Entry stop -> trailing -12% -> hard floor -15% (intraday monitor)
Reversion stop:  -4% hard stop per position (capital.json)
Reversion DD:    10% sleeve drawdown cap -> pause new entries
Daily loss cap:  3% -> no new entries if equity down >3% on the day
Commission:      0.08% per trade, min 1 USD
Corporate event: Auto-exit 3d before ex-div, skip 2d before earnings
News filter:     Gap-down >8% or 24h keyword match -> skip intraday entry
```

---

## 12. Dashboard Features

- Equity curve chart (90-day)
- Strategy sleeve status cards (US Blend blue / US Reversion orange)
- Strategy Head-to-Head comparison table (trades, WR, total P&L, avg win/loss)
- Cumulative P&L chart per strategy (line chart, Blend blue vs Reversion orange)
- Today's actions table with strategy column
- Open positions table with strategy column
- Trade history table (last 30 from trade_log.csv) with strategy column
- Algorithm brain weights + market allocation donut

---

## 13. Email Notifications

ATOS sends automatic email notifications to `heyitskaxhif@gmail.com` for every
significant event. No manual action needed — emails fire from `atos_runner.py`
whenever the relevant event occurs.

### Email types

| Trigger | Subject example | Contents |
|---|---|---|
| **Blend rebalance** (fortnightly) | `ATOS Blend — Targets: AAPL, MSFT... [date]` | Full target list, offense/defense split, risk-off flag, sleeve value |
| **Reversion entry signal** | `ATOS Reversion Signal — 3 Candidates [date]` | Table of RSI / Dip% / Vol / Price per ticker, BUY vs QUEUED |
| **Reversion exit** | `ATOS Reversion Exit — NVDA +2.3% [date]` | Ticker, P&L %, P&L SEK, hold days, exit reason, account balance |
| **Any BUY executed** | `ATOS BUY — AAPL 18 shares [date]` | Strategy, shares, price USD, value SEK, account balance |
| **Any SELL executed** | `ATOS SELL — AAPL +1,240 SEK [date]` | Strategy, shares, price USD, P&L SEK, account balance |
| **Weekly P&L report** (Fridays) | `ATOS Weekly Report — Week ending [date]` | Total equity, week P&L, open positions, closed trades table, SVG bar chart |

### Setup (one-time)

```powershell
# 1. Enable Google 2-Step Verification (if not already done)
#    https://myaccount.google.com/security

# 2. Create a Gmail App Password
#    https://myaccount.google.com/apppasswords
#    App name: "ATOS Trading"  -> copy the 16-character password

# 3. Create config/email.json from the template
Copy-Item config\email.json.template config\email.json

# 4. Edit config/email.json — paste the 16-char password into sender_password

# 5. Test it
python send_test_email.py
```

`config/email.json` is gitignored and will never be committed.

---

## 14. Task Scheduler Setup (pending if not done)

```
Task 1 — Daily cycle
  Trigger:  Daily, 06:00 PKT
  Program:  python
  Args:     E:\SaxoTrNew\SaxoTrNew\atos_runner.py

Task 2 — Intraday monitor (stop-loss watchdog)
  Trigger:  Daily, 15:30 PKT (= 09:30 ET = US market open)
  Program:  python
  Args:     E:\SaxoTrNew\SaxoTrNew\intraday_monitor.py

Tasks 3-7 — Intraday reversion scans
  Triggers: Daily 19:00, 20:30, 22:00, 23:30, 00:30 PKT
  Program:  python
  Args:     E:\SaxoTrNew\SaxoTrNew\atos_runner.py intraday
```

---

## 15. Priority Task List for Next Agent

- [ ] **Task Scheduler — add intraday reversion scans** (Tasks 3-7 above, 5 entries)
- [ ] **instrument_map.csv — add UICs for new tickers:**
  NOW, INTU, BKNG, TJX, MS, BLK, SPGI, CB, TMO, ISRG, DHR, MDT, RTX, DE, UPS, EMR, ITW, PG, KO, PEP
- [ ] **Watch Reversion for 6-8 weeks** on SIM before considering real capital
- [ ] **Strategy comparison review** (after 4-6 weeks): compare Blend vs Reversion P&L,
  WR, drawdown using the dashboard Head-to-Head chart — tighten/loosen/disable accordingly
- [ ] **Purge config/deploy.json from git history** (see SECURITY section at top)
- [ ] **Admin — reset atos_risk_state.json** (permission denied issue):
  ```powershell
  Set-Content -Path "E:\SaxoTrNew\SaxoTrNew\data\atos_risk_state.json" `
    -Value '{"available_cash_sek": 2783, "day_start_equity_sek": 10391109, "last_reset_date": "2026-08-08"}' `
    -Encoding utf8 -Force
  ```
- [ ] **Future option:** Add news API (Polygon.io / NewsAPI) for real-time fundamental
  filtering — currently using free yfinance headlines (keyword scan, ~75% accuracy)

---

## 16. Backtest Results Summary

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
Config:    RSI<33, Dip>5%, Vol>1.5x, Stop4%, 6 max slots (10% of universe)
108-stock backtest: Sharpe 1.11, WR 62.5%, MaxDD 10.3%, CAGR 13.2%, N=24
Honest OOS (61-stock): Sharpe 2.39, WR 70%, MaxDD 5.9%, N=23
VERDICT:   LIVE ON SIM — watch 6-8 weeks before real capital
```

### Futures — 3 Strategies (LIVE ON SIM)
```
Markets:    ES, NQ (CdfOnIndex), GC (FxSpot), CL, ZB (ContractFutures)
Capital:    1% of equity risked per trade, ATR-sized — no fixed budget

Strategy 1 — Donchian Channel (futures/strategy.py)
  Backtest (5y, ETF proxies): Sharpe=0.754, WR=43%, DD=9%, CAGR=8.7%
  Signal freq: ~9/year | Hold: 3-4 weeks | Grid: 180 combos

Strategy 2 — RSI(2) Pullback (futures/strategy_rsi.py)
  Expected: WR ~65-70%, ~42 signals/year | Hold: 5-10 days

Strategy 3 — EMA(5/20) + ADX (futures/strategy_ema.py)
  Expected: WR ~45-50%, ~21 signals/year | Hold: 14-20 days

Combined: ~70 signals/year ≈ weekly frequency
VERDICT: LIVE ON SIM — observe for 8 weeks before allocating more
```

### Forex — 10 Swing Strategies (LIVE ON SIM)
```
Universe:   34 FX pairs across Asian + London sessions
Capital:    1% of equity risked per trade, ATR-sized

Strategy 1 — EMA(5/30) + ADX:    Sharpe=1.619, WR=56%, DD=5%, CAGR=3.9%
             Grid: 288 combos; 116 passed all thresholds
Strategy 5 — Pullback-to-EMA:    WR ~70%+, tight stop at EMA support
Strategy 6 — Weekend Gap Fill:   WR ~80-85%, structural statistical edge
Signal freq: ~8/pair/year × 34 pairs → ~272/year combined
VERDICT: LIVE ON SIM
```

### Forex — London Breakout Day Trading (LIVE ON SIM)
```
Pairs:      EURUSD, GBPUSD, USDJPY, EURGBP, GBPJPY, AUDUSD, USDCAD (7 majors)
Capital:    15,000 SEK dedicated book (independent from swing heat)
Sessions:   London open (07:00 UTC) + NY open (13:00 UTC)
Reference:  Asian H1 range (00:00-06:59 UTC) / London morning H1 range (09:00-12:59 UTC)
R/R:        2:1 (TP = 2× range, SL = range boundary)
Risk:       1.5% per trade, pre-computed units
Range filter: 10-120 pips
Time stop:  20:00 UTC (no overnight holds)
Expected WR: ~58-63%
Scheduled:  lbo-london-open (12:00 PKT), lbo-ny-open (18:00 PKT), lbo-force-close (01:00 PKT)
Test suite: 57/57 tests passing (test_london_breakout.py)
VERDICT: LIVE ON SIM — first live run 2026-08-20
```

### Rejected (do not revisit)
| Strategy | Why |
|---|---|
| OMX30 / CPH25 momentum | 3y Sharpe 1.81 was bull mirage; 10y Sharpe 0.24, MaxDD 44% |
| Per-instrument TA | US Breakout: 1 trade/2y. Weak edge across the board |
| ML probability model | Walk-forward OOS AUC 0.52 = coin flip |
| Residual momentum | Same return as raw momentum; no added value in blend |

---

## 17. Agent Session Log

| Session | Date | Key Work Done |
|---|---|---|
| Agent #1 | 2026-08-03 | Built ATOS v1: universe, features, 5 detectors, decision engine, learner, risk engine, DB, runner |
| Agent #2 | 2026-08-03/04 | Local dashboard, auto-OAuth, placed 4 test orders on Saxo SIM |
| Agent #3 | 2026-08-04 | Fixed dashboard "---" bug, fresh DB, synced 4 Saxo positions |
| Agent #4 | 2026-08-04/06 | ATOS v2+v3: 8 detectors, regime, trailing stops, 6 strategies, backtester, validator, consensus engine |
| Agent #5 | 2026-08-06 | Bug #8 fix — live Saxo API in dashboard, 24h token, live positions |
| Agent #6 | 2026-08-06 | Pre-live audit: fixed cycle-crash, phantom trades, wrong-currency mapping |
| Agent #7 | 2026-08-08 | Strategy pivot: US Blend LIVE (61 stocks, weekly, dynamic 2-8). Intraday stop-loss monitor. Corporate events module. US Reversion: coded, backtested. |
| **Agent #4** | **2026-08-08** | **Honest OOS validation (Sharpe 2.39 OOS). Enabled Reversion on SIM. Trade log CSV + strategy labels. Dynamic capital allocation (50/50). Central config/capital.json. Strategy comparison chart + terminal scorecard. Percentage-based Reversion positions (MAX_UNIVERSE_PCT=0.10). Intraday scanner (atos/intraday_reversion.py) with bad-news + keyword news filters. run_intraday_cycle().** |
| **Agent #4 (cont)** | **2026-08-08** | **Expanded US universe 61->108 solid S&P 500 blue-chips (removed REITs, small E&P, speculative biotech, smaller names). Replaced 3 delisted tickers (HES->APA, K->CAG, BK->TROW). Email notification system (atos/notifier.py): BUY/SELL emails with account balance, weekly Blend rebalance email, Reversion signal email, exit email, Friday P&L report with SVG chart. Signal tickers on dashboard (dashboard_gen.py). Verified email delivery.** |

| **Agent #4 (cont)** | **2026-08-14/15** | **Idempotency fix: REBAL_THRESHOLD=0.10 in plan_rebalance() (prevents sell-rebuy round-trip when runner executes twice). ETF strategy module: saxo_etf_strategy/ — 4 strategies (sector_rotation, risk_off, mean_reversion, dual_ma), conservative risk (15% capital, 5 pos, 8% SL/20% TP), dry_run=True, Task Scheduler at 06:30 PKT. Terminal dashboard: combined stock+ETF trade history table from local CSV + JSON (no Saxo API). DualMA fixed: curated 50-ETF list instead of full 2,122-ETF scan. saxo_client: truncate HTML error bodies, fix raise-None crash on all-rate-limited retries. RSI_ENTRY 33→38 in us_reversion (doubles trade count, Sharpe 1.73).** |

| **Agent #4 (cont)** | **2026-08-17** | **Futures module: 3 strategies (Donchian+RSI(2)+EMA) on ES/NQ/GC/CL/ZB. Multi-strategy runner with `strategy:symbol` position keys. 180-combo Donchian grid (Sharpe=0.754). Forex module: EMA(5/30)+ADX(14) on 7 FxSpot pairs. 288-combo grid (Sharpe=1.619). Mark-to-market backtest equity fix. Vectorized numpy backtester (~50x speedup). Both modules LIVE ON SIM. Updated STRATEGY_NOTES.md and README.md.** |
| **Agent #4 (cont)** | **2026-08-19** | **London Breakout day trading strategy (Strategy 11): H1 Asian/London-morning range break at London open (07:00 UTC) + NY open (13:00 UTC). 7 majors, 2:1 R/R, no overnight holds, 15k SEK dedicated book. DAY_TRADE_STRATEGIES isolation (bypasses swing heat). Pre-computed position sizing in SEK. Email alerts on every open/close (forex/notifier.py). 3 Task Scheduler jobs (lbo-london-open 12:00 PKT, lbo-ny-open 18:00 PKT, lbo-force-close 01:00 PKT). SuperTrend NaN bug fixed (numpy loop). 57-test suite (test_london_breakout.py) 57/57 passing. Docs updated: README.md, docs/forex_strategies.md.** |

**Next agent:** You are Agent #9 (or continuing Agent #4).
- **LBO first live run**: scheduled tasks fire 2026-08-20 at 12:00 PKT — monitor `data/lbo_london.log` and check email for trade alerts
- Futures: CL and ZB UICs change monthly — run `python futures/runner.py --discover` to refresh
- Futures: observe all 3 strategies for 8 weeks before allocating more capital
- Forex swing: all 34 pairs live — observe for 4 more weeks; pnl_tracker shows per-strategy breakdown with `--scan`
- ETF: observe dry-run logs Mon-Fri before flipping `dry_run=False` in `saxo_etf_strategy/config/etf_config.py`
- ETF: sector_rotation exit-on-rank-drop not yet implemented (only SL/TP exits)
- Stocks: token expires every ~24h — run `python set_token.py` to refresh
- Stocks: BF-B, FITB, ETN missing UICs in instrument_map.csv (low priority)
- LBO: after 4+ weeks of live data, run backtests on the same 7 pairs to validate the simulated edge against real Saxo fills

---

## 16. Environment

```
OS:          Windows 10 Pro
Python:      3.11 (python or py -3)
Working dir: E:\SaxoTrNew\SaxoTrNew\
Git:         github.com/trueclickseo-ctrl/saxoTrNew.git (main)
Terminal:    PowerShell (primary) + Git Bash available
Never commit: saxo_token.json, config/deploy.json, config/email.json, data/*_state.json, data/*.db, data/*.log
```
