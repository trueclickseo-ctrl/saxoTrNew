"""
saxo_auth_auto.py
------------------
Automatic Saxo OAuth PKCE login — NO manual URL copy-paste needed.

Replaces the manual flow in saxo_auth.py with a fully automated one:
  1. Starts a temporary local HTTP server on port 8071
  2. Opens your browser to Saxo SIM login page
  3. You log in and approve the app (same as before)
  4. Saxo redirects to localhost:8071 — the local server catches it
  5. Authorization code is extracted and exchanged for tokens automatically
  6. Tokens saved to saxo_token.json — ready for the bot
  7. Browser shows "Login successful!" page, server shuts down

Usage:
    py -3 saxo_auth_auto.py

After running once, saxo_client.py auto-refreshes tokens — you should
not need to run this again unless the refresh token expires or you
disconnect the app from your Saxo account.

IMPORTANT — Redirect URI setup:
  Your Saxo SIM app must have the redirect URI registered that matches
  what this script uses. Go to https://developer.saxobank.com/ → your
  app → Redirect URLs, and add:
      http://localhost:8071/redirect
  You can keep the existing https://localhost/redirect as well.

For other agents:
  This file creates the same saxo_token.json as saxo_auth.py.
  saxo_client.py reads from that file — no changes needed there.
  This file can be used INSTEAD of saxo_auth.py (not both).
"""

import base64
import hashlib
import json
import os
import secrets
import sys
import time
import threading
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlencode, urlparse, parse_qs

import requests

# ── Saxo SIM OAuth endpoints ─────────────────────────────────────
AUTHORIZATION_ENDPOINT = "https://sim.logonvalidation.net/authorize"
TOKEN_ENDPOINT         = "https://sim.logonvalidation.net/token"

# ── App details ───────────────────────────────────────────────────
APP_KEY = os.environ.get("SAXO_APP_KEY", "60d308f45fc34cc2913ae5f3692a94ba")

# ── Callback server config ────────────────────────────────────────
CALLBACK_PORT = 8071          # port for the temporary callback server
CALLBACK_PATH = "/redirect"   # path Saxo redirects to after login
REDIRECT_URL  = f"http://localhost:{CALLBACK_PORT}{CALLBACK_PATH}"

# ── Token file (same as saxo_auth.py — shared with saxo_client.py)
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(BASE_DIR, "saxo_token.json")


# ═══════════════════════════════════════════════════════════════════
# PKCE helpers
# ═══════════════════════════════════════════════════════════════════

def _generate_pkce_pair() -> tuple[str, str]:
    """Returns (code_verifier, code_challenge) per RFC 7636."""
    code_verifier = base64.urlsafe_b64encode(
        secrets.token_bytes(40)
    ).decode("utf-8").rstrip("=")
    digest = hashlib.sha256(code_verifier.encode("utf-8")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")
    return code_verifier, code_challenge


def _save_tokens(token_data: dict) -> None:
    """Stamps the response with an absolute expiry time and writes to disk."""
    token_data["obtained_at"] = time.time()
    with open(TOKEN_FILE, "w") as f:
        json.dump(token_data, f, indent=2)
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except (AttributeError, OSError):
        pass


def _exchange_code_for_token(auth_code: str, code_verifier: str) -> dict:
    """Exchange authorization code for access + refresh tokens."""
    resp = requests.post(TOKEN_ENDPOINT, data={
        "grant_type":    "authorization_code",
        "code":          auth_code,
        "redirect_uri":  REDIRECT_URL,
        "client_id":     APP_KEY,
        "code_verifier": code_verifier,
    })
    resp.raise_for_status()
    return resp.json()


# ═══════════════════════════════════════════════════════════════════
# Success page HTML (shown in browser after successful login)
# ═══════════════════════════════════════════════════════════════════

SUCCESS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>ATOS — Saxo Login Successful</title>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: 'Inter', sans-serif;
      background: #0f1117;
      color: #e2e8f0;
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
    }
    .card {
      background: rgba(26, 29, 46, 0.8);
      backdrop-filter: blur(20px);
      border: 1px solid rgba(255,255,255,0.1);
      border-radius: 20px;
      padding: 48px;
      text-align: center;
      max-width: 480px;
      animation: fadeIn 0.6s ease-out;
    }
    @keyframes fadeIn { from { opacity: 0; transform: translateY(20px); } to { opacity: 1; transform: translateY(0); } }
    .check {
      width: 72px; height: 72px;
      background: linear-gradient(135deg, #10b981, #059669);
      border-radius: 50%;
      display: flex; align-items: center; justify-content: center;
      margin: 0 auto 24px;
      box-shadow: 0 0 30px rgba(16, 185, 129, 0.3);
      animation: scaleIn 0.5s ease-out 0.2s both;
    }
    @keyframes scaleIn { from { transform: scale(0); } to { transform: scale(1); } }
    .check svg { width: 36px; height: 36px; color: white; }
    h1 {
      font-size: 24px; font-weight: 700; margin-bottom: 12px;
      background: linear-gradient(135deg, #8b5cf6, #3b82f6);
      -webkit-background-clip: text; -webkit-text-fill-color: transparent;
    }
    p { color: #94a3b8; line-height: 1.6; margin-bottom: 8px; }
    .token-info {
      background: rgba(255,255,255,0.05);
      border-radius: 12px;
      padding: 16px;
      margin-top: 20px;
      font-size: 13px;
      text-align: left;
    }
    .token-info .row { display: flex; justify-content: space-between; padding: 4px 0; }
    .token-info .label { color: #64748b; }
    .token-info .value { color: #10b981; font-weight: 600; }
    .close-note { color: #475569; font-size: 12px; margin-top: 20px; }
  </style>
</head>
<body>
  <div class="card">
    <div class="check">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round">
        <polyline points="20 6 9 17 4 12"></polyline>
      </svg>
    </div>
    <h1>ATOS — Login Successful</h1>
    <p>Your Saxo SIM session is now active.</p>
    <p>Tokens have been saved and will auto-refresh.</p>
    <div class="token-info">
      <div class="row"><span class="label">Token file</span><span class="value">saxo_token.json</span></div>
      <div class="row"><span class="label">Access token</span><span class="value">Saved</span></div>
      <div class="row"><span class="label">Refresh token</span><span class="value">Saved</span></div>
      <div class="row"><span class="label">Auto-refresh</span><span class="value">Enabled</span></div>
    </div>
    <p class="close-note">You can close this tab. The bot will refresh tokens automatically.</p>
  </div>
</body>
</html>"""

ERROR_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>ATOS — Login Failed</title>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: 'Inter', sans-serif; background: #0f1117; color: #e2e8f0;
      display: flex; align-items: center; justify-content: center; min-height: 100vh;
    }
    .card {
      background: rgba(26, 29, 46, 0.8); backdrop-filter: blur(20px);
      border: 1px solid rgba(255,255,255,0.1); border-radius: 20px;
      padding: 48px; text-align: center; max-width: 480px;
    }
    .icon { font-size: 48px; margin-bottom: 16px; }
    h1 { font-size: 22px; color: #ef4444; margin-bottom: 12px; }
    p { color: #94a3b8; line-height: 1.6; }
    .error-detail {
      background: rgba(239,68,68,0.1); border: 1px solid rgba(239,68,68,0.3);
      border-radius: 8px; padding: 12px; margin-top: 16px;
      font-size: 13px; color: #fca5a5; word-break: break-all;
    }
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">⚠️</div>
    <h1>Login Failed</h1>
    <p>Something went wrong during the Saxo authentication.</p>
    <div class="error-detail">ERROR_MESSAGE</div>
    <p style="margin-top:16px; font-size:13px; color:#475569;">
      Try running <code>py -3 saxo_auth_auto.py</code> again.
    </p>
  </div>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════════
# Callback HTTP Server
# ═══════════════════════════════════════════════════════════════════

class _AuthResult:
    """Thread-safe container for the OAuth callback result."""
    def __init__(self):
        self.code = None
        self.error = None
        self.received = threading.Event()


class CallbackHandler(BaseHTTPRequestHandler):
    """Handles the Saxo OAuth redirect callback."""
    auth_result: _AuthResult = None
    expected_state: str = None
    code_verifier: str = None

    def do_GET(self):
        parsed = urlparse(self.path)

        # Only handle the callback path
        if parsed.path != CALLBACK_PATH:
            self.send_response(404)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h1>404 - Not the callback path</h1>")
            return

        query = parse_qs(parsed.query)

        # Check for errors from Saxo
        if "error" in query:
            error_msg = query.get("error_description", query["error"])[0]
            self.auth_result.error = error_msg
            self._send_error_page(error_msg)
            self.auth_result.received.set()
            return

        # Validate state parameter (CSRF protection)
        returned_state = query.get("state", [None])[0]
        if returned_state != self.expected_state:
            error_msg = "State mismatch — possible CSRF attack. Try again."
            self.auth_result.error = error_msg
            self._send_error_page(error_msg)
            self.auth_result.received.set()
            return

        # Extract authorization code
        if "code" not in query:
            error_msg = "No authorization code in callback URL."
            self.auth_result.error = error_msg
            self._send_error_page(error_msg)
            self.auth_result.received.set()
            return

        auth_code = query["code"][0]

        # Exchange code for tokens
        try:
            token_data = _exchange_code_for_token(auth_code, self.code_verifier)
            _save_tokens(token_data)
            self.auth_result.code = auth_code
            self._send_success_page()
        except Exception as e:
            self.auth_result.error = str(e)
            self._send_error_page(str(e))

        self.auth_result.received.set()

    def _send_success_page(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(SUCCESS_HTML.encode("utf-8"))

    def _send_error_page(self, error_msg: str):
        self.send_response(400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        html = ERROR_HTML_TEMPLATE.replace("ERROR_MESSAGE", error_msg)
        self.wfile.write(html.encode("utf-8"))

    def log_message(self, format, *args):
        """Suppress default request logging."""
        pass


# ═══════════════════════════════════════════════════════════════════
# Main Login Flow
# ═══════════════════════════════════════════════════════════════════

def login_auto():
    """
    Fully automatic PKCE login:
    1. Start local callback server
    2. Open browser to Saxo login
    3. Catch the redirect automatically
    4. Exchange code for tokens
    5. Save to saxo_token.json
    """
    print()
    print("=" * 60)
    print("  ATOS — Saxo SIM Automatic Login (PKCE)")
    print("=" * 60)

    # Generate PKCE pair
    code_verifier, code_challenge = _generate_pkce_pair()
    state = secrets.token_urlsafe(16)

    # Set up the callback handler
    auth_result = _AuthResult()
    CallbackHandler.auth_result = auth_result
    CallbackHandler.expected_state = state
    CallbackHandler.code_verifier = code_verifier

    # Start temporary callback server
    try:
        server = HTTPServer(("", CALLBACK_PORT), CallbackHandler)
    except OSError as e:
        print(f"\n  ERROR: Port {CALLBACK_PORT} is already in use.")
        print(f"  Kill the process using it, or change CALLBACK_PORT in this file.")
        print(f"  Detail: {e}")
        sys.exit(1)

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    print(f"\n  Callback server running on http://localhost:{CALLBACK_PORT}")

    # Build authorization URL
    params = {
        "response_type":        "code",
        "client_id":            APP_KEY,
        "redirect_uri":         REDIRECT_URL,
        "state":                state,
        "code_challenge":       code_challenge,
        "code_challenge_method": "S256",
    }
    auth_url = f"{AUTHORIZATION_ENDPOINT}?{urlencode(params)}"

    # Open browser
    print("  Opening browser for Saxo SIM login...")
    print(f"\n  If browser doesn't open, paste this URL manually:")
    print(f"  {auth_url}\n")
    webbrowser.open(auth_url)

    print("  Waiting for you to log in and approve the app...")
    print("  (The browser will show a success page when done)\n")

    # Wait for the callback (timeout after 5 minutes)
    if not auth_result.received.wait(timeout=300):
        print("  TIMEOUT — No callback received within 5 minutes.")
        print("  Make sure your Saxo app has this redirect URI registered:")
        print(f"    {REDIRECT_URL}")
        print("  Then try again: py -3 saxo_auth_auto.py")
        server.shutdown()
        sys.exit(1)

    # Shut down the temporary server
    server.shutdown()

    # Check result
    if auth_result.error:
        print(f"  LOGIN FAILED: {auth_result.error}")
        print()
        print("  Troubleshooting:")
        print("  1. Make sure your Saxo SIM app has this redirect URI:")
        print(f"     {REDIRECT_URL}")
        print("  2. Go to https://developer.saxobank.com/ → your app → Edit")
        print(f"  3. Add '{REDIRECT_URL}' to the Redirect URLs list")
        print("  4. Try again: py -3 saxo_auth_auto.py")
        sys.exit(1)

    # Success!
    print("  " + "=" * 54)
    print("  LOGIN SUCCESSFUL")
    print("  " + "=" * 54)
    print(f"  Tokens saved to: {TOKEN_FILE}")
    print(f"  Access token:    Valid for ~20 minutes")
    print(f"  Refresh token:   Valid for ~1 hour")
    print(f"  Auto-refresh:    Handled by saxo_client.py")
    print()
    print("  You can now run the ATOS daily cycle:")
    print("    py -3 -X utf8 run_atos.py")
    print()
    print("  Or view the dashboard:")
    print("    py -3 atos_dashboard.py")
    print()


def refresh_existing():
    """Try to refresh an existing token without interactive login."""
    if not os.path.exists(TOKEN_FILE):
        return False

    with open(TOKEN_FILE) as f:
        tokens = json.load(f)

    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        return False

    try:
        print("  Attempting to refresh existing token...")
        resp = requests.post(TOKEN_ENDPOINT, data={
            "grant_type":    "refresh_token",
            "refresh_token": refresh_token,
            "client_id":     APP_KEY,
        })
        resp.raise_for_status()
        new_tokens = resp.json()
        _save_tokens(new_tokens)
        print("  Token refreshed successfully! No login needed.")
        return True
    except Exception as e:
        print(f"  Refresh failed ({e}) — need fresh login.")
        return False


if __name__ == "__main__":
    # Try silent refresh first
    if refresh_existing():
        print("  All good — tokens are fresh.")
        sys.exit(0)

    # Fall back to interactive login
    login_auto()
