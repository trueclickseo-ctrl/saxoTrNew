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

REFRESH_SECONDS = 60

# ── Windows VT100 ─────────────────────────────────────────────────
def _enable_vt():
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle   = kernel32.GetStdHandle(-11)
        mode     = ctypes.c_ulong()
        kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        kernel32.SetConsoleMode(handle, mode.value | 0x4)
    except Exception:
        pass
    os.system("")

_enable_vt()

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

STRAT_COL = {"ema": CY, "rsi": MG, "donchian": GR}


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


def _render(once: bool = False, interval: int = REFRESH_SECONDS) -> str:
    now_ts    = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
    positions = _read_positions()
    token     = price_service.load_token()

    # Fetch live prices for ALL 7 FX pairs via Saxo API (fallback: yfinance)
    # This covers both the positions table and the live rates strip
    all_instruments = list(price_service.FX_INSTRUMENTS)  # all 7 pairs

    # Also add any position UIC overrides (in case a symbol's UIC differs from FX_INSTRUMENTS)
    pos_uics = {p["symbol"]: {"symbol": p["symbol"], "uic": p["uic"], "asset_type": p["asset_type"]}
                for p in positions if p.get("uic")}
    # Merge: prefer position UIC if available
    merged = {i["symbol"]: i for i in all_instruments}
    merged.update(pos_uics)
    instruments = list(merged.values())

    live, price_src = price_service.fetch_prices(instruments, token=token)

    W_TOTAL = 108
    HR      = f"  {DM}{'─' * W_TOTAL}{W}"

    L = []

    # ── Header ────────────────────────────────────────────────────
    L.append(f"  {BD}{CY}╔{'═'*W_TOTAL}╗{W}")
    src_tag = f"{'SAXO LIVE' if price_src=='saxo' else 'yfinance (~15min delay)'}"
    L.append(f"  {BD}{CY}║{'  FOREX QUANT DASHBOARD':^{W_TOTAL}}║{W}")
    L.append(f"  {BD}{CY}║{f'  EMA · RSI(2) · Donchian  |  7 FX Pairs  |  Prices: {src_tag}  |  {now_ts}':^{W_TOTAL}}║{W}")
    L.append(f"  {BD}{CY}╚{'═'*W_TOTAL}╝{W}")
    L.append("")

    # ── Strategy legend ───────────────────────────────────────────
    L.append(f"  {BD}STRATEGIES{W}   "
             f"{CY}{BD}■ EMA(5/30)+ADX(14){W}  trend-following           "
             f"{MG}{BD}■ RSI(2) Pullback{W}  mean-reversion in trend   "
             f"{GR}{BD}■ Donchian(20){W}  channel breakout")
    L.append(f"  {DM}Scheduler: 06:20 PKT daily  |  --live flag active  |  "
             f"Max 4 slots × 3 strategies = 12 positions{W}")
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
        f"{'Stop':>10}  {'P&L (USD)':>12}  {'%':>9}  "
        f"{'ATR':>8}  {'Days':>5}  {'Stop Risk':>12}{W}"
    )
    L.append(COL_HDR)
    L.append(HR)

    total_pnl  = 0.0
    total_cost = 0.0
    near_stop_count = 0

    if positions:
        # Group by strategy for cleaner display
        strat_order = ["ema", "rsi", "donchian"]
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

                if now_px and ep > 0:
                    raw_pnl  = (now_px - ep) if is_long else (ep - now_px)
                    pnl_usd  = raw_pnl * qty
                    pnl_pct  = raw_pnl / ep * 100
                    total_pnl  += pnl_usd
                    total_cost += ep * qty
                    pc   = GR if pnl_usd >= 0 else RD
                    pnl_s = f"{pc}{pnl_usd:>+,.0f}{W}"
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
            f"{DM}{open_count} positions  |  Cost: ${total_cost:>,.0f}  |  "
            f"Unrealized P&L: {W}"
            f"{tc}{BD}{total_pnl:>+,.0f} USD  ({tpct:>+.4f}%){W}"
        )

        # Near-stop warning
        if near_stop_count:
            L.append(f"  {RD}{BD}⚠  {near_stop_count} position(s) near stop — review immediately!{W}")
    else:
        L.append(f"  {DM}No open forex positions.{W}")
    L.append(HR)

    # ── Per-strategy summary ──────────────────────────────────────
    L.append("")
    L.append(f"  {BD}STRATEGY BREAKDOWN{W}")
    L.append("")

    strat_labels = {
        "ema":      ("EMA Trend",       "EMA(5/30)+ADX(14)",  "Sharpe 1.13",   "4 slots"),
        "rsi":      ("RSI Pullback",    "RSI(2)<10 dip-buy",  "mean reversion", "4 slots"),
        "donchian": ("Donchian Break",  "20-day high/low",    "Sharpe 1.62",   "4 slots"),
    }

    for strat, (label, desc, metric, slots) in strat_labels.items():
        sc    = STRAT_COL.get(strat, DM)
        count = sum(1 for p in positions if p["strategy"] == strat)
        pnl_s = 0.0
        for p in positions:
            if p["strategy"] != strat:
                continue
            sym     = p["symbol"]
            now_px  = live.get(sym)
            if now_px and p["entry"] > 0:
                raw = (now_px - p["entry"]) if p["direction"] == "Buy" else (p["entry"] - now_px)
                pnl_s += raw * p["qty"]
        pnl_col = GR if pnl_s >= 0 else RD
        L.append(
            f"  {sc}{BD}{label:<18}{W}  {DM}{desc:<22}{W}  {metric:<14}  "
            f"{slots:<8}  {count}/4 active  "
            f"P&L: {pnl_col}{BD}{pnl_s:>+,.0f} USD{W}"
        )

    L.append("")
    L.append(HR)

    # ── Pairs heat map ────────────────────────────────────────────
    src_label = "Saxo SIM live" if price_src == "saxo" else "yfinance ~15min delay"
    L.append("")
    L.append(f"  {BD}LIVE RATES{W}  {DM}({src_label}){W}")
    L.append("")
    rate_line = "  "
    for inst in price_service.FX_INSTRUMENTS:
        sym = inst["symbol"]
        px  = live.get(sym)
        if px:
            rate_line += f"{BD}{sym}{W}  {DM}{px:.5f}{W}    "
        else:
            rate_line += f"{DM}{sym}  —{W}    "
    L.append(rate_line)
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

    # ── P&L summary ───────────────────────────────────────────────
    try:
        import pnl_tracker
        fx_sum = pnl_tracker.get_summary("forex")
        s      = fx_sum.get("forex", {})
        pnl_r  = s.get("realized_pnl", 0.0)
        pc     = GR if pnl_r >= 0 else RD
        PNL_HR = f"  {DM}{'─'*W_TOTAL}{W}"
        L.append("")
        L.append(f"  {BD}FOREX P&L LEDGER{W}  {DM}(pnl_ledger.db — run pnl_dashboard.py for full view){W}")
        L.append(PNL_HR)
        L.append(
            f"  {BD}Realized P&L:{W}  {pc}{BD}{pnl_r:>+,.2f} USD{W}     "
            f"{DM}Closed: {s.get('closed_trades',0)}  |  "
            f"Win rate: {s.get('win_rate',0):.1f}%  |  "
            f"Best: +{s.get('best_trade',0):.2f}  |  "
            f"Worst: {s.get('worst_trade',0):+.2f}  |  "
            f"Profit factor: {s.get('profit_factor') or '—'}{W}"
        )
        L.append(PNL_HR)
    except Exception:
        pass

    # ── Footer ────────────────────────────────────────────────────
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
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()
            sys.stdout.write(out)
            sys.stdout.flush()
            time.sleep(interval)
        except KeyboardInterrupt:
            sys.stdout.write(f"\n{W}")
            break


if __name__ == "__main__":
    main()
