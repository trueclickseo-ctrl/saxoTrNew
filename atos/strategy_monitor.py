"""Strategy performance monitoring with auto-disable triggers."""

import json
import os
from datetime import datetime, date, timedelta
from dataclasses import dataclass, field

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')
MONITOR_FILE = os.path.join(DATA_DIR, 'strategy_monitor.json')


@dataclass
class MonitorReport:
    strategy_name: str
    status: str = 'ACTIVE'  # ACTIVE, WATCH, PAUSED, DISABLED
    win_rate: float = 0.0
    profit_factor: float = 0.0
    sharpe_30d: float = 0.0
    drawdown_pct: float = 0.0
    consecutive_losses: int = 0
    total_trades: int = 0
    last_review: str = ''
    alerts: list = field(default_factory=list)


class StrategyMonitor:
    """Weekly performance tracking with auto-disable logic."""
    
    def __init__(self):
        self._state = self._load_state()
    
    def _load_state(self) -> dict:
        if os.path.exists(MONITOR_FILE):
            with open(MONITOR_FILE, 'r') as f:
                return json.load(f)
        return {'strategies': {}, 'last_weekly_review': ''}
    
    def _save_state(self):
        # Atomic write so a concurrent reader never sees a truncated file.
        os.makedirs(os.path.dirname(MONITOR_FILE), exist_ok=True)
        tmp = f"{MONITOR_FILE}.tmp.{os.getpid()}"
        with open(tmp, 'w') as f:
            json.dump(self._state, f, indent=2, default=str)
        os.replace(tmp, MONITOR_FILE)
    
    def record_trade(self, strategy_name: str, pnl: float, was_profitable: bool):
        """Record a closed trade for monitoring."""
        if strategy_name not in self._state['strategies']:
            self._state['strategies'][strategy_name] = {
                'trades': [], 'status': 'ACTIVE',
                'peak_equity': 0, 'current_equity': 0,
                'consecutive_losses': 0,
            }
        
        s = self._state['strategies'][strategy_name]
        s['trades'].append({
            'date': date.today().isoformat(),
            'pnl': pnl,
            'profitable': was_profitable,
        })
        
        if was_profitable:
            s['consecutive_losses'] = 0
        else:
            s['consecutive_losses'] = s.get('consecutive_losses', 0) + 1
        
        self._save_state()
    
    def should_disable(self, strategy_name: str) -> tuple[bool, str]:
        """Check if strategy should be paused. Returns (should_pause, reason)."""
        s = self._state['strategies'].get(strategy_name)
        if not s or not s.get('trades'):
            return False, ''
        
        trades = s['trades']
        
        # Trigger 1: 5 consecutive losses
        if s.get('consecutive_losses', 0) >= 5:
            return True, '5 consecutive losses'
        
        # Trigger 2: Win rate below 35% over last 20 trades
        recent = trades[-20:]
        if len(recent) >= 10:
            wins = sum(1 for t in recent if t['profitable'])
            wr = wins / len(recent)
            if wr < 0.35:
                return True, f'Win rate {wr:.0%} < 35% over last {len(recent)} trades'
        
        # Trigger 3: Drawdown > 10%
        equity = 10000  # starting
        peak = equity
        for t in trades:
            equity += t['pnl']
            if equity > peak:
                peak = equity
        dd = (peak - equity) / peak if peak > 0 else 0
        if dd > 0.10:
            return True, f'Drawdown {dd:.1%} > 10%'
        
        # Trigger 4: Sharpe < 0 over last 30 days
        recent_30d = [t for t in trades if t['date'] >= (date.today() - timedelta(days=30)).isoformat()]
        if len(recent_30d) >= 5:
            returns = [t['pnl'] / 10000 for t in recent_30d]  # simplified
            import numpy as np
            if np.std(returns) > 0:
                sharpe = np.mean(returns) / np.std(returns) * np.sqrt(252)
                if sharpe < 0:
                    return True, f'30-day Sharpe {sharpe:.2f} < 0'
        
        return False, ''
    
    def weekly_review(self) -> str:
        """Generate weekly review report for all strategies."""
        lines = []
        lines.append(f"\nATOS Weekly Strategy Report — {date.today().isoformat()}")
        lines.append('═' * 70)
        lines.append(f"{'Strategy':<25} | {'Win%':>5} | {'PF':>5} | {'DD%':>5} | {'Trades':>6} | {'Status':>10}")
        lines.append('─' * 70)
        
        for name, s in self._state.get('strategies', {}).items():
            trades = s.get('trades', [])
            if not trades:
                lines.append(f"{name:<25} | {'---':>5} | {'---':>5} | {'---':>5} | {0:>6} | {'🔒 NEW':>10}")
                continue
            
            wins = sum(1 for t in trades if t['profitable'])
            wr = wins / len(trades) * 100 if trades else 0
            
            gross_p = sum(t['pnl'] for t in trades if t['pnl'] > 0)
            gross_l = abs(sum(t['pnl'] for t in trades if t['pnl'] < 0))
            pf = gross_p / gross_l if gross_l > 0 else gross_p
            
            equity = 10000
            peak = equity
            max_dd = 0
            for t in trades:
                equity += t['pnl']
                if equity > peak:
                    peak = equity
                dd = (peak - equity) / peak if peak > 0 else 0
                if dd > max_dd:
                    max_dd = dd
            
            should_pause, reason = self.should_disable(name)
            if should_pause:
                status = '⛔ PAUSE'
            elif max_dd > 0.07:
                status = '⚠️ WATCH'
            else:
                status = '✅ ACTIVE'
            
            lines.append(f"{name:<25} | {wr:>4.0f}% | {pf:>5.2f} | {max_dd*100:>4.1f}% | {len(trades):>6} | {status:>10}")
            
            if should_pause:
                lines.append(f"  └─ ALERT: {reason}")
        
        lines.append('═' * 70)
        self._state['last_weekly_review'] = date.today().isoformat()
        self._save_state()
        
        return '\n'.join(lines)
