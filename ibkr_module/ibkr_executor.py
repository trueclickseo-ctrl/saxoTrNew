"""
ibkr_executor.py
----------------
Strategy executors for the IBKR stocks sleeve.

Strategies:
  blend     -- US cross-sectional momentum (fortnightly rebalance)
  reversion -- US mean reversion (entry + exit checks, intraday variant)
  signals   -- 4 US Signals strategies (SMA Crossover, RSI Reversal, Momentum, Ensemble)

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

# SIM A/B: ATR Chandelier exit for blend strategies (mirrors atos_runner.py)
IBKR_ATR_MULTIPLIER = 2.5   # stop = trail_high - 2.5 × ATR(14)
IBKR_ATR_PERIOD     = 14

_ROOT = Path(__file__).parent.parent

# ── AI Copilot layer (2026-09-20) ────────────────────────────────────────────
# Same shadow_mode flag as Saxo SIM (config/ai.json stocks.shadow_mode).
# shadow_mode=true  -> log proposals/decisions, no trade changed (default).
# shadow_mode=false -> REJECT skips the trade, MODIFY reduces qty.
# Never applies to live (run_ibkr_stocks.py --live only allows blend; no
# copilot in run_rebalance since the basket-ranker is the AI layer for blend).
_ai_cfg = None
_ai_copilot = None
_ai_stock_proposal = None
_sc = None   # stock observation cards
try:
    import ai.config as _ai_cfg
    import ai.agent.trading_copilot as _ai_copilot
    from ai.features import stock_proposal as _ai_stock_proposal
    from ai.features import stock_cards as _sc
except Exception:
    pass

_LIVE_ACCOUNT_ID = "U28013794"


def _ibkr_account_env(account_id: str) -> str:
    return "ibkr_live" if account_id == _LIVE_ACCOUNT_ID else "ibkr_paper"


def _ai_ibkr_score(strategy: str, ticker: str, price: float, stop_price: float,
                   qty: int, rsi14=None, regime_bars=None,
                   open_positions: list | None = None) -> dict | None:
    """Build a stock proposal and score it with the AI copilot for IBKR paper.
    Returns the decision dict or None if AI is unavailable/disabled. Never raises."""
    if _ai_cfg is None or _ai_copilot is None or _ai_stock_proposal is None:
        return None
    try:
        if not (_ai_cfg.stocks_enabled("sim")
                and bool(_ai_cfg._load().get("agent_enabled", False))):
            return None
        prop = _ai_stock_proposal.build_stock_proposal(
            strategy=strategy, ticker=ticker, entry_price=price,
            stop_price=stop_price, target_price=None,
            rsi14=rsi14, shares=qty,
            daily_vol_pct=None, risk_eur=None, account_equity_eur=None,
            open_positions=open_positions or [],
            regime_bars=regime_bars,
            account_env="ibkr_paper",
        )
        if not prop:
            return None
        _ai_stock_proposal.log_proposal(prop)
        if not (_ai_cfg.agent_dedup_enabled()
                and _ai_stock_proposal.already_evaluated(prop)):
            dec = _ai_copilot.evaluate_stock_proposal(prop)
            _ai_stock_proposal.log_shadow_decision(prop, dec, entered=True)
            return dec
    except Exception as exc:
        print(f"  [ai] ibkr copilot hook failed for {ticker}: {exc}")
    return None


def _ai_ibkr_apply(dec: dict | None, ticker: str, qty: int,
                    strategy: str = "") -> tuple[bool, int]:
    """Apply a copilot decision to qty. Returns (skip, new_qty).
    Only applies when can_apply_stocks_decision() is True (shadow_mode=false)."""
    if dec is None or _ai_cfg is None:
        return False, qty
    if not _ai_cfg.can_apply_stocks_decision():
        return False, qty
    action = dec.get("action", "HOLD")
    if action == "REJECT":
        lbl = f"[{strategy}] " if strategy else ""
        print(f"  [ai] {lbl}{ticker}: REJECT -- {dec.get('comment', '')[:80]}")
        return True, qty
    if action == "MODIFY":
        try:
            mult = max(0.25, min(1.0, float(dec.get("size_multiplier", 1.0))))
        except (TypeError, ValueError):
            mult = 1.0
        new_qty = max(1, int(qty * mult))
        lbl = f"[{strategy}] " if strategy else ""
        print(f"  [ai] {lbl}{ticker}: MODIFY -> {new_qty} shares ({mult:.2f}x) -- "
              f"{dec.get('comment', '')[:60]}")
        return False, new_qty
    return False, qty



def _place_stop_submitted(ib, account_id: str, sym: str, qty, stop_price: float,
                           strategy: str = "") -> tuple:
    """Place a GTC stop and wait up to 15s for Submitted state.

    Returns (trade, ok) where ok=True when the stop reached Submitted.
    If the stop stays PreSubmitted, it is left in place (not cancelled) so
    heal_missing_stops can detect it later -- the DB is still updated so we
    have a record of the pending order.  PreSubmitted = the exchange has not
    yet acknowledged the order; it will usually self-resolve once the session
    stays connected, but can freeze if the session disconnects too quickly.
    """
    trade = ic.place_stop_order(ib, account_id, sym, qty, stop_price)
    for _w in range(15):   # up to 15 x 1s = 15s
        ib.sleep(1.0)
        status = trade.orderStatus.status
        if status == "Submitted":
            return trade, True
        if status in ("Filled", "Cancelled", "ApiCancelled", "Inactive"):
            return trade, False
    status = trade.orderStatus.status
    if status != "Submitted":
        lbl = f" [{strategy}]" if strategy else ""
        print(f"  WARNING{lbl}: stop for {sym} is '{status}' after 15s "
              f"(id={trade.order.orderId}) -- DB updated; heal_stops will verify at next run")
    return trade, status == "Submitted"


def _email_ibkr_fill(side: str, ticker: str, qty, fill: float,
                     strategy: str, entry_px: float = 0.0, reason: str = "") -> None:
    """Fire-and-forget email after a confirmed IBKR fill. Never raises."""
    try:
        from atos import notifier as _ntf
        pnl = (fill - entry_px) * qty if (side.upper() == "SELL" and entry_px > 0) else None
        _ntf.notify_ibkr_trade(side, ticker, qty, fill, strategy,
                               pnl_usd=pnl, reason=reason)
    except Exception:
        pass


def _email_ibkr_alert(title: str, message: str, strategy: str = "IBKR Live") -> None:
    """Fire-and-forget alert email when a live order fails. Never raises."""
    try:
        from atos import notifier as _ntf
        _ntf.notify_ibkr_alert(title, message, strategy)
    except Exception:
        pass


def _compute_plan(
    targets: list[str],
    held: list[dict],
    prices: dict[str, float],
    budget_usd: float,
    max_positions: int,
    min_trade_usd: float,
    cash_buffer_pct: float,
    fractional: bool = False,
    slot_weights: dict | None = None,
) -> tuple[list[dict], list[dict]]:
    """Return (buys, sells) action lists.
    fractional=True : buy exact dollar-value slices (4 decimal places).
    fractional=False: whole shares only, skip if price > slot budget.
    slot_weights    : {ticker: weight} override (e.g. {"HPE":0.70,"STT":0.30}).
                      Weights are normalised to sum=1 over the active buy set.
                      When absent, equal weight per slot is used.
    """
    held_symbols = {p["symbol"] for p in held}
    target_set   = set(targets[:max_positions])
    usable = budget_usd * (1 - cash_buffer_pct)

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

    # Determine which targets need buying
    to_buy = [s for s in targets[:max_positions] if s not in held_symbols]

    # Resolve per-symbol budget using slot_weights when provided
    if slot_weights and to_buy:
        total_w = sum(slot_weights.get(s, 1.0 / len(to_buy)) for s in to_buy)
        sym_budget = {s: usable * slot_weights.get(s, 1.0 / len(to_buy)) / total_w
                      for s in to_buy}
    else:
        per_slot   = usable / max_positions
        sym_budget = {s: per_slot for s in to_buy}

    buys = []
    for sym in to_buy:
        price = prices.get(sym, 0.0)
        if not price or math.isnan(price) or price <= 0:
            continue
        budget = sym_budget[sym]
        if fractional:
            qty = round(budget / price, 4)
        else:
            qty = math.floor(budget / price)
        if qty <= 0:
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


# â"€â"€ US Blend rebalance â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

def run_rebalance(ib, account_id: str, cfg: dict, dry_run: bool = True,
                  signal: dict | None = None, auto: bool = False,
                  sells_only: bool = False, buys_only: bool = False) -> None:
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
    fractional   = blend_cfg.get("fractional", False)
    slot_weights = blend_cfg.get("slot_weights", None)

    if signal is None:
        print("\n  Generating US Blend signal (Yahoo Finance)...")
        signal = sig.blend_targets()
    targets = signal.get("targets", [])
    if not targets:
        print(f"  No targets in signal ({signal.get('reason', '')}). Nothing to do.")
        return

    # force_targets: override signal picks for live (e.g. skip stocks held elsewhere)
    force_targets = blend_cfg.get("force_targets", None)
    if force_targets:
        targets = force_targets
        print(f"  [config] force_targets override: {targets}")

    print(f"  Signal targets ({len(targets)}): {', '.join(targets)}")
    if signal.get("risk_off"):
        print("  [RISK OFF] Signal is in defensive mode.")

    # TRADING RULE: IBKR live prices only. No Yahoo fallback ever.
    # Held positions come from the local DB (blend-only) so the rebalancer never
    # sells positions that belong to another strategy (scorer, reversion, etc.).
    held    = st.get_open_positions(strategy="blend")

    # 14-day rebalance guard: skip if the portfolio was built less than rebal_days ago.
    # Prevents churn on newly-bought positions when the task fires on schedule but the
    # clock hasn't elapsed since the last rebalance.
    rebal_days = blend_cfg.get("rebal_days", 14)
    if not dry_run and held:
        from datetime import datetime, timezone
        fill_dates = [p["filled_at"] for p in held if p.get("filled_at")]
        if fill_dates:
            earliest = min(fill_dates)
            try:
                last_dt   = datetime.fromisoformat(earliest.replace("Z", "+00:00"))
                days_held = (datetime.now(timezone.utc) - last_dt).days
                if days_held < rebal_days:
                    print(f"\n  [SKIP] Portfolio built {days_held}d ago; "
                          f"rebalancing in {rebal_days - days_held}d "
                          f"(rebal_days={rebal_days}).")
                    return
            except Exception:
                pass
    symbols = list({*targets, *[p["symbol"] for p in held]})
    prices  = ic.get_prices(ib, symbols)   # returns 0 if IBKR has no data
    ibkr_ok = not all(v == 0.0 for v in prices.values())
    cash    = ic.get_cash_balance(ib, account_id)
    print(f"  Cash available: ${cash:,.2f}")

    zero_syms = [s for s in symbols if prices.get(s, 0) == 0]
    if zero_syms:
        print(f"  [WARNING] IBKR returned $0 for: {zero_syms} -- those will be skipped.")
    if not ibkr_ok:
        print("  [BLOCKED] IBKR returned no prices at all -- cannot size orders.")

    if not dry_run and not ic.is_market_open():
        print("\n  [BLOCKED] US market is closed. Orders can only be placed "
              "09:30--16:00 ET (14:30--21:00 UTC).")
        return

    if not dry_run and not ibkr_ok:
        print("\n  [BLOCKED] No live IBKR prices available. Cannot size orders.")
        return

    buys, sells = _compute_plan(
        targets       = targets,
        held          = held,
        prices        = prices,
        budget_usd    = budget_usd,
        max_positions = max_pos,
        min_trade_usd = min_usd,
        cash_buffer_pct = buf_pct,
        fractional    = fractional,
        slot_weights  = slot_weights,
    )

    print(f"\n  HOLD  ({len(held) - len(sells)}): "
          f"{', '.join(p['symbol'] for p in held if p['symbol'] not in {s['symbol'] for s in sells})}")
    print(f"  SELL  ({len(sells)}): {', '.join(s['symbol'] for s in sells)}")
    print(f"  BUY   ({len(buys)}):  {', '.join(b['symbol'] for b in buys)}")

    # AI Copilot observation pass -- runs in dry-run AND live mode so every
    # rebalance signal is scored and logged regardless of execution mode.
    _bl_open_obs = [{"symbol": p["symbol"], "side": "BUY", "size": p.get("qty"),
                      "strategy": "us_blend"} for p in held]
    for b in buys:
        _stop_obs = round(b["price"] * (1 - stop_pct), 2)
        _bl_dec_obs = _ai_ibkr_score("us_blend", b["symbol"], b["price"], _stop_obs, b["qty"],
                                      open_positions=_bl_open_obs)
        _ai_ibkr_apply(_bl_dec_obs, b["symbol"], b["qty"], "blend")

    if dry_run:
        print("\n  [DRY RUN] No orders placed. Pass --execute to trade.\n")
        _print_plan(buys, sells, stop_pct)
        return

    if buys_only:
        sells = []   # skip sell phase; cash already freed by a prior sells-only run
    if sells_only:
        buys = []    # skip buy phase; reversion will use the freed cash

    # -- Execute SELLs ---------------------------------------------------------
    # Cancel ALL open sell-side orders for each symbol before selling.
    # trail_stops replaces the GTC stop each night with a new order ID; the DB
    # still holds the old ID.  Cancelling only the DB ID leaves the new stop alive
    # → IBKR error 201 (new sell + active stop > owned qty = implied short).
    if sells:
        sell_syms = {s["symbol"] for s in sells}

        # IB API rule: a client can only cancel orders IT placed.
        # Trail stops (clientId=13) places the GTC stops; blend (clientId=10) cannot
        # cancel them -- the requests are silently rejected by IB Gateway.
        # Solution: connect briefly as clientId=0 (master) which can cancel ANY order.
        live_port = cfg.get("port_live", 4001)
        ic.cancel_stops_as_master(list(sell_syms), account_id, port=live_port)

        # Belt-and-suspenders: also try to cancel DB-tracked stop IDs from current
        # clientId (covers any stop placed by this same session).
        held_by_sym = {p["symbol"]: p for p in held}
        for sym in sell_syms:
            p = held_by_sym.get(sym, {})
            stop_id = p.get("stop_order_id")
            if stop_id and str(stop_id) not in ("", "None", "0"):
                ic.cancel_order_by_id(ib, int(stop_id))

        # Verify: poll until all SELL orders for sell symbols are gone.
        # PreSubmitted -> PendingCancel -> Cancelled can take 5-15s on live Gateway.
        # We must NOT place the sell until the stop is fully cancelled -- otherwise
        # IBKR still counts it and rejects with error 201 (implied short position).
        for _poll in range(10):          # up to 10 × 2s = 20s
            ib.reqAllOpenOrders()
            ib.sleep(2.0)
            still_open = [
                t for t in ib.openTrades()
                if t.order.account == account_id
                and t.order.action == "SELL"
                and getattr(t.contract, "symbol", "") in sell_syms
                and t.orderStatus.status not in ("Cancelled", "ApiCancelled", "Inactive")
            ]
            if not still_open:
                print(f"  [pre-sell] All stops confirmed cancelled.")
                break
            statuses = {t.orderStatus.status for t in still_open}
            print(f"  [pre-sell] Waiting for stop cancellation ({statuses}) ...")
        else:
            print(f"  [pre-sell] WARNING: stop(s) still active after 20s -- sell may fail")

    failed_sells: list[str] = []
    for s in sells:
        print(f"\n  SELL {s['qty']} {s['symbol']} @ ~${s['price']:.2f}  "
              f"(value ~${s['value']:,.0f})")
        confirm = "y" if auto else input("  Confirm? [y/N]: ").strip().lower()
        if confirm != "y":
            print("  Skipped.")
            continue

        fill = None
        fill_trade = None
        for attempt in range(2):
            if attempt > 0:
                print(f"  Retrying sell {s['symbol']} (attempt {attempt + 1}/2)...")
                ib.sleep(5.0)
            _t = ic.place_market_order(ib, account_id, s["symbol"], "SELL", s["qty"])
            st.record_order(str(_t.order.orderId), s["symbol"], "SELL", s["qty"], strategy="blend")
            print(f"  Order placed (id={_t.order.orderId}). Waiting for fill...")
            _f = ic.confirm_fill(ib, _t)
            if _f is None:
                st.mark_cancelled(str(_t.order.orderId))
            else:
                fill = _f
                fill_trade = _t
                break

        if fill is None:
            failed_sells.append(s["symbol"])
            print(f"  WARNING: sell failed for {s['symbol']} after 2 attempts -- will retry next cycle.")
            _email_ibkr_alert(
                f"Sell failed: {s['symbol']}",
                f"SELL {s['qty']} {s['symbol']} failed after 2 attempts. "
                f"RETRY task fires in ~25 min. If RETRY also fails, check IB Gateway and re-run manually.",
                strategy="blend",
            )
        else:
            print(f"  Filled @ ${fill:.4f}")
            st.mark_filled(str(fill_trade.order.orderId), fill, side="SELL")
            st.close_buy_position(s["symbol"], "blend")
            _email_ibkr_fill("SELL", s["symbol"], s["qty"], fill, "blend")

            if _sc and _ai_cfg and _ai_cfg.stocks_enabled(_ibkr_account_env(account_id)):
                _acenv  = _ibkr_account_env(account_id)
                _p      = held_by_sym.get(s["symbol"], {})
                _epx    = float(_p.get("fill_price") or s["price"])
                _edate  = (_p.get("filled_at") or _p.get("created_at") or "")[:10]
                _risk   = round((_epx - float(_p.get("stop_price") or 0)) * s["qty"], 2) if _p.get("stop_price") else None
                _gpnl   = round((fill - _epx) * s["qty"], 2)
                _npnl   = round(_gpnl - 1.0, 2)   # ~$1 IBKR commission
                try:
                    _fdt  = datetime.datetime.fromisoformat((_p.get("filled_at") or "").replace("Z", "+00:00"))
                    _hhrs = round((datetime.datetime.now(datetime.timezone.utc) - _fdt).total_seconds() / 3600, 1)
                except Exception:
                    _hhrs = None
                _cid = _sc.card_id_for("us_blend", s["symbol"], _edate, _acenv)
                _sc.log_stock_exit_card(
                    card_id=_cid, exit_price=fill, exit_reason="blend_rebalance",
                    gross_pnl_sek=_gpnl, commission_sek=1.0, net_pnl_sek=_npnl,
                    holding_hours=_hhrs, sek_per_eur=None,
                    risk_sek=_risk, native_currency="USD",
                )

    if failed_sells:
        print(f"\n  Skipping buys -- sells failed: {', '.join(failed_sells)}. RETRY task will handle.")
        print("\n  Rebalance complete.")
        return

    for b in buys:
        stop_price = round(b["price"] * (1 - stop_pct), 2)

        # AI Copilot (apply path -- dedup will skip if already scored above)
        _bl_open = [{"symbol": p["symbol"], "side": "BUY", "size": p.get("qty"),
                      "strategy": "us_blend"}
                     for p in held]
        _bl_dec = _ai_ibkr_score("us_blend", b["symbol"], b["price"], stop_price, b["qty"],
                                  open_positions=_bl_open)
        _ai_skip_bl, _bl_qty = _ai_ibkr_apply(_bl_dec, b["symbol"], b["qty"], "blend")
        if _ai_skip_bl:
            continue
        if _bl_qty != b["qty"]:
            b["qty"] = _bl_qty
            b["notional"] = round(b["price"] * _bl_qty, 2)

        print(f"\n  BUY  {b['qty']} {b['symbol']} @ ~${b['price']:.2f}  "
              f"notional ~${b['notional']:,.0f}  stop=${stop_price:.2f}")
        confirm = "y" if auto else input("  Confirm? [y/N]: ").strip().lower()
        if confirm != "y":
            print("  Skipped.")
            continue

        lmt = round(b["price"] * 1.01, 2)
        trade = ic.place_limit_order(ib, account_id, b["symbol"], "BUY", b["qty"], lmt)
        st.record_order(str(trade.order.orderId), b["symbol"], "BUY", b["qty"], strategy="blend")
        print(f"  Limit order placed (id={trade.order.orderId}, lmt=${lmt:.2f}). Waiting for fill...")
        fill = ic.confirm_fill(ib, trade)
        if fill is None:
            print(f"  WARNING: fill not confirmed for {b['symbol']}. Skipping stop.")
            st.mark_cancelled(str(trade.order.orderId))
            _email_ibkr_alert(
                f"Buy failed: {b['symbol']}",
                f"BUY {b['qty']} {b['symbol']} (blend) failed to fill. "
                f"Likely insufficient cash. RETRY task fires in ~25 min.",
                strategy="blend",
            )
            continue

        print(f"  Filled @ ${fill:.4f}")
        st.mark_filled(str(trade.order.orderId), fill, side="BUY")
        _email_ibkr_fill("BUY", b["symbol"], b["qty"], fill, "blend")

        actual_stop = round(fill * (1 - stop_pct), 2)
        stop_trade, _ = _place_stop_submitted(ib, account_id, b["symbol"], b["qty"],
                                              actual_stop, strategy="blend")
        st.update_stop(b["symbol"], actual_stop,
                       str(stop_trade.order.orderId), trailing_high=fill)
        print(f"  Stop placed @ ${actual_stop:.2f} (id={stop_trade.order.orderId})")

        if _sc and _ai_cfg and _ai_cfg.stocks_enabled(_ibkr_account_env(account_id)):
            _entry_date = datetime.date.today().isoformat()
            _sc.log_stock_entry_card(
                strategy="us_blend", ticker=b["symbol"], direction="BUY",
                entry_price=fill, shares=b["qty"], stop_price=actual_stop,
                sek_per_eur=None, entry_date=_entry_date,
                risk_sek=round((fill - actual_stop) * b["qty"], 2),
                account_env=_ibkr_account_env(account_id), native_currency="USD",
            )

    print("\n  Rebalance complete.")


# ── US Blend V2 rebalance ─────────────────────────────────────────────────────

def run_rebalance_v2(ib, account_id: str, cfg: dict, dry_run: bool = True,
                     signal: dict | None = None, auto: bool = False) -> None:
    """US Blend V2 rebalance — skip-month momentum + volatility targeting.

    Uses atos/us_blend_v2.py. Positions tracked under strategy='blend_v2'
    in the IBKR state DB, fully isolated from the V1 'blend' book.

    Vol-targeting: recent daily P&L returns come from ibkr_state
    (get_recent_daily_returns). If fewer than 20 days are available, scale=1.0.
    """
    from atos import us_blend_v2 as V2

    blend_cfg   = cfg.get("strategies", {}).get("blend_v2",
                  cfg.get("strategies", {}).get("blend", cfg["capital"]))
    budget_usd  = blend_cfg.get("budget_usd",   cfg["capital"]["budget_usd"])
    max_pos     = blend_cfg.get("max_positions", cfg["capital"]["max_positions"])
    min_usd     = blend_cfg.get("min_trade_usd", cfg["capital"]["min_trade_usd"])
    buf_pct     = cfg["capital"]["cash_buffer_pct"]
    stop_pct    = blend_cfg.get("stop_pct", cfg["risk"]["stop_pct"])

    if signal is None:
        print("\n  Generating US Blend V2 signal (Yahoo Finance)...")
        signal = sig.blend_v2_targets()
    targets = signal.get("targets", [])
    if not targets:
        print(f"  [blend_v2] No targets ({signal.get('reason', '')}). Nothing to do.")
        return

    print(f"  [blend_v2] targets ({len(targets)}): {', '.join(targets)}")

    # Vol-targeting: read recent returns from state; fall back to 1.0 safely.
    recent_rets = []
    try:
        recent_rets = st.get_recent_daily_returns(strategy="blend_v2") or []
    except AttributeError:
        pass
    scale = V2.vol_scale(recent_rets)
    effective_budget = budget_usd * scale
    if scale < 1.0:
        print(f"  [blend_v2] vol-scale={scale:.2f} -> effective budget ${effective_budget:,.0f}")

    if signal.get("risk_off"):
        print("  [blend_v2] RISK OFF — signal is defensive.")

    held    = st.get_open_positions(strategy="blend_v2")
    symbols = list({*targets, *[p["symbol"] for p in held]})
    prices  = ic.get_prices(ib, symbols)
    ibkr_ok = not all(v == 0.0 for v in prices.values())
    cash    = ic.get_cash_balance(ib, account_id)
    print(f"  [blend_v2] cash: ${cash:,.2f}")

    if not dry_run and not ic.is_market_open():
        print("  [blend_v2] BLOCKED — US market is closed.")
        return
    if not dry_run and not ibkr_ok:
        print("  [blend_v2] BLOCKED — no IBKR live prices available.")
        return

    buys, sells = _compute_plan(
        targets         = targets,
        held            = held,
        prices          = prices,
        budget_usd      = effective_budget,
        max_positions   = max_pos,
        min_trade_usd   = min_usd,
        cash_buffer_pct = buf_pct,
    )

    print(f"\n  HOLD  ({len(held) - len(sells)}): "
          f"{', '.join(p['symbol'] for p in held if p['symbol'] not in {s['symbol'] for s in sells})}")
    print(f"  SELL  ({len(sells)}): {', '.join(s['symbol'] for s in sells)}")
    print(f"  BUY   ({len(buys)}):  {', '.join(b['symbol'] for b in buys)}")

    if dry_run:
        print("\n  [blend_v2 DRY RUN] No orders placed. Pass --execute to trade.\n")
        _print_plan(buys, sells, stop_pct)
        return

    for s in sells:
        print(f"\n  SELL {s['qty']} {s['symbol']} @ ~${s['price']:.2f}")
        confirm = "y" if auto else input("  Confirm? [y/N]: ").strip().lower()
        if confirm != "y":
            print("  Skipped.")
            continue
        trade = ic.place_market_order(ib, account_id, s["symbol"], "SELL", s["qty"])
        st.record_order(str(trade.order.orderId), s["symbol"], "SELL", s["qty"],
                        strategy="blend_v2")
        fill = ic.confirm_fill(ib, trade)
        if fill is None:
            print(f"  WARNING: fill not confirmed for {s['symbol']} -- sell cancelled, will retry next cycle.")
            st.mark_cancelled(str(trade.order.orderId))
        else:
            print(f"  Filled @ ${fill:.4f}")
            st.mark_filled(str(trade.order.orderId), fill, side="SELL")
            st.close_buy_position(s["symbol"], "blend_v2")
            _email_ibkr_fill("SELL", s["symbol"], s["qty"], fill, "blend_v2")

    for b in buys:
        actual_stop_est = round(b["price"] * (1 - stop_pct), 2)
        print(f"\n  BUY  {b['qty']} {b['symbol']} @ ~${b['price']:.2f}  "
              f"notional ~${b['notional']:,.0f}  stop~${actual_stop_est:.2f}")
        confirm = "y" if auto else input("  Confirm? [y/N]: ").strip().lower()
        if confirm != "y":
            print("  Skipped.")
            continue
        trade = ic.place_market_order(ib, account_id, b["symbol"], "BUY", b["qty"])
        st.record_order(str(trade.order.orderId), b["symbol"], "BUY", b["qty"],
                        strategy="blend_v2")
        fill = ic.confirm_fill(ib, trade)
        if fill is None:
            print(f"  WARNING: fill not confirmed for {b['symbol']}. Skipping stop.")
            st.mark_cancelled(str(trade.order.orderId))
            continue
        print(f"  Filled @ ${fill:.4f}")
        st.mark_filled(str(trade.order.orderId), fill, side="BUY")
        _email_ibkr_fill("BUY", b["symbol"], b["qty"], fill, "blend_v2")
        actual_stop = round(fill * (1 - stop_pct), 2)
        stop_trade, _ = _place_stop_submitted(ib, account_id, b["symbol"], b["qty"],
                                              actual_stop, strategy="blend_v2")
        st.update_stop(b["symbol"], actual_stop,
                       str(stop_trade.order.orderId), trailing_high=fill,
                       strategy="blend_v2")
        print(f"  Stop placed @ ${actual_stop:.2f} (id={stop_trade.order.orderId})")

    print("\n  [blend_v2] Rebalance complete.")


def _ibkr_atr(symbol: str, period: int = IBKR_ATR_PERIOD) -> float | None:
    """ATR(period) via yfinance OHLC history — used for stop calculation only, not execution."""
    try:
        import yfinance as yf
        import numpy as _np
        bars = yf.download(symbol, period=f"{period + 5}d", interval="1d",
                           progress=False, auto_adjust=True)
        if bars is None or len(bars) < period + 1:
            return None
        h = bars["High"].values
        l = bars["Low"].values
        c = bars["Close"].values
        tr = _np.maximum.reduce([h[1:] - l[1:], _np.abs(h[1:] - c[:-1]), _np.abs(l[1:] - c[:-1])])
        return float(tr[-period:].mean())
    except Exception:
        return None


# â"€â"€ Trail stops â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

def heal_missing_stops(ib, account_id: str, cfg: dict,
                       atr_strategies: list[str] | None = None,
                       dry_run: bool = False) -> int:
    """Re-place GTC stop orders for any open position whose stop is missing on IBKR.

    Rules:
    - Uses IBKR live prices only. If IBKR returns no prices (market closed),
      the entire heal pass is aborted -- Yahoo is never used here.
    - A position with a stored stop_order_id is only healed when IBKR prices
      ARE available, so we can positively confirm the order is absent (not just
      that openTrades() returned empty due to market closure).
    - Positions with stop_order_id=None are always healed when prices available.
    - Stop level uses fixed stop_pct only -- no ATR/Yahoo. Heal restores
      protection; trail_stops handles refinement when market is live.
    """
    stop_pct  = cfg["risk"]["stop_pct"]
    positions = st.get_open_positions()
    if not positions:
        return 0

    all_syms = [p["symbol"] for p in positions]
    prices   = {s: ic.abs_price(p) for s, p in ic.get_prices(ib, all_syms).items()}
    live_prices_available = any(v > 0 for v in prices.values())

    if not live_prices_available:
        print("  [heal-stop] IBKR returned no live prices (market closed) -- "
              "cannot verify order status, skipping heal to avoid false positives.")
        return 0

    # Filter to this account's orders only -- the single live Gateway exposes
    # both paper and live accounts so openTrades() may return paper GTC stops
    # whose order IDs coincidentally match live DB stop_order_ids.
    # Use account == account_id strictly; empty-account orders are NOT trusted.
    ib.reqOpenOrders()
    ib.sleep(1.0)
    open_order_ids = {
        str(t.order.orderId) for t in ib.openTrades()
        if t.order.account == account_id
    }

    # Fetch broker positions once for GTC-stop-trigger detection (lazy — only if needed)
    _broker_positions: dict[str, int] | None = None

    def _get_broker_positions() -> dict[str, int]:
        nonlocal _broker_positions
        if _broker_positions is None:
            try:
                _broker_positions = {
                    p["symbol"]: p["qty"] for p in ic.get_positions(ib, account_id)
                }
            except Exception as exc:
                print(f"  [heal-stop] WARNING: could not fetch broker positions: {exc}")
                _broker_positions = {}
        return _broker_positions

    healed = 0
    for pos in positions:
        sym      = pos["symbol"]
        stop_oid = pos.get("stop_order_id")
        qty      = int(pos.get("qty") or 0)
        price    = prices.get(sym, 0.0)

        if qty < 1:
            continue
        if price <= 0:
            print(f"  [heal-stop] {sym}: no IBKR price -- skipped")
            continue
        if stop_oid and str(stop_oid) in open_order_ids:
            continue  # stop confirmed live on IBKR

        # ── When a tracked stop order has disappeared from openTrades(), check
        # whether the position itself is still at the broker before re-placing.
        # If the position is gone, the GTC stop triggered and we must record
        # the exit rather than place a new stop on a closed position.
        if stop_oid:
            broker_pos = _get_broker_positions()
            if sym not in broker_pos:
                # Position closed at broker — GTC stop was triggered
                exit_price: float | None = None
                try:
                    ib.reqExecutions()
                    ib.sleep(0.5)
                    fills = ib.fills()
                    sell_fills = [
                        f for f in fills
                        if f.contract.symbol == sym and f.execution.side in ("SLD", "SELL")
                    ]
                    if sell_fills:
                        sell_fills.sort(key=lambda f: f.execution.time, reverse=True)
                        exit_price = float(sell_fills[0].execution.avgPrice)
                except Exception as exc:
                    print(f"  [heal-stop] {sym}: could not fetch executions: {exc}")

                strategy = pos.get("strategy", "blend")
                if exit_price and exit_price > 0:
                    import uuid as _uuid
                    sell_oid = f"heal_gtc_{sym}_{_uuid.uuid4().hex[:8]}"
                    if not dry_run:
                        st.record_order(sell_oid, sym, "SELL", qty,
                                        limit_price=None, strategy=strategy)
                        st.mark_filled(sell_oid, exit_price, side="SELL")
                        st.close_buy_position(sym, strategy)
                        _email_ibkr_fill("SELL", sym, qty, exit_price, strategy,
                                         float(pos.get("fill_price") or 0),
                                         "GTC stop triggered")
                    print(f"  [heal-stop] {sym}: GTC stop triggered @ "
                          f"${exit_price:.2f} -- exit recorded in DB"
                          + (" [DRY RUN]" if dry_run else ""))
                else:
                    if not dry_run:
                        st.close_buy_position(sym, strategy)
                    print(f"  [heal-stop] {sym}: GTC stop triggered (fill price "
                          f"unknown) -- position closed in DB"
                          + (" [DRY RUN]" if dry_run else ""))
                continue  # do NOT place a new stop on a closed position

        trail_high = float(pos.get("trailing_high") or pos.get("fill_price") or price)
        new_high   = max(trail_high, price)
        stop_price = round(new_high * (1 - stop_pct), 2)

        reason = "missing" if stop_oid else "never set"
        print(f"  [heal-stop] {sym}: stop {reason} -> ${stop_price:.2f}  "
              f"high=${new_high:.2f}  [{stop_pct*100:.0f}%]")

        if dry_run:
            print(f"  [heal-stop] {sym}: [DRY RUN] would place stop @ ${stop_price:.2f}")
            continue

        try:
            new_trade = ic.place_stop_order(ib, account_id, sym, qty, stop_price)
            ib.sleep(0.5)
            st.update_stop(sym, stop_price, str(new_trade.order.orderId), new_high,
                           strategy=pos.get("strategy"))
            print(f"  [heal-stop] {sym}: stop placed (id={new_trade.order.orderId})")
            healed += 1
        except Exception as exc:
            print(f"  [heal-stop] {sym}: ERROR placing stop: {exc}")

    if healed:
        print(f"  [heal-stop] restored {healed} missing stop(s)")
    elif not dry_run:
        print("  [heal-stop] all stops confirmed present on IBKR")
    return healed


def trail_stops(ib, account_id: str, cfg: dict, dry_run: bool = True,
                strategy: str | None = None,
                min_move_usd: float = 1.0,
                atr_strategies: list[str] | None = None) -> None:
    """Ratchet GTC stop-loss orders upward for open positions.

    Calls heal_missing_stops() first so any naked position gets a fresh stop
    before the trail pass. strategy filters which positions to trail; heal
    always covers all open positions regardless of strategy.

    min_move_usd: minimum stop improvement before submitting a new order
        (avoids churn on tiny price moves). Default $1.00 matches Saxo.
    atr_strategies: strategy names that use ATR Chandelier stops instead of
        fixed stop_pct (e.g. ["blend", "blend_v2"]).
    """
    # Heal first -- restore any stop that disappeared before trailing
    print("  [heal-stop] Checking for missing stops...")
    heal_missing_stops(ib, account_id, cfg,
                       atr_strategies=atr_strategies, dry_run=dry_run)
    print()

    stop_pct  = cfg["risk"]["stop_pct"]
    positions = st.get_open_positions(strategy=strategy) if strategy else st.get_open_positions()
    if not positions:
        label = f" ({strategy})" if strategy else ""
        print(f"  No open positions in ledger{label}.")
        return

    symbols = [p["symbol"] for p in positions]
    prices  = {s: ic.abs_price(p) for s, p in ic.get_prices(ib, symbols).items()}
    label   = f" [{strategy}]" if strategy else ""
    print(f"  Trail-stop check{label} for {len(positions)} position(s) "
          f"| stop_pct={stop_pct*100:.0f}%  min_move=${min_move_usd:.2f}\n")

    for pos in positions:
        sym           = pos["symbol"]
        cur_price     = prices.get(sym, 0.0)
        cur_stop      = pos.get("stop_price") or 0.0
        trail_high    = pos.get("trailing_high") or pos.get("fill_price") or 0.0
        stop_order_id = pos.get("stop_order_id")
        qty           = int(pos["qty"])

        if cur_price <= 0:
            print(f"  {sym:<8}  no price -- skipped")
            continue

        new_high = max(trail_high, cur_price)

        use_atr = bool(atr_strategies and pos.get("strategy") in atr_strategies)
        if use_atr:
            _atr = _ibkr_atr(sym)
            if _atr:
                new_stop = round(new_high - IBKR_ATR_MULTIPLIER * _atr, 2)
            else:
                new_stop = round(new_high * (1 - stop_pct), 2)
                print(f"  {sym:<8}  ATR unavailable, falling back to fixed {stop_pct*100:.0f}%")
                use_atr = False
        else:
            new_stop = round(new_high * (1 - stop_pct), 2)

        # Only ratchet if improvement exceeds min_move_usd to avoid order churn
        if new_stop <= cur_stop + min_move_usd:
            print(f"  {sym:<8}  price=${cur_price:.2f}  stop=${cur_stop:.2f}  "
                  f"high=${new_high:.2f}  -> no change "
                  f"(move ${new_stop - cur_stop:.2f} < ${min_move_usd:.2f})")
            continue

        print(f"  {sym:<8}  price=${cur_price:.2f}  stop ${cur_stop:.2f} -> ${new_stop:.2f}  "
              f"high=${new_high:.2f}{'  [ATR-2.5x]' if use_atr else f'  [{stop_pct*100:.0f}%]'}")

        if dry_run:
            print(f"           [DRY RUN] would ratchet stop to ${new_stop:.2f}")
            continue

        # Place new stop FIRST, then cancel old one only after confirmed.
        # Reversing the order prevents a naked position if placement fails.
        new_stop_trade = ic.place_stop_order(ib, account_id, sym, qty, new_stop)

        # Wait for new stop to reach Submitted before cancelling old one.
        # If we disconnect while it's still PreSubmitted, the order freezes in
        # that state and can never be cancelled via the API (requires GUI cancel).
        for _w in range(15):          # up to 15 × 1s = 15s
            ib.sleep(1.0)
            status = new_stop_trade.orderStatus.status
            if status in ("Submitted", "PreSubmitted"):
                # PreSubmitted is acceptable intermediate state; Submitted = confirmed
                if status == "Submitted":
                    break
            elif status in ("Filled", "Cancelled", "ApiCancelled", "Inactive"):
                break  # terminal state -- stop placed (or failed)
        if new_stop_trade.orderStatus.status != "Submitted":
            print(f"  WARNING: new stop for {sym} is '{new_stop_trade.orderStatus.status}' "
                  f"(not Submitted) -- keeping old stop to avoid frozen PreSubmitted state")
            ic.cancel_order_by_id(ib, new_stop_trade.order.orderId)
            continue

        if stop_order_id:
            open_orders = ib.openTrades()
            old_trade   = next((t for t in open_orders
                                if str(t.order.orderId) == str(stop_order_id)), None)
            if old_trade:
                ic.cancel_order(ib, old_trade)
                ib.sleep(0.5)

        st.update_stop(sym, new_stop, str(new_stop_trade.order.orderId), new_high,
                       strategy=pos.get("strategy"))
        print(f"           -> stop updated (new id={new_stop_trade.order.orderId})")

    print("\n  Trail-stop pass complete.")


# â"€â"€ US Reversion entries â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

def run_reversion_entries(ib, account_id: str, cfg: dict, dry_run: bool = True,
                          intraday: bool = False,
                          candidates: list | None = None,
                          auto: bool = False) -> None:
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
    # Yahoo Finance is for signal generation (RSI/EMA/scan) only -- never for
    # sizing or placing a live order.
    live_syms   = [c["ticker"] for c in new_cands[:slots_free]]
    live_prices = ic.get_prices(ib, live_syms)   # returns 0 if IBKR has no data

    if not dry_run and not ic.is_market_open():
        print(f"\n  [BLOCKED] US market is closed. Orders can only be placed "
              f"09:30--16:00 ET (14:30--21:00 UTC).")
        return

    per_slot = budget / max_slots

    for c in new_cands[:slots_free]:
        ibkr_price  = live_prices.get(c["ticker"], 0.0)
        ibkr_ok     = bool(ibkr_price and ibkr_price > 0)

        if not ibkr_ok:
            print(f"\n  [BLOCKED] {c['ticker']}: no IBKR live price -- skip")
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

        # AI Copilot
        _rev_open = [{"symbol": p["symbol"], "side": "BUY", "size": p.get("qty"),
                       "strategy": "us_reversion"}
                      for p in open_pos]
        _rev_dec = _ai_ibkr_score("us_reversion", c["ticker"], price, stop_price, qty,
                                   rsi14=c.get("rsi"), open_positions=_rev_open)
        _ai_skip, qty = _ai_ibkr_apply(_rev_dec, c["ticker"], qty, label)
        if _ai_skip:
            continue

        print(f"\n  [{label}] BUY  {c['ticker']:<8}  "
              f"RSI={c['rsi']:.0f}  dip={c['dip_pct']}%  vol={c['vol_ratio']}x")
        print(f"    qty={qty}  price~${price:.2f} [{price_src}]  "
              f"notional~${notional:,.0f}  stop=${stop_price:.2f}")

        if dry_run:
            print("    [DRY RUN] would place buy + stop")
            continue

        confirm = "y" if auto else input(f"  Confirm buy {c['ticker']}? [y/N]: ").strip().lower()
        if confirm != "y":
            print("  Skipped.")
            continue

        fill = None
        fill_trade = None
        for attempt in range(2):
            if attempt > 0:
                print(f"  Retrying buy {c['ticker']} (attempt {attempt + 1}/2)...")
                ib.sleep(5.0)
            _t = ic.place_market_order(ib, account_id, c["ticker"], "BUY", qty)
            st.record_order(str(_t.order.orderId), c["ticker"], "BUY", qty, strategy="reversion")
            print(f"  Order placed (id={_t.order.orderId}). Waiting for fill...")
            _f = ic.confirm_fill(ib, _t)
            if _f is None:
                st.mark_cancelled(str(_t.order.orderId))
            else:
                fill = _f
                fill_trade = _t
                break

        if fill is None:
            print(f"  WARNING: fill not confirmed for {c['ticker']} after 2 attempts.")
            _email_ibkr_alert(
                f"Buy failed: {c['ticker']}",
                f"BUY {qty} {c['ticker']} (reversion) failed after 2 attempts. "
                f"RETRY task fires in ~25 min. If no cash, blend sells may not have run yet.",
                strategy="reversion",
            )
            continue

        print(f"  Filled @ ${fill:.4f}")
        st.mark_filled(str(fill_trade.order.orderId), fill, side="BUY")
        actual_stop = round(fill * (1 - stop_pct), 2)
        stop_trade, _ = _place_stop_submitted(ib, account_id, c["ticker"], qty,
                                              actual_stop, strategy=label)
        _email_ibkr_fill("BUY", c["ticker"], qty, fill, label)
        st.update_stop(c["ticker"], actual_stop, str(stop_trade.order.orderId), fill)
        print(f"  Stop placed @ ${actual_stop:.2f} (id={stop_trade.order.orderId})")

        if _sc and _ai_cfg and _ai_cfg.stocks_enabled(_ibkr_account_env(account_id)):
            _entry_date = datetime.date.today().isoformat()
            _sc.log_stock_entry_card(
                strategy="us_reversion", ticker=c["ticker"], direction="BUY",
                entry_price=fill, shares=qty, stop_price=actual_stop,
                sek_per_eur=None, entry_date=_entry_date,
                risk_sek=round((fill - actual_stop) * qty, 2),
                rsi_at_entry=c.get("rsi"),
                account_env=_ibkr_account_env(account_id), native_currency="USD",
            )

    print(f"\n  [{label}] entry scan complete.")


# â"€â"€ US Reversion exits â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

def run_reversion_exits(ib, account_id: str, cfg: dict, dry_run: bool = True,
                        indicators: dict | None = None,
                        auto: bool = False) -> None:
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

        # IBKR live price only -- no Yahoo fallback per trading rules.
        cur_price = ibkr_prices.get(sym, 0.0)
        ind       = indicators.get(sym, {})

        if not cur_price or cur_price <= 0:
            print(f"  {sym:<8}  [BLOCKED] no IBKR live price -- skipped")
            continue

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
              f"{'-> EXIT: ' + reason if should_exit else 'HOLD'}")

        if not should_exit:
            continue

        if dry_run:
            print(f"    [DRY RUN] would sell {qty} {sym}")
            continue

        confirm = "y" if auto else input(f"  Confirm EXIT {sym}? [y/N]: ").strip().lower()
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
            print(f"  WARNING: exit fill not confirmed for {sym} -- sell cancelled, will retry next cycle.")
            st.mark_cancelled(str(sell_trade.order.orderId))
        else:
            pnl = (fill - entry_px) * qty
            print(f"  Sold {qty} {sym} @ ${fill:.4f}  P&L: ${pnl:+,.2f}")
            st.mark_filled(str(sell_trade.order.orderId), fill, side="SELL")
            st.close_buy_position(sym, "reversion")
            _email_ibkr_fill("SELL", sym, qty, fill, "reversion", entry_px)

            if _sc and _ai_cfg and _ai_cfg.stocks_enabled(_ibkr_account_env(account_id)):
                _acenv  = _ibkr_account_env(account_id)
                _edate  = (pos.get("filled_at") or pos.get("created_at") or "")[:10]
                _risk   = round((entry_px - float(pos.get("stop_price") or 0)) * qty, 2) if pos.get("stop_price") else None
                _gpnl   = round(pnl, 2)
                _npnl   = round(_gpnl - 1.0, 2)
                try:
                    _fdt  = datetime.datetime.fromisoformat((pos.get("filled_at") or "").replace("Z", "+00:00"))
                    _hhrs = round((datetime.datetime.now(datetime.timezone.utc) - _fdt).total_seconds() / 3600, 1)
                except Exception:
                    _hhrs = None
                _cid = _sc.card_id_for("us_reversion", sym, _edate, _acenv)
                _sc.log_stock_exit_card(
                    card_id=_cid, exit_price=fill, exit_reason=reason,
                    gross_pnl_sek=_gpnl, commission_sek=1.0, net_pnl_sek=_npnl,
                    holding_hours=_hhrs, sek_per_eur=None,
                    risk_sek=_risk, native_currency="USD",
                )

    print("\n  Reversion exit check complete.")


# ── US Penny entries ──────────────────────────────────────────────────────────

def run_penny_entries(ib, account_id: str, cfg: dict, dry_run: bool = True,
                      candidates: list | None = None,
                      auto: bool = False) -> None:
    """SIM-ONLY penny stock momentum breakout entries.

    candidates: pre-generated from ibkr_signals.penny_candidates(). If None,
      generated here (holds the IBKR connection open during Yahoo download).
      Prefer passing from main() so the connection is held for seconds only.

    SIM-ONLY: the --live gate in run_ibkr_stocks.py only allows 'blend' on live,
      so this function will never execute against a live account in normal use.
    """
    from atos import us_penny as _penny

    penny_cfg  = cfg["strategies"].get("penny", {})
    max_slots  = int(penny_cfg.get("max_slots", 3))
    stop_pct   = float(penny_cfg.get("stop_pct", 0.12))
    budget     = float(penny_cfg.get("budget_usd", 6000))
    min_usd    = float(penny_cfg.get("min_trade_usd", 50))
    max_price  = float(penny_cfg.get("max_price_usd", 2.20))

    open_pos   = st.get_open_positions("penny")
    open_syms  = {p["symbol"] for p in open_pos}
    slots_free = max_slots - len(open_pos)

    print(f"\n  [penny] {len(open_pos)}/{max_slots} slots used  ({slots_free} free)  [SIM-ONLY]")

    if slots_free <= 0:
        print("  All penny slots full.")
        return

    if candidates is None:
        candidates = sig.penny_candidates()
    new_cands = [c for c in candidates if c["ticker"] not in open_syms]

    if not new_cands:
        print("  No new penny candidates.")
        return

    live_syms   = [c["ticker"] for c in new_cands[:slots_free]]
    live_prices = ic.get_prices(ib, live_syms)

    if not dry_run and not ic.is_market_open():
        print(f"\n  [BLOCKED] US market is closed. Orders can only be placed "
              f"09:30--16:00 ET (14:30--21:00 UTC).")
        return

    per_slot = budget / max_slots

    for c in new_cands[:slots_free]:
        ibkr_price = live_prices.get(c["ticker"], 0.0)
        ibkr_ok    = bool(ibkr_price and ibkr_price > 0)

        if not ibkr_ok:
            print(f"\n  [BLOCKED] {c['ticker']}: no IBKR live price -- skip")
            continue
        price = ibkr_price

        if price > max_price:
            print(f"\n  [SKIP] {c['ticker']}: live price ${price:.2f} > ${max_price:.2f} gate")
            continue

        qty      = math.floor(per_slot / price)
        if qty < 1:
            continue
        notional = round(price * qty, 2)
        if notional < min_usd:
            continue
        stop_price = round(price * (1 - stop_pct), 2)

        # AI Copilot
        _pny_open = [{"symbol": p["symbol"], "side": "BUY", "size": p.get("qty"),
                       "strategy": "us_penny"}
                      for p in open_pos]
        _pny_dec = _ai_ibkr_score("us_penny", c["ticker"], price, stop_price, qty,
                                   open_positions=_pny_open)
        _ai_skip, qty = _ai_ibkr_apply(_pny_dec, c["ticker"], qty, "penny")
        if _ai_skip:
            continue

        print(f"\n  [penny] BUY  {c['ticker']:<8}  "
              f"vol={c['vol_ratio']}x  above_don=+{c['pct_above_don']}%  score={c['score']:.2f}")
        print(f"    qty={qty}  price~${price:.2f} [IBKR live]  "
              f"notional~${notional:,.0f}  stop=${stop_price:.2f}  [SIM-ONLY]")

        if dry_run:
            print("    [DRY RUN] would place buy + stop")
            continue

        confirm = "y" if auto else input(f"  Confirm buy {c['ticker']}? [y/N]: ").strip().lower()
        if confirm != "y":
            print("  Skipped.")
            continue

        trade = ic.place_market_order(ib, account_id, c["ticker"], "BUY", qty)
        st.record_order(str(trade.order.orderId), c["ticker"], "BUY", qty, strategy="penny")
        print(f"  Order placed (id={trade.order.orderId}). Waiting for fill...")
        fill = ic.confirm_fill(ib, trade)
        if fill is None:
            print(f"  WARNING: fill not confirmed for {c['ticker']}.")
            st.mark_cancelled(str(trade.order.orderId))
            continue

        print(f"  Filled @ ${fill:.4f}")
        st.mark_filled(str(trade.order.orderId), fill, side="BUY")
        actual_stop = round(fill * (1 - stop_pct), 2)
        stop_trade, _ = _place_stop_submitted(ib, account_id, c["ticker"], qty,
                                              actual_stop, strategy="penny")
        _email_ibkr_fill("BUY", c["ticker"], qty, fill, "penny")
        st.update_stop(c["ticker"], actual_stop, str(stop_trade.order.orderId), fill)
        print(f"  Stop placed @ ${actual_stop:.2f} (id={stop_trade.order.orderId})")

    print(f"\n  [penny] entry scan complete.")


# ── US Penny exits ────────────────────────────────────────────────────────────

def run_penny_exits(ib, account_id: str, cfg: dict, dry_run: bool = True,
                    auto: bool = False) -> None:
    """Check open penny positions for exit conditions and close if triggered.

    Exits: +25% profit target | -12% hard stop | 15-day time stop.
    IBKR live price only -- no Yahoo fallback per trading rules.
    """
    from atos import us_penny as _penny

    open_pos = st.get_open_positions("penny")
    if not open_pos:
        print("  No open penny positions.")
        return

    symbols     = [p["symbol"] for p in open_pos]
    ibkr_prices = {s: ic.abs_price(p) for s, p in ic.get_prices(ib, symbols).items()}
    today       = datetime.date.today()

    print(f"\n  [penny exits] {len(open_pos)} position(s)")

    for pos in open_pos:
        sym      = pos["symbol"]
        entry_px = float(pos.get("fill_price") or 0)
        stop_oid = pos.get("stop_order_id")
        qty      = int(pos["qty"])

        cur_price = ibkr_prices.get(sym, 0.0)
        if not cur_price or cur_price <= 0:
            print(f"  {sym:<8}  [BLOCKED] no IBKR live price -- skipped")
            continue

        filled_at_str = pos.get("filled_at") or pos.get("created_at", "")
        filled_date   = datetime.date.fromisoformat(filled_at_str[:10])
        td_held       = max(0, len(pd.bdate_range(filled_date, today)) - 1)

        trade_dict = {"entry_price": entry_px, "days_held": td_held}
        should_exit, reason = _penny.should_exit(trade_dict, cur_price)

        print(f"  {sym:<8}  px=${cur_price:.2f}  entry=${entry_px:.2f}  "
              f"held={td_held}d  "
              f"{'-> EXIT: ' + reason if should_exit else 'HOLD'}")

        if not should_exit:
            continue

        if dry_run:
            print(f"    [DRY RUN] would sell {qty} {sym}")
            continue

        confirm = "y" if auto else input(f"  Confirm EXIT {sym}? [y/N]: ").strip().lower()
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
        st.record_order(str(sell_trade.order.orderId), sym, "SELL", qty, strategy="penny")
        fill = ic.confirm_fill(ib, sell_trade)
        if fill is None:
            print(f"  WARNING: exit fill not confirmed for {sym} -- sell cancelled, will retry next cycle.")
            st.mark_cancelled(str(sell_trade.order.orderId))
        else:
            pnl = (fill - entry_px) * qty
            print(f"  Sold {qty} {sym} @ ${fill:.4f}  P&L: ${pnl:+,.2f}")
            st.mark_filled(str(sell_trade.order.orderId), fill, side="SELL")
            st.close_buy_position(sym, "penny")
            _email_ibkr_fill("SELL", sym, qty, fill, "penny", entry_px)

    print("\n  Penny exit check complete.")


# ── US Bagger entries ─────────────────────────────────────────────────────────

def run_bagger_entries(ib, account_id: str, cfg: dict, dry_run: bool = True,
                       candidates: list | None = None,
                       auto: bool = False) -> None:
    """SIM-ONLY momentum bagger entries (80%+ 6m ROC, 12% trailing stop).

    No broker stop order placed — trailing stop is managed in software each cycle.
    SIM-ONLY: never promoted to LIVE without a separate written go/no-go.
    """
    from atos import us_bagger as _UBG

    bag_cfg   = cfg["strategies"].get("bagger", {})
    max_slots = int(bag_cfg.get("max_slots", 5))
    budget    = float(bag_cfg.get("budget_usd", 7000))
    min_usd   = float(bag_cfg.get("min_trade_usd", 200))

    open_pos   = st.get_open_positions("bagger")
    open_syms  = {p["symbol"] for p in open_pos}
    slots_free = max_slots - len(open_pos)

    print(f"\n  [bagger] {len(open_pos)}/{max_slots} slots used  ({slots_free} free)  [SIM-ONLY]")

    if slots_free <= 0:
        print("  All bagger slots full.")
        return

    if candidates is None:
        candidates = sig.bagger_candidates()
    new_cands = [c for c in candidates if c["ticker"] not in open_syms]

    if not new_cands:
        print("  No new bagger candidates.")
        return

    live_syms   = [c["ticker"] for c in new_cands[:slots_free]]
    live_prices = ic.get_prices(ib, live_syms)

    if not dry_run and not ic.is_market_open():
        print(f"\n  [BLOCKED] US market is closed. Orders can only be placed "
              f"09:30--16:00 ET (14:30--21:00 UTC).")
        return

    per_slot = budget / max_slots

    for c in new_cands[:slots_free]:
        ibkr_price = live_prices.get(c["ticker"], 0.0)
        ibkr_ok    = bool(ibkr_price and ibkr_price > 0)

        if not ibkr_ok:
            print(f"\n  [BLOCKED] {c['ticker']}: no IBKR live price -- skip")
            continue
        price = ibkr_price

        qty      = math.floor(per_slot / price)
        if qty < 1:
            continue
        notional = round(price * qty, 2)
        if notional < min_usd:
            continue
        stop_price = round(price * (1 - _UBG.TRAILING_STOP_PCT), 4)

        # AI Copilot
        _bg_open = [{"symbol": p["symbol"], "side": "BUY", "size": p.get("qty"),
                      "strategy": "us_bagger"}
                     for p in open_pos]
        _bg_dec = _ai_ibkr_score("us_bagger", c["ticker"], price, stop_price, qty,
                                  rsi14=c.get("rsi"), open_positions=_bg_open)
        _ai_skip, qty = _ai_ibkr_apply(_bg_dec, c["ticker"], qty, "bagger")
        if _ai_skip:
            continue

        notional = round(price * qty, 2)
        print(f"\n  [bagger] BUY  {c['ticker']:<8}  "
              f"roc_6m={c['roc_6m']}%  rsi={c['rsi']}  "
              f"from_high={c['pct_from_high']}%  score={c['score']:.2f}")
        print(f"    qty={qty}  price~${price:.2f} [IBKR live]  "
              f"notional~${notional:,.0f}  trail_stop=12%  [SIM-ONLY]")

        if dry_run:
            print("    [DRY RUN] would place buy")
            continue

        confirm = "y" if auto else input(f"  Confirm buy {c['ticker']}? [y/N]: ").strip().lower()
        if confirm != "y":
            print("  Skipped.")
            continue

        trade = ic.place_market_order(ib, account_id, c["ticker"], "BUY", qty)
        st.record_order(str(trade.order.orderId), c["ticker"], "BUY", qty, strategy="bagger")
        print(f"  Order placed (id={trade.order.orderId}). Waiting for fill...")
        fill = ic.confirm_fill(ib, trade)
        if fill is None:
            print(f"  WARNING: fill not confirmed for {c['ticker']}.")
            st.mark_cancelled(str(trade.order.orderId))
            continue

        print(f"  Filled @ ${fill:.4f}  trailing_high = ${fill:.4f}")
        st.mark_filled(str(trade.order.orderId), fill, side="BUY")
        _email_ibkr_fill("BUY", c["ticker"], qty, fill, "bagger")
        # mark_filled sets trailing_high = fill_price; no broker stop order.

    print(f"\n  [bagger] entry scan complete.")


# ── US Bagger exits ───────────────────────────────────────────────────────────

def run_bagger_exits(ib, account_id: str, cfg: dict, dry_run: bool = True,
                     auto: bool = False) -> None:
    """Check open bagger positions for exit conditions and close if triggered.

    Updates trailing_high in DB from live IBKR price each cycle.
    Exits: 12% trailing stop | overbought exhaustion RSI>80 | 60-day time stop.
    IBKR live price only -- no Yahoo fallback per trading rules.
    """
    from atos import us_bagger as _UBG

    open_pos = st.get_open_positions("bagger")
    if not open_pos:
        print("  No open bagger positions.")
        return

    symbols     = [p["symbol"] for p in open_pos]
    ibkr_prices = {s: ic.abs_price(p) for s, p in ic.get_prices(ib, symbols).items()}

    print(f"\n  [bagger exits] {len(open_pos)} position(s)")

    for pos in open_pos:
        sym       = pos["symbol"]
        entry_px  = float(pos.get("fill_price") or 0)
        stored_th = float(pos.get("trailing_high") or entry_px or 0)
        qty       = int(pos["qty"])

        cur_price = ibkr_prices.get(sym, 0.0)
        if not cur_price or cur_price <= 0:
            print(f"  {sym:<8}  [BLOCKED] no IBKR live price -- skipped")
            continue

        new_th = max(stored_th, cur_price)
        if new_th > stored_th:
            st.update_stop(sym, 0.0, "", new_th, strategy="bagger")

        filled_at_str = pos.get("filled_at") or pos.get("created_at", "")
        trade_dict = {
            "entry_price": entry_px,
            "entry_date":  filled_at_str[:10] if filled_at_str else "",
        }
        trail_stop = round(new_th * (1 - _UBG.TRAILING_STOP_PCT), 2)
        should_exit, reason = _UBG.should_exit(trade_dict, cur_price, new_th)

        print(f"  {sym:<8}  px=${cur_price:.2f}  high=${new_th:.2f}  "
              f"stop=${trail_stop:.2f}  "
              f"{'-> EXIT: ' + reason if should_exit else 'HOLD'}")

        if not should_exit:
            continue

        if dry_run:
            print(f"    [DRY RUN] would sell {qty} {sym}")
            continue

        confirm = "y" if auto else input(f"  Confirm EXIT {sym}? [y/N]: ").strip().lower()
        if confirm != "y":
            print("  Skipped.")
            continue

        sell_trade = ic.place_market_order(ib, account_id, sym, "SELL", qty)
        st.record_order(str(sell_trade.order.orderId), sym, "SELL", qty, strategy="bagger")
        fill = ic.confirm_fill(ib, sell_trade)
        if fill is None:
            print(f"  WARNING: exit fill not confirmed for {sym} -- sell cancelled, will retry next cycle.")
            st.mark_cancelled(str(sell_trade.order.orderId))
        else:
            pnl = (fill - entry_px) * qty
            print(f"  Sold {qty} {sym} @ ${fill:.4f}  P&L: ${pnl:+,.2f}")
            st.mark_filled(str(sell_trade.order.orderId), fill, side="SELL")
            st.close_buy_position(sym, "bagger")
            _email_ibkr_fill("SELL", sym, qty, fill, "bagger", entry_px)

    print("\n  Bagger exit check complete.")


# -- US Reversion V2 entries ---------------------------------------------------

def run_reversion_v2_entries(ib, account_id: str, cfg: dict, dry_run: bool = True,
                              candidates: list | None = None,
                              auto: bool = False) -> None:
    """US Reversion V2 entry scan -- uses reversion_v2 state key so v1/v2 positions
    are tracked independently. candidates: from ibkr_signals.reversion_v2_candidates().
    """
    from atos import us_reversion_v2 as _rev2

    rev_cfg   = cfg["strategies"].get("reversion_v2", cfg["strategies"]["reversion"])
    max_slots = rev_cfg["max_slots"]
    stop_pct  = _rev2.STOP_PCT
    min_usd   = rev_cfg.get("min_trade_usd", 50)
    budget    = rev_cfg["budget_usd"]

    open_pos   = st.get_open_positions("reversion_v2")
    open_syms  = {p["symbol"] for p in open_pos}
    slots_free = max_slots - len(open_pos)

    print(f"\n  [reversion_v2] {len(open_pos)}/{max_slots} slots used  ({slots_free} free)")

    if slots_free <= 0:
        print("  All reversion_v2 slots full.")
        return

    if candidates is None:
        candidates = sig.reversion_v2_candidates()
    new_cands  = [c for c in candidates if c["ticker"] not in open_syms]

    if not new_cands:
        print("  No new reversion_v2 candidates.")
        return

    live_syms   = [c["ticker"] for c in new_cands[:slots_free]]
    live_prices = ic.get_prices(ib, live_syms)

    if not dry_run and not ic.is_market_open():
        print("\n  [BLOCKED] US market is closed.")
        return

    per_slot = budget / max_slots

    for c in new_cands[:slots_free]:
        ibkr_price  = live_prices.get(c["ticker"], 0.0)
        ibkr_ok     = bool(ibkr_price and ibkr_price > 0)

        if not ibkr_ok:
            print(f"\n  [BLOCKED] {c['ticker']}: no IBKR live price -- skip")
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

        # AI Copilot
        _rv2_open = [{"symbol": p["symbol"], "side": "BUY", "size": p.get("qty"),
                       "strategy": "us_reversion_v2"}
                      for p in open_pos]
        _rv2_dec = _ai_ibkr_score("us_reversion_v2", c["ticker"], price, stop_price, qty,
                                   rsi14=c.get("rsi"), open_positions=_rv2_open)
        _ai_skip, qty = _ai_ibkr_apply(_rv2_dec, c["ticker"], qty, "reversion_v2")
        if _ai_skip:
            continue
        notional = round(price * qty, 2)

        print(f"\n  [reversion_v2] BUY  {c['ticker']:<8}  "
              f"RSI={c['rsi']:.0f}  dip={c['dip_pct']}%  vol={c['vol_ratio']}x  R:R={c['rr_ratio']}")
        print(f"    qty={qty}  price~${price:.2f} [{price_src}]  "
              f"notional~${notional:,.0f}  stop=${stop_price:.2f}")

        if dry_run:
            print("    [DRY RUN] would place buy + stop")
            continue

        confirm = "y" if auto else input(f"  Confirm buy {c['ticker']}? [y/N]: ").strip().lower()
        if confirm != "y":
            print("  Skipped.")
            continue

        trade = ic.place_market_order(ib, account_id, c["ticker"], "BUY", qty)
        st.record_order(str(trade.order.orderId), c["ticker"], "BUY", qty, strategy="reversion_v2")
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
        _email_ibkr_fill("BUY", c["ticker"], qty, fill, "reversion_v2")
        st.update_stop(c["ticker"], actual_stop, str(stop_trade.order.orderId), fill)
        print(f"  Stop placed @ ${actual_stop:.2f} (id={stop_trade.order.orderId})")

    print("\n  [reversion_v2] entry scan complete.")


def run_reversion_v2_exits(ib, account_id: str, cfg: dict, dry_run: bool = True,
                            indicators: dict | None = None,
                            auto: bool = False) -> None:
    """Exit check for open US Reversion V2 positions."""
    from atos import us_reversion_v2 as _rev2

    open_pos = st.get_open_positions("reversion_v2")
    if not open_pos:
        print("  No open reversion_v2 positions.")
        return

    symbols = [p["symbol"] for p in open_pos]
    if indicators is None:
        indicators = sig.reversion_v2_exit_indicators(symbols)
    ibkr_prices = {s: ic.abs_price(p) for s, p in ic.get_prices(ib, symbols).items()}
    today       = datetime.date.today()

    print(f"\n  [reversion_v2 exits] {len(open_pos)} position(s)")

    for pos in open_pos:
        sym       = pos["symbol"]
        entry_px  = float(pos.get("fill_price") or 0)
        stop_oid  = pos.get("stop_order_id")
        qty       = int(pos["qty"])

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
        should_exit, reason = _rev2.should_exit(
            trade_dict, cur_price, current_rsi, sma20, td_held
        )

        rsi_str = f"{current_rsi:.0f}" if current_rsi is not None else "n/a"
        hold_or_exit = ("-> EXIT: " + reason) if should_exit else "HOLD"
        print(f"  {sym:<8}  px=${cur_price:.2f}  entry=${entry_px:.2f}  "
              f"rsi={rsi_str}  held={td_held}d  {hold_or_exit}")

        if not should_exit:
            continue

        if dry_run:
            print(f"    [DRY RUN] would sell {qty} {sym}")
            continue

        confirm = "y" if auto else input(f"  Confirm EXIT {sym}? [y/N]: ").strip().lower()
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
        st.record_order(str(sell_trade.order.orderId), sym, "SELL", qty, strategy="reversion_v2")
        fill = ic.confirm_fill(ib, sell_trade)
        if fill is None:
            print(f"  WARNING: exit fill not confirmed for {sym} -- sell cancelled, will retry next cycle.")
            st.mark_cancelled(str(sell_trade.order.orderId))
        else:
            pnl = (fill - entry_px) * qty
            print(f"  Sold {qty} {sym} @ ${fill:.4f}  P&L: ${pnl:+,.2f}")
            st.mark_filled(str(sell_trade.order.orderId), fill, side="SELL")
            st.close_buy_position(sym, "reversion_v2")
            _email_ibkr_fill("SELL", sym, qty, fill, "reversion_v2", entry_px)

    print("\n  Reversion v2 exit check complete.")



# â"€â"€ US Signals entries (SMA Crossover / RSI Reversal / Momentum / Ensemble) â"€â"€

def run_us_signals_entries(ib, account_id: str, cfg: dict, dry_run: bool = True,
                            feat_data: dict | None = None,
                            auto: bool = False) -> None:
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

    # Count currently open positions per (ticker, strategy) -- a ticker may be
    # held by multiple strategies simultaneously.
    open_by_strategy: dict[str, set[str]] = {s: set() for s in ALL_SIGNAL_STRATEGY_NAMES}
    for pos in st.get_open_positions():
        strat = pos.get("strategy", "")
        if strat in open_by_strategy:
            open_by_strategy[strat].add(pos["symbol"])

    print("\n  [us signals] scanning for BUY signals across 4 strategies...")

    # Collect all raw signals (no slot cap yet -- apply it after sorting by confidence).
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
              "09:30--16:00 ET (14:30--21:00 UTC).")
        return

    sig_tickers  = list({s["ticker"] for s in signals_found})
    live_prices  = ic.get_prices(ib, sig_tickers)
    ibkr_any_ok  = any(v and v > 0 for v in live_prices.values())
    if not ibkr_any_ok:
        print("  [WARNING] IBKR returned no live prices. All tickers will be [BLOCKED].")

    if not dry_run and not ibkr_any_ok:
        print("\n  [BLOCKED] No IBKR live prices. Cannot execute without market data.")
        return

    for row in signals_found:
        ticker = row["ticker"]
        strat  = row["strategy_name"]
        df     = row["_df"]

        ibkr_price  = live_prices.get(ticker, 0.0)
        ibkr_ok     = bool(ibkr_price and ibkr_price > 0)

        if not ibkr_ok:
            print(f"  [BLOCKED] {ticker} ({strat}): no IBKR live price -- skip")
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

        # AI Copilot
        _sig_open = [{"symbol": p["symbol"], "side": "BUY", "size": p.get("qty"),
                       "strategy": p.get("strategy", strat)}
                      for p in st.get_open_positions()
                      if p.get("strategy") in (strat,)]
        _sig_dec = _ai_ibkr_score(strat.lower().replace(" ", "_"), ticker, price,
                                   stop_price, qty, regime_bars=df,
                                   open_positions=_sig_open)
        _ai_skip, qty = _ai_ibkr_apply(_sig_dec, ticker, qty, strat)
        if _ai_skip:
            continue
        notional = round(price * qty, 2)

        reason_short = (row.get("reason") or "")[:50]
        print(f"\n  [{strat}]  BUY {ticker:<8}  conf={row.get('confidence', 0):.2f}  {reason_short}")
        print(f"    qty={qty}  price~${price:.2f} [{price_src}]  "
              f"notional~${notional:,.0f}  stop=${stop_price:.2f}")

        if dry_run:
            print("    [DRY RUN] would place buy + stop")
            continue

        confirm = "y" if auto else input(f"  Confirm buy {ticker} ({strat})? [y/N]: ").strip().lower()
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
        _email_ibkr_fill("BUY", ticker, qty, fill, strat)
        actual_stop = compute_stop(df, fill)
        stop_trade  = ic.place_stop_order(ib, account_id, ticker, qty, actual_stop)
        ib.sleep(1.0)
        st.update_stop(ticker, actual_stop, str(stop_trade.order.orderId), fill,
                       strategy=strat)
        print(f"  Stop placed @ ${actual_stop:.2f} (id={stop_trade.order.orderId})")
        open_by_strategy[strat].add(ticker)

    print("\n  [us signals] entry scan complete.")


# â"€â"€ US Signals exits â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

def run_us_signals_exits(ib, account_id: str, cfg: dict, dry_run: bool = True,
                          feat_data: dict | None = None,
                          auto: bool = False) -> None:
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
            print(f"  {sym:<8}  [{strat[:14]:<14}]  no data -- skipped")
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
              f"{'-> EXIT: ' + reason if exit_flag else 'HOLD'}")

        if not exit_flag:
            continue

        if dry_run:
            print(f"    [DRY RUN] would sell {qty} {sym}")
            continue

        confirm = "y" if auto else input(f"  Confirm EXIT {sym} ({strat})? [y/N]: ").strip().lower()
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
            print(f"  WARNING: exit fill not confirmed for {sym} -- sell cancelled, will retry next cycle.")
            st.mark_cancelled(str(sell_trade.order.orderId))
        else:
            pnl = (fill - entry_px) * qty
            print(f"  Sold {qty} {sym} @ ${fill:.4f}  P&L: ${pnl:+,.2f}")
            st.mark_filled(str(sell_trade.order.orderId), fill, side="SELL")
            st.close_buy_position(sym, strat)
            _email_ibkr_fill("SELL", sym, qty, fill, strat, entry_px)

    print("\n  [us signals] exit check complete.")


# â"€â"€ Scorer entries (ATOS US 500 scoring engine) â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

def run_scorer_entries(
    ib,
    account_id: str,
    cfg: dict,
    dry_run: bool = True,
    scorer_results: dict | None = None,
    auto: bool = False,
) -> None:
    """Buy top-scored candidates from the ATOS US 500 scoring engine.

    Runs two sub-books in the same call:
      scorer_swing     -- top Swing/Momentum picks (tighter stop, faster signals)
      scorer_portfolio -- top Hybrid/Portfolio picks (wider stop, quality bias)

    scorer_results: pre-generated dict from ibkr_scorer.run_scan(). Pass from
    main() so the Yahoo download finishes before the IBKR connection opens.
    """
    scorer_cfg   = cfg.get("strategies", {}).get("scorer", {})
    sw_cfg       = scorer_cfg.get("swing",     {})
    po_cfg       = scorer_cfg.get("portfolio", {})

    sw_budget    = float(sw_cfg.get("budget_usd",    20_000))
    sw_max_pos   = int(sw_cfg.get("max_positions",   8))
    sw_stop_pct  = float(sw_cfg.get("stop_pct",      0.06))
    sw_min_score = float(sw_cfg.get("min_score",     65.0))
    sw_min_usd   = float(sw_cfg.get("min_trade_usd", 50))

    po_budget    = float(po_cfg.get("budget_usd",    30_000))
    po_max_pos   = int(po_cfg.get("max_positions",   10))
    po_stop_pct  = float(po_cfg.get("stop_pct",      0.08))
    po_min_score = float(po_cfg.get("min_score",     65.0))
    po_min_usd   = float(po_cfg.get("min_trade_usd", 50))

    if scorer_results is None:
        from ibkr_module.ibkr_scorer import run_scan
        scorer_results = run_scan(
            n_swing=sw_max_pos,
            n_portfolio=po_max_pos,
            min_score=min(sw_min_score, po_min_score),
        )

    sw_df = scorer_results.get("swing",     pd.DataFrame())
    po_df = scorer_results.get("portfolio", pd.DataFrame())

    # Already-held tickers per sub-book
    sw_held_syms = {p["symbol"] for p in st.get_open_positions("scorer_swing")}
    po_held_syms = {p["symbol"] for p in st.get_open_positions("scorer_portfolio")}

    sw_free = sw_max_pos - len(sw_held_syms)
    po_free = po_max_pos - len(po_held_syms)

    print(f"\n  [scorer] swing:     {len(sw_held_syms)}/{sw_max_pos} slots used  "
          f"({sw_free} free)")
    print(f"  [scorer] portfolio: {len(po_held_syms)}/{po_max_pos} slots used  "
          f"({po_free} free)")

    # Filter to candidates that pass min_score and aren't already held
    sw_new = (sw_df[
        (sw_df["swing_score"] >= sw_min_score) &
        (~sw_df["ticker"].str.upper().isin(sw_held_syms))
    ].head(sw_free) if not sw_df.empty else pd.DataFrame())

    po_new = (po_df[
        (po_df["trade_score"] >= po_min_score) &
        (~po_df["ticker"].str.upper().isin(po_held_syms))
    ].head(po_free) if not po_df.empty else pd.DataFrame())

    if sw_new.empty and po_new.empty:
        print("  [scorer] No new candidates above min_score / slots full.")
        return

    # Collect all candidate tickers and fetch live IBKR prices once
    all_new_tickers = list(sw_new["ticker"]) + list(po_new["ticker"])
    print(f"\n  [scorer] fetching IBKR live prices for "
          f"{len(all_new_tickers)} candidate(s)...")
    live_prices = ic.get_prices(ib, all_new_tickers)
    ibkr_any_ok = any(v and v > 0 for v in live_prices.values())

    if not dry_run and not ic.is_market_open():
        print("\n  [BLOCKED] US market is closed. Orders can only be placed "
              "09:30--16:00 ET (14:30--21:00 UTC).")
        return

    if not dry_run and not ibkr_any_ok:
        print("\n  [BLOCKED] IBKR returned no live prices. Cannot size orders without market data.")
        return

    is_paper = cfg.get("paper", True)
    if auto and not is_paper:
        print("  [scorer] --auto flag is only permitted on paper accounts. Ignored.")
        auto = False

    def _place_book(
        candidates: pd.DataFrame,
        budget: float,
        max_pos: int,
        stop_pct: float,
        min_usd: float,
        strategy: str,
        score_col: str,
    ) -> None:
        if candidates.empty:
            return
        per_slot = budget / max_pos
        label    = strategy.replace("scorer_", "")

        for _, row in candidates.iterrows():
            ticker    = str(row["ticker"]).upper()
            score     = float(row.get(score_col, 0))
            setup     = str(row.get("setup", ""))
            ibkr_px   = live_prices.get(ticker, 0.0)
            ibkr_ok   = bool(ibkr_px and ibkr_px > 0)

            if not ibkr_ok:
                print(f"\n  [scorer/{label}] {ticker}: no IBKR live price -- skip")
                continue
            price     = ibkr_px
            price_src = "IBKR live"

            if not price or price <= 0:
                continue
            qty      = math.floor(per_slot / price)
            if qty < 1:
                continue
            notional = round(price * qty, 2)
            if notional < min_usd:
                continue
            stop_px  = round(price * (1 - stop_pct), 2)

            # AI Copilot
            _sc_open = [{"symbol": p["symbol"], "side": "BUY", "size": p.get("qty"),
                          "strategy": strategy}
                         for p in st.get_open_positions(strategy)]
            _sc_rsi = float(row.get("rsi_14") or 0) or None
            _sc_dec = _ai_ibkr_score(strategy, ticker, price, stop_px, qty,
                                      rsi14=_sc_rsi, open_positions=_sc_open)
            _ai_skip_sc, qty = _ai_ibkr_apply(_sc_dec, ticker, qty, f"scorer/{label}")
            if _ai_skip_sc:
                continue
            notional = round(price * qty, 2)

            roc  = float(row.get("roc_20d",  0))
            adx  = float(row.get("adx_14",   0))
            atr  = float(row.get("atr_pct",  0))
            print(f"\n  [scorer/{label}]  BUY {ticker:<6}  "
                  f"grade={setup}  {score_col}={score:.1f}  "
                  f"roc20={roc:+.1f}%  adx={adx:.0f}  atr={atr:.1f}%")
            print(f"    qty={qty}  price~${price:.2f} [{price_src}]  "
                  f"notional~${notional:,.0f}  stop=${stop_px:.2f} ({stop_pct*100:.0f}%)")

            if dry_run:
                print("    [DRY RUN] would place market buy + GTC stop")
                continue

            if auto:
                print(f"  [AUTO] buying {ticker} ({label})")
            else:
                confirm = input(f"  Confirm buy {ticker} ({label})? [y/N]: ").strip().lower()
                if confirm != "y":
                    print("  Skipped.")
                    continue

            trade = ic.place_market_order(ib, account_id, ticker, "BUY", qty)
            st.record_order(str(trade.order.orderId), ticker, "BUY", qty,
                            strategy=strategy)
            print(f"  Order placed (id={trade.order.orderId}). Waiting for fill...")
            fill = ic.confirm_fill(ib, trade)
            if fill is None:
                print(f"  WARNING: fill not confirmed for {ticker}.")
                st.mark_cancelled(str(trade.order.orderId))
                continue

            print(f"  Filled @ ${fill:.4f}")
            st.mark_filled(str(trade.order.orderId), fill, side="BUY")
            _email_ibkr_fill("BUY", ticker, qty, fill, strategy)
            actual_stop = round(fill * (1 - stop_pct), 2)
            stop_trade  = ic.place_stop_order(ib, account_id, ticker, qty, actual_stop)
            ib.sleep(1.0)
            st.update_stop(ticker, actual_stop,
                           str(stop_trade.order.orderId),
                           trailing_high=fill,
                           strategy=strategy)
            print(f"  Stop placed @ ${actual_stop:.2f} (id={stop_trade.order.orderId})")

    _place_book(sw_new, sw_budget, sw_max_pos, sw_stop_pct, sw_min_usd,
                "scorer_swing",     "swing_score")
    _place_book(po_new, po_budget, po_max_pos, po_stop_pct, po_min_usd,
                "scorer_portfolio", "trade_score")

    print("\n  [scorer] entry scan complete.")


def run_scorer_exits(
    ib,
    account_id: str,
    cfg: dict,
    dry_run: bool = True,
    scorer_results: dict | None = None,
) -> None:
    """Close scorer positions whose score dropped below the minimum threshold.

    On rescan, any held ticker that no longer appears in the top candidates
    (score fell below min_score) is sold. GTC stops placed at entry handle
    hard-floor protection independently on the broker side.
    """
    scorer_cfg   = cfg.get("strategies", {}).get("scorer", {})
    sw_min_score = float(scorer_cfg.get("swing",     {}).get("min_score", 65.0))
    po_min_score = float(scorer_cfg.get("portfolio", {}).get("min_score", 65.0))
    sw_max_pos   = int(scorer_cfg.get("swing",     {}).get("max_positions", 8))
    po_max_pos   = int(scorer_cfg.get("portfolio", {}).get("max_positions", 10))

    sw_open = st.get_open_positions("scorer_swing")
    po_open = st.get_open_positions("scorer_portfolio")

    if not sw_open and not po_open:
        print("  [scorer] No open scorer positions.")
        return

    if scorer_results is None:
        from ibkr_module.ibkr_scorer import run_scan
        scorer_results = run_scan(
            n_swing=sw_max_pos,
            n_portfolio=po_max_pos,
            min_score=min(sw_min_score, po_min_score),
        )

    # Build current score lookup from the full scored universe
    all_scored  = scorer_results.get("all_scored", pd.DataFrame())
    score_map: dict[str, dict] = {}
    if not all_scored.empty:
        for _, r in all_scored.iterrows():
            score_map[str(r["ticker"]).upper()] = {
                "swing_score":  float(r.get("swing_score",  0)),
                "trade_score":  float(r.get("trade_score",  0)),
                "setup":        str(r.get("setup", "PASS")),
                "hard_gate":    bool(r.get("hard_gate", False)),
            }

    def _check_book(
        open_pos: list[dict],
        min_score: float,
        score_col: str,
        strategy: str,
    ) -> None:
        label = strategy.replace("scorer_", "")
        if not open_pos:
            return

        all_syms    = [p["symbol"] for p in open_pos]
        live_prices = {s: ic.abs_price(p)
                       for s, p in ic.get_prices(ib, all_syms).items()}

        print(f"\n  [scorer/{label} exits]  {len(open_pos)} position(s)")

        for pos in open_pos:
            sym      = pos["symbol"].upper()
            entry_px = float(pos.get("fill_price") or 0)
            stop_oid = pos.get("stop_order_id")
            qty      = int(pos["qty"])

            cur_px   = live_prices.get(sym, 0.0)
            sc       = score_map.get(sym, {})
            cur_score = sc.get(score_col, 0.0)
            gate_ok   = sc.get("hard_gate", True)
            setup     = sc.get("setup", "?")

            gain_pct = ((cur_px / entry_px) - 1) * 100 if entry_px > 0 and cur_px > 0 else 0

            # Exit triggers:
            #   1. Score fell below threshold
            #   2. Hard gate now fails (delisted / too small / earnings window)
            should_exit = (cur_score < min_score) or (not gate_ok)
            reason      = ("score_below_min" if cur_score < min_score
                           else "gate_failed" if not gate_ok else "")

            print(f"  {sym:<8}  px=${cur_px:.2f}  entry=${entry_px:.2f}  "
                  f"{gain_pct:+.1f}%  {score_col}={cur_score:.1f}  grade={setup}  "
                  f"{'-> EXIT: ' + reason if should_exit else 'HOLD'}")

            if not should_exit:
                continue
            if dry_run:
                print(f"    [DRY RUN] would sell {qty} {sym}")
                continue

            if not cur_px or cur_px <= 0:
                print(f"    [BLOCKED] no live price for {sym} -- cannot execute exit")
                continue

            confirm = input(f"  Confirm EXIT {sym} ({label})? [y/N]: ").strip().lower()
            if confirm != "y":
                print("  Skipped.")
                continue

            # Cancel the GTC stop first
            if stop_oid:
                open_orders = ib.openTrades()
                old = next((t for t in open_orders
                            if str(t.order.orderId) == str(stop_oid)), None)
                if old:
                    ic.cancel_order(ib, old)
                    ib.sleep(0.5)

            sell_trade = ic.place_market_order(ib, account_id, sym, "SELL", qty)
            st.record_order(str(sell_trade.order.orderId), sym, "SELL", qty,
                            strategy=strategy)
            fill = ic.confirm_fill(ib, sell_trade)
            if fill is None:
                print(f"  WARNING: exit fill not confirmed for {sym} -- sell cancelled, will retry next cycle.")
                st.mark_cancelled(str(sell_trade.order.orderId))
            else:
                pnl = (fill - entry_px) * qty
                print(f"  Sold {qty} {sym} @ ${fill:.4f}  P&L: ${pnl:+,.2f}")
                st.mark_filled(str(sell_trade.order.orderId), fill, side="SELL")
                st.close_buy_position(sym, strategy)
                _email_ibkr_fill("SELL", sym, qty, fill, strategy, entry_px)

        print(f"  [scorer/{label}] exit check complete.")

    _check_book(sw_open, sw_min_score, "swing_score", "scorer_swing")
    _check_book(po_open, po_min_score, "trade_score", "scorer_portfolio")


# â"€â"€ Helpers â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

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

