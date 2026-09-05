"""
ai/portfolio/exposure_controller.py — Portfolio-level exposure controller.

Sits between the deterministic signal filters and the Trading Copilot.
Tracks the live book's currency-cluster positions, total open risk vs equity,
and same-pair concentration, then gates each new entry BEFORE an LLM call
is made (no point paying for a Copilot call on a trade the book can't absorb).

Integration in forex/runner.py (_run_entries):
  1. controller = ExposureController.from_positions(positions, equity)
     — call once at the top of the entry loop, not per signal
  2. Per signal, BEFORE the AI advisory block:
       ok, reason, size_mod = controller.check_entry(sym, direction, risk_eur)
       if not ok: log + continue
  3. Feed controller.snapshot() into the Copilot proposal context so the
     agent has accurate book-level data when making concentration calls.

Config (config/ai.json  "exposure_controller" block):
  enabled                       — master switch (default True)
  max_currency_cluster_positions — max open positions with net exposure in any
                                   one currency direction (default 8 for SIM;
                                   tighten after 1-week validation)
  max_open_risk_pct_equity      — total open risk as % of account equity that
                                   blocks new entries (default 30.0 — permissive
                                   for first deployment; tighten to 15-20 later)
  max_same_pair_positions       — max open positions for the same symbol across
                                   all strategies (default 2)

All defaults are intentionally permissive so the first deployment observes and
logs without blocking real P&L. Tighten after the 1-week gate passes.

Governance:
  * This module is READ-ONLY w.r.t. orders/positions — it never places, amends,
    or cancels anything. It only gates entry proposals.
  * Works on SIM and ai_sim. LIVE hard-blocks never originate here
    (LIVE already has its own margin gate and heat cap).
  * Every block is logged to data/exposure_controller.jsonl for audit.
"""

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CONFIG_PATH = os.path.join(_BASE, "config", "ai.json")
_LOG_PATH = os.path.join(_BASE, "data", "exposure_controller.jsonl")

_DEFAULTS: dict = {
    "enabled": True,
    "max_currency_cluster_positions": 8,
    "max_open_risk_pct_equity": 30.0,
    "max_same_pair_positions": 2,
}


def _load_config() -> dict:
    try:
        if os.path.exists(_CONFIG_PATH):
            with open(_CONFIG_PATH, encoding="utf-8") as f:
                data = json.load(f)
            block = data.get("exposure_controller")
            if isinstance(block, dict):
                cfg = dict(_DEFAULTS)
                cfg.update({k: block[k] for k in _DEFAULTS if k in block})
                return cfg
    except Exception:
        pass
    return dict(_DEFAULTS)


def _sym_currencies(sym: str, direction: str) -> tuple[str, str, int, int]:
    """Return (base, quote, base_sign, quote_sign) for a 6-char symbol + direction.
    Buy EURUSD → long EUR (+1), short USD (-1).
    """
    if len(sym) < 6:
        return "", "", 0, 0
    base, quote = sym[:3].upper(), sym[3:6].upper()
    sign = 1 if direction.lower() in ("buy", "long") else -1
    return base, quote, sign, -sign


class ExposureController:
    """Stateless-per-cycle book snapshot + entry gating.

    Constructed once per runner cycle from the current positions dict.
    ``check_entry`` is called per signal and is cheap (pure Python dicts).
    """

    def __init__(
        self,
        positions: dict,
        equity_eur: float,
        account_env: str = "sim",
    ) -> None:
        self._account = account_env
        self._equity = equity_eur
        self._cfg = _load_config()

        # ── Build book snapshot ───────────────────────────────────────────
        # cluster_counts[currency][sign] = # of open positions net-long (+1)
        # or net-short (-1) that currency.
        cluster_counts: dict[str, dict[int, int]] = defaultdict(lambda: {1: 0, -1: 0})
        # pair_counts[sym] = # open positions for that symbol
        pair_counts: dict[str, int] = defaultdict(int)
        total_risk = 0.0

        for key, pos in positions.items():
            sym = key.split(":", 1)[1] if ":" in key else key
            if len(sym) < 6:
                continue
            direction = pos.get("direction", "Buy")
            base, quote, base_sign, quote_sign = _sym_currencies(sym, direction)
            if base:
                cluster_counts[base][base_sign] = cluster_counts[base].get(base_sign, 0) + 1
                cluster_counts[quote][quote_sign] = cluster_counts[quote].get(quote_sign, 0) + 1
            pair_counts[sym[:6].upper()] += 1
            total_risk += float(pos.get("risk_eur_at_entry") or 0)

        self._cluster_counts = cluster_counts
        self._pair_counts = pair_counts
        self._total_risk_eur = total_risk
        self._n_positions = len(positions)

    @classmethod
    def from_positions(
        cls,
        positions: dict,
        equity_eur: float,
        account_env: str = "sim",
    ) -> "ExposureController":
        return cls(positions, equity_eur, account_env)

    # ── Public API ────────────────────────────────────────────────────────

    def enabled(self) -> bool:
        return bool(self._cfg.get("enabled", True))

    def check_entry(
        self,
        sym: str,
        direction: str,
        risk_eur: float = 0.0,
    ) -> tuple[bool, str, float]:
        """Gate a proposed entry.

        Returns:
            (ok, reason, size_modifier)
            ok=True  → trade is within limits; size_modifier=1.0
            ok=False → trade is blocked; reason explains why
            size_modifier is always 1.0 for now (future: soft trim)
        """
        if not self.enabled():
            return True, "controller_disabled", 1.0

        sym6 = sym[:6].upper() if len(sym) >= 6 else sym.upper()
        base, quote, base_sign, quote_sign = _sym_currencies(sym6, direction)

        # 1 — same-pair cap
        max_pair = int(self._cfg.get("max_same_pair_positions", 2))
        current_pair = self._pair_counts.get(sym6, 0)
        if current_pair >= max_pair:
            reason = (
                f"pair_cap: {sym6} already has {current_pair} open "
                f"position(s) (max {max_pair})"
            )
            self._log_block(sym6, direction, reason, risk_eur)
            return False, reason, 1.0

        # 2 — currency cluster cap
        max_cluster = int(self._cfg.get("max_currency_cluster_positions", 8))
        if base:
            for ccy, sign, label in [
                (base,  base_sign,  f"{base}_{'long' if base_sign > 0 else 'short'}"),
                (quote, quote_sign, f"{quote}_{'long' if quote_sign > 0 else 'short'}"),
            ]:
                current = self._cluster_counts[ccy].get(sign, 0)
                if current >= max_cluster:
                    reason = (
                        f"cluster_cap: {label} already at {current} "
                        f"positions (max {max_cluster})"
                    )
                    self._log_block(sym6, direction, reason, risk_eur)
                    return False, reason, 1.0

        # 3 — total open risk cap
        max_risk_pct = float(self._cfg.get("max_open_risk_pct_equity", 30.0))
        if self._equity and self._equity > 0 and risk_eur:
            new_total_risk = self._total_risk_eur + risk_eur
            new_risk_pct = 100.0 * new_total_risk / self._equity
            if new_risk_pct > max_risk_pct:
                reason = (
                    f"risk_cap: adding {risk_eur:.0f} EUR risk would bring "
                    f"total open risk to {new_risk_pct:.1f}% of equity "
                    f"(max {max_risk_pct:.0f}%)"
                )
                self._log_block(sym6, direction, reason, risk_eur)
                return False, reason, 1.0

        return True, "ok", 1.0

    def snapshot(self) -> dict:
        """Return a compact book summary for the Copilot prompt context."""
        top_clusters = sorted(
            [
                (f"{ccy}_long",  counts.get(1, 0))
                for ccy, counts in self._cluster_counts.items()
            ] + [
                (f"{ccy}_short", counts.get(-1, 0))
                for ccy, counts in self._cluster_counts.items()
            ],
            key=lambda x: -x[1],
        )[:10]

        return {
            "n_open_positions": self._n_positions,
            "total_open_risk_eur": round(self._total_risk_eur, 2),
            "equity_eur": round(self._equity, 2) if self._equity else None,
            "open_risk_pct_equity": (
                round(100.0 * self._total_risk_eur / self._equity, 1)
                if self._equity and self._equity > 0 else None
            ),
            "top_currency_clusters": top_clusters,
            "limits": {
                "max_cluster": self._cfg.get("max_currency_cluster_positions", 8),
                "max_pair":    self._cfg.get("max_same_pair_positions", 2),
                "max_risk_pct": self._cfg.get("max_open_risk_pct_equity", 30.0),
            },
        }

    def cluster_counts_for(self, ccy: str) -> dict:
        """Return {1: long_count, -1: short_count} for a currency."""
        return dict(self._cluster_counts.get(ccy.upper(), {}))

    # ── Internal ──────────────────────────────────────────────────────────

    def _log_block(self, sym: str, direction: str, reason: str, risk_eur: float) -> None:
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "account": self._account,
            "sym": sym,
            "direction": direction,
            "risk_eur": round(risk_eur, 2),
            "reason": reason,
            "book_n": self._n_positions,
            "book_risk_eur": round(self._total_risk_eur, 2),
        }
        try:
            with open(_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
        except Exception:
            pass
        logger.info(f"  [exposure_ctrl] BLOCK {sym}[{direction}]: {reason}")
