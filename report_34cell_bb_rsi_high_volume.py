"""
report_34cell_bb_rsi_high_volume.py
-------------------------------------
Read-only economics report for the 34-cell matrix (bb + rsi strategies x
the 17-pair HIGH_VOLUME_SYMBOLS universe) that the two real-money LIVE
accounts (SEK=bb, EUR=rsi) are now built around -- see
forex_live_two_account_redesign_2026-08-28.md session notes.

Explicit user spec (2026-08-28): per cell, show Trades / Wins-Losses / WR /
Gross P&L / Saxo costs / Net P&L / Profit Factor / Average net R / Median
holding days / Best-Worst trade / Cost-to-gross-profit ratio / cost-gate
pass-block counts. Historical (pre-2026-08-27, before forward_observation
logging existed) and forward (2026-08-27+, richer observation-card schema)
periods are reported SEPARATELY and never merged -- explicit user
instruction, since the newer cards record measurements the old ledger rows
never captured.

Decision hierarchy this report is built to support (explicit user
instruction, NOT WR-first): Net P&L after real costs -> Net R -> Profit
Factor -> sample size -> WR. A high-WR cell with poor net-R/cost economics
can be worse than a lower-WR cell with large winners.

Data sources:
  - pnl_tracker's SQLite ledger (module="forex"), for the historical
    period. Forex trades store gross==net in this ledger (commission is
    always 0 for module="forex" -- calc_commission()'s own "forex spread-
    embedded" no-op), so "Gross P&L" and "Net P&L" are IDENTICAL for every
    historical row -- not a bug in this report, a real limitation of what
    was recorded before 2026-08-27. Flagged explicitly, not hidden.
  - data/trade_observation_cards.jsonl + data/cost_gate_decisions.jsonl,
    for the forward period -- the richer schema with real
    gross/commission/net/R/holding-hours per trade and PASS/BLOCKED
    cost-gate counts.

KNOWN DATA-QUALITY EXCLUSION (historical period only): intraday_monitor.py
had a real P&L bug (fixed 2026-08-28, see
test_2026_08_28_intraday_monitor_pnl_bug.py) where stop/TP fills it closed
were logged with unconverted quote-currency P&L and zero Saxo cost netting
-- identifiable by their exit_reason ("STOP-LOSS hit @.../TAKE-PROFIT hit
@...", distinct from should_exit()'s own hard_stop/time_stop/rsi_recovery/
etc. wording). Those rows are EXCLUDED from the historical aggregates
below (counted and reported separately, never silently included as if
trustworthy) -- the fix only prevents this going forward, it does not
retroactively correct historical ledger rows.

Usage:
    python report_34cell_bb_rsi_high_volume.py
"""

from __future__ import annotations

import json
import os
import statistics
import sys
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

import pnl_tracker
from forex.universe import HIGH_VOLUME_SYMBOLS

GR, RD, YL, CY, DM, W, BD = (
    "\033[92m", "\033[91m", "\033[93m", "\033[96m", "\033[2m", "\033[0m", "\033[1m"
)

STRATEGIES = ("bb", "rsi")
PAIRS      = sorted(HIGH_VOLUME_SYMBOLS)
FORWARD_CUTOFF = "2026-08-27"   # forward_observation.py started logging this day

DATA_DIR         = os.path.join(BASE_DIR, "data")
TRADE_CARDS_LOG  = os.path.join(DATA_DIR, "trade_observation_cards.jsonl")
COST_GATE_LOG    = os.path.join(DATA_DIR, "cost_gate_decisions.jsonl")

_BUGGY_INTRADAY_MONITOR_PREFIXES = ("STOP-LOSS hit @", "TAKE-PROFIT hit @")


# ── Shared cell-stat helpers ────────────────────────────────────────────

def _empty_cell() -> dict:
    return {
        "trades": 0, "wins": 0, "losses": 0,
        "gross": [], "cost": [], "net": [], "r": [], "hold_days": [],
        "excluded_buggy": 0, "gate_pass": 0, "gate_blocked": 0,
    }


def _finalize(cell: dict) -> dict:
    n = cell["trades"]
    wins, losses = cell["wins"], cell["losses"]
    net = cell["net"]
    gross_sum = sum(cell["gross"]) if cell["gross"] else None
    cost_sum  = sum(cell["cost"])  if cell["cost"]  else None
    net_sum   = sum(net) if net else None
    win_sum   = sum(x for x in net if x > 0)
    loss_sum  = abs(sum(x for x in net if x < 0))
    pf = (win_sum / loss_sum) if loss_sum > 0 else (float("inf") if win_sum > 0 else None)
    r_vals = [x for x in cell["r"] if x is not None]
    avg_r = statistics.mean(r_vals) if r_vals else None
    hold  = [x for x in cell["hold_days"] if x is not None]
    med_hold = statistics.median(hold) if hold else None
    cost_pct = (abs(cost_sum) / gross_sum * 100) if (gross_sum and cost_sum is not None and gross_sum != 0) else None
    return {
        "trades": n, "wins": wins, "losses": losses,
        "wr": (wins / n * 100) if n else None,
        "gross_sum": gross_sum, "cost_sum": cost_sum, "net_sum": net_sum,
        "pf": pf, "avg_r": avg_r, "med_hold_days": med_hold,
        "best": max(net) if net else None, "worst": min(net) if net else None,
        "cost_pct_of_gross": cost_pct,
        "excluded_buggy": cell["excluded_buggy"],
        "gate_pass": cell["gate_pass"], "gate_blocked": cell["gate_blocked"],
    }


# ── Historical period (pre-2026-08-27, pnl_tracker ledger) ─────────────

def _load_historical() -> dict:
    cells = {(s, p): _empty_cell() for s in STRATEGIES for p in PAIRS}
    trades = pnl_tracker.get_closed_trades(module="forex", limit=100_000)
    for t in trades:
        strat, sym = t.get("strategy"), t.get("symbol")
        if strat not in STRATEGIES or sym not in HIGH_VOLUME_SYMBOLS:
            continue
        tclose = t.get("timestamp_close") or ""
        if tclose >= FORWARD_CUTOFF:
            continue   # belongs to the forward period below, not here
        cell = cells[(strat, sym)]
        reason = t.get("exit_reason") or ""
        if reason.startswith(_BUGGY_INTRADAY_MONITOR_PREFIXES):
            cell["excluded_buggy"] += 1
            continue
        net = t.get("realized_pnl")
        if net is None:
            continue
        cell["trades"] += 1
        if net > 0:
            cell["wins"] += 1
        elif net < 0:
            cell["losses"] += 1
        # Forex ledger rows store gross==net (commission is always 0 for
        # module="forex" -- see this file's own docstring) -- both recorded
        # identically, not a computation choice made here.
        cell["gross"].append(net)
        cell["cost"].append(0.0)
        cell["net"].append(net)

        ep, xp, qty = t.get("entry_price"), t.get("exit_price"), t.get("quantity")
        sp, direction = t.get("stop_price"), t.get("direction")
        r_val = None
        if ep and xp and qty and sp:
            raw = (xp - ep) if direction in ("Buy", "BUY") else (ep - xp)
            if raw != 0:
                implied_fx_rate = net / (raw * qty)
                risk_eur = abs(ep - sp) * qty * implied_fx_rate
                if risk_eur and risk_eur > 0:
                    r_val = net / risk_eur
        cell["r"].append(r_val)

        topen, tclose_ts = t.get("timestamp_open"), t.get("timestamp_close")
        hold_days = None
        for fmt_pair in ((topen, tclose_ts),):
            try:
                d_open  = datetime.fromisoformat(topen)
                d_close = datetime.fromisoformat(tclose_ts)
                hold_days = (d_close - d_open).total_seconds() / 86400
            except Exception:
                hold_days = None
        cell["hold_days"].append(hold_days)
    return {k: _finalize(v) for k, v in cells.items()}


# ── Forward period (2026-08-27+, forward_observation.py cards) ─────────

def _load_jsonl(path: str) -> list:
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def _load_forward() -> tuple[dict, int]:
    cells = {(s, p): _empty_cell() for s in STRATEGIES for p in PAIRS}
    cards = _load_jsonl(TRADE_CARDS_LOG)
    entries = {c["card_id"]: c for c in cards if c.get("event") == "entry"}
    exits   = [c for c in cards if c.get("event") == "exit"]
    still_open = 0
    for exit_card in exits:
        entry_card = entries.get(exit_card.get("card_id"))
        if entry_card is None:
            continue
        strat, sym = entry_card.get("strategy"), entry_card.get("symbol")
        if strat not in STRATEGIES or sym not in HIGH_VOLUME_SYMBOLS:
            continue
        cell = cells[(strat, sym)]
        net = exit_card.get("net_pnl_eur")
        if net is None:
            continue
        cell["trades"] += 1
        if net > 0:
            cell["wins"] += 1
        elif net < 0:
            cell["losses"] += 1
        cell["gross"].append(exit_card.get("gross_pnl_eur") or 0.0)
        cell["cost"].append(exit_card.get("commission_eur") or 0.0)
        cell["net"].append(net)
        cell["r"].append(exit_card.get("r_multiple"))
        hh = exit_card.get("holding_hours")
        cell["hold_days"].append(hh / 24 if hh is not None else None)
    entry_ids_with_exit = {c.get("card_id") for c in exits}
    for entry_card in entries.values():
        strat, sym = entry_card.get("strategy"), entry_card.get("symbol")
        if strat in STRATEGIES and sym in HIGH_VOLUME_SYMBOLS and entry_card["card_id"] not in entry_ids_with_exit:
            still_open += 1

    gate_rows = _load_jsonl(COST_GATE_LOG)
    for g in gate_rows:
        strat, sym = g.get("strategy"), g.get("symbol")
        if strat not in STRATEGIES or sym not in HIGH_VOLUME_SYMBOLS:
            continue
        cell = cells[(strat, sym)]
        if g.get("decision") == "PASS":
            cell["gate_pass"] += 1
        elif g.get("decision") == "BLOCKED":
            cell["gate_blocked"] += 1
    return {k: _finalize(v) for k, v in cells.items()}, still_open


# ── Rendering ────────────────────────────────────────────────────────────

def _fmt(v, spec="{:.2f}", none="—"):
    return none if v is None else spec.format(v)


def _render_section(title: str, cells: dict, note: str = "") -> None:
    print(f"\n{BD}{CY}{'='*118}{W}")
    print(f"{BD}{CY}  {title}{W}")
    if note:
        print(f"{DM}  {note}{W}")
    print(f"{BD}{CY}{'='*118}{W}")
    hdr = (f"{'Strat':<6}{'Pair':<9}{'N':>4}{'W/L':>7}{'WR%':>7}"
           f"{'Gross':>10}{'Cost':>9}{'Net':>10}{'PF':>7}"
           f"{'AvgR':>7}{'MedHold':>9}{'Best':>10}{'Worst':>10}{'Cost%':>8}{'Gate P/B':>10}")
    print(f"{DM}{hdr}{W}")
    print(f"{DM}{'-'*118}{W}")
    any_row = False
    for strat in STRATEGIES:
        for pair in PAIRS:
            c = cells[(strat, pair)]
            if c["trades"] == 0 and c["excluded_buggy"] == 0 and c["gate_pass"] == 0 and c["gate_blocked"] == 0:
                continue
            any_row = True
            wr_col = GR if (c["wr"] or 0) >= 50 else (RD if c["wr"] is not None else DM)
            net_col = GR if (c["net_sum"] or 0) >= 0 else RD
            pf_s = "inf" if c["pf"] == float("inf") else _fmt(c["pf"])
            row = (f"{strat:<6}{pair:<9}{c['trades']:>4}"
                   f"{str(c['wins'])+'/'+str(c['losses']):>7}"
                   f"{wr_col}{_fmt(c['wr'], '{:.0f}'):>6}{W}%"
                   f"{_fmt(c['gross_sum']):>10}{_fmt(c['cost_sum']):>9}"
                   f"{net_col}{_fmt(c['net_sum']):>10}{W}"
                   f"{pf_s:>7}{_fmt(c['avg_r']):>7}{_fmt(c['med_hold_days'], '{:.1f}'):>9}"
                   f"{_fmt(c['best']):>10}{_fmt(c['worst']):>10}"
                   f"{_fmt(c['cost_pct_of_gross'], '{:.1f}'):>7}%"
                   f"{str(c['gate_pass'])+'/'+str(c['gate_blocked']):>10}")
            print(row)
            if c["excluded_buggy"]:
                print(f"{DM}       ^ excluded {c['excluded_buggy']} trade(s) closed via the pre-2026-08-28 "
                      f"intraday_monitor.py P&L bug (unconverted/uncosted) -- not included above{W}")
    if not any_row:
        print(f"{DM}  (no trades, exclusions, or cost-gate decisions in this period for any of the 34 cells){W}")
    print(f"{DM}{'-'*118}{W}")


def main() -> None:
    print(f"{BD}{'#'*118}{W}")
    print(f"{BD}  34-CELL ECONOMICS REPORT -- bb + rsi x {len(PAIRS)} HIGH_VOLUME pairs "
          f"({len(STRATEGIES)*len(PAIRS)} cells)  |  generated {datetime.now():%Y-%m-%d %H:%M}{W}")
    print(f"{DM}  Decision hierarchy: Net P&L after real costs -> Net R -> Profit Factor -> "
          f"sample size -> WR (explicit user instruction -- WR alone must never drive a LIVE decision){W}")
    print(f"{BD}{'#'*118}{W}")

    hist = _load_historical()
    _render_section(
        "HISTORICAL PERIOD (pre-2026-08-27, pnl_tracker ledger)",
        hist,
        note=("Gross == Net for every row here: forex ledger trades store commission=0 always "
              "(calc_commission()'s 'forex spread-embedded' no-op) -- pre-2026-08-27 rows never "
              "separately recorded a true pre-cost gross figure. Cost/Cost% and Gate P/B are "
              "structurally N/A for this period (the cost gate didn't exist yet)."),
    )

    fwd, still_open = _load_forward()
    _render_section(
        f"FORWARD PERIOD ({FORWARD_CUTOFF}+, forward_observation.py cards)",
        fwd,
        note=(f"Richer per-trade schema (real gross/commission/net/R/holding-hours + cost-gate "
              f"PASS/BLOCKED counts). {still_open} bb/rsi HIGH_VOLUME position(s) currently open, "
              f"not yet counted in these closed-trade stats."),
    )

    print(f"\n{DM}Historical and forward periods are reported separately and must never be summed or "
          f"averaged together -- the forward cards record measurements (real per-trade cost, R-multiple, "
          f"MAE/MFE) the historical ledger rows never captured.{W}\n")


if __name__ == "__main__":
    main()
