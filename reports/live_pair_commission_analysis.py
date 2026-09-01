"""
reports/live_pair_commission_analysis.py

For every pair the ATOS LIVE accounts trade (17 HIGH_VOLUME_SYMBOLS, the
go-forward SEK universe) plus the legacy pairs the accounts still hold,
pull the REAL Saxo cost straight from the live API and compute -- at the
current RSI_LIVE_FIXED_RISK_EUR = 45 sizing -- the position's economics:
notional, round-trip cost as a % of it, what a full take-profit and a
modest 0.5R bounce net AFTER cost, and the smallest notional at which a
thin RSI bounce still clears cost. Purpose: never repeat MXNUSD (a
+EUR2.13 price move that netted -EUR3.05 once the flat -EUR5.19 cost hit).

WHAT "COMMISSION" MEANS AT SAXO FOR FX (from the two rates-and-conditions
pages, read 2026-09-01):
  * FX spot/forward has NO percentage or per-lot commission. It is priced
    entirely through the SPREAD (bid/ask), tiered Classic / Platinum / VIP
    -- EURUSD Classic = 1.0 pip.
  * The only fixed fee is a MINIMUM commission (transaction fee) of 1 USD
    per side, charged on small-notional trades. Round trip => 2 USD floor.
  * Overnight holds pay a Tom/Next financing markup: Classic +/-0.75%,
    Platinum +/-0.60%, VIP +/-0.50%, PLUS an extra +/-0.30% on MXN, RUB,
    TRY and ZAR crosses. (This is a HOLDING cost, not an entry cost.)
So the "commission_eur_roundtrip" column below is Saxo's own all-in cost
estimate for that exact size (infoprices Commissions.CostBuy x2) -- which
for FX spot is the SPREAD cost. That is the number the LIVE cost gate
uses.

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
# The LIVE gate is now fr's pair-independent recovery-vs-cost rule
# (RSI_LIVE_ASSUMED_EXIT_R / RSI_LIVE_MIN_RECOVERY_MULT). "verdict" below is
# scored directly against fr._live_all_in_cost_eur -- no local threshold.

# Saxo published FX pricing constants (rates-and-conditions pages, 2026-09-01)
SAXO_MIN_TICKET_USD = 1.0                      # per side; round trip => 2 USD
TOMNEXT_MARKUP_CLASSIC_PCT = 0.75             # +/- , Classic tier
TOMNEXT_EM_SURCHARGE_PCT = 0.30              # extra, on MXN/RUB/TRY/ZAR crosses
EM_SURCHARGE_CCY = {"MXN", "RUB", "TRY", "ZAR"}

# legacy pairs the LIVE accounts still hold (outside the 17 HIGH_VOLUME).
# SEK: donchian EURNOK/GBPUSD/AUDUSD/AUDCHF + rsi EURCAD.  EUR: rsi
# GBPPLN/EURUSD/GBPUSD/NZDCAD/EURCAD.  MXNUSD kept for the reference row.
LEGACY = ["EURNOK", "AUDCHF", "GBPPLN", "NZDCAD", "MXNUSD"]


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
    eur_per_usd = fr._eur_per_unit("USD", akey)
    df = fr._fetch_history(uic)
    if not (px and eur_per_q and eur_per_b) or df is None or len(df) < 20:
        return {"pair": sym, "tier": _tier(sym), "note": "no price / rate / bars"}
    atr = float(strat._atr(df["High"], df["Low"], df["Close"]).iloc[-1])
    stop_dist = STOP_MULT * atr                                  # quote ccy
    stop_pct = stop_dist / px * 100

    # ── live bid/ask spread (the true FX "commission" at Saxo) ──
    spread_pct = fr._spread_pct(uic)                              # % of mid
    pip = 0.01 if q_ccy == "JPY" else 0.0001                      # 1 pip in price
    spread_price = (spread_pct / 100 * px) if spread_pct is not None else None
    spread_pips = (spread_price / pip) if spread_price else None

    # size at the EUR45 fixed-risk ceiling (same call the runner makes)
    units = rsi.size_position(0, atr, min_units=min_units, risk_amount=RISK_EUR / eur_per_q)

    # real Saxo round-trip cost for that size, from infoprices Commissions
    # (for FX spot this IS the spread cost). Falls back to a 1-lot quote.
    rt_cost_q = (fr._round_trip_cost_quote_ccy(uic, units or min_units, akey))
    rt_cost_eur = rt_cost_q * eur_per_q if rt_cost_q is not None else None
    # cross-check: round-trip spread cost = units x full spread (pay half in,
    # half out) x eur/quote
    spread_cost_eur = (units * spread_price * eur_per_q) if (units and spread_price) else None

    R_eur = (units * stop_dist * eur_per_q) if units else None   # realised risk
    notional_eur = (units * eur_per_b) if units else None
    notional_usd = (notional_eur / eur_per_usd) if (notional_eur and eur_per_usd) else None
    comm_pct = (rt_cost_eur / notional_eur * 100) if (rt_cost_eur and notional_eur) else None

    # Saxo 1-USD-per-side minimum ticket: does the modelled round-trip cost
    # sit ABOVE the 2-USD round-trip floor? (If it doesn't, the flat fee
    # dominates -- the MXNUSD failure mode.)
    rt_cost_usd = (rt_cost_eur / eur_per_usd) if (rt_cost_eur and eur_per_usd) else None
    min_ticket_binds = (rt_cost_usd is not None and rt_cost_usd < 2 * SAXO_MIN_TICKET_USD)

    # Tom/Next overnight financing markup (Classic tier, +EM surcharge)
    em_surcharge = (b_ccy in EM_SURCHARGE_CCY) or (q_ccy in EM_SURCHARGE_CCY)
    tomnext_pct = TOMNEXT_MARKUP_CLASSIC_PCT + (TOMNEXT_EM_SURCHARGE_PCT if em_surcharge else 0.0)
    tomnext_eur_per_day = (notional_eur * tomnext_pct / 100 / 360) if notional_eur else None

    tp_gross_eur = (TP_RR * R_eur) if R_eur else None            # if TP (2R) hits
    tp_net_eur = (tp_gross_eur - rt_cost_eur) if (tp_gross_eur and rt_cost_eur) else None
    half_r_gross = (0.5 * R_eur) if R_eur else None              # a modest bounce
    half_r_net = (half_r_gross - rt_cost_eur) if (half_r_gross and rt_cost_eur) else None
    breakeven_r = (rt_cost_eur / R_eur) if (rt_cost_eur and R_eur) else None

    # ── the ACTUAL runner gate: recovery-vs-cost (fr._live_all_in_cost_eur) ──
    all_in_eur = fr._live_all_in_cost_eur(
        commission_eur=rt_cost_eur, spread_pct=spread_pct, entry_px=px,
        notional_eur=notional_eur, quote_ccy=q_ccy)
    recovery_eur = (fr.RSI_LIVE_ASSUMED_EXIT_R * R_eur) if R_eur else None
    recovery_to_cost = (recovery_eur / all_in_eur) if (recovery_eur and all_in_eur) else None
    is_live_universe = sym in HIGH_VOLUME_SYMBOLS
    rejected_at_e45 = bool(
        is_live_universe and recovery_eur is not None and all_in_eur is not None
        and recovery_eur < fr.RSI_LIVE_MIN_RECOVERY_MULT * all_in_eur)

    ok = (half_r_net is not None and half_r_net > 0
          and not min_ticket_binds and not rejected_at_e45)
    verdict = ("MIN-TICKET binds — cost < 2 USD round-trip floor" if min_ticket_binds
               else (f"REJECTED — {fr.RSI_LIVE_ASSUMED_EXIT_R:.2f}R (€{recovery_eur:,.1f}) "
                     f"< {fr.RSI_LIVE_MIN_RECOVERY_MULT:.1f}× all-in €{all_in_eur:,.2f}") if rejected_at_e45
               else "THIN — a 0.5R bounce barely clears cost" if (half_r_net is not None and half_r_net <= 5)
               else ("OK — trades at €45 risk" if is_live_universe else "legacy/open — not in the LIVE RSI universe")
               if ok else "review")

    return {
        "pair": sym, "tier": _tier(sym),
        "price": round(px, 5), "atr": round(atr, 6), "stop_pct": round(stop_pct, 3),
        "spread_pips": round(spread_pips, 2) if spread_pips else None,
        "spread_pct_of_price": round(spread_pct, 4) if spread_pct is not None else None,
        "commission_eur_roundtrip": round(rt_cost_eur, 2) if rt_cost_eur else None,
        "spread_cost_eur_roundtrip": round(spread_cost_eur, 2) if spread_cost_eur else None,
        "min_ticket_binds": bool(min_ticket_binds),
        "tomnext_markup_pct": round(tomnext_pct, 2),
        "em_swap_surcharge": bool(em_surcharge),
        "tomnext_eur_per_day": round(tomnext_eur_per_day, 2) if tomnext_eur_per_day else None,
        "units_at_E45": units,
        "notional_eur_at_E45": round(notional_eur) if notional_eur else None,
        "notional_usd_at_E45": round(notional_usd) if notional_usd else None,
        "realised_risk_eur": round(R_eur, 1) if R_eur else None,
        "commission_pct_of_notional": round(comm_pct, 3) if comm_pct else None,
        "tp_2R_gross_eur": round(tp_gross_eur, 1) if tp_gross_eur else None,
        "tp_2R_net_after_comm_eur": round(tp_net_eur, 1) if tp_net_eur else None,
        "bounce_0p5R_gross_eur": round(half_r_gross, 1) if half_r_gross else None,
        "bounce_0p5R_net_after_comm_eur": round(half_r_net, 1) if half_r_net else None,
        "breakeven_bounce_R": round(breakeven_r, 3) if breakeven_r else None,
        "all_in_cost_eur": round(all_in_eur, 2) if all_in_eur else None,
        "recovery_0p5R_eur": round(recovery_eur, 1) if recovery_eur else None,
        "recovery_to_cost_ratio": round(recovery_to_cost, 2) if recovery_to_cost else None,
        "min_recovery_mult": fr.RSI_LIVE_MIN_RECOVERY_MULT,
        "rejected_at_e45_risk": bool(rejected_at_e45),
        "verdict": verdict,
    }


def main() -> int:
    fr.set_account_env("live")
    _, akey = fr._account()
    # the real LIVE universe: 17 HIGH_VOLUME (go-forward SEK/RSI) + the
    # legacy pairs still open on either account. NOT the full 49 CORE.
    syms = sorted(HIGH_VOLUME_SYMBOLS) + LEGACY
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
               "risk_eur": RISK_EUR, "tp_rr": TP_RR,
               "enforced_gate": (
                   f"recovery-vs-cost (pair-independent): reject if "
                   f"{fr.RSI_LIVE_ASSUMED_EXIT_R}R * realised_R_eur < "
                   f"{fr.RSI_LIVE_MIN_RECOVERY_MULT} * all_in_cost_eur "
                   f"(commission + spread + {fr.RSI_LIVE_SLIPPAGE_PIPS}-pip slippage). "
                   f"Replaced MIN_LIVE_NOTIONAL_EUR and the LIVE_RSI_MIN_UNITS table."),
               "assumed_exit_r": fr.RSI_LIVE_ASSUMED_EXIT_R,
               "min_recovery_mult": fr.RSI_LIVE_MIN_RECOVERY_MULT,
               "saxo_pricing": {
                   "fx_spot_commission_published": "none -- spread-priced (Classic/Platinum/VIP), 1 USD/side min ticket",
                   "fx_spot_commission_this_account": "FLAT ~EUR2.59/side (~EUR5.18 round-trip), pair- and size-independent -- confirmed by live infoprices AND the real MXNUSD closed position (Cost -5.19). Our account is on a commission schedule, not the published spread-only retail plan.",
                   "eurusd_classic_spread_pips": 1.0,
                   "min_ticket_usd_per_side": SAXO_MIN_TICKET_USD,
                   "tomnext_markup_classic_pct": TOMNEXT_MARKUP_CLASSIC_PCT,
                   "tomnext_em_surcharge_pct": TOMNEXT_EM_SURCHARGE_PCT,
                   "em_surcharge_ccy": sorted(EM_SURCHARGE_CCY),
                   "sources": [
                       "https://www.home.saxo/rates-and-conditions/forex/spreads-and-commissions",
                       "https://www.home.saxo/rates-and-conditions/forex/trading-conditions",
                   ],
               },
               "rows": rows},
              open(OUT_JSON, "w", encoding="utf-8"), indent=1)
    print(f"\n  {len(rows)} pairs -> {OUT_CSV}\n  then: py -3.12 reports/live_pair_commission_analysis_xlsx.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
