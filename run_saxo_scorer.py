"""
run_saxo_scorer.py
------------------
Saxo SIM runner for the ATOS Scorer Portfolio strategy.

Runs in parallel with the IBKR paper version (run_ibkr_stocks.py --strategy
scorer). Same scoring engine (ibkr_module.ibkr_scorer.run_scan), different
execution layer (Saxo SIM via saxo_order / saxo_client).

State: atos/database.py, strategy='Scorer Portfolio'
Budget: 60,000 SEK / 15 slots (~SEK 4,000/slot)
Stop: 6% trailing (same as ibkr_config scorer.portfolio)

Usage:
    python run_saxo_scorer.py                    # entry scan dry-run
    python run_saxo_scorer.py --execute          # place orders (confirm each)
    python run_saxo_scorer.py --exits            # exit check dry-run
    python run_saxo_scorer.py --exits --execute  # execute exits
    python run_saxo_scorer.py --trail            # trail stop dry-run
    python run_saxo_scorer.py --trail --execute  # update stops
    python run_saxo_scorer.py --execute --auto   # paper-account auto mode

Claude never runs --execute or places Saxo orders.
"""
from __future__ import annotations

import argparse
import sys
import os
from datetime import date

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)

import saxo_client
import saxo_order
import saxo_fx
import atos.database as db
from atos.us_scorer import (
    SCORER_STOP_PCT, SCORER_MAX_POS, SCORER_MIN_SCORE, SCORER_BUDGET_SEK,
    STRATEGY_NAME,
    select_entries, select_exits, trail_updates,
)
from instrument_map import load_instrument_map
from ibkr_module.ibkr_scorer import run_scan

_SIM = "sim"


# ── Helpers ─────────────────────────────────────────────────────────────────

def _rate_usd_sek() -> float:
    """Live USD/SEK rate from Saxo. Falls back to 10.5 on failure."""
    try:
        r = saxo_fx.rate_to_sek(["USD"]).get("USD")
        return float(r) if r and r > 0 else 10.5
    except Exception:
        return 10.5


def _commission_sek(shares: int, price_usd: float, fx: float) -> float:
    """SIM commission estimate — 0.1% of value, minimum SEK 10."""
    return max(10.0, shares * price_usd * fx * 0.001)


def _sim_cap_shares(shares: int, price_usd: float, fx: float) -> int:
    """Clamp to config/capital.json sim_max_trade_notional_eur."""
    try:
        import atos.capital_config as CAP
        cap_eur = CAP.sim_max_trade_notional_eur()
        if not cap_eur or cap_eur <= 0:
            return shares
        eur_rate = saxo_fx.rate_to_sek(["EUR"]).get("EUR") or 11.5
        cap_sek = cap_eur * eur_rate
        if shares * price_usd * fx <= cap_sek:
            return shares
        capped = max(1, int(cap_sek / (price_usd * fx)))
        if capped != shares:
            print(f"  [scorer] SIM notional cap: {shares} -> {capped} sh")
        return capped
    except Exception:
        return shares


def _prices_from_scan(scorer_results: dict) -> dict[str, float]:
    """Extract {TICKER: price_usd} from scorer feature data."""
    feat = scorer_results.get("features")
    if feat is None or feat.empty:
        return {}
    prices: dict[str, float] = {}
    for _, r in feat.iterrows():
        t = str(r.get("ticker", "")).upper()
        p = float(r.get("price", 0))
        if t and p > 0:
            prices[t] = p
    return prices


# ── Exit runner ─────────────────────────────────────────────────────────────

def run_scorer_exits(scorer_results: dict, dry_run: bool, auto: bool) -> int:
    open_trades = db.get_open_trades()
    scorer_open = [t for t in open_trades if t.get("strategy") == STRATEGY_NAME]
    exits       = select_exits(open_trades, scorer_results)

    print(f"\n  [scorer/portfolio exits]  {len(scorer_open)} position(s)")
    if not scorer_open:
        return 0

    all_scored = scorer_results.get("all_scored")
    score_map: dict[str, float] = {}
    if all_scored is not None and not all_scored.empty:
        for _, r in all_scored.iterrows():
            score_map[str(r["ticker"]).upper()] = float(r.get("trade_score", 0))

    prices_usd = _prices_from_scan(scorer_results)
    fx         = _rate_usd_sek()
    exit_ids   = {e["id"] for e in exits}
    exit_reason = {e["id"]: e["exit_reason"] for e in exits}
    closed     = 0

    for t in scorer_open:
        sym      = t["ticker"].upper()
        entry_px = float(t.get("entry_price") or 0)
        qty      = int(t.get("shares") or 0)
        cur_px   = prices_usd.get(sym, 0.0)
        cur_sc   = score_map.get(sym, 0.0)
        flag     = t["id"] in exit_ids
        reason   = exit_reason.get(t["id"], "")
        gain_pct = ((cur_px / entry_px) - 1) * 100 if entry_px > 0 and cur_px > 0 else 0

        print(f"  {sym:<8}  px=${cur_px:.2f}  entry=${entry_px:.2f}  "
              f"{gain_pct:+.1f}%  trade_score={cur_sc:.1f}  "
              f"{'-> EXIT: ' + reason if flag else 'HOLD'}")

        if not flag:
            continue
        if dry_run:
            print(f"    [DRY RUN] would sell {qty} {sym}")
            continue
        if cur_px <= 0:
            print(f"    [BLOCKED] no price for {sym} — skipped")
            continue

        confirm = "y" if auto else input(f"  Confirm EXIT {sym}? [y/N]: ").strip().lower()
        if confirm != "y":
            print("  Skipped.")
            continue

        is_paper = bool(t.get("paper"))
        if not is_paper:
            stop_oid = t.get("stop_order_id")
            if stop_oid:
                try:
                    saxo_client.cancel_order(str(stop_oid), env=_SIM)
                except Exception:
                    pass
            try:
                uic = t.get("uic") or (load_instrument_map().get(sym) or {}).get("uic")
                if uic:
                    saxo_client.place_market_order(int(uic), "Stock", "Sell", qty, env=_SIM)
            except Exception as e:
                print(f"  [WARN] Saxo sell failed for {sym}: {e} — closing locally")

        pnl_sek = (cur_px - entry_px) * qty * fx
        comm    = _commission_sek(qty, cur_px, fx)
        db.close_trade(t["id"], cur_px, reason, pnl_sek - comm, comm)
        print(f"  Sold {qty} {sym} @ ${cur_px:.2f}  P&L: SEK {pnl_sek - comm:+,.0f}")
        closed += 1

    print(f"\n  [scorer/portfolio] exit check complete. {closed} closed.")
    return closed


# ── Entry runner ─────────────────────────────────────────────────────────────

def run_scorer_entries(scorer_results: dict, dry_run: bool, auto: bool) -> int:
    try:
        imap = load_instrument_map()
    except Exception as e:
        print(f"  [scorer] instrument_map load failed: {e}")
        return 0

    open_trades  = db.get_open_trades()
    open_tickers = {t["ticker"].upper() for t in open_trades
                    if t.get("strategy") == STRATEGY_NAME}
    free         = SCORER_MAX_POS - len(open_tickers)

    print(f"\n  [scorer/portfolio]  {len(open_tickers)}/{SCORER_MAX_POS} slots used  "
          f"({free} free)")

    if free <= 0:
        print("  [scorer] No free slots.")
        return 0

    portfolio_df = scorer_results.get("portfolio")
    if portfolio_df is None or portfolio_df.empty:
        print("  [scorer] No portfolio candidates from scorer.")
        return 0

    fx         = _rate_usd_sek()
    candidates = select_entries(portfolio_df, open_tickers, imap, fx_usd_sek=fx)

    if not candidates:
        print("  [scorer] No new candidates above min_score / sufficient size.")
        return 0

    ak     = saxo_client.get_account_key(env=_SIM)
    bought = 0

    for c in candidates:
        ticker = c["ticker"]
        uic    = c["uic"]
        qty    = _sim_cap_shares(c["qty"], c["price_usd"], fx)
        price  = c["price_usd"]
        stop_p = c["stop_price_usd"]
        score  = c["score"]

        if qty < 1:
            print(f"  {ticker}: qty < 1 after cap — skip")
            continue

        notional_sek = qty * price * fx
        print(f"\n  [scorer]  BUY {ticker:<6}  score={score:.1f}  "
              f"qty={qty}  price~${price:.2f}  stop=${stop_p:.2f}  "
              f"notional~SEK{notional_sek:,.0f}")

        if dry_run:
            print("    [DRY RUN] would place market buy + GTC stop")
            continue

        confirm = "y" if auto else input(f"  Confirm buy {ticker}? [y/N]: ").strip().lower()
        if confirm != "y":
            print("  Skipped.")
            continue

        paper      = 0
        entry_oid  = stop_oid = None
        fill_price = price

        try:
            entry_oid, stop_oid, _ = saxo_order.place_with_stop(
                post_fn=lambda path, body, _e=_SIM: saxo_client.post(path, body, env=_e),
                account_key=ak,
                uic=uic,
                asset_type="Stock",
                amount=qty,
                buy_sell="Buy",
                stop_price=stop_p,
                label=f"scorer:{ticker}",
                symbol=ticker,
            )
            if entry_oid is None:
                print(f"  [scorer] {ticker}: entry rejected by Saxo — paper fill")
                paper = 1
        except Exception as e:
            print(f"  [scorer] {ticker}: order failed ({e}) — paper fill")
            paper = 1

        actual_stop = round(fill_price * (1 - SCORER_STOP_PCT), 2) if paper else stop_p
        comm        = _commission_sek(qty, fill_price, fx)

        db.insert_trade({
            "strategy":           STRATEGY_NAME,
            "market_group":       "US Equities",
            "ticker":             ticker,
            "direction":          "BUY",
            "entry_date":         date.today().isoformat(),
            "entry_price":        fill_price,
            "shares":             qty,
            "commission_sek":     comm,
            "entry_score":        score,
            "d1_trend":           0, "d2_momentum": 0, "d3_breakout":    0,
            "d4_mean_revert":     0, "d5_volume":    0, "d6_smart_money": 0,
            "d7_mom_quality":     0, "d8_regime":    0,
            "trailing_stop_high": fill_price,
            "regime_at_entry":    "scorer",
            "stop_price":         actual_stop,
            "paper":              paper,
            "stop_order_id":      stop_oid if not paper else None,
            "stop_policy":        "fixed_6pct",
        })
        print(f"  {'[PAPER] ' if paper else ''}Bought {qty} {ticker} @ "
              f"${fill_price:.2f}  stop=${actual_stop:.2f}  "
              f"(stop_id={stop_oid})")
        bought += 1

    print(f"\n  [scorer/portfolio] entry scan complete. {bought} bought.")
    return bought


# ── Trail stop runner ────────────────────────────────────────────────────────

def run_trail_stops(scorer_results: dict, dry_run: bool) -> None:
    open_trades = db.get_open_trades()
    scorer_open = [t for t in open_trades if t.get("strategy") == STRATEGY_NAME]

    print(f"\n  [scorer/trail]  {len(scorer_open)} position(s)")
    if not scorer_open:
        return

    prices_usd = _prices_from_scan(scorer_results)
    updates    = trail_updates(scorer_open, prices_usd)

    if not updates:
        print("  [scorer/trail] No stop updates needed.")
        return

    try:
        imap = load_instrument_map()
    except Exception:
        imap = {}

    ak  = saxo_client.get_account_key(env=_SIM)
    fx  = _rate_usd_sek()
    print(f"  stop_pct={SCORER_STOP_PCT*100:.0f}%  fx=SEK{fx:.2f}/$\n")

    for u in updates:
        sym     = u["ticker"]
        uic_val = u["uic"] or (imap.get(sym) or {}).get("uic")
        print(f"  {sym:<8}  stop ${u['cur_stop']:.2f} -> ${u['new_stop']:.2f}  "
              f"trail_high ${u['old_trail_high']:.2f} -> ${u['new_trail_high']:.2f}")

        if dry_run:
            print(f"    [DRY RUN] would modify stop to ${u['new_stop']:.2f}")
            db.update_stop_trailing(u["trade_id"], u["new_trail_high"], u["new_stop"])
            continue

        if not u["paper"] and u["stop_order_id"] and uic_val:
            try:
                saxo_order.modify_stop_price(
                    patch_fn=lambda oid, body, _e=_SIM: saxo_client.patch_order(oid, body, env=_e),
                    account_key=ak,
                    order_id=str(u["stop_order_id"]),
                    uic=int(uic_val),
                    asset_type="Stock",
                    amount=u["qty"],
                    new_stop_price=u["new_stop"],
                    symbol=sym,
                )
            except Exception as e:
                print(f"    [WARN] Saxo modify failed for {sym}: {e} — DB updated only")

        db.update_stop_trailing(u["trade_id"], u["new_trail_high"], u["new_stop"])

    print(f"\n  [scorer/trail] {len(updates)} stop(s) ratcheted.")


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Saxo SIM Scorer Portfolio — entry/exit/trail runner"
    )
    parser.add_argument("--exits",   action="store_true",
                        help="Run exit check (score-drop + gate-fail)")
    parser.add_argument("--entries", action="store_true",
                        help="Run entry scan (default when neither --exits nor --trail)")
    parser.add_argument("--trail",   action="store_true",
                        help="Update trailing stops")
    parser.add_argument("--execute", action="store_true",
                        help="Place orders (default: dry-run)")
    parser.add_argument("--auto",    action="store_true",
                        help="Skip y/N confirmations (SIM only)")
    args = parser.parse_args()

    dry_run    = not args.execute
    do_exits   = args.exits
    do_entries = args.entries or (not args.exits and not args.trail)
    do_trail   = args.trail

    if dry_run:
        print("  [DRY RUN] pass --execute to place orders.\n")

    print("  Pre-generating scorer results (Yahoo Finance)...")
    scorer_results = run_scan(
        n_portfolio=SCORER_MAX_POS,
        n_swing=8,
        min_score=SCORER_MIN_SCORE,
        verbose=True,
    )

    if do_exits:
        run_scorer_exits(scorer_results, dry_run=dry_run, auto=args.auto)

    if do_entries:
        run_scorer_entries(scorer_results, dry_run=dry_run, auto=args.auto)

    if do_trail:
        run_trail_stops(scorer_results, dry_run=dry_run)

    print("\n  Done.")


if __name__ == "__main__":
    main()
