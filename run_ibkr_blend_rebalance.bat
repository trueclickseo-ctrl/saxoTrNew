@echo off
REM IBKR Blend Rebalance -- dry-run signal scan (no orders placed).
REM Run manually with --execute to place orders after reviewing the plan.
python run_ibkr_stocks.py --strategy blend %*
