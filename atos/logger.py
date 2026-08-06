"""
atos/logger.py
--------------
Central logging module for ATOS — Algorithmic Trading Operating System.

Provides structured logging to both console (stdout) and rotating daily
log files under logs/. Every module should use this instead of print().

Usage in any module:
    from atos.logger import get_logger
    logger = get_logger(__name__)
    logger.info("Trade placed for %s", ticker)
    logger.warning("Missing UIC for %s", ticker)
    logger.error("API call failed", exc_info=True)  # includes stack trace

Log files:
    logs/atos_YYYY-MM-DD.log   — one file per day, auto-rotated
    logs/atos_errors.log       — errors/warnings only (persistent)

Format:
    [2026-08-05 17:30:00] [INFO ] [atos_runner] Trade placed for AAPL
"""

import logging
import os
import sys
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler

# ── Paths ──────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(BASE_DIR, "logs")

# ── Ensure log directory exists ────────────────────────────────────
os.makedirs(LOG_DIR, exist_ok=True)

# ── Format ─────────────────────────────────────────────────────────
LOG_FORMAT = "[%(asctime)s] [%(levelname)-5s] [%(name)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# ── Root logger setup (run once) ───────────────────────────────────
_initialized = False


def _setup_root_logger():
    """Configure the root ATOS logger with file and console handlers."""
    global _initialized
    if _initialized:
        return
    _initialized = True

    root = logging.getLogger("atos")
    root.setLevel(logging.DEBUG)

    # Prevent duplicate handlers if called multiple times
    if root.handlers:
        return

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    # ── Console handler (INFO+) ────────────────────────────────────
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)
    root.addHandler(console)

    # ── Daily rotating file handler (DEBUG+) ───────────────────────
    today = datetime.now().strftime("%Y-%m-%d")
    daily_file = os.path.join(LOG_DIR, f"atos_{today}.log")
    file_handler = logging.FileHandler(daily_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # ── Error-only file handler (WARNING+) ─────────────────────────
    error_file = os.path.join(LOG_DIR, "atos_errors.log")
    error_handler = logging.FileHandler(error_file, encoding="utf-8")
    error_handler.setLevel(logging.WARNING)
    error_handler.setFormatter(formatter)
    root.addHandler(error_handler)


def get_logger(name: str) -> logging.Logger:
    """
    Get a named logger under the 'atos' namespace.

    Args:
        name: Module name (typically __name__). Will be shortened
              for readability (e.g., 'atos.risk' instead of 'atos.risk').

    Returns:
        Configured logger instance.
    """
    _setup_root_logger()

    # Clean up module name for readability
    if name == "__main__":
        name = "atos.main"
    elif not name.startswith("atos"):
        name = f"atos.{name}"

    return logging.getLogger(name)


def log_cycle_separator(logger: logging.Logger, title: str = "ATOS Daily Cycle"):
    """Log a visual separator for cycle start/end in log files."""
    separator = "=" * 60
    logger.info(separator)
    logger.info(title)
    logger.info(separator)


def log_section(logger: logging.Logger, title: str):
    """Log a section header within a cycle."""
    logger.info("── %s ──────────────────────────────────────", title)
