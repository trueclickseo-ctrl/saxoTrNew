# IBKR Paper Quant Bot — Complete Reference

**Account:** DUR952126 (SEK-denominated)  
**Platform:** Interactive Brokers via IB Gateway  
**Status:** PAPER — simulated trades, no real money  
**Funded:** SEK 250,000  
**Last verified:** 2026-09-26  
**Live account docs:** [ibkr_live_bot.md](ibkr_live_bot.md)

---

## Table of Contents

1. [Account & Gateway Setup](#1-account--gateway-setup)
2. [Architecture Overview](#2-architecture-overview)
3. [Paper Strategies](#3-paper-strategies)
4. [Database (ibkr_stocks.db)](#4-database-ibkr_stocksdb)
5. [Configuration (ibkr_config.json)](#5-configuration-ibkr_configjson)
6. [Task Scheduler](#6-task-scheduler)
7. [Commands & CLI Reference](#7-commands--cli-reference)
8. [Paper vs Live — Key Differences](#8-paper-vs-live--key-differences)
9. [Evolver Gate — Trade Counts](#9-evolver-gate--trade-counts)
10. [Promoting a Strategy to Live](#10-promoting-a-strategy-to-live)
11. [AI Copilot — Stock Learning Module](#11-ai-copilot--stock-learning-module)

---

## 1. Account & Gateway Setup

| Parameter       | Value |
|-----------------|-------|
| Account ID      | `DUR952126` |
| Gateway port    | `4002` |
| Currency        | SEK |
| DB file         | `data/ibkr_stocks.db` (default when `IBKR_DB_PATH` not set) |
| Gateway config  | `config.ini` |
| Gateway startup | `StartGateway.bat` |

### Gateway Requirements

- Second IB Gateway instance running on port 4002 (separate from live on 4001).
- **Read-Only API must be unchecked** in Gateway settings.
- Paper Gateway can restart freely — no real money at risk.
- Paper fills are simulated by IBKR's paper trading engine (price-dependent).

### Environment Variable

Paper is the **default** — `ibkr_state.py` defaults to `data/ibkr_stocks.db` when `IBKR_DB_PATH` is not set.

To explicitly target paper:
```
# Either unset the var, or set it explicitly:
set IBKR_DB_PATH=data/ibkr_stocks.db
```

Do **not** set `--live` flag when running paper tasks. The `--live` flag switches the DB to `data/ibkr_live_stocks.db` and targets account U28013794.

---

## 2. Architecture Overview

The paper account shares the same codebase as live:
- `ibkr_module/ibkr_executor.py` — all strategy executors
- `ibkr_module/ibkr_client.py` — IB Gateway API wrapper
- `ibkr_module/ibkr_state.py` — SQLite ledger (`data/ibkr_stocks.db`)
- `ibkr_module/ibkr_signals.py` — Yahoo Finance signal generators
- `run_ibkr_stocks.py` — CLI entry point (no `--live` flag = paper)

**What differs from live:**
- DB file: `ibkr_stocks.db` vs `ibkr_live_stocks.db`
- Gateway port: `4002` vs `4001`
- Account ID: `DUR952126` vs `U28013794`
- Strategies: paper runs all 9 strategies (11 sub-strategies: blend, reversion, intraday, signals×4, scorer swing, scorer portfolio, penny, bagger — signals counts as 4, scorer as 2); live runs only blend + reversion
- Budgets: paper uses larger paper budgets ($50k per strategy); live uses real capital (~$2k each)
- AI Copilot: applies to paper (shadow mode active); live blend has no copilot (the ranker IS the AI layer)

---

## 3. Paper Strategies

All strategies run on paper unless explicitly marked as live-promoted.

### 3.1 US Blend (Cross-Sectional Momentum)

Same algorithm as live. Paper uses larger budget and more slots.

| Parameter | Paper | Live |
|-----------|-------|------|
| Budget | $50,000 | $2,000 |
| Max positions | 10 | 5 |
| Stop | 8% ATR Chandelier | 8% ATR Chandelier |
| Rebalance guard | 14 days | 14 days |

**Purpose on paper:** Validates signal quality and stop behaviour before relying on it with real capital.

---

### 3.2 US Reversion (Mean Reversion)

Same algorithm as live. Paper uses larger budget and more slots.

| Parameter | Paper | Live |
|-----------|-------|------|
| Budget | $50,000 | $2,000 |
| Max slots | 10 | 3 |
| Stop | 3% fixed | 3% fixed |
| Max hold | 10 business days | 10 business days |

---

### 3.3 Intraday Reversion

**Status:** Paper-only. Not promoted to live.

- Uses 5-minute Yahoo Finance bars (US market hours only)
- Same screening logic as daily reversion but on intraday dips
- Only fires during 09:30–16:00 ET
- clientId: `12`

**Config:**
```json
"reversion": {
    "stop_pct": 0.03,
    "max_slots": 10,
    "rsi_entry": 38,
    "rsi_exit": 60
}
```

---

### 3.4 US Signals (4 Sub-Strategies)

**Status:** Paper-only. Not promoted to live.

Four independent signal strategies sharing the same `signals` executor:

| Sub-strategy | Logic |
|--------------|-------|
| SMA Crossover | 50-day SMA crosses above 200-day SMA |
| RSI Reversal | RSI(14) oversold then rebounds above 40 |
| Momentum | 6-month rate-of-change leaders |
| Ensemble | Weighted average of the three above |

**Config:**
```json
"signals": {
    "slot_usd": 2000,
    "max_per_strategy": 7,
    "min_trade_usd": 50
}
```

Budget: $2,000/slot × up to 7 slots per sub-strategy = $14,000 max per sub-strategy, $56,000 across all four.

clientId: `16`

---

### 3.5 ATOS US 500 Scorer

**Status:** Paper-only. Not promoted to live.

- Scores 491 US stocks using the ATOS scoring engine
- Buys top-ranked candidates split into two sleeves:

| Sleeve | Budget | Slots | Stop | Min Score |
|--------|--------|-------|------|-----------|
| Swing | $30,000 | 12 | 4% | 65.0 |
| Portfolio | $30,000 | 15 | 8% | 65.0 |

- `--exits` re-scans and closes positions whose score dropped below minimum
- clientId: `17`

---

### 3.6 Penny Stocks

**Status:** Paper-only (SIM-ONLY gate in executor). Will **never** be promoted to live.

- Sub-$2.20 momentum breakout stocks
- Universe: `PENNY_TICKERS` from `atos/universe.py`
- Hard gate in `run_ibkr_stocks.py`: `--live` flag blocks this strategy

| Parameter | Value |
|-----------|-------|
| Budget | $10,000 |
| Max slots | 5 |
| Stop | 12% |
| Target | 25% |
| Max hold | 15 days |
| Max price | $2.20 |

clientId: `19`

---

### 3.7 Bagger

**Status:** Paper-only (SIM-ONLY gate in executor). Will **never** be promoted to live.

- Multi-cap stocks with 80%+ 6-month rate-of-change (trend continuation)
- 12% trailing stop — **software-managed**, no broker GTC stop order
- Dashboard shows trailing high and software stop level

| Parameter | Value |
|-----------|-------|
| Budget | $7,000 |
| Max slots | 5 |
| Trailing stop | 12% |
| Max hold | 60 days |

clientId: `21`

---

## 4. Database (ibkr_stocks.db)

**Path:** `data/ibkr_stocks.db` (default; no env var needed)

Same schema as the live DB — see [ibkr_live_bot.md §6](ibkr_live_bot.md#6-database-ibkr_live_stocksdb) for full schema.

**Strategy column values:** `blend`, `reversion`, `intraday`, `signals_sma`, `signals_rsi`, `signals_momentum`, `signals_ensemble`, `scorer_swing`, `scorer_portfolio`, `penny`, `bagger`

Each strategy's `get_open_positions("strategy_name")` call is isolated — strategies do not see each other's positions.

---

## 5. Configuration (ibkr_config.json)

**Path:** `ibkr_module/config/ibkr_config.json`

The config file is shared between live and paper. The `"paper": true` field is used by `run_ibkr_stocks.py` but the actual account switch is done by the `--live` CLI flag.

**Client IDs (paper tasks use the non-`_live` IDs):**

| Strategy | clientId |
|----------|----------|
| blend | 10 |
| reversion | 11 |
| reversion_v2 | 18 |
| intraday | 12 |
| trail | 13 |
| info / positions | 14 |
| signals | 16 |
| scorer | 17 |
| penny | 19 |
| bagger | 21 |
| dashboard | 96 |

---

## 6. Task Scheduler

Paper tasks fire on the same schedule as live but use different `.bat` files (no `--live` flag).

| Time (PKT) | Task | clientId |
|------------|------|----------|
| 19:30 | Blend sells-only (paper) | 10 |
| 20:35 | Reversion exits (paper) | 11 |
| 20:35 | Reversion entries (paper) | 11 |
| 20:35 | Signals exits + entries (paper) | 16 |
| 21:00 | Trail stops (paper) | 13 |
| 21:00 | Blend buys-only (paper) | 10 |
| 21:00 | Penny entries (paper) | 19 |
| Various | Scorer rescan (paper, weekly) | 17 |

---

## 7. Commands & CLI Reference

```bash
# Paper account -- omit --live (paper is the default)
python run_ibkr_stocks.py                                     # blend dry-run
python run_ibkr_stocks.py --execute                           # blend execute
python run_ibkr_stocks.py --execute --auto                    # blend execute, no prompts

python run_ibkr_stocks.py --strategy reversion                # reversion dry-run
python run_ibkr_stocks.py --strategy reversion --execute --auto
python run_ibkr_stocks.py --strategy reversion --exits --execute --auto

python run_ibkr_stocks.py --strategy intraday --execute --auto
python run_ibkr_stocks.py --strategy signals --execute --auto
python run_ibkr_stocks.py --strategy signals --exits --execute --auto
python run_ibkr_stocks.py --strategy scorer --execute --auto
python run_ibkr_stocks.py --strategy scorer --exits --execute --auto  # rescan
python run_ibkr_stocks.py --strategy penny --execute --auto
python run_ibkr_stocks.py --strategy bagger --execute --auto

python run_ibkr_stocks.py --strategy all --execute --auto     # all strategies

python run_ibkr_stocks.py --trail-stops --execute
python run_ibkr_stocks.py --positions
python run_ibkr_stocks.py --info

# Dashboard (paper Gateway on port 4002)
python ibkr_dashboard.py
python ibkr_dashboard.py --once --client-id 88
```

---

## 8. Paper vs Live — Key Differences

| Aspect | Paper (DUR952126) | Live (U28013794) |
|--------|-------------------|------------------|
| Gateway port | 4002 | 4001 |
| DB file | `ibkr_stocks.db` | `ibkr_live_stocks.db` |
| CLI flag | _(none)_ | `--live` |
| Strategies | All 9 groups (blend, reversion, intraday, signals×4, scorer swing, scorer portfolio, penny, bagger — 11 sub-strategies total) | blend + reversion only |
| Capital | $50k paper per main strategy | ~$2k real per strategy |
| Fill simulation | IBKR paper engine (simulated) | Real market fills |
| AI Copilot | Active (shadow + apply mode) | Observation-only on live blend |
| Purpose | Strategy validation, A/B testing | Real capital deployment |

**Fill quality difference:** Paper fills are simulated. Slippage, partial fills, and liquidity constraints are not accurately modelled. A strategy that looks good on paper may behave differently live due to fill quality, especially on thinly-traded stocks.

---

## 9. Evolver Gate — Trade Counts

The AI Strategy Evolver requires **30 closed trades** per strategy before it can generate meaningful parameter improvements. Below are counts as of **2026-09-25** from `data/ibkr_stocks.db` (IBKR paper) and `data/pnl_ledger.db` (Saxo/all modules).

### IBKR Paper Account (DUR952126)

| Strategy | Closed | Gate (30) | Status | ETA |
|----------|--------|-----------|--------|-----|
| scorer_portfolio | 21 | 30 | need 9 more | ~1 month |
| scorer_swing | 14 | 30 | need 16 more | ~2 months |
| blend | 10 | 30 | need 20 more | ~3 months |
| blend_v2 | 9 | 30 | need 21 more | ~3 months |
| reversion | 8 | 30 | need 22 more | ~3 months |
| US RSI Reversal | 6 | 30 | need 24 more | ~4 months |
| US Ensemble | 6 | 30 | need 24 more | ~4 months |
| US SMA Crossover | 1 | 30 | need 29 more | ~5 months |

> **scorer_portfolio is closest** — could hit 30 by late October 2026 if the current pace holds (~4 trades/week).

### Saxo/Forex Strategies (pnl_ledger.db) — Already READY

These strategies have already crossed the 30-trade gate and can have the evolver run on them now:

| Strategy | Closed | Notes |
|----------|--------|-------|
| ETF Rotation | 1,346 | Well past gate |
| gap | 425 | Well past gate |
| ml | 397 | Well past gate |
| rsi | 293 | Well past gate |
| pullback | 203 | Well past gate |
| donchian | 116 | Well past gate |
| ema | 92 | Well past gate |
| cnn_lstm | 84 | Well past gate |
| US Blend (Saxo) | 72 | Well past gate |
| ema_trend | 61 | Well past gate |
| donchian_ai | 60 | Well past gate |
| bb | 54 | Well past gate |
| zscore_quality | 42 | Well past gate |
| zscore | 42 | Well past gate |
| US Reversion (Saxo) | 39 | Well past gate |
| bb_quality | 37 | Well past gate |
| advanced_ml | 31 | Just crossed |
| supertrend | 30 | Just crossed |

### Near the Gate

| Strategy | Closed | Needs |
|----------|--------|-------|
| rsi_trend | 22 | 8 more |
| advanced_pullback_master | 20 | 10 more |
| london_breakout | 19 | 11 more |

### How to Run the Evolver

Check gate status (free, no AI API call):
```bash
python ai/strategy_evolver.py --dry-run
```

Run the evolver on all strategies that have crossed the gate:
```bash
python ai/strategy_evolver.py
```

The evolver runs automatically every Monday at 01:30 PKT via Task Scheduler. Manual runs are safe (idempotent).

### Updating This Table

Re-run the trade counter to refresh counts:
```bash
python -X utf8 "C:\Users\Kwaseem\AppData\Local\Temp\claude\...\scratchpad\count_trades2.py"
```

Or ask Claude: **"count all strategy trades"** — it will re-query the databases and update this section.

---

## 10. Promoting a Strategy to Live

Before promoting a paper strategy to the live account:

1. **Minimum trades:** At least 30 closed trades on paper with consistent positive expectancy (win rate > 50%, profit factor > 1.3)
2. **Drawdown check:** Max drawdown on paper < 20% of allocated capital
3. **Manual dry-run first:** Run against live Gateway with `--live` and no `--execute` — verify the signal and sizing look correct
4. **Budget decision:** Decide the live budget (typically much smaller than paper — start conservatively)
5. **Add live config block:** Add a `"live_<strategy>"` section to `ibkr_config.json` with the live budget
6. **Unlock the live gate** in `run_ibkr_stocks.py`: add the strategy name to the live-allowed list
7. **Add tests:** Extend `test_ibkr_live_module.py` Section B with a dry-run integration test for the new live strategy
8. **Create `.bat` and Task Scheduler entry:** Assign a unique clientId from the `_live` range (20+)
9. **Update both docs:** Add to live doc strategy section; note promotion date in paper doc

> Current live-allowed strategies: `blend`, `reversion`.  
> Penny and bagger are **permanently paper-only** (hard-coded SIM-ONLY gate in executor).

---

## 11. AI Copilot — Stock Learning Module

The AI Copilot observes every IBKR trade (paper and live) and learns from outcomes, building a Stock Outcome Predictor that will eventually score new buy proposals before they execute.

### 11.1 What the AI Layer Does (Now)

| Component | What it does | Status |
|-----------|-------------|--------|
| Observation cards | Writes an entry card on every BUY fill, exit card on every SELL fill | **ACTIVE** (since commit `0114a06`) |
| Stock Outcome Predictor | GradientBoosting model trained on closed cards | Enabled but waiting for gate (need 50 cards) |
| Proposal scoring | Adds `stock_top_win_prob` to each buy proposal | Starts once model trains |
| Journal | AI retrospective on closed trades | Enabled for `stocks` block |
| Copilot APPROVE/REJECT | LLM evaluates buy proposals | Shadow-only on paper; **never** on live |

**Governance rule:** `can_apply_decision("ibkr_paper")` = **always False** in code. IBKR accounts are not in `_AI_ACTING_ACCOUNTS`. The AI can observe, learn, and log opinions on any account — it can never place, resize, or skip an order.

---

### 11.2 Observation Card Architecture

Every IBKR fill writes to `data/stock_observation_cards.jsonl` — the same file the Saxo SIM/live sleeves use. Account environments are kept separate via the `account_env` prefix in `card_id`:

| account_env | Where | Currency |
|-------------|-------|----------|
| `sim` | Saxo SIM (atos_runner.py) | SEK |
| `live_stocks` | Saxo real-money US Blend (atos_live_stocks.py) | SEK |
| `ai_sim` | AI-decision SIM twin (atos_ai_stocks.py) | SEK |
| `ibkr_paper` | IBKR paper (DUR952126) | USD |
| `ibkr_live` | IBKR live (U28013794) | USD |

**Card ID format:** `account_env:strategy:ticker:entry_date`  
Example: `ibkr_paper:us_blend:AAPL:2026-09-25`

This deterministic key lets the exit hook reconstruct the card_id without a DB lookup — the two separate processes (entry pass at 21:00, sell pass at 19:30 next day) never need to coordinate.

**Two card modules:**

| Module | Used by | Currency | EUR conversion |
|--------|---------|----------|---------------|
| `ai/features/stock_cards.py` | Saxo SIM + live_stocks | SEK | Yes (via sek_per_eur) |
| `ai/features/ibkr_stock_cards.py` | ibkr_executor.py (paper + live) | USD | None (no FX in executor) |

---

### 11.3 What Gets Logged Per Trade

**Entry card fields (written at BUY fill):**
```jsonc
{
  "card_id":          "ibkr_paper:us_blend:ROKU:2026-09-25",
  "event":            "entry",
  "market":           "equity",
  "account_env":      "ibkr_paper",
  "strategy":         "us_blend",          // or "us_reversion"
  "symbol":           "ROKU",
  "direction":        "BUY",
  "entry_price":      153.79,
  "current_stop":     141.49,              // GTC stop price
  "quantity":         4,
  "native_currency":  "USD",
  "risk_native":      49.20,               // (entry - stop) * shares in USD
  "risk_eur":         null,                // no FX in IBKR executor
  "rsi_at_entry":     null,                // populated by reversion; null for blend
  "atr_at_entry":     null
}
```

**Exit card fields (written at SELL fill):**
```jsonc
{
  "card_id":          "ibkr_paper:us_blend:ROKU:2026-09-25",
  "event":            "exit",
  "exit_price":       161.20,
  "exit_reason":      "blend_rebalance",   // or "stop_loss", "rsi_exit", "max_hold"
  "native_currency":  "USD",
  "net_pnl_native":   29.64,               // USD
  "gross_pnl_eur":    null,
  "net_pnl_eur":      null,
  "r_multiple":       0.60,                // net_pnl / risk at entry
  "holding_hours":    312.5
}
```

---

### 11.4 The 4 Logging Hooks in ibkr_executor.py

Added at commit `0114a06` — each fires only when `ai_config.stocks_enabled(account_env)` returns True:

| Hook | Trigger | Card type |
|------|---------|-----------|
| Blend BUY | After `st.update_stop()` in `run_rebalance()` | Entry |
| Blend SELL | After `_email_ibkr_fill("SELL", ...)` in `run_rebalance()` | Exit |
| Reversion BUY | After `st.update_stop()` in `run_reversion_entries()` | Entry |
| Reversion SELL | After `_email_ibkr_fill("SELL", ...)` in `run_reversion_exits()` | Exit |

The gate is `ai_config.stocks_enabled("ibkr_paper")` (or `"ibkr_live"` for live). This maps to the `stocks` block in `config/ai.json` (currently `enabled: true`). Set `stocks.enabled: false` to disable all card writing from IBKR trades.

---

### 11.5 Stock Outcome Predictor

**File:** `ai/models/stock_outcome_predictor.py`  
**Config block:** `config/ai.json → stock_outcome_predictor.enabled: true`  
**Model output:** `data/stock_outcome_model/model.pkl` + `report.json`  
**Retrain schedule:** Daily at 22:00 PKT via Task Scheduler  

**Training label:** `r_multiple > 0` (binary: profitable = 1, loss = 0)

**Feature set:**

| Feature | Source |
|---------|--------|
| `rsi14` | RSI(14) at entry (from entry card; 0 for blend) |
| `adx` | ADX from regime classifier (from trade proposal if available) |
| `daily_vol_pct` | 20-day realised daily vol % (ATR proxy) |
| `stop_pct` | `abs(entry - stop) / entry × 100` |
| `target_pct` | `abs(sma20_target - entry) / entry × 100` |
| `risk_reward` | `target_pct / stop_pct` |
| `n_open_positions` | Portfolio crowding at entry time |
| `day_of_week` | 0=Mon … 4=Fri |
| `ticker_win_rate` | Historical WR for this ticker+strategy (rolling) |
| `ticker_n_closed` | Sample size backing the WR |
| `strategy` (one-hot) | us_reversion / us_blend / unknown |
| `regime_label` (one-hot) | TRENDING_BULLISH / RANGING / etc |

**Model training split:** 70% chronological train / 30% test  
**Algorithm:** GradientBoostingClassifier (200 trees, max_depth=3, lr=0.05)

**Gate check and training:**
```bash
# Check gate status (free, no API call)
python ai_stock_outcome_predictor.py --status

# Force a training run (safe to run any time)
python ai_stock_outcome_predictor.py --train
```

**Current status (2026-09-25):**

| Metric | Value |
|--------|-------|
| Closed cards available | ~43 |
| Gate | 50 |
| Cards still needed | ~7 |
| Model trained | No |
| ETA | ~1–2 weeks (a few more paper closes) |

Once trained, every new stock buy proposal will include a `stock_top_win_prob` field showing the model's estimated probability that the trade will be profitable.

---

### 11.6 AI Config — IBKR Stock Flags

All flags live in `config/ai.json`. Reads are live (no restart needed — every scan cycle re-reads the file).

```jsonc
// Master switch for IBKR paper + all SIM stocks AI observation
"stocks": {
    "enabled": true,                  // master on/off for card writing + journal
    "shadow_mode": true,              // true = observe only; false = copilot can skip/resize SIM trades
    "journal": true,                  // feed closed stock trades to AI Journal
    "shadow_copilot_reversion": false,// LLM scores every US Reversion candidate (paid call; set true when ≥40 reversion cards)
    "shadow_copilot_signals": false,  // LLM scores US Signals entries (too few trades yet)
    "basket_ranker_blend": true       // LLM ranks US Blend offense basket (1 call/rebalance)
}

// Dedicated block for Saxo real-money stocks sleeve
"stocks_live": {
    "enabled": true,
    "journal": true,
    "basket_ranker_blend": true
}

// Stock outcome predictor
"stock_outcome_predictor": {
    "enabled": true,                  // enabled; will train once 50 cards close
    "min_samples": 50
}
```

**Activation logic for IBKR accounts:**

| Account env | `stocks_enabled()` maps to | Can apply decision |
|-------------|---------------------------|-------------------|
| `ibkr_paper` | `stocks` block | **Never** (hardcoded in `_AI_ACTING_ACCOUNTS`) |
| `ibkr_live` | `stocks` block | **Never** |

The `_AI_ACTING_ACCOUNTS` set in `ai/config.py` contains only `{"sim", "ai_sim"}`. No IBKR account can ever be promoted to this set without a code change + written go/no-go review.

---

### 11.7 AI Phase Roadmap for IBKR Stocks

| Phase | What | Gate | Status |
|-------|------|------|--------|
| **A** | Observe + log all fills to cards | On since `0114a06` | **ACTIVE** |
| **A2** | Stock Outcome Predictor trains | 50 closed cards (~7 more) | Waiting |
| **B** | Copilot APPROVE/REJECT on paper (shadow log only) | Gate 2 passes (APPROVE avg > REJECT avg) | Not yet |
| **C** | Counterfactual A/B review | 3 weeks of B data | Not yet |
| **D** | Copilot applies on paper (skip/resize) | Written go/no-go + `shadow_mode: false` | Not yet |
| **E** | Any live autonomy | Separate written decision + code change | Never automated |

**To flip Phase B (enable copilot logging on paper):**
```jsonc
// config/ai.json
"stocks": {
    "shadow_copilot_reversion": true   // or "shadow_copilot_signals": true
}
```
This only turns on **logging** of copilot opinions. No orders are changed until Phase D.

**To flip Phase D (copilot acts on SIM paper):**
```jsonc
"stocks": {
    "shadow_mode": false   // only affects sim; never affects ibkr_paper or ibkr_live
}
```

---

### 11.8 Monitoring Card Accumulation

Check card counts across all accounts:
```bash
python -X utf8 -c "
import json; from collections import defaultdict
by_env = defaultdict(int); by_event = defaultdict(int)
with open('data/stock_observation_cards.jsonl') as f:
    for line in f:
        c = json.loads(line)
        by_env[c.get('account_env','?')] += 1
        by_event[c.get('event','?')] += 1
print('By account_env:')
[print(f'  {k}: {v}') for k,v in sorted(by_env.items())]
print('By event:')
[print(f'  {k}: {v}') for k,v in sorted(by_event.items())]
"
```

Check predictor gate status:
```bash
python ai_stock_outcome_predictor.py --status
```

Check model report after training:
```bash
python -c "import json; print(json.dumps(json.load(open('data/stock_outcome_model/report.json')), indent=2))"
```
