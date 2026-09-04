"""
ibkr_executor.py
----------------
Strategy executors for the IBKR stocks sleeve.

Strategies:
  blend     — US cross-sectional momentum (fortnightly rebalance)
  reversion — US mean reversion (entry + exit checks, intraday variant)

Signal generation: ibkr_signals.py (Yahoo Finance only).
No Saxo imports. No Avanza imports.
"""
from __future__ import annotations

import datetime
import math
from pathlib import Path

import pandas as pd

from ibkr_module import ibkr_client as ic
from ibkr_module import ibkr_state as st
from ibkr_module import ibkr_signals as sig

_ROOT = Path(__file__).parent.parent


def _compute_plan(
    targets: list[str],
    held: list[dict],
    prices: dict[str, float],
    budget_usd: float,
    max_positions: int,
    min_trade_usd: float,
    cash_buffer_pct: float,
) -> tuple[list[dict], list[dict]]:
    """Return (buys, sells) action lists."""
    held_symbols = {p["symbol"] for p in held}
    target_set   = set(targets[:max_positions])

    # Available cash after buffer
    usable = budget_usd * (1 - cash_buffer_pct)
    per_slot = usable / max_positions

    sells = []
    for p in held:
        if p["symbol"] not in target_set:
            price = prices.get(p["symbol"], 0.0) or 0.0
            if math.isnan(price):
                price = 0.0
            sells.append({
                "symbol": p["symbol"],
                "qty":    p["qty"],
                "price":  price,
                "value":  round(price * p["qty"], 2),
            })

    buys = []
    for sym in targets[:max_positions]:
        if sym in held_symbols:
            continue
        price = prices.get(sym, 0.0)
        if not price or math.isnan(price) or price <= 0:
            continue
        qty = math.floor(per_slot / price)
        if qty < 1:
            continue
        notional = round(price * qty, 2)
        if notional < min_trade_usd:
            continue
        buys.append({
            "symbol":   sym,
            "qty":      qty,
            "price":    price,
            "notional": notional,
        })

    return buys, sells


# ── US Blend rebalance ────────────────────────────────────────────────────────

def run_rebalance(ib, account_id: str, cfg: dict, dry_run: bool = True) -> None:
    """US Blend cross-sectional momentum rebalance.
    Generates its own signal via ibkr_signals.blend_targets() (Yahoo Finance).
    """
    blend_cfg   = cfg.get("strategies", {}).get("blend", cfg["capital"])
    budget_usd  = blend_cfg.get("budget_usd",   cfg["capital"]["budget_usd"])
    max_pos     = blend_cfg.get("max_positions", cfg["capital"]["max_positions"])
    min_usd     = blend_cfg.get("min_trade_usd", cfg["capital"]["min_trade_usd"])
    buf_pct     = cfg["capital"]["cash_buffer_pct"]
    stop_pct    = blend_cfg.get("stop_pct", cfg["risk"]["stop_pct"])

    print("\n  Generating US Blend signal (Yahoo Finance)...")
    signal  = sig.blend_targets()
    targets = signal.get("targets", [])
    if not targets:
        print(f"  No targets in signal ({signal.get('reason', '')}). Nothing to do.")
        return

    print(f"  Signal targets ({len(targets)}): {', '.join(targets)}")
    if signal.get("risk_off"):
        print("  [RISK OFF] Signal is in defensive mode.")

    # Fetch live state
    held    = ic.get_positions(ib, account_id)
    symbols = list({*targets, *[p["symbol"] for p in held]})
    prices  = ic.get_prices(ib, symbols)
    cash    = ic.get_cash_balance(ib, account_id)
    print(f"  Cash available: ${cash:,.2f}")

    buys, sells = _compute_plan(
        targets       = targets,
        held          = held,
        prices        = prices,
        budget_usd    = budget_usd,
        max_positions = max_pos,
        min_trade_usd = min_usd,
        cash_buffer_pct = buf_pct,
    )

    print(f"\n  HOLD  ({len(held) - len(sells)}): "
          f"{', '.join(p['symbol'] for p in held if p['symbol'] not in {s['symbol'] for s in sells})}")
    print(f"  SELL  ({len(sells)}): {', '.join(s['symbol'] for s in sells)}")
    print(f"  BUY   ({len(buys)}):  {', '.join(b['symbol'] for b in buys)}")

    if dry_run:
        print("\n  [DRY RUN] No orders placed. Pass --execute to trade.\n")
        _print_plan(buys, sells, stop_pct)
        return

    # ── Execute SELLs ─────────────────────────────────────────────────────────
    for s in sells:
        print(f"\n  SELL {s['qty']} {s['symbol']} @ ~${s['price']:.2f}  "
              f"(value ~${s['value']:,.0f})")
        confirm = input("  Confirm? [y/N]: ").strip().lower()
        if confirm != "y":
            print("  Skipped.")
            continue

        trade = ic.place_market_order(ib, account_id, s["symbol"], "SELL", s["qty"])
        st.record_order(str(trade.order.orderId), s["symbol"], "SELL", s["qty"], strategy="blend")
        print(f"  Order placed (id={trade.order.orderId}). Waiting for fill...")
        fill = ic.confirm_fill(ib, trade)
        if fill is None:
            print(f"  WARNING: fill not confirmed within timeout for {s['symbol']}.")
            st.mark_cancelled(str(trade.order.orderId))
        else:
            print(f"  Filled @ ${fill:.4f}")
            st.mark_filled(str(trade.order.orderId), fill, side="SELL")

    # ── Execute BUYs ──────────────────────────────────────────────────────────
    for b in buys:
        stop_price = round(b["price"] * (1 - stop_pct), 2)
        print(f"\n  BUY  {b['qty']} {b['symbol']} @ ~${b['price']:.2f}  "
              f"notional ~${b['notional']:,.0f}  stop=${stop_price:.2f}")
        confirm = input("  Confirm? [y/N]: ").strip().lower()
        if confirm != "y":
            print("  Skipped.")
            continue

        trade = ic.place_market_order(ib, account_id, b["symbol"], "BUY", b["qty"])
        st.record_order(str(trade.order.orderId), b["symbol"], "BUY", b["qty"], strategy="blend")
        print(f"  Order placed (id={trade.order.orderId}). Waiting for fill...")
        fill = ic.confirm_fill(ib, trade)
        if fill is None:
            print(f"  WARNING: fill not confirmed for {b['symbol']}. Skipping stop.")
            st.mark_cancelled(str(trade.order.orderId))
            continue

        print(f"  Filled @ ${fill:.4f}")
        st.mark_filled(str(trade.order.orderId), fill, side="BUY")

        actual_stop = round(fill * (1 - stop_pct), 2)
        stop_trade  = ic.place_stop_order(ib, account_id, b["symbol"], b["qty"], actual_stop)
        ib.sleep(1.0)
        st.update_stop(b["symbol"], actual_stop,
                       str(stop_trade.order.orderId), trailing_high=fill)
        print(f"  Stop placed @ ${actual_stop:.2f} (id={stop_trade.order.orderId})")

    print("\n  Rebalance complete.")


# ── Trail stops ───────────────────────────────────────────────────────────────

def trail_stops(ib, account_id: str, cfg: dict, dry_run: bool = True) -> None:
    stop_pct = cfg["risk"]["stop_pct"]
    positions = st.get_open_positions()
    if not positions:
        print("  No open positions in ledger.")
        return

    symbols = [p["symbol"] for p in positions]
    prices  = ic.get_prices(ib, symbols)
    print(f"  Trail-stop check for {len(positions)} position(s) | stop_pct={stop_pct*100:.0f}%\n")

    for pos in positions:
        sym          = pos["symbol"]
        cur_price    = prices.get(sym, 0.0)
        cur_stop     = pos.get("stop_price") or 0.0
        trail_high   = pos.get("trailing_high") or pos.get("fill_price") or 0.0
        stop_order_id = pos.get("stop_order_id")
        qty          = int(pos["qty"])

        if cur_price <= 0:
            print(f"  {sym:<8}  no price — skipped")
            continue

        new_high = max(trail_high, cur_price)
        new_stop = round(new_high * (1 - stop_pct), 2)

        if new_stop <= cur_stop:
            print(f"  {sym:<8}  price=${cur_price:.2f}  stop=${cur_stop:.2f}  "
                  f"high=${new_high:.2f}  → no change")
            continue

        print(f"  {sym:<8}  price=${cur_price:.2f}  stop ${cur_stop:.2f} → ${new_stop:.2f}  "
              f"high=${new_high:.2f}")

        if dry_run:
            print(f"           [DRY RUN] would ratchet stop to ${new_stop:.2f}")
            continue

        # Cancel old stop, place new one
        if stop_order_id:
            open_orders = ib.openTrades()
            old_trade   = next((t for t in open_orders
                                if str(t.order.orderId) == str(stop_order_id)), None)
            if old_trade:
                ic.cancel_order(ib, old_trade)
                ib.sleep(0.5)

        new_stop_trade = ic.place_stop_order(ib, account_id, sym, qty, new_stop)
        ib.sleep(0.5)
        st.update_stop(sym, new_stop, str(new_stop_trade.order.orderId), new_high)
        print(f"           → stop updated (new id={new_stop_trade.order.orderId})")

    print("\n  Trail-stop pass complete.")


# ── US Reversion entries ───────────────────────────────────────────────────────

def run_reversion_entries(ib, account_id: str, cfg: dict, dry_run: bool = True,
                          intraday: bool = False) -> None:
    """Scan for US Reversion entry signals and buy new slots.

    intraday=True uses 5-min yfinance bars (US market hours only).
    """
    rev_cfg   = cfg["strategies"]["reversion"]
    max_slots = rev_cfg["max_slots"]
    stop_pct  = rev_cfg["stop_pct"]
    min_usd   = rev_cfg.get("min_trade_usd", 50)
    budget    = rev_cfg["budget_usd"]

    open_pos   = st.get_open_positions("reversion")
    open_syms  = {p["symbol"] for p in open_pos}
    slots_free = max_slots - len(open_pos)

    label = "intraday reversion" if intraday else "reversion"
    print(f"\n  [{label}] {len(open_pos)}/{max_slots} slots used  ({slots_free} free)")

    if slots_free <= 0:
        print("  All reversion slots full.")
        return

    candidates = sig.intraday_candidates() if intraday else sig.reversion_candidates()
    new_cands  = [c for c in candidates if c["ticker"] not in open_syms]

    if not new_cands:
        print("  No new reversion candidates.")
        return

    per_slot = budget / max_slots

    for c in new_cands[:slots_free]:
        price = c["price"]
        if not price or price <= 0:
            continue
        qty      = math.floor(per_slot / price)
        if qty < 1:
            continue
        notional = round(price * qty, 2)
        if notional < min_usd:
            continue
        stop_price = round(price * (1 - stop_pct), 2)

        print(f"\n  [{label}] BUY  {c['ticker']:<8}  "
              f"RSI={c['rsi']:.0f}  dip={c['dip_pct']}%  vol={c['vol_ratio']}x")
        print(f"    qty={qty}  price~${price:.2f}  notional~${notional:,.0f}  "
              f"stop=${stop_price:.2f}")

        if dry_run:
            print("    [DRY RUN] would place buy + stop")
            continue

        confirm = input(f"  Confirm buy {c['ticker']}? [y/N]: ").strip().lower()
        if confirm != "y":
            print("  Skipped.")
            continue

        trade = ic.place_market_order(ib, account_id, c["ticker"], "BUY", qty)
        st.record_order(str(trade.order.orderId), c["ticker"], "BUY", qty, strategy="reversion")
        print(f"  Order placed (id={trade.order.orderId}). Waiting for fill...")
        fill = ic.confirm_fill(ib, trade)
        if fill is None:
            print(f"  WARNING: fill not confirmed for {c['ticker']}.")
            st.mark_cancelled(str(trade.order.orderId))
            continue

        print(f"  Filled @ ${fill:.4f}")
        st.mark_filled(str(trade.order.orderId), fill, side="BUY")
        actual_stop = round(fill * (1 - stop_pct), 2)
        stop_trade  = ic.place_stop_order(ib, account_id, c["ticker"], qty, actual_stop)
        ib.sleep(1.0)
        st.update_stop(c["ticker"], actual_stop, str(stop_trade.order.orderId), fill)
        print(f"  Stop placed @ ${actual_stop:.2f} (id={stop_trade.order.orderId})")

    print(f"\n  [{label}] entry scan complete.")


# ── US Reversion exits ────────────────────────────────────────────────────────

def run_reversion_exits(ib, account_id: str, cfg: dict, dry_run: bool = True) -> None:
    """Check open reversion positions for exit conditions and close if triggered."""
    from atos import us_reversion as _rev

    rev_cfg = cfg["strategies"]["reversion"]
    stop_pct = rev_cfg["stop_pct"]

    open_pos = st.get_open_positions("reversion")
    if not open_pos:
        print("  No open reversion positions.")
        return

    symbols    = [p["symbol"] for p in open_pos]
    indicators = sig.reversion_exit_indicators(symbols)
    ibkr_prices = ic.get_prices(ib, symbols)
    today      = datetime.date.today()

    print(f"\n  [reversion exits] {len(open_pos)} position(s)")

    for pos in open_pos:
        sym       = pos["symbol"]
        entry_px  = float(pos.get("fill_price") or 0)
        stop_oid  = pos.get("stop_order_id")
        qty       = int(pos["qty"])

        # Use live IBKR price if available, fall back to Yahoo daily close
        cur_price = ibkr_prices.get(sym, 0.0)
        ind       = indicators.get(sym, {})
        if not cur_price or cur_price <= 0:
            cur_price = ind.get("price", 0.0)

        filled_at_str = pos.get("filled_at") or pos.get("created_at", "")
        filled_date   = datetime.date.fromisoformat(filled_at_str[:10])
        td_held       = max(0, len(pd.bdate_range(filled_date, today)) - 1)

        current_rsi = ind.get("rsi")
        sma20       = ind.get("sma20")

        trade_dict = {"entry_price": entry_px}
        should_exit, reason = _rev.should_exit(
            trade_dict, cur_price, current_rsi, sma20, td_held
        )

        rsi_str = f"{current_rsi:.0f}" if current_rsi is not None else "n/a"
        print(f"  {sym:<8}  px=${cur_price:.2f}  entry=${entry_px:.2f}  "
              f"rsi={rsi_str}  held={td_held}d  "
              f"{'→ EXIT: ' + reason if should_exit else 'HOLD'}")

        if not should_exit:
            continue

        if dry_run:
            print(f"    [DRY RUN] would sell {qty} {sym}")
            continue

        confirm = input(f"  Confirm EXIT {sym}? [y/N]: ").strip().lower()
        if confirm != "y":
            print("  Skipped.")
            continue

        if stop_oid:
            open_orders = ib.openTrades()
            old = next((t for t in open_orders if str(t.order.orderId) == str(stop_oid)), None)
            if old:
                ic.cancel_order(ib, old)
                ib.sleep(0.5)

        sell_trade = ic.place_market_order(ib, account_id, sym, "SELL", qty)
        st.record_order(str(sell_trade.order.orderId), sym, "SELL", qty, strategy="reversion")
        fill = ic.confirm_fill(ib, sell_trade)
        if fill is None:
            print(f"  WARNING: exit fill not confirmed for {sym}.")
            st.mark_cancelled(str(sell_trade.order.orderId))
        else:
            pnl = (fill - entry_px) * qty
            print(f"  Sold {qty} {sym} @ ${fill:.4f}  P&L: ${pnl:+,.2f}")
            st.mark_filled(str(sell_trade.order.orderId), fill, side="SELL")

    print("\n  Reversion exit check complete.")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _print_plan(buys: list[dict], sells: list[dict], stop_pct: float) -> None:
    if sells:
        print("\n  SELL plan:")
        for s in sells:
            print(f"    {s['symbol']:<8} qty={s['qty']}  price~${s['price']:.2f}  "
                  f"value~${s['value']:,.0f}")
    if buys:
        print("\n  BUY plan:")
        for b in buys:
            stop = round(b["price"] * (1 - stop_pct), 2)
            print(f"    {b['symbol']:<8} qty={b['qty']}  price~${b['price']:.2f}  "
                  f"notional~${b['notional']:,.0f}  stop=${stop:.2f}")
    print()
