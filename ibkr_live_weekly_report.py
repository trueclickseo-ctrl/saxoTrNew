"""
ibkr_live_weekly_report.py
---------------------------
Weekly portfolio report for the IBKR LIVE stocks account (U28013794).

Reads data/ibkr_live_stocks.db, fetches current prices via yfinance,
builds a rich HTML email with inline SVG charts and sends it to
atoslive500@gmail.com (the LIVE routing address in config/email.json).

Usage (manual):
    python ibkr_live_weekly_report.py

Scheduled: every Saturday 21:00 PKT via Task Scheduler (after Friday US close).
"""
from __future__ import annotations

import os
import sys
import sqlite3
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_LIVE_DB   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "ibkr_live_stocks.db")
_START_CAP_SEK = 250_000.0


# ── Data helpers ──────────────────────────────────────────────────────────────

def _conn():
    db = os.environ.get("IBKR_DB_PATH", _LIVE_DB)
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    return con


def _load_trades() -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM trades ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            pass
    return None


def _classify(trades: list[dict]) -> tuple[list[dict], list[dict]]:
    """Return (open_positions, closed_trades).

    Logic:
    - open  = BUY, status=FILLED, fill_price > 0
    - closed = matched BUY/SELL pairs or SOLD-BUY records
    """
    buys  = [t for t in trades if t["side"] == "BUY"  and (t["fill_price"] or 0) > 0]
    sells = [t for t in trades if t["side"] == "SELL" and t["status"] == "FILLED" and (t["fill_price"] or 0) > 0]

    open_pos: list[dict] = []
    closed:   list[dict] = []

    for buy in buys:
        sym = buy["symbol"]
        status = buy["status"]
        buy_dt = _parse_dt(buy.get("filled_at") or buy.get("created_at"))

        if status == "FILLED":
            # Check if a SELL exists for same symbol after this buy
            later_sell = next(
                (s for s in sells if s["symbol"] == sym
                 and (_parse_dt(s.get("filled_at") or s.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc))
                 > (buy_dt or datetime.min.replace(tzinfo=timezone.utc))),
                None,
            )
            if later_sell:
                pnl_usd = (later_sell["fill_price"] - buy["fill_price"]) * buy["qty"]
                closed.append({
                    "symbol":     sym,
                    "strategy":   buy["strategy"] or "blend",
                    "qty":        buy["qty"],
                    "entry_price": buy["fill_price"],
                    "exit_price":  later_sell["fill_price"],
                    "exit_approx": False,
                    "pnl_usd":    pnl_usd,
                    "entry_date": (buy_dt.date().isoformat() if buy_dt else ""),
                    "exit_date":  (_parse_dt(later_sell.get("filled_at")).date().isoformat()
                                   if _parse_dt(later_sell.get("filled_at")) else ""),
                    "days_held":  ((_parse_dt(later_sell.get("filled_at")) - buy_dt).days
                                   if buy_dt and _parse_dt(later_sell.get("filled_at")) else 0),
                })
            else:
                open_pos.append({
                    "symbol":      sym,
                    "strategy":    buy["strategy"] or "blend",
                    "qty":         buy["qty"],
                    "entry_price": buy["fill_price"],
                    "stop_price":  buy["stop_price"] or 0,
                    "entry_date":  (buy_dt.date().isoformat() if buy_dt else ""),
                    "days_held":   ((date.today() - buy_dt.date()).days if buy_dt else 0),
                })

        elif status == "SOLD":
            # Stop triggered — look for explicit SELL, else use stop_price
            later_sell = next(
                (s for s in sells if s["symbol"] == sym
                 and (_parse_dt(s.get("filled_at") or s.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc))
                 > (buy_dt or datetime.min.replace(tzinfo=timezone.utc))),
                None,
            )
            exit_dt_obj  = _parse_dt(buy.get("filled_at") or buy.get("created_at"))
            if later_sell:
                exit_price   = later_sell["fill_price"]
                exit_approx  = False
                exit_date    = (_parse_dt(later_sell.get("filled_at")).date().isoformat()
                                if _parse_dt(later_sell.get("filled_at")) else "")
                days_held    = ((_parse_dt(later_sell.get("filled_at")) - buy_dt).days
                                if buy_dt and _parse_dt(later_sell.get("filled_at")) else 0)
            else:
                exit_price   = buy["stop_price"] or buy["fill_price"]
                exit_approx  = True
                exit_date    = (exit_dt_obj.date().isoformat() if exit_dt_obj else "")
                days_held    = ((exit_dt_obj.date() - buy_dt.date()).days
                                if buy_dt and exit_dt_obj else 0)
            pnl_usd = (exit_price - buy["fill_price"]) * buy["qty"]
            closed.append({
                "symbol":     sym,
                "strategy":   buy["strategy"] or "blend",
                "qty":        buy["qty"],
                "entry_price": buy["fill_price"],
                "exit_price":  exit_price,
                "exit_approx": exit_approx,
                "pnl_usd":    pnl_usd,
                "entry_date": (buy_dt.date().isoformat() if buy_dt else ""),
                "exit_date":  exit_date,
                "days_held":  days_held,
            })

        # CANCELLED = DB artefact from account reset; skip for open, but check
        # if there's an explicit SELL that references it
        elif status == "CANCELLED" and (buy["fill_price"] or 0) > 0:
            later_sell = next(
                (s for s in sells if s["symbol"] == sym
                 and (_parse_dt(s.get("filled_at") or s.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc))
                 > (buy_dt or datetime.min.replace(tzinfo=timezone.utc))),
                None,
            )
            if later_sell:
                pnl_usd = (later_sell["fill_price"] - buy["fill_price"]) * buy["qty"]
                closed.append({
                    "symbol":     sym,
                    "strategy":   buy["strategy"] or "blend",
                    "qty":        buy["qty"],
                    "entry_price": buy["fill_price"],
                    "exit_price":  later_sell["fill_price"],
                    "exit_approx": False,
                    "pnl_usd":    pnl_usd,
                    "entry_date": (buy_dt.date().isoformat() if buy_dt else ""),
                    "exit_date":  (_parse_dt(later_sell.get("filled_at")).date().isoformat()
                                   if _parse_dt(later_sell.get("filled_at")) else ""),
                    "days_held":  ((_parse_dt(later_sell.get("filled_at")) - buy_dt).days
                                   if buy_dt and _parse_dt(later_sell.get("filled_at")) else 0),
                })

    # De-duplicate closed (same symbol may appear via CANCELLED + SOLD path)
    seen: set[tuple] = set()
    deduped: list[dict] = []
    for c in closed:
        key = (c["symbol"], c["entry_date"], c["exit_date"])
        if key not in seen:
            seen.add(key)
            deduped.append(c)

    return open_pos, deduped


def _fetch_prices(symbols: list[str]) -> dict[str, float]:
    """Fetch latest prices via yfinance (for display / unrealized P&L only)."""
    if not symbols:
        return {}
    try:
        import yfinance as yf
        tickers = yf.Tickers(" ".join(symbols))
        prices: dict[str, float] = {}
        for sym in symbols:
            try:
                info = tickers.tickers[sym].fast_info
                p    = getattr(info, "last_price", None) or getattr(info, "previous_close", None)
                if p:
                    prices[sym] = float(p)
            except Exception:
                pass
        return prices
    except Exception:
        return {}


def _fetch_usdsek() -> float:
    try:
        import yfinance as yf
        t = yf.Ticker("USDSEK=X")
        p = getattr(t.fast_info, "last_price", None) or getattr(t.fast_info, "previous_close", None)
        return float(p) if p else 10.5
    except Exception:
        return 10.5


# ── Chart builders (inline SVG) ───────────────────────────────────────────────

def _strategy_bars(by_strat: dict[str, float]) -> str:
    """Horizontal bar chart: strategy → all-time realized P&L (USD)."""
    if not by_strat:
        return ""
    max_abs = max(abs(v) for v in by_strat.values()) or 1
    rows = ""
    y = 18
    for strat, pnl in sorted(by_strat.items()):
        col  = "#4ade80" if pnl >= 0 else "#f87171"
        sign = "+" if pnl >= 0 else ""
        w    = max(2, int(abs(pnl) / max_abs * 160))
        rows += (
            f'<text x="0" y="{y}" fill="#94a3b8" font-size="12" font-family="sans-serif">'
            f'{strat.replace("_", " ").title()}</text>'
            f'<rect x="110" y="{y-14}" width="{w}" height="18" rx="3" fill="{col}" opacity="0.85"/>'
            f'<text x="{116+w}" y="{y}" fill="{col}" font-size="12" font-family="sans-serif">'
            f'{sign}${pnl:,.0f}</text>'
        )
        y += 32
    h = y + 4
    return (
        f'<svg viewBox="0 0 400 {h}" xmlns="http://www.w3.org/2000/svg" '
        f'style="width:100%;max-width:400px;margin:12px 0">'
        f'{rows}</svg>'
    )


def _ticker_bars(closed: list[dict]) -> str:
    """Horizontal bar chart per ticker: realized P&L USD."""
    if not closed:
        return ""
    max_abs = max(abs(c["pnl_usd"]) for c in closed) or 1
    rows = ""
    y = 18
    for c in sorted(closed, key=lambda x: x["pnl_usd"], reverse=True):
        col  = "#4ade80" if c["pnl_usd"] >= 0 else "#f87171"
        sign = "+" if c["pnl_usd"] >= 0 else ""
        w    = max(2, int(abs(c["pnl_usd"]) / max_abs * 150))
        label = f'{c["symbol"]}'
        rows += (
            f'<text x="0" y="{y}" fill="#94a3b8" font-size="12" font-family="sans-serif">'
            f'{label}</text>'
            f'<rect x="60" y="{y-14}" width="{w}" height="18" rx="3" fill="{col}" opacity="0.85"/>'
            f'<text x="{66+w}" y="{y}" fill="{col}" font-size="11" font-family="sans-serif">'
            f'{sign}${c["pnl_usd"]:,.2f}</text>'
        )
        y += 28
    h = y + 4
    return (
        f'<svg viewBox="0 0 400 {h}" xmlns="http://www.w3.org/2000/svg" '
        f'style="width:100%;max-width:400px;margin:12px 0">'
        f'{rows}</svg>'
    )


def _equity_bar(realized_usd: float, unrealized_usd: float, fx: float) -> str:
    """Simple stacked-bar showing starting cap, realized gains, unrealized gains (in SEK)."""
    realized_sek   = realized_usd * fx
    unrealized_sek = unrealized_usd * fx
    total_sek      = _START_CAP_SEK + realized_sek + unrealized_sek

    cap_w = 200
    max_v = max(abs(realized_sek), abs(unrealized_sek), 1)
    r_w   = max(2, int(abs(realized_sek)   / max_v * 80))
    u_w   = max(2, int(abs(unrealized_sek) / max_v * 80))
    r_col = "#4ade80" if realized_sek   >= 0 else "#f87171"
    u_col = "#60a5fa" if unrealized_sek >= 0 else "#f87171"
    r_sign = "+" if realized_sek   >= 0 else ""
    u_sign = "+" if unrealized_sek >= 0 else ""

    return f"""
<svg viewBox="0 0 420 110" xmlns="http://www.w3.org/2000/svg"
     style="width:100%;max-width:420px;margin:12px 0">
  <text x="0" y="18" fill="#64748b" font-size="11" font-family="sans-serif">Starting cap</text>
  <rect x="110" y="4"  width="{cap_w}" height="18" rx="3" fill="#334155" opacity="0.85"/>
  <text x="{115+cap_w}" y="18" fill="#94a3b8" font-size="11" font-family="sans-serif">
    {_START_CAP_SEK:,.0f} SEK
  </text>

  <text x="0" y="50" fill="#64748b" font-size="11" font-family="sans-serif">Realized P&amp;L</text>
  <rect x="110" y="36" width="{r_w}" height="18" rx="3" fill="{r_col}" opacity="0.85"/>
  <text x="{115+r_w}" y="50" fill="{r_col}" font-size="11" font-family="sans-serif">
    {r_sign}{realized_sek:,.0f} SEK
  </text>

  <text x="0" y="82" fill="#64748b" font-size="11" font-family="sans-serif">Unrealized</text>
  <rect x="110" y="68" width="{u_w}" height="18" rx="3" fill="{u_col}" opacity="0.5"/>
  <text x="{115+u_w}" y="82" fill="{u_col}" font-size="11" font-family="sans-serif">
    {u_sign}{unrealized_sek:,.0f} SEK (est.)
  </text>

  <text x="0" y="106" fill="#f1f5f9" font-size="12" font-weight="bold" font-family="sans-serif">
    Est. Total: {total_sek:,.0f} SEK
  </text>
</svg>"""


# ── HTML builders ─────────────────────────────────────────────────────────────

def _open_positions_html(open_pos: list[dict], prices: dict[str, float], fx: float) -> str:
    if not open_pos:
        return "<p style='color:#64748b;margin-top:8px'>No open positions.</p>"
    rows = ""
    for p in open_pos:
        sym    = p["symbol"]
        curr   = prices.get(sym, 0)
        entry  = p["entry_price"]
        qty    = p["qty"]
        unr    = (curr - entry) * qty if curr > 0 else 0
        unr_s  = unr * fx
        pct    = (curr - entry) / entry * 100 if entry > 0 and curr > 0 else 0
        col    = "#4ade80" if unr >= 0 else "#f87171"
        sign   = "+" if unr >= 0 else ""
        strat_badge = (
            '<span class="badge blend" style="font-size:10px">Blend</span>'
            if (p["strategy"] or "blend").lower() == "blend" else
            '<span class="badge watch" style="font-size:10px">Reversion</span>'
        )
        curr_str = f"${curr:.2f}" if curr > 0 else "—"
        unr_str  = (f"{sign}{unr_s:,.0f} SEK ({sign}{pct:.1f}%)" if curr > 0 else "—")
        rows += (
            f"<tr>"
            f"<td style='font-weight:700'>{sym}</td>"
            f"<td>{strat_badge}</td>"
            f"<td style='color:#94a3b8'>${entry:.2f}</td>"
            f"<td style='color:#94a3b8'>{curr_str}</td>"
            f"<td style='color:#94a3b8'>{int(qty)}</td>"
            f"<td style='color:#94a3b8'>{p['entry_date']}</td>"
            f"<td style='color:#94a3b8'>{p['days_held']}d</td>"
            f"<td style='color:{col};font-weight:700'>{unr_str}</td>"
            f"</tr>"
        )
    return f"""
<h3 style="color:#f1f5f9;font-size:15px;margin:20px 0 8px">
  Open Positions ({len(open_pos)})
  <span style="font-size:11px;color:#64748b;font-weight:400">&nbsp;· prices from Yahoo Finance (display only)</span>
</h3>
<table>
  <thead><tr>
    <th>Ticker</th><th>Strategy</th><th>Entry</th><th>Current</th>
    <th>Shares</th><th>Date</th><th>Hold</th><th>Unrealized P&amp;L</th>
  </tr></thead>
  <tbody>{rows}</tbody>
</table>"""


def _closed_html(closed: list[dict], this_week: list[dict], fx: float) -> str:
    def _rows(items: list[dict]) -> str:
        r = ""
        for c in items:
            col   = "#4ade80" if c["pnl_usd"] >= 0 else "#f87171"
            sign  = "+" if c["pnl_usd"] >= 0 else ""
            pnl_s = c["pnl_usd"] * fx
            apx   = " ~" if c.get("exit_approx") else ""
            strat = (c["strategy"] or "blend").title()
            r += (
                f"<tr>"
                f"<td style='font-weight:700'>{c['symbol']}</td>"
                f"<td style='color:#94a3b8'>{strat}</td>"
                f"<td style='color:#94a3b8'>${c['entry_price']:.2f}</td>"
                f"<td style='color:#94a3b8'>{apx}${c['exit_price']:.2f}</td>"
                f"<td style='color:#94a3b8'>{int(c['qty'])}</td>"
                f"<td style='color:#94a3b8'>{c['exit_date']}</td>"
                f"<td style='color:#94a3b8'>{c['days_held']}d</td>"
                f"<td style='color:{col};font-weight:700'>{sign}{pnl_s:,.0f} SEK</td>"
                f"</tr>"
            )
        return r

    header = "<thead><tr><th>Ticker</th><th>Strategy</th><th>Entry</th><th>Exit</th><th>Shares</th><th>Date</th><th>Hold</th><th>Realized P&amp;L</th></tr></thead>"

    week_html = ""
    if this_week:
        week_html = f"""
<h3 style="color:#f1f5f9;font-size:15px;margin:20px 0 8px">
  Closed This Week ({len(this_week)})
</h3>
<table>{header}<tbody>{_rows(this_week)}</tbody></table>"""
    else:
        week_html = "<p style='color:#64748b;margin-top:8px'>No positions closed this week.</p>"

    all_html = ""
    if closed:
        all_html = f"""
<h3 style="color:#f1f5f9;font-size:15px;margin:24px 0 8px">
  All Closed Trades ({len(closed)})
</h3>
<table>{header}<tbody>{_rows(closed)}</tbody></table>"""

    return week_html + all_html


# ── Main ──────────────────────────────────────────────────────────────────────

def _build_and_send() -> bool:
    from atos.notifier import _send, _wrap

    trades   = _load_trades()
    open_pos, closed = _classify(trades)

    today   = date.today()
    week_ago = today - timedelta(days=7)
    this_week = [c for c in closed if c.get("exit_date", "") >= week_ago.isoformat()]

    # Prices
    syms     = [p["symbol"] for p in open_pos]
    prices   = _fetch_prices(syms)
    fx       = _fetch_usdsek()

    # P&L summary
    realized_usd   = sum(c["pnl_usd"] for c in closed)
    unrealized_usd = sum((prices.get(p["symbol"], p["entry_price"]) - p["entry_price"]) * p["qty"]
                         for p in open_pos)
    week_usd       = sum(c["pnl_usd"] for c in this_week)
    realized_sek   = realized_usd   * fx
    week_sek       = week_usd       * fx
    total_sek      = _START_CAP_SEK + realized_sek + unrealized_usd * fx

    pnl_col  = "#4ade80" if week_sek >= 0 else "#f87171"
    pnl_sign = "+" if week_sek >= 0 else ""
    tot_col  = "#4ade80" if total_sek >= _START_CAP_SEK else "#f87171"
    ret_pct  = (total_sek - _START_CAP_SEK) / _START_CAP_SEK * 100
    ret_sign = "+" if ret_pct >= 0 else ""

    # Strategy P&L
    by_strat: dict[str, float] = {}
    for c in closed:
        s = (c["strategy"] or "blend").lower()
        by_strat[s] = by_strat.get(s, 0) + c["pnl_usd"]

    strat_wins: dict[str, int] = {}
    strat_n:    dict[str, int] = {}
    for c in closed:
        s = (c["strategy"] or "blend").lower()
        strat_n[s]    = strat_n.get(s, 0) + 1
        strat_wins[s] = strat_wins.get(s, 0) + (1 if c["pnl_usd"] > 0 else 0)

    # Strategy stats table
    strat_rows = ""
    for s in sorted(by_strat):
        n    = strat_n.get(s, 0)
        wins = strat_wins.get(s, 0)
        wr   = wins / n * 100 if n else 0
        pnl  = by_strat[s]
        col  = "#4ade80" if pnl >= 0 else "#f87171"
        sign = "+" if pnl >= 0 else ""
        strat_rows += (
            f"<tr>"
            f"<td style='font-weight:700'>{s.replace('_', ' ').title()}</td>"
            f"<td style='color:#94a3b8'>{n}</td>"
            f"<td style='color:#94a3b8'>{wr:.0f}%</td>"
            f"<td style='color:{col};font-weight:700'>{sign}${pnl:,.2f} ({sign}{pnl*fx:,.0f} SEK)</td>"
            f"</tr>"
        )

    strat_table_html = ""
    if strat_rows:
        strat_table_html = f"""
<h3 style="color:#f1f5f9;font-size:15px;margin:20px 0 8px">Strategy Performance (All-Time)</h3>
<table>
  <thead><tr><th>Strategy</th><th>Trades</th><th>Win Rate</th><th>Realized P&amp;L</th></tr></thead>
  <tbody>{strat_rows}</tbody>
</table>"""

    body = f"""
<div class="metric-row">
  <div class="metric">
    <div class="label">Est. Balance</div>
    <div class="value" style="color:{tot_col}">{total_sek:,.0f} SEK</div>
  </div>
  <div class="metric">
    <div class="label">Week P&amp;L</div>
    <div class="value" style="color:{pnl_col}">{pnl_sign}{week_sek:,.0f} SEK</div>
  </div>
  <div class="metric">
    <div class="label">Total Return</div>
    <div class="value" style="color:{tot_col}">{ret_sign}{ret_pct:.1f}%</div>
  </div>
  <div class="metric">
    <div class="label">Open Positions</div>
    <div class="value">{len(open_pos)}</div>
  </div>
  <div class="metric">
    <div class="label">Closed Trades</div>
    <div class="value">{len(closed)}</div>
  </div>
</div>

<h3 style="color:#f1f5f9;font-size:15px;margin:20px 0 8px">Account Equity Breakdown</h3>
{_equity_bar(realized_usd, unrealized_usd, fx)}

{strat_table_html}

<h3 style="color:#f1f5f9;font-size:15px;margin:20px 0 8px">All-Time P&amp;L by Strategy</h3>
{_strategy_bars(by_strat)}

<h3 style="color:#f1f5f9;font-size:15px;margin:20px 0 8px">Realized P&amp;L per Ticker</h3>
{_ticker_bars(closed)}

{_open_positions_html(open_pos, prices, fx)}
{_closed_html(closed, this_week, fx)}

<p class="muted" style="margin-top:16px">
  Account: U28013794 &nbsp;·&nbsp;
  Starting capital: {_START_CAP_SEK:,.0f} SEK &nbsp;·&nbsp;
  FX rate used: 1 USD = {fx:.3f} SEK &nbsp;·&nbsp;
  Open-position prices via Yahoo Finance (display only) &nbsp;·&nbsp;
  Next report: next Saturday 21:00 PKT
</p>
"""

    subject = (
        f"[IBKR LIVE] Weekly Report — "
        f"Est. {total_sek:,.0f} SEK  |  "
        f"Week: {pnl_sign}{week_sek:,.0f} SEK  |  "
        f"Open: {len(open_pos)}  [{today}]"
    )
    return _send(subject, _wrap("IBKR LIVE — Weekly Portfolio Report", body), live=True)


if __name__ == "__main__":
    ok = _build_and_send()
    sys.exit(0 if ok else 1)
