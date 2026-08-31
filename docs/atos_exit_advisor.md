# ATOS Exit Advisor — the "AI profit scan" for open positions

**Goal:** for every open position, decide *when to exit / take profit* better
than the current rule stack (RSI-recovery, 2R broker TP, 12-day time stop,
1.5×ATR hard stop, and the RSI profit ladder). Motivated by real give-back:
a GBPPLN position ran +30 → −24 PLN; a SIM RSI trade reached €153 MFE and
exited at −€24 (capture ratio −0.16).

The build is deliberately staged. **Nothing touches a real order until the
shadow record proves it beats the rules.**

---

## Stage A — deterministic give-back-risk scorer  *(live now, shadow mode)*

`forex/exit_advisor.py` · `score(pos, df, strat_name) → {score 0–100, recommendation, signals}`

Pure function, no I/O. Recommendation: `HOLD` (<45) / `TIGHTEN` (45–69) / `EXIT` (≥70).

| Signal | Contribution |
|---|---|
| **give-back fraction** `(mfe_R − r_now) / mfe_R` | up to +50 — the dominant term |
| was ever ≥ 1R in profit | +10 |
| ATR expanding (>1.25×) **and** giving back (>25%) | +15 — exits get worse fast |
| RSI **not** heading to its own exit while price fades (>30% give-back) | +15 — recovery won't save it soon |
| late in the trade (`days_held / 12 > 0.6`) | +10 |
| stop already within 0.15R of price | −15 — the ladder already has this |

**Wiring:** `forex/runner.py` `_run_exits`, after the MAE/MFE update, before
`should_exit`. `EXIT_ADVISOR_MODE = "shadow"` — every cycle it logs a row to
`data/exit_advisor_shadow.jsonl` and **never** mutates a stop or places an
order. There is deliberately **no `"active"` code path**.

Runs for **every open position on every account** (SIM + both LIVE) — the
scorer is strategy-agnostic; only the *training data* focus is RSI for now.

**Scorecard:** `python report_exit_advisor.py` — joins the shadow log against
the real exit outcome per closed trade and reports, for trades the advisor
flagged EXIT: `edge_R = advisor_exit_r − actual_exit_r` (>0 = advisor kept
more of the move, <0 = advisor clipped a winner), plus a per-trade table.

**Promotion criteria (A → limited active):** ≥ ~25–40 EXIT-flagged closed
trades, mean `edge_R` clearly positive (> +0.1 R/trade), and *no* single
large negative outlier that a human wouldn't have taken. First "active" step
would be **TIGHTEN-only** (move the stop, never a market close), still with
the ladder underneath.

---

## Stage B — ML classifier  *(later, once data allows)*

Same features as Stage A plus the raw observation-card fields, trained on the
accumulated closed trades (`data/trade_observation_cards.jsonl` — needs ~100+
per strategy, i.e. a month+ of SIM). Target: `P(this position's net R ends up
below its current r_now)` — i.e. probability of give-back. Output feeds the
same HOLD/TIGHTEN/EXIT decision, replacing the hand-tuned weights.

Kept in shadow the same way; `report_exit_advisor.py` compares A vs B vs the
rules on identical trades.

---

## Stage C — this document

The spec and the shadow-first discipline. Revisit the promotion decision only
when `report_exit_advisor.py` says the sample is large enough.

---

## Data artifacts

| File | Written by | Content |
|---|---|---|
| `data/exit_advisor_shadow.jsonl` | `_run_exits` every cycle | per-position `score`, `recommendation`, `r_now`, `mfe_r`, `signals`, `cur_stop` |
| `data/trade_observation_cards.jsonl` | entry + exit of every trade | MFE, MAE, `r_multiple`, `exit_reason`, `ladder_rung`, holding hours |

## Related

- `docs/forex_rsi_strategy.md` — the RSI profit ladder (the rule this advisor sits on top of)
- `docs/atos_ai_implementation_plan.md` — the separate AI *sizing* roadmap (v1 = multipliers, not exits)
- `report_profit_ladder.py` — ladder vs control forward test
