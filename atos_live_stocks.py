"""
atos_live_stocks.py
-------------------
Real-money US Blend stocks sleeve. SEPARATE module from ATOS LIVE FOREX
(forex/runner.py --account live) and from the SIM stocks engine
(atos_runner.run_cycle). It SHARES only the Saxo LIVE SEK sub-account login
(1070996INET) -- Saxo pools margin + positions across sub-accounts, so this
filters every snapshot by AccountKey AND AssetType=="Stock" to touch only
its own rows.

Owns:
  * ledger        data/atos_live_stocks.db          (ATOS_DB_PATH)
  * risk state    data/atos_live_stocks_risk_state.json (ATOS_RISK_STATE_FILE)
  * blend clock   data/us_momentum_state_live.json   (ATOS_US_MOMENTUM_STATE)
  * process lock  proc_lock.ATOS_LIVE_STOCKS_LOCK
  * capital cap   config/capital.json strategies.stocks_live.risk_equity_sek (30k)
  * AI env        "live_stocks"  (ai/config.py -- shadow/log only, no apply path)
  * instrument map data/instrument_map_live.csv  (LIVE Uics, USD-only)

Strategy: US Blend ONLY. Hard allowlist -- any --strategy token != "US Blend"
is an argparse error. US Reversion / the legacy per-market engine never run here.

ROLLOUT -- PHASE 1 = OBSERVE ONLY. dry_run is True unless ALL of:
  --live  AND  SAXO_LIVE_STOCKS_CONFIRMED=1  AND  LIVE_STOCKS_DRY_RUN != "0"
  is false  AND  not LIVE_STOCKS_TRADING_HALTED.
In dry-run every would-be order is logged to
data/us_blend_live_would_be_orders.jsonl + an AI card; ZERO real orders.
Phase 2 (separate approval) flips the env vars + the daily .bat to pass --live.

Usage (Claude may run the non---live forms):
    python atos_live_stocks.py --info          # SIM/LIVE Uic diff, no orders
    python atos_live_stocks.py                 # observe-only cycle
    python atos_live_stocks.py --exits-only    # observe-only, no new buys
    python atos_live_stocks.py --dashboard     # live view (refreshes every 30s)
    python atos_live_stocks.py --dashboard --fast   # refresh every 5s
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)

# ── Own state-file locations (set BEFORE importing atos_runner / atos.*) ──
os.environ.setdefault("ATOS_DB_PATH",            os.path.join(_ROOT, "data", "atos_live_stocks.db"))
os.environ.setdefault("ATOS_RISK_STATE_FILE",    os.path.join(_ROOT, "data", "atos_live_stocks_risk_state.json"))
os.environ.setdefault("ATOS_US_MOMENTUM_STATE",  os.path.join(_ROOT, "data", "us_momentum_state_live.json"))

import proc_lock
import saxo_client
import atos.capital_config as CAP

LIVE_STOCKS_ALLOWED_STRATEGIES = {"US Blend"}

# Emergency stop -- set True here (or LIVE_STOCKS_TRADING_HALTED=1 in env) to
# force dry-run regardless of every other flag. Mirrors forex's LIVE_TRADING_HALTED.
LIVE_STOCKS_TRADING_HALTED = False

# Safety rails (mirror forex LIVE)
MAX_MARGIN_UTILIZATION_PCT = 50.0   # pooled account -- shared with forex LIVE wind-down
CASH_BUFFER_PCT            = 0.10   # keep 10% of the 30k un-deployed
MAX_DAILY_LOSS_PCT         = 0.03   # ~900 SEK/day on 30k -> exits-only when breached

WOULD_BE_ORDERS = os.path.join(_ROOT, "data", "us_blend_live_would_be_orders.jsonl")
STATUS_FILE     = os.path.join(_ROOT, "data", "stocks_live_status.json")


def _write_status(dry_run: bool, exits_only: bool, snap: dict, rails: dict, result: dict) -> None:
    """Persist the last scan for live_stocks_dashboard.py -- the LIVE analogue
    of atos_runner._write_status() / data/atos_status.json. The blend target
    basket (result['signal']) is the 'scan signal'; result['actions'] are the
    would-be (or, in Phase 2, real) orders this scan produced."""
    sig = result.get("signal") or {}
    payload = {
        "status": "complete",
        "timestamp": datetime.now().isoformat(),
        "dry_run": dry_run,
        "exits_only": exits_only,
        "account_key": snap.get("account_key"),
        "open_positions": snap.get("_n_pos", 0),
        "working_orders": snap.get("_n_ord", 0),
        "budget_sek": rails.get("budget_sek"),
        "margin_util_pct": rails.get("margin_util_pct"),
        "rails_notes": rails.get("notes", []),
        "signal": {
            "targets":  sig.get("targets", []),
            "risk_off": sig.get("risk_off", False),
            "reason":   sig.get("reason", ""),
            "momentum": sig.get("momentum", []),
            "lowvol":   sig.get("lowvol", []),
        },
        "actions": result.get("actions", []),
        "buy": result.get("buy", 0),
        "sell": result.get("sell", 0),
    }
    try:
        tmp = STATUS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, STATUS_FILE)
    except Exception as exc:
        print(f"  [live stocks] could not write status file: {exc}")


# ── Safety rails (Phase 1: computed + logged, NOT gating -- no orders) ────

def _today_realized_pnl_sek() -> float:
    """Sum of realized P&L on US Blend rows in data/atos_live_stocks.db that
    CLOSED today. 0.0 if the ledger is empty / unreadable (Phase 1)."""
    import sqlite3
    from datetime import date
    dbp = os.environ["ATOS_DB_PATH"]
    if not os.path.exists(dbp):
        return 0.0
    try:
        con = sqlite3.connect(dbp)
        try:
            rows = con.execute(
                "select pnl_sek from trades where exit_price is not null "
                "and strategy='US Blend' and substr(coalesce(exit_date,''),1,10)=?",
                (date.today().isoformat(),)).fetchall()
        finally:
            con.close()
        return float(sum((r[0] or 0.0) for r in rows))
    except Exception:
        return 0.0


def _pooled_balance() -> dict:
    try:
        return saxo_client.get_balances(env="live")
    except Exception as exc:
        print(f"  [live stocks] balances/me lookup failed ({exc}) -- rails fail-open")
        return {}


def live_stocks_rails(bal: dict) -> dict:
    """Returns {budget_sek, margin_util_pct, margin_ok, exits_only, notes[]}.

    budget = min(pooled TotalValue, 30k cap) * (1 - cash buffer).
    margin_ok fails OPEN on a lookup miss (mirrors forex _margin_allows_entry).
    exits_only True when the sleeve's own daily-loss cap is breached."""
    notes: list[str] = []
    cap = CAP.stocks_live_risk_equity_sek()
    pooled = float(bal.get("TotalValue") or bal.get("NetEquityForMargin") or 0.0)
    base = min(pooled, cap) if pooled > 0 else cap
    budget = round(base * (1 - CASH_BUFFER_PCT), 2)
    notes.append(f"budget {budget:,.0f} SEK = min(pooled {pooled:,.0f}, cap {cap:,.0f}) "
                 f"* (1 - {CASH_BUFFER_PCT:.0%} buffer)")

    util = bal.get("InitialMargin", {}).get("MarginUtilizationPct")
    if util is None:
        margin_ok = True
        notes.append("margin utilization unavailable -- rail fails OPEN")
    else:
        margin_ok = float(util) < MAX_MARGIN_UTILIZATION_PCT
        notes.append(f"margin utilization {float(util):.1f}% "
                     f"({'OK' if margin_ok else 'OVER'} vs {MAX_MARGIN_UTILIZATION_PCT:.0f}%)")

    # Daily-loss cap computed against THIS sleeve's own 30k base and its own
    # ledger -- NOT atos.risk.daily_loss_cap_breached(), which is anchored to
    # SIM's STARTING_CAPITAL_SEK (10.4M) constant and would read ~100%
    # drawdown for a fresh, empty LIVE risk-state file.
    exits_only = False
    try:
        today_pnl = _today_realized_pnl_sek()
        if base > 0 and today_pnl < 0 and abs(today_pnl) / base >= MAX_DAILY_LOSS_PCT:
            exits_only = True
            notes.append(f"daily-loss {today_pnl:,.0f} SEK is >{MAX_DAILY_LOSS_PCT:.0%} of "
                         f"{base:,.0f} -> exits-only")
        else:
            notes.append(f"daily P&L {today_pnl:+,.0f} SEK (cap {MAX_DAILY_LOSS_PCT:.0%} of {base:,.0f})")
    except Exception as exc:
        notes.append(f"daily-loss cap check skipped ({exc})")

    return {"budget_sek": budget, "margin_util_pct": util,
            "margin_ok": margin_ok, "exits_only": exits_only, "notes": notes}


# ── --info : SIM vs LIVE Uic diff for every Blend-eligible ticker ────────

def cmd_info() -> int:
    from instrument_map import load_instrument_map, MAP_FILE_LIVE
    try:
        sim_map = load_instrument_map()
    except Exception as exc:
        print(f"SIM map load failed: {exc}"); return 1
    if not os.path.exists(MAP_FILE_LIVE):
        print(f"LIVE map {MAP_FILE_LIVE} not built yet.\n"
              f"Operator: run `python lookup_instruments_live.py` (read-only, hits LIVE ref-data).")
        return 1
    live_map = load_instrument_map(path=MAP_FILE_LIVE, require_usd=True)

    from atos.universe import US_TICKERS
    print(f"US Blend LIVE instrument map — {len(live_map)}/{len(set(US_TICKERS))} US tickers mapped\n")
    print(f"{'ticker':<10}{'SIM uic':>10}{'LIVE uic':>10}   note")
    missing = 0
    for tk in sorted(set(US_TICKERS)):
        s = sim_map.get(tk, {}).get("uic")
        l = live_map.get(tk, {}).get("uic")
        if l is None:
            missing += 1
            note = "MISSING from LIVE map — excluded from the LIVE Blend set"
        elif s == l:
            note = "same uic on SIM & LIVE (verify a sample in SaxoTraderGO)"
        else:
            note = "SIM/LIVE uic differ (expected) — LIVE uic is authoritative here"
        if l is None or s != l:
            print(f"{tk:<10}{str(s):>10}{str(l):>10}   {note}")
    print(f"\n{missing} ticker(s) missing from the LIVE map.")
    print("No orders placed. This is a read-only diff.")
    return 0


# ── main cycle ─────────────────────────────────────────────────────────

def run(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true",
                    help="attempt real orders (still needs SAXO_LIVE_STOCKS_CONFIRMED=1 "
                         "and LIVE_STOCKS_DRY_RUN=0). Phase 1: omit -> observe only.")
    ap.add_argument("--exits-only", action="store_true",
                    help="manage open positions only, no new buys")
    ap.add_argument("--strategy", default=None,
                    help="US Blend only -- any other value is a hard error")
    ap.add_argument("--info", action="store_true", help="SIM/LIVE Uic diff, no orders")
    ap.add_argument("--dashboard", action="store_true",
                    help="open live_stocks_dashboard.py (refreshes every 30s)")
    ap.add_argument("--fast", action="store_true", help="with --dashboard: refresh every 5s")
    ap.add_argument("--once", action="store_true", help="with --dashboard: print once and exit")
    args = ap.parse_args(argv)

    if args.strategy is not None and args.strategy not in LIVE_STOCKS_ALLOWED_STRATEGIES:
        ap.error(f"--strategy: this module only runs {sorted(LIVE_STOCKS_ALLOWED_STRATEGIES)} "
                 f"(got {args.strategy!r}). US Blend is a hard restriction on the "
                 f"real-money stocks sleeve, not a default.")

    if args.info:
        return cmd_info()
    if args.dashboard or args.fast or args.once:   # --fast / --once imply --dashboard
        import subprocess
        extra = (["--fast"] if args.fast else []) + (["--once"] if args.once else [])
        return subprocess.call([sys.executable, "-X", "utf8",
                                os.path.join(_ROOT, "live_stocks_dashboard.py"), *extra])

    # ── decide dry-run ──────────────────────────────────────────────────
    halted = LIVE_STOCKS_TRADING_HALTED or os.environ.get("LIVE_STOCKS_TRADING_HALTED") == "1"
    confirmed = os.environ.get("SAXO_LIVE_STOCKS_CONFIRMED") == "1"
    dry_env = os.environ.get("LIVE_STOCKS_DRY_RUN", "1") != "0"
    dry_run = dry_env or (not args.live) or (not confirmed) or halted

    tag = "[LIVE STOCKS DRY-RUN]" if dry_run else "[LIVE STOCKS]"
    print(f"\n{'='*64}\n{tag} US Blend sleeve — {datetime.now():%Y-%m-%d %H:%M:%S}\n{'='*64}")
    if dry_run:
        reasons = []
        if dry_env:      reasons.append("LIVE_STOCKS_DRY_RUN!=0")
        if not args.live: reasons.append("no --live")
        if not confirmed: reasons.append("SAXO_LIVE_STOCKS_CONFIRMED!=1")
        if halted:       reasons.append("LIVE_STOCKS_TRADING_HALTED")
        print(f"  OBSERVE ONLY — no real orders will be placed ({', '.join(reasons)})")

    if not proc_lock.acquire(proc_lock.ATOS_LIVE_STOCKS_LOCK, "atos_live_stocks"):
        print("  could not acquire the live-stocks lock — proceeding unprotected")
    try:
        import atos_runner
        atos_runner.set_stocks_env("live")

        # snapshot (read-only) + rails
        import housekeeping_live_stocks as hk_stocks
        try:
            _s = hk_stocks.fetch_live_stock_snapshot()
            n_pos = sum(1 for u in _s.positions_by_uic if _s.net_amount(u))
            n_ord = sum(len(v) for v in _s.orders_by_uic.values())
            snap = {"account_key": _s.account_key, "positions": [n_pos], "orders": [n_ord],
                    "_n_pos": n_pos, "_n_ord": n_ord}
            print(f"  Saxo LIVE SEK account {(_s.account_key or '?')[:8]}… — "
                  f"{n_pos} stock position(s), {n_ord} working order(s)")
        except Exception as exc:
            print(f"  [live stocks] snapshot failed: {exc}")
            snap = {"account_key": None, "positions": [], "orders": [], "_n_pos": 0, "_n_ord": 0}

        bal = _pooled_balance()
        rails = live_stocks_rails(bal)
        for n in rails["notes"]:
            print(f"    rail: {n}")
        exits_only = args.exits_only or rails["exits_only"]

        result = atos_runner.run_us_blend_live(
            budget_sek=rails["budget_sek"], dry_run=dry_run, exits_only=exits_only,
        )
        print(f"  {tag} done — {result['buy']} buy / {result['sell']} sell "
              f"({'observe-only, logged to us_blend_live_would_be_orders.jsonl' if dry_run else 'real orders'})")

        _write_status(dry_run, exits_only, snap, rails, result)

        # post-run safety pass (built now, runs no-op while there are no positions)
        try:
            import safeguard_live_stocks
            safeguard_live_stocks.run_safeguard_live_stocks()
        except Exception as exc:
            print(f"  [live stocks] safeguard pass skipped: {exc}")

        _email_summary(tag, dry_run, snap, rails, result)
        return 0
    finally:
        proc_lock.release(proc_lock.ATOS_LIVE_STOCKS_LOCK)


def _email_summary(tag, dry_run, snap, rails, result) -> None:
    try:
        import housekeeping_live as hk
    except Exception:
        return
    now = datetime.now().strftime("%d %b %Y  %H:%M PKT")
    notes = "".join(f"<li>{n}</li>" for n in rails["notes"])
    html = f"""<!DOCTYPE html><html><body style="font-family:sans-serif">
    <h2 style="color:{'#666' if dry_run else '#c0392b'}">ATOS {tag} — US Blend sleeve</h2>
    <p style="color:#666">{now} — {'OBSERVE ONLY, no real orders' if dry_run else 'REAL MONEY'}</p>
    <p>{result['buy']} would-be buy · {result['sell']} would-be sell ·
       {snap.get('_n_pos', 0)} open stock position(s)</p>
    <ul style="color:#444">{notes}</ul>
    <p style="color:#999;font-size:12px">Separate module from ATOS LIVE FOREX; shares only the
    Saxo SEK sub-account login. Would-be orders: data/us_blend_live_would_be_orders.jsonl.</p>
    </body></html>"""
    try:
        hk._send_email_live(f"{tag} US Blend — {result['buy']}B/{result['sell']}S — {now}", html)
    except Exception:
        pass


if __name__ == "__main__":
    raise SystemExit(run())
