"""
run_atos.py
-----------
ATOS Daily Runner — LOCALHOST MODE.

This wrapper runs the full ATOS daily trading cycle (from atos_runner.py)
and then launches the local dashboard instead of uploading to namazic.com.

Usage:
    py -3 -X utf8 run_atos.py

What it does:
  1. Checks Saxo token — auto-refreshes or prompts for login if needed
  2. Runs the full daily cycle (data download → signals → orders → learning)
  3. Skips FTP upload to namazic.com
  4. Launches the local dashboard at http://localhost:8070

For other agents: This file exists because the original atos_runner.py
cannot be modified (owned by user SEO, read-only for Kashif).
Once fix_permissions.bat is run, atos_runner.py can be edited directly.
"""

import sys
import os
import json
import time

# ── Path setup ────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
TOKEN_FILE = os.path.join(BASE_DIR, "saxo_token.json")


def _access_token_valid(buffer_s: int = 600) -> bool:
    """True if the saved access token is still good (with a safety buffer).
    Saxo SIM tokens last ~24h, so a daily one-click run rarely needs a browser."""
    try:
        with open(TOKEN_FILE) as f:
            d = json.load(f)
        return (d.get("obtained_at", 0) + d.get("expires_in", 0)) - buffer_s > time.time()
    except Exception:
        return False


# ── Pre-flight: ensure Saxo token is valid ────────────────────────
# Order matters: a still-valid ACCESS token needs no login even if the
# refresh token is dead — only fall back to the browser as a last resort.
print("Checking Saxo SIM token...")
try:
    import saxo_auth_auto
    if _access_token_valid():
        print("  Access token still valid — no login needed.\n")
    elif saxo_auth_auto.refresh_existing():
        print("  Token refreshed — no login needed.\n")
    else:
        print("\n  Token expired — launching browser login...\n")
        saxo_auth_auto.login_auto()
except Exception as e:
    print(f"  [WARN] Could not verify token: {e}")
    print("  The cycle will attempt to proceed — it may fail on Saxo API calls.\n")

# ── Monkey-patch: disable FTP upload ─────────────────────────────
import atos_runner
_original_upload = atos_runner.upload_dashboard
atos_runner.upload_dashboard = lambda local_file: print(
    "  [SKIP] FTP upload disabled — using localhost mode"
)

# ── Run the daily cycle ──────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 60)
    print("ATOS — Running in LOCALHOST mode (no FTP upload)")
    print("=" * 60)

    # Run the full trading cycle
    atos_runner.run_cycle()

    # Launch the local dashboard server
    print("\n  Starting local dashboard server...")
    try:
        import subprocess
        dashboard_script = os.path.join(BASE_DIR, "atos_dashboard.py")
        if os.path.exists(dashboard_script):
            subprocess.Popen(
                [sys.executable, dashboard_script],
                cwd=BASE_DIR,
                creationflags=0x00000008  # DETACHED_PROCESS on Windows
            )
            print("  Dashboard launched at http://localhost:8070")
            # Give the server a moment, then open the browser for the user.
            import webbrowser, time as _t
            _t.sleep(2)
            webbrowser.open("http://localhost:8070")
        else:
            print("  [WARN] atos_dashboard.py not found — run it manually:")
            print("         py -3 atos_dashboard.py")
    except Exception as e:
        print(f"  [WARN] Could not auto-launch dashboard: {e}")
        print("         Run manually: py -3 atos_dashboard.py")
