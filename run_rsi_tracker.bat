@echo off
REM RSI strategy tracker -- SIM + LIVE per-pair W/L/WR/PF/net-P&L, and the
REM explicit winning-vs-losing pair lists for the core "rsi" strategy
REM (forex/strategy_rsi.py -- the one we've run since day 1).
REM
REM Reads data/pnl_ledger.db directly (modules forex / forex_live /
REM forex_live_eur) -- no Saxo call, no torch, no phase-1 gather step.
REM Needs openpyxl, which lives on py -3.12 here (same as the other
REM reports/*_performance_tracker scripts).
REM
REM Writes ONE persistent workbook, overwritten in place each run:
REM   data/rsi_tracker.xlsx
REM Terminal report is printed every run regardless.
REM
REM Read-only analysis -- never touches a live signal, gate, stop, or order.
cd /d E:\SaxoTrNew\SaxoTrNew
py -3.12 reports\rsi_tracker.py
