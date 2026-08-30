"""
intraday_monitor.py
--------------------
Real-time position monitor — runs every minute (via Task Scheduler) and
immediately closes any position whose stop-loss OR take-profit level is hit.

Covers: forex (FxSpot) and futures (CfdOnIndex, ContractFutures, CdfOnEtf).

Usage:
    python intraday_monitor.py           # one check, then exit (for Task Scheduler)
    python intraday_monitor.py --watch   # loop every 60s until Ctrl+C
    python intraday_monitor.py --dry     # one check, no real orders placed
"""

import ctypes as _ct
_hwnd = _ct.windll.kernel32.GetConsoleWindow()
if _hwnd: _ct.windll.user32.ShowWindow(_hwnd, 0)
del _ct, _hwnd

import argparse
import json
import logging
import os
import smtplib
import sys
import time
from datetime import datetime, date
from email.message import EmailMessage

import requests

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)

import saxo_auth
import proc_lock
import trade_logger
import pnl_tracker
import forex.universe as _fx_universe

# ── Logging ────────────────────────────────────────────────────────────────
_LOG_DIR  = os.path.join(_ROOT, "logs")
os.makedirs(_LOG_DIR, exist_ok=True)
_LOG_FILE = os.path.join(_LOG_DIR, f"monitor_{date.today():%Y-%m-%d}.log")
_fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S")
try:
    _fh = logging.FileHandler(_LOG_FILE, encoding="utf-8")
except PermissionError:
    # Today's log was created by the SYSTEM-run scheduled task; an interactive
    # user (or any non-SYSTEM process) can't append to it. Fall back to a
    # ".fallback" sibling so merely *importing* this module never crashes.
    # scheduler_watchdog already reads whichever of the two is newer.
    _fh = logging.FileHandler(_LOG_FILE + ".fallback", encoding="utf-8")
_fh.setFormatter(_fmt)
_sh  = logging.StreamHandler()
_sh.setFormatter(_fmt)
logging.basicConfig(level=logging.INFO, handlers=[_sh, _fh])
logger = logging.getLogger("monitor")

# ── Saxo ───────────────────────────────────────────────────────────────────
BASE_URL = "https://gateway.saxobank.com/sim/openapi"
DATA_DIR = os.path.join(_ROOT, "data")

FOREX_STATE   = os.path.join(DATA_DIR, "forex_state.json")
FUTURES_STATE = os.path.join(DATA_DIR, "futures_state.json")
UIC_CACHE     = os.path.join(DATA_DIR, "futures_uic_cache.json")


def _hdrs() -> dict:
    return {"Authorization": f"Bearer {saxo_auth.get_valid_access_token()}"}


def _get(path: str, params: dict | None = None) -> dict:
    for attempt in range(1, 4):
        try:
            r = requests.get(f"{BASE_URL}{path}", headers=_hdrs(),
                             params=params, timeout=12)
            r.raise_for_status()
            return r.json()
        except (requests.exceptions.SSLError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as exc:
            if attempt < 3:
                time.sleep(3 * attempt)
                continue
            raise exc


def _post(path: str, body: dict) -> dict:
    r = requests.post(f"{BASE_URL}{path}", headers=_hdrs(), json=body, timeout=12)
    r.raise_for_status()
    return r.json()


def _account_key() -> str:
    try:
        info = _get("/port/v1/accounts/me")
        data = info.get("Data", info)
        acct = data[0] if isinstance(data, list) else data
        return (acct.get("AccountKey", "") if isinstance(acct, dict) else "") or ""
    except Exception:
        return ""


# ── Live price fetch ───────────────────────────────────────────────────────

def _fx_price(uic: int, akey: str) -> float | None:
    """Mid price for FxSpot (forex pairs). Works on SIM."""
    try:
        params = {"Uic": uic, "AssetType": "FxSpot", "FieldGroups": "Quote"}
        if akey:
            params["AccountKey"] = akey
        q = _get("/trade/v1/infoprices", params).get("Quote", {})
        bid, ask = q.get("Bid"), q.get("Ask")
        if bid and ask:
            return (float(bid) + float(ask)) / 2
        mid = q.get("Mid")
        return float(mid) if mid else None
    except Exception:
        return None


def _saxo_positions_by_uic() -> dict:
    """Fetch all open Saxo positions keyed by UIC.
    Returns {uic: {pnl, current_price, qty, side}}
    """
    result = {}
    try:
        for p in _get("/port/v1/positions/me").get("Data", []):
            pb  = p.get("PositionBase", {})
            pv  = p.get("PositionView", {})
            uic = pb.get("Uic")
            if uic is not None:
                result[uic] = {
                    "pnl":           pv.get("ProfitLossOnTrade"),
                    "current_price": pv.get("CurrentPrice") or 0,
                    "qty":           pb.get("Amount", 0),
                    "side":          pb.get("BuySell", "Buy"),
                }
    except Exception as exc:
        logger.warning(f"Could not fetch Saxo positions: {exc}")
    return result


# ── Real (cost-netted, currency-converted) P&L for a forex close ───────────
# 2026-08-28 fix: _check_forex()'s close-logging used to call
# pnl_tracker.log_close("forex", ...) with no fx_rate_to_base and no cost
# override, so it fell through to log_close()'s default raw*qty computation
# -- unconverted quote-currency P&L (a JPY pair's raw number treated as if
# it were EUR) with zero Saxo cost netting. Confirmed empirically: ~15% of
# all SIM forex closed trades in the ledger went through this exact path
# (their exit_reason matches this module's own "STOP-LOSS hit @.../
# TAKE-PROFIT hit @..." wording). forex/runner.py's own should_exit()-driven
# closes never had this problem -- they already call
# _position_net_pnl_quote_ccy() + _eur_per_unit() before logging (see that
# function's docstring in forex/runner.py). These two helpers are that same
# logic, reimplemented locally against THIS module's own _get()/_fx_price()
# rather than importing all of forex/runner.py into an every-1-minute
# script -- intraday_monitor.py is SIM-only regardless (see FOREX_STATE
# above), matching forex.runner's own default ACCOUNT_ENV.

_EUR_RATE_CACHE: dict[str, float] = {}


def _position_net_pnl_quote_ccy(uic: int, qty: float, direction: str,
                                entry_price: float) -> float | None:
    """Authoritative NET realized P&L in the position's own QUOTE currency
    -- straight mirror of forex/runner.py's function of the same name (see
    that docstring for why ProfitLossOnTrade + TradeCostsTotal, never the
    "...InBaseCurrency" fields, and why matching by qty+entry_price rather
    than just UIC, since multiple strategies can share a UIC)."""
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
    pnl = pv.get("ProfitLossOnTrade")
    costs = pv.get("TradeCostsTotal") or 0.0
    return (pnl + costs) if pnl is not None else None


def _eur_per_unit(ccy: str, akey: str) -> float | None:
    """EUR value of one unit of `ccy`, from Saxo's own live quotes --
    straight mirror of forex/runner.py's function of the same name (EUR{ccy}
    direct if traded, else USD{ccy}+EURUSD triangulation), using this
    module's own _fx_price() instead of forex.runner's price-fetch path."""
    if ccy == "EUR":
        return 1.0
    if ccy in _EUR_RATE_CACHE:
        return _EUR_RATE_CACHE[ccy]
    def _pair(sym: str):
        try:
            return _fx_universe.get_pair(sym)   # raises KeyError if unknown
        except KeyError:
            return None

    rate = None
    direct = _pair(f"EUR{ccy}")
    if direct is not None:
        px = _fx_price(direct["uic"], akey)
        if px and px > 0:
            rate = 1.0 / px
    else:
        usd_leg = _pair(f"USD{ccy}")
        eur_usd = _pair("EURUSD")
        if usd_leg is not None and eur_usd is not None:
            px_usd_ccy = _fx_price(usd_leg["uic"], akey)
            px_eur_usd = _fx_price(eur_usd["uic"], akey)
            if px_usd_ccy and px_usd_ccy > 0 and px_eur_usd and px_eur_usd > 0:
                rate = 1.0 / (px_usd_ccy * px_eur_usd)
    if rate is None:
        logger.warning(f"Saxo has no live quote for {ccy} right now -- treating as unknown")
        return None
    _EUR_RATE_CACHE[ccy] = rate
    return rate


def _futures_live_price(uic: int, asset_type: str, entry: float,
                        qty: int, direction: str,
                        saxo_pos: dict, contract_size: float = 1) -> float | None:
    """Back-calculate futures price from Saxo PnL (SIM doesn't stream non-FX prices)."""
    sp = saxo_pos.get(uic)
    if sp is None:
        return None
    cpx = sp.get("current_price") or 0
    if cpx > 0:
        return round(cpx, 6)
    pnl = sp.get("pnl")
    if pnl is None or not entry or not qty:
        return None
    # ContractFutures PnL is in account currency → divide by contract_size
    # CfdOnIndex / CdfOnEtf PnL is in price-unit × qty equivalents
    cs = contract_size if asset_type == "ContractFutures" else 1
    divisor = qty * cs
    if direction == "Buy":
        return round(entry + pnl / divisor, 6)
    return round(entry - pnl / divisor, 6)


# ── Email alert ────────────────────────────────────────────────────────────

def _send_alert(subject: str, body: str) -> None:
    try:
        cfg_path = os.path.join(_ROOT, "config", "email.json")
        if not os.path.exists(cfg_path):
            return
        cfg = json.load(open(cfg_path, encoding="utf-8"))
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"]    = cfg["sender_email"]
        msg["To"]      = cfg["recipient_email"]
        msg.set_content(body)
        with smtplib.SMTP(cfg["smtp_host"], int(cfg["smtp_port"])) as s:
            s.starttls()
            s.login(cfg["sender_email"], cfg["sender_password"])
            s.send_message(msg)
        logger.info(f"[alert] Email sent: {subject}")
    except Exception as exc:
        logger.warning(f"[alert] Email failed: {exc}")


# ── State helpers ──────────────────────────────────────────────────────────

def _load_state(path: str) -> dict:
    if not os.path.exists(path):
        return {"positions": {}}
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return {"positions": {}}


def _save_state(path: str, state: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def _load_uic_cache() -> dict:
    if not os.path.exists(UIC_CACHE):
        return {}
    return json.load(open(UIC_CACHE, encoding="utf-8"))


# ── Close order helper ─────────────────────────────────────────────────────

def _close_position(akey: str, uic: int, asset_type: str,
                    qty: int, direction: str, dry_run: bool) -> str | None:
    """Place a market close order. Returns OrderId or None."""
    close_side = "Sell" if direction == "Buy" else "Buy"
    order = {
        "AccountKey":    akey,
        "Uic":           uic,
        "AssetType":     asset_type,
        "Amount":        qty,
        "BuySell":       close_side,
        "OrderType":     "Market",
        "OrderDuration": {"DurationType": "DayOrder"},
        "ManualOrder":   False,
    }
    if dry_run:
        return "DRY"
    try:
        resp = _post("/trade/v2/orders", order)
        return resp.get("OrderId", "?")
    except requests.exceptions.HTTPError as err:
        logger.error(f"Close order failed: {err.response.text if err.response else err}")
        return None


# ── Core monitor logic ─────────────────────────────────────────────────────

def _check_forex(akey: str, dry_run: bool) -> int:
    """Check forex positions. Returns number of positions closed."""
    state     = _load_state(FOREX_STATE)
    positions = state.get("positions", {})
    if not positions:
        return 0

    closed = 0
    to_remove = []
    now = datetime.now().strftime("%H:%M:%S")

    for key, pos in positions.items():
        strat, sym = key.split(":", 1) if ":" in key else ("?", key)
        uic       = pos.get("uic")
        direction = pos.get("direction", "Buy")
        entry     = float(pos.get("entry_price", 0))
        stop      = float(pos.get("stop_price", 0))
        target    = pos.get("gap_target")        # take-profit for gap strategy
        qty       = int(pos.get("quantity", 0))
        is_long   = direction == "Buy"

        if not uic or not qty:
            continue

        live = _fx_price(uic, akey)
        if live is None:
            logger.debug(f"[{strat}:{sym}] no live price, skipping")
            continue

        pnl_pct = ((live - entry) / entry * 100) if is_long else ((entry - live) / entry * 100)
        reason  = None

        # Stop-loss hit
        if stop > 0:
            if is_long and live <= stop:
                reason = f"STOP-LOSS hit @ {live:.5f} (stop={stop:.5f})"
            elif not is_long and live >= stop:
                reason = f"STOP-LOSS hit @ {live:.5f} (stop={stop:.5f})"

        # Take-profit hit (gap strategy gap_target)
        if reason is None and target:
            tgt = float(target)
            if is_long and live >= tgt:
                reason = f"TAKE-PROFIT hit @ {live:.5f} (target={tgt:.5f})"
            elif not is_long and live <= tgt:
                reason = f"TAKE-PROFIT hit @ {live:.5f} (target={tgt:.5f})"

        if reason is None:
            logger.debug(f"[{strat}:{sym}] {'LONG' if is_long else 'SHORT'} "
                         f"live={live:.5f} stop={stop:.5f} pnl={pnl_pct:+.3f}% — OK")
            continue

        logger.info(f"[MONITOR] CLOSE {strat}:{sym} — {reason}  pnl={pnl_pct:+.2f}%")

        # 2026-08-28 fix: snapshot Saxo's own net (price + cost) P&L for this
        # position BEFORE closing it -- same reasoning as forex/runner.py's
        # should_exit()-driven closes ("captured while the position still
        # exists to look up") -- /port/v1/positions/me can't find it anymore
        # once _close_position() below actually closes it.
        net_pnl_quote = None if dry_run else _position_net_pnl_quote_ccy(uic, qty, direction, entry)

        oid = _close_position(akey, uic, "FxSpot", qty, direction, dry_run)
        if oid is None:
            logger.error(f"[MONITOR] Close order FAILED for {strat}:{sym}")
            continue

        to_remove.append(key)
        closed += 1

        # 2026-08-25: same fix as _check_futures() below -- this close
        # previously only removed the local position and emailed, never
        # logged to trade_logger/pnl_tracker. FOREX_STATE is always SIM's
        # file (this monitor doesn't know about --account live at all yet
        # -- forex_live_state.json is only touched by the two dedicated
        # daily LIVE tasks), so "forex" is always the right module here.
        close_side = "Sell" if is_long else "Buy"
        if not dry_run:
            try:
                trade_logger.log_trade(
                    module="forex", strategy=strat, symbol=sym, side=close_side,
                    quantity=qty, price=live, order_id=oid, dry_run=False,
                    stop_price=stop, notes=f"intraday_monitor: {reason}",
                )
                # 2026-08-28 fix: this used to call log_close() with no
                # fx_rate_to_base/cost override, so it fell through to
                # log_close()'s default raw*qty computation -- unconverted
                # quote-currency P&L with zero Saxo cost netting (confirmed
                # on ~15% of all SIM forex closed trades in the ledger).
                # Convert via this module's own _eur_per_unit(), mirroring
                # forex/runner.py's should_exit()-driven close path exactly.
                quote_ccy = sym[3:6] if len(sym) >= 6 else ""
                fx_rate = _eur_per_unit(quote_ccy, akey)
                if fx_rate is None:
                    logger.warning(f"[MONITOR] No Saxo rate for {quote_ccy} AND no Saxo "
                                    f"position P&L for {sym} -- realized P&L for this "
                                    f"close will use an unconverted 1.0 placeholder, "
                                    f"verify data/pnl_ledger.db for {sym} manually")
                    fx_rate = 1.0
                saxo_pnl_eur = (net_pnl_quote * fx_rate) if net_pnl_quote is not None else None
                pnl_tracker.log_close("forex", sym, live, reason, strategy=strat,
                                      asset_type="FxSpot",
                                      fx_rate_to_base=fx_rate,
                                      gross_pnl_base_override=saxo_pnl_eur)
            except Exception as exc:
                logger.warning(f"[MONITOR] trade/pnl logging failed for {strat}:{sym}: {exc}")

        subject = f"[Forex] {'🔴 STOP' if 'STOP' in reason else '🟢 TARGET'} {sym} — {strat.upper()} closed"
        body = (
            f"Position closed by intraday monitor.\n\n"
            f"Strategy  : {strat.upper()}\n"
            f"Pair      : {sym}\n"
            f"Side      : {'LONG' if is_long else 'SHORT'}\n"
            f"Reason    : {reason}\n"
            f"P&L       : {pnl_pct:+.3f}%\n"
            f"Entry     : {entry:.5f}\n"
            f"Live      : {live:.5f}\n"
            f"Order ID  : {oid}\n"
            f"Time      : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        )
        _send_alert(subject, body)

    if to_remove and not dry_run:
        for key in to_remove:
            positions.pop(key, None)
        _save_state(FOREX_STATE, state)

    return closed


def _check_futures(akey: str, dry_run: bool) -> int:
    """Check futures positions. Returns number of positions closed."""
    state     = _load_state(FUTURES_STATE)
    positions = state.get("positions", {})
    if not positions:
        return 0

    uic_cache  = _load_uic_cache()
    uic_by_sym = {v["uic"]: v for v in uic_cache.values()}
    saxo_pos   = _saxo_positions_by_uic()

    closed    = 0
    to_remove = []

    for key, pos in positions.items():
        strat, sym = key.split(":", 1) if ":" in key else ("?", key)
        uic        = pos.get("uic")
        asset_type = pos.get("asset_type", "CfdOnIndex")
        direction  = pos.get("direction", "Buy")
        entry      = float(pos.get("entry_price", 0))
        stop       = float(pos.get("stop_price", 0))
        qty        = int(pos.get("quantity", 0))
        is_long    = direction == "Buy"

        if not uic or not qty:
            continue

        cs = uic_by_sym.get(uic, {}).get("contract_size", 1)

        if asset_type == "FxSpot":
            live = _fx_price(uic, akey)
        else:
            live = _futures_live_price(uic, asset_type, entry, qty, direction,
                                       saxo_pos, contract_size=cs)

        if live is None:
            logger.debug(f"[{strat}:{sym}] no live price, skipping")
            continue

        pnl_pct = ((live - entry) / entry * 100) if is_long else ((entry - live) / entry * 100)
        reason  = None

        if stop > 0:
            if is_long and live <= stop:
                reason = f"STOP-LOSS hit @ {live:.4f} (stop={stop:.4f})"
            elif not is_long and live >= stop:
                reason = f"STOP-LOSS hit @ {live:.4f} (stop={stop:.4f})"

        if reason is None:
            logger.debug(f"[{strat}:{sym}] live={live:.4f} stop={stop:.4f} pnl={pnl_pct:+.3f}% — OK")
            continue

        logger.info(f"[MONITOR] CLOSE {strat}:{sym} — {reason}  pnl={pnl_pct:+.2f}%")
        oid = _close_position(akey, uic, asset_type, qty, direction, dry_run)
        if oid is None:
            logger.error(f"[MONITOR] Close order FAILED for {strat}:{sym}")
            continue

        to_remove.append(key)
        closed += 1

        # 2026-08-25: this close previously only removed the local position
        # and sent an email -- never logged to trade_logger/pnl_tracker, so
        # every monitor-caught stop-loss/take-profit was invisible to
        # trades_futures.csv AND every strategy-wise P&L/win-rate report
        # (found live: a real ZC stop-loss this monitor closed and emailed
        # correctly never showed up anywhere else). Mirrors exactly how
        # futures/runner.py's own exit path logs a close.
        close_side = "Sell" if is_long else "Buy"
        if not dry_run:
            try:
                trade_logger.log_trade(
                    module="futures", strategy=strat, symbol=sym, side=close_side,
                    quantity=qty, price=live, order_id=oid, dry_run=False,
                    stop_price=stop, notes=f"intraday_monitor: {reason}",
                )
                pnl_tracker.log_close("futures", sym, live, reason, strategy=strat,
                                      asset_type=asset_type, contract_size=cs)
            except Exception as exc:
                logger.warning(f"[MONITOR] trade/pnl logging failed for {strat}:{sym}: {exc}")

        subject = f"[Futures] 🔴 STOP {sym} — {strat.upper()} closed"
        body = (
            f"Futures position closed by intraday monitor.\n\n"
            f"Strategy  : {strat.upper()}\n"
            f"Market    : {sym}\n"
            f"Side      : {'LONG' if is_long else 'SHORT'}\n"
            f"Reason    : {reason}\n"
            f"P&L       : {pnl_pct:+.3f}%\n"
            f"Entry     : {entry:.4f}\n"
            f"Live      : {live:.4f}\n"
            f"Order ID  : {oid}\n"
            f"Time      : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        )
        _send_alert(subject, body)

    if to_remove and not dry_run:
        for key in to_remove:
            positions.pop(key, None)
        _save_state(FUTURES_STATE, state)

    return closed


def run_once(dry_run: bool = False) -> None:
    logger.info(f"{'='*50}")
    logger.info(f"  Intraday Monitor  {'[DRY]' if dry_run else '[LIVE]'}  "
                f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"{'='*50}")

    akey = _account_key()
    if not akey:
        logger.warning("Could not fetch AccountKey — monitor may miss some prices")

    # Cross-process locks -- this script runs as a SEPARATE process from
    # forex/runner.py and futures/runner.py, but reads/writes the exact
    # SAME forex_state.json/futures_state.json. Confirmed live 2026-08-24
    # this is a real double-entry/lost-update risk (this monitor's
    # scheduled 06:00 fire is the same instant as forex's own Intraday
    # Scan trigger, and 15 min before futures' 06:15 daily run) -- see
    # proc_lock.py. Only locked for real runs; dry_run never saves state.
    if dry_run:
        fx_closed  = _check_forex(akey, dry_run)
        fut_closed = _check_futures(akey, dry_run)
    else:
        proc_lock.acquire(proc_lock.FOREX_LOCK, "intraday_monitor")
        try:
            fx_closed = _check_forex(akey, dry_run)
        finally:
            proc_lock.release(proc_lock.FOREX_LOCK)

        proc_lock.acquire(proc_lock.FUTURES_LOCK, "intraday_monitor")
        try:
            fut_closed = _check_futures(akey, dry_run)
        finally:
            proc_lock.release(proc_lock.FUTURES_LOCK)

    total = fx_closed + fut_closed
    if total:
        logger.info(f"Monitor closed {total} position(s) "
                    f"(forex={fx_closed}, futures={fut_closed})")
    else:
        logger.info("All positions within limits — no action needed.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true",
                    help="Run continuously every 60s (Ctrl+C to stop)")
    ap.add_argument("--dry", action="store_true",
                    help="Check prices and log, but do NOT place real orders")
    args = ap.parse_args()

    if args.watch:
        logger.info("Monitor running in --watch mode (60s interval). Ctrl+C to exit.")
        while True:
            try:
                run_once(dry_run=args.dry)
            except Exception as exc:
                logger.error(f"Monitor cycle error: {exc}")
            time.sleep(60)
    else:
        run_once(dry_run=args.dry)


if __name__ == "__main__":
    main()
