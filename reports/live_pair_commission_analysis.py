"""
reports/live_pair_commission_analysis.py

For every pair ATOS can trade on the LIVE (SEK) account, pull the REAL
Saxo round-trip commission and compute -- at the current
RSI_LIVE_FIXED_RISK_EUR = 45 sizing -- the position's economics: notional,
commission as a % of it, what a full take-profit and a modest 0.5R bounce
net AFTER commission, and the smallest notional at which a thin RSI bounce
still clears cost. Purpose: never repeat MXNUSD (a +EUR2.13 price move
that netted -EUR3.05 once the flat -EUR5.19 commission hit).

Phase 1 (this file, main python -- needs forex.runner/torch): gather to
data/live_pair_commission_analysis.csv + .json.
Phase 2: `py -3.12 reports/live_pair_commission_analysis_xlsx.py` builds
data/live_pair_commission_analysis.xlsx from the json.

    python reports/live_pair_commission_analysis.py
"""
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import forex.runner as fr
import forex.strategy as strat
import forex.strategy_rsi as rsi
from forex.universe import (HIGH_VOLUME_SYMBOLS, CORE_SYMBOLS, EXOTIC_SYMBOLS,
                            METALS_SYMBOLS, get_pair)

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_CSV = os.path.join(BASE, "data", "live_pair_commission_analysis.csv")
OUT_JSON = os.path.join(BASE, "data", "live_pair_commission_analysis.json")

RISK_EUR = fr.RSI_LIVE_FIXED_RISK_EUR or 45.0
TP_RR = fr.DEFAULT_TP_RR                       # 2.0
STOP_MULT = rsi.ATR_STOP_MULT                  # 1.5
MIN_NOTIONAL_FLOOR = fr.MIN_LIVE_NOTIONAL_EUR  # 5,000 (the gate A rule)
# target: commission <= this fraction of notional (a ~0.3% RSI bounce then
# nets comfortably positive). Drives the "recommended min notional" column.
TARGET_COMM_PCT = 0.0008                       # 0.08%

# legacy pairs the LIVE accounts have actually held (outside HIGH_VOLUME)
LEGACY = ["MXNUSD", "GBPPLN", "EURPLN", "CHFAUD", "EURNOK", "AUDCHF", "EURDKK"]


def _tier(sym: str) -> str:
    if sym in HIGH_VOLUME_SYMBOLS:
        return "HIGH_VOLUME (live)"
    if sym in CORE_SYMBOLS:
        return "CORE"
    if sym in METALS_SYMBOLS:
        return "METALS"
    if sym in EXOTIC_SYMBOLS:
        return "EXOTIC"
    return "legacy/other"


def analyse(sym: str, akey: str) -> dict | None:
    try:
        pi = get_pair(sym)
    except Exception:
        return None
    uic = pi["uic"]
    min_units = int(pi.get("min_units", 1000))
    px = fr._live_price(uic, akey)
    q_ccy, b_ccy = sym[3:6], sym[:3]
    eur_per_q = fr._eur_per_unit(q_ccy, akey)
    eur_per_b = fr._eur_per_unit(b_ccy, akey)
    df = fr._fetch_history(uic)
    if not (px and eur_per_q and eur_per_b) or df is None or len(df) < 20:
        return {"pair": sym, "tier": _tier(sym), "note": "no price / rate / bars"}
    atr = float(strat._atr(df["High"], df["Low"], df["Close"]).iloc[-1])
    stop_dist = STOP_MULT * atr                                  # quote ccy
    stop_pct = stop_dist / px * 100

    # size at the EUR45 fixed-risk ceiling (same call the runner makes)
    units = rsi.size_position(0, atr, min_units=min_units, risk_amount=RISK_EUR / eur_per_q)
    # real Saxo round-trip commission for that size (falls back to a 1-lot
    # quote if the sized qty query fails)
    rt_cost_q = (fr._round_trip_cost_quote_ccy(uic, units or min_units, akey))
    rt_cost_eur = rt_cost_q * eur_per_q if rt_cost_q is not None else None

    R_eur = (units * stop_dist * eur_per_q) if units else None   # realised risk
    notional_eur = (units * eur_per_b) if units else None
    comm_pct = (rt_cost_eur / notional_eur * 100) if (rt_cost_eur and notional_eur) else None

    tp_gross_eur = (TP_RR * R_eur) if R_eur else None            # if TP (2R) hits
    tp_net_eur = (tp_gross_eur - rt_cost_eur) if (tp_gross_eur and rt_cost_eur) else None
    half_r_gross = (0.5 * R_eur) if R_eur else None              # a modest bounce
    half_r_net = (half_r_gross - rt_cost_eur) if (half_r_gross and rt_cost_eur) else None
    breakeven_r = (rt_cost_eur / R_eur) if (rt_cost_eur and R_eur) else None

    # recommended MIN notional: commission <= TARGET_COMM_PCT of it, and at
    # least the gate-A floor
    rec_notional = max(MIN_NOTIONAL_FLOOR,
                       (rt_cost_eur / TARGET_COMM_PCT) if rt_cost_eur else MIN_NOTIONAL_FLOOR)
    rec_units = int(round(rec_notional / eur_per_b / min_units)) * min_units if eur_per_b else None

    ok = (half_r_net is not None and half_r_net > 0
          and notional_eur is not None and notional_eur >= MIN_NOTIONAL_FLOOR)
    verdict = ("OK" if ok else
               "THIN — a 0.5R bounce barely clears cost" if (half_r_net is not None and half_r_net <= 5)
               else "BELOW €5k notional floor" if (notional_eur and notional_eur < MIN_NOTIONAL_FLOOR)
               else "review")

    return {
        "pair": sym, "tier": _tier(sym),
        "price": round(px, 5), "atr": round(atr, 6), "stop_pct": round(stop_pct, 3),
        "commission_eur_roundtrip": round(rt_cost_eur, 2) if rt_cost_eur else None,
        "units_at_E45": units,
        "notional_eur_at_E45": round(notional_eur) if notional_eur else None,
        "realised_risk_eur": round(R_eur, 1) if R_eur else None,
        "commission_pct_of_notional": round(comm_pct, 3) if comm_pct else None,
        "tp_2R_gross_eur": round(tp_gross_eur, 1) if tp_gross_eur else None,
        "tp_2R_net_after_comm_eur": round(tp_net_eur, 1) if tp_net_eur else None,
        "bounce_0p5R_gross_eur": round(half_r_gross, 1) if half_r_gross else None,
        "bounce_0p5R_net_after_comm_eur": round(half_r_net, 1) if half_r_net else None,
        "breakeven_bounce_R": round(breakeven_r, 3) if breakeven_r else None,
        "recommended_min_notional_eur": round(rec_notional),
        "recommended_min_units": rec_units,
        "verdict": verdict,
    }


def main() -> int:
    fr.set_account_env("live")
    _, akey = fr._account()
    syms = sorted(HIGH_VOLUME_SYMBOLS) + [s for s in sorted(CORE_SYMBOLS) if s not in HIGH_VOLUME_SYMBOLS] + LEGACY
    seen, rows = set(), []
    for s in syms:
        if s in seen:
            continue
        seen.add(s)
        r = analyse(s, akey)
        if r:
            rows.append(r)
            print(f"  {r['pair']:8s} {r.get('verdict','?'):32s} "
                  f"units={r.get('units_at_E45')}  notl=€{r.get('notional_eur_at_E45')}  "
                  f"comm=€{r.get('commission_eur_roundtrip')} ({r.get('commission_pct_of_notional')}%)  "
                  f"0.5R-net=€{r.get('bounce_0p5R_net_after_comm_eur')}")

    cols = list(rows[0].keys())
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in cols})
    json.dump({"generated": __import__("datetime").datetime.now().isoformat(),
               "risk_eur": RISK_EUR, "tp_rr": TP_RR, "min_notional_floor": MIN_NOTIONAL_FLOOR,
               "target_comm_pct": TARGET_COMM_PCT, "rows": rows},
              open(OUT_JSON, "w", encoding="utf-8"), indent=1)
    print(f"\n  {len(rows)} pairs -> {OUT_CSV}\n  then: py -3.12 reports/live_pair_commission_analysis_xlsx.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
