# IBKR Gateway Setup

**Last updated:** 2026-09-23  
**Accounts:** Live U28013794 (port 4001) · Paper DUR952126 (port 4002)

---

## Architecture overview

Two IB Gateway instances run side-by-side on Windows startup:

```
Boot (03:15 AM PKT reboot)
  └─ Task Scheduler ──► "IBC Start IB Gateway"        → StartGatewayLive.bat /INLINE → port 4001 (LIVE)
  └─ Task Scheduler ──► "IBC Start IB Gateway Paper"  → StartGatewayPaper.bat /INLINE → port 4002 (PAPER)

Nightly (while machine is up)
  02:15 AM PKT → IBC auto-restarts LIVE Gateway  (no 2FA — TST token)
  02:30 AM PKT → IBC auto-restarts PAPER Gateway (no 2FA — TST token, staggered 15 min)

Watchdog (every 5 min via ATOS IBKR Gateway Watchdog task)
  Monitors port 4001 only; restarts live Gateway if unresponsive
  Quiet window 02:00–04:15 AM PKT (auto-restart + machine reboot) — watchdog waits, does not intervene
```

---

## File locations

| Purpose | Path |
|---------|------|
| **Live IBC config** | `C:\Users\Kwaseem\Documents\IBC\config_live.ini` |
| **Paper IBC config** | `C:\Users\Kwaseem\Documents\IBC\config_paper.ini` |
| **Live Gateway bat** | `C:\IBC\StartGatewayLive.bat` |
| **Paper Gateway bat** | `C:\IBC\StartGatewayPaper.bat` |
| **Live IBC logs** | `C:\IBC\Logs_live\IBC-3.24.2_GATEWAY-1050_<DAY>.txt` |
| **Paper IBC logs** | `C:\IBC\Logs_paper\IBC-3.24.2_GATEWAY-1050_<DAY>.txt` |
| **Live Jts settings** | `C:\Jts_live\` |
| **Paper Jts settings** | `C:\Jts_paper\` |
| **Live TST token** | `C:\Jts_live\ekkombenemjbfmlimblicplfkmlgadmacnghoiin\thts.cache` |
| **Paper TST token** | `C:\Jts_paper\<hash>\thts.cache` (created on first auto-restart) |
| **Watchdog script** | `ibkr_gateway_watchdog.py` |
| **Watchdog state** | `data/ibkr_gateway_watchdog.json` |
| **Watchdog log** | `data/ibkr_gateway_watchdog.log` |
| **IBKR module config** | `ibkr_module/config/ibkr_config.json` |

---

## Task Scheduler tasks

Both tasks run `/INLINE` so Task Scheduler tracks the process lifetime.  
**Both require elevation (RunLevel=Highest).** Use `CreatePaperGatewayTask.ps1` style scripts (run as admin) to modify them — `Set-ScheduledTask` returns Access Denied from a non-elevated shell.

### "IBC Start IB Gateway" (live)
| Setting | Value |
|---------|-------|
| Execute | `C:\IBC\StartGatewayLive.bat` |
| Arguments | `/INLINE` |
| Working dir | `C:\IBC` |
| Trigger 1 | At boot + **2 min** delay |
| Trigger 2 | At logon + **1 min** delay |
| Trigger 3 | Daily **13:00 PKT** (catches the 12:55→13:00 cycle if config.ini ClosedownAt ever fires) |

### "IBC Start IB Gateway Paper" (paper)
| Setting | Value |
|---------|-------|
| Execute | `C:\IBC\StartGatewayPaper.bat` |
| Arguments | `/INLINE` |
| Working dir | `C:\IBC` |
| Trigger 1 | At boot + **4 min** delay (2 min after live to avoid session race) |
| Trigger 2 | At logon + **2 min** delay |
| Trigger 3 | Daily **13:05 PKT** (5 min staggered from live) |

---

## IBC config: key settings explained

### config_live.ini
```ini
TradingMode=live          # connects to U28013794 (real money)
OverrideTwsApiPort=4001   # Gateway API port
AutoRestartTime=02:15 AM  # daily graceful restart -- IBC writes autorestart file on first execution
                          # subsequent machine reboots find the file → no 2FA dialog
ColdRestartTime=          # BLANK -- no IBC-scheduled weekly forced re-auth
                          # 2FA only when IBKR expires the TST token (months apart)
CommandServerPort=7462    # watchdog sends STOP here before any kill → preserves TST token
```

### config_paper.ini
```ini
TradingMode=paper         # connects to DUR952126 (paper/simulation account)
OverrideTwsApiPort=4002   # paper Gateway API port
AutoRestartTime=02:30 AM  # staggered 15 min after live to avoid concurrent conflicts
ColdRestartTime=          # BLANK
CommandServerPort=7463    # separate port from live (7462)
```

---

## How the zero-2FA mechanism works

1. **First boot after setup**: 2FA push sent to IBKR Mobile → user approves once.
2. IBC converts the SOFT (2FA) token → **TST (Time Session Token)** stored in `thts.cache`.
3. At `AutoRestartTime`: Gateway does a graceful daily restart. IBC writes an **autorestart file** in the Jts settings folder.
4. On every subsequent startup (reboot, crash, watchdog restart): IBC finds the autorestart file + TST → **skips login dialog entirely**, no 2FA.
5. TST token expires after months. When it does, one IBKR Mobile push notification arrives. Approve it on the phone (no computer needed — approve from notification shade). Clock resets.

**Critical rule**: never `taskkill /F ibgateway1.exe` directly — this destroys `thts.cache` and forces 2FA on the next boot. The watchdog sends a graceful `STOP` to the IBC command server (port 7462/7463) first; only falls back to taskkill if the command server is unreachable.

---

## Modifying Task Scheduler tasks

Tasks are registered with `RunLevel=Highest`. Modifying them requires admin elevation.

**Pattern (create a .ps1, run as admin):**
```powershell
$action = New-ScheduledTaskAction -Execute "C:\IBC\StartGatewayLive.bat" -Argument "/INLINE" -WorkingDirectory "C:\IBC"
Set-ScheduledTask -TaskName "IBC Start IB Gateway" -Action $action
```

Or export XML, patch, re-import:
```powershell
schtasks /Query /TN "IBC Start IB Gateway" /XML ONE > task.xml
# edit task.xml
schtasks /Create /TN "IBC Start IB Gateway" /XML task.xml /F
```

Save the script in `C:\IBC\`, run as admin, then delete it.

---

## Modifying IBC configs

`config_live.ini` and `config_paper.ini` are in `C:\Users\Kwaseem\Documents\IBC\` (outside the repo — not in git).  
Changes take effect on the **next Gateway startup** (current running session ignores them).  
To apply immediately: gracefully stop Gateway via watchdog or restart the Task Scheduler task.

**Never change** `AutoRestartTime` without also considering the machine reboot at 03:15 AM PKT. The auto-restart must happen **before** the reboot so the autorestart file exists when the machine comes back up. Current margin: 02:15 AM restart → 03:15 AM reboot = 60 min.

---

## IBKR module config

`ibkr_module/config/ibkr_config.json`:
```json
{
  "port_paper": 4002,   ← paper Gateway (DUR952126)
  "port_live":  4001,   ← live Gateway  (U28013794)
  "live_account_id":  "U28013794",
  "paper_account_id": "DUR952126"
}
```

Strategies connect to `port_paper` by default (`"paper": true` at top of config). Only the `live_blend` strategy uses `port_live` / `live_account_id`.

---

## Diagnosis checklist

### Gateway not coming up after reboot (port 4001 or 4002 not LISTEN)
1. Check Task Scheduler: `Get-ScheduledTask | Where TaskName -like "*Gateway*"` — is State `Running`?
2. Check IBC log: `C:\IBC\Logs_live\IBC-3.24.2_GATEWAY-1050_<DAY>.txt` (or `Logs_paper`)
3. Look for `autorestart file not found` → 2FA push was missed; approve on IBKR Mobile
4. Look for `ExistingSessionDetectedAction` → scenario 4 (another session won); check for stray ibgateway processes

### 2FA required unexpectedly (outside first-boot)
- TST token expired (check `thts.cache` timestamp vs last auto-restart log)
- Autorestart file was deleted (check `C:\Jts_live\` for the autorestart file)
- Gateway was killed with `taskkill /F` which destroys the token — avoid this

### Paper Gateway connects but gets wrong account
- Verify `config_paper.ini` has `TradingMode=paper` and `OverrideTwsApiPort=4002`
- Verify `StartGatewayPaper.bat` has `set TRADING_MODE=paper` and `set TWS_SETTINGS_PATH=C:\Jts_paper`
- Check that `C:\Jts_paper\jts.ini` has `tradingMode=p` (set by IBC on first login)

### Port conflict (both Gateways fighting over port 4001)
- Live and paper are different IBKR session types and can coexist
- Port conflict = same port configured for both. Check `OverrideTwsApiPort` in both configs

---

## History

| Date | Change |
|------|--------|
| 2026-09-23 | Initial setup. Root cause: Task Scheduler used `StartGateway.bat` → `config.ini` (no AutoRestartTime). Fixed to `StartGatewayLive.bat` → `config_live.ini`. Added AutoRestartTime=02:15 AM, CommandServerPort=7462, blank ColdRestartTime. Created paper Gateway (config_paper.ini, StartGatewayPaper.bat, Task Scheduler task). Watchdog updated with quiet window + graceful STOP. |
