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

## What already exists in this codebase (don't rebuild these)

- **#2 AI Signal Scoring / #5 AI Trade Veto** — `forex/signal_filter.py` already has an ML consensus gate (`ml_probability()` / `passes_ml()`) that scores signals and can block them. Currently inert: it requires 150 labeled closed trades to activate and has 0/150 across most tiers. The fastest path to a working version of #2/#5 is accumulating enough real closed-trade history, not building a new system.
- **#20 Self-Evaluation / Learning** — `strategy_learner.py` already reweights each strategy's slot allocation by its own realized performance (`get_weights()`, `log_weights_table()`, per-module weight files). A regime-aware version of this (weight by performance *conditioned on* the current regime, not just overall) is a refinement of an existing system, not new.
- **A cautionary example already in production**: `cnn_lstm` (a real deep-learning strategy, trained 2026-08-19) is technically live in every SIM scan but walk-forward validated at only 36.9% accuracy — barely above the 33% random baseline for its 3-class problem. "Technically active, practically inert." This is the concrete reason to validate any new model rigorously (walk-forward, not just backtest-fit) before trusting it, and why the newer/fancier items below are ranked more cautiously than the user's own priority column.

---

## Recommended build order (Claude's assessment, for Friday)

1. **#1 Market Regime Detection** — buildable purely from price data already being fetched (ATR/ADX/rolling volatility), no new data source needed. Feeds #6/#9/#10 naturally once it exists — start here.
2. **#18 AI Trading Journal** — the cheapest real win on the list. Doesn't need a trained model at all: summarizing already-logged closed trades (`pnl_tracker`/`trade_logger`) into "why this won/lost" is a text-generation task, not a modeling task. Could ship in a day.
3. **#16 Trade Management AI / #8 Exit Optimization** — direct real-money impact since exits determine realized P&L, and there's existing ATR/time-stop logic to extend rather than replace.

**Deprioritize relative to the user's own ranking**: #14 News/Event Intelligence, #15 Sentiment Analysis, #19 Strategy Discovery — most infrastructure-heavy, easiest to fool yourself with on a backtest, per the cnn_lstm lesson above. If news handling is wanted early, a hard-coded economic-calendar blackout (no entries / wider stops around known high-impact releases like NFP or rate decisions) gets most of the practical risk-reduction without needing any actual news-reading AI — recommended as a cheaper first step before real sentiment analysis.

**Testing discipline**: every item tested on SIM first, same standing rule already applied to every strategy/pair change in this codebase (see `feedback_no_core_logic_changes` memory) — nothing here touches either LIVE account until proven on SIM.

---

## Task list — starting Friday 2026-08-28, one at a time

- [ ] #1 Market Regime Detection (trend/range/high-vol/low-vol classifier from existing ATR/ADX data)
- [ ] #18 AI Trading Journal (LLM summary pass over closed trades, explaining win/loss patterns)
- [ ] #16 Trade Management AI / #8 Exit Optimization (extend existing ATR/time-stop logic)
- [ ] #6 Dynamic Position Sizing, #9 Stop-Loss Optimization, #10 Take-Profit Optimization (once regime detection exists to condition on)
- [ ] #2 AI Signal Scoring / #5 AI Trade Veto (activate once enough real closed-trade data exists for `signal_filter.py`'s existing ML gate)
- [ ] #11 Volatility Prediction, #12 Anomaly Detection
- [ ] #3 Strategy Selection, #13 Correlation Intelligence, #17 Portfolio Optimization
- [ ] #7 Entry Optimization
- [ ] #14 News/Event Intelligence (start with economic-calendar blackout, not full NLP)
- [ ] #15 Sentiment Analysis, #19 Strategy Discovery (lowest priority, most speculative)

Remaining items (#4 Trade Quality Prediction) fold naturally into #2/#5's ML gate once it's active — not a separate build.
