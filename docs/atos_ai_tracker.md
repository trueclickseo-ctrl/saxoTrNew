# ATOS AI — Work Tracker

Running log of the AI layer's build. Update this file **whenever an AI change lands** (commit, decision, blocker cleared). It is the single "where are we" reference.

- **Vision / governance:** [`docs/atos_ai_roadmap.md`](atos_ai_roadmap.md)
- **Sprint plan / test gates:** [`docs/atos_ai_implementation_plan.md`](atos_ai_implementation_plan.md)
- **Kill switch:** `ai/config.py` → `ai_enabled_for(account_env)`. Ships OFF. LIVE excluded in code (`_AI_ALLOWED_ACCOUNTS = {"sim"}`), not just config.

---

## Current state (2026-08-31)

| | |
|---|---|
| **Last sprint shipped** | Sprint 3 — Trading Copilot agent, shadow mode (`80e8b04`), import-hardened (`22b9f75`) |
| **Next sprint** | Sprint 4 — wire the agent's `size_multiplier` into SIM sizing (Level 2, SIM only) |
| **Sprint 4 status** | **BLOCKED** — 2 open decisions + no shadow-evidence sample yet |
| **AI live in production?** | No. `config/ai.json`: `enabled_sim=false`, `agent_enabled=false`. Every hook is a no-op. |
| **AI touching LIVE money?** | No — impossible without a code change (`_AI_ALLOWED_ACCOUNTS`). |
| **`anthropic` SDK** | Installed 2026-08-31 (`1.2.0`, `pip install` as admin). Still dormant — needs `ANTHROPIC_API_KEY` + config flags. |

### To start collecting Sprint 3/4 evidence (when ready)
1. Set `ANTHROPIC_API_KEY` as a User env var.
2. `config/ai.json`: `enabled_sim: true`, `agent_enabled: true` (keep `shadow_mode: true`).
3. Let SIM run. Each RSI/other signal now logs a proposal + an LLM decision to `data/ai_shadow_decisions.jsonl` — **decision is never applied**.
4. Review weekly: `python ai_shadow_report.py`.
5. Gate to Sprint 4 = ~40+ resolved decisions **AND** the user explicitly agreeing the window contained a real adverse/volatile stretch.

---

## Open decisions (owed by the user)

| # | Decision | Where it bites | Current placeholder | Options |
|---|---|---|---|---|
| D1 | Multiplier **`FLOOR`** in `[FLOOR, 1.0]` — how far the agent may shrink a trade | `ai/agent/trading_copilot.py` `MULTIPLIER_FLOOR`; consumed by Sprint 4 sizing hook | `0.10` | `0.25` (can only quarter) · `0.10` (near-zero without a full REJECT) · "never below the pair's `min_units`" |
| D2 | Who rules a shadow-review window **"volatile enough"** to pass the evidence gate | Sprints 3 & 4 exit criteria | Plan says: the **user**, explicitly, each review, in conversation — `ai_shadow_report.py` only surfaces candidate rough days | Confirm the plan's answer, or define a numeric rule instead |

---

## Sprint ledger

### Sprint 0 — scaffolding & kill switch ✅
- **Commits:** `6f95903` → re-landed `e5bbf9f` (post PR #1 rebase)
- **Shipped:** `ai/` package (`__init__`, `regime/`, `features/`, `agent/`); `ai/config.py` with `ai_enabled_for()`, `shadow_mode()`, `config_path()`; `config/ai.json` (ships `enabled_sim:false`). LIVE excluded in code.
- **Tests:** `test_ai_config.py` — 7 ✅ (missing file → disabled; bad JSON → disabled not crash; `enabled_sim:true` → sim only, never live/live_eur).
- **Notes:** feature ships OFF on a clean checkout. Nothing in `forex/runner.py` changed.

### Sprint 1 — regime classifier (pure code, no LLM) ✅
- **Commits:** `cc1ad8c` → re-landed `fc0d5b5`
- **Shipped:** `ai/regime/classifier.py` → `classify_regime(bars) -> {label, adx, plus_di, minus_di, atr_pct, atr_ratio, ma_slope, confidence}`. 7 labels: `TRENDING_BULLISH/BEARISH`, `RANGING`, `BREAKOUT`, `HIGH_VOLATILITY`, `LOW_VOLATILITY`, `CHAOTIC` (`NEWS_DRIVEN` deferred — needs the news layer). Wilder ADX+DI for trend (fixed thresholds `ADX_TREND=25`, `ADX_WEAK=15`); ATR vs 60-bar median for volatility (`ATR_RATIO_HIGH=1.6`, `ATR_RATIO_LOW=0.6` — relative, pair-agnostic). Reuses `forex.strategy._adx/_atr/_ema`. `MIN_BARS=65`.
- **Decision order:** CHAOTIC → BREAKOUT → HIGH_VOL → LOW_VOL → TRENDING_* → RANGING.
- **Tests:** `test_ai_regime_classifier.py` — 12 ✅ (synthetic trend/range/vol-spike/quiet/breakout; list-of-dict == DataFrame; no flapping across a moving window; **asserts NOT imported into `forex/runner.py`** — Sprint 1 exit criterion).
- **Spot check:** `ai_regime_spot_check.py` on real data — EURUSD→TRENDING_BULLISH, USDJPY→TRENDING_BEARISH, GBPJPY/EURGBP→RANGING (all match tape).
- **Not wired into the runner.** Standalone utility.

### Sprint 2 — trade-proposal logging (inert, log-only) ✅
- **Commit:** `cc1a5b1` (direct to main, post-merge)
- **Shipped:** `ai/features/trade_proposal.py` → `build_proposal(...) -> dict` (roadmap schema: `ts/account_env/symbol/side/entry_price/stop_loss/take_profit/timeframe/strategy_name/signal_strength/raw_score/agreement_count/ml_prob/account_equity/open_positions/n_open_positions/volatility_atr/atr_pct/rsi2/regime{...}`). `timeframe` = `H1` for `london_breakout*`/`gap*` else `D1`. `log_proposal()` → `data/ai_trade_proposals.jsonl`. `trade_id(p)` = `account|strategy|symbol|date`.
- **Hook:** exactly ONE in `forex/runner._run_entries`, right after `signal_filter.evaluate()` passes, before sizing. Guarded by `ai_config.ai_enabled_for()`, wrapped in try/except. `regime_data=market_data` threaded through `_run_entries` for the regime label. Test asserts the hook sits between the filter gate and sizing and mutates nothing (`entries`, `positions[...]`, `place_with_stop`, `_post`, `size_position` all absent from the hook block).
- **Tests:** `test_ai_trade_proposal.py` — 7 ✅.
- **Enable = pure logging, free:** `config/ai.json` `enabled_sim:true`.

### Sprint 3 — the Trading Copilot agent, shadow mode ✅
- **Commits:** `80e8b04`; hardened `22b9f75`
- **Shipped:** `ai/agent/trading_copilot.py` → `evaluate_proposal(proposal) -> {action, size_multiplier, adjusted_stop_loss, adjusted_take_profit, comment, _agent}`.
  - `action ∈ {APPROVE, REJECT, MODIFY}` — else coerced to `HOLD`. **v1 = multiplier-only:** `adjusted_stop_loss`/`adjusted_take_profit` forced `null`; a model that sets them gets it stripped and a MODIFY-that-only-touched-SL/TP is downgraded to APPROVE.
  - `size_multiplier` clamped to `[MULTIPLIER_FLOOR, 1.0]`; forced `1.0` for APPROVE/REJECT (agent can only ever **reduce** size).
  - One in-process `claude-opus-5` call, 25s timeout, `max_retries=1`. Lazy `import anthropic`.
  - **Resilience (governance #6):** SDK missing / no API key / network / timeout / bad JSON / `stop_reason=="refusal"` → `action:"HOLD"` (no-op the caller ignores). `evaluate_proposal` **never raises**.
- **Hook:** in `_run_entries`, behind a *second* switch `ai_config.agent_enabled_for()` (nested under `ai_enabled_for()`). Stashes `(sym, proposal, decision)` during the loop; after the loop flushes each to `data/ai_shadow_decisions.jsonl` via `log_shadow_decision(prop, dec, entered=sym in entered_syms)`. **Decision is never applied.**
- **Report:** `ai_shadow_report.py` — joins `ai_shadow_decisions.jsonl` against `data/pnl_ledger.db` closed trades: APPROVE-vs-REJECT WR/expectancy, MODIFY multiplier P&L effect, and flags the roughest ledger days for the user to judge. Says "not enough" below 40 decisions.
- **Tests:** `test_ai_trading_copilot.py` — 13 ✅ (valid parse; prose-wrapped JSON; malformed → HOLD; timeout → HOLD; refusal → HOLD; no-SDK → HOLD; multiplier clamp/force; SL/TP strip+downgrade; bad action → HOLD; hook agent-gated + downstream-inert; agent default-off; shadow-row shape; **resilience drill: 20 calls to an unreachable endpoint → 20× HOLD, 0 exceptions**).
- **`requirements.txt`:** `anthropic` added (marked OPTIONAL — only needed when `agent_enabled`).
- **Enable = costs money per signal:** `config/ai.json` `enabled_sim:true` + `agent_enabled:true` + `ANTHROPIC_API_KEY`.
- **Hardening (`22b9f75`, 2026-08-31):** the 19:05 intraday run logged `forex reconciliation failed: cannot import name 'log_shadow_decision'` — a partial-module import race (runner runs as a script, then is re-imported by the post-run safeguard/housekeeping pass; the AI import chain `trade_proposal → regime.classifier → forex.strategy` created a re-entrant edge). Fixed: `classify_regime` now imported **lazily inside `build_proposal()`**; `runner.py`'s `ai.*` imports wrapped in try/except → no-op stubs + `ai_config=None` guard on the hook. The deterministic engine now loads and reconciles even if the AI package fails to import entirely.

### Sprint 4 — wire the multiplier into SIM sizing (Level 2) ⏳ NEXT, BLOCKED
- **Not started.** Blocked on D1 + D2 above and a real shadow-evidence sample.
- **Plan:** when `ai_enabled_for(env)` and `shadow_mode==false`, multiply the Risk-Engine `qty` by `size_multiplier` (clamp `[FLOOR,1.0]`); on `REJECT` → `continue` (same skip shape as spread/exposure/opposing-position skips). Strictly additive, runs after every deterministic check. SIM only.
- **Test gate:** `test_ai_sizing_hook.py` (multiplier applies + clamps; REJECT → `place_with_stop` not called; shadow/off → qty byte-for-byte identical); SIM A/B (Version A off vs Version B Level 2) on WR/PF/expectancy/DD over a window incl. a user-agreed volatile stretch; kill-switch mid-run drill.

### Sprint 5 — decision checkpoint (not a build) ⬜
Separate written go/no-go before anything past Sprint 4. LIVE is out of scope for this plan entirely.

---

## Adjacent AI-roadmap work (not sprints, feeds the same evidence base)

| Item | Roadmap ref | Commit | State |
|---|---|---|---|
| **Exit Advisor Stage A** — deterministic give-back-risk scorer (`forex/exit_advisor.py`), `score()→HOLD/TIGHTEN/EXIT`, `EXIT_ADVISOR_MODE="shadow"`, logs to `data/exit_advisor_shadow.jsonl`, never touches a stop. Report: `report_exit_advisor.py`. | #16 / #8 | `fbc8f94` | Shadow. Stage B (ML on cards) + Stage C (`docs/atos_exit_advisor.md`) later. |
| **RSI(2) signal registry** — `forex/rsi_signal_registry.py` logs every RSI trigger in the study band (≤15 long / ≥85 short), forward-resolves each, `report_rsi_thresholds.py` buckets ≤5/7/10/12/15 by expectancy-after-cost. Observe-only; `RSI_OVERSOLD=10` unchanged. | #4 (Trade Probability) | `5646e30` | Logging. Needs ~40–80 resolved/account, then the report answers "best threshold". |

---

## News / sentiment integration (roadmap #14) — deferred, by decision

Not in the v1 sprint plan. Cheap interim version (hardcoded economic-calendar blackout windows — no entries N minutes around known high-impact releases) is a candidate **after Sprint 4**, as its own small deliverable. The full news/sentiment agent stays in the roadmap's later-phase list.

---

## Change log for THIS file

- **2026-08-31** — created. Sprints 0–3 recorded as ✅; Sprint 4 logged as blocked on D1/D2 + evidence. Import-hardening (`22b9f75`) noted under Sprint 3.
