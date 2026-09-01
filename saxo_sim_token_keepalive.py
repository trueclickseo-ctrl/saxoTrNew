"""
saxo_sim_token_keepalive.py
----------------------------
Keeps the Saxo SIM OAuth session alive across the overnight scan gap.

Root problem (found 2026-08-30): a proper PKCE login (`python saxo_auth.py`)
issues a SIM access_token good for only ~20 min (expires_in ~1180) and a
refresh_token good for only ~60 min (refresh_token_expires_in ~3580) --
identical to the LIVE token profile, NOT the 24h that `set_token.py`'s
hardcoded `expires_in: 86400` assumed (that value is for a Saxo Developer
Portal 24-hour token, a different artefact).

The SIM scan (`ATOS Forex Intraday Scan`) runs every 30 min but only for a
20h55m window (06:05 -> ~03:00 PKT), then a ~3-hour overnight gap. Across
that gap the 60-min refresh_token dies with nothing left to refresh FROM,
so the first scan of the next day fails with a TOKEN EXPIRED alert and
someone has to run `python saxo_auth.py` again.

This script does the one thing that fixes that: calls
`saxo_auth.get_valid_access_token(env="sim")` on its own more-frequent
schedule (recommended: every 15 min, comfortably inside the 20-min access-
token window) so the refresh chain never goes fully cold. It does NOT log
in for the first time -- that one-time interactive PKCE login
(`python saxo_auth.py`) is still yours to do once; after that this keeps
it alive indefinitely.

Exactly one alert email on failure (via forex/notifier's `_send`), silent
on every successful tick. Mirrors `saxo_live_token_keepalive.py` -- kept a
SEPARATE script/task so a SIM failure never touches LIVE and vice versa.

Usage:
    python saxo_sim_token_keepalive.py
"""

from __future__ import annotations

import logging
import sys

import saxo_auth

logger = logging.getLogger("saxo_sim_token_keepalive")


def run_once() -> bool:
    try:
        saxo_auth.get_valid_access_token(env="sim")
        # get_valid_access_token only does time math on the file's own
        # expires_in -- it happily returns a structurally-broken token (e.g.
        # a bad Ctrl+V paste into set_token.py that saved "\x16") or one
        # Saxo has since revoked. Confirmed 2026-09-01: it reported "OK" 24
        # times against a 1-char token while every real scan was failing. A
        # live /port/v1/users/me call is the only check that means anything.
        import saxo_client
        me = saxo_client.test_connection(env="sim")
        logger.info(f"[keepalive] SIM token OK — {me.get('Name', '?')} "
                    f"(UserId {me.get('UserId', '?')})")
        return True
    except Exception as exc:
        logger.error(f"[keepalive] SIM token check FAILED: {exc}")
        _send_alert(str(exc))
        return False


def _send_alert(detail: str) -> None:
    try:
        from forex.notifier import _send
        from datetime import datetime
        now = datetime.now().strftime("%d %b %Y  %H:%M PKT")
        html = f"""<!DOCTYPE html><html><body style="font-family:sans-serif">
        <h2 style="color:#c0392b">SIM token keepalive FAILED</h2>
        <p style="color:#666">{now}</p>
        <p>The SIM refresh-token chain has broken (likely the refresh_token
        itself expired during an overnight gap, or the app was disconnected).
        The next scheduled SIM scan will be skipped until you log in again:</p>
        <pre>python saxo_auth.py</pre>
        <p style="color:#666;font-size:12px">Detail: {detail}</p>
        </body></html>"""
        _send("[SIM] Token keepalive FAILED — re-login required", html)
    except Exception:
        logger.exception("[keepalive] also failed to send the failure alert email")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
    ok = run_once()
    sys.exit(0 if ok else 1)
