# ATOS AI Roadmap

Planning discussion held 2026-08-26. **Not started yet** — explicit user decision to begin building this on **Friday, 2026-08-28**, one item at a time, tested on SIM first before any of it touches either LIVE account (SEK or EUR).

---

## Full feature wishlist (user's list, 2026-08-26)

| # | AI Feature | What it does | Priority (user) |
|---|---|---|---|
| 1 | Market Regime Detection | Trend / range / high-volatility / low-volatility | ⭐⭐⭐⭐⭐ |
| 2 | AI Signal Scoring | Scores each trade 0–100 | ⭐⭐⭐⭐⭐ |
| 3 | Strategy Selection | Chooses the best strategy for current conditions | ⭐⭐⭐⭐⭐ |
| 4 | Trade Quality Prediction | Estimates probability of successful trade | ⭐⭐⭐⭐⭐ |
| 5 | AI Trade Veto | Blocks poor-quality signals | ⭐⭐⭐⭐⭐ |
| 6 | Dynamic Position Sizing | Adjusts risk according to conditions | ⭐⭐⭐⭐ |
| 7 | Entry Optimization | Finds better entry timing | ⭐⭐⭐⭐ |
| 8 | Exit Optimization | Determines whether to hold, reduce or exit | ⭐⭐⭐⭐⭐ |
| 9 | Stop-Loss Optimization | Suggests adaptive stop distance | ⭐⭐⭐⭐ |
| 10 | Take-Profit Optimization | Estimates optimal target | ⭐⭐⭐⭐ |
| 11 | Volatility Prediction | Forecasts upcoming volatility | ⭐⭐⭐⭐⭐ |
| 12 | Anomaly Detection | Detects abnormal market behavior | ⭐⭐⭐⭐⭐ |
| 13 | Correlation Intelligence | Detects hidden portfolio concentration | ⭐⭐⭐⭐ |
| 14 | News/Event Intelligence | Detects important economic events/news | ⭐⭐⭐⭐ |
| 15 | Sentiment Analysis | Analyzes market/news sentiment | ⭐⭐⭐ |
| 16 | Trade Management AI | Monitors open trades continuously | ⭐⭐⭐⭐⭐ |
| 17 | Portfolio Optimization | Determines allocation across instruments | ⭐⭐⭐⭐ |
| 18 | AI Trading Journal | Explains why trades succeeded/failed | ⭐⭐⭐⭐⭐ |
| 19 | Strategy Discovery | Finds new patterns/strategies | ⭐⭐⭐⭐ |
| 20 | Self-Evaluation / Learning | Learns from historical outcomes | ⭐⭐⭐⭐⭐ |

---

## Governance principles (non-negotiable, apply to every item below)

These came out of the 2026-08-26 discussion and should gate every AI feature built from this roadmap, not just the first one:

1. **AI is an advisory layer around the existing quantitative engine, never a replacement for it.** The strategies, signal generation, and order logic already in this codebase stay as they are — AI adds scoring/filtering/sizing intelligence on top, it doesn't replace `donchian`/`ema`/`rsi`/etc.
2. **The Risk Engine is a hard, deterministic gate that sits between every AI decision and the broker.** AI can recommend a risk tier, a veto, an adjusted stop — it never directly places or blocks an order. Max loss, max size, max exposure, and the kill switch are enforced in code, unconditionally, regardless of what any model says.
3. **AI proposes, the backtester verifies — never AI proposes straight to LIVE.** A model finding ("ORB performs best when ATR percentile > 63 AND ADX > 24") is a hypothesis to backtest, not an instruction to trade on. This mirrors the standing rule already in this codebase (see `feedback_no_core_logic_changes` memory) and the `cnn_lstm` lesson below.
4. **Every item ships to SIM first, on both accounts' worth of history, before it's allowed to influence a LIVE order.** No exceptions, regardless of how confident a model looks in backtest.
5. **Model promotion needs to pass predefined statistical and risk gates**, not just "look better once." A candidate model replaces the current one only after clearing walk-forward validation and paper trading, evaluated head-to-head against what's already running — see item #20 (AI Model Evolution) for the full pipeline.

### The overall architecture (target shape, not built yet)

```
                     ATOS-DEEP
                         │
        ┌────────────────┼────────────────┐
        │                │                │
        ▼                ▼                ▼
   MARKET AI       STRATEGY AI       PORTFOLIO AI
        │                │                │
        ▼                ▼                ▼
 Regime Detection   Strategy Select   Correlation
 Volatility         Signal Score      Exposure
 Anomaly            Trade Quality     Allocation
 Sentiment          Entry/Exit        Portfolio Risk
 News               Veto
        │                │                │
        └────────────────┼────────────────┘
                         ▼
                   AI DECISION
                         │
                         ▼
                  ┌──────────────┐
                  │ RISK ENGINE  │
                  │              │
                  │ HARD LIMITS  │
                  │ MAX LOSS     │
                  │ MAX SIZE     │
                  │ MAX EXPOSURE │
                  │ KILL SWITCH  │
                  └──────┬───────┘
                         │
                    APPROVE/REJECT
                         │
                         ▼
                   ORDER ENGINE
                         │
                         ▼
                       SAXO
                         │
                         ▼
                   LIVE TRADING
                         │
                         ▼
                  TRADE DATABASE
                         │
                         ▼
                    AI LEARNING
```

Note on data plumbing: Saxo's streaming architecture (WebSocket subscriptions for quotes/positions/orders/balances) is well suited to this once built — ATOS could receive live updates rather than polling the REST API repeatedly, which several of these features (especially Open Position AI and Anomaly Detection) would benefit from running continuously. Not implemented anywhere in this codebase yet — everything today is poll-based (`_get`/`_fetch_history` on a schedule).

---

## Detailed feature specs (worked examples, user's notes 2026-08-26)

### 1. 🧠 Market Regime Detection
Classifies current conditions per pair so ATOS knows which strategies should even be active:
```
EURUSD
Trend:          Strong bullish
Volatility:     Medium
Momentum:       High
Liquidity:      High
Regime:         TRENDING
Confidence:     91%
```
Regime taxonomy: `TRENDING_BULLISH`, `TRENDING_BEARISH`, `RANGING`, `BREAKOUT`, `HIGH_VOLATILITY`, `LOW_VOLATILITY`, `CHAOTIC`, `NEWS_DRIVEN`.

### 2. 🎯 AI Signal Score
A second layer on top of a strategy's existing signal, scoring it 0–100:
```
ORB Strategy — BUY EURUSD

Trend alignment       92
Momentum               87
Volatility             81
Liquidity              94
Session quality        89
Market regime          91
────────────────────────
AI SCORE               89/100
```
Bands: 90–100 Excellent · 80–89 Strong · 70–79 Acceptable · 60–69 Weak · <60 Reject.

### 3. 🤖 AI Strategy Selector
Given the current regime, recommends which of the available strategies should be active rather than running all of them uniformly:
```
Current regime: TRENDING + HIGH MOMENTUM

Recommended:
Trend Following      94%
Momentum              91%
Breakout              87%
ORB                   82%
Mean Reversion        21%
```
Could become one of ATOS's core intelligence components — this is the natural extension of `strategy_learner.py`'s existing (regime-blind) performance weighting.

### 4. 🔮 Trade Probability Model
Instead of a bare BUY/SELL, an expected-value estimate:
```
Probability of positive outcome:
1R target:     73%
2R target:     61%
3R target:     42%

Win probability = 61%, Reward = 2R, Risk = 1R
Expected value = 0.61 × 2 − 0.39 × 1 = +0.83R
```

### 5. 🚫 AI Trade Veto
Strategy signals, AI can override before the Risk Engine even sees it:
```
Strategy: BUY EURUSD
AI: Signal quality 38 · Regime mismatch · Volatility anomaly · Major event approaching
AI: VETO → Risk engine: REJECT → No order.
```

### 6. 📏 Dynamic Position Sizing
AI recommends a risk tier; the deterministic Risk Engine still enforces the hard maximum:
```
Normal conditions:   0.50%
Strong signal:       0.50%
Weak conditions:     0.25%
Extreme volatility:  0.10%
Anomaly:             0%
```

### 7. 🎯 AI Entry Optimization
Given a signal, decides timing rather than firing immediately — analyzes spread, momentum, order-book conditions, volatility, short-term structure, recent price acceleration:
```
BUY EURUSD → ENTER NOW / WAIT 5 minutes / WAIT FOR PULLBACK
```
Particularly relevant for swing-style entries.

### 8. 🚪 AI Exit Optimization
Potentially more valuable than entry optimization — can recommend a partial reduction instead of a binary hold/close:
```
EURUSD LONG — Entry 1.1700, Current 1.1780, Profit +0.68R
AI sees: trend weakening, momentum declining, volatility increasing, regime transition
Recommendation: REDUCE 50% (not just HOLD)
```

### 9. 🛑 AI Stop-Loss Intelligence
Replaces a fixed pip stop with one derived from live structure/volatility — the Risk Engine still verifies it doesn't violate max-loss rules:
```
ATR = 62 pips, Structure support = 1.1685, Volatility = high
Suggested SL: 1.1678
```

### 10. 💰 AI Take-Profit Intelligence
Probability-weighted targets instead of one fixed TP:
```
1R → 82%, 2R → 67%, 3R → 41%, 4R → 18%
→ TP1 = 1R, TP2 = 2R, trail the remainder
```

### 11. 🌪️ Volatility Prediction
Forecasts near-term volatility, not just measures current:
```
Current ATR: 45, Predicted ATR in 2h: 72, Confidence: 84%
→ reduce size / widen or adjust stops / avoid new positions / prepare for breakout
```

### 12. 🚨 Market Anomaly Detection
Flags behavior outside normal historical patterns and can trigger a defensive mode:
```
EURUSD — Normal volatility 0.35%, Current 1.21%
Volume anomaly: HIGH · Spread anomaly: HIGH · Correlation anomaly: HIGH
→ MARKET ANOMALY → switch to DEFENSIVE MODE
```
Especially relevant for LIVE.

### 13. 🔗 Portfolio Correlation AI
Catches hidden concentration a simple position count hides:
```
EURUSD LONG, GBPUSD LONG, AUDUSD LONG, EURGBP SHORT
→ looks diversified (4 trades), but AI: "you are effectively heavily exposed to USD weakness"
USD exposure: HIGH · Correlation risk: HIGH → Risk Engine can reject another USD-related trade
```

### 14. 📰 Economic Event Intelligence
```
EURUSD — ECB decision: 45 minutes · US CPI: tomorrow · FOMC: 2 days
→ Event risk = HIGH → don't open new trade, or reduce position size
```
**Cheap first version, recommended before full news/sentiment**: a hard-coded economic calendar blackout (no entries / wider stops around known high-impact releases like NFP or rate decisions) — most of the practical risk reduction, none of the NLP infrastructure.

### 15. 🧠 Sentiment AI
Converts news/commentary/headlines/central-bank statements into a directional score:
```
EUR sentiment: +0.72, USD sentiment: -0.41 → EURUSD sentiment: +0.83 bullish
```
Ranked below quantitative regime detection for v1 — most infrastructure-heavy, easiest to overfit.

### 16. 👁️ Open Position AI ("Trade Management AI")
Runs continuously on every open position, not just at entry:
```
EURUSD LONG, holding 12 days, P/L +1.4R
AI: trend bullish, momentum weakening, regime changing, exit probability next 24h: 34%
Recommendation: HOLD
--- later ---
Exit probability: 71% → Recommendation: REDUCE 50%
```
Makes ATOS behave more like an active portfolio manager than a fire-and-forget signal generator.

### 17. 📊 AI Portfolio Manager
Evaluates the whole book together, not each trade independently:
```
Portfolio Risk: Market risk 42% · USD exposure 31% · Equity exposure 27% · Gold exposure 8%
Correlation risk: HIGH
```

### 18. 📔 AI Trading Journal
Auto-generated per-trade retrospective — no trained model required, pure summarization over already-logged data:
```
TRADE #1827 — EURUSD LONG — Strategy: Trend + Momentum — AI confidence: 87
Entry quality: Excellent · Exit quality: Average
Why profitable: Strong trend alignment
What went wrong: Exited 18% too early
Lesson: In this regime, trailing exits produced better results than fixed TP.
```
Becomes a genuinely valuable research database over time — recommended as one of the very first builds since it needs zero new infrastructure (`pnl_tracker`/`trade_logger` already have everything it summarizes).

### 19. 🔬 AI Strategy Discovery
Mines historical trades/features for high-expectancy conditions a human wouldn't necessarily hand-pick:
```
"Find conditions where ORB has unusually high expectancy."
AI discovers: ORB performs best when ATR percentile > 63, ADX > 24, London session,
previous-day range < X, momentum > Y.
```
**Hard rule**: AI proposes → Backtester verifies. Never AI proposes → Live immediately.

### 20. 🧬 AI Model Evolution
The full lifecycle any model on this roadmap should go through before touching real money:
```
Historical data → Feature engineering → ML training → Backtest → Walk-forward test
→ Paper trading → Validation → LIVE
```
Periodically evaluate "current model vs candidate model" and only promote the candidate if it clears predefined statistical and risk gates — never just because it looked better once.

---

## What already exists in this codebase (don't rebuild these)

- **#2 AI Signal Scoring / #5 AI Trade Veto** — `forex/signal_filter.py` already has an ML consensus gate (`ml_probability()` / `passes_ml()`) that scores signals and can block them. Currently inert: it requires 150 labeled closed trades to activate and has 0/150 across most tiers. The fastest path to a working version of #2/#5 is accumulating enough real closed-trade history, not building a new system.
- **#20 Self-Evaluation / Learning** — `strategy_learner.py` already reweights each strategy's slot allocation by its own realized performance (`get_weights()`, `log_weights_table()`, per-module weight files). A regime-aware version of this (weight by performance *conditioned on* the current regime, not just overall) is a refinement of an existing system, not new.
- **A cautionary example already in production**: `cnn_lstm` (a real deep-learning strategy, trained 2026-08-19) is technically live in every SIM scan but walk-forward validated at only 36.9% accuracy — barely above the 33% random baseline for its 3-class problem. "Technically active, practically inert." This is the concrete reason to validate any new model rigorously (walk-forward, not just backtest-fit) before trusting it, and why the governance principles above exist.

---

## Build order

**User's own "ATOS v1 AI" priority list (2026-08-26) — this is the one that governs Friday's actual sequence:**

1. Market Regime AI
2. Trade Quality / Probability Model
3. AI Signal Scoring
4. AI Strategy Selector
5. AI Trade Veto
6. Volatility Prediction
7. Anomaly Detection
8. Open Position AI
9. Portfolio Correlation AI
10. AI Trading Journal

Then news/sentiment (#14/#15) and strategy discovery (#19) after those ten.

**Claude's earlier quick-start suggestion** (Market Regime → AI Trading Journal → Trade Management/Exit Optimization) agrees with the user's list on item #1 and includes #10/#16 earlier than the user's ordering — worth revisiting on Friday whether to interleave the Trading Journal earlier since it's the cheapest of all ten to ship (no model training needed, pure summarization over existing logged data), while the others build up real feature/scoring infrastructure in sequence.

**Testing discipline**: every item tested on SIM first, same standing rule already applied to every strategy/pair change in this codebase (see `feedback_no_core_logic_changes` memory) — nothing here touches either LIVE account until proven on SIM, and per the governance principles above, no model goes live without walk-forward validation and paper trading first regardless of backtest results.

---

## Task list — starting Friday 2026-08-28, one at a time

- [ ] #1 Market Regime AI (trend/range/high-vol/low-vol classifier from existing ATR/ADX data — feeds nearly everything below)
- [ ] #4 Trade Quality / Probability Model (expected-value framing: win probability × reward − loss probability × risk)
- [ ] #2 AI Signal Scoring (0–100 score per signal — extends `signal_filter.py`'s existing ML gate rather than replacing it)
- [ ] #3 AI Strategy Selector (regime-conditioned extension of `strategy_learner.py`'s existing weighting)
- [ ] #5 AI Trade Veto (sits before the Risk Engine, never replaces it)
- [ ] #11 Volatility Prediction
- [ ] #12 Anomaly Detection (feeds a "defensive mode" concept, most relevant to LIVE)
- [ ] #16 Open Position AI / Trade Management (continuous monitoring of held positions, not just entry-time scoring)
- [ ] #13 Portfolio Correlation AI (exposure concentration across instruments, not just position count)
- [ ] #18 AI Trading Journal (cheapest to ship — no model needed, pure summarization over `pnl_tracker`/`trade_logger`; candidate to pull forward earlier in the sequence)
- [ ] #6 Dynamic Position Sizing, #9 Stop-Loss Intelligence, #10 Take-Profit Intelligence (once regime detection exists to condition on)
- [ ] #7 AI Entry Optimization
- [ ] #17 AI Portfolio Manager (whole-book view, builds on #13)
- [ ] #14 Economic Event Intelligence (start with a hard-coded calendar blackout, not full NLP)
- [ ] #15 Sentiment Analysis, #19 Strategy Discovery, #20 AI Model Evolution pipeline (lowest priority, most speculative — #19/#20 also define the promotion process every earlier model should retroactively be held to)
