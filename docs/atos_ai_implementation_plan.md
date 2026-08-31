# ATOS AI — Implementation Plan (Sprints, Friday 2026-08-28 start)

> **Status (2026-08-31):** Sprint 0 ✅ (`e5bbf9f`) · Sprint 1 ✅ (`fc0d5b5`) · Sprint 2 ✅ (`cc1a5b1`) · Sprint 3 ✅ (`80e8b04`, hardened `22b9f75`) · Sprint 3.5 ✅ cost-controls + shadow-on-LIVE (`aace238`), model → `claude-sonnet-5` (`997aedf`).
>
> **The shadow study is RUNNING** (`327e204`) — `claude-sonnet-5` scores every RSI signal on SIM + both real LIVE accounts and logs APPROVE/REJECT/MODIFY next to the real outcome. Nothing is applied (shadow on SIM; `can_apply_decision` hardcoded False for LIVE). Now: accumulate ~40+ resolved decisions spanning a user-confirmed adverse stretch, review weekly with `ai_shadow_report.py`.
>
> **Sprint 4 CODE is shipped inert** (2026-08-31) — the SIM sizing hook exists but `can_apply_decision("sim")` is False under the committed `config/ai.json` (`shadow_mode:true`), so it is a no-op on `main`. Activation = flip `shadow_mode → false`, still **BLOCKED** on the evidence sample + the M5 adverse-window review. Run the SIM A/B + kill-switch drill at activation.
>
> Adjacent, feeding the same evidence base: exit-advisor Stage A shadow scorer (`fbc8f94`, roadmap #16/#8), RSI-signal-registry (`5646e30`, roadmap #4). Pipeline health: `ai_shadow_health.py` (wired into `scheduler_watchdog.py`).
>
> **Live progress log: [`docs/atos_ai_tracker.md`](atos_ai_tracker.md)** — every AI commit, test count, decision, and the current blocker, kept current as work lands.
>
> **Open decisions:** (1) multiplier `FLOOR` — ✅ **RESOLVED → `0.25`** (`MULTIPLIER_FLOOR` in `ai/agent/trading_copilot.py`); (2) who rules a review window "volatile enough" — still the user, explicitly, at the M5 review.

Companion to [`docs/atos_ai_roadmap.md`](atos_ai_roadmap.md), which owns the *vision/governance* (v1 scope, autonomy levels, Trade Constitution, JSON schemas). This doc owns the *how and in what order* — six sprints, each with its own deliverable, its own test gate, and its own go/no-go before the next one starts. No sprint begins until the previous one's test gate is green. Nothing here touches LIVE; Sprint 5 (SIM A/B) is the ceiling for this plan, and rolling to LIVE is explicitly out of scope — it requires the separate written decision already mandated in the roadmap doc's governance rules.

Locked scope this plan implements (from the roadmap doc, do not re-litigate): one consolidated agent, not six. Regime is a deterministic code feature (ADX/ATR/MA-slope/vol-bands), not an LLM call. Sizing is a bounded multiplier only — no dynamic SL/TP in v1. Shadow mode is exited on evidence, not a calendar date. Level 2 semi-autonomous is the ceiling; anything past it needs its own separate decision.

---

## Sprint 0 — Scaffolding & the off-switch (half day)

**Goal:** every later sprint has somewhere to live, and there's a single, obvious way to turn AI participation off instantly, before there's anything to turn off.

**Build:**
- New package `ai/` — `ai/__init__.py`, `ai/regime/`, `ai/features/`, `ai/agent/`.
- `ai/config.py`: reads `config/ai.json` (new file, mirrors `config/capital.json`'s pattern). Fields: `enabled_sim: bool` (default `false`), `enabled_live: bool` (hardcoded `false` in code, not just config — LIVE is not reachable from this plan at all, not even behind a flag), `shadow_mode: bool` (default `true`).
- A single `ai_enabled_for(account_env: str) -> bool` function every later hook calls — this is the kill switch. Flipping `config/ai.json`'s `enabled_sim` to `false` (or deleting the file — must fail safe to "disabled") turns off every AI touchpoint without touching `forex/runner.py`.

**Test gate:**
- `test_ai_config.py`: missing file → disabled; malformed JSON → disabled (not a crash); `enabled_sim: true` → enabled for `"sim"`, still disabled for `"live"`/`"live_eur"` regardless of config content.
- Manual: confirm `ai_enabled_for()` is `False` by default on a clean checkout — the feature must ship OFF.

**Exit criteria:** package imports cleanly, config tests green, default-off confirmed. Nothing in `forex/runner.py` changed yet.

---

## Sprint 1 — Regime classifier (pure code, zero AI, ~1 day)

**Goal:** a deterministic function, no model, no API call: price history in, one of a fixed set of regime labels out. This is the safest possible starting point — it's testable against known history the same way `strategy_learner.py` is.

**Build:**
- `ai/regime/classifier.py`: `classify_regime(h1_bars: list[dict]) -> dict` returning `{"label": "TRENDING_BULLISH"|"TRENDING_BEARISH"|"RANGING"|"BREAKOUT"|"HIGH_VOLATILITY"|"LOW_VOLATILITY"|"CHAOTIC", "adx": float, "atr_pct": float, "ma_slope": float}`. Computed from ADX (trend strength/direction), ATR as % of price (volatility), and a fast/slow MA slope — the exact inputs already named in the roadmap doc's regime spec. No new dependency: ADX/ATR are already computed elsewhere in this codebase (strategies use ATR for sizing/stops) — reuse those, don't reimplement.

**Test gate (`test_ai_regime_classifier.py`):**
- Unit tests with synthetic bar series (monotonic uptrend → `TRENDING_BULLISH`; flat noise → `RANGING`; synthetic volatility spike → `HIGH_VOLATILITY`).
- Historical sanity check: pull real H1 history already cached by this codebase for 2-3 known periods and eyeball the labels make sense (e.g. a known high-volatility stretch shouldn't classify as `LOW_VOLATILITY`). This is a spot-check script (`ai_regime_backtest_check.py`, scratch/throwaway is fine), not a permanent test — its job is catching an inverted sign or wrong ATR window before Sprint 2 depends on it.
- Stability check: classify the same symbol bar-by-bar across a week of H1 data, confirm the label doesn't flap every single bar (some persistence is expected of a regime).

**Exit criteria:** unit tests green, historical spot-check labels look sane to a human, no flapping. This function is never called from `forex/runner.py` yet — it's a standalone, fully-tested utility at the end of this sprint.

---

## Sprint 2 — Trade proposal logging, no AI call yet (~1 day)

**Goal:** prove the "signal → structured candidate" pipe end-to-end while it's still completely inert — logging only, zero behavior change, zero risk to real trading.

**Build:**
- `ai/features/trade_proposal.py`: `build_proposal(sym, direction, sig, features, positions, equity, regime) -> dict`, matching the schema already agreed in the roadmap doc (symbol/side/entry/stop/tp/timeframe/strategy_name/signal_strength/account_equity/open_positions/volatility_atr), plus the Sprint 1 regime label folded in.
- One hook in `forex/runner.py`: right after the `signal_filter.evaluate()` gate at [forex/runner.py:1663-1669](../forex/runner.py) — i.e. only for signals that already passed every existing deterministic filter — call `build_proposal(...)` and append it to `data/ai_trade_proposals.jsonl`, guarded by `ai_enabled_for(ACCOUNT_ENV)`. No return value is used; this call cannot change `entries`, `qty`, or anything downstream. If it throws, it must be caught and logged, never allowed to interrupt the entry loop (same non-negotiable as the rest of that loop already follows for order rejections).

**Test gate (`test_ai_trade_proposal.py`):**
- Unit: `build_proposal()` output matches the schema exactly, handles a signal missing optional fields (e.g. `london_breakout`'s pre-computed `units` vs. ATR-based `qty`) without crashing.
- Integration: run a normal SIM dry-run (`--dry-run`, already a supported mode) with `enabled_sim: true`, confirm proposals get written for real firing signals and confirm **zero other output changes** — diff the dry-run log against a run with AI disabled, the only difference should be the new jsonl file appearing.
- Live-ish check: let it run for a day or two of real SIM signals, hand-inspect ~10 real proposals for correctness (does `open_positions` actually reflect what's open, is `account_equity` the real number).

**Exit criteria:** proposals are well-formed and complete across a real day of SIM activity, and — critically — a full regression run of the existing forex test suite (`test_2026_08_25_live_forex_account.py` etc.) is unchanged with the hook enabled. This is the gate that proves the pipe is inert before Sprint 3 puts a real decision-maker behind it.

---

## Sprint 3 — The agent itself, shadow mode only (~2-3 days)

**Goal:** the actual "Trading Copilot" — one consolidated agent, takes a proposal, returns a decision — but the decision is logged, never applied. This is where evidence starts accumulating toward the roadmap's evidence-gate.

**Build:**
- `ai/agent/trading_copilot.py`: `evaluate_proposal(proposal: dict) -> dict`, returns the decision schema already agreed in the roadmap doc (`action: APPROVE|REJECT|MODIFY`, `size_multiplier`, `adjusted_stop_loss`/`adjusted_take_profit` — both forced to `null` in v1, the field exists in the schema but the multiplier-only rule means this agent must never populate them — `comment`). Implementation shape decision: in-process call, not a FastAPI microservice — no operational reason yet to run a second process for one agent (revisit only if latency or resource isolation becomes a real problem, not preemptively).
- Strict JSON-schema validation on the response with a hard fallback: any malformed/missing/timeout response → treated as `action: HOLD` (no-op), logged as an agent failure, and — this is the resilience test from the roadmap's governance principle #6 — must never raise out of the hook and must never block the entry loop.
- Hook: same place as Sprint 2 (right after `build_proposal`), call `evaluate_proposal()`, log proposal+decision together to `data/ai_shadow_decisions.jsonl`, keyed by a trade ID that Sprint 3's reporting can later join against the real trade outcome once it closes. **Still does not touch `qty` or `entries` — decision is recorded, not applied.**
- `ai_shadow_report.py`: joins `ai_shadow_decisions.jsonl` against `pnl_tracker`'s real closed-trade outcomes by trade ID, produces "what the agent would have done vs. what actually happened" — win rate / expectancy of AI-approved vs AI-rejected signals, and specifically whether the agent's judgment holds up during any adverse/volatile stretch in the window (the roadmap's evidence-gate explicitly requires this, not just a smooth-market sample).

**Test gate:**
- `test_ai_trading_copilot.py`: mocked-LLM-response tests — valid response parses correctly; malformed JSON → HOLD, no exception; timeout → HOLD, no exception; response with a non-null `adjusted_stop_loss` → rejected/ignored (v1 must not honor a field outside its locked scope, even if the model tries to use it).
- Resilience drill: deliberately point the agent at an unreachable endpoint for a full SIM run, confirm the entry loop completes exactly as if AI were disabled (this is the "AI unavailable must degrade safely" principle, tested for real, not just asserted in the doc).
- Evidence accumulation: this sprint's *exit* criterion is not a fixed day count — per the roadmap's locked evidence-gate rule, wait for a real sample of shadow decisions (aim for the same order of magnitude as the roadmap's "M live-approved trades" language) spanning at least one visibly volatile or adverse stretch, not just a calm week, before moving to Sprint 4.
- **Who decides "volatile enough":** `ai_shadow_report.py`'s job is to *surface candidates*, not to rule on this itself — e.g. flag the N days in the window with the largest realized drawdown, biggest single-day ATR spike, or most simultaneous stop-outs across pairs, and hand that list to a human. **The call on whether the window genuinely includes a qualifying adverse stretch is the user's, made explicitly when reviewing the report — not an automated threshold the script applies on its own.** This is the one subjective judgment call in an otherwise numeric gate, and it needs to stay visibly a decision, not get quietly absorbed into the script's pass/fail output.

**Exit criteria:** resilience drill passes, shadow report shows the agent isn't systematically worse than a coin flip on approve/reject quality, and the user has explicitly confirmed the sample includes a real rough patch, not only calm days — this confirmation happens in conversation, not inside the script.

---

## Sprint 4 — Wire the decision into SIM order flow, Level 2 (~1-2 days)

**Goal:** the first point where the agent's decision has a *real* effect — on SIM only.

**Build:**
- Modify the sizing step at [forex/runner.py:1695-1712](../forex/runner.py): when `ai_enabled_for(ACCOUNT_ENV)` and `shadow_mode` is `false`, multiply the already-computed `qty` by the agent's `size_multiplier`, clamped to `[FLOOR, 1.0]` — the upper bound of `1.0` is a fixed engineering decision (v1 only ever *reduces* size, never amplifies beyond what the Risk Engine already sized, matching the Trade Constitution's "AI may resize, never exceed risk"), but **`FLOOR` is not yet decided and must not ship as an unexamined placeholder** — it's a business call, not an engineering one, and there's no existing "minimum risk multiplier" concept in this codebase to derive it from (`min_units` on each pair is an absolute-units floor, a different thing). Bring this to Friday explicitly: candidates are something like `0.25` (conservative, multiplier can only halve-then-half-again), `0.1` (agent can nearly zero out size without fully rejecting), or reusing the existing per-pair `min_units` as a hard floor regardless of what the multiplier computes to. Whichever value is picked, write it into `ai/config.py` as a named constant with the reasoning in a comment, not a bare number in the sizing hook. On `action: REJECT`, `continue` the loop before placing the order — same `continue` pattern already used for every other skip reason in that loop (spread, currency exposure, opposing position, etc.), so a rejected trade produces the exact same log/skip shape the loop already produces for other skip reasons.
- This is strictly additive to the existing skip-reason chain at [forex/runner.py:1673-1692](../forex/runner.py) — it does not replace or reorder any existing deterministic check, it runs after all of them.

**Test gate:**
- `test_ai_sizing_hook.py`: multiplier applies correctly and stays within the clamp; REJECT genuinely produces zero order placement calls (mock `saxo_order.place_with_stop`, assert not called); AI disabled or in shadow mode → sizing math is byte-for-byte identical to today's, verified by diffing qty output with the hook on (shadow) vs off.
- A/B run on SIM: the methodology already in the roadmap doc — Version A (AI off) vs Version B (AI on, Level 2) — compared on WR/PF/expectancy/drawdown over a real SIM window, using the exact same evidence-gate discipline as Sprint 3 (must include a volatile stretch, not just a quiet one).
- Kill-switch drill: flip `enabled_sim` to `false` mid-run (or delete `config/ai.json`), confirm the very next cycle reverts to pre-AI behavior with no restart needed if the config is read fresh each cycle (or documents clearly that a restart is required, if it's cached at process start — either is fine, but it must be true and stated, not assumed).

**Exit criteria:** the same evidence bar as Sprint 3's — including the same rule that "adverse stretch qualified" is a user judgment call made in review, not a script threshold — now measured on realized sizing decisions rather than shadow ones: Version B doesn't harm risk-adjusted return relative to Version A, through a stretch the user has explicitly agreed counts. This is Level 2, on SIM, and it is the ceiling for this plan.

---

## Sprint 5 — Decision point, not a sprint (open-ended)

Not a build sprint. This is the explicit, separate go/no-go the roadmap's governance rules require before anything past Sprint 4: reviewing the Sprint 4 evidence together, deciding whether to extend the SIM run longer, extend to the second SIM-side strategy/account, or — only as its own deliberate, separately-approved decision, not an automatic next step — begin discussing what a LIVE trial would require. Nothing here is scheduled or assumed; it's a checkpoint, not a task.

---

## What stays unbuilt in this plan on purpose

Everything from the roadmap doc's later-phase list — dynamic SL/TP, the 6-agent architecture, news/sentiment, portfolio correlation, the trading journal, strategy discovery — stays exactly where it already is in `docs/atos_ai_roadmap.md`'s "Detailed feature specs" and "Build order" sections. This plan implements only the locked v1 scope end to end, deliberately, before any of that is reopened.

## Sequencing summary

| Sprint | Deliverable | Touches real trading? | Exit gate |
|---|---|---|---|
| 0 | `ai/` scaffolding + kill switch | No | Config tests green, default-off confirmed |
| 1 | Regime classifier | No | Unit tests + historical sanity check |
| 2 | Trade proposal logging | No (log-only) | Schema correct, zero behavior diff on regression suite |
| 3 | Agent, shadow mode | No (log-only) | Resilience drill + evidence sample incl. a volatile stretch |
| 4 | Agent wired into SIM sizing | **Yes — SIM only** | A/B evidence bar cleared, kill-switch verified |
| 5 | Review checkpoint | — | Separate written decision before anything further |

## Open items for Friday (not blockers, but not to skip past)

1. **Sprint 4's multiplier floor (`FLOOR` in `[FLOOR, 1.0]`)** is deliberately left undecided in this doc — it's a business call (risk tolerance), not derivable from existing code. Pick a value Friday, not by default.
2. **Who rules a review window "volatile enough" to satisfy the evidence gate (Sprints 3 & 4)** — decided to be the user, explicitly, in conversation, each time — not a threshold `ai_shadow_report.py` applies automatically. Confirm this Friday so it doesn't quietly become the script's call by default once it exists.
