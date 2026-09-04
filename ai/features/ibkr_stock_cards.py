"""
ai/features/ibkr_stock_cards.py -- observation cards for the IBKR paper stocks
sleeve (ibkr_module/ibkr_executor.py), 2026-09-04.

Mirrors stock_cards.py but for USD-denominated IBKR trades. Writes to the SAME
data/stock_observation_cards.jsonl that the AI Trading Journal and the Stock
Outcome Predictor already read -- so IBKR trades flow into those pipelines for
free without touching either file.

Key differences from stock_cards.py:
  - native_currency = "USD"
  - account_env defaults to "ibkr_paper" (distinct id namespace from Saxo SIM)
  - No SEK/EUR conversion pass (IBKR executor has no live FX rates); all _eur
    fields are None. The Journal handles None gracefully (same as coarse trades).
  - card_id reuses card_id_for() from stock_cards -- the namespace prefix keeps
    "ibkr_paper:us_sma_crossover:AAPL:2026-09-04" distinct from
    "sim:us_sma_crossover:AAPL:2026-09-04".

Pure except for the jsonl append. Never raises. Gated by ai_config at the CALL
SITE, not here.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from ai.features.stock_cards import STOCK_CARDS_LOG, card_id_for   # shared file + id helper

_ACCOUNT_ENV = "ibkr_paper"


def _append(row: dict) -> None:
    try:
        with open(STOCK_CARDS_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")
    except Exception:
        pass


def log_ibkr_entry_card(*, strategy: str, ticker: str, direction: str,
                         entry_price: float, shares: float,
                         stop_price: float | None,
                         entry_date: str,
                         risk_usd: float | None = None,
                         confidence: float | None = None,
                         account_env: str = _ACCOUNT_ENV) -> str:
    """Written once at IBKR fill confirmation. `strategy` is the DB column name
    e.g. 'us_sma_crossover'. `entry_date` = 'YYYY-MM-DD'. Returns card_id.
    Never raises."""
    card_id = card_id_for(strategy, ticker, entry_date, account_env)
    _append({
        "card_id":          card_id,
        "event":            "entry",
        "timestamp":        datetime.now(timezone.utc).isoformat(),
        "market":           "equity",
        "account_env":      account_env,
        "strategy":         strategy,
        "symbol":           ticker,
        "direction":        direction,
        "entry_price":      entry_price,
        "current_stop":     stop_price,
        "quantity":         shares,
        "native_currency":  "USD",
        "risk_native":      round(risk_usd, 2) if risk_usd is not None else None,
        "risk_eur":         None,   # no FX in IBKR executor
        "signal_confidence": round(confidence, 3) if confidence is not None else None,
        # equity fields the Journal reads
        "rsi_at_entry":     None,
        "sma20_target":     None,
        "atr_at_entry":     None,
    })
    return card_id


def log_ibkr_exit_card(*, card_id: str, exit_price: float, exit_reason: str,
                        gross_pnl_usd: float | None, net_pnl_usd: float | None,
                        commission_usd: float | None,
                        entry_price: float | None = None,
                        risk_usd: float | None = None,
                        holding_hours: float | None = None) -> None:
    """Written once at exit fill confirmation. Never raises; silently no-ops if
    card_id is falsy (pre-feature trade)."""
    if not card_id:
        return
    r_mult = None
    if risk_usd and risk_usd > 0 and net_pnl_usd is not None:
        try:
            r_mult = round(float(net_pnl_usd) / float(risk_usd), 2)
        except (TypeError, ValueError, ZeroDivisionError):
            r_mult = None
    _append({
        "card_id":          card_id,
        "event":            "exit",
        "timestamp":        datetime.now(timezone.utc).isoformat(),
        "market":           "equity",
        "native_currency":  "USD",
        "exit_price":       exit_price,
        "exit_reason":      exit_reason,
        "net_pnl_native":   round(net_pnl_usd, 2) if net_pnl_usd is not None else None,
        "gross_pnl_eur":    None,   # no FX in IBKR executor
        "commission_eur":   None,
        "net_pnl_eur":      None,
        "r_multiple":       r_mult,
        "holding_hours":    holding_hours,
        "mae_eur":          None,
        "mfe_eur":          None,
    })
