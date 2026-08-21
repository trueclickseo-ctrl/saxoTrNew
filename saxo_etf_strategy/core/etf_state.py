"""
Persistent state for ETF positions only.

Writes to config.state_path (default: saxo_etf_strategy/data/etf_positions.json).
Completely separate from the shares strategies' data files — different
directory, different filename, never touched by shares code.
"""

import json
import os
import logging
from typing import Dict, Optional

logger = logging.getLogger("etf_strategy.state")


class ETFStateStore:
    def __init__(self, path: str):
        self.path   = path
        self._state = self._load()

    def _load(self) -> dict:
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as exc:
                logger.warning(f"Failed to load ETF state ({exc}); starting fresh")
        return {"positions": {}, "orders": []}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self._state, f, indent=2)

    # ---------- positions ----------

    def get_position(self, uic) -> Optional[dict]:
        return self._state["positions"].get(str(uic))

    def all_positions(self) -> Dict[str, dict]:
        return self._state["positions"]

    def upsert_position(self, uic, data: dict) -> None:
        self._state["positions"][str(uic)] = data
        self._save()

    def remove_position(self, uic) -> None:
        self._state["positions"].pop(str(uic), None)
        self._save()

    def position_count(self) -> int:
        return len(self._state["positions"])

    # ---------- order log ----------

    def log_order(self, order_record: dict) -> None:
        import datetime
        order_record.setdefault("timestamp", datetime.datetime.utcnow().isoformat())
        self._state["orders"].append(order_record)
        self._save()
