@echo off
REM Wrapper for the ATOS IBKR Signals Exits scheduled task.
REM Calls run_ibkr_signals.bat --exits so the Task Scheduler action
REM does not need to pass arguments inside a quoted bat path (which
REM cmd.exe treats as a single token and fails to resolve).
call "%~dp0run_ibkr_signals.bat" --exits
