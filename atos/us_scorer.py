"""
atos/us_scorer.py
-----------------
Pure-logic helpers for the Saxo SIM Scorer Portfolio.

Entry/exit candidate selection and trailing-stop ratchet.
No I/O. No order placement. Dry-run safe.

Execution: run_saxo_scorer.py
State: atos/database.py, strategy='Scorer Portfolio'
"""
from __future__ import annotations

import pandas as pd

SCORER_STOP_PCT      = 0.06     # 6% trailing stop (matches ibkr_config scorer.portfolio)
SCORER_MAX_POS       = 15       # max concurrent positions
SCORER_MIN_SCORE     = 65.0     # trade_score minimum to hold / enter
SCORER_BUDGET_SEK    = 60_000   # total sleeve (SEK) — ~$30k equivalent
SCORER_MIN_TRADE_SEK = 200      # skip slot if notional would be less than this
STRATEGY_NAME        = "Scorer Portfolio"


def select_entries(
    portfolio_df: pd.DataFrame,
    open_tickers: set[str],
    imap: dict,
    max_pos: int = SCORER_MAX_POS,
    budget_sek: float = SCORER_BUDGET_SEK,
    fx_usd_sek: float = 10.5,
    stop_pct: float = SCORER_STOP_PCT,
    min_score: float = SCORER_MIN_SCORE,
) -> list[dict]:
    """Return buy candidates sorted by score descending.

    Each dict: {ticker, uic, qty, price_usd, stop_price_usd, score, slot_sek}
    Only tickers present in imap (Saxo instrument map) are considered.
    """
    free = max_pos - len(open_tickers)
    if free <= 0 or portfolio_df is None or portfolio_df.empty:
        return []

    slot_sek   = budget_sek / max_pos
    candidates = []

    for _, row in portfolio_df.iterrows():
        ticker = str(row.get("ticker", "")).upper()
        if not ticker or ticker in open_tickers:
            continue
        if ticker not in imap:
            continue
        score = float(row.get("trade_score", 0))
        if score < min_score:
            continue
        price_usd = float(row.get("price", 0))
        if price_usd <= 0:
            continue

        price_sek = price_usd * fx_usd_sek
        qty       = max(1, int(slot_sek / price_sek))
        if qty * price_sek < SCORER_MIN_TRADE_SEK:
            continue

        candidates.append({
            "ticker":         ticker,
            "uic":            imap[ticker]["uic"],
            "qty":            qty,
            "price_usd":      price_usd,
            "stop_price_usd": round(price_usd * (1 - stop_pct), 2),
            "score":          score,
            "slot_sek":       slot_sek,
        })

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[:free]


def select_exits(
    open_trades: list[dict],
    scorer_results: dict,
    min_score: float = SCORER_MIN_SCORE,
) -> list[dict]:
    """Return Scorer Portfolio trades that should be exited.

    Exit triggers (mirrors IBKR version):
      1. trade_score dropped below min_score
      2. hard_gate is now False (delisted / liquidity / earnings window)
    """
    scorer_open = [t for t in open_trades if t.get("strategy") == STRATEGY_NAME]
    if not scorer_open:
        return []

    all_scored = scorer_results.get("all_scored")
    if all_scored is None or all_scored.empty:
        return []

    score_map: dict[str, dict] = {}
    for _, r in all_scored.iterrows():
        t = str(r["ticker"]).upper()
        score_map[t] = {
            "trade_score": float(r.get("trade_score", 0)),
            "hard_gate":   bool(r.get("hard_gate", False)),
        }

    exits = []
    for t in scorer_open:
        sym       = t["ticker"].upper()
        sc        = score_map.get(sym, {})
        cur_score = sc.get("trade_score", 0.0)
        gate_ok   = sc.get("hard_gate", True)
        reason    = ("score_below_min" if cur_score < min_score
                     else "gate_failed" if not gate_ok
                     else "")
        if reason:
            exits.append({**t, "exit_reason": reason, "cur_score": cur_score})

    return exits


def trail_updates(
    open_trades: list[dict],
    prices_usd: dict[str, float],
    stop_pct: float = SCORER_STOP_PCT,
    min_move_usd: float = 1.0,
) -> list[dict]:
    """Return positions where the trailing stop should ratchet upward.

    Only moves the stop UP, never down. Requires a minimum improvement of
    min_move_usd before returning a position (avoids order churn on small moves).

    Each result dict:
      {trade_id, ticker, uic, qty, cur_stop, new_stop,
       old_trail_high, new_trail_high, stop_order_id, paper}
    """
    scorer_open = [t for t in open_trades if t.get("strategy") == STRATEGY_NAME]
    updates = []

    for t in scorer_open:
        sym        = t["ticker"].upper()
        cur_price  = prices_usd.get(sym, 0.0)
        if cur_price <= 0:
            continue

        cur_stop   = float(t.get("stop_price") or 0)
        trail_high = float(t.get("trailing_stop_high") or t.get("entry_price") or 0)
        new_high   = max(trail_high, cur_price)
        new_stop   = round(new_high * (1 - stop_pct), 2)

        if new_stop <= cur_stop + min_move_usd:
            continue

        updates.append({
            "trade_id":       t["id"],
            "ticker":         sym,
            "uic":            t.get("uic") or 0,
            "qty":            int(t.get("shares") or 0),
            "cur_stop":       cur_stop,
            "new_stop":       new_stop,
            "old_trail_high": trail_high,
            "new_trail_high": new_high,
            "stop_order_id":  t.get("stop_order_id"),
            "paper":          bool(t.get("paper")),
        })

    return updates
