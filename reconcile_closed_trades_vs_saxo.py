"""
reconcile_closed_trades_vs_saxo.py -- verify LIVE closed-trade records
against Saxo's own authoritative closed-position history.

WHY. ATOS's ledger (data/pnl_ledger.db) and forex observation cards
(data/trade_observation_cards.jsonl) are the substrate the AI journal and
the P2 give-back report learn from. Until 2026-09-01 ATOS recorded the
scan-bar close as the fill price, not the real fill (MXNUSD LIVE booked
0.058876 vs the real 0.058687). The fill-confirmation fix (2aa38c0) now
writes Saxo's real OpenPrice / ClosingPrice at record time -- this file is
the BACKSTOP: a deterministic pass that, while Saxo still has the closed
position in its retention window (~a week), re-checks every LIVE closed
trade against Saxo's ClosedPosition record and corrects + logs any drift.

DESIGN.
  * Read-only w.r.t. trading state -- never places, cancels or amends an
    order, never touches an OPEN position. Only rewrites already-closed
    ledger rows / observation cards, and only the price-derived fields.
  * LIVE only. Saxo's SIM /port/v1/closedpositions/me returns HTTP 400 --
    there is no SIM equivalent. SIM entry/exit prices are already
    Saxo-sourced at record time via positions/me (which SIM does serve).
  * Never raises into a caller. `run()` is safe to call from the post-run
    safeguard slot.
  * Matches by symbol + |amount| + side + close-time proximity (Saxo's
    ClosedPosition carries neither SourceOrderId nor AccountKey). An
    ambiguous match (>1 candidate) is flagged, never auto-corrected.

    python reconcile_closed_trades_vs_saxo.py            # dry run, last 7d
    python reconcile_closed_trades_vs_saxo.py --apply
    python reconcile_closed_trades_vs_saxo.py --since 2026-08-20 --json
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(BASE, "data", "pnl_ledger.db")
CARDS = os.path.join(BASE, "data", "trade_observation_cards.jsonl")

# module -> Saxo env whose (pooled) closedpositions feed covers it
LIVE_MODULE_ENV = {"forex_live": "live", "forex_live_eur": "live_eur"}
MARKER = "saxo-reconcile"
PRICE_TOL_BPS = 3.0          # correct a price off by more than this (~0.3 pip
                            # on a 5-dp pair) -- fills should match exactly;
                            # the band only tolerates sub-pip float noise
MATCH_WINDOW_MIN = 120       # ledger close-time vs Saxo close-time
LOOKBACK_DAYS = 7            # Saxo closed-position retention is ~a week
LEDGER_TZ_OFFSET_H = 5       # ledger timestamps are naive PKT (UTC+5)

G, R, Y, C, DIM, X, B = (
    "\033[92m", "\033[91m", "\033[93m", "\033[96m", "\033[2m", "\033[0m", "\033[1m"
)


# ── Saxo side ───────────────────────────────────────────────────────────────
def _saxo_closed(env: str) -> list[dict]:
    """Normalised closed positions from Saxo for `env`. [] on any failure
    (SIM 400, network, auth). The feed is pooled across the login's
    sub-accounts -- callers disambiguate by symbol/amount/time."""
    try:
        import saxo_client as sc
        resp = sc._request_with_retry(
            "GET", f"{sc._base_url(env)}/port/v1/closedpositions/me",
            headers=sc._headers(env),
            params={"FieldGroups": "ClosedPosition,DisplayAndFormat"},
        )
        resp.raise_for_status()
        rows = resp.json().get("Data", [])
    except Exception:
        return []
    out = []
    for c in rows:
        cp = c.get("ClosedPosition", {})
        sym = (c.get("DisplayAndFormat", {}) or {}).get("Symbol")
        op, clp = cp.get("OpenPrice"), cp.get("ClosingPrice")
        ct = cp.get("ExecutionTimeClose")
        if not (sym and op and clp and ct):
            continue
        out.append({
            "symbol": sym,
            "amount": abs(float(cp.get("Amount") or 0)),
            "side": cp.get("BuyOrSell"),
            "open_price": float(op),
            "close_price": float(clp),
            "gross_quote": float(cp.get("ClosedProfitLoss") or 0.0),
            "cost_quote": abs(float(cp.get("CostOpening") or 0.0)) + abs(float(cp.get("CostClosing") or 0.0)),
            "open_time": _parse_utc(cp.get("ExecutionTimeOpen")),
            "close_time": _parse_utc(ct),
            "opening_position_id": cp.get("OpeningPositionId"),
        })
    return out


def _parse_utc(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return None


def _ledger_ts_to_utc(s: str | None) -> datetime | None:
    """Ledger timestamps are naive local (PKT, UTC+5)."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc) - timedelta(hours=LEDGER_TZ_OFFSET_H)
    return dt.astimezone(timezone.utc)


# ── matching ────────────────────────────────────────────────────────────────
def _match(row: dict, saxo: list[dict]):
    """('ok', cp) | ('none', None) | ('ambiguous', [cp, ...])."""
    close_utc = _ledger_ts_to_utc(row["timestamp_close"])
    qty = abs(float(row["quantity"] or 0))
    cands = []
    for cp in saxo:
        if cp["symbol"] != row["symbol"]:
            continue
        if (cp["side"] or "").lower() != (row["direction"] or "").lower():
            continue
        if qty > 0 and abs(cp["amount"] - qty) > max(1.0, qty * 0.02):
            continue
        if close_utc and cp["close_time"]:
            if abs((cp["close_time"] - close_utc).total_seconds()) > MATCH_WINDOW_MIN * 60:
                continue
        cands.append(cp)
    if not cands:
        return "none", None
    if len(cands) == 1:
        return "ok", cands[0]
    # tie-break on closest close-time before giving up
    if close_utc and all(c["close_time"] for c in cands):
        cands.sort(key=lambda c: abs((c["close_time"] - close_utc).total_seconds()))
        if abs((cands[0]["close_time"] - cands[1]["close_time"]).total_seconds()) > 60:
            return "ok", cands[0]
    return "ambiguous", cands


def _bps(a: float, b: float) -> float:
    """Absolute difference a-vs-b in basis points of b."""
    if not b:
        return 0.0
    return abs(a - b) / abs(b) * 1e4


# ── observation cards ───────────────────────────────────────────────────────
def _load_cards() -> list[dict]:
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


def _save_cards(cards: list[dict]) -> None:
    tmp = CARDS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for c in cards:
            fh.write(json.dumps(c, ensure_ascii=False) + "\n")
    os.replace(tmp, CARDS)


def _match_card(row: dict, cards: list[dict]):
    """(entry_card, exit_card) for this ledger row, or (None, None).

    Only the ENTRY event carries account_env / symbol / strategy; the EXIT
    event carries only card_id + the exit metrics. So: pick candidate
    entry cards by (account_env, symbol[, strategy]), then attach the exit
    event that shares the card_id, and keep the one whose exit time is
    closest to (and within the match window of) the ledger close."""
    env = {"forex_live": "live", "forex_live_eur": "live_eur"}.get(row["module"])
    if not env:
        return None, None
    close_utc = _ledger_ts_to_utc(row["timestamp_close"])

    exit_by_id: dict = {}
    for c in cards:
        if c.get("event") == "exit" and c.get("card_id"):
            exit_by_id[c["card_id"]] = c

    best, best_gap = None, None
    for c in cards:
        if c.get("event") != "entry" or not c.get("card_id"):
            continue
        if c.get("account_env") != env or c.get("symbol") != row["symbol"]:
            continue
        if row.get("strategy") and c.get("strategy") and c["strategy"] != row["strategy"]:
            continue
        x = exit_by_id.get(c["card_id"])
        if not x:
            continue
        xt = _parse_utc(x.get("timestamp"))
        gap = abs((xt - close_utc).total_seconds()) if (close_utc and xt) else 0
        if close_utc and xt and gap > MATCH_WINDOW_MIN * 60:
            continue
        if best is None or gap < best_gap:
            best, best_gap = (c, x), gap
    return best if best else (None, None)


def _eur_per_quote(entry_c: dict, exit_c: dict, cp: dict, old_e: float) -> float | None:
    """EUR per unit of the pair's quote currency, needed to re-scale
    risk_eur onto the corrected entry price.

    Primary: Saxo's own numbers -- net_quote = gross_quote - cost_quote,
    and the card already stores net_pnl_eur for the same close, so
    rate = |net_pnl_eur| / |net_quote|. This does NOT depend on the
    (possibly also-wrong) old risk_eur.
    Fallback: back-derive from old risk_eur / old stop distance (valid
    when only the price drifted and the rate used at entry was right)."""
    net_eur = exit_c.get("net_pnl_eur")
    net_quote = cp["gross_quote"] - cp["cost_quote"]
    if isinstance(net_eur, (int, float)) and abs(net_quote) > 1e-9 and abs(net_eur) > 1e-9:
        return abs(net_eur) / abs(net_quote)
    stop, risk, qty = entry_c.get("current_stop"), entry_c.get("risk_eur"), entry_c.get("quantity")
    if stop and risk and qty and abs(old_e - stop) > 0:
        return risk / (abs(old_e - stop) * qty)
    return None


def _correct_card(entry_c: dict, exit_c: dict, cp: dict) -> list[str]:
    """Rewrite the card's price + price-derived fields from Saxo. Returns
    a list of human-readable change notes."""
    notes = []
    old_e = entry_c.get("entry_price")
    if old_e and _bps(old_e, cp["open_price"]) > PRICE_TOL_BPS:
        stop = entry_c.get("current_stop")
        qty = entry_c.get("quantity") or abs(cp["amount"])
        rate = _eur_per_quote(entry_c, exit_c, cp, old_e)
        if stop and rate:
            entry_c["risk_eur"] = round(abs(cp["open_price"] - stop) * qty * rate, 2)
        notes.append(f"card entry {old_e:.6f}->{cp['open_price']:.6f} risk_eur~{entry_c.get('risk_eur')}")
        entry_c["entry_price"] = cp["open_price"]
        entry_c["price_source"] = MARKER
        # exit-card R follows the corrected risk
        risk = entry_c.get("risk_eur")
        net = exit_c.get("net_pnl_eur")
        if risk and risk > 0 and isinstance(net, (int, float)):
            exit_c["r_multiple"] = round(net / risk, 2)

    old_x = exit_c.get("exit_price")
    if old_x and _bps(old_x, cp["close_price"]) > PRICE_TOL_BPS:
        notes.append(f"card exit {old_x:.6f}->{cp['close_price']:.6f}")
        exit_c["exit_price"] = cp["close_price"]
        exit_c["price_source"] = MARKER
    return notes


# ── main pass ───────────────────────────────────────────────────────────────
class Finding(dict):
    pass


def reconcile(apply: bool = False, since: str | None = None,
              price_tol_bps: float = PRICE_TOL_BPS) -> list[Finding]:
    if not os.path.exists(LEDGER):
        return []
    since_dt = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since)
        except ValueError:
            since_dt = None
    if since_dt is None:
        since_dt = datetime.now() - timedelta(days=LOOKBACK_DAYS)
    since_iso = since_dt.isoformat()

    con = sqlite3.connect(LEDGER)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM trades WHERE status='closed' AND module IN ({}) "
        "AND timestamp_close >= ? ORDER BY id".format(
            ",".join("?" * len(LIVE_MODULE_ENV))),
        (*LIVE_MODULE_ENV.keys(), since_iso),
    )]

    saxo_by_env: dict = {}
    for env in set(LIVE_MODULE_ENV.values()):
        saxo_by_env[env] = _saxo_closed(env)

    cards = _load_cards() if os.path.exists(CARDS) else []
    cards_dirty = False
    findings: list[Finding] = []

    for row in rows:
        env = LIVE_MODULE_ENV[row["module"]]
        status, cp = _match(row, saxo_by_env.get(env, []))
        f = Finding(id=row["id"], module=row["module"], symbol=row["symbol"],
                    close=row["timestamp_close"], match=status,
                    ledger_changed=False, card_changed=False, notes=[])

        if status == "none":
            f["notes"].append("not in Saxo retention window (old trade or already aged out)")
            findings.append(f)
            continue
        if status == "ambiguous":
            f["notes"].append(f"{len(cp)} Saxo candidates -- NOT auto-corrected, review manually")
            findings.append(f)
            continue

        e_bps = _bps(row["entry_price"] or 0, cp["open_price"])
        x_bps = _bps(row["exit_price"] or 0, cp["close_price"])
        f["entry_bps"], f["exit_bps"] = round(e_bps, 1), round(x_bps, 1)

        new_entry = cp["open_price"] if e_bps > price_tol_bps else None
        new_exit = cp["close_price"] if x_bps > price_tol_bps else None
        if new_entry is not None or new_exit is not None:
            ne = new_entry if new_entry is not None else row["entry_price"]
            nx = new_exit if new_exit is not None else row["exit_price"]
            f["notes"].append(
                (f"ledger entry {row['entry_price']:.6f}->{ne:.6f} " if new_entry is not None else "")
                + (f"ledger exit {row['exit_price']:.6f}->{nx:.6f}" if new_exit is not None else "")
            )
            f["ledger_changed"] = True
            if apply:
                con.execute("UPDATE trades SET entry_price=?, exit_price=? WHERE id=?",
                            (ne, nx, row["id"]))

        e_card, x_card = _match_card(row, cards)
        if e_card and x_card:
            card_notes = _correct_card(e_card, x_card, cp)
            if card_notes:
                f["card_changed"] = True
                f["notes"].extend(card_notes)
                cards_dirty = True

        findings.append(f)

    if apply:
        con.commit()
        if cards_dirty and cards:
            _save_cards(cards)
    con.close()
    return findings


def run(env: str | None = None) -> int:
    """Post-run hook: apply corrections for the last 2 days, best-effort.
    Returns the number of rows/cards corrected. Never raises. `env` is
    accepted for call-site symmetry but the pass always covers every LIVE
    module (the Saxo feed is pooled anyway)."""
    try:
        since = (datetime.now() - timedelta(days=2)).isoformat()
        fs = reconcile(apply=True, since=since)
        n = sum(1 for f in fs if f.get("ledger_changed") or f.get("card_changed"))
        if n:
            print(f"[reconcile-vs-saxo] corrected {n} closed-trade record(s) from Saxo truth")
        return n
    except Exception as exc:                      # never break the caller
        print(f"[reconcile-vs-saxo] skipped (non-fatal): {exc}")
        return 0


# ── CLI ─────────────────────────────────────────────────────────────────────
def _print(findings: list[Finding], apply: bool) -> None:
    if not findings:
        print(f"{DIM}No LIVE closed trades in range.{X}")
        return
    for f in findings:
        tag = {
            "ok": G + "MATCH" + X, "none": DIM + "n/a  " + X,
            "ambiguous": Y + "AMBIG" + X,
        }.get(f["match"], f["match"])
        chg = ""
        if f["ledger_changed"] or f["card_changed"]:
            chg = R + B + ("  APPLIED" if apply else "  would fix") + X
        head = (f"  [{tag}] {f['module']:<14} {f['symbol']:<8} "
                f"close={str(f['close'])[:19]}")
        if "entry_bps" in f:
            head += f"  entryΔ={f['entry_bps']}bp exitΔ={f['exit_bps']}bp"
        print(head + chg)
        for n in f["notes"]:
            print(f"        {DIM}{n}{X}")
    n_fix = sum(1 for f in findings if f["ledger_changed"] or f["card_changed"])
    n_amb = sum(1 for f in findings if f["match"] == "ambiguous")
    print()
    print(f"  {len(findings)} checked | {n_fix} {'corrected' if apply else 'need correction'} "
          f"| {n_amb} ambiguous")
    if not apply and n_fix:
        print(f"  {Y}re-run with --apply to write{X}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write corrections (default: dry run)")
    ap.add_argument("--since", help="ISO date; default = 7 days ago")
    ap.add_argument("--tol-bps", type=float, default=PRICE_TOL_BPS)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    findings = reconcile(apply=a.apply, since=a.since, price_tol_bps=a.tol_bps)
    if a.json:
        print(json.dumps(findings, indent=2, default=str))
    else:
        _print(findings, a.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
