# `attention.py` — the one "ATOS needs a human" channel

**Goal (user, 2026-09-01): minimum human interaction. When ATOS genuinely
cannot resolve something on its own, exactly one clear email goes out, and
it keeps nagging (once a day) until the thing is dealt with.**

Before this, a "needs a human" finding was one line in a routine digest
labelled `FIXED` when it wasn't — the two `futures` unattributable
positions from 2026-08-26 sat unseen for 6 days that way.

## The module

| Function | What |
|---|---|
| `raise_attention(key, title=, detail=, source=, grace_minutes=, recheck_minutes=)` | Declare / refresh an open condition. Call it on **every run** it's still true — cheap, just a timestamp. |
| `clear_attention(key, note=)` | The condition resolved. |
| `flush()` | Reconcile the open set and send the consolidated digest. Called by the safeguard agents (every 30 min). |
| `open_items()` | Current open items (tests / dashboards). CLI: `python attention.py`. |

State lives in `data/attention_state.json`.

### Escalation rules

- An item **emails only once it has been continuously open for its
  `grace_minutes`** — a transient blip never pages.
- Once escalated, it **re-emails every 24 h** (`RE_EMAIL_HOURS`) until it
  clears.
- If a caller simply **stops raising** a key (condition went away, no
  explicit `clear`), the item **auto-expires** after `recheck_minutes` and
  is reported resolved. So a caller crash can't leave a stale nag forever.
- One consolidated email per flush: `🔴 ATOS needs a human — N open item(s)`
  with every open item + a "resolved since the last alert" section.

## What's wired in

| Source | Key | Behaviour |
|---|---|---|
| `safeguard.py` (SIM) — `fully_untracked` position | — | SIM is paper: safeguard **flat-closes** the position (opposing Market order). No escalation on success. |
| `safeguard.py` (SIM) — untracked position ATOS **failed to close** | `safeguard-sim:untracked:<mod>:<sym>` | grace 120 min. The only SIM case that pages — the forward-test data for that pair is skewed. Chronic SIM stop-replace / quote-restriction failures do **not** page (nothing a human does about them; the routine safeguard email still lists them). |
| `safeguard_live.py` / `safeguard_live_eur.py` — `fully_untracked` | `safeguard-live[-eur]:<sym>:needs_human_review` | Real money: **never auto-closed**. Escalates immediately (`grace_minutes=0`). |
| `safeguard_live[_eur].py` — NOT-FIXED outcome | `safeguard-live[-eur]:<sym>:<action>` | grace 45 min. |
| `forex/runner.py` `_note_operational_blocks()` (LIVE only, end of `run_daily`) | `<env>:margin-block` | Saxo shared-margin utilization ≥ 50% (no LIVE entry can be placed). grace 120 min. |
| `forex/runner.py` `_note_operational_blocks()` | `<env>:venue-circuit-open` | Order-venue circuit breaker open (8+ consecutive rejects). grace 30 min. |

The safeguard agents run every 30 min, so a block raised by the runner is
picked up and emailed on the next safeguard cycle — the raise/clear and
the flush/email are deliberately decoupled.

## Not wired in (deliberately)

- Token expiry, watchdog "task hung", silent-scan-outage — already have
  their own dedicated emails (`saxo_auth`, `scheduler_watchdog`). Not
  duplicated here.
