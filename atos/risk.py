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

# -- Configuration --------------------------------------------------
STARTING_CAPITAL_SEK  = 10_000   # total paper capital for ATOS
RISK_PER_TRADE_PCT    = 0.01     # risk max 1% of market-group capital per trade
ATR_STOP_MULTIPLE     = 2.5      # stop = entry - 2.5 * ATR
MAX_POSITIONS_TOTAL   = 8        # max simultaneous open positions across all markets
MAX_POSITIONS_PER_MKT = {
    "US Equities":   4,
    "OMX30":         2,
    "CPH25":         2,
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
        return state
    with open(RISK_STATE_FILE) as f:
        return json.load(f)


# Market-group → trading currency, used to convert open-position values into
# SEK for equity. Mirrors _currency_for() in atos_runner.py.
_MARKET_CURRENCY = {
    "US Equities": "USD",
    "OMX30":       "SEK",
    "CPH25":       "DKK",
    "DAX40":       "EUR",
    "Commodities": "USD",
    "Forex":       "USD",
}


def _save_state(state: dict):
    """Atomic write: serialize to a temp file, then os.replace() so a
    concurrent reader (e.g. the dashboard) never sees a half-written file."""
    os.makedirs(os.path.dirname(RISK_STATE_FILE), exist_ok=True)
    tmp = f"{RISK_STATE_FILE}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, RISK_STATE_FILE)


def kill_switch_active() -> bool:
    return os.path.exists(KILL_SWITCH_FILE)


def get_available_cash() -> float:
    state = _load_state()
    return state.get("available_cash_sek", state.get("risk_capital_sek", STARTING_CAPITAL_SEK))


def get_risk_capital() -> float:
    return get_available_cash()


def _position_value_sek(open_trades: list[dict]) -> float:
    """Sum open-position cost basis, converted to SEK by each position's
    trading currency. Without this, a EUR entry_price (e.g. PRX.AS) and a SEK
    entry_price get added as if the same unit — silently wrong, not rounding."""
    try:
        import fx  # local import: fx pulls in yfinance and is only on the
                   # runner's sys.path, so keep it out of module import time.
    except Exception:
        fx = None
    # Prefer the instrument's own currency from the map (handles legacy
    # positions whose stored market_group — e.g. PRX.AS as "EU_OTHER" — isn't
    # in _MARKET_CURRENCY); fall back to the market-group default, then SEK.
    imap = {}
    try:
        from instrument_map import load_instrument_map
        imap = load_instrument_map()
    except Exception:
        imap = {}
    total = 0.0
    for t in open_trades:
        shares = t.get("shares", 0) or 0
        price  = t.get("entry_price", 0) or 0
        ticker = t.get("ticker", "")
        if ticker in imap:
            currency = imap[ticker]["currency"]
        else:
            currency = _MARKET_CURRENCY.get(t.get("market_group", ""), "SEK")
        rate = 1.0
        if fx is not None and currency != "SEK":
            try:
                rate = fx.get_rate_to_sek(currency)
            except Exception:
                rate = 1.0  # fx already logs; fall back to unconverted
        total += shares * price * rate
    return total


def get_total_equity(open_trades: list[dict] = None) -> float:
    """Total equity = available cash (SEK) + SEK value of all open positions."""
    state = _load_state()
    cash = state.get("available_cash_sek", state.get("risk_capital_sek", STARTING_CAPITAL_SEK))
    if open_trades is None:
        return cash  # no position data available
    return cash + _position_value_sek(open_trades)


def record_fill(cash_delta_sek: float) -> float:
    """
    Update risk capital after a fill.
    Negative for buys (capital leaves), positive for sells (capital returns).
    """
    state = _load_state()
    key = "available_cash_sek" if "available_cash_sek" in state else "risk_capital_sek"
    state[key] = max(0, state[key] + cash_delta_sek)
    _save_state(state)
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
    return drawdown >= MAX_DAILY_LOSS_PCT


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
        return result

    def register_fill(self, market_group: str):
        """Call after a BUY order is placed to keep in-cycle counts accurate."""
        self.open_total += 1
        self.open_by_market[market_group] = self.open_by_market.get(market_group, 0) + 1
