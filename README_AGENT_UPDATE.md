# ATOS — Agent Handover Update (2026-08-04)

> [!IMPORTANT]
> **HANDOVER DOCUMENTATION ORDER**
> Read the main `README.md` **FIRST** for foundational system context, architecture, and design specifications, then read this file for the latest changes and operational state.

---

## What Changed

- **Localhost Dashboard**: The ATOS monitoring dashboard has been transitioned to run on `localhost` only. Previously, dashboard static files were uploaded via FTP to `namazic.com`.
- **Standalone Web Server (`atos_dashboard.py`)**: Created a standalone Python HTTP server that dynamically serves an interactive, premium dashboard UI at `http://localhost:8070` by reading directly from `data/atos.db`.
- **UI Theme Support**: Added a Dark / Light theme toggle with automatic persistence via browser local storage.
- **Local Runner Wrapper (`run_atos.py`)**: Created a clean execution wrapper to run the daily cycle locally without performing remote FTP uploads. Auto-checks/refreshes Saxo token before each run.
- **Automatic Saxo Login (`saxo_auth_auto.py`)**: Replaced manual copy-paste OAuth flow with fully automatic PKCE login. Runs a temporary callback server on port 8071, catches the Saxo redirect, exchanges code for tokens automatically. Shows a premium success/error page in the browser.
- **Permission Fix Script (`fix_permissions.bat`)**: Provided an administrator script to resolve Windows file ownership and access control issues across the repository.

---

## New Files Added

| File Name | Description |
| :--- | :--- |
| `atos_dashboard.py` | Standalone local HTTP dashboard web server serving at `http://localhost:8070`. Dark/light theme toggle. |
| `saxo_auth_auto.py` | Automatic OAuth PKCE login — no manual URL copy-paste. Callback server on port 8071. |
| `run_atos.py` | Daily execution runner wrapper (auto-auth + bypass FTP + local dashboard). |
| `fix_permissions.bat` | One-time administrative batch script to grant full write permissions to user `Kashif`. |
| `README_DASHBOARD.md` | Comprehensive documentation for dashboard setup, sections, and REST API endpoints. |
| `README_AGENT_UPDATE.md` | Handover documentation tracking recent changes, state, and task lists (this file). |
| `atos_runner_new.py` | Modified runner script with local mode support. *(Can be deleted once `fix_permissions.bat` is run and original `atos_runner.py` is overwritten).* |

---

## Port Usage

| Port | Used By | Purpose |
| :--- | :--- | :--- |
| **8070** | `atos_dashboard.py` | Dashboard web server (persistent, runs in foreground) |
| **8071** | `saxo_auth_auto.py` | Temporary OAuth callback server (starts on login, shuts down after) |

---

## Saxo OAuth Authentication

### Old flow (saxo_auth.py — still works)
1. Run `py -3 saxo_auth.py`
2. Browser opens Saxo login
3. After login, browser redirects to localhost — page fails to load
4. Manually copy the URL from browser address bar
5. Paste it into the terminal

### New flow (saxo_auth_auto.py — recommended)
1. Run `py -3 saxo_auth_auto.py`
2. Browser opens Saxo login
3. After login, local server catches the redirect automatically
4. Browser shows a "Login Successful!" page
5. Tokens saved — done

### Redirect URI Setup (one-time)
The new auto-login uses `http://localhost:8071/redirect` as the redirect URI. This must be registered in the Saxo developer portal:
1. Go to https://developer.saxobank.com/
2. Open your app settings → Edit
3. Add `http://localhost:8071/redirect` to the Redirect URLs list
4. Keep the existing `https://localhost/redirect` as well (for backward compatibility)

### Token Auto-Refresh
- `run_atos.py` automatically checks and refreshes the token before each daily cycle
- `saxo_client.py` handles transparent refresh during API calls
- Tokens saved in `saxo_token.json` (gitignored, local only)

---

## File Permission Issue

- **Root Cause**: Repository files were created under user account `SEO` (original developer). The active Windows session user `Kashif` currently inherits Read + Execute permissions via `BUILTIN\Users`.
- **Operational Impact**: New file creation succeeds, but modifying pre-existing files directly will fail with permission errors until permissions are reset.
- **Resolution Procedure**:
  1. Locate `fix_permissions.bat` in the workspace directory.
  2. Right-click `fix_permissions.bat` and select **Run as administrator** (one-time requirement).
  3. Upon completion, full write access to all existing project files is granted to `Kashif`.

---

## Current System State

- **Database (`data/atos.db`)**: All 6 schema tables exist, but all tables are currently **EMPTY (0 rows)**. The initial daily cycle has not yet executed.
- **Execution History**: The daily runner has never been executed; there are no active positions, trade logs, or historical equity data.
- **Authentication**: Saxo SIM tokens are **EXPIRED** (~20+ hours ago as of 2026-08-04 02:00 PKT). Run `py -3 saxo_auth_auto.py` to re-authenticate.
- **Instrument Mapping**: ATOS trading universe tickers have not yet been mapped to Saxo Universal Instrument Codes (UICs). `lookup_instruments.py` must be run prior to trading.

---

## How to Run Now

1. **Fix Permissions** *(One-time step)*:
   Right-click `fix_permissions.bat` → **Run as administrator**.
2. **Saxo Login** *(When token expired)*:
   ```cmd
   py -3 saxo_auth_auto.py
   ```
   Browser opens, you log in, tokens saved automatically.
3. **Launch Dashboard**:
   ```cmd
   py -3 atos_dashboard.py
   ```
   Open `http://localhost:8070` in your web browser.
4. **Execute Daily Trading Cycle (Local)**:
   ```cmd
   py -3 -X utf8 run_atos.py
   ```
   This auto-checks token, runs the cycle, and launches the dashboard.
5. **Execute Daily Trading Cycle (Legacy with FTP)**:
   ```cmd
   py -3 -X utf8 atos_runner.py
   ```

---

## Priority Tasks (Updated)

1. **Fix File Permissions**: Execute `fix_permissions.bat` with elevated Administrator privileges.
2. **Register Redirect URI**: Add `http://localhost:8071/redirect` to your Saxo SIM app in the developer portal.
3. **Authenticate**: Run `py -3 saxo_auth_auto.py` to get fresh tokens.
4. **Instrument Lookup**: Run `lookup_instruments.py` to map trading universe tickers to Saxo UICs.
5. **First Daily Run**: Execute `py -3 -X utf8 run_atos.py` to populate initial market signals.
6. **Dashboard Verification**: Validate rendering at `http://localhost:8070`.

---

## Technical Guidance for Other Agents

- **Documentation**: Always consult `README.md` first, followed by `README_AGENT_UPDATE.md` and `README_DASHBOARD.md`.
- **Compatibility**: All newly created files integrate seamlessly with the existing codebase, importing common modules and pointing to `data/atos.db`.
- **Core Package**: The `atos/` directory contains core algorithm logic (detectors, portfolio management, risk engine). **Do NOT modify `atos/` core files unless explicitly requested.**
- **Python Executable**: Use `py -3` as the standard launcher command on this host environment (`python` may map differently).
- **Encoding Flag**: Always include `-X utf8` flag when running scripts printing Unicode characters or terminal indicators (e.g., `py -3 -X utf8 <script>.py`).
- **Auth Files**: `saxo_auth_auto.py` (auto) and `saxo_auth.py` (manual) both write to the same `saxo_token.json`. Use whichever works.
- **Version Control**: Git is initialized, but the git index may require ownership repair or reset from the `SEO` user environment if index locking occurs.

---
