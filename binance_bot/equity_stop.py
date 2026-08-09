"""
binance_bot/equity_stop.py
--------------------------
Portfolio-level equity drawdown stop for the Binance bot.

Tracks running equity vs. its all-time peak within the bot's lifetime and
halts new entries — and optionally closes all positions — when equity falls
more than `drawdown_threshold` from peak.

Checked every scan in run_binance_bot.py (both mean-reversion and any future
strategy code path).  State is persisted to a JSON file so the peak survives
bot restarts.  Recovery requires a manual operator action (call reset() or
delete the state file) to prevent automatic resumption after a large loss.

State file format:
    {
        "peak_equity":  float,      # highest equity seen since last reset
        "halted":       bool,       # True = new entries blocked
        "halt_equity":  float|null, # equity at the moment halt triggered
        "halt_ts":      str|null,   # ISO-8601 timestamp of halt
        "updated_ts":   str         # timestamp of last state write
    }
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone


class EquityStop:
    """
    Usage in bot scan loop:

        stop = EquityStop(state_path, drawdown_threshold=0.15)
        halted, reason = stop.check(current_equity_usdt)
        if halted:
            log.warning("EQUITY STOP: %s", reason)
            return   # skip new entries this scan

    To recover after a halt (operator action only):

        stop.reset()
    """

    def __init__(self, state_path: str, drawdown_threshold: float) -> None:
        """
        state_path          -- path to the persistent JSON state file
        drawdown_threshold  -- fractional drawdown that triggers halt, e.g. 0.15 = 15%
        """
        self._path      = state_path
        self._threshold = drawdown_threshold
        self._state     = self._load()

    # ── Public API ────────────────────────────────────────────────────────────

    def check(self, current_equity: float) -> tuple[bool, str]:
        """
        Update peak equity and evaluate halt condition.

        Returns (halted, reason_string).
        - halted=True  → skip new entries; caller may also flatten positions.
        - halted=False → proceed normally.

        Always call this even when already halted so that the state file stays
        current (dashboard can display current drawdown depth).
        """
        state = self._state

        # Update peak — only if not already halted.  Once halted we freeze the
        # peak so the drawdown reading stays stable and meaningful.
        if not state["halted"]:
            if current_equity > state["peak_equity"]:
                state["peak_equity"] = current_equity

        peak = state["peak_equity"]
        if peak <= 0:
            return False, ""

        drawdown = (peak - current_equity) / peak

        if not state["halted"] and drawdown >= self._threshold:
            state["halted"]      = True
            state["halt_equity"] = current_equity
            state["halt_ts"]     = datetime.now(timezone.utc).isoformat()

        self._save()

        if state["halted"]:
            reason = (
                f"drawdown {drawdown*100:.1f}% from peak "
                f"(${peak:,.0f} -> ${current_equity:,.0f}); "
                f"threshold={self._threshold*100:.0f}%. "
                f"Manual reset required to resume."
            )
            return True, reason

        return False, ""

    def reset(self) -> None:
        """
        Clear the halt and reset peak to current state.
        Operator action only — do not call automatically.
        """
        self._state = {
            "peak_equity":  0.0,
            "halted":       False,
            "halt_equity":  None,
            "halt_ts":      None,
            "updated_ts":   datetime.now(timezone.utc).isoformat(),
        }
        self._save()

    @property
    def halted(self) -> bool:
        return self._state["halted"]

    @property
    def peak_equity(self) -> float:
        return self._state["peak_equity"]

    # ── Internal ──────────────────────────────────────────────────────────────

    def _load(self) -> dict:
        if os.path.exists(self._path):
            try:
                with open(self._path, encoding="utf-8") as f:
                    data = json.load(f)
                # Validate expected keys; fall through to defaults if corrupt.
                if isinstance(data.get("peak_equity"), (int, float)):
                    return data
            except Exception:
                pass
        return {
            "peak_equity":  0.0,
            "halted":       False,
            "halt_equity":  None,
            "halt_ts":      None,
            "updated_ts":   datetime.now(timezone.utc).isoformat(),
        }

    def _save(self) -> None:
        self._state["updated_ts"] = datetime.now(timezone.utc).isoformat()
        tmp = self._path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._state, f, indent=2)
            os.replace(tmp, self._path)
        except Exception as exc:
            # Never let state-file I/O crash the bot.
            print(f"[equity_stop] state write failed: {exc}")
