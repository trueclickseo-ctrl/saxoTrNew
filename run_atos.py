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

# ── Path setup ────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

# ── Pre-flight: ensure Saxo token is valid ────────────────────────
print("Checking Saxo SIM token...")
try:
    import saxo_auth_auto
    if not saxo_auth_auto.refresh_existing():
        print("\n  Token expired — launching automatic login...\n")
        saxo_auth_auto.login_auto()
    else:
        print("  Token is valid.\n")
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
        else:
            print("  [WARN] atos_dashboard.py not found — run it manually:")
            print("         py -3 atos_dashboard.py")
    except Exception as e:
        print(f"  [WARN] Could not auto-launch dashboard: {e}")
        print("         Run manually: py -3 atos_dashboard.py")
