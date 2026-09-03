"""
ai/config.py -- the single AI kill switch.

Every AI touchpoint anywhere in the codebase calls ai_enabled_for(env)
before doing anything. Flip config/ai.json's flags to false (or delete the
file) and every AI hook goes inert on the next cycle, without touching
forex/runner.py.

Two separate permissions, deliberately:
  * OBSERVE / LOG  -- allowed on sim AND the live accounts (so the agent
    can score real LIVE trades and log what it *would* have done). Gated by
    config: "enabled_sim" for sim, "enabled_live_shadow" for live/live_eur.
  * ACT (apply a decision, i.e. resize or skip an order) -- allowed on
    "sim" ONLY, enforced in CODE here (_AI_ACTING_ACCOUNTS), not config.
    A LIVE account can never reach can_apply_decision() == True regardless
    of what config/ai.json says. Promoting AI to act on LIVE needs its own
    separate written decision (roadmap governance) AND a change here.

Fail-safe contract (Sprint 0 test gate):
  * missing config/ai.json  -> disabled
  * malformed JSON          -> disabled (logged, never a crash)
  * any live account + ACT  -> ALWAYS False, hardcoded
"""

from __future__ import annotations

import json
import os

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONFIG_PATH = os.path.join(_BASE_DIR, "config", "ai.json")

# Accounts the AI layer may OBSERVE + LOG for (shadow). LIVE is here so the
# agent can be evaluated against real money trades -- log-only.
#   live_stocks (2026-09-02) -- the real-money US Blend sleeve
#   (atos_live_stocks.py). Shadow ONLY: it is deliberately NOT in
#   _AI_ACTING_ACCOUNTS, so can_apply_decision("live_stocks") is False forever.
#   ai_sim (2026-09-03) -- the AI-DECISION SIM twin (forex/runner.py
#   --account ai_sim + atos_ai_stocks.py). A paper book on the SIM login
#   where the Copilot's resize/skip (forex) and the basket-ranker's
#   re-ranked pick (stocks) ARE applied -- a live forward A/B vs the
#   deterministic SIM books. In _AI_ACTING_ACCOUNTS below.
_AI_SHADOW_ACCOUNTS = {"sim", "live", "live_eur", "live_stocks", "ai_sim"}

# Accounts an AI decision may ever ACTUALLY CHANGE an order for. SIM paper
# only, in code. This is the hard wall between "AI has an opinion on LIVE"
# and "AI moves real money" -- a config flip cannot cross it.
#   ai_sim is a paper book (no real orders anywhere) whose whole purpose is
#   to let the agent act, so it can be A/B'd against the deterministic book.
_AI_ACTING_ACCOUNTS = {"sim", "ai_sim"}

_DEFAULTS = {
    "enabled_sim": False,
    # AI-DECISION SIM twin (2026-09-03): forex/runner.py --account ai_sim +
    # atos_ai_stocks.py. A paper book where the Copilot's resize/skip and the
    # basket-ranker's re-ranked pick ARE applied -- a live forward A/B vs the
    # deterministic SIM books, on `ai_dashboard.py`. Needs agent_enabled too
    # (it makes a paid call per signal). Not gated by `shadow_mode` -- acting
    # is the twin's whole purpose (shadow_mode("ai_sim") is hardcoded False).
    "enabled_ai_sim": False,
    # log-only AI opinions on the real LIVE accounts. Cannot ever act on
    # LIVE (see _AI_ACTING_ACCOUNTS) -- this only turns on proposal + agent
    # logging for live / live_eur.
    "enabled_live_shadow": False,
    "shadow_mode": True,     # even when enabled, only observe -- until a sprint flips it (sim only)
    # Sprint 3: the trade-proposal LOG (enabled_*) is free; actually calling
    # the LLM agent to evaluate each proposal costs money per signal. Kept a
    # separate switch so proposal logging can run without paid agent calls.
    "agent_enabled": False,
    # 2026-08-31: user switched Opus -> Sonnet for the shadow study to keep
    # the bill low (~5x cheaper per token). Revisit if Sonnet's judgement
    # looks weak in ai_shadow_report.py.
    "agent_model": "claude-sonnet-5",
    # cost controls for the paid agent call (Sprint 3.5):
    #   agent_strategies -- which strategies the agent evaluates. ["*"] = all.
    #     Default is rsi only: the shadow study only needs one strategy's
    #     worth of decisions to prove/disprove edge, and rsi is the one that
    #     also runs on both LIVE accounts.
    #   agent_dedup -- evaluate each signal (account|strategy|symbol|date)
    #     ONCE per day, not once every 30-min rescan while it still stands.
    "agent_strategies": ["rsi"],
    "agent_dedup": True,
    # AI Trading Journal (2026-08-31, roadmap #18): an LLM retrospective on
    # each CLOSED trade -- read-only, never touches an order/position/stop.
    #   journal_enabled -- master switch for ai/features/trade_journal.py.
    #   journal_model    -- kept separate from agent_model so the journal can
    #     use a different model without disturbing the shadow study.
    #   journal_max_trades_per_run -- hard cap on how many un-journaled
    #     closed trades one run will feed the model (one batched call).
    "journal_enabled": False,
    "journal_model": "claude-sonnet-5",
    "journal_max_trades_per_run": 40,
    # ── Stocks AI (2026-09-02) ────────────────────────────────────────────
    # The forex AI layer (proposal log -> shadow Copilot -> Journal) extended
    # to the SIM stocks module (atos_runner.py). Independently gated and
    # ships OFF (`enabled: false`) -- the whole `stocks` subtree is inert
    # until it's flipped. Sub-flags let each piece be toggled once enabled:
    #   journal                   -- feed stocks closed trades to the Journal
    #   shadow_copilot_reversion  -- score every US Reversion entry (log-only)
    #   basket_ranker_blend       -- shadow-rank the US Blend fortnightly
    #                                offense basket (log-only, changes nothing)
    # Every stocks hook is OBSERVE/LOG only -- there is no apply path in the
    # code, so a future forex Sprint-4 can_apply_decision("sim")==True can
    # never leak into a stocks trade (AST-checked in the stocks-AI tests).
    "stocks": {
        "enabled": False,
        "journal": True,
        "shadow_copilot_reversion": True,
        "basket_ranker_blend": True,
    },
    # ── LIVE stocks AI (2026-09-02) ───────────────────────────────────────
    # The real-money US Blend sleeve (atos_live_stocks.py). Same OBSERVE/LOG-
    # only contract as `stocks`, its OWN on/off so the SIM stocks study and
    # the real-money sleeve are toggled independently. can_apply_decision(
    # "live_stocks") is False in code forever (not in _AI_ACTING_ACCOUNTS).
    # No shadow_copilot_reversion key -- the LIVE sleeve trades US Blend only.
    "stocks_live": {
        "enabled": False,
        "journal": True,
        "basket_ranker_blend": True,
    },
    # ── AI-decision stocks twin (2026-09-03) ──────────────────────────────
    # atos_ai_stocks.py -- a SIM paper US Blend book that TRADES the
    # basket-ranker's re-ranked pick instead of the deterministic top-N.
    # account_env "ai_sim". `basket_ranker_blend` here means "apply", not
    # "shadow-log". Its own on/off.
    "stocks_ai": {
        "enabled": False,
        "journal": True,
        "basket_ranker_blend": True,
    },
    # ── Trade Outcome Predictor (2026-09-03, roadmap #20) ────────────────
    # ai/models/trade_outcome_predictor.py — a GradientBoosting classifier
    # trained on our actual closed observation cards (entry context + real
    # r_multiple outcomes). Replaces the CNN-LSTM's raw price-direction
    # approach with direct trade-profitability prediction.
    # Gate: won't train below min_samples (100) closed, non-orphaned cards.
    # Output feeds top_win_prob in the trade proposal; the Copilot can use it
    # alongside signal_filter's ml_prob. Ships OFF.
    # Retrain: python ai_outcome_predictor.py --train (weekly once gate clears).
    "outcome_predictor": {
        "enabled": False,
        "min_samples": 100,
    },
    # ── AI Research Analyst (2026-09-03, roadmap #19) ─────────────────────
    # ai/features/research_analyst.py -- OFFLINE, READ-ONLY. Aggregates the
    # closed-trade record + the Journal + the decomposition harness into a
    # digest, has an LLM propose SPECIFIED testable strategy filters, auto-
    # runs the cheap decomposition gate, and keeps a triaged backlog
    # (data/ai_research_hypotheses.jsonl). It NEVER edits a strategy or
    # touches an order -- a human writes the deterministic gate and ships a
    # SIM A/B twin. Ships OFF. Needs an ANTHROPIC_API_KEY for the propose
    # step (degrades to digest-only without one).
    #   sweep_years            -- how far back the decomposition replay goes
    #   max_hypotheses_per_run -- hard cap on LLM-proposed hypotheses/run
    "research_analyst": {
        "enabled": False,
        "model": "claude-sonnet-5",
        "sweep_years": 13,
        "max_hypotheses_per_run": 8,
    },
}


def _load() -> dict:
    """config/ai.json merged over defaults. Any failure -> defaults
    (i.e. disabled). Never raises."""
    cfg = dict(_DEFAULTS)
    try:
        if os.path.exists(_CONFIG_PATH):
            with open(_CONFIG_PATH, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for k in _DEFAULTS:
                    if k in data:
                        cfg[k] = data[k]
    except Exception as exc:  # malformed JSON, permissions, anything
        try:
            print(f"[ai.config] could not read {_CONFIG_PATH}: {exc} -- AI disabled", flush=True)
        except Exception:
            pass
    return cfg


def ai_enabled_for(account_env: str) -> bool:
    """True if the AI layer may OBSERVE + LOG for this account. Hard-off for
    any account not in _AI_SHADOW_ACCOUNTS. For sim: needs enabled_sim. For
    live / live_eur: needs enabled_live_shadow (and even then it is log-only
    forever -- see can_apply_decision)."""
    if account_env not in _AI_SHADOW_ACCOUNTS:
        return False
    cfg = _load()
    if account_env == "sim":
        return bool(cfg.get("enabled_sim", False))
    if account_env == "ai_sim":
        return bool(cfg.get("enabled_ai_sim", False))
    return bool(cfg.get("enabled_live_shadow", False))


def shadow_mode(account_env: str = "sim") -> bool:
    """True = AI observes/logs only, never changes an order.
      * any LIVE account -> ALWAYS True (hardcoded, not in _AI_ACTING_ACCOUNTS)
      * ai_sim -> ALWAYS False when enabled (the paper twin exists to ACT,
        so it is not gated by the SIM shadow-evidence flag)
      * sim -> from config (a later sprint's evidence gate flips it)
      * AI not enabled at all -> True (safe)"""
    if not ai_enabled_for(account_env):
        return True
    if account_env not in _AI_ACTING_ACCOUNTS:
        return True
    if account_env == "ai_sim":
        return False
    return bool(_load().get("shadow_mode", True))


def can_apply_decision(account_env: str) -> bool:
    """Sprint 4+ gate: may the agent's decision ACTUALLY change an order
    (resize / skip) for this account? True only for sim, only with the agent
    enabled, only when shadow_mode is off. A LIVE account can never make
    this True -- it is not in _AI_ACTING_ACCOUNTS."""
    if account_env not in _AI_ACTING_ACCOUNTS:
        return False
    return agent_enabled_for(account_env) and not shadow_mode(account_env)


def agent_enabled_for(account_env: str) -> bool:
    """True only if the paid LLM agent (ai/agent/trading_copilot) should be
    called this run. Requires ai_enabled_for() AND config agent_enabled.
    The agent itself still degrades to HOLD if credentials are missing."""
    return ai_enabled_for(account_env) and bool(_load().get("agent_enabled", False))


def agent_strategy_allowed(strategy: str) -> bool:
    """True if the paid agent should evaluate signals from this strategy.
    Config "agent_strategies": ["*"] for all, or an explicit list."""
    lst = _load().get("agent_strategies") or ["rsi"]
    if not isinstance(lst, list):
        return False
    return "*" in lst or strategy in lst


def agent_dedup_enabled() -> bool:
    """True = evaluate each signal once per day, not every rescan."""
    return bool(_load().get("agent_dedup", True))


def agent_model() -> str:
    return str(_load().get("agent_model") or "claude-sonnet-5")


def journal_enabled() -> bool:
    """True if the AI Trading Journal (ai/features/trade_journal.py) should
    run. Independent of the shadow study's switches -- the journal only
    READS closed-trade logs and writes its own file, it never evaluates a
    live signal or touches an order."""
    return bool(_load().get("journal_enabled", False))


def journal_model() -> str:
    return str(_load().get("journal_model") or "claude-sonnet-5")


def journal_max_trades_per_run() -> int:
    try:
        return max(1, int(_load().get("journal_max_trades_per_run", 40)))
    except (TypeError, ValueError):
        return 40


def _stocks_cfg(account_env: str = "sim") -> dict:
    """The AI config block for a stocks account: `stocks_live` for the
    real-money US Blend sleeve, `stocks_ai` for the AI-decision SIM twin
    (atos_ai_stocks.py), `stocks` for everything else (SIM)."""
    key = {"live_stocks": "stocks_live", "ai_sim": "stocks_ai"}.get(account_env, "stocks")
    s = _load().get(key)
    return s if isinstance(s, dict) else {}


def stocks_enabled(account_env: str = "sim") -> bool:
    """Master switch for a stocks AI layer. False if the relevant block
    (`stocks` / `stocks_live`) is missing or `enabled` is not true. Nothing in
    that stocks AI path does anything while this is False."""
    return bool(_stocks_cfg(account_env).get("enabled", False))


def stocks_journal_enabled(account_env: str = "sim") -> bool:
    """Feed a stocks module's closed trades to the AI Trading Journal.
    Requires stocks_enabled(account_env) AND the `journal` sub-flag AND
    journal_enabled() (the Journal's own master switch)."""
    return (stocks_enabled(account_env) and bool(_stocks_cfg(account_env).get("journal", False))
            and journal_enabled())


def stocks_reversion_copilot_enabled() -> bool:
    """Score every US Reversion entry candidate with the shadow Trading
    Copilot (log-only, applies nothing). SIM-only -- US Reversion never runs
    on the LIVE sleeve. Requires stocks_enabled() AND the
    `shadow_copilot_reversion` sub-flag AND agent_enabled_for('sim')."""
    return (stocks_enabled("sim") and bool(_stocks_cfg("sim").get("shadow_copilot_reversion", False))
            and agent_enabled_for("sim"))


def stocks_basket_ranker_enabled(account_env: str = "sim") -> bool:
    """Run the US Blend basket-ranker for this account. For `sim` /
    `live_stocks` it is shadow-log only (the deterministic pick is untouched).
    For **`ai_sim`** the returned pick is what the twin actually trades
    (atos_ai_stocks.py). Requires stocks_enabled(account_env) AND the
    `basket_ranker_blend` sub-flag AND agent_enabled_for the matching env."""
    _agent_env = account_env if account_env in ("live_stocks", "ai_sim") else "sim"
    return (stocks_enabled(account_env)
            and bool(_stocks_cfg(account_env).get("basket_ranker_blend", False))
            and agent_enabled_for(_agent_env))


def basket_ranker_applies(account_env: str) -> bool:
    """True only for the AI-decision twin (`ai_sim`) -- the one place the
    basket-ranker's pick is TRADED rather than just logged. Everywhere else
    the deterministic basket is authoritative (governance)."""
    return account_env == "ai_sim" and stocks_basket_ranker_enabled("ai_sim")


def outcome_predictor_cfg() -> dict:
    """The config/ai.json `outcome_predictor` block (merged over defaults)."""
    s = _load().get("outcome_predictor")
    return s if isinstance(s, dict) else dict(_DEFAULTS["outcome_predictor"])


def outcome_predictor_enabled() -> bool:
    """True if the TOP model may score proposals.  Read-only / offline --
    independent of account gates.  Only needs its own flag (and a trained
    model on disk -- gracefully returns None without one)."""
    return bool(outcome_predictor_cfg().get("enabled", False))


def research_analyst_cfg() -> dict:
    """The config/ai.json `research_analyst` block (merged over defaults)."""
    s = _load().get("research_analyst")
    return s if isinstance(s, dict) else dict(_DEFAULTS["research_analyst"])


def research_analyst_enabled() -> bool:
    """Master switch for ai/features/research_analyst.py. OFF unless the
    `research_analyst` block's `enabled` is true. The analyst is offline and
    read-only -- it never trades -- so it is NOT gated by agent_enabled /
    shadow_mode / any account; it only needs its own flag (and an API key,
    which it degrades gracefully without)."""
    return bool(research_analyst_cfg().get("enabled", False))


def config_path() -> str:
    return _CONFIG_PATH
