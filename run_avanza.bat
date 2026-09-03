@echo off
REM Avanza ISK sleeve — semi-auto US Blend mirror.
REM
REM Default (no args): dry-run — shows the rebalance plan, places nothing.
REM Pass --execute to enter semi-auto mode: each trade asks y/n/q before
REM placing a real limit order on Avanza.
REM
REM Credentials must be set in .env.avanza (see .env.avanza.example):
REM   AVANZA_USERNAME, AVANZA_PASSWORD, AVANZA_TOTP_SECRET, AVANZA_ACCOUNT_ID
REM
REM Other useful modes:
REM   python run_avanza.py --positions          show current Avanza positions
REM   python run_avanza.py --info               account summary
REM   python run_avanza.py --resolve-tickers AAPL MSFT
REM   python run_avanza.py --dashboard          live refreshing dashboard (Python)

cd /d E:\SaxoTrNew\SaxoTrNew
python -X utf8 run_avanza.py %*
