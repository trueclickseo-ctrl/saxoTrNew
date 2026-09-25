"""
ibkr_gateway_watchdog.py  (ATOS System Watchdog)
-------------------------------------------------
Monitors all 4 trading gateways and sends email on DOWN/UP transitions:

  IBKR Live  Gateway: 127.0.0.1:4001  (U28013794  real money)
  IBKR Paper Gateway: 127.0.0.1:4002  (DUR952126  paper)
  Saxo LIVE  token:   OAuth session for the real-money Saxo account
  Saxo SIM   token:   OAuth session for the Saxo SIM account

IBKR behaviour (full auto-restart):
  1. TCP connect to port → ib_insync API handshake (reqCurrentTime)
  2. If either fails: IBC STOP → kill java PID → relaunch via bat file
  3. Poll up to 600 s → email UP or "manual action needed"
  Kill is port-specific: live restart never touches paper and vice versa.
  Safety: never restarts while an IBKR strategy task is Running (mid-trade).
  Limit: 3 restarts/hour per gateway; skips quiet window 02:00-04:15 PKT.

Saxo behaviour (keepalive + alert, no auto-restart):
  1. Refresh token via saxo_auth.get_valid_access_token(env)
  2. Test connection via saxo_client.test_connection(env)
  3. OK  → log "Token OK"; send UP email if previously reported down
  4. FAIL → email "manual login required": python saxo_auth.py [--live]
  Runs every watchdog tick (~5 min) -- replaces the separate
  saxo_sim_token_keepalive and saxo_live_token_keepalive tasks.

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
from dataclasses import dataclass
from datetime import datetime

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)


# ── Gateway configs ────────────────────────────────────────────────────────────
@dataclass
class _GW:
    name: str
    port: int
    bat: str
    ibc_cmd_port: int
    watchdog_client_id: int
    state_key: str


LIVE_GW = _GW(
    name="Live",
    port=4001,
    bat=r"C:\IBC\StartGatewayLive.bat",
    ibc_cmd_port=7462,
    watchdog_client_id=99,
    state_key="live",
)

PAPER_GW = _GW(
    name="Paper",
    port=4002,
    bat=r"C:\IBC\StartGatewayPaper.bat",
    ibc_cmd_port=7463,
    watchdog_client_id=98,
    state_key="paper",
)

GATEWAYS = [LIVE_GW, PAPER_GW]

GATEWAY_HOST      = "127.0.0.1"
STARTUP_TIMEOUT_S = 600
STARTUP_POLL_S    = 5
MAX_RESTARTS_PER_HOUR = 3

# ── Quiet windows (PKT = UTC+5) ────────────────────────────────────────────────
# 02:00-03:00 AM PKT: Gateways do their daily AutoRestart (graceful, no 2FA).
# 03:00-04:15 AM PKT: machine reboots at 03:15; IBC relaunches with stored token.
_QUIET_START_PKT = (2,  0)
_QUIET_END_PKT   = (4, 15)

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
            s = json.load(f)
    except Exception:
        s = {}
    # Migrate from old single-gateway format
    if "restarts" in s and "live_restarts" not in s:
        s["live_restarts"]    = s.pop("restarts", [])
        s["live_last_alert"]  = s.pop("last_alert_ts", 0)
    s.setdefault("live_restarts",    [])
    s.setdefault("live_last_alert",  0)
    s.setdefault("live_down_ts",     0)   # ts when "DOWN" email last sent; 0 = up
    s.setdefault("paper_restarts",   [])
    s.setdefault("paper_last_alert", 0)
    s.setdefault("paper_down_ts",    0)   # ts when "DOWN" email last sent; 0 = up
    # Saxo tokens (can't auto-restart -- only keepalive + alert)
    s.setdefault("saxo_live_down_ts",    0)
    s.setdefault("saxo_live_last_alert", 0)
    s.setdefault("saxo_sim_down_ts",     0)
    s.setdefault("saxo_sim_last_alert",  0)
    return s


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
    """Return the task name if any IBKR strategy task is mid-run, else None."""
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
def _tcp_ok(port: int, timeout_s: float = 5.0) -> bool:
    try:
        with socket.create_connection((GATEWAY_HOST, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def _api_ok(gw: _GW) -> bool:
    try:
        from ib_insync import IB
        ib = IB()
        ib.connect(GATEWAY_HOST, gw.port,
                   clientId=gw.watchdog_client_id, timeout=10, readonly=True)
        ib.reqCurrentTime()
        ib.disconnect()
        return True
    except Exception as e:
        _log(f"  [{gw.name}] API handshake failed: {e}")
        return False


# ── Quiet-window helpers ──────────────────────────────────────────────────────
def _pkt_now() -> tuple[int, int]:
    import datetime as _dt
    pkt = _dt.datetime.utcnow() + _dt.timedelta(hours=5)
    return (pkt.hour, pkt.minute)


def _in_quiet_window() -> bool:
    h, m = _pkt_now()
    now_min = h * 60 + m
    start   = _QUIET_START_PKT[0] * 60 + _QUIET_START_PKT[1]
    end     = _QUIET_END_PKT[0]   * 60 + _QUIET_END_PKT[1]
    return start <= now_min <= end


# ── Process management ────────────────────────────────────────────────────────
def _ibc_stop_graceful(gw: _GW, timeout_s: float = 10.0) -> bool:
    """Send STOP to this gateway's IBC command server port."""
    try:
        with socket.create_connection(("127.0.0.1", gw.ibc_cmd_port),
                                      timeout=timeout_s) as s:
            s.sendall(b"STOP\n")
            time.sleep(1)
        _log(f"  [{gw.name}] IBC STOP sent to port {gw.ibc_cmd_port}")
        return True
    except OSError:
        _log(f"  [{gw.name}] IBC command server not reachable on port {gw.ibc_cmd_port}"
             " -- will use port-specific PID kill")
        return False


def _kill_gateway(gw: _GW) -> None:
    """Shut down this gateway gracefully (IBC STOP), then kill the PID holding its port."""
    if _ibc_stop_graceful(gw):
        time.sleep(8)
        if not _tcp_ok(gw.port, timeout_s=2.0):
            return  # already gone

    # Find the PID holding this specific port and kill only it
    # This avoids touching the other gateway instance
    time.sleep(2)
    if not _tcp_ok(gw.port, timeout_s=1.0):
        return
    try:
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True, text=True, timeout=10
        )
        for line in result.stdout.splitlines():
            if f":{gw.port}" in line and "LISTENING" in line:
                parts = line.split()
                pid = parts[-1] if parts else ""
                if pid.isdigit():
                    subprocess.run(["taskkill", "/F", "/T", "/PID", pid],
                                   capture_output=True, timeout=5)
                    _log(f"  [{gw.name}] killed PID {pid} holding port {gw.port}")
    except Exception as e:
        _log(f"  [{gw.name}] kill error: {e}")


def _start_gateway(gw: _GW) -> None:
    if not os.path.exists(gw.bat):
        raise FileNotFoundError(f"IBC launcher not found: {gw.bat}")
    subprocess.Popen(
        ["cmd.exe", "/c", gw.bat],
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    _log(f"  [{gw.name}] launched via IBC: {gw.bat}")


def _wait_for_gateway(gw: _GW) -> bool:
    _log(f"  [{gw.name}] waiting up to {STARTUP_TIMEOUT_S}s for Gateway...")
    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    while time.monotonic() < deadline:
        if _tcp_ok(gw.port, timeout_s=2.0):
            time.sleep(2)
            if _api_ok(gw):
                return True
        time.sleep(STARTUP_POLL_S)
    return False


# ── Per-gateway check ─────────────────────────────────────────────────────────
def _check_gateway(gw: _GW, state: dict, simulate_crash: bool = False) -> None:
    now_ts  = time.time()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    restarts_key = f"{gw.state_key}_restarts"
    alert_key    = f"{gw.state_key}_last_alert"
    down_key     = f"{gw.state_key}_down_ts"

    if simulate_crash and gw.name == "Live":
        _log(f"  [{gw.name}] [SIMULATE] forcing failure path")
        is_down = True
    elif _tcp_ok(gw.port):
        if _api_ok(gw):
            _log(f"  [{gw.name}] Gateway OK")
            # If it was previously reported as down, send a "back up" notification
            if state.get(down_key, 0) > 0:
                down_ago = int(now_ts - state[down_key])
                _send_alert(
                    f"[ATOS IBKR] {gw.name} Gateway is back UP",
                    f"IB {gw.name} Gateway is healthy again at {now_str}.\n"
                    f"It was down for approximately {down_ago // 60} min {down_ago % 60} s.\n\n"
                    f"No manual action required -- strategies will reconnect on next run.",
                )
                state[down_key] = 0
                _save_state(state)
            return
        _log(f"  [{gw.name}] TCP open but API handshake failed -- will restart")
        is_down = True
    else:
        _log(f"  [{gw.name}] TCP connect to port {gw.port} failed -- Gateway is down")
        is_down = True

    # ── Send immediate "DOWN" email once per outage (throttled to 1/hour) ─────
    if is_down:
        last_down = state.get(down_key, 0)
        if now_ts - last_down > 3600:
            _send_alert(
                f"[ATOS IBKR] {gw.name} Gateway is DOWN",
                f"IB {gw.name} Gateway (port {gw.port}) is not responding at {now_str}.\n\n"
                f"Watchdog will attempt an automatic restart now.\n"
                f"You will receive a follow-up email once it is back up or if manual action is needed.",
            )
            state[down_key] = now_ts
            _save_state(state)

    # Never restart while a strategy task has the connection open
    busy = _ibkr_task_running()
    if busy:
        _log(f"  [{gw.name}] SKIP restart -- IBKR task is Running: {busy}")
        return

    if _in_quiet_window():
        pkt_h, pkt_m = _pkt_now()
        _log(f"  [{gw.name}] down at {pkt_h:02d}:{pkt_m:02d} PKT -- inside quiet window, waiting.")
        return

    one_hour_ago = now_ts - 3600
    recent = [t for t in state.get(restarts_key, []) if t > one_hour_ago]

    if len(recent) >= MAX_RESTARTS_PER_HOUR:
        last_alert = state.get(alert_key) or 0
        if now_ts - last_alert > 3600:
            _send_alert(
                f"[ATOS IBKR] {gw.name} Gateway repeatedly failing -- manual login needed",
                f"IB {gw.name} Gateway restarted {len(recent)}x in the last hour "
                f"and is STILL down at {now_str}.\n\n"
                f"Action: check the Gateway window -- it may need credentials entered manually.\n"
                f"If no window is visible, run {gw.bat} manually.\n",
            )
            state[alert_key] = now_ts
            _save_state(state)
        _log(f"  [{gw.name}] restart limit reached -- manual login likely needed")
        return

    # ── Restart ───────────────────────────────────────────────────────────────
    _log(f"  [{gw.name}] Restart #{len(recent)+1} (of {MAX_RESTARTS_PER_HOUR} allowed/hour)")
    if simulate_crash and gw.name == "Live":
        _log(f"  [{gw.name}] [SIMULATE] skipping actual kill/launch")
        came_up = True
    else:
        _kill_gateway(gw)
        time.sleep(3)
        _start_gateway(gw)
        came_up = _wait_for_gateway(gw)

    state[restarts_key] = recent + [now_ts]
    _save_state(state)

    if came_up:
        _log(f"  [{gw.name}] Gateway restarted OK at {datetime.now().strftime('%H:%M:%S')}")
        state[down_key] = 0   # clear down flag -- "back up" already implied by this email
        _save_state(state)
        _send_alert(
            f"[ATOS IBKR] {gw.name} Gateway back UP -- auto-restarted OK",
            f"IB {gw.name} Gateway was down and was automatically restarted at {now_str}.\n"
            f"Restart #{len(recent)+1} in the past hour.\n\n"
            f"All IBKR strategies will reconnect on their next scheduled run.\n"
            f"No manual action required.",
        )
    else:
        _log(f"  [{gw.name}] Gateway did NOT respond within {STARTUP_TIMEOUT_S}s")
        state[alert_key] = now_ts
        _save_state(state)
        _send_alert(
            f"[ATOS IBKR] {gw.name} Gateway restart failed -- manual action needed",
            f"IB {gw.name} Gateway was restarted via IBC at {now_str} but did not become\n"
            f"responsive within {STARTUP_TIMEOUT_S}s.\n\n"
            f"Action: open the Gateway window -- it may be waiting for credentials.\n"
            f"If no window is visible, run {gw.bat} manually.\n",
        )


# ── Saxo token check ─────────────────────────────────────────────────────────
def _check_saxo(env: str, label: str, state: dict) -> None:
    """Refresh + test a Saxo OAuth token.

    env:   "live" or "sim"
    label: "LIVE" or "SIM"

    Unlike IBKR gateways, a dead Saxo session cannot be auto-restarted --
    a browser-based PKCE login is required once the refresh-token chain
    breaks.  This function:
      - Refreshes the access token (no-op if it is still fresh)
      - Makes a live /port/v1/users/me call to confirm the token is valid
      - Sends one DOWN email per hour if the check fails
      - Sends an UP email when recovery is detected (user did manual re-login)
    Runs every watchdog tick, replacing the separate keepalive tasks.
    """
    down_key  = f"saxo_{env}_down_ts"
    alert_key = f"saxo_{env}_last_alert"
    now_ts    = time.time()
    now_str   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        import saxo_auth
        import saxo_client
        saxo_auth.get_valid_access_token(env=env)
        me = saxo_client.test_connection(env=env)
        name = me.get("Name", "?")
        uid  = me.get("UserId", "?")
        _log(f"  [Saxo {label}] Token OK — {name} (UserId {uid})")

        if state.get(down_key, 0) > 0:
            down_ago = int(now_ts - state[down_key])
            _send_alert(
                f"[ATOS] Saxo {label} token back UP",
                f"Saxo {label} API is responding again at {now_str}.\n"
                f"Was down for approximately {down_ago // 60} min {down_ago % 60} s.\n\n"
                f"All Saxo {label} strategies will work normally on next run.\n"
                f"No manual action required.",
            )
            state[down_key] = 0
            _save_state(state)

    except Exception as exc:
        _log(f"  [Saxo {label}] Token check FAILED: {exc}")
        last_down = state.get(down_key, 0)
        if now_ts - last_down > 3600:
            login_cmd = "python saxo_auth.py --live" if env == "live" else "python saxo_auth.py"
            _send_alert(
                f"[ATOS] Saxo {label} token is DOWN — manual login required",
                f"Saxo {label} token check failed at {now_str}.\n\n"
                f"Error: {exc}\n\n"
                f"Action required — run:\n"
                f"  {login_cmd}\n\n"
                f"The watchdog will automatically send a recovery email once the\n"
                f"token is healthy again.",
            )
            state[down_key] = now_ts
            _save_state(state)


# ── Main ──────────────────────────────────────────────────────────────────────
def main(simulate_crash: bool = False) -> None:
    _init_log()
    _log("=== ATOS System Watchdog (IBKR + Saxo) ===")
    state = _load_state()
    for gw in GATEWAYS:
        _check_gateway(gw, state, simulate_crash=simulate_crash)
    _check_saxo("live", "LIVE", state)
    _check_saxo("sim",  "SIM",  state)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--simulate-crash", action="store_true",
                   help="Force the failure path for the Live gateway without killing it "
                        "(tests detection, state, email, and restart logic end-to-end)")
    args = p.parse_args()
    main(simulate_crash=args.simulate_crash)
