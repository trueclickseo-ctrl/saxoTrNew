@echo off
REM IBKR Live Trail Stops -- ratchet stop-loss orders upward for live ISK account.
REM Runs daily at 21:00 PKT (12:00 ET, midday US session), same window as paper.
REM
REM Requires:
REM   IB Gateway running on port 4001 (live)
REM   IBKR_LIVE_CONFIRMED=1 in User environment
REM
REM Stops only ever raised, never lowered. No new positions placed.

cd /d E:\SaxoTrNew\SaxoTrNew
python run_ibkr_stocks.py --trail-stops --live --execute
