"""
forex/runner.py
---------------
Multi-strategy daily execution runner for FX pairs. Executes via IBKR paper
(ibkr_client.py/ibkr_order.py) -- see _ibkr_uic() below for how each pair's
IBKR conId is resolved (forex/universe.py's PAIRS still carries Saxo Uics,
kept for backward-compat/logging only, not used for trading).

Strategies:
  ema         — EMA(5/30) + ADX(14) crossover  (trend-following)
  rsi         — RSI(2) pullback within EMA(200) trend (mean-reversion within trend)
  donchian    — 30-day Donchian + EMA(200) + ADX(25) strict breakout (momentum)
  bb          — Bollinger Band(20,2) + RSI(14) mean-reversion (fade extremes)
  pullback    — EMA(20) pullback in EMA(50) trend (~70% win rate, tight stops)
  gap         — Weekend gap fill — fade Sunday open vs Friday close (~80-85% WR)
  supertrend  — SuperTrend(10,3) + EMA(200) trend-following (~65% WR)
  zscore      — Z-score mean reversion: fade 2σ extremes back to mean (~63% WR)
  ml          — Logistic regression on 7 technical features (~57-62% WR)
  london_breakout — Asian/London range breakout at London open + NY open (~58-63% WR)

Universe:
  34 pairs — 7 G7 majors + 27 crosses (IBKR conIds resolved live; verify Scandi/EM with --info)
  Asian session  (14): JPY crosses, AUD/NZD pairs — run at 06:20 PKT
  London session (20): EUR/GBP/USD crosses + Scandi/CAD — run at 18:00 PKT

Usage:
    python forex/runner.py                          # all 4 strategies, all 27 pairs, dry-run
    python forex/runner.py --live                   # all 4, real IBKR paper orders
    python forex/runner.py --session asian --live    # Asian session (14 pairs, 06:20 PKT)
    python forex/runner.py --session london --live   # London session (13 pairs, 18:00 PKT)
    python forex/runner.py --exits-only --live       # stop-check only, all pairs (14:00 PKT)
    python forex/runner.py --strategy pullback       # Pullback strategy only
    python forex/runner.py --strategy ema            # EMA only
    python forex/runner.py --scan                   # 4-panel market snapshot
    python forex/runner.py --status                 # open positions
    python forex/runner.py --info                   # verify UICs live

State:
    data/forex_state.json   — open positions (keyed as "strategy:symbol")
    data/forex_orders.json  — order log (last 500 entries)
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, date, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)
sys.path.insert(0, _HERE)

import pandas as pd
import ibkr_client
import ibkr_order
import ibkr_history
import ibkr_price_service

from forex.universe import PAIRS, ASSET_TYPE, get_pair
import forex.strategy             as strat_ema
import forex.strategy_rsi         as strat_rsi
import forex.strategy_donchian    as strat_donchian
import forex.strategy_bb          as strat_bb
import forex.strategy_pullback    as strat_pullback
import forex.strategy_gap         as strat_gap
import forex.strategy_supertrend  as strat_supertrend
import forex.strategy_zscore      as strat_zscore
import forex.strategy_ml                as strat_ml
import forex.strategy_cnn_lstm         as strat_cnn_lstm
import forex.strategy_london_breakout  as strat_lbo
import pnl_tracker
import trade_logger
import strategy_learner
import forex.notifier      as fx_notify
import forex.signal_filter as signal_filter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("forex.runner")

# ── Strategy registry ─────────────────────────────────────────────────────────
STRATEGIES = {
    "ema":         strat_ema,
    "rsi":         strat_rsi,
    "donchian":    strat_donchian,
    "bb":          strat_bb,
    "pullback":    strat_pullback,
    "gap":         strat_gap,
    "supertrend":  strat_supertrend,
    "zscore":      strat_zscore,
    "ml":              strat_ml,
    "cnn_lstm":        strat_cnn_lstm,
    "london_breakout": strat_lbo,
}
SLOTS_PER_STRATEGY = {
    "ema": 4, "rsi": 34, "donchian": 4, "bb": 4,
    "pullback": 34, "gap": 34,
    "supertrend": 20, "zscore": 20, "ml": 20, "cnn_lstm": 20,
    "london_breakout": 7,   # max 7 simultaneous (one per pair)
}

# Day-trade strategies run independently of the swing book's heat budget.
# They size conservatively (1-2% risk/trade) and always close same-day,
# so the shared 6% heat cap would unfairly block them when the swing book
# is fully deployed. Each day-trade strategy has its own position-count cap
# (SLOTS_PER_STRATEGY) which already limits maximum concurrent exposure.
DAY_TRADE_STRATEGIES = {"london_breakout"}

# ── Session-aware pair groups ──────────────────────────────────────────────────
# asian  : 06:20 PKT  — Tokyo/Sydney session (JPY crosses, AUD, NZD)
# london : 18:00 PKT  — London-NY overlap (EUR, GBP, USD pairs; tightest spreads)
# all    : no filter  — every pair in universe
SESSION_PAIRS = {
    "asian": {
        "USDJPY", "EURJPY", "GBPJPY", "AUDJPY", "CADJPY", "NZDJPY", "CHFJPY",
        "AUDUSD", "NZDUSD", "AUDCAD", "AUDCHF", "AUDNZD", "NZDCAD", "NZDCHF",
    },
    "london": {
        "EURUSD", "GBPUSD", "USDCAD", "USDCHF",
        "EURGBP", "EURAUD", "EURNZD", "EURCAD", "EURCHF",
        "GBPAUD", "GBPCAD", "GBPCHF", "GBPNZD",
        # Scandinavian / EM — London open gives best liquidity
        "CADCHF", "EURNOK", "EURSEK", "USDNOK", "USDSEK", "USDDKK", "USDMXN",
    },
}

# ── Currency exposure limit ───────────────────────────────────────────────────
# Maximum times any single currency can appear (long OR short) across ALL open
# positions simultaneously. Prevents correlated drawdowns when one currency
# moves sharply — e.g. 5 EUR-long positions all losing on one ECB surprise.
MAX_CURRENCY_EXPOSURE = 3

# ── Constants ─────────────────────────────────────────────────────────────────
DATA_DIR    = os.path.join(_ROOT, "data")
STATE_FILE  = os.path.join(DATA_DIR, "forex_state.json")
ORDERS_FILE = os.path.join(DATA_DIR, "forex_orders.json")
CHART_BARS  = 340   # enough for ML strategy: EMA(200) + 126 lookback + 14 buffer

# ── Portfolio risk limits ─────────────────────────────────────────────────────
PORTFOLIO_HEAT_LIMIT  = 0.06   # pause new entries when heat ≥ 6% of equity
DRAWDOWN_PAUSE_PCT    = 0.10   # pause entries when drawdown > 10% from rolling peak
DAILY_LOSS_LIMIT_PCT  = 0.03   # block entries if today's realised P&L ≤ −3% of equity
PEAK_EQUITY_FILE      = os.path.join(DATA_DIR, "forex_peak_equity.json")

# ── Breakeven stop parameters ─────────────────────────────────────────────────
# Trend strategies: move stop to entry_price once profit ≥ this many ATRs
BREAKEVEN_THRESHOLD_ATR = 1.0
# Gap strategy: move stop to entry_price once price is this % toward the gap target
BREAKEVEN_GAP_FILL_PCT  = 0.50


# ── IBKR conId resolution ───────────────────────────────────────────────────
# PAIRS (forex/universe.py) carries Saxo Uics (pair["uic"]) -- kept as-is for
# backward compat/logging, but every broker call site in this file resolves
# the IBKR conId via this instead. Resolved once per symbol per process.

_IBKR_UIC_CACHE: dict[str, int | None] = {}


def _ibkr_uic(symbol: str) -> int | None:
    if symbol in _IBKR_UIC_CACHE:
        return _IBKR_UIC_CACHE[symbol]
    try:
        matches = ibkr_client.find_instrument(symbol, asset_type=ASSET_TYPE)
        conid = matches[0]["Uic"] if matches else None
    except Exception as exc:
        logger.warning(f"IBKR lookup failed for {symbol}: {exc}")
        conid = None
    if conid is None:
        logger.warning(f"{symbol}: not resolvable on IBKR (IDEALPRO) — "
                        f"skipping this pair for any broker call")
    _IBKR_UIC_CACHE[symbol] = conid
    return conid


# ── Connection / Account ─────────────────────────────────────────────────────

def _verify_token(scheduled_time: str = "") -> bool:
    """
    Confirm IB Gateway is reachable. Returns True if so.
    On failure, sends a "broker unreachable" alert email and returns False so
    the caller can exit cleanly without placing any orders. Named _verify_token
    for call-site compat with the Saxo version this replaces; IBKR has no
    token to verify, just a socket connection to an already-logged-in Gateway.
    """
    try:
        ibkr_client.test_connection()
        return True
    except Exception:
        logger.error("IB Gateway unreachable — sending alert email")
        fx_notify.send_broker_unreachable(scheduled_time)
        return False


_QUOTE_RATE_CACHE: dict[str, float] = {}


def _sek_per_unit(ccy: str) -> float | None:
    """SEK value of one unit of `ccy`, via the shared fx module."""
    if ccy == "SEK":
        return 1.0
    if ccy in _QUOTE_RATE_CACHE:
        return _QUOTE_RATE_CACHE[ccy]
    try:
        import fx as _fx
        rate = float(_fx.get_rate_to_sek(ccy))
        if rate <= 0:
            return None
    except Exception as exc:
        logger.warning(f"FX rate lookup failed for {ccy}: {exc}")
        return None
    _QUOTE_RATE_CACHE[ccy] = rate
    return rate


def _equity_in_quote(equity_sek: float, symbol: str) -> float | None:
    """Restate SEK equity in a pair's quote currency, for position sizing.

    ATR (and therefore stop distance) is quoted in the pair's quote currency.
    Dividing a SEK risk budget by a JPY distance is a unit error, so the
    budget is converted first.
    """
    quote = symbol[3:6] if len(symbol) >= 6 else ""
    if not quote:
        return None
    rate = _sek_per_unit(quote)
    if not rate or rate <= 0:
        return None
    return equity_sek / rate


def _risk_equity(raw_equity: float) -> float:
    """Cap the sizing base at configured real capital.

    The broker figure is IBKR's full paper equity (~1,000,000 SEK of demo
    credit), not the user's intended risk capital. Sizing off it directly
    would make positions far larger than intended -- same failure mode
    found in the Saxo version (positions ~33x the intended 300,000 SEK).
    FX trades in fine unit increments, so this scales positions down
    cleanly rather than making pairs untradeable.
    """
    try:
        import atos.capital_config as _CAP
        cap = _CAP.forex_risk_equity_sek()
    except Exception as exc:
        logger.warning(f"Could not read forex risk equity cap: {exc}")
        return raw_equity
    if cap <= 0:
        return raw_equity
    return min(raw_equity, cap) if raw_equity > 0 else cap


def _account() -> tuple[float, str]:
    equity, key = 0.0, ""
    try:
        bal    = ibkr_client.get_balances()
        equity = float(bal.get("TotalValue") or 0)
        raw    = equity
        equity = _risk_equity(equity)
        if equity < raw:
            logger.info(f"  Equity {raw:,.0f} {bal.get('Currency','SEK')} (broker) -> sizing off "
                        f"{equity:,.0f} (capped at configured capital)")
    except Exception as exc:
        logger.warning(f"Could not read equity: {exc}")
    try:
        key = ibkr_client.get_account_key()
    except Exception as exc:
        logger.warning(f"Could not read IBKR account id: {exc}")
    return equity, key


# ── Price data ────────────────────────────────────────────────────────────────

def _fetch_history(uic: int, count: int = CHART_BARS) -> pd.DataFrame | None:
    """Fetch daily OHLC for an FX instrument (IBKR conId). Mid = MIDPOINT
    whatToShow, IBKR's own bid/ask-midpoint bar type -- same "mid of the
    market" semantics as Saxo's (Ask+Bid)/2 this replaces.

    Each strategy enforces its own MIN_BARS; we just need at least a few rows
    here to confirm the instrument responded with real data. Fetches a fixed
    2-year window (comfortably covers CHART_BARS=340 daily bars even with
    weekends/holidays) and trims to the requested count.
    """
    df = ibkr_history.get_bars(uic, bar_size="1 day", duration="2 Y", what_to_show="MIDPOINT")
    if df is None or len(df) < 5:
        logger.debug(f"conId {uic}: only {0 if df is None else len(df)} bars returned")
        return None
    return df[["Open", "High", "Low", "Close"]].tail(count + 5).reset_index(drop=True)


def _fetch_history_h1(uic: int, count: int = 48) -> pd.DataFrame | None:
    """Fetch H1 OHLC bars (IBKR conId) with UTC hour label.

    Returns DataFrame with columns Open/High/Low/Close/HourUTC.
    Used by the session gap strategy to find the reference bar before each session.
    Fetches a fixed 6-day window (comfortably covers count=48+2 hourly bars
    even across a weekend) and trims to the requested count.
    """
    df = ibkr_history.get_bars(uic, bar_size="1 hour", duration="6 D", what_to_show="MIDPOINT")
    if df is None or len(df) < 4:
        return None
    df = df.tail(count + 2).reset_index(drop=True)
    df["HourUTC"] = df["Date"].dt.hour
    return df[["Open", "High", "Low", "Close", "HourUTC"]]


def _detect_gap_session() -> str | None:
    """Return the active gap session based on current UTC time, or None.

    FX session opens (UTC):
      Sydney/Weekly — Sunday 22:00 UTC (FX market reopens after weekend close)
      Tokyo         — Monday-Friday 00:00 UTC
      London        — Monday-Friday 07:00 UTC  (largest daily volume, 35% of FX)
      New York      — Monday-Friday 12:00 UTC

    Entry windows (first 90 minutes of each session):
      weekly  — Sun 22:00 UTC through Mon 06:00 UTC
                (Sunday 22:00 PKT Mon 03:00 → correct: dow=7, h>=22)
      tokyo   — Mon-Fri 00:00-01:30 UTC   (skipped on Monday — covered by weekly)
      london  — Mon-Fri 07:00-08:30 UTC
      newyork — Mon-Fri 12:00-13:30 UTC
    """
    now  = datetime.now(timezone.utc)
    dow  = now.isoweekday()   # 1=Mon … 7=Sun
    h    = now.hour
    m    = now.minute

    # Weekly: Sunday 22:00 UTC → Monday 06:00 UTC (FX reopens Sunday night)
    if (dow == 7 and h >= 22) or (dow == 1 and h < 6):
        return "weekly"

    # Session gaps: Monday-Friday only
    if 1 <= dow <= 5:
        if h == 7 or (h == 8 and m < 30):
            return "london"
        # Tokyo: skip Monday (00:00-01:30 UTC Monday is already covered by weekly window above)
        if dow >= 2 and (h == 0 or (h == 1 and m < 30)):
            return "tokyo"
        if h == 12 or (h == 13 and m < 30):
            return "newyork"
    return None


def _momentum_rank(market_data: dict, top_n: int) -> set:
    """
    Rank all pairs by 20-day normalised momentum (price-change / ATR).
    Returns the set of top_n symbol names. Exits always run on all pairs;
    only NEW entries are restricted to these top performers.
    """
    scores = {}
    for sym, df in market_data.items():
        if df is None or len(df) < 22:
            continue
        close_col = "Close" if "Close" in df.columns else (
                    "CloseAsk" if "CloseAsk" in df.columns else None)
        if close_col is None:
            continue
        c = df[close_col].dropna()
        if len(c) < 22:
            continue
        move = abs(float(c.iloc[-1]) - float(c.iloc[-21]))
        hi   = df["High"].dropna()   if "High"    in df.columns else \
               df["HighAsk"].dropna() if "HighAsk" in df.columns else c
        lo   = df["Low"].dropna()    if "Low"     in df.columns else \
               df["LowAsk"].dropna()  if "LowAsk"  in df.columns else c
        tr   = pd.concat([(hi - lo).abs(),
                           (hi - c.shift(1)).abs(),
                           (lo - c.shift(1)).abs()], axis=1).max(axis=1)
        atr  = float(tr.rolling(14).mean().iloc[-1])
        if atr > 0:
            scores[sym] = move / atr
    ranked   = sorted(scores, key=scores.__getitem__, reverse=True)
    selected = set(ranked[:top_n])
    skipped  = sorted(set(scores) - selected)
    if skipped:
        logger.info(f"Momentum pre-filter: top {top_n}/{len(scores)} pairs for entries "
                    f"| filtered: {skipped}")
    else:
        logger.info(f"Momentum pre-filter: all {len(scores)} pairs qualify")
    return selected


def _fetch_live_prices(pairs: list) -> dict:
    """Fetch current mid prices for a list of pairs (used by gap strategy)."""
    instruments = []
    for pair in pairs:
        ibkr_uic = _ibkr_uic(pair["symbol"])
        if ibkr_uic is not None:
            instruments.append({"symbol": pair["symbol"], "uic": ibkr_uic})
    prices, _status = ibkr_price_service.fetch_prices(instruments)
    return prices


def _live_price(uic: int, account_key: str) -> float | None:
    """uic here is an IBKR conId (see _ibkr_uic()); account_key is accepted
    for call-site compat but unused, same as ibkr_order's account_key."""
    prices, _status = ibkr_price_service.fetch_prices([{"symbol": "_", "uic": uic}])
    return prices.get("_")


# ── State ─────────────────────────────────────────────────────────────────────

def _load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                state = json.load(f)
            # Migrate old single-strategy keys ("EURUSD" → "ema:EURUSD")
            positions = state.setdefault("positions", {})
            old_keys  = [k for k in positions if ":" not in k]
            for k in old_keys:
                positions[f"ema:{k}"] = positions.pop(k)
            return state
        except Exception:
            pass
    return {"positions": {}, "last_run": None}


def _save_state(state: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2, default=str)
        # Atomic replace — works even when the target was created by another user
        if os.path.exists(STATE_FILE):
            os.replace(tmp, STATE_FILE)
        else:
            os.rename(tmp, STATE_FILE)
    except PermissionError:
        # State file owned by SYSTEM (Task Scheduler). Delete and recreate.
        try:
            os.remove(STATE_FILE)
        except Exception:
            pass
        try:
            os.rename(tmp, STATE_FILE)
        except Exception as e:
            logger.error(f"Cannot write state file — run PowerShell as Admin and fix permissions: {e}")
            if os.path.exists(tmp):
                os.remove(tmp)


GAP_COOLDOWN_FILE = os.path.join(DATA_DIR, "gap_cooldown.json")


def _gap_week_key() -> str:
    """ISO week key for the current week, e.g. '2026-W34'."""
    today = datetime.now(timezone.utc)
    return f"{today.isocalendar()[0]}-W{today.isocalendar()[1]:02d}"


def _load_gap_cooldown() -> set:
    """Return the set of symbols exhausted for this week's gap event."""
    week = _gap_week_key()
    if os.path.exists(GAP_COOLDOWN_FILE):
        try:
            with open(GAP_COOLDOWN_FILE) as f:
                data = json.load(f)
            if data.get("week_key") == week:
                return set(data.get("exhausted", []))
        except Exception:
            pass
    return set()


def _mark_gap_exhausted(sym: str) -> None:
    """Add sym to this week's gap cooldown so it cannot re-enter."""
    week = _gap_week_key()
    exhausted = _load_gap_cooldown()
    exhausted.add(sym)
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        with open(GAP_COOLDOWN_FILE, "w") as f:
            json.dump({"week_key": week, "exhausted": sorted(exhausted)}, f, indent=2)
    except Exception as e:
        logger.warning(f"gap_cooldown: could not write {GAP_COOLDOWN_FILE}: {e}")


def _log_order(entry: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    orders = []
    if os.path.exists(ORDERS_FILE):
        try:
            with open(ORDERS_FILE) as f:
                orders = json.load(f)
        except Exception:
            pass
    entry["timestamp"] = datetime.now().isoformat()
    orders.append(entry)
    with open(ORDERS_FILE, "w") as f:
        json.dump(orders[-500:], f, indent=2)
    # Persistent CSV — never truncated
    trade_logger.log_trade(
        module     = "forex",
        strategy   = entry.get("strategy", ""),
        symbol     = entry.get("symbol", ""),
        side       = entry.get("side", ""),
        quantity   = entry.get("quantity", 0),
        price      = entry.get("entry_price") or entry.get("exit_price") or 0,
        order_id   = entry.get("order_id"),
        dry_run    = entry.get("dry_run", False),
        stop_price = entry.get("stop_price", 0),
        notes      = entry.get("reason", ""),
    )


# ── Currency exposure helpers ─────────────────────────────────────────────────

def _currency_exposure(positions: dict) -> dict:
    """Count net directional exposure per currency across all open positions.

    Returns {currency: net_count} where positive = net long, negative = net short.
    Example: {"EUR": +2, "USD": -3, "GBP": +1}
    """
    exposure: dict[str, int] = {}
    for key, pos in positions.items():
        sym = key.split(":", 1)[1] if ":" in key else key
        if len(sym) != 6:
            continue
        base  = sym[:3]   # e.g. EUR in EURUSD
        quote = sym[3:]   # e.g. USD in EURUSD
        if pos.get("direction", "Buy") == "Buy":
            exposure[base]  = exposure.get(base,  0) + 1
            exposure[quote] = exposure.get(quote, 0) - 1
        else:
            exposure[base]  = exposure.get(base,  0) - 1
            exposure[quote] = exposure.get(quote, 0) + 1
    return exposure


def _currency_ok(sym: str, direction: str, exposure: dict) -> bool:
    """Return True if adding this position keeps all currencies within the limit."""
    base  = sym[:3]
    quote = sym[3:]
    if direction == "Buy":
        new_base  = exposure.get(base,  0) + 1
        new_quote = exposure.get(quote, 0) - 1
    else:
        new_base  = exposure.get(base,  0) - 1
        new_quote = exposure.get(quote, 0) + 1
    return (abs(new_base)  <= MAX_CURRENCY_EXPOSURE and
            abs(new_quote) <= MAX_CURRENCY_EXPOSURE)


def _update_exposure(exposure: dict, sym: str, direction: str) -> None:
    """Update exposure dict in-place after a new position is opened."""
    base  = sym[:3]
    quote = sym[3:]
    if direction == "Buy":
        exposure[base]  = exposure.get(base,  0) + 1
        exposure[quote] = exposure.get(quote, 0) - 1
    else:
        exposure[base]  = exposure.get(base,  0) - 1
        exposure[quote] = exposure.get(quote, 0) + 1


# ── Portfolio risk guard helpers ──────────────────────────────────────────────

def _portfolio_heat_pct(positions: dict, equity: float) -> float:
    """Sum of |entry-stop| × qty across all open positions, as fraction of equity."""
    if equity <= 0:
        return 0.0
    heat = sum(
        abs(float(pos.get("entry_price", 0)) - float(pos.get("stop_price", 0)))
        * float(pos.get("quantity", 0))
        for pos in positions.values()
    )
    return heat / equity


def _heat_allows_entry(positions: dict, equity: float) -> bool:
    heat = _portfolio_heat_pct(positions, equity)
    if heat >= PORTFOLIO_HEAT_LIMIT:
        logger.info(f"  [HEAT] Portfolio heat {heat:.1%} >= {PORTFOLIO_HEAT_LIMIT:.0%} — blocking entries")
        return False
    return True


def _update_peak_equity(equity: float) -> None:
    peak = 0.0
    if os.path.exists(PEAK_EQUITY_FILE):
        try:
            with open(PEAK_EQUITY_FILE) as f:
                peak = float(json.load(f).get("peak", 0))
        except Exception:
            pass
    if equity > peak:
        with open(PEAK_EQUITY_FILE, "w") as f:
            json.dump({"peak": equity, "updated": datetime.now().isoformat()}, f)


def _drawdown_allows_entry(equity: float) -> bool:
    try:
        with open(PEAK_EQUITY_FILE) as f:
            peak = float(json.load(f).get("peak", equity))
    except Exception:
        return True
    if peak <= 0:
        return True
    dd = (peak - equity) / peak
    if dd > DRAWDOWN_PAUSE_PCT:
        logger.warning(f"  [DRAWDOWN] {dd:.1%} from peak {peak:,.0f} — pausing new entries")
        return False
    return True


def _entries_blocked_by_loss_limit(equity: float) -> bool:
    today = date.today().isoformat()
    try:
        trades    = pnl_tracker.get_closed_trades(module="forex", limit=500, since=today)
        daily_pnl = sum(t.get("realized_pnl") or 0 for t in trades)
    except Exception:
        return False
    limit = -(DAILY_LOSS_LIMIT_PCT * equity)
    if daily_pnl <= limit:
        logger.warning(f"  [LOSS_LIMIT] Today's realised P&L {daily_pnl:+,.0f} <= "
                       f"-{DAILY_LOSS_LIMIT_PCT:.0%} of equity — blocking new entries")
        return True
    return False


# ── Breakeven stop helpers ────────────────────────────────────────────────────

def _amend_stop_order(order_id: str, new_price: float, sym: str, akey: str) -> bool:
    """Amend an existing IBKR stop order to a new price in place. akey
    accepted for call-site compat; unused (see ibkr_order.amend_stop)."""
    dp = 3 if sym.upper().endswith("JPY") else 5
    rounded = round(new_price, dp)
    try:
        amended = ibkr_order.amend_stop(order_id, rounded, symbol=sym, asset_type=ASSET_TYPE)
        if not amended:
            logger.warning(f"  [BREAKEVEN] Stop order {order_id} not found among open "
                           f"orders — heal will re-place on next run")
            return False
        logger.info(f"  [BREAKEVEN] Stop order {order_id} amended → {rounded:.{dp}f}  ✓")
        return True
    except Exception as exc:
        logger.warning(f"  [BREAKEVEN] Could not amend stop {order_id}: {exc} "
                       f"— heal will re-place on next run")
        return False


def _apply_breakeven_stop(key: str, pos: dict, df, strat_name: str,
                           akey: str, dry_run: bool) -> bool:
    """Move stop to entry_price (breakeven) once position is sufficiently in profit.

    Trend strategies : triggers when unrealised profit >= BREAKEVEN_THRESHOLD_ATR × ATR_at_entry
    Gap strategy     : triggers when price is >= BREAKEVEN_GAP_FILL_PCT toward gap_target

    One-shot per position — stored as pos["breakeven_triggered"] = True.
    Returns True if the stop was moved this call.
    """
    if pos.get("breakeven_triggered"):
        return False
    if df is None or len(df) < 1:
        return False

    direction   = pos.get("direction", "Buy")
    entry_price = float(pos.get("entry_price", 0))
    cur_stop    = float(pos.get("stop_price", 0))
    cur_close   = float(df["Close"].iloc[-1])
    cur_high    = float(df["High"].iloc[-1])
    cur_low     = float(df["Low"].iloc[-1])

    if strat_name == "gap":
        gap_target = float(pos.get("gap_target", entry_price))
        if abs(gap_target - entry_price) < 1e-8:
            return False
        if direction == "Buy":
            fill_pct      = (cur_high - entry_price) / (gap_target - entry_price)
            should_trigger = fill_pct >= BREAKEVEN_GAP_FILL_PCT and cur_stop < entry_price
        else:
            fill_pct      = (entry_price - cur_low) / (entry_price - gap_target)
            should_trigger = fill_pct >= BREAKEVEN_GAP_FILL_PCT and cur_stop > entry_price
    else:
        atr_entry = float(pos.get("atr_at_entry", 0))
        if atr_entry <= 0:
            return False
        threshold = BREAKEVEN_THRESHOLD_ATR * atr_entry
        if direction == "Buy":
            should_trigger = (cur_close - entry_price) >= threshold and cur_stop < entry_price
        else:
            should_trigger = (entry_price - cur_close) >= threshold and cur_stop > entry_price

    if not should_trigger:
        return False

    sym = key.split(":", 1)[1] if ":" in key else key
    tag = "[DRY] " if dry_run else ""
    logger.info(f"  {tag}[BREAKEVEN] {key}: stop {cur_stop:.5f} → {entry_price:.5f} (entry)")

    pos["stop_price"]          = entry_price
    pos["breakeven_triggered"] = True

    if not dry_run:
        stop_oid = pos.get("stop_order_id")
        if stop_oid and stop_oid not in ("synced", None, ""):
            _amend_stop_order(stop_oid, entry_price, sym, akey)

    return True


# ── Per-strategy exit / entry helpers ─────────────────────────────────────────

def _run_exits(strat_name: str, strat_mod, positions: dict,
               market_data: dict, akey: str, dry_run: bool,
               today_str: str) -> int:
    exits = 0
    prefix = f"{strat_name}:"
    for key in [k for k in positions if k.startswith(prefix)]:
        sym       = key.split(":", 1)[1]
        pos       = positions[key]
        df        = market_data.get(sym)
        ed        = pos.get("entry_date", today_str)
        cal_days  = (date.today() - date.fromisoformat(ed)).days

        # Trail stop
        if df is not None and hasattr(strat_mod, "trailing_stop_update"):
            from forex.strategy import _atr as _ema_atr
            try:
                atr_fn = getattr(strat_mod, "_atr", _ema_atr)
                atr_now  = float(atr_fn(df["High"], df["Low"], df["Close"]).iloc[-1])
                cur_stop = float(pos.get("stop_price", 0))
                new_stop = strat_mod.trailing_stop_update(
                    cur_stop, float(df["Close"].iloc[-1]), atr_now, pos.get("direction", "Buy"))
                if round(new_stop, 6) != round(cur_stop, 6) and new_stop > 0:
                    pos["stop_price"] = round(new_stop, 6)
            except Exception:
                pass

        # Breakeven stop — move to entry_price once profit threshold reached
        _apply_breakeven_stop(key, pos, df, strat_name, akey, dry_run)

        exit_flag, reason = strat_mod.should_exit(pos, df, cal_days)
        if not exit_flag:
            continue

        uic = _ibkr_uic(sym)
        if uic is None:
            logger.warning(f"  [{strat_name}] {sym}: unresolvable on IBKR — cannot check exit")
            continue
        qty        = pos.get("quantity", 1_000)
        direction  = pos.get("direction", "Buy")
        is_long    = direction == "Buy"
        close_side = "Sell" if is_long else "Buy"
        live_px    = _live_price(uic, akey) or float(pos.get("entry_price", 0))
        entry      = float(pos.get("entry_price", 0))
        pnl_pct    = ((live_px - entry) / entry * 100) if is_long else ((entry - live_px) / entry * 100)

        tag = "L" if is_long else "S"
        if dry_run:
            logger.info(f"  [DRY] {close_side:<4} {qty:,}x {sym}[{tag}] "
                        f"({strat_name}) — {reason}  P&L {pnl_pct:+.2f}%")
        else:
            # This is a runner-driven exit (time stop, trailing stop, etc.), not
            # a broker-side stop/TP fill -- the bracket's resting stop/TP legs
            # don't know the position they protected is about to be gone, and
            # would sit as orphaned orders that could open an unintended reverse
            # position if later triggered. Cancel them before closing.
            for oid in (pos.get("stop_order_id"), pos.get("tp_order_id")):
                if oid and oid not in ("synced", None, ""):
                    ibkr_client.cancel_order(oid)
            resp = ibkr_client.place_market_order(uic, ASSET_TYPE, close_side, qty)
            logger.info(f"  {close_side} {resp.get('OrderId','?')}: {qty:,}x {sym}[{tag}] "
                        f"({strat_name}) — {reason}  P&L {pnl_pct:+.2f}%")

        _log_order({"side": close_side, "symbol": sym, "strategy": strat_name,
                    "uic": uic, "quantity": qty, "exit_price": live_px,
                    "reason": reason, "pnl_pct": round(pnl_pct, 3), "dry_run": dry_run})
        if not dry_run:
            pnl_tracker.log_close("forex", sym, live_px, reason, strategy=strat_name)
            if strat_name == "gap":
                _mark_gap_exhausted(sym)
            # Label the signal-log outcome for ML training data
            raw_pnl = ((live_px - pos["entry_price"]) * qty if is_long
                       else (pos["entry_price"] - live_px) * qty)
            signal_filter.label_outcome(key, won=raw_pnl > 0)
            if strat_name == "london_breakout":
                fx_notify.send_lbo_trade_closed(
                    symbol=sym, direction=direction,
                    entry=float(pos.get("entry_price", live_px)),
                    exit_px=live_px, pnl_pct=pnl_pct, units=qty,
                    reason=reason,
                    session=pos.get("lbo_session", ""),
                )
        del positions[key]
        exits += 1
    return exits


def _run_entries(strat_name: str, strat_mod, positions: dict,
                 market_data: dict, equity: float, akey: str,
                 dry_run: bool, today_str: str,
                 live_prices: dict | None = None,
                 agreement: dict | None = None,
                 weight: float = 1.0) -> int:
    # Gap strategy: only run during defined session windows (weekly/london/newyork/tokyo).
    # Outside those windows, any overnight move ≥ 0.10% would generate false signals
    # with none of the structural fill edge that makes gap fading profitable.
    gap_session: str | None = _detect_gap_session() if strat_name == "gap" else None
    if strat_name == "gap" and gap_session is None:
        logger.info(f"  [gap] Entries skipped — not in a gap session window "
                    f"({datetime.now(timezone.utc).strftime('%A %H:%M UTC')})")
        return 0

    base_slots = SLOTS_PER_STRATEGY[strat_name]
    max_slots  = max(1, int(base_slots * strategy_learner.slot_scale(weight)))
    prefix     = f"{strat_name}:"
    held       = sum(1 for k in positions if k.startswith(prefix))
    slots_free = max_slots - held
    if slots_free <= 0 or equity <= 0:
        return 0

    open_syms = {k.split(":", 1)[1] for k in positions if k.startswith(prefix)}

    if strat_name == "london_breakout":
        # London/NY Breakout: fetch H1 bars for the 7 liquid pairs only.
        lbo_pairs  = strat_lbo.PAIRS
        h1_lbo: dict = {}
        pair_meta: dict = {}
        for pi in PAIRS:
            sym = pi["symbol"]
            if sym not in lbo_pairs:
                continue
            ibkr_uic = _ibkr_uic(sym)
            if ibkr_uic is None:
                continue
            h1_lbo[sym]  = _fetch_history_h1(ibkr_uic)
            pair_meta[sym] = {"pip_size": pi.get("pip_size", 0.0001)}
        # LBO is a separate day-trading book with its own capital, not a slice of
        # the swing account. Passing `equity` here made it risk 1.5% of everything.
        try:
            import atos.capital_config as _CAP
            lbo_equity = _CAP.forex_lbo_capital_sek()
        except Exception:
            lbo_equity = 15_000.0
        # stop_distance is in each pair's quote currency, so convert per pair.
        lbo_eq_by_pair = {}
        for sym in h1_lbo:
            eq_q = _equity_in_quote(lbo_equity, sym)
            if eq_q is not None:
                lbo_eq_by_pair[sym] = eq_q
        logger.info(f"  [london_breakout] book capital {lbo_equity:,.0f} SEK "
                    f"(1.5% = {lbo_equity*strat_lbo.RISK_PCT:,.0f} SEK/trade)")
        signals = strat_lbo.generate_signals(
            h1_lbo, pair_meta, open_syms,
            account_equity=lbo_equity, equity_by_pair=lbo_eq_by_pair,
        )
        logger.info(f"  [london_breakout] {len(h1_lbo)} pairs scanned → {len(signals)} signal(s)")
    elif strat_name == "gap" and gap_session != "weekly":
        # Session gap (London / NY / Tokyo): fetch H1 bars for ALL 34 pairs.
        # No pair-list restriction — gap_pct filter selects only pairs that actually
        # gapped (EURNOK, USDSEK, NZDJPY, AUDCHF etc. all get a fair look).
        gap_exhausted = _load_gap_cooldown()
        if gap_exhausted:
            logger.info(f"  [gap:{gap_session}] cooldown: skipping {sorted(gap_exhausted)}")
        h1_data: dict = {}
        for pi in PAIRS:
            ibkr_uic = _ibkr_uic(pi["symbol"])
            h1_data[pi["symbol"]] = _fetch_history_h1(ibkr_uic) if ibkr_uic is not None else None
        signals = strat_mod.generate_session_signals(
            gap_session, h1_data, open_symbols=open_syms, live_prices=live_prices or {},
            exhausted_symbols=gap_exhausted,
        )
        logger.info(f"  [gap:{gap_session}] {len(h1_data)} pairs scanned → {len(signals)} signal(s)")
    elif getattr(strat_mod, "NEEDS_LIVE_PRICES", False):
        kw: dict = {"open_symbols": open_syms, "live_prices": live_prices or {}}
        if strat_name == "gap":
            gap_exhausted = _load_gap_cooldown()
            if gap_exhausted:
                logger.info(f"  [gap:weekly] cooldown: skipping {sorted(gap_exhausted)}")
            kw["exhausted_symbols"] = gap_exhausted
        signals = strat_mod.generate_signals(market_data, **kw)
    else:
        signals = strat_mod.generate_signals(market_data, open_symbols=open_syms)

    exposure  = _currency_exposure(positions)
    agreement = agreement or {}

    entries = 0
    for sig in signals:
        if entries >= slots_free:
            break
        sym       = sig["symbol"]
        direction = sig["direction"]

        # ── Signal filter: consensus + ML meta-filter ──────────────────────
        passes, features, reason = signal_filter.evaluate(
            sym, direction, sig, agreement, STRATEGIES)
        if not passes:
            logger.info(f"  [{strat_name}] SKIP {sym}[{direction}] "
                        f"— signal_filter: {reason}")
            continue
        agrees = features["agreement_count"]
        ml_info = (f"  ml_prob={features['ml_prob']}" if features.get("ml_prob") else "")

        if not _currency_ok(sym, direction, exposure):
            logger.info(f"  [{strat_name}] SKIP {sym}[{direction}] "
                        f"— currency exposure limit (max {MAX_CURRENCY_EXPOSURE})")
            continue
        if strat_name not in DAY_TRADE_STRATEGIES and not _heat_allows_entry(positions, equity):
            break   # heat cap reached — stop all entries for this strategy
        pair_info = get_pair(sym)
        uic       = _ibkr_uic(sym)
        if uic is None:
            logger.info(f"  [{strat_name}] SKIP {sym}[{direction}] — not resolvable on IBKR")
            continue
        rp_kw     = {"risk_pct": sig["risk_pct_override"]} if "risk_pct_override" in sig else {}
        if "units" in sig:
            qty = sig["units"]   # london_breakout pre-computes sizing from SEK capital
        else:
            # size_position() computes units = (equity * RISK_PCT) / (mult * ATR).
            # ATR is in the pair's QUOTE currency but equity is in SEK, so without
            # converting, realised risk scales with the numeric size of the quote
            # currency: JPY pairs risked ~0.1% while USD/CHF pairs risked 20-38%,
            # a 447x spread across positions that are all nominally "1%".
            # Converting equity into the quote currency makes the division
            # dimensionally correct and gives every pair the same real risk.
            eq_quote = _equity_in_quote(equity, sym)
            if eq_quote is None:
                logger.warning(f"  [{strat_name}] SKIP {sym}: no FX rate for quote "
                               f"currency — refusing to size without conversion")
                continue
            qty = strat_mod.size_position(eq_quote, sig["atr"],
                                          pair_info["min_units"], **rp_kw)

        tag    = "LONG" if direction == "Buy" else "SHORT"
        detail = (f"rsi={sig['rsi']:.1f}" if "rsi" in sig
                  else f"range={sig.get('range_pips', 0):.0f}p" if "range_pips" in sig
                  else f"breakout={sig.get('breakout_level', 0):.5f}" if "breakout_level" in sig
                  else f"adx={sig.get('adx', 0):.1f}")
        stop_oid = None; tp_oid = None
        agree_tag = f"  agree={agrees}/{len(STRATEGIES)}{ml_info}"
        if dry_run:
            logger.info(f"  [DRY] {direction:<4} {qty:,}x {sym}[{tag}] "
                        f"({strat_name})  @ {sig['close']:.5f}  "
                        f"stop={sig['stop_price']:.5f}  {detail}{agree_tag}")
        else:
            tp = sig.get("tp_price") or sig.get("gap_target")   # london_breakout + gap both provide TP
            try:
                entry_oid, stop_oid, tp_oid = ibkr_order.place_with_stop(
                    account_key       = akey,
                    uic               = uic,
                    asset_type        = ASSET_TYPE,
                    amount            = qty,
                    buy_sell          = direction,
                    stop_price        = sig["stop_price"],
                    label             = f"{strat_name}:{sym}",
                    take_profit_price = tp,
                    symbol            = sym,
                )
            except Exception as exc:
                # place_with_stop() raises if the entry itself was rejected
                # (bad symbol, insufficient funds, an account's own FX
                # trading restrictions, etc.) rather than silently returning
                # order ids for an order that never actually filled -- skip
                # this signal rather than recording a position that doesn't
                # exist at the broker, and keep going so one bad signal
                # doesn't abort the rest of this strategy's entries.
                logger.warning(f"  [{strat_name}] {direction} {sym} entry REJECTED: {exc}")
                continue
            tp_info = f"  tp_order={tp_oid}" if tp_oid else ""
            logger.info(f"  {direction} {entry_oid}: {qty:,}x {sym}[{tag}] "
                        f"({strat_name})  @ {sig['close']:.5f}  stop={sig['stop_price']:.5f}"
                        f"  stop_order={stop_oid}{tp_info}{agree_tag}")

        pos_key = f"{strat_name}:{sym}"
        pos_record = {
            "uic":            uic,
            "direction":      direction,
            "entry_price":    sig["close"],
            "stop_price":     sig["stop_price"],
            "quantity":       qty,
            "entry_date":     today_str,
            "entry_datetime": datetime.now().isoformat(),  # hour-based time stop for session gaps
            "atr_at_entry":   sig["atr"],
            "stop_order_id":  stop_oid,
            "tp_order_id":    tp_oid if not dry_run else None,
        }
        if "gap_target" in sig:
            pos_record["gap_target"]   = sig["gap_target"]
            pos_record["friday_close"] = sig.get("friday_close", sig["gap_target"])
            pos_record["gap_pct"]      = sig.get("gap_pct", 0.0)
        if "gap_type" in sig:
            pos_record["gap_type"] = sig["gap_type"]   # "weekly"/"london"/"newyork"/"tokyo"
        if "tp_price" in sig:
            pos_record["tp_price"]    = sig["tp_price"]
            pos_record["range_high"]  = sig.get("range_high", 0)
            pos_record["range_low"]   = sig.get("range_low",  0)
            pos_record["lbo_session"] = sig.get("session", "")
        positions[pos_key] = pos_record
        _update_exposure(exposure, sym, direction)
        oid = entry_oid if not dry_run else None
        _log_order({"side": direction, "symbol": sym, "strategy": strat_name,
                    "uic": uic, "quantity": qty, "entry_price": sig["close"],
                    "stop_price": sig["stop_price"], "dry_run": dry_run})
        if not dry_run:
            pnl_tracker.log_open("forex", strat_name, sym, direction, qty,
                                 sig["close"], sig["stop_price"], order_id=oid)
            signal_filter.log_signal(pos_key, features)   # builds ML training data
            if strat_name == "london_breakout":
                fx_notify.send_lbo_trade_opened(
                    symbol=sym, direction=direction,
                    entry=sig["close"], stop=sig["stop_price"],
                    tp=sig.get("tp_price", 0), units=qty,
                    session=sig.get("session", ""),
                    range_pips=sig.get("range_pips", 0),
                )
        entries += 1
    return entries


# ── Heal missing stop / TP orders ─────────────────────────────────────────────

def _fetch_open_orders(akey: str) -> set | None:
    """
    Return set of (conId, buy_sell, order_type) for all open orders.
    Returns None if the query fails (heal will be skipped to avoid duplicates).
    akey accepted for call-site compat; unused (ibkr_client scopes to
    whichever account IB Gateway is logged into).
    """
    try:
        return ibkr_client.get_open_orders()
    except Exception as exc:
        logger.warning(f"[heal] Could not fetch open orders from IBKR: {exc} — skipping heal")
        return None


def _heal_missing_stops(positions: dict, akey: str) -> int:
    """
    Place a standalone stop order for positions missing a stop_order_id.
    Queries IBKR first to avoid creating duplicate stops.
    Returns number of stops successfully placed.
    """
    missing = [(k, v) for k, v in positions.items()
               if v.get("stop_order_id") is None]
    if not missing:
        return 0

    open_orders = _fetch_open_orders(akey)
    if open_orders is None:
        return 0  # can't safely distinguish existing from missing — skip

    healed = 0
    for key, pos in missing:
        sym = key.split(":", 1)[1] if ":" in key else key
        uic = _ibkr_uic(sym)
        if uic is None:
            logger.warning(f"  [heal_stops] {key}: not resolvable on IBKR — skipping")
            continue
        direction  = pos.get("direction", "Buy")
        qty        = pos["quantity"]
        stop_price = pos["stop_price"]
        close_side = "Sell" if direction == "Buy" else "Buy"

        if (uic, close_side.upper(), "STP") in open_orders or \
           (uic, close_side.upper(), "STP LMT") in open_orders:
            # Stop already exists in IBKR — just record it
            pos["stop_order_id"] = "synced"
            logger.debug(f"  [heal_stops] {key}  stop already in IBKR (synced)")
            continue

        logger.info(f"[heal_stops] {key} missing stop — placing {close_side} Stop@{stop_price}...")
        try:
            stop_oid = ibkr_order.place_stop(uic, ASSET_TYPE, qty, close_side, stop_price, symbol=sym)
            pos["stop_order_id"] = stop_oid
            logger.info(f"  [heal_stops] {key}  stop_id={stop_oid}  ✓")
            healed += 1
        except Exception as exc:
            logger.warning(f"  [heal_stops] FAILED for {key}: {exc}")
    return healed


def _heal_missing_tp(positions: dict, akey: str) -> int:
    """
    Place a standalone Limit TP order for gap positions that have a
    gap_target but no tp_order_id. Queries IBKR first to avoid duplicates.
    Returns number of TP orders successfully placed.
    """
    missing = [(k, v) for k, v in positions.items()
               if k.startswith("gap:") and
               v.get("gap_target") is not None and
               v.get("tp_order_id") is None]
    if not missing:
        return 0

    open_orders = _fetch_open_orders(akey)
    if open_orders is None:
        return 0

    healed = 0
    for key, pos in missing:
        sym = key.split(":", 1)[1] if ":" in key else key
        uic = _ibkr_uic(sym)
        if uic is None:
            logger.warning(f"  [heal_tp] {key}: not resolvable on IBKR — skipping")
            continue
        direction  = pos.get("direction", "Buy")
        qty        = pos["quantity"]
        tp_price   = pos["gap_target"]
        close_side = "Sell" if direction == "Buy" else "Buy"

        if (uic, close_side.upper(), "LMT") in open_orders:
            pos["tp_order_id"] = "synced"
            logger.debug(f"  [heal_tp] {key}  TP already in IBKR (synced)")
            continue

        logger.info(f"[heal_tp] {key} missing TP — placing {close_side} Limit@{tp_price}...")
        try:
            tp_oid = ibkr_order.place_limit(uic, ASSET_TYPE, qty, close_side, tp_price, symbol=sym)
            pos["tp_order_id"] = tp_oid
            logger.info(f"  [heal_tp] {key}  tp_id={tp_oid}  ✓")
            healed += 1
        except Exception as exc:
            logger.warning(f"  [heal_tp] FAILED for {key}: {exc}")
    return healed


# ── Main daily cycle ──────────────────────────────────────────────────────────

def run_exits_only(dry_run: bool = True,
                   active_strategies: list | None = None,
                   session: str = "all") -> dict:
    """Check stops and time-stops on all open positions — no new entries."""
    if active_strategies is None:
        active_strategies = list(STRATEGIES)

    session_filter = SESSION_PAIRS.get(session) if session != "all" else None
    active_pairs   = [p for p in PAIRS
                      if session_filter is None or p["symbol"] in session_filter]

    mode = "DRY-RUN" if dry_run else "LIVE (IBKR paper)"
    logger.info("=" * 60)
    logger.info(f"  FX Runner [EXITS-ONLY] — {mode}  session={session}  "
                f"{datetime.now().strftime('%Y-%m-%d %H:%M')}")
    logger.info("=" * 60)

    if not dry_run and not _verify_token("exits-only"):
        return {"exits": 0, "holding": 0, "dry_run": False, "error": "token_expired"}

    state     = _load_state()
    positions = state.setdefault("positions", {})
    _, akey   = _account()
    today_str = date.today().isoformat()

    logger.info(f"Open positions : {len(positions)}")
    logger.info(f"Pairs checked  : {len(active_pairs)} ({session} session)")

    if not dry_run:
        healed = _heal_missing_stops(positions, akey) + _heal_missing_tp(positions, akey)
        if healed:
            _save_state(state)

    market_data: dict[str, pd.DataFrame | None] = {}
    for pair in active_pairs:
        ibkr_uic = _ibkr_uic(pair["symbol"])
        market_data[pair["symbol"]] = _fetch_history(ibkr_uic) if ibkr_uic is not None else None

    total_exits = 0
    for strat_name in active_strategies:
        strat_mod = STRATEGIES[strat_name]
        exits = _run_exits(strat_name, strat_mod, positions,
                           market_data, akey, dry_run, today_str)
        if exits:
            logger.info(f"  [{strat_name}] Closed {exits} position(s)")
        total_exits += exits

    logger.info("=" * 60)
    logger.info(f"  EXITS-ONLY complete — Closed: {total_exits}  "
                f"|  Still holding: {len(positions)}")
    for key, pos in positions.items():
        strat, sym = key.split(":", 1) if ":" in key else ("ema", key)
        df      = market_data.get(sym)
        cur_px  = float(df["Close"].iloc[-1]) if df is not None else pos["entry_price"]
        is_long = pos.get("direction", "Buy") == "Buy"
        pnl_pct = ((cur_px - pos["entry_price"]) / pos["entry_price"] * 100
                   if is_long else
                   (pos["entry_price"] - cur_px) / pos["entry_price"] * 100)
        held    = (date.today() - date.fromisoformat(pos.get("entry_date", today_str))).days
        tag     = "L" if is_long else "S"
        logger.info(f"  HOLD [{strat}] {sym}[{tag}]  "
                    f"entry={pos['entry_price']:.5f}  now={cur_px:.5f}  "
                    f"P&L {pnl_pct:+.2f}%  stop={pos['stop_price']:.5f}  {held}d")
    logger.info("=" * 60)

    # See run_daily() — dry-run exits mutate `positions`, so saving would delete
    # real open positions from tracking.
    if dry_run:
        logger.info("  [DRY] state NOT saved — open positions left untouched")
    else:
        state["last_exits_check"] = datetime.now().isoformat()
        _save_state(state)
    return {"exits": total_exits, "holding": len(positions), "dry_run": dry_run}


def run_daily(dry_run: bool = True, active_strategies: list | None = None,
              session: str = "all") -> dict:
    if active_strategies is None:
        active_strategies = list(STRATEGIES)

    session_filter = SESSION_PAIRS.get(session) if session != "all" else None
    active_pairs   = [p for p in PAIRS
                      if session_filter is None or p["symbol"] in session_filter]

    strat_label = "+".join(active_strategies)
    mode        = "DRY-RUN" if dry_run else "LIVE (IBKR paper)"
    run_time    = datetime.now().strftime("%H:%M")
    logger.info("=" * 60)
    logger.info(f"  FX Runner [{strat_label}] — {mode}  session={session}  "
                f"{datetime.now().strftime('%Y-%m-%d %H:%M')}")
    logger.info("=" * 60)

    # ── Token guard — email alert + abort if expired ──────────────────────────
    if not dry_run and not _verify_token(run_time):
        return {"exits": 0, "entries": 0, "holding": 0,
                "dry_run": False, "error": "token_expired"}

    state     = _load_state()
    positions = state.setdefault("positions", {})
    equity, akey = _account()
    today_str    = date.today().isoformat()

    total_slots = sum(SLOTS_PER_STRATEGY[s] for s in active_strategies)
    logger.info(f"Account equity : {equity:,.0f}")
    logger.info(f"Open positions : {len(positions)} / {total_slots} total slots")
    logger.info(f"FX pairs scanned: {len(active_pairs)} of {len(PAIRS)} ({session} session)")
    logger.info(f"Strategies     : {strat_label}")

    # ── Portfolio risk pre-flight ─────────────────────────────────────────────
    if not dry_run:
        _update_peak_equity(equity)
    loss_limit_hit  = not dry_run and _entries_blocked_by_loss_limit(equity)
    drawdown_paused = not dry_run and not _drawdown_allows_entry(equity)
    entries_blocked = loss_limit_hit or drawdown_paused
    if entries_blocked:
        reason = "daily loss limit" if loss_limit_hit else "drawdown circuit breaker"
        logger.warning(f"  [RISK] New entries BLOCKED for this run — {reason}")
    heat_pct = _portfolio_heat_pct(positions, equity)
    logger.info(f"Portfolio heat : {heat_pct:.1%}  (limit {PORTFOLIO_HEAT_LIMIT:.0%})")

    # ── Heal missing stop / TP orders (e.g. from prior API failures) ────────────
    healed_stops = healed_tp = 0
    if not dry_run:
        healed_stops = _heal_missing_stops(positions, akey)
        healed_tp    = _heal_missing_tp(positions, akey)
        if healed_stops or healed_tp:
            _save_state(state)

    # ── Fetch price history once for active session pairs ─────────────────────
    market_data: dict[str, pd.DataFrame | None] = {}
    for pair in active_pairs:
        ibkr_uic = _ibkr_uic(pair["symbol"])
        market_data[pair["symbol"]] = _fetch_history(ibkr_uic) if ibkr_uic is not None else None

    # ── Momentum pre-filter: restrict NEW entries to top trending pairs ────────
    # Exits always run on the full market_data (we never suppress stop-checks).
    # Entries only fire on the top 60% of pairs ranked by 20-day momentum / ATR.
    _top_n_entry = max(8, round(len(active_pairs) * 0.6))
    top_pairs    = _momentum_rank(market_data, top_n=_top_n_entry)
    entry_market_data = {k: v for k, v in market_data.items() if k in top_pairs}

    # ── Fetch live prices if any strategy needs them (gap strategy) ───────────
    needs_live = any(getattr(STRATEGIES[s], "NEEDS_LIVE_PRICES", False)
                     for s in active_strategies)
    live_prices: dict = _fetch_live_prices(active_pairs) if needs_live else {}
    if live_prices:
        logger.info(f"Live prices fetched : {len(live_prices)} pairs")

    # ── Pre-compute cross-strategy agreement map for signal filter ────────────
    agreement = signal_filter.compute_agreement(market_data, live_prices, STRATEGIES)
    sf_status  = signal_filter.training_status()
    logger.info(f"Signal filter: consensus active | "
                f"ML training data: {sf_status['labeled_trades']}/{signal_filter.MIN_TRADES_FOR_ML} trades "
                f"| ML model: {'✓ active' if sf_status['model_exists'] else '— not yet (need more data)'}")

    # ── Load strategy weights — higher weight runs first (priority access) ────
    strat_weights = strategy_learner.get_weights("forex")
    strategy_learner.log_weights_table("forex")
    # Sort active strategies by weight descending so proven winners get first
    # pick of currency exposure slots and portfolio heat capacity
    active_strategies = sorted(
        active_strategies,
        key=lambda s: strat_weights.get(s, 1.0),
        reverse=True,
    )

    # ── Run each strategy ─────────────────────────────────────────────────────
    total_exits = total_entries = 0
    for strat_name in active_strategies:
        strat_mod = STRATEGIES[strat_name]
        prefix    = f"{strat_name}:"
        holding   = sum(1 for k in positions if k.startswith(prefix))
        w         = strat_weights.get(strat_name, 1.0)
        logger.info(f"{'─'*60}")
        logger.info(f"  Strategy: {strat_name.upper()}  weight={w:.3f}  "
                    f"slots_scale=×{strategy_learner.slot_scale(w):.2f}")

        exits   = _run_exits(strat_name, strat_mod, positions,
                             market_data, akey, dry_run, today_str)
        entries = 0
        # Day-trade strategies bypass the swing-book drawdown gate but still
        # respect the daily loss limit (hard safety rail).
        run_entries = (not entries_blocked
                       or (strat_name in DAY_TRADE_STRATEGIES and not loss_limit_hit))
        if run_entries:
            # Gap strategy bypasses the momentum filter — it needs all pairs for
            # gap-percentage detection. All others only look at top-momentum pairs.
            _edata = market_data if strat_name in ("gap", "london_breakout") else entry_market_data
            entries = _run_entries(strat_name, strat_mod, positions,
                                   _edata, equity, akey, dry_run, today_str,
                                   live_prices=live_prices, agreement=agreement,
                                   weight=w)

        if exits == 0 and entries == 0:
            remaining = sum(1 for k in positions if k.startswith(prefix))
            logger.info(f"  [{strat_name}] No signals today  |  Holding: {remaining}")

        total_exits   += exits
        total_entries += entries

    # ── Summary ───────────────────────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info(f"  TOTAL — Exits: {total_exits}  |  Entries: {total_entries}  "
                f"|  Holding: {len(positions)}")
    for key, pos in positions.items():
        strat, sym = key.split(":", 1) if ":" in key else ("ema", key)
        df      = market_data.get(sym)
        cur_px  = float(df["Close"].iloc[-1]) if df is not None else pos["entry_price"]
        is_long = pos.get("direction", "Buy") == "Buy"
        pnl_pct = ((cur_px - pos["entry_price"]) / pos["entry_price"] * 100
                   if is_long else
                   (pos["entry_price"] - cur_px) / pos["entry_price"] * 100)
        held    = (date.today() - date.fromisoformat(pos.get("entry_date", today_str))).days
        tag     = "L" if is_long else "S"
        logger.info(f"  HOLD [{strat}] {sym}[{tag}]  qty={pos['quantity']:,}  "
                    f"entry={pos['entry_price']:.5f}  now={cur_px:.5f}  "
                    f"P&L {pnl_pct:+.2f}%  stop={pos['stop_price']:.5f}  {held}d")
    logger.info("=" * 60)

    # NEVER persist state from a dry run. _process_exits() does `del positions[key]`
    # unconditionally — simulated exits mutate the dict exactly like real ones — so
    # saving here would permanently delete tracking for real open positions. They
    # would stay open at the broker with no stop management and no record.
    # (Same defect was found and fixed in futures/runner.py; see a34ffd0.)
    if dry_run:
        logger.info("  [DRY] state NOT saved — open positions left untouched")
    else:
        state["last_run"] = datetime.now().isoformat()
        _save_state(state)

    # ── Strategy learning pass — update weights from today's closed trades ────
    try:
        learn_result = strategy_learner.run_learning_pass("forex")
        if learn_result["new_trades"] > 0:
            logger.info(f"  [learner] Processed {learn_result['new_trades']} new trade(s) — "
                        f"weights updated")
    except Exception as exc:
        logger.warning(f"  [learner] Learning pass failed: {exc}")

    # ── Run-summary email (live only) ─────────────────────────────────────────
    if not dry_run:
        try:
            today_trades   = [t for t in trade_logger.tail("forex", n=200)
                              if t.get("date") == today_str and t.get("mode") == "LIVE"]
            strategy_stats = pnl_tracker.get_strategy_summary("forex")
            fx_notify.send_run_summary(
                session        = session,
                entries        = total_entries,
                exits          = total_exits,
                holdings       = len(positions),
                equity         = equity,
                today_trades   = today_trades,
                strategy_stats = strategy_stats,
                healed_stops   = healed_stops,
                healed_tp      = healed_tp,
            )
        except Exception as exc:
            logger.warning(f"Run-summary email failed: {exc}")

    return {"exits": total_exits, "entries": total_entries,
            "holding": len(positions), "dry_run": dry_run}


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="FX multi-strategy runner")
    ap.add_argument("--live",        action="store_true",
                    help="Place real orders on IBKR paper (default: dry-run)")
    ap.add_argument("--exits-only",  action="store_true",
                    help="Check stops only — no new entries (intraday stop check)")
    ap.add_argument("--strategy", default="all",
                    choices=["all", "ema", "rsi", "donchian", "bb", "pullback", "gap",
                             "supertrend", "zscore", "ml", "london_breakout"],
                    help="Which strategy to run (default: all)")
    ap.add_argument("--status",   action="store_true",
                    help="Print open positions and exit")
    ap.add_argument("--scan",     action="store_true",
                    help="Show 4-panel market snapshot")
    ap.add_argument("--info",     action="store_true",
                    help="Verify conIds via live IBKR quotes")
    ap.add_argument("--session",  default="all",
                    choices=["all", "asian", "london"],
                    help="Restrict to session pairs: asian (06:20 PKT) | london (18:00 PKT) | all")
    args = ap.parse_args()

    if args.info:
        print(f"\n{'Pair':<10} {'conId':>9}  {'Bid':>10} {'Ask':>10}  Description")
        print("  " + "-" * 58)
        for pair in PAIRS:
            uic = _ibkr_uic(pair["symbol"])
            if uic is None:
                print(f"  {pair['symbol']:<10} {'?':>9}  NOT RESOLVABLE ON IBKR")
                continue
            try:
                prices, _status = ibkr_price_service.fetch_prices([{"symbol": pair["symbol"], "uic": uic}])
                px = prices.get(pair["symbol"], "?")
                print(f"  {pair['symbol']:<10} {uic:>9}  "
                      f"{'':>10} {px:>10}  {pair['description']}")
            except Exception as exc:
                print(f"  {pair['symbol']:<10} {uic:>9}  ERROR: {exc}")
        sys.exit(0)

    if args.status:
        state     = _load_state()
        positions = state.get("positions", {})
        print(f"\nFX open positions ({len(positions)}):")
        if not positions:
            print("  None")
        for key, pos in positions.items():
            strat, sym = key.split(":", 1) if ":" in key else ("ema", key)
            held = (date.today() - date.fromisoformat(pos["entry_date"])).days
            tag  = "L" if pos.get("direction", "Buy") == "Buy" else "S"
            print(f"  [{strat}] {sym:<10}[{tag}]  qty={pos['quantity']:,}  "
                  f"entry={pos['entry_price']:.5f}  stop={pos['stop_price']:.5f}  {held}d")

        # Currency exposure summary
        exposure = _currency_exposure(positions)
        if exposure:
            print(f"\nCurrency exposure (limit: ±{MAX_CURRENCY_EXPOSURE}):")
            for ccy, net in sorted(exposure.items(), key=lambda x: abs(x[1]), reverse=True):
                if net == 0:
                    continue
                bar   = ("▲" * abs(net)) if net > 0 else ("▼" * abs(net))
                warn  = "  ← AT LIMIT" if abs(net) >= MAX_CURRENCY_EXPOSURE else ""
                print(f"  {ccy}  {net:+d}  {bar}{warn}")
        sys.exit(0)

    if args.scan:
        market_data = {}
        for pair in PAIRS:
            ibkr_uic = _ibkr_uic(pair["symbol"])
            market_data[pair["symbol"]] = _fetch_history(ibkr_uic) if ibkr_uic is not None else None
        scan_live_prices = _fetch_live_prices(PAIRS)

        # Panel 1 — EMA crossover
        print(f"\n[EMA] 5/30 crossover + ADX(14)")
        rows = strat_ema.scan_summary(market_data)
        print(f"  {'Pair':<10} {'Close':>10} {'FastEMA':>10} {'SlowEMA':>10} "
              f"{'Gap%':>7} {'ADX':>6} {'+DI':>6} {'-DI':>6}  Status")
        print("  " + "-" * 80)
        for r in rows:
            if r["status"] != "ok":
                print(f"  {r['symbol']:<10}  no data"); continue
            adx_flag = "TREND" if r["adx_ok"] else "range"
            print(f"  {r['symbol']:<10} {r['close']:>10.5f} {r['fast_ema']:>10.5f} "
                  f"{r['slow_ema']:>10.5f} {r['gap_pct']:>7.2f}% "
                  f"{r['adx']:>6.1f} {r['plus_di']:>6.1f} {r['minus_di']:>6.1f}"
                  f"  {r['trend']} / {adx_flag}")

        # Panel 2 — RSI(2) pullback
        print(f"\n[RSI] RSI(2) pullback within EMA(200) trend")
        rows = strat_rsi.scan_summary(market_data)
        print(f"  {'Pair':<10} {'Close':>10} {'RSI(2)':>8} {'EMA200':>12}  Trend  Signal")
        print("  " + "-" * 60)
        for r in rows:
            if r["status"] != "ok":
                print(f"  {r['symbol']:<10}  no data"); continue
            flag = f"  *** {r['flag']} ***" if r["flag"].strip() else ""
            print(f"  {r['symbol']:<10} {r['close']:>10.5f} {r['rsi2']:>8.1f} "
                  f"{r['ema200']:>12.5f}  {r['trend']}{flag}")

        # Panel 3 — Donchian
        print(f"\n[DONCHIAN] 20-day channel breakout")
        rows = strat_donchian.scan_summary(market_data)
        print(f"  {'Pair':<10} {'Close':>10} {'20d-Hi':>10} {'20d-Lo':>10}  Signal")
        print("  " + "-" * 60)
        for r in rows:
            if r["status"] != "ok":
                print(f"  {r['symbol']:<10}  no data"); continue
            print(f"  {r['symbol']:<10} {r['close']:>10.5f} {r['high20']:>10.5f} "
                  f"{r['low20']:>10.5f}  {r['signal']}")

        # Panel 4 — Bollinger Band reversion
        print(f"\n[BB] Bollinger Band(20,2) + RSI(14) mean reversion")
        rows = strat_bb.scan_summary(market_data)
        print(f"  {'Pair':<10} {'Close':>10} {'BB_Upper':>10} {'BB_Mid':>10} {'BB_Lower':>10} "
              f"{'BB%':>6} {'RSI14':>7}  Signal")
        print("  " + "-" * 80)
        for r in rows:
            if r["status"] != "ok":
                print(f"  {r['symbol']:<10}  no data"); continue
            flag = f"  *** {r['flag']} ***" if r.get("flag") else ""
            print(f"  {r['symbol']:<10} {r['close']:>10.5f} {r['bb_upper']:>10.5f} "
                  f"{r['bb_mid']:>10.5f} {r['bb_lower']:>10.5f} "
                  f"{r['bb_pct']:>6.1f}% {r['rsi14']:>7.1f}{flag}")

        # Panel 5 — Pullback to EMA(20)

        print(f"\n[PULLBACK] EMA(20) pullback in EMA(50) trend  (~70% WR)")
        rows = strat_pullback.scan_summary(market_data)
        print(f"  {'Pair':<10} {'Close':>10} {'EMA20':>10} {'EMA50':>10} "
              f"{'ADX':>6}  {'Trend':<6} {'ADX?':<6} {'PB?':<6}  Signal")
        print("  " + "-" * 80)
        for r in rows:
            if r["status"] != "ok":
                print(f"  {r['symbol']:<10}  no data"); continue
            adx_flag = "YES" if r["adx_ok"] else "no"
            pb_flag  = "YES" if r["pb_touch"] else "no"
            pos_flag = "above" if r["above_pb"] else "below"
            signal = ""
            if r["adx_ok"] and r["pb_touch"]:
                if r["trend"] == "BULL" and r["above_pb"]:
                    signal = "*** LONG SIGNAL ***"
                elif r["trend"] == "BEAR" and not r["above_pb"]:
                    signal = "*** SHORT SIGNAL ***"
            print(f"  {r['symbol']:<10} {r['close']:>10.5f} {r['ema20']:>10.5f} "
                  f"{r['ema50']:>10.5f} {r['adx']:>6.1f}  "
                  f"{r['trend']:<6} {adx_flag:<6} {pb_flag:<6}  {signal}")

        # Panel 6 — Weekend Gap Fill
        print(f"\n[GAP] Weekend Gap Fill  (~80-85% WR)  "
              f"{'— live prices available' if scan_live_prices else '— no live prices (run on Sunday)'}")
        rows = strat_gap.scan_summary(market_data, live_prices=scan_live_prices)
        print(f"  {'Pair':<10} {'Fri Close':>10} {'Sun Open':>10} {'Gap':>8} {'Gap%':>6}  Signal")
        print("  " + "-" * 70)
        for r in rows:
            if r["status"] != "ok":
                print(f"  {r['symbol']:<10}  no data"); continue
            gap_str = f"{r['gap']:+.5f}" if r['sunday_open'] > 0 else "n/a"
            pct_str = f"{r['gap_pct']:.3f}%" if r['sunday_open'] > 0 else "n/a"
            sun_str = f"{r['sunday_open']:.5f}" if r['sunday_open'] > 0 else "n/a"
            print(f"  {r['symbol']:<10} {r['friday_close']:>10.5f} {sun_str:>10} "
                  f"{gap_str:>8} {pct_str:>6}  {r['signal']}")

        # Panel 7 — SuperTrend
        print(f"\n[SUPERTREND] SuperTrend(10,3) + EMA(200)  (~65% WR)")
        rows = strat_supertrend.scan_summary(market_data)
        print(f"  {'Pair':<10} {'Close':>10} {'Direction':>10} {'ST Level':>10} {'EMA200':>10} {'ATR':>8}  Status")
        print("  " + "-" * 80)
        for r in rows:
            if r["status"] != "ok":
                print(f"  {r['symbol']:<10}  no data" if r["status"] == "no_data"
                      else f"  {r['symbol']:<10}  error"); continue
            dir_str = "BULL ↑" if r["direction"] == 1 else "BEAR ↓"
            ema_flag = ">" if r["close"] > r["ema200"] else "<"
            print(f"  {r['symbol']:<10} {r['close']:>10.5f} {dir_str:>10} "
                  f"{r['st_level']:>10.5f} {r['ema200']:>10.5f} {r['atr']:>8.5f}"
                  f"  price {ema_flag} EMA200")

        # Panel 8 — Z-Score Mean Reversion
        print(f"\n[ZSCORE] Z-Score(20) mean reversion + EMA(200)  (~63% WR)")
        rows = strat_zscore.scan_summary(market_data)
        print(f"  {'Pair':<10} {'Close':>10} {'Z-Score':>8} {'ATR':>10}  Signal")
        print("  " + "-" * 60)
        for r in rows:
            if r["status"] != "ok":
                print(f"  {r['symbol']:<10}  no data"); continue
            z = r["zscore"]
            flag = ""
            if z < -2.0:   flag = "  *** OVERSOLD → LONG ***"
            elif z > 2.0:  flag = "  *** OVERBOUGHT → SHORT ***"
            print(f"  {r['symbol']:<10} {r['close']:>10.5f} {z:>+8.2f} {r['atr']:>10.5f}{flag}")

        # Panel 9 — ML Signals
        print(f"\n[ML] Logistic Regression signals  (~57-62% WR)  [requires 336 bars]")
        rows = strat_ml.scan_summary(market_data)
        print(f"  {'Pair':<10} {'Close':>10} {'ML Prob':>8}  Signal")
        print("  " + "-" * 50)
        for r in rows:
            if r["status"] != "ok":
                print(f"  {r['symbol']:<10}  no data"); continue
            prob = r["ml_prob"]
            if prob is None:
                print(f"  {r['symbol']:<10} {r['close']:>10.5f}  {'—':>8}  insufficient bars"); continue
            flag = ""
            if prob >= 0.58:    flag = f"  *** BUY  (conf={prob:.2f}) ***"
            elif prob <= 0.42:  flag = f"  *** SELL (conf={1-prob:.2f}) ***"
            print(f"  {r['symbol']:<10} {r['close']:>10.5f} {prob:>8.3f}{flag}")

        sys.exit(0)

    active = list(STRATEGIES) if args.strategy == "all" else [args.strategy]
    if args.exits_only:
        run_exits_only(dry_run=not args.live, active_strategies=active,
                       session=args.session)
    else:
        run_daily(dry_run=not args.live, active_strategies=active,
                  session=args.session)
