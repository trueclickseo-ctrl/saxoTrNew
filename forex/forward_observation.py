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
EXIT_ADVISOR_LOG  = os.path.join(_DATA_DIR, "exit_advisor_shadow.jsonl")
SIGNAL_REJECT_LOG = os.path.join(_DATA_DIR, "signal_rejections.jsonl")


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
                            min_edge_to_cost_ratio: float, decision: str, reason: str = "",
                            notional_eur: float | None = None,
                            realised_r_eur: float | None = None,
                            all_in_cost_eur: float | None = None,
                            recovery_thin: bool = False,
                            rate_source: str | None = None) -> None:
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
        "notional_eur": round(notional_eur, 0) if notional_eur is not None else None,
        "realised_r_eur": round(realised_r_eur, 2) if realised_r_eur is not None else None,
        "all_in_cost_eur": round(all_in_cost_eur, 2) if all_in_cost_eur is not None else None,
        "r_to_all_in_cost": (round(realised_r_eur / all_in_cost_eur, 2)
                             if (realised_r_eur is not None and all_in_cost_eur) else None),
        # RSI signal whose 0.5R recovery does NOT clear 3x the all-in cost.
        # On LIVE this is a BLOCK; on SIM the trade still runs (full breadth)
        # but this flag lets the AI / analysis separate the healthy signals
        # from the cost-dominated ones once RSI trades all 184 pairs.
        "recovery_thin": bool(recovery_thin),
        # how the EUR conversion behind realised_r_eur / all_in_cost_eur was
        # obtained: "live" (fresh Saxo quote), "last_good" (persisted Saxo
        # rate < 24h old -- fine for an R denominator), or None (no rate at
        # all -> the *_eur fields above are None). Lets an analysis down-weight
        # or exclude last_good rows if it wants to.
        "rate_source": rate_source,
    })


def log_signal_rejected(*, account_env: str, strategy: str, symbol: str, direction: str,
                         stage: str, detail: str = "",
                         entry_price: float | None = None, stop_price: float | None = None,
                         tp_price: float | None = None,
                         rsi: float | None = None, adx: float | None = None,
                         atr: float | None = None) -> None:
    """A signal the strategy generated that the pipeline dropped BEFORE the
    cost-clearance gate -- so it never reached cost_gate_decisions.jsonl and,
    before this, existed only as a text line in the scheduler log.

    `stage` is the filter that rejected it: "stale_price", "signal_filter",
    "currency_exposure", "opposing_strategy", "wide_spread", "no_fx_rate",
    "risk_budget". With entry/stop/tp captured, a later pass can look up what
    price actually did and measure the counterfactual -- the same question
    the cost-gate log was built for, extended to the earlier filter stage.

    Pure observation. Never changes whether the signal is skipped.
    """
    _append_jsonl(SIGNAL_REJECT_LOG, {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "account_env": account_env, "strategy": strategy, "symbol": symbol,
        "direction": direction, "stage": stage, "detail": detail,
        "entry_price": entry_price, "stop_price": stop_price, "tp_price": tp_price,
        "rsi": round(rsi, 1) if isinstance(rsi, (int, float)) else None,
        "adx": round(adx, 1) if isinstance(adx, (int, float)) else None,
        "atr": atr,
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
                          exposure_before_eur: dict, exposure_after_eur: dict,
                          all_in_cost_eur: float | None = None,
                          recovery_to_cost_ratio: float | None = None,
                          recovery_thin: bool = False,
                          rate_source: str | None = None) -> str:
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
        # all-in transaction cost (commission + spread + slippage) and the
        # realistic-recovery-vs-cost ratio the LIVE gate thresholds at 3.0.
        # `recovery_thin` = an RSI signal that would be REJECTED on LIVE; on
        # SIM the trade still ran (full breadth) -- the flag lets the journal
        # weigh it as a marginal setup rather than a clean one.
        "all_in_cost_eur": round(all_in_cost_eur, 2) if all_in_cost_eur is not None else None,
        "recovery_to_cost_ratio": recovery_to_cost_ratio,
        "recovery_thin": bool(recovery_thin),
        # "live" / "last_good" / None -- how risk_eur & all_in_cost_eur's EUR
        # conversion was sourced (see log_cost_gate_decision's note).
        "rate_source": rate_source,
        "exposure_before_eur": {k: round(v, 2) for k, v in exposure_before_eur.items()},
        "exposure_after_eur": {k: round(v, 2) for k, v in exposure_after_eur.items()},
    })
    return card_id


def log_trade_exit_card(*, card_id: str, exit_price: float, exit_reason: str,
                         gross_pnl_eur: float | None, commission_eur: float | None,
                         net_pnl_eur: float | None, r_multiple: float | None,
                         mae_eur: float | None, mfe_eur: float | None,
                         holding_hours: float | None,
                         ladder_rung: str | None = None,
                         ladder_rung_r: float | None = None,
                         mae_mfe_coarse: bool = False,
                         net_pnl_reconstructed: bool = False,
                         mae_mfe_invalidated: str | None = None) -> None:
    """Written once, at close, referencing the entry card's card_id.
    MAE/MFE are in EUR (worst/best unrealized excursion seen while the
    position was open) -- accumulated by update_mae_mfe() below on every
    exits-check cycle, bounded to the holding window (fixed 2026-09-01).
    `mae_mfe_coarse` = the excursion was taken from a single daily bar
    because this is an intraday strategy (gap / london_breakout*) -- treat
    it as a loose upper bound, not a precise figure.

    ladder_rung / ladder_rung_r (2026-08-31): the highest RSI profit-ladder
    rung this trade reached and at what R -- None if the ladder wasn't
    active for it or it never got far enough. Lets report_profit_ladder.py
    measure give-back-prevented vs winner-clipped per rung."""
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
        "ladder_rung": ladder_rung,
        "ladder_rung_r": ladder_rung_r,
        "mae_mfe_coarse": bool(mae_mfe_coarse),
        # True = Saxo's positions/me net P&L was implausible (SIM data
        # glitch) so net_pnl_eur is price-move minus a modeled round-trip
        # cost, not Saxo's own figure. See forex/runner._sane_net_pnl_quote.
        "net_pnl_reconstructed": bool(net_pnl_reconstructed),
        # non-null = MAE/MFE were nulled at write time (over the sane-R cap,
        # accumulated before a fix deployed). report_giveback / journal skip.
        **({"mae_mfe_invalidated": mae_mfe_invalidated} if mae_mfe_invalidated else {}),
    })


def log_exit_advisor_shadow(*, account_env: str, strategy: str, symbol: str,
                            card_id: str | None, score: float, recommendation: str,
                            r_now: float, mfe_r: float, signals: dict,
                            cur_stop: float) -> None:
    """One row per open position per exits-check cycle, recording what the
    Stage-A exit advisor (forex/exit_advisor.py) WOULD recommend. SHADOW
    ONLY -- nothing acts on it. report_exit_advisor.py joins these against
    the eventual real exit (trade_observation_cards.jsonl) to measure
    whether acting on 'EXIT'/'TIGHTEN' would have beaten the real outcome.
    Best-effort -- never raises into a trading run."""
    _append_jsonl(EXIT_ADVISOR_LOG, {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "account_env": account_env, "strategy": strategy, "symbol": symbol,
        "card_id": card_id,
        "score": score, "recommendation": recommendation,
        "r_now": r_now, "mfe_r": mfe_r, "cur_stop": cur_stop,
        "signals": signals,
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
