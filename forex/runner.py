"""
forex/runner.py
---------------
Multi-strategy daily execution runner for FX pairs.

Strategies:
  ema         — EMA(5/30) + ADX(14) crossover  (trend-following)
  rsi         — RSI(2) pullback within EMA(200) trend (mean-reversion within trend)
  donchian    — 30-day Donchian + EMA(200) + ADX(25) strict breakout (momentum)
  bb          — Bollinger Band(20,2) + RSI(14) mean-reversion (fade extremes)
  pullback    — EMA(20) pullback in EMA(50) trend (~70% win rate, tight stops)
  gap         — Weekend gap fill — fade Sunday open vs Friday close (~80-85% WR)
  supertrend  — SuperTrend(10,3) + EMA(200) trend-following (~65% WR)
  zscore      — Z-score mean reversion: fade 2σ extremes back to mean (~63% WR)
  ml          — Logistic regression on 7 technical features (~57-62% WR)
  london_breakout — Asian/London range breakout at London open + NY open (~58-63% WR)

Universe:
  34 pairs — 7 G7 majors + 27 crosses (UICs confirmed Saxo SIM; Scandi/EM verify with --info)
  Asian session  (14): JPY crosses, AUD/NZD pairs — run at 06:20 PKT
  London session (20): EUR/GBP/USD crosses + Scandi/CAD — run at 18:00 PKT

Usage:
    python forex/runner.py                          # all 4 strategies, all 27 pairs, dry-run
    python forex/runner.py --live                   # all 4, real Saxo SIM orders
    python forex/runner.py --session asian --live    # Asian session (14 pairs, 06:20 PKT)
    python forex/runner.py --session london --live   # London session (13 pairs, 18:00 PKT)
    python forex/runner.py --exits-only --live       # stop-check only, all pairs (14:00 PKT)
    python forex/runner.py --strategy pullback       # Pullback strategy only
    python forex/runner.py --strategy ema            # EMA only
    python forex/runner.py --scan                   # 4-panel market snapshot
    python forex/runner.py --status                 # open positions
    python forex/runner.py --info                   # verify UICs live

State:
    data/forex_state.json   — open positions (keyed as "strategy:symbol")
    data/forex_orders.json  — order log (last 500 entries)
"""

import argparse
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, date, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)
sys.path.insert(0, _HERE)

import requests
import saxo_order
import pandas as pd
import saxo_auth

from forex.universe import PAIRS, ASSET_TYPE, get_pair, price_decimals as get_price_decimals, CORE_SYMBOLS, EXOTIC_SYMBOLS, HIGH_VOLUME_SYMBOLS, METALS_SYMBOLS
import forex.strategy             as strat_ema
import forex.strategy_advanced_ema as strat_advanced_ema
import forex.strategy_ema_trend   as strat_ema_trend
import forex.strategy_rsi         as strat_rsi
import forex.strategy_rsi_trend   as strat_rsi_trend
import forex.strategy_rsi_atr     as strat_rsi_atr
import forex.strategy_advanced_rsi_master as strat_advanced_rsi_master
import forex.strategy_donchian    as strat_donchian
import forex.strategy_donchian_quality as strat_donchian_quality
import forex.strategy_donchian_ai as strat_donchian_ai  # 2026-09-03: SIM A/B + AI gates
import forex.strategy_bb          as strat_bb
import forex.strategy_advanced_bb_master as strat_advanced_bb_master
import forex.strategy_bb_quality    as strat_bb_quality
import forex.strategy_bb_quality_hv as strat_bb_quality_hv
import forex.strategy_pullback    as strat_pullback
import forex.strategy_advanced_pullback_master as strat_advanced_pullback_master
import forex.strategy_gap         as strat_gap
import forex.strategy_gap_weekend as strat_gap_weekend
import forex.strategy_supertrend  as strat_supertrend
import forex.strategy_zscore      as strat_zscore
import forex.strategy_zscore_quality    as strat_zscore_quality
import forex.strategy_zscore_quality_tb as strat_zscore_quality_tb
import forex.strategy_ml                as strat_ml
import forex.strategy_advanced_ml       as strat_advanced_ml
# cnn_lstm needs torch (a heavy, optional dep). A missing/broken torch must
# NOT take down the whole runner -- every other strategy, the LIVE RSI book
# included, has to keep trading. Confirmed 2026-09-02: forex.strategy_cnn_lstm
# does a top-level `import torch`, so a bare `import` here would abort the
# process before entries/exits on an install without torch. Degrade instead:
# the two cnn_lstm strategies drop out of STRATEGIES (below) and are rejected
# as --strategy args; nothing else is affected.
try:
    import forex.strategy_cnn_lstm         as strat_cnn_lstm
    import forex.strategy_advanced_cnn_lstm_master as strat_advanced_cnn_lstm_master
except Exception as _cnn_import_exc:  # pragma: no cover - env-dependent
    strat_cnn_lstm = None
    strat_advanced_cnn_lstm_master = None
    logging.getLogger("forex.runner").warning(
        "cnn_lstm strategies unavailable (%s: %s) -- every other strategy, "
        "including the LIVE RSI book, is unaffected",
        type(_cnn_import_exc).__name__, _cnn_import_exc)
import forex.strategy_london_breakout  as strat_lbo
import forex.strategy_london_breakout_v2 as strat_lbo_v2
import pnl_tracker
import trade_logger
import strategy_learner
try:
    import attention          # the one "ATOS needs a human" channel; optional
except Exception:
    attention = None
import forex.notifier      as fx_notify
import forex.signal_filter as signal_filter
import forex.forward_observation as forward_observation
import forex.exit_advisor  as exit_advisor
import forex.rsi_signal_registry as rsi_signal_registry

# AI advisory layer (Sprint 0+). Ships OFF (ai_enabled_for -> False by
# default). Every touchpoint is guarded by that call. Import is cheap and
# side-effect-free; nothing here runs unless config/ai.json enables it.
#
# Wrapped in try/except because this module is run as a script AND re-imported
# by safeguard/housekeeping in the same process -- if the AI sub-package ever
# fails to import (partial-module race, a broken edit, a missing optional
# dep), the deterministic trading engine must still load and run. On failure
# the hooks below see ai_config is None / the stubs and skip themselves.
try:
    import ai.config as ai_config
    import ai.agent.trading_copilot as ai_trading_copilot
    from ai.features.trade_proposal import (build_proposal as _ai_build_proposal,
                                            log_proposal as _ai_log_proposal,
                                            log_shadow_decision as _ai_log_shadow,
                                            already_evaluated as _ai_already_evaluated)
except Exception as _ai_import_exc:                      # pragma: no cover
    logging.getLogger(__name__).warning(
        "  [ai] advisory layer unavailable, running deterministic-only: %s", _ai_import_exc)
    ai_config = None
    ai_trading_copilot = None
    def _ai_build_proposal(*a, **k): return {}
    def _ai_log_proposal(*a, **k): return None
    def _ai_log_shadow(*a, **k): return None
    def _ai_already_evaluated(*a, **k): return False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("forex.runner")

# ── Strategy registry ─────────────────────────────────────────────────────────
STRATEGIES = {
    "ema":         strat_ema,
    # 2026-08-30: SIM-only parallel A/B test against "ema" (user-supplied
    # design, "implement this too on ATOS SIM account like above"). Adds a
    # rising-ADX regime check + ATR-percentile band, EMA50 macro-trend
    # confirmation, a recent-crossover age limit, and a trend-quality
    # composite score (ADX + DI dominance + EMA separation) instead of
    # ADX alone. "ema" (forex/strategy.py) is completely untouched. Never
    # added to either LIVE allowlist -- SIM only.
    "advanced_ema": strat_advanced_ema,
    # 2026-09-02: SIM-only A/B vs "ema" -- IDENTICAL to "ema" except an entry
    # is only kept when the EMA(5/30) crossover is BOTH fresh (age <= 3 bars,
    # base allows 15) AND backed by a real +DI/-DI gap (|spread| >= 15). A
    # 12y/49-pair decomposition showed "ema"'s edge (unstable, CI spans zero)
    # concentrates entirely in fresh + high-conviction crossovers: that
    # subset ran +0.30 R/trade, PF ~2, positive in both halves. "ema" is
    # UNTOUCHED. Never in either LIVE allowlist. See forex/strategy_ema_trend.py.
    "ema_trend":   strat_ema_trend,
    "rsi":         strat_rsi,
    # 2026-09-02: SIM-only A/B vs "rsi" -- IDENTICAL to "rsi" except entries
    # are gated on ai.regime.classifier: Buy only when TRENDING_BULLISH,
    # Sell only when TRENDING_BEARISH. An 11y/49-pair decomposition showed
    # RSI(2)'s stable edge lives entirely in the TRENDING buckets (+0.08 R,
    # positive in both halves) while RANGING (+0.011, unstable) is the
    # regime-luck. "rsi" is UNTOUCHED. Never in either LIVE allowlist -- SIM
    # forward-test + walk-forward first. See forex/strategy_rsi_trend.py.
    "rsi_trend":   strat_rsi_trend,
    # 2026-09-03: SIM-only A/B twin gating RSI(2) entries to atr_pctile > 0.66
    # (top 34% of ATR readings vs trailing 252-bar window). Decomposition gate
    # (H20260903-74e4df, 7,152 trades / 13y): high-vol bucket avg_r +0.051,
    # WR 65.7%, PF 1.25, CI [+0.028, +0.074] stable across both halves vs
    # low-vol avg_r −0.005. "rsi" is UNTOUCHED. Never in either LIVE allowlist.
    # See forex/strategy_rsi_atr.py.
    "rsi_atr":     strat_rsi_atr,
    # NB: the confirmation-delay idea (module forex/strategy_rsi_confirm.py)
    # was built + backtested 2026-09-02 and RETIRED before it ever scanned --
    # a 12,700-signal / 12y backtest showed the delay systematically enters
    # AFTER the mean reversion it targets (win 56%->42%, every variant worse
    # than entering on the signal). The module is kept unwired as the
    # documented negative result; do not re-register without a new backtest.
    # 2026-08-30: SIM-only A/B vs "rsi" (user-supplied "master" design).
    # Robust one-sided RSI(2), EMA50/EMA200 alignment + EMA200 slope, a
    # minimum EMA200-distance gate, ATR-percentile band, a post-extreme
    # reversal-confirmation bar, and DI confirmation. "rsi" (and the LIVE_EUR
    # account that runs it) is completely untouched -- this is a shadow/A/B
    # on SIM only, never in either LIVE allowlist.
    "advanced_rsi_master": strat_advanced_rsi_master,
    "donchian":    strat_donchian,
    # 2026-09-03: SIM-only Donchian + AI gates (DI-spread + regime + ATR-pctile)
    "donchian_ai": strat_donchian_ai,
    # 2026-08-29: SIM-only parallel A/B test against "donchian" -- adds
    # the breakout-quality filters (min/max breakout strength, ADX-rising,
    # max EMA200 distance) from the user's own design doc, plus a REALLY
    # enforced 4-position cap (see SLOTS_PER_STRATEGY below and this
    # module's own docstring item 5). "donchian" itself is untouched and
    # keeps running exactly as before.
    "donchian_quality": strat_donchian_quality,
    "bb":          strat_bb,
    # 2026-08-30: SIM-only A/B vs "bb" (user-supplied "master" design).
    # Adds an ADX_MAX ceiling (avoid band-walks), ATR-percentile band, a
    # minimum band-width and minimum excursion-in-ATR gate, and a
    # prior-excursion + today's-reversal confirmation. "bb" is untouched;
    # SIM only, never in either LIVE allowlist.
    "advanced_bb_master": strat_advanced_bb_master,
    # 2026-09-02: SIM-only A/B vs "bb" -- IDENTICAL to "bb" except an entry
    # is only kept when the market is non-directional at the signal bar
    # (|plus_di - minus_di| <= 14). A 12y/49-pair decomposition showed "bb"
    # (already stable-positive at +0.048 R) gives most of its edge back on
    # the high-DI-spread signals; the low-DI-spread half ran +0.15-0.22 R,
    # PF ~2, positive in both halves. "bb" is UNTOUCHED. Never in either LIVE
    # allowlist. See forex/strategy_bb_quality.py.
    "bb_quality":    strat_bb_quality,
    "bb_quality_hv": strat_bb_quality_hv,   # 2026-09-04: A/B twin + HIGH_VOLATILITY regime gate (H20260904-37779e)
    "pullback":    strat_pullback,
    # 2026-08-30: SIM-only A/B vs "pullback" (user-supplied "master"
    # design). Adds EMA5>EMA20>EMA50 structure, ATR-percentile band,
    # ADX-not-fading check, DI confirmation, and a same-day bounce
    # (close > prev close). "pullback" is untouched; SIM only.
    "advanced_pullback_master": strat_advanced_pullback_master,
    "gap":         strat_gap,
    # 2026-08-29: SIM-only parallel A/B test against "gap" -- fixed
    # sizing/ref-close bugs, sessions disabled pending separate-by-type
    # results (see strategy_gap_weekend.py's module docstring). Never
    # added to LIVE_ALLOWED_STRATEGIES / LIVE_EUR_ALLOWED_STRATEGIES below,
    # so it can never run on either LIVE account regardless of this entry.
    "gap_weekend": strat_gap_weekend,
    "supertrend":  strat_supertrend,
    "zscore":      strat_zscore,
    # 2026-09-02: SIM-only A/B vs "zscore" -- IDENTICAL except an entry is
    # only kept when the market is non-directional at the signal bar
    # (|plus_di - minus_di| <= 14). A 12y/49-pair decomposition showed
    # "zscore" as a whole is a coin flip (+0.002 R, CI spans zero) but the
    # low-DI-spread quartile ran +0.132 R, positive in both halves -- the
    # exact same filter that works for "bb". "zscore" is UNTOUCHED. Never in
    # either LIVE allowlist. See forex/strategy_zscore_quality.py.
    "zscore_quality":    strat_zscore_quality,
    "zscore_quality_tb": strat_zscore_quality_tb,  # 2026-09-04: A/B twin + TRENDING_BULLISH regime gate (H20260904-c9e606)
    "ml":              strat_ml,
    # 2026-08-30: SIM-only parallel A/B test against "ml" (user-supplied
    # design, "implement this strategy too along our ML, lets see if catch
    # new signals"). Regularized (L2) logistic regression, 252-bar window,
    # 5-day ATR-normalized target with a neutral zone excluded from
    # training, plus regime (ADX + ATR-percentile band) and directional
    # EMA-stack trend filters, threshold 0.62. Also ships an
    # update_stop_price() breakeven+trail hook (wired generically in
    # _run_exits). "ml" itself is completely untouched. Never added to
    # either LIVE allowlist -- SIM only.
    "advanced_ml":     strat_advanced_ml,
    "cnn_lstm":        strat_cnn_lstm,
    # 2026-08-30: SIM-only A/B vs "cnn_lstm" (user-supplied "master"
    # design). Same pre-trained model, NO retrain -- just a stricter
    # selection wrapper: confidence 0.52 + class-margin 0.08 + hold-prob
    # ceiling, ADX/ATR-percentile regime confirmation, ADX-rising check,
    # and an EMA20/50 + DI directional agreement gate. "cnn_lstm" is
    # untouched; SIM only.
    "advanced_cnn_lstm_master": strat_advanced_cnn_lstm_master,
    "london_breakout": strat_lbo,
    # 2026-08-29: SIM-only parallel A/B test against "london_breakout" --
    # fixes the range-hour boundary bug, the not-actually-2:1 R/R, the
    # backwards scoring formula, repeat-signal risk, and the fallback
    # size_position()'s hardcoded equity/10.7 bug; adds a real 4-position
    # cap and cuts RISK_PCT to 0.5% (see strategy_london_breakout_v2.py's
    # module docstring for the full 9-point list). "london_breakout"
    # itself is untouched and keeps running exactly as before.
    "london_breakout_v2": strat_lbo_v2,
}
# Drop any strategy whose module failed to import (cnn_lstm when torch is
# missing -- see the guarded import above). Keeps STRATEGIES / _VALID_STRATS
# honest so nothing downstream calls a None module.
STRATEGIES = {k: v for k, v in STRATEGIES.items() if v is not None}

# ── Retired strategies (2026-09-02) ─────────────────────────────────────────
# Still in STRATEGIES (so any OPEN position keeps full exit management via
# _legacy_exit_strategies, and the dashboard / ledger / reports keep showing
# their history) -- but excluded from the default entry rotation, so they
# never open anything new. A 12-year / 49-CORE-pair edge decomposition
# (docs/strategy_decomposition_2026-09-02.md) showed each is net-negative
# with no filter that survives a both-halves + bootstrap-CI test:
#   donchian / donchian_quality -- negative, no rescuing filter
#   pullback                    -- negative
#   ml                          -- -0.046 R/trade, bootstrap CI fully negative
#   supertrend                  -- negative in 10 of 12 years; own score inverted
# An explicit `--strategy <name>` still runs one (for research) with a warning.
# To un-retire: remove from this set + re-confirm with a fresh walk-forward.
RETIRED_STRATEGIES: set[str] = {
    # donchian + pullback re-activated 2026-09-05 as AI research tracks --
    # removed from this set so the scanner runs them without a warning.
    # They are SIM-paper only; AI copilot scores every signal so we can
    # measure whether the AI improves their edge over the original.
    "donchian_quality", "ml", "supertrend",
}

# ── SIM entry roster (2026-09-02, explicit user decision) ───────────────────
# The user cut the SIM forex book down to the day-1 RSI(2) baseline + the four
# decomposition-validated "improved" twins, and force-flattened everything
# else once (close_all_forex_sim.py). Only these take NEW SIM entries and only
# these show on forex_dashboard.py. Every other strategy -- the untouched
# originals (ema/bb/zscore), the advanced_*/*_master A/B experiments, the
# RETIRED_STRATEGIES set, the LBO day-trade book -- is dormant: its module
# stays importable and any lingering open position still exit-manages
# (run_exits_only iterates ALL of STRATEGIES; run_daily's _legacy_exit path
# covers the rest), but it opens nothing new. `--strategy <name>` still runs
# any one on explicit request, with a warning. Reversible: edit this list.
# LIVE (SEK rsi / EUR exits-only) is UNAFFECTED -- that path resolves from
# LIVE_ALLOWED_STRATEGIES, never this.
SIM_ACTIVE_STRATEGIES: list[str] = [
    "rsi", "rsi_trend", "rsi_atr", "ema_trend", "bb_quality", "bb_quality_hv",
    "zscore_quality", "zscore_quality_tb",
    "donchian_ai",  # 2026-09-03: SIM-only A/B; original donchian raw was retired, AI-gated twin entered forward study
    # 2026-09-05: AI research tracks -- SIM paper only, copilot scores every
    # signal. Goal: AI observes, learns, and proposes improvements to these
    # net-negative strategies before any live consideration.
    "donchian", "pullback",
]
_ACTIVE_STRATEGIES = [k for k in SIM_ACTIVE_STRATEGIES if k in STRATEGIES]

# ── Disabled `gap` session legs (2026-09-02) ────────────────────────────────
# A ~2.8y / H1-bar decomposition (docs/strategy_decomposition_2026-09-02.md)
# of every reconstructed London / NY session gap:
#   newyork  +0.090 R/trade, PF 1.33, stable both halves          -> KEEP
#   london   -0.008 R/trade, PF 0.98, 2nd half negative           -> disable
#   tokyo    untestable (thin yfinance H1 at 23-00 UTC), ~0 ledger -> disable
# `weekly` (+0.10 R on the 12y ledger) is unaffected. Open london/tokyo
# positions still exit-manage. Reversible: empty the set.
DISABLED_GAP_SESSIONS: set[str] = {"london", "tokyo"}
# Within the surviving `newyork` leg the edge is concentrated in RANGING /
# TRENDING regimes -- HIGH_VOLATILITY newyork gaps ran -0.357 R at a 43% win
# rate. Drop those.
GAP_NEWYORK_SKIP_REGIMES: set[str] = {"HIGH_VOLATILITY"}

_SWING_SLOTS = len(PAIRS)   # 2026-08-28 fix: was hardcoded 117 (stale since the
                            # SCANDI tier alone brought the real universe to 149,
                            # before today's 35-pair currencypairs addition brought
                            # it to 184) -- computed live now so this can't silently
                            # under-cap strategies below the real universe size again.
SLOTS_PER_STRATEGY = {
    # ema/donchian/bb/supertrend/zscore/ml/cnn_lstm previously capped at 4-20
    # slots — a legacy holdover from a smaller pair universe with no risk
    # rationale documented anywhere (unlike london_breakout below). All of
    # them scan the same full universe as rsi/pullback/gap, so capped below
    # the universe size they'd needlessly miss signals on pairs beyond their
    # slot count. Raised to 34 (2026-08-20), then to 117 (2026-08-21) when the
    # universe was expanded to the full major+EM/exotic set for SIM testing —
    # so every swing strategy can take a position in every pair it signals on.
    "ema": _SWING_SLOTS, "advanced_ema": _SWING_SLOTS,  # advanced_ema (2026-08-30): uncapped, mirrors "ema" for a clean A/B
    "ema_trend": _SWING_SLOTS,   # 2026-09-02: mirrors "ema" -- full universe, clean A/B
    "bb_quality":    _SWING_SLOTS,  # 2026-09-02: mirrors "bb"  -- full universe, clean A/B
    "bb_quality_hv": _SWING_SLOTS,  # 2026-09-04: A/B twin of bb_quality + HIGH_VOLATILITY regime gate
    "rsi": _SWING_SLOTS, "rsi_trend": _SWING_SLOTS, "rsi_atr": _SWING_SLOTS,  # 2026-09-03: rsi_atr SIM twin
    "donchian": _SWING_SLOTS, "donchian_ai": strat_donchian_ai.MAX_POSITIONS,  # 2026-09-03: AI-gated cap matches module
    "bb": _SWING_SLOTS,
    "pullback": _SWING_SLOTS, "gap": _SWING_SLOTS, "gap_weekend": _SWING_SLOTS,
    # 2026-08-30: the 4 user-supplied "advanced_*_master" A/B strategies --
    # each uncapped, mirroring its original (rsi / bb / pullback / cnn_lstm)
    # so neither side of the comparison has an artificial concurrency edge.
    "advanced_rsi_master": _SWING_SLOTS, "advanced_bb_master": _SWING_SLOTS,
    "advanced_pullback_master": _SWING_SLOTS, "advanced_cnn_lstm_master": _SWING_SLOTS,
    # 2026-08-29: unlike "donchian" (which shares _SWING_SLOTS with every
    # other swing strategy -- confirmed live that its own module-level
    # MAX_POSITIONS=4 was never actually enforced by the runner), this cap
    # for "donchian_quality" IS the real enforced limit, matching that
    # module's own MAX_POSITIONS constant -- explicit fix for the gap the
    # user's design doc flagged ("verify MAX_POSITIONS=4 is actually
    # enforced"). "donchian" itself is left exactly as it was.
    "donchian_quality": strat_donchian_quality.MAX_POSITIONS,
    "supertrend": _SWING_SLOTS, "zscore": _SWING_SLOTS,
    "zscore_quality": _SWING_SLOTS, "zscore_quality_tb": _SWING_SLOTS,  # 2026-09-04: A/B twin
    "ml": _SWING_SLOTS, "cnn_lstm": _SWING_SLOTS,
    # 2026-08-30: mirrors "ml" -- uncapped slots so the A/B comparison isn't
    # distorted by an artificial concurrency limit one side doesn't have.
    # Its own regime/trend filters + 0.62 threshold are the real selectivity.
    "advanced_ml": _SWING_SLOTS,
    "london_breakout": 28,  # universe expanded to 28 pairs 2026-08-20. Slots raised
                             # 10 -> 28 (2026-08-21, one slot per pair) so a multi-pair
                             # breakout day is never capped below what the pair list can
                             # offer. Max concurrent exposure: 28 x 1.5% = 42% of the LBO
                             # book if every slot fills (was 15% at 10 slots) — a real
                             # risk increase, done at the user's explicit request.
                             # NOT tied to _SWING_SLOTS -- LBO trades its own fixed
                             # 28-pair subset regardless of the broader universe size.
    # 2026-08-29: user's explicit concern -- 28 pairs x 1.5% risk is a
    # theoretical 42% account risk with heavy correlated FX-cross exposure
    # (e.g. EURUSD/GBPUSD/EURJPY/GBPJPY buys can all just be one USD-
    # weakness move). "london_breakout_v2" gets a REAL 4-position cap
    # (matching its own MAX_LBO_POSITIONS) and RISK_PCT cut to 0.5% --
    # worst case 4 x 0.5% = 2% of the LBO book. "london_breakout" itself
    # is untouched, still 28 slots / 1.5% as before.
    "london_breakout_v2": strat_lbo_v2.MAX_LBO_POSITIONS,
}

# Day-trade strategies run independently of the swing book's heat budget.
# They size conservatively (1-2% risk/trade) and always close same-day,
# so the shared 6% heat cap would unfairly block them when the swing book
# is fully deployed. Each day-trade strategy has its own position-count cap
# (SLOTS_PER_STRATEGY) which already limits maximum concurrent exposure.
DAY_TRADE_STRATEGIES = {"london_breakout", "london_breakout_v2"}

# Strategies that open AND close within a day -- their MAE/MFE can't be
# measured honestly from DAILY bars (one bar's High/Low spans 24h, the trade
# a few hours). For these the forward-observation excursion is taken from the
# single most-recent daily bar only (bounded, but coarse -- flagged as such
# on the exit card).
_INTRADAY_STRATEGIES = DAY_TRADE_STRATEGIES | {"gap", "gap_weekend"}

# An unrealised excursion more than this many times the trade's own entry
# risk is not real -- it means the bar window wasn't bounded to the holding
# period (the 2026-09-01 bug) or a quote/FX rate was bad on that cycle.
# Reject the update rather than let one bad reading poison the running
# MAE/MFE. 25R is far beyond any legitimate FX move over a normal hold.
_MAE_MFE_SANE_R = 25.0

# ── Session-aware pair groups ──────────────────────────────────────────────────
# asian  : 06:20 PKT  — Tokyo/Sydney session (JPY crosses, AUD, NZD)
# london : 18:00 PKT  — London-NY overlap (EUR, GBP, USD pairs; tightest spreads)
# all    : no filter  — every pair in universe
SESSION_PAIRS = {
    "asian": {
        "USDJPY", "EURJPY", "GBPJPY", "AUDJPY", "CADJPY", "NZDJPY", "CHFJPY",
        "AUDUSD", "NZDUSD", "AUDCAD", "AUDCHF", "AUDNZD", "NZDCAD", "NZDCHF",
    },
    "london": {
        "EURUSD", "GBPUSD", "USDCAD", "USDCHF",
        "EURGBP", "EURAUD", "EURNZD", "EURCAD", "EURCHF",
        "GBPAUD", "GBPCAD", "GBPCHF", "GBPNZD",
        # Scandinavian / EM — London open gives best liquidity
        "CHFAUD", "EURNOK", "EURSEK", "USDNOK", "USDSEK", "USDDKK", "USDMXN",
    },
}

# ── Currency exposure limit ───────────────────────────────────────────────────
# Maximum times any single currency can appear (long OR short) across ALL open
# positions simultaneously. Prevents correlated drawdowns when one currency
# moves sharply — e.g. 5 EUR-long positions all losing on one ECB surprise.
# Disabled (set effectively unlimited) 2026-08-21 at user's explicit request —
# "do not limit it" — to let the SIM account fully test every strategy across
# the expanded 117-pair universe without this gate suppressing signals. The
# exposure dict/tracking (_currency_exposure, dashboard's "Currency exposure"
# panel) is untouched, so real exposure is still visible — just no longer
# blocking. Reconsider re-enabling a real limit before trading live capital.
MAX_CURRENCY_EXPOSURE = 999

# 2026-08-26/27: the "reconsider before live" comment above was written on
# 2026-08-21 -- this is that reconsideration, triggered by a real incident:
# LIVE-SEK's donchian strategy opened AUDCHF Buy and CHFAUD Sell within hours
# of each other. Both are the exact same directional bet (long AUD, short
# CHF) -- confirmed by decomposing each into base/quote exposure, not by
# ticker name (the CHFAUD side was mislabeled "CADCHF" in forex/universe.py
# at the time, a separate bug fixed the same day -- see
# test_2026_08_26_chfaud_uic_mismatch.py). MAX_CURRENCY_EXPOSURE being
# unlimited (999) let it through uncontested; the user closed the duplicate
# leg manually once found. The cap was initially set to 1 -- deliberately
# tight for a 500-6,000 SEK/EUR account.
#
# 2026-08-29: user raised it to 5. LIVE_EUR was rejecting nearly every RSI
# signal because one MXNUSD position consumed the whole USD slot and shut
# out EURUSD / USDDKK / GBPUSD the same night (LIVE_EUR scheduler log,
# 00:46-03:00 runs, all four SKIP "currency exposure limit (max 1)"). At 5,
# any single currency may be net long OR short by up to 5 positions' worth
# across all open positions. This does re-open the original AUDCHF-Buy +
# CHFAUD-Sell doubling scenario (net exposure only 2, under the cap) --
# accepted trade-off; the opposite-direction and opposing-strategy guards
# still catch same-ticker / directly-opposed cases. Revisit if the account
# starts concentrating heavily on one currency.
LIVE_MAX_CURRENCY_EXPOSURE = 5


def _max_currency_exposure() -> int:
    """SIM stays unlimited (explicit 2026-08-21 user request, for full
    signal-testing breadth); both real-money accounts get the real cap."""
    if ACCOUNT_ENV in ("live", "live_eur"):
        return LIVE_MAX_CURRENCY_EXPOSURE
    return MAX_CURRENCY_EXPOSURE


# 2026-08-27: user explicitly asked for LIVE to start at a SMALLER risk %
# than SIM for the initial pilot ("If SIM says 0.25%, I would initially
# consider LIVE at something like 0.10-0.15% per trade... after live
# execution confirms spreads/fills/costs match assumptions, scale toward
# the intended risk"). The mechanism is built (size_position() in bb/rsi/
# pullback all accept an optional risk_pct override now) -- the actual
# 2026-08-28: reversed from the 2026-08-27 plan above -- real per-cell
# analysis (17 HIGH_VOLUME_SYMBOLS pairs x rsi/bb, real Saxo ATR/cost)
# showed 0.25% (and even 0.50%) clears the risk gate AND the cost gate
# together on 0/34 cells at any realistic LIVE capital level (up to a
# combined ~1,441 EUR account -- 900 EUR + 6,000 SEK at the live
# EUR/SEK rate). 0.75% clears 14/34; 1.00% clears 28/34. Explicit user
# decision (via AskUserQuestion, presented with the real per-risk-level
# cell counts): 0.75%, LARGER than SIM's 0.25%, not smaller as
# originally planned -- deliberately overriding the 2026-08-27 "start
# smaller" intent now that the real cost-viability math is known. Paired
# same-day with re-enabling the portfolio heat cap (see
# _heat_allows_entry()) for LIVE/LIVE_EUR, since a bigger RISK_PCT
# without that gate reintroduces exactly the correlated-position risk
# the heat cap exists to catch.
LIVE_RISK_PCT_OVERRIDE: float | None = 0.0075


def _live_risk_pct() -> float | None:
    """None = no override (module's own RISK_PCT applies, same as SIM).
    A real value here only takes effect for live/live_eur -- SIM is
    never affected regardless of what this constant holds."""
    if ACCOUNT_ENV in ("live", "live_eur"):
        return LIVE_RISK_PCT_OVERRIDE
    return None


# 2026-08-29: user instruction -- "do not buy 1 quantity for RSI always buy
# 10, 20 ... 100 units" (units meant in thousands; Saxo's FX minimum is
# 1,000 and it cannot place less). RSI on a real-money account risk-sizes
# exactly as before, then SNAPS the result to the nearest 10,000-unit rung,
# clamped to [10,000, 100,000]. Why: at the 1,000-unit minimum lot Saxo's
# flat ~5 EUR round-trip commission dominated the trade -- it turned RSI's
# designed 2:1 reward:risk into ~0.9:1 net (a losing edge even on winners).
# Snapping UP, not skipping: a signal that already cleared block_below_min
# at 1,000 units is taken at >=10,000. Accepted trade-off -- on tight-stop
# pairs the 10k floor pushes realised risk above the 0.75% target (GBPUSD
# ~1.25% of the 6,000 EUR cap), still inside the 6% portfolio heat cap and
# RSI's own 4-position limit. SIM is deliberately untouched: its demo
# equity already sizes RSI in the ~8k-170k range and a 100k ceiling there
# would suppress signal-testing breadth.
RSI_LIVE_LOT_RUNG = 10_000
RSI_LIVE_LOT_MIN  = 10_000
RSI_LIVE_LOT_MAX  = 100_000

# 2026-08-31: explicit user decision -- cap RSI's per-trade risk on the
# real-money accounts at a FIXED EUR45 MAXIMUM loss-if-stopped, uniform
# across pairs regardless of stop width, instead of the equity-% +
# 10k-lot-ladder combo above (which gave wildly uneven realised risk:
# ~EUR8 on MXNUSD vs ~EUR73 on GBPUSD -- the ladder snaps SIZE, not RISK).
# Rules the user set explicitly:
#   1. EUR45 = MAXIMUM risk, never a floor/target.
#   2. Round the lot DOWN to Saxo's 1,000-unit increment.
#   3. If even one min-lot would risk more than EUR45 -> SKIP the trade.
#   4. The round-trip commission stays a SEPARATE edge/cost filter
#      (MIN_EDGE_TO_COST_RATIO, unchanged) -- not folded into sizing.
# Implemented via strategy_rsi.size_position's `risk_amount` param (an
# absolute ceiling; it floors down and returns 0 below one lot). The
# EUR45 is converted to the pair's quote currency with _eur_per_unit; if
# that rate is unavailable the trade is skipped (no looser %-based
# fallback on real money). _snap_rsi_live_lot is bypassed entirely.
# RSI_LIVE_LOT_MAX still applies as a pure sanity backstop (it never binds
# at EUR45 risk). Set to None to revert to the equity-% + 10k-ladder path.
# SIM is never affected.
#
# History: the first cut this same day rounded UP and treated EUR45 as a
# minimum -- user corrected it to the max/round-down/skip rules above.
RSI_LIVE_FIXED_RISK_EUR: float | None = 45.0


def _snap_rsi_live_lot(raw_qty: int) -> int:
    """Snap a risk-sized RSI quantity to the nearest 10k rung, clamped to
    [10k, 100k]. Caller gates on ACCOUNT_ENV in ('live','live_eur') and
    strategy == 'rsi'. Only used when RSI_LIVE_FIXED_RISK_EUR is None."""
    rung = int(round(raw_qty / RSI_LIVE_LOT_RUNG)) * RSI_LIVE_LOT_RUNG
    return max(RSI_LIVE_LOT_MIN, min(RSI_LIVE_LOT_MAX, rung))

# Reject a signal if the pair's live spread is wider than this % of price —
# a proxy for "this pair's home market is currently illiquid" without needing
# a per-currency trading-hours table (which several EM/exotic currencies don't
# cleanly fit anyway — e.g. TRY and CNH trade meaningfully outside their
# "home" session too). Checked live at signal time (see 2026-08-21 spread
# survey: majors run ~0.01-0.02%, most EM/exotic pairs ~0.02-0.09% under
# normal conditions) — 0.20% gives headroom above normal EM spreads while
# still catching a genuine liquidity-crunch widening. Starting value, not
# empirically tuned yet — watch SIM results and adjust.
MAX_SPREAD_PCT = 0.20

# ── Constants ─────────────────────────────────────────────────────────────────
# SIM values, unchanged. `set_account_env("live")` reassigns BASE_URL/
# STATE_FILE/ORDERS_FILE/PEAK_EQUITY_FILE/ACCOUNT_ENV below at process
# startup (called once from main(), before any Saxo request or state load
# happens) -- every function in this file reads these as module globals at
# call time, so reassigning them once redirects everything downstream with
# no further changes needed. A single process invocation is always either
# SIM or LIVE, never both, so a module-level "current account" is safe here.
BASE_URL    = "https://gateway.saxobank.com/sim/openapi"
DATA_DIR    = os.path.join(_ROOT, "data")
STATE_FILE  = os.path.join(DATA_DIR, "forex_state.json")
ORDERS_FILE = os.path.join(DATA_DIR, "forex_orders.json")
CHART_BARS  = 500   # 2026-08-30: 340 -> 500 for "advanced_ml" (EMA200 + 252-bar
                    # training window + buffer = MIN_BARS 492). Every other
                    # strategy reads converged span<=200 indicators at iloc[-1],
                    # already stable well before 340 bars -- their signals are
                    # unchanged; the only cost is a slightly larger per-pair fetch.

ACCOUNT_ENV = "sim"

# 2026-08-25: the real-money LIVE account is restricted to exactly these
# strategies (explicit user decision, not a technical limitation) and to
# CORE_SYMBOLS only (no exotic pairs) -- enforced in set_account_env() /
# _filter_pairs_for_account() / the CLI dispatch in main(), not just
# documented here, so a mistaken invocation can't slip past it.
#
# 2026-08-27: changed from {donchian, ema, rsi} -- explicit user decision
# during the forward-SIM "no-touch" observation period. This is a
# strategy-selection change, not one of the frozen items from that period
# (the cost gate ratio, the exposure cap, the donchian A/B/C stop logic,
# and the observation-card schema are what stay untouched -- which
# strategies are allowed on LIVE was never on that list). Has zero live
# effect while LIVE_TRADING_HALTED stays True -- this just makes sure the
# right allowlist is in place for whenever it lifts.
#
# Went through three steps in one conversation -- recorded all of them so
# a future read of this history isn't confused by the apparent back-and-
# forth, it's real deliberate reversals, not a stale comment:
#   1. {bb, rsi}            ("remove donchian and ema... NO PULLBACK for now")
#   2. {bb, rsi, pullback}  ("add RSI PULLBACK both and BB" -> clarified to
#                            mean re-including pullback, not just confirming bb+rsi)
#   3. {bb, rsi}            (2026-08-28, reconciling a "34 cells" = 17x2
#                            reference in a later message that assumed only
#                            2 strategies -- explicitly confirmed via
#                            question: drop pullback again)
# 2026-08-28: narrowed again, from {bb, rsi} to {bb} only -- the two-
# account pilot design puts rsi on the EUR account instead (see
# LIVE_EUR_ALLOWED_STRATEGIES below), each account running exactly one
# strategy. SEK trades the full 17-pair HIGH_VOLUME_SYMBOLS universe (see
# _filter_pairs_for_account()) -- same as the EUR account below. Both
# accounts sharing the same 17 pairs is safe: see
# housekeeping_live.py's fetch_live_snapshot() docstring for the
# AccountKey-based disambiguation that makes pair overlap a non-issue.
# (History: a HIGH_VOLUME_GROUP_A/B 9+8 pair-split, and later a brief
# "EUR gets zero pairs" pause, were both built then explicitly superseded
# same-day before ever being committed.)
#
# 2026-08-31: changed {bb} -> {rsi} -- explicit user decision (via
# AskUserQuestion, presented with the double-exposure consequence and the
# margin-gate state: "both EUR and SEK enable and take order for RSI").
# Both real-money accounts now run RSI(2). Pair universes differ (see
# _filter_pairs_for_account): SEK LIVE stays on the 17 HIGH_VOLUME_SYMBOLS,
# EUR LIVE is on the 49 CORE_SYMBOLS -- so the 17 HIGH_VOLUME pairs are
# taken on BOTH accounts (double real-money exposure per signal), the
# other 32 CORE pairs on EUR alone. Sizing is per account (SEK off the
# 15,000 SEK cap, EUR off the 8,000 EUR cap -- _sizing_equity() /
# capital_config); the 8% rsi heat cap is PER account, so ~16% combined
# worst case. BB is no longer traded live on any account. The 4 legacy
# `donchian:` positions still open on the SEK account keep their
# broker-side GTC stops but get no ATOS trailing / time-stop management
# now that donchian isn't in the allowlist (same as while the task was
# disabled) -- close them manually when convenient.
#
# 2026-09-02: CONSOLIDATION #2 -- the user is moving all real-money trading
# OFF forex and ONTO stocks (new atos_live_stocks.py / US Blend, 30k SEK
# sleeve, same Saxo LIVE SEK sub-account). Emptied like LIVE_EUR was on
# 2026-09-01: `--account live` takes NO new entries, but the 5 open
# positions (2 donchian + 3 rsi) stay exit-managed --
# _run_daily/_run_exits_only call _legacy_exit_strategies(active=[],
# positions) -> ['donchian','rsi'] -> _run_exits runs their stop / ATR-trail
# / time-stop / broker-bracket management. The `ATOS Forex LIVE Daily Run`
# task is already Disabled; the `ATOS Forex LIVE Exit Check` task stays on to
# wind the 5 positions down. Re-populate this set only on the user's explicit
# go-ahead to resume live forex.
LIVE_ALLOWED_STRATEGIES: set[str] = set()

# 2026-08-26: a SECOND, genuinely separate real-money account -- the EUR
# sub-account under the same Saxo LIVE login (see _account()'s Currency
# matching), isolated from the SEK account above with its own capital cap.
# Originally: test RSI Pullback, and ONLY RSI Pullback, on the 83 EXOTIC
# pairs. 2026-08-28: repurposed as the second half of the two-account
# HIGH_VOLUME pilot (explicit user decision, "two €500 test accounts,
# Account A one strategy, Account B the other") -- the user does NOT want
# this account trading exotic pairs live any longer, and now trades the
# SAME full 17-pair HIGH_VOLUME_SYMBOLS universe as the SEK account
# ("I want to test both strategies BB and RSI ... on 17 Pairs"). Legacy
# open EXOTIC_SYMBOLS positions from this account's original design are
# still tracked/protected by housekeeping_live_eur.py/safeguard_live_eur.py
# alongside any new HIGH_VOLUME positions.
#
# 2026-09-01: CONSOLIDATION. User is moving all funds to the SEK account
# and running RSI there only (the EUR sub-account was only ~EUR893 of its
# own cash, 9x its EUR8,000 cap -- see config/capital.json's forex_live_eur
# comment). Empty allowlist = this account takes NO new entries, but the
# open positions it still holds ARE exit-managed: _run_daily/_run_exits_only
# call _legacy_exit_strategies(active=[], positions) -> ['rsi'] -> _run_exits
# runs. Once its positions close, disable the scheduled task entirely.
LIVE_EUR_ALLOWED_STRATEGIES: set[str] = set()

# 2026-08-26: EMERGENCY HALT, both real-money accounts (SEK "live" and EUR
# "live_eur") -- explicit user instruction after the P&L base-currency bug
# was found (a real live_eur close's WIN/LOSS email and ledger figure were
# both wrong -- see _position_net_pnl_quote_ccy()'s docstring and
# test_2026_08_26_live_pnl_base_currency_bug.py). Blocked BOTH entries and
# exits for --live runs on either account -- existing positions kept their
# real broker-side stop/TP orders throughout, which never depended on this
# flag or on ATOS's scheduler running at all.
#
# 2026-08-28: LIFTED, explicit user go-ahead (via AskUserQuestion, "Yes,
# lift it now") -- the underlying P&L bug was fixed the same day it was
# found (2026-08-26). Since then: cost-clearance gate added (2026-08-26),
# block_below_min risk gate added, portfolio heat cap re-enabled for LIVE/
# LIVE_EUR, capital caps raised to reflect the real pooled Saxo balance
# (15,000 SEK / 1,350 EUR), and RISK_PCT raised to 0.75% -- all same-day,
# 2026-08-28, all explicit user decisions with real-data verification
# before each one (see forex_live_capital_and_risk_decision_2026-08-28.md
# and forex_live_block_below_min_sizing_2026-08-28.md memory notes for the
# full basis). Lifting this alone does NOT start automatic trading -- the
# actual Windows Scheduled Tasks (ATOS Forex LIVE Daily Run / LIVE EUR
# Daily Run / LIVE Exit Check / LIVE EUR Exit Check) were all left
# Disabled while this halt was active and still need enabling separately.
LIVE_TRADING_HALTED = False


def set_account_env(env: str) -> None:
    """Switches every Saxo-facing constant in this module to the given
    account ("sim" default, "live" for the real-money SEK account,
    "live_eur" for the real-money EUR sub-account added 2026-08-26). Must
    be called exactly once, before any request/state-file access, from
    main()'s CLI dispatch -- never mid-run."""
    global BASE_URL, STATE_FILE, ORDERS_FILE, PEAK_EQUITY_FILE, ACCOUNT_ENV
    if env == "sim":
        BASE_URL         = "https://gateway.saxobank.com/sim/openapi"
        STATE_FILE       = os.path.join(DATA_DIR, "forex_state.json")
        ORDERS_FILE      = os.path.join(DATA_DIR, "forex_orders.json")
        PEAK_EQUITY_FILE = os.path.join(DATA_DIR, "forex_peak_equity.json")
    elif env == "live":
        BASE_URL         = "https://gateway.saxobank.com/openapi"
        STATE_FILE       = os.path.join(DATA_DIR, "forex_live_state.json")
        ORDERS_FILE      = os.path.join(DATA_DIR, "forex_live_orders.json")
        PEAK_EQUITY_FILE = os.path.join(DATA_DIR, "forex_live_peak_equity.json")
    elif env == "live_eur":
        # Same Saxo LIVE gateway/login/token as "live" -- it's the SAME
        # OAuth app and account holder, just a different sub-account
        # (Currency=="EUR" instead of "SEK", resolved in _account()).
        # Genuinely separate state/orders/equity files so this experiment
        # can never read or write the SEK account's tracking, and a crash
        # in one can't corrupt the other's local state.
        BASE_URL         = "https://gateway.saxobank.com/openapi"
        STATE_FILE       = os.path.join(DATA_DIR, "forex_live_eur_state.json")
        ORDERS_FILE      = os.path.join(DATA_DIR, "forex_live_eur_orders.json")
        PEAK_EQUITY_FILE = os.path.join(DATA_DIR, "forex_live_eur_peak_equity.json")
    elif env == "ai_sim":
        # AI-DECISION SIM TWIN (2026-09-03). Uses the SIM gateway for QUOTES
        # only -- _paper_only_account() forces every entry/exit through the
        # local paper path (PAPER- ids, pos["paper"]=True), so NO order is
        # ever sent to Saxo and there is no account contention with the main
        # SIM book. Own state / ledger (`forex_ai`) so the forward A/B is
        # fully isolated. The one behavioural difference vs `sim`: the
        # Trading Copilot's resize/skip is APPLIED (can_apply_decision(
        # "ai_sim") is True).
        BASE_URL         = "https://gateway.saxobank.com/sim/openapi"
        STATE_FILE       = os.path.join(DATA_DIR, "forex_state_ai.json")
        ORDERS_FILE      = os.path.join(DATA_DIR, "forex_orders_ai.json")
        PEAK_EQUITY_FILE = os.path.join(DATA_DIR, "forex_peak_equity_ai.json")
    else:
        raise ValueError(f"Unknown account env {env!r} -- expected 'sim', 'live', 'live_eur', or 'ai_sim'.")
    ACCOUNT_ENV = env


def _pnl_module() -> str:
    """pnl_tracker module name for the CURRENT account -- "forex_live" under
    the SEK LIVE account, "forex_live_eur" under the EUR LIVE account
    (2026-08-26), "forex" under SIM (unchanged). Deliberately NOT added to
    pnl_tracker.MODULES: that tuple drives get_summary()'s no-args grand
    total, which sums every module together -- adding either LIVE module
    there would silently blend real P&L into the same total as SIM's demo
    EUR/etf/futures credit. Every pnl_tracker function used here already
    takes `module` as a plain string with no validation against MODULES,
    so calling it with these names explicitly works with zero pnl_tracker
    changes, and neither ever appears in anything that doesn't ask for it
    by name."""
    if ACCOUNT_ENV == "live":
        return "forex_live"
    if ACCOUNT_ENV == "live_eur":
        return "forex_live_eur"
    if ACCOUNT_ENV == "ai_sim":
        return "forex_ai"
    return "forex"


_PAIR_STATS_CACHE: dict = {}


def _pair_history_stats(sym: str, strat_name: str) -> dict | None:
    """Per-pair closed-trade track record for the AI proposal -- so the
    Copilot can weigh 'this pair/strategy has actually worked' alongside
    the live signal. LIVE has almost no closed history yet, so this uses
    the SIM 'forex' ledger as the proxy and SAYS SO (source field). Cached
    for the process. Read-only; never raises."""
    key = (strat_name,)
    if key not in _PAIR_STATS_CACHE:
        try:
            # prefer this account's own module; fall back to SIM 'forex'
            rows = pnl_tracker.get_strategy_symbol_summary(_pnl_module())
            src = _pnl_module()
            if sum(r.get("n", 0) for r in rows) < 30:
                rows = pnl_tracker.get_strategy_symbol_summary("forex")
                src = "forex (SIM proxy)"
            by_sym = {r.get("symbol"): r for r in rows if r.get("strategy") == strat_name}
            _PAIR_STATS_CACHE[key] = (by_sym, src)
        except Exception:
            _PAIR_STATS_CACHE[key] = ({}, None)
    by_sym, src = _PAIR_STATS_CACHE[key]
    r = by_sym.get(sym)
    n = (r or {}).get("trades") or 0
    if not r or n < 3 or r.get("unresolved"):
        return None
    return {
        "n_closed": n,
        "win_rate_pct": r.get("win_rate"),
        "avg_pnl_eur": round((r.get("total_pnl") or 0) / n, 2),
        "profit_factor": r.get("profit_factor"),
        "source": src,
    }


def _filter_pairs_for_account(pairs: list) -> list:
    """2026-08-28 two-account LIVE design: SEK LIVE and EUR LIVE (originally
    bb and rsi respectively; both rsi since 2026-08-31) traded the SAME
    17-pair HIGH_VOLUME_SYMBOLS universe (narrowed from all 34 CORE_SYMBOLS
    on 2026-08-27) -- explicit user decision, "I want to test both
    strategies BB and RSI ... on 17 Pairs". No exotic pairs on either LIVE
    account. (EUR later expanded to 49 CORE pairs, SEK kept at 17 -- see
    below.)

    Sharing pairs across two accounts is only safe because of a same-day
    finding: Saxo's pooled /port/v1/positions/me and /port/v1/orders/me
    responses carry a genuine per-record AccountKey (verified live), so
    housekeeping_live.py/housekeeping_live_eur.py can attribute each pooled
    position/order to the correct account directly, instead of relying on
    non-overlapping pair sets to infer ownership. See housekeeping_live.py's
    fetch_live_snapshot() docstring for the full history (including an
    earlier HIGH_VOLUME_GROUP_A/B 9+8 pair-split, and a brief "EUR gets zero
    pairs" pause, both superseded same-day before being committed).

    2026-08-28 (later, same day): EUR LIVE (rsi) expanded 17 -> all 49
    CORE_SYMBOLS pairs (HIGH_VOLUME_SYMBOLS + CORE_STANDARD_SYMBOLS),
    explicit user request ("add these pairs too only for RSI"). Verified
    with real live Saxo ATR/cost before this change: 17/49 CORE pairs
    clear both the risk gate and cost gate at the current cap / 0.75%
    risk -- the other 32 candidate pairs are still scanned every cycle
    (so they trade automatically once conditions/capital change) but
    won't place an order until they naturally clear both gates.

    2026-08-31: SEK LIVE switched from bb to rsi (LIVE_ALLOWED_STRATEGIES),
    but its pair universe deliberately stays at the 17 HIGH_VOLUME_SYMBOLS
    -- NOT expanded to 49 -- so the extra real-money exposure from running
    the same strategy on two accounts is limited to the 17 highest-liquidity
    pairs. EUR keeps its 49."""
    if ACCOUNT_ENV == "live_eur":
        return [p for p in pairs if p["symbol"] in CORE_SYMBOLS]
    if ACCOUNT_ENV == "live":
        return [p for p in pairs if p["symbol"] in HIGH_VOLUME_SYMBOLS]
    return pairs


# ── Portfolio risk limits ─────────────────────────────────────────────────────
PORTFOLIO_HEAT_LIMIT  = 0.06   # pause new entries when heat ≥ 6% of equity

# 2026-08-30: per-strategy heat-cap override, explicit user decision
# (AskUserQuestion: "raise to 8% for RSI only"). The RSI(2) pullback book is
# meant to run ~10 concurrent positions; at 0.75% risk/trade that needs ~8%
# heat, not the shared 6%. Scoped to strat_name, so every non-rsi strategy
# keeps the 6% guardrail. NB (2026-08-31): the cap is evaluated per account
# from that account's own positions state -- with rsi now live on BOTH the
# SEK and EUR accounts, the effective combined ceiling is ~16% across the
# two on the shared Saxo balance. Saxo's real 50% margin gate
# (_margin_allows_entry) is still the hard backstop above all of this.
_HEAT_LIMIT_BY_STRATEGY = {"rsi": 0.08, "rsi_trend": 0.08}   # rsi_trend mirrors rsi for the A/B
DRAWDOWN_PAUSE_PCT    = 0.10   # pause entries when drawdown > 10% from rolling peak
DAILY_LOSS_LIMIT_PCT  = 0.03   # block entries if today's realised P&L ≤ −3% of equity
PEAK_EQUITY_FILE      = os.path.join(DATA_DIR, "forex_peak_equity.json")

# ── Breakeven stop parameters ─────────────────────────────────────────────────
# Trend strategies: move stop to entry_price once profit ≥ this many ATRs
BREAKEVEN_THRESHOLD_ATR = 1.0
# Gap strategy: move stop to entry_price once price is this % toward the gap target
BREAKEVEN_GAP_FILL_PCT  = 0.50

# ── RSI(2) profit-protection ladder (2026-08-31) ─────────────────────────────
# An OPT-IN alternative to the always-on trailing_stop_update + one-shot
# _apply_breakeven_stop for the RSI(2) mean-reversion book. Design hypothesis
# (user-specified): protect an unrealised gain in stages instead of a single
# move-to-exact-entry, and don't trail at all until the trade has proven
# itself, so RSI(2)'s normal noise/retracement isn't stopped out prematurely.
# R = the initial entry-to-stop distance (1.5 x ATR_at_entry for RSI):
#
#   >= 0.75 R profit  -> stop to entry + COST_BUFFER_R x R  (breakeven + costs)
#   >= 1.00 R profit  -> stop to entry + LOCK_R x R          (lock +0.5 R)
#   >= 1.25 R profit  -> stop to max(lock level, close - TRAIL_ATR_MULT x ATR_now)
#
# Ratchet only (a rung never loosens the stop). Primary exit is still RSI
# recovery / 2 R broker TP / 12-day time stop / hard stop -- unchanged.
#
# 2026-08-31: turned ON for both real-money accounts, then SIM too, at the
# user's explicit repeated request -- the exact 3-stage design they
# specified (+0.75 R breakeven+costs / +1.0 R lock +0.5 R / +1.25 R 1xATR
# trail), after a GBPPLN position gave back +30 -> -24 PLN. The backtest
# (backtests/rsi_exit_ladder_backtest.py, 17 pairs / 12 y / 2365 trades)
# showed only a small net edge and that it doesn't fully close the give-back
# (avg MFE ~0.51 R sits below the 0.75 R first rung) -- but the ladder only
# ever TIGHTENS a stop (pure ratchet, never loosens), the primary RSI-
# recovery / 2 R TP / 12-day exits are unchanged, and the user accepts the
# "may clip some winners a touch early" trade-off to stop the give-back.
#
# SIM added 2026-08-31 to forward-test it live: it applies ONLY to the "rsi"
# strategy (PROFIT_LADDER_STRATEGIES), so "advanced_rsi_master" -- rsi's
# untouched A/B twin -- keeps the plain breakeven + 1.5xATR trail and is a
# clean control. report_profit_ladder.py compares the two. Every rung move
# is logged ([PROFIT-LADDER ... rung=...]) and stamped on the position
# (pos["ladder_rung"]) so the give-back-prevented vs winner-clipped
# trade-off is measurable, not guessed. Empty the set to revert everything
# to the plain breakeven + 1.5xATR trail.
PROFIT_LADDER_ACCOUNTS: set[str] = {"sim", "live", "live_eur"}
# "rsi_trend" (2026-09-02) is included so the SIM A/B isolates ONE variable
# -- the regime entry gate -- against "rsi". Both arms then share identical
# exit management (the ladder). "advanced_rsi_master" stays OUT as the
# ladder-vs-no-ladder control.
PROFIT_LADDER_STRATEGIES         = {"rsi", "rsi_trend", "rsi_atr"}
PROFIT_LADDER_BREAKEVEN_R        = 0.75
PROFIT_LADDER_COST_BUFFER_R      = 0.10
PROFIT_LADDER_LOCK_ACTIVATE_R    = 1.00
PROFIT_LADDER_LOCK_R             = 0.50
PROFIT_LADDER_TRAIL_ACTIVATE_R   = 1.25
PROFIT_LADDER_TRAIL_ATR_MULT     = 1.00

# ── Exit advisor — the "AI profit scan" for open positions ───────────────────
# Stage A (2026-08-31): forex/exit_advisor.py is a deterministic give-back-
# risk scorer (HOLD / TIGHTEN / EXIT). It runs every exits-check cycle for
# EVERY open position on EVERY account and logs what it WOULD recommend to
# data/exit_advisor_shadow.jsonl -- it never touches a stop or an order.
# report_exit_advisor.py joins that shadow log against the real exit
# outcome per trade to answer: would acting on it have beaten the plain
# ladder / RSI-recovery exits? Only "shadow" is implemented -- there is
# deliberately no "active" path yet; promoting it needs weeks of shadow
# evidence AND an explicit decision (and, for Stage B, a trained model).
EXIT_ADVISOR_MODE = "shadow"   # "shadow" | (future: "active")


# ── SIM paper-fill fallback (2026-08-31) ─────────────────────────────────────
# Saxo's SIM order engine has had two multi-hour outages in a week
# (2026-08-28, 2026-08-31 -- both "CouldNotCompleteRequest (90)" on every
# POST /trade/v2/orders while reads/quotes kept working fine). During those
# the whole SIM forward-test stalls: strategies generate signals, none fill.
#
# When enabled and ACCOUNT_ENV == "sim", a rejected SIM ENTRY is booked
# LOCALLY instead of dropped -- at the live Saxo quote, with a "PAPER-"
# order id and pos["paper"] = True. From then on it is managed entirely by
# ATOS's own exit logic (trailing / breakeven / profit-ladder / should_exit)
# marked against real Saxo quotes, and closed locally too. No broker order
# is ever placed, amended, or cancelled for it. housekeeping/safeguard skip
# PAPER- positions (they have no Saxo counterpart, by design).
#
# LIVE is NEVER paper-filled -- _sim_paper_fill_enabled() hard-checks
# ACCOUNT_ENV == "sim". This is a testing-continuity tool, not a trading
# feature.
SIM_PAPER_FILL_ON_REJECT = True


def _paper_only_account() -> bool:
    """ai_sim -- the AI-decision SIM twin. EVERY entry/exit is booked locally
    at the live quote; no order is ever sent to Saxo. Distinct from the
    `sim` paper-fill FALLBACK below (which only kicks in on a Saxo rejection)."""
    return ACCOUNT_ENV == "ai_sim"


def _sim_paper_fill_enabled() -> bool:
    return (SIM_PAPER_FILL_ON_REJECT and ACCOUNT_ENV == "sim") or _paper_only_account()


def _is_paper_position(pos: dict) -> bool:
    return bool(pos.get("paper"))


def _profit_ladder_active(strat_name: str) -> bool:
    return (ACCOUNT_ENV in PROFIT_LADDER_ACCOUNTS
            and strat_name in PROFIT_LADDER_STRATEGIES)


def _profit_ladder_target_stop(pos: dict, df, strat_name: str) -> float | None:
    """Return the laddered stop level for `pos` given current bars, or None if
    no rung applies yet. Pure — no I/O, no mutation — so the backtest can call
    it directly. Ratcheting against the position's current stop is the
    caller's job (see _apply_profit_ladder_stop)."""
    if df is None or len(df) < 1:
        return None
    is_long = pos.get("direction", "Buy") == "Buy"
    entry   = float(pos.get("entry_price", 0) or 0)
    if entry <= 0:
        return None

    init_stop = pos.get("initial_stop_price")
    if init_stop:
        R = abs(entry - float(init_stop))
    else:
        atr_entry = float(pos.get("atr_at_entry", 0) or 0)
        R = strat_rsi.ATR_STOP_MULT * atr_entry
    if R <= 0:
        return None

    cur_close = float(df["Close"].iloc[-1])
    profit    = (cur_close - entry) if is_long else (entry - cur_close)
    r_mult    = profit / R

    if r_mult >= PROFIT_LADDER_TRAIL_ACTIVATE_R:
        lock = (entry + PROFIT_LADDER_LOCK_R * R) if is_long else (entry - PROFIT_LADDER_LOCK_R * R)
        try:
            atr_now = float(strat_rsi._atr(df["High"], df["Low"], df["Close"]).iloc[-1])
        except Exception:
            atr_now = 0.0
        if atr_now > 0:
            trail = (cur_close - PROFIT_LADDER_TRAIL_ATR_MULT * atr_now) if is_long \
                    else (cur_close + PROFIT_LADDER_TRAIL_ATR_MULT * atr_now)
            return max(lock, trail) if is_long else min(lock, trail)
        return lock
    if r_mult >= PROFIT_LADDER_LOCK_ACTIVATE_R:
        return (entry + PROFIT_LADDER_LOCK_R * R) if is_long else (entry - PROFIT_LADDER_LOCK_R * R)
    if r_mult >= PROFIT_LADDER_BREAKEVEN_R:
        buf = PROFIT_LADDER_COST_BUFFER_R * R
        return (entry + buf) if is_long else (entry - buf)
    return None

# ── Default take-profit (2026-08-22) ──────────────────────────────────────────
# gap/london_breakout compute their own tp_price/gap_target from their own
# session-range logic. The other 9 strategies (ema/rsi/donchian/bb/pullback/
# ml/zscore/supertrend/cnn_lstm) only ever computed a stop_price — profit was
# taken exclusively by the scheduler's periodic should_exit()/trailing-stop
# scan, so a position sat with only downside protection at the broker between
# runs. Per explicit user direction: every position must get BOTH legs placed
# on Saxo atomically at entry, not dependent on the next scheduled run.
# DEFAULT_TP_RR gives those 9 strategies a broker-side take-profit at this
# multiple of their own already-computed stop distance (entry-to-stop) —
# matching london_breakout's established 2:1 reward:risk convention (see
# docs/forex_strategies.md's "TP ratio | 2.0 × range" for LBO). This does not
# change any strategy's own exit logic (trailing stop, time-stop, hard-stop
# are still evaluated every run exactly as before) -- it only adds a resting
# Limit order at the broker so a winning trade can be captured even if a
# scheduled run is delayed or skipped.
DEFAULT_TP_RR = 2.0


def _bars_for_excursion(df: pd.DataFrame | None, pos: dict, strat_name: str) -> pd.DataFrame | None:
    """The bars to measure a position's MAE/MFE over: its HOLDING PERIOD
    only, not the whole ~1-year daily window `df` carries for signal
    generation.

    Bug fixed 2026-09-01: the caller used the full `df`, so MAE was the
    lowest low in ~350 daily bars -- on trending/volatile crosses (gap on
    ZARJPY/MXN/ILS, donchian, ml) that inflated MAE to tens of thousands of
    EUR against a ~EUR80 risk. 53 of 63 closed trades in
    trade_observation_cards.jsonl were affected.

    Swing strategies: one bar per calendar day held + a 2-bar buffer.
    Intraday strategies (_INTRADAY_STRATEGIES): just the latest daily bar --
    still coarse for a sub-day hold, but bounded, and the exit card is
    flagged `mae_mfe_coarse`.
    """
    if df is None or len(df) == 0:
        return df
    if strat_name in _INTRADAY_STRATEGIES:
        return df.tail(1)
    try:
        ed = date.fromisoformat(str(pos.get("entry_date", ""))[:10])
        held_days = max((date.today() - ed).days, 0)
    except Exception:
        held_days = 1
    return df.tail(min(len(df), held_days + 2))


def _resolve_tp_price(sig: dict, direction: str) -> float:
    """Take-profit price for a signal: the strategy's own target if it
    computed one (gap's gap_target, london_breakout's tp_price), else
    DEFAULT_TP_RR applied to the signal's own stop distance."""
    tp = sig.get("tp_price") or sig.get("gap_target")
    if tp is not None:
        return tp
    stop_distance = abs(sig["close"] - sig["stop_price"])
    return (sig["close"] + DEFAULT_TP_RR * stop_distance if direction == "Buy"
            else sig["close"] - DEFAULT_TP_RR * stop_distance)


def _ai_apply_decision_to_qty(qty: int, decision: dict, min_units: float,
                              floor: float = 0.25) -> tuple[int, str | None]:
    """AI Sprint 4: turn a Trading Copilot decision into a (possibly reduced)
    order quantity. Pure -- no I/O, no globals.

      REJECT           -> (0, reason)   caller treats qty<=0 as a skip
      MODIFY (m < 1.0) -> (max(int(qty*m), min_units), note)   m clamped to [floor, 1.0]
      APPROVE / HOLD / anything else / m>=1.0 -> (qty, None)   unchanged

    The agent can only ever REDUCE size (multiplier <= 1.0, enforced here
    AND in ai/agent/trading_copilot._coerce_decision). This never runs
    unless ai_config.can_apply_decision(env) is True -- sim only, shadow
    mode off."""
    act = decision.get("action")
    note = (decision.get("comment") or "")[:120]
    if act == "REJECT":
        return 0, f"AI Copilot REJECT: {note}"
    if act == "MODIFY":
        try:
            m = max(float(floor), min(1.0, float(decision.get("size_multiplier", 1.0))))
        except (TypeError, ValueError):
            m = 1.0
        if m < 1.0:
            scaled = max(int(qty * m), int(min_units))
            if scaled != qty:
                return scaled, (f"AI Copilot MODIFY x{m:.2f} — "
                                f"{qty:,} → {scaled:,} units ({note})")
    return qty, None


# ── Order-venue circuit breaker (2026-08-31) ─────────────────────────────────
# When Saxo's order endpoint has an outage it rejects EVERY order with
# "CouldNotCompleteRequest (90)" (seen 2026-08-28, and all day 2026-08-31:
# ~4,000 rejections, 0 fills, 769 stranded working orders on SIM by noon).
# Each rejected entry still costs ~4 API calls (bracket + fallback entry +
# separate stop + separate TP) plus rate-limit backoff, so a full scan
# against a dead venue runs 60-90 min instead of ~13, overruns its Task
# Scheduler window (the 2026-08-31 watchdog false-alarm incident), and every
# failed bracket leaves working orders piling up on the account.
#
# This breaker counts CONSECUTIVE entry-order rejections across one whole
# run. Once it hits the threshold it stops attempting any further NEW
# ENTRIES for the rest of the run (all remaining signals, every later
# strategy) and _run_entries returns immediately. It deliberately does NOT
# touch exits or stop-loss healing: protecting an open position is worth
# retrying even mid-outage, and on a real-money account a protective action
# is never skipped to save time. One fresh process per scheduled run makes
# module-level state naturally per-run; _reset_order_circuit() is still
# called at the top of every run entrypoint so repeated in-process calls
# (tests, a manual loop) start clean.
CIRCUIT_BREAKER_MAX_CONSECUTIVE_REJECTS = 8

# Written when the circuit trips; scheduler_watchdog.py sees it and re-fires
# "ATOS Forex Intraday Scan" ahead of its normal cadence (a "fast retry" so
# real fills resume sooner once Saxo recovers). Cleared by a clean run.
VENUE_DOWN_FLAG = os.path.join(DATA_DIR, "forex_venue_down.flag")

_order_circuit = {
    "consecutive_rejects": 0, "open": False, "notified": False,
    "blocked": [],        # [(strategy, sym, direction, paper_filled)] this run
    "last_saxo_error": "",
}


def _reset_order_circuit() -> None:
    _order_circuit.update({"consecutive_rejects": 0, "open": False,
                           "notified": False, "blocked": [], "last_saxo_error": ""})


def _order_circuit_is_open() -> bool:
    return _order_circuit["open"]


def _note_blocked_signal(strategy: str, sym: str, direction: str, paper_filled: bool) -> None:
    """Record a signal that Saxo couldn't fill this run (for the venue-down
    email). `paper_filled` = it was booked locally instead of dropped."""
    _order_circuit["blocked"].append((strategy, sym, direction, paper_filled))


def _clear_venue_down_flag() -> None:
    try:
        if os.path.exists(VENUE_DOWN_FLAG):
            os.remove(VENUE_DOWN_FLAG)
    except Exception:
        pass


def _record_entry_result(rejected: bool, saxo_error: str = "") -> None:
    """Feed one entry-order outcome to the circuit breaker. `rejected` True
    means Saxo returned no entry order id (nothing opened). Any success
    resets the consecutive-rejection count."""
    if not rejected:
        _order_circuit["consecutive_rejects"] = 0
        return
    _order_circuit["consecutive_rejects"] += 1
    if saxo_error:
        _order_circuit["last_saxo_error"] = saxo_error
    if (not _order_circuit["open"] and
            _order_circuit["consecutive_rejects"] >= CIRCUIT_BREAKER_MAX_CONSECUTIVE_REJECTS):
        _order_circuit["open"] = True
        logger.error(
            f"  [circuit-breaker] {_order_circuit['consecutive_rejects']} consecutive entry "
            f"rejections — Saxo's order endpoint looks down. "
            + ("Paper-filling the rest this run (SIM). " if _sim_paper_fill_enabled()
               else "Halting NEW entries for the rest of this run (exits/stop-heal continue). ")
            + "Watchdog will retry the scan ahead of schedule."
        )
        try:
            with open(VENUE_DOWN_FLAG, "w", encoding="utf-8") as f:
                f.write(datetime.now().isoformat())
        except Exception:
            pass


def _venue_down_email_if_needed() -> None:
    """Called once at the end of a run: if the circuit tripped, send ONE
    email naming every blocked/paper-filled signal + the real Saxo error."""
    if not _order_circuit["open"] or _order_circuit["notified"]:
        return
    _order_circuit["notified"] = True
    try:
        fx_notify.send_order_venue_down(
            account_env=ACCOUNT_ENV,
            consecutive=_order_circuit["consecutive_rejects"],
            saxo_error=_order_circuit["last_saxo_error"],
            blocked=list(_order_circuit["blocked"]),
            paper_fill=_sim_paper_fill_enabled(),
        )
    except Exception as exc:
        logger.warning(f"  [circuit-breaker] venue-down email failed: {exc}")


# ── Saxo HTTP helpers ─────────────────────────────────────────────────────────

def _hdrs(idempotent_id: str | None = None) -> dict:
    h = {"Authorization": f"Bearer {saxo_auth.get_valid_access_token(env=ACCOUNT_ENV)}"}
    if idempotent_id:
        # Saxo rejects an identical POST/PATCH to an order endpoint within a
        # 15s rolling window as a duplicate (409 Conflict) unless each attempt
        # carries its own x-request-id — without this, a legitimate fast
        # retry (or two different strategies closing near-identical orders
        # back-to-back) can get silently blocked as "duplicate."
        h["x-request-id"] = idempotent_id
    return h


def _sleep_for_rate_limit(resp) -> float:
    reset = resp.headers.get("X-RateLimit-Orders-Reset") or resp.headers.get("X-RateLimit-Reset")
    try:
        return max(1.0, float(reset))
    except (TypeError, ValueError):
        return 2.0


def _get(path: str, params: dict | None = None) -> dict:
    for attempt in range(1, 4):
        try:
            r = requests.get(f"{BASE_URL}{path}", headers=_hdrs(),
                             params=params, timeout=15)
            if r.status_code == 429 and attempt < 3:
                wait = _sleep_for_rate_limit(r)
                logger.warning(f"429 rate-limited on GET {path} — waiting {wait:.0f}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except (requests.exceptions.SSLError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as exc:
            if attempt < 3:
                time.sleep(5 * attempt)
                continue
            raise exc


def _post(path: str, body: dict) -> dict:
    req_id = str(uuid.uuid4())
    for attempt in range(1, 4):
        r = requests.post(f"{BASE_URL}{path}", headers=_hdrs(req_id), json=body, timeout=15)
        if r.status_code == 429 and attempt < 3:
            wait = _sleep_for_rate_limit(r)
            logger.warning(f"429 rate-limited on POST {path} — waiting {wait:.0f}s")
            time.sleep(wait)
            continue
        try:
            r.raise_for_status()
        except requests.exceptions.HTTPError as e:
            # raise_for_status()'s default message is just status+URL — it
            # drops Saxo's actual error body (ErrorCode/Message explaining
            # WHY, e.g. "too small," "market closed," "invalid precision").
            # Without this, a rejected order is undiagnosable after the fact.
            raise requests.exceptions.HTTPError(
                f"{e} — Saxo response body: {r.text}", response=r) from e
        return r.json()


def _patch(path: str, body: dict) -> dict:
    req_id = str(uuid.uuid4())
    for attempt in range(1, 4):
        r = requests.patch(f"{BASE_URL}{path}", headers=_hdrs(req_id), json=body, timeout=15)
        if r.status_code == 429 and attempt < 3:
            wait = _sleep_for_rate_limit(r)
            logger.warning(f"429 rate-limited on PATCH {path} — waiting {wait:.0f}s")
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()


def _delete(path: str, params: dict | None = None) -> None:
    r = requests.delete(f"{BASE_URL}{path}", headers=_hdrs(), params=params, timeout=15)
    r.raise_for_status()


# ── Tick-size rounding ────────────────────────────────────────────────────────
# Every FX price we send Saxo (stop, take-profit, breakeven amend) has to land
# on a valid tick increment or Saxo rejects the whole order with
# "PriceNotInTickSizeIncrements" (400). For the 34 majors + most crosses the
# instrument's own decimal precision (forex.universe.price_decimals, itself
# derived from Saxo's live Format.Decimals) already rounds cleanly onto the
# tick. The precious-metal tier does NOT: XAUJPY / XAUTHB / XPTZAR quote with
# pip_size = 10, which price_decimals() maps to 1dp, but their real Saxo
# TickSize is 1.0 — so round(146892.59, 1) = 146892.6 is not a whole tick and
# the stop/TP is rejected outright, leaving a naked position (seen live
# 2026-08-30 on ml:XAUTHB and advanced_ml:XAUTHB — both stop_order_id=None).
# Same bug class as ZC's 0.25 tick in the futures module (2026-08-24), and
# saxo_order._round_price already accepts a tick_size override for exactly
# this — forex just never passed one. Metals is a SIM-only tier, so this
# only ever affects SIM.

_METALS_TICK_CACHE: dict[str, float | None] = {}


def _metals_tick_size(sym: str) -> float | None:
    """Real Saxo TickSize for a precious-metal pair, from the live
    /ref/v1/instruments/details reference data (NOT guessed from decimal
    places — TickSizeStopOrder can be coarser than Format.Decimals implies).
    Returns None for any non-metals pair (decimal-place rounding is correct
    for those and this avoids ~180 ref-data calls per scan) and for a metals
    pair whose lookup fails (caller falls back to decimal rounding). Cached
    per process — an instrument's tick size doesn't change at runtime."""
    if sym not in METALS_SYMBOLS:
        return None
    if sym in _METALS_TICK_CACHE:
        return _METALS_TICK_CACHE[sym]
    tick: float | None = None
    try:
        uic  = get_pair(sym)["uic"]
        data = _get("/ref/v1/instruments/details",
                    {"Uics": str(uic), "AssetType": ASSET_TYPE}).get("Data", [])
        if data:
            raw = data[0].get("TickSizeStopOrder") or data[0].get("TickSize")
            tick = float(raw) if raw is not None else None
    except Exception as exc:
        logger.warning(f"  [tick_size] {sym}: live TickSize lookup failed ({exc}) "
                       f"— falling back to decimal-place rounding")
    _METALS_TICK_CACHE[sym] = tick
    return tick


def _round_order_price(sym: str, price: float) -> float:
    """Round an FX order price to the instrument's real tick size when one is
    known (metals), otherwise to its decimal precision. Mirrors
    saxo_order._round_price's tick logic so the heal / breakeven paths that
    POST their own order bodies stay consistent with place_with_stop()."""
    tick = _metals_tick_size(sym)
    if tick:
        return round(round(price / tick) * tick, 10)
    return round(price, get_price_decimals(sym))


# ── Token / Account ───────────────────────────────────────────────────────────

def _verify_token(scheduled_time: str = "") -> bool:
    """
    Test the Saxo token. Returns True if valid.
    On failure, sends a token-expired email and returns False so the caller
    can exit cleanly without placing any orders.
    """
    try:
        _get("/port/v1/accounts/me")
        return True
    except Exception:
        logger.error("Saxo token appears expired or invalid — sending alert email")
        fx_notify.send_token_expired(scheduled_time, live=(ACCOUNT_ENV == "live"))
        return False


_QUOTE_RATE_CACHE: dict[str, float] = {}
_PAIRS_BY_SYMBOL = {p["symbol"]: p for p in PAIRS}

# Persistent last-good Saxo rate store -- ANALYTICS ONLY (never sizing). The
# in-memory _QUOTE_RATE_CACHE dies with each scheduled process, so a quote
# currency Saxo can't price on this run has no rate at all -- which is why
# ~80% of SIM RSI cost-gate rows had recovery_thin/all_in_cost_eur = None
# (2026-09-02 finding). _eur_per_unit() writes every fresh success here;
# _eur_rate_for_log() reads it back as a fallback for R-multiple / all-in-cost
# enrichment so the AI journal / shadow study gets a real number. Strict
# callers (sizing, exposure, the LIVE €45 risk cap) never touch this.
_EUR_RATE_STORE_PATH = os.path.join(DATA_DIR, "eur_rate_cache.json")
_EUR_RATE_STORE_MAX_AGE_H = 24.0


def _load_eur_rate_store() -> dict:
    try:
        with open(_EUR_RATE_STORE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_eur_rate(ccy: str, rate: float) -> None:
    if not rate or rate <= 0:
        return
    try:
        store = _load_eur_rate_store()
        store[ccy] = {"rate": rate, "ts": datetime.now(timezone.utc).isoformat()}
        tmp = _EUR_RATE_STORE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(store, f)
        os.replace(tmp, _EUR_RATE_STORE_PATH)
    except Exception:
        pass  # analytics cache -- must never break a run


def _eur_rate_for_log(ccy: str, akey: str | None = None) -> tuple[float | None, str | None]:
    """EUR value of one unit of `ccy` for ANALYTICS ONLY -- R-multiple /
    all-in-cost enrichment, cost-gate telemetry, observation cards. NEVER for
    sizing or converting a live trade (those stay on strict _eur_per_unit(),
    which still returns None on a Saxo miss). Tries a fresh Saxo quote; on a
    miss, falls back to the last good Saxo rate we persisted (still a Saxo
    number, minutes-to-hours old -- a fine denominator for an R-multiple).
    Returns (rate, source) with source "live" / "last_good" / None.
    """
    live = _eur_per_unit(ccy, akey)
    if live is not None:
        return live, "live"
    rec = _load_eur_rate_store().get(ccy)
    if rec:
        try:
            age_h = (datetime.now(timezone.utc)
                     - datetime.fromisoformat(rec["ts"])).total_seconds() / 3600.0
            if 0 <= age_h <= _EUR_RATE_STORE_MAX_AGE_H and float(rec.get("rate", 0)) > 0:
                return float(rec["rate"]), "last_good"
        except Exception:
            pass
    return None, None


def _live_price_retry(uic: int, akey: str, attempts: int = 2) -> float | None:
    """_live_price with a couple of retries -- a single infoprices call
    fails often enough under load (confirmed live 2026-08-22: 35 of 94
    concurrent Saxo price requests failed in one forex_dashboard.py
    refresh, a different random subset each time) that one miss shouldn't
    be treated as "Saxo has nothing," now that there's no other source to
    fall back to for this."""
    for _ in range(attempts):
        px = _live_price(uic, akey)
        if px:
            return px
    return None


def _eur_per_unit(ccy: str, akey: str | None = None) -> float | None:
    """EUR value of one unit of `ccy`, from Saxo's OWN live quotes only.

    Per explicit user direction (2026-08-22): live SIM orders and the
    dashboard must always use Saxo prices -- Yahoo is for historical/
    backtest data only, never for anything that sizes or converts a live
    trade. Triangulates via a pair already in our own universe: EUR{ccy}
    directly if we trade it (every currency here has one except AED/DKK),
    else USD{ccy} + EURUSD. Returns None if Saxo has no live quote right
    now -- callers must treat that as "unknown" (skip sizing/skip this
    pair's contribution), not silently substitute a non-Saxo number.
    """
    if ccy == "EUR":
        return 1.0
    if ccy in _QUOTE_RATE_CACHE:
        return _QUOTE_RATE_CACHE[ccy]

    rate = None
    akey = akey or ""
    direct = _PAIRS_BY_SYMBOL.get(f"EUR{ccy}")
    if direct is not None:
        px = _live_price_retry(direct["uic"], akey)
        if px and px > 0:
            rate = 1.0 / px
    else:
        usd_leg = _PAIRS_BY_SYMBOL.get(f"USD{ccy}")
        eur_usd = _PAIRS_BY_SYMBOL.get("EURUSD")
        if usd_leg is not None and eur_usd is not None:
            px_usd_ccy = _live_price_retry(usd_leg["uic"], akey)
            px_eur_usd = _live_price_retry(eur_usd["uic"], akey)
            if px_usd_ccy and px_usd_ccy > 0 and px_eur_usd and px_eur_usd > 0:
                rate = 1.0 / (px_usd_ccy * px_eur_usd)

    if rate is None:
        logger.warning(f"Saxo has no live quote for {ccy} right now -- treating as unknown")
        return None
    _QUOTE_RATE_CACHE[ccy] = rate
    _save_eur_rate(ccy, rate)   # persist for _eur_rate_for_log()'s analytics fallback
    return rate


_SEK_QUOTE_RATE_CACHE: dict[str, float] = {}


def _sek_per_unit(ccy: str, akey: str | None = None) -> float | None:
    """SEK value of one unit of `ccy` -- the LIVE-account equivalent of
    _eur_per_unit() above. LIVE is SEK-denominated (SIM is EUR); found
    2026-08-25 that _equity_in_quote() was calling the EUR function
    unconditionally, which for LIVE silently treated its 6,000 SEK equity
    as if it were 6,000 EUR -- an ~11x oversizing on every pair not quoted
    directly in SEK. Triangulates via USDSEK (always in the 34-pair core
    universe) + USD{ccy} or {ccy}USD -- EUR is never a needed quote
    currency for any of the 34 core pairs, so no EUR leg is needed here."""
    if ccy == "SEK":
        return 1.0
    if ccy in _SEK_QUOTE_RATE_CACHE:
        return _SEK_QUOTE_RATE_CACHE[ccy]

    akey = akey or ""
    usdsek_pair = _PAIRS_BY_SYMBOL.get("USDSEK")
    if usdsek_pair is None:
        return None
    px_usdsek = _live_price_retry(usdsek_pair["uic"], akey)
    if not px_usdsek or px_usdsek <= 0:
        return None

    rate = None
    if ccy == "USD":
        rate = px_usdsek
    else:
        usd_leg = _PAIRS_BY_SYMBOL.get(f"USD{ccy}")
        if usd_leg is not None:
            px = _live_price_retry(usd_leg["uic"], akey)
            if px and px > 0:
                rate = px_usdsek / px
        else:
            inv_leg = _PAIRS_BY_SYMBOL.get(f"{ccy}USD")
            if inv_leg is not None:
                px = _live_price_retry(inv_leg["uic"], akey)
                if px and px > 0:
                    rate = px_usdsek * px

    if rate is None:
        logger.warning(f"Saxo has no live quote for {ccy} (SEK conversion) right now -- treating as unknown")
        return None
    _SEK_QUOTE_RATE_CACHE[ccy] = rate
    return rate


def _equity_in_quote(equity_base: float, symbol: str) -> float | None:
    """Restate account-base-currency equity in a pair's quote currency, for
    position sizing.

    ATR (and therefore stop distance) is quoted in the pair's quote currency.
    Dividing a base-currency risk budget by a mismatched-currency distance
    is a unit error, so the budget is converted first.

    2026-08-25: made account-env aware -- was unconditionally calling the
    EUR conversion (_eur_per_unit), correct for SIM but silently wrong for
    LIVE (SEK-denominated): treated 6,000 SEK as 6,000 EUR, an ~11x
    oversizing on every pair not quoted directly in SEK. Now picks
    _sek_per_unit() under --account live, _eur_per_unit() under SIM
    (unchanged).
    """
    quote = symbol[3:6] if len(symbol) >= 6 else ""
    if not quote:
        return None
    rate = _sek_per_unit(quote) if ACCOUNT_ENV == "live" else _eur_per_unit(quote)
    if not rate or rate <= 0:
        return None
    return equity_base / rate


def _risk_equity(raw_equity: float) -> float:
    """Cap the sizing base at configured real capital.

    SIM: the broker figure is SIM demo credit (~945,000 EUR), not the user's
    money. Sizing off it made positions ~33x the intended 300,000 SEK. FX
    trades in fine unit increments, so this scales positions down cleanly
    rather than making pairs untradeable (contrast futures, where lumpy
    contract sizes mean the cap blocks whole markets).

    LIVE (2026-08-25): the account itself IS the real money (SEK-denominated,
    6,000 SEK opening balance) -- no conversion needed, just a direct cap in
    case the broker-reported balance ever differs from the intended figure
    (e.g. before the first deposit clears).
    """
    try:
        import atos.capital_config as _CAP
        if ACCOUNT_ENV == "live":
            cap = _CAP.forex_live_risk_equity_sek()
        elif ACCOUNT_ENV == "live_eur":
            cap = _CAP.forex_live_eur_risk_equity_eur()
        else:
            cap = _CAP.forex_risk_equity_eur()
    except Exception as exc:
        logger.warning(f"Could not read forex risk equity cap: {exc}")
        return raw_equity
    if cap <= 0:
        return raw_equity
    return min(raw_equity, cap) if raw_equity > 0 else cap


def _account() -> tuple[float, str]:
    """Resolves (equity, AccountKey) for the CURRENT account (ACCOUNT_ENV).

    2026-08-25: a single Saxo LIVE login was confirmed to control THREE
    sub-accounts (SEK/EUR/USD) -- blindly taking accounts/me's Data[0]
    happened to land on the right (SEK) one only because of list ordering,
    not because anything guaranteed it. Now resolves the AccountKey FIRST,
    explicitly matching Currency=='SEK' under --account live (hard-errors
    if ambiguous rather than guessing -- see saxo_client.get_account_key's
    identical fix), THEN fetches balances scoped to that specific
    AccountKey via Saxo's own AccountKey query param, so equity and the
    account real orders go to are guaranteed to be the same one. SIM is
    unaffected (single account, same Data[0] fallback as always)."""
    equity, key = 0.0, ""
    try:
        info = _get("/port/v1/accounts/me")
        data = info.get("Data", info)
        accounts = data if isinstance(data, list) and data else ([data] if isinstance(data, dict) else [])
        expected_ccy = {"live": "SEK", "live_eur": "EUR"}.get(ACCOUNT_ENV)
        acct = None
        if expected_ccy:
            acct = next((a for a in accounts if isinstance(a, dict) and a.get("Currency") == expected_ccy), None)
            if acct is None and len(accounts) > 1:
                currencies = [a.get("Currency") for a in accounts if isinstance(a, dict)]
                raise RuntimeError(
                    f"Saxo {ACCOUNT_ENV.upper()} login has {len(accounts)} sub-accounts "
                    f"({currencies}) but none is {expected_ccy}-denominated -- refusing "
                    f"to guess which one to trade on."
                )
        if acct is None:
            acct = accounts[0] if accounts else {}
        key = (acct.get("AccountKey", "") if isinstance(acct, dict) else "") or ""
    except Exception as exc:
        logger.warning(f"Could not read AccountKey: {exc}")

    try:
        bal    = _get("/port/v1/balances/me", {"AccountKey": key} if key else None)
        equity = float(bal.get("TotalValue") or bal.get("NetEquityForMargin")
                       or bal.get("CashBalance") or 0)
        raw    = equity
        equity = _risk_equity(equity)
        if equity < raw:
            logger.info(f"  Equity {raw:,.0f} -> sizing off "
                        f"{equity:,.0f} (capped at configured capital)")
    except Exception as exc:
        logger.warning(f"Could not read equity: {exc}")
    return equity, key


# ── Price data ────────────────────────────────────────────────────────────────

# A single FX chart bar whose Ask/Bid spread is wider than this fraction of
# the Bid is not a real market -- it's a stale/frozen quote on one side.
# Confirmed live 2026-08-31: GBPPLN daily bars came back CloseAsk 5.13695 /
# CloseBid 5.01086 (a 2.5% "spread") with the Ask identical bar-to-bar,
# while the Bid tracked the real ~5.05 market. Taking the mid there put the
# close ~0.5% high and, worse, distorted ATR and every R-based decision.
# Real FX spreads -- even wide exotics -- are well under 0.5%. When a bar
# trips this, build its OHLC from the trustworthy Bid side alone (Bid is
# also what a long actually exits at, so it's the honest + conservative
# choice); a genuinely two-sided-bad bar just gets a slightly conservative
# read, which is fine.
_MAX_SANE_BAR_SPREAD = 0.02


def _fetch_history(uic: int, count: int = CHART_BARS) -> pd.DataFrame | None:
    """Fetch daily OHLC for an FxSpot instrument. Mid = (Ask+Bid)/2, except
    bars with a pathological Ask/Bid spread (see _MAX_SANE_BAR_SPREAD) fall
    back to Bid-only OHLC.

    Each strategy enforces its own MIN_BARS; we just need at least a few rows
    here to confirm the instrument responded with real data.
    """
    try:
        resp = _get("/chart/v3/charts", {
            "Uic": uic, "AssetType": ASSET_TYPE,
            "Horizon": 1440, "Count": count + 5,
        })
        raw = resp.get("Data", [])
        rows = []
        bad_spread_bars = 0
        for _idx, bar in enumerate(raw):
            if not isinstance(bar, dict):
                continue
            _is_last = _idx == len(raw) - 1
            if "CloseAsk" in bar and "CloseBid" in bar:
                ask_c = float(bar["CloseAsk"]); bid_c = float(bar["CloseBid"])
                # Historical bars: Close spread only (unchanged -- a
                # completed bar's intraday High/Low legitimately carry a
                # wide spread on a thin pair and rewriting 100+ of them
                # would shift that pair's indicators). The still-forming
                # LAST bar also gets its Open/High/Low checked -- a stale
                # Ask there (NZDPLN 08-31: OpenAsk 2.2481 / OpenBid 2.2078,
                # a 1.8% spread -> mid Open 2.22795, ~1% above the real
                # market) is what every rsi/pullback scan then entered at.
                _legs = [(ask_c, bid_c)]
                if _is_last:
                    _legs += [(bar.get("OpenAsk"), bar.get("OpenBid")),
                              (bar.get("HighAsk"), bar.get("HighBid")),
                              (bar.get("LowAsk"),  bar.get("LowBid"))]
                _wide = any(a is not None and b is not None and float(b) > 0
                            and (float(a) - float(b)) / float(b) > _MAX_SANE_BAR_SPREAD
                            for a, b in _legs)
                if _wide:
                    bad_spread_bars += 1
                    o = float(bar.get("OpenBid",  bid_c))
                    h = float(bar.get("HighBid",  bid_c))
                    l = float(bar.get("LowBid",   bid_c))
                    c = bid_c
                else:
                    o = (float(bar.get("OpenAsk",  ask_c)) + float(bar.get("OpenBid",  bid_c))) / 2
                    h = (float(bar.get("HighAsk",  ask_c)) + float(bar.get("HighBid",  bid_c))) / 2
                    l = (float(bar.get("LowAsk",   ask_c)) + float(bar.get("LowBid",   bid_c))) / 2
                    c = (ask_c + bid_c) / 2
            elif "Close" in bar:
                o = float(bar.get("Open",  bar["Close"]))
                h = float(bar.get("High",  bar["Close"]))
                l = float(bar.get("Low",   bar["Close"]))
                c = float(bar["Close"])
            else:
                continue
            if c > 0:
                rows.append({"Open": o, "High": h, "Low": l, "Close": c})
        if bad_spread_bars:
            logger.warning(f"UIC {uic}: {bad_spread_bars}/{len(rows)} bars had a "
                           f">{_MAX_SANE_BAR_SPREAD:.0%} Ask/Bid spread — used Bid-only "
                           f"OHLC for those (stale Ask on the chart feed)")
        if len(rows) >= 5:
            return pd.DataFrame(rows)
        logger.debug(f"UIC {uic}: only {len(rows)} bars returned")
        return None
    except Exception as exc:
        logger.warning(f"Chart fetch failed for UIC {uic}: {exc}")
        return None


# The last daily bar is still forming -- on a thin instrument Saxo's SIM
# chart feed can leave its Close frozen at an early print (hours old, and
# up to ~1% from the live tradable quote) while /trade/v1/infoprices stays
# real-time. A strategy then enters at that stale Close and the position is
# born ~1% underwater vs the price it can actually be closed at, so it
# stops straight out -- and, because the chart stays frozen, the same
# signal re-fires every scan (the NZDPLN pullback/advanced_pullback_master
# re-entry loop, 2026-09-01). Repair the forming bar to the live mid
# whenever they diverge by more than this.
_STALE_FORMING_BAR_TOL = 0.004   # 0.4%


def _repair_stale_forming_bars(market_data: dict, live_prices: dict) -> int:
    """Overwrite the last (still-forming) daily bar's Close with the live
    tradable mid when the chart feed has left it stale. Clamps High/Low so
    the bar stays internally consistent. Returns the number repaired."""
    n = 0
    for sym, df in market_data.items():
        live = live_prices.get(sym)
        if df is None or not live or len(df) == 0:
            continue
        try:
            last = float(df["Close"].iloc[-1])
        except (KeyError, IndexError, ValueError):
            continue
        if last <= 0 or abs(last - live) / live <= _STALE_FORMING_BAR_TOL:
            continue
        i = df.index[-1]
        df.at[i, "Close"] = live
        df.at[i, "High"] = max(float(df.at[i, "High"]), live)
        df.at[i, "Low"] = min(float(df.at[i, "Low"]), live)
        n += 1
        logger.warning(
            f"  [chart] {sym}: forming-bar close {last:.5f} was {(last/live-1)*100:+.2f}% "
            f"off the live quote {live:.5f} — repaired to live (stale SIM chart feed)")
    return n


def _add_held_position_history(market_data: dict, positions: dict) -> None:
    """Fetch daily history for any OPEN position whose pair is NOT in the
    scanned universe and mutate it into `market_data`.

    Found live 2026-08-31: `rsi:GBPPLN` on the LIVE EUR account (opened as
    an exotic 2026-08-26, before that account was narrowed to CORE_SYMBOLS
    only) had `market_data.get("GBPPLN")` return None every run for days,
    because `market_data` is only ever built for `active_pairs`. With
    df=None EVERY exit path silently no-ops -- the generic trailing block,
    _apply_breakeven_stop, _apply_profit_ladder_stop AND strategy_rsi.
    should_exit (which early-returns on df=None, so even the 12-day time
    stop never fires). The position ran +30 -> -24 PLN managed only by its
    original entry-day broker stop/TP. This closes that gap for every
    strategy: a held position on a since-dropped pair is still fully
    exit-managed, not just protected by its resting broker orders."""
    held = {k.split(":", 1)[1] for k in positions if ":" in k}
    for sym in held - set(market_data):
        pi = _PAIRS_BY_SYMBOL.get(sym)
        if pi is None:
            logger.warning(f"  [exits] held position on {sym} but it's not in the "
                           f"pair universe at all — can't fetch history to manage it")
            continue
        market_data[sym] = _fetch_history(pi["uic"])
        if market_data[sym] is None:
            logger.warning(f"  [exits] {sym}: held position outside the scanned "
                           f"universe and its history fetch returned nothing — "
                           f"still protected by its broker stop/TP only this run")


def _fetch_history_h1(uic: int, count: int = 48) -> pd.DataFrame | None:
    """Fetch H1 OHLC bars (Horizon=60 minutes) with UTC hour label.

    Returns DataFrame with columns Open/High/Low/Close/HourUTC.
    Used by the session gap strategy to find the reference bar before each session.
    """
    try:
        resp = _get("/chart/v3/charts", {
            "Uic": uic, "AssetType": ASSET_TYPE,
            "Horizon": 60, "Count": count + 2,
        })
        rows = []
        for bar in resp.get("Data", []):
            if not isinstance(bar, dict):
                continue
            # Extract timestamp for HourUTC label
            ts_raw = bar.get("Time") or bar.get("OpenTime") or ""
            hour_utc = -1
            if ts_raw:
                try:
                    dt = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                    hour_utc = dt.astimezone(timezone.utc).hour
                except Exception:
                    pass

            if "CloseAsk" in bar and "CloseBid" in bar:
                ask_c = float(bar["CloseAsk"]); bid_c = float(bar["CloseBid"])
                o = (float(bar.get("OpenAsk",  ask_c)) + float(bar.get("OpenBid",  bid_c))) / 2
                h = (float(bar.get("HighAsk",  ask_c)) + float(bar.get("HighBid",  bid_c))) / 2
                l = (float(bar.get("LowAsk",   ask_c)) + float(bar.get("LowBid",   bid_c))) / 2
                c = (ask_c + bid_c) / 2
            elif "Close" in bar:
                o = float(bar.get("Open",  bar["Close"]))
                h = float(bar.get("High",  bar["Close"]))
                l = float(bar.get("Low",   bar["Close"]))
                c = float(bar["Close"])
            else:
                continue
            if c > 0:
                rows.append({"Open": o, "High": h, "Low": l, "Close": c, "HourUTC": hour_utc})
        if len(rows) >= 4:
            return pd.DataFrame(rows)
        return None
    except Exception as exc:
        logger.warning(f"H1 chart fetch failed for UIC {uic}: {exc}")
        return None


def _detect_gap_session() -> str | None:
    """Return the active gap session based on current UTC time, or None.

    FX session opens (UTC):
      Sydney/Weekly — Sunday 22:00 UTC (FX market reopens after weekend close)
      Tokyo         — Monday-Friday 00:00 UTC
      London        — Monday-Friday 07:00 UTC  (largest daily volume, 35% of FX)
      New York      — Monday-Friday 12:00 UTC

    Entry windows (first 90 minutes of each session):
      weekly  — Sun 22:00 UTC through Mon 06:00 UTC
                (Sunday 22:00 PKT Mon 03:00 → correct: dow=7, h>=22)
      tokyo   — Mon-Fri 00:00-01:30 UTC   (skipped on Monday — covered by weekly)
      london  — Mon-Fri 07:00-08:30 UTC
      newyork — Mon-Fri 12:00-13:30 UTC
    """
    now  = datetime.now(timezone.utc)
    dow  = now.isoweekday()   # 1=Mon … 7=Sun
    h    = now.hour
    m    = now.minute

    # Weekly: Sunday 22:00 UTC → Monday 06:00 UTC (FX reopens Sunday night)
    if (dow == 7 and h >= 22) or (dow == 1 and h < 6):
        return "weekly"

    # Session gaps: Monday-Friday only
    if 1 <= dow <= 5:
        if h == 7 or (h == 8 and m < 30):
            return "london"
        # Tokyo: skip Monday (00:00-01:30 UTC Monday is already covered by weekly window above)
        if dow >= 2 and (h == 0 or (h == 1 and m < 30)):
            return "tokyo"
        if h == 12 or (h == 13 and m < 30):
            return "newyork"
    return None


def _fx_market_open(now_utc: datetime | None = None) -> bool:
    """True when the FX spot market is inside its normal trading week.

    FX trades continuously from ~Sunday 22:00 UTC to ~Friday 22:00 UTC and
    is fully closed in between (all of Saturday, and Sunday before 22:00).
    The real weekend boundary shifts between 21:00 and 22:00 UTC with US
    daylight saving; this uses 22:00 UTC at both ends -- matching
    _detect_gap_session()'s Sunday-reopen constant -- and deliberately errs
    toward "closed" in that 1-hour margin. The only thing gated on this is
    whether to PLACE a new entry order: a daily strategy losing the first
    hour of the FX week is immaterial, a Market order resting on a closed
    market and filling later at an unrelated price is not.

    Per-currency local-market hours (TRY/MXN/ZAR etc. trading thin outside
    their home session even mid-week) are deliberately NOT modelled here --
    the live per-pair spread check (MAX_SPREAD_PCT) already rejects a pair
    whose market is currently illiquid, the same 2026-08-21 design choice
    that picked spread-checking over a per-currency trading-hours table.
    So mid-week this returns True for every pair and scanning stays full.
    """
    now = now_utc or datetime.now(timezone.utc)
    dow = now.isoweekday()   # 1=Mon … 7=Sun
    if dow == 6:                       # Saturday — closed all day
        return False
    if dow == 7 and now.hour < 22:     # Sunday, before the 22:00 UTC reopen
        return False
    if dow == 5 and now.hour >= 22:    # Friday, after the 22:00 UTC close
        return False
    return True


def _momentum_rank(market_data: dict, top_n: int) -> set:
    """
    Rank all pairs by 20-day normalised momentum (price-change / ATR).
    Returns the set of top_n symbol names. Exits always run on all pairs;
    only NEW entries are restricted to these top performers.
    """
    scores = {}
    for sym, df in market_data.items():
        if df is None or len(df) < 22:
            continue
        close_col = "Close" if "Close" in df.columns else (
                    "CloseAsk" if "CloseAsk" in df.columns else None)
        if close_col is None:
            continue
        c = df[close_col].dropna()
        if len(c) < 22:
            continue
        move = abs(float(c.iloc[-1]) - float(c.iloc[-21]))
        hi   = df["High"].dropna()   if "High"    in df.columns else \
               df["HighAsk"].dropna() if "HighAsk" in df.columns else c
        lo   = df["Low"].dropna()    if "Low"     in df.columns else \
               df["LowAsk"].dropna()  if "LowAsk"  in df.columns else c
        tr   = pd.concat([(hi - lo).abs(),
                           (hi - c.shift(1)).abs(),
                           (lo - c.shift(1)).abs()], axis=1).max(axis=1)
        atr  = float(tr.rolling(14).mean().iloc[-1])
        if atr > 0:
            scores[sym] = move / atr
    ranked   = sorted(scores, key=scores.__getitem__, reverse=True)
    selected = set(ranked[:top_n])
    skipped  = sorted(set(scores) - selected)
    if skipped:
        logger.info(f"Momentum pre-filter: top {top_n}/{len(scores)} pairs for entries "
                    f"| filtered: {skipped}")
    else:
        logger.info(f"Momentum pre-filter: all {len(scores)} pairs qualify")
    return selected


def _spread_pct(uic: int) -> float | None:
    """Live bid/ask spread as % of mid price — proxy for current liquidity.

    Checked at signal time, not tied to any fixed trading-hours table, so it
    reacts to the pair's actual current market condition (a EUR pair during a
    holiday-thin session gets caught the same as an EM pair outside its home
    hours) instead of a static per-currency assumption. Returns None if the
    quote can't be fetched — callers should treat that as "can't verify,
    proceed" rather than blocking the trade on a data hiccup.
    """
    try:
        resp = _get("/trade/v1/infoprices", {"Uic": uic, "AssetType": ASSET_TYPE, "FieldGroups": "Quote"})
        q   = resp.get("Quote", {})
        bid, ask = float(q.get("Bid", 0)), float(q.get("Ask", 0))
        if bid <= 0 or ask <= 0:
            return None
        mid = (bid + ask) / 2
        return (ask - bid) / mid * 100
    except Exception as exc:
        logger.debug(f"Spread check failed for UIC {uic}: {exc}")
        return None


def _fetch_live_prices(pairs: list) -> dict:
    """Current bid/ask mid for a list of pairs, {symbol: mid}.

    Uses Saxo's BATCH endpoint (/trade/v1/infoprices/list, many Uics per
    call) -- the per-pair loop this replaced was ~184 sequential calls on a
    full scan (>2 min). Chunked at 50 Uics; a failed chunk just omits those
    pairs (caller treats a missing symbol as "no live price")."""
    by_uic = {p["uic"]: p["symbol"] for p in pairs if p.get("uic")}
    uics = list(by_uic)
    prices: dict = {}
    for i in range(0, len(uics), 50):
        chunk = uics[i:i + 50]
        try:
            resp = _get("/trade/v1/infoprices/list", {
                "Uics": ",".join(str(u) for u in chunk),
                "AssetType": ASSET_TYPE, "FieldGroups": "Quote",
            })
            for row in resp.get("Data", []):
                q = row.get("Quote", {})
                mid = q.get("Mid")
                if mid is None and q.get("Ask") and q.get("Bid"):
                    mid = (float(q["Ask"]) + float(q["Bid"])) / 2
                sym = by_uic.get(row.get("Uic"))
                if sym and mid and float(mid) > 0:
                    prices[sym] = float(mid)
        except Exception as exc:
            logger.debug(f"Batch live-price fetch failed for {len(chunk)} uics: {exc}")
    return prices


def _position_net_pnl_quote_ccy(uic: int, qty: float, direction: str,
                                entry_price: float) -> float | None:
    """Authoritative NET realized P&L in the position's own QUOTE currency
    (e.g. PLN for EURPLN), straight from Saxo's own live figures — not our
    own rate estimate, and not just the raw price move.

    Saxo's /port/v1/positions/me returns PositionView.ProfitLossOnTrade
    (pure price movement, entry vs current price x quantity) and
    TradeCostsTotal (spread cost at entry + accrued overnight swap/
    financing), both in the pair's own quote currency. Adding them gives
    the true net figure, not the gross one (confirmed live 2026-08-21: a
    position showing +4,362 gross also carried -65 of TradeCostsTotal, not
    yet subtracted).

    DELIBERATELY does NOT use the "...InBaseCurrency" variants of these
    same fields. Confirmed live 2026-08-26 on the live_eur account's first
    closed trade: those fields are NOT denominated in this sub-account's
    own currency (EUR) — they use Saxo's Client-level base currency, SEK
    (this Saxo login's primary/default sub-account), regardless of which
    AccountKey the request runs under. A -1.29 EUR real net loss (confirmed
    against Saxo's own web trader Closed Positions view) came back from
    these fields as -14.19 -- off by almost exactly the EUR/SEK rate,
    because that number was SEK being read as if it were already EUR. The
    caller must convert this function's quote-currency return value itself,
    via this codebase's own _eur_per_unit() (Saxo-quote-based, already
    correct), never via these "InBaseCurrency" fields for a live/live_eur
    account.

    Multiple strategies can hold positions on the same UIC simultaneously, so
    match on quantity + entry price (not just UIC) to find the right row.
    """
    try:
        resp = _get("/port/v1/positions/me")
    except Exception as exc:
        logger.warning(f"Position P&L lookup failed for UIC {uic}: {exc}")
        return None
    want_amount = qty if direction in ("Buy", "BUY") else -qty
    best, best_diff = None, None
    for p in resp.get("Data", []):
        pb = p.get("PositionBase", {})
        if pb.get("Uic") != uic or pb.get("AssetType") != "FxSpot":
            continue
        amount = pb.get("Amount", 0)
        if abs(amount) != abs(want_amount):
            continue
        if (amount > 0) != (want_amount > 0):
            continue
        diff = abs((pb.get("OpenPrice") or 0) - entry_price)
        if best_diff is None or diff < best_diff:
            best, best_diff = p, diff
    if best is None:
        return None
    pv  = best.get("PositionView", {})
    pnl = pv.get("ProfitLossOnTrade")
    if pnl is None:
        return None
    costs = pv.get("TradeCostsTotal") or 0.0
    return float(pnl) + float(costs)   # costs is already negative-signed


def _sane_net_pnl_quote(net_quote, entry, exit_px, qty, is_long, uic, akey, sym):
    """Sanity-gate Saxo's positions/me net P&L.

    On LIVE that figure (ProfitLossOnTrade + TradeCostsTotal) is
    authoritative. On SIM it is UNRELIABLE -- confirmed 2026-09-01: SIM
    reported ~+$11 net for an MXNUSD rsi 170,000 Buy that moved -$3.91 on
    price, so the trade booked +8.23 EUR, the WIN/LOSS email said
    "WIN +$10 / -0.04%", the ML label recorded the loss as a win, and the
    observation card carried a +0.12R for the journal / give-back.

    Commission is always a COST: a trustworthy net is <= the gross price
    move and within a sane band of it. When Saxo's number fails that,
    rebuild net = gross price move - real Saxo-quoted round-trip commission
    (or a small modeled cost if that lookup fails). Returns
    (value, was_rebuilt)."""
    if net_quote is None:
        return None, False
    gross = (exit_px - entry) * qty if is_long else (entry - exit_px) * qty
    implied_cost = gross - net_quote                    # >0 == Saxo net below gross (normal)
    notional = abs(entry * qty) or 1.0
    plausible = (implied_cost >= -1e-9
                 and abs(implied_cost) <= max(notional * 0.01, abs(gross) * 2.0 + 10.0))
    if plausible:
        return net_quote, False
    rt = None
    try:
        rt = _round_trip_cost_quote_ccy(uic, qty, akey)
    except Exception:
        rt = None
    if rt is None:
        rt = min(notional * 0.0006, abs(gross) + notional * 0.0002)
    fixed = gross - abs(rt)
    logger.warning(
        f"  [PNL] {sym}: Saxo net {net_quote:.2f} implausible vs price-move {gross:.2f} "
        f"(implied cost {implied_cost:.2f}) — rebuilt as {fixed:.2f} "
        f"(price move − round-trip cost {abs(rt):.2f})")
    return fixed, True


# ── Fill confirmation ───────────────────────────────────────────────────────
# Saxo's order POST (/trade/v2/orders) returns ONLY an OrderId -- never a
# fill confirmation and never a fill price. Until 2026-09-01 the code took
# "got an OrderId back" to mean "filled at sig['close']" -- sig['close']
# being the scan chart's last bar close, routinely 10-60 min stale by the
# time the order actually executes. Confirmed live on the EUR account:
# MXNUSD booked at entry 0.058876 / exit 0.0588435, the real Saxo fills
# were 0.058687 / 0.058811 -- a 0.32% entry error, a third of that trade's
# stop distance, and enough to flip the recorded P&L sign. That stale price
# then poisons the R-multiple, MAE/MFE, the observation cards and every
# downstream analysis (P2 give-back etc).
#
# After every real (non-paper) entry we now poll positions/me for the
# position this order opened (PositionBase.SourceOrderId == our OrderId),
# take its OpenPrice as the true average fill, and -- if no such position
# ever appears -- treat the order as accepted-but-unfilled (LIVE: cancel it
# and its bracket legs, record nothing; SIM: keep it at a live quote).
_FILL_CONFIRM_ATTEMPTS = 3
_FILL_CONFIRM_DELAY_S   = 1.5
_FILL_RECENT_OPEN_S     = 180
_FILL_LOG_THRESHOLD     = 0.0005   # 5 bp: log the correction when it matters


def _confirm_entry_fill(entry_oid: str, uic: int) -> tuple[bool, float]:
    """(filled, real_average_fill_price) for the position `entry_oid` opened.

    Primary match: PositionBase.SourceOrderId == entry_oid. Fallback: a
    position on the same Uic whose ExecutionTimeOpen is within the last
    _FILL_RECENT_OPEN_S seconds (covers Saxo re-iding the source order on an
    aggregated/partial fill). positions/me is already scoped to this
    account by _get()'s env-aware headers. Never raises."""
    want = str(entry_oid)
    now = datetime.now(timezone.utc)
    for attempt in range(_FILL_CONFIRM_ATTEMPTS):
        if attempt:
            time.sleep(_FILL_CONFIRM_DELAY_S)
        try:
            data = _get("/port/v1/positions/me").get("Data", [])
        except Exception:
            continue
        fallback_px = None
        for p in data:
            pb = p.get("PositionBase", {})
            op = pb.get("OpenPrice")
            if str(pb.get("SourceOrderId", "")) == want and op:
                return True, float(op)
            if pb.get("Uic") == uic and op and fallback_px is None:
                opened = pb.get("ExecutionTimeOpen", "")
                try:
                    dt = datetime.fromisoformat(opened.replace("Z", "+00:00"))
                    if (now - dt).total_seconds() <= _FILL_RECENT_OPEN_S:
                        fallback_px = float(op)
                except (ValueError, AttributeError):
                    pass
        if fallback_px is not None:
            return True, fallback_px
    return False, 0.0


def _live_position_open(uic: int, qty: float, direction: str,
                        n_tracked: int) -> str:
    """Is the LIVE position we're about to close still actually open at the
    broker?  Returns "open", "gone", or "unknown".

    Guards the exit path. should_exit() runs off LOCAL state, which can be
    stale -- a broker-side stop/TP fill that no exits-check has reconciled
    yet (e.g. the fill happened while the runner wasn't running at all). A
    market close order for a position that no longer exists does NOT flatten
    anything: FX has no reduce-only, so Saxo OPENS a fresh position the
    other way. Confirmed live 2026-09-02 -- a ~2-day-stale rsi:NZDCAD
    "hard_stop" sent Sell 9,000 against a flat account and opened a 9,000
    short.

    Only "gone" skips the close order. "unknown" (lookup failed, or a
    snapshot that looks degraded) FALLS THROUGH to the normal close so a
    genuine exit is never suppressed by a transient API problem.

    A same-Uic, same-DIRECTION position whose size merely differs from
    pos["quantity"] (aggregation, a partial manual close, a stop-heal that
    re-placed a different lot) is still "open" -- NOT "gone". Booking a
    phantom close there would leave a real live position untracked and
    unprotected (naked). A residual after our normal close is only a
    reconciliation nit; a naked position is a real hazard. Only a genuinely
    absent (or opposite-direction / netted-out) position is "gone".

    `n_tracked` = how many positions local state currently holds. If the
    broker snapshot is empty but we track several, the snapshot is suspect
    -> "unknown", not "gone".
    """
    try:
        data = _get("/port/v1/positions/me").get("Data", [])
    except Exception as exc:
        logger.warning(f"  [exit-guard] position lookup failed for UIC {uic}: {exc}")
        return "unknown"
    want_amount = qty if direction in ("Buy", "BUY") else -qty
    fx_rows = 0
    same_dir_other_size = None
    for p in data:
        pb = p.get("PositionBase", {})
        if pb.get("AssetType") != "FxSpot":
            continue
        fx_rows += 1
        amount = pb.get("Amount", 0) or 0
        if pb.get("Uic") != uic or amount == 0:
            continue
        same_dir = (amount > 0) == (want_amount > 0)
        if abs(amount) == abs(want_amount) and same_dir:
            return "open"
        if same_dir:
            same_dir_other_size = amount
    if same_dir_other_size is not None:
        logger.warning(f"  [exit-guard] UIC {uic}: broker holds {same_dir_other_size:+,.0f} "
                       f"this direction vs tracked {want_amount:+,.0f} — size mismatch, "
                       f"treating as OPEN (a normal close is safe; a phantom-book would "
                       f"strand a live position)")
        return "open"
    # No matching row. Trust that only if the snapshot itself looks healthy:
    # at least one FxSpot position came back, OR we genuinely track nothing
    # else. A totally empty snapshot while we hold several tracked positions
    # is a bad fetch, not a flat account.
    if fx_rows == 0 and n_tracked > 1:
        logger.warning(f"  [exit-guard] UIC {uic}: 0 FxSpot positions in the "
                       f"snapshot but {n_tracked} tracked locally — treating "
                       f"as an unreliable fetch, not a closed position")
        return "unknown"
    return "gone"


def _confirm_exit_fill(uic: int, qty: float, direction: str) -> float | None:
    """True ClosingPrice for the position just closed on `uic` (best-effort,
    2 quick attempts). Matches the most recently closed position on this Uic
    whose ExecutionTimeClose is within the last _FILL_RECENT_OPEN_S seconds
    and whose Amount matches. Returns None if nothing suitable -- caller
    keeps its live-quote estimate. Never raises."""
    now = datetime.now(timezone.utc)
    want_amt = abs(qty)
    for attempt in range(2):
        if attempt:
            time.sleep(_FILL_CONFIRM_DELAY_S)
        try:
            data = _get("/port/v1/closedpositions/me",
                        {"FieldGroups": "ClosedPosition"}).get("Data", [])
        except Exception:
            continue
        best, best_dt = None, None
        for c in data:
            cp = c.get("ClosedPosition", {})
            if cp.get("Uic") != uic:
                continue
            if abs(abs(cp.get("Amount") or 0) - want_amt) > max(1.0, want_amt * 0.02):
                continue
            closed = cp.get("ExecutionTimeClose", "")
            try:
                dt = datetime.fromisoformat(closed.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                continue
            if (now - dt).total_seconds() > _FILL_RECENT_OPEN_S:
                continue
            if best_dt is None or dt > best_dt:
                best, best_dt = cp.get("ClosingPrice"), dt
        if best:
            return float(best)
    return None


def _live_price(uic: int, account_key: str) -> float | None:
    try:
        params = {"Uic": uic, "AssetType": ASSET_TYPE, "FieldGroups": "Quote"}
        if account_key:
            params["AccountKey"] = account_key
        resp = _get("/trade/v1/infoprices", params)
        q    = resp.get("Quote", {})
        mid  = q.get("Mid")
        if mid is None and q.get("Ask") and q.get("Bid"):
            mid = (float(q["Ask"]) + float(q["Bid"])) / 2
        return float(mid) if mid else None
    except Exception as exc:
        logger.warning(f"Live price failed for UIC {uic}: {exc}")
        return None


# 2026-08-26: minimum expected-edge-to-cost ratio a signal must clear before
# it's allowed to open a position. Root-caused the live_eur account's first
# closed trade (EURPLN, RSI Pullback): confirmed live via Saxo's own
# /trade/v1/infoprices Commissions field that Saxo charges a FLAT ~5.15 EUR
# round-trip commission per 1,000-unit FX trade -- the SAME ~5.15 EUR
# regardless of pair (EURUSD, GBPUSD, EURPLN, USDTRY, EURHUF all converted to
# within a few cents of each other). This is NOT an exotic-pair problem --
# it's a position-size-vs-flat-cost problem on ANY pair whose position is
# floored at the 1,000-unit minimum. That trade's target implied ~223 pips
# needed just to break even and only captured 167 -- a real, right-direction
# trade that was still a guaranteed-thin-or-negative bet before it opened,
# because nothing checked whether the position's own economics could clear
# this flat cost. 3x is deliberately conservative -- it isn't tuned yet,
# see docs/atos_ai_implementation_plan.md-style backtest-before-trust
# discipline: validate against SIM history before this gates anything LIVE.
MIN_EDGE_TO_COST_RATIO = 3.0

# 2026-09-01 (user): LIVE gets a STRICTER version of the viability checks
# after MXNUSD closed a real -EUR3.05 loss whose only problem was size --
# +EUR2.13 gross price move, -EUR5.19 flat commission (a legacy 1,000-lot
# trade whose R had collapsed to ~EUR5, so the flat fee was ~100% of R). On
# real money:
#   * the edge-to-cost ratio on the 2R target is 5x, not 3x;
#   * the RECOVERY-vs-COST gate below (RSI only) -- a realistic partial
#     recovery must clear the all-in round-trip cost by a healthy margin;
#   * an UNKNOWN round-trip cost (the infoprices Commissions lookup
#     failed) blocks the trade on LIVE instead of passing -- don't gamble
#     real money on a transient API hiccup. SIM still treats unknown as
#     "don't block" (forward-test continuity).
MIN_LIVE_EDGE_TO_COST_RATIO = 5.0

# ── LIVE RSI recovery-vs-cost viability gate (2026-09-01, user) ────────────
# REPLACES both the generic MIN_LIVE_NOTIONAL_EUR and the pair-specific
# LIVE_RSI_MIN_UNITS table -- ONE pair-independent rule.
#
# Rationale: at a FIXED EUR45 risk the economics are already near-identical
# across every LIVE pair -- realised R EUR37-45, flat commission ~EUR5.18,
# so commission is ~12% of R everywhere and a 0.5R recovery clears the
# all-in cost by 3.1-4.0x on all 17 HIGH_VOLUME pairs. There is no
# per-pair number to encode. What actually breaks the economics is R
# COLLAPSING (tight stop + lot rounding, or a future low-notional pair) --
# that is what this gate catches, directly.
#
# The gate: ASSUMED_EXIT_R * realised_R_eur  >=  MIN_RECOVERY_MULT * all_in_cost_eur
# all_in_cost = flat Saxo commission + one live spread crossing + a small
# round-trip slippage buffer. Tom/Next financing is deliberately NOT in the
# gate: it is a HOLDING cost (only accrues if held overnight) and its sign
# is not even fixed (carry can be earned). It is tracked in the analysis
# report and will show up in the journal's realised per-trade costs.
#
# ASSUMED_EXIT_R = 0.5 is provisional -- a stand-in for the real median RSI
# exit, which the AI trade journal will measure over ~1 week of clean LIVE
# data; then this one constant is updated (the mechanism does not change).
# MIN_RECOVERY_MULT = 3.0 is the user's chosen safety margin. At EUR45 risk
# all 17 HIGH_VOLUME pairs clear it today (ratios 3.0-4.0x); the gate only
# bites if R collapses.
RSI_LIVE_ASSUMED_EXIT_R      = 0.5     # provisional; journal replaces with measured median
RSI_LIVE_MIN_RECOVERY_MULT   = 3.0     # 0.5R must be >= 3x the all-in round-trip cost
RSI_LIVE_SLIPPAGE_PIPS       = 0.5     # round-trip slippage buffer (~0.25 pip each side)
_EM_SWAP_SURCHARGE_CCY = frozenset({"MXN", "RUB", "TRY", "ZAR"})   # +0.30% Tom/Next markup (report only)


def _live_all_in_cost_eur(commission_eur: float | None, spread_pct: float | None,
                          entry_px: float, notional_eur: float | None,
                          quote_ccy: str) -> float | None:
    """All-in round-trip TRANSACTION cost of a LIVE trade, in EUR:
        flat Saxo commission
      + crossing the live bid/ask spread once (half in, half out)
      + an RSI_LIVE_SLIPPAGE_PIPS round-trip slippage buffer.
    Financing is a separate holding cost -- not included here. Pure (no API
    calls). Returns None if the flat commission (the dominant term) is
    unknown.
    """
    if commission_eur is None:
        return None
    cost = float(commission_eur)
    if notional_eur:
        if spread_pct:                                     # _spread_pct returns % of mid
            cost += (spread_pct / 100.0) * notional_eur
        if entry_px:
            pip = 0.01 if quote_ccy == "JPY" else 0.0001
            cost += (RSI_LIVE_SLIPPAGE_PIPS * pip / entry_px) * notional_eur
    return cost


def _min_edge_ratio() -> float:
    return (MIN_LIVE_EDGE_TO_COST_RATIO if ACCOUNT_ENV in ("live", "live_eur")
            else MIN_EDGE_TO_COST_RATIO)


_COMMISSION_CACHE: dict[tuple[int, float], float] = {}


def _round_trip_cost_quote_ccy(uic: int, qty: float, account_key: str) -> float | None:
    """Live-quoted, position-size-aware estimate of this trade's total round-
    trip commission, in the pair's own quote currency -- straight from
    Saxo's own /trade/v1/infoprices Commissions field (CostBuy/CostSell,
    each already an entry-or-exit-side figure for this exact Amount), not a
    guessed/hardcoded number. Returns None if the lookup fails -- callers
    must treat that as "unknown," never silently assume zero cost.

    Cached per (uic, qty) for the lifetime of this process (same pattern as
    _QUOTE_RATE_CACHE above) -- unlike price, Saxo's commission schedule
    doesn't move intra-run, so re-querying it for every candidate signal on
    the same pair/size within one scan is pure wasted latency, not freshness.
    """
    cache_key = (uic, qty)
    if cache_key in _COMMISSION_CACHE:
        return _COMMISSION_CACHE[cache_key]
    try:
        params = {"Uic": uic, "AssetType": ASSET_TYPE, "Amount": qty,
                  "FieldGroups": "Commissions"}
        if account_key:
            params["AccountKey"] = account_key
        resp = _get("/trade/v1/infoprices", params)
        comm = resp.get("Commissions", {})
        cost_buy = comm.get("CostBuy")
        if cost_buy is None:
            return None
        cost = float(cost_buy) * 2   # one side each for entry + exit
        _COMMISSION_CACHE[cache_key] = cost
        return cost
    except Exception as exc:
        logger.warning(f"Commission lookup failed for UIC {uic}: {exc}")
        return None


# ── State ─────────────────────────────────────────────────────────────────────

def _load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                state = json.load(f)
            # Migrate old single-strategy keys ("EURUSD" → "ema:EURUSD")
            positions = state.setdefault("positions", {})
            old_keys  = [k for k in positions if ":" not in k]
            for k in old_keys:
                positions[f"ema:{k}"] = positions.pop(k)
            return state
        except Exception:
            pass
    return {"positions": {}, "last_run": None}


def _save_state(state: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2, default=str)
        # Atomic replace — works even when the target was created by another user
        if os.path.exists(STATE_FILE):
            os.replace(tmp, STATE_FILE)
        else:
            os.rename(tmp, STATE_FILE)
    except PermissionError:
        # State file owned by SYSTEM (Task Scheduler). Delete and recreate.
        try:
            os.remove(STATE_FILE)
        except Exception:
            pass
        try:
            os.rename(tmp, STATE_FILE)
        except Exception as e:
            logger.error(f"Cannot write state file — run PowerShell as Admin and fix permissions: {e}")
            if os.path.exists(tmp):
                os.remove(tmp)


GAP_COOLDOWN_FILE = os.path.join(DATA_DIR, "gap_cooldown.json")
# 2026-08-29: "gap_weekend" gets its own cooldown file so a symbol traded
# by one gap strategy this week doesn't block the other -- they're being
# A/B tested independently, not sharing exhausted-symbol state.
GAP_COOLDOWN_FILES = {
    "gap":         GAP_COOLDOWN_FILE,
    "gap_weekend": os.path.join(DATA_DIR, "gap_weekend_cooldown.json"),
}


def _gap_week_key() -> str:
    """ISO week key for the current week, e.g. '2026-W34'."""
    today = datetime.now(timezone.utc)
    return f"{today.isocalendar()[0]}-W{today.isocalendar()[1]:02d}"


def _load_gap_cooldown(strat_name: str = "gap") -> set:
    """Return the set of symbols exhausted for this week's gap event."""
    week = _gap_week_key()
    path = GAP_COOLDOWN_FILES.get(strat_name, GAP_COOLDOWN_FILE)
    if os.path.exists(path):
        try:
            with open(path) as f:
                data = json.load(f)
            if data.get("week_key") == week:
                return set(data.get("exhausted", []))
        except Exception:
            pass
    return set()


def _mark_gap_exhausted(sym: str, strat_name: str = "gap") -> None:
    """Add sym to this week's gap cooldown so it cannot re-enter."""
    week = _gap_week_key()
    exhausted = _load_gap_cooldown(strat_name)
    exhausted.add(sym)
    os.makedirs(DATA_DIR, exist_ok=True)
    path = GAP_COOLDOWN_FILES.get(strat_name, GAP_COOLDOWN_FILE)
    try:
        with open(path, "w") as f:
            json.dump({"week_key": week, "exhausted": sorted(exhausted)}, f, indent=2)
    except Exception as e:
        logger.warning(f"gap_cooldown: could not write {path}: {e}")


# 2026-08-29: "london_breakout_v2"'s fix #4 (repeat-signal protection) --
# once a symbol trades in a given UTC-date+session, it's done for that
# session-day even if the position closes early while price is still
# beyond the same range boundary. Naturally self-pruning: only today's UTC
# date's keys are ever kept, so the file never grows unbounded.
LBO_V2_SESSION_COOLDOWN_FILE = os.path.join(DATA_DIR, "lbo_v2_session_cooldown.json")


def _load_lbo_v2_session_cooldown() -> set:
    """Return the set of 'YYYY-MM-DD:SYMBOL:session_label' keys already
    traded today (UTC) by london_breakout_v2."""
    if not os.path.exists(LBO_V2_SESSION_COOLDOWN_FILE):
        return set()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        with open(LBO_V2_SESSION_COOLDOWN_FILE) as f:
            data = json.load(f)
        return {k for k in data.get("traded", []) if k.startswith(today + ":")}
    except Exception:
        return set()


def _mark_lbo_v2_session_traded(session_key: str) -> None:
    """Add session_key (from the signal's own 'session_key' field) to
    today's london_breakout_v2 cooldown."""
    traded = _load_lbo_v2_session_cooldown()
    traded.add(session_key)
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        with open(LBO_V2_SESSION_COOLDOWN_FILE, "w") as f:
            json.dump({"traded": sorted(traded)}, f, indent=2)
    except Exception as e:
        logger.warning(f"lbo_v2_session_cooldown: could not write {LBO_V2_SESSION_COOLDOWN_FILE}: {e}")


def _log_order(entry: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    orders = []
    if os.path.exists(ORDERS_FILE):
        try:
            with open(ORDERS_FILE) as f:
                orders = json.load(f)
        except Exception:
            pass
    entry["timestamp"] = datetime.now().isoformat()
    orders.append(entry)
    with open(ORDERS_FILE, "w") as f:
        json.dump(orders[-500:], f, indent=2)
    # Persistent CSV — never truncated
    trade_logger.log_trade(
        module     = _pnl_module(),
        strategy   = entry.get("strategy", ""),
        symbol     = entry.get("symbol", ""),
        side       = entry.get("side", ""),
        quantity   = entry.get("quantity", 0),
        price      = entry.get("entry_price") or entry.get("exit_price") or 0,
        order_id   = entry.get("order_id"),
        dry_run    = entry.get("dry_run", False),
        stop_price = entry.get("stop_price", 0),
        notes      = entry.get("reason", ""),
    )


# ── Currency exposure helpers ─────────────────────────────────────────────────

def _currency_exposure(positions: dict) -> dict:
    """Count net directional exposure per currency across all open positions.

    Returns {currency: net_count} where positive = net long, negative = net short.
    Example: {"EUR": +2, "USD": -3, "GBP": +1}
    """
    exposure: dict[str, int] = {}
    for key, pos in positions.items():
        sym = key.split(":", 1)[1] if ":" in key else key
        if len(sym) != 6:
            continue
        base  = sym[:3]   # e.g. EUR in EURUSD
        quote = sym[3:]   # e.g. USD in EURUSD
        if pos.get("direction", "Buy") == "Buy":
            exposure[base]  = exposure.get(base,  0) + 1
            exposure[quote] = exposure.get(quote, 0) - 1
        else:
            exposure[base]  = exposure.get(base,  0) - 1
            exposure[quote] = exposure.get(quote, 0) + 1
    return exposure


def _currency_exposure_notional_eur(positions: dict) -> dict[str, float]:
    """Net EUR-notional exposure per currency across all open positions --
    the economically real version of _currency_exposure() above, which only
    counts positions (a 1,000-unit position and a 48,000-unit position both
    counted as "1", making it meaningless across a 149-pair universe with
    wildly varying position sizes -- user's finding, 2026-08-27, reviewing
    the SIM workbook).

    Buying 1,000 units of AUDCHF means +1,000 AUD (long) and, economically,
    the same EUR-equivalent value short CHF (you paid that many CHF to get
    those AUD at the current rate) -- so both legs get the SAME EUR-notional
    magnitude, opposite sign, using _eur_per_unit() on the BASE currency
    (Saxo-quote-based, already the correct live-rate source used everywhere
    else in this module).

    VISIBILITY ONLY, deliberately not wired into any gate yet -- see the
    2026-08-27 decision: measure real economic exposure correctly first,
    decide on a real (and preferably volatility-adjusted) € threshold
    afterward, as its own separate decision. Don't cap on this number
    without that follow-up decision.
    """
    exposure: dict[str, float] = {}
    for key, pos in positions.items():
        sym = key.split(":", 1)[1] if ":" in key else key
        if len(sym) != 6:
            continue
        base, quote = sym[:3], sym[3:]
        qty = pos.get("quantity", 0)
        rate = _eur_per_unit(base, None)
        if rate is None:
            continue
        notional_eur = qty * rate
        sign = 1 if pos.get("direction", "Buy") == "Buy" else -1
        exposure[base]  = exposure.get(base,  0.0) + sign * notional_eur
        exposure[quote] = exposure.get(quote, 0.0) - sign * notional_eur
    return exposure


def _currency_ok(sym: str, direction: str, exposure: dict) -> bool:
    """Return True if adding this position keeps all currencies within the limit."""
    base  = sym[:3]
    quote = sym[3:]
    if direction == "Buy":
        new_base  = exposure.get(base,  0) + 1
        new_quote = exposure.get(quote, 0) - 1
    else:
        new_base  = exposure.get(base,  0) - 1
        new_quote = exposure.get(quote, 0) + 1
    limit = _max_currency_exposure()
    return (abs(new_base)  <= limit and
            abs(new_quote) <= limit)


def _opposing_strategy_holds(sym: str, direction: str, positions: dict) -> str | None:
    """Return the strategy name already holding the OPPOSITE direction on
    this exact symbol, or None if there's no conflict.

    Each strategy's own generate_signals() call only ever sees ITS OWN open
    positions (open_symbols is built with a per-strategy key prefix, see
    the `prefix = f"{strat_name}:"` line above _run_entries) -- it has zero
    visibility into what any of the other 9 strategies are doing on the
    same pair. Found live 2026-08-22 (user spotted it on the dashboard):
    NZDUSD held Long via donchian+pullback AND Short via bb+ml
    simultaneously; same pattern on USDTHB and USDCZK. That combination has
    no upside, ever -- it pays spread/commission on both legs while the net
    directional exposure is smaller than either position alone, for zero
    diversification benefit (unlike spreading risk across DIFFERENT pairs).
    Same-DIRECTION stacking across strategies is deliberately left alone --
    multiple strategies independently agreeing isn't inherently wrong, and
    capping it would be a real risk-budget design decision, not a bug fix.
    """
    for key, pos in positions.items():
        if ":" not in key:
            continue
        other_strat, other_sym = key.split(":", 1)
        if other_sym != sym:
            continue
        if pos.get("direction", "Buy") != direction:
            return other_strat
    return None


def _update_exposure(exposure: dict, sym: str, direction: str) -> None:
    """Update exposure dict in-place after a new position is opened."""
    base  = sym[:3]
    quote = sym[3:]
    if direction == "Buy":
        exposure[base]  = exposure.get(base,  0) + 1
        exposure[quote] = exposure.get(quote, 0) - 1
    else:
        exposure[base]  = exposure.get(base,  0) - 1
        exposure[quote] = exposure.get(quote, 0) + 1


# ── Portfolio risk guard helpers ──────────────────────────────────────────────

def _portfolio_heat_pct(positions: dict, equity: float) -> float:
    """Sum of |entry-stop| × qty across all open positions, converted to EUR,
    as a fraction of EUR equity.

    Two independent bugs used to inflate this wildly:

    1. No currency conversion: |entry-stop|×qty is in the pair's QUOTE
       currency, but was summed directly against EUR equity. A JPY-quoted
       pair (prices ~100-200) or NOK/SEK pair produces a numeric risk value
       ~100-180x larger than the equivalent EUR amount, so a single JPY
       position could read as 180%+ "heat" on its own even when correctly
       risking ~1% of equity. Fixed by converting each position's risk into
       EUR via _eur_per_unit() before summing — mirrors the same fix already
       applied to position sizing via _equity_in_quote().
    2. Positions opened before the capital-cap fix (5bf8a5f) were sized off
       the ~945k-985k EUR SIM broker balance instead of the ~27,800 EUR
       configured capital, so their EUR risk is ~35x too large to compare
       against the new equity base. Only count positions carrying
       "sized_under_cap" (i.e. opened under the fixed sizing logic) so the
       gate reflects real risk from here forward instead of stale pre-fix
       noise.
    """
    if equity <= 0:
        return 0.0
    heat = 0.0
    for key, pos in positions.items():
        if not pos.get("sized_under_cap"):
            continue
        sym   = key.split(":", 1)[1] if ":" in key else key
        quote = sym[3:6] if len(sym) >= 6 else "EUR"
        rate  = _eur_per_unit(quote)
        if not rate or rate <= 0:
            logger.debug(f"  [HEAT] No FX rate for {quote} — skipping {key} in heat calc")
            continue
        risk_quote = abs(float(pos.get("entry_price", 0)) - float(pos.get("stop_price", 0))) \
                     * float(pos.get("quantity", 0))
        heat += risk_quote * rate
    return heat / equity


def _heat_allows_entry(positions: dict, equity: float, strat_name: str | None = None) -> bool:
    """Disabled 2026-08-21 at user's explicit request — "do not block new
    entries, I want to test fully all strategies" — while the SIM account is
    scanning the expanded 117-pair universe. Heat is still computed and
    logged every run (telemetry, and still shown via `--status`) so real risk
    is visible; it just no longer gates entries for SIM.

    2026-08-28: RE-ENABLED for LIVE/LIVE_EUR only, explicit user decision
    (via AskUserQuestion), same discussion that raised LIVE_RISK_PCT_OVERRIDE
    to 0.75% -- a real correlated-position concern was raised (many
    simultaneous positions across different currencies, each individually
    small, summing to meaningful aggregate risk) that LIVE_MAX_CURRENCY_
    EXPOSURE=1 doesn't fully cover (it caps exposure PER currency, not the
    portfolio-wide aggregate). This gate's own docstring already said it
    "should be reinstated before trading live capital" -- that reinstatement
    never actually happened until now. SIM stays disabled, unchanged, for
    the original 2026-08-21 full-testing-breadth reason."""
    limit = _HEAT_LIMIT_BY_STRATEGY.get(strat_name, PORTFOLIO_HEAT_LIMIT)
    heat = _portfolio_heat_pct(positions, equity)
    if heat >= limit:
        if ACCOUNT_ENV in ("live", "live_eur"):
            logger.info(f"  [HEAT] Portfolio heat {heat:.1%} >= {limit:.0%} "
                        f"— blocking new entries (LIVE{f', {strat_name}' if strat_name in _HEAT_LIMIT_BY_STRATEGY else ''})")
            return False
        logger.info(f"  [HEAT] Portfolio heat {heat:.1%} >= {limit:.0%} "
                    f"(limit disabled for SIM testing — NOT blocking)")
    return True


# ── Live margin gate (2026-08-24) ───────────────────────────────────────────
# _heat_allows_entry above is a SOFT, self-computed proxy (stop-distance x
# qty) and was deliberately disabled for SIM testing. It is not a substitute
# for this: Saxo's own margin math is the actual hard constraint, and it can
# diverge sharply from our own heat estimate -- confirmed live 2026-08-24,
# ~24M EUR of pre-cap-fix legacy positions (opened before RISK_PCT's cap
# existed) pushed real margin utilization to 98.56% while our own heat
# metric looked unremarkable, and that 98.56% would have silently blocked
# EVERY other strategy and module sharing this account (LBO, futures, ETF,
# stocks) from ever getting a turn -- not just forex's own swing book.
# Per explicit user direction: reserve real margin headroom for every
# strategy, always, not just the one that happens to be scanning first.
# Applies to every strategy including day-trade ones (LBO) -- running out of
# real broker margin isn't a soft risk-budget choice, it's a hard wall
# regardless of which internal "book" a position is nominally assigned to.
#
# 2026-08-28: SIM-only exemption, same reasoning and same precedent as
# _heat_allows_entry()'s 2026-08-21 disable and the loss-limit/drawdown
# gates' 2026-08-24 disable -- explicit user request ("test all strategies,
# no blocking") after SIM's own accumulated positions pushed simulated
# margin utilization to 52%+ for hours, blocking forex AND futures SIM
# entries. LIVE/LIVE_EUR are UNCHANGED and keep the real, hard-blocking
# gate -- this account's margin is real money, the "reserve headroom"
# reasoning above still fully applies there. Still computed/logged for SIM
# so real (simulated) margin pressure stays visible, same telemetry-not-
# gate pattern as every other disabled-for-SIM check in this file.
MAX_MARGIN_UTILIZATION_PCT = 50.0   # leave HALF the margin pool for other strategies/modules
_MARGIN_CACHE_TTL_SECONDS  = 20     # avoid hammering balances/me once per signal
_margin_cache: dict = {"utilization": None, "checked_at": 0.0}


def _note_operational_blocks() -> None:
    """LIVE only, best-effort. Declare/clear the operational conditions that
    silently stop new entries so attention.py can escalate them: the 50%
    shared-margin cap, and the order-venue circuit breaker. attention.py
    only emails once a block has persisted past its grace period, then
    nags once a day until it clears -- so a transient spike is not a page."""
    try:
        env = ACCOUNT_ENV
        util = _margin_cache.get("utilization")
        if util is None:
            try:
                util = _get("/port/v1/balances/me").get("InitialMargin", {}).get("MarginUtilizationPct")
            except Exception:
                util = None
        if util is not None and util >= MAX_MARGIN_UTILIZATION_PCT:
            attention.raise_attention(
                f"{env}:margin-block", source=f"forex {env}",
                title=f"Shared Saxo margin at {util:.0f}% — new entries blocked",
                detail=(f"Margin utilization {util:.1f}% ≥ the {MAX_MARGIN_UTILIZATION_PCT:.0f}% cap, so "
                        f"NO new LIVE forex entry can be placed on any account (the pool is shared). "
                        f"Free margin by closing a position, or the block persists."),
                severity="warn", grace_minutes=120, recheck_minutes=1500)
        else:
            attention.clear_attention(f"{env}:margin-block",
                                      note=f"margin back under {MAX_MARGIN_UTILIZATION_PCT:.0f}%")

        if _order_circuit_is_open():
            attention.raise_attention(
                f"{env}:venue-circuit-open", source=f"forex {env}",
                title="Order-venue circuit breaker OPEN — entries halted",
                detail=(f"{CIRCUIT_BREAKER_MAX_CONSECUTIVE_REJECTS}+ consecutive Saxo order rejections; "
                        f"last error: {_order_circuit.get('last_saxo_error','?')}. New entries are "
                        f"halted until a scan completes cleanly."),
                severity="critical", grace_minutes=30, recheck_minutes=1500)
        else:
            attention.clear_attention(f"{env}:venue-circuit-open", note="venue answering again")
    except Exception as exc:
        logger.warning(f"  [attention] operational-block note failed: {exc}")


def _margin_allows_entry() -> bool:
    """True if Saxo's own live margin utilization is still below
    MAX_MARGIN_UTILIZATION_PCT. Cached briefly so a strategy placing many
    entries in one run doesn't re-fetch balances/me for every signal.
    Fails OPEN (returns True) if the check itself can't be made -- a
    lookup failure shouldn't silently freeze all trading. SIM-only: logs
    but never blocks (see this section's 2026-08-28 comment); LIVE/LIVE_EUR
    keep the real hard block."""
    now = time.time()
    if _margin_cache["utilization"] is not None and \
       now - _margin_cache["checked_at"] < _MARGIN_CACHE_TTL_SECONDS:
        util = _margin_cache["utilization"]
    else:
        try:
            bal  = _get("/port/v1/balances/me")
            util = bal.get("InitialMargin", {}).get("MarginUtilizationPct")
        except Exception as exc:
            logger.warning(f"  [MARGIN] Could not check margin utilization: {exc} — not blocking")
            return True
        if util is None:
            return True
        _margin_cache["utilization"] = util
        _margin_cache["checked_at"]  = now

    if util >= MAX_MARGIN_UTILIZATION_PCT:
        if ACCOUNT_ENV in ("live", "live_eur"):
            logger.warning(f"  [MARGIN] utilization {util:.1f}% >= {MAX_MARGIN_UTILIZATION_PCT:.0f}% "
                            f"— blocking new entries to preserve room for every strategy")
            return False
        logger.info(f"  [MARGIN] utilization {util:.1f}% >= {MAX_MARGIN_UTILIZATION_PCT:.0f}% "
                    f"(limit disabled for SIM testing — NOT blocking)")
    return True


def _update_peak_equity(equity: float) -> None:
    peak = 0.0
    if os.path.exists(PEAK_EQUITY_FILE):
        try:
            with open(PEAK_EQUITY_FILE) as f:
                peak = float(json.load(f).get("peak", 0))
        except Exception:
            pass
    # A >80% gap between current equity and the recorded peak is implausible
    # for a risk-controlled swing book (stops are 1.5-2x ATR) and almost
    # certainly means the peak predates a rescale of the sizing equity base
    # (e.g. the capital cap added in 5bf8a5f, which sizes off ~27,800 EUR
    # instead of the ~945,000 EUR broker SIM balance). Reseed rather than
    # let a stale, wrong-scale peak permanently trip the drawdown breaker.
    if peak > 0 and equity > 0 and equity < peak * 0.2:
        logger.warning(f"  [DRAWDOWN] peak {peak:,.0f} looks stale vs current "
                        f"equity {equity:,.0f} — reseeding peak")
        peak = 0.0
    if equity > peak:
        with open(PEAK_EQUITY_FILE, "w") as f:
            json.dump({"peak": equity, "updated": datetime.now().isoformat()}, f)


def _drawdown_allows_entry(equity: float) -> bool:
    try:
        with open(PEAK_EQUITY_FILE) as f:
            peak = float(json.load(f).get("peak", equity))
    except Exception:
        return True
    if peak <= 0:
        return True
    dd = (peak - equity) / peak
    if dd > DRAWDOWN_PAUSE_PCT:
        logger.warning(f"  [DRAWDOWN] {dd:.1%} from peak {peak:,.0f} — pausing new entries")
        return False
    return True


def _entries_blocked_by_loss_limit(equity: float) -> bool:
    today = date.today().isoformat()
    try:
        trades    = pnl_tracker.get_closed_trades(module=_pnl_module(), limit=500, since=today)
        daily_pnl = sum(t.get("realized_pnl") or 0 for t in trades)
    except Exception:
        return False
    limit = -(DAILY_LOSS_LIMIT_PCT * equity)
    if daily_pnl <= limit:
        logger.warning(f"  [LOSS_LIMIT] Today's realised P&L {daily_pnl:+,.0f} <= "
                       f"-{DAILY_LOSS_LIMIT_PCT:.0%} of equity — blocking new entries")
        return True
    return False


# ── Breakeven stop helpers ────────────────────────────────────────────────────

def _amend_stop_order(order_id: str, new_price: float, sym: str, akey: str, uic: int) -> bool:
    """Amend an existing Saxo GTC stop order to a new price via PATCH.

    Saxo's PATCH /trade/v2/orders/{OrderId} 404s if the body only carries the
    changed fields — it also needs OrderId/Uic/AssetType repeated in the body
    to resolve which order+instrument context to modify (confirmed: every
    prior amend attempt 404'd with just AccountKey+OrderPrice+OrderDuration,
    even against order ids verified live via GET /port/v1/orders/me).
    """
    dp = get_price_decimals(sym)
    rounded = _round_order_price(sym, new_price)   # tick-snaps metals; == round(_,dp) otherwise
    try:
        _patch(f"/trade/v2/orders/{order_id}", {
            "AccountKey":    akey,
            "OrderId":       str(order_id),
            "Uic":           uic,
            "AssetType":     ASSET_TYPE,
            "OrderPrice":    rounded,
            "OrderDuration": {"DurationType": "GoodTillCancel"},
        })
        logger.info(f"  [BREAKEVEN] Stop order {order_id} amended → {rounded:.{dp}f}  ✓")
        return True
    except Exception as exc:
        logger.warning(f"  [BREAKEVEN] Could not amend stop {order_id}: {exc} "
                       f"— will try cancel+replace")
        return False


def _cancel_order(order_id: str, akey: str) -> bool:
    """Cancel a live Saxo order. Returns True on success (incl. if it's
    already gone — a 404 here means there's nothing left to double-protect
    against, so treat it as cancelled rather than aborting the replace)."""
    try:
        _delete(f"/trade/v2/orders/{order_id}", {"AccountKey": akey})
        return True
    except requests.exceptions.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return True
        logger.warning(f"  Could not cancel order {order_id}: {exc}")
        return False
    except Exception as exc:
        logger.warning(f"  Could not cancel order {order_id}: {exc}")
        return False


def _replace_stop_order(pos: dict, sym: str, akey: str, new_price: float) -> str | None:
    """Cancel the position's current stop and place a fresh one at new_price.

    Fallback for when PATCH-amend 404s (Saxo SIM doesn't support in-place
    amend for these stop orders even with full Uic/AssetType/OrderId in the
    body — confirmed against multiple live order ids). Returns the new order
    id, or None if either step failed — caller must treat None as "possibly
    unprotected, do not mark breakeven_triggered".
    """
    old_oid = pos.get("stop_order_id")
    if old_oid and old_oid not in ("synced", None, ""):
        if not _cancel_order(old_oid, akey):
            return None   # couldn't confirm the old stop is gone — don't risk a duplicate

    direction  = pos.get("direction", "Buy")
    close_side = "Sell" if direction == "Buy" else "Buy"
    dp         = get_price_decimals(sym)
    rounded    = _round_order_price(sym, new_price)   # tick-snaps metals; == round(_,dp) otherwise
    try:
        resp    = _post("/trade/v2/orders", {
            "AccountKey":    akey,
            "Uic":           pos["uic"],
            "AssetType":     ASSET_TYPE,
            "Amount":        pos["quantity"],
            "BuySell":       close_side,
            "OrderType":     "Stop",
            "OrderPrice":    rounded,
            "OrderDuration": {"DurationType": "GoodTillCancel"},
            "ManualOrder":   False,
        })
        new_oid = str(resp.get("OrderId", "?"))
        logger.info(f"  [BREAKEVEN] Replaced stop {old_oid} → {new_oid} @ {rounded:.{dp}f}  ✓")
        return new_oid
    except Exception as exc:
        logger.warning(f"  [BREAKEVEN] Cancelled stop {old_oid} but re-place FAILED @ "
                        f"{rounded:.{dp}f}: {exc} — POSITION MAY BE UNPROTECTED, check manually")
        return None


def _apply_breakeven_stop(key: str, pos: dict, df, strat_name: str,
                           akey: str, dry_run: bool) -> bool:
    """Move stop to entry_price (breakeven) once position is sufficiently in profit.

    Trend strategies : triggers when unrealised profit >= BREAKEVEN_THRESHOLD_ATR × ATR_at_entry
    Gap strategy     : triggers when price is >= BREAKEVEN_GAP_FILL_PCT toward gap_target

    One-shot per position — stored as pos["breakeven_triggered"] = True.
    Returns True if the stop was moved this call.
    """
    if pos.get("breakeven_triggered"):
        return False
    if df is None or len(df) < 1:
        return False

    direction   = pos.get("direction", "Buy")
    entry_price = float(pos.get("entry_price", 0))
    cur_stop    = float(pos.get("stop_price", 0))
    cur_close   = float(df["Close"].iloc[-1])

    if strat_name in ("gap", "gap_weekend"):
        gap_target = float(pos.get("gap_target", entry_price))
        if abs(gap_target - entry_price) < 1e-8:
            return False
        # cur_close, not cur_high/cur_low. Same staleness bug as the one
        # fixed in should_exit() above (df.strategy_gap docstring there) --
        # missed here originally since gap's breakeven path was rarely
        # exercised before session gaps started actually trading
        # 2026-08-24 (they were structurally blocked by the consensus-
        # filter bug fixed in signal_filter.py the same day). cur_high/low
        # is the CURRENT PERIOD's cumulative extreme (whole day for
        # weekly, whole H1 candle for session) -- once price so much as
        # wicked 50%+ of the way to target for an instant, this one-shot
        # trigger fired PERMANENTLY, moving the stop to breakeven even if
        # price immediately reverted. Confirmed live: 15+ session gap
        # trades 2026-08-24 got their stop moved to ~entry_price within
        # the same evaluation cycle they opened in, then stopped out for
        # a quick small loss on ordinary noise -- not a real reversal.
        if direction == "Buy":
            fill_pct      = (cur_close - entry_price) / (gap_target - entry_price)
            should_trigger = fill_pct >= BREAKEVEN_GAP_FILL_PCT and cur_stop < entry_price
        else:
            fill_pct      = (entry_price - cur_close) / (entry_price - gap_target)
            should_trigger = fill_pct >= BREAKEVEN_GAP_FILL_PCT and cur_stop > entry_price
    else:
        atr_entry = float(pos.get("atr_at_entry", 0))
        if atr_entry <= 0:
            return False
        threshold = BREAKEVEN_THRESHOLD_ATR * atr_entry
        if direction == "Buy":
            should_trigger = (cur_close - entry_price) >= threshold and cur_stop < entry_price
        else:
            should_trigger = (entry_price - cur_close) >= threshold and cur_stop > entry_price

    if not should_trigger:
        return False

    sym = key.split(":", 1)[1] if ":" in key else key
    tag = "[DRY] " if dry_run else ""
    logger.info(f"  {tag}[BREAKEVEN] {key}: stop {cur_stop:.5f} → {entry_price:.5f} (entry)")

    pos["stop_price"] = entry_price

    if dry_run or _is_paper_position(pos):
        # paper position: no broker order to amend, the local stop_price
        # (just set) is the whole stop -- _run_exits marks it against real
        # quotes and closes locally when it's hit.
        pos["breakeven_triggered"] = True
        return True

    stop_oid = pos.get("stop_order_id")
    if stop_oid and stop_oid not in ("synced", None, "") and \
       _amend_stop_order(stop_oid, entry_price, sym, akey, pos["uic"]):
        pos["breakeven_triggered"] = True
        return True

    # PATCH-amend failed — fall back to cancel + re-place at the new price
    # (Saxo SIM 404s in-place amends for some stop orders regardless of body).
    new_oid = _replace_stop_order(pos, sym, akey, entry_price)
    if new_oid:
        pos["stop_order_id"]       = new_oid
        pos["breakeven_triggered"] = True
        return True

    # Both amend and cancel+replace failed — drop the stale id so
    # _heal_missing_stops places a fresh GTC stop at the updated stop_price
    # next run, instead of the position silently believing the broker-side
    # stop moved when it never did. Don't set breakeven_triggered:
    # should_trigger recomputes false now that stop_price == entry_price,
    # so this is a quiet no-op until healed.
    pos["stop_order_id"] = None
    return False


def _apply_profit_ladder_stop(key: str, pos: dict, df, strat_name: str,
                              akey: str, dry_run: bool) -> bool:
    """Ratchet `pos["stop_price"]` up to the RSI profit-ladder level for the
    current bars, and best-effort sync the broker stop order (same amend /
    cancel+replace / drop-for-heal path as _apply_breakeven_stop). Returns
    True if the local stop moved this call.

    Only ever called from _run_exits when _profit_ladder_active(strat_name)
    is True (opt-in, see PROFIT_LADDER_ACCOUNTS) — and in that case it
    REPLACES both the generic trailing_stop_update block and
    _apply_breakeven_stop for this position, so the two stop-management
    systems never fight."""
    target = _profit_ladder_target_stop(pos, df, strat_name)
    if target is None:
        return False

    is_long  = pos.get("direction", "Buy") == "Buy"
    cur_stop = float(pos.get("stop_price", 0) or 0)
    new_stop = max(cur_stop, target) if is_long else min(cur_stop, target)
    if new_stop <= 0 or abs(new_stop - cur_stop) < 1e-9:
        return False

    sym = key.split(":", 1)[1] if ":" in key else key
    tag = "[DRY] " if dry_run else ""
    entry = float(pos.get("entry_price", 0) or 0)
    init_stop = pos.get("initial_stop_price")
    R = abs(entry - float(init_stop)) if init_stop else strat_rsi.ATR_STOP_MULT * float(pos.get("atr_at_entry", 0) or 0)
    r_now = ((float(df["Close"].iloc[-1]) - entry) if is_long
             else (entry - float(df["Close"].iloc[-1]))) / R if R > 0 else 0.0
    rung = ("trail-1ATR"     if r_now >= PROFIT_LADDER_TRAIL_ACTIVATE_R
            else "lock+0.5R"  if r_now >= PROFIT_LADDER_LOCK_ACTIVATE_R
            else "breakeven+costs")
    logger.info(f"  {tag}[PROFIT-LADDER] {key}: {r_now:.2f}R in profit, rung={rung} — "
                f"stop {cur_stop:.5f} → {new_stop:.5f}")

    # Stamp the position so the exit record / report_profit_ladder.py can
    # show which rung each trade reached and at what R (highest wins).
    if r_now > float(pos.get("ladder_rung_r", -9e9)):
        pos["ladder_rung"]   = rung
        pos["ladder_rung_r"] = round(r_now, 2)

    pos["stop_price"] = round(new_stop, 6)
    if dry_run or _is_paper_position(pos):
        return True   # local stop_price is the whole stop for a paper position

    stop_oid = pos.get("stop_order_id")
    if stop_oid and stop_oid not in ("synced", None, "") and \
       _amend_stop_order(stop_oid, new_stop, sym, akey, pos["uic"]):
        return True
    new_oid = _replace_stop_order(pos, sym, akey, new_stop)
    if new_oid:
        pos["stop_order_id"] = new_oid
        return True
    # Broker sync failed — drop the stale id so _heal_missing_stops re-places
    # a fresh GTC stop at the new price next run. The local stop_price still
    # moved and should_exit() enforces it softly every cycle in the meantime.
    pos["stop_order_id"] = None
    return True


# ── Per-strategy exit / entry helpers ─────────────────────────────────────────

def _close_orphan_ledger_rows(positions: dict) -> int:
    """Mark `pnl_ledger.db` open rows closed when the strategy no longer
    tracks that position in state -- it was closed by a broker-side
    resting stop (so _run_exits/pnl_tracker.log_close never fired) or
    cleared by reconciliation. Confirmed 2026-09-01: 719 forex + 1349 etf
    open rows vs 153 real positions (ml:USDRON 53 open / 0 closed). Closed
    with realized_pnl NULL -- the true exit P&L is unknown, and every
    SUM()/AVG() in pnl_tracker's reports skips NULL. Best-effort."""
    try:
        import pnl_tracker as _pt
        mod = _pnl_module()
        want = {(k.split(":", 1)[0], k.split(":", 1)[1]) for k in positions if ":" in k}
        with _pt._conn() as c:
            rows = c.execute(
                "SELECT id, strategy, symbol FROM trades WHERE module=? AND status!='closed'",
                (mod,)).fetchall()
            stale = [r["id"] for r in rows if (r["strategy"], r["symbol"]) not in want]
            if stale:
                c.executemany(
                    "UPDATE trades SET status='closed', realized_pnl=NULL, "
                    "exit_reason='reconciled_no_state', timestamp_close=? WHERE id=?",
                    [(datetime.now().isoformat(), i) for i in stale])
        if stale:
            logger.info(f"  [ledger] closed {len(stale)} orphan open row(s) with no state position")
        return len(stale)
    except Exception as exc:
        logger.debug(f"  [ledger] orphan-close skipped: {exc}")
        return 0


def _legacy_exit_strategies(active_strategies, positions) -> list:
    """Strategies that have an OPEN position but are no longer in the entry
    allowlist -- e.g. the SEK LIVE account's 4 `donchian:` positions after
    it moved to rsi-only (2026-08-31). Without this they'd sit on a frozen
    entry-day broker bracket with NO trailing / channel-break / time-stop
    exit at all (the config table's "close manually" note was the stopgap).
    Entries for these strategies stay blocked -- this is exits only."""
    active = set(active_strategies)
    held = {k.split(":", 1)[0] for k in positions if ":" in k}
    return sorted((held - active) & set(STRATEGIES))


def _reconcile_closed_vs_saxo() -> None:
    """Post-run backstop: verify recently-closed LIVE trades against Saxo's
    own closed-position record and correct any price drift in the ledger /
    observation cards (the substrate the AI journal + give-back learn
    from). LIVE only -- Saxo SIM has no closedpositions endpoint. Fully
    read-only w.r.t. trading state; never raises. See
    reconcile_closed_trades_vs_saxo.py."""
    if ACCOUNT_ENV not in ("live", "live_eur"):
        return
    try:
        import reconcile_closed_trades_vs_saxo as _rc
        _rc.run(env=ACCOUNT_ENV)
    except Exception as exc:
        logger.warning(f"  [reconcile-vs-saxo] skipped (non-fatal): {exc}")


_SLIPPAGE_LOG = os.path.join(BASE, "data", "stop_slippage.jsonl")


def _log_stop_slippage(
    strategy: str, symbol: str, direction: str,
    stop_price: float, fill_price: float,
    entry_price: float, df,
) -> None:
    """Log intended_stop vs actual_fill for hard_stop exits (P4 measurement).

    Called once per hard_stop close after the fill price is confirmed. Writes
    to data/stop_slippage.jsonl — read-only analysis, never affects orders.
    Slippage is adverse when a long fills BELOW the stop (stop > fill) or a
    short fills ABOVE it (fill > stop).
    """
    try:
        from forex.strategy import _atr as _atr_fn
        atr_val: float | None = None
        if df is not None and len(df) >= 14:
            try:
                atr_val = float(_atr_fn(df["High"], df["Low"], df["Close"]).iloc[-1])
            except Exception:
                pass

        is_long = direction.lower() in ("buy", "long")
        # Adverse slippage is negative for longs, positive for shorts
        slip_price = fill_price - stop_price if is_long else stop_price - fill_price
        risk_price = abs(entry_price - stop_price) if entry_price and stop_price else None
        slip_r = (slip_price / risk_price) if (risk_price and risk_price > 0) else None
        slip_atr = (slip_price / atr_val) if (atr_val and atr_val > 0) else None

        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "account": ACCOUNT_ENV,
            "strategy": strategy,
            "symbol": symbol,
            "direction": direction,
            "stop_price": round(stop_price, 6),
            "fill_price": round(fill_price, 6),
            "slippage_price": round(slip_price, 6),
            "slippage_atr": round(slip_atr, 4) if slip_atr is not None else None,
            "slippage_r": round(slip_r, 4) if slip_r is not None else None,
            "entry_price": round(entry_price, 6),
            "atr": round(atr_val, 6) if atr_val is not None else None,
        }
        with open(_SLIPPAGE_LOG, "a", encoding="utf-8") as _f:
            _f.write(json.dumps(row) + "\n")
    except Exception:
        pass


def _run_exits(strat_name: str, strat_mod, positions: dict,
               market_data: dict, akey: str, dry_run: bool,
               today_str: str, state: dict | None = None) -> int:
    exits = 0
    prefix = f"{strat_name}:"
    for key in [k for k in positions if k.startswith(prefix)]:
        sym       = key.split(":", 1)[1]
        pos       = positions[key]
        df        = market_data.get(sym)
        ed        = pos.get("entry_date", today_str)
        cal_days  = (date.today() - date.fromisoformat(ed)).days

        # RSI profit-protection ladder: when active for this account+strategy
        # (PROFIT_LADDER_ACCOUNTS x PROFIT_LADDER_STRATEGIES -- as of
        # 2026-08-31 all three accounts, "rsi" only) it OWNS this position's
        # stop management and REPLACES both the generic trailing block below
        # and _apply_breakeven_stop -- so the two systems can never fight.
        # Every other strategy (incl. rsi's A/B twin advanced_rsi_master)
        # and any account not in the set is completely unaffected.
        _ladder_active = _profit_ladder_active(strat_name)

        # Trail stop
        if not _ladder_active and df is not None and hasattr(strat_mod, "trailing_stop_update"):
            from forex.strategy import _atr as _ema_atr
            try:
                atr_fn = getattr(strat_mod, "_atr", _ema_atr)
                atr_now  = float(atr_fn(df["High"], df["Low"], df["Close"]).iloc[-1])
                cur_stop = float(pos.get("stop_price", 0))
                new_stop = strat_mod.trailing_stop_update(
                    cur_stop, float(df["Close"].iloc[-1]), atr_now, pos.get("direction", "Buy"))
                if round(new_stop, 6) != round(cur_stop, 6) and new_stop > 0:
                    pos["stop_price"] = round(new_stop, 6)
            except Exception:
                pass

        # 2026-08-30: strategy_advanced_ml ships its own combined breakeven+
        # trail as update_stop_price(position, df). Applied here as a local
        # pos["stop_price"] ratchet -- same contract as the trailing_stop_update
        # block above (broker-side sync is handled by _apply_breakeven_stop's
        # amend below plus the stop invigilator, identical to every other
        # trailing strategy). No existing strategy defines this method, so
        # this branch only ever runs for "advanced_ml".
        if df is not None and hasattr(strat_mod, "update_stop_price"):
            try:
                cur_stop = float(pos.get("stop_price", 0))
                new_stop = strat_mod.update_stop_price(pos, df)
                if new_stop and round(float(new_stop), 6) != round(cur_stop, 6):
                    pos["stop_price"] = round(float(new_stop), 6)
            except Exception:
                pass

        # Breakeven stop — move to entry_price once profit threshold reached.
        # When the RSI profit ladder is active it takes over this role
        # entirely (it has its own breakeven rung plus lock/trail rungs).
        if _ladder_active:
            _apply_profit_ladder_stop(key, pos, df, strat_name, akey, dry_run)
        else:
            _apply_breakeven_stop(key, pos, df, strat_name, akey, dry_run)

        # Forward-SIM observation (2026-08-27): MAE/MFE from the daily bars
        # already fetched for should_exit()/trailing-stop above -- no extra
        # API call per open position per cycle (97 SIM positions x 10
        # strategies would make that expensive for real intrabar precision
        # this doesn't need; day-level High/Low since entry is the honest
        # trade-off). Runs every cycle for every open position, not just
        # ones that close this cycle.
        if df is not None and not dry_run:
            try:
                window = _bars_for_excursion(df, pos, strat_name)
                is_long_pos = pos.get("direction", "Buy") == "Buy"
                worst_price = float(window["Low"].min()) if is_long_pos else float(window["High"].max())
                best_price  = float(window["High"].max()) if is_long_pos else float(window["Low"].min())
                entry_px = float(pos.get("entry_price", 0))
                qty_pos  = pos.get("quantity", 0)
                sym_quote = sym[3:6] if len(sym) >= 6 else ""
                rate_pos  = _eur_per_unit(sym_quote, akey)
                if strat_name in _INTRADAY_STRATEGIES:
                    pos["mae_mfe_coarse"] = True
                if entry_px and rate_pos:
                    worst_pnl_eur = ((worst_price - entry_px) * qty_pos * rate_pos if is_long_pos
                                      else (entry_px - worst_price) * qty_pos * rate_pos)
                    best_pnl_eur = ((best_price - entry_px) * qty_pos * rate_pos if is_long_pos
                                     else (entry_px - best_price) * qty_pos * rate_pos)
                    # Reject an implausible reading (a bad quote / FX rate this
                    # cycle) instead of letting it poison the running min/max.
                    risk_ref = pos.get("risk_eur_at_entry")
                    cap = _MAE_MFE_SANE_R * risk_ref if (risk_ref and risk_ref > 0) else None
                    if cap and (abs(worst_pnl_eur) > cap or abs(best_pnl_eur) > cap):
                        logger.debug(f"[obs] {sym} MAE/MFE update rejected — "
                                     f"{worst_pnl_eur:.0f}/{best_pnl_eur:.0f} EUR exceeds "
                                     f"{_MAE_MFE_SANE_R:.0f}R (EUR{cap:.0f})")
                    else:
                        forward_observation.update_mae_mfe(pos, worst_pnl_eur)
                        forward_observation.update_mae_mfe(pos, best_pnl_eur)
            except Exception:
                pass

        # Exit advisor -- Stage A (2026-08-31): SHADOW ONLY. Score the
        # position's give-back risk and LOG what it would recommend; never
        # act on it. See forex/exit_advisor.py and EXIT_ADVISOR_MODE. Runs
        # after the MAE/MFE update above so pos["mfe_eur"] is current.
        if EXIT_ADVISOR_MODE == "shadow" and not dry_run:
            try:
                adv = exit_advisor.score(pos, df, strat_name)
                if adv is not None:
                    forward_observation.log_exit_advisor_shadow(
                        account_env=ACCOUNT_ENV, strategy=strat_name, symbol=sym,
                        card_id=pos.get("observation_card_id"),
                        score=adv["score"], recommendation=adv["recommendation"],
                        r_now=adv["r_now"], mfe_r=adv["mfe_r"], signals=adv["signals"],
                        cur_stop=float(pos.get("stop_price", 0) or 0),
                    )
                    if adv["recommendation"] != "HOLD":
                        logger.info(f"  [exit-advisor:SHADOW] {key}: {adv['recommendation']} "
                                    f"(score {adv['score']}, {adv['r_now']}R now / {adv['mfe_r']}R peak) "
                                    f"— not acted on")
            except Exception:
                pass

        exit_flag, reason = strat_mod.should_exit(pos, df, cal_days)
        if not exit_flag:
            continue

        pair_info  = get_pair(sym)
        uic        = pair_info["uic"]
        qty        = pos.get("quantity", 1_000)
        direction  = pos.get("direction", "Buy")
        is_long    = direction == "Buy"
        close_side = "Sell" if is_long else "Buy"
        live_px    = _live_price(uic, akey) or float(pos.get("entry_price", 0))
        entry      = float(pos.get("entry_price", 0))
        pnl_pct    = ((live_px - entry) / entry * 100) if is_long else ((entry - live_px) / entry * 100)

        # should_exit()'s decision is based on df's close (an H1/D1 bar
        # close, fetched once per run, possibly minutes old by the time
        # dozens of positions have been checked in this same sweep) --
        # but the actual closing MARKET order executes at live_px, a
        # separately-fetched fresh quote. If price moved between those
        # two lookups, the position can close on a label ("gap_filled" or
        # "hard_stop") that live_px no longer actually supports, at a
        # worse price than the label implies. Confirmed live 2026-08-24:
        # 42 session-gap "gap_filled" exits, only 3 real wins, net
        # -2,179 EUR — most had exit prices that never reached gap_target
        # at all. Gap's entry always places a REAL resting Stop+Limit
        # bracket on Saxo (see saxo_order.place_with_stop); this
        # should_exit() check only exists as a backup for the case that
        # order is somehow missing (see its docstring) — so re-validating
        # against the SAME live_px used for execution, and skipping (not
        # forcing a bad close) when it disagrees, costs nothing: a
        # genuine hit still closes via the resting order regardless, and
        # skipping never touches (or cancels) that resting order.
        if strat_name in ("gap", "gap_weekend") and (reason.startswith("gap_filled") or reason.startswith("hard_stop")):
            gap_target = float(pos.get("gap_target", entry))
            stop_price = float(pos.get("stop_price", 0))
            if reason.startswith("gap_filled"):
                confirmed = (live_px >= gap_target) if is_long else (live_px <= gap_target)
                level, level_name = gap_target, "target"
            else:
                confirmed = (live_px <= stop_price) if is_long else (live_px >= stop_price)
                level, level_name = stop_price, "stop"
            if not confirmed:
                logger.info(f"  [gap] SKIP exit {sym} — should_exit() fired '{reason}' off a "
                            f"stale close, but live price {live_px:.5f} hasn't actually reached "
                            f"the {level_name} {level:.5f}; leaving the resting order to handle it")
                continue

        # Snapshot Saxo's own net (price + cost) P&L for this position, in its
        # own quote currency, right before closing it — captured while the
        # position still exists to look up. Falls back to our own rate
        # estimate below if this lookup fails (network hiccup, no match).
        _paper = _is_paper_position(pos)

        # ── EXIT GUARD (2026-09-02) ──────────────────────────────────────
        # Before sending ANY real close order on a LIVE account, verify the
        # position still exists at the broker. should_exit() fired off local
        # state, which can be stale: a broker-side stop/TP fill that no
        # exits-check reconciled (the fill can happen while the runner isn't
        # even running). A market close for a position that's already flat
        # does NOT reduce anything -- FX has no reduce-only, so Saxo opens a
        # fresh position the OTHER way. Confirmed live 2026-09-02: a stale
        # rsi:NZDCAD hard_stop sent Sell 9,000 against a flat account and
        # opened a 9,000 short. Only a definite "gone" skips the order;
        # "unknown" (API/snapshot problem) falls through to the normal close
        # so a genuine exit is never suppressed.
        _broker_closed = False
        if not dry_run and not _paper and ACCOUNT_ENV in ("live", "live_eur"):
            _pos_state = _live_position_open(uic, qty, direction, len(positions))
            if _pos_state == "gone":
                _broker_closed = True
                logger.warning(
                    f"  [exit-guard] {sym} ({strat_name}): should_exit fired "
                    f"'{reason}' but NO matching open position at the broker — "
                    f"already closed broker-side. Booking the close from Saxo, "
                    f"NOT sending an order (would open a {close_side} the wrong way).")
                if attention is not None:
                    try:
                        attention.raise_attention(
                            f"{ACCOUNT_ENV}:stale-exit:{sym}",
                            title=f"{sym}: closed broker-side, booked late",
                            detail=(f"{sym} ({strat_name}) was closed by its own broker "
                                    f"stop/TP while local state still tracked it as open. "
                                    f"The exit-check booked it without sending an order "
                                    f"(the exit-guard blocked a would-be wrong-way "
                                    f"{close_side}). Verify the {ACCOUNT_ENV} ledger / "
                                    f"state for {sym} matches Saxo."),
                            source=f"exit-guard ({ACCOUNT_ENV} forex)",
                            severity="warn", grace_minutes=0)
                    except Exception:
                        pass

        net_pnl_quote = None if (dry_run or _paper or _broker_closed) else _position_net_pnl_quote_ccy(uic, qty, direction, entry)
        net_pnl_quote, _net_rebuilt = _sane_net_pnl_quote(
            net_pnl_quote, entry, live_px, qty, is_long, uic, akey, sym)

        order = {"AccountKey": akey, "Uic": uic, "AssetType": ASSET_TYPE,
                 "Amount": qty, "BuySell": close_side, "OrderType": "Market",
                 "OrderDuration": {"DurationType": "DayOrder"}, "ManualOrder": False}

        tag = "L" if is_long else "S"
        if dry_run:
            logger.info(f"  [DRY] {close_side:<4} {qty:,}x {sym}[{tag}] "
                        f"({strat_name}) — {reason}  P&L {pnl_pct:+.2f}%")
        elif _paper:
            # paper position -- no broker order to cancel or send; the close
            # is booked locally at the live quote. P&L below falls back to
            # the raw price calc (no Saxo cost -- there was no real fill).
            logger.info(f"  [PAPER] CLOSE {qty:,}x {sym}[{tag}] "
                        f"({strat_name}) — {reason}  P&L {pnl_pct:+.2f}%  @ {live_px:.5f}")
        elif _broker_closed:
            # Position already flat at the broker (stop/TP filled). Do NOT
            # _post a close -- just clean up any orphan resting leg and book
            # the close from Saxo's own record. _reconcile_closed_vs_saxo()
            # (post-run) corrects the exact price/P&L against the
            # ClosedPosition record afterwards.
            for oid_key in ("stop_order_id", "tp_order_id"):
                oid = pos.get(oid_key)
                if oid and oid not in ("synced", None, ""):
                    _cancel_order(oid, akey)
            _real_exit = _confirm_exit_fill(uic, qty, direction)
            if _real_exit:
                live_px = _real_exit
                pnl_pct = ((live_px - entry) / entry * 100) if is_long else ((entry - live_px) / entry * 100)
            logger.info(f"  [exit-guard] BOOKED (no order) {qty:,}x {sym}[{tag}] "
                        f"({strat_name}) — {reason}  @ {live_px:.5f}  P&L {pnl_pct:+.2f}%")
        else:
            # This close is happening because OUR should_exit() logic fired
            # (hard-stop/time-stop/trailing), not because the broker's own
            # resting stop/TP order triggered it. Those two resting orders
            # are therefore still live and now protecting a position that's
            # about to go flat -- cancel them FIRST, before sending the
            # market close, or either one could fire in the gap and open an
            # unintended new position in the opposite direction (confirmed
            # live 2026-08-22: this exact gap is how a bracket's un-linked
            # fallback legs end up orphaned -- see saxo_order.place_with_stop).
            for oid_key in ("stop_order_id", "tp_order_id"):
                oid = pos.get(oid_key)
                if oid and oid not in ("synced", None, ""):
                    _cancel_order(oid, akey)
            resp = _post("/trade/v2/orders", order)
            logger.info(f"  {close_side} {resp.get('OrderId','?')}: {qty:,}x {sym}[{tag}] "
                        f"({strat_name}) — {reason}  P&L {pnl_pct:+.2f}%")
            # Record the TRUE close fill, not live_px (a quote taken just
            # before the order). net_pnl_quote above is already Saxo's
            # authoritative P&L; this only corrects the price the ledger /
            # observation card store for exit_price and pnl_pct.
            _real_exit = _confirm_exit_fill(uic, qty, direction)
            if _real_exit:
                if abs(_real_exit - live_px) / max(abs(live_px), 1e-9) > _FILL_LOG_THRESHOLD:
                    logger.info(f"  [{strat_name}] {sym} real close {_real_exit:.5f} vs "
                                f"quote {live_px:.5f} ({(_real_exit/live_px-1)*100:+.2f}%)")
                live_px = _real_exit
                pnl_pct = ((live_px - entry) / entry * 100) if is_long else ((entry - live_px) / entry * 100)

        # P4 — stop-slippage measurement: log intended stop_price vs actual
        # fill for hard_stop exits (market order latency + spread). Read-only.
        if not dry_run and not _paper and "hard_stop" in reason.lower():
            _sp = pos.get("stop_price")
            if _sp:
                _log_stop_slippage(strat_name, sym, direction,
                                   float(_sp), live_px, entry, df)

        _log_order({"side": close_side, "symbol": sym, "strategy": strat_name,
                    "uic": uic, "quantity": qty, "exit_price": live_px,
                    "reason": reason, "pnl_pct": round(pnl_pct, 3), "dry_run": dry_run})
        if not dry_run:
            # Convert this pair's raw quote-currency P&L into the account's
            # actual base currency (EUR) before it's stored — otherwise a JPY
            # pair's raw number (e.g. -7,612) gets summed into the ledger
            # alongside a USD/CHF pair's raw number as if they were the same
            # currency, when the JPY figure is really only ~-50 EUR.
            quote_ccy = sym[3:6] if len(sym) >= 6 else ""
            fx_rate   = _eur_per_unit(quote_ccy, akey)
            if fx_rate is None:
                # Only used if net_pnl_quote (Saxo's own authoritative
                # positions/me price+cost figure, the primary source below) is
                # ALSO unavailable -- a double failure. 1.0 is a known-bad
                # placeholder, not a real rate; logged loudly rather than
                # silently trusted, since there's no Yahoo fallback to
                # reach for anymore (Saxo-only per 2026-08-22 direction).
                logger.warning(f"  [PNL] No Saxo rate for {quote_ccy} AND no Saxo "
                                f"position P&L for {sym} -- realized P&L for this "
                                f"close will use an unconverted 1.0 placeholder, "
                                f"verify data/pnl_ledger.db for {sym} manually")
                fx_rate = 1.0
            # net_pnl_quote is in the pair's own quote currency (see
            # _position_net_pnl_quote_ccy docstring for why the EUR
            # conversion happens here, via our own _eur_per_unit(), rather
            # than trusting Saxo's own "...InBaseCurrency" fields).
            saxo_pnl_eur = (net_pnl_quote * fx_rate) if net_pnl_quote is not None else None
            pnl_tracker.log_close(_pnl_module(), sym, live_px, reason, strategy=strat_name,
                                  fx_rate_to_base=fx_rate,
                                  gross_pnl_base_override=saxo_pnl_eur)
            if strat_name in ("gap", "gap_weekend"):
                _mark_gap_exhausted(sym, strat_name)
            # Label the signal-log outcome for ML training data — prefer the
            # true net (price + broker cost) figure when we have it, so a
            # signal that "won" on raw price but lost to cost isn't labeled
            # a win for training purposes.
            raw_pnl = ((live_px - pos["entry_price"]) * qty if is_long
                       else (pos["entry_price"] - live_px) * qty)
            won_for_ml = (net_pnl_quote > 0) if net_pnl_quote is not None else (raw_pnl > 0)
            signal_filter.label_outcome(key, won=won_for_ml, module=_pnl_module())
            if not _paper:
                # paper closes are silent (like paper entries) -- they'd be
                # dozens of emails during a Saxo outage. Visibility is via
                # the [PAPER] log line, the ledger (order_id LIKE 'PAPER-%'),
                # the observation cards, and the daily summary.
                fx_notify.send_trade_closed(
                    strategy=strat_name, symbol=sym, direction=direction,
                    entry=float(pos.get("entry_price", live_px)),
                    exit_px=live_px, pnl_pct=pnl_pct, units=qty,
                    reason=reason,
                    session=pos.get("lbo_session", "") if strat_name in ("london_breakout", "london_breakout_v2") else "",
                    live=(ACCOUNT_ENV in ("live", "live_eur")),
                    net_pnl_native=net_pnl_quote,
                    net_reconstructed=_net_rebuilt,
                )

            # Forward-SIM observation exit card (2026-08-27) -- only if this
            # position had an entry card (older positions opened before
            # this logging existed won't have one, and that's fine, just
            # skip rather than log a card with no matching entry).
            card_id = pos.get("observation_card_id")
            if card_id:
                gross_pnl_eur = raw_pnl * fx_rate
                net_pnl_eur = saxo_pnl_eur if saxo_pnl_eur is not None else gross_pnl_eur
                commission_eur = (gross_pnl_eur - net_pnl_eur) if saxo_pnl_eur is not None else None
                risk_at_entry = pos.get("risk_eur_at_entry")
                r_multiple = (round(net_pnl_eur / risk_at_entry, 2)
                              if risk_at_entry and risk_at_entry > 0 else None)
                holding_hours = None
                try:
                    entry_dt = datetime.fromisoformat(pos.get("entry_datetime", ""))
                    holding_hours = round((datetime.now() - entry_dt).total_seconds() / 3600, 1)
                except Exception:
                    pass
                # Final MAE/MFE sanity gate at write time. The per-cycle
                # reject (see _bars_for_excursion call site) only skips the
                # CURRENT reading -- a value accumulated into pos["mae_eur"]
                # by an earlier cycle (e.g. before the 2026-09-01 holding-
                # window fix deployed, or on a cycle where risk_eur_at_entry
                # was momentarily unavailable so the cap was None) still
                # rode through to here. Confirmed: 59 sim:gap:* cards with
                # MAE/MFE up to 170R. Null anything past the cap.
                _mae, _mfe = pos.get("mae_eur"), pos.get("mfe_eur")
                _mm_bad = False
                if risk_at_entry and risk_at_entry > 0:
                    _lim = _MAE_MFE_SANE_R * risk_at_entry
                    if (_mae is not None and abs(_mae) > _lim) or (_mfe is not None and abs(_mfe) > _lim):
                        _mm_bad = True
                        logger.warning(f"  [obs] {sym}: MAE/MFE {_mae}/{_mfe} EUR over "
                                       f"{_MAE_MFE_SANE_R:.0f}R (EUR{_lim:.0f}) — nulled on the card")
                        _mae = _mfe = None
                forward_observation.log_trade_exit_card(
                    card_id=card_id, exit_price=live_px, exit_reason=reason,
                    gross_pnl_eur=gross_pnl_eur, commission_eur=commission_eur,
                    net_pnl_eur=net_pnl_eur, r_multiple=r_multiple,
                    mae_eur=_mae, mfe_eur=_mfe,
                    mae_mfe_invalidated=("accumulated-over-cap" if _mm_bad else None),
                    holding_hours=holding_hours,
                    ladder_rung=pos.get("ladder_rung"),
                    ladder_rung_r=pos.get("ladder_rung_r"),
                    mae_mfe_coarse=bool(pos.get("mae_mfe_coarse")),
                    net_pnl_reconstructed=_net_rebuilt,
                )
        del positions[key]
        exits += 1
        # Checkpoint IMMEDIATELY, not just at the end of this strategy's pass
        # (2026-09-01): the real close order above already happened at the
        # broker. If the process is killed/watchdog-restarted before the
        # pass-end _save_state below, the NEXT scan still sees this position
        # as open and re-sends a close against an already-flat position --
        # Saxo executes it as a fresh position the other way (naked, and
        # untracked by every module, because state still thought it was
        # gap:GBPNZD/EURCAD/etc. and never recorded a NEW one). This is the
        # exact class that created the untracked 41,000 GBPNZD long
        # (2026-08-24) and a 46,000 EURCAD naked short (2026-09-01, SIM).
        if state is not None and not dry_run:
            _save_state(state)
    return exits


def _run_entries(strat_name: str, strat_mod, positions: dict,
                 market_data: dict, equity: float, akey: str,
                 dry_run: bool, today_str: str,
                 live_prices: dict | None = None,
                 agreement: dict | None = None,
                 weight: float = 1.0,
                 regime_data: dict | None = None,
                 state: dict | None = None) -> int:
    # regime_data: the FULL daily-bar market_data dict (unfiltered), used
    # only to fold a regime label into the AI trade-proposal log (Sprint 2).
    # `market_data` (4th arg) may be a momentum-filtered subset for some
    # strategies, so regime is read from regime_data instead.
    # Order-venue circuit breaker (2026-08-31): a prior strategy in this same
    # run already hit the consecutive-rejection threshold, so Saxo's order
    # endpoint is down. Skip ALL entry work for every remaining strategy --
    # the expensive H1 fetches / generate_signals below included -- and let
    # the next scheduled scan retry. Exits/stop-heal are unaffected (separate
    # call path). See _record_entry_result.
    if not dry_run and _order_circuit_is_open() and not _sim_paper_fill_enabled():
        logger.info(f"  [{strat_name}] entries skipped — order-venue circuit breaker open "
                    f"(Saxo rejected {CIRCUIT_BREAKER_MAX_CONSECUTIVE_REJECTS}+ entries in a row this run)")
        return 0
    # With paper-fill on (SIM), the circuit being open does NOT stop entries
    # -- every signal is still generated and booked locally; the real order
    # attempt is just skipped for speed (see the place_with_stop call site).

    # Gap strategy (and its "gap_weekend" A/B sibling, 2026-08-29): only run
    # during defined session windows (weekly/london/newyork/tokyo).
    # Outside those windows, any overnight move ≥ 0.10% would generate false signals
    # with none of the structural fill edge that makes gap fading profitable.
    _GAP_STRATS = ("gap", "gap_weekend")
    gap_session: str | None = _detect_gap_session() if strat_name in _GAP_STRATS else None
    if strat_name in _GAP_STRATS and gap_session is None:
        logger.info(f"  [{strat_name}] Entries skipped — not in a gap session window "
                    f"({datetime.now(timezone.utc).strftime('%A %H:%M UTC')})")
        return 0
    # 2026-09-02: `london` and `tokyo` session-gap legs disabled. A ~2.8y /
    # H1-bar decomposition (docs/strategy_decomposition_2026-09-02.md) put
    # london at -0.008 R/trade (PF 0.98, 2nd half negative) and tokyo
    # untestable / ~zero ledger volume. `newyork` (+0.090 R, PF 1.33, stable
    # both halves) and `weekly` (+0.10 R on the 12y ledger) stay on. Existing
    # open london/tokyo positions still exit-manage normally. Reversible --
    # drop the session from this set. gap_weekend only ever runs `weekly`, so
    # it is unaffected.
    if strat_name == "gap" and gap_session in DISABLED_GAP_SESSIONS:
        logger.info(f"  [gap] Entries skipped — the '{gap_session}' session leg is "
                    f"disabled (net-negative, see the 2026-09-02 decomposition); "
                    f"exits on any open {gap_session} position still run")
        return 0

    # 2026-08-29: on a real-money account, never place a NEW entry while the
    # FX market is closed for the weekend. A signal computed on stale Friday
    # data would otherwise be sent as a Market order that just rests until
    # Monday's open and fills there at an unrelated price, with no re-check
    # of the setup -- the exact "fake early RSI trigger fills after reopen"
    # problem this guards against. Exits and stop-management are untouched:
    # they run every cycle regardless (run_daily / run_exits_only), so a
    # weekend scan still fully protects open positions. Gap strategies are
    # exempt -- they have their own session windows just above and
    # "gap_weekend" is meant to trade the Sunday reopen. SIM is unaffected
    # (it deliberately scans and trades all 7 days for forward-test breadth).
    # Mid-week this is always open, so scanning covers every pair as normal.
    # NB: this is a FLAG, not an early return -- signals are still generated
    # below so a weekend signal can be emailed (send_signals_detected), it
    # just isn't acted on.
    _weekend_entry_block = (ACCOUNT_ENV in ("live", "live_eur")
                            and strat_name not in _GAP_STRATS
                            and not _fx_market_open())

    base_slots = SLOTS_PER_STRATEGY[strat_name]
    max_slots  = max(1, int(base_slots * strategy_learner.slot_scale(weight)))
    prefix     = f"{strat_name}:"
    held       = sum(1 for k in positions if k.startswith(prefix))
    slots_free = max_slots - held
    if slots_free <= 0 or equity <= 0:
        return 0

    open_syms = {k.split(":", 1)[1] for k in positions if k.startswith(prefix)}

    # Ledger-backed re-entry guard (2026-09-01). `positions` is this run's
    # loaded state -- normally checkpointed after every entry/exit (see the
    # `state is not None` saves below), but as a second, independent line of
    # defense against exactly the divergence that stacked 4 SIM EURCAD longs
    # in one afternoon (a scan opened it, was killed/watchdog-restarted
    # before its checkpoint saved, and the next scan's state didn't know
    # about it) -- fold in every symbol this strategy already has an OPEN
    # row for in the pnl ledger, even if `positions` doesn't know about it.
    try:
        _ledger_open_syms = {r["symbol"] for r in pnl_tracker.get_open_positions(module=_pnl_module())
                             if r.get("strategy") == strat_name}
    except Exception:
        _ledger_open_syms = set()
    _ledger_only = _ledger_open_syms - open_syms
    if _ledger_only:
        logger.warning(f"  [{strat_name}] ledger already has an OPEN position on "
                       f"{sorted(_ledger_only)} that local state didn't know about "
                       f"-- not stacking a duplicate")
    open_syms = open_syms | _ledger_open_syms

    if strat_name in ("london_breakout", "london_breakout_v2"):
        # London/NY Breakout: fetch H1 bars for the configured pairs only.
        # Generalized to strat_mod (was hardcoded to strat_lbo) 2026-08-29
        # so "london_breakout_v2" runs through the identical dispatch path.
        lbo_pairs  = strat_mod.PAIRS
        h1_lbo: dict = {}
        pair_meta: dict = {}
        for pi in PAIRS:
            sym = pi["symbol"]
            if sym not in lbo_pairs:
                continue
            h1_lbo[sym]  = _fetch_history_h1(pi["uic"])
            pair_meta[sym] = {"pip_size": pi.get("pip_size", 0.0001)}
        # LBO is a separate day-trading book with its own capital, not a slice of
        # the swing account. Passing `equity` here made it risk 1.5% of everything.
        # Both london_breakout and london_breakout_v2 currently share this SAME
        # dedicated book -- v2's much smaller RISK_PCT (0.5%) and slot cap (4)
        # keep its worst-case ADDITIONAL draw on it small (max 2% vs the
        # original's already-existing 42% theoretical max), but this is a real,
        # deliberate sharing decision worth knowing, not a separate pool.
        try:
            import atos.capital_config as _CAP
            lbo_equity = _CAP.forex_lbo_capital_eur()
        except Exception:
            lbo_equity = 1_390.0
        # stop_distance is in each pair's quote currency, so convert per pair.
        lbo_eq_by_pair = {}
        for sym in h1_lbo:
            eq_q = _equity_in_quote(lbo_equity, sym)
            if eq_q is not None:
                lbo_eq_by_pair[sym] = eq_q
        logger.info(f"  [{strat_name}] book capital {lbo_equity:,.0f} EUR "
                    f"({strat_mod.RISK_PCT*100:.1f}% = {lbo_equity*strat_mod.RISK_PCT:,.0f} EUR/trade)")
        lbo_kw = {}
        if strat_name == "london_breakout_v2":
            # Fix #4 (repeat-signal protection): thread the persisted
            # already-traded-this-session-day set through -- see
            # _load_lbo_v2_session_cooldown's docstring.
            lbo_kw["already_traded_sessions"] = _load_lbo_v2_session_cooldown()
        signals = strat_mod.generate_signals(
            h1_lbo, pair_meta, open_syms,
            account_equity=lbo_equity, equity_by_pair=lbo_eq_by_pair,
            **lbo_kw,
        )
        logger.info(f"  [{strat_name}] {len(h1_lbo)} pairs scanned → {len(signals)} signal(s)")
    elif strat_name in _GAP_STRATS and gap_session != "weekly":
        # Session gap (London / NY / Tokyo): fetch H1 bars for ALL 34 pairs.
        # No pair-list restriction — gap_pct filter selects only pairs that actually
        # gapped (EURNOK, USDSEK, NZDJPY, AUDCHF etc. all get a fair look).
        gap_exhausted = _load_gap_cooldown(strat_name)
        if gap_exhausted:
            logger.info(f"  [{strat_name}:{gap_session}] cooldown: skipping {sorted(gap_exhausted)}")
        h1_data: dict = {}
        for pi in PAIRS:
            h1_data[pi["symbol"]] = _fetch_history_h1(pi["uic"])
        signals = strat_mod.generate_session_signals(
            gap_session, h1_data, open_symbols=open_syms, live_prices=live_prices or {},
            exhausted_symbols=gap_exhausted,
        )
        # 2026-09-02: within the (surviving) newyork leg, drop gaps whose pair
        # is in a skip-regime -- HIGH_VOLATILITY newyork gaps ran -0.357 R /
        # 43% WR in the decomposition. regime_data = the full daily-bar dict.
        if (strat_name == "gap" and gap_session == "newyork"
                and GAP_NEWYORK_SKIP_REGIMES and regime_data):
            try:
                from ai.regime.classifier import classify_regime
                _kept = []
                for _s in signals:
                    _bars = regime_data.get(_s["symbol"])
                    _lbl = classify_regime(_bars).get("label") if _bars is not None else None
                    if _lbl in GAP_NEWYORK_SKIP_REGIMES:
                        logger.info(f"  [gap:newyork] SKIP {_s['symbol']} — {_lbl} regime "
                                    f"(net-negative for newyork gaps)")
                    else:
                        _kept.append(_s)
                signals = _kept
            except Exception as _exc:
                logger.warning(f"  [gap:newyork] regime filter skipped (non-fatal): {_exc}")
        logger.info(f"  [{strat_name}:{gap_session}] {len(h1_data)} pairs scanned → {len(signals)} signal(s)")
    elif getattr(strat_mod, "NEEDS_LIVE_PRICES", False):
        kw: dict = {"open_symbols": open_syms, "live_prices": live_prices or {}}
        if strat_name in _GAP_STRATS:
            gap_exhausted = _load_gap_cooldown(strat_name)
            if gap_exhausted:
                logger.info(f"  [{strat_name}:weekly] cooldown: skipping {sorted(gap_exhausted)}")
            kw["exhausted_symbols"] = gap_exhausted
        signals = strat_mod.generate_signals(market_data, **kw)
    else:
        signals = strat_mod.generate_signals(market_data, open_symbols=open_syms)

    # Weekend on a real-money account: signals are generated (above) so they
    # can be surfaced, but no entry is placed -- they'd only rest as stale
    # Market orders until Monday. Email them and stop here.
    if _weekend_entry_block:
        logger.info(f"  [{strat_name}] {len(signals)} signal(s) detected — NOT entered, "
                    f"FX market closed for the weekend "
                    f"({datetime.now(timezone.utc).strftime('%A %H:%M UTC')}); "
                    f"exits/stops still run, entries resume Sunday 22:00 UTC")
        if signals and not dry_run:
            try:
                fx_notify.send_signals_detected(strat_name, signals, entered=[],
                                                account_env=ACCOUNT_ENV, market_closed=True)
            except Exception as exc:
                logger.warning(f"  [{strat_name}] signals-detected email failed: {exc}")
        return 0

    exposure  = _currency_exposure(positions)
    agreement = agreement or {}

    entries = 0
    entered_syms: list[str] = []
    _ai_shadow_pending: list = []   # (sym, proposal, decision) -- flushed after the loop
    _ai_decision_by_sym: dict = {}  # AI Sprint 4: latest decision per symbol, for the sizing hook
    def _rej(stage: str, detail: str = "") -> None:
        # Structured record for a signal dropped BEFORE the cost gate (so it
        # never reaches cost_gate_decisions.jsonl). Pure observation.
        try:
            forward_observation.log_signal_rejected(
                account_env=ACCOUNT_ENV, strategy=strat_name, symbol=sym, direction=direction,
                stage=stage, detail=detail,
                entry_price=float(sig.get("close", 0) or 0) or None,
                stop_price=float(sig.get("stop_price", 0) or 0) or None,
                tp_price=(_resolve_tp_price(sig, direction) if sig.get("close") else None),
                rsi=sig.get("rsi"), adx=sig.get("adx"), atr=sig.get("atr"))
        except Exception:
            pass

    for sig in signals:
        if entries >= slots_free:
            break
        sym       = sig["symbol"]
        direction = sig["direction"]

        # ── Stale-price guard ─────────────────────────────────────────────
        # Even after _repair_stale_forming_bars, refuse to open a position
        # when the signal's own price still disagrees with the live tradable
        # quote by more than the tolerance -- the strategy is acting on a
        # frozen SIM chart print and the position would be born underwater
        # vs the price it can actually be closed at (the NZDPLN re-entry
        # loop, 2026-09-01).
        _lq = (live_prices or {}).get(sym) or _live_price(get_pair(sym)["uic"], akey)
        _sc = float(sig.get("close", 0) or 0)
        if _lq and _sc and abs(_sc - _lq) / _lq > _STALE_FORMING_BAR_TOL:
            logger.warning(
                f"  [{strat_name}] SKIP {sym}[{direction}] — signal price {_sc:.5f} is "
                f"{(_sc/_lq-1)*100:+.2f}% off the live quote {_lq:.5f} (stale chart bar)")
            _rej("stale_price", f"{(_sc/_lq-1)*100:+.2f}% off live quote")
            continue

        # ── Signal filter: consensus + ML meta-filter ──────────────────────
        passes, features, reason = signal_filter.evaluate(
            sym, direction, sig, agreement, STRATEGIES, firing_strategy=strat_name,
            module=_pnl_module())
        if not passes:
            logger.info(f"  [{strat_name}] SKIP {sym}[{direction}] "
                        f"— signal_filter: {reason}")
            _rej("signal_filter", reason)
            continue
        agrees = features["agreement_count"]
        ml_info = (f"  ml_prob={features['ml_prob']}" if features.get("ml_prob") else "")

        # ── AI advisory layer (Sprints 2-3) — INERT, log-only ─────────────
        # For a signal that passed every deterministic filter above:
        #   Sprint 2: write a structured candidate to ai_trade_proposals.jsonl
        #   Sprint 3: if agent_enabled, also call the Trading Copilot and
        #             stash (proposal, decision) -- logged with the real
        #             entered/skipped outcome after this loop.
        # Guarded by the AI kill switch (OFF by default). On the LIVE
        # accounts this is log-only forever (ai.config.can_apply_decision is
        # hardcoded False for them). Cannot change entries / qty / anything
        # downstream; any exception is swallowed. Cost controls: the paid
        # LLM call is scoped to config agent_strategies and de-duped so each
        # signal is evaluated once per day, not every rescan.
        if ai_config is not None and ai_config.ai_enabled_for(ACCOUNT_ENV):
            try:
                # give the agent the trade's real economics: the flat Saxo
                # round-trip commission AND the all-in transaction cost (the
                # number the deterministic recovery-vs-cost gate uses), plus
                # this pair/strategy's closed-trade track record. Commission
                # is size-flat; a nominal 10k lot is representative for the
                # spread term too.
                _uic_ai = get_pair(sym)["uic"]
                _rt_q = _round_trip_cost_quote_ccy(_uic_ai, 10_000, akey)
                _q_rate = _eur_rate_for_log(sym[3:6] if len(sym) >= 6 else "", akey)[0]
                _b_rate = _eur_rate_for_log(sym[:3], akey)[0]
                _comm_eur = round(_rt_q * _q_rate, 2) if (_rt_q and _q_rate) else None
                _all_in_eur = _live_all_in_cost_eur(
                    commission_eur=_comm_eur, spread_pct=_spread_pct(_uic_ai),
                    entry_px=float(sig.get("close") or 0),
                    notional_eur=(10_000 * _b_rate) if _b_rate else None,
                    quote_ccy=sym[3:6] if len(sym) >= 6 else "")
                _prop = _ai_build_proposal(
                    account_env=ACCOUNT_ENV, strategy=strat_name, symbol=sym,
                    direction=direction, sig=sig, features=features,
                    positions=positions, equity=equity,
                    take_profit=_resolve_tp_price(sig, direction),
                    n_strategies=len(STRATEGIES),
                    regime_bars=(regime_data or {}).get(sym),
                    est_commission_eur=_comm_eur,
                    est_all_in_cost_eur=(round(_all_in_eur, 2) if _all_in_eur else None),
                    fixed_risk_eur=(RSI_LIVE_FIXED_RISK_EUR if strat_name == "rsi" else None),
                    pair_stats=_pair_history_stats(sym, strat_name),
                )
                _ai_log_proposal(_prop)
                if (ai_config.agent_enabled_for(ACCOUNT_ENV)
                        and ai_config.agent_strategy_allowed(strat_name)):
                    # Sprint 4: when the agent may actually act on this
                    # account (can_apply_decision -- sim only, agent on,
                    # shadow_mode OFF), evaluate on EVERY rescan so the
                    # sizing hook below always has a fresh decision. In pure
                    # shadow mode the daily dedup still applies (cost).
                    _ai_acting = ai_config.can_apply_decision(ACCOUNT_ENV)
                    _ai_already = (ai_config.agent_dedup_enabled()
                                   and _ai_already_evaluated(_prop))
                    if _ai_acting or not _ai_already:
                        _dec = ai_trading_copilot.evaluate_proposal(_prop)
                        _ai_decision_by_sym[sym] = _dec
                        if not _ai_already:
                            _ai_shadow_pending.append((sym, _prop, _dec))
            except Exception as exc:
                logger.warning(f"  [ai] advisory hook failed for {sym}: {exc}")

        if not _currency_ok(sym, direction, exposure):
            logger.info(f"  [{strat_name}] SKIP {sym}[{direction}] "
                        f"— currency exposure limit (max {_max_currency_exposure()})")
            _rej("currency_exposure", f"max {_max_currency_exposure()}")
            continue
        opposing = _opposing_strategy_holds(sym, direction, positions)
        if opposing is not None:
            logger.info(f"  [{strat_name}] SKIP {sym}[{direction}] "
                        f"— {opposing} already holds the opposite direction on {sym}, "
                        f"no upside to taking both sides")
            _rej("opposing_strategy", f"{opposing} holds opposite")
            continue
        pair_info = get_pair(sym)
        uic       = pair_info["uic"]
        spread    = _spread_pct(uic)
        if spread is not None and spread > MAX_SPREAD_PCT:
            logger.info(f"  [{strat_name}] SKIP {sym}[{direction}] "
                        f"— spread {spread:.3f}% wider than {MAX_SPREAD_PCT}% "
                        f"(illiquid right now, not a good time to trade this pair)")
            _rej("wide_spread", f"{spread:.3f}% > {MAX_SPREAD_PCT}%")
            continue
        if not _margin_allows_entry():
            break   # real Saxo margin too tight — stop entries for EVERY strategy, LBO included
        if strat_name not in DAY_TRADE_STRATEGIES and not _heat_allows_entry(positions, equity, strat_name):
            break   # heat cap reached — stop all entries for this strategy
        rp_kw     = {"risk_pct": sig["risk_pct_override"]} if "risk_pct_override" in sig else {}
        if "risk_pct" not in rp_kw and _live_risk_pct() is not None:
            rp_kw["risk_pct"] = _live_risk_pct()
        # 2026-08-29: "gap_weekend"'s size_position() takes stop_mult as an
        # explicit required parameter (fix for the sizing bug where the
        # original gap strategy always sized off the weekly 1.5x multiplier
        # even for session gaps whose real stop is 2.0x) -- only signals
        # from that module ever carry a "stop_mult" key, so this is a no-op
        # for every other strategy.
        if "stop_mult" in sig:
            rp_kw["stop_mult"] = sig["stop_mult"]
        # 2026-08-28, explicit user decision: LIVE/LIVE_EUR skip a trade
        # entirely (size_position() returns 0) rather than force it up to
        # the 1,000-unit floor when the account's own risk budget doesn't
        # naturally justify that size. Confirmed via real computation that
        # at current pilot capital (6,000 SEK / 500 EUR, even the EUR
        # account's full 900 EUR balance), 0/34 (pair x strategy)
        # combinations on the 17-pair HIGH_VOLUME_SYMBOLS universe
        # naturally clear 1,000 units -- this is a deliberate, accepted
        # near-total-halt tradeoff, not an oversight. SIM keeps the
        # historical floor-up behavior (its ~945,000 EUR demo credit
        # clears 1,000 units for most pairs anyway).
        if ACCOUNT_ENV in ("live", "live_eur"):
            rp_kw["block_below_min"] = True
        # 2026-08-31: fixed ~EUR45 per-trade risk for RSI on the real-money
        # accounts (see RSI_LIVE_FIXED_RISK_EUR). Express it in the pair's
        # quote currency (size_position's risk_amount and atr are both quote-
        # ccy) and drop risk_pct -- an absolute budget overrides the %.
        if (ACCOUNT_ENV in ("live", "live_eur") and strat_name == "rsi"
                and RSI_LIVE_FIXED_RISK_EUR):
            _q_ccy   = sig["symbol"][3:6] if len(sig["symbol"]) >= 6 else ""
            _eur_per = _eur_per_unit(_q_ccy, akey)
            if not _eur_per:
                logger.warning(f"  [{strat_name}] SKIP {sym}: no live EUR rate for "
                               f"{_q_ccy} — can't enforce the €{RSI_LIVE_FIXED_RISK_EUR:.0f} "
                               f"risk cap, not falling back to %-based sizing on real money")
                _rej("no_fx_rate", f"no live EUR rate for {_q_ccy} (LIVE €45 cap)")
                continue
            rp_kw["risk_amount"] = RSI_LIVE_FIXED_RISK_EUR / _eur_per
            rp_kw.pop("risk_pct", None)
        if "units" in sig:
            qty = sig["units"]   # london_breakout pre-computes sizing from SEK capital
        else:
            # size_position() computes units = (equity * RISK_PCT) / (mult * ATR).
            # ATR is in the pair's QUOTE currency but equity is in EUR, so without
            # converting, realised risk scales with the numeric size of the quote
            # currency: JPY pairs risked ~0.1% while USD/CHF pairs risked 20-38%,
            # a 447x spread across positions that are all nominally "1%".
            # Converting equity into the quote currency makes the division
            # dimensionally correct and gives every pair the same real risk.
            eq_quote = _equity_in_quote(equity, sym)
            if eq_quote is None:
                logger.warning(f"  [{strat_name}] SKIP {sym}: no FX rate for quote "
                               f"currency — refusing to size without conversion")
                _rej("no_fx_rate", "no quote-ccy FX rate for sizing")
                continue
            qty = strat_mod.size_position(eq_quote, sig["atr"],
                                          pair_info["min_units"], **rp_kw)
            if qty <= 0:
                if rp_kw.get("risk_amount") is not None:
                    logger.info(f"  [{strat_name}] SKIP {sym}[{direction}] — one "
                                f"{pair_info['min_units']:,.0f}-unit lot would risk more than the "
                                f"€{RSI_LIVE_FIXED_RISK_EUR:.0f} cap (stop too wide for this pair)")
                else:
                    logger.info(f"  [{strat_name}] SKIP {sym}[{direction}] — risk budget "
                                f"doesn't naturally justify even the {pair_info['min_units']:,.0f}-unit "
                                f"minimum at current capital; not forcing an oversized trade")
                continue

            # 2026-08-29: RSI on a real-money account snaps to a 10k-100k
            # lot ladder (see _snap_rsi_live_lot's docstring). Placed after
            # the qty>0 / block_below_min check above and BEFORE the cost
            # gate below, so the gate evaluates the real order size. Guarded
            # on the pair's own min_units so a pair whose minimum lot is
            # itself above 10k (none of the 17 live HIGH_VOLUME pairs, but
            # be safe) keeps its normal floored size.
            if (ACCOUNT_ENV in ("live", "live_eur") and strat_name == "rsi"
                    and pair_info["min_units"] <= RSI_LIVE_LOT_RUNG):
                if RSI_LIVE_FIXED_RISK_EUR:
                    # Fixed-EUR-risk-CEILING mode: size_position already
                    # rounded qty DOWN to 1,000-unit increments so realised
                    # risk <= €45 (and returned 0 -> skipped above if one
                    # lot already exceeded it). RSI_LIVE_LOT_MAX is only a
                    # sanity backstop here; it never binds at €45 risk.
                    capped = min(qty, RSI_LIVE_LOT_MAX)
                    if capped != qty:
                        logger.warning(f"  [{strat_name}] {sym}: {qty:,} → {capped:,} "
                                       f"(LIVE max-lot backstop {RSI_LIVE_LOT_MAX:,} hit — "
                                       f"unexpected at €45 risk, check ATR/stop)")
                    qty = capped
                else:
                    snapped = _snap_rsi_live_lot(qty)
                    if snapped != qty:
                        logger.info(f"  [{strat_name}] {sym}: risk-sized {qty:,} → "
                                    f"{snapped:,} (LIVE 10k–100k lot ladder)")
                    qty = snapped

        # ── AI Sprint 4: apply the Trading Copilot's decision to sizing ──────
        # Live ONLY when ai_config.can_apply_decision(ACCOUNT_ENV) is True:
        # sim account + agent_enabled + shadow_mode OFF (config/ai.json). On
        # main today shadow_mode is ON, so can_apply_decision("sim") is False
        # and this whole block is inert -- it ships exactly as dormant as the
        # Sprint 2/3 hooks did. LIVE can never reach here (can_apply_decision
        # is hardcoded False for live/live_eur in ai/config.py).
        #
        # REJECT -> skip with the same `continue` shape as every deterministic
        # skip above. MODIFY -> scale qty by size_multiplier (the agent has
        # already clamped it to [MULTIPLIER_FLOOR, 1.0] and can only ever
        # REDUCE), then floor back to the pair minimum. APPROVE/HOLD -> no
        # change. Runs after every deterministic gate and BEFORE the cost
        # gate so the commission check sees the real (reduced) order size.
        if (ai_config is not None and ai_trading_copilot is not None
                and ai_config.can_apply_decision(ACCOUNT_ENV)
                and sym in _ai_decision_by_sym and "units" not in sig):
            _ai_qty, _ai_note = _ai_apply_decision_to_qty(
                qty, _ai_decision_by_sym[sym], pair_info["min_units"],
                floor=ai_trading_copilot.MULTIPLIER_FLOOR)
            if _ai_note:
                logger.info(f"  [{strat_name}] {sym}[{direction}] {_ai_note}")
            if _ai_qty <= 0:
                continue
            qty = _ai_qty

        # ── SIM per-trade notional cap (2026-09-01, user) ───────────────────
        # SIM is for strategy testing, not size. Even at 0.25% risk, low-ATR
        # pairs sized to ~EUR 180k notional on a ~EUR 27,800 base. Cap the
        # NOTIONAL (not the risk math) to config/capital.json
        # account.sim_max_trade_notional_eur, then floor to the pair minimum.
        # SIM only; LIVE accounts keep their real (small) sizing untouched.
        if ACCOUNT_ENV == "sim" and "units" not in sig:
            import atos.capital_config as _cap_cfg
            _cap_eur = _cap_cfg.sim_max_trade_notional_eur()
            if _cap_eur and _cap_eur > 0:
                _base_ccy = sym[:3]
                _eur_per_base = _eur_per_unit(_base_ccy, akey)
                if _eur_per_base:
                    _notional_eur = qty * _eur_per_base
                    if _notional_eur > _cap_eur:
                        _capped = max(int(_cap_eur / _eur_per_base), int(pair_info["min_units"]))
                        if _capped != qty:
                            logger.info(f"  [{strat_name}] {sym}[{direction}] SIM notional cap: "
                                        f"{qty:,} → {_capped:,} units "
                                        f"(~€{_notional_eur:,.0f} → ~€{_capped * _eur_per_base:,.0f}, "
                                        f"cap €{_cap_eur:,.0f})")
                            qty = _capped

        # london_breakout/gap provide their own session-range-based target;
        # every other strategy gets a broker-side TP at DEFAULT_TP_RR times
        # its own stop distance, so it's protected on both sides at the
        # broker from the moment it's opened (see DEFAULT_TP_RR docstring).
        tp = _resolve_tp_price(sig, direction)

        # Cost-clearance gate (2026-08-26): skip trades whose own target
        # can't plausibly clear Saxo's real round-trip commission by a
        # healthy margin -- see MIN_EDGE_TO_COST_RATIO's docstring for the
        # incident this closes. A None cost (lookup failed) does NOT block
        # the entry -- treat "unknown" as "don't block", same as the spread
        # check above, rather than let a transient API hiccup halt trading.
        #
        # 2026-08-28: deliberately placed immediately after qty/tp (the two
        # unavoidable inputs the real per-quantity commission quote needs --
        # Saxo's round-trip cost genuinely depends on order size, so "cost
        # viability" can't be checked with literally zero quantity in hand)
        # and BEFORE every other per-trade step below (labels, order-spec
        # building) -- explicit user priority order: cost viability first,
        # stop-based quantity second (the qty above), risk scaling third.
        # A cost-nonviable signal is skipped as early as structurally
        # possible, before any of that other work runs for nothing.
        #
        # 2026-08-27: forward-SIM observation phase -- log every signal that
        # reaches this point (PASS or BLOCKED), not just skip counts, so a
        # later pass can ask the real question: what was the counterfactual
        # performance of the trades this gate rejected?
        expected_target_profit = abs(tp - sig["close"]) * qty
        round_trip_cost = _round_trip_cost_quote_ccy(uic, qty, akey)
        quote_ccy_for_log = sym[3:6] if len(sym) >= 6 else ""
        # analytics rate: fresh Saxo quote, else last-good Saxo rate (< 24h).
        # NOT used to size anything -- the LIVE €45 risk cap has its own strict
        # _eur_per_unit() call above and still SKIPs on a miss.
        eur_rate_for_log, _rate_src = _eur_rate_for_log(quote_ccy_for_log, akey)
        _is_live = ACCOUNT_ENV in ("live", "live_eur")
        _edge_ratio = _min_edge_ratio()

        # this trade's EUR notional (qty is in the pair's BASE ccy). Computed
        # for EVERY account -- SIM included -- so the cost-gate telemetry and
        # the all-in-cost figure the AI accumulates are identical on SIM and
        # LIVE (the deterministic *block* below is still LIVE-only).
        notional_eur = None
        _base_rate = _eur_rate_for_log(sym[:3], akey)[0]
        if _base_rate:
            notional_eur = qty * _base_rate

        # RSI recovery-vs-cost viability check (2026-09-01, user). One rule,
        # pair-independent: a realistic partial recovery (RSI_LIVE_ASSUMED_
        # EXIT_R of R) must clear the all-in round-trip cost by RSI_LIVE_MIN_
        # RECOVERY_MULT. Catches R collapsing (tight stop + lot rounding, a
        # low-notional pair).
        #   * LIVE: a REJECT (never resize up) -- replaced MIN_LIVE_NOTIONAL_
        #     EUR + the LIVE_RSI_MIN_UNITS table.
        #   * SIM: NOT a reject -- SIM keeps full forward-test breadth (RSI on
        #     all 184 pairs, user 2026-09-01). But `_rsi_recovery_thin` is
        #     recorded on every SIM RSI signal so the AI shadow study /
        #     journal / analysis can tell a cost-dominated signal from a
        #     healthy one without the trade being suppressed.
        # The 0.5R assumption is RSI(2)-specific; other strategies keep the
        # 2R-target `cost_not_cleared` gate. SIM still fails OPEN on unknown
        # cost/rate.
        _realised_r_eur = (abs(sig["close"] - sig["stop_price"]) * qty * eur_rate_for_log
                           if eur_rate_for_log else None)
        _all_in_cost_eur = _live_all_in_cost_eur(
            commission_eur=(round_trip_cost * eur_rate_for_log)
                           if (round_trip_cost is not None and eur_rate_for_log) else None,
            spread_pct=spread, entry_px=sig["close"], notional_eur=notional_eur,
            quote_ccy=quote_ccy_for_log)
        _rsi_recovery_thin = (
            strat_name == "rsi" and _realised_r_eur is not None and _all_in_cost_eur
            and RSI_LIVE_ASSUMED_EXIT_R * _realised_r_eur
                < RSI_LIVE_MIN_RECOVERY_MULT * _all_in_cost_eur)

        blocked = False
        block_reason = ""
        if round_trip_cost is not None and expected_target_profit < round_trip_cost * _edge_ratio:
            blocked, block_reason = True, "cost_not_cleared"
        elif _is_live and (round_trip_cost is None or eur_rate_for_log is None):
            blocked, block_reason = True, "cost_unknown_live"
        elif _is_live and _rsi_recovery_thin:
            blocked, block_reason = True, "recovery_below_cost_margin"

        forward_observation.log_cost_gate_decision(
            account_env=ACCOUNT_ENV, strategy=strat_name, symbol=sym, direction=direction,
            entry_price=sig["close"], stop_price=sig["stop_price"], tp_price=tp, qty=qty,
            expected_target_profit_quote=expected_target_profit, round_trip_cost_quote=round_trip_cost,
            expected_target_profit_eur=(expected_target_profit * eur_rate_for_log) if eur_rate_for_log else None,
            round_trip_cost_eur=(round_trip_cost * eur_rate_for_log) if (round_trip_cost is not None and eur_rate_for_log) else None,
            min_edge_to_cost_ratio=_edge_ratio,
            decision="BLOCKED" if blocked else "PASS",
            reason=(block_reason
                    or ("recovery_thin_sim" if _rsi_recovery_thin else "")
                    or ("cost_unknown" if round_trip_cost is None else "")),
            notional_eur=notional_eur,
            realised_r_eur=_realised_r_eur, all_in_cost_eur=_all_in_cost_eur,
            recovery_thin=bool(_rsi_recovery_thin), rate_source=_rate_src,
        )
        if blocked:
            if block_reason == "recovery_below_cost_margin":
                logger.info(f"  [{strat_name}] SKIP {sym}[{direction}] — a "
                            f"{RSI_LIVE_ASSUMED_EXIT_R:.2f}R recovery "
                            f"(€{RSI_LIVE_ASSUMED_EXIT_R * _realised_r_eur:,.1f}) doesn't clear "
                            f"{RSI_LIVE_MIN_RECOVERY_MULT:.1f}× the all-in cost "
                            f"(€{_all_in_cost_eur:,.2f}) at {qty:,} units — realised R only "
                            f"€{_realised_r_eur:,.1f}, rejecting (not resizing up)")
            elif block_reason == "cost_unknown_live":
                logger.info(f"  [{strat_name}] SKIP {sym}[{direction}] — round-trip cost or FX "
                            f"rate lookup failed on a LIVE account; not opening a real position blind")
            else:
                logger.info(f"  [{strat_name}] SKIP {sym}[{direction}] "
                            f"— target profit {expected_target_profit:.2f} doesn't clear "
                            f"{_edge_ratio}x round-trip cost ({round_trip_cost:.2f}) "
                            f"at {qty:,} units — too small to be worth the fixed commission")
            continue

        # Everything below only runs for a cost-cleared signal -- labels/
        # order-spec building deferred until after the gate above, not
        # computed for a trade that's about to be skipped anyway.
        tag    = "LONG" if direction == "Buy" else "SHORT"
        detail = (f"rsi={sig['rsi']:.1f}" if "rsi" in sig
                  else f"range={sig.get('range_pips', 0):.0f}p" if "range_pips" in sig
                  else f"breakout={sig.get('breakout_level', 0):.5f}" if "breakout_level" in sig
                  else f"adx={sig.get('adx', 0):.1f}")
        stop_oid = None; tp_oid = None
        agree_tag = f"  agree={agrees}/{len(STRATEGIES)}{ml_info}"

        # Fetch live Saxo mid-price before computing the bracket — sig["close"]
        # is the scan-bar close (H1 = up to 1h stale; daily = up to ~20h stale).
        # Keep the ATR stop DISTANCE unchanged; shift both stop and TP so they
        # are anchored to the live tradable price rather than the bar close.
        _live_entry = _live_price(uic, akey)
        if _live_entry:
            _bar_c     = float(sig["close"])
            _stop_dist = abs(_bar_c - float(sig["stop_price"]))
            _tp_dist   = abs(tp - _bar_c)
            if abs(_live_entry - _bar_c) / max(abs(_bar_c), 1e-9) > _FILL_LOG_THRESHOLD:
                logger.info(f"  [{strat_name}] {sym}: bar {_bar_c:.5f} → "
                            f"live {_live_entry:.5f} "
                            f"({(_live_entry/_bar_c-1)*100:+.3f}%) "
                            f"— re-anchoring stop/TP to live price")
            sig["close"]      = _live_entry
            sig["stop_price"] = (_live_entry - _stop_dist if direction == "Buy"
                                 else _live_entry + _stop_dist)
            tp                = (_live_entry + _tp_dist if direction == "Buy"
                                 else _live_entry - _tp_dist)

        if dry_run:
            logger.info(f"  [DRY] {direction:<4} {qty:,}x {sym}[{tag}] "
                        f"({strat_name})  @ {sig['close']:.5f}  "
                        f"stop={sig['stop_price']:.5f}  tp={tp:.5f}  {detail}{agree_tag}")
        elif _paper_only_account():
            # AI SIM twin: never send a Saxo order. Book the fill locally at
            # the live quote; the exit stack manages it against quotes (the
            # PAPER- id / pos["paper"]=True path, same as the SIM rejection
            # fallback). `qty` here is already AI-resized (the Sprint-4 hook
            # above ran -- can_apply_decision("ai_sim") is True).
            _fill_px = _live_price(uic, akey) or float(sig["close"])
            sig["close"] = _fill_px
            entry_oid = "PAPER-" + uuid.uuid4().hex[:12]
            stop_oid, tp_oid = "PAPER-STOP", "PAPER-TP"
            _record_entry_result(rejected=False)
            logger.info(f"  [{strat_name}] AI-SIM {direction} {qty:,}x {sym}[{tag}] "
                        f"@ {_fill_px:.5f}  stop={sig['stop_price']:.5f}  tp={tp:.5f}{agree_tag}")
        else:
            if _sim_paper_fill_enabled() and _order_circuit_is_open():
                # Venue already confirmed down this run — don't waste the
                # ~4 API calls + timeouts per signal; go straight to paper.
                entry_oid = stop_oid = tp_oid = None
            else:
                entry_oid, stop_oid, tp_oid = saxo_order.place_with_stop(
                    post_fn           = _post,
                    account_key       = akey,
                    uic               = uic,
                    asset_type        = ASSET_TYPE,
                    amount            = qty,
                    buy_sell          = direction,
                    stop_price        = sig["stop_price"],
                    label             = f"{strat_name}:{sym}",
                    take_profit_price = tp,
                    symbol            = sym,
                    price_decimals    = get_price_decimals(sym),
                    tick_size         = _metals_tick_size(sym),
                )
            if entry_oid is None:
                # Order rejected by Saxo — nothing was opened at the broker.
                _saxo_err = getattr(saxo_order, "LAST_ENTRY_ERROR", "") or "order endpoint rejection"
                _record_entry_result(rejected=True, saxo_error=_saxo_err)
                _note_blocked_signal(strat_name, sym, direction, _sim_paper_fill_enabled())
                if _sim_paper_fill_enabled():
                    # SIM only: book the fill LOCALLY so the forward-test
                    # keeps running through a Saxo SIM order-engine outage.
                    # PAPER- ids + pos["paper"]=True make every downstream
                    # broker touch a no-op; the position is managed by
                    # ATOS's own exit logic against real quotes.
                    fill_px = _live_price(uic, akey) or float(sig["close"])
                    entry_oid = "PAPER-" + uuid.uuid4().hex[:12]
                    stop_oid  = "PAPER-STOP"
                    tp_oid    = "PAPER-TP"
                    sig["close"] = fill_px   # record the position at the real fill price
                    logger.warning(f"  [{strat_name}] PAPER-FILL {sym}[{direction}] "
                                   f"{qty:,} @ {fill_px:.5f} — Saxo SIM rejected the order; "
                                   f"booked locally, managed by ATOS stop/TP/exit logic")
                    # falls through to the pos_record block, tagged paper below
                else:
                    # Not paper-filling: skip this signal and keep going —
                    # one rejection must not stop the rest of this strategy's
                    # signals or any strategy queued after it.
                    logger.warning(f"  [{strat_name}] SKIP {sym}[{direction}] "
                                    f"— entry order rejected, no position opened")
                    if _order_circuit_is_open():
                        break
                    continue
            else:
                _record_entry_result(rejected=False)
                # Saxo's order POST returned an OrderId but no fill/price.
                # Confirm the position actually opened and record its REAL
                # average fill, not sig["close"] (a stale scan-bar close).
                _filled, _fill_px = _confirm_entry_fill(entry_oid, uic)
                if _filled:
                    if abs(_fill_px - sig["close"]) / max(abs(sig["close"]), 1e-9) > _FILL_LOG_THRESHOLD:
                        logger.info(
                            f"  [{strat_name}] {sym} real fill {_fill_px:.5f} vs scan "
                            f"close {sig['close']:.5f} ({(_fill_px/sig['close']-1)*100:+.2f}%)")
                    sig["close"] = _fill_px
                elif ACCOUNT_ENV in ("live", "live_eur"):
                    # Real money: an accepted-but-unfilled entry becomes a
                    # phantom position the moment we record it. Pull the
                    # order + its bracket legs and record nothing -- missing
                    # a trade beats tracking one that isn't real.
                    for _oid in (entry_oid, stop_oid, tp_oid):
                        if _oid and not str(_oid).startswith("PAPER"):
                            _cancel_order(_oid, akey)
                    logger.warning(
                        f"  [{strat_name}] LIVE {sym}[{direction}] entry {entry_oid} "
                        f"accepted but NOT filled after {_FILL_CONFIRM_ATTEMPTS} checks "
                        f"— cancelled entry+brackets, no position recorded")
                    _note_blocked_signal(strat_name, sym, direction, False)
                    continue
                else:
                    _q = _live_price(uic, akey)
                    if _q:
                        sig["close"] = _q
                    logger.warning(
                        f"  [{strat_name}] {sym}[{direction}] entry accepted but fill "
                        f"unconfirmed — recording at live quote {sig['close']:.5f}")
            tp_info = f"  tp_order={tp_oid}" if tp_oid else ""
            logger.info(f"  {direction} {entry_oid}: {qty:,}x {sym}[{tag}] "
                        f"({strat_name})  @ {sig['close']:.5f}  stop={sig['stop_price']:.5f}"
                        f"  stop_order={stop_oid}{tp_info}{agree_tag}")

        pos_key = f"{strat_name}:{sym}"
        pos_record = {
            "uic":            uic,
            "direction":      direction,
            "entry_price":    sig["close"],
            "stop_price":     sig["stop_price"],
            "initial_stop_price": sig["stop_price"],  # frozen R reference; stop_price gets ratcheted, this doesn't
            "tp_price":       tp,   # broker-side target for every strategy now (own or DEFAULT_TP_RR fallback)
            "quantity":       qty,
            "entry_date":     today_str,
            "entry_datetime": datetime.now().isoformat(),  # hour-based time stop for session gaps
            "atr_at_entry":   sig["atr"],
            "stop_order_id":  stop_oid,
            "tp_order_id":    tp_oid if not dry_run else None,
            "sized_under_cap": True,
        }
        if isinstance(entry_oid, str) and entry_oid.startswith("PAPER-"):
            pos_record["paper"] = True   # ATOS-simulated fill; see _sim_paper_fill_enabled()
        # 2026-09-02: stamp the market regime at entry on rsi / rsi_trend
        # positions so the dashboard's "RSI REGIME FIT" division can show
        # which open positions match the TRENDING gate (i.e. which ones
        # rsi_trend would hold) WITHOUT re-classifying on every render.
        # rsi_trend's own signal already carries `regime_at_entry`.
        if strat_name in ("rsi", "rsi_trend"):
            _reg = sig.get("regime_at_entry")
            if not _reg and regime_data is not None:
                try:
                    from ai.regime.classifier import classify_regime
                    _rb = regime_data.get(sym)
                    if _rb is not None:
                        _reg = classify_regime(_rb).get("label")
                except Exception:
                    _reg = None
            if _reg:
                pos_record["regime_at_entry"] = _reg
                pos_record["regime_fit"] = bool(
                    (_reg == "TRENDING_BULLISH" and direction == "Buy") or
                    (_reg == "TRENDING_BEARISH" and direction == "Sell"))
        # 2026-09-02: persist the A/B entry-gate values for ema_trend / bb_quality
        # (the SIM twins of ema / bb) so report/dashboard views can show which
        # open positions cleared each gate, without re-deriving the indicators.
        if strat_name == "ema_trend":
            if sig.get("crossover_age") is not None:
                pos_record["crossover_age"] = sig["crossover_age"]
            if sig.get("di_spread") is not None:
                pos_record["di_spread"] = sig["di_spread"]
        elif strat_name in ("bb_quality", "zscore_quality") and sig.get("di_spread") is not None:
            pos_record["di_spread"] = sig["di_spread"]
        if "gap_target" in sig:
            pos_record["gap_target"]   = sig["gap_target"]
            pos_record["friday_close"] = sig.get("friday_close", sig["gap_target"])
            pos_record["gap_pct"]      = sig.get("gap_pct", 0.0)
        if "gap_type" in sig:
            pos_record["gap_type"] = sig["gap_type"]   # "weekly"/"london"/"newyork"/"tokyo"
        if "tp_price" in sig:
            pos_record["tp_price"]    = sig["tp_price"]
            pos_record["range_high"]  = sig.get("range_high", 0)
            pos_record["range_low"]   = sig.get("range_low",  0)
            pos_record["lbo_session"] = sig.get("session", "")
        exposure_before_notional = _currency_exposure_notional_eur(positions)
        positions[pos_key] = pos_record
        _update_exposure(exposure, sym, direction)
        oid = entry_oid if not dry_run else None
        _log_order({"side": direction, "symbol": sym, "strategy": strat_name,
                    "uic": uic, "quantity": qty, "entry_price": sig["close"],
                    "stop_price": sig["stop_price"], "dry_run": dry_run})
        if not dry_run:
            pnl_tracker.log_open(_pnl_module(), strat_name, sym, direction, qty,
                                 sig["close"], sig["stop_price"], order_id=oid,
                                 currency="EUR", gap_type=sig.get("gap_type"))
            signal_filter.log_signal(pos_key, features, module=_pnl_module())   # builds ML training data

            # Forward-SIM observation card (2026-08-27): structural/hybrid
            # stop candidates are donchian-specific (its own 15-day exit
            # channel) -- None for every other strategy, not a guess. See
            # backtests/donchian_stop_variant_backtest.py for the same
            # structural-level computation.
            structural_stop = hybrid_stop = None
            if strat_name == "donchian":
                try:
                    from forex.strategy_donchian import EXIT_PERIOD, ATR_STOP_MULT as _DONCHIAN_ATR_MULT
                    df_sym = market_data.get(sym)
                    if df_sym is not None and len(df_sym) > EXIT_PERIOD:
                        window = df_sym["Close"].iloc[-(EXIT_PERIOD + 1):-1]
                        is_long_ = (direction == "Buy")
                        structural_stop = float(window.min()) if is_long_ else float(window.max())
                        floor_dist = _DONCHIAN_ATR_MULT / 2 * sig["atr"]
                        hybrid_stop = (min(structural_stop, sig["close"] - floor_dist) if is_long_
                                       else max(structural_stop, sig["close"] + floor_dist))
                except Exception:
                    pass
            # analytics rate (fresh Saxo quote, else last-good < 24h) -- this
            # only ever feeds the observation card / R-multiple / MAE-MFE cap,
            # never a size or a gate, so a slightly stale Saxo rate beats a
            # None that drops the trade out of the give-back sample entirely.
            eur_rate_entry, _rate_src_entry = _eur_rate_for_log(quote_ccy_for_log, akey)
            risk_eur_entry = (abs(sig["close"] - sig["stop_price"]) * qty * eur_rate_entry
                               if eur_rate_entry else None)
            pos_record["risk_eur_at_entry"] = risk_eur_entry  # for a true R-multiple at exit, not a re-derived guess
            pos_record["observation_card_id"] = forward_observation.log_trade_entry_card(
                account_env=ACCOUNT_ENV, strategy=strat_name, symbol=sym, direction=direction,
                entry_price=sig["close"], atr_at_entry=sig["atr"], current_stop=sig["stop_price"],
                structural_stop=structural_stop, hybrid_stop=hybrid_stop, quantity=qty,
                risk_eur=risk_eur_entry,
                cost_eur=(round_trip_cost * eur_rate_entry) if (round_trip_cost is not None and eur_rate_entry) else None,
                cost_to_edge_ratio=(round(expected_target_profit / round_trip_cost, 2)
                                    if round_trip_cost else None),
                all_in_cost_eur=_all_in_cost_eur,
                recovery_to_cost_ratio=(round(RSI_LIVE_ASSUMED_EXIT_R * _realised_r_eur / _all_in_cost_eur, 2)
                                        if (_realised_r_eur is not None and _all_in_cost_eur) else None),
                recovery_thin=bool(_rsi_recovery_thin),
                rate_source=_rate_src_entry,
                exposure_before_eur=exposure_before_notional,
                exposure_after_eur=_currency_exposure_notional_eur(positions),
            )
            if strat_name in ("london_breakout", "london_breakout_v2"):
                fx_notify.send_lbo_trade_opened(
                    symbol=sym, direction=direction,
                    entry=sig["close"], stop=sig["stop_price"],
                    tp=sig.get("tp_price", 0), units=qty,
                    session=sig.get("session", ""),
                    range_pips=sig.get("range_pips", 0),
                )
            if strat_name == "london_breakout_v2" and "session_key" in sig:
                # Fix #4: mark this exact symbol+date+session as traded so
                # the SAME underlying breakout can't re-signal later today
                # even if this position closes (TP/SL) while price is still
                # beyond the range boundary.
                _mark_lbo_v2_session_traded(sig["session_key"])
        # Checkpoint IMMEDIATELY (2026-09-01), not just at the end of this
        # strategy's pass: the real entry order above already happened at
        # the broker. If the process is killed/watchdog-restarted before the
        # pass-end _save_state, the NEXT scan's `positions` won't have this
        # entry, its open_syms won't exclude `sym`, and the strategy can
        # signal + open the SAME pair again -- the exact mechanism that
        # stacked 4 SIM EURCAD longs in one afternoon.
        if state is not None and not dry_run:
            _save_state(state)
        entries += 1
        entered_syms.append(sym)

    # 2026-08-29: on a real-money account, email every signal this scan
    # produced and whether it was entered -- so a signal blocked by a gate
    # (exposure cap, cost, spread, slots, heat) is visible, not just the
    # ones that made it through. SIM never sends this (noise + no need).
    if ACCOUNT_ENV in ("live", "live_eur") and signals and not dry_run:
        try:
            fx_notify.send_signals_detected(strat_name, signals, entered=entered_syms,
                                            account_env=ACCOUNT_ENV, market_closed=False)
        except Exception as exc:
            logger.warning(f"  [{strat_name}] signals-detected email failed: {exc}")

    # ── AI Sprint 3: flush shadow decisions with the real entered/skipped
    # outcome. Decision was already computed above; it is LOGGED, never
    # applied (Sprint 4 is where a decision can affect SIM sizing).
    _ai_acted_this_run = bool(ai_config is not None
                              and ai_config.can_apply_decision(ACCOUNT_ENV))
    for _s, _p, _d in _ai_shadow_pending:
        try:
            _ai_log_shadow(_p, _d, entered=(_s in entered_syms),
                           applied=(_ai_acted_this_run
                                    and _d.get("action") in ("APPROVE", "REJECT", "MODIFY")))
            if _d.get("action") not in ("APPROVE", "HOLD"):
                logger.info(f"  [ai:SHADOW] {strat_name}:{_s} — agent said "
                            f"{_d['action']} (x{_d.get('agent_size_multiplier', _d.get('size_multiplier'))}) "
                            f"— not acted on")
        except Exception as exc:
            logger.warning(f"  [ai] shadow-decision log failed for {_s}: {exc}")

    # RSI-threshold study registry: log every RSI(2) trigger in the study
    # band (incl. the 11-15 the live threshold rejects) + resolve past ones
    # against today's bars. Observe-only -- the live entry threshold is
    # unchanged. See forex/rsi_signal_registry.py + report_rsi_thresholds.py.
    if strat_name == "rsi" and not dry_run:
        try:
            fired = {s["symbol"] for s in signals}
            rsi_signal_registry.observe(ACCOUNT_ENV, market_data,
                                        fired_syms=fired, taken_syms=set(entered_syms))
            rsi_signal_registry.resolve(ACCOUNT_ENV, market_data)
        except Exception as exc:
            logger.warning(f"  [rsi] signal-registry logging failed: {exc}")
    return entries


# ── Heal missing stop / TP orders ─────────────────────────────────────────────

def _fetch_open_orders(akey: str) -> set | None:
    """
    Return set of (uic, buy_sell, order_type) for all open GTC orders.
    Returns None if the query fails (heal will be skipped to avoid duplicates).
    """
    try:
        resp   = _get("/port/v1/orders/me", {"AssetType": ASSET_TYPE})
        orders = resp.get("Data", [])
        result = set()
        for o in orders:
            dur = o.get("Duration", {}).get("DurationType", "")
            if dur == "GoodTillCancel":
                # Saxo's live order objects carry the type under
                # "OpenOrderType", not "OrderType" -- confirmed live 2026-08-22
                # (dumped a real order response). Reading "OrderType" here
                # returned None for every order, so this dedup check never
                # matched and _heal_missing_stops/_heal_missing_tp couldn't
                # tell an existing live order from a missing one.
                result.add((o.get("Uic"), o.get("BuySell"), o.get("OpenOrderType")))
        return result
    except Exception as exc:
        logger.warning(f"[heal] Could not fetch open orders from Saxo: {exc} — skipping heal")
        return None


def _heal_missing_stops(positions: dict, akey: str) -> int:
    """
    Place GTC stop orders for positions missing a stop_order_id.
    Queries Saxo first to avoid creating duplicate stops.
    Returns number of stops successfully placed.
    """
    missing = [(k, v) for k, v in positions.items()
               if v.get("stop_order_id") is None]
    if not missing:
        return 0

    open_orders = _fetch_open_orders(akey)
    if open_orders is None:
        return 0  # can't safely distinguish existing from missing — skip

    healed = 0
    for key, pos in missing:
        sym        = key.split(":", 1)[1] if ":" in key else key
        uic        = pos["uic"]
        direction  = pos.get("direction", "Buy")
        qty        = pos["quantity"]
        stop_price = pos["stop_price"]
        close_side = "Sell" if direction == "Buy" else "Buy"
        # Same rounding rule as saxo_order.place_with_stop — this healing path
        # had its own independent JPY-only/5dp guess, which is exactly the bug
        # that let AUDTRY/CNH-pair stops fail with PriceNotInTickSizeIncrements
        # in the first place (2026-08-21). _round_order_price also snaps
        # metals (XAUJPY/XAUTHB/XPTZAR) to their real 1.0 tick — plain decimal
        # rounding there produced naked positions (2026-08-30).
        rounded    = _round_order_price(sym, stop_price)

        if (uic, close_side, "Stop") in open_orders or \
           (uic, close_side, "StopLimit") in open_orders:
            # Stop already exists in Saxo — just record it
            pos["stop_order_id"] = "synced"
            logger.debug(f"  [heal_stops] {key}  stop already in Saxo (synced)")
            continue

        logger.info(f"[heal_stops] {key} missing stop — placing {close_side} Stop@{rounded}...")
        stop_body = {
            "AccountKey":    akey,
            "Uic":           uic,
            "AssetType":     ASSET_TYPE,
            "Amount":        qty,
            "BuySell":       close_side,
            "OrderType":     "Stop",
            "OrderPrice":    rounded,
            "OrderDuration": {"DurationType": "GoodTillCancel"},
            "ManualOrder":   False,
        }
        try:
            resp     = _post("/trade/v2/orders", stop_body)
            stop_oid = str(resp.get("OrderId", "?"))
            pos["stop_order_id"] = stop_oid
            logger.info(f"  [heal_stops] {key}  stop_id={stop_oid}  ✓")
            healed += 1
        except Exception as exc:
            logger.warning(f"  [heal_stops] FAILED for {key}: {exc}")
    return healed


def _heal_missing_tp(positions: dict, akey: str) -> int:
    """
    Place GTC Limit TP orders for ANY position that has a tp_price but no
    tp_order_id -- every strategy now gets a tp_price at entry (its own
    session-range target for gap/london_breakout, DEFAULT_TP_RR's fallback
    for everything else, see _run_entries), so this is no longer gap-only.
    Queries Saxo first to avoid duplicates.
    Returns number of TP orders successfully placed.
    """
    missing = [(k, v) for k, v in positions.items()
               if v.get("tp_price") is not None and
               v.get("tp_order_id") is None]
    if not missing:
        return 0

    open_orders = _fetch_open_orders(akey)
    if open_orders is None:
        return 0

    healed = 0
    for key, pos in missing:
        sym        = key.split(":", 1)[1] if ":" in key else key
        uic        = pos["uic"]
        direction  = pos.get("direction", "Buy")
        qty        = pos["quantity"]
        tp_price   = pos["tp_price"]
        close_side = "Sell" if direction == "Buy" else "Buy"
        rounded_tp = _round_order_price(sym, tp_price)

        if (uic, close_side, "Limit") in open_orders:
            pos["tp_order_id"] = "synced"
            logger.debug(f"  [heal_tp] {key}  TP already in Saxo (synced)")
            continue

        logger.info(f"[heal_tp] {key} missing TP — placing {close_side} Limit@{rounded_tp}...")
        tp_body = {
            "AccountKey":    akey,
            "Uic":           uic,
            "AssetType":     ASSET_TYPE,
            "Amount":        qty,
            "BuySell":       close_side,
            "OrderType":     "Limit",
            "OrderPrice":    rounded_tp,
            "OrderDuration": {"DurationType": "GoodTillCancel"},
            "ManualOrder":   False,
        }
        try:
            resp   = _post("/trade/v2/orders", tp_body)
            tp_oid = str(resp.get("OrderId", "?"))
            pos["tp_order_id"] = tp_oid
            logger.info(f"  [heal_tp] {key}  tp_id={tp_oid}  ✓")
            healed += 1
        except Exception as exc:
            logger.warning(f"  [heal_tp] FAILED for {key}: {exc}")
    return healed


# ── Cross-process lock ─────────────────────────────────────────────────────────
# Discovered live 2026-08-24: `ATOS Forex Gap Fill` and `ATOS Forex Gap Monday
# Early` both fire at the exact same instant (Mon 03:00 PKT). This is a real
# double-entry risk, not just redundant scanning: forex_state.json's atomic
# write (_save_state) only prevents file CORRUPTION, not a race between two
# full processes -- both can load state before either writes back, both
# independently decide to open the same signal, both place a REAL Saxo
# order, and whichever saves last silently drops the other's position from
# local state while both real orders exist on Saxo. Same class of bug as the
# LBO duplicate-schedule fix from 2026-08-20 ([[forex_london_breakout]]).
# Extended the same day to a shared proc_lock.py once intraday_monitor.py
# was found to independently read/write this SAME forex_state.json from a
# completely separate process, invisible to a forex-runner-only lock -- see
# proc_lock.py's module docstring for the full writeup and FUTURES_LOCK's
# equivalent fix for futures_state.json.
import proc_lock


def _lock_path() -> str:
    """LIVE uses its own lock, entirely separate from FOREX_LOCK -- see
    proc_lock.FOREX_LIVE_LOCK's docstring. intraday_monitor.py (SIM-only)
    re-acquires FOREX_LOCK every minute; sharing it with LIVE meant a real
    live run could sit polling against an unrelated process for several
    minutes despite there being zero actual shared-file risk between them."""
    if ACCOUNT_ENV == "live":
        return proc_lock.FOREX_LIVE_LOCK
    if ACCOUNT_ENV == "ai_sim":
        return proc_lock.FOREX_AI_LOCK
    return proc_lock.FOREX_LOCK


def _acquire_lock(label: str = "") -> bool:
    return proc_lock.acquire(_lock_path(), label, logger=logger)


def _release_lock() -> None:
    proc_lock.release(_lock_path())


# ── Main daily cycle ──────────────────────────────────────────────────────────

def run_exits_only(dry_run: bool = True,
                   active_strategies: list | None = None,
                   session: str = "all") -> dict:
    """Check stops and time-stops on all open positions — no new entries."""
    # No weekday gate by design -- see run_daily() above for why.
    if active_strategies is None:
        active_strategies = list(STRATEGIES)

    session_filter = SESSION_PAIRS.get(session) if session != "all" else None
    active_pairs   = [p for p in PAIRS
                      if session_filter is None or p["symbol"] in session_filter]
    active_pairs   = _filter_pairs_for_account(active_pairs)

    mode = "DRY-RUN" if dry_run else f"LIVE (Saxo {ACCOUNT_ENV.upper()})"
    logger.info("=" * 60)
    logger.info(f"  FX Runner [EXITS-ONLY] — {mode}  session={session}  "
                f"{datetime.now().strftime('%Y-%m-%d %H:%M')}")
    logger.info("=" * 60)

    if not dry_run and not _verify_token("exits-only"):
        return {"exits": 0, "holding": 0, "dry_run": False, "error": "token_expired"}

    state     = _load_state()
    positions = state.setdefault("positions", {})
    _, akey   = _account()
    today_str = date.today().isoformat()

    logger.info(f"Open positions : {len(positions)}")
    logger.info(f"Pairs checked  : {len(active_pairs)} ({session} session)")

    if not dry_run:
        healed = _heal_missing_stops(positions, akey) + _heal_missing_tp(positions, akey)
        if healed:
            _save_state(state)

    market_data: dict[str, pd.DataFrame | None] = {}
    for pair in active_pairs:
        market_data[pair["symbol"]] = _fetch_history(pair["uic"])
    _add_held_position_history(market_data, positions)

    # Repair any stale forming bar (see _repair_stale_forming_bars) so
    # should_exit() sees the same price the close order will execute at.
    # Only the HELD pairs matter here -- no need to price all 184.
    _held_syms = {k.split(":", 1)[1] for k in positions if ":" in k}
    _held_pairs = [_PAIRS_BY_SYMBOL[s] for s in _held_syms if s in _PAIRS_BY_SYMBOL]
    _rep = _repair_stale_forming_bars(market_data, _fetch_live_prices(_held_pairs))
    if _rep:
        logger.warning(f"  [chart] repaired {_rep} stale forming bar(s) to the live quote")

    total_exits = 0
    for strat_name in active_strategies:
        strat_mod = STRATEGIES[strat_name]
        exits = 0
        try:
            exits = _run_exits(strat_name, strat_mod, positions,
                               market_data, akey, dry_run, today_str, state=state)
        except Exception as exc:
            # See the matching try/except in run_daily() for the full
            # writeup -- same fix, same reason: one rejected close order
            # must not erase every other strategy's already-completed
            # closes from this same run.
            logger.error(f"  [{strat_name}] exits pass crashed, continuing "
                        f"to next strategy (state already saved below): {exc}")
        if not dry_run:
            _save_state(state)
        if exits:
            logger.info(f"  [{strat_name}] Closed {exits} position(s)")
        total_exits += exits

    for strat_name in _legacy_exit_strategies(active_strategies, positions):
        logger.info(f"  [{strat_name}] legacy position(s) held — running its own "
                    f"exit rules (entries stay blocked)")
        try:
            n = _run_exits(strat_name, STRATEGIES[strat_name], positions,
                           market_data, akey, dry_run, today_str, state=state)
        except Exception as exc:
            logger.error(f"  [{strat_name}] legacy exits pass crashed, continuing: {exc}")
            n = 0
        if not dry_run:
            _save_state(state)
        if n:
            logger.info(f"  [{strat_name}] (legacy, exits only) Closed {n} position(s)")
        total_exits += n

    logger.info("=" * 60)
    logger.info(f"  EXITS-ONLY complete — Closed: {total_exits}  "
                f"|  Still holding: {len(positions)}")
    for key, pos in positions.items():
        strat, sym = key.split(":", 1) if ":" in key else ("ema", key)
        df      = market_data.get(sym)
        cur_px  = float(df["Close"].iloc[-1]) if df is not None else pos["entry_price"]
        is_long = pos.get("direction", "Buy") == "Buy"
        pnl_pct = ((cur_px - pos["entry_price"]) / pos["entry_price"] * 100
                   if is_long else
                   (pos["entry_price"] - cur_px) / pos["entry_price"] * 100)
        held    = (date.today() - date.fromisoformat(pos.get("entry_date", today_str))).days
        tag     = "L" if is_long else "S"
        logger.info(f"  HOLD [{strat}] {sym}[{tag}]  "
                    f"entry={pos['entry_price']:.5f}  now={cur_px:.5f}  "
                    f"P&L {pnl_pct:+.2f}%  stop={pos['stop_price']:.5f}  {held}d")
    logger.info("=" * 60)

    # See run_daily() — dry-run exits mutate `positions`, so saving would delete
    # real open positions from tracking.
    if dry_run:
        logger.info("  [DRY] state NOT saved — open positions left untouched")
    else:
        state["last_exits_check"] = datetime.now().isoformat()
        _save_state(state)
        _close_orphan_ledger_rows(positions)
        _reconcile_closed_vs_saxo()
    return {"exits": total_exits, "holding": len(positions), "dry_run": dry_run}


def run_daily(dry_run: bool = True, active_strategies: list | None = None,
              session: str = "all") -> dict:
    # No weekday gate here by design: user explicitly wants every scheduled
    # trigger to keep scanning all 7 days rather than risk missing a signal
    # (e.g. a broker-side partial Saturday session, a scan running slightly
    # early/late relative to the exact weekend close/reopen boundary). A
    # scan that finds a genuinely closed market just finds no fresh data
    # and no signals -- harmless. Skipping outright is the riskier failure
    # mode if the boundary assumption is ever wrong.
    if active_strategies is None:
        active_strategies = list(_ACTIVE_STRATEGIES)   # the 5-strategy SIM roster (2026-09-02)

    _reset_order_circuit()   # per-run state; explicit reset for in-process re-calls

    session_filter = SESSION_PAIRS.get(session) if session != "all" else None
    active_pairs   = [p for p in PAIRS
                      if session_filter is None or p["symbol"] in session_filter]
    active_pairs   = _filter_pairs_for_account(active_pairs)

    strat_label = "+".join(active_strategies)
    mode        = "DRY-RUN" if dry_run else f"LIVE (Saxo {ACCOUNT_ENV.upper()})"
    run_time    = datetime.now().strftime("%H:%M")
    logger.info("=" * 60)
    logger.info(f"  FX Runner [{strat_label}] — {mode}  session={session}  "
                f"{datetime.now().strftime('%Y-%m-%d %H:%M')}")
    logger.info("=" * 60)

    # ── Token guard — email alert + abort if expired ──────────────────────────
    if not dry_run and not _verify_token(run_time):
        return {"exits": 0, "entries": 0, "holding": 0,
                "dry_run": False, "error": "token_expired"}

    state     = _load_state()
    positions = state.setdefault("positions", {})
    equity, akey = _account()
    today_str    = date.today().isoformat()

    # Forward-SIM observation (2026-08-27): one exposure snapshot per run
    # cycle, not per strategy -- exposure barely moves within one cycle,
    # logging it once per strategy would just be noise.
    forward_observation.log_exposure_snapshot(
        account_env=ACCOUNT_ENV,
        count_exposure=_currency_exposure(positions),
        notional_exposure_eur=_currency_exposure_notional_eur(positions),
        equity_eur=equity if ACCOUNT_ENV == "sim" else None,  # equity is SEK/EUR-native for live accounts, not EUR
    )

    total_slots = sum(SLOTS_PER_STRATEGY[s] for s in active_strategies)
    logger.info(f"Account equity : {equity:,.0f}")
    logger.info(f"Open positions : {len(positions)} / {total_slots} total slots")
    logger.info(f"FX pairs scanned: {len(active_pairs)} of {len(PAIRS)} ({session} session)")
    logger.info(f"Strategies     : {strat_label}")

    # ── Portfolio risk pre-flight ─────────────────────────────────────────────
    # 2026-08-24: daily loss limit and drawdown circuit breaker no longer
    # block entries -- explicit request ("do not block any new entries, let
    # it run freely, we need to test all strategies"). Both checks still run
    # and log so today's real P&L/drawdown stays visible; neither can stop a
    # strategy from entering anymore. Margin/heat gates are unrelated (they
    # protect against exhausting shared capacity, not against a bad P&L day)
    # and are untouched.
    if not dry_run:
        _update_peak_equity(equity)
    loss_limit_hit  = not dry_run and _entries_blocked_by_loss_limit(equity)
    drawdown_paused = not dry_run and not _drawdown_allows_entry(equity)
    entries_blocked = False
    if loss_limit_hit or drawdown_paused:
        reason = "daily loss limit" if loss_limit_hit else "drawdown circuit breaker"
        logger.info(f"  [RISK] {reason} condition is true, but no longer blocks entries (disabled for testing)")
    heat_pct = _portfolio_heat_pct(positions, equity)
    logger.info(f"Portfolio heat : {heat_pct:.1%}  (limit {PORTFOLIO_HEAT_LIMIT:.0%})")

    # ── Heal missing stop / TP orders (e.g. from prior API failures) ────────────
    healed_stops = healed_tp = 0
    if not dry_run:
        healed_stops = _heal_missing_stops(positions, akey)
        healed_tp    = _heal_missing_tp(positions, akey)
        if healed_stops or healed_tp:
            _save_state(state)

    # ── Fetch price history once for active session pairs ─────────────────────
    market_data: dict[str, pd.DataFrame | None] = {}
    for pair in active_pairs:
        market_data[pair["symbol"]] = _fetch_history(pair["uic"])
    _add_held_position_history(market_data, positions)

    # ── Live prices for EVERY active/held pair, then repair stale chart bars ──
    # Always fetched now (was gated on the gap strategy): every strategy's
    # entry/exit decision must be made on a forming bar that matches the
    # price it can actually trade at, not a frozen SIM chart print.
    live_prices: dict = _fetch_live_prices(active_pairs)
    for _sym in list(market_data):
        if _sym not in live_prices:
            _pi = _PAIRS_BY_SYMBOL.get(_sym)
            if _pi:
                _lp = _live_price(_pi["uic"], akey)
                if _lp:
                    live_prices[_sym] = _lp
    if live_prices:
        logger.info(f"Live prices fetched : {len(live_prices)} pairs")
    _repaired = _repair_stale_forming_bars(market_data, live_prices)
    if _repaired:
        logger.warning(f"  [chart] repaired {_repaired} stale forming bar(s) to the live quote")

    # ── Momentum pre-filter: restrict NEW entries to top trending pairs ────────
    # Exits always run on the full market_data (we never suppress stop-checks).
    # Entries only fire on the top 60% of pairs ranked by 20-day momentum / ATR.
    _top_n_entry = max(8, round(len(active_pairs) * 0.6))
    top_pairs    = _momentum_rank(market_data, top_n=_top_n_entry)
    entry_market_data = {k: v for k, v in market_data.items() if k in top_pairs}

    # ── Pre-compute cross-strategy agreement map for signal filter ────────────
    agreement = signal_filter.compute_agreement(market_data, live_prices, STRATEGIES)
    sf_status  = signal_filter.training_status(module=_pnl_module())
    logger.info(f"Signal filter: consensus active | "
                f"ML training data: {sf_status['labeled_trades']}/{signal_filter.MIN_TRADES_FOR_ML} trades "
                f"| ML model: {'✓ active' if sf_status['model_exists'] else '— not yet (need more data)'}")

    # ── Load strategy weights — higher weight runs first (priority access) ────
    strat_weights = strategy_learner.get_weights(_pnl_module())
    strategy_learner.log_weights_table(_pnl_module())
    # Sort active strategies by weight descending so proven winners get first
    # pick of currency exposure slots and portfolio heat capacity
    active_strategies = sorted(
        active_strategies,
        key=lambda s: strat_weights.get(s, 1.0),
        reverse=True,
    )

    # ── Run each strategy ─────────────────────────────────────────────────────
    # ai_sim only: load AI-written override module if one exists for this
    # strategy (forex/ai_variants/strategy_<name>_override.py). Falls back
    # to the original on any error. Never runs on live accounts.
    _forex_ai_overrides: dict = {}
    if ACCOUNT_ENV == "ai_sim":
        try:
            from forex.ai_variants import get_override as _get_forex_override
            for _sn in active_strategies:
                _ov = _get_forex_override(_sn)
                if _ov is not None:
                    _forex_ai_overrides[_sn] = _ov
                    logger.info(f"  [ai_sim] loaded AI override for {_sn}")
        except Exception as _ov_exc:
            logger.warning(f"  [ai_sim] forex ai_variants load error: {_ov_exc}")

    total_exits = total_entries = 0
    for strat_name in active_strategies:
        strat_mod = _forex_ai_overrides.get(strat_name) or STRATEGIES[strat_name]
        prefix    = f"{strat_name}:"
        holding   = sum(1 for k in positions if k.startswith(prefix))
        w         = strat_weights.get(strat_name, 1.0)
        logger.info(f"{'─'*60}")
        logger.info(f"  Strategy: {strat_name.upper()}  weight={w:.3f}  "
                    f"slots_scale=×{strategy_learner.slot_scale(w):.2f}")

        exits = entries = 0
        try:
            exits = _run_exits(strat_name, strat_mod, positions,
                               market_data, akey, dry_run, today_str, state=state)
            # entries_blocked is always False now (see the risk pre-flight
            # block above) -- kept as a variable rather than removed outright
            # so this stays a one-line revert if the loss-limit/drawdown
            # gates are ever turned back on.
            run_entries = (not entries_blocked
                           or (strat_name in DAY_TRADE_STRATEGIES and not loss_limit_hit))
            if run_entries:
                # Gap and LBO bypass the momentum filter — they need all pairs
                # for gap-percentage / session-breakout detection, unrelated
                # to trend. rsi/bb/zscore are MEAN-REVERSION strategies
                # (dip-buy, fade, z-score reversion) — the filter ranks by
                # DIRECTIONAL trend strength (price move / ATR), which is
                # backwards for them: their edge is catching
                # reversals/chop, so restricting them to only the
                # most-trending pairs suppresses exactly the setups they're
                # designed to find. Only trend-following strategies (ema,
                # donchian, pullback, supertrend, ml, cnn_lstm) should be
                # momentum-filtered.
                _NO_MOMENTUM_FILTER = ("gap", "gap_weekend", "london_breakout", "london_breakout_v2",
                                       "rsi", "rsi_trend", "bb", "zscore", "zscore_quality",
                                       # 2026-08-30: mean-reversion A/B variants -- exempt for the
                                       # same reason as their originals ("rsi"/"bb"): the momentum
                                       # pre-filter ranks by trend strength, which suppresses the
                                       # reversal setups they are designed to catch. (rsi_trend has
                                       # its OWN regime gate -- don't double-filter it.)
                                       "advanced_rsi_master", "advanced_bb_master",
                                       # 2026-09-02: bb_quality is bb + a non-directional-market
                                       # gate -- still mean-reversion, exempt like "bb". ema_trend
                                       # is trend-following (twins "ema") -- deliberately NOT here,
                                       # it SHOULD be momentum-filtered like its parent.
                                       "bb_quality")
                _edata = market_data if strat_name in _NO_MOMENTUM_FILTER else entry_market_data
                entries = _run_entries(strat_name, strat_mod, positions,
                                       _edata, equity, akey, dry_run, today_str,
                                       live_prices=live_prices, agreement=agreement,
                                       weight=w, regime_data=market_data, state=state)
        except Exception as exc:
            # 2026-08-25: a single rejected order (e.g. WouldExceedMargin)
            # used to crash out of this entire function uncaught, which
            # meant _save_state() at the bottom never ran and EVERY
            # already-completed close/entry from strategies processed
            # earlier in this same run -- real broker-side fills, not
            # simulated -- silently vanished from local tracking. Confirmed
            # live: this is exactly how gap:GBPNZD's real close on 2026-08-24
            # never persisted, so the next run still saw it as open, fired a
            # second close against an already-flat position, and created a
            # brand-new untracked 41,000 GBPNZD long. Catching here and
            # checkpointing state immediately below (whether this strategy
            # errored or not) means one bad order can no longer erase
            # anyone else's work.
            logger.error(f"  [{strat_name}] pass crashed, continuing to next "
                        f"strategy (state already saved below): {exc}")

        if not dry_run:
            _save_state(state)

        if exits == 0 and entries == 0:
            remaining = sum(1 for k in positions if k.startswith(prefix))
            logger.info(f"  [{strat_name}] No signals today  |  Holding: {remaining}")

        total_exits   += exits
        total_entries += entries

    # ── Legacy-position exit management (no entries) ──────────────────────────
    for strat_name in _legacy_exit_strategies(active_strategies, positions):
        logger.info(f"{'─'*60}")
        logger.info(f"  Strategy: {strat_name.upper()}  (legacy positions — exit rules only, no entries)")
        try:
            n = _run_exits(strat_name, STRATEGIES[strat_name], positions,
                           market_data, akey, dry_run, today_str, state=state)
        except Exception as exc:
            logger.error(f"  [{strat_name}] legacy exits pass crashed, continuing: {exc}")
            n = 0
        if not dry_run:
            _save_state(state)
        if n:
            logger.info(f"  [{strat_name}] (legacy, exits only) Closed {n} position(s)")
        total_exits += n

    # ── Summary ───────────────────────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info(f"  TOTAL — Exits: {total_exits}  |  Entries: {total_entries}  "
                f"|  Holding: {len(positions)}")
    for key, pos in positions.items():
        strat, sym = key.split(":", 1) if ":" in key else ("ema", key)
        df      = market_data.get(sym)
        cur_px  = float(df["Close"].iloc[-1]) if df is not None else pos["entry_price"]
        is_long = pos.get("direction", "Buy") == "Buy"
        pnl_pct = ((cur_px - pos["entry_price"]) / pos["entry_price"] * 100
                   if is_long else
                   (pos["entry_price"] - cur_px) / pos["entry_price"] * 100)
        held    = (date.today() - date.fromisoformat(pos.get("entry_date", today_str))).days
        tag     = "L" if is_long else "S"
        logger.info(f"  HOLD [{strat}] {sym}[{tag}]  qty={pos['quantity']:,}  "
                    f"entry={pos['entry_price']:.5f}  now={cur_px:.5f}  "
                    f"P&L {pnl_pct:+.2f}%  stop={pos['stop_price']:.5f}  {held}d")
    logger.info("=" * 60)

    # NEVER persist state from a dry run. _process_exits() does `del positions[key]`
    # unconditionally — simulated exits mutate the dict exactly like real ones — so
    # saving here would permanently delete tracking for real open positions. They
    # would stay open at the broker with no stop management and no record.
    # (Same defect was found and fixed in futures/runner.py; see a34ffd0.)
    if dry_run:
        logger.info("  [DRY] state NOT saved — open positions left untouched")
    else:
        state["last_run"] = datetime.now().isoformat()
        _save_state(state)
        _close_orphan_ledger_rows(positions)
        _reconcile_closed_vs_saxo()

    # ── Order-venue circuit breaker: one email at end of run + retry flag ─────
    if not dry_run:
        if _order_circuit_is_open():
            _venue_down_email_if_needed()
        else:
            _clear_venue_down_flag()   # a clean run -> Saxo is answering again

    # ── Operational-block escalation (LIVE): if something has silently been
    # stopping new entries for hours, route it to the one human-decision
    # channel (attention.py) so it emails once + nags daily until cleared.
    if not dry_run and ACCOUNT_ENV in ("live", "live_eur") and attention is not None:
        _note_operational_blocks()

    # ── Real-account equity snapshot (LIVE): one row on the equity curve
    # per run -- the honest peak/drawdown/return, NOT the sizing cap.
    # Reporting only; never gates anything. Once per pooled group is
    # enough, so only the SEK ("live") run does it.
    if not dry_run and ACCOUNT_ENV == "live":
        try:
            import account_equity
            account_equity.snapshot()
        except Exception as exc:
            logger.warning(f"  [account_equity] snapshot failed: {exc}")

    # ── Strategy learning pass — update weights from today's closed trades ────
    try:
        learn_result = strategy_learner.run_learning_pass(_pnl_module())
        if learn_result["new_trades"] > 0:
            logger.info(f"  [learner] Processed {learn_result['new_trades']} new trade(s) — "
                        f"weights updated")
    except Exception as exc:
        logger.warning(f"  [learner] Learning pass failed: {exc}")

    # ── Run-summary email (live only) ─────────────────────────────────────────
    if not dry_run:
        try:
            today_trades   = [t for t in trade_logger.tail(_pnl_module(), n=200)
                              if t.get("date") == today_str and t.get("mode") == "LIVE"]
            strategy_stats = pnl_tracker.get_strategy_summary(_pnl_module())
            fx_notify.send_run_summary(
                session        = session,
                entries        = total_entries,
                exits          = total_exits,
                holdings       = len(positions),
                equity         = equity,
                today_trades   = today_trades,
                strategy_stats = strategy_stats,
                healed_stops   = healed_stops,
                healed_tp      = healed_tp,
                live           = (ACCOUNT_ENV == "live"),
                # holdings counts strategy:symbol keys; pairs_trading is the
                # distinct-symbol count -- both shown so the email and the
                # dashboard header reconcile (see forex_dashboard.py note).
                pairs_trading  = len({k.split(":", 1)[1] for k in positions}),
                strategy_count = len(active_strategies),
                pair_count     = len(active_pairs),
                venue          = f"Saxo {ACCOUNT_ENV.upper()}",
            )
        except Exception as exc:
            logger.warning(f"Run-summary email failed: {exc}")

    return {"exits": total_exits, "entries": total_entries,
            "holding": len(positions), "dry_run": dry_run}


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="FX multi-strategy runner")
    ap.add_argument("--live",        action="store_true",
                    help="Place real orders in Saxo SIM (default: dry-run)")
    ap.add_argument("--exits-only",  action="store_true",
                    help="Check stops only — no new entries (intraday stop check)")
    ap.add_argument("--strategy", default="all",
                    help="Which strategy to run: 'all', a single name, or a "
                         "comma-separated list (e.g. donchian,ema,rsi). Choices: "
                         "ema, rsi, donchian, bb, pullback, gap, supertrend, "
                         "zscore, ml, cnn_lstm, london_breakout")
    ap.add_argument("--account", default="sim", choices=["sim", "live", "live_eur", "ai_sim"],
                    help="Which Saxo account to run against (default: sim). "
                         "'ai_sim' is the AI-decision SIM paper twin (Copilot "
                         "resize/skip applied; no real orders; own forex_ai ledger). "
                         "'live' is the real-money SEK account -- restricted to "
                         "LIVE_ALLOWED_STRATEGIES (rsi since 2026-08-31) and the "
                         "17-pair HIGH_VOLUME_SYMBOLS universe, requires "
                         "SAXO_LIVE_CONFIRMED=1 to place real orders. 'live_eur' is "
                         "the real-money EUR sub-account (added 2026-08-26) -- "
                         "restricted to LIVE_EUR_ALLOWED_STRATEGIES (rsi only), on "
                         "the 49-pair CORE_SYMBOLS universe. Both accounts run RSI, "
                         "so the 17 HIGH_VOLUME pairs are taken on both (safe pair "
                         "overlap via AccountKey-based reconciliation, see "
                         "housekeeping_live.py); requires SAXO_LIVE_EUR_CONFIRMED=1 "
                         "to place real orders.")
    ap.add_argument("--status",   action="store_true",
                    help="Print open positions and exit")
    ap.add_argument("--scan",     action="store_true",
                    help="Show 4-panel market snapshot")
    ap.add_argument("--info",     action="store_true",
                    help="Verify UICs via live Saxo quotes")
    ap.add_argument("--session",  default="all",
                    choices=["all", "asian", "london"],
                    help="Restrict to session pairs: asian (06:20 PKT) | london (18:00 PKT) | all")
    args = ap.parse_args()

    _VALID_STRATS = set(STRATEGIES)
    if args.strategy == "all":
        requested_strategies = None   # resolved below, account-dependent
    else:
        requested_strategies = [s.strip() for s in args.strategy.split(",") if s.strip()]
        bad = [s for s in requested_strategies if s not in _VALID_STRATS]
        if bad:
            ap.error(f"--strategy: unknown strategy/strategies {bad} -- "
                     f"choices are {sorted(_VALID_STRATS)}")

    set_account_env(args.account)

    if args.account in ("live", "live_eur") and args.live and LIVE_TRADING_HALTED:
        ap.error(
            f"LIVE_TRADING_HALTED is set in forex/runner.py -- all real-money "
            f"runs (--account {args.account} --live, entries AND exits) are "
            f"refused until this is cleared. Set by explicit user instruction "
            f"2026-08-26 after the P&L base-currency bug was found (a real "
            f"live_eur close's WIN/LOSS email and ledger figure were both "
            f"wrong -- see _position_net_pnl_quote_ccy() docstring and "
            f"test_2026_08_26_live_pnl_base_currency_bug.py) -- 'stop new "
            f"trades now until we fix the PL and Stop Loss properly... Stop "
            f"the scanner for ATOS LIVE completely until the issue is "
            f"resolved completely.' Existing positions keep their real "
            f"broker-side stop/TP orders regardless of this flag -- this "
            f"only blocks ATOS's own code from running for these accounts. "
            f"Clear LIVE_TRADING_HALTED only on the user's explicit go-ahead."
        )

    if args.account == "live":
        # ── Hard rails for the real-money account (2026-08-25) ────────────
        # Two independent gates, both required to place a real order:
        # (1) every requested strategy must be in the approved 3, and
        # (2) an explicit env-var confirmation, separate from --live itself,
        # so a copied/scheduled `--account live --live` can't silently place
        # real orders on a machine that hasn't deliberately opted in.
        effective_strats = requested_strategies or sorted(LIVE_ALLOWED_STRATEGIES)
        not_allowed = [s for s in effective_strats if s not in LIVE_ALLOWED_STRATEGIES]
        if not_allowed:
            ap.error(f"--account live only allows {sorted(LIVE_ALLOWED_STRATEGIES)} -- "
                     f"got {not_allowed}. This is a hard restriction on the "
                     "real-money account, not a default.")
        if args.live and os.environ.get("SAXO_LIVE_CONFIRMED") != "1":
            ap.error(
                "--account live --live requires SAXO_LIVE_CONFIRMED=1 in the "
                "environment as an explicit second confirmation before any "
                "real order can be placed. This is deliberate -- set it only "
                "on the machine/task you actually want placing real orders."
            )
        requested_strategies = effective_strats

    if args.account == "live_eur":
        # ── Hard rails for the EUR sub-account experiment (2026-08-26) ────
        # Same two-gate pattern as the SEK account above, but with its OWN
        # confirmation env var (SAXO_LIVE_EUR_CONFIRMED, not SAXO_LIVE_
        # CONFIRMED) -- deliberately separate so already having the SEK
        # account armed can never accidentally arm this one too. Each
        # real-money account needs its own explicit opt-in.
        effective_strats = requested_strategies or sorted(LIVE_EUR_ALLOWED_STRATEGIES)
        not_allowed = [s for s in effective_strats if s not in LIVE_EUR_ALLOWED_STRATEGIES]
        if not_allowed:
            ap.error(f"--account live_eur only allows {sorted(LIVE_EUR_ALLOWED_STRATEGIES)} -- "
                     f"got {not_allowed}. This is a hard restriction on the "
                     "real-money EUR account, not a default.")
        if args.live and os.environ.get("SAXO_LIVE_EUR_CONFIRMED") != "1":
            ap.error(
                "--account live_eur --live requires SAXO_LIVE_EUR_CONFIRMED=1 in "
                "the environment as an explicit second confirmation before any "
                "real order can be placed. This is deliberate -- set it only "
                "on the machine/task you actually want placing real orders."
            )
        requested_strategies = effective_strats

    if args.info:
        info_pairs = _filter_pairs_for_account(PAIRS)
        _info_note = {
            "live":     "  (CORE pairs only -- Uics must be re-verified for LIVE, never assume SIM's numbers carry over)",
            "live_eur": "  (EXOTIC pairs only -- Uics must be re-verified for LIVE, never assume SIM's numbers carry over)",
        }.get(ACCOUNT_ENV, "")
        print(f"\nAccount: {ACCOUNT_ENV.upper()}{_info_note}")
        print(f"\n{'Pair':<10} {'UIC':>6}  {'Bid':>10} {'Ask':>10}  Description")
        print("  " + "-" * 58)
        for pair in info_pairs:
            uic = pair["uic"]
            try:
                resp = _get("/trade/v1/infoprices",
                            {"Uic": uic, "AssetType": ASSET_TYPE, "FieldGroups": "Quote"})
                q   = resp.get("Quote", {})
                print(f"  {pair['symbol']:<10} {uic:>6}  "
                      f"{q.get('Bid','?'):>10} {q.get('Ask','?'):>10}  {pair['description']}")
            except Exception as exc:
                print(f"  {pair['symbol']:<10} {uic:>6}  ERROR: {exc}")
        sys.exit(0)

    if args.status:
        state     = _load_state()
        positions = state.get("positions", {})
        print(f"\nFX open positions ({len(positions)}):")
        if not positions:
            print("  None")
        for key, pos in positions.items():
            strat, sym = key.split(":", 1) if ":" in key else ("ema", key)
            held = (date.today() - date.fromisoformat(pos["entry_date"])).days
            tag  = "L" if pos.get("direction", "Buy") == "Buy" else "S"
            print(f"  [{strat}] {sym:<10}[{tag}]  qty={pos['quantity']:,}  "
                  f"entry={pos['entry_price']:.5f}  stop={pos['stop_price']:.5f}  {held}d")

        # Currency exposure summary
        exposure = _currency_exposure(positions)
        if exposure:
            # ASCII only — Windows' default console codepage (cp1252) can't
            # encode ▲/▼/±, which crashed this whole --status call with an
            # unhandled UnicodeEncodeError on a plain (non-UTF-8) console.
            limit = _max_currency_exposure()
            print(f"\nCurrency exposure (limit: +/-{limit}):")
            for ccy, net in sorted(exposure.items(), key=lambda x: abs(x[1]), reverse=True):
                if net == 0:
                    continue
                bar   = ("+" * abs(net)) if net > 0 else ("-" * abs(net))
                warn  = "  <- AT LIMIT" if abs(net) >= limit else ""
                print(f"  {ccy}  {net:+d}  {bar}{warn}")

        # Real economic exposure (EUR notional) -- visibility only, not a
        # gate yet. Position-count exposure above treats a 1,000-unit
        # position the same as a 48,000-unit one; this doesn't.
        notional = _currency_exposure_notional_eur(positions)
        if notional:
            print(f"\nCurrency exposure (EUR notional, visibility only -- not a gate):")
            for ccy, net_eur in sorted(notional.items(), key=lambda x: abs(x[1]), reverse=True):
                if abs(net_eur) < 1:
                    continue
                print(f"  {ccy}  {net_eur:+,.0f} EUR")
        sys.exit(0)

    if args.scan:
        market_data = {}
        for pair in PAIRS:
            market_data[pair["symbol"]] = _fetch_history(pair["uic"])
        scan_live_prices = _fetch_live_prices(PAIRS)

        # Panel 1 — EMA crossover
        print(f"\n[EMA] 5/30 crossover + ADX(14)")
        rows = strat_ema.scan_summary(market_data)
        print(f"  {'Pair':<10} {'Close':>10} {'FastEMA':>10} {'SlowEMA':>10} "
              f"{'Gap%':>7} {'ADX':>6} {'+DI':>6} {'-DI':>6}  Status")
        print("  " + "-" * 80)
        for r in rows:
            if r["status"] != "ok":
                print(f"  {r['symbol']:<10}  no data"); continue
            adx_flag = "TREND" if r["adx_ok"] else "range"
            print(f"  {r['symbol']:<10} {r['close']:>10.5f} {r['fast_ema']:>10.5f} "
                  f"{r['slow_ema']:>10.5f} {r['gap_pct']:>7.2f}% "
                  f"{r['adx']:>6.1f} {r['plus_di']:>6.1f} {r['minus_di']:>6.1f}"
                  f"  {r['trend']} / {adx_flag}")

        # Panel 2 — RSI(2) pullback
        print(f"\n[RSI] RSI(2) pullback within EMA(200) trend")
        rows = strat_rsi.scan_summary(market_data)
        print(f"  {'Pair':<10} {'Close':>10} {'RSI(2)':>8} {'EMA200':>12}  Trend  Signal")
        print("  " + "-" * 60)
        for r in rows:
            if r["status"] != "ok":
                print(f"  {r['symbol']:<10}  no data"); continue
            flag = f"  *** {r['flag']} ***" if r["flag"].strip() else ""
            print(f"  {r['symbol']:<10} {r['close']:>10.5f} {r['rsi2']:>8.1f} "
                  f"{r['ema200']:>12.5f}  {r['trend']}{flag}")

        # Panel 3 — Donchian
        print(f"\n[DONCHIAN] 20-day channel breakout")
        rows = strat_donchian.scan_summary(market_data)
        print(f"  {'Pair':<10} {'Close':>10} {'20d-Hi':>10} {'20d-Lo':>10}  Signal")
        print("  " + "-" * 60)
        for r in rows:
            if r["status"] != "ok":
                print(f"  {r['symbol']:<10}  no data"); continue
            print(f"  {r['symbol']:<10} {r['close']:>10.5f} {r['high20']:>10.5f} "
                  f"{r['low20']:>10.5f}  {r['signal']}")

        # Panel 4 — Bollinger Band reversion
        print(f"\n[BB] Bollinger Band(20,2) + RSI(14) mean reversion")
        rows = strat_bb.scan_summary(market_data)
        print(f"  {'Pair':<10} {'Close':>10} {'BB_Upper':>10} {'BB_Mid':>10} {'BB_Lower':>10} "
              f"{'BB%':>6} {'RSI14':>7}  Signal")
        print("  " + "-" * 80)
        for r in rows:
            if r["status"] != "ok":
                print(f"  {r['symbol']:<10}  no data"); continue
            flag = f"  *** {r['flag']} ***" if r.get("flag") else ""
            print(f"  {r['symbol']:<10} {r['close']:>10.5f} {r['bb_upper']:>10.5f} "
                  f"{r['bb_mid']:>10.5f} {r['bb_lower']:>10.5f} "
                  f"{r['bb_pct']:>6.1f}% {r['rsi14']:>7.1f}{flag}")

        # Panel 5 — Pullback to EMA(20)

        print(f"\n[PULLBACK] EMA(20) pullback in EMA(50) trend  (~70% WR)")
        rows = strat_pullback.scan_summary(market_data)
        print(f"  {'Pair':<10} {'Close':>10} {'EMA20':>10} {'EMA50':>10} "
              f"{'ADX':>6}  {'Trend':<6} {'ADX?':<6} {'PB?':<6}  Signal")
        print("  " + "-" * 80)
        for r in rows:
            if r["status"] != "ok":
                print(f"  {r['symbol']:<10}  no data"); continue
            adx_flag = "YES" if r["adx_ok"] else "no"
            pb_flag  = "YES" if r["pb_touch"] else "no"
            pos_flag = "above" if r["above_pb"] else "below"
            signal = ""
            if r["adx_ok"] and r["pb_touch"]:
                if r["trend"] == "BULL" and r["above_pb"]:
                    signal = "*** LONG SIGNAL ***"
                elif r["trend"] == "BEAR" and not r["above_pb"]:
                    signal = "*** SHORT SIGNAL ***"
            print(f"  {r['symbol']:<10} {r['close']:>10.5f} {r['ema20']:>10.5f} "
                  f"{r['ema50']:>10.5f} {r['adx']:>6.1f}  "
                  f"{r['trend']:<6} {adx_flag:<6} {pb_flag:<6}  {signal}")

        # Panel 6 — Weekend Gap Fill
        print(f"\n[GAP] Weekend Gap Fill  (~80-85% WR)  "
              f"{'— live prices available' if scan_live_prices else '— no live prices (run on Sunday)'}")
        rows = strat_gap.scan_summary(market_data, live_prices=scan_live_prices)
        print(f"  {'Pair':<10} {'Fri Close':>10} {'Sun Open':>10} {'Gap':>8} {'Gap%':>6}  Signal")
        print("  " + "-" * 70)
        for r in rows:
            if r["status"] != "ok":
                print(f"  {r['symbol']:<10}  no data"); continue
            gap_str = f"{r['gap']:+.5f}" if r['sunday_open'] > 0 else "n/a"
            pct_str = f"{r['gap_pct']:.3f}%" if r['sunday_open'] > 0 else "n/a"
            sun_str = f"{r['sunday_open']:.5f}" if r['sunday_open'] > 0 else "n/a"
            print(f"  {r['symbol']:<10} {r['friday_close']:>10.5f} {sun_str:>10} "
                  f"{gap_str:>8} {pct_str:>6}  {r['signal']}")

        # Panel 7 — SuperTrend
        print(f"\n[SUPERTREND] SuperTrend(10,3) + EMA(200)  (~65% WR)")
        rows = strat_supertrend.scan_summary(market_data)
        print(f"  {'Pair':<10} {'Close':>10} {'Direction':>10} {'ST Level':>10} {'EMA200':>10} {'ATR':>8}  Status")
        print("  " + "-" * 80)
        for r in rows:
            if r["status"] != "ok":
                print(f"  {r['symbol']:<10}  no data" if r["status"] == "no_data"
                      else f"  {r['symbol']:<10}  error"); continue
            dir_str = "BULL ↑" if r["direction"] == 1 else "BEAR ↓"
            ema_flag = ">" if r["close"] > r["ema200"] else "<"
            print(f"  {r['symbol']:<10} {r['close']:>10.5f} {dir_str:>10} "
                  f"{r['st_level']:>10.5f} {r['ema200']:>10.5f} {r['atr']:>8.5f}"
                  f"  price {ema_flag} EMA200")

        # Panel 8 — Z-Score Mean Reversion
        print(f"\n[ZSCORE] Z-Score(20) mean reversion + EMA(200)  (~63% WR)")
        rows = strat_zscore.scan_summary(market_data)
        print(f"  {'Pair':<10} {'Close':>10} {'Z-Score':>8} {'ATR':>10}  Signal")
        print("  " + "-" * 60)
        for r in rows:
            if r["status"] != "ok":
                print(f"  {r['symbol']:<10}  no data"); continue
            z = r["zscore"]
            flag = ""
            if z < -2.0:   flag = "  *** OVERSOLD → LONG ***"
            elif z > 2.0:  flag = "  *** OVERBOUGHT → SHORT ***"
            print(f"  {r['symbol']:<10} {r['close']:>10.5f} {z:>+8.2f} {r['atr']:>10.5f}{flag}")

        # Panel 9 — ML Signals
        print(f"\n[ML] Logistic Regression signals  (~57-62% WR)  [requires 336 bars]")
        rows = strat_ml.scan_summary(market_data)
        print(f"  {'Pair':<10} {'Close':>10} {'ML Prob':>8}  Signal")
        print("  " + "-" * 50)
        for r in rows:
            if r["status"] != "ok":
                print(f"  {r['symbol']:<10}  no data"); continue
            prob = r["ml_prob"]
            if prob is None:
                print(f"  {r['symbol']:<10} {r['close']:>10.5f}  {'—':>8}  insufficient bars"); continue
            flag = ""
            if prob >= 0.58:    flag = f"  *** BUY  (conf={prob:.2f}) ***"
            elif prob <= 0.42:  flag = f"  *** SELL (conf={1-prob:.2f}) ***"
            print(f"  {r['symbol']:<10} {r['close']:>10.5f} {prob:>8.3f}{flag}")

        # Panel 10 — CNN-LSTM deep learning
        if strat_cnn_lstm is None:
            print("\n[CNN-LSTM] unavailable (torch not installed) — skipping panel")
        else:
            print(f"\n[CNN-LSTM] Multi-scale CNN + BiLSTM + Attention  "
                  f"(36.9% val acc, threshold={strat_cnn_lstm.CONFIDENCE_THRESHOLD} — rarely fires)")
            rows = strat_cnn_lstm.scan_summary(market_data)
            print(f"  {'Pair':<10} {'Close':>10} {'P(Sell)':>8} {'P(Hold)':>8} {'P(Buy)':>8} {'ADX':>6}  Signal")
            print("  " + "-" * 70)
            for r in rows:
                if r["status"] == "no_model":
                    print(f"  {r['symbol']:<10}  no trained model"); continue
                if r["status"] != "ok":
                    print(f"  {r['symbol']:<10}  no data"); continue
                flag = f"  *** {r['signal']} ***" if r["signal"] != "hold" else ""
                print(f"  {r['symbol']:<10} {r['close']:>10.5f} {r['p_sell']:>8.3f} "
                      f"{r['p_hold']:>8.3f} {r['p_buy']:>8.3f} {r['adx']:>6.1f}{flag}")

        # Panel 11 — London/NY Breakout (day trading)
        print(f"\n[LBO] London/NY Session Breakout — day trading (~58-63% WR)")
        lbo_h1: dict = {}
        lbo_meta: dict = {}
        for pair in PAIRS:
            if pair["symbol"] not in strat_lbo.PAIRS:
                continue
            lbo_h1[pair["symbol"]]   = _fetch_history_h1(pair["uic"])
            lbo_meta[pair["symbol"]] = {"pip_size": pair.get("pip_size", 0.0001)}
        rows = strat_lbo.scan_summary(lbo_h1, lbo_meta)
        print(f"  {'Pair':<10} {'Range':>10} {'Hi':>10} {'Lo':>10} {'Close':>10} "
              f"{'Pips':>6}  Tradeable  Breakout")
        print("  " + "-" * 90)
        for r in rows:
            if r["status"] != "ok":
                print(f"  {r['symbol']:<10}  no data"); continue
            flag = f"  *** {r['breakout']} BREAKOUT ***" if r["breakout"] != "inside" else ""
            trd  = "yes" if r["tradeable"] else "no"
            print(f"  {r['symbol']:<10} {r['range_ref']:>10} {r['range_hi']:>10.5f} "
                  f"{r['range_lo']:>10.5f} {r['close']:>10.5f} {r['range_pip']:>6.1f}  "
                  f"{trd:^9}{flag}")

        sys.exit(0)

    active = requested_strategies if requested_strategies is not None else list(_ACTIVE_STRATEGIES)
    if requested_strategies is not None and ACCOUNT_ENV in ("sim", "ai_sim"):
        _explicit_retired = sorted(set(active) & RETIRED_STRATEGIES)
        _explicit_offroster = sorted((set(active) & set(STRATEGIES))
                                     - set(SIM_ACTIVE_STRATEGIES) - set(_explicit_retired))
        if _explicit_retired:
            logger.warning(f"  running RETIRED strateg{'y' if len(_explicit_retired)==1 else 'ies'} "
                           f"{_explicit_retired} on explicit --strategy request -- net-negative, see "
                           f"docs/strategy_decomposition_2026-09-02.md; running anyway for research")
        if _explicit_offroster:
            logger.warning(f"  {_explicit_offroster} not in the current SIM roster "
                           f"({SIM_ACTIVE_STRATEGIES}) -- dormant since 2026-09-02, "
                           f"running on explicit request")
    # Serialize concurrent live invocations project-wide -- see LOCK_FILE's
    # docstring above _acquire_lock() for why this exists (a real double-
    # entry risk between overlapping scheduled tasks, found 2026-08-24, not
    # just this file's two callers below). Dry-runs never place real orders
    # so they skip locking entirely -- no risk, and no reason to block on a
    # live run in progress while testing/debugging.
    if args.live:
        _acquire_lock("exits-only" if args.exits_only else "daily")
    try:
        if args.exits_only:
            # Exits-only is safe (and useful) to include LBO in "all" — it never
            # opens new positions here, only checks stops/time-stops, so running
            # it as an extra safety net alongside LBO's own dedicated force-close
            # schedule can't cause duplicate entries.
            run_exits_only(dry_run=not args.live, active_strategies=active,
                           session=args.session)
        else:
            # LBO has its own dedicated capital, slots, and schedule
            # (lbo-london-open / lbo-ny-open / lbo-force-close). It must NEVER
            # run as a side effect of the generic "all strategies" Daily/London
            # scheduled entries — those fire at times (e.g. 18:00 PKT / 13:00 UTC)
            # that land inside LBO's own auto-detected NY-session entry window,
            # which would silently duplicate the dedicated lbo-ny-open task's
            # entries (same signals, same pairs, double the position size).
            # LBO only runs here if explicitly requested via --strategy.
            if args.strategy == "all":
                # london_breakout_v2 has the same "own dedicated schedule,
                # never as an 'all' side effect" requirement as the
                # original -- same reasoning as the comment above.
                active = [s for s in active if s not in ("london_breakout", "london_breakout_v2")]
            run_daily(dry_run=not args.live, active_strategies=active,
                      session=args.session)

        if args.live:
            # Reconcile local state against live Saxo AND fix/verify every
            # naked-position and mismatch finding, right after every real
            # run, while this process still holds the lock -- catches drift
            # from Saxo's own opposite-direction netting or a race-condition
            # duplicate before the NEXT run's stops/exits act on stale
            # numbers. See housekeeping.py's and safeguard.py's module
            # docstrings (2026-08-24 audit found 24+ orphaned entries, 6
            # duplicate stops, and — once safeguard.py existed to actually
            # act instead of only report — 19 fully naked live positions).
            # LIVE runs its OWN safeguard_live.py (2026-08-25, later same
            # day) -- a fully separate module from SIM's safeguard.py, not
            # shared code/state, per explicit user direction. Built once
            # LIVE's schedule went to 9x/day: a naked-position safety net is
            # worth having in place before the first real entry, not after
            # an incident (exactly SIM's own history with safeguard.py).
            try:
                if ACCOUNT_ENV == "live":
                    import safeguard_live
                    safeguard_live.run_safeguard_live()
                elif ACCOUNT_ENV == "live_eur":
                    # 2026-08-26: EUR sub-account (RSI Pullback / 83 exotic
                    # pairs) gets its OWN safeguard, same reasoning as the
                    # SEK account's safeguard_live -- a fully separate
                    # module, never SIM's safeguard.py or the SEK account's
                    # safeguard_live.py.
                    import safeguard_live_eur
                    safeguard_live_eur.run_safeguard_live_eur()
                else:
                    import safeguard
                    safeguard.run_safeguard(["forex"])
            except Exception as exc:
                logger.warning(f"  [SAFEGUARD] post-run fix pass failed: {exc}")
    finally:
        if args.live:
            _release_lock()
