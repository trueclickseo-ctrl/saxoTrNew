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
from datetime import datetime, timezone
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
from binance_bot.logger          import get_logger, log_trade, log_signal
from binance_bot.equity_stop     import EquityStop
from core.broker_interface       import BrokerError
from strategies.binance          import mean_reversion as MR
from strategies.binance.mean_reversion import STRATEGY_ID

EQUITY_STATE_PATH = ROOT / "data" / "binance_equity_state.json"

log = get_logger(__name__)

CONFIG_PATH = ROOT / "binance_bot" / "config" / "binance_testnet_config.yaml"
PID_FILE    = ROOT / "logs" / "binance_bot.pid"

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
                strategy_tag=STRATEGY_ID,
            )
            log.info(
                "Order placed: %s %s qty=%s fill=%s status=%s",
                result.side, result.symbol, result.qty,
                result.filled_price, result.status,
            )
            log_trade({
                "ts":          datetime.now(timezone.utc).isoformat(),
                "symbol":      result.symbol,
                "side":        "BUY",
                "price":       float(result.filled_price or sig.price),
                "qty":         float(result.qty),
                "strategy":    STRATEGY_ID,
                "order_id":    result.order_id,
                "rsi":         round(sig.rsi, 2),
                "dip_pct":     round(sig.dip_pct, 2),
                "vol_ratio":   round(sig.vol_ratio, 3),
                "exit_reason": None,
                "realized_pnl": None,
            })
        except BrokerError as exc:
            log.error("Order failed for %s: %s", sig.symbol, exc)


def _flatten_all(adapter: BinanceAdapter, cfg: dict, dry_run: bool) -> None:
    """
    Close all positions opened by this bot by placing market SELL orders.
    Called when the equity stop fires and equity_stop_flatten is true.

    Uses the same net-qty reconstruction as _open_slots() / _total_open_notional().
    Skips any symbol where net qty is negligible (testnet dust).
    """
    universe = cfg["symbols"]
    log.warning("EQUITY STOP -- FLATTEN: closing all positions (dry_run=%s)", dry_run)
    for symbol in universe:
        try:
            trades  = adapter._c.get_my_trades(symbol, limit=100)
            net_qty = sum(
                float(t["qty"]) * (1.0 if t["isBuyer"] else -1.0)
                for t in trades
            )
            if net_qty <= 1e-8:
                continue
            log.warning("  Flattening %s  qty=%.8f", symbol, net_qty)
            if dry_run:
                log.warning("  (dry_run=True -- SELL order not placed)")
                continue
            result = adapter.place_order(
                symbol=symbol,
                side="SELL",
                qty=net_qty,
                order_type="MARKET",
                strategy_tag="equity_stop_flatten",
            )
            log.warning(
                "  Flatten order placed: %s qty=%s fill=%s status=%s",
                result.symbol, result.qty, result.filled_price, result.status,
            )
            log_trade({
                "ts":           datetime.now(timezone.utc).isoformat(),
                "symbol":       result.symbol,
                "side":         "SELL",
                "price":        float(result.filled_price or 0),
                "qty":          float(result.qty),
                "strategy":     "equity_stop_flatten",
                "order_id":     result.order_id,
                "exit_reason":  "equity_stop",
                "realized_pnl": None,
            })
        except Exception as exc:
            log.error("  Flatten failed for %s: %s", symbol, exc)


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

    # ── Portfolio-level equity stop ───────────────────────────────────────────
    risk_cfg    = cfg.get("risk", {})
    dd_threshold = float(risk_cfg.get("equity_stop_drawdown_pct", 15.0)) / 100.0
    do_flatten   = bool(risk_cfg.get("equity_stop_flatten", False))

    eq_stop = EquityStop(str(EQUITY_STATE_PATH), drawdown_threshold=dd_threshold)
    halted, halt_reason = eq_stop.check(float(balance.total_equity))

    if halted:
        log.warning("*** EQUITY STOP ACTIVE *** %s", halt_reason)
        if do_flatten:
            _flatten_all(adapter, cfg, dry_run=dry_run)
        log.warning("No new entries this scan. Manual reset required to resume.")
        return
    else:
        log.info(
            "Equity stop: OK  peak=%.2f  current=%.2f  drawdown=%.1f%%  threshold=%.0f%%",
            eq_stop.peak_equity,
            float(balance.total_equity),
            (eq_stop.peak_equity - float(balance.total_equity)) / max(eq_stop.peak_equity, 1) * 100,
            dd_threshold * 100,
        )

    open_slots = _open_slots(adapter, cfg)
    log.info("Open slots: %d / %d", open_slots, cfg["capital"]["max_slots"])

    symbols = cfg["symbols"]
    log.info("Scanning %d symbols: %s", len(symbols), ", ".join(symbols))

    scan_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    signals = MR.scan(adapter, symbols, cfg["strategy"], open_slots)
    _print_scan_summary(signals)

    # Structured signal log — every symbol, every scan, regardless of action
    _ts = datetime.now(timezone.utc).isoformat()
    for sig in signals:
        log_signal({
            "ts":        _ts,
            "scan_id":   scan_id,
            "symbol":    sig.symbol,
            "action":    sig.action,
            "reason":    sig.reason,
            "rsi":       round(sig.rsi, 2),
            "dip_pct":   round(sig.dip_pct, 2),
            "vol_ratio": round(sig.vol_ratio, 3),
            "price":     float(sig.price),
            "strategy":  sig.strategy,
        })

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
        # Write PID so the dashboard can detect whether the loop is alive
        try:
            PID_FILE.parent.mkdir(parents=True, exist_ok=True)
            PID_FILE.write_text(str(os.getpid()))
        except Exception:
            pass

        try:
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
        finally:
            try:
                PID_FILE.unlink(missing_ok=True)
            except Exception:
                pass
    else:
        run_once(execute=args.execute)


if __name__ == "__main__":
    main()
