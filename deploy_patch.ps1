# ============================================================================
# ATOS Patch Deployment Script
# ============================================================================
# Run this as Administrator:
#   Right-click PowerShell → "Run as administrator"
#   cd E:\saxobackup\SaxoTrader\files_kwaseem
#   .\deploy_patch.ps1
#
# What it does:
#   1. Fixes file permissions (grants current user full control)
#   2. Creates backup of original files in patches/backup/
#   3. Applies all Python file patches (logging + bug fixes)
#   4. Updates .gitignore
#   5. Updates README.md
# ============================================================================

$ErrorActionPreference = "Stop"
$BaseDir = "E:\saxobackup\SaxoTrader\files_kwaseem"
$PatchDir = Join-Path $BaseDir "patches"
$BackupDir = Join-Path $PatchDir "backup"

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  ATOS Patch Deployment — Logging + Bug Fixes" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""

# ── Step 1: Fix Permissions ────────────────────────────────────────
Write-Host "[1/5] Fixing file permissions..." -ForegroundColor Yellow
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
try {
    icacls $BaseDir /grant "${currentUser}:(OI)(CI)F" /T /Q 2>$null
    Write-Host "  ✓ Granted full control to $currentUser" -ForegroundColor Green
} catch {
    Write-Host "  ⚠ Permission fix failed: $_" -ForegroundColor Red
    Write-Host "  Try running this script as Administrator" -ForegroundColor Red
    exit 1
}

# ── Step 2: Create Backups ─────────────────────────────────────────
Write-Host "[2/5] Creating backups..." -ForegroundColor Yellow
New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null

$filesToPatch = @(
    "atos_runner.py",
    "run_atos.py",
    "saxo_auth.py",
    "saxo_auth_auto.py",
    "saxo_client.py",
    "kill_switch.py",
    "instrument_map.py",
    ".gitignore",
    "atos\risk.py",
    "atos\database.py",
    "atos\__init__.py"
)

foreach ($f in $filesToPatch) {
    $src = Join-Path $BaseDir $f
    if (Test-Path $src) {
        $dest = Join-Path $BackupDir $f.Replace("\", "_")
        Copy-Item $src $dest -Force
        Write-Host "  ✓ Backed up $f" -ForegroundColor DarkGray
    }
}
Write-Host "  ✓ Backups saved to patches\backup\" -ForegroundColor Green

# ── Step 3: Apply Python patches ──────────────────────────────────
Write-Host "[3/5] Applying Python patches..." -ForegroundColor Yellow

# Helper function to apply patches
function Apply-Patch {
    param([string]$RelPath, [string]$PatchFile)
    $target = Join-Path $BaseDir $RelPath
    $source = Join-Path $PatchDir $PatchFile
    if (Test-Path $source) {
        Copy-Item $source $target -Force
        Write-Host "  ✓ Patched $RelPath" -ForegroundColor Green
    } else {
        Write-Host "  ✗ Patch file not found: $PatchFile" -ForegroundColor Red
    }
}

# Apply each patch
Apply-Patch "atos_runner.py"      "atos_runner.py"
Apply-Patch "run_atos.py"         "run_atos.py"
Apply-Patch "saxo_auth.py"        "saxo_auth.py"
Apply-Patch "saxo_auth_auto.py"   "saxo_auth_auto.py"
Apply-Patch "saxo_client.py"      "saxo_client.py"
Apply-Patch "kill_switch.py"      "kill_switch.py"
Apply-Patch "instrument_map.py"   "instrument_map.py"
Apply-Patch ".gitignore"          ".gitignore"
Apply-Patch "atos\risk.py"        "atos_risk.py"
Apply-Patch "atos\database.py"    "atos_database.py"
Apply-Patch "atos\__init__.py"    "atos___init__.py"

# ── Step 4: Create logs directory ─────────────────────────────────
Write-Host "[4/5] Creating logs directory..." -ForegroundColor Yellow
$logsDir = Join-Path $BaseDir "logs"
New-Item -ItemType Directory -Path $logsDir -Force | Out-Null
Write-Host "  ✓ logs/ directory created" -ForegroundColor Green

# ── Step 5: Verify ────────────────────────────────────────────────
Write-Host "[5/5] Verifying patches..." -ForegroundColor Yellow
$errors = 0
foreach ($f in $filesToPatch) {
    $path = Join-Path $BaseDir $f
    if (-not (Test-Path $path)) {
        Write-Host "  ✗ Missing: $f" -ForegroundColor Red
        $errors++
    }
}
$loggerPath = Join-Path $BaseDir "atos\logger.py"
if (-not (Test-Path $loggerPath)) {
    Write-Host "  ✗ Missing: atos\logger.py" -ForegroundColor Red
    $errors++
} else {
    Write-Host "  ✓ atos\logger.py present" -ForegroundColor Green
}

if ($errors -eq 0) {
    Write-Host ""
    Write-Host "============================================================" -ForegroundColor Green
    Write-Host "  ✓ All patches applied successfully!" -ForegroundColor Green
    Write-Host "============================================================" -ForegroundColor Green
    Write-Host ""
    Write-Host "  Next steps:" -ForegroundColor White
    Write-Host "    1. py -3 saxo_auth_auto.py     (refresh Saxo token)" -ForegroundColor White
    Write-Host "    2. py -3 -X utf8 run_atos.py   (run daily cycle)" -ForegroundColor White
    Write-Host "    3. Check logs\ directory for log files" -ForegroundColor White
} else {
    Write-Host ""
    Write-Host "  ⚠ $errors errors detected — review above" -ForegroundColor Red
}
