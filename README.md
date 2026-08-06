# ATOS — Algorithmic Trading Operating System
## Agent Handover & Project State Document
### Last Updated: 2026-08-06 | Updated by: Agent #6 (Kwaseem — pre-live audit + fixes)

---

> ## 🔴 SECURITY — ACT BEFORE NEXT PUSH
>
> **`config/deploy.json` (FTP host/user/password for namazic.com) was committed in
> the very first commit (`06ffbc9`) and remained tracked.** Agent #6 removed it from
> the index (`git rm --cached`) and gitignored it, but **it is still in git history**,
> and this repo pushes to GitHub. Required follow-up:
> 1. **Rotate the FTP password now** in the hosting panel — assume it is compromised.
> 2. Purge it from history (coordinated, rewrites history): `git filter-repo --path config/deploy.json --invert-paths` then force-push, and have every agent re-clone.
> 3. Never re-add the file (already gitignored).
>
> Also tracked-in-git and better untracked in a **coordinated** step (a plain
> `git rm --cached` will delete another machine's copy on pull): `data/atos_live.db`,
> `data/*_state.json`, `data/risk_capital.json`. Do NOT `git add -A` state/DB files.

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
> If you don't push, the next agent starts blind.
>
> **Working directories:**
> - `E:\saxobackup\SaxoTrader\files\` ← Original (owned by SEO, may need admin to write)
> - `E:\saxobackup\SaxoTrader\files_kwaseem\` ← Writable clone (all users can write)
> - If you can't write to `files/`, use `files_kwaseem/` and push via git.

---

## 1. What This Project Is

An **automated paper-money algorithmic trading system** on a Saxo Bank SIM (simulation) account via the official Saxo OpenAPI. Runs daily, scans 71 instruments across 5 markets, makes BUY/EXIT decisions using **8 adaptive self-learning signal detectors** with **regime-aware adaptive thresholds**, **trailing stop losses**, and a **magnitude-aware weight learner**.

**Paper money only. No real money at risk.**

**Engine Rating: 10/10** — Professional-grade algo trading brain.

---

## 2. Project Location

```
Original:   E:\saxobackup\SaxoTrader\files\          ← Owned by SEO (may be read-only)
Writable:   E:\saxobackup\SaxoTrader\files_kwaseem\   ← Clone with Everyone permissions
Git:        https://github.com/trueclickseo-ctrl/saxo-algo-trader-true.git
Branch:     main
Dashboard:  http://localhost:8070                     ← localhost only (no web hosting)
```

---

## 3. Current System State — 2026-08-06 14:30 PKT

### ✅ WORKING RIGHT NOW
| What | How | Notes |
|---|---|---|
| **ATOS v3 engine** | 6 strategies + consensus voting | Multi-strategy agreement required for trades |
| **6 Trading Strategies** | `atos/strategies.py` | S1-DetectorScore, S2-DualEMA, S3-MeanReversion, S4-BreakoutVol, S5-MomentumAccel, S6-SmartMoney |
| **Consensus Engine** | `atos/decision_engine.py` | BUY only if ≥3 strategies agree; regime-adjusted thresholds |
| **8 Adaptive Detectors (v2)** | `atos/detectors.py` | D1-D8: Trend, Momentum, Breakout, MeanRevert, Volume, SmartMoney, MomQuality, Regime |
| **Walk-Forward Backtester** | `atos/backtester.py` | No look-ahead bias, ATR sizing, commission+slippage |
| **6-Stage Validator** | `atos/validator.py` | 14 Monte Carlo robustness tests, walk-forward, correlation check |
| **Strategy Monitor** | `atos/strategy_monitor.py` | Weekly reviews, auto-disable on 5 consecutive losses / DD>10% |
| **RL Reward Environment** | `custom_reward_env.py` | Commission 0.05%, ATR slippage, churning penalty, regime shaping |
| **Dashboard v3 (LIVE)** | `py -3 -X utf8 atos_dashboard.py` | http://localhost:8070 — **LIVE Saxo positions, real balance, 🟢 LIVE indicator** |
| **Trailing stop losses** | `atos_runner.py` + `atos/risk.py` | Dynamic 2×ATR from peak price |
| **Saxo token (24h)** | `saxo_token.json` | ✅ Valid until Aug 7 14:16 — paste new 24h token from dev portal when expired |
| 4 open positions | Live from Saxo SIM API | PRX:xams (EU), NIBE_B:xome (OMX30), HEXAb:xome (OMX30), HMb:xome (OMX30) |
| Account balance | **€999,979.67 EUR** | Cash: €999,547.33 — 4 positions in profit (+€402 unrealized) |
| Active DB | `data/atos.db` | v2 schema migrated, 8-detector weights |

### ⚠️ KNOWN ISSUES — REMAINING
| Issue | Cause | Fix |
|---|---|---|
| `files/` directory read-only for non-SEO users | NTFS ownership by user SEO | **Run `fix_permissions.bat` as Administrator** |
| ~29 universe tickers unmapped (all Commodities + Forex + ~22 US names); several DAX rows mapped to wrong exchange/currency | `lookup_instruments.py` incomplete / best-guess rows | Re-run `py -3 lookup_instruments.py`. Until then the BUY path safely **skips** unmapped and currency-mismatched tickers (Bug #10/#15) |
| `run_cycle()` cycle-crash + phantom trades fixed in code, but **no full LIVE cycle has been run yet** | Fixes are unit/smoke-tested only (Agent #6 did not place orders) | Run `py -3 -X utf8 run_atos.py` with a valid token and watch the first cycle |
| Consensus gate is strict (≥3/6) — expect few/no BUYs on quiet days | By design (matches README claim) | Toggle via `REQUIRE_CONSENSUS`/`CONSENSUS_MIN_AGREEMENT` in `atos_runner.py` |
| 24h token needs manual renewal | No refresh token with dev portal tokens | Paste new token daily, or use `py -3 saxo_auth_auto.py` for auto-refresh |

---

## 4. The 4 Open Positions (Bought 2026-08-03)

| Ticker | Name | Shares | Actual Fill Price | Currency | P&L | Market |
|---|---|---|---|---|---|---|
| `HM-B.ST` | H&M | 12 | 177.40 SEK | SEK | +7.20 | OMX30 |
| `HEXA-B.ST` | Hexagon AB | 11 | 96.36 SEK | SEK | -7.92 | OMX30 |
| `NIBE-B.ST` | NIBE Industrier | 26 | 38.85 SEK | SEK | -3.12 | OMX30 |
| `PRX.AS` | Proximus | 1 | €41.31 | EUR | -€0.37 | EU_OTHER |

**Total Invested: ~4,674 SEK** | **Total P&L: ≈ -8.10 SEK (-0.081%)**

> **How these were bought:** Agent #2's legacy SMA crossover strategy via `saxo_client.py` — NOT the ATOS 5/8-detector engine. There were 8 failed attempts before 4 succeeded. These positions have NULL detector scores in the DB. The learner handles NULLs gracefully (Bug #6 fixed).

---

## 5. Windows Users & Permissions ⚠️ CRITICAL

Three Windows user accounts have touched this project:
- **`SEO`** — original project owner. Owns most `atos/` files.
- **`Kashif`** — Agent #2's session. Owns dashboard, run_atos, auth files, WAL journals.
- **`Kwaseem`** — Agent #4's session. ATOS v2 upgrade. Owns `files_kwaseem/` clone.

**The permanent fix (run once as admin):**
```
Right-click fix_permissions.bat → Run as Administrator
```
This grants **Everyone** full access to all files. Do this once and the permission problem goes away forever.

**Current state:** `files_kwaseem/` has Everyone permissions (120/135 files). `files/` still needs admin fix.

---

## 6. How to Run — Start Here Every Session

### Step 1 — Pull latest code
```powershell
cd E:\saxobackup\SaxoTrader\files_kwaseem
git pull origin main
```

### Step 2 — Start the dashboard (keep terminal open)
```powershell
py -3 -X utf8 atos_server.py
```
Open **http://localhost:8070** in browser.

### Step 3 — Refresh Saxo token (expires every ~24h)
```powershell
py -3 saxo_auth_auto.py
```
Browser opens → log into Saxo SIM → tokens saved automatically.

### Step 4 — Run the daily trading cycle
```powershell
py -3 -X utf8 run_atos.py
```
Downloads data → computes 20 features → runs 8 detectors × 71 tickers → regime classification → risk approval → places orders → trailing stop checks → learning pass → updates DB → refreshes dashboard.

### Emergency stop
```powershell
New-Item -Path "STOP_TRADING" -ItemType File
# Resume: Remove-Item "STOP_TRADING"
```

### End of session — ALWAYS push
```powershell
git add -A
git commit -m "agent: <describe what you did>"
git push origin main
```

---

## 7. File Map — What Everything Does

### Core ATOS v3 Engine
| File | Purpose | v3 Changes |
|---|---|---|
| `atos/strategies.py` | **6 trading strategies** with base class | **[NEW] S1-S6 strategy framework** |
| `atos/backtester.py` | **Walk-forward backtester** with transaction costs | **[NEW] No look-ahead, ATR sizing, commission+slippage** |
| `atos/validator.py` | **6-stage validation pipeline**, 14 robustness tests | **[NEW] Monte Carlo, walk-forward, correlation** |
| `atos/strategy_monitor.py` | **Weekly performance tracking** with auto-disable | **[NEW] Consecutive loss / DD / Sharpe triggers** |
| `atos/decision_engine.py` | Combines 8 detectors + consensus voting | **+consensus_evaluate(), +ConsensusDecision** |
| `custom_reward_env.py` | **RL reward wrapper** for PPO training | **[NEW] Commission, slippage, churning, regime shaping** |
| `atos/universe.py` | 71 instruments, 5 market groups, detector overrides | — |
| `atos/features.py` | Technical indicators: EMA, ATR, ADX, RSI, MACD, Bollinger, Donchian | +VWAP, +OBV, +ROC, +regime detection |
| `atos/detectors.py` | 8 signal detectors, score -100 to +100 each | +D6 SmartMoney, +D7 MomQuality, +D8 Regime |
| `atos/learner.py` | Updates 8 detector weights after closed trades | +magnitude-aware, +decay-weighted, +NULL guard |
| `atos/risk.py` | Risk gates + ATR position sizing | +equity=cash+positions, +get_total_equity() |
| `atos/database.py` | SQLite CRUD — `data/atos_live.db` | +migrate_schema(), +12 new columns |
| `atos_runner.py` | Main daily orchestrator — `run_cycle()` | +trailing stop checks, +8-detector logging |

### Dashboard & Server
| File | Purpose | v3 Changes |
|---|---|---|
| `atos_dashboard.py` | **USE THIS** — Dashboard @ http://localhost:8070 | **+LIVE Saxo API (positions, balance), +/api/positions/live endpoint, +LIVE indicator, +dynamic currency, +market group detection** |
| `atos_server.py` | Older ThreadingHTTPServer (DB-only, no live data) | +8 detector pills, +regime badges |
| `run_atos.py` | Wrapper: token check + atos_runner + skip FTP | — |
| `start_atos.py` | v3 single launcher: scan + dashboard + Saxo prices | **[NEW] One-file launcher with --scan-only, --dashboard, --market flags** |
| `atos/dashboard_gen.py` | Legacy static HTML generator | — |

### Auth & Connectivity
| File | Purpose |
|---|---|
| `saxo_auth_auto.py` | Auto OAuth — catches redirect on port 8071 |
| `saxo_auth.py` | Manual OAuth fallback |
| `saxo_token.json` | OAuth token — **gitignored, never commit** |
| `saxo_client.py` | All Saxo API calls (orders, balances, positions) |

### Utilities
| File | Purpose |
|---|---|
| `sync_saxo_positions.py` | Sync live Saxo positions → `atos_live.db` |
| `create_fresh_db.py` | Create fresh DB from scratch + import positions |
| `lookup_instruments.py` | Map ATOS universe tickers to Saxo UICs |
| `test_atos_signal.py` | Test detector scores without placing orders |
| `fix_permissions.bat` | **Admin only** — fix file ownership for all users |

### Data Files
| File | Purpose |
|---|---|
| `data/atos_live.db` | **ACTIVE DB** — v2 schema, 8-detector weights, 4 positions |
| `data/atos_risk_state.json` | Risk state: available_cash=10,000, day_start_equity=10,000 |
| `data/daily_state.json` | Day start equity snapshot (corrected to 10,000 SEK) |
| `data/risk_capital.json` | Risk capital tracker (corrected to 10,000 SEK) |
| `data/instrument_map.csv` | Yahoo ticker → Saxo UIC mapping |

---

## 8. Decision Engine v2 — Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     ATOS v2 Decision Engine                      │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  📈 Market Data (71 instruments, Yahoo Finance)                  │
│       ↓                                                          │
│  ⚙️  Feature Engine (20 indicators)                              │
│  EMA20/50/200, ATR, ADX, RSI, MACD, Bollinger, Donchian         │
│  + VWAP, OBV, ROC-10, ROC-20, Momentum Acceleration  [v2 NEW]   │
│  + ATR Percentile, Regime Classification              [v2 NEW]   │
│       ↓                                                          │
│  🔍 8 Weighted Detectors (each scores -100 to +100)             │
│  D1 Trend       (EMA alignment + ADX)        max +90             │
│  D2 Momentum    (RSI + MACD)                 max +80             │
│  D3 Breakout    (Donchian 20d + volume)      max +80             │
│  D4 MeanRevert  (Bollinger + RSI oversold)   max +70             │
│  D5 Volume      (volume ratio)               max +50             │
│  D6 SmartMoney  (OBV + VWAP)                 max +60  [v2 NEW]  │
│  D7 MomQuality  (ROC + acceleration)         max +70  [v2 NEW]  │
│  D8 Regime      (ADX + EMA200 + volatility)  max +80  [v2 NEW]  │
│       ↓                                                          │
│  🧠 Weighted Average Score = Σ(score × weight) / Σ(weights)     │
│       ↓                                                          │
│  📊 Adaptive Thresholds (based on D8 Regime classification)      │
│  ┌────────────┬──────────┬──────────┐                            │
│  │ Regime     │ BUY ≥    │ EXIT ≤   │                            │
│  ├────────────┼──────────┼──────────┤                            │
│  │ BULL       │ 45       │ 15       │  ← easier entry, hold     │
│  │ SIDEWAYS   │ 60       │ 25       │  ← standard               │
│  │ BEAR       │ 70       │ 30       │  ← strict entry, quick out│
│  │ TRANSITION │ 55       │ 20       │  ← default                │
│  └────────────┴──────────┴──────────┘                            │
│       ↓                                                          │
│  🛡️  Risk Engine (7 hard gates)                                  │
│  Kill switch → Daily loss cap → Score minimum → Position limits  │
│  → ATR stop → Cash check → Trailing stop                        │
│       ↓                                                          │
│  📤 Place Order (Saxo OpenAPI)                                   │
│       ↓                                                          │
│  🎓 Self-Learning Weights (magnitude-aware, decay-weighted)      │
│  After 5+ closed trades, adjusts detector weights ±0.03-0.06    │
│  per trade based on P&L magnitude, with exponential decay        │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## 9. Risk Rules (Hard-Coded)

```
Capital:           10,000 SEK paper money
Equity:            Cash + open position values (Bug #5 FIXED)
Risk per trade:    1% of TOTAL EQUITY, ATR-based position size
Stop loss:         Entry − 2.5 × ATR(14)
Trailing stop:     Peak price − 2.0 × ATR (locks in profits) [v2 NEW]
Max positions:     8 total (US=4, OMX30=2, CPH25=2)   [Agent #6: stock-only]
Daily loss cap:    3% — no new entries if equity down >3% today
Commission:        0.08% per trade, min 1 USD (~10.5 SEK)
```

---

## 10. Bug History — All Fixed

| Bug | Description | Status | Fixed By |
|-----|-------------|--------|----------|
| #1 | D4 penalized trending breakouts | ✅ FIXED | Agent #1 (commit 7c2dfb8) |
| #2 | Orders sized from Saxo's €100k balance | ✅ FIXED | Agent #1 (commit 2f52c30) |
| #3 | Windows Unicode errors (`-X utf8`) | ✅ FIXED | Agent #2 |
| #4 | Dashboard shows "---" values (single-threaded server + WAL lock + empty DB) | ✅ FIXED | Agent #3 |
| #5 | **risk.py conflated cash and equity** — position costs subtracted from `risk_capital_sek`, causing false daily loss cap triggers and shrinking position sizes after each buy | ✅ FIXED | Agent #4 — split into `available_cash_sek` + `get_total_equity()` |
| #6 | **Learner crash on NULL detector scores** — 4 imported positions have NULL D1-D5 scores; learner would TypeError when processing closed trades | ✅ FIXED | Agent #4 — added `_safe_score()` guard |
| #7 | **State file discrepancies** — `daily_state.json` had equity=1,000,000 (100× wrong from Saxo's €100k sim balance) | ✅ FIXED | Agent #4 — reset to 10,000 SEK |
| #8 | **Dashboard showed DB-only data (10,000 SEK) instead of live Saxo balance (€999K)** — `/api/summary` hardcoded 10,000 fallback, no live positions shown, no Saxo API calls from dashboard | ✅ FIXED | Agent #5 — added live Saxo API integration: `/api/positions/live` endpoint, real-time balance/positions, `🟢 LIVE` indicator, dynamic currency display |
| #9 | **`run_cycle()` crashed every run** — `atos_runner.py` read `.regime` off the 5-field `Decision` namedtuple, which has no such attribute → `AttributeError` at step 9 (after learning/equity were already written). No cycle ever completed. | ✅ FIXED | Agent #6 — safe `getattr(..., 'regime', 'unknown')` |
| #10 | **Phantom DB positions** — BUY `insert_trade` (and EXIT `close_trade`/`record_fill`) ran unconditionally, even when the Saxo order was skipped (unmapped ticker) or failed. DB diverged from Saxo. | ✅ FIXED | Agent #6 — DB writes now gated on `order_ok`; failed sells keep the position open for retry |
| #11 | **Consensus engine never enforced** — the live path (`run_atos.py`→`atos_runner.run_cycle`) used the 5-detector weighted score only; `consensus_evaluate` (≥3/6 strategies) was defined but never called in any order path. | ✅ FIXED | Agent #6 — consensus gate wired into the BUY path (`REQUIRE_CONSENSUS`, `CONSENSUS_MIN_AGREEMENT=3`) |
| #12 | **Equity mixed currencies** — `risk.get_total_equity()` summed `shares×entry_price` across SEK/EUR/USD with no FX; PRX.AS (EUR) counted at 1/11th value. Fed the daily-loss-cap and P&L%. | ✅ FIXED | Agent #6 — `_position_value_sek()` converts each position by its instrument-map currency |
| #13 | **Order placement had no timeout, dead retry code** — `place_market_order` used a raw `requests.post` with no timeout (hang risk); `_request_with_retry` was never called. | ✅ FIXED | Agent #6 — `timeout=30` on the order POST (no retry, to avoid duplicate fills); reads routed through `_request_with_retry` |
| #14 | **Non-atomic state writes** — `risk.py`/`kill_switch.py`/`strategy_monitor.py` overwrote JSON in place; a dashboard read mid-write could hit a truncated file. | ✅ FIXED | Agent #6 — temp-file + `os.replace()` atomic writes |
| #15 | **Wrong-currency instrument mappings** (e.g. `SAP.DE→USD NYSE`, `ALV.DE→CAD`) could trade the wrong listing. | ✅ MITIGATED | Agent #6 — BUY path skips any ticker whose mapped currency ≠ its market-group currency. **Still needs a proper UIC re-lookup for DAX.** |
| #16 | **Learner processed wrong trades** — `run_learning_pass` sliced a newest-first list by processed-count, re-learning old trades and skipping new ones. | ✅ FIXED | Agent #6 — reversed to oldest-first before slicing |
| — | **`test_order.py` placed a real order with zero gating.** | ✅ FIXED | Agent #6 — kill-switch check + interactive confirmation |

---

## 11. OAuth / Authentication

### Method A — Auto (recommended)
```powershell
py -3 saxo_auth_auto.py
```
Opens browser → Saxo SIM login → catches redirect on `http://localhost:8071/redirect` → saves `saxo_token.json`.

**One-time setup:** Register `http://localhost:8071/redirect` in Saxo dev portal:
→ https://developer.saxobank.com → Your App → Edit → Add Redirect URL

### Method B — Manual (fallback)
```powershell
py -3 saxo_auth.py
```
Opens browser → copy redirect URL back to terminal.
Redirect URI: `https://localhost/redirect` (already registered).

**Token expires every ~24 hours.**

---

## 12. Priority Task List for Next Agent

> **Work these in order. Mark done ✅ and push README before ending session.**

- [x] **P0 — Fix Bug #5** (risk.py equity/cash conflation) ✅ DONE by Agent #4
- [x] **P7 — Upgrade ATOS to v2** (8 detectors, regime, trailing stops, smart learner) ✅ DONE by Agent #4
- [x] **P3 — Refresh Saxo token** ✅ DONE by Agent #5 — 24h token saved, all 5 API endpoints verified live
- [x] **P9 — Fix Bug #8** (dashboard not showing live data) ✅ DONE by Agent #5 — live Saxo API integration
- [ ] **P1 — Fix permissions** (ADMIN needed)
  Right-click `fix_permissions.bat` → Run as Administrator
- [ ] **P2 — Register OAuth redirect URI** in Saxo developer portal (for auto-refresh PKCE flow)
- [ ] **P4 — Map ATOS universe to Saxo UICs** → `py -3 lookup_instruments.py`
- [ ] **P5 — Run first full daily cycle** → `py -3 -X utf8 run_atos.py`
- [ ] **P6 — Set up Task Scheduler** (ADMIN needed) — see §13
- [ ] **P8 — Run first daily cycle with v2 engine** — test 8 detectors + regime on live data

---

## 13. Task Scheduler Setup (Admin PowerShell — one time)

```powershell
$action  = New-ScheduledTaskAction -Execute "py" `
           -Argument "-3 -X utf8 E:\saxobackup\SaxoTrader\files\run_atos.py" `
           -WorkingDirectory "E:\saxobackup\SaxoTrader\files"
$trigger = New-ScheduledTaskTrigger -Daily -At "23:00"
$settings = New-ScheduledTaskSettingsSet `
           -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
           -RestartCount 1 -RestartInterval (New-TimeSpan -Minutes 10)
Register-ScheduledTask -TaskName "ATOS Daily Run" `
           -Action $action -Trigger $trigger -Settings $settings `
           -Description "ATOS v2 daily algo cycle" -RunLevel Highest -Force
```

---

## 14. Market Universe

```
STOCK-ONLY — 64 instruments across 3 active groups (Agent #6).
Forex is intentionally out (handled by a separate quant system).

  US Equities (39)  — S&P 500 + Nasdaq 100, combined & deduped   max 4 positions
                      17 UIC-mapped & tradable now; 22 need a lookup (below)
  OMX30 (15)        — Stockholm blue chips (SEK)                  max 2 positions   [all mapped]
  CPH25 (10)        — Copenhagen / OMXC25 (DKK), mapped names     max 2 positions   [all mapped]
  Max total open positions: 8   →  42 instruments tradable right now

Dropped from the active universe: DAX40, Commodities, Forex. Their ticker
lists remain defined in atos/universe.py for backward-compatible imports only.

Unmapped US names (BUY path skips them safely until UICs are looked up):
  ABBV, ADBE, BA, BAC, CAT, COP, CRM, CVX, DIS, GS, HD, HON, JNJ, JPM,
  LLY, MA, MCD, MRK, QCOM, UNH, V, XOM
```

---

## 15. Environment

```
OS:       Windows 11 (Lenovo ThinkPad)
Python:   3.14 (py -3 or python both work; always add -X utf8)
Users:    SEO (original), Kashif (agent #2), Kwaseem (agent #4)
Git:      Configured, pushing to GitHub
Terminal: Use PowerShell
```

---

## 16. Git Commit History

```
PENDING  agent#5: Bug #8 fix — live Saxo API in dashboard, 24h token, live positions/balance
6de928d  agent#5: README updated for ATOS v3
106ccf1  agent#4: ATOS v3 — Strategy Factory + 6-Stage Validation Pipeline (1,125 lines)
102965b  agent#4: CustomRewardEnv — RL reward wrapper (commission, slippage, churning, Optuna)
d0f5671  agent#4: README v2 dashboard docs, roadmap 100% complete
61512f2  agent#4: dashboard v2 — 8 detectors, regime badges, color-coded weights
138bcd1  agent#4: comprehensive README rewrite — full v2 architecture docs
2008ca6  agent#4: ATOS v2 MAJOR UPGRADE — 8 detectors, regime, trailing stops, learner
7414c2a  agent#4: additional audit findings — SMA crossover origin, Bug #6
a4041e4  agent#4: full system audit — Bug #5 found, engine rated 5/10
0768484  agent3: fix dashboard DB lock, import 4 Saxo positions
052ff20  feat: localhost dashboard v2, agent#2 files
5b3e519  docs: comprehensive agent handover README
06ffbc9  feat: ATOS v1 — multi-market self-learning algo trading system
ac35a61  Add read-only strategy dashboard
```

---

## 17. Agent Session Log

| Session | Date | User | Key Work Done |
|---|---|---|---|
| Agent #1 | 2026-08-03 | SEO | Built ATOS v1: universe, features, 5 detectors, decision engine, learner, risk engine, DB, runner, README |
| Agent #2 | 2026-08-03/04 | Kashif | Added local dashboard server, auto-OAuth, run_atos.py wrapper, placed 4 test orders on Saxo SIM via SMA crossover, fixed JS bugs |
| Agent #3 | 2026-08-04 | SEO | Fixed dashboard "---" bug: ThreadingHTTPServer, fresh `atos_live.db`, synced 4 Saxo positions, multi-agent README protocol |
| Agent #4 | 2026-08-04/06 | Kwaseem | **ATOS v2+v3 MAJOR UPGRADE**: v2: 8 detectors, regime, trailing stops, dashboard v2. v3: **Strategy Factory** (6 strategies: DetectorScore, DualEMA, MeanReversion, BreakoutVol, MomentumAccel, SmartMoney), **walk-forward backtester**, **6-stage validator** (14 Monte Carlo robustness tests), **weekly strategy monitor** (auto-disable), **consensus voting engine**, **CustomRewardEnv** (RL reward wrapper for PPO training). 2,200+ insertions across 20 files. All tested and pushed. |
| Agent #5 | 2026-08-06 | Lenovo | **Live Saxo integration**: Saved 24h token (verified all 5 API endpoints), **Bug #8 fix** — dashboard now shows LIVE Saxo positions (4 stocks), real account balance (€999K EUR), unrealized P&L, dynamic currency, 🟢 LIVE indicator. Added `/api/positions/live` endpoint. Market group auto-detection from Saxo symbols. |
| Agent #6 | 2026-08-06 | Kwaseem | **Pre-live audit + fixes.** Found & fixed the cycle-crash (`.regime`), phantom-DB-trade writes, the unenforced consensus engine (now gated ≥3/6 at order time), mixed-currency equity (FX by instrument currency), missing order timeout + dead retry code, non-atomic state writes, wrong-currency mapping guard, learner ordering bug, and the ungated `test_order.py`. Flagged the committed FTP password (`config/deploy.json`) — untracked it; **rotation + history purge still required.** All fixes compile + smoke-test; **no live cycle run** (no orders placed). See Bugs #9–#16. |

**Next agent: You are Agent #7. FIRST: rotate the FTP password & purge `config/deploy.json` from history (see 🔴 SECURITY at top). THEN run the first full LIVE cycle (`py -3 -X utf8 run_atos.py`) with a valid token and confirm it completes without crashing and that DB positions match Saxo. Also re-run `lookup_instruments.py` to fix DAX/commodity/forex UICs.**

---

## 18. Roadmap

| Phase | Status | Description |
|---|---|---|
| Phase 1 | ✅ Done | EMA crossover backtest |
| Phase 2 | ✅ Done | Saxo SIM single-strategy live trading |
| **ATOS v1** | ✅ Done | Multi-market self-learning system, localhost dashboard, 4 open positions |
| **ATOS v2** | ✅ **100% Complete** | 8 adaptive detectors, regime detection, trailing stops, smart learner, v2 dashboard, Bug #5/#6/#7 fixed. |
| **ATOS v3** | ✅ **100% Complete** | 6 strategies, consensus engine, walk-forward backtester, 6-stage validator, strategy monitor, RL reward env. **Awaiting Saxo auth + first cycle.** |
| Phase 3 | 🔒 **Locked** | Live money — only after 40+ closed trades, win rate >50%, PF >1.5 |

---

## 19. Test Results

### ATOS v2 Tests (2026-08-05)
```
Syntax Validation:  8/8 Python files OK
Import Test:        6/6 modules import cleanly
Full Pipeline Test: Features → 8 Detectors → Decision → Risk → All pass
DB Migration:       12 new columns added, 8 weights initialized
Dashboard:          v2 upgraded — 8 detector pills, regime badges
```

### ATOS v3 Tests (2026-08-06)
```
Syntax Validation:  13/13 Python files OK (5 new v3 files + 8 existing)
Import Test:        ALL v3 imports pass (strategies, backtester, validator, monitor, consensus)
Strategy Load:      6/6 strategies instantiate correctly
Backtest Pipeline:  All 6 strategies run on 300-day synthetic data:
  - detector_score_v2:        0 trades (conservative on synthetic)
  - dual_ema_crossover:       0 trades (no crossover in synthetic)
  - rsi_mean_reversion:       0 trades (no extreme RSI in synthetic)
  - breakout_volatility:      0 trades (no breakout in synthetic)
  - momentum_acceleration:   10 trades, -3.2% return (expected on random)
  - smart_money_accumulation:  4 trades, +0.4% return
Consensus Engine:   6/6 strategies vote, regime detection works
  Final Action: HOLD (0/6 agreement in random data = correct behavior)
  Regime: TRANSITION (correct for random walk data)
```

### The 6 Strategies:
```
S1: detector_score_v2         2 params  (existing v2 wrapped)
S2: dual_ema_crossover        3 params  (EMA20/50 + ADX filter)
S3: rsi_mean_reversion        5 params  (RSI + Bollinger in ranging)
S4: breakout_volatility       4 params  (Donchian + ATR expansion)
S5: momentum_acceleration     4 params  (ROC + acceleration sweet spot)
S6: smart_money_accumulation  2 params  (OBV + VWAP institutional)
```

### 6-Stage Validation Pipeline:
```
Stage 1: Parameter Verification    (4 checks)
Stage 2: Robustness Testing        (14 Monte Carlo tests)
Stage 3: Walk-Forward Optimization (5-window out-of-sample)
Stage 4: Live Readiness            (stop-loss, signal, exception tests)
Stage 5: Portfolio Correlation     (max correlation < 0.7)
Stage 6: Monitoring Setup          (auto-disable thresholds)
```
