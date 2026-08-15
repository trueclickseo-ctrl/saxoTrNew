"""
ETF strategy configuration.

Completely separate from the shares strategies — nothing here is imported
by, or imports from, the shares code. Change these freely.
"""

import os
from dataclasses import dataclass, field

# Absolute root of this ETF module (saxo_etf_strategy/)
_ETF_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@dataclass
class SaxoEnvConfig:
    base_url: str = "https://gateway.saxobank.com/sim/openapi"
    # Switch to "https://gateway.saxobank.com/openapi" for LIVE


@dataclass
class ETFUniverseConfig:
    asset_type: str = "Etf"
    page_size: int = 1000
    exchange_ids: list = field(default_factory=list)   # empty = query all exchanges
    cache_path: str = field(default_factory=lambda: os.path.join(_ETF_ROOT, "data", "etf_universe.json"))
    cache_ttl_hours: int = 24
    request_delay_sec: float = 1.0   # chart API rate-limit safe; 50 ETFs = ~60s
    max_retries: int = 3


@dataclass
class ETFStrategyConfig:
    # Which strategy to run: "sector_rotation" | "dual_ma" | "risk_off" | "mean_reversion"
    strategy_name: str = "sector_rotation"
    lookback_days_fast: int = 20
    lookback_days_slow: int = 100
    min_avg_daily_turnover_usd: float = 1_000_000  # dual_ma liquidity filter
    max_candidates_per_run: int = 3               # top-N for rotation strategies
    rebalance_frequency_hours: int = 24


@dataclass
class ETFRiskConfig:
    # AccountKey: leave empty to auto-discover from /port/v1/accounts/me
    # (same SIM account as shares — capital is separated in code, not at broker level)
    etf_account_key: str = ""

    # Conservative: 15% of account balance, 5 positions max, 8% SL / 20% TP
    total_allocation_pct_of_account: float = 0.15
    max_positions: int = 5
    max_position_pct: float = 0.03   # 15% / 5 slots = 3% of total cash per position
    stop_loss_pct: float = 0.08
    take_profit_pct: float = 0.20


@dataclass
class ETFConfig:
    env: SaxoEnvConfig = field(default_factory=SaxoEnvConfig)
    universe: ETFUniverseConfig = field(default_factory=ETFUniverseConfig)
    strategy: ETFStrategyConfig = field(default_factory=ETFStrategyConfig)
    risk: ETFRiskConfig = field(default_factory=ETFRiskConfig)
    state_path: str = field(default_factory=lambda: os.path.join(_ETF_ROOT, "data", "etf_positions.json"))
    log_path: str = field(default_factory=lambda: os.path.join(_ETF_ROOT, "logs", "etf_strategy.log"))
    dry_run: bool = True   # stays True until you explicitly flip it after reviewing dry-run logs


DEFAULT_CONFIG = ETFConfig()
