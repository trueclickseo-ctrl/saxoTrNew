"""
CustomRewardEnv — Institutional-Grade Reward Wrapper for RL Training
=====================================================================
Designed to teach a Stable-Baselines3 PPO agent to trade like an
institutional quant desk, NOT a high-frequency gambler.

Penalties modeled after real-world trading friction:
  1. NET PORTFOLIO REWARD:     Change in portfolio value (unrealized + realized)
  2. COMMISSION PENALTY:       0.05% of trade volume (Saxo Bank / IB tiered)
  3. VOLATILITY SLIPPAGE:      ATR-based execution decay (0.02%-0.05%)
  4. CHURNING BLOCK:           -2.0 penalty for rapid open/close flips
  5. REGIME-AWARE SHAPING:     Reward mean-reversion in low-ATR, trend in high-ATR

Usage in Google Colab:
    from custom_reward_env import CustomRewardEnv
    env = CustomRewardEnv(base_env, atr_series, regime_series)
    model = PPO("MlpPolicy", env, verbose=1)
    model.learn(total_timesteps=100_000)
    model.save("institutional_pairs_brain")

Author:  ATOS v3 Upgrade — Agent #4 (Kwaseem session)
Date:    2026-08-06
"""

import numpy as np
import gymnasium as gym
from collections import deque


# ─── Configuration ────────────────────────────────────────────────────────────

COMMISSION_RATE       = 0.0005    # 0.05% per trade volume (Saxo Bank / IB tiered)
SLIPPAGE_BASE_PCT     = 0.0002    # 0.02% base slippage
SLIPPAGE_ATR_SCALE    = 0.0003    # Additional 0.03% at max ATR (total 0.05% at peak vol)
CHURNING_PENALTY      = -2.0      # Flat penalty for opening + closing within window
CHURNING_WINDOW       = 5         # Steps — if trade opened and closed within 5 bars = churn
MIN_HOLD_REWARD_BONUS = 0.01      # Small bonus per step for holding a winning position


class CustomRewardEnv(gym.Wrapper):
    """
    Gym wrapper that transforms raw environment rewards into
    institutional-grade reward signals with real-world friction.

    Actions expected from base_env:
        0 = HOLD  (do nothing)
        1 = BUY   (open long position)
        2 = SELL   (close position / go flat)

    Parameters
    ----------
    env : gym.Env
        Base trading environment (must have .action_space and return
        portfolio value in info dict or as observation).
    atr_lookback : int
        Number of bars to compute ATR (default 14).
    initial_capital : float
        Starting capital for portfolio tracking.
    """

    def __init__(self, env, atr_lookback=14, initial_capital=10_000.0):
        super().__init__(env)
        self.atr_lookback = atr_lookback
        self.initial_capital = initial_capital

        # ── Internal State ──
        self._prev_portfolio_value = initial_capital
        self._portfolio_value      = initial_capital
        self._position             = 0       # 0=flat, 1=long
        self._position_entry_step  = None    # step index when position was opened
        self._step_count           = 0
        self._trade_history        = deque(maxlen=100)  # recent trade timestamps
        self._total_commission     = 0.0
        self._total_slippage       = 0.0
        self._total_churning_pen   = 0.0

        # ATR tracking (rolling High/Low/Close)
        self._highs  = deque(maxlen=atr_lookback + 1)
        self._lows   = deque(maxlen=atr_lookback + 1)
        self._closes = deque(maxlen=atr_lookback + 1)

    def reset(self, **kwargs):
        obs, info = self.env.reset(**kwargs)
        self._prev_portfolio_value = self.initial_capital
        self._portfolio_value      = self.initial_capital
        self._position             = 0
        self._position_entry_step  = None
        self._step_count           = 0
        self._trade_history.clear()
        self._total_commission     = 0.0
        self._total_slippage       = 0.0
        self._total_churning_pen   = 0.0
        self._highs.clear()
        self._lows.clear()
        self._closes.clear()
        return obs, info

    # ── ATR Calculation ───────────────────────────────────────────────────

    def _update_atr(self, high, low, close):
        """Update rolling ATR(14) from OHLC data."""
        self._highs.append(high)
        self._lows.append(low)
        self._closes.append(close)

    def _get_atr(self):
        """Compute current ATR from buffered OHLC data."""
        if len(self._closes) < 2:
            return 0.0

        trs = []
        closes = list(self._closes)
        highs  = list(self._highs)
        lows   = list(self._lows)

        for i in range(1, len(closes)):
            tr = max(
                highs[i] - lows[i],                   # High - Low
                abs(highs[i] - closes[i - 1]),         # High - Prev Close
                abs(lows[i] - closes[i - 1]),          # Low  - Prev Close
            )
            trs.append(tr)

        if not trs:
            return 0.0
        return float(np.mean(trs))

    # ── Penalty Calculations ──────────────────────────────────────────────

    def _commission_penalty(self, trade_volume):
        """
        SAXO BANK COMMISSION: 0.05% of total trade volume.
        Applied immediately when agent changes position (BUY or SELL).
        """
        penalty = trade_volume * COMMISSION_RATE
        self._total_commission += penalty
        return penalty

    def _slippage_penalty(self, trade_volume, atr, price):
        """
        VOLATILITY SLIPPAGE: Dynamic penalty based on ATR.
        Higher ATR = more slippage = worse execution price.

        Formula:
            slippage = trade_volume × (base_pct + atr_scale × (ATR / price))

        At low vol:  0.02% slippage
        At high vol: up to 0.05% slippage
        """
        if price <= 0:
            return 0.0

        atr_ratio = min(atr / price, 0.05)  # Cap ATR/price at 5%
        slippage_pct = SLIPPAGE_BASE_PCT + (SLIPPAGE_ATR_SCALE * atr_ratio * 20)
        # The * 20 scales atr_ratio (typically 0.01-0.03) into meaningful range

        penalty = trade_volume * slippage_pct
        self._total_slippage += penalty
        return penalty

    def _churning_penalty(self):
        """
        CHURNING BLOCK: -2.0 flat penalty if agent opens and closes
        a trade within CHURNING_WINDOW steps.
        Forces the neural network to seek patient, high-confidence
        swing trades instead of high-frequency over-trading.
        """
        if (self._position_entry_step is not None and
                self._step_count - self._position_entry_step < CHURNING_WINDOW):
            self._total_churning_pen += abs(CHURNING_PENALTY)
            return CHURNING_PENALTY
        return 0.0

    def _regime_reward_shaping(self, atr, price, pnl_change):
        """
        REGIME-AWARE REWARD SHAPING:
        - Low ATR (quiet market):  Bonus for mean-reversion captures
        - High ATR (trending):     Bonus for trend-following momentum
        """
        if price <= 0 or atr <= 0:
            return 0.0

        atr_pct = atr / price  # Normalized ATR

        if atr_pct < 0.015:
            # QUIET REGIME: Reward mean-reversion (smaller, more frequent gains)
            if pnl_change > 0:
                return pnl_change * 0.1  # 10% bonus for capturing small reversions
        elif atr_pct > 0.03:
            # TRENDING REGIME: Reward holding through trends
            if self._position == 1 and pnl_change > 0:
                return pnl_change * 0.15  # 15% bonus for riding trends
            if self._position == 0 and pnl_change > 0:
                return -pnl_change * 0.05  # Small penalty for missing trends

        return 0.0

    # ── Main Step Function ────────────────────────────────────────────────

    def step(self, action):
        """
        Execute one step with institutional reward calculation.

        Reward = portfolio_change
               - commission_penalty    (if traded)
               - slippage_penalty      (if traded, scaled by ATR)
               - churning_penalty      (if rapid open/close)
               + regime_shaping        (bonus for correct strategy in regime)
               + hold_bonus            (small reward for holding winners)
        """
        obs, raw_reward, terminated, truncated, info = self.env.step(action)
        self._step_count += 1

        # ── Extract market data from observation/info ──
        # Adapt these indices to match your base environment's observation space
        price = info.get('close_price', info.get('price', obs[0] if len(obs) > 0 else 100.0))
        high  = info.get('high', price * 1.01)
        low   = info.get('low', price * 0.99)

        self._update_atr(high, low, price)
        atr = self._get_atr()

        # ── Portfolio value tracking ──
        self._prev_portfolio_value = self._portfolio_value
        self._portfolio_value = info.get('portfolio_value', self._portfolio_value + raw_reward)

        # ─────────────────────────────────────────────────────────────────
        # 1. NET PORTFOLIO REWARD (baseline)
        # ─────────────────────────────────────────────────────────────────
        pnl_change = self._portfolio_value - self._prev_portfolio_value
        reward = pnl_change

        # ── Detect position changes ──
        prev_position = self._position
        traded = False

        if action == 1 and prev_position == 0:
            # BUY — opening a new position
            self._position = 1
            self._position_entry_step = self._step_count
            traded = True

        elif action == 2 and prev_position == 1:
            # SELL — closing the position
            self._position = 0
            traded = True

        # ── Apply penalties only when trading ──
        if traded:
            trade_volume = abs(price) * info.get('trade_quantity', 1)

            # ─────────────────────────────────────────────────────────────
            # 2. COMMISSION PENALTY: 0.05% of trade volume
            # ─────────────────────────────────────────────────────────────
            comm = self._commission_penalty(trade_volume)
            reward -= comm

            # ─────────────────────────────────────────────────────────────
            # 3. VOLATILITY SLIPPAGE PENALTY: ATR-based execution decay
            # ─────────────────────────────────────────────────────────────
            slip = self._slippage_penalty(trade_volume, atr, price)
            reward -= slip

            # ─────────────────────────────────────────────────────────────
            # 4. CHURNING PENALTY: -2.0 for rapid flips
            # ─────────────────────────────────────────────────────────────
            if action == 2:  # Only on SELL (completing a round-trip)
                churn = self._churning_penalty()
                reward += churn  # churn is negative

            # Track trade for analysis
            self._trade_history.append({
                'step': self._step_count,
                'action': 'BUY' if action == 1 else 'SELL',
                'price': price,
                'atr': atr,
                'commission': comm,
                'slippage': slip,
            })

        # ─────────────────────────────────────────────────────────────────
        # 5. REGIME-AWARE REWARD SHAPING
        # ─────────────────────────────────────────────────────────────────
        regime_bonus = self._regime_reward_shaping(atr, price, pnl_change)
        reward += regime_bonus

        # ─────────────────────────────────────────────────────────────────
        # 6. HOLD BONUS: Small reward for holding a winning position
        # ─────────────────────────────────────────────────────────────────
        if self._position == 1 and pnl_change > 0:
            reward += MIN_HOLD_REWARD_BONUS

        # ── Enrich info dict with penalty breakdown ──
        info['custom_reward'] = {
            'raw_pnl_change':     round(pnl_change, 4),
            'commission':         round(self._total_commission, 4),
            'slippage':           round(self._total_slippage, 4),
            'churning_penalties': round(self._total_churning_pen, 4),
            'regime_bonus':       round(regime_bonus, 4),
            'net_reward':         round(reward, 4),
            'atr':                round(atr, 4),
            'position':           self._position,
            'step':               self._step_count,
            'portfolio_value':    round(self._portfolio_value, 2),
        }

        return obs, reward, terminated, truncated, info

    # ── Utility: Get summary stats ────────────────────────────────────────

    def get_trading_summary(self):
        """Return a summary dict of all penalty accumulations."""
        trades = list(self._trade_history)
        return {
            'total_steps':       self._step_count,
            'total_trades':      len(trades),
            'total_commission':  round(self._total_commission, 4),
            'total_slippage':    round(self._total_slippage, 4),
            'total_churning':    round(self._total_churning_pen, 4),
            'final_portfolio':   round(self._portfolio_value, 2),
            'net_return_pct':    round(
                (self._portfolio_value - self.initial_capital)
                / self.initial_capital * 100, 2
            ),
            'trade_log':         trades,
        }


# ─── Optuna Integration ──────────────────────────────────────────────────────

def optuna_objective(trial, env_fn, total_timesteps=50_000):
    """
    Optuna objective function to optimize PPO hyperparameters.
    Maximizes the final portfolio Sharpe Ratio.

    Usage:
        import optuna
        from stable_baselines3 import PPO

        study = optuna.create_study(direction="maximize")
        study.optimize(
            lambda trial: optuna_objective(trial, make_env),
            n_trials=5
        )
    """
    from stable_baselines3 import PPO

    # Hyperparameters to optimize
    gamma       = trial.suggest_float("gamma", 0.90, 0.999, log=True)
    lr          = trial.suggest_float("learning_rate", 1e-5, 1e-3, log=True)
    n_steps     = trial.suggest_categorical("n_steps", [256, 512, 1024, 2048])
    ent_coef    = trial.suggest_float("ent_coef", 1e-8, 0.1, log=True)
    clip_range  = trial.suggest_float("clip_range", 0.1, 0.4)

    # Create environment
    env = env_fn()

    # Train PPO with trial hyperparameters
    model = PPO(
        "MlpPolicy",
        env,
        gamma=gamma,
        learning_rate=lr,
        n_steps=n_steps,
        ent_coef=ent_coef,
        clip_range=clip_range,
        verbose=0,
    )
    model.learn(total_timesteps=total_timesteps)

    # Evaluate: compute Sharpe Ratio
    obs, info = env.reset()
    daily_returns = []
    prev_value = env.initial_capital

    for _ in range(252):  # ~1 year of trading days
        action, _ = model.predict(obs, deterministic=True)
        obs, reward, done, truncated, info = env.step(action)
        current_value = info.get('custom_reward', {}).get('portfolio_value', prev_value)
        daily_ret = (current_value - prev_value) / max(prev_value, 1)
        daily_returns.append(daily_ret)
        prev_value = current_value
        if done or truncated:
            break

    # Sharpe Ratio = mean(returns) / std(returns) * sqrt(252)
    returns = np.array(daily_returns)
    if len(returns) < 10 or np.std(returns) == 0:
        return -10.0  # Bad trial

    sharpe = (np.mean(returns) / np.std(returns)) * np.sqrt(252)
    return sharpe


# ─── Trade Log Export ─────────────────────────────────────────────────────────

def export_trade_log(env, filename="trading_logs.csv"):
    """
    Export trade history to CSV for Streamlit dashboard.

    Columns: timestamp, asset, action, quantity, strategy, balance
    """
    import csv
    from datetime import datetime

    summary = env.get_trading_summary()
    trades  = summary['trade_log']

    with open(filename, 'a', newline='') as f:
        writer = csv.writer(f)
        # Write header if file is empty
        if f.tell() == 0:
            writer.writerow([
                'timestamp', 'asset', 'action', 'quantity',
                'price', 'atr', 'commission', 'slippage',
                'strategy', 'balance'
            ])

        for trade in trades:
            writer.writerow([
                datetime.now().isoformat(),
                'ATOS_UNIVERSE',
                trade['action'],
                1,  # quantity
                trade['price'],
                trade['atr'],
                trade['commission'],
                trade['slippage'],
                'Institutional StatArb PPO',
                summary['final_portfolio'],
            ])

    print(f"Exported {len(trades)} trades to {filename}")


# ─── Quick Test ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("CustomRewardEnv — Institutional Reward Wrapper")
    print("=" * 50)
    print(f"Commission Rate:    {COMMISSION_RATE * 100:.2f}%")
    print(f"Slippage Base:      {SLIPPAGE_BASE_PCT * 100:.2f}%")
    print(f"Slippage ATR Scale: {SLIPPAGE_ATR_SCALE * 100:.2f}%")
    print(f"Churning Penalty:   {CHURNING_PENALTY}")
    print(f"Churning Window:    {CHURNING_WINDOW} bars")
    print()
    print("To use in Colab:")
    print("  env = CustomRewardEnv(your_base_env)")
    print("  model = PPO('MlpPolicy', env)")
    print("  model.learn(100_000)")
    print("  model.save('institutional_pairs_brain')")
