"""
kill_switch.py
---------------
Same pattern as the Avanza OMX30 project's risk architecture: a kill switch
file that halts all trading instantly, and local persistence of the day's
starting equity so the daily loss circuit breaker (config.MAX_DAILY_LOSS_PCT)
can be checked against Saxo's own reported equity.

Also persists a RISK-CAPITAL figure, separate on purpose from Saxo's
reported account equity. Saxo SIM/demo accounts are typically funded with
a balance far larger than config.STARTING_CAPITAL, and sizing trades off
that real (inflated) balance sizes for a completely different account
than the one Phase 1's backtest was tuned and validated against — that's
what caused the oversized BUY orders that got rejected. This tracker
starts at config.STARTING_CAPITAL and moves the same way
backtest.py's self.capital does: down by the full cost on a filled buy,
up by the full proceeds on a filled sell. See record_fill().
"""

import json
import os
from datetime import date

import config
from atos.logger import get_logger
logger = get_logger("kill_switch")

KILL_SWITCH_FILE = os.path.join(os.path.dirname(__file__), "STOP_TRADING")
DAILY_STATE_FILE = os.path.join(os.path.dirname(__file__), "data", "daily_state.json")
RISK_CAPITAL_FILE = os.path.join(os.path.dirname(__file__), "data", "risk_capital.json")


def kill_switch_active() -> bool:
    active = os.path.exists(KILL_SWITCH_FILE)
    if active:
        logger.warning("Kill switch is ACTIVE — STOP_TRADING file detected")
    return active


def get_day_start_equity(current_equity: float) -> float:
    """
    Returns today's starting equity, initializing it from current_equity
    the first time this is called on a new calendar day. Persisted to disk
    so it survives the script exiting between daily runs.
    """
    os.makedirs(os.path.dirname(DAILY_STATE_FILE), exist_ok=True)
    today = date.today().isoformat()

    state = {}
    if os.path.exists(DAILY_STATE_FILE):
        with open(DAILY_STATE_FILE) as f:
            state = json.load(f)

    if state.get("date") != today:
        state = {"date": today, "day_start_equity": current_equity}
        with open(DAILY_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
        logger.info("New trading day — day start equity set to %.2f", current_equity)

    return state["day_start_equity"]


def daily_loss_cap_breached(day_start_equity: float, current_equity: float, max_daily_loss_pct: float) -> bool:
    if day_start_equity <= 0:
        return False
    drawdown_pct = (day_start_equity - current_equity) / day_start_equity
    return drawdown_pct >= max_daily_loss_pct


def get_risk_capital() -> float:
    """
    Returns the SEK capital base to use for position sizing — NOT Saxo's
    reported account equity. Initializes to config.STARTING_CAPITAL the
    first time it's called (persisted to disk so it survives the script
    exiting between daily runs); after that, moves only via record_fill().
    """
    os.makedirs(os.path.dirname(RISK_CAPITAL_FILE), exist_ok=True)
    if not os.path.exists(RISK_CAPITAL_FILE):
        state = {"risk_capital": config.STARTING_CAPITAL}
        with open(RISK_CAPITAL_FILE, "w") as f:
            json.dump(state, f, indent=2)
        capital = state["risk_capital"]
        logger.debug("Risk capital: %.2f SEK", capital)
        return capital

    with open(RISK_CAPITAL_FILE) as f:
        state = json.load(f)
    capital = state.get("risk_capital", config.STARTING_CAPITAL)
    logger.debug("Risk capital: %.2f SEK", capital)
    return capital


def record_fill(cash_delta_sek: float) -> float:
    """
    Adjusts the local risk-capital tracker after a FILLED order only
    (never call this for a blocked/failed order). Pass a negative number
    for a buy (the SEK cost leaves the sizing pool) and a positive number
    for a sell (the SEK proceeds return to it) — mirrors how
    backtest.py's self.capital behaves. Returns the new balance.
    """
    current = get_risk_capital()
    new_balance = current + cash_delta_sek
    with open(RISK_CAPITAL_FILE, "w") as f:
        json.dump({"risk_capital": new_balance}, f, indent=2)
    logger.info("Fill recorded: delta=%.2f SEK, new balance=%.2f SEK", cash_delta_sek, new_balance)
    return new_balance
