@echo off
REM ATOS Avanza Trail Stops -- ratchet stop-loss orders upward.
REM Runs daily at 21:00 PKT (12:00 ET, midday US session).
REM
REM This is PROTECTIVE only: stops are only ever raised, never lowered.
REM No new positions are opened. Credentials loaded from .env.avanza.
REM
REM Scheduled via setup_scheduler_avanza_trail_stops.ps1 as
REM "ATOS Avanza Trail Stops". Watchdog monitors: avanza_trail_stops.log.

cd /d E:\SaxoTrNew\SaxoTrNew
python run_avanza.py --trail-stops --execute
