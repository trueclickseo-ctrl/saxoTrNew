"""
saxo_auth.py
------------
Handles the PKCE OAuth login flow against Saxo's SIM (and, since
2026-08-25, LIVE) environment, and keeps the resulting access/refresh
tokens on disk so the bot can run unattended without you re-logging-in
every 20 minutes.

WHY THIS EXISTS
The 24-hour token from the developer portal (SAXO_TOKEN) is fine for a
quick manual test but is useless for a bot that needs to keep running.
This file gets you a proper access_token + refresh_token pair via PKCE,
and refresh_access_token() lets the bot silently renew its own access
token without any human involved — that's the actual unattended part.

HOW TO USE THIS (one-time, per login)
    python saxo_auth.py            # SIM login (default, unchanged)
    python saxo_auth.py --live     # LIVE login (real money account)
This prints a login URL, opens it in your browser, you log in with your
Saxo credentials for that environment and approve the app, then your
browser is redirected to a localhost URL that will fail to load (that's
expected — nothing is listening there). Copy the FULL URL from your
browser's address bar at that point and paste it back into the terminal
when prompted.

After that, saxo_client.py / forex/runner.py automatically pick up and
refresh the saved token for whichever `env` they ask for — you should not
need to run this file again unless the refresh token itself expires or you
disconnect the app from your Saxo account.

SIM vs LIVE (added 2026-08-25 for the real-money forex account)
Every function below takes an `env: str = "sim"` parameter. Omitting it
(or passing "sim") is byte-identical to this file's behavior before LIVE
support existed — every existing caller (saxo_client.py, price_service.py,
etc.) is unaffected. `env="live"` uses a SEPARATE app registration
(SAXO_LIVE_APP_KEY, never the SIM app key), Saxo's LIVE OAuth endpoints,
and a separate token file (saxo_token_live.json) — SIM and LIVE tokens are
never mixed. Saxo requires a genuinely separate app registration per
environment; there is no shared-key shortcut.
NOT YET VERIFIED against Saxo's current developer portal (per the standing
rule to verify against live API/docs, not guess): whether a LIVE app uses
the same public PKCE flow as SIM, or requires a confidential Authorization
Code Grant with a client secret. This file supports both — if Saxo's LIVE
app page shows a client secret, set SAXO_LIVE_APP_SECRET and it's used
automatically; if not, leave it unset and PKCE-only proceeds exactly like
SIM. Confirm which applies when the LIVE app is registered.

SECURITY
- The SIM App Key is not a secret (that's the point of PKCE) — safe to
  keep in config/env vars, not sensitive if it leaks. A LIVE app secret
  (if Saxo requires one), by contrast, IS a real secret — env var only,
  never hardcoded, never committed, never pasted into chat.
- The saved token files (saxo_token.json, saxo_token_live.json) DO matter
  — each is effectively a working login session. Don't commit them, don't
  share them, don't paste their contents into chat. Excluded via
  .gitignore-style convention; make sure your own .gitignore includes them.
"""

import argparse
import base64
import hashlib
import json
import os
import secrets
import time
import webbrowser
from urllib.parse import urlencode, urlparse, parse_qs

import requests

_HERE = os.path.dirname(os.path.abspath(__file__))

# ── Per-environment OAuth config ─────────────────────────────────────────
# "sim" values are exactly what this file always used -- unchanged.
# "live" endpoints follow Saxo's documented sim->live naming convention
# (gateway.saxobank.com/sim/openapi -> .../openapi, sim.logonvalidation.net
# -> live.logonvalidation.net) but have not been exercised against a real
# Saxo LIVE app yet -- verify on the LIVE app's own portal page before
# relying on this for the first real login (see module docstring).
# Endpoint URLs and token file paths are genuinely static -- fine at module
# level. app_key/app_secret are NOT: they were originally read here too
# (os.environ.get(...) evaluated once at import time), which meant a
# long-lived process could never notice SAXO_LIVE_APP_KEY being unset,
# changed, or set for the first time after that process started -- it kept
# using whatever value existed at import forever. Found 2026-08-26 via a
# test that unsets the env var and expects _cfg("live") to raise: it
# didn't, because the cached value from import time was still there. Moved
# into _cfg() below so every call reads the CURRENT environment, not a
# frozen snapshot -- matters most for exactly the kind of long-running
# process this session already found once today (an 8-hour-old stray
# python.exe from a much earlier, unrelated incident).
_ENV_CONFIG = {
    "sim": {
        "auth_endpoint": "https://sim.logonvalidation.net/authorize",
        "token_endpoint": "https://sim.logonvalidation.net/token",
        "token_file": os.path.join(_HERE, "saxo_token.json"),
    },
    "live": {
        "auth_endpoint": "https://live.logonvalidation.net/authorize",
        "token_endpoint": "https://live.logonvalidation.net/token",
        "token_file": os.path.join(_HERE, "saxo_token_live.json"),
    },
}


def _cfg(env: str) -> dict:
    # "live_eur" (added 2026-08-26, forex/runner.py's EUR sub-account
    # experiment) is the SAME Saxo LIVE login/app/OAuth token as "live" --
    # only the trading sub-account (AccountKey) differs, resolved entirely
    # in forex/runner.py's _account(), never here. Normalizing to "live"
    # means one shared token file/refresh chain for both, not a second
    # duplicate login -- there's only ever one real LIVE authentication.
    if env == "live_eur":
        env = "live"
    # "ai_sim" (2026-09-03, forex/runner.py's AI-decision SIM twin) is the
    # SAME Saxo SIM login/token as "sim" -- it just books to its own ledger
    # / state files and lets the Trading Copilot resize/skip. One shared SIM
    # token, no second login.
    if env == "ai_sim":
        env = "sim"
    if env not in _ENV_CONFIG:
        raise ValueError(f"Unknown Saxo env {env!r} -- expected 'sim' or 'live' "
                         "(or 'live_eur', normalized to 'live').")
    cfg = dict(_ENV_CONFIG[env])
    if env == "sim":
        cfg["app_key"]    = os.environ.get("SAXO_APP_KEY", "60d308f45fc34cc2913ae5f3692a94ba")
        cfg["app_secret"] = os.environ.get("SAXO_APP_SECRET")   # unset for SIM's PKCE app
    else:
        cfg["app_key"]    = os.environ.get("SAXO_LIVE_APP_KEY")
        cfg["app_secret"] = os.environ.get("SAXO_LIVE_APP_SECRET")
        if not cfg["app_key"]:
            raise RuntimeError(
                "SAXO_LIVE_APP_KEY is not set. Register a separate LIVE app on "
                "https://www.developer.saxo, then set SAXO_LIVE_APP_KEY (and "
                "SAXO_LIVE_APP_SECRET, only if that app page shows a client "
                "secret) before logging in to LIVE."
            )
    return cfg


REDIRECT_URL = os.environ.get("SAXO_REDIRECT_URL", "http://localhost/redirect")

# Back-compat module-level names — SIM only, unchanged from before `env`
# support existed, in case anything imports these constants directly.
AUTHORIZATION_ENDPOINT = _ENV_CONFIG["sim"]["auth_endpoint"]
TOKEN_ENDPOINT = _ENV_CONFIG["sim"]["token_endpoint"]
APP_KEY = os.environ.get("SAXO_APP_KEY", "60d308f45fc34cc2913ae5f3692a94ba")
TOKEN_FILE = _ENV_CONFIG["sim"]["token_file"]


def _generate_pkce_pair() -> tuple[str, str]:
    """Returns (code_verifier, code_challenge) per RFC 7636."""
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(40)).decode("utf-8").rstrip("=")
    digest = hashlib.sha256(code_verifier.encode("utf-8")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")
    return code_verifier, code_challenge


def _save_tokens(token_data: dict, env: str = "sim") -> None:
    """Stamps the response with an absolute expiry time and writes it to disk."""
    token_file = _cfg(env)["token_file"]
    token_data["obtained_at"] = time.time()
    with open(token_file, "w") as f:
        json.dump(token_data, f, indent=2)
    # Best-effort: restrict file permissions to the current user (no-op on Windows)
    try:
        os.chmod(token_file, 0o600)
    except (AttributeError, OSError):
        pass


def _load_tokens(env: str = "sim") -> dict | None:
    token_file = _cfg(env)["token_file"]
    if not os.path.exists(token_file):
        return None
    with open(token_file) as f:
        return json.load(f)


def login_interactive(env: str = "sim") -> dict:
    """
    Runs the one-time interactive PKCE login flow for the given environment
    ("sim" or "live"). Prints a URL, opens the browser, waits for you to
    paste back the redirected URL, exchanges the authorization code for
    tokens, and saves them to disk (a separate token file per env).
    """
    cfg = _cfg(env)
    code_verifier, code_challenge = _generate_pkce_pair()
    state = secrets.token_urlsafe(16)

    params = {
        "response_type": "code",
        "client_id": cfg["app_key"],
        "redirect_uri": REDIRECT_URL,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    auth_url = f"{cfg['auth_endpoint']}?{urlencode(params)}"

    print(f"Opening your browser to log in to Saxo {env.upper()}...")
    if env == "live":
        print("*** THIS IS YOUR REAL, REAL-MONEY SAXO ACCOUNT. ***")
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

    token_data = _exchange_code_for_token(auth_code, code_verifier, env=env)
    _save_tokens(token_data, env=env)
    print(f"\nLogin successful. Tokens saved to {cfg['token_file']} — you shouldn't need to run this again.")
    return token_data


def _exchange_code_for_token(auth_code: str, code_verifier: str, env: str = "sim") -> dict:
    cfg = _cfg(env)
    body = {
        "grant_type": "authorization_code",
        "code": auth_code,
        "redirect_uri": REDIRECT_URL,
        "client_id": cfg["app_key"],
        "code_verifier": code_verifier,
    }
    if cfg["app_secret"]:
        body["client_secret"] = cfg["app_secret"]
    resp = requests.post(cfg["token_endpoint"], data=body)
    resp.raise_for_status()
    return resp.json()


def _refresh(refresh_token: str, env: str = "sim") -> dict:
    cfg = _cfg(env)
    body = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": cfg["app_key"],
    }
    if cfg["app_secret"]:
        body["client_secret"] = cfg["app_secret"]
    resp = requests.post(cfg["token_endpoint"], data=body)
    resp.raise_for_status()
    return resp.json()


def get_valid_access_token(env: str = "sim") -> str:
    """
    The function saxo_client.py / forex/runner.py should call before every
    request (or cache per-process). Returns a valid access token for the
    given environment ("sim" default, "live" for the real-money account),
    transparently refreshing it from disk if the current one is close to
    expiry. Raises a clear error telling you to run
    `python saxo_auth.py` / `python saxo_auth.py --live` if there's no
    saved login yet for that environment or its refresh token has expired.
    """
    tokens = _load_tokens(env=env)
    if tokens is None:
        login_hint = "python saxo_auth.py" if env == "sim" else "python saxo_auth.py --live"
        raise RuntimeError(
            f"No saved Saxo {env.upper()} login found. Run `{login_hint}` once to "
            "log in interactively — after that this refreshes itself automatically."
        )

    # Structural sanity check: a real Saxo access token is a ~500-char JWT.
    # A bad Ctrl+V paste into set_token.py once saved the 1-char string
    # "\x16" and this function returned it happily for hours (2026-09-01).
    _at = tokens.get("access_token") or ""
    _hint = "python set_token.py" if env == "sim" else "python saxo_auth.py --live"
    if len(_at) < 100 or any(ord(c) < 32 or c == " " for c in _at):
        raise RuntimeError(
            f"Saved Saxo {env.upper()} access_token is malformed (len {len(_at)}) — "
            f"likely a bad paste. Re-set it with `{_hint}`."
        )

    expires_at = tokens["obtained_at"] + tokens.get("expires_in", 1200)
    # 300s buffer: with a 15-min keepalive and 20-min TTL, 60s was too thin —
    # the keepalive would see 6 min remaining (>60s) and skip the refresh, then
    # the token expired before the next tick (T+15 → expires T+20 → next keepalive T+30).
    # 300s ensures the keepalive at T+15 always refreshes (5 min left ≤ 5 min buffer).
    if time.time() < expires_at - 300:
        return tokens["access_token"]

    try:
        refreshed = _refresh(tokens["refresh_token"], env=env)
    except requests.exceptions.HTTPError as e:
        login_hint = "python saxo_auth.py" if env == "sim" else "python saxo_auth.py --live"
        raise RuntimeError(
            f"Could not refresh the Saxo {env.upper()} session (refresh token likely "
            "expired, or the app was disconnected in SaxoTraderGO). Run "
            f"`{login_hint}` to log in again. Original error: {e}"
        )

    _save_tokens(refreshed, env=env)
    return refreshed["access_token"]


if __name__ == "__main__":
    _parser = argparse.ArgumentParser()
    _parser.add_argument("--live", action="store_true",
                          help="Log in to the real-money LIVE account instead of SIM.")
    _args = _parser.parse_args()
    login_interactive(env="live" if _args.live else "sim")
