# ATOS AI — Work Tracker

Running log of the AI layer's build. Update this file **whenever an AI change lands** (commit, decision, blocker cleared). It is the single "where are we" reference.

- **Vision / governance:** [`docs/atos_ai_roadmap.md`](atos_ai_roadmap.md)
- **Sprint plan / test gates:** [`docs/atos_ai_implementation_plan.md`](atos_ai_implementation_plan.md)
- **Kill switch:** `ai/config.py`. `ai_enabled_for(env)` = may observe/log (sim + live shadow). `can_apply_decision(env)` = may change an order — hardcoded to `sim` only (`_AI_ACTING_ACCOUNTS = {"sim"}`). Set every `config/ai.json` flag to `false` (or delete the file) → every hook inert next cycle.

---

## Current state (2026-09-01)

| | |
|---|---|
| **AI live in production?** | **Shadow study RUNNING** (`327e204`) — `enabled_sim` + `enabled_live_shadow` + `agent_enabled` all true. `claude-sonnet-5` scores every RSI signal on SIM + both LIVE accounts, logs it, **applies nothing** (`shadow_mode` true on SIM; `can_apply_decision` hardcoded False for LIVE). **AI Trading Journal** also live (`journal_enabled:true`) — read-only per-trade retrospective, all forex accounts. |
| **AI touching money (SIM or LIVE)?** | **No.** Sprint 4 code (SIM sizing) is shipped but inert. LIVE acting is impossible without a code change (`_AI_ACTING_ACCOUNTS = {"sim"}`). |
| **Last thing shipped** | Copilot prompt fix — was 21/21 MODIFY, now APPROVE/MODIFY/REJECT contrast (`2a554f3`, live A/B 4 APPROVE / 1 MODIFY) · `MAX_TOKENS`→2048 + truncation salvage (`d00c019`) · shadow-study **green heartbeat email** 09:00 + 21:00 PKT + daily-digest section (`bcf5407`) · AI-data integrity sweep — net-P&L gate, stale-chart-bar repair, `verify_ai_data.py`, MAE/MFE write-time cap, ledger dedup (`e292ce4`→`c9751e1`) · fill-price confirmation + Saxo closed-trade reconciliation (`2aa38c0`, `7bd1f31`) |
| **Operator tasks** | ✅ Reboot · ✅ Anthropic console monthly spend cap **$10** · ✅ "ATOS AI Health Email" task registered (09:00 + 21:00 PKT) |
| **`anthropic` SDK** | `1.2.0`. `ANTHROPIC_API_KEY` set (User scope), inherited by scheduled tasks — verified with real `agent_meta.ok=true` calls on SIM + `live` + `live_eur` (2026-09-01: 21 real timed decisions, 5–28 s latency). |
| **Data the AI reads** | Post the 2026-09-01 sweep: entry/exit = **real Saxo fills** (LIVE re-verified vs `closedpositions` every run); net P&L sanity-gated for the unreliable SIM feed; stale SIM chart bars repaired to the live quote; MAE/MFE capped at write time; ledger deduped. `python verify_ai_data.py` = repeatable audit (currently 3 pre-fix historical rows, flagged). |

### ⏭️ NEXT — nothing to build; two things accumulating

1. **Shadow evidence (M4)** — **21 decisions** logged (SIM+LIVE) as of 2026-09-01, target ~40. The prompt fix (`2a554f3`) means decisions from here carry real APPROVE/MODIFY/REJECT variety (before, 21/21 were MODIFY). Weekly: `python ai_shadow_report.py`. Then the **M5 review**, then flip `config/ai.json shadow_mode → false` to activate Sprint 4 on SIM.
2. **Clean give-back data (P2)** — the 2026-09-01 MAE/MFE + fill-price + net-P&L fixes mean clean data only starts now (~15–30 SIM closes/day). Weekly: `python report_giveback.py`. When mature (~1 week, ≥10–15 clean trades/strategy), it drives the exit-improvement / SuperTrend-V2 decision.

Everything else (opportunity ranking, calendar blackout, correlation gate, PLN-slippage measurement) is queued — see **Next modules queue** below.

### Pipeline health monitoring (`ai_shadow_health.py`, 2026-08-31)

The AI layer is wrapped in try/except everywhere: a broken `ANTHROPIC_API_KEY` / hit spend cap / import break makes `evaluate_proposal()` return `HOLD` silently **and a shadow row is still written** — so the file grows at the normal rate while nothing is actually evaluated. `scheduler_watchdog.py` only checks the *task ran*, not the AI sub-layer.

`ai_shadow_health.check()` closes that gap — called every watchdog pass (main watchdog only, under one dedup key "AI Shadow Health", same 4h re-alert + email path). Flags:
- **degraded rate** — >30% of the last 7d of decisions came back HOLD/degraded on a ≥8 sample;
- **silent hook** — ≥3 distinct agent-eligible signals logged as proposals in 48h, none ever got a decision;
- **total silence** — neither `data/ai_*.jsonl` written in >96h while the study is on.
Runnable by hand: `python ai_shadow_health.py`. Tests: `test_2026_08_31_ai_shadow_health.py` (16 ✅). Only fires when `config/ai.json` has the study on; silent otherwise.

**Positive heartbeat (2026-09-01, `bcf5407`)** — the watchdog only emails on *problems*, so a healthy bot was silent. Added: `daily_summary._ai_health_section()` (GREEN/RED banner + decisions-24h / proposals-24h / LLM-ok-rate / last-decision-age / 7d-verdict-mix, in the 23:30 digest) and `ai_shadow_health.py --email` / `heartbeat_html()` — a standalone status email scheduled as **"ATOS AI Health Email", 09:00 + 21:00 PKT** (`setup_ai_health_email.ps1`, in `scheduler_watchdog.WINDOWS_TASKS`). `test_2026_09_01_ai_health_email.py` (7 ✅).

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
| M3.6 | **AI Trading Journal** — read-only per-trade retrospective, SIM + both LIVE | ✅ | 2026-08-31 | `test_2026_08_31_ai_trade_journal` (24 ✅); first run found the MAE/MFE bug |
| M3.7 | MAE/MFE measurement fixed + 68 corrupted historical cards invalidated | ✅ | 2026-09-01 | `test_2026_09_01_mae_mfe_window_fix` (10 ✅) |
| **M4** | **Shadow evidence: ~40+ resolved decisions spanning a user-confirmed adverse stretch** | 🟡 **in progress** (21 so far; prompt fixed `2a554f3` so verdicts now vary) | — | `ai_shadow_report.py`; accumulating SIM + LIVE since 2026-08-31 |
| M4b | **Clean give-back evidence** — ≥10–15 clean (post-MAE-fix) closed trades per strategy | 🟡 **in progress** (0 so far) | — | `report_giveback.py`; accrues ~15–30/day from 2026-09-01 |
| M5 | Review: does APPROVE beat REJECT on expectancy through a rough patch? | ⬜ | — | user + report, in conversation |
| M5b | Review the give-back distribution — which strategies (if any) systematically fail to monetise favorable excursions? | ⬜ | — | user + `report_giveback.py`, in conversation |
| M6 | Sprint 4 — agent's `size_multiplier` changes SIM order size (Level 2, SIM only) | 🟡 **code shipped inert** 2026-08-31; activation blocked on M4/M5 | — | `test_2026_08_31_ai_sprint4_sizing_hook` (14 ✅); SIM A/B pending activation |
| M7 | Sprint 5 — separate written go/no-go before anything further. LIVE acting is its own decision, not in this plan. | ⬜ | — | — |

Autonomy ladder: currently **Level 1** (advisory, shadow). M6 = **Level 2** (semi-autonomous, SIM only). That is the ceiling of the current plan. The Journal (M3.6) and give-back analysis (M4b) are a **diagnostic layer** — always read-only, never on the autonomy ladder.

---

## Next modules queue (priority order)

Nothing here is started. Each is gated on the evidence above and follows the governance rule: **AI observes → human/quant hypothesis → deterministic code → backtest → validation → deploy.**

| # | Module | Gate / when | Notes |
|---|---|---|---|
| 1 | **M5 review + Sprint 4 activation** | M4 (~40 shadow decisions + adverse window) | Flip `shadow_mode → false`; run the SIM A/B + kill-switch drill first. |
| 2 | **P2 give-back review** | M4b (~1 week of clean data) | `report_giveback.py` per strategy × account. If a strategy shows (e.g.) >35% of ≥2R trades finishing <0R → hypothesis for a strategy-specific exit change. **This is what decides whether SuperTrend V2 / an exit rule gets built.** |
| 3 | **Opportunity ranking (roadmap #2 ext)** | after M5 (Copilot proven on rsi) | Rank a scan's candidate signals against each other, take the best N instead of first-come-first-served. Biggest "more profitable" lever. Touches entry selection → shadow-first. |
| 4 | **P3 — portfolio correlation / concentration gate** | after P2 | `ml` + `advanced_ml` both long XAUTRY = one thesis, not two independent opportunities. Portfolio-level exposure check, deterministic, not inside individual strategies. |
| 5 | **P4 — PLN-cross stop-slippage measurement** | independent | Log intended-stop → actual-fill at stop-execution time (small runner change), then measure slippage in ATR/R and its expectancy impact. Some "strategy losses" may be execution losses. |
| 6 | **Economic-calendar blackout (roadmap #14, cheap version)** | after Sprint 4 | Hard-coded high-impact windows (NFP/FOMC/ECB/CPI) → no new entries / wider stops. No NLP. Biggest loss-avoidance lever. Shadow-first. |
| 7 | **Add `pullback` (then `bb`) to the shadow agent** | after M5 | One-line `config/ai.json agent_strategies` change. Second-wave A/B once rsi has proven the Copilot has edge. |
| 8 | **Exit Advisor Stage B** | after P2 | ML on the observation cards (Stage A deterministic scorer already shadow-logging). |
| — | **SuperTrend V2** (user-provided, prior session — **file not in repo**, must be re-shared) | after P2 | Do not build against corrupted give-back evidence. Decision comes out of module #2. |

Deferred to the roadmap's later-phase list: full news/sentiment agent, strategy discovery (#19), the 6-agent split, adaptive scan cadence.

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
| **AI Trading Journal** — `ai/features/trade_journal.py`: batched LLM calls (≤8 trades each) over a day's CLOSED trades (paired entry+exit observation cards + AI shadow verdict + exit-advisor flags) → per-trade `entry_quality`/`exit_quality`/`why_result`/`lesson`/`tags` + a `day_summary`, appended to `data/ai_trade_journal.jsonl`. **STRICTLY READ-ONLY** — imports nothing trade-capable, no order/position/stop mutation, writes only its own file (locked by 3 dedicated tests). Covers **all forex accounts — SIM + both LIVE** (no account filter; every forex trade writes an observation card). Runs from `daily_summary.py` (best-effort, before the email) + `python ai_trade_journal.py [--report]`. Gated by `config/ai.json journal_enabled` (independent of the shadow study). Forex module only (not futures/etf/stocks) for v1. | #18 | (this commit) | **LIVE** — `journal_enabled:true`. ~1 call/day per 8 closes. |
| **P2 give-back analysis** — `report_giveback.py` (read-only). Over the observation cards: MFE_R / Final_R / Giveback_R (all ÷ initial risk), "went our way then went bad" rules (MFE≥1R→final<0.25R, ≥2R→<0R, ≥3R→<1R), a lifecycle distribution (loss / small win / large win → avg MFE_R + give-back), **broken down by strategy AND by account** (user: never a global rule). Skips the pre-fix `mae_mfe_invalidated` trades; buckets `mae_mfe_coarse` (intraday) separately. `test_2026_09_01_giveback_report.py` (9 ✅). | — (P2) | (this commit) | **Waiting for data** — 0 clean trades until the 2026-09-01 MAE fix accrues (~15–30 SIM closes/day). Re-run weekly. Its output is a hypothesis, not a change. |
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
- **2026-08-31 (AI Trading Journal, roadmap #18)** — user picked it as the next build, with a hard requirement: **strictly read-only w.r.t. trading state during the shadow phase**. Shipped `ai/features/trade_journal.py` + `ai_trade_journal.py` CLI + a `daily_summary.py` section, `config/ai.json journal_enabled:true`. Batched LLM calls, ≤8-trade chunks (`CHUNK_SIZE=8`, `EVAL_TIMEOUT_S=120`, `MAX_TOKENS=16000`; first chunk carries whole-day context + emits the `day_summary`; salvages a truncated response for whatever complete objects it holds; one summary per day). Covers **SIM + both LIVE forex accounts** (no account filter — user requirement; every forex trade writes an observation card). ~$0.02–0.10/day. Read-only enforced by `test_2026_08_31_ai_trade_journal.py` (24 ✅): no trade-capable imports, no order-mutation calls in source, write-mode `open()` only ever targets `JOURNAL_LOG`, inputs byte-unchanged across a run. First real run's `day_summary` independently flagged: the corrupted MAE/MFE bug, a "won on paper, lost on the tape" give-back/cost pattern, and duplicate correlated signals (ml+advanced_ml on XAUTRY; pullback+advanced_pullback_master on NZDPLN).
- **2026-09-01 (MAE/MFE window bug — journal's first real find, fixed) `4e2edb8`** — `forex/runner._run_exits` measured MAE/MFE over the **full ~350-bar daily chart window** (`since_entry = df`), not the holding period → 67 of 68 closed trades had a corrupted excursion (MAE −9,412 EUR vs 76 EUR risk). Fix: `_bars_for_excursion()` bounds to calendar-days-held + buffer; intraday strategies (`gap`/`gap_weekend`/`london_breakout*`) get one daily bar, flagged `mae_mfe_coarse`; an excursion > `_MAE_MFE_SANE_R` (25×) entry-risk is rejected, not logged. `fix_observation_card_mae_mfe.py` nulled the 68 historical values (raw kept as `*_raw`, marker `mae_mfe_invalidated`). Clean MAE/MFE accrues forward. `test_2026_09_01_mae_mfe_window_fix.py` (10 ✅). Same commit: **AI Journal now explicitly covers SIM + both LIVE forex accounts** (user requirement — it always had no account filter; made it visible: account column, LIVE weighted heavier, MAE quality flags in the dossier). `CHUNK_SIZE` 15→8, `MAX_TOKENS`→16000, truncated-response salvage, one `day_summary`/day.
- **2026-09-01 (governance rule encoded)** — the AI Journal is a **strictly read-only diagnostic layer**. Findings flow: **journal observes → human/quant hypothesis → deterministic code → backtest → validation → deploy.** The LLM never converts a finding ("systemic missing trailing rule") into a code change. Enforced structurally: the journal module imports nothing trade-capable and its write-mode `open()` only ever targets its own file (AST-checked in tests).
- **2026-09-01 (P2 give-back report) `b252cd7`** — `report_giveback.py` (read-only). MFE_R / Final_R / Giveback_R / Capture, the "went our way then went bad" rules (≥1R→<0.25R, ≥2R→<0R, ≥3R→<1R), a lifecycle distribution, **broken down by strategy AND by account**. Skips the pre-fix invalidated trades; coarse (intraday) trades in a separate loose-bound bucket. 0 clean trades today; matures ~1 week. `test_2026_09_01_giveback_report.py` (9 ✅). Its output is a hypothesis, not a change. Added **Next modules queue** to this file. **SuperTrend V2 (user-provided, prior session) is NOT in the repo** — no `strategy_supertrend_v2.py`, no git history, no stash; must be re-shared before that work starts. Decision to build it (or an exit rule) comes out of the P2 review.
- **2026-09-01 (Copilot sees real economics + pair track record) `4fe4ad5`** — user: "calculate commissions and calculate real profit … determine the real quantity … use real power of AI." The proposal now carries **`trade_economics`** (net of Saxo's flat ~€5.18 round-trip commission: `reward_risk_ratio`, `risk_eur`, `tp_net_after_commission_eur`, `small_win_0p5R_net_eur` — the realistic RSI-bounce case — `breakeven_bounce_R`) and **`pair_history`** (this pair+strategy's `win_rate_pct` / `n_closed` / `avg_pnl_eur` / `profit_factor`, with a `source` flag — LIVE has too few closed trades so it falls back to the SIM `forex` ledger and says so). `_SYSTEM` updated: a commission-dominated small win → MODIFY/REJECT; a well-sampled high pair win rate → lean APPROVE. **Still observe-only on LIVE.** The size lever is Sprint 4's `size_multiplier` — SIM-only, inert until M4+M5, and can only ever **reduce** size (Trade Constitution: "AI may resize, never exceed risk"). "Buy more on a high-WR pair" = amplification = a separate governance decision + backtest, per the user's own observe→hypothesise→code→validate→deploy rule. `test_ai_trade_proposal.py` (11).
- **2026-09-01 (LIVE recovery-vs-cost gate + Copilot follows suit) `31463dc`** — replaced `MIN_LIVE_NOTIONAL_EUR` (and a same-day-discarded pair-specific `LIVE_RSI_MIN_UNITS` table) with ONE pair-independent deterministic gate: reject a LIVE RSI signal when `RSI_LIVE_ASSUMED_EXIT_R (0.5) × realised_R_eur < RSI_LIVE_MIN_RECOVERY_MULT (3.0) × _live_all_in_cost_eur` (flat commission + one spread crossing + 0.5-pip slippage; financing excluded). Reject-not-resize. At fixed €45 risk all 17 HIGH_VOLUME pairs clear it (0.5R÷all-in 3.09–3.88). **AI side:** the proposal's `trade_economics` now uses the **all-in** cost, not commission alone — `all_in_cost_eur`, `tp_net_after_cost_eur` (renamed from `_after_commission_eur`), `recovery_0p5R_to_cost_ratio`; `_SYSTEM` tells the agent a signal it sees has *already* passed the 3.0 floor, so a ratio near 3.0 = thin-margin → trim on MODIFY. `forward_observation.log_cost_gate_decision` gains `realised_r_eur` / `all_in_cost_eur` / `r_to_all_in_cost` — the journal will use these to measure the real median RSI exit and, in ~1 week, replace the provisional `ASSUMED_EXIT_R = 0.5` (deterministic-code change, not an LLM one). **SIM parity:** the all-in cost + realised R are computed identically on SIM (the AI accumulates SIM data too — `notional_eur` is no longer behind an `if _is_live` guard, so SIM proposals + cost-gate telemetry carry the full spread+slippage figure); only the deterministic *block* stays LIVE-only (SIM keeps max forward-test breadth). `test_2026_09_01_live_cost_viability.py` (13: 9 unit + 3 black-box + SIM-parity), `test_ai_trade_proposal.py`, `test_2026_08_26_forex_cost_clearance_gate.py` updated.
- **2026-09-01 (shadow heartbeat + copilot review) `bcf5407` → `2a554f3`** — a positive "AI bot is green" email: `daily_summary._ai_health_section()` (23:30 digest) + `ai_shadow_health.py --email` scheduled as **"ATOS AI Health Email" 09:00 + 21:00 PKT**. Verifying the live shadow log surfaced two copilot fixes: **(1)** `MAX_TOKENS` 1024→2048 + `_salvage_partial()` — one decision was truncated mid-comment and logged as a false HOLD; **(2)** the prompt was **21/21 MODIFY** because it told the agent a low `agreement_count` is "the clearest REJECT case", but rsi/bb/pullback are contrarian so their agreement is structurally ~1. Revised `_SYSTEM`: "START FROM APPROVE" + a STRATEGY FAMILIES section. **Live A/B: the 5 all-MODIFY LIVE proposals → 4 APPROVE, 1 MODIFY.** M5 now has real APPROVE/MODIFY/REJECT contrast. `test_ai_trading_copilot.py` (17).
- **2026-09-01 (AI-data integrity sweep) `e292ce4` → `c9751e1`** — a run of fixes so the journal / give-back / learner read true numbers: **(1)** implausible SIM net P&L gated (`_sane_net_pnl_quote`) — SIM `positions/me` reported +$11 for an MXNUSD rsi that lost $3.91; **(2)** stale SIM forming-bar repaired to the live quote (`_repair_stale_forming_bars` + entry stale-price guard) — root cause of the NZDPLN pullback re-entry loop (frozen Open, bogus 1.8% Ask spread → entry 2.22795 vs real 2.20, born underwater, instant stop, re-fire); **(3)** `verify_ai_data.py` — repeatable read-only audit (impossible commission, P&L sign, price drift, MAE/MFE over-R, unpaired cards, dup open rows); **(4)** MAE/MFE nulled at exit-card write when over the sane-R cap (found 59 `sim:gap:*` cards up to 170R — accumulated pre-fix); **(5)** 1732 duplicate "open" ledger rows deduped + `_close_orphan_ledger_rows()` each cycle (`log_close` never fired for broker-stop exits). `test_2026_09_01_{sane_net_pnl,stale_forming_bar,verify_ai_data}.py` (11+8+11). Residual: ~3 pre-fix historical rows flagged, not nulled.
- **2026-09-01 (Saxo closed-trade reconciliation) `7bd1f31`** — `reconcile_closed_trades_vs_saxo.py`: the deterministic backstop the fill-price fix implied. After every LIVE forex run (`_reconcile_closed_vs_saxo()` in `run_daily`/`run_exits_only`, LIVE envs only), re-checks each recently-closed ledger row + observation card against Saxo's own `ClosedPosition` record while it's still in the ~1-week retention window, and corrects any `entry_price`/`exit_price` drift beyond 3 bp (re-scaling `risk_eur` and `r_multiple` off an FX rate derived from Saxo's `ClosedProfitLoss` vs the card's `net_pnl_eur`). Match is symbol + \|amount\| + side + close-time (Saxo's `ClosedPosition` has no `SourceOrderId`/`AccountKey`); ambiguous matches are flagged, never auto-corrected. **Read-only w.r.t. trading state** (only UPDATEs already-closed rows, never places/cancels/amends an order — AST-checked in tests), never raises. LIVE only — Saxo SIM's `/closedpositions/me` returns HTTP 400. This is the answer to "should the AI take prices from Saxo?": the AI keeps reading one decoupled store; a deterministic verification gate keeps that store true to Saxo. `test_2026_09_01_reconcile_vs_saxo.py` (17 ✅). Manual CLI: `python reconcile_closed_trades_vs_saxo.py --apply`.
- **2026-09-01 (fill-price confirmation) `2aa38c0`** — Saxo's order POST returns only an `OrderId`, no fill/price. `forex/runner.py` + `atos_runner.py` recorded every position at `sig["close"]` (the scan chart's last bar close, 10–60 min stale). Confirmed live: MXNUSD LIVE EUR booked 0.058876 / 0.0588435 vs the real fills 0.058687 / 0.058811 — 0.32% entry error, flipped the recorded P&L sign, poisoned R-multiple / MAE-MFE / observation cards / **P2 give-back**. Same class as the accepted-but-unfilled phantom-position bug on SIM stocks. Fix: `_confirm_entry_fill` / `_confirm_exit_fill` / `_confirm_stock_fill` poll `positions/me` / `closedpositions/me` for the true fill via `PositionBase.SourceOrderId` (fallback: same-Uic <180s); `_run_entries` records the real fill and **cancels + drops a LIVE entry that never fills** (SIM keeps it at a quote / stocks books it paper); `_run_exits` records the true `ClosingPrice`. One-time `fix_live_fill_prices_2026-09-01.py` rewrote the 7 open LIVE positions + the MXNUSD round-trip from the live API — observation cards included (`entry_price` / `risk_eur` / `r_multiple` / gross recomputed, marker `price_source="saxo-fill-truth-2026-09-01"`; the −207 EUR MXNUSD MAE nulled). `test_2026_09_01_fill_confirmation.py` (14 ✅). **The journal / give-back now see the real fill going forward** — was the answer to "will the AI record the truth?".
- **2026-09-01 (operator task done)** — Anthropic console monthly spend cap set to **$10** (~$0.05 used, resets Sep 1 UTC). All operator tasks for the shadow study now complete.
