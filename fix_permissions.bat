@echo off
REM ================================================
REM Fix Permissions — Run as Administrator
REM ================================================
REM This script grants full access to ALL users on this PC
REM for both the original files/ and the kwaseem clone.
REM
REM Run this by right-clicking → "Run as Administrator"
REM ================================================

echo Fixing ownership...
takeown /f "e:\saxobackup\SaxoTrader\files" /r /d y
takeown /f "e:\saxobackup\SaxoTrader\files_kwaseem" /r /d y

echo.
echo Granting full permissions to Everyone...
icacls "e:\saxobackup\SaxoTrader\files" /grant Everyone:(OI)(CI)F /T /Q /C
icacls "e:\saxobackup\SaxoTrader\files_kwaseem" /grant Everyone:(OI)(CI)F /T /Q /C

echo.
echo Also fixing git safe.directory...
git config --global --add safe.directory e:/saxobackup/SaxoTrader/files
git config --global --add safe.directory e:/saxobackup/SaxoTrader/files_kwaseem

echo.
echo ================================================
echo Done! All users and Claude agents can now read/write.
echo ================================================
pause
