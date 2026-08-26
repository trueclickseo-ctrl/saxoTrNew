"""
forex/forward_observation.py
-----------------------------
Structured, append-only logging for the forward-SIM validation phase
decided 2026-08-27: freeze the architecture (cost gate, exposure
measurement, stop rule) and let real forward trading generate evidence
instead of tuning any of it further against a tiny historical sample.

Three log streams, one JSONL file each (one JSON object per line -- easy
to append, easy to load with pandas.read_json(lines=True) or a plain
line-by-line loop later):

  data/cost_gate_decisions.jsonl
      Every signal that reaches the cost-clearance gate, PASS or
      BLOCKED, with enough detail (entry/stop/tp/qty/symbol/timestamp)
      to look up later what price actually did -- the "what was the
      counterfactual performance of rejected trades" question this
      whole phase is built around. Not just skip counts.

  data/currency_exposure_snapshots.jsonl
      One row per scan run: net EUR-notional exposure per currency,
      concentration ranking, and both raw EUR and %-of-equity framing.
      Accumulates the time series needed to eventually say "at €X
      exposure, drawdown characteristics deteriorate" instead of
      guessing a threshold.

  data/trade_observation_cards.jsonl
      One row per trade, written at entry and updated at close: ATR,
      structural stop, hybrid stop, and the strategy's actual chosen
      stop side by side (donchian-relevant fields are None for other
      strategies), risk/cost/exposure at entry, then gross/commission/
      net/R/MAE/MFE/holding-time once it closes.

This module only OBSERVES -- it never changes a signal, a stop, a size,
or a gate decision. Pure logging, safe to add without touching any
strategy's actual behavior.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

COST_GATE_LOG     = os.path.join(_DATA_DIR, "cost_gate_decisions.jsonl")
EXPOSURE_LOG      = os.path.join(_DATA_DIR, "currency_exposure_snapshots.jsonl")
TRADE_CARDS_LOG   = os.path.join(_DATA_DIR, "trade_observation_cards.jsonl")


def _append_jsonl(path: str, record: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception:
        pass  # observation logging must never break a live trading run


def log_cost_gate_decision(*, account_env: str, strategy: str, symbol: str, direction: str,
                            entry_price: float, stop_price: float, tp_price: float, qty: float,
                            expected_target_profit_quote: float, round_trip_cost_quote: float | None,
                            expected_target_profit_eur: float | None, round_trip_cost_eur: float | None,
                            min_edge_to_cost_ratio: float, decision: str, reason: str = "") -> None:
    """decision: "PASS" or "BLOCKED". Called for every signal that reaches
    this gate, not just the ones it blocks -- the point is to be able to
    ask later "of the signals it let through, how many were actually
    thin?" as well as "of the ones it blocked, what would they have done?"
    """
    ratio_actual = None
    if round_trip_cost_quote:
        ratio_actual = round(expected_target_profit_quote / round_trip_cost_quote, 2)
    _append_jsonl(COST_GATE_LOG, {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "account_env": account_env, "strategy": strategy, "symbol": symbol, "direction": direction,
        "entry_price": entry_price, "stop_price": stop_price, "tp_price": tp_price, "quantity": qty,
        "expected_target_profit_quote": round(expected_target_profit_quote, 4),
        "round_trip_cost_quote": round(round_trip_cost_quote, 4) if round_trip_cost_quote is not None else None,
        "expected_target_profit_eur": round(expected_target_profit_eur, 2) if expected_target_profit_eur is not None else None,
        "round_trip_cost_eur": round(round_trip_cost_eur, 2) if round_trip_cost_eur is not None else None,
        "ratio_actual": ratio_actual,
        "min_edge_to_cost_ratio": min_edge_to_cost_ratio,
        "decision": decision, "reason": reason,
    })


def log_exposure_snapshot(*, account_env: str, count_exposure: dict, notional_exposure_eur: dict,
                           equity_eur: float | None) -> None:
    """One row per scan run. count_exposure/notional_exposure_eur are the
    raw dicts from _currency_exposure()/_currency_exposure_notional_eur().
    equity_eur lets this be read back later as %-of-equity without
    needing to separately reconstruct historical equity."""
    pct_of_equity = {}
    if equity_eur and equity_eur > 0:
        pct_of_equity = {ccy: round(abs(v) / equity_eur * 100, 2)
                          for ccy, v in notional_exposure_eur.items()}
    ranked = sorted(notional_exposure_eur.items(), key=lambda kv: abs(kv[1]), reverse=True)
    _append_jsonl(EXPOSURE_LOG, {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "account_env": account_env,
        "count_exposure": count_exposure,
        "notional_exposure_eur": {k: round(v, 2) for k, v in notional_exposure_eur.items()},
        "pct_of_equity": pct_of_equity,
        "equity_eur": round(equity_eur, 2) if equity_eur else None,
        "top_currency_by_notional": ranked[0][0] if ranked else None,
        "top_currency_notional_eur": round(ranked[0][1], 2) if ranked else None,
    })


def log_trade_entry_card(*, account_env: str, strategy: str, symbol: str, direction: str,
                          entry_price: float, atr_at_entry: float, current_stop: float,
                          structural_stop: float | None, hybrid_stop: float | None,
                          quantity: float, risk_eur: float | None, cost_eur: float | None,
                          cost_to_edge_ratio: float | None,
                          exposure_before_eur: dict, exposure_after_eur: dict) -> str:
    """Written once, at entry. structural_stop/hybrid_stop are only
    meaningful for donchian (its own channel data) -- None for every
    other strategy, not a placeholder guess. Returns a card_id to pass
    to log_trade_exit_card() when this position closes."""
    card_id = f"{account_env}:{strategy}:{symbol}:{datetime.now(timezone.utc).isoformat()}"
    _append_jsonl(TRADE_CARDS_LOG, {
        "card_id": card_id, "event": "entry",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "account_env": account_env, "strategy": strategy, "symbol": symbol, "direction": direction,
        "entry_price": entry_price, "atr_at_entry": atr_at_entry, "quantity": quantity,
        "current_stop": current_stop, "structural_stop": structural_stop, "hybrid_stop": hybrid_stop,
        "risk_eur": round(risk_eur, 2) if risk_eur is not None else None,
        "cost_eur": round(cost_eur, 2) if cost_eur is not None else None,
        "cost_to_edge_ratio": cost_to_edge_ratio,
        "exposure_before_eur": {k: round(v, 2) for k, v in exposure_before_eur.items()},
        "exposure_after_eur": {k: round(v, 2) for k, v in exposure_after_eur.items()},
    })
    return card_id


def log_trade_exit_card(*, card_id: str, exit_price: float, exit_reason: str,
                         gross_pnl_eur: float | None, commission_eur: float | None,
                         net_pnl_eur: float | None, r_multiple: float | None,
                         mae_eur: float | None, mfe_eur: float | None,
                         holding_hours: float | None) -> None:
    """Written once, at close, referencing the entry card's card_id.
    MAE/MFE are in EUR (worst/best unrealized excursion seen while the
    position was open) -- accumulated by update_mae_mfe() below on every
    exits-check cycle, not reconstructed after the fact."""
    _append_jsonl(TRADE_CARDS_LOG, {
        "card_id": card_id, "event": "exit",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "exit_price": exit_price, "exit_reason": exit_reason,
        "gross_pnl_eur": round(gross_pnl_eur, 2) if gross_pnl_eur is not None else None,
        "commission_eur": round(commission_eur, 2) if commission_eur is not None else None,
        "net_pnl_eur": round(net_pnl_eur, 2) if net_pnl_eur is not None else None,
        "r_multiple": r_multiple,
        "mae_eur": round(mae_eur, 2) if mae_eur is not None else None,
        "mfe_eur": round(mfe_eur, 2) if mfe_eur is not None else None,
        "holding_hours": holding_hours,
    })


def update_mae_mfe(position: dict, unrealized_pnl_eur: float) -> None:
    """Call once per exits-check cycle for an open position (mutates the
    position dict in place -- caller is responsible for persisting state).
    Tracks the worst (MAE) and best (MFE) unrealized EUR P&L seen so far,
    so it's a real running measurement, not reconstructed from bars after
    the fact."""
    mae = position.get("mae_eur")
    mfe = position.get("mfe_eur")
    position["mae_eur"] = unrealized_pnl_eur if mae is None else min(mae, unrealized_pnl_eur)
    position["mfe_eur"] = unrealized_pnl_eur if mfe is None else max(mfe, unrealized_pnl_eur)
