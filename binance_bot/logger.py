"""
binance_bot/logger.py
---------------------
Logging for the Binance bot -- deliberately separate from atos/logger.py.

The Saxo logger is namespaced to 'atos' and writes to logs/atos_*.log.
This logger is namespaced to 'binance' and writes to logs/binance_*.log
so the two bots' output never interleaves.

Usage:
    from binance_bot.logger import get_logger
    log = get_logger(__name__)
    log.info("Signal found: %s", symbol)
"""

import json
import logging
import os
import sys
import threading
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR  = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

LOG_FORMAT  = "[%(asctime)s] [%(levelname)-5s] [%(name)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_initialized = False


def _setup():
    global _initialized
    if _initialized:
        return
    _initialized = True

    root = logging.getLogger("binance")
    root.setLevel(logging.DEBUG)
    if root.handlers:
        return

    fmt = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)
    root.addHandler(console)

    today = datetime.now().strftime("%Y-%m-%d")
    daily = logging.FileHandler(
        os.path.join(LOG_DIR, f"binance_{today}.log"), encoding="utf-8"
    )
    daily.setLevel(logging.DEBUG)
    daily.setFormatter(fmt)
    root.addHandler(daily)

    errors = logging.FileHandler(
        os.path.join(LOG_DIR, "binance_errors.log"), encoding="utf-8"
    )
    errors.setLevel(logging.WARNING)
    errors.setFormatter(fmt)
    root.addHandler(errors)


_jsonl_lock = threading.Lock()

TRADES_JSONL  = os.path.join(LOG_DIR, "binance_trades.jsonl")
SIGNALS_JSONL = os.path.join(LOG_DIR, "binance_signals.jsonl")


def _append_jsonl(path: str, record: dict) -> None:
    """Append a single JSON record to a JSONL file.  Thread-safe, never raises."""
    try:
        with _jsonl_lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")
    except Exception:
        pass   # JSONL write failure must never interrupt the bot loop


def log_trade(event: dict) -> None:
    """
    Persist a trade event (BUY entry or SELL exit) to logs/binance_trades.jsonl.

    Required keys: ts, symbol, side, price, qty, strategy
    Optional keys: order_id, rsi, dip_pct, vol_ratio, exit_reason, realized_pnl

    Example:
        log_trade({
            "ts": datetime.now(timezone.utc).isoformat(),
            "symbol": "BTCUSDT", "side": "BUY", "price": 65000.0,
            "qty": 0.001, "strategy": "mean_reversion_v1",
            "order_id": "12345", "rsi": 32.1, "dip_pct": 6.2, "vol_ratio": 2.1,
        })
    """
    _setup()
    _append_jsonl(TRADES_JSONL, event)


def log_signal(event: dict) -> None:
    """
    Persist a scan signal (any action: BUY / QUEUED / SKIP) to logs/binance_signals.jsonl.

    Required keys: ts, scan_id, symbol, action, strategy
    Optional keys: reason, rsi, dip_pct, vol_ratio, price

    Example:
        log_signal({
            "ts": "2026-08-09T11:00:00+00:00",
            "scan_id": "20260809_110000",
            "symbol": "BTCUSDT", "action": "SKIP",
            "reason": "RSI=45.2>=35", "rsi": 45.2, "dip_pct": 2.1,
            "vol_ratio": 0.8, "price": 65000.0, "strategy": "mean_reversion_v1",
        })
    """
    _setup()
    _append_jsonl(SIGNALS_JSONL, event)


def get_logger(name: str) -> logging.Logger:
    _setup()
    if name == "__main__":
        name = "binance.main"
    elif not name.startswith("binance"):
        name = f"binance.{name}"
    return logging.getLogger(name)
