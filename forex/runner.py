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
import forex.strategy_rsi         as strat_rsi
import forex.strategy_advanced_rsi_master as strat_advanced_rsi_master
import forex.strategy_donchian    as strat_donchian
import forex.strategy_donchian_quality as strat_donchian_quality
import forex.strategy_bb          as strat_bb
import forex.strategy_advanced_bb_master as strat_advanced_bb_master
import forex.strategy_pullback    as strat_pullback
import forex.strategy_advanced_pullback_master as strat_advanced_pullback_master
import forex.strategy_gap         as strat_gap
import forex.strategy_gap_weekend as strat_gap_weekend
import forex.strategy_supertrend  as strat_supertrend
import forex.strategy_zscore      as strat_zscore
import forex.strategy_ml                as strat_ml
import forex.strategy_advanced_ml       as strat_advanced_ml
import forex.strategy_cnn_lstm         as strat_cnn_lstm
import forex.strategy_advanced_cnn_lstm_master as strat_advanced_cnn_lstm_master
import forex.strategy_london_breakout  as strat_lbo
import forex.strategy_london_breakout_v2 as strat_lbo_v2
import pnl_tracker
import trade_logger
import strategy_learner
import forex.notifier      as fx_notify
import forex.signal_filter as signal_filter
import forex.forward_observation as forward_observation
import forex.exit_advisor  as exit_advisor
import forex.rsi_signal_registry as rsi_signal_registry

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
    "rsi":         strat_rsi,
    # 2026-08-30: SIM-only A/B vs "rsi" (user-supplied "master" design).
    # Robust one-sided RSI(2), EMA50/EMA200 alignment + EMA200 slope, a
    # minimum EMA200-distance gate, ATR-percentile band, a post-extreme
    # reversal-confirmation bar, and DI confirmation. "rsi" (and the LIVE_EUR
    # account that runs it) is completely untouched -- this is a shadow/A/B
    # on SIM only, never in either LIVE allowlist.
    "advanced_rsi_master": strat_advanced_rsi_master,
    "donchian":    strat_donchian,
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
    "rsi": _SWING_SLOTS, "donchian": _SWING_SLOTS, "bb": _SWING_SLOTS,
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
    "supertrend": _SWING_SLOTS, "zscore": _SWING_SLOTS, "ml": _SWING_SLOTS, "cnn_lstm": _SWING_SLOTS,
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
LIVE_ALLOWED_STRATEGIES = {"rsi"}

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
LIVE_EUR_ALLOWED_STRATEGIES = {"rsi"}

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
    else:
        raise ValueError(f"Unknown account env {env!r} -- expected 'sim', 'live', or 'live_eur'.")
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
    return "forex"


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
_HEAT_LIMIT_BY_STRATEGY = {"rsi": 0.08}
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
PROFIT_LADDER_STRATEGIES         = {"rsi"}
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


def _sim_paper_fill_enabled() -> bool:
    return SIM_PAPER_FILL_ON_REJECT and ACCOUNT_ENV == "sim"


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
        rows = []
        bad_spread_bars = 0
        for bar in resp.get("Data", []):
            if not isinstance(bar, dict):
                continue
            if "CloseAsk" in bar and "CloseBid" in bar:
                ask_c = float(bar["CloseAsk"]); bid_c = float(bar["CloseBid"])
                if bid_c > 0 and (ask_c - bid_c) / bid_c > _MAX_SANE_BAR_SPREAD:
                    # Ask side is untrustworthy on this bar -- use Bid only.
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
    """Fetch current bid/ask mid prices for a list of pairs (used by gap strategy)."""
    prices = {}
    for pair in pairs:
        try:
            resp = _get("/trade/v1/infoprices", {
                "Uic": pair["uic"], "AssetType": ASSET_TYPE, "FieldGroups": "Quote"
            })
            q   = resp.get("Quote", {})
            bid = float(q.get("Bid", 0))
            ask = float(q.get("Ask", 0))
            if bid > 0 and ask > 0:
                prices[pair["symbol"]] = (bid + ask) / 2
        except Exception as exc:
            logger.debug(f"Live price fetch failed for {pair['symbol']}: {exc}")
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

def _run_exits(strat_name: str, strat_mod, positions: dict,
               market_data: dict, akey: str, dry_run: bool,
               today_str: str) -> int:
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
                entry_dt = pos.get("entry_date", today_str)
                since_entry = df  # df is already just recent history; entry_date bounds it implicitly enough for daily bars
                is_long_pos = pos.get("direction", "Buy") == "Buy"
                worst_price = float(since_entry["Low"].min()) if is_long_pos else float(since_entry["High"].max())
                best_price  = float(since_entry["High"].max()) if is_long_pos else float(since_entry["Low"].min())
                entry_px = float(pos.get("entry_price", 0))
                qty_pos  = pos.get("quantity", 0)
                sym_quote = sym[3:6] if len(sym) >= 6 else ""
                rate_pos  = _eur_per_unit(sym_quote, akey)
                if entry_px and rate_pos:
                    worst_pnl_eur = ((worst_price - entry_px) * qty_pos * rate_pos if is_long_pos
                                      else (entry_px - worst_price) * qty_pos * rate_pos)
                    best_pnl_eur = ((best_price - entry_px) * qty_pos * rate_pos if is_long_pos
                                     else (entry_px - best_price) * qty_pos * rate_pos)
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
        net_pnl_quote = None if (dry_run or _paper) else _position_net_pnl_quote_ccy(uic, qty, direction, entry)

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
                forward_observation.log_trade_exit_card(
                    card_id=card_id, exit_price=live_px, exit_reason=reason,
                    gross_pnl_eur=gross_pnl_eur, commission_eur=commission_eur,
                    net_pnl_eur=net_pnl_eur, r_multiple=r_multiple,
                    mae_eur=pos.get("mae_eur"), mfe_eur=pos.get("mfe_eur"),
                    holding_hours=holding_hours,
                    ladder_rung=pos.get("ladder_rung"),
                    ladder_rung_r=pos.get("ladder_rung_r"),
                )
        del positions[key]
        exits += 1
    return exits


def _run_entries(strat_name: str, strat_mod, positions: dict,
                 market_data: dict, equity: float, akey: str,
                 dry_run: bool, today_str: str,
                 live_prices: dict | None = None,
                 agreement: dict | None = None,
                 weight: float = 1.0) -> int:
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
    for sig in signals:
        if entries >= slots_free:
            break
        sym       = sig["symbol"]
        direction = sig["direction"]

        # ── Signal filter: consensus + ML meta-filter ──────────────────────
        passes, features, reason = signal_filter.evaluate(
            sym, direction, sig, agreement, STRATEGIES, firing_strategy=strat_name,
            module=_pnl_module())
        if not passes:
            logger.info(f"  [{strat_name}] SKIP {sym}[{direction}] "
                        f"— signal_filter: {reason}")
            continue
        agrees = features["agreement_count"]
        ml_info = (f"  ml_prob={features['ml_prob']}" if features.get("ml_prob") else "")

        if not _currency_ok(sym, direction, exposure):
            logger.info(f"  [{strat_name}] SKIP {sym}[{direction}] "
                        f"— currency exposure limit (max {_max_currency_exposure()})")
            continue
        opposing = _opposing_strategy_holds(sym, direction, positions)
        if opposing is not None:
            logger.info(f"  [{strat_name}] SKIP {sym}[{direction}] "
                        f"— {opposing} already holds the opposite direction on {sym}, "
                        f"no upside to taking both sides")
            continue
        pair_info = get_pair(sym)
        uic       = pair_info["uic"]
        spread    = _spread_pct(uic)
        if spread is not None and spread > MAX_SPREAD_PCT:
            logger.info(f"  [{strat_name}] SKIP {sym}[{direction}] "
                        f"— spread {spread:.3f}% wider than {MAX_SPREAD_PCT}% "
                        f"(illiquid right now, not a good time to trade this pair)")
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
        eur_rate_for_log = _eur_per_unit(quote_ccy_for_log, akey)
        blocked = (round_trip_cost is not None and
                   expected_target_profit < round_trip_cost * MIN_EDGE_TO_COST_RATIO)
        forward_observation.log_cost_gate_decision(
            account_env=ACCOUNT_ENV, strategy=strat_name, symbol=sym, direction=direction,
            entry_price=sig["close"], stop_price=sig["stop_price"], tp_price=tp, qty=qty,
            expected_target_profit_quote=expected_target_profit, round_trip_cost_quote=round_trip_cost,
            expected_target_profit_eur=(expected_target_profit * eur_rate_for_log) if eur_rate_for_log else None,
            round_trip_cost_eur=(round_trip_cost * eur_rate_for_log) if (round_trip_cost is not None and eur_rate_for_log) else None,
            min_edge_to_cost_ratio=MIN_EDGE_TO_COST_RATIO,
            decision="BLOCKED" if blocked else "PASS",
            reason="cost_not_cleared" if blocked else ("cost_unknown" if round_trip_cost is None else ""),
        )
        if blocked:
            logger.info(f"  [{strat_name}] SKIP {sym}[{direction}] "
                        f"— target profit {expected_target_profit:.2f} doesn't clear "
                        f"{MIN_EDGE_TO_COST_RATIO}x round-trip cost ({round_trip_cost:.2f}) "
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

        if dry_run:
            logger.info(f"  [DRY] {direction:<4} {qty:,}x {sym}[{tag}] "
                        f"({strat_name})  @ {sig['close']:.5f}  "
                        f"stop={sig['stop_price']:.5f}  tp={tp:.5f}  {detail}{agree_tag}")
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
            eur_rate_entry = _eur_per_unit(quote_ccy_for_log, akey)
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
    return proc_lock.FOREX_LIVE_LOCK if ACCOUNT_ENV == "live" else proc_lock.FOREX_LOCK


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

    total_exits = 0
    for strat_name in active_strategies:
        strat_mod = STRATEGIES[strat_name]
        exits = 0
        try:
            exits = _run_exits(strat_name, strat_mod, positions,
                               market_data, akey, dry_run, today_str)
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
        active_strategies = list(STRATEGIES)

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

    # ── Momentum pre-filter: restrict NEW entries to top trending pairs ────────
    # Exits always run on the full market_data (we never suppress stop-checks).
    # Entries only fire on the top 60% of pairs ranked by 20-day momentum / ATR.
    _top_n_entry = max(8, round(len(active_pairs) * 0.6))
    top_pairs    = _momentum_rank(market_data, top_n=_top_n_entry)
    entry_market_data = {k: v for k, v in market_data.items() if k in top_pairs}

    # ── Fetch live prices if any strategy needs them (gap strategy) ───────────
    needs_live = any(getattr(STRATEGIES[s], "NEEDS_LIVE_PRICES", False)
                     for s in active_strategies)
    live_prices: dict = _fetch_live_prices(active_pairs) if needs_live else {}
    if live_prices:
        logger.info(f"Live prices fetched : {len(live_prices)} pairs")

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
    total_exits = total_entries = 0
    for strat_name in active_strategies:
        strat_mod = STRATEGIES[strat_name]
        prefix    = f"{strat_name}:"
        holding   = sum(1 for k in positions if k.startswith(prefix))
        w         = strat_weights.get(strat_name, 1.0)
        logger.info(f"{'─'*60}")
        logger.info(f"  Strategy: {strat_name.upper()}  weight={w:.3f}  "
                    f"slots_scale=×{strategy_learner.slot_scale(w):.2f}")

        exits = entries = 0
        try:
            exits = _run_exits(strat_name, strat_mod, positions,
                               market_data, akey, dry_run, today_str)
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
                                       "rsi", "bb", "zscore",
                                       # 2026-08-30: mean-reversion A/B variants -- exempt for the
                                       # same reason as their originals ("rsi"/"bb"): the momentum
                                       # pre-filter ranks by trend strength, which suppresses the
                                       # reversal setups they are designed to catch.
                                       "advanced_rsi_master", "advanced_bb_master")
                _edata = market_data if strat_name in _NO_MOMENTUM_FILTER else entry_market_data
                entries = _run_entries(strat_name, strat_mod, positions,
                                       _edata, equity, akey, dry_run, today_str,
                                       live_prices=live_prices, agreement=agreement,
                                       weight=w)
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

    # ── Order-venue circuit breaker: one email at end of run + retry flag ─────
    if not dry_run:
        if _order_circuit_is_open():
            _venue_down_email_if_needed()
        else:
            _clear_venue_down_flag()   # a clean run -> Saxo is answering again

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
    ap.add_argument("--account", default="sim", choices=["sim", "live", "live_eur"],
                    help="Which Saxo account to run against (default: sim). "
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

    active = requested_strategies if requested_strategies is not None else list(STRATEGIES)
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
