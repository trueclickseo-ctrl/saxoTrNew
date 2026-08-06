"""
backtest.py
-----------
Simulates the strategy day-by-day across the whole universe, applying the
SAME risk rules a live/paper bot would use: position sizing, stop-losses,
max open positions, and a daily-loss circuit breaker.

This does NOT guarantee future performance. Markets change. Its only job
is to tell us honestly: would this exact rule set have made or lost money
historically, and how bumpy would the ride have been.
"""

import pandas as pd
import numpy as np
import config
from strategy import add_indicators, position_size, commission


class Backtester:
    def __init__(self, universe_data: dict[str, pd.DataFrame]):
        self.universe_data = {t: add_indicators(df) for t, df in universe_data.items()}
        self.capital = config.STARTING_CAPITAL
        self.equity_curve = []
        self.open_positions = {}   # ticker -> dict(shares, entry_price, stop_price)
        self.trade_log = []

    def run(self):
        # Align all tickers on a common set of trading dates
        all_dates = sorted(set.union(*[set(df.index) for df in self.universe_data.values()]))

        for date in all_dates:
            day_start_capital = self.capital + self._open_positions_value(date)
            day_loss_limit = day_start_capital * (1 - config.MAX_DAILY_LOSS_PCT)

            # 1. Manage existing positions: check stops and trend-down exits
            for ticker in list(self.open_positions.keys()):
                self._check_exit(ticker, date)

            # 2. Circuit breaker: stop opening new trades if today's losses are too steep
            current_equity = self.capital + self._open_positions_value(date)
            if current_equity < day_loss_limit:
                self.equity_curve.append((date, current_equity))
                continue

            # 3. Look for new entries
            if len(self.open_positions) < config.MAX_OPEN_POSITIONS:
                self._check_entries(date)

            self.equity_curve.append((date, self.capital + self._open_positions_value(date)))

        return self._summarize()

    def _open_positions_value(self, date):
        value = 0.0
        for ticker, pos in self.open_positions.items():
            df = self.universe_data[ticker]
            price = None
            if date in df.index:
                close = df.loc[date, "Close"]
                if pd.notna(close):
                    price = close
            if price is None:
                price = pos.get("last_known_price", pos["entry_price"])
            else:
                pos["last_known_price"] = price
            value += pos["shares"] * price
        return value

    def _check_exit(self, ticker, date):
        df = self.universe_data[ticker]
        if date not in df.index:
            return
        row = df.loc[date]
        if pd.isna(row["Close"]) or pd.isna(row["Low"]):
            return  # no usable price today — wait for the next valid day
        pos = self.open_positions[ticker]

        hit_stop = row["Low"] <= pos["stop_price"]
        trend_broke = row["cross_down"]

        if hit_stop or trend_broke:
            exit_price = pos["stop_price"] if hit_stop else row["Close"]
            exit_commission = commission(pos["shares"], exit_price)
            proceeds = pos["shares"] * exit_price - exit_commission
            pnl = proceeds - (pos["shares"] * pos["entry_price"] + pos["entry_commission"])
            self.capital += proceeds
            self.trade_log.append({
                "ticker": ticker, "entry_date": pos["entry_date"], "exit_date": date,
                "entry_price": pos["entry_price"], "exit_price": exit_price,
                "shares": pos["shares"], "pnl": pnl,
                "total_commission": pos["entry_commission"] + exit_commission,
                "reason": "stop_loss" if hit_stop else "trend_reversal",
            })
            del self.open_positions[ticker]

    def _check_entries(self, date):
        for ticker, df in self.universe_data.items():
            if ticker in self.open_positions or date not in df.index:
                continue
            row = df.loc[date]
            if not row["cross_up"] or pd.isna(row["atr"]) or pd.isna(row["Close"]):
                continue

            entry_price = row["Close"]
            stop_price = entry_price - config.ATR_STOP_MULTIPLE * row["atr"]
            shares = position_size(self.capital, entry_price, stop_price)
            entry_commission = commission(shares, entry_price)
            cost = shares * entry_price + entry_commission

            if shares > 0 and cost <= self.capital:
                self.capital -= cost
                self.open_positions[ticker] = {
                    "shares": shares, "entry_price": entry_price,
                    "stop_price": stop_price, "entry_date": date,
                    "entry_commission": entry_commission,
                }
                if len(self.open_positions) >= config.MAX_OPEN_POSITIONS:
                    break

    def _summarize(self):
        equity_df = pd.DataFrame(self.equity_curve, columns=["date", "equity"]).set_index("date")
        trades_df = pd.DataFrame(self.trade_log)

        total_return_pct = (equity_df["equity"].iloc[-1] / config.STARTING_CAPITAL - 1) * 100
        running_max = equity_df["equity"].cummax()
        drawdown = (equity_df["equity"] - running_max) / running_max
        max_drawdown_pct = drawdown.min() * 100

        daily_returns = equity_df["equity"].pct_change().dropna()
        sharpe = (daily_returns.mean() / daily_returns.std() * np.sqrt(252)) if daily_returns.std() > 0 else 0

        win_rate = (trades_df["pnl"] > 0).mean() * 100 if not trades_df.empty else 0
        avg_win = trades_df.loc[trades_df["pnl"] > 0, "pnl"].mean() if not trades_df.empty else 0
        avg_loss = trades_df.loc[trades_df["pnl"] <= 0, "pnl"].mean() if not trades_df.empty else 0

        summary = {
            "starting_capital": config.STARTING_CAPITAL,
            "ending_equity": round(equity_df["equity"].iloc[-1], 2),
            "total_return_pct": round(total_return_pct, 2),
            "max_drawdown_pct": round(max_drawdown_pct, 2),
            "sharpe_ratio": round(sharpe, 2),
            "num_trades": len(trades_df),
            "win_rate_pct": round(win_rate, 2),
            "avg_win": round(avg_win, 2) if not pd.isna(avg_win) else 0,
            "avg_loss": round(avg_loss, 2) if not pd.isna(avg_loss) else 0,
        }
        return summary, equity_df, trades_df
