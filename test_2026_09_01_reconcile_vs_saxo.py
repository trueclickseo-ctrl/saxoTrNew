"""
Regression test -- 2026-09-01 reconcile_closed_trades_vs_saxo.py

A deterministic backstop: after every LIVE forex run, re-check each
recently-closed trade against Saxo's own ClosedPosition record and correct
any price drift in the ledger + observation cards (the substrate the AI
journal and P2 give-back learn from). LIVE only -- Saxo SIM's
closedpositions endpoint returns HTTP 400.

Covers: the bps/time helpers, symbol+amount+side+close-time matching
(Saxo's ClosedPosition carries no SourceOrderId / AccountKey), the
entry/exit observation-card pairing (only the ENTRY event carries
account_env/symbol), the Saxo-derived FX rate used to re-scale risk_eur,
the full dry-run -> apply -> idempotent-rerun cycle on a temp DB, and the
read-only / never-raises guarantees.
"""

import ast
import inspect
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

G, R, Y, X, B = "\033[92m", "\033[91m", "\033[93m", "\033[0m", "\033[1m"
_res = []


def _run(n, f):
    try:
        f()
        _res.append((n, True, None))
    except Exception as e:
        import traceback
        _res.append((n, False, f"{type(e).__name__}: {e}\n{traceback.format_exc()}"))


import reconcile_closed_trades_vs_saxo as rc


# ── helpers ────────────────────────────────────────────────────────────
def test_bps():
    assert rc._bps(1.0, 1.0) == 0.0
    assert round(rc._bps(1.0005, 1.0), 1) == 5.0
    assert rc._bps(1.0, 0.0) == 0.0          # no divide-by-zero


def test_ledger_ts_to_utc_is_pkt():
    # naive ledger stamp is PKT (UTC+5)
    got = rc._ledger_ts_to_utc("2026-09-01T02:15:57")
    assert got == datetime(2026, 8, 31, 21, 15, 57, tzinfo=timezone.utc)
    assert rc._ledger_ts_to_utc(None) is None
    assert rc._ledger_ts_to_utc("garbage") is None


def test_parse_utc_forms():
    a = rc._parse_utc("2026-08-31T21:16:00.5Z")
    b = rc._parse_utc("2026-08-31T21:16:00.5+00:00")
    assert a == b and a.tzinfo == timezone.utc
    assert rc._parse_utc(None) is None


# ── matching ───────────────────────────────────────────────────────────
def _cp(symbol="MXNUSD", amount=20000, side="Buy", op=0.058687, clp=0.058811,
        close="2026-08-31T21:16:00Z", gross=2.47, cost=6.0):
    return {"symbol": symbol, "amount": float(amount), "side": side,
            "open_price": op, "close_price": clp,
            "gross_quote": gross, "cost_quote": cost,
            "open_time": rc._parse_utc("2026-08-28T19:15:03Z"),
            "close_time": rc._parse_utc(close), "opening_position_id": "x"}


def _row(**kw):
    d = dict(id=1, module="forex_live_eur", strategy="rsi", symbol="MXNUSD",
             direction="Buy", quantity=20000.0, entry_price=0.05999,
             exit_price=0.05884, realized_pnl=-3.0, currency="EUR",
             timestamp_open="2026-08-29T00:15:00", timestamp_close="2026-09-01T02:15:57",
             commission=0.0, status="closed")
    d.update(kw)
    return d


def test_match_ok():
    st, cp = rc._match(_row(), [_cp()])
    assert st == "ok" and cp["symbol"] == "MXNUSD"


def test_match_none_when_symbol_or_side_or_qty_or_time_differ():
    assert rc._match(_row(), [_cp(symbol="EURUSD")])[0] == "none"
    assert rc._match(_row(), [_cp(side="Sell")])[0] == "none"
    assert rc._match(_row(), [_cp(amount=5000)])[0] == "none"
    assert rc._match(_row(), [_cp(close="2026-08-20T00:00:00Z")])[0] == "none"


def test_match_ambiguous_then_tiebreak():
    near = _cp(close="2026-08-31T21:16:00Z")
    far  = _cp(close="2026-08-31T21:16:40Z")   # 40s from the other, both within window
    st, cands = rc._match(_row(), [near, far])
    assert st == "ambiguous" and len(cands) == 2
    # >60s apart -> tie-break picks the closest
    far2 = _cp(close="2026-08-31T21:18:00Z")
    st2, cp2 = rc._match(_row(), [near, far2])
    assert st2 == "ok" and cp2 is near


# ── observation-card pairing ───────────────────────────────────────────
def _cards():
    cid = "live_eur:rsi:MXNUSD:2026-08-28T19:15:00+00:00"
    return [
        {"card_id": cid, "event": "entry", "account_env": "live_eur", "symbol": "MXNUSD",
         "strategy": "rsi", "entry_price": 0.05999, "current_stop": 0.0584189,
         "quantity": 20000, "risk_eur": 27.15},
        # exit event carries NO account_env / symbol -- only card_id
        {"card_id": cid, "event": "exit", "timestamp": "2026-08-31T21:16:01+00:00",
         "exit_price": 0.05884, "net_pnl_eur": -3.05, "r_multiple": -0.11},
        # a decoy from another account, same symbol
        {"card_id": "live:rsi:MXNUSD:2026-08-01T00:00:00+00:00", "event": "entry",
         "account_env": "live", "symbol": "MXNUSD", "strategy": "rsi",
         "entry_price": 0.05, "current_stop": 0.049, "quantity": 1000, "risk_eur": 5.0},
    ]


def test_match_card_pairs_entry_and_bare_exit():
    e, x = rc._match_card(_row(), _cards())
    assert e is not None and x is not None
    assert e["account_env"] == "live_eur" and x["event"] == "exit"


def test_match_card_respects_strategy_and_window():
    assert rc._match_card(_row(strategy="donchian"), _cards()) == (None, None)
    far = _row(timestamp_close="2026-09-05T00:00:00")
    assert rc._match_card(far, _cards()) == (None, None)


# ── FX rate + card correction ──────────────────────────────────────────
def test_eur_per_quote_prefers_saxo_numbers():
    e, x = _cards()[0], _cards()[1]
    # net_quote = 2.47 - 6.0 = -3.53 ; net_eur = -3.05 -> ~0.864
    rate = rc._eur_per_quote(e, x, _cp(), e["entry_price"])
    assert 0.80 < rate < 0.92
    # fallback when no net_pnl_eur: back-derive from risk / stop distance
    x2 = {"event": "exit"}
    rate2 = rc._eur_per_quote(e, x2, _cp(), e["entry_price"])
    assert rate2 == e["risk_eur"] / (abs(e["entry_price"] - e["current_stop"]) * e["quantity"])


def test_correct_card_fixes_price_risk_and_r():
    e, x = _cards()[0], _cards()[1]
    notes = rc._correct_card(e, x, _cp())
    assert abs(e["entry_price"] - 0.058687) < 1e-6
    assert e["price_source"] == rc.MARKER
    assert 3.5 < e["risk_eur"] < 6.0                 # re-scaled onto the real entry
    assert abs(x["exit_price"] - 0.058811) < 1e-6
    assert x["r_multiple"] == round(-3.05 / e["risk_eur"], 2)
    assert notes


def test_correct_card_noop_within_tolerance():
    e = {"event": "entry", "entry_price": 0.0586880, "current_stop": 0.0584189,
         "quantity": 20000, "risk_eur": 4.63}
    x = {"event": "exit", "exit_price": 0.0588110, "net_pnl_eur": -3.05}
    assert rc._correct_card(e, x, _cp()) == []


# ── _saxo_closed never raises ──────────────────────────────────────────
def test_saxo_closed_returns_empty_on_error(monkey=None):
    import saxo_client as sc
    orig = sc._request_with_retry
    sc._request_with_retry = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
    try:
        assert rc._saxo_closed("live") == []
    finally:
        sc._request_with_retry = orig


# ── full flow on a temp DB + temp cards ────────────────────────────────
def test_end_to_end_dry_then_apply_then_idempotent():
    tmpdb = os.path.join(BASE, "data", "_test_reconcile.db")
    tmpcards = os.path.join(BASE, "data", "_test_reconcile_cards.jsonl")
    for p in (tmpdb, tmpcards):
        if os.path.exists(p):
            os.remove(p)
    con = sqlite3.connect(tmpdb)
    con.execute("""CREATE TABLE trades (id INTEGER PRIMARY KEY, module TEXT, strategy TEXT,
        symbol TEXT, direction TEXT, quantity REAL, entry_price REAL, exit_price REAL,
        stop_price REAL, realized_pnl REAL, currency TEXT, order_id TEXT, exit_reason TEXT,
        status TEXT, timestamp_open TEXT, timestamp_close TEXT, source_ref TEXT,
        created_at TEXT, commission REAL, gap_type TEXT)""")
    con.execute("INSERT INTO trades (id,module,strategy,symbol,direction,quantity,"
                "entry_price,exit_price,realized_pnl,currency,status,timestamp_open,timestamp_close)"
                " VALUES (1,'forex_live_eur','rsi','MXNUSD','Buy',20000,0.05999,0.05884,"
                "-3.0,'EUR','closed','2026-08-29T00:15:00','2026-09-01T02:15:57')")
    # an old trade Saxo no longer retains
    con.execute("INSERT INTO trades (id,module,strategy,symbol,direction,quantity,"
                "entry_price,exit_price,realized_pnl,currency,status,timestamp_open,timestamp_close)"
                " VALUES (2,'forex_live','donchian','CHFAUD','Sell',1000,1.7333,1.7312,"
                "-4.6,'EUR','closed','2026-08-26T11:15:43','2026-08-27T00:13:07')")
    con.commit()
    con.close()
    with open(tmpcards, "w", encoding="utf-8") as fh:
        for c in _cards()[:2]:
            fh.write(json.dumps(c) + "\n")

    o_led, o_cards, o_saxo = rc.LEDGER, rc.CARDS, rc._saxo_closed
    rc.LEDGER, rc.CARDS = tmpdb, tmpcards
    rc._saxo_closed = lambda env: [_cp()] if env == "live_eur" else []
    try:
        # dry run: flags, writes nothing
        fs = rc.reconcile(apply=False, since="2026-08-20")
        mx = next(f for f in fs if f["symbol"] == "MXNUSD")
        assert mx["match"] == "ok" and mx["ledger_changed"] and mx["card_changed"]
        ch = next(f for f in fs if f["symbol"] == "CHFAUD")
        assert ch["match"] == "none" and not ch["ledger_changed"]
        con = sqlite3.connect(tmpdb)
        assert con.execute("SELECT entry_price FROM trades WHERE id=1").fetchone()[0] == 0.05999
        con.close()

        # apply
        rc.reconcile(apply=True, since="2026-08-20")
        con = sqlite3.connect(tmpdb)
        ep, xp = con.execute("SELECT entry_price, exit_price FROM trades WHERE id=1").fetchone()
        con.close()
        assert abs(ep - 0.058687) < 1e-6 and abs(xp - 0.058811) < 1e-6
        cards = [json.loads(l) for l in open(tmpcards, encoding="utf-8")]
        ecard = next(c for c in cards if c["event"] == "entry")
        assert abs(ecard["entry_price"] - 0.058687) < 1e-6
        assert ecard["price_source"] == rc.MARKER

        # idempotent: second apply finds nothing to change
        fs2 = rc.reconcile(apply=True, since="2026-08-20")
        assert not any(f["ledger_changed"] or f["card_changed"] for f in fs2)
    finally:
        rc.LEDGER, rc.CARDS, rc._saxo_closed = o_led, o_cards, o_saxo
        for p in (tmpdb, tmpcards, tmpcards + ".tmp"):
            try:
                os.path.exists(p) and os.remove(p)
            except OSError:
                pass


def test_run_never_raises_even_if_saxo_client_explodes():
    o = rc._saxo_closed
    rc._saxo_closed = lambda env: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        assert rc.run(env="live") == 0          # swallowed, returns 0
    finally:
        rc._saxo_closed = o


# ── read-only / wiring ─────────────────────────────────────────────────
def test_module_is_read_only_wrt_trading():
    src = inspect.getsource(rc)
    for banned in ("place_with_stop", "place_market_order", "place_stop_only",
                   "cancel_order", "_amend_stop", "_replace_stop", "/trade/v2/orders"):
        assert banned not in src, f"reconcile must not touch orders: {banned}"
    # only closed rows are ever UPDATEd, never INSERT/DELETE
    assert "INSERT INTO trades" not in src and "DELETE FROM trades" not in src
    assert "status='closed'" in src


def test_forex_runner_wires_it_live_only():
    import forex.runner as fr
    s = inspect.getsource(fr._reconcile_closed_vs_saxo)
    assert 'ACCOUNT_ENV not in ("live", "live_eur")' in s and "return" in s
    assert "reconcile_closed_trades_vs_saxo" in s
    assert "_reconcile_closed_vs_saxo()" in inspect.getsource(fr.run_daily)
    assert "_reconcile_closed_vs_saxo()" in inspect.getsource(fr.run_exits_only)


def test_module_parses():
    ast.parse(inspect.getsource(rc))


for _n, _f in list(globals().items()):
    if _n.startswith("test_") and callable(_f):
        _run(_n, _f)

print(f"\n{B}{'=' * 66}{X}")
bad = [(n, e) for n, ok, e in _res if not ok]
for n, ok, e in _res:
    print(f"  [{G}PASS{X}]" if ok else f"  [{R}FAIL{X}]", n)
    if e:
        print(f"      {Y}{e}{X}")
print(f"{B}{'=' * 66}{X}")
if bad:
    print(f"{R}{B}  {len(bad)} / {len(_res)} FAILED{X}")
    sys.exit(1)
print(f"{G}{B}  ALL {len(_res)} TESTS PASSED{X}")
sys.exit(0)
