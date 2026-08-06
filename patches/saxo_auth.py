"""
saxo_auth.py
------------
Handles the PKCE OAuth login flow against Saxo's SIM environment, and keeps
the resulting access/refresh tokens on disk so the bot can run unattended
without you re-logging-in every 20 minutes.

WHY THIS EXISTS
The 24-hour token from the developer portal (SAXO_TOKEN) is fine for a
quick manual test but is useless for a bot that needs to keep running.
This file gets you a proper access_token + refresh_token pair via PKCE,
and refresh_access_token() lets the bot silently renew its own access
token without any human involved — that's the actual unattended part.

HOW TO USE THIS (one-time, per login)
    python saxo_auth.py
This prints a login URL, opens it in your browser, you log in with your
Saxo SIM credentials and approve the app, then your browser is redirected
to a localhost URL that will fail to load (that's expected — nothing is
listening there). Copy the FULL URL from your browser's address bar at
that point and paste it back into the terminal when prompted.

After that, saxo_client.py automatically picks up and refreshes the saved
token — you should not need to run this file again unless the refresh
token itself expires or you disconnect the app from your Saxo account.

SECURITY
- The App Key is not a secret (that's the point of PKCE) — safe to keep
  in config/env vars, not sensitive if it leaks.
- The saved token file (saxo_token.json) DOES matter — it's effectively
  a working login session. Don't commit it, don't share it, don't paste
  its contents into chat. It's excluded via .gitignore-style convention;
  make sure your own .gitignore includes it if this becomes a git repo.
"""

import base64
import hashlib
import json
import os
import secrets
import time
import webbrowser
from urllib.parse import urlencode, urlparse, parse_qs

import requests

from atos.logger import get_logger
logger = get_logger("saxo_auth")

# ── Saxo SIM OAuth endpoints (fixed, from your Application details page) ──
AUTHORIZATION_ENDPOINT = "https://sim.logonvalidation.net/authorize"
TOKEN_ENDPOINT = "https://sim.logonvalidation.net/token"

# ── Your app's details ──────────────────────────────────────────────────
# App Key is not secret for a PKCE app — fine as a plain default, but env
# var override is supported if you'd rather not have it in the file.
APP_KEY = os.environ.get("SAXO_APP_KEY", "16ede6a528b745e1afcf054be485a2eb")
if not os.environ.get("SAXO_APP_KEY"):
    logger.info("Using built-in SAXO_APP_KEY (PKCE — not a secret). "
                "Set env var to override: $env:SAXO_APP_KEY = 'your-key'")
REDIRECT_URL = os.environ.get("SAXO_REDIRECT_URL", "https://localhost/redirect")

TOKEN_FILE = "saxo_token.json"


def _generate_pkce_pair() -> tuple[str, str]:
    """Returns (code_verifier, code_challenge) per RFC 7636."""
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(40)).decode("utf-8").rstrip("=")
    digest = hashlib.sha256(code_verifier.encode("utf-8")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")
    return code_verifier, code_challenge


def _save_tokens(token_data: dict) -> None:
    """Stamps the response with an absolute expiry time and writes it to disk."""
    token_data["obtained_at"] = time.time()
    with open(TOKEN_FILE, "w") as f:
        json.dump(token_data, f, indent=2)
    # Best-effort: restrict file permissions to the current user (no-op on Windows)
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except (AttributeError, OSError):
        pass
    logger.info("Tokens saved to %s", TOKEN_FILE)


def _load_tokens() -> dict | None:
    if not os.path.exists(TOKEN_FILE):
        return None
    with open(TOKEN_FILE) as f:
        return json.load(f)


def login_interactive() -> dict:
    """
    Runs the one-time interactive PKCE login flow. Prints a URL, opens the
    browser, waits for you to paste back the redirected URL, exchanges the
    authorization code for tokens, and saves them to disk.
    """
    logger.info("Starting interactive PKCE login flow")
    code_verifier, code_challenge = _generate_pkce_pair()
    state = secrets.token_urlsafe(16)

    params = {
        "response_type": "code",
        "client_id": APP_KEY,
        "redirect_uri": REDIRECT_URL,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    auth_url = f"{AUTHORIZATION_ENDPOINT}?{urlencode(params)}"

    print("Opening your browser to log in to Saxo SIM...")
    print(f"If it doesn't open automatically, paste this URL yourself:\n  {auth_url}\n")
    webbrowser.open(auth_url)

    print("After you log in and approve the app, your browser will try to load a")
    print("localhost page and FAIL — that's expected, nothing is running there.")
    redirected_url = input("Copy the full URL from your browser's address bar at that point and paste it here:\n> ").strip()

    parsed = urlparse(redirected_url)
    query = parse_qs(parsed.query)

    if "error" in query:
        raise RuntimeError(f"Saxo returned an error: {query.get('error_description', query['error'])}")
    if query.get("state", [None])[0] != state:
        raise RuntimeError("State mismatch — the pasted URL doesn't match this login attempt. Try again.")
    if "code" not in query:
        raise RuntimeError("No authorization code found in the pasted URL. Make sure you copied the full address bar.")

    auth_code = query["code"][0]

    token_data = _exchange_code_for_token(auth_code, code_verifier)
    _save_tokens(token_data)
    print("\nLogin successful. Tokens saved to saxo_token.json — you shouldn't need to run this again.")
    return token_data


def _exchange_code_for_token(auth_code: str, code_verifier: str) -> dict:
    resp = requests.post(TOKEN_ENDPOINT, data={
        "grant_type": "authorization_code",
        "code": auth_code,
        "redirect_uri": REDIRECT_URL,
        "client_id": APP_KEY,
        "code_verifier": code_verifier,
    })
    resp.raise_for_status()
    return resp.json()


def _refresh(refresh_token: str) -> dict:
    resp = requests.post(TOKEN_ENDPOINT, data={
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": APP_KEY,
    })
    resp.raise_for_status()
    return resp.json()


def get_valid_access_token() -> str:
    """
    The function saxo_client.py should call before every request (or cache
    per-process). Returns a valid access token, transparently refreshing it
    from disk if the current one is close to expiry. Raises a clear error
    telling you to run `python saxo_auth.py` if there's no saved login yet
    or the refresh token itself has expired.
    """
    tokens = _load_tokens()
    if tokens is None:
        raise RuntimeError(
            "No saved Saxo login found. Run `python saxo_auth.py` once to log in "
            "interactively — after that this refreshes itself automatically."
        )

    expires_at = tokens["obtained_at"] + tokens.get("expires_in", 1200)
    # Refresh a bit early (60s buffer) rather than cutting it exactly at expiry
    if time.time() < expires_at - 60:
        logger.debug("Token valid, expires in %.0fs", expires_at - time.time())
        return tokens["access_token"]

    try:
        refreshed = _refresh(tokens["refresh_token"])
        logger.info("Access token refreshed successfully")
    except requests.exceptions.HTTPError as e:
        logger.error("Token refresh failed", exc_info=True)
        raise RuntimeError(
            "Could not refresh the Saxo session (refresh token likely expired, or "
            "the app was disconnected in SaxoTraderGO). Run `python saxo_auth.py` "
            f"to log in again. Original error: {e}"
        )

    _save_tokens(refreshed)
    return refreshed["access_token"]


if __name__ == "__main__":
    login_interactive()
