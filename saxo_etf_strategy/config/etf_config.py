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
    # 2026-08-28: switched sector_rotation -> dual_ma -- explicit user
    # request ("expand the universe of ETF and raise the cap up to 50 or
    # 100"). sector_rotation's own SECTORS list is a hard 11-symbol ceiling
    # (there's no 12th US sector to rank), so no cap change could ever have
    # satisfied that request under sector_rotation; dual_ma already existed
    # with a curated ~50-ETF universe (broad market/sectors/bonds/
    # commodities/factors) and was expanded the same day to 101 symbols
    # (see DualMAStrategy.UNIVERSE in etf_strategy.py) -- more sub-sectors,
    # international/regional, fixed income, commodities, factor/style, and
    # dividend/income names, all real liquid non-leveraged tickers.
    strategy_name: str = "dual_ma"
    lookback_days_fast: int = 20
    lookback_days_slow: int = 100
    min_avg_daily_turnover_usd: float = 1_000_000  # dual_ma liquidity filter
    max_candidates_per_run: int = 10               # top-N for rotation strategies.
                                                    # History: 3->10->11->100->20->10.
                                                    # 2026-09-03: narrowed 20->10 per user
                                                    # request ("limit to top 10, rest all sell").
                                                    # trim_out_of_ranking() in ETFExecutor sells
                                                    # any open position not in this top-N each run.
    rebalance_frequency_hours: int = 24


@dataclass
class ETFRiskConfig:
    # AccountKey: leave empty to auto-discover from /port/v1/accounts/me
    # (same SIM account as shares — capital is separated in code, not at broker level)
    etf_account_key: str = ""

    # Conservative: 15% of account balance, 20 positions max, 8% SL / 20% TP.
    # max_positions: 3->10->11->100->20 -- see max_candidates_per_run's
    # comment in ETFStrategyConfig above for the full history; narrowed
    # back down 2026-08-28 alongside it (same 20-symbol TOP-20-by-volume
    # universe). Same 15% total budget, now spread rank-weighted across up
    # to 20 names -- each position is meaningfully larger than the
    # 100-name-split era while still giving the top-ranked pick more
    # capital than the bottom-ranked one.
    total_allocation_pct_of_account: float = 0.15
    max_positions: int = 10  # 20->10 on 2026-09-03 (match top-N cap)
    max_position_pct: float = 0.03   # ceiling per name; rank-1's weighted share
                                      # (a larger fraction of the 15% budget now
                                      # that it's split across only 20 names,
                                      # not 100) stays comfortably under this
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
    dry_run: bool = False  # flipped 2026-08-15 — real SIM orders enabled


DEFAULT_CONFIG = ETFConfig()
