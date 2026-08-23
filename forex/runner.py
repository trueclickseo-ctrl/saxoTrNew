"""
forex/runner.py
---------------
Multi-strategy daily execution runner for FX pairs.

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
  34 pairs — 7 G7 majors + 27 crosses (UICs confirmed Saxo SIM; Scandi/EM verify with --info)
  Asian session  (14): JPY crosses, AUD/NZD pairs — run at 06:20 PKT
  London session (20): EUR/GBP/USD crosses + Scandi/CAD — run at 18:00 PKT

Usage:
    python forex/runner.py                          # all 4 strategies, all 27 pairs, dry-run
    python forex/runner.py --live                   # all 4, real Saxo SIM orders
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
import time
import uuid
from datetime import datetime, date, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)
sys.path.insert(0, _HERE)

import requests
import saxo_order
import pandas as pd
import saxo_auth

from forex.universe import PAIRS, ASSET_TYPE, get_pair, price_decimals as get_price_decimals
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
    # ema/donchian/bb/supertrend/zscore/ml/cnn_lstm previously capped at 4-20
    # slots — a legacy holdover from a smaller pair universe with no risk
    # rationale documented anywhere (unlike london_breakout below). All of
    # them scan the same full universe as rsi/pullback/gap, so capped below
    # the universe size they'd needlessly miss signals on pairs beyond their
    # slot count. Raised to 34 (2026-08-20), then to 117 (2026-08-21) when the
    # universe was expanded to the full major+EM/exotic set for SIM testing —
    # so every swing strategy can take a position in every pair it signals on.
    "ema": 117, "rsi": 117, "donchian": 117, "bb": 117,
    "pullback": 117, "gap": 117,
    "supertrend": 117, "zscore": 117, "ml": 117, "cnn_lstm": 117,
    "london_breakout": 28,  # universe expanded to 28 pairs 2026-08-20. Slots raised
                             # 10 -> 28 (2026-08-21, one slot per pair) so a multi-pair
                             # breakout day is never capped below what the pair list can
                             # offer. Max concurrent exposure: 28 x 1.5% = 42% of the LBO
                             # book if every slot fills (was 15% at 10 slots) — a real
                             # risk increase, done at the user's explicit request.
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
# Disabled (set effectively unlimited) 2026-08-21 at user's explicit request —
# "do not limit it" — to let the SIM account fully test every strategy across
# the expanded 117-pair universe without this gate suppressing signals. The
# exposure dict/tracking (_currency_exposure, dashboard's "Currency exposure"
# panel) is untouched, so real exposure is still visible — just no longer
# blocking. Reconsider re-enabling a real limit before trading live capital.
MAX_CURRENCY_EXPOSURE = 999

# Reject a signal if the pair's live spread is wider than this % of price —
# a proxy for "this pair's home market is currently illiquid" without needing
# a per-currency trading-hours table (which several EM/exotic currencies don't
# cleanly fit anyway — e.g. TRY and CNH trade meaningfully outside their
# "home" session too). Checked live at signal time (see 2026-08-21 spread
# survey: majors run ~0.01-0.02%, most EM/exotic pairs ~0.02-0.09% under
# normal conditions) — 0.20% gives headroom above normal EM spreads while
# still catching a genuine liquidity-crunch widening. Starting value, not
# empirically tuned yet — watch SIM results and adjust.
MAX_SPREAD_PCT = 0.20

# ── Constants ─────────────────────────────────────────────────────────────────
BASE_URL    = "https://gateway.saxobank.com/sim/openapi"
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

# ── Default take-profit (2026-08-22) ──────────────────────────────────────────
# gap/london_breakout compute their own tp_price/gap_target from their own
# session-range logic. The other 9 strategies (ema/rsi/donchian/bb/pullback/
# ml/zscore/supertrend/cnn_lstm) only ever computed a stop_price — profit was
# taken exclusively by the scheduler's periodic should_exit()/trailing-stop
# scan, so a position sat with only downside protection at the broker between
# runs. Per explicit user direction: every position must get BOTH legs placed
# on Saxo atomically at entry, not dependent on the next scheduled run.
# DEFAULT_TP_RR gives those 9 strategies a broker-side take-profit at this
# multiple of their own already-computed stop distance (entry-to-stop) —
# matching london_breakout's established 2:1 reward:risk convention (see
# docs/forex_strategies.md's "TP ratio | 2.0 × range" for LBO). This does not
# change any strategy's own exit logic (trailing stop, time-stop, hard-stop
# are still evaluated every run exactly as before) -- it only adds a resting
# Limit order at the broker so a winning trade can be captured even if a
# scheduled run is delayed or skipped.
DEFAULT_TP_RR = 2.0


def _resolve_tp_price(sig: dict, direction: str) -> float:
    """Take-profit price for a signal: the strategy's own target if it
    computed one (gap's gap_target, london_breakout's tp_price), else
    DEFAULT_TP_RR applied to the signal's own stop distance."""
    tp = sig.get("tp_price") or sig.get("gap_target")
    if tp is not None:
        return tp
    stop_distance = abs(sig["close"] - sig["stop_price"])
    return (sig["close"] + DEFAULT_TP_RR * stop_distance if direction == "Buy"
            else sig["close"] - DEFAULT_TP_RR * stop_distance)


# ── Saxo HTTP helpers ─────────────────────────────────────────────────────────

def _hdrs(idempotent_id: str | None = None) -> dict:
    h = {"Authorization": f"Bearer {saxo_auth.get_valid_access_token()}"}
    if idempotent_id:
        # Saxo rejects an identical POST/PATCH to an order endpoint within a
        # 15s rolling window as a duplicate (409 Conflict) unless each attempt
        # carries its own x-request-id — without this, a legitimate fast
        # retry (or two different strategies closing near-identical orders
        # back-to-back) can get silently blocked as "duplicate."
        h["x-request-id"] = idempotent_id
    return h


def _sleep_for_rate_limit(resp) -> float:
    reset = resp.headers.get("X-RateLimit-Orders-Reset") or resp.headers.get("X-RateLimit-Reset")
    try:
        return max(1.0, float(reset))
    except (TypeError, ValueError):
        return 2.0


def _get(path: str, params: dict | None = None) -> dict:
    for attempt in range(1, 4):
        try:
            r = requests.get(f"{BASE_URL}{path}", headers=_hdrs(),
                             params=params, timeout=15)
            if r.status_code == 429 and attempt < 3:
                wait = _sleep_for_rate_limit(r)
                logger.warning(f"429 rate-limited on GET {path} — waiting {wait:.0f}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except (requests.exceptions.SSLError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as exc:
            if attempt < 3:
                time.sleep(5 * attempt)
                continue
            raise exc


def _post(path: str, body: dict) -> dict:
    req_id = str(uuid.uuid4())
    for attempt in range(1, 4):
        r = requests.post(f"{BASE_URL}{path}", headers=_hdrs(req_id), json=body, timeout=15)
        if r.status_code == 429 and attempt < 3:
            wait = _sleep_for_rate_limit(r)
            logger.warning(f"429 rate-limited on POST {path} — waiting {wait:.0f}s")
            time.sleep(wait)
            continue
        try:
            r.raise_for_status()
        except requests.exceptions.HTTPError as e:
            # raise_for_status()'s default message is just status+URL — it
            # drops Saxo's actual error body (ErrorCode/Message explaining
            # WHY, e.g. "too small," "market closed," "invalid precision").
            # Without this, a rejected order is undiagnosable after the fact.
            raise requests.exceptions.HTTPError(
                f"{e} — Saxo response body: {r.text}", response=r) from e
        return r.json()


def _patch(path: str, body: dict) -> dict:
    req_id = str(uuid.uuid4())
    for attempt in range(1, 4):
        r = requests.patch(f"{BASE_URL}{path}", headers=_hdrs(req_id), json=body, timeout=15)
        if r.status_code == 429 and attempt < 3:
            wait = _sleep_for_rate_limit(r)
            logger.warning(f"429 rate-limited on PATCH {path} — waiting {wait:.0f}s")
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()


def _delete(path: str, params: dict | None = None) -> None:
    r = requests.delete(f"{BASE_URL}{path}", headers=_hdrs(), params=params, timeout=15)
    r.raise_for_status()


# ── Token / Account ───────────────────────────────────────────────────────────

def _verify_token(scheduled_time: str = "") -> bool:
    """
    Test the Saxo token. Returns True if valid.
    On failure, sends a token-expired email and returns False so the caller
    can exit cleanly without placing any orders.
    """
    try:
        _get("/port/v1/accounts/me")
        return True
    except Exception:
        logger.error("Saxo token appears expired or invalid — sending alert email")
        fx_notify.send_token_expired(scheduled_time)
        return False


_QUOTE_RATE_CACHE: dict[str, float] = {}
_PAIRS_BY_SYMBOL = {p["symbol"]: p for p in PAIRS}


def _live_price_retry(uic: int, akey: str, attempts: int = 2) -> float | None:
    """_live_price with a couple of retries -- a single infoprices call
    fails often enough under load (confirmed live 2026-08-22: 35 of 94
    concurrent Saxo price requests failed in one forex_dashboard.py
    refresh, a different random subset each time) that one miss shouldn't
    be treated as "Saxo has nothing," now that there's no other source to
    fall back to for this."""
    for _ in range(attempts):
        px = _live_price(uic, akey)
        if px:
            return px
    return None


def _eur_per_unit(ccy: str, akey: str | None = None) -> float | None:
    """EUR value of one unit of `ccy`, from Saxo's OWN live quotes only.

    Per explicit user direction (2026-08-22): live SIM orders and the
    dashboard must always use Saxo prices -- Yahoo is for historical/
    backtest data only, never for anything that sizes or converts a live
    trade. Triangulates via a pair already in our own universe: EUR{ccy}
    directly if we trade it (every currency here has one except AED/DKK),
    else USD{ccy} + EURUSD. Returns None if Saxo has no live quote right
    now -- callers must treat that as "unknown" (skip sizing/skip this
    pair's contribution), not silently substitute a non-Saxo number.
    """
    if ccy == "EUR":
        return 1.0
    if ccy in _QUOTE_RATE_CACHE:
        return _QUOTE_RATE_CACHE[ccy]

    rate = None
    akey = akey or ""
    direct = _PAIRS_BY_SYMBOL.get(f"EUR{ccy}")
    if direct is not None:
        px = _live_price_retry(direct["uic"], akey)
        if px and px > 0:
            rate = 1.0 / px
    else:
        usd_leg = _PAIRS_BY_SYMBOL.get(f"USD{ccy}")
        eur_usd = _PAIRS_BY_SYMBOL.get("EURUSD")
        if usd_leg is not None and eur_usd is not None:
            px_usd_ccy = _live_price_retry(usd_leg["uic"], akey)
            px_eur_usd = _live_price_retry(eur_usd["uic"], akey)
            if px_usd_ccy and px_usd_ccy > 0 and px_eur_usd and px_eur_usd > 0:
                rate = 1.0 / (px_usd_ccy * px_eur_usd)

    if rate is None:
        logger.warning(f"Saxo has no live quote for {ccy} right now -- treating as unknown")
        return None
    _QUOTE_RATE_CACHE[ccy] = rate
    return rate


def _equity_in_quote(equity_eur: float, symbol: str) -> float | None:
    """Restate EUR equity in a pair's quote currency, for position sizing.

    ATR (and therefore stop distance) is quoted in the pair's quote currency.
    Dividing an EUR risk budget by a JPY distance is a unit error, so the
    budget is converted first.
    """
    quote = symbol[3:6] if len(symbol) >= 6 else ""
    if not quote:
        return None
    rate = _eur_per_unit(quote)
    if not rate or rate <= 0:
        return None
    return equity_eur / rate


def _risk_equity(raw_equity: float) -> float:
    """Cap the sizing base at configured real capital.

    The broker figure is SIM demo credit (~945,000 EUR), not the user's money.
    Sizing off it made positions ~33x the intended 300,000 SEK. FX trades in
    fine unit increments, so this scales positions down cleanly rather than
    making pairs untradeable (contrast futures, where lumpy contract sizes mean
    the cap blocks whole markets).
    """
    try:
        import atos.capital_config as _CAP
        cap = _CAP.forex_risk_equity_eur()
    except Exception as exc:
        logger.warning(f"Could not read forex risk equity cap: {exc}")
        return raw_equity
    if cap <= 0:
        return raw_equity
    return min(raw_equity, cap) if raw_equity > 0 else cap


def _account() -> tuple[float, str]:
    equity, key = 0.0, ""
    try:
        bal    = _get("/port/v1/balances/me")
        equity = float(bal.get("TotalValue") or bal.get("NetEquityForMargin")
                       or bal.get("CashBalance") or 0)
        raw    = equity
        equity = _risk_equity(equity)
        if equity < raw:
            logger.info(f"  Equity {raw:,.0f} EUR (broker) -> sizing off "
                        f"{equity:,.0f} EUR (capped at configured capital)")
    except Exception as exc:
        logger.warning(f"Could not read equity: {exc}")
    try:
        info = _get("/port/v1/accounts/me")
        data = info.get("Data", info)
        acct = data[0] if isinstance(data, list) else data
        key  = (acct.get("AccountKey", "") if isinstance(acct, dict) else "") or ""
    except Exception as exc:
        logger.warning(f"Could not read AccountKey: {exc}")
    return equity, key


# ── Price data ────────────────────────────────────────────────────────────────

def _fetch_history(uic: int, count: int = CHART_BARS) -> pd.DataFrame | None:
    """Fetch daily OHLC for an FxSpot instrument. Mid = (Ask+Bid)/2.

    Each strategy enforces its own MIN_BARS; we just need at least a few rows
    here to confirm the instrument responded with real data.
    """
    try:
        resp = _get("/chart/v3/charts", {
            "Uic": uic, "AssetType": ASSET_TYPE,
            "Horizon": 1440, "Count": count + 5,
        })
        rows = []
        for bar in resp.get("Data", []):
            if not isinstance(bar, dict):
                continue
            if "CloseAsk" in bar and "CloseBid" in bar:
                ask_c = float(bar["CloseAsk"]); bid_c = float(bar["CloseBid"])
                o = (float(bar.get("OpenAsk",  ask_c)) + float(bar.get("OpenBid",  bid_c))) / 2
                h = (float(bar.get("HighAsk",  ask_c)) + float(bar.get("HighBid",  bid_c))) / 2
                l = (float(bar.get("LowAsk",   ask_c)) + float(bar.get("LowBid",   bid_c))) / 2
                c = (ask_c + bid_c) / 2
            elif "Close" in bar:
                o = float(bar.get("Open",  bar["Close"]))
                h = float(bar.get("High",  bar["Close"]))
                l = float(bar.get("Low",   bar["Close"]))
                c = float(bar["Close"])
            else:
                continue
            if c > 0:
                rows.append({"Open": o, "High": h, "Low": l, "Close": c})
        if len(rows) >= 5:
            return pd.DataFrame(rows)
        logger.debug(f"UIC {uic}: only {len(rows)} bars returned")
        return None
    except Exception as exc:
        logger.warning(f"Chart fetch failed for UIC {uic}: {exc}")
        return None


def _fetch_history_h1(uic: int, count: int = 48) -> pd.DataFrame | None:
    """Fetch H1 OHLC bars (Horizon=60 minutes) with UTC hour label.

    Returns DataFrame with columns Open/High/Low/Close/HourUTC.
    Used by the session gap strategy to find the reference bar before each session.
    """
    try:
        resp = _get("/chart/v3/charts", {
            "Uic": uic, "AssetType": ASSET_TYPE,
            "Horizon": 60, "Count": count + 2,
        })
        rows = []
        for bar in resp.get("Data", []):
            if not isinstance(bar, dict):
                continue
            # Extract timestamp for HourUTC label
            ts_raw = bar.get("Time") or bar.get("OpenTime") or ""
            hour_utc = -1
            if ts_raw:
                try:
                    dt = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                    hour_utc = dt.astimezone(timezone.utc).hour
                except Exception:
                    pass

            if "CloseAsk" in bar and "CloseBid" in bar:
                ask_c = float(bar["CloseAsk"]); bid_c = float(bar["CloseBid"])
                o = (float(bar.get("OpenAsk",  ask_c)) + float(bar.get("OpenBid",  bid_c))) / 2
                h = (float(bar.get("HighAsk",  ask_c)) + float(bar.get("HighBid",  bid_c))) / 2
                l = (float(bar.get("LowAsk",   ask_c)) + float(bar.get("LowBid",   bid_c))) / 2
                c = (ask_c + bid_c) / 2
            elif "Close" in bar:
                o = float(bar.get("Open",  bar["Close"]))
                h = float(bar.get("High",  bar["Close"]))
                l = float(bar.get("Low",   bar["Close"]))
                c = float(bar["Close"])
            else:
                continue
            if c > 0:
                rows.append({"Open": o, "High": h, "Low": l, "Close": c, "HourUTC": hour_utc})
        if len(rows) >= 4:
            return pd.DataFrame(rows)
        return None
    except Exception as exc:
        logger.warning(f"H1 chart fetch failed for UIC {uic}: {exc}")
        return None


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


def _spread_pct(uic: int) -> float | None:
    """Live bid/ask spread as % of mid price — proxy for current liquidity.

    Checked at signal time, not tied to any fixed trading-hours table, so it
    reacts to the pair's actual current market condition (a EUR pair during a
    holiday-thin session gets caught the same as an EM pair outside its home
    hours) instead of a static per-currency assumption. Returns None if the
    quote can't be fetched — callers should treat that as "can't verify,
    proceed" rather than blocking the trade on a data hiccup.
    """
    try:
        resp = _get("/trade/v1/infoprices", {"Uic": uic, "AssetType": ASSET_TYPE, "FieldGroups": "Quote"})
        q   = resp.get("Quote", {})
        bid, ask = float(q.get("Bid", 0)), float(q.get("Ask", 0))
        if bid <= 0 or ask <= 0:
            return None
        mid = (bid + ask) / 2
        return (ask - bid) / mid * 100
    except Exception as exc:
        logger.debug(f"Spread check failed for UIC {uic}: {exc}")
        return None


def _fetch_live_prices(pairs: list) -> dict:
    """Fetch current bid/ask mid prices for a list of pairs (used by gap strategy)."""
    prices = {}
    for pair in pairs:
        try:
            resp = _get("/trade/v1/infoprices", {
                "Uic": pair["uic"], "AssetType": ASSET_TYPE, "FieldGroups": "Quote"
            })
            q   = resp.get("Quote", {})
            bid = float(q.get("Bid", 0))
            ask = float(q.get("Ask", 0))
            if bid > 0 and ask > 0:
                prices[pair["symbol"]] = (bid + ask) / 2
        except Exception as exc:
            logger.debug(f"Live price fetch failed for {pair['symbol']}: {exc}")
    return prices


def _position_pnl_base_ccy(uic: int, qty: float, direction: str,
                           entry_price: float) -> float | None:
    """Authoritative NET realized P&L in the account's own base currency
    (EUR), straight from Saxo's own live conversion — not our own rate
    estimate, and not just the raw price move.

    Saxo's /port/v1/positions/me returns PositionView.ProfitLossOnTrade in
    the pair's quote currency AND ProfitLossOnTradeInBaseCurrency, already
    converted using Saxo's own real-time dealt rate (ConversionRateCurrent).
    That's what actually happens to the account balance, so it's the right
    source of truth for the P&L ledger — not a manually-applied external rate.

    IMPORTANT: ProfitLossOnTradeInBaseCurrency is PURE PRICE MOVEMENT (entry
    vs current price x quantity) — it does NOT net out spread cost at entry
    or accrued overnight swap/financing. Those live in a SEPARATE field,
    TradeCostsTotalInBaseCurrency (confirmed live 2026-08-21: a position
    showing +4,362.06 EUR gross also carried -65.26 EUR of TradeCostsTotal,
    not yet subtracted — a position can show green gross and be meaningfully
    less green, or even red, net of costs, especially after several nights
    held). Both are added here so the stored realized_pnl is the true net
    figure, not the gross one.

    Multiple strategies can hold positions on the same UIC simultaneously, so
    match on quantity + entry price (not just UIC) to find the right row.
    """
    try:
        resp = _get("/port/v1/positions/me")
    except Exception as exc:
        logger.warning(f"Position P&L lookup failed for UIC {uic}: {exc}")
        return None
    want_amount = qty if direction in ("Buy", "BUY") else -qty
    best, best_diff = None, None
    for p in resp.get("Data", []):
        pb = p.get("PositionBase", {})
        if pb.get("Uic") != uic or pb.get("AssetType") != "FxSpot":
            continue
        amount = pb.get("Amount", 0)
        if abs(amount) != abs(want_amount):
            continue
        if (amount > 0) != (want_amount > 0):
            continue
        diff = abs((pb.get("OpenPrice") or 0) - entry_price)
        if best_diff is None or diff < best_diff:
            best, best_diff = p, diff
    if best is None:
        return None
    pv  = best.get("PositionView", {})
    pnl = pv.get("ProfitLossOnTradeInBaseCurrency")
    if pnl is None:
        return None
    costs = pv.get("TradeCostsTotalInBaseCurrency") or 0.0
    return float(pnl) + float(costs)   # costs is already negative-signed


def _live_price(uic: int, account_key: str) -> float | None:
    try:
        params = {"Uic": uic, "AssetType": ASSET_TYPE, "FieldGroups": "Quote"}
        if account_key:
            params["AccountKey"] = account_key
        resp = _get("/trade/v1/infoprices", params)
        q    = resp.get("Quote", {})
        mid  = q.get("Mid")
        if mid is None and q.get("Ask") and q.get("Bid"):
            mid = (float(q["Ask"]) + float(q["Bid"])) / 2
        return float(mid) if mid else None
    except Exception as exc:
        logger.warning(f"Live price failed for UIC {uic}: {exc}")
        return None


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


def _opposing_strategy_holds(sym: str, direction: str, positions: dict) -> str | None:
    """Return the strategy name already holding the OPPOSITE direction on
    this exact symbol, or None if there's no conflict.

    Each strategy's own generate_signals() call only ever sees ITS OWN open
    positions (open_symbols is built with a per-strategy key prefix, see
    the `prefix = f"{strat_name}:"` line above _run_entries) -- it has zero
    visibility into what any of the other 9 strategies are doing on the
    same pair. Found live 2026-08-22 (user spotted it on the dashboard):
    NZDUSD held Long via donchian+pullback AND Short via bb+ml
    simultaneously; same pattern on USDTHB and USDCZK. That combination has
    no upside, ever -- it pays spread/commission on both legs while the net
    directional exposure is smaller than either position alone, for zero
    diversification benefit (unlike spreading risk across DIFFERENT pairs).
    Same-DIRECTION stacking across strategies is deliberately left alone --
    multiple strategies independently agreeing isn't inherently wrong, and
    capping it would be a real risk-budget design decision, not a bug fix.
    """
    for key, pos in positions.items():
        if ":" not in key:
            continue
        other_strat, other_sym = key.split(":", 1)
        if other_sym != sym:
            continue
        if pos.get("direction", "Buy") != direction:
            return other_strat
    return None


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
    """Sum of |entry-stop| × qty across all open positions, converted to EUR,
    as a fraction of EUR equity.

    Two independent bugs used to inflate this wildly:

    1. No currency conversion: |entry-stop|×qty is in the pair's QUOTE
       currency, but was summed directly against EUR equity. A JPY-quoted
       pair (prices ~100-200) or NOK/SEK pair produces a numeric risk value
       ~100-180x larger than the equivalent EUR amount, so a single JPY
       position could read as 180%+ "heat" on its own even when correctly
       risking ~1% of equity. Fixed by converting each position's risk into
       EUR via _eur_per_unit() before summing — mirrors the same fix already
       applied to position sizing via _equity_in_quote().
    2. Positions opened before the capital-cap fix (5bf8a5f) were sized off
       the ~945k-985k EUR SIM broker balance instead of the ~27,800 EUR
       configured capital, so their EUR risk is ~35x too large to compare
       against the new equity base. Only count positions carrying
       "sized_under_cap" (i.e. opened under the fixed sizing logic) so the
       gate reflects real risk from here forward instead of stale pre-fix
       noise.
    """
    if equity <= 0:
        return 0.0
    heat = 0.0
    for key, pos in positions.items():
        if not pos.get("sized_under_cap"):
            continue
        sym   = key.split(":", 1)[1] if ":" in key else key
        quote = sym[3:6] if len(sym) >= 6 else "EUR"
        rate  = _eur_per_unit(quote)
        if not rate or rate <= 0:
            logger.debug(f"  [HEAT] No FX rate for {quote} — skipping {key} in heat calc")
            continue
        risk_quote = abs(float(pos.get("entry_price", 0)) - float(pos.get("stop_price", 0))) \
                     * float(pos.get("quantity", 0))
        heat += risk_quote * rate
    return heat / equity


def _heat_allows_entry(positions: dict, equity: float) -> bool:
    """Disabled 2026-08-21 at user's explicit request — "do not block new
    entries, I want to test fully all strategies" — while the SIM account is
    scanning the expanded 117-pair universe. Heat is still computed and
    logged every run (telemetry, and still shown via `--status`) so real risk
    is visible; it just no longer gates entries. PORTFOLIO_HEAT_LIMIT and this
    gate should be reinstated before trading live capital."""
    heat = _portfolio_heat_pct(positions, equity)
    if heat >= PORTFOLIO_HEAT_LIMIT:
        logger.info(f"  [HEAT] Portfolio heat {heat:.1%} >= {PORTFOLIO_HEAT_LIMIT:.0%} "
                    f"(limit disabled for SIM testing — NOT blocking)")
    return True


def _update_peak_equity(equity: float) -> None:
    peak = 0.0
    if os.path.exists(PEAK_EQUITY_FILE):
        try:
            with open(PEAK_EQUITY_FILE) as f:
                peak = float(json.load(f).get("peak", 0))
        except Exception:
            pass
    # A >80% gap between current equity and the recorded peak is implausible
    # for a risk-controlled swing book (stops are 1.5-2x ATR) and almost
    # certainly means the peak predates a rescale of the sizing equity base
    # (e.g. the capital cap added in 5bf8a5f, which sizes off ~27,800 EUR
    # instead of the ~945,000 EUR broker SIM balance). Reseed rather than
    # let a stale, wrong-scale peak permanently trip the drawdown breaker.
    if peak > 0 and equity > 0 and equity < peak * 0.2:
        logger.warning(f"  [DRAWDOWN] peak {peak:,.0f} looks stale vs current "
                        f"equity {equity:,.0f} — reseeding peak")
        peak = 0.0
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

def _amend_stop_order(order_id: str, new_price: float, sym: str, akey: str, uic: int) -> bool:
    """Amend an existing Saxo GTC stop order to a new price via PATCH.

    Saxo's PATCH /trade/v2/orders/{OrderId} 404s if the body only carries the
    changed fields — it also needs OrderId/Uic/AssetType repeated in the body
    to resolve which order+instrument context to modify (confirmed: every
    prior amend attempt 404'd with just AccountKey+OrderPrice+OrderDuration,
    even against order ids verified live via GET /port/v1/orders/me).
    """
    dp = get_price_decimals(sym)
    rounded = round(new_price, dp)
    try:
        _patch(f"/trade/v2/orders/{order_id}", {
            "AccountKey":    akey,
            "OrderId":       str(order_id),
            "Uic":           uic,
            "AssetType":     ASSET_TYPE,
            "OrderPrice":    rounded,
            "OrderDuration": {"DurationType": "GoodTillCancel"},
        })
        logger.info(f"  [BREAKEVEN] Stop order {order_id} amended → {rounded:.{dp}f}  ✓")
        return True
    except Exception as exc:
        logger.warning(f"  [BREAKEVEN] Could not amend stop {order_id}: {exc} "
                       f"— will try cancel+replace")
        return False


def _cancel_order(order_id: str, akey: str) -> bool:
    """Cancel a live Saxo order. Returns True on success (incl. if it's
    already gone — a 404 here means there's nothing left to double-protect
    against, so treat it as cancelled rather than aborting the replace)."""
    try:
        _delete(f"/trade/v2/orders/{order_id}", {"AccountKey": akey})
        return True
    except requests.exceptions.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return True
        logger.warning(f"  Could not cancel order {order_id}: {exc}")
        return False
    except Exception as exc:
        logger.warning(f"  Could not cancel order {order_id}: {exc}")
        return False


def _replace_stop_order(pos: dict, sym: str, akey: str, new_price: float) -> str | None:
    """Cancel the position's current stop and place a fresh one at new_price.

    Fallback for when PATCH-amend 404s (Saxo SIM doesn't support in-place
    amend for these stop orders even with full Uic/AssetType/OrderId in the
    body — confirmed against multiple live order ids). Returns the new order
    id, or None if either step failed — caller must treat None as "possibly
    unprotected, do not mark breakeven_triggered".
    """
    old_oid = pos.get("stop_order_id")
    if old_oid and old_oid not in ("synced", None, ""):
        if not _cancel_order(old_oid, akey):
            return None   # couldn't confirm the old stop is gone — don't risk a duplicate

    direction  = pos.get("direction", "Buy")
    close_side = "Sell" if direction == "Buy" else "Buy"
    dp         = get_price_decimals(sym)
    rounded    = round(new_price, dp)
    try:
        resp    = _post("/trade/v2/orders", {
            "AccountKey":    akey,
            "Uic":           pos["uic"],
            "AssetType":     ASSET_TYPE,
            "Amount":        pos["quantity"],
            "BuySell":       close_side,
            "OrderType":     "Stop",
            "OrderPrice":    rounded,
            "OrderDuration": {"DurationType": "GoodTillCancel"},
            "ManualOrder":   False,
        })
        new_oid = str(resp.get("OrderId", "?"))
        logger.info(f"  [BREAKEVEN] Replaced stop {old_oid} → {new_oid} @ {rounded:.{dp}f}  ✓")
        return new_oid
    except Exception as exc:
        logger.warning(f"  [BREAKEVEN] Cancelled stop {old_oid} but re-place FAILED @ "
                        f"{rounded:.{dp}f}: {exc} — POSITION MAY BE UNPROTECTED, check manually")
        return None


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

    pos["stop_price"] = entry_price

    if dry_run:
        pos["breakeven_triggered"] = True
        return True

    stop_oid = pos.get("stop_order_id")
    if stop_oid and stop_oid not in ("synced", None, "") and \
       _amend_stop_order(stop_oid, entry_price, sym, akey, pos["uic"]):
        pos["breakeven_triggered"] = True
        return True

    # PATCH-amend failed — fall back to cancel + re-place at the new price
    # (Saxo SIM 404s in-place amends for some stop orders regardless of body).
    new_oid = _replace_stop_order(pos, sym, akey, entry_price)
    if new_oid:
        pos["stop_order_id"]       = new_oid
        pos["breakeven_triggered"] = True
        return True

    # Both amend and cancel+replace failed — drop the stale id so
    # _heal_missing_stops places a fresh GTC stop at the updated stop_price
    # next run, instead of the position silently believing the broker-side
    # stop moved when it never did. Don't set breakeven_triggered:
    # should_trigger recomputes false now that stop_price == entry_price,
    # so this is a quiet no-op until healed.
    pos["stop_order_id"] = None
    return False


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

        pair_info  = get_pair(sym)
        uic        = pair_info["uic"]
        qty        = pos.get("quantity", 1_000)
        direction  = pos.get("direction", "Buy")
        is_long    = direction == "Buy"
        close_side = "Sell" if is_long else "Buy"
        live_px    = _live_price(uic, akey) or float(pos.get("entry_price", 0))
        entry      = float(pos.get("entry_price", 0))
        pnl_pct    = ((live_px - entry) / entry * 100) if is_long else ((entry - live_px) / entry * 100)

        # Snapshot Saxo's own base-currency P&L for this position right before
        # closing it — this is the broker's real dealt conversion (what
        # actually happens to the account balance), captured while the
        # position still exists to look up. Falls back to our own rate
        # estimate below if this lookup fails (network hiccup, no match).
        saxo_pnl_eur = None if dry_run else _position_pnl_base_ccy(uic, qty, direction, entry)

        order = {"AccountKey": akey, "Uic": uic, "AssetType": ASSET_TYPE,
                 "Amount": qty, "BuySell": close_side, "OrderType": "Market",
                 "OrderDuration": {"DurationType": "DayOrder"}, "ManualOrder": False}

        tag = "L" if is_long else "S"
        if dry_run:
            logger.info(f"  [DRY] {close_side:<4} {qty:,}x {sym}[{tag}] "
                        f"({strat_name}) — {reason}  P&L {pnl_pct:+.2f}%")
        else:
            # This close is happening because OUR should_exit() logic fired
            # (hard-stop/time-stop/trailing), not because the broker's own
            # resting stop/TP order triggered it. Those two resting orders
            # are therefore still live and now protecting a position that's
            # about to go flat -- cancel them FIRST, before sending the
            # market close, or either one could fire in the gap and open an
            # unintended new position in the opposite direction (confirmed
            # live 2026-08-22: this exact gap is how a bracket's un-linked
            # fallback legs end up orphaned -- see saxo_order.place_with_stop).
            for oid_key in ("stop_order_id", "tp_order_id"):
                oid = pos.get(oid_key)
                if oid and oid not in ("synced", None, ""):
                    _cancel_order(oid, akey)
            resp = _post("/trade/v2/orders", order)
            logger.info(f"  {close_side} {resp.get('OrderId','?')}: {qty:,}x {sym}[{tag}] "
                        f"({strat_name}) — {reason}  P&L {pnl_pct:+.2f}%")

        _log_order({"side": close_side, "symbol": sym, "strategy": strat_name,
                    "uic": uic, "quantity": qty, "exit_price": live_px,
                    "reason": reason, "pnl_pct": round(pnl_pct, 3), "dry_run": dry_run})
        if not dry_run:
            # Convert this pair's raw quote-currency P&L into the account's
            # actual base currency (EUR) before it's stored — otherwise a JPY
            # pair's raw number (e.g. -7,612) gets summed into the ledger
            # alongside a USD/CHF pair's raw number as if they were the same
            # currency, when the JPY figure is really only ~-50 EUR.
            quote_ccy = sym[3:6] if len(sym) >= 6 else ""
            fx_rate   = _eur_per_unit(quote_ccy, akey)
            if fx_rate is None:
                # Only used if saxo_pnl_eur (Saxo's own authoritative
                # positions/me conversion, the primary source below) is
                # ALSO unavailable -- a double failure. 1.0 is a known-bad
                # placeholder, not a real rate; logged loudly rather than
                # silently trusted, since there's no Yahoo fallback to
                # reach for anymore (Saxo-only per 2026-08-22 direction).
                logger.warning(f"  [PNL] No Saxo rate for {quote_ccy} AND no Saxo "
                                f"position P&L for {sym} -- realized P&L for this "
                                f"close will use an unconverted 1.0 placeholder, "
                                f"verify data/pnl_ledger.db for {sym} manually")
                fx_rate = 1.0
            pnl_tracker.log_close("forex", sym, live_px, reason, strategy=strat_name,
                                  fx_rate_to_base=fx_rate,
                                  gross_pnl_base_override=saxo_pnl_eur)
            if strat_name == "gap":
                _mark_gap_exhausted(sym)
            # Label the signal-log outcome for ML training data
            raw_pnl = ((live_px - pos["entry_price"]) * qty if is_long
                       else (pos["entry_price"] - live_px) * qty)
            signal_filter.label_outcome(key, won=raw_pnl > 0)
            fx_notify.send_trade_closed(
                strategy=strat_name, symbol=sym, direction=direction,
                entry=float(pos.get("entry_price", live_px)),
                exit_px=live_px, pnl_pct=pnl_pct, units=qty,
                reason=reason,
                session=pos.get("lbo_session", "") if strat_name == "london_breakout" else "",
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
        # London/NY Breakout: fetch H1 bars for the 28 configured pairs only.
        lbo_pairs  = strat_lbo.PAIRS
        h1_lbo: dict = {}
        pair_meta: dict = {}
        for pi in PAIRS:
            sym = pi["symbol"]
            if sym not in lbo_pairs:
                continue
            h1_lbo[sym]  = _fetch_history_h1(pi["uic"])
            pair_meta[sym] = {"pip_size": pi.get("pip_size", 0.0001)}
        # LBO is a separate day-trading book with its own capital, not a slice of
        # the swing account. Passing `equity` here made it risk 1.5% of everything.
        try:
            import atos.capital_config as _CAP
            lbo_equity = _CAP.forex_lbo_capital_eur()
        except Exception:
            lbo_equity = 1_390.0
        # stop_distance is in each pair's quote currency, so convert per pair.
        lbo_eq_by_pair = {}
        for sym in h1_lbo:
            eq_q = _equity_in_quote(lbo_equity, sym)
            if eq_q is not None:
                lbo_eq_by_pair[sym] = eq_q
        logger.info(f"  [london_breakout] book capital {lbo_equity:,.0f} EUR "
                    f"(1.5% = {lbo_equity*strat_lbo.RISK_PCT:,.0f} EUR/trade)")
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
            h1_data[pi["symbol"]] = _fetch_history_h1(pi["uic"])
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
        opposing = _opposing_strategy_holds(sym, direction, positions)
        if opposing is not None:
            logger.info(f"  [{strat_name}] SKIP {sym}[{direction}] "
                        f"— {opposing} already holds the opposite direction on {sym}, "
                        f"no upside to taking both sides")
            continue
        pair_info = get_pair(sym)
        uic       = pair_info["uic"]
        spread    = _spread_pct(uic)
        if spread is not None and spread > MAX_SPREAD_PCT:
            logger.info(f"  [{strat_name}] SKIP {sym}[{direction}] "
                        f"— spread {spread:.3f}% wider than {MAX_SPREAD_PCT}% "
                        f"(illiquid right now, not a good time to trade this pair)")
            continue
        if strat_name not in DAY_TRADE_STRATEGIES and not _heat_allows_entry(positions, equity):
            break   # heat cap reached — stop all entries for this strategy
        rp_kw     = {"risk_pct": sig["risk_pct_override"]} if "risk_pct_override" in sig else {}
        if "units" in sig:
            qty = sig["units"]   # london_breakout pre-computes sizing from SEK capital
        else:
            # size_position() computes units = (equity * RISK_PCT) / (mult * ATR).
            # ATR is in the pair's QUOTE currency but equity is in EUR, so without
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
        # london_breakout/gap provide their own session-range-based target;
        # every other strategy gets a broker-side TP at DEFAULT_TP_RR times
        # its own stop distance, so it's protected on both sides at the
        # broker from the moment it's opened (see DEFAULT_TP_RR docstring).
        tp = _resolve_tp_price(sig, direction)
        if dry_run:
            logger.info(f"  [DRY] {direction:<4} {qty:,}x {sym}[{tag}] "
                        f"({strat_name})  @ {sig['close']:.5f}  "
                        f"stop={sig['stop_price']:.5f}  tp={tp:.5f}  {detail}{agree_tag}")
        else:
            entry_oid, stop_oid, tp_oid = saxo_order.place_with_stop(
                post_fn           = _post,
                account_key       = akey,
                uic               = uic,
                asset_type        = ASSET_TYPE,
                amount            = qty,
                buy_sell          = direction,
                stop_price        = sig["stop_price"],
                label             = f"{strat_name}:{sym}",
                take_profit_price = tp,
                symbol            = sym,
                price_decimals    = get_price_decimals(sym),
            )
            if entry_oid is None:
                # Order rejected by Saxo — nothing was opened. Must not fall
                # through to the pos_record block below (that would record a
                # phantom position that doesn't exist at the broker). Skip
                # this signal and keep going — one rejection must not stop
                # the rest of this strategy's signals or any strategy queued
                # after it (see saxo_order._place_entry_then_stop docstring).
                logger.warning(f"  [{strat_name}] SKIP {sym}[{direction}] "
                                f"— entry order rejected, no position opened")
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
            "tp_price":       tp,   # broker-side target for every strategy now (own or DEFAULT_TP_RR fallback)
            "quantity":       qty,
            "entry_date":     today_str,
            "entry_datetime": datetime.now().isoformat(),  # hour-based time stop for session gaps
            "atr_at_entry":   sig["atr"],
            "stop_order_id":  stop_oid,
            "tp_order_id":    tp_oid if not dry_run else None,
            "sized_under_cap": True,
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
                                 sig["close"], sig["stop_price"], order_id=oid,
                                 currency="EUR")
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
    Return set of (uic, buy_sell, order_type) for all open GTC orders.
    Returns None if the query fails (heal will be skipped to avoid duplicates).
    """
    try:
        resp   = _get("/port/v1/orders/me", {"AssetType": ASSET_TYPE})
        orders = resp.get("Data", [])
        result = set()
        for o in orders:
            dur = o.get("Duration", {}).get("DurationType", "")
            if dur == "GoodTillCancel":
                # Saxo's live order objects carry the type under
                # "OpenOrderType", not "OrderType" -- confirmed live 2026-08-22
                # (dumped a real order response). Reading "OrderType" here
                # returned None for every order, so this dedup check never
                # matched and _heal_missing_stops/_heal_missing_tp couldn't
                # tell an existing live order from a missing one.
                result.add((o.get("Uic"), o.get("BuySell"), o.get("OpenOrderType")))
        return result
    except Exception as exc:
        logger.warning(f"[heal] Could not fetch open orders from Saxo: {exc} — skipping heal")
        return None


def _heal_missing_stops(positions: dict, akey: str) -> int:
    """
    Place GTC stop orders for positions missing a stop_order_id.
    Queries Saxo first to avoid creating duplicate stops.
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
        sym        = key.split(":", 1)[1] if ":" in key else key
        uic        = pos["uic"]
        direction  = pos.get("direction", "Buy")
        qty        = pos["quantity"]
        stop_price = pos["stop_price"]
        close_side = "Sell" if direction == "Buy" else "Buy"
        # Same rounding rule as saxo_order.place_with_stop's price_decimals —
        # this healing path had its own independent JPY-only/5dp guess,
        # which is exactly the bug that let AUDTRY/CNH-pair stops fail with
        # PriceNotInTickSizeIncrements in the first place (2026-08-21).
        rounded    = round(stop_price, get_price_decimals(sym))

        if (uic, close_side, "Stop") in open_orders or \
           (uic, close_side, "StopLimit") in open_orders:
            # Stop already exists in Saxo — just record it
            pos["stop_order_id"] = "synced"
            logger.debug(f"  [heal_stops] {key}  stop already in Saxo (synced)")
            continue

        logger.info(f"[heal_stops] {key} missing stop — placing {close_side} Stop@{rounded}...")
        stop_body = {
            "AccountKey":    akey,
            "Uic":           uic,
            "AssetType":     ASSET_TYPE,
            "Amount":        qty,
            "BuySell":       close_side,
            "OrderType":     "Stop",
            "OrderPrice":    rounded,
            "OrderDuration": {"DurationType": "GoodTillCancel"},
            "ManualOrder":   False,
        }
        try:
            resp     = _post("/trade/v2/orders", stop_body)
            stop_oid = str(resp.get("OrderId", "?"))
            pos["stop_order_id"] = stop_oid
            logger.info(f"  [heal_stops] {key}  stop_id={stop_oid}  ✓")
            healed += 1
        except Exception as exc:
            logger.warning(f"  [heal_stops] FAILED for {key}: {exc}")
    return healed


def _heal_missing_tp(positions: dict, akey: str) -> int:
    """
    Place GTC Limit TP orders for ANY position that has a tp_price but no
    tp_order_id -- every strategy now gets a tp_price at entry (its own
    session-range target for gap/london_breakout, DEFAULT_TP_RR's fallback
    for everything else, see _run_entries), so this is no longer gap-only.
    Queries Saxo first to avoid duplicates.
    Returns number of TP orders successfully placed.
    """
    missing = [(k, v) for k, v in positions.items()
               if v.get("tp_price") is not None and
               v.get("tp_order_id") is None]
    if not missing:
        return 0

    open_orders = _fetch_open_orders(akey)
    if open_orders is None:
        return 0

    healed = 0
    for key, pos in missing:
        sym        = key.split(":", 1)[1] if ":" in key else key
        uic        = pos["uic"]
        direction  = pos.get("direction", "Buy")
        qty        = pos["quantity"]
        tp_price   = pos["tp_price"]
        close_side = "Sell" if direction == "Buy" else "Buy"
        rounded_tp = round(tp_price, get_price_decimals(sym))

        if (uic, close_side, "Limit") in open_orders:
            pos["tp_order_id"] = "synced"
            logger.debug(f"  [heal_tp] {key}  TP already in Saxo (synced)")
            continue

        logger.info(f"[heal_tp] {key} missing TP — placing {close_side} Limit@{rounded_tp}...")
        tp_body = {
            "AccountKey":    akey,
            "Uic":           uic,
            "AssetType":     ASSET_TYPE,
            "Amount":        qty,
            "BuySell":       close_side,
            "OrderType":     "Limit",
            "OrderPrice":    rounded_tp,
            "OrderDuration": {"DurationType": "GoodTillCancel"},
            "ManualOrder":   False,
        }
        try:
            resp   = _post("/trade/v2/orders", tp_body)
            tp_oid = str(resp.get("OrderId", "?"))
            pos["tp_order_id"] = tp_oid
            logger.info(f"  [heal_tp] {key}  tp_id={tp_oid}  ✓")
            healed += 1
        except Exception as exc:
            logger.warning(f"  [heal_tp] FAILED for {key}: {exc}")
    return healed


# ── Cross-process lock ─────────────────────────────────────────────────────────
# Discovered live 2026-08-24: `ATOS Forex Gap Fill` and `ATOS Forex Gap Monday
# Early` both fire at the exact same instant (Mon 03:00 PKT). This is a real
# double-entry risk, not just redundant scanning: forex_state.json's atomic
# write (_save_state) only prevents file CORRUPTION, not a race between two
# full processes -- both can load state before either writes back, both
# independently decide to open the same signal, both place a REAL Saxo
# order, and whichever saves last silently drops the other's position from
# local state while both real orders exist on Saxo. Same class of bug as the
# LBO duplicate-schedule fix from 2026-08-20 ([[forex_london_breakout]]).
# Extended the same day to a shared proc_lock.py once intraday_monitor.py
# was found to independently read/write this SAME forex_state.json from a
# completely separate process, invisible to a forex-runner-only lock -- see
# proc_lock.py's module docstring for the full writeup and FUTURES_LOCK's
# equivalent fix for futures_state.json.
import proc_lock


def _acquire_lock(label: str = "") -> bool:
    return proc_lock.acquire(proc_lock.FOREX_LOCK, label, logger=logger)


def _release_lock() -> None:
    proc_lock.release(proc_lock.FOREX_LOCK)


# ── Main daily cycle ──────────────────────────────────────────────────────────

def run_exits_only(dry_run: bool = True,
                   active_strategies: list | None = None,
                   session: str = "all") -> dict:
    """Check stops and time-stops on all open positions — no new entries."""
    # No weekday gate by design -- see run_daily() above for why.
    if active_strategies is None:
        active_strategies = list(STRATEGIES)

    session_filter = SESSION_PAIRS.get(session) if session != "all" else None
    active_pairs   = [p for p in PAIRS
                      if session_filter is None or p["symbol"] in session_filter]

    mode = "DRY-RUN" if dry_run else "LIVE (Saxo SIM)"
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
        market_data[pair["symbol"]] = _fetch_history(pair["uic"])

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
    # No weekday gate here by design: user explicitly wants every scheduled
    # trigger to keep scanning all 7 days rather than risk missing a signal
    # (e.g. a broker-side partial Saturday session, a scan running slightly
    # early/late relative to the exact weekend close/reopen boundary). A
    # scan that finds a genuinely closed market just finds no fresh data
    # and no signals -- harmless. Skipping outright is the riskier failure
    # mode if the boundary assumption is ever wrong.
    if active_strategies is None:
        active_strategies = list(STRATEGIES)

    session_filter = SESSION_PAIRS.get(session) if session != "all" else None
    active_pairs   = [p for p in PAIRS
                      if session_filter is None or p["symbol"] in session_filter]

    strat_label = "+".join(active_strategies)
    mode        = "DRY-RUN" if dry_run else "LIVE (Saxo SIM)"
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
        market_data[pair["symbol"]] = _fetch_history(pair["uic"])

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
            # Gap and LBO bypass the momentum filter — they need all pairs for
            # gap-percentage / session-breakout detection, unrelated to trend.
            # rsi/bb/zscore are MEAN-REVERSION strategies (dip-buy, fade,
            # z-score reversion) — the filter ranks by DIRECTIONAL trend
            # strength (price move / ATR), which is backwards for them: their
            # edge is catching reversals/chop, so restricting them to only the
            # most-trending pairs suppresses exactly the setups they're
            # designed to find. Only trend-following strategies (ema,
            # donchian, pullback, supertrend, ml, cnn_lstm) should be
            # momentum-filtered.
            _NO_MOMENTUM_FILTER = ("gap", "london_breakout", "rsi", "bb", "zscore")
            _edata = market_data if strat_name in _NO_MOMENTUM_FILTER else entry_market_data
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
                    help="Place real orders in Saxo SIM (default: dry-run)")
    ap.add_argument("--exits-only",  action="store_true",
                    help="Check stops only — no new entries (intraday stop check)")
    ap.add_argument("--strategy", default="all",
                    choices=["all", "ema", "rsi", "donchian", "bb", "pullback", "gap",
                             "supertrend", "zscore", "ml", "cnn_lstm", "london_breakout"],
                    help="Which strategy to run (default: all)")
    ap.add_argument("--status",   action="store_true",
                    help="Print open positions and exit")
    ap.add_argument("--scan",     action="store_true",
                    help="Show 4-panel market snapshot")
    ap.add_argument("--info",     action="store_true",
                    help="Verify UICs via live Saxo quotes")
    ap.add_argument("--session",  default="all",
                    choices=["all", "asian", "london"],
                    help="Restrict to session pairs: asian (06:20 PKT) | london (18:00 PKT) | all")
    args = ap.parse_args()

    if args.info:
        print(f"\n{'Pair':<10} {'UIC':>6}  {'Bid':>10} {'Ask':>10}  Description")
        print("  " + "-" * 58)
        for pair in PAIRS:
            uic = pair["uic"]
            try:
                resp = _get("/trade/v1/infoprices",
                            {"Uic": uic, "AssetType": ASSET_TYPE, "FieldGroups": "Quote"})
                q   = resp.get("Quote", {})
                print(f"  {pair['symbol']:<10} {uic:>6}  "
                      f"{q.get('Bid','?'):>10} {q.get('Ask','?'):>10}  {pair['description']}")
            except Exception as exc:
                print(f"  {pair['symbol']:<10} {uic:>6}  ERROR: {exc}")
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
            # ASCII only — Windows' default console codepage (cp1252) can't
            # encode ▲/▼/±, which crashed this whole --status call with an
            # unhandled UnicodeEncodeError on a plain (non-UTF-8) console.
            print(f"\nCurrency exposure (limit: +/-{MAX_CURRENCY_EXPOSURE}):")
            for ccy, net in sorted(exposure.items(), key=lambda x: abs(x[1]), reverse=True):
                if net == 0:
                    continue
                bar   = ("+" * abs(net)) if net > 0 else ("-" * abs(net))
                warn  = "  <- AT LIMIT" if abs(net) >= MAX_CURRENCY_EXPOSURE else ""
                print(f"  {ccy}  {net:+d}  {bar}{warn}")
        sys.exit(0)

    if args.scan:
        market_data = {}
        for pair in PAIRS:
            market_data[pair["symbol"]] = _fetch_history(pair["uic"])
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

        # Panel 10 — CNN-LSTM deep learning
        print(f"\n[CNN-LSTM] Multi-scale CNN + BiLSTM + Attention  "
              f"(36.9% val acc, threshold={strat_cnn_lstm.CONFIDENCE_THRESHOLD} — rarely fires)")
        rows = strat_cnn_lstm.scan_summary(market_data)
        print(f"  {'Pair':<10} {'Close':>10} {'P(Sell)':>8} {'P(Hold)':>8} {'P(Buy)':>8} {'ADX':>6}  Signal")
        print("  " + "-" * 70)
        for r in rows:
            if r["status"] == "no_model":
                print(f"  {r['symbol']:<10}  no trained model"); continue
            if r["status"] != "ok":
                print(f"  {r['symbol']:<10}  no data"); continue
            flag = f"  *** {r['signal']} ***" if r["signal"] != "hold" else ""
            print(f"  {r['symbol']:<10} {r['close']:>10.5f} {r['p_sell']:>8.3f} "
                  f"{r['p_hold']:>8.3f} {r['p_buy']:>8.3f} {r['adx']:>6.1f}{flag}")

        # Panel 11 — London/NY Breakout (day trading)
        print(f"\n[LBO] London/NY Session Breakout — day trading (~58-63% WR)")
        lbo_h1: dict = {}
        lbo_meta: dict = {}
        for pair in PAIRS:
            if pair["symbol"] not in strat_lbo.PAIRS:
                continue
            lbo_h1[pair["symbol"]]   = _fetch_history_h1(pair["uic"])
            lbo_meta[pair["symbol"]] = {"pip_size": pair.get("pip_size", 0.0001)}
        rows = strat_lbo.scan_summary(lbo_h1, lbo_meta)
        print(f"  {'Pair':<10} {'Range':>10} {'Hi':>10} {'Lo':>10} {'Close':>10} "
              f"{'Pips':>6}  Tradeable  Breakout")
        print("  " + "-" * 90)
        for r in rows:
            if r["status"] != "ok":
                print(f"  {r['symbol']:<10}  no data"); continue
            flag = f"  *** {r['breakout']} BREAKOUT ***" if r["breakout"] != "inside" else ""
            trd  = "yes" if r["tradeable"] else "no"
            print(f"  {r['symbol']:<10} {r['range_ref']:>10} {r['range_hi']:>10.5f} "
                  f"{r['range_lo']:>10.5f} {r['close']:>10.5f} {r['range_pip']:>6.1f}  "
                  f"{trd:^9}{flag}")

        sys.exit(0)

    active = list(STRATEGIES) if args.strategy == "all" else [args.strategy]
    # Serialize concurrent live invocations project-wide -- see LOCK_FILE's
    # docstring above _acquire_lock() for why this exists (a real double-
    # entry risk between overlapping scheduled tasks, found 2026-08-24, not
    # just this file's two callers below). Dry-runs never place real orders
    # so they skip locking entirely -- no risk, and no reason to block on a
    # live run in progress while testing/debugging.
    if args.live:
        _acquire_lock("exits-only" if args.exits_only else "daily")
    try:
        if args.exits_only:
            # Exits-only is safe (and useful) to include LBO in "all" — it never
            # opens new positions here, only checks stops/time-stops, so running
            # it as an extra safety net alongside LBO's own dedicated force-close
            # schedule can't cause duplicate entries.
            run_exits_only(dry_run=not args.live, active_strategies=active,
                           session=args.session)
        else:
            # LBO has its own dedicated capital, slots, and schedule
            # (lbo-london-open / lbo-ny-open / lbo-force-close). It must NEVER
            # run as a side effect of the generic "all strategies" Daily/London
            # scheduled entries — those fire at times (e.g. 18:00 PKT / 13:00 UTC)
            # that land inside LBO's own auto-detected NY-session entry window,
            # which would silently duplicate the dedicated lbo-ny-open task's
            # entries (same signals, same pairs, double the position size).
            # LBO only runs here if explicitly requested via --strategy.
            if args.strategy == "all":
                active = [s for s in active if s != "london_breakout"]
            run_daily(dry_run=not args.live, active_strategies=active,
                      session=args.session)
    finally:
        if args.live:
            _release_lock()
