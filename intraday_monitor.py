"""
intraday_monitor.py
--------------------
ATOS Intraday Price Monitor — the real-time safety net for live positions.
Checks live prices from Saxo API every second and sells IMMEDIATELY when any
position breaches its stop-loss. No signal generation, no rebalance.

Architecture:
  Daily engine (daily_run.py / Task Scheduler 06:00 PKT):
      signal generation, monthly rebalance, learning pass

  THIS SCRIPT (Task Scheduler 15:30 PKT = 09:30 ET = US market open):
      1-second polling → stop-loss check → instant sell
      exits automatically at 16:00 ET (US market close)

Reliability (real-money safe):
  - Rate-limit aware: backs off on HTTP 429 (respects Retry-After header)
  - Auth-error aware: on 401, attempts token refresh before retrying
  - Circuit breaker: if prices unavailable for BLIND_SECONDS, logs CRITICAL
    so you can see the alert in the log file / console immediately
  - Double-sell guard: checks DB before placing sell — won't sell twice
  - Every sell attempt is logged with full P&L detail
  - Sell order failures are logged as CRITICAL (order not placed)

Stop-loss priority (first rule that applies):
  1. Fixed stop_price from DB (set by risk engine at entry)
  2. Trailing stop: TRAILING_PCT below trailing_stop_high from DB
  3. Hard floor: HARD_STOP_PCT below entry_price (last resort — US Blend
     positions have stop_price=0; this catches extreme crashes for them)

Rate limits (Saxo SIM):
  ~120 req/min per endpoint. At 1 req/s = 60 req/min. Safe.
  For live production API, verify limits in the Saxo developer portal and
  adjust POLL_INTERVAL if needed.

Task Scheduler setup:
  Trigger  : Daily, 15:30 PKT
  Program  : python
  Arguments: intraday_monitor.py
  Start in : E:\\SaxoTrNew\\SaxoTrNew
"""

import os
import sys
import time
import logging
import requests
from datetime import datetime, date, timezone, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import saxo_client
import fx
from atos import database as db
from atos.risk import commission_sek

# US market holidays 2025–2026 (NYSE observed dates)
_US_HOLIDAYS = {
    date(2025, 1, 1), date(2025, 1, 20), date(2025, 2, 17), date(2025, 4, 18),
    date(2025, 5, 26), date(2025, 6, 19), date(2025, 7, 4), date(2025, 9, 1),
    date(2025, 11, 27), date(2025, 12, 25),
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
}

# ── Configuration ──────────────────────────────────────────────────
POLL_INTERVAL    = 1            # seconds between stop-loss price checks
DISPLAY_INTERVAL = 5            # seconds between screen redraws (lets you copy text)
HARD_STOP_PCT    = 0.15         # 15% below entry price (last-resort floor)
TRAILING_PCT     = 0.12         # 12% below trailing_stop_high
BLIND_SECONDS    = 180          # circuit-breaker: alert if blind for 3 min
LOG_FILE         = os.path.join(BASE_DIR, "data", "monitor_log.txt")

# US market hours (Eastern Time)
_EDT = timezone(timedelta(hours=-4))   # EDT (Apr–Nov)
_EST = timezone(timedelta(hours=-5))   # EST (Nov–Mar)
MARKET_OPEN  = (9, 30)
MARKET_CLOSE = (16, 0)

# ── Logging ────────────────────────────────────────────────────────
os.makedirs(os.path.join(BASE_DIR, "data"), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [MONITOR] %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("monitor")


# ── ET helpers (DST-aware) ─────────────────────────────────────────

def _et_now() -> datetime:
    """Return current time in US Eastern (picks EDT or EST based on month)."""
    now_utc = datetime.now(timezone.utc)
    # DST: second Sunday in March to first Sunday in November
    year = now_utc.year
    # Rough DST check (good enough; avoids an external dependency)
    mar_second_sun = datetime(year, 3, 8, 2, 0, tzinfo=timezone.utc)
    while mar_second_sun.weekday() != 6:
        mar_second_sun += timedelta(days=1)
    nov_first_sun = datetime(year, 11, 1, 2, 0, tzinfo=timezone.utc)
    while nov_first_sun.weekday() != 6:
        nov_first_sun += timedelta(days=1)
    tz = _EDT if mar_second_sun <= now_utc < nov_first_sun else _EST
    return now_utc.astimezone(tz)


def _is_trading_day(d: date = None) -> bool:
    """True if the given date is a US stock market trading day."""
    d = d or _et_now().date()
    if d.weekday() >= 5:          # weekend
        return False
    return d not in _US_HOLIDAYS


def _market_is_open() -> bool:
    """True if US market is currently open (Mon–Fri 09:30–16:00 ET, not a holiday)."""
    now = _et_now()
    if not _is_trading_day(now.date()):
        return False
    t = (now.hour, now.minute)
    return MARKET_OPEN <= t < MARKET_CLOSE


def _seconds_until_close() -> float:
    now = _et_now()
    close = now.replace(hour=MARKET_CLOSE[0], minute=MARKET_CLOSE[1],
                        second=0, microsecond=0)
    return max((close - now).total_seconds(), 0)


# ── Yahoo Finance last-close prices (used when market is closed) ────

def _fetch_yahoo_prices(tickers: list) -> dict:
    """Fetch last closing price for each ticker from Yahoo Finance.
    Returns {ticker: last_close_price}. Used when Saxo market is closed."""
    if not tickers:
        return {}
    try:
        import yfinance as yf
        raw = yf.download(
            tickers, period="2d", interval="1d",
            auto_adjust=True, threads=False, progress=False
        )
        prices = {}
        if len(tickers) == 1:
            if not raw.empty:
                prices[tickers[0]] = float(raw["Close"].iloc[-1])
        else:
            for t in tickers:
                try:
                    prices[t] = float(raw["Close"][t].dropna().iloc[-1])
                except Exception:
                    pass
        return prices
    except Exception as e:
        log.warning("Yahoo price fetch failed: %s", e)
        return {}


def _print_closed_table(open_trades: list, yahoo_prices: dict, imap: dict):
    """Show portfolio with last closing prices when market is closed."""
    global _display_initialized
    now_et = _et_now()

    pfx = (_ANSI_CLEAR + _ANSI_HOME) if not _display_initialized else _ANSI_HOME
    _display_initialized = True
    sys.stdout.write(pfx)
    print(f"{'─'*80}")
    trading_day = _is_trading_day()
    day_label = "trading day" if trading_day else "holiday/weekend"
    print(f"  ATOS Portfolio  |  {now_et.strftime('%A %H:%M')} ET  |  "
          f"Market CLOSED ({day_label})")
    print(f"  Prices: last Yahoo Finance close (Saxo API inactive)")
    print(f"{'─'*80}")
    print(f"  {'Ticker':<8}  {'Entry':>10}  {'Last Close':>10}  {'Shares':>7}  "
          f"{'P&L (SEK)':>12}  {'%Chg':>7}  {'Stop':>10}")
    print(f"{'─'*80}")

    total_pnl = 0.0
    total_cost = 0.0
    for t in open_trades:
        ticker = t.get("ticker", "")
        if ticker not in imap:
            continue
        shares     = t.get("shares", 0) or 0
        entry      = t.get("entry_price", 0) or 0
        stop_p     = t.get("stop_price", 0) or 0
        trail_high = t.get("trailing_stop_high") or entry
        cur        = yahoo_prices.get(ticker, 0)
        currency   = imap[ticker].get("currency", "USD")
        try:
            rate = fx.get_rate_to_sek(currency) if currency != "SEK" else 1.0
        except Exception:
            rate = 10.95

        eff_stop = (stop_p if stop_p > 0
                    else trail_high * (1.0 - TRAILING_PCT) if trail_high > 0
                    else entry * (1.0 - HARD_STOP_PCT))

        pnl_sek  = (cur - entry) * shares * rate if cur > 0 else 0.0
        pct_chg  = (cur - entry) / entry * 100 if entry > 0 and cur > 0 else 0.0
        total_pnl  += pnl_sek
        total_cost += entry * shares * rate

        cur_str = f"{cur:.2f}" if cur > 0 else "  ---"
        pnl_str = f"{pnl_sek:+,.0f}" if cur > 0 else "  ---"
        pct_str = f"{pct_chg:+.2f}%" if cur > 0 else "  ---"
        col   = "\033[92m" if pnl_sek >= 0 else "\033[91m"
        reset = "\033[0m"
        print(f"  {ticker:<8}  {currency} {entry:>8.2f}  {currency} {cur_str:>8}  "
              f"{int(shares):>7,}  {col}{pnl_str:>12}{reset}  "
              f"{col}{pct_str:>7}{reset}  {currency} {eff_stop:>8.2f}")

    print(f"{'─'*80}")
    total_pct = total_pnl / total_cost * 100 if total_cost > 0 else 0
    col   = "\033[92m" if total_pnl >= 0 else "\033[91m"
    reset = "\033[0m"
    print(f"  {'TOTAL':<8}  {'':>10}  {'':>10}  {'':>7}  "
          f"{col}{total_pnl:>+12,.0f}{reset}  {col}{total_pct:>+7.2f}%{reset}")
    print(f"{'─'*80}")
    print()
    print("  Monitor will AUTO-START live Saxo polling when market opens (09:30 ET).")
    print("  (Press Ctrl+C to exit)")
    print()


# ── Live price fetching ────────────────────────────────────────────

def _fetch_saxo_prices(consecutive_failures: list) -> dict:
    """Fetch current prices for all Saxo positions.
    Returns {uic: current_price_in_instrument_currency}.
    Updates consecutive_failures[0] for the circuit breaker."""
    try:
        resp = requests.get(
            f"{saxo_client.SIM_BASE_URL}/port/v1/positions/me",
            headers=saxo_client._headers(),
            params={"FieldGroups": "PositionBase,DisplayAndFormat,PositionView"},
            timeout=10,
        )
    except (requests.exceptions.Timeout,
            requests.exceptions.ConnectionError) as e:
        consecutive_failures[0] += 1
        log.warning("Network error fetching prices (fail #%d): %s",
                    consecutive_failures[0], e)
        return {}

    # Rate-limited
    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", 5))
        consecutive_failures[0] += 1
        log.warning("Rate limited by Saxo (429) — backing off %ds", retry_after)
        time.sleep(retry_after)
        return {}

    # Auth expired — token needs refresh
    if resp.status_code == 401:
        consecutive_failures[0] += 1
        log.warning("Auth error (401) — token may have expired. Attempting refresh...")
        try:
            import saxo_auth
            saxo_auth.get_valid_access_token()   # PKCE auto-refresh
            log.info("Token refreshed OK")
        except Exception as e:
            log.error("Token refresh failed: %s", e)
        return {}

    if resp.status_code != 200:
        consecutive_failures[0] += 1
        log.warning("Saxo returned HTTP %d (fail #%d)",
                    resp.status_code, consecutive_failures[0])
        return {}

    # Successful fetch — reset failure counter
    consecutive_failures[0] = 0

    prices = {}
    for pos in resp.json().get("Data", []):
        pv  = pos.get("PositionView", {})
        pb  = pos.get("PositionBase", {})
        uic = pb.get("Uic")
        if uic is None:
            continue

        cur = pv.get("CurrentPrice") or 0

        # Fallback 1: MarketValue / Amount
        if cur == 0:
            mv     = pv.get("MarketValue") or 0
            amount = pb.get("Amount") or 0
            if amount > 0 and mv != 0:
                cur = abs(mv) / abs(amount)

        # Fallback 2: OpenPrice + P&L / shares
        if cur == 0:
            entry  = pb.get("OpenPrice") or 0
            pnl    = pv.get("ProfitLossOnTrade") or 0
            amount = pb.get("Amount") or 0
            if entry > 0 and amount > 0:
                cur = entry + pnl / amount

        prices[uic] = cur

    return prices


# ── Stop-loss evaluation ───────────────────────────────────────────

def _stop_triggered(trade: dict, cur_price: float) -> tuple[bool, str]:
    """Return (triggered, reason) for one open position."""
    shares      = trade.get("shares", 0) or 0
    entry_price = trade.get("entry_price", 0) or 0
    stop_price  = trade.get("stop_price", 0) or 0
    trail_high  = trade.get("trailing_stop_high") or entry_price

    if cur_price <= 0 or shares <= 0 or entry_price <= 0:
        return False, ""

    # Rule 1: fixed stop
    if stop_price > 0 and cur_price <= stop_price:
        return True, f"fixed stop {stop_price:.4f} (cur {cur_price:.4f})"

    # Rule 2: trailing stop from DB high-water mark
    if trail_high > 0:
        trail_stop = trail_high * (1.0 - TRAILING_PCT)
        if cur_price <= trail_stop:
            return True, (f"trailing stop {trail_stop:.4f} "
                          f"({TRAILING_PCT*100:.0f}% below high {trail_high:.4f}, "
                          f"cur {cur_price:.4f})")

    # Rule 3: hard floor (catches US Blend positions which have stop_price=0)
    hard_floor = entry_price * (1.0 - HARD_STOP_PCT)
    if cur_price <= hard_floor:
        return True, (f"hard floor {hard_floor:.4f} "
                      f"({HARD_STOP_PCT*100:.0f}% below entry {entry_price:.4f}, "
                      f"cur {cur_price:.4f})")

    return False, ""


# ── Sell execution ─────────────────────────────────────────────────

def _execute_stop_sell(trade: dict, cur_price: float, reason: str, imap: dict):
    """Place market sell and update DB. Logs extensively for audit trail."""
    ticker = trade.get("ticker", "")
    shares = int(trade.get("shares", 0) or 0)
    mkt    = trade.get("market_group", "US Equities")

    # Double-sell guard: re-read the DB row to confirm it's still open
    live_trades = {t["id"]: t for t in db.get_open_trades()}
    if trade["id"] not in live_trades:
        log.info("Trade %d (%s) already closed — skip double-sell", trade["id"], ticker)
        return

    if ticker not in imap:
        log.critical(
            "STOP triggered on %s but NOT in instrument_map.csv — CANNOT SELL! "
            "Close this position manually in SaxoTraderGO immediately!",
            ticker
        )
        return

    uic      = imap[ticker]["uic"]
    currency = imap[ticker].get("currency", "USD")
    try:
        rate = fx.get_rate_to_sek(currency) if currency != "SEK" else 1.0
    except Exception:
        rate = 10.95   # rough fallback; only affects P&L logging, not the order

    price_sek = cur_price * rate
    comm      = commission_sek(shares, price_sek)
    entry_sek = (trade.get("entry_price", cur_price) or cur_price) * rate
    pnl_sek   = shares * (price_sek - entry_sek) - comm

    log.warning(
        "*** STOP-LOSS TRIGGERED *** %s x%d @ %.4f | %s | est. P&L %.0f SEK",
        ticker, shares, cur_price, reason, pnl_sek
    )

    try:
        saxo_client.place_market_order(uic, "Stock", "Sell", shares)
        log.warning("Sell order PLACED — %s x%d @ market", ticker, shares)
    except Exception as e:
        log.critical(
            "SELL ORDER FAILED for %s: %s — "
            "CLOSE THIS POSITION MANUALLY IN SAXOTRADEGO IMMEDIATELY!",
            ticker, e
        )
        return

    try:
        db.close_trade(trade["id"], cur_price, "intraday_stop", pnl_sek, comm)
        log.info("DB updated: trade %d closed (%s)", trade["id"], ticker)
    except Exception as e:
        log.error(
            "DB close_trade failed for %s (sell ORDER was placed!): %s",
            ticker, e
        )


# ── Terminal portfolio table ───────────────────────────────────────

_ANSI_HOME  = "\033[H"      # move cursor to top-left (no screen wipe)
_ANSI_CLEAR = "\033[2J"    # clear screen — only used once at startup

_display_initialized = False


def _render_table(open_trades: list, prices: dict, imap: dict, tick: int,
                  mode: str = "LIVE") -> str:
    """Build the full portfolio table as a string (no side-effects)."""
    now_et = _et_now()
    rows = []
    total_pnl = 0.0
    total_cost = 0.0

    for t in open_trades:
        ticker = t.get("ticker", "")
        if ticker not in imap:
            continue
        uic        = imap[ticker]["uic"]
        shares     = t.get("shares", 0) or 0
        entry      = t.get("entry_price", 0) or 0
        stop_p     = t.get("stop_price", 0) or 0
        trail_high = t.get("trailing_stop_high") or entry
        cur        = prices.get(uic, 0)
        currency   = imap[ticker].get("currency", "USD")
        try:
            rate = fx.get_rate_to_sek(currency) if currency != "SEK" else 1.0
        except Exception:
            rate = 10.95

        eff_stop = (stop_p if stop_p > 0
                    else trail_high * (1.0 - TRAILING_PCT) if trail_high > 0
                    else entry * (1.0 - HARD_STOP_PCT))

        pnl_sek = (cur - entry) * shares * rate if cur > 0 else 0.0
        pct_chg = (cur - entry) / entry * 100 if entry > 0 and cur > 0 else 0.0
        total_pnl  += pnl_sek
        total_cost += entry * shares * rate

        rows.append({"ticker": ticker, "entry": entry, "cur": cur,
                     "shares": int(shares), "pnl_sek": pnl_sek,
                     "pct": pct_chg, "stop": eff_stop, "currency": currency})

    total_pct = total_pnl / total_cost * 100 if total_cost > 0 else 0
    W = "\033[0m"

    lines = []
    lines.append("─" * 80)
    lines.append(f"  ATOS Portfolio  |  {now_et.strftime('%H:%M:%S')} ET  |  "
                 f"Market: {mode}  |  Polling every {POLL_INTERVAL}s  "
                 f"(display every {DISPLAY_INTERVAL}s — text is copyable)")
    lines.append("─" * 80)
    lines.append(f"  {'Ticker':<8}  {'Entry':>10}  {'Current':>10}  {'Shares':>7}  "
                 f"{'P&L (SEK)':>12}  {'%Chg':>7}  {'Stop':>10}")
    lines.append("─" * 80)

    for r in rows:
        cur_str = f"{r['cur']:.2f}" if r['cur'] > 0 else "    ---"
        pnl_str = f"{r['pnl_sek']:+,.0f}" if r['cur'] > 0 else "    ---"
        pct_str = f"{r['pct']:+.2f}%" if r['cur'] > 0 else "    ---"
        col = "\033[92m" if r['pnl_sek'] >= 0 else "\033[91m"
        lines.append(
            f"  {r['ticker']:<8}  {r['currency']} {r['entry']:>8.2f}  "
            f"{r['currency']} {cur_str:>8}  {r['shares']:>7,}  "
            f"{col}{pnl_str:>12}{W}  {col}{pct_str:>7}{W}  "
            f"{r['currency']} {r['stop']:>8.2f}"
        )

    lines.append("─" * 80)
    col = "\033[92m" if total_pnl >= 0 else "\033[91m"
    lines.append(
        f"  {'TOTAL':<8}  {'':>10}  {'':>10}  {'':>7}  "
        f"{col}{total_pnl:>+12,.0f}{W}  {col}{total_pct:>+7.2f}%{W}"
    )
    lines.append("─" * 80)
    lines.append(f"  Stop rules: trailing {TRAILING_PCT*100:.0f}% below high  |  "
                 f"hard floor {HARD_STOP_PCT*100:.0f}% below entry")
    lines.append("")
    lines.append("  Ctrl+C to stop.  Text above stays still — safe to select & copy.")
    lines.append("")   # blank line so cursor parks below the table
    return "\n".join(lines)


def _print_portfolio_table(open_trades: list, prices: dict, imap: dict, tick: int):
    """Overwrites the terminal table in place without flashing."""
    global _display_initialized
    text = _render_table(open_trades, prices, imap, tick, mode="LIVE")
    if not _display_initialized:
        # First paint: clear once so we start with a clean slate
        sys.stdout.write(_ANSI_CLEAR + _ANSI_HOME + text)
        _display_initialized = True
    else:
        # Subsequent paints: jump to top and overwrite — no flash, text is stable
        sys.stdout.write(_ANSI_HOME + text)
    sys.stdout.flush()


# ── Single poll ────────────────────────────────────────────────────

def _poll_once(imap: dict, consecutive_failures: list, blind_since: list,
               tick: int, show_table: bool, last_display: list) -> dict:
    """Poll prices, check stops, optionally refresh display.
    last_display[0] = timestamp of last screen redraw (throttled to DISPLAY_INTERVAL).
    Returns the latest prices dict (or {} on failure)."""
    open_trades = db.get_open_trades()
    if not open_trades:
        consecutive_failures[0] = 0
        return {}

    prices = _fetch_saxo_prices(consecutive_failures)

    # Circuit breaker
    if not prices:
        if blind_since[0] is None:
            blind_since[0] = time.time()
        blind_for = time.time() - blind_since[0]
        if blind_for > BLIND_SECONDS:
            log.critical(
                "NO LIVE PRICES FOR %.0fs — MONITOR IS BLIND. "
                "Check token / network / Saxo status. "
                "Consider closing positions manually if market is moving.",
                blind_for,
            )
        return {}
    blind_since[0] = None

    # Redraw display only every DISPLAY_INTERVAL seconds so text stays
    # stable long enough for the user to select and copy it
    if show_table and (time.time() - last_display[0]) >= DISPLAY_INTERVAL:
        _print_portfolio_table(open_trades, prices, imap, tick)
        last_display[0] = time.time()

    # Check stops (always runs every POLL_INTERVAL — independent of display)
    uic_to_trade = {imap[t["ticker"]]["uic"]: t
                    for t in open_trades if t.get("ticker") in imap}

    for uic, trade in uic_to_trade.items():
        cur_price = prices.get(uic, 0)
        ticker    = trade.get("ticker", "")
        if cur_price <= 0:
            log.debug("No live price for %s (UIC %d)", ticker, uic)
            continue

        triggered, reason = _stop_triggered(trade, cur_price)
        if triggered:
            _execute_stop_sell(trade, cur_price, reason, imap)
        else:
            entry = trade.get("entry_price", 0) or 0
            pct   = (cur_price - entry) / entry * 100 if entry > 0 else 0
            log.debug("  %-6s %.4f  (%+.2f%%)", ticker, cur_price, pct)

    return prices


# ── Main ───────────────────────────────────────────────────────────

def main():
    from instrument_map import load_instrument_map

    # --no-display: suppress the terminal table (e.g. when running as a
    # background Task Scheduler job where no console is attached)
    show_table = "--no-display" not in sys.argv

    log.info("=" * 60)
    log.info("ATOS Intraday Monitor — %s ET", _et_now().strftime("%Y-%m-%d %H:%M:%S"))
    log.info("Poll: %ds | Trailing: %.0f%% | Hard floor: %.0f%% | Table: %s",
             POLL_INTERVAL, TRAILING_PCT * 100, HARD_STOP_PCT * 100,
             "ON" if show_table else "OFF (--no-display)")
    log.info("=" * 60)

    try:
        imap = load_instrument_map()
    except Exception as e:
        log.critical("Cannot load instrument_map: %s", e)
        sys.exit(1)

    db.init_db()

    if not _market_is_open():
        log.info("Market not yet open — will start checking at 09:30 ET")

    consecutive_failures = [0]
    blind_since          = [None]
    last_display         = [0.0]   # timestamp of last screen redraw
    tick                 = 0

    open_trades      = db.get_open_trades()
    yahoo_prices: dict = {}
    yahoo_fetched_at = 0.0

    try:
        while True:
            now_et = _et_now()
            market_open = _market_is_open()

            if market_open:
                # ── LIVE MODE — Saxo API every 1s, display every 5s ──
                try:
                    _poll_once(imap, consecutive_failures, blind_since,
                               tick, show_table, last_display)
                except Exception as e:
                    log.error("Unhandled poll error: %s", e)
                tick += 1
                if tick % 60 == 0:
                    log.info("Heartbeat tick=%d | %s ET | %d positions watched",
                             tick, now_et.strftime("%H:%M:%S"),
                             len(db.get_open_trades()))

                if _seconds_until_close() <= 0:
                    log.info("US market closed (16:00 ET). Live polling stopped.")

            else:
                # ── CLOSED/HOLIDAY MODE — Yahoo prices every 5min ────
                if show_table and (time.time() - yahoo_fetched_at > 300):
                    open_trades = db.get_open_trades()
                    tickers = [t["ticker"] for t in open_trades
                               if t.get("ticker") in imap]
                    if tickers:
                        yahoo_prices = _fetch_yahoo_prices(tickers)
                    yahoo_fetched_at = time.time()
                    _print_closed_table(open_trades, yahoo_prices, imap)

                time.sleep(30)
                continue

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        log.info("Monitor stopped by user (Ctrl+C).")

    # Final closed-market snapshot after market closes
    if show_table:
        open_trades = db.get_open_trades()
        tracked_tickers = [t["ticker"] for t in open_trades if t.get("ticker") in imap]
        if tracked_tickers:
            log.info("Fetching final closing prices from Yahoo...")
            yahoo_prices = _fetch_yahoo_prices(tracked_tickers)
        _print_closed_table(open_trades, yahoo_prices, imap)
        input("\n  Market closed. Press Enter to exit...")


if __name__ == "__main__":
    main()
