"""
ibkr_executor.py
----------------
Strategy executors for the IBKR stocks sleeve.

Strategies:
  blend     — US cross-sectional momentum (fortnightly rebalance)
  reversion — US mean reversion (entry + exit checks, intraday variant)
  signals   — 4 US Signals strategies (SMA Crossover, RSI Reversal, Momentum, Ensemble)

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

def run_rebalance(ib, account_id: str, cfg: dict, dry_run: bool = True,
                  signal: dict | None = None) -> None:
    """US Blend cross-sectional momentum rebalance.

    signal: pre-generated result from ibkr_signals.blend_targets().
    If None, generated here (keeps older call sites working but holds
    the IBKR connection open during the ~30s Yahoo Finance download).
    Prefer passing signal from main() so the connection is brief.
    """
    blend_cfg   = cfg.get("strategies", {}).get("blend", cfg["capital"])
    budget_usd  = blend_cfg.get("budget_usd",   cfg["capital"]["budget_usd"])
    max_pos     = blend_cfg.get("max_positions", cfg["capital"]["max_positions"])
    min_usd     = blend_cfg.get("min_trade_usd", cfg["capital"]["min_trade_usd"])
    buf_pct     = cfg["capital"]["cash_buffer_pct"]
    stop_pct    = blend_cfg.get("stop_pct", cfg["risk"]["stop_pct"])

    if signal is None:
        print("\n  Generating US Blend signal (Yahoo Finance)...")
        signal = sig.blend_targets()
    targets = signal.get("targets", [])
    if not targets:
        print(f"  No targets in signal ({signal.get('reason', '')}). Nothing to do.")
        return

    print(f"  Signal targets ({len(targets)}): {', '.join(targets)}")
    if signal.get("risk_off"):
        print("  [RISK OFF] Signal is in defensive mode.")

    # TRADING RULE: execution uses IBKR live prices only — never Yahoo Finance.
    held    = ic.get_positions(ib, account_id)
    symbols = list({*targets, *[p["symbol"] for p in held]})
    prices  = ic.get_prices(ib, symbols)   # returns 0 if IBKR has no data
    ibkr_ok = not all(v == 0.0 for v in prices.values())
    cash    = ic.get_cash_balance(ib, account_id)
    print(f"  Cash available: ${cash:,.2f}")
    if not ibkr_ok:
        print("  [WARNING] IBKR returned no prices — plan shown as Yahoo estimates, not for execution.")

    if not dry_run and not ic.is_market_open():
        print("\n  [BLOCKED] US market is closed. Orders can only be placed "
              "09:30–16:00 ET (14:30–21:00 UTC).")
        return

    if not dry_run and not ibkr_ok:
        print("\n  [BLOCKED] No IBKR live prices available. "
              "Cannot execute without live market data.")
        return

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
    prices  = {s: ic.abs_price(p) for s, p in ic.get_prices(ib, symbols).items()}
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
        st.update_stop(sym, new_stop, str(new_stop_trade.order.orderId), new_high,
                       strategy=pos.get("strategy"))
        print(f"           → stop updated (new id={new_stop_trade.order.orderId})")

    print("\n  Trail-stop pass complete.")


# ── US Reversion entries ───────────────────────────────────────────────────────

def run_reversion_entries(ib, account_id: str, cfg: dict, dry_run: bool = True,
                          intraday: bool = False,
                          candidates: list | None = None) -> None:
    """Scan for US Reversion entry signals and buy new slots.

    intraday=True uses 5-min yfinance bars (US market hours only).
    candidates: pre-generated list from ibkr_signals.reversion_candidates() /
      ibkr_signals.intraday_candidates(). If None, generated here (holds the
      IBKR connection open during the Yahoo Finance download). Prefer passing
      from main() so the connection is held for seconds, not minutes.
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

    if candidates is None:
        candidates = sig.intraday_candidates() if intraday else sig.reversion_candidates()
    new_cands  = [c for c in candidates if c["ticker"] not in open_syms]

    if not new_cands:
        print("  No new reversion candidates.")
        return

    # Fetch live IBKR prices. TRADING RULE: execution uses IBKR prices only.
    # Yahoo Finance is for signal generation (RSI/EMA/scan) only — never for
    # sizing or placing a live order.
    live_syms   = [c["ticker"] for c in new_cands[:slots_free]]
    live_prices = ic.get_prices(ib, live_syms)   # returns 0 if IBKR has no data

    if not dry_run and not ic.is_market_open():
        print(f"\n  [BLOCKED] US market is closed. Orders can only be placed "
              f"09:30–16:00 ET (14:30–21:00 UTC).")
        return

    per_slot = budget / max_slots

    for c in new_cands[:slots_free]:
        ibkr_price  = live_prices.get(c["ticker"], 0.0)
        yahoo_price = c["price"]   # daily close from scan signal — display only
        ibkr_ok     = bool(ibkr_price and ibkr_price > 0)

        # Dry-run: show estimate even without live price (clearly labeled)
        if dry_run:
            price     = ibkr_price if ibkr_ok else yahoo_price
            price_src = "IBKR" if ibkr_ok else "Yahoo est. (IBKR unavailable — not for execution)"
        else:
            # Execute: IBKR price required
            if not ibkr_ok:
                print(f"\n  [BLOCKED] {c['ticker']}: no IBKR live price. "
                      f"Cannot size order. Run during market hours with IBKR market data.")
                continue
            price     = ibkr_price
            price_src = "IBKR live"

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
        print(f"    qty={qty}  price~${price:.2f} [{price_src}]  "
              f"notional~${notional:,.0f}  stop=${stop_price:.2f}")

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

def run_reversion_exits(ib, account_id: str, cfg: dict, dry_run: bool = True,
                        indicators: dict | None = None) -> None:
    """Check open reversion positions for exit conditions and close if triggered.

    indicators: pre-generated {symbol: {price, rsi, sma20}} from
      ibkr_signals.reversion_exit_indicators(symbols). If None, generated here.
      Prefer passing from main() so the connection is held for seconds, not minutes.
    """
    from atos import us_reversion as _rev

    rev_cfg = cfg["strategies"]["reversion"]
    stop_pct = rev_cfg["stop_pct"]

    open_pos = st.get_open_positions("reversion")
    if not open_pos:
        print("  No open reversion positions.")
        return

    symbols = [p["symbol"] for p in open_pos]
    if indicators is None:
        indicators = sig.reversion_exit_indicators(symbols)
    ibkr_prices = {s: ic.abs_price(p) for s, p in ic.get_prices(ib, symbols).items()}
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


# ── US Signals entries (SMA Crossover / RSI Reversal / Momentum / Ensemble) ──

def run_us_signals_entries(ib, account_id: str, cfg: dict, dry_run: bool = True,
                            feat_data: dict | None = None) -> None:
    """Scan all 4 US Signals strategies for BUY signals and place entries.

    feat_data: pre-generated from ibkr_signals.us_signals_data(). Pass from main()
    so the Yahoo download happens before the IBKR connection is open.
    """
    from atos.us_signals import (
        get_entry_signals, compute_stop,
        ALL_SIGNAL_STRATEGY_NAMES, MAX_POSITIONS_PER_STRATEGY,
    )

    sig_cfg      = cfg.get("strategies", {}).get("signals", {})
    slot_usd     = float(sig_cfg.get("slot_usd",          2000))
    max_per_str  = int(sig_cfg.get("max_per_strategy",    MAX_POSITIONS_PER_STRATEGY))
    min_usd      = float(sig_cfg.get("min_trade_usd",     50))

    if feat_data is None:
        feat_data = sig.us_signals_data()

    # Count currently open positions per (ticker, strategy) — a ticker may be
    # held by multiple strategies simultaneously.
    open_by_strategy: dict[str, set[str]] = {s: set() for s in ALL_SIGNAL_STRATEGY_NAMES}
    for pos in st.get_open_positions():
        strat = pos.get("strategy", "")
        if strat in open_by_strategy:
            open_by_strategy[strat].add(pos["symbol"])

    print("\n  [us signals] scanning for BUY signals across 4 strategies...")

    # Collect all raw signals (no slot cap yet — apply it after sorting by confidence).
    all_raw: list[dict] = []
    for ticker, df in feat_data.items():
        try:
            for sig_row in get_entry_signals(ticker, df):
                strat = sig_row["strategy_name"]
                if ticker not in open_by_strategy.get(strat, set()):
                    all_raw.append({**sig_row, "ticker": ticker, "_df": df})
        except Exception:
            continue

    # Sort by confidence descending so the slot cap selects the strongest signals.
    all_raw.sort(key=lambda r: r.get("confidence", 0), reverse=True)

    slots_used: dict[str, int] = {s: len(open_by_strategy.get(s, set()))
                                   for s in ALL_SIGNAL_STRATEGY_NAMES}
    signals_found: list[dict] = []
    for row in all_raw:
        strat = row["strategy_name"]
        if slots_used.get(strat, 0) < max_per_str:
            signals_found.append(row)
            slots_used[strat] = slots_used.get(strat, 0) + 1

    print(f"  [us signals] open: "
          + "  ".join(f"{s[:14]}: {len(open_by_strategy[s])}/{max_per_str}"
                      for s in ALL_SIGNAL_STRATEGY_NAMES))
    if not signals_found:
        print("  [us signals] No new entry signals.")
        return

    total_candidates = len(all_raw)
    print(f"  [us signals] {len(signals_found)} signal(s) selected "
          f"(top by confidence from {total_candidates} candidates).")

    if not dry_run and not ic.is_market_open():
        print("\n  [BLOCKED] US market is closed. Orders can only be placed "
              "09:30–16:00 ET (14:30–21:00 UTC).")
        return

    sig_tickers  = list({s["ticker"] for s in signals_found})
    live_prices  = ic.get_prices(ib, sig_tickers)
    ibkr_any_ok  = any(v and v > 0 for v in live_prices.values())
    if not ibkr_any_ok:
        print("  [WARNING] IBKR returned no live prices. "
              "Dry-run shows Yahoo estimates; execute is blocked.")

    if not dry_run and not ibkr_any_ok:
        print("\n  [BLOCKED] No IBKR live prices. Cannot execute without market data.")
        return

    for row in signals_found:
        ticker = row["ticker"]
        strat  = row["strategy_name"]
        df     = row["_df"]

        ibkr_price  = live_prices.get(ticker, 0.0)
        ibkr_ok     = bool(ibkr_price and ibkr_price > 0)
        yahoo_price = float(df["Close"].dropna().iloc[-1]) if "Close" in df.columns else 0.0

        if dry_run:
            price     = ibkr_price if ibkr_ok else yahoo_price
            price_src = "IBKR" if ibkr_ok else "Yahoo est. (not for execution)"
        else:
            if not ibkr_ok:
                print(f"  [BLOCKED] {ticker} ({strat}): no IBKR live price — skip")
                continue
            price     = ibkr_price
            price_src = "IBKR live"

        if not price or price <= 0:
            continue
        qty      = math.floor(slot_usd / price)
        if qty < 1:
            continue
        notional = round(price * qty, 2)
        if notional < min_usd:
            continue
        stop_price = compute_stop(df, price)

        reason_short = (row.get("reason") or "")[:50]
        print(f"\n  [{strat}]  BUY {ticker:<8}  conf={row.get('confidence', 0):.2f}  {reason_short}")
        print(f"    qty={qty}  price~${price:.2f} [{price_src}]  "
              f"notional~${notional:,.0f}  stop=${stop_price:.2f}")

        if dry_run:
            print("    [DRY RUN] would place buy + stop")
            continue

        confirm = input(f"  Confirm buy {ticker} ({strat})? [y/N]: ").strip().lower()
        if confirm != "y":
            print("  Skipped.")
            continue

        trade = ic.place_market_order(ib, account_id, ticker, "BUY", qty)
        st.record_order(str(trade.order.orderId), ticker, "BUY", qty, strategy=strat)
        print(f"  Order placed (id={trade.order.orderId}). Waiting for fill...")
        fill = ic.confirm_fill(ib, trade)
        if fill is None:
            print(f"  WARNING: fill not confirmed for {ticker}.")
            st.mark_cancelled(str(trade.order.orderId))
            continue

        print(f"  Filled @ ${fill:.4f}")
        st.mark_filled(str(trade.order.orderId), fill, side="BUY")
        actual_stop = compute_stop(df, fill)
        stop_trade  = ic.place_stop_order(ib, account_id, ticker, qty, actual_stop)
        ib.sleep(1.0)
        st.update_stop(ticker, actual_stop, str(stop_trade.order.orderId), fill,
                       strategy=strat)
        print(f"  Stop placed @ ${actual_stop:.2f} (id={stop_trade.order.orderId})")
        open_by_strategy[strat].add(ticker)

    print("\n  [us signals] entry scan complete.")


# ── US Signals exits ──────────────────────────────────────────────────────────

def run_us_signals_exits(ib, account_id: str, cfg: dict, dry_run: bool = True,
                          feat_data: dict | None = None) -> None:
    """Check open US Signals positions for exit conditions and close if triggered.

    feat_data: pre-generated from ibkr_signals.us_signals_data() or
    ibkr_signals.us_signals_exit_data(open_symbols). Pass from main() so the
    Yahoo download happens before the IBKR connection is open.
    """
    from atos.us_signals import should_exit, ALL_SIGNAL_STRATEGY_NAMES

    open_pos = [p for p in st.get_open_positions()
                if p.get("strategy") in ALL_SIGNAL_STRATEGY_NAMES]
    if not open_pos:
        print("  [us signals] No open us_signals positions.")
        return

    symbols = [p["symbol"] for p in open_pos]
    if feat_data is None:
        feat_data = sig.us_signals_exit_data(symbols)
    else:
        # Restrict passed full-universe data to just what's open
        feat_data = {s: feat_data[s] for s in symbols if s in feat_data}

    live_prices = {s: ic.abs_price(p)
                   for s, p in ic.get_prices(ib, symbols).items()}
    print(f"\n  [us signals exits] {len(open_pos)} position(s)")

    for pos in open_pos:
        sym      = pos["symbol"]
        entry_px = float(pos.get("fill_price") or 0)
        stop_oid = pos.get("stop_order_id")
        qty      = int(pos["qty"])
        strat    = pos.get("strategy", "")

        df = feat_data.get(sym)
        if df is None or df.empty:
            print(f"  {sym:<8}  [{strat[:14]:<14}]  no data — skipped")
            continue

        cur_price = live_prices.get(sym, 0.0)
        if not cur_price or cur_price <= 0:
            cur_price = float(df["Close"].dropna().iloc[-1])

        trade_dict = {
            "strategy":   strat,
            "ticker":     sym,
            "stop_price": pos.get("stop_price") or 0,
            "entry_date": (pos.get("filled_at") or pos.get("created_at", ""))[:10],
        }
        exit_flag, reason = should_exit(trade_dict, df, cur_price)

        gain_pct = ((cur_price / entry_px) - 1) * 100 if entry_px > 0 else 0
        print(f"  {sym:<8}  [{strat[:16]:<16}]  px=${cur_price:.2f}  "
              f"entry=${entry_px:.2f}  {gain_pct:+.1f}%  "
              f"{'→ EXIT: ' + reason if exit_flag else 'HOLD'}")

        if not exit_flag:
            continue

        if dry_run:
            print(f"    [DRY RUN] would sell {qty} {sym}")
            continue

        confirm = input(f"  Confirm EXIT {sym} ({strat})? [y/N]: ").strip().lower()
        if confirm != "y":
            print("  Skipped.")
            continue

        if stop_oid:
            open_orders = ib.openTrades()
            old = next((t for t in open_orders
                        if str(t.order.orderId) == str(stop_oid)), None)
            if old:
                ic.cancel_order(ib, old)
                ib.sleep(0.5)

        sell_trade = ic.place_market_order(ib, account_id, sym, "SELL", qty)
        st.record_order(str(sell_trade.order.orderId), sym, "SELL", qty, strategy=strat)
        fill = ic.confirm_fill(ib, sell_trade)
        if fill is None:
            print(f"  WARNING: exit fill not confirmed for {sym}.")
            st.mark_cancelled(str(sell_trade.order.orderId))
        else:
            pnl = (fill - entry_px) * qty
            print(f"  Sold {qty} {sym} @ ${fill:.4f}  P&L: ${pnl:+,.2f}")
            st.mark_filled(str(sell_trade.order.orderId), fill, side="SELL")

    print("\n  [us signals] exit check complete.")


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
