"""
daily_chart.py
---------------
Generates a daily per-strategy performance chart for any (or all) of the 4
trading modules — stock, etf, futures, forex — from the unified
data/pnl_ledger.db (pnl_tracker.py).

Header strip: total realized P&L, profit factor, win rate, closed/open counts,
best/worst trade — pulled from the same pnl_tracker.get_summary() the
PowerShell dashboard uses, so this number can never drift from the live one.

Four panels per module:
  1. Cumulative realized P&L per strategy over time (one line per strategy;
     strategies with open-but-unclosed positions are flagged in the legend
     so a flat line doesn't get misread as "no profit" when it really means
     "nothing has closed yet")
  2. Today's realized P&L per strategy (bar chart)
  3. Total realized P&L by symbol, each bar annotated with its own profit
     factor and win/loss count
  4. Average (and max) trade notional size by symbol, across open + closed
     trades, in the module's native currency

Saves both a dated file (data/charts/{module}_strategy_YYYY-MM-DD.png, a
permanent daily record) and an overwritten data/charts/{module}_strategy_latest.png.
Then emails all of today's dated charts as attachments (config/email.json —
same credentials every other notifier in this repo uses; silently skips
sending if that file is missing).

Usage:
    python daily_chart.py                # all 4 modules
    python daily_chart.py --module forex # one module only
    python daily_chart.py --no-email     # generate only, skip the email
"""

import argparse
import json
import os
import smtplib
import sys
from datetime import date, datetime
from collections import defaultdict
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
CHARTS_DIR = os.path.join(BASE_DIR, "data", "charts")
EMAIL_CFG  = os.path.join(BASE_DIR, "config", "email.json")

sys.path.insert(0, BASE_DIR)
import pnl_tracker

MODULE_TITLES = {
    "stock":   "Stocks (US Blend / ATOS)",
    "etf":     "ETF Rotation",
    "futures": "Futures",
    "forex":   "Forex",
}

# Rotating color palette — enough distinct colors for any module's strategy
# count without needing a hand-maintained per-strategy table.
_PALETTE = ["#3fb950", "#58a6ff", "#f0883e", "#a371f7", "#d29922", "#79c0ff",
            "#ff7b72", "#39d353", "#e3b341", "#f778ba", "#56d4dd", "#8b949e"]


def _fetch_closed_trades(module: str) -> list:
    with pnl_tracker._conn() as c:
        rows = c.execute("""
            SELECT strategy, symbol, realized_pnl, timestamp_close
              FROM trades
             WHERE module=? AND status='closed' AND timestamp_close IS NOT NULL
             ORDER BY timestamp_close
        """, (module,)).fetchall()
    return [dict(r) for r in rows]


def generate(module: str, out_dir: str = CHARTS_DIR) -> str | None:
    import matplotlib
    matplotlib.use("Agg")   # headless — no display needed, scheduled task safe
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    os.makedirs(out_dir, exist_ok=True)
    trades = _fetch_closed_trades(module)
    today  = date.today()
    title  = MODULE_TITLES.get(module, module.title())

    if not trades:
        print(f"[{module}] no closed trades yet — skipping chart")
        return None

    daily_by_strat: dict = defaultdict(lambda: defaultdict(float))
    for t in trades:
        strat = t["strategy"] or "unknown"
        d     = t["timestamp_close"][:10]
        daily_by_strat[strat][d] += t["realized_pnl"] or 0.0

    # Open-position counts per strategy — the cumulative panel only reflects
    # REALIZED P&L. A strategy with no recent closes shows flat even while
    # sitting on real (unrealized) gains or losses in open positions, which
    # reads as "no profit" if you don't know that distinction. Not fetching
    # live prices here to compute actual unrealized P&L — this script runs
    # unattended nightly, and every extra live API call this session has
    # been a source of its own bugs — so this is an honest count, not a
    # number that could itself be wrong.
    with pnl_tracker._conn() as c:
        open_counts = defaultdict(int)
        for r in c.execute("SELECT strategy, COUNT(*) n FROM trades WHERE module=? "
                           "AND status='open' GROUP BY strategy", (module,)):
            open_counts[r["strategy"] or "unknown"] = r["n"]

    # Module-level stats for the header strip — pulled from the same
    # get_summary() the PowerShell dashboard uses, so the number on this
    # chart can never silently drift from the number the user checks live.
    mod_summary = pnl_tracker.get_summary(module).get(module, {})

    fig, (ax1, ax2, ax3, ax4) = plt.subplots(4, 1, figsize=(13, 23), facecolor="#0d1117")
    for ax in (ax1, ax2, ax3, ax4):
        ax.set_facecolor("#0d1117")
        ax.tick_params(colors="#8b949e")
        for spine in ax.spines.values():
            spine.set_color("#30363d")
        ax.grid(True, color="#21262d", linewidth=0.6)

    # forex realized_pnl is stored in the Saxo SIM account's actual base
    # currency (EUR — see forex/runner.py's _equity_in_quote / cap.forex_risk_equity_eur,
    # "SIM demo credit ~945,000 EUR"), not USD. etf/futures trade and settle in USD.
    cur_label = "SEK" if module == "stock" else "EUR (base)" if module == "forex" else "USD"
    pf_disp   = mod_summary.get("profit_factor")
    pf_str    = f"{pf_disp:.2f}" if pf_disp is not None else "N/A (no losing trades yet)"
    header_l1 = (
        f"REALIZED P&L: {mod_summary.get('realized_pnl', 0):+,.2f} {cur_label}   |   "
        f"PROFIT FACTOR: {pf_str}   |   "
        f"WIN RATE: {mod_summary.get('win_rate', 0):.1f}%"
    )
    header_l2 = (
        f"CLOSED: {mod_summary.get('closed_trades', 0)}   |   "
        f"OPEN: {mod_summary.get('open_trades', 0)}   |   "
        f"BEST: {mod_summary.get('best_trade', 0):+,.2f}   |   "
        f"WORST: {mod_summary.get('worst_trade', 0):+,.2f}"
    )
    fig.suptitle(f"{title} — Daily Performance Report ({today.isoformat()})",
                 color="#e6edf3", fontsize=17, fontweight="bold", y=0.997)
    hdr_color = "#3fb950" if mod_summary.get("realized_pnl", 0) >= 0 else "#f85149"
    box = plt.Rectangle((0.02, 0.964), 0.96, 0.026, transform=fig.transFigure,
                        facecolor="#161b22", edgecolor="#30363d", linewidth=1, zorder=1)
    stripe = plt.Rectangle((0, 0.962), 1, 0.003, transform=fig.transFigure,
                           facecolor=hdr_color, linewidth=0, zorder=1)
    fig.patches.extend([box, stripe])
    fig.text(0.5, 0.981, header_l1, ha="center", color="#e6edf3", fontsize=11,
              family="monospace", fontweight="bold", zorder=2)
    fig.text(0.5, 0.973, header_l2, ha="center", color="#8b949e", fontsize=9.5,
              family="monospace", zorder=2)

    strategies = sorted(daily_by_strat.keys())
    colors = {s: _PALETTE[i % len(_PALETTE)] for i, s in enumerate(strategies)}

    today_dt = datetime.combine(today, datetime.min.time())
    for strat in strategies:
        days = sorted(daily_by_strat[strat].keys())
        if not days:
            continue
        dates = [datetime.strptime(d, "%Y-%m-%d") for d in days]
        cum, running = [], 0.0
        for d in days:
            running += daily_by_strat[strat][d]
            cum.append(running)
        # Extend flat to today if this strategy's last close wasn't today —
        # otherwise the line just stops mid-chart the moment a strategy goes
        # quiet, which reads as "data is missing" rather than "still at this
        # level, nothing has closed since." A real gap in history is a much
        # bigger deal than a strategy that simply hasn't traded recently.
        if dates[-1] < today_dt:
            dates.append(today_dt)
            cum.append(running)
        n_open = open_counts.get(strat, 0)
        label = f"{strat} ({n_open} open, not in this line)" if n_open else strat
        ax1.plot(dates, cum, marker="o", markersize=4, label=label, color=colors[strat], linewidth=1.8)

    ax1.set_title(f"{title} — Cumulative REALIZED P&L per Strategy (through {today.isoformat()})",
                  color="#e6edf3", fontsize=13, pad=12)
    ax1.set_ylabel("Cumulative P&L (realized only)", color="#c9d1d9")
    ax1.axhline(0, color="#484f58", linewidth=0.8)
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    ax1.legend(loc="upper left", facecolor="#161b22", edgecolor="#30363d",
              labelcolor="#c9d1d9", fontsize=8, ncol=2)
    ax1.text(0.5, -0.11, "A flat line means no NEW closed trades — it does not mean no open-position "
             "gains/losses. Check the live dashboard for current unrealized P&L.",
             ha="center", transform=ax1.transAxes, color="#8b949e", fontsize=8, style="italic")

    today_str = today.isoformat()
    today_pnl = {s: daily_by_strat[s].get(today_str, 0.0) for s in strategies}
    labels = strategies
    values = [today_pnl[s] for s in strategies]
    bar_colors = ["#3fb950" if v >= 0 else "#f85149" for v in values]

    ax2.bar(labels, values, color=bar_colors)
    ax2.axhline(0, color="#484f58", linewidth=0.8)
    ax2.set_title(f"Today's Realized P&L per Strategy ({today_str})", color="#e6edf3", fontsize=13, pad=12)
    ax2.set_ylabel("P&L", color="#c9d1d9")
    plt.setp(ax2.get_xticklabels(), rotation=30, ha="right", color="#c9d1d9")

    # ── Panel 3: total realized P&L by symbol/pair ("currency wise" for forex,
    # per-ticker for stocks/ETF, per-market for futures) — every module's
    # trades table uses the same "symbol" column, so this works uniformly.
    # Each bar is also annotated with that symbol's own profit factor so a
    # pair that's "green" on total P&L but only because of one outlier trade
    # is visible at a glance (PF close to 1 = fragile, not robust).
    pair_stats = pnl_tracker.get_pair_summary(module)
    if pair_stats:
        pair_stats = sorted(pair_stats, key=lambda r: r["total_pnl"], reverse=True)
        p_labels = [r["symbol"] for r in pair_stats]
        p_values = [r["total_pnl"] for r in pair_stats]
        p_colors = ["#3fb950" if v >= 0 else "#f85149" for v in p_values]
        y_pos = list(range(len(p_labels)))
        bars = ax3.barh(y_pos, p_values, color=p_colors)
        ax3.set_yticks(y_pos)
        ax3.set_yticklabels(p_labels, color="#c9d1d9", fontsize=9)
        ax3.invert_yaxis()   # best at top
        ax3.axvline(0, color="#484f58", linewidth=0.8)
        ax3.set_title(f"Total Realized P&L by Symbol — with Profit Factor (through {today.isoformat()})",
                      color="#e6edf3", fontsize=13, pad=12)
        ax3.set_xlabel("Total P&L", color="#c9d1d9")
        xmax = max((abs(v) for v in p_values), default=1) or 1
        ax3.set_xlim(min(0, min(p_values)) - xmax * 0.18, max(0, max(p_values)) + xmax * 0.18)
        for bar, r in zip(bars, pair_stats):
            pf = r["profit_factor"]
            pf_txt = f"PF {pf:.2f}" if pf is not None else "PF —"
            wl_txt = f"{r['wins']}W/{r['losses']}L"
            w = bar.get_width()
            x = w + (xmax * 0.02 if w >= 0 else -xmax * 0.02)
            ha = "left" if w >= 0 else "right"
            ax3.text(x, bar.get_y() + bar.get_height() / 2, f"{pf_txt}  ({wl_txt})",
                     va="center", ha=ha, color="#8b949e", fontsize=7.5)
    else:
        ax3.text(0.5, 0.5, "No closed trades yet", ha="center", va="center",
                 color="#8b949e", fontsize=12, transform=ax3.transAxes)
        ax3.set_title("Total Realized P&L by Symbol", color="#e6edf3", fontsize=13, pad=12)

    # ── Panel 4: trade size by symbol — across BOTH open and closed trades,
    # since position sizing is what the user asked to see ("how much a
    # currency pair trade"), not just closed history.
    #
    # FOREX IS SPECIAL-CASED. quantity is units of the pair's BASE currency
    # (Saxo FxSpot "Amount" convention) and entry_price is quote-currency
    # price per 1 base unit — quantity*entry_price is therefore a notional in
    # the QUOTE currency, which is wildly different in magnitude pair to pair
    # (a JPY-quoted pair's price is ~150-190 vs ~0.6-1.6 for a USD/EUR/GBP-
    # quoted pair). An earlier version of this panel multiplied them anyway
    # and put every symbol on one shared "USD" axis — EURJPY showed as a
    # ~32,000,000 "USD" trade next to EURUSD's ~1,500,000, off by the JPY
    # price ratio, not real size. Converting properly needs a live FX rate
    # per pair, which this unattended nightly script deliberately avoids
    # (see the open_counts comment above). So for forex this shows raw
    # QUANTITY — units of the pair's own base currency, labeled per bar —
    # which is honest and directly comparable to within the real value ratio
    # between major currencies (~2x), not a fabricated 100x+ distortion.
    if module == "forex":
        with pnl_tracker._conn() as c:
            size_rows = c.execute("""
                SELECT symbol, AVG(quantity) AS avg_units, MAX(quantity) AS max_units,
                       COUNT(*) AS n
                  FROM trades
                 WHERE module=? AND quantity IS NOT NULL
                 GROUP BY symbol
                 ORDER BY avg_units DESC
            """, (module,)).fetchall()
    else:
        with pnl_tracker._conn() as c:
            size_rows = c.execute("""
                SELECT symbol, AVG(quantity * entry_price) AS avg_units,
                       MAX(quantity * entry_price) AS max_units, COUNT(*) AS n
                  FROM trades
                 WHERE module=? AND quantity IS NOT NULL AND entry_price IS NOT NULL
                 GROUP BY symbol
                 ORDER BY avg_units DESC
            """, (module,)).fetchall()

    if size_rows:
        s_labels, s_avg, s_max = [], [], []
        for r in size_rows:
            lbl = r["symbol"]
            if module == "forex" and len(lbl) >= 6:
                lbl = f"{lbl} ({lbl[:3]} units)"
            s_labels.append(lbl)
            s_avg.append(r["avg_units"] or 0.0)
            s_max.append(r["max_units"] or 0.0)
        y_pos4 = list(range(len(s_labels)))
        ax4.barh(y_pos4, s_avg, color="#58a6ff", label="Average")
        ax4.barh(y_pos4, s_max, color="#58a6ff", alpha=0.25, label="Max")
        ax4.set_yticks(y_pos4)
        ax4.set_yticklabels(s_labels, color="#c9d1d9", fontsize=8)
        ax4.invert_yaxis()   # largest at top
        if module == "forex":
            ax4.set_title("Average Position Size by Pair — Units of the Pair's OWN Base "
                          "Currency (bars NOT directly comparable across pairs)",
                          color="#e6edf3", fontsize=12.5, pad=12)
            ax4.set_xlabel("Units traded (base currency of that pair — see label)", color="#c9d1d9")
        else:
            ax4.set_title(f"Average Trade Size (Notional = Qty × Entry Price) by Symbol — "
                          f"{cur_label}, all trades incl. open", color="#e6edf3", fontsize=13, pad=12)
            ax4.set_xlabel(f"Notional value ({cur_label})", color="#c9d1d9")
        ax4.legend(loc="lower right", facecolor="#161b22", edgecolor="#30363d",
                  labelcolor="#c9d1d9", fontsize=8)
    else:
        ax4.text(0.5, 0.5, "No trades yet", ha="center", va="center",
                 color="#8b949e", fontsize=12, transform=ax4.transAxes)
        ax4.set_title("Average Trade Size by Symbol", color="#e6edf3", fontsize=13, pad=12)

    fig.tight_layout(rect=[0, 0, 1, 0.955])

    dated_path  = os.path.join(out_dir, f"{module}_strategy_{today_str}.png")
    latest_path = os.path.join(out_dir, f"{module}_strategy_latest.png")
    fig.savefig(dated_path, dpi=110, facecolor=fig.get_facecolor())
    fig.savefig(latest_path, dpi=110, facecolor=fig.get_facecolor())
    plt.close(fig)

    print(f"[{module}] saved: {dated_path}")
    print(f"[{module}] saved: {latest_path}")
    return dated_path


def _email_charts(chart_paths: dict) -> None:
    """chart_paths: {module: dated_png_path}. Skips silently if
    config/email.json is missing — same convention as every other notifier
    in this repo, so a missing config never breaks the actual scheduled run."""
    if not chart_paths:
        print("[email] no charts generated today — skipping email")
        return
    if not os.path.exists(EMAIL_CFG):
        print("[email] no config/email.json — skipping email")
        return
    try:
        with open(EMAIL_CFG) as f:
            cfg = json.load(f)
    except Exception as exc:
        print(f"[email] could not read config/email.json: {exc}")
        return

    today = date.today().isoformat()
    modules_str = ", ".join(m.upper() for m in sorted(chart_paths))

    rows = "".join(
        f'<div style="margin:16px 0"><h3 style="color:#e6edf3;font-family:sans-serif">'
        f'{MODULE_TITLES.get(m, m.title())}</h3>'
        f'<img src="cid:{m}" style="max-width:100%;border-radius:6px"></div>'
        for m in sorted(chart_paths)
    )
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,sans-serif;background:#0d1117;color:#e6edf3;padding:20px">
<div style="max-width:900px;margin:0 auto;background:#161b22;border-radius:10px;
            border-top:3px solid #3fb950;padding:20px 24px">
<h2 style="color:#3fb950;margin:0 0 4px">Daily Strategy Performance — {today}</h2>
<div style="color:#8b949e;font-size:12px;margin-bottom:8px">{modules_str}</div>
{rows}
<hr style="border:none;border-top:1px solid #21262d;margin:16px 0">
<div style="color:#484f58;font-size:11px">ATOS Daily Chart · runs 23:15 PKT daily, after PnL Sync</div>
</div></body></html>"""

    try:
        msg = MIMEMultipart("related")
        msg["Subject"] = f"Daily Strategy Charts — {today} ({modules_str})"
        msg["From"]    = f"ATOS Daily Chart <{cfg['sender_email']}>"
        msg["To"]      = cfg["recipient_email"]
        msg.attach(MIMEText(html, "html"))
        for m, path in chart_paths.items():
            with open(path, "rb") as f:
                img = MIMEImage(f.read())
            img.add_header("Content-ID", f"<{m}>")
            img.add_header("Content-Disposition", "inline", filename=os.path.basename(path))
            msg.attach(img)
        with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"]) as s:
            s.starttls()
            s.login(cfg["sender_email"], cfg["sender_password"])
            s.sendmail(cfg["sender_email"], cfg["recipient_email"], msg.as_string())
        print(f"[email] sent daily chart email for {modules_str}")
    except Exception as exc:
        print(f"[email] FAILED to send: {exc}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--module", choices=list(pnl_tracker.MODULES) + ["all"], default="all")
    ap.add_argument("--no-email", action="store_true", help="generate charts only, skip the email")
    args = ap.parse_args()
    modules = list(pnl_tracker.MODULES) if args.module == "all" else [args.module]
    chart_paths = {}
    for m in modules:
        try:
            path = generate(m)
            if path:
                chart_paths[m] = path
        except Exception as exc:
            print(f"[{m}] chart generation FAILED: {exc}")
    if not args.no_email:
        _email_charts(chart_paths)


if __name__ == "__main__":
    main()
