<#
fix_data_permissions.ps1
------------------------
Makes data/, logs/ and saxo_etf_strategy/data/ writable by the current
user REGARDLESS of which process created a given file.

WHY: the scheduled trading tasks run with "highest privileges" (RunLevel
HIGHEST). A file an elevated process creates via os.replace() is owned by
BUILTIN\Administrators and only carries the folder's *inheritable* ACEs.
data/ granted Kwaseem FullControl on the FOLDER but with InheritanceFlags
= None, so that grant never reached the files -- leaving every state /
orders / weights / observation-card file created by a scheduled run
readable-but-not-writable from a normal (non-elevated) shell. A manual
`python forex\runner.py --live` then died with PermissionError on
_save_state(). Confirmed 2026-09-02.

THE FIX (two parts):
  1. Add an INHERITABLE (OI)(CI) Modify ACE for the current user to each
     data folder -> every file created there from now on is writable by
     this user even when an elevated task made it.
  2. Rewrite each currently-locked file in place via a temp + os.replace
     so it picks up the new inherited ACE (icacls can't re-ACL a file the
     non-elevated user has no WRITE_DAC on; delete-child + recreate can).

Safe to re-run any time. Does NOT need elevation (the current user owns
the folders). Files held open by a running task are skipped and reported
-- re-run when that task isn't active, or they self-heal on their next
atomic write.
#>

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$sid = ([Security.Principal.WindowsIdentity]::GetCurrent()).User.Value
$dirs = @('data', 'logs', 'saxo_etf_strategy\data') | Where-Object { Test-Path $_ }

Write-Host "User SID: $sid"
foreach ($d in $dirs) {
    & icacls $d /grant ("*{0}:(OI)(CI)(M)" -f $sid) | Out-Null
    Write-Host "  inheritable Modify ACE -> $d"
}

$exts = '.json', '.jsonl', '.csv', '.log', '.txt', '.xml'
$fixed = 0; $ok = 0; $held = @()
foreach ($d in $dirs) {
    Get-ChildItem $d -Recurse -File | Where-Object { $exts -contains $_.Extension } | ForEach-Object {
        $p = $_.FullName
        try { [IO.File]::Open($p, 'Open', 'ReadWrite').Close(); $ok++; return } catch {}
        try {
            $bytes = [IO.File]::ReadAllBytes($p)
            $tmp = "$p.acltmp"
            [IO.File]::WriteAllBytes($tmp, $bytes)
            [IO.File]::Replace($tmp, $p, $null)
            [IO.File]::Open($p, 'Open', 'ReadWrite').Close()
            $fixed++
        } catch {
            $held += (Resolve-Path -Relative $p)
            if (Test-Path "$p.acltmp") { Remove-Item "$p.acltmp" -Force -ErrorAction SilentlyContinue }
        }
    }
}

Write-Host ""
Write-Host "already writable : $ok"
Write-Host "rewritten        : $fixed"
if ($held.Count) {
    Write-Host "still locked (held open by a running task -- re-run later): $($held.Count)"
    $held | ForEach-Object { Write-Host "  $_" }
} else {
    Write-Host "still locked     : none"
}
