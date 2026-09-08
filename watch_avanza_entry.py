"""
watch_avanza_entry.py
---------------------
Live price monitor for any Avanza stock before placing an entry order.

Polls bid/ask every 2 seconds for a configurable window (default 30 s),
prints a live table, then prints the recommended entry: qty, limit price,
stop level.  No orders are placed — observation only.

Records every session to data/execution_quality.db (same schema as
execution_monitor.py) so fill-quality can be compared across entries.

Usage:
    python watch_avanza_entry.py --ticker HUM --budget-sek 2200
    python watch_avanza_entry.py --ticker DELL --budget-sek 5000 --window 60
    python watch_avanza_entry.py --ticker NTAP --budget-sek 2200 --sek-usd 10.5

When used before every Avanza buy this gives:
  - A clear "go / no-go" based on whether the price is near the signal price
  - A precise share count and limit price ready to type into Avanza
  - A stop-loss level to place immediately after fill

Entry timing guidance (US market hours, PKT = ET + 9h during EDT):
  - Market opens 18:30 PKT (9:30 AM ET)
  - AVOID first 15-30 min: wide spreads, price discovery (18:30-19:00 PKT)
  - BEST window 1: 19:00-20:00 PKT (10:00-11:00 AM ET) -- settled, liquid
  - AVOID lunch:  20:30-22:00 PKT (11:30 AM-1:00 PM ET) -- thin volume
  - BEST window 2: 23:00-00:00 PKT (2:00-3:00 PM ET)  -- afternoon session
  - AVOID last 15 min: 00:45-01:00 PKT (3:45-4:00 PM ET) -- MOC noise
  Our scans fire at 19:20 PKT (window 1) and 23:30 PKT (window 2) by design.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)


# ── Avanza setup ──────────────────────────────────────────────────────────────

def _load_env() -> None:
    env_file = os.path.join(_ROOT, ".env.avanza")
    if not os.path.exists(env_file):
        return
    with open(env_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _get_quote(client, order_book_id: str) -> dict | None:
    """Return {bid, ask, mid, spread_pct, last} from Avanza stock_info.

    Avanza quote fields:
      quote.sell  = best bid (what the market pays you if you sell)
      quote.buy   = best ask (what you pay to buy)
      quote.last  = last traded price
    """
    try:
        info  = client.get_stock_info(order_book_id)
        quote = info.get("quote") or {}
        # Avanza API: quote.buy = BID (best buy order), quote.sell = ASK (best sell order)
        bid   = float(quote.get("buy")  or quote.get("last") or 0.0)
        ask   = float(quote.get("sell") or quote.get("last") or 0.0)
        last  = float(quote.get("last") or 0.0)
        if ask <= 0 and last > 0:
            ask = last
        if bid <= 0 and last > 0:
            bid = last
        mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else last
        spread_pct = ((ask - bid) / mid * 100) if mid > 0 else 0.0
        return {"bid": bid, "ask": ask, "mid": mid,
                "spread_pct": spread_pct, "last": last}
    except Exception as exc:
        print(f"  [quote] {exc}", file=sys.stderr)
        return None


# ── Signal price lookup ───────────────────────────────────────────────────────

def _signal_price_for(ticker: str) -> float | None:
    """Try to find the signal (scan) price for ticker from stocks_live_status.json."""
    sig_file = os.path.join(_ROOT, "data", "stocks_live_status.json")
    if not os.path.exists(sig_file):
        return None
    try:
        with open(sig_file, encoding="utf-8") as f:
            data = json.load(f)
        targets = data.get("signal", {}).get("targets", [])
        for t in targets:
            if isinstance(t, dict) and t.get("ticker", "").upper() == ticker.upper():
                return float(t.get("price") or t.get("close") or 0) or None
        # targets may be plain strings; no price in that case
    except Exception:
        pass
    return None


# ── DB record ────────────────────────────────────────────────────────────────

_DB_PATH = os.path.join(_ROOT, "data", "execution_quality.db")


def _db_write(row: dict) -> None:
    try:
        con = sqlite3.connect(_DB_PATH, timeout=10)
        con.execute("""
            CREATE TABLE IF NOT EXISTS executions (
                exec_id              TEXT PRIMARY KEY,
                ts                   TEXT NOT NULL,
                module               TEXT NOT NULL,
                strategy             TEXT NOT NULL,
                symbol               TEXT NOT NULL,
                direction            TEXT NOT NULL,
                env                  TEXT NOT NULL,
                signal_price         REAL,
                signal_ts            TEXT,
                window_seconds       REAL,
                max_adverse_pct      REAL,
                elapsed_seconds      REAL,
                timed_out            INTEGER,
                entry_condition      TEXT,
                n_quotes             INTEGER,
                quotes_json          TEXT,
                bid_at_entry         REAL,
                ask_at_entry         REAL,
                mid_at_entry         REAL,
                spread_pct_at_entry  REAL,
                order_price          REAL,
                fill_price           REAL,
                slippage_vs_signal   REAL,
                slippage_vs_order    REAL,
                time_to_fill_s       REAL
            )
        """)
        cols   = ", ".join(row.keys())
        places = ", ".join("?" * len(row))
        con.execute(f"INSERT OR IGNORE INTO executions ({cols}) VALUES ({places})",
                    list(row.values()))
        con.commit()
        con.close()
    except Exception as exc:
        print(f"  [db] write failed: {exc}", file=sys.stderr)


# ── Display helpers ───────────────────────────────────────────────────────────

def _bar(price: float, signal_price: float | None, width: int = 20) -> str:
    """A simple ASCII bar showing price vs signal baseline."""
    if not signal_price or signal_price <= 0:
        return ""
    pct = (price - signal_price) / signal_price * 100
    marker = "+" if pct >= 0 else "-"
    n = min(width, int(abs(pct) / 0.05))
    return f"{'':>{width - n}}{marker * n}  {pct:+.2f}%"


def _timing_banner() -> None:
    print()
    print("  Entry timing guide (PKT / US EDT market hours):")
    print("  +-------------------------------------------------------------+")
    print("  |  18:30-19:00 PKT  AVOID  - open volatility, wide spreads   |")
    print("  |  19:00-20:00 PKT  BEST   - settled, liquid  <- scan 19:20  |")
    print("  |  20:30-22:00 PKT  AVOID  - lunch thin, spreads widen       |")
    print("  |  23:00-00:00 PKT  GOOD   - afternoon session <- scan 23:30 |")
    print("  |  00:45-01:00 PKT  AVOID  - MOC noise, last 15 min          |")
    print("  +-------------------------------------------------------------+")
    print()


# ── Main watch loop ───────────────────────────────────────────────────────────

def watch(ticker: str, budget_sek: float, window_s: float,
          sek_usd: float, stop_pct: float) -> None:

    _load_env()

    from avanza_module import avanza_client as ac
    from avanza_module import avanza_instrument_cache as ic

    print(f"\n  Connecting to Avanza...", end=" ", flush=True)
    try:
        client = ac.get_client()
        print("OK")
    except Exception as exc:
        print(f"\n  ERROR: {exc}")
        sys.exit(1)

    # Resolve ticker -> orderBookId
    cache  = ic.load_cache()
    ob_id  = ic.lookup(client, ticker, cache)
    ic.save_cache(cache)
    if not ob_id:
        print(f"  ERROR: {ticker} not found on Avanza. Try: python run_avanza.py --resolve-tickers {ticker}")
        sys.exit(1)

    entry = cache.get(ticker.upper(), cache.get(ticker, {}))
    name  = entry.get("name", ticker)
    ccy   = entry.get("currency", "USD")
    print(f"  Resolved: {ticker} -> orderBookId={ob_id}  name={name}  currency={ccy}")

    signal_price = _signal_price_for(ticker)
    if signal_price:
        print(f"  Signal price (last scan close): {signal_price:.2f} {ccy}")
    else:
        print(f"  Signal price: not found in signal file (will use first live quote as baseline)")

    _timing_banner()

    print(f"  Watching {ticker} for {int(window_s)}s  "
          f"(budget {budget_sek:,.0f} SEK  |  1 USD ~ {sek_usd:.2f} SEK  |  "
          f"stop {stop_pct*100:.0f}% below fill)")
    print()
    print(f"  {'#':>3}  {'Time':>8}  {'Bid':>8}  {'Ask':>8}  {'Mid':>8}  "
          f"{'Sprd%':>6}  {'Qty':>5}  {'~SEK':>8}  vs Signal")
    print("  " + "-" * 80)

    exec_id    = uuid.uuid4().hex
    start_ts   = datetime.now(timezone.utc)
    start_s    = time.monotonic()
    quotes:    list[dict] = []
    baseline   = signal_price  # may be updated on first quote if signal_price is None

    try:
        n = 0
        while True:
            elapsed = time.monotonic() - start_s

            q = _get_quote(client, ob_id)
            now_str = datetime.now().strftime("%H:%M:%S")

            if q and q.get("ask", 0) > 0:
                bid, ask, mid = q["bid"], q["ask"], q["mid"]
                sp            = q["spread_pct"]

                if baseline is None:
                    baseline = mid  # anchor to first live quote

                # Sizing: budget_sek / (price_in_sek)
                if ccy == "USD":
                    price_sek = ask * sek_usd
                else:
                    price_sek = ask
                qty = max(1, int(budget_sek / price_sek)) if price_sek > 0 else 0
                value_sek = round(qty * price_sek, 0)

                bar = _bar(mid, baseline)
                print(f"  {n+1:>3}  {now_str}  "
                      f"{bid:>8.2f}  {ask:>8.2f}  {mid:>8.2f}  "
                      f"{sp:>6.3f}  {qty:>5}  {value_sek:>8,.0f}  {bar}")

                quotes.append({
                    "t": datetime.now(timezone.utc).isoformat(),
                    "bid": bid, "ask": ask, "mid": mid,
                    "sp": round(sp, 4), "el": round(elapsed, 2),
                })
                n += 1
            else:
                print(f"  {n+1:>3}  {now_str}  -- no quote --")

            remaining = window_s - elapsed
            if remaining <= 0:
                break
            time.sleep(min(2.0, remaining))

    except KeyboardInterrupt:
        print("\n  [interrupted]")

    elapsed_total = time.monotonic() - start_s

    # ── Final recommendation ──────────────────────────────────────────────────
    if not quotes:
        print("\n  No quotes received — cannot recommend entry.")
        return

    last_q      = quotes[-1]
    ask_final   = last_q["ask"]
    bid_final   = last_q["bid"]
    mid_final   = last_q["mid"]
    sp_final    = last_q["sp"]

    limit_price  = round(ask_final * 1.002, 2)   # 0.2% above ask for fill probability
    if ccy == "USD":
        price_sek = limit_price * sek_usd
    else:
        price_sek = limit_price
    qty_rec   = max(1, int(budget_sek / price_sek)) if price_sek > 0 else 0
    cost_sek  = round(qty_rec * price_sek, 0)
    stop_lvl  = round(limit_price * (1 - stop_pct), 2)

    # Drift from signal
    if baseline and baseline > 0:
        drift_pct = (mid_final - baseline) / baseline * 100
        drift_str = f"  Drift from signal: {drift_pct:+.2f}%"
    else:
        drift_str = ""

    print()
    print("  " + "=" * 68)
    print(f"  ENTRY RECOMMENDATION  —  {ticker}  ({name})")
    print("  " + "=" * 68)
    print(f"  Last bid / ask : {bid_final:.2f} / {ask_final:.2f} {ccy}  "
          f"(spread {sp_final:.3f}%)")
    print(f"  Limit price    : {limit_price:.2f} {ccy}  (+0.2% above ask)")
    print(f"  Shares to buy  : {qty_rec}  (~{cost_sek:,.0f} SEK  "
          f"with {budget_sek - cost_sek:,.0f} SEK leftover)")
    print(f"  Stop-loss      : {stop_lvl:.2f} {ccy}  ({stop_pct*100:.0f}% below limit)")
    if drift_str:
        print(drift_str)
    print()
    print("  Steps:")
    print(f"    1. On Avanza: BUY {qty_rec} x {ticker} @ {limit_price:.2f} (limit order)")
    print(f"    2. After fill: place Stop-Loss @ {stop_lvl:.2f} (8% GTC)")
    print(f"    3. Trail stops daily via: python run_avanza.py --trail-stops --execute")
    print("  " + "=" * 68)
    print()

    # DB record
    _db_write({
        "exec_id":             exec_id,
        "ts":                  start_ts.isoformat(),
        "module":              "avanza",
        "strategy":            "US Blend",
        "symbol":              ticker.upper(),
        "direction":           "Buy",
        "env":                 "live",
        "signal_price":        baseline,
        "signal_ts":           "",
        "window_seconds":      window_s,
        "max_adverse_pct":     0.002,
        "elapsed_seconds":     round(elapsed_total, 2),
        "timed_out":           0,
        "entry_condition":     f"watch_complete @ {elapsed_total:.1f}s",
        "n_quotes":            len(quotes),
        "quotes_json":         json.dumps(quotes),
        "bid_at_entry":        bid_final,
        "ask_at_entry":        ask_final,
        "mid_at_entry":        mid_final,
        "spread_pct_at_entry": sp_final,
        "order_price":         limit_price,
        "fill_price":          None,
        "slippage_vs_signal":  None,
        "slippage_vs_order":   None,
        "time_to_fill_s":      None,
    })
    print(f"  Observation recorded -> execution_quality.db  (exec_id={exec_id[:8]}...)")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(
        description="Watch an Avanza stock's live price before placing an entry order."
    )
    p.add_argument("--ticker",      required=True, metavar="TICKER",
                   help="Stock ticker, e.g. HUM, DELL, NTAP")
    p.add_argument("--budget-sek",  type=float, default=2200.0,
                   help="SEK to spend on this position (default 2200)")
    p.add_argument("--window",      type=float, default=30.0,
                   help="Observation window in seconds (default 30)")
    p.add_argument("--sek-usd",     type=float,
                   default=float(os.environ.get("AVANZA_SEK_USD_RATE", "10.5")),
                   help="SEK per USD rate for sizing (default 10.5 or env AVANZA_SEK_USD_RATE)")
    p.add_argument("--stop-pct",    type=float, default=0.08,
                   help="Stop-loss distance below fill (default 0.08 = 8%%)")
    args = p.parse_args()

    watch(
        ticker     = args.ticker,
        budget_sek = args.budget_sek,
        window_s   = args.window,
        sek_usd    = args.sek_usd,
        stop_pct   = args.stop_pct,
    )


if __name__ == "__main__":
    main()
