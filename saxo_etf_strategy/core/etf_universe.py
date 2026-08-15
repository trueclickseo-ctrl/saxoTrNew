"""
Builds and caches the widest possible ETF universe from Saxo's API.

Strategy for "maximum universe":
1. Global pass: page through /ref/v1/instruments?AssetTypes=Etf with
   $top/$skip until a page is smaller than page_size.
2. Per-exchange pass: list exchanges via /ref/v1/exchanges, then repeat
   step 1 with ExchangeId=<id> for each. Catches instruments the global
   pass may have truncated.
3. De-duplicate by Uic (Saxo's unique instrument id) and cache to disk.

Note: ExchangeId is accepted as a filter by /ref/v1/instruments but Saxo
may ignore it for exchanges it does not map cleanly. The fallback global
pass ensures nothing is missed if the per-exchange filter is a no-op.

This module only writes to its own cache file (etf_universe.json) and
never reads or writes any file used by the shares strategies.
"""

import json
import logging
import os
import time
from typing import Dict, List, Optional

from core.saxo_client import SaxoClient
from config.etf_config import ETFUniverseConfig

logger = logging.getLogger("etf_strategy.universe")


class ETFUniverseBuilder:
    def __init__(self, client: SaxoClient, cfg: ETFUniverseConfig):
        self.client = client
        self.cfg    = cfg

    # ---------- public API ----------

    def get_universe(self, force_refresh: bool = False) -> List[dict]:
        """Return cached universe, rebuilding if stale or missing."""
        cached = self._load_cache()
        if not force_refresh and cached is not None:
            age_hours = (time.time() - cached["built_at"]) / 3600
            if age_hours < self.cfg.cache_ttl_hours:
                logger.info(f"Cached ETF universe: {len(cached['instruments'])} ETFs "
                            f"({age_hours:.1f}h old)")
                return cached["instruments"]

        logger.info("Rebuilding ETF universe from Saxo API...")
        universe = self._build_universe()
        self._save_cache(universe)
        return universe

    # ---------- internals ----------

    def _list_exchanges(self) -> List[str]:
        if self.cfg.exchange_ids:
            return self.cfg.exchange_ids
        try:
            resp = self.client.get("/ref/v1/exchanges", params={"$top": 1000})
            ids  = [row["ExchangeId"] for row in resp.get("Data", []) if row.get("ExchangeId")]
            logger.info(f"Found {len(ids)} exchanges to scan")
            return ids
        except Exception as exc:
            logger.warning(f"Could not list exchanges ({exc}); using global pass only")
            return []

    def _page_through(self, params: dict) -> List[dict]:
        results = []
        skip    = 0
        while True:
            p = dict(params, **{"$top": self.cfg.page_size, "$skip": skip})
            resp = self.client.get("/ref/v1/instruments", params=p)
            data = resp.get("Data", [])
            results.extend(data)
            if len(data) < self.cfg.page_size:
                break
            skip += self.cfg.page_size
        return results

    def _build_universe(self) -> List[dict]:
        by_uic: Dict[int, dict] = {}

        # Pass 1: global scan (no exchange filter)
        try:
            hits = self._page_through({"AssetTypes": self.cfg.asset_type})
            for row in hits:
                uic = row.get("Identifier")
                if uic is not None:
                    by_uic[uic] = row
            logger.info(f"Global pass: {len(hits)} ETFs ({len(by_uic)} unique so far)")
        except Exception as exc:
            logger.warning(f"Global pass failed: {exc}")

        # Pass 2: per-exchange scan to recover any instruments the global pass truncated
        exchange_ids = self._list_exchanges()
        for i, ex_id in enumerate(exchange_ids):
            try:
                hits = self._page_through({
                    "AssetTypes": self.cfg.asset_type,
                    "ExchangeId": ex_id,
                })
                added = 0
                for row in hits:
                    uic = row.get("Identifier")
                    if uic is not None and uic not in by_uic:
                        by_uic[uic] = row
                        added += 1
                if hits:
                    logger.info(f"[{i+1}/{len(exchange_ids)}] {ex_id}: "
                                f"{len(hits)} ETFs ({added} new, total {len(by_uic)})")
            except Exception as exc:
                logger.warning(f"Exchange {ex_id} scan failed: {exc}")

        universe = list(by_uic.values())
        logger.info(f"Built ETF universe: {len(universe)} unique instruments")
        return universe

    def _load_cache(self) -> Optional[dict]:
        if not os.path.exists(self.cfg.cache_path):
            return None
        try:
            with open(self.cfg.cache_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            logger.warning(f"Failed to read universe cache: {exc}")
            return None

    def _save_cache(self, universe: List[dict]) -> None:
        os.makedirs(os.path.dirname(self.cfg.cache_path) or ".", exist_ok=True)
        with open(self.cfg.cache_path, "w", encoding="utf-8") as f:
            json.dump({"built_at": time.time(), "instruments": universe}, f)
        logger.info(f"Cached {len(universe)} ETFs → {self.cfg.cache_path}")
