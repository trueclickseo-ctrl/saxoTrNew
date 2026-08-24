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

BASE_DIR            = os.path.dirname(os.path.abspath(__file__))
FOREX_STATE_PATH    = os.path.join(BASE_DIR, "data", "forex_state.json")
FOREX_LOG_PATH      = os.path.join(BASE_DIR, "data", "forex_scheduler.log")

sys.path.insert(0, BASE_DIR)
import price_service
from forex.universe import PAIRS as _UNIVERSE_PAIRS

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
    """Clear the Windows console via the Console API (works in all terminal hosts)."""
    try:
        import ctypes, struct
        k32  = ctypes.windll.kernel32
        h    = k32.GetStdHandle(-11)
        buf  = ctypes.create_string_buffer(22)
        k32.GetConsoleScreenBufferInfo(h, buf)
        _, _, _, _, _, left, top, right, bottom, _, _ = struct.unpack("hhhhHhhhhhh", buf.raw)
        cols = right - left + 1
        rows = bottom - top + 1
        size = cols * rows
        done = ctypes.c_ulong(0)
        k32.FillConsoleOutputCharacterW(h, 32, size, 0, ctypes.byref(done))
        k32.FillConsoleOutputAttribute(h, 7,  size, 0, ctypes.byref(done))
        k32.SetConsoleCursorPosition(h, 0)   # COORD {0,0}
    except Exception:
        sys.stdout.write("\033[2J\033[H")
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


def _last_log_lines(n: int = 8) -> list:
    if not os.path.exists(FOREX_LOG_PATH):
        return []
    try:
        with open(FOREX_LOG_PATH, encoding="utf-8", errors="replace") as f:
            lines = [l.rstrip() for l in f.readlines() if l.strip()]
        return lines[-n:]
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

    W_TOTAL = 108
    HR      = f"  {DM}{'─' * W_TOTAL}{W}"

    L = []

    # ── Header ────────────────────────────────────────────────────
    L.append(f"  {BD}{CY}╔{'═'*W_TOTAL}╗{W}")
    src_tag = "SAXO LIVE" if price_src == "saxo" else "n/a (token expired)"
    L.append(f"  {BD}{CY}║{'  FOREX QUANT DASHBOARD':^{W_TOTAL}}║{W}")
    L.append(f"  {BD}{CY}║{f'  11 Strategies  |  117 FX Pairs (34 core + 83 exotic)  |  Prices: {src_tag}  |  {now_ts}':^{W_TOTAL}}║{W}")
    L.append(f"  {BD}{CY}╚{'═'*W_TOTAL}╝{W}")
    L.append("")

    OR  = "\033[38;5;208m"
    LV  = "\033[38;5;147m"
    LM  = "\033[38;5;119m"
    # ── Strategy legend ───────────────────────────────────────────
    L.append(f"  {BD}STRATEGIES{W}   "
             f"{CY}{BD}■ EMA{W}  trend   "
             f"{MG}{BD}■ RSI(2){W}  pullback   "
             f"{GR}{BD}■ Donchian(30){W}  breakout   "
             f"{YL}{BD}■ BB(20,2){W}  fade   "
             f"{BL}{BD}■ Pullback{W}  ~70% WR   "
             f"{WH}{BD}■ Gap Fill{W}  ~80% WR   "
             f"{OR}{BD}■ SuperTrend{W}  trend   "
             f"{LV}{BD}■ Z-Score{W}  mean-rev   "
             f"{LM}{BD}■ ML{W}  ML signals   "
             f"\033[38;5;135m{BD}■ CNN-LSTM{W}  deep learning   "
             f"\033[38;5;214m{BD}■ LBO{W}  day trade")
    L.append(f"  {DM}Scheduler: every 30min 06:00-22:00 PKT (scan)  |  14:00 PKT (exit check)  |  "
             f"Mon 03:00 PKT weekly + session gap windows (gap fill)  |  "
             f"117 pairs: 34 core + 83 exotic (SIM-only)  |  Max slots 117 (28 for day-trade LBO){W}")
    L.append(HR)
    L.append("")

    # ── Positions table ───────────────────────────────────────────
    open_count = len(positions)
    L.append(f"  {BD}OPEN POSITIONS{W}  {DM}({open_count} active){W}")
    L.append("")

    # Column header
    COL_HDR = (
        f"  {DM}"
        f"{'Strategy':<10}  {'Pair':<7}  {'Side':<6}  "
        f"{'Qty':>12}  {'Entry':>10}  {'Now':>10}  "
        f"{'Stop':>10}  {'P&L (EUR)':>12}  {'%':>9}  "
        f"{'ATR':>8}  {'Days':>5}  {'Stop Risk':>12}{W}"
    )
    L.append(COL_HDR)
    L.append(HR)

    total_pnl   = 0.0
    total_cost  = 0.0
    total_costs_eur = 0.0   # spread + accrued swap/financing, NOT included in total_pnl
    near_stop_count = 0
    near_stop_list  = []

    if positions:
        # Group by strategy for cleaner display
        strat_order = ["ema", "rsi", "donchian", "bb", "pullback", "gap",
                       "supertrend", "zscore", "ml", "cnn_lstm", "london_breakout"]
        grouped: dict = {}
        for p in positions:
            grouped.setdefault(p["strategy"], []).append(p)

        first_group = True
        for strat in strat_order:
            grp = grouped.get(strat, [])
            if not grp:
                continue
            sc = STRAT_COL.get(strat, DM)
            if not first_group:
                L.append(f"  {DM}{'·'*W_TOTAL}{W}")
            first_group = False

            for p in grp:
                sym      = p["symbol"]
                is_long  = p["direction"] == "Buy"
                now_px   = live.get(sym)
                ep       = p["entry"]
                stop_px  = p["stop"]
                qty      = p["qty"]
                atr      = p["atr"]
                try:
                    held = (date.today() - date.fromisoformat(p["entry_date"])).days
                except Exception:
                    held = 0

                side_tag = f"{GR}{BD}LONG {W}" if is_long else f"{RD}{BD}SHORT{W}"

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
                    pnl_s = f"{pc}{pnl_eur:>+,.0f}{W}"
                    pct_s = f"{pc}{pnl_pct:>+.4f}%{W}"
                    now_s = f"{now_px:.5f}"
                else:
                    pnl_s = f"{DM}{'—':>12}{W}"
                    pct_s = f"{DM}{'—':>9}{W}"
                    now_s = f"{'—':>10}"

                # Stop proximity warning
                near = (stop_px > 0 and now_px and
                        ((is_long and now_px < stop_px * 1.005) or
                         (not is_long and now_px > stop_px * 0.995)))
                if near:
                    near_stop_count += 1
                    side_label = "LONG" if is_long else "SHORT"
                    dist_warn  = abs(now_px - stop_px) / now_px * 100 if now_px else 0
                    near_stop_list.append(
                        f"{strat.upper()} {sym} {side_label} — {dist_warn:.2f}% from stop ({stop_px:.5f})"
                    )
                stp_col = f"{RD}{BD}" if near else DM
                stop_s  = f"{stop_px:.5f}"

                # Distance from stop (in % of current price)
                if stop_px > 0 and now_px:
                    dist_pct = abs(now_px - stop_px) / now_px * 100
                    dist_s   = f"{DM}{dist_pct:.2f}% from stop{W}"
                else:
                    dist_s = f"{DM}{'—':>12}{W}"

                L.append(
                    f"  {sc}{BD}{strat:<10}{W}  {BD}{sym:<7}{W}  {side_tag}  "
                    f"{DM}{qty:>12,}{W}  {DM}{ep:>10.5f}{W}  {BD}{now_s:>10}{W}  "
                    f"{stp_col}{stop_s:>10}{W}  {_pad_ansi(pnl_s, 17)}  "
                    f"{_pad_ansi(pct_s, 14)}  {DM}{atr:>8.5f}{W}  "
                    f"{DM}{held:>5}d{W}  {dist_s}"
                )

        L.append(HR)

        # ── Totals row ────────────────────────────────────────────
        tc   = GR if total_pnl >= 0 else RD
        tpct = (total_pnl / total_cost * 100) if total_cost > 0 else 0
        L.append(
            f"  {BD}TOTAL{W}  "
            f"{DM}{open_count} positions  |  Cost: €{total_cost:>,.0f}  |  "
            f"Unrealized P&L (gross): {W}"
            f"{tc}{BD}{total_pnl:>+,.0f} EUR  ({tpct:>+.4f}%){W}"
        )
        net_pnl = total_pnl + total_costs_eur   # total_costs_eur is already negative-signed
        nc = GR if net_pnl >= 0 else RD
        L.append(
            f"  {DM}         Spread + accrued swap/financing since entry: "
            f"{RD}{total_costs_eur:>+,.0f} EUR{DM}  →  "
            f"Net of costs: {W}{nc}{BD}{net_pnl:>+,.0f} EUR{W}"
        )

        # Near-stop warning with details
        if near_stop_count:
            L.append(f"  {RD}{BD}⚠  {near_stop_count} position(s) within 0.5% of stop — review immediately!{W}")
            for ns in near_stop_list:
                L.append(f"  {RD}   • {ns}{W}")
    else:
        L.append(f"  {DM}No open forex positions.{W}")
    L.append(HR)

    # ── Strategy breakdown — the dashboard's main analytical view ──
    # Sorted by realized P&L (best to worst), one row per strategy, every
    # number a strategy's track record actually needs: win rate, profit
    # factor, realized vs unrealized. No per-pair/currency table here by
    # design (2026-08-24, explicit request) — that's a different question
    # ("which pairs work") from this one ("which strategies work"); see
    # pnl_dashboard.py or the Strategy Overlap Tracker artifact for that.
    L.append("")
    L.append(f"  {BD}STRATEGY BREAKDOWN{W}  {DM}(sorted by realized P&L){W}")
    L.append("")

    strat_labels = {
        "ema":        ("EMA Trend",        "117"),
        "rsi":        ("RSI Pullback",     "117"),
        "donchian":   ("Donchian Break",   "117"),
        "bb":         ("BB Reversion",     "117"),
        "pullback":   ("EMA Pullback ★",   "117"),
        "gap":        ("Gap Fill ★★",      "117"),
        "supertrend": ("SuperTrend",       "117"),
        "zscore":     ("Z-Score Rev",      "117"),
        "ml":         ("ML Signals",       "117"),
        "cnn_lstm":   ("CNN-LSTM",         "117"),
        "london_breakout": ("LBO Day Trade", "28"),
    }

    # Realized P&L per strategy (closed trades) drives the headline number and
    # color — this is each strategy's actual locked-in track record, not
    # paper gains from currently-open positions that could reverse tomorrow.
    # Unrealized is shown alongside it, never blended into the same total.
    try:
        import pnl_tracker
        strat_rows = pnl_tracker.get_strategy_summary("forex")  # already sorted by total_pnl desc
    except Exception:
        strat_rows = []
    stats_by_strat = {r["strategy"]: r for r in strat_rows}

    # Strategies with a closed trade come first (real track record, best to
    # worst); strategies still 100% open (no closed trades yet, so no row
    # from get_strategy_summary) follow after, alphabetically — nothing to
    # rank them by yet, but they should still show their open count.
    ordered = [s for s in stats_by_strat if s in strat_labels]
    ordered += sorted(s for s in strat_labels if s not in stats_by_strat)

    SB_HDR = (
        f"  {DM}{'Strategy':<16}  {'Active':>6}  {'Closed':>7}  {'W/L':>7}  "
        f"{'WR%':>6}  {'PF':>6}  {'Realized P&L':>15}  {'Unrealized':>13}{W}"
    )
    L.append(SB_HDR)
    L.append(HR)

    grand_realized = grand_unrealized = 0.0
    grand_closed = grand_wins = grand_losses = 0

    for strat in ordered:
        label, max_slots = strat_labels[strat]
        sc    = STRAT_COL.get(strat, DM)
        count = sum(1 for p in positions if p["strategy"] == strat)

        unrealized = 0.0
        for p in positions:
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

        grand_realized   += realized
        grand_unrealized += unrealized
        grand_closed     += n_closed
        grand_wins       += wins
        grand_losses     += losses

        pnl_col = GR if realized >= 0 else RD
        u_col   = GR if unrealized >= 0 else RD
        wr_col  = GR if wr >= 50 else (YL if n_closed else DM)
        pf_s    = f"{pf:.2f}" if pf is not None else ("∞" if realized > 0 and n_closed else "—")
        wr_s    = f"{wr:.1f}%" if n_closed else "—"

        realized_cell   = _pad_ansi(f"{pnl_col}{BD}{realized:>+,.0f} EUR{W}", 15)
        unrealized_cell = _pad_ansi(f"{u_col}{unrealized:>+,.0f} EUR{W}", 13)
        L.append(
            f"  {sc}{BD}{label:<16}{W}  {DM}{f'{count}/{max_slots}':>6}{W}  "
            f"{n_closed:>7}  {DM}{f'{wins}W/{losses}L':>7}{W}  "
            f"{wr_col}{wr_s:>6}{W}  {DM}{pf_s:>6}{W}  "
            f"{realized_cell}  {unrealized_cell}"
        )
        # Flag when one trade dominates the total — a "profitable" strategy
        # total can be almost entirely one outlier win, masking a real losing
        # trend in every other trade (found 2026-08-21: Gap Fill's +13,759
        # total was 130% explained by one legacy oversized trade from before
        # a sizing bug fix — the other 16 trades net -4,082).
        if n_closed >= 2 and realized > 0 and best > 0 and best >= 0.5 * realized:
            pct = best / realized * 100
            L.append(
                f"  {DM}{'':<16}  {YL}⚠ single best trade ({best:>+,.0f} EUR) is {pct:.0f}% of "
                f"this total — check it isn't propping up an otherwise-losing strategy{W}"
            )

    L.append(HR)
    g_wr = (grand_wins / grand_closed * 100) if grand_closed else 0.0
    try:
        g_pf = pnl_tracker.get_summary("forex").get("forex", {}).get("profit_factor")
    except Exception:
        g_pf = None
    g_pf_s = f"{g_pf:.2f}" if g_pf is not None else "—"
    g_col  = GR if grand_realized >= 0 else RD
    gu_col = GR if grand_unrealized >= 0 else RD
    g_realized_cell   = _pad_ansi(f"{g_col}{BD}{grand_realized:>+,.0f} EUR{W}", 15)
    g_unrealized_cell = _pad_ansi(f"{gu_col}{grand_unrealized:>+,.0f} EUR{W}", 13)
    L.append(
        f"  {BD}{'ALL STRATEGIES':<16}{W}  {DM}{open_count:>6}{W}  {grand_closed:>7}  "
        f"{DM}{f'{grand_wins}W/{grand_losses}L':>7}{W}  {g_col}{g_wr:>5.1f}%{W}  {DM}{g_pf_s:>6}{W}  "
        f"{g_realized_cell}  {g_unrealized_cell}"
    )
    L.append("")
    L.append(HR)

    # ── Recent scheduler log ──────────────────────────────────────
    log_lines = _last_log_lines(10)
    L.append("")
    L.append(f"  {BD}SCHEDULER LOG{W}  {DM}(last 10 lines from forex_scheduler.log){W}")
    L.append("")
    for line in log_lines:
        # Colour-code log output
        if "ERROR" in line or "error" in line:
            L.append(f"  {RD}{line}{W}")
        elif "BUY" in line or "placed" in line or "LONG" in line:
            L.append(f"  {GR}{line}{W}")
        elif "SELL" in line or "SHORT" in line:
            L.append(f"  {RD}{line}{W}")
        elif "exit" in line.lower() or "closed" in line.lower():
            L.append(f"  {YL}{line}{W}")
        else:
            L.append(f"  {DM}{line}{W}")
    if not log_lines:
        L.append(f"  {DM}No log entries yet.{W}")
    L.append("")
    L.append(HR)

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
