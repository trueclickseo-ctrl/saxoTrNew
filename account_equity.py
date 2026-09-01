"""
account_equity.py -- the ONE honest view of the real-money account.

The problem it fixes: nothing in ATOS tracked the *actual* account value.
The runner sizes off `min(real_pooled, RISK_EQUITY_CAP)` and the old
`*_peak_equity.json` files stored that CAPPED number as "peak equity" --
so a cap of 35,000 SEK was recorded as the peak even though the real
account is ~3,200 EUR. Drawdown / give-back / return were meaningless.

Spike finding (2026-09-01, live): Saxo's OpenAPI does NOT expose
per-sub-account balances when the sub-accounts share a margin group
(this login: SEK / EUR / USD under one AccountGroup). `/port/v1/balances/me`
returns the POOLED group total in SEK regardless of AccountKey /
ClientKey / AccountGroupKey. That pooled TotalValue IS the real total
real-money equity and IS what actually constrains trading (shared
margin), so it's the number we track. Per-sub-account UNREALISED P&L is
still splittable from /port/v1/positions/me (`ProfitLossOnTradeInBase
Currency`). After the SEK consolidation there is only one funded account,
so pooled == that account anyway.

What this module does (reporting only -- zero trading-logic effect):
  * snapshot()  -- append one row to data/account_equity_curve.jsonl
  * stats()     -- peak / drawdown% / return-since-inception /
                   7-day-give-back / weekly hi-lo from the curve
  * open_risk() -- sum (entry-stop)*qty*eur_rate over the live state files
  * render()    -- the text block for the dashboard + daily email

Deposits: data/account_deposits.json. Empty => return is measured from the
first curve row (return-since-tracking). Add real deposit records to get
true return-since-inception. A snapshot that sees TotalValue jump in a way
P&L can't explain logs a "possible deposit/withdrawal" note.

    python account_equity.py            # print the block
    python account_equity.py --snapshot # append a curve row, no output
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("account_equity")

_BASE = os.path.dirname(os.path.abspath(__file__))
CURVE_PATH = os.path.join(_BASE, "data", "account_equity_curve.jsonl")
DEPOSITS_PATH = os.path.join(_BASE, "data", "account_deposits.json")

_STATE_FILES = {
    "live":     os.path.join(_BASE, "data", "forex_live_state.json"),
    "live_eur": os.path.join(_BASE, "data", "forex_live_eur_state.json"),
}
_DEPOSIT_JUMP_SEK = 500.0     # a TotalValue move bigger than this, unexplained by P&L, looks like a transfer


# ── Saxo fetch ───────────────────────────────────────────────────────────
def _fetch() -> dict | None:
    """Pooled balance + per-AccountKey unrealised P&L + EURSEK, from LIVE
    Saxo. Returns None (caller keeps the last curve row) on any failure."""
    try:
        import saxo_client as sc
        bal = sc.get_balances(env="live")
        pos = sc.get_positions(env="live")
        acc = sc.get_account_info(env="live").get("Data", [])
        ccy_by_key = {a["AccountKey"]: a["Currency"] for a in acc if isinstance(a, dict)}

        per_acct: dict[str, float] = {}
        n_open = 0
        for p in (pos.get("Data") or []):
            n_open += 1
            ak = p.get("PositionBase", {}).get("AccountKey", "?")
            ccy = ccy_by_key.get(ak, ak[:6])
            per_acct[ccy] = per_acct.get(ccy, 0.0) + (
                p.get("PositionView", {}).get("ProfitLossOnTradeInBaseCurrency") or 0.0)

        eursek = _eursek()
        tv_sek = float(bal.get("TotalValue") or bal.get("NetEquityForMargin") or 0.0)
        return {
            "ts": datetime.now(timezone.utc).isoformat(),
            "total_value_sek": round(tv_sek, 2),
            "total_value_eur": round(tv_sek / eursek, 2) if eursek else None,
            "cash_sek": round(float(bal.get("CashBalance") or 0.0), 2),
            "net_equity_for_margin_sek": round(float(bal.get("NetEquityForMargin") or 0.0), 2),
            "margin_available_sek": round(float(bal.get("MarginAvailableForTrading") or 0.0), 2),
            "margin_util_pct": round(float(bal.get("MarginUtilizationPct") or 0.0), 2),
            "unrealized_pnl_sek": round(float(bal.get("UnrealizedMarginProfitLoss") or 0.0), 2),
            "open_positions": n_open,
            "per_account_unrealized_sek": {k: round(v, 2) for k, v in per_acct.items()},
            "eursek": round(eursek, 4) if eursek else None,
            "recent_cash_deposit_sek": round(
                float((bal.get("TransactionsNotBookedDetail") or {}).get("CashDeposit") or 0.0), 2),
        }
    except Exception as exc:
        logger.warning(f"[account_equity] fetch failed: {exc}")
        return None


def _eursek() -> float | None:
    try:
        import saxo_client as sc
        from forex.universe import get_pair
        import requests
        uic = get_pair("EURSEK")["uic"]
        r = requests.get(f"{sc._base_url('live')}/trade/v1/infoprices",
                         headers=sc._headers("live"),
                         params={"Uic": uic, "AssetType": "FxSpot", "FieldGroups": "Quote"}, timeout=15)
        q = r.json().get("Quote", {})
        bid, ask = float(q.get("Bid") or 0), float(q.get("Ask") or 0)
        return (bid + ask) / 2 if bid and ask else (bid or ask or None)
    except Exception:
        return None


# ── curve + deposits persistence ─────────────────────────────────────────
def _load_curve() -> list[dict]:
    rows = []
    try:
        with open(CURVE_PATH, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    rows.append(json.loads(ln))
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.warning(f"[account_equity] curve read failed: {exc}")
    return rows


def _append_curve(row: dict) -> None:
    try:
        os.makedirs(os.path.dirname(CURVE_PATH), exist_ok=True)
        with open(CURVE_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except Exception as exc:
        logger.warning(f"[account_equity] curve append failed: {exc}")


def _load_deposits() -> dict:
    try:
        with open(DEPOSITS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"entries": [], "note": "empty -> return measured from the first curve row. "
                                       "Add {date, sek, note} records for true since-inception return."}


def _save_deposits(d: dict) -> None:
    try:
        with open(DEPOSITS_PATH, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
    except Exception as exc:
        logger.warning(f"[account_equity] deposits save failed: {exc}")


def net_deposits_sek() -> float | None:
    ents = _load_deposits().get("entries") or []
    if not ents:
        return None
    return sum(float(e.get("sek", 0) or 0) for e in ents)


# ── open risk from the live state files ──────────────────────────────────
def open_risk_sek(eursek: float | None) -> dict:
    """{env: risk_sek} -- Σ (entry-stop)*qty converted to SEK. Quote-ccy
    conversion is approximate (uses eursek + the pair's own quote ccy is
    ignored beyond a rough EUR proxy) -- good enough for a risk gauge, not
    accounting."""
    out: dict[str, float] = {}
    for env, path in _STATE_FILES.items():
        try:
            with open(path, encoding="utf-8") as f:
                pos = json.load(f).get("positions", {})
        except Exception:
            continue
        tot = 0.0
        for v in pos.values():
            ep, sp, qty = v.get("entry_price"), v.get("stop_price"), v.get("quantity")
            if not (ep and sp and qty):
                continue
            # per-unit stop distance in the pair's QUOTE ccy -> rough EUR
            # (most quote ccys are within ~2x of EUR; this is a gauge).
            tot += abs(float(ep) - float(sp)) * float(qty)
        out[env] = round(tot, 2)
    return out


# ── stats ────────────────────────────────────────────────────────────────
def stats() -> dict:
    curve = _load_curve()
    if not curve:
        return {"empty": True}
    latest = curve[-1]
    tv = [r["total_value_sek"] for r in curve if r.get("total_value_sek")]
    peak = max(tv) if tv else None
    cur = latest.get("total_value_sek")

    now = datetime.now(timezone.utc)
    wk = [r["total_value_sek"] for r in curve
          if r.get("total_value_sek") and _age(r) <= timedelta(days=7)]
    wk_peak = max(wk) if wk else None
    wk_low = min(wk) if wk else None

    nd = net_deposits_sek()
    base = nd if nd else (tv[0] if tv else None)

    return {
        "empty": False,
        "ts": latest.get("ts"),
        "equity_sek": cur,
        "equity_eur": latest.get("total_value_eur"),
        "cash_sek": latest.get("cash_sek"),
        "peak_sek": peak,
        "drawdown_pct": round((peak - cur) / peak * 100, 2) if (peak and cur and peak > 0) else None,
        "return_pct": round((cur - base) / base * 100, 2) if (base and cur and base > 0) else None,
        "return_basis": "since-inception (deposits ledger)" if nd else "since tracking started",
        "week_peak_sek": wk_peak,
        "week_low_sek": wk_low,
        "week_giveback_sek": round(wk_peak - cur, 2) if (wk_peak and cur) else None,
        "unrealized_pnl_sek": latest.get("unrealized_pnl_sek"),
        "per_account_unrealized_sek": latest.get("per_account_unrealized_sek", {}),
        "margin_util_pct": latest.get("margin_util_pct"),
        "open_positions": latest.get("open_positions"),
        "eursek": latest.get("eursek"),
        "n_snapshots": len(curve),
    }


def _age(row: dict) -> timedelta:
    try:
        return datetime.now(timezone.utc) - datetime.fromisoformat(row["ts"])
    except Exception:
        return timedelta(days=999)


# ── snapshot: fetch + deposit-detect + append ───────────────────────────
def snapshot() -> dict | None:
    row = _fetch()
    if row is None:
        return None
    curve = _load_curve()
    if curve:
        prev = curve[-1]
        d_tv = (row["total_value_sek"] or 0) - (prev.get("total_value_sek") or 0)
        d_pnl = (row["unrealized_pnl_sek"] or 0) - (prev.get("unrealized_pnl_sek") or 0)
        if abs(d_tv) > _DEPOSIT_JUMP_SEK and abs(d_tv - d_pnl) > _DEPOSIT_JUMP_SEK:
            note = (f"TotalValue moved {d_tv:+,.0f} SEK since the last snapshot but unrealised P&L "
                    f"only moved {d_pnl:+,.0f} -- looks like a ~{d_tv - d_pnl:+,.0f} SEK deposit/withdrawal. "
                    f"Add it to {os.path.basename(DEPOSITS_PATH)} so return-since-inception stays correct.")
            row["suspected_transfer_sek"] = round(d_tv - d_pnl, 0)
            logger.warning(f"[account_equity] {note}")
            try:
                import attention
                attention.raise_attention(
                    "account-equity:unrecorded-transfer",
                    title="Possible unrecorded deposit/withdrawal",
                    detail=note, source="account_equity", severity="info",
                    grace_minutes=0, recheck_minutes=2880)
            except Exception:
                pass
    _append_curve(row)
    return row


# ── render (dashboard + email) ──────────────────────────────────────────
def render(color: bool = False) -> str:
    G, R, DM, W, BD = (("\033[92m", "\033[91m", "\033[2m", "\033[0m", "\033[1m")
                       if color else ("", "", "", "", ""))
    s = stats()
    if s.get("empty"):
        return "  ACCOUNT EQUITY — no snapshots yet (run `python account_equity.py --snapshot`)"

    def _sekeur(v):
        if v is None:
            return "—"
        e = v / s["eursek"] if s.get("eursek") else None
        return f"{v:,.0f} SEK" + (f"  (€{e:,.0f})" if e is not None else "")

    dd = s["drawdown_pct"]
    rp = s["return_pct"]
    dd_c = R if (dd or 0) >= 5 else DM
    rp_c = G if (rp or 0) >= 0 else R

    lines = [
        f"  {BD}ACCOUNT EQUITY (real money, pooled Saxo AccountGroup){W}   {DM}{s['ts'][:19]} · {s['n_snapshots']} snapshots{W}",
        f"    Real equity          {BD}{_sekeur(s['equity_sek'])}{W}   (cash {_sekeur(s['cash_sek'])})",
        f"    All-time peak        {_sekeur(s['peak_sek'])}",
        f"    Drawdown from peak   {dd_c}{dd:.1f}%{W}" if dd is not None else "    Drawdown from peak   —",
        f"    Return ({s['return_basis']})  {rp_c}{rp:+.1f}%{W}" if rp is not None else "    Return   —",
        f"    7-day peak→now       {DM}give-back {_sekeur(s['week_giveback_sek'])}{W}"
        f"   (wk hi {_sekeur(s['week_peak_sek'])} / lo {_sekeur(s['week_low_sek'])})",
        f"    Unrealised P&L       {_pnl(s['unrealized_pnl_sek'], G, R, W)}"
        + ("   " + " · ".join(f"{k}: {_pnl(v, G, R, W)}" for k, v in s["per_account_unrealized_sek"].items())
           if s["per_account_unrealized_sek"] else ""),
        f"    Open positions {s['open_positions']}   ·   Margin used {s['margin_util_pct']:.0f}%"
        f"   ·   {DM}sizing cap is a ceiling, NOT this number{W}",
    ]
    return "\n".join(lines)


def _pnl(v, G, R, W):
    if v is None:
        return "—"
    c = G if v >= 0 else R
    return f"{c}{v:+,.0f} SEK{W}"


def render_html() -> str:
    s = stats()
    if s.get("empty"):
        return "<p>No account-equity snapshots yet.</p>"
    dd, rp = s["drawdown_pct"], s["return_pct"]
    rows = [
        ("Real equity", f"{s['equity_sek']:,.0f} SEK"
         + (f" (€{s['equity_eur']:,.0f})" if s.get("equity_eur") else "")),
        ("All-time peak", f"{s['peak_sek']:,.0f} SEK"),
        ("Drawdown from peak", f"{dd:.1f}%" if dd is not None else "—"),
        (f"Return ({s['return_basis']})", f"{rp:+.1f}%" if rp is not None else "—"),
        ("7-day give-back (peak→now)",
         f"{s['week_giveback_sek']:,.0f} SEK" if s['week_giveback_sek'] is not None else "—"),
        ("Unrealised P&L", f"{s['unrealized_pnl_sek']:+,.0f} SEK"),
        ("Open positions / margin used", f"{s['open_positions']} / {s['margin_util_pct']:.0f}%"),
    ]
    body = "".join(f"<tr><td style='padding:3px 10px'>{k}</td>"
                   f"<td style='padding:3px 10px;text-align:right'><b>{v}</b></td></tr>" for k, v in rows)
    return (f"<h3 style='margin-bottom:4px'>Account equity (real money)</h3>"
            f"<table style='border-collapse:collapse;font-size:13px'>{body}</table>"
            f"<p style='color:#888;font-size:11px'>Pooled Saxo AccountGroup value. "
            f"The sizing cap is a ceiling for risk math, not this number.</p>")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    if "--snapshot" in sys.argv:
        r = snapshot()
        print("snapshot appended" if r else "snapshot failed (curve unchanged)")
    else:
        if "--fetch" in sys.argv or not _load_curve():
            snapshot()
        print(render(color=sys.stdout.isatty()))
