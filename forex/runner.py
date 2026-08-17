"""
forex/runner.py
---------------
Multi-strategy daily execution runner for FX pairs.

Strategies:
  ema       — EMA(5/30) + ADX(14) crossover  (trend-following)
  rsi       — RSI(2) pullback within EMA(200) trend (mean-reversion within trend)
  donchian  — 20-day Donchian channel breakout (momentum)
  bb        — Bollinger Band(20,2) + RSI(14) mean-reversion (fade extremes)

Universe:
  7 G7 majors: EURUSD GBPUSD USDJPY AUDUSD USDCAD NZDUSD USDCHF
  5 crosses:   EURGBP EURJPY GBPJPY AUDJPY CADJPY

Usage:
    python forex/runner.py                       # all 4 strategies, dry-run
    python forex/runner.py --live                # all 4, real Saxo SIM orders
    python forex/runner.py --strategy ema        # EMA only
    python forex/runner.py --strategy rsi        # RSI only
    python forex/runner.py --strategy donchian   # Donchian only
    python forex/runner.py --strategy bb         # BB reversion only
    python forex/runner.py --scan                # 4-panel market snapshot
    python forex/runner.py --status              # open positions
    python forex/runner.py --info                # verify UICs live

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
from datetime import datetime, date

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)
sys.path.insert(0, _HERE)

import requests
import pandas as pd
import saxo_auth

from forex.universe import PAIRS, ASSET_TYPE, get_pair
import forex.strategy          as strat_ema
import forex.strategy_rsi      as strat_rsi
import forex.strategy_donchian as strat_donchian
import forex.strategy_bb       as strat_bb
import pnl_tracker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("forex.runner")

# ── Strategy registry ─────────────────────────────────────────────────────────
STRATEGIES = {
    "ema":      strat_ema,
    "rsi":      strat_rsi,
    "donchian": strat_donchian,
    "bb":       strat_bb,
}
SLOTS_PER_STRATEGY = {"ema": 4, "rsi": 4, "donchian": 4, "bb": 4}

# ── Constants ─────────────────────────────────────────────────────────────────
BASE_URL    = "https://gateway.saxobank.com/sim/openapi"
DATA_DIR    = os.path.join(_ROOT, "data")
STATE_FILE  = os.path.join(DATA_DIR, "forex_state.json")
ORDERS_FILE = os.path.join(DATA_DIR, "forex_orders.json")
CHART_BARS  = 220   # enough for EMA(200) + buffer


# ── Saxo HTTP helpers ─────────────────────────────────────────────────────────

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


# ── Account ───────────────────────────────────────────────────────────────────

def _account() -> tuple[float, str]:
    equity, key = 0.0, ""
    try:
        bal    = _get("/port/v1/balances/me")
        equity = float(bal.get("TotalValue") or bal.get("NetEquityForMargin")
                       or bal.get("CashBalance") or 0)
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
    """Fetch daily OHLC for an FxSpot instrument. Mid = (Ask+Bid)/2."""
    min_bars = max(strat.MIN_BARS for strat in STRATEGIES.values())
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
        if len(rows) >= min_bars:
            return pd.DataFrame(rows)
        logger.debug(f"UIC {uic}: only {len(rows)} bars (need {min_bars})")
        return None
    except Exception as exc:
        logger.warning(f"Chart fetch failed for UIC {uic}: {exc}")
        return None


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

        order = {"AccountKey": akey, "Uic": uic, "AssetType": ASSET_TYPE,
                 "Amount": qty, "BuySell": close_side, "OrderType": "Market",
                 "OrderDuration": {"DurationType": "DayOrder"}, "ManualOrder": False}

        tag = "L" if is_long else "S"
        if dry_run:
            logger.info(f"  [DRY] {close_side:<4} {qty:,}x {sym}[{tag}] "
                        f"({strat_name}) — {reason}  P&L {pnl_pct:+.2f}%")
        else:
            resp = _post("/trade/v2/orders", order)
            logger.info(f"  {close_side} {resp.get('OrderId','?')}: {qty:,}x {sym}[{tag}] "
                        f"({strat_name}) — {reason}  P&L {pnl_pct:+.2f}%")

        _log_order({"side": close_side, "symbol": sym, "strategy": strat_name,
                    "uic": uic, "quantity": qty, "exit_price": live_px,
                    "reason": reason, "pnl_pct": round(pnl_pct, 3), "dry_run": dry_run})
        if not dry_run:
            pnl_tracker.log_close("forex", sym, live_px, reason, strategy=strat_name)
        del positions[key]
        exits += 1
    return exits


def _run_entries(strat_name: str, strat_mod, positions: dict,
                 market_data: dict, equity: float, akey: str,
                 dry_run: bool, today_str: str) -> int:
    max_slots  = SLOTS_PER_STRATEGY[strat_name]
    prefix     = f"{strat_name}:"
    held       = sum(1 for k in positions if k.startswith(prefix))
    slots_free = max_slots - held
    if slots_free <= 0 or equity <= 0:
        return 0

    open_syms = {k.split(":", 1)[1] for k in positions if k.startswith(prefix)}
    signals   = strat_mod.generate_signals(market_data, open_symbols=open_syms)

    entries = 0
    for sig in signals[:slots_free]:
        sym       = sig["symbol"]
        pair_info = get_pair(sym)
        uic       = pair_info["uic"]
        qty       = strat_mod.size_position(equity, sig["atr"], pair_info["min_units"])
        direction = sig["direction"]

        order = {"AccountKey": akey, "Uic": uic, "AssetType": ASSET_TYPE,
                 "Amount": qty, "BuySell": direction, "OrderType": "Market",
                 "OrderDuration": {"DurationType": "DayOrder"}, "ManualOrder": False}

        tag    = "LONG" if direction == "Buy" else "SHORT"
        detail = (f"rsi={sig['rsi']:.1f}" if "rsi" in sig
                  else f"breakout={sig.get('breakout_level', 0):.5f}" if "breakout_level" in sig
                  else f"adx={sig.get('adx', 0):.1f}")
        if dry_run:
            logger.info(f"  [DRY] {direction:<4} {qty:,}x {sym}[{tag}] "
                        f"({strat_name})  @ {sig['close']:.5f}  "
                        f"stop={sig['stop_price']:.5f}  {detail}")
        else:
            resp = _post("/trade/v2/orders", order)
            logger.info(f"  {direction} {resp.get('OrderId','?')}: {qty:,}x {sym}[{tag}] "
                        f"({strat_name})  @ {sig['close']:.5f}  stop={sig['stop_price']:.5f}")

        positions[f"{strat_name}:{sym}"] = {
            "uic":          uic,
            "direction":    direction,
            "entry_price":  sig["close"],
            "stop_price":   sig["stop_price"],
            "quantity":     qty,
            "entry_date":   today_str,
            "atr_at_entry": sig["atr"],
        }
        oid = resp.get("OrderId") if not dry_run else None
        _log_order({"side": direction, "symbol": sym, "strategy": strat_name,
                    "uic": uic, "quantity": qty, "entry_price": sig["close"],
                    "stop_price": sig["stop_price"], "dry_run": dry_run})
        if not dry_run:
            pnl_tracker.log_open("forex", strat_name, sym, direction, qty,
                                 sig["close"], sig["stop_price"], order_id=oid)
        entries += 1
    return entries


# ── Main daily cycle ──────────────────────────────────────────────────────────

def run_daily(dry_run: bool = True, active_strategies: list | None = None) -> dict:
    if active_strategies is None:
        active_strategies = list(STRATEGIES)

    strat_label = "+".join(active_strategies)
    mode        = "DRY-RUN" if dry_run else "LIVE (Saxo SIM)"
    logger.info("=" * 60)
    logger.info(f"  FX Runner [{strat_label}] — {mode}  "
                f"{datetime.now().strftime('%Y-%m-%d %H:%M')}")
    logger.info("=" * 60)

    state     = _load_state()
    positions = state.setdefault("positions", {})
    equity, akey = _account()
    today_str    = date.today().isoformat()

    total_slots = sum(SLOTS_PER_STRATEGY[s] for s in active_strategies)
    logger.info(f"Account equity : {equity:,.0f}")
    logger.info(f"Open positions : {len(positions)} / {total_slots} total slots")
    logger.info(f"FX pairs tracked: {len(PAIRS)}")
    logger.info(f"Strategies     : {strat_label}")

    # ── Fetch price history once for all pairs ────────────────────────────────
    market_data: dict[str, pd.DataFrame | None] = {}
    for pair in PAIRS:
        market_data[pair["symbol"]] = _fetch_history(pair["uic"])

    # ── Run each strategy ─────────────────────────────────────────────────────
    total_exits = total_entries = 0
    for strat_name in active_strategies:
        strat_mod = STRATEGIES[strat_name]
        prefix    = f"{strat_name}:"
        holding   = sum(1 for k in positions if k.startswith(prefix))
        logger.info(f"{'─'*60}")
        logger.info(f"  Strategy: {strat_name.upper()}")

        exits   = _run_exits(strat_name, strat_mod, positions,
                             market_data, akey, dry_run, today_str)
        entries = _run_entries(strat_name, strat_mod, positions,
                               market_data, equity, akey, dry_run, today_str)

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

    state["last_run"] = datetime.now().isoformat()
    _save_state(state)
    return {"exits": total_exits, "entries": total_entries,
            "holding": len(positions), "dry_run": dry_run}


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="FX multi-strategy runner")
    ap.add_argument("--live",     action="store_true",
                    help="Place real orders in Saxo SIM (default: dry-run)")
    ap.add_argument("--strategy", default="all",
                    choices=["all", "ema", "rsi", "donchian", "bb"],
                    help="Which strategy to run (default: all)")
    ap.add_argument("--status",   action="store_true",
                    help="Print open positions and exit")
    ap.add_argument("--scan",     action="store_true",
                    help="Show 4-panel market snapshot")
    ap.add_argument("--info",     action="store_true",
                    help="Verify UICs via live Saxo quotes")
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
        sys.exit(0)

    if args.scan:
        market_data = {}
        for pair in PAIRS:
            market_data[pair["symbol"]] = _fetch_history(pair["uic"])

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
        sys.exit(0)

    active = list(STRATEGIES) if args.strategy == "all" else [args.strategy]
    run_daily(dry_run=not args.live, active_strategies=active)
