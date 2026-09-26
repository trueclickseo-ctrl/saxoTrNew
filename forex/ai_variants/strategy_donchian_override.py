# SUPERSEDED 2026-09-27: Phases 2-5 filters retired — passthrough so ai_sim
# runs identical donchian signals to regular SIM. Copilot (now in
# agent_strategies) will learn BUY/SELL and pair-tier distinctions from live
# forward data rather than having them hardcoded. Prior filters preserved below
# as comments for reference if re-activation is needed.
#
# Prior phases (for reference):
#   Phase 2: two-consecutive-close hard_stop confirmation
#   Phase 3: block exotic-quote currencies (TRY, MXN, CZK, DKK, PLN, NOK, HUF, ZAR, SGD)
#   Phase 4: monster-trend pre-filter (HIGH_VOL+CORE_STD, ADX≥35, ATR expansion, score≥0.3)
#   Phase 5: BUY-only (BUY: +11,047 EUR / SELL: -804 EUR on 52 regular SIM trades)
# Reason for passthrough: Phase 4+5 was so restrictive it produced 0 ai_sim
# trades since 2026-09-25. Zero data is worse than noisy data for copilot learning.

from forex.strategy_donchian import generate_signals, should_exit, size_position  # noqa: F401
