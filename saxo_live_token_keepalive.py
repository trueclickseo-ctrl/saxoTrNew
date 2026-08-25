"""
saxo_live_token_keepalive.py
------------------------------
Keeps the real-money Saxo LIVE OAuth session alive between trading runs.

Root problem (found 2026-08-25): LIVE's app issues an access_token good for
only 20 min (expires_in=1200) and a refresh_token good for only 1 hour
(refresh_token_expires_in=3600) -- both far shorter than SIM's (SIM's
access_token lasts a full 24h, no such problem there). LIVE's own trading
schedule fires every ~2 hours (06:00, 08:00, 10:00, 12:30, ...). By the time
the next scheduled run starts, the refresh_token itself has already been
dead for roughly an hour -- there is nothing left to refresh FROM, so
saxo_auth.get_valid_access_token(env="live") can only fail with a clear
"log in again" error, and every run in between gets skipped with a
TOKEN EXPIRED alert email instead of actually scanning.

This script does the one thing that fixes that: calls
saxo_auth.get_valid_access_token(env="live") on its own more-frequent
schedule (recommended: every 15 min, comfortably inside the 20-min access-
token window) so the refresh chain never goes fully cold between real
trading runs. It does NOT log in for the first time -- that one-time
interactive PKCE login (python saxo_auth.py --live) is still something
only you can do, since it requires your own browser + Saxo credentials.
Once that's done, this keeps it alive indefinitely on its own.

Sends exactly one alert email (via forex/notifier's underlying _send(),
reused directly rather than duplicated) only when a keepalive call fails
-- silent on every successful tick, so it doesn't add email noise on top
of the real trading-run summaries.

Usage:
    python saxo_live_token_keepalive.py
"""

from __future__ import annotations

import logging
import sys

import saxo_auth

logger = logging.getLogger("saxo_live_token_keepalive")


def run_once() -> bool:
    try:
        saxo_auth.get_valid_access_token(env="live")
        logger.info("[keepalive] LIVE token OK (refreshed if it was close to expiry)")
        return True
    except Exception as exc:
        logger.error(f"[keepalive] LIVE token refresh FAILED: {exc}")
        _send_alert(str(exc))
        return False


def _send_alert(detail: str) -> None:
    try:
        from forex.notifier import _send
        from datetime import datetime
        now = datetime.now().strftime("%d %b %Y  %H:%M PKT")
        html = f"""<!DOCTYPE html><html><body style="font-family:sans-serif">
        <h2 style="color:#c0392b">LIVE token keepalive FAILED</h2>
        <p style="color:#666">{now}</p>
        <p>The refresh-token chain has broken (likely the refresh_token itself
        expired, or the app was disconnected in SaxoTraderGO). The next
        scheduled LIVE trading run will be skipped until you log in again:</p>
        <pre>python saxo_auth.py --live</pre>
        <p style="color:#666;font-size:12px">Detail: {detail}</p>
        </body></html>"""
        _send("[LIVE] Token keepalive FAILED — re-login required", html)
    except Exception:
        logger.exception("[keepalive] also failed to send the failure alert email")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
    ok = run_once()
    sys.exit(0 if ok else 1)
