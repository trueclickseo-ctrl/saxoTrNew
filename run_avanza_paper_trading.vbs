Set objShell = CreateObject("WScript.Shell")
Set objFSO = CreateObject("Scripting.FileSystemObject")

Dim sRoot, sLog, sCmd
sRoot = "E:\SaxoTrNew\SaxoTrNew"
sLog  = sRoot & "\data\avanza_paper_trading.log"

sCmd = "cmd /c cd /d """ & sRoot & """ && python avanza_paper_trading.py >> """ & sLog & """ 2>&1"
objShell.Run sCmd, 0, True
