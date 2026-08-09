"""
bots/run_binance_bot.py
-----------------------
Entry point for the Binance testnet bot.  Runs as its own process,
completely independent of the Saxo bot (atos_runner.py).

Usage:
    python bots/run_binance_bot.py            # single scan, dry-run
    python bots/run_binance_bot.py --execute  # single scan, place orders if signal
    python bots/run_binance_bot.py --loop     # continuous loop (uses scan_interval_minutes)

Kill switch: create a file named STOP_BINANCE in the project root to halt the
loop cleanly between scans (same pattern as the Saxo STOP_TRADING file).

Auth: HMAC-SHA256 (different from Saxo OAuth2).
      Keys stored in .env.binance (gitignored), never in code.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
from decimal import Decimal
from pathlib import Path

# ── Ensure project root is on sys.path ───────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

KILL_SWITCH = ROOT / "STOP_BINANCE"

# ── Load .env.binance before any other local imports ─────────────────────────
try:
    from dotenv import load_dotenv          # type: ignore
    env_path = ROOT / ".env.binance"
    if env_path.exists():
        load_dotenv(env_path)
    else:
        print(
            "[WARN] .env.binance not found -- copy .env.binance from the template "
            "and fill in BINANCE_TESTNET_API_KEY / BINANCE_TESTNET_API_SECRET"
        )
except ImportError:
    print("[WARN] python-dotenv not installed -- run: pip install python-dotenv")

# ── Now safe to import project modules ───────────────────────────────────────
try:
    import yaml                             # type: ignore
except ImportError:
    print("ERROR: pyyaml not installed -- run: pip install pyyaml")
    sys.exit(1)

from binance_bot.binance_client  import BinanceClient
from binance_bot.binance_adapter import BinanceAdapter
from binance_bot.logger          import get_logger
from core.broker_interface       import BrokerError
from strategies.binance          import mean_reversion as MR

log = get_logger(__name__)

CONFIG_PATH = ROOT / "binance_bot" / "config" / "binance_testnet_config.yaml"

# Optional: reuse the existing Saxo email notifier for crash alerts.
# It reads the same config/email.json so no extra setup is needed.
try:
    from atos.notifier import _send, _wrap   # type: ignore
    _EMAIL_AVAILABLE = True
except ImportError:
    _EMAIL_AVAILABLE = False


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _kill_switch_active() -> bool:
    """Return True if STOP_BINANCE file exists -- halts the loop cleanly."""
    return KILL_SWITCH.exists()


def _send_crash_alert(exc: Exception) -> None:
    """Email a crash notification so an unattended loop failure is visible."""
    if not _EMAIL_AVAILABLE:
        return
    try:
        tb = traceback.format_exc()
        body = (
            '<p style="color:#f87171;font-size:15px;font-weight:700">'
            "Binance bot loop crashed and stopped."
            "</p>"
            '<pre style="background:#0f172a;color:#94a3b8;padding:12px;border-radius:6px;'
            'font-size:12px;overflow-x:auto">' + tb + "</pre>"
            '<p style="color:#94a3b8;font-size:13px">'
            "To resume: fix the issue, delete STOP_BINANCE if present, and "
            "re-run: <code>python bots/run_binance_bot.py --loop</code>"
            "</p>"
        )
        _send(
            subject=f"ATOS Binance -- LOOP CRASHED: {type(exc).__name__}",
            html=_wrap("Binance Bot Crash Alert", body),
        )
    except Exception:
        pass   # email failure must never mask the original error


def _open_slots(adapter: BinanceAdapter, cfg: dict) -> int:
    """
    Count slots used by positions WE opened (via our own API key trade history).

    Using balance alone is unreliable on testnet because Binance pre-loads
    all universe assets into new accounts. get_my_trades() only returns
    trades placed by our key, so pre-loaded assets are invisible here.

    Durability note: this reconstruction from trade history works for testnet
    but has gaps (pagination, manual trades, partial fills). For production,
    maintain position state in a database updated on every fill instead.
    See docs/binance/testnet_vs_live.md for details.
    """
    max_slots = int(cfg["capital"]["max_slots"])
    universe  = cfg["symbols"]
    open_count = 0
    open_symbols = []
    for symbol in universe:
        try:
            trades = adapter._c.get_my_trades(symbol, limit=100)
            net_qty = sum(
                float(t["qty"]) * (1.0 if t["isBuyer"] else -1.0)
                for t in trades
            )
            if net_qty > 1e-8:
                open_count += 1
                open_symbols.append(symbol)
        except Exception:
            pass
    if open_symbols:
        log.info("Our open positions: %s", ", ".join(open_symbols))
    else:
        log.info("Our open positions: none (testnet pre-loads excluded)")
    return max(0, max_slots - open_count)


def _total_open_notional(adapter: BinanceAdapter, cfg: dict) -> Decimal:
    """
    Estimate total USDT value of all positions we opened.
    Used to enforce max_account_risk_pct before placing a new order.
    """
    universe = cfg["symbols"]
    total = Decimal("0")
    for symbol in universe:
        try:
            trades = adapter._c.get_my_trades(symbol, limit=100)
            net_qty = sum(
                float(t["qty"]) * (1.0 if t["isBuyer"] else -1.0)
                for t in trades
            )
            if net_qty > 1e-8:
                ticker = adapter._c.get_symbol_ticker(symbol)
                total += Decimal(str(net_qty)) * Decimal(ticker["price"])
        except Exception:
            pass
    return total


def _print_scan_summary(signals: list) -> None:
    buys    = [s for s in signals if s.action == "BUY"]
    queued  = [s for s in signals if s.action == "QUEUED"]
    skipped = [s for s in signals if s.action == "SKIP"]

    log.info("--- Scan results ---")
    for s in buys:
        log.info(
            "  BUY    %s  price=%.4f  RSI=%.1f  dip=%.1f%%  vol=%.2fx",
            s.symbol, s.price, s.rsi, s.dip_pct, s.vol_ratio
        )
    for s in queued:
        log.info(
            "  QUEUED %s  price=%.4f  RSI=%.1f  dip=%.1f%%  (no free slot)",
            s.symbol, s.price, s.rsi, s.dip_pct
        )
    for s in skipped:
        log.debug("  SKIP   %s  %s", s.symbol, s.reason)
    log.info("  Total: %d buy / %d queued / %d skip", len(buys), len(queued), len(skipped))


def _execute_buys(adapter: BinanceAdapter, signals: list, cfg: dict) -> None:
    """
    Place market orders for all BUY signals.

    Exposure cap: total open notional must stay below
    max_account_risk_pct * total_equity before each new order is placed.
    """
    balance       = adapter.get_account_balance()
    pos_size_pct  = Decimal(str(cfg["capital"]["position_size_pct"])) / 100
    max_risk_pct  = Decimal(str(cfg["capital"]["max_account_risk_pct"])) / 100
    max_risk_usdt = balance.total_equity * max_risk_pct

    for sig in signals:
        if sig.action != "BUY":
            continue

        # Exposure cap check before each order
        open_notional = _total_open_notional(adapter, cfg)
        if open_notional >= max_risk_usdt:
            log.warning(
                "Exposure cap hit: open %.2f USDT >= max %.2f USDT (%.0f%% of equity). "
                "Skipping %s.",
                open_notional, max_risk_usdt,
                float(max_risk_pct) * 100, sig.symbol,
            )
            continue

        notional = balance.available_cash * pos_size_pct
        qty      = notional / sig.price
        try:
            result = adapter.place_order(
                symbol=sig.symbol,
                side="BUY",
                qty=qty,
                order_type="MARKET",
                strategy_tag="crypto_mean_reversion",
            )
            log.info(
                "Order placed: %s %s qty=%s fill=%s status=%s",
                result.side, result.symbol, result.qty,
                result.filled_price, result.status,
            )
        except BrokerError as exc:
            log.error("Order failed for %s: %s", sig.symbol, exc)


# ── Main ─────────────────────────────────────────────────────────────────────

def run_once(execute: bool = False) -> None:
    cfg     = _load_config()
    dry_run = cfg["execution"].get("dry_run", True) and not execute

    log.info(
        "=== Binance Testnet Bot -- %s ===",
        "DRY RUN" if dry_run else "LIVE ORDERS",
    )

    try:
        client  = BinanceClient.from_env()
        adapter = BinanceAdapter(client)
    except (ValueError, RuntimeError) as exc:
        log.error("Cannot start bot: %s", exc)
        return

    balance = adapter.get_account_balance()
    log.info(
        "Account balance: %.2f USDT free / %.2f USDT total",
        balance.available_cash, balance.total_equity,
    )

    open_slots = _open_slots(adapter, cfg)
    log.info("Open slots: %d / %d", open_slots, cfg["capital"]["max_slots"])

    symbols = cfg["symbols"]
    log.info("Scanning %d symbols: %s", len(symbols), ", ".join(symbols))

    signals = MR.scan(adapter, symbols, cfg["strategy"], open_slots)
    _print_scan_summary(signals)

    if not dry_run:
        _execute_buys(adapter, signals, cfg)
    else:
        buys = [s for s in signals if s.action == "BUY"]
        if buys:
            log.info("Dry run -- %d BUY signals found but no orders placed.", len(buys))
            log.info("Run with --execute to place real testnet orders.")
        else:
            log.info("Dry run -- no signals this scan.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Binance testnet mean-reversion bot")
    parser.add_argument("--execute", action="store_true",
                        help="Place testnet orders (overrides dry_run in config)")
    parser.add_argument("--loop",    action="store_true",
                        help="Run continuously on scan_interval_minutes")
    args = parser.parse_args()

    if args.loop:
        cfg          = _load_config()
        interval_min = int(cfg["execution"].get("scan_interval_minutes", 60))
        log.info(
            "Loop mode -- scanning every %d minutes. "
            "Ctrl+C or touch STOP_BINANCE to stop cleanly.",
            interval_min,
        )
        while True:
            if _kill_switch_active():
                log.info("STOP_BINANCE file detected -- shutting down loop cleanly.")
                break
            try:
                run_once(execute=args.execute)
            except KeyboardInterrupt:
                log.info("Stopped by user (Ctrl+C).")
                break
            except Exception as exc:
                log.error("Unhandled error in loop: %s", exc, exc_info=True)
                _send_crash_alert(exc)
                log.info("Crash alert sent. Continuing loop in %d minutes...", interval_min)
            log.info("Next scan in %d minutes...", interval_min)
            time.sleep(interval_min * 60)
    else:
        run_once(execute=args.execute)


if __name__ == "__main__":
    main()
