"""
ibkr_gateway_watchdog.py
------------------------
Monitors IB Gateway on 127.0.0.1:4001 and auto-restarts it when the
connection drops or the API becomes unresponsive.

Scheduled every 5 minutes via "ATOS IBKR Gateway Watchdog" (Windows Task
Scheduler). One check-and-exit per invocation -- no internal loop.

Checks (in order):
  1. TCP connect to port 4001 (fast -- no ib_insync overhead)
  2. ib_insync API handshake (reqCurrentTime) to confirm API responds
  3. If either fails: kill ibgateway1.exe process tree + relaunch it
  4. Poll port 4001 for up to 90 s after relaunch
  5. Email alert (success or failure)

Safety guards:
  - Never restarts while any IBKR strategy task is Running (mid-trade)
  - Stops auto-restarting after 3 restarts/hour; emails for manual login
    (IB Gateway may need a 2FA re-authentication after repeated crashes)

State:  data/ibkr_gateway_watchdog.json
Log:    data/ibkr_gateway_watchdog.log
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)

# ── Config ────────────────────────────────────────────────────────────────────
GATEWAY_HOST = "127.0.0.1"
GATEWAY_PORT = 4001
GATEWAY_EXE  = r"C:\Jts\ibgateway\1050\ibgateway1.exe"

# ib_insync client ID for the watchdog's own heartbeat connect -- must NOT
# collide with any strategy client ID in ibkr_module/config/ibkr_config.json.
WATCHDOG_CLIENT_ID = 99

STARTUP_TIMEOUT_S = 90   # wait this long for Gateway to bind port after restart
STARTUP_POLL_S    = 3    # polling interval during startup wait

MAX_RESTARTS_PER_HOUR = 3  # give up + email if Gateway keeps dying

STATE_FILE = os.path.join(_ROOT, "data", "ibkr_gateway_watchdog.json")
LOG_FILE   = os.path.join(_ROOT, "data", "ibkr_gateway_watchdog.log")


# ── Logging ───────────────────────────────────────────────────────────────────
class _Tee:
    def __init__(self, a, b):
        self._a, self._b = a, b
    def write(self, d):
        self._a.write(d); self._b.write(d)
    def flush(self):
        self._a.flush(); self._b.flush()


def _init_log() -> None:
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    f = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
    sys.stdout = _Tee(sys.stdout, f)
    sys.stderr = _Tee(sys.stderr, f)


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ── State ─────────────────────────────────────────────────────────────────────
def _load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"restarts": [], "last_alert_ts": 0}


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Email ─────────────────────────────────────────────────────────────────────
def _send_alert(subject: str, body: str) -> None:
    try:
        from atos.notifier import _send
        _send(subject, f"<pre style='font-family:monospace'>{body}</pre>")
        _log(f"  [email] sent: {subject}")
    except Exception as e:
        _log(f"  [email] failed: {e}")


# ── Strategy task guard ───────────────────────────────────────────────────────
def _ibkr_task_running() -> str | None:
    """Return the task name if any IBKR strategy task is mid-run, else None.

    Restarting Gateway while a strategy is connected would abruptly disconnect
    it -- potentially leaving an order in an unknown state.
    """
    try:
        out = subprocess.check_output(
            ["schtasks", "/query", "/fo", "CSV", "/nh"],
            text=True, timeout=15, stderr=subprocess.DEVNULL
        )
        for line in out.splitlines():
            parts = line.split(",")
            if len(parts) < 3:
                continue
            name   = parts[0].strip('"')
            status = parts[2].strip('"').lower()
            if ("ibkr" in name.lower()
                    and "watchdog" not in name.lower()
                    and "running" in status):
                return name
    except Exception:
        pass
    return None


# ── Connectivity checks ───────────────────────────────────────────────────────
def _tcp_ok(timeout_s: float = 5.0) -> bool:
    """True if port 4001 accepts a TCP connection."""
    try:
        with socket.create_connection((GATEWAY_HOST, GATEWAY_PORT), timeout=timeout_s):
            return True
    except OSError:
        return False


def _api_ok() -> bool:
    """True if ib_insync can handshake and get a response (reqCurrentTime)."""
    try:
        from ib_insync import IB
        ib = IB()
        # timeout=10: max seconds to wait for the API handshake
        ib.connect(GATEWAY_HOST, GATEWAY_PORT,
                   clientId=WATCHDOG_CLIENT_ID, timeout=10, readonly=True)
        ib.reqCurrentTime()
        ib.disconnect()
        return True
    except Exception as e:
        _log(f"  [api] handshake failed: {e}")
        return False


# ── Process management ────────────────────────────────────────────────────────
def _kill_gateway() -> None:
    """Kill ibgateway1.exe and all its child processes (including java.exe)."""
    for proc in ("ibgateway1.exe",):
        result = subprocess.run(
            ["taskkill", "/F", "/T", "/IM", proc],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            _log(f"  killed {proc} (and children)")
        else:
            # Not found is fine -- it may have already crashed
            if "not found" not in result.stderr.lower():
                _log(f"  taskkill {proc}: {result.stderr.strip()}")

    # Belt-and-suspenders: also kill any orphaned java.exe whose parent was
    # ibgateway1.exe (handles the case where ibgateway1.exe already exited
    # but left a zombie java.exe still holding port 4001).
    time.sleep(2)
    if _tcp_ok(timeout_s=1.0):
        # Port still bound -- find and kill the java process holding it
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True, text=True, timeout=10
        )
        for line in result.stdout.splitlines():
            if f":{GATEWAY_PORT}" in line and "LISTENING" in line:
                parts = line.split()
                pid = parts[-1] if parts else ""
                if pid.isdigit():
                    subprocess.run(["taskkill", "/F", "/PID", pid],
                                   capture_output=True, timeout=5)
                    _log(f"  killed orphan PID {pid} holding port {GATEWAY_PORT}")


def _start_gateway() -> None:
    """Launch IB Gateway. Requires auto-login to be configured in Gateway settings."""
    if not os.path.exists(GATEWAY_EXE):
        raise FileNotFoundError(f"IB Gateway executable not found: {GATEWAY_EXE}")
    subprocess.Popen(
        [GATEWAY_EXE],
        cwd=os.path.dirname(GATEWAY_EXE),
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    _log(f"  launched: {GATEWAY_EXE}")


def _wait_for_gateway() -> bool:
    """Poll port 4001 until Gateway is API-responsive or timeout."""
    _log(f"  waiting up to {STARTUP_TIMEOUT_S}s for Gateway...")
    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    while time.monotonic() < deadline:
        if _tcp_ok(timeout_s=2.0):
            time.sleep(2)   # let the API layer finish initialising
            if _api_ok():
                return True
        time.sleep(STARTUP_POLL_S)
    return False


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    _init_log()
    now_ts  = time.time()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _log("=== IBKR Gateway Watchdog ===")

    # Fast path: Gateway is up and healthy
    if _tcp_ok():
        if _api_ok():
            _log("  Gateway OK")
            return
        _log("  TCP open but API handshake failed -- will restart")
    else:
        _log("  TCP connect to port 4001 failed -- Gateway is down")

    # ── Gateway is not responsive ─────────────────────────────────────────────

    # Never restart while a strategy task has the connection open
    busy = _ibkr_task_running()
    if busy:
        _log(f"  SKIP restart -- IBKR task is Running: {busy}")
        _log("  Will retry on the next 5-min watchdog cycle.")
        return

    # Restart rate limiter
    state = _load_state()
    one_hour_ago = now_ts - 3600
    recent = [t for t in state.get("restarts", []) if t > one_hour_ago]

    if len(recent) >= MAX_RESTARTS_PER_HOUR:
        msg = (
            f"IB Gateway restarted {len(recent)}x in the last hour "
            f"and is STILL down at {now_str}.\n\n"
            f"IB Gateway may be waiting for manual 2FA login.\n"
            f"Action: open C:\\Jts\\ibgateway\\1050\\ibgateway1.exe manually and log in."
        )
        _log(f"  [ALERT] restart limit reached -- manual login likely needed")
        last_alert = state.get("last_alert_ts") or 0
        if now_ts - last_alert > 3600:
            _send_alert("[ATOS IBKR] Gateway repeatedly failing -- manual login needed", msg)
            state["last_alert_ts"] = now_ts
            _save_state(state)
        return

    # ── Restart ───────────────────────────────────────────────────────────────
    _log(f"  Restart #{len(recent)+1} (of {MAX_RESTARTS_PER_HOUR} allowed/hour)")
    _kill_gateway()
    time.sleep(3)   # let OS release the port before we relaunch
    _start_gateway()

    if _wait_for_gateway():
        _log(f"  Gateway restarted OK at {datetime.now().strftime('%H:%M:%S')}")
        state["restarts"] = recent + [now_ts]
        _save_state(state)
        _send_alert(
            "[ATOS IBKR] Gateway auto-restarted OK",
            f"IB Gateway was down and was automatically restarted at {now_str}.\n"
            f"Restart #{len(recent)+1} in the past hour.\n\n"
            f"All IBKR strategies will reconnect on their next scheduled run.\n"
            f"No manual action required.",
        )
    else:
        _log(f"  Gateway did NOT respond within {STARTUP_TIMEOUT_S}s -- may need 2FA")
        state["restarts"] = recent + [now_ts]
        state["last_alert_ts"] = now_ts
        _save_state(state)
        _send_alert(
            "[ATOS IBKR] Gateway restart failed -- manual login required",
            f"IB Gateway was restarted at {now_str} but did not become responsive\n"
            f"within {STARTUP_TIMEOUT_S}s.\n\n"
            f"IB Gateway may require a manual 2FA login.\n"
            f"Action: open C:\\Jts\\ibgateway\\1050\\ibgateway1.exe and log in manually.",
        )


if __name__ == "__main__":
    main()
