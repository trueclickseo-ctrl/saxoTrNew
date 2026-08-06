from abc import ABC, abstractmethod
import pandas as pd
import numpy as np

class Strategy(ABC):
    """Base class for all ATOS trading strategies."""
    name: str = 'base'
    version: str = '1.0'
    description: str = ''
    asset_classes: list = ['US Equities', 'OMX30', 'DAX40']
    min_history: int = 50  # minimum bars needed
    
    def __init__(self, **param_overrides):
        """Initialize with default parameters, allowing overrides."""
        self.params = {**self.default_parameters()}
        self.params.update(param_overrides)
    
    @abstractmethod
    def default_parameters(self) -> dict:
        """Return dict of parameter_name -> default_value."""
        return {}
    
    @abstractmethod
    def signal(self, df: pd.DataFrame) -> str:
        """Analyze DataFrame, return 'BUY', 'SELL', or 'HOLD'.
        df has all features from features.add_all() plus OHLCV."""
        return 'HOLD'
    
    @abstractmethod
    def stop_loss(self, df: pd.DataFrame, entry_price: float) -> float:
        """Calculate stop-loss price given current data and entry."""
        return 0.0
    
    def take_profit(self, df: pd.DataFrame, entry_price: float) -> float:
        """Optional take-profit. Return 0.0 for no TP (default)."""
        return 0.0
    
    def confidence(self, df: pd.DataFrame) -> float:
        """Return confidence 0.0 to 1.0. Higher = more certain."""
        return 0.5
    
    def __repr__(self):
        return f'<Strategy: {self.name} v{self.version}>'

class S1_DetectorScore(Strategy):
    name = 'detector_score_v2'
    version = '2.0'
    description = 'ATOS v2 8-detector weighted scoring system'
    
    def default_parameters(self):
        return {'buy_threshold': 55, 'exit_threshold': 20}
    
    def signal(self, df):
        # Import and use existing decision engine
        from .decision_engine import evaluate
        if len(df) < self.min_history:
            return 'HOLD'
        row = df.iloc[-1]
        # Use default market group since we don't know it here
        decision = evaluate(row, 'US Equities')
        return decision.action  # BUY, EXIT, or HOLD
    
    def stop_loss(self, df, entry_price):
        atr = df.iloc[-1].get('atr', 0)
        if pd.notna(atr) and atr > 0:
            return entry_price - 2.5 * atr
        return entry_price * 0.95
    
    def confidence(self, df):
        from .decision_engine import evaluate
        row = df.iloc[-1]
        decision = evaluate(row, 'US Equities')
        return min(abs(decision.score) / 100.0, 1.0)

class S2_DualEMA(Strategy):
    name = 'dual_ema_crossover'
    version = '1.0'
    description = 'EMA20/50 crossover with ADX trend confirmation'
    min_history = 55
    
    def default_parameters(self):
        return {'fast_period': 20, 'slow_period': 50, 'adx_min': 20}
    
    def signal(self, df):
        if len(df) < self.min_history:
            return 'HOLD'
        row = df.iloc[-1]
        prev = df.iloc[-2]
        
        ema_fast = row.get('ema20', None)
        ema_slow = row.get('ema50', None)
        prev_fast = prev.get('ema20', None)
        prev_slow = prev.get('ema50', None)
        adx = row.get('adx', 0)
        
        if any(pd.isna(v) for v in [ema_fast, ema_slow, prev_fast, prev_slow]):
            return 'HOLD'
        
        # BUY: EMA20 crosses above EMA50, ADX confirms trend
        if prev_fast <= prev_slow and ema_fast > ema_slow and adx > self.params['adx_min']:
            return 'BUY'
        
        # SELL: EMA20 crosses below EMA50
        if prev_fast >= prev_slow and ema_fast < ema_slow:
            return 'SELL'
        
        return 'HOLD'
    
    def stop_loss(self, df, entry_price):
        atr = df.iloc[-1].get('atr', 0)
        if pd.notna(atr) and atr > 0:
            return entry_price - 2.5 * atr
        return entry_price * 0.95
    
    def confidence(self, df):
        adx = df.iloc[-1].get('adx', 0)
        if pd.isna(adx):
            return 0.3
        return min(adx / 50.0, 1.0)  # ADX 50+ = max confidence

class S3_MeanReversion(Strategy):
    name = 'rsi_mean_reversion'
    version = '1.0'
    description = 'RSI oversold bounce off lower Bollinger Band in ranging markets'
    asset_classes = ['US Equities', 'OMX30', 'DAX40']  # Not Forex/Commodities
    min_history = 30
    
    def default_parameters(self):
        return {
            'rsi_oversold': 30, 'rsi_overbought': 70,
            'bb_period': 20, 'bb_std': 2.0,
            'adx_max': 25  # Only works in ranging markets
        }
    
    def signal(self, df):
        if len(df) < self.min_history:
            return 'HOLD'
        row = df.iloc[-1]
        rsi = row.get('rsi', 50)
        adx = row.get('adx', 30)
        close = row.get('Close', 0)
        bb_lower = row.get('bb_lower', 0)
        bb_upper = row.get('bb_upper', 0)
        
        if any(pd.isna(v) for v in [rsi, adx, close, bb_lower, bb_upper]):
            return 'HOLD'
        
        # Only in ranging markets
        if adx > self.params['adx_max']:
            return 'HOLD'
        
        # BUY: RSI oversold + price near lower BB
        if rsi < self.params['rsi_oversold'] and close <= bb_lower * 1.01:
            return 'BUY'
        
        # SELL: RSI overbought or price at upper BB
        if rsi > self.params['rsi_overbought'] or close >= bb_upper * 0.99:
            return 'SELL'
        
        return 'HOLD'
    
    def stop_loss(self, df, entry_price):
        bb_lower = df.iloc[-1].get('bb_lower', entry_price * 0.95)
        atr = df.iloc[-1].get('atr', 0)
        if pd.notna(bb_lower) and pd.notna(atr) and atr > 0:
            return bb_lower - 0.5 * atr
        return entry_price * 0.95
    
    def take_profit(self, df, entry_price):
        bb_upper = df.iloc[-1].get('bb_upper', 0)
        if pd.notna(bb_upper) and bb_upper > entry_price:
            return bb_upper
        return entry_price * 1.05
    
    def confidence(self, df):
        rsi = df.iloc[-1].get('rsi', 50)
        if pd.isna(rsi):
            return 0.3
        # More oversold = more confident
        if rsi < 20:
            return 0.9
        elif rsi < 30:
            return 0.7
        return 0.4

class S4_BreakoutVol(Strategy):
    name = 'breakout_volatility'
    version = '1.0'
    description = 'Donchian breakout with ATR expansion and volume surge'
    min_history = 25
    
    def default_parameters(self):
        return {
            'entry_period': 20, 'exit_period': 10,
            'vol_expansion': 1.1,  # ATR must be 10% above yesterday
            'volume_threshold': 1.5  # Volume 50% above average
        }
    
    def signal(self, df):
        if len(df) < self.min_history:
            return 'HOLD'
        row = df.iloc[-1]
        prev = df.iloc[-2]
        close = row.get('Close', 0)
        donchian_high = row.get('donchian_high', 0)
        donchian_low_10 = df['Low'].rolling(self.params['exit_period']).min().iloc[-1]
        atr = row.get('atr', 0)
        prev_atr = prev.get('atr', 0)
        vol_ratio = row.get('vol_ratio', 1.0)
        
        if any(pd.isna(v) for v in [close, donchian_high, atr, prev_atr]):
            return 'HOLD'
        
        # BUY: Price breaks Donchian high + ATR expanding + volume surge
        atr_expanding = atr > prev_atr * self.params['vol_expansion'] if prev_atr > 0 else False
        volume_ok = pd.notna(vol_ratio) and vol_ratio > self.params['volume_threshold']
        
        if close >= donchian_high and atr_expanding and volume_ok:
            return 'BUY'
        
        # SELL: Price breaks 10-day low
        if pd.notna(donchian_low_10) and close <= donchian_low_10:
            return 'SELL'
        
        return 'HOLD'
    
    def stop_loss(self, df, entry_price):
        atr = df.iloc[-1].get('atr', 0)
        if pd.notna(atr) and atr > 0:
            return entry_price - 2.0 * atr
        return entry_price * 0.95
    
    def confidence(self, df):
        vol_ratio = df.iloc[-1].get('vol_ratio', 1.0)
        if pd.isna(vol_ratio):
            return 0.4
        return min(vol_ratio / 3.0, 1.0)  # Vol ratio 3x = max confidence

class S5_MomentumAccel(Strategy):
    name = 'momentum_acceleration'
    version = '1.0'
    description = 'Catches accelerating momentum in the sweet spot (building, not exhausted)'
    min_history = 25
    
    def default_parameters(self):
        return {
            'roc_threshold': 3.0,  # ROC-10 must be > 3%
            'rsi_min': 40, 'rsi_max': 65,  # Sweet spot
            'decel_days': 3  # Exit after 3 days of deceleration
        }
    
    def signal(self, df):
        if len(df) < self.min_history:
            return 'HOLD'
        row = df.iloc[-1]
        roc_10 = row.get('roc_10', 0)
        mom_accel = row.get('mom_acceleration', 0)
        rsi = row.get('rsi', 50)
        
        if any(pd.isna(v) for v in [roc_10, mom_accel, rsi]):
            return 'HOLD'
        
        # BUY: ROC strong + accelerating + RSI in sweet spot
        if (roc_10 > self.params['roc_threshold'] and
            mom_accel > 0 and
            self.params['rsi_min'] < rsi < self.params['rsi_max']):
            return 'BUY'
        
        # SELL: ROC turned negative or decelerating for N days
        if roc_10 < 0:
            return 'SELL'
        
        # Check for multi-day deceleration
        decel_count = 0
        for i in range(1, min(self.params['decel_days'] + 1, len(df))):
            past_accel = df.iloc[-i].get('mom_acceleration', 0)
            if pd.notna(past_accel) and past_accel < 0:
                decel_count += 1
        if decel_count >= self.params['decel_days']:
            return 'SELL'
        
        return 'HOLD'
    
    def stop_loss(self, df, entry_price):
        atr = df.iloc[-1].get('atr', 0)
        if pd.notna(atr) and atr > 0:
            return entry_price - 2.0 * atr
        return entry_price * 0.96
    
    def confidence(self, df):
        roc = df.iloc[-1].get('roc_10', 0)
        accel = df.iloc[-1].get('mom_acceleration', 0)
        if pd.isna(roc) or pd.isna(accel):
            return 0.3
        # Strong ROC + accelerating = high confidence
        score = min(roc / 10.0, 0.5) + (0.5 if accel > 0 else 0.0)
        return min(max(score, 0.0), 1.0)

class S6_SmartMoney(Strategy):
    name = 'smart_money_accumulation'
    version = '1.0'
    description = 'Detects institutional accumulation via OBV trend + VWAP support'
    min_history = 30
    
    def default_parameters(self):
        return {
            'obv_rising_days': 5,  # OBV must be rising for 5+ days
            'obv_falling_days': 3,  # Exit after 3 days of OBV falling
        }
    
    def signal(self, df):
        if len(df) < self.min_history:
            return 'HOLD'
        row = df.iloc[-1]
        close = row.get('Close', 0)
        vwap = row.get('vwap', 0)
        obv_rising = row.get('obv_rising', False)
        vol_ratio = row.get('vol_ratio', 1.0)
        
        if any(pd.isna(v) for v in [close, vwap]):
            return 'HOLD'
        
        # Count consecutive OBV rising days
        rising_count = 0
        for i in range(1, min(self.params['obv_rising_days'] + 2, len(df))):
            if df.iloc[-i].get('obv_rising', False):
                rising_count += 1
            else:
                break
        
        # BUY: OBV rising for N days + price above VWAP + volume increasing
        if (rising_count >= self.params['obv_rising_days'] and
            close > vwap and
            pd.notna(vol_ratio) and vol_ratio > 1.0):
            return 'BUY'
        
        # SELL: OBV falling for N days
        falling_count = 0
        for i in range(1, min(self.params['obv_falling_days'] + 2, len(df))):
            if not df.iloc[-i].get('obv_rising', True):
                falling_count += 1
            else:
                break
        
        if falling_count >= self.params['obv_falling_days']:
            return 'SELL'
        
        return 'HOLD'
    
    def stop_loss(self, df, entry_price):
        vwap = df.iloc[-1].get('vwap', entry_price * 0.97)
        atr = df.iloc[-1].get('atr', 0)
        if pd.notna(vwap) and pd.notna(atr) and atr > 0:
            return min(vwap - 1.0 * atr, entry_price - 2.0 * atr)
        return entry_price * 0.95
    
    def confidence(self, df):
        vol_ratio = df.iloc[-1].get('vol_ratio', 1.0)
        obv_rising = df.iloc[-1].get('obv_rising', False)
        conf = 0.4
        if obv_rising:
            conf += 0.3
        if pd.notna(vol_ratio) and vol_ratio > 1.5:
            conf += 0.3
        return min(conf, 1.0)

ALL_STRATEGIES = [
    S1_DetectorScore,
    S2_DualEMA,
    S3_MeanReversion,
    S4_BreakoutVol,
    S5_MomentumAccel,
    S6_SmartMoney,
]

def get_strategy(name: str) -> Strategy:
    for cls in ALL_STRATEGIES:
        if cls.name == name:
            return cls()
    raise ValueError(f'Unknown strategy: {name}')
