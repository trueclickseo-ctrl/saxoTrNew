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
  - Skips cold-restart during the quiet window 02:00-04:15 AM PKT
    (auto-restart at 02:15 + machine reboot at 03:15 -- no 2FA needed)
  - Skips cold-restart on Sunday 07:30 AM PKT (weekly 2FA push -- user approves once)

2FA schedule (after config_live.ini AutoRestartTime=02:15 AM):
  Mon-Sat: zero 2FA -- Gateway auto-restarts at 02:15 AM with stored session token
  Sunday:  one 2FA push at 07:30 AM PKT -- IBKR requires weekly re-authentication

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
IBC_BAT      = r"C:\IBC\StartGatewayLive.bat"   # IBC launcher (handles login + 2FA push)

# ib_insync client ID for the watchdog's own heartbeat connect -- must NOT
# collide with any strategy client ID in ibkr_module/config/ibkr_config.json.
WATCHDOG_CLIENT_ID = 99

STARTUP_TIMEOUT_S = 600  # increased: auto-restart token path is fast, but allow headroom
STARTUP_POLL_S    = 5    # polling interval during startup wait

MAX_RESTARTS_PER_HOUR = 3  # give up + email if Gateway keeps dying

# IBC command server port for graceful STOP (must match CommandServerPort in config_live.ini).
IBC_COMMAND_PORT = 7462

# ── Quiet windows (PKT = UTC+5) ────────────────────────────────────────────────
# 02:00-03:00 AM PKT: Gateway does its daily AutoRestart (graceful, no 2FA).
# 03:00-04:15 AM PKT: machine reboots at 03:15; IBC relaunches with stored token.
# During these windows the watchdog waits rather than triggering a cold restart.
_QUIET_START_PKT = (2,  0)   # (hour, minute) PKT
_QUIET_END_PKT   = (4, 15)   # Gateway reliably back by 04:15 AM PKT

# Sunday cold-restart window — IBC sends a 2FA push at 07:30 AM PKT once/week.
_COLD_RESTART_PKT = (7, 30)

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


# ── Quiet-window helpers ──────────────────────────────────────────────────────

def _pkt_now() -> tuple[int, int]:
    """Return current PKT (UTC+5) as (hour, minute)."""
    import datetime as _dt
    utc_now = _dt.datetime.utcnow()
    pkt = utc_now + _dt.timedelta(hours=5)
    return (pkt.hour, pkt.minute)


def _in_quiet_window() -> bool:
    """True when Gateway is expected to be briefly down (auto-restart or reboot).

    During this window the watchdog skips cold-restart attempts and waits for
    Gateway to come back on its own using its stored session token (no 2FA).
    """
    h, m = _pkt_now()
    now_min  = h * 60 + m
    start    = _QUIET_START_PKT[0]  * 60 + _QUIET_START_PKT[1]
    end      = _QUIET_END_PKT[0]    * 60 + _QUIET_END_PKT[1]
    return start <= now_min <= end


def _is_sunday_cold_restart_window() -> bool:
    """True on Sunday ±20 min around the cold-restart time (2FA required)."""
    import datetime as _dt
    utc_now = _dt.datetime.utcnow()
    pkt = utc_now + _dt.timedelta(hours=5)
    if pkt.weekday() != 6:   # 6 = Sunday
        return False
    now_min  = pkt.hour * 60 + pkt.minute
    cr_min   = _COLD_RESTART_PKT[0] * 60 + _COLD_RESTART_PKT[1]
    return abs(now_min - cr_min) <= 20


# ── Graceful IBC stop ─────────────────────────────────────────────────────────

def _ibc_stop_graceful(timeout_s: float = 10.0) -> bool:
    """Send STOP to the IBC command server so Gateway shuts down gracefully.

    A graceful shutdown lets IBC/Gateway save the auto-restart session token
    to disk.  Returns True if the command was accepted, False otherwise.
    """
    try:
        with socket.create_connection(("127.0.0.1", IBC_COMMAND_PORT),
                                      timeout=timeout_s) as s:
            s.sendall(b"STOP\n")
            time.sleep(1)
        _log(f"  [ibc] STOP sent to command server port {IBC_COMMAND_PORT}")
        return True
    except OSError:
        _log(f"  [ibc] command server not reachable on port {IBC_COMMAND_PORT}"
             " -- will use taskkill")
        return False


# ── Process management ────────────────────────────────────────────────────────
def _kill_gateway() -> None:
    """Shut down ibgateway1.exe gracefully (IBC STOP), then force-kill if needed.

    Graceful shutdown is preferred because it lets IBC save the auto-restart
    session token so the next startup can reconnect without 2FA.
    """
    if _ibc_stop_graceful():
        time.sleep(8)  # wait for IBC to close Gateway cleanly
        if not _tcp_ok(timeout_s=2.0):
            return       # already gone, no need for taskkill

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
    """Launch IB Gateway via IBC (auto-fills credentials, sends 2FA push to phone)."""
    if not os.path.exists(IBC_BAT):
        raise FileNotFoundError(f"IBC launcher not found: {IBC_BAT}")
    subprocess.Popen(
        ["cmd.exe", "/c", IBC_BAT],
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    _log(f"  launched via IBC: {IBC_BAT}")


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
def main(simulate_crash: bool = False) -> None:
    _init_log()
    now_ts  = time.time()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _log("=== IBKR Gateway Watchdog ===")

    if simulate_crash:
        _log("  [SIMULATE] --simulate-crash: skipping real TCP/API checks, forcing failure path")
        _log("  [SIMULATE] Gateway reported as DOWN")
    elif _tcp_ok():
        # Fast path: Gateway is up and healthy
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

    # During the daily auto-restart / machine-reboot quiet window (02:00-04:15 AM PKT),
    # Gateway is expected to be briefly down.  IBC will bring it back using the stored
    # auto-restart session token -- no 2FA needed.  We wait and skip cold-restart logic.
    if _in_quiet_window():
        pkt_h, pkt_m = _pkt_now()
        _log(f"  Gateway down at {pkt_h:02d}:{pkt_m:02d} PKT -- inside quiet window "
             f"({_QUIET_START_PKT[0]:02d}:{_QUIET_START_PKT[1]:02d}-"
             f"{_QUIET_END_PKT[0]:02d}:{_QUIET_END_PKT[1]:02d} PKT).")
        _log("  Expected: auto-restart / machine reboot.  IBC will reconnect without 2FA.")
        _log("  Will check again on next watchdog cycle (no cold-restart triggered).")
        return

    # Sunday cold-restart window (07:30 AM PKT ±20 min): IBC sends a 2FA push once/week.
    if _is_sunday_cold_restart_window():
        _log("  Gateway down during Sunday cold-restart window (07:30 AM PKT).")
        _log("  IBKR requires 2FA re-authentication once per week.")
        _log("  Check your IBKR Mobile app for a push notification and approve it.")
        state = _load_state()
        last_alert = state.get("last_alert_ts") or 0
        if now_ts - last_alert > 7200:
            _send_alert(
                "[ATOS IBKR] Weekly 2FA re-authentication required (Sunday)",
                f"IB Gateway is down for its weekly cold restart at {now_str} PKT.\n\n"
                f"IBKR requires full re-authentication once per week (Sundays).\n"
                f"Action: check your IBKR Mobile app for a push notification and approve it.\n\n"
                f"Gateway will reconnect automatically once you approve.\n"
                f"No action needed for the remaining 6 days -- auto-restart handles those.",
            )
            state["last_alert_ts"] = now_ts
            _save_state(state)
        return

    # Restart rate limiter
    state = _load_state()
    one_hour_ago = now_ts - 3600
    recent = [t for t in state.get("restarts", []) if t > one_hour_ago]

    if len(recent) >= MAX_RESTARTS_PER_HOUR:
        msg = (
            f"IB Gateway restarted {len(recent)}x in the last hour "
            f"and is STILL down at {now_str}.\n\n"
            f"This is outside the expected quiet window, so something unusual happened.\n\n"
            f"IBC may be waiting for 2FA approval on your IBKR Mobile app.\n"
            f"Action: check your IBKR Mobile app for a push notification and approve it.\n"
            f"If no notification arrives, run C:\\IBC\\StartGatewayLive.bat manually.\n\n"
            f"Normal schedule (no 2FA needed):\n"
            f"  02:15 AM PKT: daily auto-restart (token-based, seamless)\n"
            f"  07:30 AM PKT Sundays only: weekly cold restart (2FA push to phone)\n"
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
    if simulate_crash:
        _log("  [SIMULATE] would kill ibgateway1.exe (skipped -- Gateway still running)")
        _log("  [SIMULATE] would launch via IBC: C:\\IBC\\StartGatewayLive.bat (skipped)")
        _log("  [SIMULATE] Gateway is actually still up -- reporting restart success")
        came_up = True
    else:
        _kill_gateway()
        time.sleep(3)   # let OS release the port before we relaunch
        _start_gateway()
        came_up = _wait_for_gateway()

    if came_up:
        _log(f"  Gateway restarted OK at {datetime.now().strftime('%H:%M:%S')}"
             + ("  [SIMULATED]" if simulate_crash else ""))
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
        _log(f"  Gateway did NOT respond within {STARTUP_TIMEOUT_S}s -- 2FA approval needed")
        state["restarts"] = recent + [now_ts]
        state["last_alert_ts"] = now_ts
        _save_state(state)
        _send_alert(
            "[ATOS IBKR] Gateway restart failed -- 2FA approval needed",
            f"IB Gateway was restarted via IBC at {now_str} but did not become responsive\n"
            f"within {STARTUP_TIMEOUT_S}s.\n\n"
            f"This is an unexpected restart (outside the 02:00-04:15 AM PKT quiet window).\n"
            f"IBC may be waiting for 2FA approval on your IBKR Mobile app.\n"
            f"Action: check your IBKR Mobile app for a push notification and approve it.\n"
            f"If no notification arrives, run C:\\IBC\\StartGatewayLive.bat manually.\n\n"
            f"Normal schedule (no 2FA needed):\n"
            f"  02:15 AM PKT: daily auto-restart (token-based, seamless)\n"
            f"  07:30 AM PKT Sundays only: weekly cold restart (2FA push to phone)\n",
        )


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--simulate-crash", action="store_true",
                   help="Force the failure path without actually killing Gateway "
                        "(tests detection, state, email, and restart logic end-to-end)")
    args = p.parse_args()
    main(simulate_crash=args.simulate_crash)
