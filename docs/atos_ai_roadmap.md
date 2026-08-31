# ATOS AI Roadmap

Planning discussion held 2026-08-26. Build started 2026-08-28, one item at a time, tested on SIM first before any of it touches either LIVE account (SEK or EUR).

> **Build progress:** Sprints 0–3 shipped (shadow mode, nothing touches live trading). Current state, commits, test counts, and open decisions are tracked in **[`docs/atos_ai_tracker.md`](atos_ai_tracker.md)**. Sprint order and per-sprint test gates: [`docs/atos_ai_implementation_plan.md`](atos_ai_implementation_plan.md).

---

## ✅ RESOLVED — the actual v1 scope (2026-08-26, final)

The three open conflicts flagged during planning are now settled. This is the real Friday starting point — everything else in this document is the roadmap, not the sprint:

> **v1 = a single consolidated agent, delivering Signal Scoring (with market regime as a code-computed input feature, not its own LLM call or agent), sizing decisions limited to a bounded multiplier only — no dynamic SL/TP adjustment yet.**

**1. Single agent, not six.** The 6-agent + Supervisor design (see "Multi-agent architecture" below) is the *target* architecture, not the v1 build — deliberately deferred, not abandoned. Reasoning: each extra agent is another LLM call, meaning more latency, more cost, and more places the pipeline can silently fail or disagree with itself; six sets of prompts/weighting logic can't be validated against real trade outcomes until there's live/backtest data to tune against — that's guessing at 6x the behavior instead of 1x; and a single structured JSON response can hold "regime, signal quality, news risk, portfolio exposure" as sections of one output, getting the same decision quality without the orchestration overhead. Split into separate agents later, and only once there's evidence a specific piece (e.g. news analysis) needs its own tuning loop separate from the rest — that's a refactor triggered by evidence, not a default starting posture.

**2. Regime detection and Signal Scoring were never actually competing phases — it's a sequencing question, not a choice.** Regime is an *input* to signal scoring, not a rival to it. Build the regime classification first, but as a cheap deterministic/statistical calculation in code (ADX, ATR, moving-average slope, volatility bands) — explicitly **not** an LLM call and not its own agent. Feed that regime label into the AI Trade Score as one of its ~10 factors. So: **Phase 1 is Signal Scoring**, with regime as a code-computed input feature built in the same phase, not two separate phases.

**3. v1 sizing is multiplier-only — no dynamic SL/TP adjustment.** A position-size multiplier is bounded and reversible: worst case the AI says 0.3x and the trade is just smaller — it can never increase risk beyond what the Risk Engine already caps. Dynamic SL/TP adjustment is a materially different failure mode: it changes the actual risk-per-trade math the Risk Engine already validated, so a wrong widened stop ahead of news is a loss the deterministic system never originally sanctioned. SL/TP adjustment moves to **Phase 6 (Position Management)**, after the multiplier-only version has run in shadow mode long enough to trust the model's judgment on these specific instruments.

---

## The long-term vision: an always-on oversight agent (user's notes, 2026-08-26)

Not a change to the v1 scope above — this is the *reason* the roadmap has the later phases it does, stated as one coherent goal rather than a feature list:

> Find a stronger signal scanner that identifies the best pairs to trade using AI, and an agent that runs continuously — checking every log, every trade, analyzing losing and winning trades, and reporting back with the reasoning behind each decision it makes.

Mapped onto what's already planned, this isn't new scope, it's the connective purpose behind items already on the roadmap:
- **"Stronger signal scanner, best pairs"** → #2 AI Signal Scoring plus the "rank ALL opportunities" extension (both already in the v1 scope above) and, later, #3 AI Strategy Selector.
- **"Runs continuously, checks everything, analyzes wins/losses, reports with reasoning"** → #16 Open Position AI / Trade Management, #18 AI Trading Journal, and the Learning Agent from the 6-agent design (all later-phase, not v1).

**Scope of "makes decisions by self" — ✅ CONFIRMED by the user, locked in (2026-08-26).** Two genuinely different senses of "autonomy" were being conflated, and they're now explicitly separated:

- **Autonomy of analysis/decision-production — IN SCOPE, this is the actual goal.** Every 45-minute cycle, unattended, the agent produces a structured recommendation with reasoning: approve/reject, a confidence score, a suggested size multiplier, and flags — without a human reviewing each signal in real time. This *is* genuine autonomy in the sense that matters day-to-day: nobody is sitting there approving trades one by one. This is what "the agent decides" means.
- **Autonomy of execution authority — NOT in scope, and treated as a categorically different capability, not a lesser degree of the same thing.** The agent never calls Saxo's order-placement endpoint directly, never overrides the Risk Engine's caps, never touches SL/TP. Conflating "it decides confidently and unattended" with "it can act on that decision beyond what's already approved" is exactly the ambiguity this section exists to close off before it gets expensive later.

**Escalation past Level 2 is gated on evidence, not time — explicit rule, not a default.** The natural temptation once shadow-mode recommendations start looking good is "it's clearly reliable, let it size/adjust stops itself" on a timeline ("we've watched it for N weeks"). Rejected deliberately: time alone doesn't prove much if the market's been calm the whole time. The actual gate: **M live-approved trades with tracked outcomes, where the agent's recommendations have hurt risk-adjusted returns in zero of them, including through at least one adverse/volatile stretch** — not just a quiet run.

**Any increase in autonomy level beyond Level 2 requires a separate, explicitly-approved phase — a written decision, not an assumed extension of trust in the sizing agent.** Sizing working well is not evidence that SL/TP adjustment (or any other Level-3-adjacent capability) should follow automatically — that's a new capability with a new failure mode (see the governance principles at the very top of this document), and it gets its own decision, made deliberately, not inherited from an unrelated success.

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
6. **The system must degrade safely if the AI is unavailable.** If an LLM API is down or a model fails to load, ATOS keeps running — either skip new entries or fall back to the deterministic strategy alone. The trading system is never *dependent* on an AI service being reachable.

### The single most important architectural change (user's framing, 2026-08-26)

Current shape: **"If it finds a signal → place order."** That's `AI → Saxo → BUY` in one hop — too simplistic and too risky to build AI into directly.

Target shape: **"If it finds a signal → create a candidate → AI evaluates the candidate → ATOS Risk Engine validates → execute only if approved."** This single change is what turns ATOS from a rule-based bot into a hybrid quantitative + AI decision system, and it's the one to get right before any individual AI feature matters. Concretely:

```
Every 45 min                          Every 45 min (target shape)
     ↓                                       ↓
   Scan                              Market Scanner
     ↓                                       ↓
  Signal                            Strategy Engine
     ↓                                       ↓
  Order                            Candidate Signals
                                             ↓
                                        AI Agent
                                             ↓
                                        AI Score
                                             ↓
                                     Portfolio Risk
                                             ↓
                                   ATOS Risk Engine
                                             ↓
                                   APPROVE / REJECT
                                             ↓
                                        Execution
                                             ↓
                                          Saxo
                                             ↓
                                  SL + Profit Secure
```
The 45-min cadence itself doesn't need to change on day one — this is purely about inserting a candidate/evaluation step before execution, not a scheduling change. Later, cadence can become adaptive: normal market → scan every 45 min, high volatility → scan every 10 min, an open position → monitor continuously, major news → re-evaluate immediately, a stop/profit event → trigger an AI re-evaluation. Saxo's OpenAPI supports WebSocket streaming for quotes/positions/orders/balances, which is what would make continuous/event-driven monitoring practical instead of polling on a timer — not implemented anywhere in this codebase yet, worth verifying the exact streaming contract against Saxo's real docs before building on it.

### The AI "Trade Constitution" — hard rules, not suggestions

Expands governance principle #2 above into an explicit, enumerable rule set every agent must respect:

**AI MUST NEVER:** exceed ATOS's maximum configured risk · bypass the stop-loss · bypass portfolio risk checks · trade outside the allowed instrument/pair list for that account · increase a position for emotional/"revenge trade" reasons · override the emergency kill switch · place an order directly (only ATOS's execution layer talks to Saxo).

**AI MAY:** approve · reject · rank opportunities against each other · reduce position size · suggest an entry timing adjustment · suggest a stop adjustment · suggest a partial/full exit · classify the market regime · analyze news/sentiment · analyze historical performance.

Flow: `AI recommendation → ATOS validates → Risk Engine validates → Execution Engine → Saxo`. Every arrow is a real, separate checkpoint — never collapsed into one step.

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

### Alternate view of the same idea: the 5-layer stack (user's framing, 2026-08-26)

Same principle as the diagram above, drawn as explicit layers with the AI's own internal responsibilities broken out:

```
┌─────────────────────────────┐
│        AI TRADE AGENT       │
│                             │
│  Market Regime              │
│  Signal Quality             │
│  News/Sentiment             │
│  Risk Assessment            │
│  Trade Ranking              │
│  Position Management        │
└──────────────┬──────────────┘
               │
       AI Decision: BUY / SELL / SKIP + confidence
               │
               ▼
┌────────────────────────────────────┐
│        ATOS DECISION ENGINE        │
│  Signal validation                 │
│  Strategy confirmation             │
│  Position sizing                   │
│  Portfolio exposure                │
│  Risk limits                       │
└────────────────┬───────────────────┘
                 │
                 ▼
┌────────────────────────────────────┐
│          ATOS EXECUTION            │
│  Order creation                    │
│  Stop Loss                         │
│  Take Profit                       │
│  Profit Secure / Trailing          │
│  Order monitoring                  │
└────────────────┬───────────────────┘
                 │
                 ▼
        ┌──────────────────┐
        │   SAXO OPEN API  │
        │      SIM/LIVE    │
        └──────────────────┘
```
The key property both diagrams share: the AI's output is a *decision with a confidence score*, not an order — the Decision Engine and Risk Engine are separate, deterministic checkpoints the AI's output must pass through before anything reaches Saxo.

### Hybrid AI brain — don't rely on one AI technique for everything

Explicit recommendation: don't build `ATOS → single LLM call → BUY/SELL` as the core intelligence. Different techniques are good at different sub-problems:

```
                 AI BRAIN
                    │
        ┌───────────┼────────────┐
        │           │            │
        ▼           ▼            ▼
    ML Model     LLM Agent    Statistical
    Scoring      Reasoning     Models
        │           │            │
        └───────────┼────────────┘
                    ▼
              AI Decision
                    │
                    ▼
              ATOS Risk Engine
                    │
                    ▼
                 Saxo
```
- **ML models** (XGBoost/LightGBM/Random Forest/PyTorch/scikit-learn) — probability, classification, signal scoring, regime detection, expected return, volatility, trade-outcome prediction. Numerical prediction problems.
- **LLM** — interpreting news, explaining a decision in plain language, synthesizing multiple structured signals into one narrative, spotting an unusual situation a rule wouldn't catch, generating trade reasoning, conversing with the user (e.g. the AI Trading Journal).
- **Statistical models** — volatility, correlations, drawdown, expected value, portfolio exposure, risk — the things that are genuinely just math, not prediction.

Tooling note: since ATOS is already Python, keep the whole AI layer in Python too — local ML (scikit-learn/XGBoost/PyTorch etc.) for the numerical side, a local LLM (this codebase has prior experience with local coding models) for news/explanation/reasoning where keeping inference on-machine matters, and a cloud LLM only where the extra reasoning quality is worth the external dependency — governed by principle #6 above (AI unavailable must never mean ATOS stops trading safely).

### Multi-agent architecture — 6 agents + 1 supervisor

Rather than one monolithic "AI trader," decompose into named agents with clear inputs/outputs:

1. **Market Analyst** — "what is happening in the market?" Outputs: regime, trend, volatility, momentum, liquidity, macro context.
2. **Signal Analyst** — "is ATOS's signal actually good?" Outputs: signal score, expected probability, expected R, confidence.
3. **News Agent** — "is there anything that could invalidate this setup?" Outputs: news risk, event risk, sentiment, potential volatility.
4. **Portfolio Risk Agent** — "does this make sense with everything already open?" Outputs: correlation, exposure, concentration, risk, recommended size.
5. **Trade Manager** — for open positions: KEEP / TIGHTEN STOP / TAKE PROFIT / PARTIAL EXIT / EXIT.
6. **Learning Agent** — after every trade: prediction vs. actual, signal quality, AI confidence, strategy performance, regime, mistakes. This is the feedback loop.

```
                 AI SUPERVISOR
                       │
        ┌──────────────┼──────────────┐
        ▼              ▼              ▼
 Market Agent    Signal Agent    News Agent
        │              │              │
        └──────────────┼──────────────┘
                       ▼
                Portfolio Agent
                       │
                       ▼
                  FINAL DECISION
                       │
                       ▼
                  ATOS RISK ENGINE
                       │
                       ▼
                    SAXO
```
The Supervisor's job is to combine structured outputs from the other agents, not to "think" independently — it's a merge/aggregation step, not its own source of judgment.

### Trade memory — what to log for the learning loop to actually work

For the Learning Agent (and #18 AI Trading Journal, and #19 Strategy Discovery) to be useful, every trade needs a rich record, not just entry/exit price: trade ID, instrument, strategy, direction, entry, stop, target, market regime at entry, indicator values, AI score, AI decision, position size, news conditions at entry, result, MAE (max adverse excursion), MFE (max favorable excursion), exit reason, R-multiple. After enough trades accumulate (the user's benchmark: ~1,000), this becomes a real dataset an AI can mine for patterns like "ORB works 09:00-11:00 with ADX>25 and above-average volume, but poorly on low-volatility Friday afternoons." `pnl_tracker`/`trade_logger` already capture the trade-outcome half of this; the regime/indicator/AI-score half doesn't exist yet and would need adding alongside whichever AI feature first needs it (naturally, Market Regime Detection first).

### Proposed code layout (once building starts)

```
ATOS/
├── atos/
├── strategies/
├── risk/
├── execution/
├── broker/
├── market/
├── ai/
│   ├── agent/
│   ├── models/
│   ├── features/
│   ├── regime/
│   ├── scoring/
│   ├── news/
│   ├── portfolio/
│   ├── trade_manager/
│   └── supervisor/
├── data/
├── backtest/
├── reports/
└── tests/
```
Doesn't need to match this codebase's actual current layout exactly (this repo's real structure is flatter — `forex/`, `futures/`, `atos/`, top-level dashboards/schedulers) — captured here as the *shape* of separation to aim for (an `ai/` package with its own agent/model/feature/regime/scoring/news/portfolio/trade_manager/supervisor submodules) rather than a literal directory-for-directory migration plan.

### Validating the AI (expands the existing "AI proposes, backtester verifies" principle)

Before any AI-influenced decision reaches LIVE, run a real A/B comparison, not just "does the new version look good":

- **Version A**: ATOS without AI (current, exactly as it runs today)
- **Version B**: ATOS + AI (candidate)
- **Compare**: net return, Sharpe, Sortino, max drawdown, profit factor, win rate, average R, expectancy, number of trades, false-signal rate, risk-adjusted return.

The AI has to earn its place — a higher return alone isn't sufficient justification, and a flat-or-lower return isn't automatically disqualifying either:
- If `ATOS = +32%` and `ATOS+AI = +27%` → the AI made the system worse. Reject.
- If `ATOS = +32%` and `ATOS+AI = +34%` but max drawdown drops from 18% to 9% → the AI may be extremely valuable even without a dramatic return improvement, because it materially reduced risk for a similar/better outcome.

### Saxo capabilities referenced in this discussion (verify against real Saxo OpenAPI docs before relying on them)

Three specific capabilities were mentioned as available on Saxo's platform, not yet verified against this codebase's actual `saxo_client.py`/API usage: **order precheck** (an endpoint that returns estimated trading costs without actually placing the order — would let ATOS validate a candidate order before submission), **trailing-stop order type** support (relevant to AI Take-Profit/Stop-Loss Intelligence and Trade Manager's "TIGHTEN STOP" action), and **WebSocket streaming** for quotes/positions/orders/balances (already noted above). None of these are used anywhere in this codebase today — confirm the exact endpoint/contract against Saxo's live OpenAPI reference before designing a feature around any of them.

---

## Third contribution: concrete v1 scope, autonomy levels, rollout plan (2026-08-26)

### v1 mandate — pick a narrow role, don't hand the agent everything at once

Three candidate roles for the very first version, deliberately narrow:

| Role | Label | What it does |
|---|---|---|
| Signal filter | Approve / reject | Reviews each signal ATOS's strategies find and decides trade-or-skip using broader context (regime, news, volatility) |
| Position sizing advisor | Size / reduce | ATOS says BUY; the agent suggests how much to risk (e.g. scale down in high volatility) |
| Risk overlay | Tighten / relax risk | Adjusts stop-loss distance, take-profit logic, or declares "no new trades" in stressed markets |

**User's own choice for v1: Signal filter + Position sizing advisor.** Risk overlay comes later.

**✅ RESOLVED, see "RESOLVED — the actual v1 scope" at the top of this document.** Multiplier only — no dynamic SL/TP in v1. That moves to Phase 6 (Position Management) once the multiplier version is proven in shadow mode.

### Trade proposal schema (concrete, ready to use as a starting point)

The shape ATOS's scan would emit instead of going straight to Saxo:
```json
{
  "symbol": "EURUSD",
  "side": "BUY",
  "entry_price": 1.0850,
  "stop_loss": 1.0820,
  "take_profit": 1.0910,
  "timeframe": "M15",
  "strategy_name": "MA_CROSS",
  "signal_strength": 0.78,
  "account_equity": 25000,
  "open_positions": [
    {"symbol": "EURUSD", "side": "SELL", "size": 0.5}
  ],
  "volatility_atr": 0.0009
}
```
And the structured decision the agent returns:
```json
{
  "action": "APPROVE",
  "size_multiplier": 0.7,
  "adjusted_stop_loss": null,
  "adjusted_take_profit": null,
  "comment": "Trend is up, volatility moderate, but existing short EURUSD position suggests reducing size."
}
```
`action` is one of `APPROVE` / `REJECT` / `MODIFY`. On `APPROVE`, ATOS sends the order with the adjusted size/risk; on `REJECT`, ATOS logs the reason and places nothing; on `MODIFY`, ATOS applies the changes then places the order. This maps directly onto this codebase's existing pattern of a strategy signal dict flowing into `_run_entries()` — the AI step would sit between signal generation and `saxo_order.place_with_stop()`, not replace either.

### Single agent vs. the 6-agent design — ✅ RESOLVED

**Single agent for v1.** See "RESOLVED — the actual v1 scope" at the top of this document for the full reasoning (latency/cost/failure-surface of 6 LLM calls, can't validate 6 sets of prompts without real data to tune against, one structured JSON response can hold all the same sections). The 6-agent + Supervisor design below stays the *target* architecture — split out a piece into its own agent later, only once there's evidence it needs a separate tuning loop (e.g. news analysis proves it needs different iteration speed than the rest).

### Autonomy levels — recommended staging

1. **Shadow mode** (precedes all 3 levels below): the agent evaluates every real signal but influences nothing — ATOS trades exactly as it does today, the agent's decision is only logged next to the actual outcome. Not gated on a fixed time window (see below) — gated on accumulating enough tracked outcomes to actually judge it.
2. **Level 1 — Advisory only**: agent gives a recommendation, a human approves or rejects manually. No automatic effect on order flow.
3. **Level 2 — Semi-autonomous** (recommended starting point once shadow mode looks good, and the current ceiling — see below): the agent can skip or resize a trade automatically, unattended, every cycle, but can never exceed the fixed risk limits already enforced in code and never touches SL/TP. This is genuine day-to-day autonomy (nobody manually reviews each signal) without execution authority (it never calls Saxo directly or overrides the Risk Engine).
4. **Level 3 — Fully autonomous** (explicitly much later): agent could also pause a strategy, change its own parameters, or adjust SL/TP. Not in scope for any near-term work.

**✅ CONFIRMED (2026-08-26): the gate from Level 2 to anything beyond it is evidence-based, not time-based, and requires a separate explicit decision.** Not "we've run Level 2 for N weeks" — that proves little if the market stayed calm the whole time. The actual bar: a defined number (M) of live-approved trades with tracked outcomes, where the agent's recommendations have hurt risk-adjusted returns in zero of them, **including through at least one adverse or volatile stretch** — and even clearing that bar doesn't auto-grant the next level. Sizing working well is never itself the justification for granting SL/TP authority or any other new capability; each is a distinct decision with its own failure mode, made deliberately, not inherited.

### Implementation shape referenced (not yet decided)

A microservice pattern was proposed: a small separate Python/FastAPI (or Node) service exposing a single `/evaluate_trade` endpoint that ATOS calls once per candidate signal, which internally calls the LLM and returns the structured decision JSON above. This is a real architectural choice to make deliberately — a separate service vs. an in-process function call inside `forex/runner.py` — rather than something to default into. Given the 45-minute (or faster, event-driven) cadence isn't latency-sensitive, either shape is timing-wise fine; the tradeoff is operational (a service to deploy/monitor/keep alive vs. one more Python import) not a performance one. A draft system-prompt starting point was also provided:
```
You are a trading decision AI.
You NEVER place orders.
You ONLY evaluate trade proposals from the ATOS quant bot.

Your job:
- Approve or reject trades
- Adjust position size
- Adjust SL/TP if needed
- Consider volatility, trend, sentiment, open positions, risk limits

Output ONLY valid JSON:
{
  "action": "APPROVE | REJECT | MODIFY",
  "size_multiplier": number,
  "adjusted_stop_loss": number or null,
  "adjusted_take_profit": number or null,
  "comment": "short explanation"
}
```
Tool-use/function-calling (Claude's native capability) was suggested as the mechanism for the context-gathering step specifically — giving the agent tool access to a news/search API, current open positions, and recent trade history, rather than stuffing all of that into the prompt statically every call.

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

**Extension worth building alongside this (user's notes, 2026-08-26): rank ALL simultaneous opportunities against each other, don't just score each one in isolation.** If a scan finds 7 candidate signals across different instruments but there's only risk budget for 3, the useful question isn't "does each pass the bar" — it's "which 3 have the best risk-adjusted opportunity right now":
```
AI OPPORTUNITY RANKING
1. XAUUSD     91/100   ███████████████████
2. EURUSD     87/100   █████████████████
3. SP500      82/100   ████████████████
4. USDJPY     77/100   ███████████████
5. AAPL       71/100   █████████████
6. GBPUSD     64/100   ███████████
7. NASDAQ     58/100   █████████
```
This turns the scoring layer into a portfolio-wide allocation decision, not just a per-trade pass/fail gate — relevant to both #2 (this item) and #17 (Portfolio Optimization) below.

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

**A separate, more concrete v1 scope decision exists too** (see "Third contribution" section above): user chose **Signal filter + Position sizing advisor** as the actual v1 mandate — narrower than any of the ranked feature lists below, and framed around agent *role* (approve/reject, resize) rather than feature *category*. This may be the real starting point regardless of which feature-priority ordering below is chosen — worth reconciling all of these into one plan on Friday rather than picking a list in isolation.

**Signal Scoring vs. Regime Detection ordering — ✅ RESOLVED, and it turned out not to be a real conflict.** Regime is an *input feature* to signal scoring, not a competing phase: build the regime classification first, but as a cheap deterministic/statistical calculation in code (ADX, ATR, MA slope, volatility bands) — not an LLM call, not its own agent — then feed that label into the AI Trade Score as one of its factors. **Phase 1 is Signal Scoring, with regime computed as part of the same phase**, not two sequential phases. See "RESOLVED — the actual v1 scope" at the top of this document.

**Testing discipline**: every item tested on SIM first, same standing rule already applied to every strategy/pair change in this codebase (see `feedback_no_core_logic_changes` memory) — nothing here touches either LIVE account until proven on SIM, and per the governance principles above, no model goes live without walk-forward validation and paper trading first regardless of backtest results.

---

## Task list — starting Friday 2026-08-28, one at a time

**Full phased implementation plan (sprints 0-5, files, hook points, and test gates for each) is in [`docs/atos_ai_implementation_plan.md`](atos_ai_implementation_plan.md) — testing happens within each sprint, not deferred to the end.**

**Actual v1 sprint (resolved 2026-08-26 — this is what Friday starts with):**
- [ ] Regime classifier as a plain code function (ADX/ATR/MA-slope/vol-bands → a regime label) — no LLM, no agent, just a feature
- [ ] Single consolidated agent, one structured JSON call, combining Signal Scoring (0-100, regime as one input factor) + Position Sizing (bounded multiplier only, no SL/TP adjustment)
- [ ] Shadow mode first: agent evaluates every real SIM signal, logs its decision next to the actual outcome, influences nothing — gated on accumulating enough tracked outcomes (including at least one adverse/volatile stretch) to judge it, not on a fixed time window
- [ ] Only after shadow mode looks good: Level 2 semi-autonomous (agent can skip/resize within the existing fixed risk limits) — still SIM only, still both accounts' history before LIVE

**Everything below this line is the roadmap for later phases, not the Friday sprint:**

- [ ] #1 Market Regime AI (trend/range/high-vol/low-vol classifier from existing ATR/ADX data — feeds nearly everything below)
- [ ] #4 Trade Quality / Probability Model (expected-value framing: win probability × reward − loss probability × risk)
- [ ] #2 AI Signal Scoring (0–100 score per signal — extends `signal_filter.py`'s existing ML gate rather than replacing it)
- [ ] #3 AI Strategy Selector (regime-conditioned extension of `strategy_learner.py`'s existing weighting)
- [ ] #5 AI Trade Veto (sits before the Risk Engine, never replaces it)
- [ ] #11 Volatility Prediction
- [ ] #12 Anomaly Detection (feeds a "defensive mode" concept, most relevant to LIVE)
- [ ] #16 Open Position AI / Trade Management (continuous monitoring of held positions, not just entry-time scoring)
- [ ] #13 Portfolio Correlation AI (exposure concentration across instruments, not just position count)
- [x] **#18 AI Trading Journal — SHIPPED 2026-08-31.** `ai/features/trade_journal.py`: one batched LLM call per trading day over that day's CLOSED trades (paired entry+exit observation cards + AI shadow verdict + exit-advisor flags) → per-trade entry/exit quality, why-won/lost, one lesson, tags + a daily pattern summary. **Strictly read-only** w.r.t. all trading state (user requirement; locked by tests). Runs from `daily_summary.py`; `python ai_trade_journal.py --report`. `config/ai.json journal_enabled`. See `docs/atos_ai_tracker.md`.
- [ ] #6 Dynamic Position Sizing, #9 Stop-Loss Intelligence, #10 Take-Profit Intelligence (once regime detection exists to condition on)
- [ ] #7 AI Entry Optimization
- [ ] #17 AI Portfolio Manager (whole-book view, builds on #13)
- [ ] #14 Economic Event Intelligence (start with a hard-coded calendar blackout, not full NLP)
- [ ] #15 Sentiment Analysis, #19 Strategy Discovery, #20 AI Model Evolution pipeline (lowest priority, most speculative — #19/#20 also define the promotion process every earlier model should retroactively be held to)
