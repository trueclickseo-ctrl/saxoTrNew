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

# ── Logging ────────────────────────────────────────────────────────────────
_LOG_DIR  = os.path.join(_ROOT, "logs")
os.makedirs(_LOG_DIR, exist_ok=True)
_LOG_FILE = os.path.join(_LOG_DIR, f"monitor_{date.today():%Y-%m-%d}.log")
_fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S")
_fh  = logging.FileHandler(_LOG_FILE, encoding="utf-8")
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
        oid = _close_position(akey, uic, "FxSpot", qty, direction, dry_run)
        if oid is None:
            logger.error(f"[MONITOR] Close order FAILED for {strat}:{sym}")
            continue

        to_remove.append(key)
        closed += 1

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
