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
from datetime import datetime, date

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)
sys.path.insert(0, _HERE)

import requests
import saxo_order
import pandas as pd
import saxo_auth

from forex.universe import PAIRS, ASSET_TYPE, get_pair
import forex.strategy             as strat_ema
import forex.strategy_rsi         as strat_rsi
import forex.strategy_donchian    as strat_donchian
import forex.strategy_bb          as strat_bb
import forex.strategy_pullback    as strat_pullback
import forex.strategy_gap         as strat_gap
import forex.strategy_supertrend  as strat_supertrend
import forex.strategy_zscore      as strat_zscore
import forex.strategy_ml          as strat_ml
import pnl_tracker
import trade_logger
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
    "ml":          strat_ml,
}
SLOTS_PER_STRATEGY = {
    "ema": 4, "rsi": 34, "donchian": 4, "bb": 4,
    "pullback": 34, "gap": 34,
    "supertrend": 20, "zscore": 20, "ml": 20,
}

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
BASE_URL    = "https://gateway.saxobank.com/sim/openapi"
DATA_DIR    = os.path.join(_ROOT, "data")
STATE_FILE  = os.path.join(DATA_DIR, "forex_state.json")
ORDERS_FILE = os.path.join(DATA_DIR, "forex_orders.json")
CHART_BARS  = 340   # enough for ML strategy: EMA(200) + 126 lookback + 14 buffer


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
            # Label the signal-log outcome for ML training data
            raw_pnl = ((live_px - pos["entry_price"]) * qty if is_long
                       else (pos["entry_price"] - live_px) * qty)
            signal_filter.label_outcome(key, won=raw_pnl > 0)
        del positions[key]
        exits += 1
    return exits


def _run_entries(strat_name: str, strat_mod, positions: dict,
                 market_data: dict, equity: float, akey: str,
                 dry_run: bool, today_str: str,
                 live_prices: dict | None = None,
                 agreement: dict | None = None) -> int:
    max_slots  = SLOTS_PER_STRATEGY[strat_name]
    prefix     = f"{strat_name}:"
    held       = sum(1 for k in positions if k.startswith(prefix))
    slots_free = max_slots - held
    if slots_free <= 0 or equity <= 0:
        return 0

    open_syms = {k.split(":", 1)[1] for k in positions if k.startswith(prefix)}
    if getattr(strat_mod, "NEEDS_LIVE_PRICES", False):
        signals = strat_mod.generate_signals(market_data, open_symbols=open_syms,
                                             live_prices=live_prices or {})
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
        pair_info = get_pair(sym)
        uic       = pair_info["uic"]
        qty       = strat_mod.size_position(equity, sig["atr"], pair_info["min_units"])

        tag    = "LONG" if direction == "Buy" else "SHORT"
        detail = (f"rsi={sig['rsi']:.1f}" if "rsi" in sig
                  else f"breakout={sig.get('breakout_level', 0):.5f}" if "breakout_level" in sig
                  else f"adx={sig.get('adx', 0):.1f}")
        stop_oid = None; tp_oid = None
        agree_tag = f"  agree={agrees}/{len(STRATEGIES)}{ml_info}"
        if dry_run:
            logger.info(f"  [DRY] {direction:<4} {qty:,}x {sym}[{tag}] "
                        f"({strat_name})  @ {sig['close']:.5f}  "
                        f"stop={sig['stop_price']:.5f}  {detail}{agree_tag}")
        else:
            tp = sig.get("gap_target")   # only gap strategy provides a fixed target
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
            )
            tp_info = f"  tp_order={tp_oid}" if tp_oid else ""
            logger.info(f"  {direction} {entry_oid}: {qty:,}x {sym}[{tag}] "
                        f"({strat_name})  @ {sig['close']:.5f}  stop={sig['stop_price']:.5f}"
                        f"  stop_order={stop_oid}{tp_info}{agree_tag}")

        pos_key = f"{strat_name}:{sym}"
        pos_record = {
            "uic":           uic,
            "direction":     direction,
            "entry_price":   sig["close"],
            "stop_price":    sig["stop_price"],
            "quantity":      qty,
            "entry_date":    today_str,
            "atr_at_entry":  sig["atr"],
            "stop_order_id": stop_oid,
            "tp_order_id":   tp_oid if not dry_run else None,
        }
        if "gap_target" in sig:
            pos_record["gap_target"]   = sig["gap_target"]
            pos_record["friday_close"] = sig.get("friday_close", sig["gap_target"])
            pos_record["gap_pct"]      = sig.get("gap_pct", 0.0)
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
                result.add((o.get("Uic"), o.get("BuySell"), o.get("OrderType")))
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
        dp         = 3 if sym.upper().endswith("JPY") else 5
        rounded    = round(stop_price, dp)

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
    Place GTC Limit TP orders for gap positions that have a gap_target but no tp_order_id.
    Queries Saxo first to avoid duplicates.
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
        sym        = key.split(":", 1)[1] if ":" in key else key
        uic        = pos["uic"]
        direction  = pos.get("direction", "Buy")
        qty        = pos["quantity"]
        tp_price   = pos["gap_target"]
        close_side = "Sell" if direction == "Buy" else "Buy"
        dp         = 3 if sym.upper().endswith("JPY") else 5
        rounded_tp = round(tp_price, dp)

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
                               market_data, equity, akey, dry_run, today_str,
                               live_prices=live_prices, agreement=agreement)

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
                             "supertrend", "zscore", "ml"],
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

        sys.exit(0)

    active = list(STRATEGIES) if args.strategy == "all" else [args.strategy]
    if args.exits_only:
        run_exits_only(dry_run=not args.live, active_strategies=active,
                       session=args.session)
    else:
        run_daily(dry_run=not args.live, active_strategies=active,
                  session=args.session)
