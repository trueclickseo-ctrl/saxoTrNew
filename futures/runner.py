"""
futures/runner.py
-----------------
Daily execution runner for 6 independent futures strategies.

STRATEGIES (each runs with its own position slots, 5 markets each):
  donchian  — Donchian Channel 30-day breakout         ~8-9 signals/yr
  rsi       — RSI(2) pullback within trend              ~50-60 signals/yr
  ema       — EMA(5/20) crossover + ADX(14) filter      ~20-25 signals/yr
  macd      — MACD(12,26,9) momentum crossover          ~12-18 signals/yr
  squeeze   — Bollinger Band Squeeze breakout           ~10-15 signals/yr
  ma_cross  — SMA(50/200) Golden/Death Cross            ~4-8 signals/yr
  TOTAL                                                 ~104-135 signals/yr
  MAX POSITIONS                                         30 (6 strategies × 5 slots)

Usage:
    python futures/runner.py              # all 6 strategies, dry-run
    python futures/runner.py --live       # all 6 strategies, real orders
    python futures/runner.py --strategy donchian|rsi|ema|macd|squeeze|ma_cross
    python futures/runner.py --discover   # refresh UIC cache then exit
    python futures/runner.py --status     # print open positions then exit
    python futures/runner.py --scan       # 6-panel market snapshot

State:
    data/futures_state.json     — open positions (keyed by strategy:symbol)
    data/futures_orders.json    — order log (last 500 entries)
    data/futures_uic_cache.json — instrument UIC map
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
import futures.strategy          as strat_donchian
import futures.strategy_rsi      as strat_rsi
import futures.strategy_ema      as strat_ema
import futures.strategy_macd     as strat_macd
import futures.strategy_squeeze  as strat_squeeze
import futures.strategy_ma_cross as strat_ma_cross
import pnl_tracker
import trade_logger

# Registry: name → strategy module
STRATEGIES = {
    "donchian": strat_donchian,
    "rsi":      strat_rsi,
    "ema":      strat_ema,
    "macd":     strat_macd,
    "squeeze":  strat_squeeze,
    "ma_cross": strat_ma_cross,
}

# Positions-per-strategy slot limit (independent of each other)
SLOTS_PER_STRATEGY = {
    "donchian": 5,
    "rsi":      5,
    "ema":      5,
    "macd":     5,
    "squeeze":  5,
    "ma_cross": 5,
}

_LOG_DIR  = os.path.join(_ROOT, "logs")
os.makedirs(_LOG_DIR, exist_ok=True)
_LOG_FILE = os.path.join(_LOG_DIR, f"futures_{date.today():%Y-%m-%d}.log")

_fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S")
_fh  = logging.FileHandler(_LOG_FILE, encoding="utf-8")
_fh.setFormatter(_fmt)
_sh  = logging.StreamHandler()
_sh.setFormatter(_fmt)

logging.basicConfig(level=logging.INFO, handlers=[_sh, _fh])
logger = logging.getLogger("futures.runner")

# ── Constants ──────────────────────────────────────────────────────────────
BASE_URL    = "https://gateway.saxobank.com/sim/openapi"
DATA_DIR    = os.path.join(_ROOT, "data")
STATE_FILE  = os.path.join(DATA_DIR, "futures_state.json")
ORDERS_FILE = os.path.join(DATA_DIR, "futures_orders.json")
CHART_BARS  = 260         # daily bars to fetch — 260 covers SMA(200) + buffer for MA Cross
MIN_BARS    = 55          # minimum valid bars (covers Donchian 30 + ATR 14 + buffer)

# Chart API horizon: 1440 = daily bars.
# Asset types that the chart API accepts per instrument type:
CHART_ASSET_TYPE = {
    "CfdOnIndex":      "CfdOnIndex",
    "FxSpot":          "FxSpot",
    "ContractFutures": "ContractFutures",
}


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

def _fetch_history(uic: int, asset_type: str,
                   count: int = CHART_BARS) -> pd.DataFrame | None:
    """Fetch daily OHLCV from Saxo chart API v3. Returns DataFrame or None."""
    try:
        resp = _get("/chart/v3/charts", {
            "Uic":       uic,
            "AssetType": asset_type,
            "Horizon":   1440,
            "Count":     count + 5,
        })
        rows = []
        for bar in resp.get("Data", []):
            if not isinstance(bar, dict):
                continue
            # ContractFutures: Open/High/Low/Close fields
            # CfdOnIndex / FxSpot: bid/ask spread — use mid price
            if "Close" in bar or "close" in bar:
                o = bar.get("Open",  bar.get("open",  None))
                h = bar.get("High",  bar.get("high",  None))
                l = bar.get("Low",   bar.get("low",   None))
                c = bar.get("Close", bar.get("close", None))
                v = bar.get("Volume",bar.get("volume",0))
            elif "CloseAsk" in bar and "CloseBid" in bar:
                ask_c = float(bar["CloseAsk"]); bid_c = float(bar["CloseBid"])
                ask_h = float(bar.get("HighAsk", ask_c))
                bid_l = float(bar.get("LowBid",  bid_c))
                ask_o = float(bar.get("OpenAsk", ask_c))
                bid_o = float(bar.get("OpenBid", bid_c))
                o = (ask_o + bid_o) / 2
                h = (ask_h + float(bar.get("HighBid", ask_h))) / 2
                l = (bid_l + float(bar.get("LowAsk",  bid_l))) / 2
                c = (ask_c + bid_c) / 2
                v = 0
            else:
                continue
            if None not in (o, h, l, c) and float(c) > 0:
                rows.append({"Open": float(o), "High": float(h),
                             "Low":  float(l), "Close": float(c),
                             "Volume": float(v)})
        if len(rows) >= MIN_BARS:
            return pd.DataFrame(rows)
        logger.debug(f"UIC {uic}: only {len(rows)} bars (need {MIN_BARS})")
        return None
    except Exception as exc:
        logger.warning(f"Chart fetch failed for UIC {uic} ({asset_type}): {exc}")
        return None


def _live_price(uic: int, asset_type: str, account_key: str) -> float | None:
    """Current mid price via /trade/v1/infoprices."""
    try:
        params = {"Uic": uic, "AssetType": asset_type, "FieldGroups": "Quote"}
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
    # Persistent CSV — never truncated
    trade_logger.log_trade(
        module     = "futures",
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


# ── Strategy dispatch helpers ──────────────────────────────────────────────

def _run_strategy_exits(strat_name: str, strat_mod, positions: dict,
                        market_data: dict, universe: dict,
                        equity: float, akey: str,
                        today_str: str, dry_run: bool) -> int:
    """Process exits for one strategy's positions. Returns exit count."""
    exits = 0
    prefix = f"{strat_name}:"

    for key in list(positions):
        if not key.startswith(prefix):
            continue
        sym     = key[len(prefix):]
        pos     = positions[key]
        df      = market_data.get(sym)
        ed      = pos.get("entry_date", today_str)
        cal_days = (date.today() - date.fromisoformat(ed)).days

        # Trail stop before checking exit
        if df is not None:
            atr_now  = float(strat_mod._atr(df["High"], df["Low"], df["Close"]).iloc[-1])
            cur_stop = float(pos.get("stop_price", 0))
            new_stop = strat_mod.trailing_stop_update(
                cur_stop, float(df["Close"].iloc[-1]), atr_now, pos.get("direction", "Buy"))
            if round(new_stop, 6) != round(cur_stop, 6) and new_stop > 0:
                pos["stop_price"] = round(new_stop, 6)

        exit_flag, reason = strat_mod.should_exit(pos, df, cal_days)
        if not exit_flag:
            continue

        info       = universe.get(sym, {})
        uic        = info.get("uic") or pos.get("uic")
        asset_type = info.get("asset_type") or pos.get("asset_type", "CfdOnIndex")
        qty        = pos.get("quantity", 1)
        direction  = pos.get("direction", "Buy")
        is_long    = direction == "Buy"
        close_side = "Sell" if is_long else "Buy"

        live_px = _live_price(uic, asset_type, akey) if uic else None
        live_px = live_px or float(pos.get("entry_price", 0))
        entry   = float(pos.get("entry_price", 0))
        raw_pnl = (live_px - entry) if is_long else (entry - live_px)
        pnl_pct = raw_pnl / entry * 100 if entry else 0.0

        order = {
            "AccountKey": akey, "Uic": uic, "AssetType": asset_type,
            "Amount": qty, "BuySell": close_side,
            "OrderType": "Market",
            "OrderDuration": {"DurationType": "DayOrder"},
            "ManualOrder": False,
        }
        tag = "L" if is_long else "S"
        if dry_run:
            logger.info(f"[DRY][{strat_name}] {close_side} {qty}x {sym}[{tag}] "
                        f"— {reason} @ ~{live_px:.4f}  P&L {pnl_pct:+.1f}%")
        else:
            resp = _post("/trade/v2/orders", order)
            logger.info(f"[{strat_name}] {close_side} {resp.get('OrderId','?')}: "
                        f"{qty}x {sym}[{tag}] — {reason} @ ~{live_px:.4f}  P&L {pnl_pct:+.1f}%")

        _log_order({"strategy": strat_name, "side": close_side, "symbol": sym,
                    "uic": uic, "quantity": qty, "position_direction": direction,
                    "exit_price": live_px, "reason": reason,
                    "pnl_pct": round(pnl_pct, 2), "dry_run": dry_run})
        if not dry_run:
            pnl_tracker.log_close("futures", sym, live_px, reason, strategy=strat_name)
        del positions[key]
        exits += 1
    return exits


def _run_strategy_entries(strat_name: str, strat_mod, positions: dict,
                          market_data: dict, universe: dict,
                          equity: float, akey: str,
                          today_str: str, dry_run: bool) -> int:
    """Process entries for one strategy. Returns entry count."""
    prefix    = f"{strat_name}:"
    max_slots = SLOTS_PER_STRATEGY[strat_name]
    open_in_strat = {k[len(prefix):] for k in positions if k.startswith(prefix)}
    slots_free = max_slots - len(open_in_strat)
    if slots_free <= 0 or equity <= 0:
        return 0

    signals = strat_mod.generate_signals(market_data, open_symbols=open_in_strat)
    signals = [s for s in signals if s["symbol"] in universe]
    entries = 0

    for sig in signals[:slots_free]:
        sym        = sig["symbol"]
        info       = universe[sym]
        uic        = info["uic"]
        asset_type = info["asset_type"]
        contract_size = info.get("contract_size", 1)
        qty        = strat_mod.size_position(equity, sig["atr"], contract_size)
        direction  = sig["direction"]

        order = {
            "AccountKey": akey, "Uic": uic, "AssetType": asset_type,
            "Amount": qty, "BuySell": direction,
            "OrderType": "Market",
            "OrderDuration": {"DurationType": "DayOrder"},
            "ManualOrder": False,
        }
        tag = "LONG" if direction == "Buy" else "SHORT"
        if dry_run:
            logger.info(f"[DRY][{strat_name}] {direction} {qty}x {sym}[{tag}] "
                        f"@ ~{sig['close']:.4f}  stop={sig['stop_price']:.4f}")
            _log_order({"strategy": strat_name, "side": direction, "symbol": sym,
                        "uic": uic, "quantity": qty, "entry_price": sig["close"],
                        "stop_price": sig["stop_price"], "dry_run": True})
            entries += 1
            continue  # DRY: do not write to state or post order

        try:
            resp = _post("/trade/v2/orders", order)
        except requests.exceptions.HTTPError as _err:
            _sc = _err.response.status_code if _err.response is not None else 0
            try:
                _body = _err.response.json() if _err.response is not None else {}
            except Exception:
                _body = {}
            _ec   = (_body.get("ErrorInfo") or {}).get("ErrorCode", "")
            _msg  = (_body.get("ErrorInfo") or {}).get("Message", "") or _body.get("Message", "")
            if _sc == 403:
                if _ec == "NotAllowedForApplication":
                    logger.warning(f"[{strat_name}] SKIP {sym}: CME ContractFutures not enabled on this SIM app")
                else:
                    logger.warning(f"[{strat_name}] SKIP {sym}: 403 Forbidden — {_ec} {_msg}")
                continue
            if _sc == 409:
                logger.warning(f"[{strat_name}] SKIP {sym}: 409 Conflict — {_ec} {_msg} (position/order already exists?)")
                continue
            if _sc == 400 and "OrderSizeGreaterThanMaximumAllowed" in _ec:
                logger.warning(f"[{strat_name}] SKIP {sym}: 400 order size {qty} exceeds broker max — reduce sizing")
                continue
            raise
        logger.info(f"[{strat_name}] {direction} {resp.get('OrderId','?')}: "
                    f"{qty}x {sym}[{tag}] @ ~{sig['close']:.4f}  stop={sig['stop_price']:.4f}")

        positions[f"{strat_name}:{sym}"] = {
            "uic":          uic,
            "asset_type":   asset_type,
            "direction":    direction,
            "entry_price":  sig["close"],
            "stop_price":   sig["stop_price"],
            "quantity":     qty,
            "entry_date":   today_str,
            "atr_at_entry": sig["atr"],
            "strategy":     strat_name,
        }
        oid = resp.get("OrderId")
        _log_order({"strategy": strat_name, "side": direction, "symbol": sym,
                    "uic": uic, "quantity": qty, "entry_price": sig["close"],
                    "stop_price": sig["stop_price"], "dry_run": False})
        if True:  # always true now (dry_run path has already continued)
            pnl_tracker.log_open("futures", strat_name, sym, direction, qty,
                                 sig["close"], sig["stop_price"],
                                 order_id=oid, timestamp=today_str)
        entries += 1
    return entries


# ── Core run ───────────────────────────────────────────────────────────────

def run_daily(dry_run: bool = True,
              active_strategies: list | None = None) -> dict:
    """Execute one daily futures cycle across all (or selected) strategies."""
    if active_strategies is None:
        active_strategies = list(STRATEGIES.keys())

    mode = "DRY-RUN" if dry_run else "LIVE (Saxo SIM)"
    strat_label = "+".join(active_strategies)
    logger.info(f"{'='*60}")
    logger.info(f"  Futures Runner [{strat_label}] — {mode}  "
                f"{datetime.now().strftime('%Y-%m-%d %H:%M')}")
    logger.info(f"{'='*60}")

    universe     = load_universe()
    state        = _load_state()
    positions    = state.setdefault("positions", {})

    # Migrate old-format keys (plain "GC") to new format ("donchian:GC")
    old_keys = [k for k in positions if ":" not in k]
    for k in old_keys:
        positions[f"donchian:{k}"] = positions.pop(k)

    equity, akey = _account()

    total_slots = sum(SLOTS_PER_STRATEGY[s] for s in active_strategies)
    logger.info(f"Account equity : {equity:,.0f}")
    logger.info(f"Open positions : {len(positions)} / {total_slots} total slots")
    logger.info(f"Markets tracked: {len(universe)}")
    logger.info(f"Strategies     : {strat_label}")

    # ── Fetch history for all markets (once, shared by all strategies) ────
    market_data: dict[str, pd.DataFrame | None] = {}
    for sym, info in universe.items():
        market_data[sym] = _fetch_history(info["uic"], info["asset_type"])

    today_str    = date.today().isoformat()
    total_exits  = 0
    total_entries = 0

    for name in active_strategies:
        mod = STRATEGIES[name]
        logger.info(f"{'─'*60}")
        logger.info(f"  Strategy: {name.upper()}")

        exits   = _run_strategy_exits(name, mod, positions, market_data,
                                      universe, equity, akey, today_str, dry_run)
        entries = _run_strategy_entries(name, mod, positions, market_data,
                                        universe, equity, akey, today_str, dry_run)
        if entries == 0:
            open_in = sum(1 for k in positions if k.startswith(f"{name}:"))
            logger.info(f"  [{name}] No signals today  |  Holding: {open_in}")

        total_exits   += exits
        total_entries += entries
    # ── Final summary ─────────────────────────────────────────────────────
    logger.info(f"{'='*60}")
    logger.info(f"  TOTAL — Exits: {total_exits}  |  Entries: {total_entries}  "
                f"|  Holding: {len(positions)}")
    for key, pos in positions.items():
        strat, sym = key.split(":", 1) if ":" in key else ("donchian", key)
        df       = market_data.get(sym)
        cur_px   = float(df["Close"].iloc[-1]) if df is not None else pos["entry_price"]
        is_long  = pos.get("direction", "Buy") == "Buy"
        raw_pnl  = (cur_px - pos["entry_price"]) if is_long else (pos["entry_price"] - cur_px)
        pnl_pct  = raw_pnl / pos["entry_price"] * 100
        held     = (date.today() - date.fromisoformat(pos["entry_date"])).days
        tag      = "L" if is_long else "S"
        logger.info(f"  HOLD [{strat}] {sym}[{tag}]  qty={pos['quantity']}  "
                    f"entry={pos['entry_price']:.4f}  now={cur_px:.4f}  "
                    f"P&L {pnl_pct:+.1f}%  stop={pos['stop_price']:.4f}  {held}d")
    logger.info(f"{'='*60}")

    state["last_run"] = datetime.now().isoformat()
    _save_state(state)

    return {
        "exits":    total_exits,
        "entries":  total_entries,
        "holding":  len(positions),
        "dry_run":  dry_run,
    }


# ── CLI ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Futures multi-strategy runner")
    ap.add_argument("--live",     action="store_true",
                    help="Place real orders in Saxo SIM (default: dry-run)")
    ap.add_argument("--strategy", default="all",
                    choices=["all"] + list(STRATEGIES.keys()),
                    help="Which strategy to run (default: all)")
    ap.add_argument("--discover", action="store_true",
                    help="(Re)discover instrument UICs from Saxo and exit")
    ap.add_argument("--status",   action="store_true",
                    help="Print open positions and exit (no orders)")
    ap.add_argument("--scan",     action="store_true",
                    help="Show all-strategy snapshot for all markets")
    args = ap.parse_args()

    if args.discover:
        logger.info("Discovering futures instrument UICs from Saxo SIM...")
        from futures.universe import discover_uics, UIC_CACHE
        import json as _json

        def _get_fn(path, params=None):
            return _get(path, params)

        universe = discover_uics(_get_fn)
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(UIC_CACHE, "w") as f:
            _json.dump(universe, f, indent=2)
        print(f"\nDiscovered {len(universe)} markets:")
        for sym, info in universe.items():
            print(f"  {sym:<6}  UIC={info['uic']:<12}  {info['asset_type']:<20}  "
                  f"{info['description']}")
        sys.exit(0)

    if args.status:
        state     = _load_state()
        positions = state.get("positions", {})
        print(f"\nFutures open positions ({len(positions)}):")
        if not positions:
            print("  None")
        for key, pos in positions.items():
            strat, sym = key.split(":", 1) if ":" in key else ("donchian", key)
            held = (date.today() - date.fromisoformat(pos["entry_date"])).days
            tag  = "L" if pos.get("direction", "Buy") == "Buy" else "S"
            print(f"  [{strat}] {sym:<6}[{tag}]  qty={pos['quantity']}  "
                  f"entry={pos['entry_price']:.4f}  "
                  f"stop={pos['stop_price']:.4f}  {held}d")
        sys.exit(0)

    if args.scan:
        from futures.strategy import BREAKOUT_PERIOD
        universe = load_universe()
        print(f"\nFetching data for {len(universe)} markets...")
        market_data = {}
        for sym, info in universe.items():
            market_data[sym] = _fetch_history(info["uic"], info["asset_type"])

        # ── Donchian scan ──────────────────────────────────────────────────
        print(f"\n[DONCHIAN] 30-day breakout levels")
        print(f"  {'Sym':<6} {'Price':>10} {'30d-Hi':>10} {'30d-Lo':>10} "
              f"{'Gap%':>7}  Signal")
        print("  " + "-" * 58)
        from futures.strategy import _atr as _atr_d
        for sym, df in market_data.items():
            if df is None:
                print(f"  {sym:<6}  no data"); continue
            c = df["Close"].dropna(); h = df["High"].dropna(); l = df["Low"].dropna()
            px    = float(c.iloc[-1])
            hi30  = float(c.iloc[-(BREAKOUT_PERIOD + 1):-1].max())
            lo30  = float(c.iloc[-(BREAKOUT_PERIOD + 1):-1].min())
            atr_v = float(_atr_d(h, l, c).iloc[-1])
            gap   = (px / hi30 - 1) * 100
            flag  = "BREAKOUT!" if px > hi30 else ("SHORT BREAK!" if px < lo30 else f"{gap:+.1f}%")
            print(f"  {sym:<6} {px:>10.4f} {hi30:>10.4f} {lo30:>10.4f} "
                  f"{gap:>7.1f}%  {flag}")

        # ── RSI scan ───────────────────────────────────────────────────────
        print(f"\n[RSI] Pullback signals (RSI5 vs 50d SMA trend)")
        print(f"  {'Sym':<6} {'Price':>10} {'RSI5':>6} {'50d-SMA':>10}  Trend  Status")
        print("  " + "-" * 52)
        for row in strat_rsi.scan_summary(market_data):
            sym = row["symbol"]
            if row["status"] != "ok":
                print(f"  {sym:<6}  no data"); continue
            flag = f"{'**OS**' if row['ob_flag']=='OS' else ('**OB**' if row['ob_flag']=='OB' else '      ')}"
            print(f"  {sym:<6} {row['close']:>10.4f} {row['rsi5']:>6.1f} "
                  f"{row['sma50']:>10.4f}  {row['trend']}  {flag}")

        # ── EMA scan ───────────────────────────────────────────────────────
        print(f"\n[EMA] 5/20 crossover + ADX(14)")
        print(f"  {'Sym':<6} {'Price':>10} {'EMA5':>10} {'EMA20':>10} "
              f"{'Gap%':>7} {'ADX':>6}  Trend / ADX")
        print("  " + "-" * 68)
        for row in strat_ema.scan_summary(market_data):
            sym = row["symbol"]
            if row["status"] != "ok":
                print(f"  {sym:<6}  no data"); continue
            adx_lbl = "TREND" if row["adx_ok"] else "range"
            print(f"  {sym:<6} {row['close']:>10.4f} {row['fast_ema']:>10.4f} "
                  f"{row['slow_ema']:>10.4f} {row['gap_pct']:>7.2f}% "
                  f"{row['adx']:>6.1f}  {row['trend']} / {adx_lbl}")

        # ── MACD scan ──────────────────────────────────────────────────────
        print(f"\n[MACD] 12/26/9 momentum crossover + ADX(14)")
        print(f"  {'Sym':<6} {'Price':>10} {'MACD':>9} {'Signal':>9} {'Hist':>8} {'ADX':>6}  Zone")
        print("  " + "-" * 60)
        for row in strat_macd.scan_summary(market_data):
            sym = row["symbol"]
            if row["status"] != "ok":
                print(f"  {sym:<6}  no data"); continue
            zone = "BULL" if row["macd"] > 0 else "bear"
            hist_mark = "+" if row["hist"] > 0 else "-"
            print(f"  {sym:<6} {row['close']:>10.4f} {row['macd']:>9.4f} "
                  f"{row['signal']:>9.4f} {row['hist']:>7.4f}{hist_mark} "
                  f"{row['adx']:>6.1f}  {zone}")

        # ── Squeeze scan ────────────────────────────────────────────────────
        print(f"\n[SQUEEZE] Bollinger Band Squeeze (BB inside Keltner)")
        print(f"  {'Sym':<6} {'Price':>10} {'BB-Width':>9} {'Momentum':>10}  Squeeze")
        print("  " + "-" * 52)
        for row in strat_squeeze.scan_summary(market_data):
            sym = row["symbol"]
            if row["status"] != "ok":
                print(f"  {sym:<6}  no data"); continue
            sq_flag = "**SQUEEZE**" if row["squeeze"] else "       off"
            mom_dir = "+" if row["momentum"] > 0 else "-"
            print(f"  {sym:<6} {row['close']:>10.4f} {row['bb_width']:>9.4f} "
                  f"{row['momentum']:>9.4f}{mom_dir}  {sq_flag}")

        # ── MA Cross scan ───────────────────────────────────────────────────
        print(f"\n[MA CROSS] SMA(50/200) Golden/Death Cross + ADX(14)")
        print(f"  {'Sym':<6} {'Price':>10} {'SMA50':>10} {'SMA200':>10} {'Gap%':>7} {'ADX':>6}  Regime")
        print("  " + "-" * 68)
        for row in strat_ma_cross.scan_summary(market_data):
            sym = row["symbol"]
            if row["status"] != "ok":
                print(f"  {sym:<6}  no data"); continue
            print(f"  {sym:<6} {row['close']:>10.4f} {row['sma50']:>10.4f} "
                  f"{row['sma200']:>10.4f} {row['gap_pct']:>7.2f}% "
                  f"{row['adx']:>6.1f}  {row['cross']}")

        sys.exit(0)

    active = list(STRATEGIES.keys()) if args.strategy == "all" else [args.strategy]
    run_daily(dry_run=not args.live, active_strategies=active)
