"""
backtest_cfd.py
----------------
Same trend-following logic as backtest.py, adapted for CFD indices:
  - Uses position_size_cfd() (margin-aware) instead of position_size()
  - Charges daily financing cost on open positions (CFDs aren't free to
    hold overnight the way owning shares is)
  - Everything else — stops, trend-reversal exits, circuit breaker, max
    open positions — works the same way as the stock backtester.

Kept as a SEPARATE class from Backtester in backtest.py on purpose: mixing
share-based and CFD-based accounting in one loop is a good way to introduce
subtle bugs. Run them side by side, compare results, combine later once
both are individually trustworthy.

Reminder: config.CFD_MARGIN_RATE and config.CFD_ANNUAL_FINANCING_RATE are
placeholder estimates. Verify real values in SaxoTraderGO before trusting
these numbers as anything more than directional.
"""

import pandas as pd
import numpy as np
import config
from strategy import add_indicators, position_size_cfd


class CFDBacktester:
    def __init__(self, universe_data: dict[str, pd.DataFrame]):
        self.universe_data = {t: add_indicators(df) for t, df in universe_data.items()}
        self.capital = config.STARTING_CAPITAL
        self.equity_curve = []
        self.open_positions = {}   # label -> dict(contracts, entry_price, stop_price, entry_date)
        self.trade_log = []
        self.daily_financing_charged = 0.0  # running total, for visibility in the summary

    def run(self):
        all_dates = sorted(set.union(*[set(df.index) for df in self.universe_data.values()]))

        for date in all_dates:
            day_start_capital = self.capital + self._open_positions_value(date)
            day_loss_limit = day_start_capital * (1 - config.MAX_DAILY_LOSS_PCT)

            # 1. Charge overnight financing on anything still open from yesterday
            self._charge_financing()

            # 2. Manage existing positions: stops and trend-down exits
            for label in list(self.open_positions.keys()):
                self._check_exit(label, date)

            # 3. Circuit breaker
            current_equity = self.capital + self._open_positions_value(date)
            if current_equity < day_loss_limit:
                self.equity_curve.append((date, current_equity))
                continue

            # 4. New entries
            if len(self.open_positions) < config.MAX_OPEN_POSITIONS:
                self._check_entries(date)

            self.equity_curve.append((date, self.capital + self._open_positions_value(date)))

        return self._summarize()

    def _charge_financing(self):
        """
        Daily financing cost on notional value of every open position.
        Simplified: charges every day including weekends (real Saxo
        financing typically charges 3x on Wednesdays to cover the weekend
        instead — this is a simplification worth refining once the basic
        model is validated).
        """
        daily_rate = config.CFD_ANNUAL_FINANCING_RATE / 365
        for label, pos in self.open_positions.items():
            notional = pos["contracts"] * pos.get("last_known_price", pos["entry_price"])
            charge = notional * daily_rate
            self.capital -= charge
            self.daily_financing_charged += charge

    def _open_positions_value(self, date):
        """
        Mark-to-market value of open positions = margin held as collateral
        + unrealized P&L. NOT full notional — that capital was never spent,
        only the margin fraction was, so only margin (plus/minus how the
        trade has moved) belongs in the equity total.
        """
        value = 0.0
        for label, pos in self.open_positions.items():
            df = self.universe_data[label]
            price = None
            if date in df.index:
                close = df.loc[date, "Close"]
                if pd.notna(close):
                    price = close
            if price is None:
                price = pos.get("last_known_price", pos["entry_price"])
            else:
                pos["last_known_price"] = price
            unrealized_pnl = pos["contracts"] * (price - pos["entry_price"])
            value += pos["margin"] + unrealized_pnl
        return value

    def _check_exit(self, label, date):
        df = self.universe_data[label]
        if date not in df.index:
            return
        row = df.loc[date]
        if pd.isna(row["Close"]) or pd.isna(row["Low"]):
            return
        pos = self.open_positions[label]

        hit_stop = row["Low"] <= pos["stop_price"]
        trend_broke = row["cross_down"]

        if hit_stop or trend_broke:
            exit_price = pos["stop_price"] if hit_stop else row["Close"]
            notional_pnl = pos["contracts"] * (exit_price - pos["entry_price"])
            exit_commission = pos["contracts"] * exit_price * config.COMMISSION_PCT
            # entry_commission was already deducted from capital when the
            # trade opened — don't subtract it again here, just report it
            # in the trade log so pnl reflects the true round-trip cost.
            pnl = notional_pnl - pos["entry_commission"] - exit_commission
            # Release the margin that was held, plus/minus what the trade
            # made or lost since entry (minus the exit commission we're
            # paying right now — entry commission is already accounted for
            # in self.capital from when the position was opened).
            self.capital += pos["margin"] + notional_pnl - exit_commission
            self.trade_log.append({
                "label": label, "entry_date": pos["entry_date"], "exit_date": date,
                "entry_price": pos["entry_price"], "exit_price": exit_price,
                "contracts": pos["contracts"], "pnl": pnl,
                "reason": "stop_loss" if hit_stop else "trend_reversal",
            })
            del self.open_positions[label]

    def _check_entries(self, date):
        for label, df in self.universe_data.items():
            if label in self.open_positions or date not in df.index:
                continue
            row = df.loc[date]
            if not row["cross_up"] or pd.isna(row["atr"]) or pd.isna(row["Close"]):
                continue

            entry_price = row["Close"]
            stop_price = entry_price - config.ATR_STOP_MULTIPLE * row["atr"]
            contracts = position_size_cfd(self.capital, entry_price, stop_price)

            margin = contracts * entry_price * config.CFD_MARGIN_RATE
            entry_commission = contracts * entry_price * config.COMMISSION_PCT
            cost = margin + entry_commission

            if contracts > 0 and cost <= self.capital:
                self.capital -= cost   # THE FIX: actually spend the margin, don't just check it
                self.open_positions[label] = {
                    "contracts": contracts, "entry_price": entry_price,
                    "stop_price": stop_price, "entry_date": date,
                    "margin": margin, "entry_commission": entry_commission,
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
            "total_financing_charged": round(self.daily_financing_charged, 2),
        }
        return summary, equity_df, trades_df
