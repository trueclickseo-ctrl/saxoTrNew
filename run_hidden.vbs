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

' If log path provided, redirect output
If WScript.Arguments.Count >= 2 Then
    logFile = WScript.Arguments(1)
    cmd = "cmd.exe /c """ & cmd & """ >> """ & logFile & """ 2>&1"
Else
    cmd = "cmd.exe /c """ & cmd & """"
End If

' Window style 0 = hidden, False = don't wait (fire-and-forget)
objShell.Run cmd, 0, False

Set objShell = Nothing
