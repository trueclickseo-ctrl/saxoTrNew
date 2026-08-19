"""
place_all_stops.py
------------------
One-shot script: places native Saxo stop-loss orders for every open
position that does not already have one.

Run any time to ensure all positions are protected:
    python place_all_stops.py          # live — places real stop orders
    python place_all_stops.py --dry    # preview only, no orders sent

Stop price sources (in priority order):
  1. forex_state.json / futures_state.json  — ATR-based stop calculated
                                              by the strategy at entry
  2. etf_positions.json  —  entry_price × (1 - stop_loss_pct=8%)
  3. Saxo OpenPrice      —  entry_price × (1 - default_stop_pct=5%)
                            used for stocks and any position not in a
                            state file
"""

import argparse
import json
import logging
import os
import sys

import requests

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)
import saxo_auth
import saxo_order

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("place_all_stops")

BASE_URL = "https://gateway.saxobank.com/sim/openapi"
DATA_DIR = os.path.join(_ROOT, "data")

# Default stop distance when no strategy stop is available (stocks)
DEFAULT_STOP_PCT = 0.05   # 5% below entry
ETF_STOP_PCT     = 0.08   # matches etf_config.py stop_loss_pct


# ── Saxo helpers ────────────────────────────────────────────────────────────

def _hdrs():
    return {"Authorization": f"Bearer {saxo_auth.get_valid_access_token()}"}


def _get(path, params=None):
    r = requests.get(f"{BASE_URL}{path}", headers=_hdrs(), params=params, timeout=12)
    r.raise_for_status()
    return r.json()


def _post(path, body):
    r = requests.post(f"{BASE_URL}{path}", headers=_hdrs(), json=body, timeout=12)
    r.raise_for_status()
    return r.json()


def _account_key():
    try:
        info = _get("/port/v1/accounts/me")
        data = info.get("Data", info)
        acct = data[0] if isinstance(data, list) else data
        return (acct.get("AccountKey", "") if isinstance(acct, dict) else "") or ""
    except Exception:
        return ""


# Positions API uses "CdfOnEtf" but orders API requires "CfdOnEtf"
_ORDER_ASSET_TYPE: dict[str, str] = {
    "CdfOnEtf": "CfdOnEtf",
}


def _fetch_tick_sizes(uic_asset_pairs: list) -> dict:
    """
    Fetch TickSizeStopOrder for multiple instruments.
    Returns {uic: tick_size}.  Falls back to 0.01 on error.
    When TickSize is None, uses Format.Decimals to derive tick (10^-decimals).
    """
    result = {}
    # Group by asset_type, deduplicate UICs
    by_type: dict[str, list[int]] = {}
    seen = set()
    for uic, at in uic_asset_pairs:
        if (uic, at) not in seen:
            seen.add((uic, at))
            # Use normalised asset type for ref API (same as positions API)
            by_type.setdefault(at, []).append(uic)

    for asset_type, uics in by_type.items():
        try:
            data = _get("/ref/v1/instruments/details", {
                "Uics":      ",".join(str(u) for u in uics),
                "AssetType": asset_type,
            }).get("Data", [])
            for item in data:
                uic  = item.get("Uic")
                tick = item.get("TickSizeStopOrder") or item.get("TickSize")
                if tick is None:
                    decimals = (item.get("Format") or {}).get("OrderDecimals") or \
                               (item.get("Format") or {}).get("Decimals") or 2
                    tick = 10 ** (-int(decimals))
                result[uic] = float(tick)
        except Exception as exc:
            logger.warning(f"Tick size fetch failed for {asset_type} {uics}: {exc}")
            for uic in uics:
                result.setdefault(uic, 0.01)
    return result


def _round_to_tick(price: float, tick: float) -> float:
    """Round price to the nearest tick size increment."""
    if tick <= 0:
        return price
    import math
    # Number of decimal places in tick
    dp = max(0, -int(math.floor(math.log10(tick)))) if tick < 1 else 0
    rounded = round(round(price / tick) * tick, dp + 1)
    return round(rounded, dp)


# ── Stop price sources ───────────────────────────────────────────────────────

def _build_stop_map():
    """Build {uic: {stop, direction, source, key}} from all state files."""
    stop_map = {}

    # Forex + futures state files
    for fname in ("forex_state.json", "futures_state.json"):
        path = os.path.join(DATA_DIR, fname)
        if not os.path.exists(path):
            continue
        state = json.load(open(path, encoding="utf-8"))
        for key, pos in state.get("positions", {}).items():
            uic  = pos.get("uic")
            stop = pos.get("stop_price")
            if uic and stop:
                stop_map[uic] = {
                    "stop":      float(stop),
                    "direction": pos.get("direction", "Buy"),  # Buy or Sell from strategy
                    "source":    fname,
                    "key":       key,
                }

    # ETF positions (always long — Buy)
    etf_path = os.path.join(_ROOT, "saxo_etf_strategy", "data", "etf_positions.json")
    if os.path.exists(etf_path):
        etf = json.load(open(etf_path, encoding="utf-8"))
        for uic_str, pos in etf.get("positions", {}).items():
            uic   = int(uic_str)
            entry = pos.get("entry_price")
            sym   = pos.get("symbol", uic_str)
            if entry:
                stop = round(entry * (1 - ETF_STOP_PCT), 2)
                stop_map[uic] = {
                    "stop":      stop,
                    "direction": "Buy",
                    "source":    "etf_positions.json",
                    "key":       sym,
                }

    return stop_map


def _existing_stop_uics():
    """Return set of UICs that already have an open stop order on Saxo."""
    covered = set()
    try:
        orders = _get("/port/v1/orders/me").get("Data", [])
        for o in orders:
            # Orders API uses OpenOrderType, not OrderType
            otype = o.get("OpenOrderType", "") or o.get("OrderType", "") or ""
            if "Stop" in otype:
                covered.add(o.get("Uic"))
    except Exception as exc:
        logger.warning(f"Could not fetch existing orders: {exc}")
    return covered


# ── Main ────────────────────────────────────────────────────────────────────

def run(dry_run: bool = False):
    logger.info("=" * 60)
    logger.info(f"  Place-All-Stops  {'[DRY]' if dry_run else '[LIVE]'}")
    logger.info("=" * 60)

    akey     = _account_key()
    stop_map = _build_stop_map()
    covered  = _existing_stop_uics()

    logger.info(f"Stop prices loaded from state files: {len(stop_map)}")
    logger.info(f"UICs already covered by a Saxo stop: {len(covered)}")

    positions = _get("/port/v1/positions/me").get("Data", [])
    logger.info(f"Open positions on Saxo: {len(positions)}")

    # Fetch tick sizes for all positions upfront (one batch per asset type)
    tick_sizes = _fetch_tick_sizes([
        (p["PositionBase"].get("Uic"), p["PositionBase"].get("AssetType", ""))
        for p in positions if p.get("PositionBase")
    ])
    logger.info("")

    placed = 0
    skipped = 0
    failed  = 0

    for p in positions:
        pb           = p.get("PositionBase", {})
        uic          = pb.get("Uic")
        asset_type   = pb.get("AssetType", "")
        raw_qty      = float(pb.get("Amount", 0))
        qty          = int(abs(raw_qty))
        open_price   = float(pb.get("OpenPrice") or 0)
        can_be_closed = pb.get("CanBeClosed", True)

        if not uic or not qty:
            continue

        # Skip positions Saxo won't let us close (e.g., expiring futures)
        if not can_be_closed:
            logger.warning(f"  SKIP  UIC={uic:<10} {asset_type:<16} — CanBeClosed=False (near expiry)")
            skipped += 1
            continue

        # Skip if already has a stop
        if uic in covered:
            logger.info(f"  SKIP  UIC={uic:<10} {asset_type:<16} — stop already on Saxo")
            skipped += 1
            continue

        # Determine direction:
        # 1. Use state file direction (most accurate — set by strategy at entry)
        # 2. Fall back to Amount sign (negative = short)
        if uic in stop_map and "direction" in stop_map[uic]:
            buy_sell = stop_map[uic]["direction"]
        else:
            buy_sell = "Buy" if raw_qty > 0 else "Sell"

        is_long = buy_sell == "Buy"

        # Determine stop price
        if uic in stop_map:
            stop_price = stop_map[uic]["stop"]
            source     = stop_map[uic]["source"]
            label      = stop_map[uic]["key"]
        elif open_price > 0:
            pct        = ETF_STOP_PCT if asset_type == "Etf" else DEFAULT_STOP_PCT
            stop_price = round(open_price * (1 - pct) if is_long else open_price * (1 + pct), 6)
            source     = f"OpenPrice×{pct*100:.0f}%"
            label      = f"UIC={uic}"
        else:
            logger.warning(f"  SKIP  UIC={uic:<10} {asset_type:<16} — no stop price available")
            skipped += 1
            continue

        # Validate: stop must be on the correct side of entry price
        if is_long and stop_price >= open_price:
            logger.warning(f"  SKIP  {label} — stop {stop_price:.5f} >= entry {open_price:.5f} "
                           f"(stop already breached for long — close manually)")
            skipped += 1
            continue
        if not is_long and stop_price <= open_price:
            logger.warning(f"  SKIP  {label} — stop {stop_price:.5f} <= entry {open_price:.5f} "
                           f"(stop already breached for short — close manually)")
            skipped += 1
            continue

        close_side = saxo_order._close_side(buy_sell, asset_type)
        stop_type  = saxo_order._stop_type(buy_sell, asset_type)
        dur        = saxo_order._stop_duration(asset_type)
        tick       = tick_sizes.get(uic, 0.001)
        rounded    = _round_to_tick(stop_price, tick)

        slp_preview = ""
        if stop_type == "StopLimit":
            slp_v = saxo_order._stop_limit_price(rounded, close_side, asset_type)
            slp_preview = f"  slp={slp_v}"
        logger.info(
            f"  {'[DRY] ' if dry_run else ''}STOP  {label:<22} {asset_type:<16} "
            f"{buy_sell:<5} qty={qty:<10,} stop={rounded:<12} tick={tick:<8} "
            f"type={stop_type}{slp_preview}  src={source}"
        )

        if dry_run:
            placed += 1
            continue

        # Some asset types use different names in the orders API vs positions API
        order_asset_type = _ORDER_ASSET_TYPE.get(asset_type, asset_type)

        def _try_place_stop(order_price, limit_price=None):
            body = {
                "AccountKey":    akey,
                "Uic":           uic,
                "AssetType":     order_asset_type,
                "Amount":        abs(qty),
                "BuySell":       close_side,
                "OrderType":     stop_type,
                "OrderPrice":    order_price,
                "OrderDuration": dur,
                "ManualOrder":   False,
            }
            if limit_price is not None:
                body["StopLimitPrice"] = limit_price
            return _post("/trade/v2/orders", body)

        slp = saxo_order._stop_limit_price(rounded, close_side, asset_type) if stop_type == "StopLimit" else None
        try:
            resp = _try_place_stop(rounded, slp)
            oid  = resp.get("OrderId", "?")
            logger.info(f"         → OrderId={oid}")
            placed += 1
        except requests.exceptions.HTTPError as err:
            body_err = {}
            try:
                body_err = err.response.json()
            except Exception:
                pass
            # Saxo error body can be in two shapes:
            # {"ErrorInfo": {"ErrorCode": ..., "Message": ...}} or
            # {"ErrorCode": ..., "Message": ..., "ModelState": {...}}
            ei  = body_err.get("ErrorInfo") or {}
            ec  = ei.get("ErrorCode", "") or body_err.get("ErrorCode", "")
            msg = ei.get("Message", "") or body_err.get("Message", "") or str(err)
            ms  = body_err.get("ModelState", {})
            detail = "; ".join(f"{k}: {v}" for k, v in ms.items()) if ms else ""

            if ec == "OnWrongSideOfMarket" and open_price > 0:
                # Stop is already above/below market — back-calc current price from PnL,
                # then set stop 3% beyond current price to limit further loss.
                pnl = float(p.get("PositionView", {}).get("ProfitLossOnTrade") or 0)
                cur = open_price + (pnl / qty) if is_long else open_price - (pnl / qty)
                new_stop = round(cur * (0.97 if is_long else 1.03), 6)
                new_rounded = _round_to_tick(new_stop, tick)
                new_slp = saxo_order._stop_limit_price(new_rounded, close_side, asset_type) if stop_type == "StopLimit" else None
                logger.warning(
                    f"         → stop {rounded} already past market (cur≈{cur:.4f}) "
                    f"— retrying at {new_rounded} (3% from market)"
                )
                try:
                    resp2 = _try_place_stop(new_rounded, new_slp)
                    oid2  = resp2.get("OrderId", "?")
                    logger.info(f"         → OrderId={oid2}  (stop adjusted to {new_rounded})")
                    placed += 1
                except requests.exceptions.HTTPError as err2:
                    body2 = {}
                    try:
                        body2 = err2.response.json()
                    except Exception:
                        pass
                    ei2  = body2.get("ErrorInfo") or {}
                    ec2  = ei2.get("ErrorCode", "") or body2.get("ErrorCode", "")
                    msg2 = ei2.get("Message", "") or body2.get("Message", "") or str(err2)
                    logger.error(f"         → RETRY FAILED [{ec2}] {msg2}")
                    failed += 1
            else:
                logger.error(f"         → FAILED [{ec}] {msg}{(' — ' + detail) if detail else ''}")
                failed += 1

    logger.info("")
    logger.info(f"Done — placed={placed}  skipped={skipped}  failed={failed}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="Preview only, no real orders")
    args = ap.parse_args()
    run(dry_run=args.dry)
