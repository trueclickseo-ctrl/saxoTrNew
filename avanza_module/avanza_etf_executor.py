"""
avanza_etf_executor.py
----------------------
Rebalance logic for the Avanza ETF sleeve.

Workflow:
  1. Compute momentum scores from Yahoo Finance
  2. Fetch current Avanza ETF positions
  3. Compare → generate BUY / SELL / HOLD plan
  4. Execute interactively (one y/n confirmation per trade)
  5. On fill: record to avanza_etf_trades.db + place stop-loss

Budget: set manually in avanza_etf_config.json (B3 design).
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from avanza import Avanza

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from avanza_module import avanza_client  as ac
from avanza_module import avanza_etf_universe as universe
from avanza_module import avanza_etf_signal   as signal
from avanza_module import avanza_etf_state    as state


# ── Action plan ───────────────────────────────────────────────────────────────

def _compute_actions(ranked: list[dict], held_positions: list[dict],
                     budget_sek: float, max_positions: int,
                     etf_cache: dict, client: "Avanza",
                     sek_usd: float = 10.5) -> list[dict]:
    """Generate BUY / SELL / HOLD / SKIP action list."""
    top_n      = max_positions
    exit_rank  = max_positions + 2   # e.g., top_n=3 → exit if rank > 5
    held_map   = {p["ticker"].upper(): p for p in held_positions}
    top_tickers = {r["ticker"].upper() for r in ranked[:top_n]
                   if r["score"] is not None}

    actions = []

    # SELLs: held but ranked below exit_rank
    for r in ranked:
        t = r["ticker"].upper()
        if t in held_map and r["rank"] > exit_rank:
            pos   = held_map[t]
            ob_id = pos.get("order_book_id", etf_cache.get(r["ticker"], {}).get("id", ""))
            actions.append({
                "action": "SELL", "ticker": r["ticker"],
                "order_book_id": ob_id,
                "qty":    int(pos.get("qty", 1)),
                "price":  pos.get("current_price", 0),
                "value_sek": pos.get("value_sek", 0),
                "reason": f"rank {r['rank']} > exit threshold {exit_rank}",
                "rank":   r["rank"], "score": r["score"],
            })

    # Per-position budget
    n_buys = len([r for r in ranked[:top_n] if r["ticker"].upper() not in held_map
                  and r["score"] is not None])
    per_pos_sek = (budget_sek / max_positions) if budget_sek > 0 else 0

    # BUYs: in top_n, not held
    for r in ranked[:top_n]:
        t = r["ticker"].upper()
        if t in held_map:
            continue
        if r["score"] is None:
            actions.append({"action": "SKIP", "ticker": r["ticker"],
                            "reason": "no momentum data", "rank": r["rank"]})
            continue
        if per_pos_sek <= 0:
            actions.append({"action": "SKIP", "ticker": r["ticker"],
                            "reason": "budget_sek is 0 — update avanza_etf_config.json",
                            "rank": r["rank"]})
            continue

        ob_id = etf_cache.get(r["ticker"], {}).get("id")
        if not ob_id:
            actions.append({"action": "SKIP", "ticker": r["ticker"],
                            "reason": "not found on Avanza", "rank": r["rank"]})
            continue

        price_info = ac.get_stock_price(client, ob_id)
        price = price_info["price"]
        ccy   = price_info["currency"]

        if price <= 0:
            actions.append({"action": "SKIP", "ticker": r["ticker"],
                            "reason": "no live price", "rank": r["rank"]})
            continue

        price_sek  = price * sek_usd if ccy == "USD" else price
        qty        = max(1, int(per_pos_sek / price_sek))
        value_sek  = round(qty * price_sek, 0)
        limit      = round(price * 1.002, 4)

        actions.append({
            "action":        "BUY",
            "ticker":        r["ticker"],
            "order_book_id": ob_id,
            "qty":           qty,
            "price":         limit,
            "value_sek":     value_sek,
            "currency":      ccy,
            "reason":        f"rank {r['rank']}  score {r['score']:+.2f}",
            "rank":          r["rank"],
            "score":         r["score"],
        })

    # HOLDs: in top_n and already held
    for r in ranked:
        t = r["ticker"].upper()
        if t in held_map and r["rank"] <= exit_rank:
            pos = held_map[t]
            actions.append({
                "action":  "HOLD",
                "ticker":  r["ticker"],
                "qty":     pos.get("qty", 0),
                "price":   pos.get("current_price", 0),
                "value_sek": pos.get("value_sek", 0),
                "gain_pct":  pos.get("gain_pct", 0),
                "reason":  f"rank {r['rank']} — in range",
                "rank":    r["rank"],
            })

    return actions


def _print_plan(actions: list[dict]) -> None:
    sells = [a for a in actions if a["action"] == "SELL"]
    buys  = [a for a in actions if a["action"] == "BUY"]
    holds = [a for a in actions if a["action"] == "HOLD"]
    skips = [a for a in actions if a["action"] == "SKIP"]
    w = 72
    print("\n" + "=" * w)
    print("  AVANZA ETF REBALANCE PLAN")
    print("=" * w)
    if sells:
        print(f"\n  SELLS ({len(sells)}):")
        print(f"  {'Ticker':8s} {'Qty':>5} {'Price':>8} {'SEK':>10}  Reason")
        print("  " + "-" * (w - 2))
        for a in sells:
            print(f"  {a['ticker']:8s} {a['qty']:>5} {a['price']:>8.2f} "
                  f"{a['value_sek']:>10,.0f}  {a['reason']}")
    if buys:
        print(f"\n  BUYS ({len(buys)}):")
        print(f"  {'Ticker':8s} {'Qty':>5} {'Limit':>8} {'~SEK':>10}  Score / Reason")
        print("  " + "-" * (w - 2))
        for a in buys:
            print(f"  {a['ticker']:8s} {a['qty']:>5} {a['price']:>8.4f} "
                  f"{a['value_sek']:>10,.0f}  {a['reason']}")
    if holds:
        print(f"\n  HOLDS ({len(holds)}) — no action:")
        for a in holds:
            g = a.get("gain_pct", 0)
            s = "+" if g >= 0 else ""
            print(f"  {a['ticker']:8s}  rank {a['rank']}  {s}{g:.1f}%  {a['reason']}")
    if skips:
        print(f"\n  SKIPPED:")
        for a in skips:
            print(f"  {a['ticker']:8s} — {a['reason']}")
    print("\n" + "=" * w)


# ── Main rebalance ────────────────────────────────────────────────────────────

def run_rebalance(client: "Avanza", account_id: str,
                  config: dict, dry_run: bool = True,
                  only_ticker: str | None = None) -> dict:

    budget_sek    = float(config.get("budget_sek", 0))
    max_positions = int(config.get("max_positions", 3))
    min_trade_sek = float(config.get("min_trade_sek", 300))
    stop_pct      = float(config.get("stop_pct", 0.12))
    slippage_pct  = float(config.get("stop_sell_slippage_pct", 0.01))
    w_3m = float(config.get("momentum_weights", {}).get("3m", 0.5))
    w_6m = float(config.get("momentum_weights", {}).get("6m", 0.5))
    try:
        sek_usd = float(os.environ.get("AVANZA_SEK_USD_RATE", "10.5"))
    except ValueError:
        sek_usd = 10.5

    if budget_sek <= 0:
        print("\n  *** budget_sek is 0 in avanza_etf_config.json ***")
        print("  Set it to your allocated ETF budget before running --execute.")

    print(f"\n  ETF Budget: {budget_sek:,.0f} SEK  |  Max positions: {max_positions}")

    # Step 1: resolve universe against Avanza
    etf_cache = universe.load_cache()
    universe_list = universe.UNIVERSE
    if only_ticker:
        universe_list = [e for e in universe.UNIVERSE
                         if e["ticker"].upper() == only_ticker.upper()]
        if not universe_list:
            print(f"  --only {only_ticker}: not in universe.")
            return {}

    # Ensure all tickers are resolved
    for etf in universe_list:
        universe.lookup_etf(client, etf["ticker"], etf_cache)
    universe.save_cache(etf_cache)

    # Step 2: compute momentum scores
    print("  Computing momentum scores from Yahoo Finance...")
    ranked = signal.compute_scores(universe_list, w_3m=w_3m, w_6m=w_6m)

    held_tickers = state.get_held_tickers()
    signal.print_scores(ranked, top_n=max_positions,
                        exit_rank=max_positions + 2,
                        held_tickers=held_tickers)

    # Step 3: current Avanza positions (filter to ETF tickers only)
    all_positions = ac.get_positions(client, account_id)
    etf_tickers   = {e["ticker"].upper() for e in universe.UNIVERSE}
    etf_positions = [p for p in all_positions
                     if p.get("ticker", "").upper() in etf_tickers]
    print(f"  Current ETF positions on Avanza: {len(etf_positions)}")

    # Step 4: compute action plan
    actions = _compute_actions(ranked, etf_positions, budget_sek, max_positions,
                               etf_cache, client, sek_usd)
    _print_plan(actions)

    if dry_run:
        print("  [DRY RUN] No orders placed. Pass --execute to place real orders.")
        state.write_status({
            "timestamp":      datetime.now().isoformat(),
            "dry_run":        True,
            "budget_sek":     budget_sek,
            "top_ranked":     [r["ticker"] for r in ranked[:max_positions]],
            "momentum_scores": {r["ticker"]: r["score"] for r in ranked},
        })
        return {"buys": 0, "sells": 0, "skips": 0, "dry_run": True}

    # Step 5: execute
    executed_buys = executed_sells = skips = 0

    for action in actions:
        if action["action"] not in ("BUY", "SELL"):
            continue
        if action["action"] == "BUY" and action.get("value_sek", 0) < min_trade_sek:
            print(f"\n  SKIP {action['ticker']}: {action['value_sek']:,.0f} SEK < min {min_trade_sek:,.0f}")
            skips += 1
            continue

        side   = action["action"]
        ticker = action["ticker"]
        qty    = action["qty"]
        price  = action["price"]
        ob_id  = action["order_book_id"]

        print(f"\n  -- {side} {qty}x {ticker} @ {price:.4f}  (~{action.get('value_sek',0):,.0f} SEK) --")
        ans = input("  Place this order? [y/n/q=quit]: ").strip().lower()
        if ans == "q":
            break
        if ans != "y":
            skips += 1
            continue

        try:
            if side == "BUY":
                resp = ac.place_buy(client, account_id, ob_id, qty, price)
            else:
                resp = ac.place_sell(client, account_id, ob_id, qty, price)

            order_id = resp.get("orderId") if isinstance(resp, dict) else None
            status   = resp.get("orderRequestStatus", "UNKNOWN") if isinstance(resp, dict) else "UNKNOWN"

            if status == "SUCCESS" or order_id:
                print(f"  Order placed — orderId={order_id}")
                if side == "BUY":
                    r = next((x for x in ranked if x["ticker"] == ticker), {})
                    trade_id = state.record_order(
                        ticker, ob_id, qty, price, order_id,
                        value_sek=action.get("value_sek", 0),
                        momentum_score=r.get("score"),
                        rank_at_entry=r.get("rank"),
                    )
                    fill_price = ac.confirm_fill(client, account_id, order_id, ob_id,
                                                 timeout_s=120, poll_s=10)
                    if fill_price is None:
                        state.mark_cancelled(order_id)
                        print(f"  {ticker}: limit not filled in 2 min — cancelled.")
                        skips += 1
                        continue
                    actual = fill_price if fill_price > 0 else price
                    state.mark_filled(order_id, fill_price=actual)
                    print(f"  {ticker} filled @ {actual:.4f}")
                    executed_buys += 1

                    # Stop-loss
                    initial_stop = round(actual * (1 - stop_pct), 4)
                    try:
                        sl_resp  = ac.place_stop_loss(client, account_id, ob_id, qty,
                                                      initial_stop, slippage_pct)
                        sl_id    = sl_resp.get("stoplossOrderId") if isinstance(sl_resp, dict) else None
                        sl_status = sl_resp.get("status", "") if isinstance(sl_resp, dict) else ""
                        if sl_id or sl_status == "SUCCESS":
                            state.update_stop(trade_id, sl_id, initial_stop, actual)
                            print(f"    Stop-loss @ {initial_stop:.4f} "
                                  f"({stop_pct*100:.0f}% below {actual:.4f}) — id={sl_id}")
                        else:
                            print(f"    Stop-loss FAILED: {sl_resp}")
                            print(f"    Place manually @ {initial_stop:.4f}")
                    except Exception as sl_exc:
                        print(f"    Stop-loss error: {sl_exc}")
                        print(f"    Place manually @ {initial_stop:.4f}")
                else:
                    state.record_close(ticker, ob_id, qty, price, order_id)
                    fill_price = ac.confirm_fill(client, account_id, order_id, ob_id,
                                                 timeout_s=120, poll_s=10)
                    if fill_price is None:
                        state.mark_cancelled(order_id)
                        skips += 1
                        continue
                    actual = fill_price if fill_price > 0 else price
                    state.mark_filled(order_id, fill_price=actual)
                    print(f"  {ticker} sold @ {actual:.4f}")
                    executed_sells += 1
            else:
                msg = resp.get("message", "") if isinstance(resp, dict) else str(resp)
                print(f"  Order REJECTED: {msg}")
                skips += 1

        except Exception as exc:
            print(f"  Order error: {exc}")
            skips += 1

    print(f"\n  Done — {executed_buys} buy(s), {executed_sells} sell(s), {skips} skipped.")
    state.write_status({
        "timestamp":      datetime.now().isoformat(),
        "dry_run":        False,
        "budget_sek":     budget_sek,
        "executed_buys":  executed_buys,
        "executed_sells": executed_sells,
        "top_ranked":     [r["ticker"] for r in ranked[:max_positions]],
    })
    return {"buys": executed_buys, "sells": executed_sells, "skips": skips}
