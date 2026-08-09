"""
binance/logger.py
-----------------
Logging for the Binance bot — deliberately separate from atos/logger.py.

The Saxo logger is namespaced to 'atos' and writes to logs/atos_*.log.
This logger is namespaced to 'binance' and writes to logs/binance_*.log
so the two bots' output never interleaves.

Usage:
    from binance.logger import get_logger
    log = get_logger(__name__)
    log.info("Signal found: %s", symbol)
"""

import logging
import os
import sys
from datetime import datetime

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


def get_logger(name: str) -> logging.Logger:
    _setup()
    if name == "__main__":
        name = "binance.main"
    elif not name.startswith("binance"):
        name = f"binance.{name}"
    return logging.getLogger(name)
