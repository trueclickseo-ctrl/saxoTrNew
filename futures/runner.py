"""
futures/runner.py
-----------------
Daily execution runner for the Futures Trend-Following strategy.

Usage:
    python futures/runner.py              # scan + dry-run (no real orders)
    python futures/runner.py --live       # real orders in Saxo SIM
    python futures/runner.py --discover   # refresh UIC cache then exit
    python futures/runner.py --status     # print open positions then exit

State:
    data/futures_state.json   — open positions
    data/futures_orders.json  — order log (last 500 entries)
    data/futures_uic_cache.json — instrument UIC map (populated by --discover)
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, date

# ── Path setup ────────────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)
sys.path.insert(0, _HERE)

import requests
import pandas as pd
import saxo_auth

from futures.universe import load_universe, MARKETS
from futures.strategy import (
    generate_signals, should_exit, size_position,
    trailing_stop_update,
    MAX_POSITIONS, MIN_BARS,
    LONG_ONLY_MARKETS, BIDIRECTIONAL_MARKETS,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("futures.runner")

# ── Constants ──────────────────────────────────────────────────────────────
BASE_URL    = "https://gateway.saxobank.com/sim/openapi"
ASSET_TYPE  = "CfdOnFutures"
DATA_DIR    = os.path.join(_ROOT, "data")
STATE_FILE  = os.path.join(DATA_DIR, "futures_state.json")
ORDERS_FILE = os.path.join(DATA_DIR, "futures_orders.json")
CHART_BARS  = 60          # daily bars to fetch (needs ≥ MIN_BARS = 39)


# ── Saxo HTTP helpers ──────────────────────────────────────────────────────

def _hdrs() -> dict:
    return {"Authorization": f"Bearer {saxo_auth.get_valid_access_token()}"}


def _get(path: str, params: dict | None = None) -> dict:
    for attempt in range(1, 4):
        try:
            r = requests.get(f"{BASE_URL}{path}", headers=_hdrs(),
                             params=params, timeout=15)
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
    r = requests.post(f"{BASE_URL}{path}", headers=_hdrs(), json=body, timeout=15)
    r.raise_for_status()
    return r.json()


# ── Account ────────────────────────────────────────────────────────────────

def _account() -> tuple[float, str]:
    """Returns (equity_float, account_key_str)."""
    equity, key = 0.0, ""
    try:
        bal    = _get("/port/v1/balances/me")
        equity = float(
            bal.get("TotalValue")
            or bal.get("NetEquityForMargin")
            or bal.get("CashBalance")
            or 0
        )
    except Exception as exc:
        logger.warning(f"Could not read account equity: {exc}")
    try:
        info = _get("/port/v1/accounts/me")
        data = info.get("Data", info)
        acct = data[0] if isinstance(data, list) else data
        key  = (acct.get("AccountKey", "") if isinstance(acct, dict) else "") or ""
    except Exception as exc:
        logger.warning(f"Could not read AccountKey: {exc}")
    return equity, key


# ── Price data ─────────────────────────────────────────────────────────────

def _fetch_history(uic: int, count: int = CHART_BARS) -> pd.DataFrame | None:
    """Fetch daily OHLCV from Saxo chart API v3. Returns DataFrame or None."""
    try:
        resp = _get("/chart/v3/charts", {
            "Uic":       uic,
            "AssetType": ASSET_TYPE,
            "Horizon":   1440,
            "Count":     count + 5,
        })
        rows = []
        for bar in resp.get("Data", []):
            if isinstance(bar, dict):
                o = bar.get("Open",  bar.get("open",  None))
                h = bar.get("High",  bar.get("high",  None))
                l = bar.get("Low",   bar.get("low",   None))
                c = bar.get("Close", bar.get("close", None))
                v = bar.get("Volume",bar.get("volume",0))
                if None not in (o, h, l, c) and float(c) > 0:
                    rows.append({"Open": float(o), "High": float(h),
                                 "Low":  float(l), "Close": float(c),
                                 "Volume": float(v)})
        if len(rows) >= MIN_BARS:
            return pd.DataFrame(rows)
        logger.debug(f"UIC {uic}: only {len(rows)} bars (need {MIN_BARS})")
        return None
    except Exception as exc:
        logger.warning(f"Chart fetch failed for UIC {uic}: {exc}")
        return None


def _live_price(uic: int, account_key: str) -> float | None:
    """Current mid price via /trade/v1/infoprices."""
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


# ── State management ───────────────────────────────────────────────────────

def _load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"positions": {}, "last_run": None}


def _save_state(state: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


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


# ── Core run ───────────────────────────────────────────────────────────────

def run_daily(dry_run: bool = True) -> dict:
    """Execute one daily futures cycle. Returns summary dict."""
    mode = "DRY-RUN" if dry_run else "LIVE (Saxo SIM)"
    logger.info(f"{'='*55}")
    logger.info(f"  Futures Runner — {mode}  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    logger.info(f"{'='*55}")

    universe  = load_universe()
    state     = _load_state()
    positions = state.setdefault("positions", {})
    equity, akey = _account()

    logger.info(f"Account equity : {equity:,.0f} SEK (approx)")
    logger.info(f"Open positions : {len(positions)} / {MAX_POSITIONS}")
    logger.info(f"Markets tracked: {len(universe)}")

    # ── Fetch daily history for all markets ──────────────────────────────
    market_data: dict[str, pd.DataFrame | None] = {}
    for sym, info in universe.items():
        market_data[sym] = _fetch_history(info["uic"])

    # ── Trail stops for open positions ───────────────────────────────────
    from futures.strategy import _atr
    for sym, pos in positions.items():
        df = market_data.get(sym)
        if df is None:
            continue
        closes    = df["Close"].dropna()
        highs     = df["High"].dropna()
        lows      = df["Low"].dropna()
        direction = pos.get("direction", "Buy")
        atr_now   = float(_atr(highs, lows, closes).iloc[-1])
        cur_stop  = float(pos.get("stop_price", 0))
        new_stop  = trailing_stop_update(cur_stop, float(closes.iloc[-1]),
                                         atr_now, direction)
        if new_stop != cur_stop and new_stop > 0:
            logger.info(f"  Trail stop {sym} ({direction}): {cur_stop:.4f} → {new_stop:.4f}")
            pos["stop_price"] = round(new_stop, 6)

    # ── Exit review ───────────────────────────────────────────────────────
    today_str   = date.today().isoformat()
    exits_done  = 0

    for sym in list(positions):
        pos     = positions[sym]
        df      = market_data.get(sym)
        ed      = pos.get("entry_date", today_str)
        cal_days = (date.today() - date.fromisoformat(ed)).days
        exit_flag, reason = should_exit(pos, df, cal_days)

        if not exit_flag:
            continue

        info      = universe.get(sym, {})
        uic       = info.get("uic") or pos.get("uic")
        qty       = pos.get("quantity", 1)
        direction = pos.get("direction", "Buy")
        is_long   = direction == "Buy"
        # Closing side: long → Sell;  short → Buy
        close_side = "Sell" if is_long else "Buy"

        live_px = _live_price(uic, akey) if uic else None
        live_px = live_px or float(pos.get("entry_price", 0))
        entry   = float(pos.get("entry_price", 0))
        raw_pnl = (live_px - entry) if is_long else (entry - live_px)
        pnl_pct = raw_pnl / entry * 100 if entry else 0.0

        order = {
            "AccountKey":    akey,
            "Uic":           uic,
            "AssetType":     ASSET_TYPE,
            "Amount":        qty,
            "BuySell":       close_side,
            "OrderType":     "Market",
            "OrderDuration": {"DurationType": "DayOrder"},
            "ManualOrder":   False,
        }

        tag = f"[{direction}]"
        if dry_run:
            logger.info(f"[DRY] {close_side:<4} {qty}x {sym} {tag} — {reason} "
                        f"@ ~{live_px:.4f}  P&L {pnl_pct:+.1f}%")
        else:
            resp = _post("/trade/v2/orders", order)
            logger.info(f"{close_side} {resp.get('OrderId','?')}: {qty}x {sym} {tag} "
                        f"— {reason} @ ~{live_px:.4f}  P&L {pnl_pct:+.1f}%")

        _log_order({"side": close_side, "symbol": sym, "uic": uic, "quantity": qty,
                    "position_direction": direction,
                    "exit_price": live_px, "reason": reason,
                    "pnl_pct": round(pnl_pct, 2), "dry_run": dry_run})
        del positions[sym]
        exits_done += 1

    # ── Entry signals ─────────────────────────────────────────────────────
    slots_free   = MAX_POSITIONS - len(positions)
    entries_done = 0
    signals      = []

    if slots_free > 0 and equity > 0:
        signals = generate_signals(market_data)
        signals = [s for s in signals if s["symbol"] not in positions
                   and s["symbol"] in universe]

        for sig in signals[:slots_free]:
            sym       = sig["symbol"]
            info      = universe[sym]
            uic       = info["uic"]
            qty       = size_position(equity, sig["atr"])
            direction = sig["direction"]   # "Buy" (long) or "Sell" (short)

            order = {
                "AccountKey":    akey,
                "Uic":           uic,
                "AssetType":     ASSET_TYPE,
                "Amount":        qty,
                "BuySell":       direction,
                "OrderType":     "Market",
                "OrderDuration": {"DurationType": "DayOrder"},
                "ManualOrder":   False,
            }

            tag = "LONG" if direction == "Buy" else "SHORT"
            if dry_run:
                logger.info(f"[DRY] {direction:<4} {qty}x {sym} [{tag}] "
                            f"@ ~{sig['close']:.4f}  stop={sig['stop_price']:.4f}  "
                            f"score={sig['score']:.3f}  ATR={sig['atr']:.4f}")
            else:
                resp = _post("/trade/v2/orders", order)
                logger.info(f"{direction} {resp.get('OrderId','?')}: {qty}x {sym} [{tag}] "
                            f"@ ~{sig['close']:.4f}  stop={sig['stop_price']:.4f}")

            positions[sym] = {
                "uic":          uic,
                "direction":    direction,
                "entry_price":  sig["close"],
                "stop_price":   sig["stop_price"],
                "quantity":     qty,
                "entry_date":   today_str,
                "atr_at_entry": sig["atr"],
                "score":        sig["score"],
            }
            _log_order({"side": direction, "symbol": sym, "uic": uic, "quantity": qty,
                        "entry_price": sig["close"], "stop_price": sig["stop_price"],
                        "score": sig["score"], "dry_run": dry_run})
            entries_done += 1

    elif slots_free > 0:
        logger.info("No entry signals today (or regime filter active).")

    # ── Final status ──────────────────────────────────────────────────────
    logger.info(f"{'─'*55}")
    logger.info(f"  Exits: {exits_done}  |  Entries: {entries_done}  "
                f"|  Holding: {len(positions)}")
    for sym, pos in positions.items():
        df        = market_data.get(sym)
        cur_px    = float(df["Close"].iloc[-1]) if df is not None else pos["entry_price"]
        direction = pos.get("direction", "Buy")
        is_long   = direction == "Buy"
        raw_pnl   = (cur_px - pos["entry_price"]) if is_long else (pos["entry_price"] - cur_px)
        pnl_pct   = raw_pnl / pos["entry_price"] * 100
        held      = (date.today() - date.fromisoformat(pos["entry_date"])).days
        tag       = "L" if is_long else "S"
        logger.info(f"  HOLD {sym:<4}[{tag}]  qty={pos['quantity']}  "
                    f"entry={pos['entry_price']:.4f}  now={cur_px:.4f}  "
                    f"P&L {pnl_pct:+.1f}%  stop={pos['stop_price']:.4f}  "
                    f"{held}d held")
    logger.info(f"{'─'*55}")

    state["last_run"] = datetime.now().isoformat()
    _save_state(state)

    return {
        "exits":    exits_done,
        "entries":  entries_done,
        "holding":  len(positions),
        "signals":  len(signals),
        "dry_run":  dry_run,
    }


# ── CLI ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Futures trend-following runner")
    ap.add_argument("--live",     action="store_true",
                    help="Place real orders in Saxo SIM (default: dry-run)")
    ap.add_argument("--discover", action="store_true",
                    help="(Re)discover instrument UICs from Saxo and exit")
    ap.add_argument("--status",   action="store_true",
                    help="Print open positions and exit (no orders)")
    args = ap.parse_args()

    if args.discover:
        logger.info("Discovering futures instrument UICs from Saxo SIM...")
        from futures.universe import discover_uics, UIC_CACHE
        import json, os

        def _get_fn(path, params=None):
            return _get(path, params)

        universe = discover_uics(_get_fn)
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(UIC_CACHE, "w") as f:
            json.dump(universe, f, indent=2)
        print(f"\nDiscovered {len(universe)} markets:")
        for sym, info in universe.items():
            print(f"  {sym:<6}  UIC={info['uic']}  {info['description']}")
        sys.exit(0)

    if args.status:
        state     = _load_state()
        positions = state.get("positions", {})
        print(f"\nFutures open positions ({len(positions)}):")
        if not positions:
            print("  None")
        for sym, pos in positions.items():
            held = (date.today() - date.fromisoformat(pos["entry_date"])).days
            print(f"  {sym:<6}  qty={pos['quantity']}  "
                  f"entry={pos['entry_price']:.4f}  "
                  f"stop={pos['stop_price']:.4f}  {held}d")
        sys.exit(0)

    run_daily(dry_run=not args.live)
