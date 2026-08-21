"""
daily_chart.py
---------------
Generates a daily per-strategy performance chart for any (or all) of the 4
trading modules — stock, etf, futures, forex — from the unified
data/pnl_ledger.db (pnl_tracker.py).

Two panels per module:
  1. Cumulative realized P&L per strategy over time (one line per strategy)
  2. Today's realized P&L per strategy (bar chart)

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

    fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(13, 15), facecolor="#0d1117")
    for ax in (ax1, ax2, ax3):
        ax.set_facecolor("#0d1117")
        ax.tick_params(colors="#8b949e")
        for spine in ax.spines.values():
            spine.set_color("#30363d")
        ax.grid(True, color="#21262d", linewidth=0.6)

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
        ax1.plot(dates, cum, marker="o", markersize=4, label=strat, color=colors[strat], linewidth=1.8)

    ax1.set_title(f"{title} — Cumulative Realized P&L per Strategy (through {today.isoformat()})",
                  color="#e6edf3", fontsize=13, pad=12)
    ax1.set_ylabel("Cumulative P&L", color="#c9d1d9")
    ax1.axhline(0, color="#484f58", linewidth=0.8)
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    ax1.legend(loc="upper left", facecolor="#161b22", edgecolor="#30363d",
              labelcolor="#c9d1d9", fontsize=8, ncol=2)

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
    pair_stats = pnl_tracker.get_pair_summary(module)
    if pair_stats:
        pair_stats = sorted(pair_stats, key=lambda r: r["total_pnl"], reverse=True)
        p_labels = [r["symbol"] for r in pair_stats]
        p_values = [r["total_pnl"] for r in pair_stats]
        p_colors = ["#3fb950" if v >= 0 else "#f85149" for v in p_values]
        y_pos = range(len(p_labels))
        ax3.barh(list(y_pos), p_values, color=p_colors)
        ax3.set_yticks(list(y_pos))
        ax3.set_yticklabels(p_labels, color="#c9d1d9", fontsize=9)
        ax3.invert_yaxis()   # best at top
        ax3.axvline(0, color="#484f58", linewidth=0.8)
        ax3.set_title(f"Total Realized P&L by Symbol (through {today.isoformat()})",
                      color="#e6edf3", fontsize=13, pad=12)
        ax3.set_xlabel("Total P&L", color="#c9d1d9")
    else:
        ax3.text(0.5, 0.5, "No closed trades yet", ha="center", va="center",
                 color="#8b949e", fontsize=12, transform=ax3.transAxes)
        ax3.set_title("Total Realized P&L by Symbol", color="#e6edf3", fontsize=13, pad=12)

    fig.tight_layout()

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
