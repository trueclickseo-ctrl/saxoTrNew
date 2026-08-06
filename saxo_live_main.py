"""
saxo_live_main.py
-------------------
Run this once per trading day to execute one live-SIM decision cycle.

    python saxo_live_main.py

This is NOT a polling loop — the strategy is daily-bar based, so running it
every 5 minutes would just repeat the same signal all day. Instead, schedule
it to run once, after market close (OMX exchange closes ~17:30 CET) or
before the next open.

SCHEDULING IT (Windows Task Scheduler):
  1. Open Task Scheduler -> Create Basic Task
  2. Trigger: Daily, pick a time after 17:30 CET (e.g. 18:00)
  3. Action: Start a program
     Program: path to your python.exe (find it with `where python`)
     Arguments: saxo_live_main.py
     Start in: this project's folder (e.g. E:\\SaxoTrader\\files)
  4. Finish, then right-click the task -> Run, once, to confirm it works

To halt trading at any time: create an empty file named STOP_TRADING in
this project's root folder. Delete it to resume. Check results/live_order_log.csv
to see every order this cycle placed (or blocked).
"""

from saxo_live_engine import run_cycle

if __name__ == "__main__":
    run_cycle()
