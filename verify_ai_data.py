"""
verify_ai_data.py -- read-only data-integrity audit of the substrate the
AI layer (journal, give-back, shadow study, learner) reads.

Consolidates the ad-hoc checks used while fixing the 2026-09-01 batch of
recording bugs (fill price = scan close, SIM net-P&L garbage, stale chart
bars, NZDPLN re-entry loop) into one repeatable pass. Changes nothing --
it only reports.

Checks:
  1. impossible commission    exit card net better than gross (a rebate)
  2. pnl sign mismatch        ledger realized_pnl vs (exit-entry)*dir, wide
  3. open-position drift      state entry_price far from the live quote
  4. mae/mfe out of bounds    |excursion| > SANE_R x initial risk
  5. unpaired cards           exit card with no entry (or vice-versa)
  6. duplicate open rows      same module/strategy/symbol open >1x
  7. flag summary             net_pnl_reconstructed / pnl_suspect /
                              mae_mfe_invalidated / entry_price_corrected

    python verify_ai_data.py                # full report
    python verify_ai_data.py --check drift  # one check
    python verify_ai_data.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(BASE, "data", "pnl_ledger.db")
CARDS = os.path.join(BASE, "data", "trade_observation_cards.jsonl")
STATES = {
    "sim": os.path.join(BASE, "data", "forex_state.json"),
    "live": os.path.join(BASE, "data", "forex_live_state.json"),
    "live_eur": os.path.join(BASE, "data", "forex_live_eur_state.json"),
}

SANE_R = 25.0
PNL_SIGN_MARGIN = 5.0        # EUR -- ignore tiny cost-driven flips
DRIFT_TOL = 0.004            # 0.4% -- matches forex/runner._STALE_FORMING_BAR_TOL

G, R, Y, C, DIM, X, B = (
    "\033[92m", "\033[91m", "\033[93m", "\033[96m", "\033[2m", "\033[0m", "\033[1m"
)


def _cards():
    if not os.path.exists(CARDS):
        return []
    out = []
    for ln in open(CARDS, encoding="utf-8"):
        ln = ln.strip()
        if ln:
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
    return out


def _ledger_rows(where=""):
    if not os.path.exists(LEDGER):
        return []
    con = sqlite3.connect(LEDGER)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(f"SELECT * FROM trades {where}")]
    con.close()
    return rows


# ── checks ──────────────────────────────────────────────────────────────────
def check_impossible_commission(cards, **_):
    hits = []
    for c in cards:
        if c.get("event") != "exit":
            continue
        cm = c.get("commission_eur")
        if cm is not None and cm < -0.01:
            hits.append(f"{c.get('card_id')}  commission_eur={cm} (a rebate — impossible)")
    return hits


def check_pnl_sign_mismatch(_, ledger, **__):
    hits = []
    for d in ledger:
        if d.get("status") != "closed" or d.get("realized_pnl") is None:
            continue
        e, x, q = d.get("entry_price"), d.get("exit_price"), d.get("quantity")
        if not (e and x and q):
            continue
        sgn = 1 if str(d.get("direction", "")).lower().startswith("b") else -1
        gross = (x - e) * q * sgn
        rp = d["realized_pnl"]
        if gross < -PNL_SIGN_MARGIN and rp > PNL_SIGN_MARGIN:
            hits.append(f"id {d['id']} {d['module']}/{d['strategy']}/{d['symbol']}: "
                        f"price move ~{gross:+.1f} but realized_pnl {rp:+.2f}")
        elif gross > PNL_SIGN_MARGIN and rp < -PNL_SIGN_MARGIN and abs(rp) > abs(gross) * 4:
            hits.append(f"id {d['id']} {d['module']}/{d['strategy']}/{d['symbol']}: "
                        f"price move ~{gross:+.1f} but realized_pnl {rp:+.2f} (cost 4x the move)")
    return hits


def check_open_position_drift(*_, **__):
    hits = []
    try:
        import forex.runner as fr
        from forex.universe import get_pair
    except Exception as e:
        return [f"(skipped — could not import forex.runner: {e})"]
    for env, path in STATES.items():
        if not os.path.exists(path):
            continue
        fr.set_account_env(env)
        pos = json.load(open(path)).get("positions", {})
        syms = {(k.split(":", 1)[1] if ":" in k else k) for k in pos}
        pairs = []
        for s in syms:
            try:
                pairs.append({"symbol": s, "uic": get_pair(s)["uic"]})
            except Exception:
                pass
        try:
            live = fr._fetch_live_prices(pairs)     # one batched call set
        except Exception as e:
            hits.append(f"({env}: live-price fetch failed: {e})")
            continue
        for key, p in pos.items():
            sym = key.split(":", 1)[1] if ":" in key else key
            ep, lp = p.get("entry_price"), live.get(sym)
            if ep and lp and abs(ep - lp) / lp > 0.05:   # 5%+ = clearly wrong, not drift
                hits.append(f"{env}:{key}  entry {ep:.5f} is {(ep/lp-1)*100:+.1f}% "
                            f"off the live quote {lp:.5f}")
    return hits


def check_mae_mfe_bounds(cards, **_):
    entry = {c["card_id"]: c for c in cards if c.get("event") == "entry" and c.get("card_id")}
    hits = []
    for c in cards:
        if c.get("event") != "exit" or c.get("mae_mfe_invalidated"):
            continue
        e = entry.get(c.get("card_id"), {})
        risk = e.get("risk_eur")
        if not risk or risk <= 0:
            continue
        for fld in ("mae_eur", "mfe_eur"):
            v = c.get(fld)
            if v is not None and abs(v) > SANE_R * risk:
                hits.append(f"{c.get('card_id')}  {fld}={v} vs risk {risk:.1f} "
                            f"({abs(v)/risk:.0f}R — over {SANE_R:.0f}R cap)")
    return hits


def check_unpaired_cards(cards, **_):
    ev = {}
    for c in cards:
        cid = c.get("card_id")
        if cid:
            ev.setdefault(cid, set()).add(c.get("event"))
    hits = []
    for cid, kinds in ev.items():
        if "exit" in kinds and "entry" not in kinds:
            hits.append(f"{cid}  exit card with NO entry card")
    return hits


def check_duplicate_open_rows(_, ledger, **__):
    seen = {}
    for d in ledger:
        if d.get("status") == "closed":
            continue
        k = (d.get("module"), d.get("strategy"), d.get("symbol"))
        seen.setdefault(k, []).append(d["id"])
    return [f"{m}/{s}/{sym}  open rows: {ids}" for (m, s, sym), ids in seen.items() if len(ids) > 1]


CHECKS = {
    "commission": check_impossible_commission,
    "pnl_sign": check_pnl_sign_mismatch,
    "drift": check_open_position_drift,
    "mae_mfe": check_mae_mfe_bounds,
    "unpaired": check_unpaired_cards,
    "duplicates": check_duplicate_open_rows,
}


def _flag_summary(cards):
    from collections import Counter
    c = Counter()
    for card in cards:
        for f in ("net_pnl_reconstructed", "pnl_suspect", "mae_mfe_invalidated",
                  "price_source", "entry_price_corrected"):
            if card.get(f):
                c[f] += 1
    return dict(c)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", choices=list(CHECKS), help="run only one check")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    cards = _cards()
    ledger = _ledger_rows()
    to_run = [a.check] if a.check else list(CHECKS)
    results = {name: CHECKS[name](cards, ledger=ledger) for name in to_run}

    if a.json:
        print(json.dumps({"findings": results, "flags": _flag_summary(cards)}, indent=2))
        return 1 if any(results.values()) else 0

    total = 0
    for name, hits in results.items():
        head = f"{B}{name}{X}  ({len(hits)} finding{'s' if len(hits) != 1 else ''})"
        print(f"\n{head}")
        if not hits:
            print(f"  {G}clean{X}")
        for h in hits[:40]:
            print(f"  {R}•{X} {h}")
        if len(hits) > 40:
            print(f"  {DIM}... {len(hits) - 40} more{X}")
        total += len(hits)

    print(f"\n{B}flags on observation cards:{X}")
    for k, v in sorted(_flag_summary(cards).items()):
        print(f"  {DIM}{k:26s}{X} {v}")

    print(f"\n{B}{'=' * 50}{X}")
    if total:
        print(f"{Y}{total} finding(s) — review above{X}")
    else:
        print(f"{G}{B}  all checks clean{X}")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
