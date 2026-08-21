' run_hidden.vbs
' Generic silent launcher for ATOS scheduled tasks.
' Runs any bat file or command with window style 0 (completely hidden).
'
' Usage (Task Scheduler action):
'   Command : wscript.exe
'   Arguments: "E:\SaxoTrNew\SaxoTrNew\run_hidden.vbs" "cmd-or-bat" ["log-file"]
'
' If a log file path is given, stdout+stderr are appended to it.
' The calling wscript.exe window itself never appears (wscript has no console).

Dim objShell, cmd, logFile

Set objShell = CreateObject("WScript.Shell")

If WScript.Arguments.Count < 1 Then
    WScript.Quit 1
End If

cmd = WScript.Arguments(0)

' If the command starts with "python " (case-insensitive), replace with pythonw
' so the launched process has no console window at all (pythonw = windowless Python)
Dim lc : lc = LCase(cmd)
If Left(lc, 7) = "python " Then
    cmd = "pythonw" & Mid(cmd, 7)
ElseIf Left(lc, 10) = "python.exe" Then
    cmd = "pythonw.exe" & Mid(cmd, 11)
End If

' If log path provided, redirect output.
'
' Quoting note: cmd.exe's "/c" quote-stripping only preserves quotes cleanly
' when the whole argument has EXACTLY ONE quoted segment. With a quoted
' command path AND a quoted log path (two separate segments), cmd.exe falls
' back to blindly stripping the first and last quote characters in the
' string, which mangles the command name (leaves a stray trailing quote) and
' breaks the redirection — the whole thing silently fails with exit code 1
' and never runs. Fix: wrap the ENTIRE "cmd >> log 2>&1" expression in one
' more outer quote pair so there's only one top-level quoted segment.
' (Empirically verified against real cmd.exe: without the extra wrap this
' returns rc=1 and no output; with it, rc=0 and correct output.)
Dim q : q = Chr(34)
Dim rawCmd : rawCmd = cmd  ' keep the un-redirected command for the no-log fallback below

Function WithLog(baseCmd, target)
    WithLog = "cmd.exe /c " & q & q & baseCmd & q & " >> " & q & target & q & " 2>&1" & q
End Function

If WScript.Arguments.Count >= 2 Then
    logFile = WScript.Arguments(1)
    cmd = WithLog(rawCmd, logFile)
Else
    cmd = "cmd.exe /c " & q & rawCmd & q
End If

' Window style 0 = hidden, True = wait for completion.
' MUST wait: Task Scheduler tracks wscript.exe's own exit, not the child's.
' A fire-and-forget launch (False) makes wscript.exe return instantly, so
' Task Scheduler reports success before the real command has even started —
' and the detached child is then torn down with no output ever written
' (confirmed live: 2026-08-19 18:00 and 2026-08-20 06:20 forex runs both
' silently no-opped this way while showing LastTaskResult=0).
'
' MUST propagate the exit code: without this, wscript.exe always exits 0
' itself regardless of what objShell.Run returned, so Task Scheduler's
' LastTaskResult is a false positive even when the inner command failed or
' never ran (confirmed: LBO London Open showed LastTaskResult=0 the same day
' NumberOfMissedRuns incremented and no fresh log line was written).
' Retry on failure: a locked log file (another process still holding it
' open, e.g. a hung prior run whose enclosing cmd.exe never exited --
' confirmed live 2026-08-21/22, every scheduled futures run failed at the
' ">> logfile" redirect step with "The process cannot access the file
' because it is being used by another process", while wscript.exe itself
' still reported success) can be transient (AV scan, backup tool, a
' monitor script's read) or persistent (a genuinely stuck prior instance
' that this account has no permission to kill). A short retry costs
' nothing on the transient case; it won't fix the persistent case, but
' the fallbacks below do.
Dim rc, attempt
attempt = 1
rc = objShell.Run(cmd, 0, True)
Do While rc <> 0 And attempt < 3
    WScript.Sleep 5000
    attempt = attempt + 1
    rc = objShell.Run(cmd, 0, True)
Loop

' Still failing after retries and a log path was requested: the redirect
' target itself is the problem (persistently locked), not the underlying
' command. A dead log file must never be allowed to block the actual
' trading logic from running -- fall back to a sibling ".fallback" path
' first (so there's still SOMEWHERE to see what happened), and if even
' that fails, run with no log redirect at all rather than give up.
If rc <> 0 And logFile <> "" Then
    Dim fallbackLog : fallbackLog = logFile & ".fallback"
    rc = objShell.Run(WithLog(rawCmd, fallbackLog), 0, True)
    If rc <> 0 Then
        rc = objShell.Run("cmd.exe /c " & q & rawCmd & q, 0, True)
    End If
End If

Set objShell = Nothing
WScript.Quit rc
