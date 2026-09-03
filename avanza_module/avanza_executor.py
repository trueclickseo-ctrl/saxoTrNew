"""
avanza_executor.py
------------------
Semi-automatic execution: reads the ATOS US Blend signal, shows what to
BUY/SELL on Avanza, asks for per-trade confirmation, then places limit orders.

Signal source priority:
  1. data/stocks_live_status.json  (live ATOS stocks scan result)
  2. data/backtest_us_signals_latest.json  (latest backtest — fallback)

Workflow called from run_avanza.py:
    run_rebalance(client, account_id, config, dry_run=False)
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from avanza import Avanza

from avanza_module import avanza_client as ac
from avanza_module import avanza_instrument_cache as ic
from avanza_module import avanza_state as state

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_SIGNAL_FILES = [
    os.path.join(_ROOT, "data", "stocks_live_status.json"),
    os.path.join(_ROOT, "data", "backtest_us_signals_latest.json"),
]


# ── Signal reading ────────────────────────────────────────────────────────────

def _load_signal() -> dict:
    """Return {tickers: [str], source: str, timestamp: str, risk_off: bool}."""
    for path in _SIGNAL_FILES:
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)

            # stocks_live_status.json format
            if "signal" in data:
                sig = data["signal"]
                tickers = sig.get("targets", [])
                # targets may be list of str or list of {ticker:...}
                if tickers and isinstance(tickers[0], dict):
                    tickers = [t.get("ticker", t.get("symbol", "")) for t in tickers]
                return {
                    "tickers":   [t for t in tickers if t],
                    "source":    os.path.basename(path),
                    "timestamp": data.get("timestamp", ""),
                    "risk_off":  sig.get("risk_off", False),
                    "reason":    sig.get("reason", ""),
                }

            # backtest_us_signals_latest.json format
            if "signals" in data or "US Blend" in str(data):
                raw = data.get("signals") or []
                # Each signal row has {ticker, strategy, ...}
                blend = [r.get("ticker", r.get("symbol", ""))
                         for r in raw
                         if r.get("strategy", "") == "US Blend" or not raw]
                if not blend:
                    blend = list({r.get("ticker", r.get("symbol", "")) for r in raw})
                return {
                    "tickers":   [t for t in blend if t],
                    "source":    os.path.basename(path),
                    "timestamp": data.get("run_date", data.get("date", "")),
                    "risk_off":  False,
                    "reason":    "backtest signal",
                }
        except Exception as exc:
            print(f"  [signal] Could not read {path}: {exc}")

    return {"tickers": [], "source": "none", "timestamp": "", "risk_off": False, "reason": ""}


# ── Action computation ────────────────────────────────────────────────────────

def compute_actions(target_tickers: list[str],
                    current_positions: list[dict],
                    budget_sek: float,
                    max_positions: int,
                    instrument_cache: dict,
                    client: "Avanza") -> list[dict]:
    """Return list of {action, ticker, order_book_id, qty, price, value_sek, reason}.

    action = "BUY" | "SELL" | "HOLD"
    """
    held = {pos["ticker"].upper(): pos for pos in current_positions}
    target_set = {t.upper() for t in target_tickers[:max_positions]}

    actions = []

    # ── SELLs: held but no longer in target ──────────────────────────────────
    for ticker, pos in held.items():
        if ticker not in target_set:
            ob_id = pos["order_book_id"]
            price = pos["current_price"] or pos["avg_price"]
            actions.append({
                "action":        "SELL",
                "ticker":        ticker,
                "order_book_id": ob_id,
                "qty":           pos["qty"],
                "price":         round(price, 2),
                "value_sek":     round(pos["value_sek"], 0),
                "reason":        "dropped from target basket",
            })

    # ── BUYs: in target but not held ─────────────────────────────────────────
    buy_count = len(target_set) - len([t for t in target_set if t in held])
    if buy_count > 0 and budget_sek > 0:
        per_pos_sek = budget_sek / max(len(target_set), 1)

        for ticker in target_tickers[:max_positions]:
            tu = ticker.upper()
            if tu in held:
                continue  # already held — HOLD, no BUY needed

            ob_id = ic.lookup(client, ticker, instrument_cache)
            if not ob_id:
                print(f"  [avanza] {ticker}: not found on Avanza — skipping")
                actions.append({
                    "action": "SKIP", "ticker": ticker,
                    "order_book_id": None, "qty": 0, "price": 0,
                    "value_sek": 0, "reason": "not found on Avanza",
                })
                continue

            price_info = ac.get_stock_price(client, ob_id)
            price = price_info["price"]
            currency = price_info["currency"]

            if price <= 0:
                print(f"  [avanza] {ticker}: no live price — skipping")
                actions.append({
                    "action": "SKIP", "ticker": ticker,
                    "order_book_id": ob_id, "qty": 0, "price": 0,
                    "value_sek": 0, "reason": "no live price",
                })
                continue

            # Sizing: per_pos_sek / price (if currency is SEK; if USD, convert approx)
            if currency == "USD":
                # Avanza ISK: orders placed in USD; sizing in SEK converted at ~10.5
                try:
                    sek_per_usd = float(os.environ.get("AVANZA_SEK_USD_RATE", "10.5"))
                except ValueError:
                    sek_per_usd = 10.5
                qty = max(1, int(per_pos_sek / (price * sek_per_usd)))
            else:
                qty = max(1, int(per_pos_sek / price))

            value_sek = round(qty * price * (sek_per_usd if currency == "USD" else 1), 0)

            # Buy limit slightly above last price to improve fill probability
            limit_price = round(price * 1.002, 2)

            actions.append({
                "action":        "BUY",
                "ticker":        ticker,
                "order_book_id": ob_id,
                "qty":           qty,
                "price":         limit_price,
                "value_sek":     value_sek,
                "reason":        "new target",
                "currency":      currency,
            })

    # ── HOLDs: in target and already held ────────────────────────────────────
    for ticker in target_tickers[:max_positions]:
        if ticker.upper() in held:
            pos = held[ticker.upper()]
            actions.append({
                "action":        "HOLD",
                "ticker":        ticker,
                "order_book_id": pos["order_book_id"],
                "qty":           pos["qty"],
                "price":         pos["current_price"],
                "value_sek":     pos["value_sek"],
                "gain_pct":      pos.get("gain_pct", 0),
                "reason":        "in target basket — no change",
            })

    return actions


# ── Interactive confirm + execute ─────────────────────────────────────────────

def _print_action_table(actions: list[dict]) -> None:
    sells = [a for a in actions if a["action"] == "SELL"]
    buys  = [a for a in actions if a["action"] == "BUY"]
    holds = [a for a in actions if a["action"] == "HOLD"]
    skips = [a for a in actions if a["action"] == "SKIP"]

    w = 68
    print("\n" + "=" * w)
    print("  AVANZA REBALANCE PLAN")
    print("=" * w)

    if sells:
        print(f"\n  SELLS ({len(sells)}):")
        print(f"  {'Ticker':<8} {'Qty':>5} {'Price':>8} {'Value SEK':>10}  Reason")
        print("  " + "-" * (w - 2))
        for a in sells:
            print(f"  {a['ticker']:<8} {a['qty']:>5} {a['price']:>8.2f} {a['value_sek']:>10,.0f}  {a['reason']}")

    if buys:
        print(f"\n  BUYS ({len(buys)}):")
        print(f"  {'Ticker':<8} {'Qty':>5} {'Limit':>8} {'~SEK':>10}  Currency")
        print("  " + "-" * (w - 2))
        for a in buys:
            ccy = a.get("currency", "USD")
            print(f"  {a['ticker']:<8} {a['qty']:>5} {a['price']:>8.2f} {a['value_sek']:>10,.0f}  {ccy}")

    if holds:
        print(f"\n  HOLDS ({len(holds)}) — no action:")
        for a in holds:
            g = a.get("gain_pct", 0)
            sign = "+" if g >= 0 else ""
            print(f"  {a['ticker']:<8} {a['qty']:>5} shares  {sign}{g:.1f}%")

    if skips:
        print(f"\n  SKIPPED (not found on Avanza):")
        for a in skips:
            print(f"  {a['ticker']:<8} — {a['reason']}")

    print("\n" + "=" * w)


def run_rebalance(client: "Avanza", account_id: str,
                  config: dict, dry_run: bool = True) -> dict:
    """Main rebalance function. Returns summary dict for status file."""

    budget_sek   = float(config.get("budget_sek", 36000))
    max_positions = int(config.get("max_positions", 10))
    min_trade_sek = float(config.get("min_trade_sek", 500))

    print(f"\n  Budget: {budget_sek:,.0f} SEK  |  Max positions: {max_positions}")

    # Load signal
    signal = _load_signal()
    print(f"  Signal source: {signal['source']}  ({signal['timestamp']})")
    if signal["risk_off"]:
        print("  *** RISK OFF — signal says hold cash, no new buys ***")

    if not signal["tickers"]:
        print("  No signal tickers found — nothing to do.")
        return {"buys": 0, "sells": 0, "skips": 0, "signal": signal}

    print(f"  Target basket ({len(signal['tickers'])} tickers): {', '.join(signal['tickers'][:max_positions])}")

    # Fetch current Avanza positions
    current_positions = ac.get_positions(client, account_id)
    print(f"  Current Avanza positions: {len(current_positions)}")

    # Load instrument cache
    cache = ic.load_cache()

    # Compute actions
    actions = compute_actions(
        target_tickers   = signal["tickers"],
        current_positions= current_positions,
        budget_sek       = budget_sek if not signal["risk_off"] else 0,
        max_positions    = max_positions,
        instrument_cache = cache,
        client           = client,
    )

    ic.save_cache(cache)

    _print_action_table(actions)

    if dry_run:
        print("\n  [DRY RUN] No orders placed. Pass --execute to place real orders.")
        summary = {"buys": 0, "sells": 0, "skips": 0,
                   "dry_run": True, "actions": actions, "signal": signal}
        state.write_status({
            "timestamp":          datetime.now().isoformat(),
            "dry_run":            True,
            "account_id":         account_id,
            "budget_sek":         budget_sek,
            "signal_source":      signal["source"],
            "signal_tickers":     signal["tickers"][:max_positions],
            "current_positions":  len(current_positions),
            "planned_buys":       len([a for a in actions if a["action"] == "BUY"]),
            "planned_sells":      len([a for a in actions if a["action"] == "SELL"]),
        })
        return summary

    # ── Semi-auto: confirm each trade ────────────────────────────────────────
    executed_buys  = 0
    executed_sells = 0
    skips          = 0

    for action in actions:
        if action["action"] not in ("BUY", "SELL"):
            continue
        if action["value_sek"] < min_trade_sek and action["action"] == "BUY":
            print(f"\n  SKIP {action['ticker']}: value {action['value_sek']:,.0f} SEK "
                  f"< min {min_trade_sek:,.0f} SEK")
            skips += 1
            continue

        side = action["action"]
        ticker = action["ticker"]
        qty = action["qty"]
        price = action["price"]
        ob_id = action["order_book_id"]

        print(f"\n  ── {side} {qty}x {ticker} @ {price:.2f}  (~{action['value_sek']:,.0f} SEK) ──")
        ans = input("  Place this order? [y/n/q=quit]: ").strip().lower()
        if ans == "q":
            print("  Stopping at user request.")
            break
        if ans != "y":
            print(f"  Skipped {ticker}.")
            skips += 1
            continue

        try:
            if side == "BUY":
                resp = ac.place_buy(client, account_id, ob_id, qty, price)
            else:
                resp = ac.place_sell(client, account_id, ob_id, qty, price)

            order_id = resp.get("orderId") if isinstance(resp, dict) else None
            status   = resp.get("orderRequestStatus", "UNKNOWN") if isinstance(resp, dict) else "UNKNOWN"
            msg      = resp.get("message", "") if isinstance(resp, dict) else str(resp)

            if status == "SUCCESS" or order_id:
                print(f"  ✓ {side} order placed — orderId={order_id}")
                if side == "BUY":
                    trade_id = state.record_order(ticker, ob_id, "BUY", qty, price,
                                                  order_id, value_sek=action["value_sek"])
                    executed_buys += 1
                    # ── Place stop-loss immediately after entry ────────────────
                    stop_pct      = float(config.get("stop_pct", 0.08))
                    slippage_pct  = float(config.get("stop_sell_slippage_pct", 0.01))
                    initial_stop  = round(price * (1 - stop_pct), 2)
                    try:
                        sl_resp   = ac.place_stop_loss(client, account_id, ob_id, qty,
                                                       initial_stop, slippage_pct)
                        sl_id     = sl_resp.get("stoplossOrderId") if isinstance(sl_resp, dict) else None
                        sl_status = sl_resp.get("status", "") if isinstance(sl_resp, dict) else ""
                        if sl_id or sl_status == "SUCCESS":
                            state.update_stop(trade_id, sl_id, initial_stop, price)
                            print(f"    ✓ Stop-loss @ {initial_stop:.2f} "
                                  f"({stop_pct*100:.0f}% below {price:.2f}) — id={sl_id}")
                        else:
                            print(f"    ⚠ Stop-loss FAILED: {sl_resp} — "
                                  f"position unprotected, place manually @ {initial_stop:.2f}")
                    except Exception as sl_exc:
                        print(f"    ⚠ Stop-loss error: {sl_exc} — "
                              f"position unprotected, place manually @ {initial_stop:.2f}")
                else:
                    state.record_close(ticker, ob_id, qty, price, order_id,
                                       pnl_sek=0.0)
                    executed_sells += 1
            else:
                print(f"  ✗ Order REJECTED: {msg} (status={status})")
                skips += 1

        except Exception as exc:
            print(f"  ✗ Order failed: {exc}")
            skips += 1

    print(f"\n  Done — {executed_buys} buy(s), {executed_sells} sell(s), {skips} skipped.")

    summary = {
        "timestamp":         datetime.now().isoformat(),
        "dry_run":           False,
        "account_id":        account_id,
        "budget_sek":        budget_sek,
        "signal_source":     signal["source"],
        "signal_tickers":    signal["tickers"][:max_positions],
        "current_positions": len(current_positions),
        "executed_buys":     executed_buys,
        "executed_sells":    executed_sells,
        "skipped":           skips,
    }
    state.write_status(summary)
    return summary


# ── Trailing stop management ──────────────────────────────────────────────────

def trail_stops(client: "Avanza", account_id: str,
                config: dict, dry_run: bool = True) -> dict:
    """Ratchet Avanza stop-loss orders upward as positions appreciate.

    For each open BUY position, computes:
        trailing_high = max(recorded high, current price)
        new_stop      = trailing_high × (1 - stop_pct)

    If new_stop > current stop by more than $0.01, cancels the existing
    stop-loss and places a fresh one. DB is updated with the new IDs.

    Returns {updated, skipped, errors}.
    """
    stop_pct     = float(config.get("stop_pct", 0.08))
    slippage_pct = float(config.get("stop_sell_slippage_pct", 0.01))
    min_move     = 0.01  # ignore sub-cent ratchets

    open_buys = state.get_open_buy_positions()
    if not open_buys:
        print("  No open buy positions to trail.")
        return {"updated": 0, "skipped": 0, "errors": 0}

    # Live prices from Avanza account positions
    live_positions = ac.get_positions(client, account_id)
    price_map  = {p["order_book_id"]: p["current_price"] for p in live_positions}
    price_map2 = {p["ticker"].upper(): p["current_price"] for p in live_positions}

    # Existing stop-losses on Avanza (to confirm IDs are still live)
    live_stops = {sl["stop_loss_id"]: sl for sl in ac.get_stop_losses(client)}

    w = 72
    print(f"\n  {'─' * w}")
    print(f"  AVANZA TRAILING STOP UPDATE  "
          f"({'DRY RUN — no changes' if dry_run else 'LIVE'})")
    print(f"  {'─' * w}")
    print(f"  {'Ticker':<8} {'Entry':>8} {'CurPrice':>9} {'TrailHigh':>10} "
          f"{'CurStop':>9} {'NewStop':>9}  Action")
    print(f"  {'─' * w}")

    updated = skipped = errors = 0

    for trade in open_buys:
        ticker    = trade.get("ticker", "?")
        ob_id     = trade.get("order_book_id", "")
        qty       = int(trade.get("qty") or 0)
        trade_id  = trade["id"]

        entry_price  = float(trade.get("entry_price") or trade.get("price") or 0)
        trail_high   = float(trade.get("trailing_stop_high") or entry_price)
        cur_stop     = float(trade.get("stop_price") or (entry_price * (1 - stop_pct)))
        old_sl_id    = trade.get("stop_order_id")

        # Resolve live price — prefer order_book_id, fall back to ticker symbol
        cur_price = price_map.get(ob_id) or price_map2.get(ticker.upper()) or 0.0
        if not cur_price:
            print(f"  {ticker:<8} — no live price, skipping")
            skipped += 1
            continue

        new_high = max(trail_high, cur_price)
        new_stop = round(new_high * (1 - stop_pct), 2)

        # Determine action label
        if new_stop > cur_stop + min_move:
            action = "RATCHET UP"
        elif not old_sl_id or (old_sl_id and old_sl_id not in live_stops):
            action = "PLACE MISSING"
        else:
            action = "hold"

        print(f"  {ticker:<8} {entry_price:>8.2f} {cur_price:>9.2f} "
              f"{new_high:>10.2f} {cur_stop:>9.2f} {new_stop:>9.2f}  {action}")

        if action == "hold":
            skipped += 1
            continue

        if dry_run:
            continue

        # ── Cancel existing stop if present and still live ────────────────────
        if old_sl_id and old_sl_id in live_stops and live_stops[old_sl_id].get("deletable"):
            ac.delete_stop_loss(client, account_id, old_sl_id)

        # ── Place new (or first) stop ──────────────────────────────────────────
        try:
            sl_resp = ac.place_stop_loss(client, account_id, ob_id, qty,
                                         new_stop, slippage_pct)
            new_sl_id = sl_resp.get("stoplossOrderId") if isinstance(sl_resp, dict) else None
            sl_status = sl_resp.get("status", "") if isinstance(sl_resp, dict) else ""
            if new_sl_id or sl_status == "SUCCESS":
                state.update_stop(trade_id, new_sl_id, new_stop, new_high)
                print(f"    ✓ {ticker}: stop {cur_stop:.2f} → {new_stop:.2f} "
                      f"(high={new_high:.2f}) id={new_sl_id}")
                updated += 1
            else:
                print(f"    ✗ {ticker}: stop placement failed: {sl_resp}")
                errors += 1
        except Exception as exc:
            print(f"    ✗ {ticker}: {exc}")
            errors += 1

    print(f"  {'─' * w}")
    print(f"  Result: {updated} updated, {skipped} unchanged, {errors} errors")
    if dry_run and (updated + errors) > 0:
        print("  [DRY RUN] Pass --execute to apply.")
    return {"updated": updated, "skipped": skipped, "errors": errors}
