"""
futures_dashboard.py  —  Live Futures positions dashboard
----------------------------------------------------------
Usage:
    python futures_dashboard.py            # refresh every 30s
    python futures_dashboard.py --fast     # refresh every 5s
    python futures_dashboard.py --once     # print once and exit
"""

import os, sys, json, time, requests
from datetime import date, datetime

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR             = os.path.dirname(os.path.abspath(__file__))
FUTURES_STATE_PATH   = os.path.join(BASE_DIR, "data", "futures_state.json")
FUTURES_ORDERS_PATH  = os.path.join(BASE_DIR, "data", "futures_orders.json")
FUTURES_LOG_PATH     = os.path.join(BASE_DIR, "data", "futures_scheduler.log")
UIC_CACHE_PATH       = os.path.join(BASE_DIR, "data", "futures_uic_cache.json")

sys.path.insert(0, BASE_DIR)
import price_service

REFRESH_SECONDS = 30

SIM_BASE = "https://gateway.saxobank.com/sim/openapi"

MARKET_DESC = {
    "ES": "S&P 500",
    "NQ": "NASDAQ-100",
    "GC": "Gold (XAU/USD)",
    "CL": "WTI Crude Oil",
    "ZB": "US 30Y T-Bond",
}

# ── Windows console clear ──────────────────────────────────────────
def _clear_console():
    try:
        import ctypes, struct
        k32  = ctypes.windll.kernel32
        h    = k32.GetStdHandle(-11)
        buf  = ctypes.create_string_buffer(22)
        k32.GetConsoleScreenBufferInfo(h, buf)
        _, _, _, _, _, left, top, right, bottom, _, _ = struct.unpack("hhhhHhhhhhh", buf.raw)
        cols = right - left + 1; rows = bottom - top + 1
        size = cols * rows; done = ctypes.c_ulong(0)
        k32.FillConsoleOutputCharacterW(h, 32, size, 0, ctypes.byref(done))
        k32.FillConsoleOutputAttribute(h, 7,  size, 0, ctypes.byref(done))
        k32.SetConsoleCursorPosition(h, 0)
    except Exception:
        sys.stdout.write("\033[2J\033[H"); sys.stdout.flush()

try:
    import ctypes as _ct
    _k32 = _ct.windll.kernel32; _h = _k32.GetStdHandle(-11); _m = _ct.c_ulong()
    _k32.GetConsoleMode(_h, _ct.byref(_m)); _k32.SetConsoleMode(_h, _m.value | 0x4)
except Exception:
    pass

# ── Colours ────────────────────────────────────────────────────────
GR  = "\033[92m"; RD  = "\033[91m"; YL  = "\033[93m"
BL  = "\033[94m"; CY  = "\033[96m"; MG  = "\033[95m"
W   = "\033[0m";  BD  = "\033[1m";  DM  = "\033[2m"; WH = "\033[97m"

STRAT_COL = {
    "donchian": GR, "rsi": CY, "ema": YL,
    "macd": BL, "squeeze": WH, "ma_cross": MG,
}
STRAT_DESC = {
    "donchian": "Donchian 30-day breakout",
    "rsi":      "RSI(2) mean-reversion",
    "ema":      "EMA(5/20)+ADX(14) trend",
    "macd":     "MACD(12,26,9) momentum",
    "squeeze":  "BB Squeeze breakout",
    "ma_cross": "SMA(50/200) cross",
}


def _pad_ansi(s: str, width: int) -> str:
    import re
    visible = len(re.sub(r"\033\[[0-9;]*m", "", s))
    return s + " " * max(0, width - visible)


def _read_positions() -> list:
    if not os.path.exists(FUTURES_STATE_PATH):
        return []
    try:
        d = json.load(open(FUTURES_STATE_PATH, encoding="utf-8"))
        out = []
        for key, pos in d.get("positions", {}).items():
            strat, sym = key.split(":", 1) if ":" in key else ("donchian", key)
            out.append({
                "key":        key,
                "strategy":   pos.get("strategy", strat),
                "symbol":     sym,
                "uic":        pos.get("uic"),
                "asset_type": pos.get("asset_type", ""),
                "direction":  pos.get("direction", "Buy"),
                "qty":        pos.get("quantity", 0),
                "entry":      float(pos.get("entry_price", 0)),
                "stop":       float(pos.get("stop_price", 0)),
                "entry_date": pos.get("entry_date", ""),
                "atr":        float(pos.get("atr_at_entry", 0)),
                "score":      pos.get("score", 0),
            })
        return out
    except Exception:
        return []


def _saxo_live_prices(token: str) -> dict:
    """Fetch CurrentPrice for every open position from Saxo positions API.
    Returns {uic(int): price(float)}.
    """
    if not token:
        return {}
    try:
        r = requests.get(
            f"{SIM_BASE}/port/v1/positions/me",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if r.status_code != 200:
            return {}
        px_map = {}
        for p in r.json().get("Data", []):
            pb  = p.get("PositionBase", {})
            pv  = p.get("PositionView", {})
            uic = pb.get("Uic")
            px  = float(pv.get("CurrentPrice") or 0)
            ep  = float(pb.get("OpenPrice") or 0)
            qty = float(pb.get("Amount") or 1)
            pnl = float(pv.get("ProfitLossOnTrade") or 0)
            if px == 0 and ep > 0 and qty != 0:
                px = ep + pnl / qty
            if uic and px > 0:
                px_map[int(uic)] = round(px, 5)
        return px_map
    except Exception:
        return {}


def _load_uic_cache() -> dict:
    try:
        return json.load(open(UIC_CACHE_PATH, encoding="utf-8"))
    except Exception:
        return {}


def _recent_orders(n: int = 10) -> list:
    if not os.path.exists(FUTURES_ORDERS_PATH):
        return []
    try:
        entries = json.load(open(FUTURES_ORDERS_PATH, encoding="utf-8"))
        return entries[-n:] if isinstance(entries, list) else []
    except Exception:
        return []


def _last_log_lines(n: int = 8) -> list:
    if not os.path.exists(FUTURES_LOG_PATH):
        return []
    try:
        with open(FUTURES_LOG_PATH, encoding="utf-8", errors="replace") as f:
            lines = [l.rstrip() for l in f if l.strip()]
        return lines[-n:]
    except Exception:
        return []


def _render(once: bool = False, interval: int = REFRESH_SECONDS) -> str:
    now_ts    = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
    positions = _read_positions()
    token     = price_service.load_token()
    uic_cache = _load_uic_cache()

    # Live prices keyed by UIC
    px_by_uic = _saxo_live_prices(token)
    any_live  = bool(px_by_uic)

    if token and any_live:
        src_note = f"{GR}{BD}SAXO LIVE{W}"
        src_tag  = "Saxo SIM live"
    elif token:
        src_note = f"{YL}SIM — prices from open positions only{W}"
        src_tag  = "open positions only"
    else:
        src_note = f"{RD}token expired — run: python set_token.py{W}"
        src_tag  = "n/a"

    W_TOTAL = 112
    HR      = f"  {DM}{'─' * W_TOTAL}{W}"
    L       = []

    # ── Header ────────────────────────────────────────────────────
    L.append(f"  {BD}{MG}╔{'═'*W_TOTAL}╗{W}")
    L.append(f"  {BD}{MG}║{'  FUTURES QUANT DASHBOARD':^{W_TOTAL}}║{W}")
    sub = f"  Donchian · RSI(2) · EMA · MACD · Squeeze · MA Cross  |  ES · NQ · GC · CL · ZB  |  {now_ts}"
    L.append(f"  {BD}{MG}║{sub:^{W_TOTAL}}║{W}")
    L.append(f"  {BD}{MG}╚{'═'*W_TOTAL}╝{W}")
    L.append(f"  Price source: {src_note}")
    L.append("")

    # ── Strategy legend ───────────────────────────────────────────
    L.append(
        f"  {BD}STRATEGIES{W}   "
        f"{GR}{BD}■ Donchian{W}  breakout   "
        f"{CY}{BD}■ RSI(2){W}  mean-rev   "
        f"{YL}{BD}■ EMA(5/20){W}  trend   "
        f"{BL}{BD}■ MACD{W}  momentum   "
        f"{WH}{BD}■ Squeeze{W}  BB squeeze   "
        f"{MG}{BD}■ MA Cross{W}  golden/death"
    )
    L.append(
        f"  {DM}Scheduler: daily run  |  6 strategies × 5 slots = 30 max positions  |  "
        f"Markets: ES (S&P500) · NQ (NASDAQ) · GC (Gold) · CL (Oil) · ZB (T-Bond){W}"
    )
    L.append(HR)
    L.append("")

    # ── Positions table ───────────────────────────────────────────
    open_count = len(positions)
    L.append(f"  {BD}OPEN POSITIONS{W}  {DM}({open_count} active){W}")
    L.append("")
    COL_HDR = (
        f"  {DM}{'Strategy':<11}  {'Symbol':<6}  {'Market':<16}  {'Side':<6}  "
        f"{'Qty':>8}  {'Entry':>10}  {'Now':>10}  "
        f"{'Stop':>10}  {'P&L (USD)':>12}  {'%':>8}  "
        f"{'ATR':>8}  {'Days':>5}  {'Stop Risk':>10}{W}"
    )
    L.append(COL_HDR)
    L.append(HR)

    total_pnl  = 0.0
    total_cost = 0.0
    near_stop_list = []

    if positions:
        strat_order = ["donchian", "rsi", "ema", "macd", "squeeze", "ma_cross"]
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
                uic      = int(p["uic"]) if p["uic"] else None
                is_long  = p["direction"] == "Buy"
                ep       = p["entry"]
                stop_px  = p["stop"]
                qty      = p["qty"]
                atr      = p["atr"]
                desc     = MARKET_DESC.get(sym, p["asset_type"])
                now_px   = px_by_uic.get(uic) if uic else None

                try:
                    held = (date.today() - date.fromisoformat(p["entry_date"])).days
                except Exception:
                    held = 0

                side_tag = f"{GR}{BD}LONG {W}" if is_long else f"{RD}{BD}SHORT{W}"

                if now_px and ep > 0:
                    raw_pnl = (now_px - ep) if is_long else (ep - now_px)
                    pnl_usd = raw_pnl * qty
                    pnl_pct = raw_pnl / ep * 100
                    total_pnl  += pnl_usd
                    total_cost += ep * qty
                    pc    = GR if pnl_usd >= 0 else RD
                    pnl_s = f"{pc}{pnl_usd:>+,.0f}{W}"
                    pct_s = f"{pc}{pnl_pct:>+.3f}%{W}"
                    now_s = f"{now_px:.4f}"
                else:
                    pnl_s = f"{DM}{'—':>12}{W}"
                    pct_s = f"{DM}{'—':>8}{W}"
                    now_s = f"{'—':>10}"

                near = (stop_px > 0 and now_px and
                        ((is_long and now_px < stop_px * 1.005) or
                         (not is_long and now_px > stop_px * 0.995)))
                if near:
                    dist_w = abs(now_px - stop_px) / now_px * 100 if now_px else 0
                    near_stop_list.append(
                        f"{strat.upper()} {sym} {'LONG' if is_long else 'SHORT'} — "
                        f"{dist_w:.2f}% from stop ({stop_px:.4f})"
                    )
                stp_col = f"{RD}{BD}" if near else DM
                stop_s  = f"{stop_px:.4f}"

                if stop_px > 0 and now_px:
                    dist_pct = abs(now_px - stop_px) / now_px * 100
                    dist_s   = f"{DM}{dist_pct:.2f}% away{W}"
                else:
                    dist_s   = f"{DM}{'—':>10}{W}"

                L.append(
                    f"  {sc}{BD}{strat:<11}{W}  {BD}{sym:<6}{W}  {DM}{desc:<16}{W}  {side_tag}  "
                    f"{DM}{qty:>8,}{W}  {DM}{ep:>10.4f}{W}  {BD}{now_s:>10}{W}  "
                    f"{stp_col}{stop_s:>10}{W}  {_pad_ansi(pnl_s, 17)}  "
                    f"{_pad_ansi(pct_s, 13)}  {DM}{atr:>8.4f}{W}  "
                    f"{DM}{held:>5}d{W}  {dist_s}"
                )

        L.append(HR)

        # Totals
        tc   = GR if total_pnl >= 0 else RD
        tpct = (total_pnl / total_cost * 100) if total_cost > 0 else 0
        L.append(
            f"  {BD}TOTAL{W}  "
            f"{DM}{open_count} positions  |  Notional: ${total_cost:>,.0f}  |  "
            f"Unrealized P&L: {W}"
            f"{tc}{BD}{total_pnl:>+,.2f} USD  ({tpct:>+.3f}%){W}"
        )
        if not any_live:
            L.append(
                f"  {YL}Live P&L unavailable — prices only visible for open positions{W}"
            )

        if near_stop_list:
            L.append(f"  {RD}{BD}  {len(near_stop_list)} position(s) within 0.5% of stop!{W}")
            for ns in near_stop_list:
                L.append(f"  {RD}   • {ns}{W}")
    else:
        L.append(f"  {DM}No open futures positions.{W}")
    L.append(HR)

    # ── Market overview strip ─────────────────────────────────────
    L.append("")
    L.append(f"  {BD}MARKET OVERVIEW{W}  {DM}({src_tag}){W}")
    L.append("")
    uic_map = {
        "ES": 4913, "NQ": 4912, "GC": 8176,
        "CL": uic_cache.get("CL", {}).get("uic"),
        "ZB": uic_cache.get("ZB", {}).get("uic"),
    }
    mkt_row = "  "
    for sym, mkt_uic in uic_map.items():
        desc = MARKET_DESC.get(sym, sym)
        px   = px_by_uic.get(int(mkt_uic)) if mkt_uic else None
        if px:
            mkt_row += f"{BD}{MG}{sym}{W}  {BD}{px:,.4f}{W}  {DM}{desc}{W}    "
        else:
            mkt_row += f"{DM}{sym}  —  {desc}{W}    "
    L.append(mkt_row)
    L.append("")
    L.append(HR)

    # ── Strategy breakdown ────────────────────────────────────────
    L.append("")
    L.append(f"  {BD}STRATEGY BREAKDOWN{W}")
    L.append("")
    for strat in ["donchian", "rsi", "ema", "macd", "squeeze", "ma_cross"]:
        sc    = STRAT_COL.get(strat, DM)
        desc  = STRAT_DESC.get(strat, strat)
        count = sum(1 for p in positions if p["strategy"] == strat)
        strat_pnl = 0.0
        for p in positions:
            if p["strategy"] != strat:
                continue
            uic    = int(p["uic"]) if p["uic"] else None
            now_px = px_by_uic.get(uic) if uic else None
            if now_px and p["entry"] > 0:
                raw = (now_px - p["entry"]) if p["direction"] == "Buy" else (p["entry"] - now_px)
                strat_pnl += raw * p["qty"]
        pc = GR if strat_pnl >= 0 else RD
        L.append(
            f"  {sc}{BD}{strat:<11}{W}  {DM}{desc:<28}{W}  "
            f"{count}/5 active    "
            f"P&L: {pc}{BD}{strat_pnl:>+,.2f} USD{W}"
        )

    L.append("")
    L.append(HR)

    # ── Recent orders ─────────────────────────────────────────────
    orders = _recent_orders(10)
    L.append("")
    L.append(f"  {BD}RECENT ORDERS{W}  {DM}(last 10 from futures_orders.json){W}")
    L.append("")
    if orders:
        for o in reversed(orders):
            ts    = o.get("timestamp", o.get("time", ""))[:19]
            strat = o.get("strategy", "?")
            sym   = o.get("symbol", "?")
            side  = o.get("side", o.get("direction", "?"))
            qty   = o.get("quantity", o.get("qty", "?"))
            px    = o.get("price", o.get("entry_price", "?"))
            oid   = o.get("order_id", o.get("orderId", "—"))
            mode  = o.get("mode", "LIVE")
            sc    = STRAT_COL.get(strat, DM)
            col   = GR if side in ("Buy", "LONG") else RD
            mc    = DM if mode == "DRY" else W
            try:
                px_s = f"{float(px):.4f}"
            except Exception:
                px_s = str(px)
            try:
                qty_s = f"{int(qty):,}"
            except Exception:
                qty_s = str(qty)
            L.append(
                f"  {mc}{DM}{ts}{W}  {sc}{BD}{strat:<11}{W}  "
                f"{BD}{sym:<5}{W}  {col}{BD}{side:<5}{W}  "
                f"{mc}qty={qty_s}  @{px_s}  id={oid}  [{mode}]{W}"
            )
    else:
        L.append(f"  {DM}No orders logged yet.{W}")
    L.append("")
    L.append(HR)

    # ── Scheduler log ─────────────────────────────────────────────
    log_lines = _last_log_lines(8)
    if log_lines:
        L.append("")
        L.append(f"  {BD}SCHEDULER LOG{W}  {DM}(futures_scheduler.log){W}")
        L.append("")
        for line in log_lines:
            if "ERROR" in line or "error" in line:
                L.append(f"  {RD}{line}{W}")
            elif "Buy" in line or "LONG" in line or "placed" in line:
                L.append(f"  {GR}{line}{W}")
            elif "Sell" in line or "SHORT" in line:
                L.append(f"  {RD}{line}{W}")
            elif "exit" in line.lower() or "closed" in line.lower():
                L.append(f"  {YL}{line}{W}")
            else:
                L.append(f"  {DM}{line}{W}")
        L.append("")
        L.append(HR)

    # ── P&L ledger ────────────────────────────────────────────────
    try:
        import pnl_tracker
        s  = pnl_tracker.get_summary("futures").get("futures", {})
        pr = s.get("realized_pnl", 0.0)
        pc = GR if pr >= 0 else RD
        L.append("")
        L.append(f"  {BD}FUTURES P&L LEDGER{W}  {DM}(pnl_ledger.db){W}")
        L.append(HR)
        L.append(
            f"  {BD}Realized P&L:{W}  {pc}{BD}{pr:>+,.2f} USD{W}     "
            f"{DM}Closed: {s.get('closed_trades',0)}  |  "
            f"Win rate: {s.get('win_rate',0):.1f}%  |  "
            f"Best: +{s.get('best_trade',0):.2f}  |  "
            f"Worst: {s.get('worst_trade',0):+.2f}  |  "
            f"Profit factor: {s.get('profit_factor') or '—'}{W}"
        )
        L.append(HR)
    except Exception:
        pass

    # ── Footer ────────────────────────────────────────────────────
    if not once:
        L.append(
            f"  {DM}Refreshes every {interval}s  |  Ctrl+C to exit  |  "
            f"Run: python futures/runner.py --live  to force update{W}"
        )
    L.append("")
    return "\n".join(L)


def main():
    fast     = "--fast" in sys.argv
    once     = "--once" in sys.argv
    interval = 5 if fast else REFRESH_SECONDS

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
