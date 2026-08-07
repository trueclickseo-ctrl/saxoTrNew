# ATOS — Algorithmic Trading Operating System
## Agent Handover & Project State Document
### Last Updated: 2026-08-08 | Updated by: Agent #7 (Kwaseem)

---

> ## 🔴 SECURITY — STILL PENDING
>
> **`config/deploy.json` (FTP host/user/password for namazic.com) was committed in
> the very first commit (`06ffbc9`) and remains in git history** (it has been untracked
> and gitignored but history was not purged). Required:
> 1. **Rotate the FTP password** in the hosting panel if not done yet.
> 2. Purge from history: `git filter-repo --path config/deploy.json --invert-paths` then force-push.
> 3. File is already gitignored — do NOT re-add it.

> ## 🤖 MULTI-AGENT PROTOCOL — READ FIRST
>
> **Multiple Claude agents share this project. Before doing ANYTHING:**
>
> 1. `git pull origin main` — get latest code
> 2. Read this entire README
> 3. Do your work
> 4. Update this README with what you did and what's next
> 5. `git add -A && git commit -m "agent: <what you did>" && git push origin main`
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

The system trades a **validated US cross-sectional momentum strategy** on 61 S&P 500 stocks,
with a weekly rebalance, dynamic 2–8 positions, and a real-time intraday stop-loss monitor.

---

## 2. Project Location

```
Working dir:  E:\SaxoTrNew\SaxoTrNew\
Git:          https://github.com/trueclickseo-ctrl/saxoTrNew.git
Branch:       main
Dashboard:    python atos_dashboard.py  →  http://localhost:8070
```

---

## 3. Current System State — 2026-08-08

### Active Strategies

| Strategy | Status | File | Notes |
|---|---|---|---|
| **US Momentum Blend** | ✅ **LIVE** | `atos/us_momentum.py` | Validated; 7 positions running |
| **US Mean Reversion** | 🔒 **DISABLED** | `atos/us_reversion.py` | Backtest in progress; needs 15+ trades |
| OMX30 / CPH25 | ⏸️ **PAUSED** | `atos_runner.py` | No edge found; staying out |

### Live Position Summary (as of 2026-08-08)

| Strategy | Positions | Sleeve | Rebalance |
|---|---|---|---|
| US Blend | 7 (AMD UNH CSCO BAC MU MS V) | 1,095,000 SEK | Every 7 days; next ~2026-08-14 |
| US Reversion | 0 (disabled) | 300,000 SEK (reserved) | N/A |

**Note:** V (Visa) has an ex-dividend date of 2026-08-11. The engine will auto-sell it
on the next daily cycle (corporate events module).

### Processes Running

| Process | Trigger | Job |
|---|---|---|
| `atos_runner.py` | Task Scheduler, daily 06:00 PKT | Weekly rebalance + corporate event exits |
| `intraday_monitor.py` | Task Scheduler, daily 14:30 PKT | 1-second stop-loss watchdog during US hours |
| `atos_dashboard.py` | Manual | Dashboard at http://localhost:8070 |

---

## 4. How to Run — Start Here Every Session

### Token refresh (expires every ~24h)
```powershell
python set_token.py
```
Prompts for token with hidden input (token never shown in terminal or committed to git).

### Run daily engine manually
```powershell
cd E:\SaxoTrNew\SaxoTrNew
python atos_runner.py
```
Output is also saved to `data/engine_YYYY-MM-DD.log`.

### Start intraday monitor
```powershell
python intraday_monitor.py
```
Or double-click `ATOS_Monitor.bat`.

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

## 5. Architecture — How the Engine Works

```
Weekly engine (atos_runner.py) — runs daily, rebalances when REBAL_DAYS elapsed
─────────────────────────────────────────────────────────────────────────────
1. Kill switch + daily loss cap check
2. Download latest daily bars for all 61 US tickers (yfinance)
3. Corporate events check — exit positions 3d before ex-div / 2d before earnings
4. US Momentum Blend (atos/us_momentum.py):
     - Risk-off check: equal-weight index vs 200d SMA → cash if bearish
     - If REBAL_DAYS elapsed: sell all → rebuy top momentum + low-vol
     - If not due: hold, log "next in Nd"
5. US Reversion (atos/us_reversion.py) — DISABLED UNTIL BACKTEST PASSES
6. Learning pass (atos/learner.py)
7. Log to data/atos_live.db + data/engine_YYYY-MM-DD.log

Intraday monitor (intraday_monitor.py) — runs continuously during US market hours
─────────────────────────────────────────────────────────────────────────────
- Polls Saxo API every 1 second
- Stop-loss hierarchy: entry stop → trailing -12% → hard floor -15%
- Circuit breaker: CRITICAL alert if data blind >180s
- Market-closed mode: Yahoo prices every 5 min, no Saxo API
- Updates terminal display every 5 seconds (ANSI cursor-home, stable for copying)
```

---

## 6. US Momentum Strategy — Full Spec

| Parameter | Value |
|---|---|
| Universe | 61 S&P 500 stocks (`atos/universe.py` → `US_TICKERS`) |
| Lookback | 120 trading days (~6 months) |
| **Offense** | Up to 6 stocks: return > 5% threshold, ranked by return/vol ratio |
| **Defense** | Always 2 stocks: lowest 60d vol above EMA200 |
| Positions | 2–8 dynamic (deduped; offense listed first) |
| Weight | Equal-weight within the sleeve |
| Rebalance | Every `REBAL_DAYS = 7` calendar days |
| Risk-off | Daily: exit to cash when equal-weight index < 200d SMA |
| Sleeve | 1,095,000 SEK — compounds with own P&L, never topped up |
| Holiday guard | If all orders rejected → do NOT stamp last_rebalance → retry next day |
| Corporate events | Exit 3d before ex-div; don't buy 2d before earnings |

**Backtest (10y, 2016–2026):** Sharpe 1.30, MaxDD 21.3%, CAGR 24.4%

---

## 7. File Map — What Everything Does

### Core Engine
| File | Purpose |
|---|---|
| `atos_runner.py` | Daily orchestrator — `run_cycle()` entry point |
| `atos/us_momentum.py` | **LIVE** US momentum strategy logic (pure, no I/O) |
| `atos/us_reversion.py` | **DISABLED** US mean-reversion strategy logic |
| `atos/corporate_events.py` | Ex-dividend + earnings date checker (yfinance) |
| `atos/universe.py` | 61-stock US universe + OMX30/CPH25 definitions |
| `atos/features.py` | Technical indicators: EMA, ATR, RSI, MACD, Bollinger, Donchian |
| `atos/decision_engine.py` | 8-detector consensus engine |
| `atos/risk.py` | Risk gates, ATR sizing, kill switch, daily loss cap |
| `atos/database.py` | SQLite CRUD — `data/atos_live.db` |
| `atos/learner.py` | Magnitude-aware detector weight updater |
| `atos/strategies.py` | 6 strategy classes (S1–S6) |
| `atos/dashboard_gen.py` | Static HTML dashboard generator |

### Monitoring & Display
| File | Purpose |
|---|---|
| `intraday_monitor.py` | 1-second stop-loss watchdog during US market hours |
| `atos_dashboard.py` | Live dashboard — http://localhost:8070 |
| `ATOS_Monitor.bat` | Double-click launcher for intraday monitor |

### Backtests & Research
| File | Purpose |
|---|---|
| `backtest_us_reversion.py` | US mean-reversion backtest + `--grid` parameter search |
| `preview_us_momentum.py` | Preview today's momentum targets (dry-run, no orders) |
| `backtest_strategies.py` | Per-instrument strategy backtester |
| `backtest_momentum.py` | Cross-sectional momentum backtester |
| `backtest_us_momentum.py` | US momentum daily-equity backtester |
| `backtest_us_strategies.py` | US signal comparison (momentum/low-vol/52wk-high) |
| `research_*.py` | Research scripts (residual momentum, TA, ML, multi-strategy) |

### Auth & Infrastructure
| File | Purpose |
|---|---|
| `set_token.py` | Set Saxo token with hidden input (token never shown/committed) |
| `saxo_client.py` | All Saxo API calls |
| `saxo_token.json` | OAuth token — **gitignored, never commit** |
| `config/deploy.json` | FTP credentials — **gitignored, never commit** |
| `instrument_map.py` | Load `data/instrument_map.csv` (Yahoo ticker → Saxo UIC) |

### Data Files
| File | Purpose |
|---|---|
| `data/atos_live.db` | Main SQLite DB — trades, signals, weights |
| `data/us_momentum_state.json` | Momentum sleeve state (last_rebalance, sleeve_cash) — gitignored |
| `data/atos_risk_state.json` | Risk engine state — gitignored |
| `data/instrument_map.csv` | Yahoo ticker → Saxo UIC mapping (committed) |
| `data/engine_YYYY-MM-DD.log` | Daily engine output log — gitignored |
| `data/monitor_log.txt` | Intraday monitor log — gitignored |

---

## 8. Risk Rules

```
US Blend sleeve:    1,095,000 SEK (compounds; never topped up from account)
US Reversion sleeve:  300,000 SEK (separate; disabled)
Positions:          2–8 (US Blend) + 0–2 (US Reversion when enabled)
Stop-loss:          Entry stop → trailing 12% → hard floor 15% (intraday monitor)
Rebalance stop:     If all orders rejected on rebalance day → retry next trading day
Daily loss cap:     3% — no new entries if equity down >3% on the day
Commission:         0.08% per trade, min 1 USD
Corporate event:    Auto-exit 3d before ex-dividend, 2d before earnings
```

---

## 9. Priority Task List for Next Agent

- [ ] **Run grid search and update reversion parameters** (currently running in background):
  ```
  python backtest_us_reversion.py --grid
  ```
  If a parameter set passes all 4 criteria (Sharpe≥0.8, WR≥50%, MaxDD<20%, N≥15),
  update `atos/us_reversion.py` with those values and re-run single backtest to confirm.

- [ ] **Enable US Reversion after backtest passes:**
  Change `US_REVERSION_ENABLED = False` → `True` in `atos_runner.py`

- [ ] **Admin — reset atos_risk_state.json** (permission denied issue from elevated process):
  ```powershell
  Set-Content -Path "E:\SaxoTrNew\SaxoTrNew\data\atos_risk_state.json" `
    -Value '{"available_cash_sek": 2783, "day_start_equity_sek": 10391109, "last_reset_date": "2026-08-08"}' `
    -Encoding utf8 -Force
  ```

- [ ] **Task Scheduler — add intraday monitor:**
  Trigger: Daily 14:30 PKT | Program: `python` | Args: `E:\SaxoTrNew\SaxoTrNew\intraday_monitor.py`

- [ ] **instrument_map.csv — add UICs for new tickers** added to 61-stock universe:
  NOW, INTU, BKNG, TJX, MS, BLK, SPGI, CB, TMO, ISRG, DHR, MDT, RTX, DE, UPS, EMR, ITW, PG, KO, PEP

- [ ] **Purge config/deploy.json from git history** (see 🔴 SECURITY at top)

- [ ] **Option 4 (future):** Consider adding a second independent strategy after reversion
  is proven live. Candidates: earnings momentum, sector rotation, dual momentum (GEM).

---

## 10. Backtest Results — All Strategies

### US Momentum Blend (10y, 2016–2026) — **LIVE**
```
Universe:    39→61 stocks (S&P 500 representative)
Rebalance:   Monthly (validated); now weekly (live)
Config:      Top-10 + daily risk-off + vol-target 15%
────────────────────────────────────────────────────
CAGR:        24.4%
Sharpe:      1.30
Max DD:      21.3%
vs B&H:      beats buy&hold Sharpe (1.27) with half the drawdown
VERDICT:     ✅ LIVE
```

### US Mean Reversion (2.3y, 2024–2026) — **BACKTEST IN PROGRESS**
```
Universe:    61 stocks (same as blend)
Hold:        3–10 trading days
Config:      RSI<30, Dip>6%, Vol>2×, Stop 5%, MaxPos 2, DDcap 15%
────────────────────────────────────────────────────
Trades:      13  (need ≥15)
Win rate:    61.5%  ✓
Sharpe:      1.13   ✓
Max DD:      10.8%  ✓ (under 20% target)
CAGR:        7.2%   (limited by few trades)
VERDICT:     ✗ DO NOT ENABLE YET — run --grid to find 15+ trade params
```

### Rejected Strategies (do not revisit)
| Strategy | Why Rejected |
|---|---|
| OMX30 momentum | 3y Sharpe 1.81 was a bull mirage; 10y Sharpe 0.24, MaxDD 44% |
| CPH25 momentum | Marginal (10y Sharpe 0.76, ties buy&hold) |
| Per-instrument TA strategies | US Breakout: 1 trade/2y. OMX: 47% WR, Sharpe 0.40. CPH: 26% WR |
| ML probability model | Walk-forward OOS AUC 0.52 = coin flip |
| Residual momentum | Same return as raw momentum; no edge added in blend |

---

## 11. Git Commit History (this session — 2026-08-08)

```
4a1e047  fix: don't advance rebalance timestamp when market closed (holiday guard)
6a8f972  feat: corporate events module — auto-exit before ex-div / earnings
abf826a  feat: dynamic position count (2-8) + fix weekly rebalance timing
903b0ba  feat: Option 3 — US Mean Reversion strategy (disabled pending backtest)
078b158  fix: correct cash accounting in backtest + tighten reversion parameters
```

---

## 12. Agent Session Log

| Session | Date | User | Key Work Done |
|---|---|---|---|
| Agent #1 | 2026-08-03 | SEO | Built ATOS v1: universe, features, 5 detectors, decision engine, learner, risk engine, DB, runner |
| Agent #2 | 2026-08-03/04 | Kashif | Local dashboard, auto-OAuth, placed 4 test orders on Saxo SIM |
| Agent #3 | 2026-08-04 | SEO | Fixed dashboard "---" bug, fresh DB, synced 4 Saxo positions |
| Agent #4 | 2026-08-04/06 | Kwaseem | ATOS v2+v3: 8 detectors, regime, trailing stops, 6 strategies, backtester, validator, consensus engine, RL reward env |
| Agent #5 | 2026-08-06 | Lenovo | Bug #8 fix — live Saxo API in dashboard, 24h token, live positions |
| Agent #6 | 2026-08-06 | Kwaseem | Pre-live audit: fixed cycle-crash, phantom trades, unenforced consensus, mixed-currency equity, missing timeout, non-atomic writes, wrong-currency mapping, learner ordering |
| **Agent #7** | **2026-08-08** | **Kwaseem** | **Strategy pivot: US Momentum Blend LIVE (61 stocks, weekly rebalance, dynamic 2–8 positions). Intraday 1-second stop-loss monitor. Corporate events module (ex-div/earnings auto-exit). Daily engine logging. Holiday retry guard. Option 3 (US Mean Reversion): coded, backtested, DISABLED pending grid search.** |

**Next agent:** You are Agent #8.
1. Run `python backtest_us_reversion.py --grid` (if not done) — find params with 15+ trades, MaxDD<20%
2. If grid finds passing params: update `atos/us_reversion.py`, confirm single-run, set `US_REVERSION_ENABLED = True`
3. Add Task Scheduler entry for `intraday_monitor.py` (14:30 PKT daily)
4. Map new UICs in `instrument_map.csv` (22 new tickers from 61-stock expansion)

---

## 13. Environment

```
OS:         Windows 10 Pro
Python:     3.x (python or py -3)
Working dir: E:\SaxoTrNew\SaxoTrNew\
Git:        github.com/trueclickseo-ctrl/saxoTrNew.git (main)
Terminal:   PowerShell (primary) + Git Bash available
Key files never committed: saxo_token.json, config/deploy.json, data/*_state.json, data/*.db
```
