"""
atos/risk.py
-------------
Risk Engine -- the last gate every trade must pass before an order is placed.

Rules (hard -- never bypassed by signal quality):
  1. ATR-based stop loss placement
  2. ATR-based position sizing (risk <= 1% of allocated capital)
  3. Max open positions per market group
  4. Daily loss cap (stop new entries if total ATOS equity down 3% today)
  5. Kill switch (STOP_TRADING file)
  6. Min score threshold (no weak signals)
  7. Cash affordability check (never spend more than available)

Position sizing uses a LOCAL risk-capital tracker (same principle as
kill_switch.py in the original bot) -- not Saxo's inflated SIM balance.
"""

import os
import json
from datetime import date

import pandas as pd
from atos.logger import get_logger
logger = get_logger("atos.risk")

# -- Configuration --------------------------------------------------
STARTING_CAPITAL_SEK  = 10_000   # total paper capital for ATOS
RISK_PER_TRADE_PCT    = 0.01     # risk max 1% of market-group capital per trade
ATR_STOP_MULTIPLE     = 2.5      # stop = entry - 2.5 * ATR
MAX_POSITIONS_TOTAL   = 10       # max simultaneous open positions across all markets
MAX_POSITIONS_PER_MKT = {
    "US Equities":   4,
    "OMX30":         2,
    "DAX40":         2,
    "Commodities":   2,
    "Forex":         2,
}
MAX_DAILY_LOSS_PCT    = 0.03     # stop new entries if ATOS down 3% today
MIN_SCORE_TO_ENTER    = 55       # minimum combined score to place a BUY

KILL_SWITCH_FILE  = os.path.join(os.path.dirname(os.path.dirname(__file__)), "STOP_TRADING")
RISK_STATE_FILE   = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                  "data", "atos_risk_state.json")

# -- Commission model (mirrors existing strategy.py) ----------------
COMMISSION_RATE  = 0.0008   # 0.08% of trade value (Saxo Classic tier)
MIN_COMMISSION   = 1.00     # $1 USD minimum per order (~10 SEK)
FX_USD_TO_SEK    = 10.5     # rough SEK/USD rate for commission floor (updated daily via fx.py if available)


def _load_state() -> dict:
    os.makedirs(os.path.dirname(RISK_STATE_FILE), exist_ok=True)
    if not os.path.exists(RISK_STATE_FILE):
        state = {
            "available_cash_sek": STARTING_CAPITAL_SEK,
            "day_start_equity_sek": STARTING_CAPITAL_SEK,
            "last_reset_date": date.today().isoformat(),
        }
        _save_state(state)
        logger.debug("Risk state loaded: cash=%.2f", state.get('available_cash_sek', 0))
        return state
    with open(RISK_STATE_FILE) as f:
        state = json.load(f)
        logger.debug("Risk state loaded: cash=%.2f", state.get('available_cash_sek', 0))
        return state


def _save_state(state: dict):
    os.makedirs(os.path.dirname(RISK_STATE_FILE), exist_ok=True)
    with open(RISK_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    logger.debug("Risk state saved")


def kill_switch_active() -> bool:
    active = os.path.exists(KILL_SWITCH_FILE)
    if active:
        logger.warning("Kill switch is ACTIVE — STOP_TRADING file detected")
    return active


def get_available_cash() -> float:
    state = _load_state()
    return state.get("available_cash_sek", state.get("risk_capital_sek", STARTING_CAPITAL_SEK))


def get_risk_capital() -> float:
    return get_available_cash()


def get_total_equity(open_trades: list[dict] = None) -> float:
    """Total equity = available cash + sum(shares * entry_price) for all open positions."""
    state = _load_state()
    cash = state.get("available_cash_sek", state.get("risk_capital_sek", STARTING_CAPITAL_SEK))
    if open_trades is None:
        return cash  # no position data available
    position_value = sum(
        (t.get("shares", 0) or 0) * (t.get("entry_price", 0) or 0)
        for t in open_trades
    )
    return cash + position_value


def record_fill(cash_delta_sek: float) -> float:
    """
    Update risk capital after a fill.
    Negative for buys (capital leaves), positive for sells (capital returns).
    """
    state = _load_state()
    key = "available_cash_sek" if "available_cash_sek" in state else "risk_capital_sek"
    state[key] = max(0, state[key] + cash_delta_sek)
    _save_state(state)
    logger.info("Fill recorded: delta=%.2f SEK, new balance=%.2f SEK", cash_delta_sek, state[key])
    return state[key]


def get_day_start_equity(open_trades: list[dict] = None) -> float:
    state = _load_state()
    today = date.today().isoformat()
    if state.get("last_reset_date") != today:
        # New day -- snapshot today's starting equity
        state["day_start_equity_sek"] = get_total_equity(open_trades)
        state["last_reset_date"] = today
        _save_state(state)
    return state.get("day_start_equity_sek", state.get("day_start_equity", STARTING_CAPITAL_SEK))


def daily_loss_cap_breached(open_trades: list[dict] = None) -> bool:
    state = _load_state()
    day_start = state.get("day_start_equity_sek", state.get("day_start_equity", STARTING_CAPITAL_SEK))
    current_equity = get_total_equity(open_trades)
    if day_start <= 0:
        return False
    drawdown = (day_start - current_equity) / day_start
    breached = drawdown >= MAX_DAILY_LOSS_PCT
    if breached:
        logger.warning("Daily loss cap breached: drawdown=%.2f%% (limit=%.0f%%)", drawdown*100, MAX_DAILY_LOSS_PCT*100)
    return breached


def commission_sek(shares: float, price_sek: float) -> float:
    """Realistic Saxo commission in SEK."""
    trade_value = shares * price_sek
    rate_fee    = trade_value * COMMISSION_RATE
    min_fee_sek = MIN_COMMISSION * FX_USD_TO_SEK
    return max(rate_fee, min_fee_sek)


def calculate_stop(entry_price: float, atr: float) -> float:
    """ATR-based stop loss below entry."""
    return entry_price - ATR_STOP_MULTIPLE * atr


def calculate_position_size(equity_sek: float, entry_price_sek: float,
                             stop_price_sek: float) -> int:
    """
    Risk-based position sizing.
    Never risk more than RISK_PER_TRADE_PCT of the market group's capital.
    """
    risk_amount     = equity_sek * RISK_PER_TRADE_PCT  # 1% of TOTAL equity
    per_share_risk  = entry_price_sek - stop_price_sek
    if per_share_risk <= 0:
        return 0
    return max(0, int(risk_amount / per_share_risk))


class RiskEngine:
    """
    Stateful risk engine for one daily cycle.
    Tracks open position counts per market group within the cycle.
    """

    def __init__(self, open_trades: list[dict]):
        """open_trades: list of trade dicts from database.get_open_trades()"""
        self.open_trades = open_trades
        self.open_total = len(open_trades)
        self.open_by_market: dict[str, int] = {}
        for t in open_trades:
            mkt = t.get("market_group", "Unknown")
            self.open_by_market[mkt] = self.open_by_market.get(mkt, 0) + 1

    def approve_entry(self, ticker: str, market_group: str, score: float,
                      entry_price_sek: float, atr_sek: float,
                      cash_available_sek: float) -> dict:
        """
        Check all risk rules for a new entry.

        Returns dict:
          approved   : True / False
          reason     : string explaining decision
          shares     : int (0 if not approved)
          stop_price : float
          cost_sek   : float
          comm_sek   : float
        """
        result = {"approved": False, "reason": "", "shares": 0,
                  "stop_price": 0.0, "cost_sek": 0.0, "comm_sek": 0.0}

        if kill_switch_active():
            result["reason"] = "Kill switch active (STOP_TRADING file)"
            return result

        if daily_loss_cap_breached(self.open_trades):
            result["reason"] = f"Daily loss cap breached (>{MAX_DAILY_LOSS_PCT*100:.0f}%)"
            return result

        if score < MIN_SCORE_TO_ENTER:
            result["reason"] = f"Score {score} below minimum {MIN_SCORE_TO_ENTER}"
            return result

        if self.open_total >= MAX_POSITIONS_TOTAL:
            result["reason"] = f"Max total positions ({MAX_POSITIONS_TOTAL}) reached"
            return result

        mkt_max  = MAX_POSITIONS_PER_MKT.get(market_group, 2)
        mkt_open = self.open_by_market.get(market_group, 0)
        if mkt_open >= mkt_max:
            result["reason"] = f"{market_group}: max positions ({mkt_max}) reached"
            return result

        if pd.isna(atr_sek) or atr_sek <= 0:
            result["reason"] = "ATR unavailable -- cannot set stop"
            return result

        risk_equity = get_total_equity(self.open_trades)
        stop_price   = calculate_stop(entry_price_sek, atr_sek)
        shares       = calculate_position_size(risk_equity, entry_price_sek, stop_price)

        if shares < 1:
            result["reason"] = "Position size rounds to 0 shares -- capital too small"
            return result

        comm     = commission_sek(shares, entry_price_sek)
        cost_sek = shares * entry_price_sek + comm

        if cost_sek > cash_available_sek:
            result["reason"] = (f"Insufficient cash: need {cost_sek:.0f} SEK, "
                                f"have {cash_available_sek:.0f} SEK")
            return result

        result.update({
            "approved":   True,
            "reason":     "All checks passed",
            "shares":     shares,
            "stop_price": round(stop_price, 4),
            "cost_sek":   round(cost_sek, 2),
            "comm_sek":   round(comm, 2),
        })
        logger.info("Risk %s for %s: %s (score=%.0f, shares=%d)",
                    'APPROVED' if result['approved'] else 'REJECTED',
                    ticker, result['reason'], score, result['shares'])
        return result

    def register_fill(self, market_group: str):
        """Call after a BUY order is placed to keep in-cycle counts accurate."""
        self.open_total += 1
        self.open_by_market[market_group] = self.open_by_market.get(market_group, 0) + 1
