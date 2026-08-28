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
    max_candidates_per_run: int = 100              # top-N for rotation strategies — widened
                                                    # 2026-08-24 (3->10), 2026-08-28 (10->11
                                                    # under sector_rotation, the hard ceiling
                                                    # for its 11-symbol universe), then
                                                    # 2026-08-28 again (11->100) after
                                                    # switching to dual_ma's ~101-symbol
                                                    # universe -- explicit user request ("50
                                                    # or 100"), matches the universe almost
                                                    # exactly rather than an arbitrary number;
                                                    # etf_executor.process_signals() weights
                                                    # capital by rank so #1 still gets more
                                                    # than #100, not an equal split
    rebalance_frequency_hours: int = 24


@dataclass
class ETFRiskConfig:
    # AccountKey: leave empty to auto-discover from /port/v1/accounts/me
    # (same SIM account as shares — capital is separated in code, not at broker level)
    etf_account_key: str = ""

    # Conservative: 15% of account balance, 100 positions max, 8% SL / 20% TP.
    # max_positions widened 3->10->11->100 alongside max_candidates_per_run
    # above (see that field's comment for the full history) -- same 15%
    # total budget now spread rank-weighted across up to 100 names instead
    # of 11, so each individual position is naturally much smaller (keeps
    # margin/cash headroom free) while still giving the top-ranked pick
    # more capital than the bottom-ranked one. In practice dual_ma will
    # rarely have 100 simultaneous BUY-crossover candidates -- this cap is
    # sized to the universe (101 symbols), not a number expected to bind
    # every run.
    total_allocation_pct_of_account: float = 0.15
    max_positions: int = 100
    max_position_pct: float = 0.03   # ceiling per name; rank-1's weighted share
                                      # (now a small fraction of the 15% budget
                                      # split across up to 100 names) stays
                                      # comfortably under this
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
