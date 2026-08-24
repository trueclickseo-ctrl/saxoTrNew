"""
Standalone entry point for the ETF strategy.

Run directly (from this directory):
    python run_etf_bot.py

Or add to Windows Task Scheduler via run_etf_daily.bat.
See INTEGRATION_GUIDE.md for all integration options.

This file is the ONLY place that touches the parent app's auth module.
Everything else under saxo_etf_strategy/ is self-contained.
"""

import logging
import os
import sys
import time
from datetime import date

_ETF_DIR = os.path.dirname(os.path.abspath(__file__))
# Insert ETF root first so 'core' resolves to saxo_etf_strategy/core/ — not
# to the parent app's core/ package (which also exists and would shadow ours).
sys.path.insert(0, os.path.join(_ETF_DIR, ".."))  # parent dir for saxo_auth
sys.path.insert(0, _ETF_DIR)                       # ETF root — highest priority for 'core'
import saxo_auth   # the existing shares-app auth module — read-only usage

from core.saxo_client import SaxoClient
from core.etf_universe import ETFUniverseBuilder
from core.etf_strategy import ETFStrategyEngine
from core.etf_executor import ETFExecutor
from core.etf_state import ETFStateStore
from config.etf_config import DEFAULT_CONFIG, ETFConfig


def _get_saxo_token() -> str:
    """
    Token provider for the ETF SaxoClient.
    Reuses the existing shares-app auth module (saxo_auth.get_valid_access_token),
    which handles silent refresh via the stored saxo_token.json.
    We only READ the token — never modify the refresh logic or token file.
    """
    return saxo_auth.get_valid_access_token()


def setup_logging(cfg: ETFConfig) -> None:
    os.makedirs(os.path.dirname(cfg.log_path), exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=[
            logging.FileHandler(cfg.log_path, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


class ETFBot:
    """
    Full ETF pipeline: universe → signals → execution.
    Holds no references to any shares-strategy object — cannot mutate shares state.
    """

    def __init__(self, token_provider=_get_saxo_token, cfg: ETFConfig = DEFAULT_CONFIG):
        self.cfg = cfg
        self.client = SaxoClient(
            base_url=cfg.env.base_url,
            token_provider=token_provider,
            max_retries=cfg.universe.max_retries,
            request_delay_sec=cfg.universe.request_delay_sec,
        )
        self.universe_builder = ETFUniverseBuilder(self.client, cfg.universe)
        self.strategy         = ETFStrategyEngine(self.client, cfg.strategy)
        self.state            = ETFStateStore(cfg.state_path)
        self.executor         = ETFExecutor(self.client, self.state, cfg)

    def run_once(self, force_refresh_universe: bool = False) -> None:
        log = logging.getLogger("etf_bot")

        # US-listed ETFs don't trade on weekends -- skip the whole cycle
        # rather than burn a universe fetch + strategy scan + exit review
        # against a closed market every single Saturday/Sunday.
        if date.today().weekday() >= 5:  # 5=Saturday, 6=Sunday
            log.info("=== ETF run skipped -- weekend, US market closed ===")
            return

        log.info(f"=== ETF run  strategy={self.cfg.strategy.strategy_name}  "
                 f"dry_run={self.cfg.dry_run} ===")

        universe = self.universe_builder.get_universe(force_refresh=force_refresh_universe)
        log.info(f"Universe: {len(universe)} ETFs")

        # Exits first — free slots before looking for entries
        self.executor.review_exits()

        signals = self.strategy.generate_signals(universe)
        log.info(f"Signals: {len(signals)} BUY candidate(s) this run")

        self.executor.process_signals(signals)
        log.info("=== ETF run complete ===")

        if not self.cfg.dry_run:
            # See housekeeping.py's module docstring (project root) -- catches
            # local state drift against live Saxo (e.g. a position netted
            # away by another module trading the same underlying) right
            # after every real run.
            try:
                import housekeeping
                housekeeping.reconcile_all(["etf"])
                housekeeping.scan_naked_positions()
            except Exception:
                log.exception("[HOUSEKEEPING] post-run reconciliation failed")

    def run_forever(self) -> None:
        interval_sec = self.cfg.strategy.rebalance_frequency_hours * 3600
        while True:
            try:
                self.run_once()
            except Exception:
                logging.getLogger("etf_bot").exception("ETF run failed — will retry next interval")
            time.sleep(interval_sec)


if __name__ == "__main__":
    # Gracefully exit if the Saxo token is not available (e.g. Task Scheduler firing
    # before the daily_run has had a chance to refresh it).
    try:
        _get_saxo_token()
    except RuntimeError as e:
        print(f"[etf_bot] Token not available — skipping run: {e}")
        sys.exit(0)

    setup_logging(DEFAULT_CONFIG)
    bot = ETFBot(token_provider=_get_saxo_token, cfg=DEFAULT_CONFIG)
    bot.run_once()
