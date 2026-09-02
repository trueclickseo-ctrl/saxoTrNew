"""
forex_dashboard.py  —  Live Forex positions dashboard
------------------------------------------------------
Usage:
    python forex_dashboard.py            # refresh every 60s
    python forex_dashboard.py --fast     # refresh every 10s
    python forex_dashboard.py --once     # print once and exit
"""

import os, sys, json, time
from datetime import date

# 2026-08-25: matches futures_dashboard.py's own safeguard -- without this,
# any invocation whose stdout isn't already UTF-8 (piped/redirected output,
# a non-UTF-8 console codepage) crashes with UnicodeEncodeError on the box-
# drawing characters used throughout this file's output.
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR            = os.path.dirname(os.path.abspath(__file__))
FOREX_STATE_PATH    = os.path.join(BASE_DIR, "data", "forex_state.json")

sys.path.insert(0, BASE_DIR)
import price_service
from forex.universe import (
    PAIRS as _UNIVERSE_PAIRS, CORE_SYMBOLS, SCANDI_SYMBOLS, HIGH_VOLUME_SYMBOLS,
    CORE_STANDARD_SYMBOLS, METALS_SYMBOLS, EXOTIC_SYMBOLS, EXOTIC_ASIA_SYMBOLS,
    EXOTIC_EUROPE_SYMBOLS, EXOTIC_CARRY_SYMBOLS, EXOTIC_LATAM_MIDEAST_SYMBOLS,
)

_UNIVERSE_BY_SYMBOL = {p["symbol"]: p for p in _UNIVERSE_PAIRS}

REFRESH_SECONDS = 60

# ── Quote-currency -> EUR conversion (account base currency) ──────────────
# A pair's raw (now_px - entry) * qty P&L is in the PAIR's quote currency —
# JPY, CHF, NOK, CAD, AUD, GBP, etc. — not automatically EUR/USD. Summing
# those raw numbers across pairs and labeling the total one currency silently
# mixes currencies (a JPY pair's raw P&L is ~150x its true EUR value). See
# pnl_tracker.log_close's fx_rate_to_base param, fixed the same way 2026-08-21.
#
# Per explicit user direction (2026-08-22): the dashboard must always use
# Saxo prices, not Yahoo -- Yahoo is for historical/backtest data only.
# _fx_conversion_instruments() below adds the EUR{ccy}/USD{ccy} pairs this
# dashboard needs to the SAME batched price_service.fetch_prices() call
# already made for the held positions, so conversion rates come from the
# same live Saxo quotes as everything else on screen, no extra round trip.
_EUR_RATE_CACHE: dict = {}


def _fx_conversion_instruments(quote_ccys) -> list[dict]:
    """Saxo instruments needed to convert each of `quote_ccys` to EUR:
    EUR{ccy} directly if we trade it (every universe currency except
    AED/DKK), else USD{ccy} + EURUSD for triangulation."""
    needed, seen, need_eurusd = [], set(), False
    for ccy in quote_ccys:
        if ccy in ("EUR", "") or ccy in seen:
            continue
        p = _UNIVERSE_BY_SYMBOL.get(f"EUR{ccy}")
        if p:
            seen.add(f"EUR{ccy}")
            needed.append({"symbol": f"EUR{ccy}", "uic": p["uic"], "asset_type": "FxSpot"})
            continue
        p = _UNIVERSE_BY_SYMBOL.get(f"USD{ccy}")
        if p:
            need_eurusd = True
            seen.add(f"USD{ccy}")
            needed.append({"symbol": f"USD{ccy}", "uic": p["uic"], "asset_type": "FxSpot"})
    if need_eurusd and "EURUSD" not in seen:
        p = _UNIVERSE_BY_SYMBOL.get("EURUSD")
        if p:
            needed.append({"symbol": "EURUSD", "uic": p["uic"], "asset_type": "FxSpot"})
    return needed


def _eur_per_unit(ccy: str, live_prices: dict | None = None) -> float | None:
    """EUR value of one unit of `ccy`, from Saxo's live quotes only.

    Returns None if Saxo doesn't have a live quote for the needed pair(s)
    right now -- callers must treat that as "unknown," the same as a
    missing position price, NOT fall back to a non-Saxo source. Per
    explicit user direction (2026-08-22): Yahoo is for historical/backtest
    data only, never for a live SIM order or anything shown on this
    dashboard. price_service.fetch_prices() already retries misses once
    (see its own docstring) before this is even called, so a None here
    means Saxo genuinely had nothing for this cycle.
    """
    if ccy == "EUR":
        return 1.0
    if ccy in _EUR_RATE_CACHE:
        return _EUR_RATE_CACHE[ccy]

    rate = None
    live_prices = live_prices or {}
    direct = live_prices.get(f"EUR{ccy}")
    if direct:
        rate = 1.0 / direct
    else:
        usd_leg = live_prices.get(f"USD{ccy}")
        eur_usd = live_prices.get("EURUSD")
        if usd_leg and eur_usd:
            rate = 1.0 / (usd_leg * eur_usd)

    if rate is not None:
        _EUR_RATE_CACHE[ccy] = rate   # only cache a real hit -- a miss may
    return rate                       # resolve on the very next 60s refresh

# ── Windows console clear ─────────────────────────────────────────
def _clear_console():
    """Clear the console before each refresh.

    2026-08-25: switched from the Win32 FillConsoleOutputCharacterW approach
    to a straight ANSI clear. The old path filled exactly `cols * rows` cells
    starting from GetConsoleScreenBufferInfo's reported window -- on a ConPTY
    host (Windows Terminal / VS Code's integrated terminal, the common case
    now) that buffer geometry doesn't always match what's actually visible,
    so a longer previous frame's trailing characters could survive past the
    end of a shorter new line on the same row (reported live: old ALL/EXOTIC
    row text bleeding into a new CORE section header). `\033[2J` clears the
    visible screen, `\033[3J` also drops back-scroll (xterm-compatible, most
    modern Windows terminals honor it), `\033[H` homes the cursor -- VT
    processing is already enabled unconditionally below, so this is safe as
    the primary path, not just an exception fallback."""
    sys.stdout.write("\033[H\033[2J\033[3J")
    sys.stdout.flush()

# Enable VT colours for ANSI colour codes (does NOT affect clear logic)
try:
    import ctypes as _ct
    _k32 = _ct.windll.kernel32
    _h   = _k32.GetStdHandle(-11)
    _m   = _ct.c_ulong()
    _k32.GetConsoleMode(_h, _ct.byref(_m))
    _k32.SetConsoleMode(_h, _m.value | 0x4)
except Exception:
    pass

# ── Colours ───────────────────────────────────────────────────────
GR  = "\033[92m"   # bright green
RD  = "\033[91m"   # bright red
YL  = "\033[93m"   # yellow
BL  = "\033[94m"   # blue
CY  = "\033[96m"   # cyan
MG  = "\033[95m"   # magenta
W   = "\033[0m"    # reset
BD  = "\033[1m"    # bold
DM  = "\033[2m"    # dim
HOME  = "\033[H"
CLEAR = "\033[2J"

WH = "\033[97m"   # bright white

STRAT_COL = {
    "ema":        CY,
    "rsi":        MG,
    "donchian":   GR,
    "bb":         YL,
    "pullback":   BL,
    "gap":        WH,
    "supertrend": "\033[38;5;208m",   # orange
    "zscore":     "\033[38;5;147m",   # lavender
    "ml":         "\033[38;5;119m",   # lime
    "london_breakout": "\033[38;5;214m",   # amber — day trading book
    "cnn_lstm":   "\033[38;5;135m",   # purple
    # 2026-08-29: the 3 new SIM-only A/B-test strategies -- distinct colors
    # so they're never mistaken for their originals at a glance.
    "gap_weekend":        "\033[38;5;80m",    # teal
    "donchian_quality":   "\033[38;5;120m",   # light green
    "london_breakout_v2": "\033[38;5;220m",   # gold
    # 2026-08-30: the 6 user-supplied "advanced_*" SIM-only A/B strategies --
    # each a lighter shade of its original's colour so the pairing reads at
    # a glance (advanced_ema~ema, advanced_ml~ml, etc.).
    "advanced_ema":               "\033[38;5;51m",    # bright cyan  (~ema)
    "advanced_rsi_master":        "\033[38;5;213m",   # pink         (~rsi)
    "advanced_bb_master":         "\033[38;5;229m",   # pale yellow  (~bb)
    "advanced_pullback_master":   "\033[38;5;75m",    # light blue   (~pullback)
    "advanced_ml":                "\033[38;5;156m",   # pale green   (~ml)
    "advanced_cnn_lstm_master":   "\033[38;5;177m",   # light purple (~cnn_lstm)
}


def _read_positions() -> list:
    if not os.path.exists(FOREX_STATE_PATH):
        return []
    try:
        d         = json.load(open(FOREX_STATE_PATH, encoding="utf-8"))
        positions = d.get("positions", {})
        out = []
        for key, pos in positions.items():
            strat, sym = key.split(":", 1) if ":" in key else ("ema", key)
            out.append({
                "key":        key,
                "strategy":   strat,
                "symbol":     sym,
                "uic":        pos.get("uic"),
                "asset_type": pos.get("asset_type", "FxSpot"),
                "direction":  pos.get("direction", "Buy"),
                "qty":        pos.get("quantity", 0),
                "entry":      float(pos.get("entry_price", 0)),
                "stop":       float(pos.get("stop_price", 0)),
                "entry_date": pos.get("entry_date", ""),
                "atr":        float(pos.get("atr_at_entry", 0)),
                "order_id":   pos.get("order_id", "—"),
            })
        return out
    except Exception:
        return []


def _pad_ansi(s: str, width: int) -> str:
    """Right-pad string to visible `width`, ignoring ANSI escape codes."""
    import re
    visible = len(re.sub(r"\033\[[0-9;]*m", "", s))
    return s + " " * max(0, width - visible)


def _fetch_position_costs(token: str) -> dict:
    """{(uic, abs(amount)): TradeCostsTotalInBaseCurrency} for every open FxSpot
    position — spread cost at entry + accrued overnight swap/financing, NOT
    included in the raw price-based P&L shown per row below. One request for
    everything held, not per-position. See forex/runner.py's
    _position_pnl_base_ccy() for the same fix applied to the realized ledger —
    confirmed live 2026-08-21 that Saxo tracks this as a genuinely separate
    field from ProfitLossOnTrade (a position can show green gross P&L and be
    meaningfully less green, or red, once this is netted in)."""
    if not token:
        return {}
    try:
        import requests
        r = requests.get(price_service.SIM_BASE + "port/v1/positions/me",
                         headers={"Authorization": f"Bearer {token}"}, timeout=10)
        r.raise_for_status()
        out = {}
        for p in r.json().get("Data", []):
            pb, pv = p.get("PositionBase", {}), p.get("PositionView", {})
            if pb.get("AssetType") != "FxSpot":
                continue
            uic = pb.get("Uic")
            amt = abs(pb.get("Amount", 0))
            costs = pv.get("TradeCostsTotalInBaseCurrency")
            if uic is not None and costs is not None:
                out[(uic, amt)] = float(costs)
        return out
    except Exception:
        return {}


def _section_header(text: str, color: str, w: int) -> list:
    """Bordered box header for a major CORE/EXOTIC/ALL section.

    2026-08-25: added per explicit user request for "clear separation
    between core and exotic" -- a single-line box (vs the main dashboard
    title's double-line ╔═╗ box) makes each section impossible to mistake
    for plain body text or for a neighboring section, and the `color`
    (green=core/live-candidate, amber=exotic/SIM-only, dim=all-blended)
    keeps that distinction visible even mid-scroll, not just at the top."""
    return [
        f"  {BD}{color}┌{'─'*w}┐{W}",
        f"  {BD}{color}│{text:^{w}}│{W}",
        f"  {BD}{color}└{'─'*w}┘{W}",
        "",
    ]


STRAT_LABELS_ALL = {
    "ema":        "EMA Trend",
    # 2026-09-01 (user): "RSI Pullback" / "EMA Pullback" read as one combined
    # RSI+Pullback strategy -- they are two SEPARATE strategies. Renamed so
    # each row is unambiguous: rsi = the RSI mean-reversion one, pullback
    # = the EMA(20)-in-EMA(50) one.
    # 2026-09-02 (user): "RSI (2)" -> plain "RSI" -- the core day-1 strategy.
    # It was reading too close to "RSI2 (A/B)" (advanced_rsi_master, the
    # separate SIM-only variant just below). "RSI" = core, "RSI2" = variant.
    "rsi":        "RSI",
    "donchian":   "Donchian Break",
    "bb":         "BB Reversion",
    "pullback":   "Pullback ★",
    "gap":        "Gap Fill ★★",
    "supertrend": "SuperTrend",
    "zscore":     "Z-Score Rev",
    "ml":         "ML Signals",
    "cnn_lstm":   "CNN-LSTM",
    "london_breakout": "LBO Day Trade",
    # 2026-08-29: without an entry here, a strategy with real closed trades
    # is SILENTLY DROPPED from the per-strategy breakdown entirely (see
    # `ordered = [s for s in stats_by_strat if s in strat_labels] + ...`
    # below) -- not just badly colored, genuinely invisible.
    "gap_weekend":        "Gap Wknd (A/B)",
    "donchian_quality":   "Donchian Qual (A/B)",
    "london_breakout_v2": "LBO V2 (A/B)",
    # 2026-08-30: the 6 user-supplied "advanced_*" SIM-only A/B strategies.
    # Same rule as the 2026-08-29 note above -- no entry here == silently
    # dropped from the per-strategy breakdown even with real closed trades.
    "advanced_ema":               "EMA Adv (A/B)",
    "advanced_rsi_master":        "RSI2 (A/B)",   # 2026-09-02 (user): call it RSI2 — a separate strategy from the day-1 "rsi", must stay distinct
    "advanced_bb_master":         "BB Master (A/B)",
    "advanced_pullback_master":   "Pullback Mstr (A/B)",
    "advanced_ml":                "ML Adv (A/B)",
    "advanced_cnn_lstm_master":   "CNN-LSTM Mstr (A/B)",
}


def _strategy_breakdown_table(title: str, positions: list, live: dict,
                               symbols: set | None = None,
                               universe_size: int | None = None,
                               exclude: set = frozenset(),
                               color: str = CY,
                               total_label: str | None = None,
                               module: str = "forex",
                               currency_label: str = "EUR") -> list:
    """One STRATEGY BREAKDOWN table, optionally scoped to a pair subset.

    `symbols=None` -> the full-universe blended view (call with
    universe_size=len(forex.universe.PAIRS) for that case -- no longer
    defaulted to a hardcoded literal here, see the 2026-08-28 fix note in
    _render()). Passing forex.universe.CORE_SYMBOLS, SCANDI_SYMBOLS,
    METALS_SYMBOLS, or the exotic remainder/regions restricts realized/
    today/unrealized stats AND the active-position count to that subset --
    this is what lets the dashboard answer "is this tier actually better
    than the full universe?" directly, per-strategy, instead of only as
    one blended all-pairs number.

    `exclude` drops strategies that structurally never trade in this scope
    (e.g. london_breakout from the exotic-only table -- it only ever trades
    its own 28-pair core subset, so an exotic row for it would always be a
    meaningless 0/0).

    `total_label` names the grand-total row (e.g. "CORE TOTAL"). Fixed
    2026-08-25 -- this used to hardcode the literal string "ALL STRATEGIES"
    regardless of scope, so the CORE and EXOTIC tables' own totals row was
    mislabeled as if it were the blended all-117 total, undermining the
    exact "clear separation" this split exists to provide."""
    import pnl_tracker
    total_label = total_label or title

    strat_labels = {k: v for k, v in STRAT_LABELS_ALL.items() if k not in exclude}
    lbo_slots    = 28 if "london_breakout" not in exclude else None
    # 2026-08-29: the two new REAL-capped strategies (unlike gap_weekend,
    # which -- like "gap" -- uses the full swing universe_size) -- see
    # SLOTS_PER_STRATEGY in forex/runner.py for where these caps live.
    STRAT_MAX_SLOTS_OVERRIDE = {"london_breakout_v2": 4, "donchian_quality": 4}

    if symbols is not None:
        pos_in_scope = [p for p in positions if p["symbol"] in symbols]
    else:
        pos_in_scope = positions

    try:
        strat_rows = pnl_tracker.get_strategy_summary(module, symbols=symbols)
        today_str  = date.today().isoformat()
        today_rows = pnl_tracker.get_strategy_summary_since(module, today_str, symbols=symbols)
    except Exception:
        strat_rows = []
        today_rows = []
    stats_by_strat = {r["strategy"]: r for r in strat_rows if r["strategy"] not in exclude}
    today_by_strat = {r["strategy"]: r for r in today_rows if r["strategy"] not in exclude}

    ordered = [s for s in stats_by_strat if s in strat_labels]
    ordered += sorted(s for s in strat_labels if s not in stats_by_strat)

    W_TOTAL = 108
    HR = f"  {DM}{'─' * W_TOTAL}{W}"
    L = _section_header(title, color, W_TOTAL)
    L.append(
        f"  {DM}{'Strategy':<16}  {'Active':>6}  {'Closed':>7}  {'W/L':>7}  "
        f"{'WR%':>6}  {'PF':>6}  {'All-Time P&L':>15}  {'Today':>11}  {'Unrealized':>13}{W}"
    )
    L.append(HR)

    grand_realized = grand_unrealized = grand_gp = grand_gl = 0.0
    grand_closed = grand_wins = grand_losses = 0
    grand_active = len(pos_in_scope)

    for strat in ordered:
        label      = strat_labels[strat]
        max_slots  = (lbo_slots if strat == "london_breakout"
                      else STRAT_MAX_SLOTS_OVERRIDE.get(strat, universe_size))
        sc    = STRAT_COL.get(strat, DM)
        count = sum(1 for p in pos_in_scope if p["strategy"] == strat)

        unrealized = 0.0
        for p in pos_in_scope:
            if p["strategy"] != strat:
                continue
            sym    = p["symbol"]
            now_px = live.get(sym)
            if now_px and p["entry"] > 0:
                quote_ccy = sym[3:6] if len(sym) >= 6 else ""
                eur_rate  = _eur_per_unit(quote_ccy, live)
                if eur_rate is not None:
                    raw = (now_px - p["entry"]) if p["direction"] == "Buy" else (p["entry"] - now_px)
                    unrealized += raw * p["qty"] * eur_rate

        stats    = stats_by_strat.get(strat, {})
        realized = stats.get("total_pnl", 0.0)
        n_closed = stats.get("trades", 0)
        wins     = stats.get("wins", 0)
        losses   = stats.get("losses", 0)
        wr       = stats.get("win_rate", 0.0)
        pf       = stats.get("profit_factor")
        best     = stats.get("best", 0.0)

        today_stats = today_by_strat.get(strat, {})
        today_pnl   = today_stats.get("total_pnl", 0.0)
        today_n     = today_stats.get("trades", 0)

        grand_realized   += realized
        grand_unrealized += unrealized
        grand_closed     += n_closed
        grand_wins       += wins
        grand_losses     += losses
        grand_gp         += stats.get("gross_profit", 0.0)
        grand_gl         += stats.get("gross_loss", 0.0)

        pnl_col   = GR if realized >= 0 else RD
        u_col     = GR if unrealized >= 0 else RD
        wr_col    = GR if wr >= 50 else (YL if n_closed else DM)
        pf_s      = f"{pf:.2f}" if pf is not None else ("∞" if realized > 0 and n_closed else "—")
        wr_s      = f"{wr:.1f}%" if n_closed else "—"
        today_col = GR if today_pnl >= 0 else RD

        realized_cell   = _pad_ansi(f"{pnl_col}{BD}{realized:>+,.0f} {currency_label}{W}", 15)
        today_cell      = _pad_ansi(f"{today_col}{today_pnl:>+,.0f} {currency_label}{W}" if today_n else f"{DM}—{W}", 11)
        unrealized_cell = _pad_ansi(f"{u_col}{unrealized:>+,.0f} {currency_label}{W}", 13)
        L.append(
            f"  {sc}{BD}{label:<16}{W}  {DM}{f'{count}/{max_slots}':>6}{W}  "
            f"{n_closed:>7}  {DM}{f'{wins}W/{losses}L':>7}{W}  "
            f"{wr_col}{wr_s:>6}{W}  {DM}{pf_s:>6}{W}  "
            f"{realized_cell}  {today_cell}  {unrealized_cell}"
        )
        if realized > 0 and today_pnl < 0 and abs(today_pnl) >= 0.25 * realized:
            L.append(
                f"  {DM}{'':<16}  {RD}⚠ today alone is {today_pnl:>+,.0f} {currency_label} — "
                f"lifetime total above nets positive only because of earlier trades{W}"
            )
        if n_closed >= 2 and realized > 0 and best > 0 and best >= 0.5 * realized:
            pct = best / realized * 100
            L.append(
                f"  {DM}{'':<16}  {YL}⚠ single best trade ({best:>+,.0f} {currency_label}) is {pct:.0f}% of "
                f"this total — check it isn't propping up an otherwise-losing strategy{W}"
            )

    L.append(HR)
    g_wr   = (grand_wins / grand_closed * 100) if grand_closed else 0.0
    g_pf   = (grand_gp / grand_gl) if grand_gl > 0 else None
    g_pf_s = f"{g_pf:.2f}" if g_pf is not None else "—"
    grand_today = sum(r.get("total_pnl", 0.0) for r in today_rows if r["strategy"] not in exclude)
    g_col   = GR if grand_realized >= 0 else RD
    gt_col  = GR if grand_today >= 0 else RD
    gu_col  = GR if grand_unrealized >= 0 else RD
    g_realized_cell   = _pad_ansi(f"{g_col}{BD}{grand_realized:>+,.0f} {currency_label}{W}", 15)
    g_today_cell      = _pad_ansi(f"{gt_col}{grand_today:>+,.0f} {currency_label}{W}" if today_rows else f"{DM}—{W}", 11)
    g_unrealized_cell = _pad_ansi(f"{gu_col}{grand_unrealized:>+,.0f} {currency_label}{W}", 13)
    L.append(
        f"  {BD}{total_label:<16}{W}  {DM}{grand_active:>6}{W}  {grand_closed:>7}  "
        f"{DM}{f'{grand_wins}W/{grand_losses}L':>7}{W}  {g_col}{g_wr:>5.1f}%{W}  {DM}{g_pf_s:>6}{W}  "
        f"{g_realized_cell}  {g_today_cell}  {g_unrealized_cell}"
    )
    L.append("")
    return L


def _abbr_eur(v: float) -> str:
    """Compact EUR: +25.3k / -1,941 / +0."""
    if v is None:
        return "—"
    if abs(v) >= 1000:
        return f"{v/1000:+,.1f}k"
    return f"{v:+,.0f}"


def _consolidated_breakdown(positions: list, live: dict, color: str = CY) -> list:
    """Replaces the 8 per-tier STRATEGY BREAKDOWN tables (2026-09-01, user:
    "1 instead of all separately but have all information smartly") with:
      1. TIER SCORECARD  -- one row per tier (pairs/active/closed/WR/PF/
         all-time/today/unrealized). The 8 old "TOTAL" lines, side by side.
      2. STRATEGY x TIER P&L GRID -- strategies (rows with any activity) x
         tiers (cols), cell = all-time realized P&L. Shows *where* each
         strategy makes/loses money -- the cross-tab the 8 separate tables
         couldn't. Data is per-strategy per-tier from
         pnl_tracker.get_strategy_summary(symbols=<tier set>), exactly what
         the old tables used.
    The full per-strategy ALL-pairs table + the strategy x SYMBOL detail
    (pnl_tracker.get_strategy_symbol_summary) are unchanged / still available.
    """
    import pnl_tracker
    _LBO_EXCL = {"london_breakout", "london_breakout_v2"}
    tiers = [
        ("High Vol",  "HIGH VOLUME",   HIGH_VOLUME_SYMBOLS,          frozenset()),
        ("Core Std",  "CORE STANDARD", CORE_STANDARD_SYMBOLS,        frozenset()),
        ("Scandi",    "SCANDI",        SCANDI_SYMBOLS,               _LBO_EXCL),
        ("Metals",    "METALS",        METALS_SYMBOLS,               _LBO_EXCL),
        ("Ex Asia",   "EXOTIC ASIA",   EXOTIC_ASIA_SYMBOLS,          _LBO_EXCL),
        ("Ex Euro",   "EXOTIC EUROPE", EXOTIC_EUROPE_SYMBOLS,        _LBO_EXCL),
        ("Ex Carry",  "EXOTIC CARRY",  EXOTIC_CARRY_SYMBOLS,         _LBO_EXCL),
        ("Ex LatAm",  "EXOTIC LATAM",  EXOTIC_LATAM_MIDEAST_SYMBOLS, _LBO_EXCL),
    ]
    today_str = date.today().isoformat()

    # per-tier: {strategy: realized_pnl}, plus tier totals
    tier_strat_pnl: dict = {}
    tier_totals: dict = {}
    for short, _full, syms, excl in tiers:
        try:
            srows = pnl_tracker.get_strategy_summary("forex", symbols=syms)
            trows = pnl_tracker.get_strategy_summary_since("forex", today_str, symbols=syms)
        except Exception:
            srows, trows = [], []
        by_strat = {r["strategy"]: r for r in srows if r["strategy"] not in excl}
        tier_strat_pnl[short] = {k: (v.get("total_pnl") or 0.0) for k, v in by_strat.items()}

        pos_scope = [p for p in positions if p["symbol"] in syms and p["strategy"] not in excl]
        unreal = 0.0
        for p in pos_scope:
            now_px = live.get(p["symbol"])
            if now_px and p["entry"] > 0:
                er = _eur_per_unit(p["symbol"][3:6], live)
                if er is not None:
                    raw = (now_px - p["entry"]) if p["direction"] == "Buy" else (p["entry"] - now_px)
                    unreal += raw * p["qty"] * er
        realized = sum(v.get("total_pnl") or 0.0 for v in by_strat.values())
        gp = sum(v.get("gross_profit", 0.0) or 0.0 for v in by_strat.values())
        gl = abs(sum(v.get("gross_loss", 0.0) or 0.0 for v in by_strat.values()))
        wins = sum(v.get("wins", 0) for v in by_strat.values())
        losses = sum(v.get("losses", 0) for v in by_strat.values())
        n_closed = sum(v.get("trades", 0) for v in by_strat.values())
        today_pnl = sum(r.get("total_pnl") or 0.0 for r in trows if r["strategy"] not in excl)
        tier_totals[short] = {
            "pairs": len(syms), "active": len(pos_scope), "closed": n_closed,
            "wins": wins, "losses": losses,
            "wr": (wins / n_closed * 100) if n_closed else None,
            "pf": (gp / gl) if gl > 0 else None,
            "realized": realized, "today": today_pnl, "unreal": unreal,
        }

    W_TOTAL = 108
    HR = f"  {DM}{'─' * W_TOTAL}{W}"
    L = _section_header("STRATEGY BREAKDOWN — BY TIER (all 8 tiers, one view)", color, W_TOTAL)

    # ── 1. TIER SCORECARD ──────────────────────────────────────────────────
    L.append(f"  {DM}{'Tier':<14}  {'Pairs':>5}  {'Active':>6}  {'Closed':>7}  {'W/L':>8}  "
             f"{'WR%':>6}  {'PF':>7}  {'All-Time':>13}  {'Today':>10}  {'Unreal':>10}{W}")
    L.append(HR)
    for short, full, _syms, _excl in tiers:
        t = tier_totals[short]
        rc = GR if t["realized"] >= 0 else RD
        uc = GR if t["unreal"] >= 0 else RD
        tc = GR if t["today"] >= 0 else RD
        wr_s = f"{t['wr']:.1f}%" if t["wr"] is not None else "—"
        pf_s = f"{t['pf']:.2f}" if t["pf"] is not None else "—"
        wl_s = f"{t['wins']}W/{t['losses']}L"
        today_s = _abbr_eur(t["today"]) if t["today"] else "—"
        L.append(
            f"  {BD}{short:<14}{W}  {DM}{t['pairs']:>5}  {t['active']:>6}  {t['closed']:>7}  "
            f"{wl_s:>8}{W}  {wr_s:>6}  {DM}{pf_s:>7}{W}  "
            f"{rc}{BD}{_abbr_eur(t['realized']):>13}{W}  "
            f"{tc}{today_s:>10}{W}  "
            f"{uc}{_abbr_eur(t['unreal']):>10}{W}"
        )
    L.append(HR)

    # ── 2. STRATEGY x TIER P&L GRID (all-time realized) ────────────────────
    active_strats = [s for s in STRAT_LABELS_ALL
                     if any(abs(tier_strat_pnl[sh].get(s, 0.0)) > 0.5 for sh, *_ in tiers)]
    if active_strats:
        L.append("")
        L.append(f"  {BD}Strategy × tier — all-time realised P&L (EUR){W}")
        L.append(f"  {DM}{'':<16}" + "".join(f"{sh:>11}" for sh, *_ in tiers) + f"{'  Σ':>11}{W}")
        for s in active_strats:
            row_vals = [tier_strat_pnl[sh].get(s, 0.0) for sh, *_ in tiers]
            cells = ""
            for v in row_vals:
                c = GR if v > 0.5 else (RD if v < -0.5 else DM)
                cells += f"{c}{_abbr_eur(v) if abs(v) > 0.5 else '·':>11}{W}"
            tot = sum(row_vals)
            tc = GR if tot > 0.5 else (RD if tot < -0.5 else DM)
            L.append(f"  {STRAT_COL.get(s, DM)}{BD}{STRAT_LABELS_ALL[s]:<16}{W}{cells}"
                     f"{tc}{BD}{_abbr_eur(tot):>11}{W}")
    L.append(HR)
    L.append(f"  {DM}·  = no closed trades in that tier   ·   "
             f"per-strategy overall = the ALL-PAIRS table below   ·   "
             f"per (strategy, pair) = pnl_tracker.get_strategy_symbol_summary('forex'){W}")
    L.append("")
    return L


def _positions_section(title: str, positions_subset: list, live: dict,
                        position_costs: dict, W_TOTAL: int, HR: str,
                        color: str = CY) -> tuple:
    """One OPEN POSITIONS table (grouped by strategy, per-row P&L, stop
    proximity) scoped to whatever pair subset the caller already filtered
    into `positions_subset`. Returns (lines, total_pnl, total_cost,
    total_costs_eur) so the caller can combine two calls' subtotals into
    one grand total line without re-deriving the numbers.

    Split out 2026-08-25 so CORE (34 pairs) and EXOTIC (83 pairs) can each
    get their own position list + subtotal, same split already applied to
    the STRATEGY BREAKDOWN tables — the live-vs-SIM-only decision needs to
    see which OPEN positions sit in which tier too, not just closed-trade
    stats. Boxed/colored header (same as _strategy_breakdown_table) added
    2026-08-25 for unambiguous visual separation between tiers."""
    L = _section_header(f"{title}  ({len(positions_subset)} active)", color, W_TOTAL)

    # 2026-09-01: table is grouped by PAIR (pair is the group header +
    # mini-subtotal). Column widths below are wide enough for METALS
    # magnitudes (gold ~3,500, XAUJPY ~700,000) and the long "(A/B)"
    # strategy labels; price/ATR precision scales with magnitude so the
    # columns actually line up (fixed 2026-09-01, user).
    _CW = {"strat": 19, "side": 5, "qty": 10, "px": 11, "pnl": 10, "pct": 9,
           "atr": 10, "days": 4}
    COL_HDR = (
        f"  {DM}"
        f"{'Strategy':<{_CW['strat']}}  {'Side':<{_CW['side']}}  "
        f"{'Qty':>{_CW['qty']}}  {'Entry':>{_CW['px']}}  {'Now':>{_CW['px']}}  "
        f"{'Stop':>{_CW['px']}}  {'P&L(EUR)':>{_CW['pnl']}}  {'%':>{_CW['pct']}}  "
        f"{'ATR':>{_CW['atr']}}  {'Days':>{_CW['days'] + 1}}  {'Stop Risk'}{W}"
    )
    L.append(COL_HDR)
    L.append(HR)

    def _pxfmt(v: float, dec: int) -> str:
        return f"{v:,.{dec}f}" if v is not None else "—"

    def _px_dec(v: float) -> int:
        av = abs(v or 0)
        return 0 if av >= 100_000 else 1 if av >= 1_000 else 2 if av >= 10 else 5

    total_pnl   = 0.0
    total_cost  = 0.0
    total_costs_eur = 0.0   # spread + accrued swap/financing, NOT included in total_pnl

    if positions_subset:
        strat_order = ["ema", "advanced_ema", "rsi", "advanced_rsi_master",
                       "donchian", "donchian_quality", "bb", "advanced_bb_master",
                       "pullback", "advanced_pullback_master",
                       "gap", "gap_weekend", "supertrend", "zscore",
                       "ml", "advanced_ml", "cnn_lstm", "advanced_cnn_lstm_master",
                       "london_breakout", "london_breakout_v2"]
        _sidx = {s: i for i, s in enumerate(strat_order)}

        # 2026-09-01 (user): group by PAIR, strategies nested under each, with
        # a per-pair mini-subtotal. Surfaces multi-strategy concentration on
        # one instrument (e.g. XAUEUR held by rsi + gap + advanced_ml) that
        # the old strategy-grouping scattered across three places.
        by_pair: dict = {}
        for p in positions_subset:
            by_pair.setdefault(p["symbol"], []).append(p)

        def _row_metrics(p):
            """(pnl_eur|None, cost_eur|None, dist_pct|None) for one position."""
            sym, ep, qty = p["symbol"], p["entry"], p["qty"]
            now_px = live.get(sym)
            is_long = p["direction"] == "Buy"
            quote_ccy = sym[3:6] if len(sym) >= 6 else ""
            eur_rate = _eur_per_unit(quote_ccy, live) if now_px and ep > 0 else None
            pnl_eur = cost_eur = None
            if now_px and ep > 0 and eur_rate is not None:
                raw = (now_px - ep) if is_long else (ep - now_px)
                pnl_eur = raw * qty * eur_rate
                cost_eur = ep * qty * eur_rate
            dist = (abs(now_px - p["stop"]) / now_px * 100
                    if (p["stop"] > 0 and now_px) else None)
            return pnl_eur, cost_eur, dist

        # order the pairs: most-concentrated first, then worst P&L, then name
        pair_rank = []
        for sym, grp in by_pair.items():
            pnls = [x[0] for x in (_row_metrics(p) for p in grp) if x[0] is not None]
            pair_rank.append((
                -len({p["strategy"] for p in grp}),         # more strategies first
                sum(pnls) if pnls else 0.0,                  # then worse P&L first
                sym,
            ))
        ordered_syms = [s for _, _, s in sorted(pair_rank)]

        for gi, sym in enumerate(ordered_syms):
            grp = sorted(by_pair[sym], key=lambda p: _sidx.get(p["strategy"], 99))
            if gi:
                L.append(f"  {DM}{'·'*W_TOTAL}{W}")

            m = [_row_metrics(p) for p in grp]
            pair_pnl   = sum(x[0] for x in m if x[0] is not None)
            pair_units = sum(p["qty"] for p in grp)
            n_strat    = len({p["strategy"] for p in grp})
            closest    = min((x[2] for x in m if x[2] is not None), default=None)
            _priced    = any(x[0] is not None for x in m)
            hc = GR if pair_pnl >= 0 else RD
            pnl_txt = (f"{hc}{pair_pnl:>+,.0f} EUR{W}" if _priced else f"{DM}—{W}")
            close_txt = (f"  ·  closest stop {closest:.2f}%" if closest is not None else "")
            L.append(
                f"  {color}{BD}{sym:<7}{W}  {DM}"
                f"{n_strat} strateg{'y' if n_strat == 1 else 'ies'}  ·  "
                f"{pair_units:,} unit{'' if pair_units == 1 else 's'}  ·  "
                f"{W}{pnl_txt}{DM}{close_txt}{W}"
            )

            for p in grp:
                strat    = p["strategy"]
                is_long  = p["direction"] == "Buy"
                now_px   = live.get(sym)
                ep       = p["entry"]
                stop_px  = p["stop"]
                qty      = p["qty"]
                atr      = p["atr"]
                sc       = STRAT_COL.get(strat, DM)
                label    = STRAT_LABELS_ALL.get(strat, strat)[:_CW["strat"]]
                try:
                    held = (date.today() - date.fromisoformat(p["entry_date"])).days
                except Exception:
                    held = 0

                side_tag = f"{GR}{BD}{'LONG':<{_CW['side']}}{W}" if is_long else f"{RD}{BD}{'SHORT':<{_CW['side']}}{W}"

                dec = _px_dec(ep)
                quote_ccy = sym[3:6] if len(sym) >= 6 else ""
                eur_rate  = _eur_per_unit(quote_ccy, live) if now_px and ep > 0 else None
                if now_px and ep > 0 and eur_rate is not None:
                    raw_pnl  = (now_px - ep) if is_long else (ep - now_px)
                    pnl_eur  = raw_pnl * qty * eur_rate
                    pnl_pct  = raw_pnl / ep * 100
                    total_pnl  += pnl_eur
                    total_cost += ep * qty * eur_rate
                    pos_cost = position_costs.get((p.get("uic"), round(qty)))
                    if pos_cost is not None:
                        total_costs_eur += pos_cost
                    pc   = GR if pnl_eur >= 0 else RD
                    pnl_s = f"{pc}{pnl_eur:>+{_CW['pnl']},.0f}{W}"
                    pct_s = f"{pc}{pnl_pct:>+{_CW['pct'] - 1}.2f}%{W}"
                    now_s = f"{BD}{_pxfmt(now_px, dec):>{_CW['px']}}{W}"
                else:
                    pnl_s = f"{DM}{'—':>{_CW['pnl']}}{W}"
                    pct_s = f"{DM}{'—':>{_CW['pct']}}{W}"
                    now_s = f"{DM}{'—':>{_CW['px']}}{W}"

                near = (stop_px > 0 and now_px and
                        ((is_long and now_px < stop_px * 1.005) or
                         (not is_long and now_px > stop_px * 0.995)))
                stp_col = f"{RD}{BD}" if near else DM
                stop_s  = _pxfmt(stop_px, dec) if stop_px > 0 else "—"

                if stop_px > 0 and now_px:
                    dist_pct = abs(now_px - stop_px) / now_px * 100
                    dist_s   = f"{DM}{dist_pct:.2f}% from stop{W}"
                else:
                    dist_s = f"{DM}—{W}"

                L.append(
                    f"  {sc}{BD}{label:<{_CW['strat']}}{W}  {side_tag}  "
                    f"{DM}{qty:>{_CW['qty']},}{W}  {DM}{_pxfmt(ep, dec):>{_CW['px']}}{W}  {now_s}  "
                    f"{stp_col}{stop_s:>{_CW['px']}}{W}  {pnl_s}  {pct_s}  "
                    f"{DM}{_pxfmt(atr, _px_dec(atr)):>{_CW['atr']}}{W}  "
                    f"{DM}{held:>{_CW['days']}}d{W}  {dist_s}"
                )

        L.append(HR)
        tc   = GR if total_pnl >= 0 else RD
        tpct = (total_pnl / total_cost * 100) if total_cost > 0 else 0
        L.append(
            f"  {BD}SUBTOTAL{W}  "
            f"{DM}{len(positions_subset)} positions  |  Cost: €{total_cost:>,.0f}  |  "
            f"Unrealized P&L (gross): {W}"
            f"{tc}{BD}{total_pnl:>+,.0f} EUR  ({tpct:>+.4f}%){W}"
        )
        net_pnl = total_pnl + total_costs_eur
        nc = GR if net_pnl >= 0 else RD
        L.append(
            f"  {DM}         Spread + accrued swap/financing since entry: "
            f"{RD}{total_costs_eur:>+,.0f} EUR{DM}  →  "
            f"Net of costs: {W}{nc}{BD}{net_pnl:>+,.0f} EUR{W}"
        )
    else:
        L.append(f"  {DM}No open positions in this tier.{W}")
    L.append(HR)
    L.append("")
    return L, total_pnl, total_cost, total_costs_eur


def _render(once: bool = False, interval: int = REFRESH_SECONDS) -> str:
    now_ts    = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
    positions = _read_positions()
    token     = price_service.load_token()

    # Fetch live prices only for pairs we actually hold — the standalone
    # full-universe rates strip was removed, so there's no need to price
    # every FX_INSTRUMENTS pair on every refresh. Also pull in whatever
    # EUR{ccy}/USD{ccy} pairs are needed to convert each held pair's quote
    # currency to EUR (see _eur_per_unit) -- one batched call, same live
    # Saxo source as the position prices themselves.
    instruments = [{"symbol": p["symbol"], "uic": p["uic"], "asset_type": p["asset_type"]}
                   for p in positions if p.get("uic")]
    quote_ccys  = {p["symbol"][3:6] for p in positions if len(p.get("symbol", "")) >= 6}
    instruments += _fx_conversion_instruments(quote_ccys)

    live, price_src = price_service.fetch_prices(instruments, token=token)
    position_costs   = _fetch_position_costs(token)

    # 2026-08-28 fix: total/tier pair counts below are now computed from
    # the real universe.py sets rather than hardcoded literals -- the same
    # class of staleness bug found and fixed in futures_dashboard.py the
    # same day (a hardcoded "149"/"34 core" etc. silently drifting out of
    # sync the next time the universe changes).
    _total_pairs = len(_UNIVERSE_PAIRS)
    W_TOTAL = 139  # widened 2026-08-26 for the positions/pairs header segment
    HR      = f"  {DM}{'─' * W_TOTAL}{W}"

    L = []

    # ── Header ────────────────────────────────────────────────────
    L.append(f"  {BD}{CY}╔{'═'*W_TOTAL}╗{W}")
    src_tag = "SAXO LIVE" if price_src == "saxo" else "n/a (token expired)"
    pairs_trading = len({p["symbol"] for p in positions})
    # 2026-08-29: show BOTH the position count and the distinct-pair count.
    # The run-summary email's "Positions" metric counts open positions
    # (one per strategy:symbol key, so the same pair held by 2 strategies
    # is 2), while this line used to show only distinct pairs -- the two
    # numbers looked contradictory (e.g. email "119" vs dashboard "81/184").
    # Both are now labelled explicitly and shown side by side here and in
    # the email so they reconcile at a glance.
    L.append(f"  {BD}{CY}║{'  FOREX QUANT DASHBOARD':^{W_TOTAL}}║{W}")
    # 2026-08-29: was hardcoded "11 Strategies" -- same class of drift bug
    # this file's own 2026-08-25 fix note warns about elsewhere (a literal
    # number silently going stale as strategies get added). Computed from
    # STRAT_LABELS_ALL so it can never drift again.
    L.append(f"  {BD}{CY}║{f'  {len(STRAT_LABELS_ALL)} Strategies  |  {_total_pairs} FX Pairs  |  {len(positions)} positions in {pairs_trading}/{_total_pairs} pairs  |  Prices: {src_tag}  |  {now_ts}':^{W_TOTAL}}║{W}")
    # 2026-08-28: 2nd row added -- uses the EXACT Forex Grouping tier names
    # (not a coarser "core"/"exotic" paraphrase) per explicit user request,
    # so this line can be copy-referenced directly when configuring ATOS
    # LIVE later. Split onto its own row (rather than appended to row 1)
    # because the combined string overflows W_TOTAL=139 and breaks box
    # alignment; each row individually fits.
    L.append(f"  {BD}{CY}║{f'  Forex Grouping:  {len(HIGH_VOLUME_SYMBOLS)} High Volume + {len(CORE_STANDARD_SYMBOLS)} Core Standard + {len(SCANDI_SYMBOLS)} Scandi + {len(METALS_SYMBOLS)} Metals + {len(EXOTIC_SYMBOLS)} Exotic':^{W_TOTAL}}║{W}")
    L.append(f"  {BD}{CY}╚{'═'*W_TOTAL}╝{W}")
    L.append("")

    OR  = "\033[38;5;208m"
    LV  = "\033[38;5;147m"
    LM  = "\033[38;5;119m"
    # ── Strategy legend ───────────────────────────────────────────
    L.append(f"  {BD}STRATEGIES{W}   "
             f"{CY}{BD}■ EMA{W}  trend   "
             f"{MG}{BD}■ RSI{W}  mean-rev   "
             f"{GR}{BD}■ Donchian(30){W}  breakout   "
             f"{YL}{BD}■ BB(20,2){W}  fade   "
             f"{BL}{BD}■ Pullback{W}  EMA20-in-EMA50   "
             f"{WH}{BD}■ Gap Fill{W}  ~80% WR   "
             f"{OR}{BD}■ SuperTrend{W}  trend   "
             f"{LV}{BD}■ Z-Score{W}  mean-rev   "
             f"{LM}{BD}■ ML{W}  ML signals   "
             f"\033[38;5;135m{BD}■ CNN-LSTM{W}  deep learning   "
             f"\033[38;5;214m{BD}■ LBO{W}  day trade   "
             # 2026-08-29: 3 new SIM-only A/B-test strategies
             f"\033[38;5;80m{BD}■ Gap Wknd{W}  A/B   "
             f"\033[38;5;120m{BD}■ Donchian Qual{W}  A/B   "
             f"\033[38;5;220m{BD}■ LBO V2{W}  A/B   "
             # 2026-08-30: 6 user-supplied SIM-only A/B "advanced_*" strategies
             f"{STRAT_COL['advanced_ema']}{BD}■ EMA Adv{W}  A/B   "
             f"{STRAT_COL['advanced_rsi_master']}{BD}■ RSI2{W}  A/B   "
             f"{STRAT_COL['advanced_bb_master']}{BD}■ BB Mstr{W}  A/B   "
             f"{STRAT_COL['advanced_pullback_master']}{BD}■ Pullback Mstr{W}  A/B   "
             f"{STRAT_COL['advanced_ml']}{BD}■ ML Adv{W}  A/B   "
             f"{STRAT_COL['advanced_cnn_lstm_master']}{BD}■ CNN-LSTM Mstr{W}  A/B")
    L.append(f"  {DM}Scheduler: every 30min 06:00-03:00 PKT (scan)  |  14:00 PKT (exit check)  |  "
             f"Mon 03:00 PKT weekly + session gap windows (gap fill)  |  "
             f"{_total_pairs} pairs: {len(HIGH_VOLUME_SYMBOLS)} high volume + {len(CORE_STANDARD_SYMBOLS)} core standard + "
             f"{len(SCANDI_SYMBOLS)} scandi + {len(METALS_SYMBOLS)} metals + "
             f"{len(EXOTIC_SYMBOLS)} exotic ({len(EXOTIC_ASIA_SYMBOLS)} asia + {len(EXOTIC_EUROPE_SYMBOLS)} europe + "
             f"{len(EXOTIC_CARRY_SYMBOLS)} carry + {len(EXOTIC_LATAM_MIDEAST_SYMBOLS)} latam/mideast, SIM-only)  |  "
             f"Max slots {_total_pairs} (28 for day-trade LBO, 4 for Donchian Qual / LBO V2 A/B tests){W}")
    L.append(HR)
    L.append("")

    # ── Tier colors — used for every HIGH VOLUME/CORE STANDARD/SCANDI/
    # EXOTIC/ALL section box below, so the same color always means the same
    # tier no matter which table it's attached to (2026-08-25, explicit
    # "clear separation" request; SCANDI color added same day when that
    # tier was introduced). CORE_COLOR retired 2026-08-28 along with the
    # standalone CORE section (see below) -- GR reused below for EXOTIC ASIA.
    SCANDI_COLOR, ALLTIER_COLOR = MG, CY
    HIGH_VOLUME_COLOR = WH
    # 2026-08-28: was BL (standard blue) -- user-reported "blue on blue"
    # readability problem (terminal background/theme made it near-invisible).
    # Replaced with a 256-color warm gold/tan, unclaimed by any other
    # tier/strategy color in this file (confirmed via grep before picking it).
    CORE_STANDARD_COLOR = "\033[38;5;178m"
    # EXOTIC regional sub-colors (2026-08-28) -- standard 8-color ANSI is
    # already fully spoken for by the tiers above, so these use 256-color
    # codes (same technique the strategy legend already uses for CNN-LSTM/
    # LBO) to stay visually distinct from every other tier/region color.
    EXOTIC_ASIA_COLOR, EXOTIC_EUROPE_COLOR = GR, "\033[38;5;208m"
    EXOTIC_CARRY_COLOR, EXOTIC_LATAM_MIDEAST_COLOR = "\033[38;5;201m", "\033[38;5;51m"

    high_volume_positions = [p for p in positions if p["symbol"] in HIGH_VOLUME_SYMBOLS]
    core_standard_positions = [p for p in positions if p["symbol"] in CORE_STANDARD_SYMBOLS]
    scandi_positions    = [p for p in positions if p["symbol"] in SCANDI_SYMBOLS]
    exotic_asia_positions          = [p for p in positions if p["symbol"] in EXOTIC_ASIA_SYMBOLS]
    exotic_europe_positions        = [p for p in positions if p["symbol"] in EXOTIC_EUROPE_SYMBOLS]
    exotic_carry_positions         = [p for p in positions if p["symbol"] in EXOTIC_CARRY_SYMBOLS]
    exotic_latam_mideast_positions = [p for p in positions if p["symbol"] in EXOTIC_LATAM_MIDEAST_SYMBOLS]
    open_count          = len(positions)

    # ── Positions tables — HIGH VOLUME (17) + CORE STANDARD (17) first
    # (2026-08-28: the standalone CORE (34) section was removed -- explicit
    # user instruction, since HIGH VOLUME + CORE STANDARD already exactly
    # partition it, 17+17=34, and showing all three was redundant), then
    # SCANDI (32), then EXOTIC (83). Boxed/colored headers (_section_header)
    # give unambiguous visual separation between tiers.
    #
    # HIGH VOLUME — added 2026-08-26, a CURATED SUBSET of what used to be
    # shown as CORE (7 G7 majors + 10 major crosses) -- exists to let
    # strategies be judged specifically on the highest-liquidity pairs,
    # separate from CORE STANDARD's remaining mix (which also includes the
    # Scandi-adjacent EUR/USD-vs-NOK/SEK/DKK/MXN pairs). Its P&L/cost now
    # feeds the TOTAL sum directly (previously excluded there, back when
    # CORE's own sum already counted it).
    high_volume_lines, high_volume_pnl, high_volume_cost, high_volume_costs_eur = _positions_section(
        "OPEN POSITIONS — HIGH VOLUME (17 pairs, majors + liquid crosses)",
        high_volume_positions, live, position_costs, W_TOTAL, HR, color=HIGH_VOLUME_COLOR)
    L.extend(high_volume_lines)

    # CORE STANDARD — added 2026-08-28, HIGH VOLUME's exact complement
    # (an exact partition of CORE_SYMBOLS, not a new tier) -- explicit user
    # request for a matching "high volume vs the rest" split so each
    # half's own performance is visible on its own. Its P&L/cost also now
    # feeds the TOTAL sum directly, same reasoning as HIGH VOLUME above.
    # 2026-08-28: CORE_SYMBOLS grew from 34 to 49 (currencypairs cross-
    # check added 15 new CORE pairs, all joining CORE_STANDARD, none
    # joining the hand-curated HIGH_VOLUME_SYMBOLS) -- CORE_STANDARD is
    # now 32 pairs, not the original 17, computed live below so this
    # can't go stale again.
    core_standard_lines, core_standard_pnl, core_standard_cost, core_standard_costs_eur = _positions_section(
        f"OPEN POSITIONS — CORE STANDARD ({len(CORE_STANDARD_SYMBOLS)} pairs, the other half of former CORE)",
        core_standard_positions, live, position_costs, W_TOTAL, HR, color=CORE_STANDARD_COLOR)
    L.extend(core_standard_lines)

    scandi_lines, scandi_pnl, scandi_cost, scandi_costs_eur = _positions_section(
        f"OPEN POSITIONS — SCANDI ({len(SCANDI_SYMBOLS)} pairs, SIM-only, NOK/SEK/DKK crosses)",
        scandi_positions, live, position_costs, W_TOTAL, HR, color=SCANDI_COLOR)
    L.extend(scandi_lines)

    # METALS — added 2026-08-28, explicit user request ("get all supported
    # currency pairs from saxo... add in the relevant groups"). 17
    # precious-metal spot pairs, its own tier -- see
    # forex/universe.py's METALS_SYMBOLS comment for why it isn't folded
    # into CORE/SCANDI/EXOTIC's fiat-currency system.
    METALS_COLOR = "\033[38;5;220m"
    metals_positions = [p for p in positions if p["symbol"] in METALS_SYMBOLS]
    metals_lines, metals_pnl, metals_cost, metals_costs_eur = _positions_section(
        f"OPEN POSITIONS — METALS ({len(METALS_SYMBOLS)} pairs, Gold/Silver/Platinum spot)",
        metals_positions, live, position_costs, W_TOTAL, HR, color=METALS_COLOR)
    L.extend(metals_lines)

    # EXOTIC regional split — added 2026-08-28 alongside the blended
    # 83-pair EXOTIC section, then that blended section was REMOVED the
    # same day (explicit user instruction: "Remove Exotic pairs 83 from
    # dashboard as we have now separately EM ASIA/EUROPE/CARRY/
    # LATAM_MIDEAST") -- same reasoning as CORE's earlier removal: these 4
    # groups already exactly partition EXOTIC_SYMBOLS (30+25+17+11=83), so
    # showing the blended total alongside them was redundant. Their P&L/
    # cost now feeds the grand TOTAL sum directly (previously excluded
    # there, back when the blended EXOTIC section's own sum already
    # counted it).
    exotic_asia_lines, exotic_asia_pnl, exotic_asia_cost, exotic_asia_costs_eur = _positions_section(
        "OPEN POSITIONS — EXOTIC ASIA (30 pairs, CNH/HKD/SGD/THB)",
        exotic_asia_positions, live, position_costs, W_TOTAL, HR, color=EXOTIC_ASIA_COLOR)
    L.extend(exotic_asia_lines)

    exotic_europe_lines, exotic_europe_pnl, exotic_europe_cost, exotic_europe_costs_eur = _positions_section(
        "OPEN POSITIONS — EXOTIC EUROPE (25 pairs, CZK/HUF/PLN/RON)",
        exotic_europe_positions, live, position_costs, W_TOTAL, HR, color=EXOTIC_EUROPE_COLOR)
    L.extend(exotic_europe_lines)

    exotic_carry_lines, exotic_carry_pnl, exotic_carry_cost, exotic_carry_costs_eur = _positions_section(
        "OPEN POSITIONS — EXOTIC HIGH-YIELD/CARRY (17 pairs, TRY/ZAR)",
        exotic_carry_positions, live, position_costs, W_TOTAL, HR, color=EXOTIC_CARRY_COLOR)
    L.extend(exotic_carry_lines)

    exotic_lm_lines, exotic_lm_pnl, exotic_lm_cost, exotic_lm_costs_eur = _positions_section(
        "OPEN POSITIONS — EXOTIC LATAM/MIDEAST (11 pairs, MXN/ILS/AED)",
        exotic_latam_mideast_positions, live, position_costs, W_TOTAL, HR, color=EXOTIC_LATAM_MIDEAST_COLOR)
    L.extend(exotic_lm_lines)

    total_pnl       = (high_volume_pnl + core_standard_pnl + scandi_pnl + metals_pnl
                       + exotic_asia_pnl + exotic_europe_pnl + exotic_carry_pnl + exotic_lm_pnl)
    total_cost      = (high_volume_cost + core_standard_cost + scandi_cost + metals_cost
                       + exotic_asia_cost + exotic_europe_cost + exotic_carry_cost + exotic_lm_cost)
    total_costs_eur = (high_volume_costs_eur + core_standard_costs_eur + scandi_costs_eur + metals_costs_eur
                       + exotic_asia_costs_eur + exotic_europe_costs_eur + exotic_carry_costs_eur + exotic_lm_costs_eur)
    tc   = GR if total_pnl >= 0 else RD
    tpct = (total_pnl / total_cost * 100) if total_cost > 0 else 0
    net_pnl = total_pnl + total_costs_eur
    nc = GR if net_pnl >= 0 else RD
    L.append(
        f"  {BD}TOTAL (ALL PAIRS){W}  "
        f"{DM}{open_count} positions  |  Cost: €{total_cost:>,.0f}  |  "
        f"Unrealized P&L (gross): {W}"
        f"{tc}{BD}{total_pnl:>+,.0f} EUR  ({tpct:>+.4f}%){W}  "
        f"{DM}|  Net of costs: {W}{nc}{BD}{net_pnl:>+,.0f} EUR{W}"
    )
    L.append(HR)
    L.append("")

    # ── Strategy breakdown — the dashboard's main analytical view ──
    # Sorted by realized P&L (best to worst), one row per strategy, every
    # number a strategy's track record actually needs: win rate, profit
    # factor, realized vs unrealized. No per-pair/currency table here by
    # design (2026-08-24, explicit request) — that's a different question
    # ("which pairs work") from this one ("which strategies work"); see
    # pnl_dashboard.py or the Strategy Overlap Tracker artifact for that.
    #
    # 2026-08-25: split into CORE / SCANDI / EXOTIC / ALL (blended
    # reference, universe_size computed live -- see the 2026-08-28 fix
    # note above) so the live-vs-SIM-only universe decision can be made
    # per strategy, not just from one blended all-pairs number. SCANDI
    # added same day as its own tier so its SIM track record (32 new
    # NOK/SEK/DKK crosses) can be judged on its own before folding any of
    # it into CORE or writing it off, exactly the same way EXOTIC's track
    # record already gets judged separately.
    #
    # 2026-08-28: the standalone CORE (34) breakdown was removed here too
    # (explicit user instruction) -- HIGH VOLUME + CORE STANDARD below
    # already exactly partition it (17+17=34), so showing all three was
    # redundant. HIGH VOLUME now leads (same reason CORE used to: the
    # actionable half of the live-vs-SIM-only decision this whole split
    # exists for), same LBO inclusion as the old CORE section (LBO trades
    # its own 28-pair majors/crosses subset, genuinely overlaps both CORE
    # halves, unlike SCANDI/EXOTIC below where LBO structurally never
    # trades).
    # 2026-09-01 (user: "1 instead of all separately but have all info
    # smartly"): the 8 near-identical per-tier STRATEGY BREAKDOWN tables
    # (HIGH VOLUME / CORE STANDARD / SCANDI / METALS / EXOTIC ASIA|EUROPE|
    # CARRY|LATAM), each ~20 mostly-zero rows, collapsed into a TIER
    # SCORECARD (8 rows) + a strategy x tier all-time-P&L grid. Same
    # underlying data (pnl_tracker.get_strategy_summary per tier set).
    L.extend(_consolidated_breakdown(positions, live, color=HIGH_VOLUME_COLOR))

    # The one full per-strategy table stays -- master reference across the
    # whole 184-pair universe.
    L.extend(_strategy_breakdown_table(
        f"STRATEGY BREAKDOWN — ALL {_total_pairs} PAIRS (blended reference)",
        positions, live, symbols=None, universe_size=_total_pairs,
        color=ALLTIER_COLOR, total_label="ALL TOTAL"))

    # ── Footer ────────────────────────────────────────────────────
    L.append(f"  {DM}Per-pair/currency breakdown removed from this view by design (2026-08-24) — "
             f"still available via pnl_tracker.get_pair_summary('forex') if needed.{W}")
    if not once:
        L.append(f"  {DM}Refreshes every {interval}s  |  Ctrl+C to exit  |  "
                 f"Run: python forex/runner.py --live  to force update{W}")
    L.append("")
    return "\n".join(L)


# Need datetime imported in module scope for _render
from datetime import datetime


def main():
    fast = "--fast" in sys.argv
    once = "--once" in sys.argv
    interval = 10 if fast else REFRESH_SECONDS

    if once:
        sys.stdout.write(_render(once=True, interval=interval))
        sys.stdout.flush()
        return

    while True:
        try:
            out = _render(interval=interval)
            _clear_console()
            sys.stdout.write(out)
            sys.stdout.flush()
            time.sleep(interval)
        except KeyboardInterrupt:
            sys.stdout.write(f"\n{W}")
            break


if __name__ == "__main__":
    main()
