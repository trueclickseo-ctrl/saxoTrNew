@echo off
REM Weekly task — syncs pnl_ledger.db from all 4 module state files.
REM Keeps the ledger current for the K4 export and P&L statement.
cd /d E:\SaxoTrNew\SaxoTrNew
pythonw -X utf8 pnl_tracker.py --sync >> "E:\SaxoTrNew\SaxoTrNew\data\pnl_sync.log" 2>&1
