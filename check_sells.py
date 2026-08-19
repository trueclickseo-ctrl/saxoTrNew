"""
check_sells.py — show open positions and any sell signals today.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import yfinance as yf
from atos import database as db_module
from atos.us_reversion import should_exit, _rsi
from atos.us_momentum import REBAL_DAYS
from datetime import date

trades = db_module.get_open_trades()

print(f"Open positions: {len(trades)}")
if not trades:
    print("No open positions.")
    sys.exit()

tickers = [t["ticker"] for t in trades]
raw = yf.download(tickers if len(tickers) > 1 else tickers,
                  period="3mo", interval="1d",
                  auto_adjust=True, progress=False,
                  group_by="ticker" if len(tickers) > 1 else None)

print()
print(f"{'Ticker':<8}  {'Strategy':<14}  {'Entry':>7}  {'Last':>7}  {'P&L%':>6}  {'Days':>4}  Sell Signal?")
print("-" * 80)

sell_signals = []
hold_signals = []

for trade in trades:
    tk       = trade["ticker"]
    strategy = trade.get("strategy", "?")
    entry    = float(trade.get("entry_price", 0))
    opened   = trade.get("opened_at", "")
    days_held = 0
    if opened:
        try:
            opened_date = date.fromisoformat(str(opened)[:10])
            days_held = (date.today() - opened_date).days
        except Exception:
            pass

    # Get last price
    try:
        if len(tickers) == 1:
            close_s = raw["Close"].dropna()
        else:
            close_s = raw[tk]["Close"].dropna()
        last = float(close_s.iloc[-1])
        pnl_pct = (last - entry) / entry * 100 if entry > 0 else 0
    except Exception:
        last = 0.0
        pnl_pct = 0.0

    # Check sell signal
    signal = ""
    if strategy == "US Reversion":
        try:
            rsi_val = float(_rsi(close_s).iloc[-1])
            sma20   = float(close_s.rolling(20).mean().iloc[-1])
            exit_flag, reason = should_exit(trade, last, rsi_val, sma20, days_held)
            if exit_flag:
                signal = f"SELL — {reason}"
        except Exception as e:
            signal = f"(err: {e})"
    elif strategy == "US Blend":
        # Momentum positions: sell signal = not in current top targets
        # We just flag age here; actual sell decision is at rebalance
        signal = f"hold until rebalance (day {days_held}/{REBAL_DAYS})"

    pnl_sign = "+" if pnl_pct >= 0 else ""
    row = f"{tk:<8}  {strategy:<14}  ${entry:>6.2f}  ${last:>6.2f}  {pnl_sign}{pnl_pct:>5.1f}%  {days_held:>4}d  {signal}"
    print(row)
    if signal.startswith("SELL"):
        sell_signals.append(tk)
    else:
        hold_signals.append(tk)

print()
print(f"Sell signals today: {len(sell_signals)}  {sell_signals if sell_signals else '(none)'}")
print(f"Hold:               {len(hold_signals)}")
