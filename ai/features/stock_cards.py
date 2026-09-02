"""
ai/features/stock_cards.py -- observation cards for the SIM stocks module
(atos_runner.py), 2026-09-02.

The forex AI Journal reads paired entry+exit "observation cards" from
data/trade_observation_cards.jsonl. The stocks module (US Blend, US
Reversion) never wrote those -- it books to atos_live.db + pnl_ledger.db.
This module gives it a card writer with the SAME key shape the Journal's
_closed_trades() / build_dossiers() expect, so a stocks close flows into
the Journal for free.

DELIBERATELY a SEPARATE file -- data/stock_observation_cards.jsonl -- not
the shared forex one: many forex reports (report_giveback, report_exit_
advisor, verify_ai_data, ...) read the shared file and assume forex-only
fields (atr_at_entry, ladder rungs, EUR exposure). Isolation keeps their
blast radius zero. The Journal reads BOTH files.

CURRENCY: stocks P&L is SEK. The Journal / reports are EUR. Values are
converted at write time (`sek_per_eur`, passed by the caller which has FX
access) and stored as `*_eur`; the raw SEK figure is kept as
`net_pnl_native` / `native_currency` for traceability. R-multiple is
currency-agnostic (pnl / risk).

Pure except for the jsonl append. Never raises -- the caller (a hook in a
trading run) must be able to ignore it entirely. Gated by
ai_config.stocks_enabled() at the CALL SITE, not here.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "data")
STOCK_CARDS_LOG = os.path.join(_DATA_DIR, "stock_observation_cards.jsonl")

_ACCOUNT_ENV = "sim"   # atos_runner.py is SIM-only (_STOCKS_ENV)


def card_id_for(strategy: str, ticker: str, entry_date: str) -> str:
    """Deterministic card id from the trade's own entry_date -- so the exit
    hook (a separate process run) can reconstruct it without a DB column.
    Safe because neither equity strategy opens a second position on the same
    ticker the same day (US Reversion skips tickers it already holds; US
    Blend rebalances at most once/day)."""
    return f"{_ACCOUNT_ENV}:{strategy}:{ticker}:{entry_date}"


def _append(row: dict) -> None:
    try:
        with open(STOCK_CARDS_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")
    except Exception:
        pass


def _eur(sek: float | None, sek_per_eur: float | None) -> float | None:
    if sek is None or not sek_per_eur:
        return None
    try:
        return round(float(sek) / float(sek_per_eur), 2)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def log_stock_entry_card(*, strategy: str, ticker: str, direction: str,
                         entry_price: float, shares: float,
                         stop_price: float | None, sek_per_eur: float | None,
                         entry_date: str,
                         risk_sek: float | None = None,
                         rsi_at_entry: float | None = None,
                         sma20_target: float | None = None) -> str:
    """Written once at entry. `strategy` is "us_reversion" or "us_blend".
    `entry_date` = the trade's DB entry_date ('YYYY-MM-DD'); the returned
    card_id is derived from it so the exit hook can reconstruct it. Never
    raises."""
    card_id = card_id_for(strategy, ticker, entry_date)
    _append({
        "card_id": card_id,
        "event": "entry",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "market": "equity",
        "account_env": _ACCOUNT_ENV,
        "strategy": strategy,
        "symbol": ticker,
        "direction": direction,
        "entry_price": entry_price,
        "current_stop": stop_price,
        "quantity": shares,
        "native_currency": "SEK",
        "risk_native": round(risk_sek, 2) if risk_sek is not None else None,
        "risk_eur": _eur(risk_sek, sek_per_eur),
        # equity-specific context the Journal prompt is taught to read
        "rsi_at_entry": round(rsi_at_entry, 1) if rsi_at_entry is not None else None,
        "sma20_target": sma20_target,
        # forex-only fields the Journal dossier .get()s -- explicit None so a
        # reader never confuses "not applicable" with "missing data"
        "atr_at_entry": None,
    })
    return card_id


def log_stock_exit_card(*, card_id: str, exit_price: float, exit_reason: str,
                        gross_pnl_sek: float | None, commission_sek: float | None,
                        net_pnl_sek: float | None, holding_hours: float | None,
                        sek_per_eur: float | None,
                        risk_sek: float | None = None) -> None:
    """Written once at close, referencing the entry card's card_id. Never
    raises; silently no-ops if card_id is falsy (pre-feature trade)."""
    if not card_id:
        return
    gross_eur = _eur(gross_pnl_sek, sek_per_eur)
    net_eur = _eur(net_pnl_sek, sek_per_eur)
    comm_eur = _eur(commission_sek, sek_per_eur)
    r_mult = None
    if risk_sek and risk_sek > 0 and net_pnl_sek is not None:
        try:
            r_mult = round(float(net_pnl_sek) / float(risk_sek), 2)
        except (TypeError, ValueError, ZeroDivisionError):
            r_mult = None
    _append({
        "card_id": card_id,
        "event": "exit",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "market": "equity",
        "exit_price": exit_price,
        "exit_reason": exit_reason,
        "native_currency": "SEK",
        "net_pnl_native": round(net_pnl_sek, 2) if net_pnl_sek is not None else None,
        "gross_pnl_eur": gross_eur,
        "commission_eur": comm_eur,
        "net_pnl_eur": net_eur,
        "r_multiple": r_mult,
        "holding_hours": holding_hours,
        # not measured for equities in v1 -- explicit None, Journal skips it
        "mae_eur": None,
        "mfe_eur": None,
    })
