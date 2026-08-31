"""
ai/features/trade_proposal.py -- AI Sprint 2: turn a strategy signal that
already passed every deterministic filter into a structured "candidate"
and log it. NO AI call yet -- this sprint just proves the pipe end to end
while it's completely inert (log-only, zero behaviour change).

The proposal is the exact shape the roadmap agreed on
(docs/atos_ai_roadmap.md "Trade proposal schema"), plus the Sprint 1
regime label folded in as a nested object. Sprint 3 puts an agent behind
this; Sprint 4 lets that agent's decision affect SIM sizing.

Pure except for the append to data/ai_trade_proposals.jsonl. Never raises
(the caller must be able to ignore it entirely).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

# NOTE: ai.regime.classifier is imported LAZILY inside build_proposal(), not
# here. classifier.py pulls in forex.strategy at its module top; importing
# it here put forex.strategy on this module's import-time dependency chain,
# and forex/runner.py imports THIS module. Under the runner's "run as a
# script, then re-imported by safeguard/housekeeping" pattern (and any
# threaded first-import), that chain opened a window where a second importer
# could observe a half-initialised ai.features.trade_proposal and raise
# "cannot import name 'log_shadow_decision'" -- which broke forex
# reconciliation on 2026-08-31. The lazy import keeps module load trivial
# and side-effect-free.

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "data")
PROPOSALS_LOG = os.path.join(_DATA_DIR, "ai_trade_proposals.jsonl")
SHADOW_DECISIONS_LOG = os.path.join(_DATA_DIR, "ai_shadow_decisions.jsonl")

# strategies whose signals are computed on H1 bars, not daily
_H1_STRATEGIES = {"london_breakout", "london_breakout_v2", "gap", "gap_weekend"}


def build_proposal(*, account_env: str, strategy: str, symbol: str, direction: str,
                   sig: dict, features: dict, positions: dict, equity: float,
                   take_profit: float | None, n_strategies: int,
                   regime_bars=None) -> dict:
    """Assemble one trade-proposal dict. `sig` is the strategy's signal dict
    (close/stop_price/atr/score, optionally rsi/range_pips/breakout_level).
    `features` is signal_filter.evaluate()'s output (agreement_count,
    ml_prob). `regime_bars` is a daily-bar DataFrame (or None)."""
    features = features or {}
    entry = float(sig.get("close", 0) or 0)
    atr   = float(sig.get("atr", 0) or 0)
    agree = features.get("agreement_count")

    open_pos = []
    for key, v in (positions or {}).items():
        s = key.split(":", 1)[1] if ":" in key else key
        open_pos.append({
            "symbol": s,
            "side": "BUY" if v.get("direction", "Buy") == "Buy" else "SELL",
            "size": v.get("quantity"),
            "strategy": key.split(":", 1)[0] if ":" in key else None,
        })

    if regime_bars is not None:
        try:
            from ai.regime.classifier import classify_regime
            regime = classify_regime(regime_bars)
        except Exception:
            regime = {"label": "UNKNOWN"}
    else:
        regime = {"label": "UNKNOWN"}

    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "account_env": account_env,
        "symbol": symbol,
        "side": "BUY" if direction == "Buy" else "SELL",
        "entry_price": entry,
        "stop_loss": float(sig.get("stop_price", 0) or 0),
        "take_profit": float(take_profit) if take_profit is not None else None,
        "timeframe": "H1" if strategy in _H1_STRATEGIES else "D1",
        "strategy_name": strategy,
        # 0..1 -- share of the strategy stack that agreed (signal_filter's
        # consensus count). The agent can weight this however it likes.
        "signal_strength": round(agree / n_strategies, 3) if (agree is not None and n_strategies) else None,
        "raw_score": sig.get("score"),
        "agreement_count": agree,
        "ml_prob": features.get("ml_prob"),
        "account_equity": round(float(equity), 2) if equity else None,
        "open_positions": open_pos,
        "n_open_positions": len(open_pos),
        "volatility_atr": round(atr, 6),
        "atr_pct": round(atr / entry * 100, 3) if entry > 0 else None,
        "rsi2": sig.get("rsi"),
        "regime": {
            "label": regime.get("label"),
            "adx": regime.get("adx"),
            "atr_ratio": regime.get("atr_ratio"),
            "ma_slope": regime.get("ma_slope"),
            "confidence": regime.get("confidence"),
        },
    }


def _append(path: str, row: dict) -> None:
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")
    except Exception:
        pass


def log_proposal(proposal: dict) -> None:
    """Append one proposal to data/ai_trade_proposals.jsonl. Best-effort."""
    _append(PROPOSALS_LOG, proposal)


def trade_id(proposal: dict) -> str:
    """Stable key for joining a shadow decision to the eventual real trade
    outcome: account | strategy | symbol | UTC date."""
    return "|".join((
        str(proposal.get("account_env")), str(proposal.get("strategy_name")),
        str(proposal.get("symbol")),
        str(proposal.get("ts", ""))[:10],
    ))


def log_shadow_decision(proposal: dict, decision: dict, entered: bool) -> None:
    """Sprint 3: proposal + the agent's decision, logged together. The
    decision is NEVER applied here -- `entered` records what ATOS actually
    did so ai_shadow_report.py can compare."""
    _append(SHADOW_DECISIONS_LOG, {
        "trade_id": trade_id(proposal),
        "ts": proposal.get("ts"),
        "account_env": proposal.get("account_env"),
        "strategy": proposal.get("strategy_name"),
        "symbol": proposal.get("symbol"),
        "side": proposal.get("side"),
        "regime": (proposal.get("regime") or {}).get("label"),
        "signal_strength": proposal.get("signal_strength"),
        "entered_by_atos": bool(entered),
        "agent_action": decision.get("action"),
        "agent_size_multiplier": decision.get("size_multiplier"),
        "agent_comment": decision.get("comment"),
        "agent_meta": decision.get("_agent"),
    })


# Fields every proposal must carry (Sprint 2 test gate checks this exactly).
REQUIRED_FIELDS = (
    "ts", "account_env", "symbol", "side", "entry_price", "stop_loss",
    "take_profit", "timeframe", "strategy_name", "signal_strength",
    "account_equity", "open_positions", "volatility_atr", "regime",
)
