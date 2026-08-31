# ATOS AI — Work Tracker

Running log of the AI layer's build. Update this file **whenever an AI change lands** (commit, decision, blocker cleared). It is the single "where are we" reference.

- **Vision / governance:** [`docs/atos_ai_roadmap.md`](atos_ai_roadmap.md)
- **Sprint plan / test gates:** [`docs/atos_ai_implementation_plan.md`](atos_ai_implementation_plan.md)
- **Kill switch:** `ai/config.py`. `ai_enabled_for(env)` = may observe/log (sim + live shadow). `can_apply_decision(env)` = may change an order — hardcoded to `sim` only (`_AI_ACTING_ACCOUNTS = {"sim"}`). Set every `config/ai.json` flag to `false` (or delete the file) → every hook inert next cycle.

---

## Current state (2026-08-31)

| | |
|---|---|
| **Last sprint shipped** | Sprint 4 **code** — SIM sizing hook, ships **inert** (`shadow_mode:true` ⇒ `can_apply_decision("sim")` False). Sprint 3 (`80e8b04`) + 3.5 (`aace238`) + model → sonnet (`997aedf`) before it. |
| **Next step** | Accumulate shadow evidence (M4), then the M5 review, then **flip `config/ai.json` `shadow_mode` → `false`** to activate Sprint 4 on SIM. |
| **Sprint 4 status** | **Code shipped inert 2026-08-31.** D1 decided (`FLOOR = 0.25`). Activation still gated on M4 (~40 decisions) + M5 (user confirms an adverse window). Flipping `shadow_mode` is the whole activation — no further code. |
| **AI live in production?** | **Shadow study RUNNING** (`327e204`, 2026-08-31) — `enabled_sim` + `enabled_live_shadow` + `agent_enabled` all true. `claude-sonnet-5` scores every RSI signal on SIM + both LIVE accounts and logs it. **Nothing applied** (`shadow_mode` true on SIM; `can_apply_decision` hardcoded False for LIVE). Pending: console spend cap + a reboot for the scheduled tasks to inherit `ANTHROPIC_API_KEY`. |
| **AI touching LIVE money?** | No — impossible without a code change. LIVE can *shadow-log* (`enabled_live_shadow`) but `can_apply_decision("live")` / `("live_eur")` is hardcoded `False` (`_AI_ACTING_ACCOUNTS = {"sim"}`). |
| **`anthropic` SDK** | `1.2.0`, installed 2026-08-31. `ANTHROPIC_API_KEY` set (User scope). End-to-end verified — `evaluate_proposal()` returned a real APPROVE. |

### ⏭️ NEXT STEP — right now

**Nothing to build.** The shadow study is running; it just needs to accumulate. One operator task left, then wait:

1. **Anthropic console → Settings → Limits → set a monthly spend cap** (e.g. $10). **Still not done.**
2. ✅ **Reboot done (2026-08-31).** Scheduled scans now inherit `ANTHROPIC_API_KEY` — verified: the 16:29 UTC SIM shadow decision has `agent_meta.ok=true` (real `claude-sonnet-5` call).
3. **Wait ~1–3 weeks.** Every RSI signal on SIM + both LIVE accounts gets scored and logged to `data/ai_shadow_decisions.jsonl` (nothing applied).
4. **Weekly:** `python ai_shadow_report.py`. It says "not enough" under 40 decisions, then shows APPROVE-vs-REJECT expectancy + flags candidate rough days.

### Pipeline health monitoring (`ai_shadow_health.py`, 2026-08-31)

The AI layer is wrapped in try/except everywhere: a broken `ANTHROPIC_API_KEY` / hit spend cap / import break makes `evaluate_proposal()` return `HOLD` silently **and a shadow row is still written** — so the file grows at the normal rate while nothing is actually evaluated. `scheduler_watchdog.py` only checks the *task ran*, not the AI sub-layer.

`ai_shadow_health.check()` closes that gap — called every watchdog pass (main watchdog only, under one dedup key "AI Shadow Health", same 4h re-alert + email path). Flags:
- **degraded rate** — >30% of the last 7d of decisions came back HOLD/degraded on a ≥8 sample;
- **silent hook** — ≥3 distinct agent-eligible signals logged as proposals in 48h, none ever got a decision;
- **total silence** — neither `data/ai_*.jsonl` written in >96h while the study is on.
Runnable by hand: `python ai_shadow_health.py`. Tests: `test_2026_08_31_ai_shadow_health.py` (16 ✅). Only fires when `config/ai.json` has the study on; silent otherwise.

The next *build* is **Sprint 4**, and it stays blocked until:
- ~40+ resolved shadow decisions on SIM **AND** the user confirms the window included a real adverse/volatile stretch (D2), and
- the multiplier `FLOOR` is decided (D1).

### Cost controls in place (`aace238`)
| Lever | Effect |
|---|---|
| `agent_strategies: ["rsi"]` | paid call on ~1/20th of signals |
| `agent_dedup: true` | ~10× fewer calls (no re-scoring standing signals every rescan) |
| prompt caching on the system prompt | ~0.1× system-token cost within a scan |
| `claude-sonnet-5` not opus (`997aedf`) | ~5× cheaper per token |
| Est. spend, SIM+LIVE shadow, rsi-only, deduped | **cents/day** — $5 lasts weeks |
Plus: set a hard **monthly spend limit** on the console.

---

## Milestones

| # | Milestone | Status | Date | Evidence / gate |
|---|---|---|---|---|
| M0 | AI package exists, kill switch works, ships OFF | ✅ | 2026-08-28 | `test_ai_config` green, default-off confirmed |
| M1 | Deterministic regime classifier, validated on real history | ✅ | 2026-08-30 | `test_ai_regime_classifier` + `ai_regime_spot_check` match tape |
| M2 | Signal → structured proposal pipe, log-only, zero behaviour change | ✅ | 2026-08-31 | `test_ai_trade_proposal`, regression unchanged |
| M3 | Trading Copilot agent returns a decision; any failure → HOLD, never raises | ✅ | 2026-08-31 | `test_ai_trading_copilot` incl. 20× unreachable-endpoint drill |
| M3.5 | Cost controls + shadow-on-LIVE (log-only, can never act on LIVE) | ✅ | 2026-08-31 | `test_ai_config` two-tier gate asserts; live `evaluate_proposal()` verified |
| **M4** | **Shadow evidence: ~40+ resolved decisions spanning a user-confirmed adverse stretch** | 🟡 **in progress** | — | `ai_shadow_report.py`; decisions accumulating from 2026-08-31 |
| M5 | Review: does APPROVE beat REJECT on expectancy through a rough patch? | ⬜ | — | user + report, in conversation |
| M6 | Sprint 4 — agent's `size_multiplier` changes SIM order size (Level 2, SIM only) | 🟡 **code shipped inert** 2026-08-31; activation blocked on M4/M5 | — | `test_2026_08_31_ai_sprint4_sizing_hook` (14 ✅); SIM A/B pending activation |
| M7 | Sprint 5 — separate written go/no-go before anything further. LIVE acting is its own decision, not in this plan. | ⬜ | — | — |

Autonomy ladder: currently **Level 1** (advisory, shadow). M6 = **Level 2** (semi-autonomous, SIM only). That is the ceiling of the current plan.

---

## Open decisions (owed by the user)

| # | Decision | Where it bites | Status |
|---|---|---|---|
| D1 | Multiplier **`FLOOR`** in `[FLOOR, 1.0]` — how far the agent may shrink a trade | `ai/agent/trading_copilot.py` `MULTIPLIER_FLOOR` + `forex/runner._ai_apply_decision_to_qty` | ✅ **RESOLVED 2026-08-31 → `0.25`** (a MODIFY can cut to at most a quarter; smaller = REJECT). Prompt + coerce clamp + runner helper all updated. |
| D2 | Who rules a shadow-review window **"volatile enough"** to pass the evidence gate | M4/M5 exit criteria | ⬜ Open. Plan's answer stands: the **user**, explicitly, each review, in conversation — `ai_shadow_report.py` only surfaces candidate rough days. Confirm at the M5 review. |

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
  - One in-process LLM call (`agent_model`, default `claude-sonnet-5` since 2026-08-31 — was `claude-opus-5`, switched for cost), 25s timeout, `max_retries=1`. Lazy `import anthropic`.
  - **Resilience (governance #6):** SDK missing / no API key / network / timeout / bad JSON / `stop_reason=="refusal"` → `action:"HOLD"` (no-op the caller ignores). `evaluate_proposal` **never raises**.
- **Hook:** in `_run_entries`, behind a *second* switch `ai_config.agent_enabled_for()` (nested under `ai_enabled_for()`). Stashes `(sym, proposal, decision)` during the loop; after the loop flushes each to `data/ai_shadow_decisions.jsonl` via `log_shadow_decision(prop, dec, entered=sym in entered_syms)`. **Decision is never applied.**
- **Report:** `ai_shadow_report.py` — joins `ai_shadow_decisions.jsonl` against `data/pnl_ledger.db` closed trades: APPROVE-vs-REJECT WR/expectancy, MODIFY multiplier P&L effect, and flags the roughest ledger days for the user to judge. Says "not enough" below 40 decisions.
- **Tests:** `test_ai_trading_copilot.py` — 13 ✅ (valid parse; prose-wrapped JSON; malformed → HOLD; timeout → HOLD; refusal → HOLD; no-SDK → HOLD; multiplier clamp/force; SL/TP strip+downgrade; bad action → HOLD; hook agent-gated + downstream-inert; agent default-off; shadow-row shape; **resilience drill: 20 calls to an unreachable endpoint → 20× HOLD, 0 exceptions**).
- **`requirements.txt`:** `anthropic` added (marked OPTIONAL — only needed when `agent_enabled`).
- **Enable = costs money per signal:** `config/ai.json` `enabled_sim:true` + `agent_enabled:true` + `ANTHROPIC_API_KEY`.
- **Hardening (`22b9f75`, 2026-08-31):** the 19:05 intraday run logged `forex reconciliation failed: cannot import name 'log_shadow_decision'` — a partial-module import race (runner runs as a script, then is re-imported by the post-run safeguard/housekeeping pass; the AI import chain `trade_proposal → regime.classifier → forex.strategy` created a re-entrant edge). Fixed: `classify_regime` now imported **lazily inside `build_proposal()`**; `runner.py`'s `ai.*` imports wrapped in try/except → no-op stubs + `ai_config=None` guard on the hook. The deterministic engine now loads and reconciles even if the AI package fails to import entirely.

### Sprint 4 — wire the multiplier into SIM sizing (Level 2) 🟡 CODE SHIPPED INERT
- **Code shipped 2026-08-31, dormant.** `can_apply_decision("sim")` is False under the committed `config/ai.json` (`shadow_mode:true`), so the hook is a no-op on `main` — same "ships inert" property as Sprints 2 & 3. Activation = flip `shadow_mode` → `false`, gated on M4 evidence + the M5 review.
- **D1 resolved → `FLOOR = 0.25`.** `ai/agent/trading_copilot.py` `MULTIPLIER_FLOOR = 0.25`; system prompt + `_coerce_decision` clamp updated to match.
- **What shipped:**
  - `forex/runner._ai_apply_decision_to_qty(qty, decision, min_units, floor=0.25)` — pure helper. `REJECT → (0, reason)`; `MODIFY → (max(int(qty*m), min_units), note)` with `m` clamped `[floor, 1.0]`; `APPROVE`/`HOLD`/`m≥1.0` → unchanged. Agent can only ever **reduce**.
  - Runner hook in `_run_entries`, **after** `size_position()` and **before** the cost-clearance gate (so the commission check sees the real reduced size). `REJECT` → `continue`, the same skip shape as every deterministic gate. Guarded by `ai_config.can_apply_decision(ACCOUNT_ENV)` + `ai_trading_copilot is not None`.
  - **Dedup bypass when acting:** in shadow mode the paid call is still de-duped once/day/signal; when `can_apply_decision` is True the agent is evaluated on **every rescan** so sizing always has a fresh decision (`_ai_acting` in `_run_entries`). Shadow-log rows stay de-duped (one/day).
  - `log_shadow_decision(..., applied: bool)` — new field, `true` only when `can_apply_decision` was True this run and the action was APPROVE/REJECT/MODIFY. Lets `ai_shadow_report.py` separate "would have" from "did".
- **Test gate:** `test_2026_08_31_ai_sprint4_sizing_hook.py` — 14 ✅ (FLOOR=0.25; helper REJECT/MODIFY/clamp/floor/no-amplify/bad-input; ships inert under committed config; `can_apply` sim-only & out-of-shadow-only; hook placement after sizing / before cost gate; `applied` flag logged; prompt reflects 0.25).
- **Still owed before activation:** SIM A/B (Version A off vs B Level 2) on WR/PF/expectancy/DD over a window incl. a user-agreed volatile stretch; a kill-switch mid-run drill. Run these once there's a real shadow sample and `shadow_mode` is about to flip.

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

- **2026-08-31** — created. Sprints 0–3 recorded ✅; Sprint 4 blocked on D1/D2 + evidence. Import-hardening (`22b9f75`).
- **2026-08-31 (later)** — Sprint 3.5 (`aace238`): shadow-on-LIVE + cost controls. Model → `claude-sonnet-5` (`997aedf`). **Shadow study TURNED ON** for SIM + both LIVE (`327e204`) — `agent_enabled` true, `claude-sonnet-5` scoring every RSI signal, nothing applied. API key set + verified end-to-end. Added the Milestones table (M0–M7, currently at M4 "in progress", Level 1 autonomy). Next step = operator tasks (spend cap + reboot) then wait for evidence; next build = Sprint 4 (M6).
- **2026-08-31 (later still)** — reboot done, scheduled-scan API key inheritance verified live. **`ai_shadow_health.py`** added + wired into `scheduler_watchdog.py` (degraded-rate / silent-hook / total-silence alerting for the shadow pipeline — the try/except-everywhere AI layer would otherwise hide a dead key / hit cap). 3 stale `test_ai_*` "ships OFF" assertions repaired (the committed `config/ai.json` is deliberately enabled now — they test the code-level fail-safe defaults instead). User confirmed **keep `agent_strategies` at `rsi`-only until the M5 review**, then consider adding `pullback` (+ `bb`) as the second wave.
- **2026-08-31 (Sprint 4)** — D1 resolved → **`FLOOR = 0.25`**. **Sprint 4 code shipped inert**: `forex/runner._ai_apply_decision_to_qty` + the `_run_entries` sizing hook (after sizing, before the cost gate), gated by `can_apply_decision` which is False under the committed config. Dedup bypass when acting. `log_shadow_decision` gains an `applied` field. `test_2026_08_31_ai_sprint4_sizing_hook.py` (14 ✅). **Activation = flip `config/ai.json` `shadow_mode` → `false`** once M4 evidence + M5 review clear — no further code, but run the SIM A/B + kill-switch drill first.
