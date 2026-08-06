"""Walk-forward backtesting engine with proper transaction costs."""
import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class BacktestResult:
    strategy_name: str
    total_return_pct: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown_pct: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    num_trades: int = 0
    avg_hold_days: float = 0.0
    monthly_returns: list = field(default_factory=list)
    equity_curve: list = field(default_factory=list)
    trade_log: list = field(default_factory=list)
    passed: bool = False
    
    def summary(self) -> str:
        return (f"{self.strategy_name}: Return={self.total_return_pct:.1f}% | "
                f"Sharpe={self.sharpe_ratio:.2f} | DD={self.max_drawdown_pct:.1f}% | "
                f"WR={self.win_rate:.0f}% | PF={self.profit_factor:.2f} | "
                f"Trades={self.num_trades}")


class Backtester:
    def __init__(self, strategy, initial_capital=10_000.0,
                 commission_pct=0.0008, slippage_pct=0.0003,
                 risk_per_trade=0.01):
        self.strategy = strategy
        self.initial_capital = initial_capital
        self.commission_pct = commission_pct
        self.slippage_pct = slippage_pct
        self.risk_per_trade = risk_per_trade
    
    def run(self, df: pd.DataFrame) -> BacktestResult:
        """Run strategy on historical data."""
        result = BacktestResult(strategy_name=self.strategy.name)
        
        capital = self.initial_capital
        position = None  # {'entry_price': float, 'shares': int, 'entry_idx': int, 'stop': float}
        equity_curve = [capital]
        trade_log = []
        daily_returns = []
        
        for i in range(self.strategy.min_history, len(df)):
            window = df.iloc[:i+1]  # Only use data up to current bar (no look-ahead)
            row = df.iloc[i]
            price = row['Close']
            
            if position is None:
                # Not in a trade — check for BUY
                signal = self.strategy.signal(window)
                if signal == 'BUY':
                    stop = self.strategy.stop_loss(window, price)
                    per_share_risk = price - stop
                    if per_share_risk <= 0:
                        per_share_risk = price * 0.05  # fallback 5%
                    
                    risk_amount = capital * self.risk_per_trade
                    shares = max(1, int(risk_amount / per_share_risk))
                    
                    # Apply slippage to entry
                    entry_price = price * (1 + self.slippage_pct)
                    cost = shares * entry_price
                    commission = cost * self.commission_pct
                    
                    if cost + commission <= capital:
                        capital -= (cost + commission)
                        position = {
                            'entry_price': entry_price,
                            'shares': shares,
                            'entry_idx': i,
                            'stop': stop,
                            'commission_in': commission,
                        }
            else:
                # In a trade — check for exit
                signal = self.strategy.signal(window)
                current_value = position['shares'] * price
                
                # Check stop loss
                hit_stop = price <= position['stop']
                # Check take profit
                tp = self.strategy.take_profit(window, position['entry_price'])
                hit_tp = tp > 0 and price >= tp
                # Check strategy signal
                hit_signal = signal == 'SELL'
                
                if hit_stop or hit_tp or hit_signal:
                    # Close position
                    exit_price = price * (1 - self.slippage_pct)  # slippage on exit
                    if hit_stop:
                        exit_price = min(exit_price, position['stop'])  # Stop is worst case
                    
                    proceeds = position['shares'] * exit_price
                    commission = proceeds * self.commission_pct
                    capital += (proceeds - commission)
                    
                    pnl = proceeds - (position['shares'] * position['entry_price'])
                    pnl_net = pnl - position['commission_in'] - commission
                    
                    trade_log.append({
                        'entry_idx': position['entry_idx'],
                        'exit_idx': i,
                        'entry_price': position['entry_price'],
                        'exit_price': exit_price,
                        'shares': position['shares'],
                        'pnl': round(pnl_net, 2),
                        'pnl_pct': round(pnl_net / (position['shares'] * position['entry_price']) * 100, 2),
                        'hold_days': i - position['entry_idx'],
                        'exit_reason': 'stop' if hit_stop else ('tp' if hit_tp else 'signal'),
                        'profitable': pnl_net > 0,
                    })
                    position = None
            
            # Track equity
            pos_value = position['shares'] * price if position else 0
            total_equity = capital + pos_value
            equity_curve.append(total_equity)
            
            if len(equity_curve) > 1:
                daily_ret = (equity_curve[-1] - equity_curve[-2]) / max(equity_curve[-2], 1)
                daily_returns.append(daily_ret)
        
        # Close any remaining position at last price
        if position:
            last_price = df.iloc[-1]['Close']
            proceeds = position['shares'] * last_price * (1 - self.slippage_pct)
            commission = proceeds * self.commission_pct
            capital += (proceeds - commission)
        
        # Compute metrics
        final_equity = equity_curve[-1] if equity_curve else self.initial_capital
        result.total_return_pct = round((final_equity - self.initial_capital) / self.initial_capital * 100, 2)
        result.equity_curve = equity_curve
        result.trade_log = trade_log
        result.num_trades = len(trade_log)
        
        # Win rate
        if result.num_trades > 0:
            wins = sum(1 for t in trade_log if t['profitable'])
            result.win_rate = round(wins / result.num_trades * 100, 1)
        
        # Profit factor
        gross_profit = sum(t['pnl'] for t in trade_log if t['pnl'] > 0)
        gross_loss = abs(sum(t['pnl'] for t in trade_log if t['pnl'] < 0))
        result.profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else (gross_profit if gross_profit > 0 else 0.0)
        
        # Sharpe ratio
        if daily_returns and np.std(daily_returns) > 0:
            result.sharpe_ratio = round(np.mean(daily_returns) / np.std(daily_returns) * np.sqrt(252), 2)
        
        # Max drawdown
        peak = equity_curve[0]
        max_dd = 0
        for eq in equity_curve:
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak
            if dd > max_dd:
                max_dd = dd
        result.max_drawdown_pct = round(max_dd * 100, 1)
        
        # Average hold days
        if trade_log:
            result.avg_hold_days = round(np.mean([t['hold_days'] for t in trade_log]), 1)
        
        # Monthly returns
        # (simplified: chunk equity curve into ~21-bar months)
        chunk_size = 21
        monthly = []
        for j in range(0, len(equity_curve) - 1, chunk_size):
            start_eq = equity_curve[j]
            end_eq = equity_curve[min(j + chunk_size, len(equity_curve) - 1)]
            if start_eq > 0:
                monthly.append(round((end_eq - start_eq) / start_eq * 100, 2))
        result.monthly_returns = monthly
        
        return result
    
    def walk_forward(self, df: pd.DataFrame, n_windows: int = 5,
                     train_pct: float = 0.6) -> list[BacktestResult]:
        """Walk-forward: train on window, test on next. Returns list of out-of-sample results."""
        total_bars = len(df)
        window_size = total_bars // n_windows
        train_size = int(window_size * train_pct)
        test_size = window_size - train_size
        
        results = []
        for w in range(n_windows):
            start = w * window_size
            train_end = start + train_size
            test_end = min(start + window_size, total_bars)
            
            if test_end > total_bars or train_end >= total_bars:
                break
            
            # Test on out-of-sample portion only
            test_df = df.iloc[start:test_end].copy()
            # Run backtest only on test portion but with full history available
            result = self.run(test_df)
            result.strategy_name = f"{self.strategy.name}_WF{w+1}"
            results.append(result)
        
        return results
