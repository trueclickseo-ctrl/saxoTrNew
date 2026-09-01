"""
Regression test -- 2026-09-01 implausible SIM net-P&L gate.

Incident: forex/runner._run_exits took net P&L straight from Saxo's
positions/me (ProfitLossOnTrade + TradeCostsTotal). On SIM that field is
unreliable: sim:rsi:MXNUSD (Buy 170k) moved -$3.91 on price but SIM
reported ~+$11 net, so:
  * pnl_ledger.realized_pnl booked +8.23 EUR (a WIN)
  * the WIN/LOSS email said "WIN ✓  P&L -0.04%  +$10" (badge and % disagree)
  * signal_filter labelled the loss a WIN for ML training
  * the observation card carried +0.12R for the journal / give-back
Also 23 NZDPLN / CHFMXN cards showed a NEGATIVE commission (a rebate --
impossible).

Fixes:
  * forex/runner._sane_net_pnl_quote: commission is always a cost, so a
    trustworthy net is <= gross price move and within a sane band; when
    Saxo's figure fails that, rebuild net = gross - Saxo-quoted round-trip
    commission and flag net_pnl_reconstructed.
  * forex/notifier.send_trade_closed: net_reconstructed -> "(estimated:
    price move - modeled cost)" note.
  * forward_observation.log_trade_exit_card: net_pnl_reconstructed field.
  * fix_impossible_commission_2026-09-01.py: recomputed the MXNUSD SIM
    record; nulled the 23 thin-exotic / re-entry-loop records as
    pnl_suspect; report_giveback + trade_journal skip pnl_suspect.
"""

import ast
import inspect
import json
import os
import sys

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


import forex.runner as fr
import forex.notifier as fx_notify


def _sane(net, entry, exit_px, qty, is_long):
    # stub the commission lookup so the test is offline & deterministic
    o = fr._round_trip_cost_quote_ccy
    fr._round_trip_cost_quote_ccy = lambda uic, q, ak: 6.0
    try:
        return fr._sane_net_pnl_quote(net, entry, exit_px, qty, is_long, 1, "AK", "MXNUSD")
    finally:
        fr._round_trip_cost_quote_ccy = o


# ── _sane_net_pnl_quote ────────────────────────────────────────────────
def test_none_passes_through():
    assert _sane(None, 1.0, 1.0, 1000, True) == (None, False)


def test_plausible_net_is_kept():
    # long, price +0.002*10000 = +20 gross; net +14 after 6 cost -> fine
    val, rebuilt = _sane(14.0, 1.0000, 1.0020, 10000, True)
    assert val == 14.0 and rebuilt is False


def test_legit_cost_driven_sign_flip_on_a_small_trade_is_kept():
    # LIVE MXNUSD: gross +2.5, commission 6 -> net -3.5. Real, not rebuilt.
    val, rebuilt = _sane(-3.5, 0.058687, 0.058811, 20000, True)
    assert abs(val - (-3.5)) < 1e-9 and rebuilt is False


def test_negative_implied_cost_is_rebuilt():
    # the MXNUSD SIM case: gross -3.91, SIM "net" +9.6 -> implied cost -13.5 (rebate)
    gross = (0.0588525 - 0.0588755) * 170000            # ~ -3.91
    val, rebuilt = _sane(9.6, 0.0588755, 0.0588525, 170000, True)
    assert rebuilt is True
    assert val < 0 and abs(val - (gross - 6.0)) < 1e-6   # gross - round-trip cost


def test_absurdly_large_cost_is_rebuilt():
    # gross -3.91 but SIM "net" -250 -> implied cost 246, way over the band
    val, rebuilt = _sane(-250.0, 0.0588755, 0.0588525, 170000, True)
    assert rebuilt is True and val > -250


def test_rebuilt_value_reduces_pnl_never_increases_it():
    gross = (0.0588525 - 0.0588755) * 170000
    val, rebuilt = _sane(9.6, 0.0588755, 0.0588525, 170000, True)
    assert val <= gross + 1e-9                           # cost only ever hurts


# ── wiring in _run_exits ──────────────────────────────────────────────
def test_run_exits_gates_and_propagates_the_flag():
    src = inspect.getsource(fr._run_exits)
    assert "_sane_net_pnl_quote(" in src
    i_pos = src.index("_position_net_pnl_quote_ccy(uic, qty, direction, entry)")
    i_sane = src.index("_sane_net_pnl_quote(")
    assert i_pos < i_sane                                # gate runs on the raw value
    assert "_net_rebuilt" in src
    assert "net_reconstructed=_net_rebuilt" in src       # -> email
    assert "net_pnl_reconstructed=_net_rebuilt" in src   # -> observation card


# ── notifier ─────────────────────────────────────────────────────────
def test_notifier_shows_estimated_note(monkeypatch=None):
    sent = {}
    o = fx_notify._send
    fx_notify._send = lambda subj, html: sent.update(subject=subj, html=html)
    try:
        fx_notify.send_trade_closed(
            strategy="rsi", symbol="MXNUSD", direction="Buy",
            entry=0.0588755, exit_px=0.0588525, pnl_pct=-0.04, units=170000,
            reason="rsi_recovery", live=False, net_pnl_native=-8.55,
            net_reconstructed=True)
        assert "estimated" in sent["html"].lower()
        assert "LOSS" in sent["html"]                    # -8.55 -> loss, consistent with -0.04%
    finally:
        fx_notify._send = o


# ── give-back + journal skip pnl_suspect ─────────────────────────────
def test_giveback_and_journal_skip_pnl_suspect():
    import report_giveback
    assert 'x.get("pnl_suspect")' in inspect.getsource(report_giveback._load_trades)
    import ai.features.trade_journal as tj
    assert 'x.get("pnl_suspect")' in inspect.getsource(tj._closed_trades)


# ── the one-time correction landed ──────────────────────────────────
def test_correction_applied_to_records():
    import sqlite3
    con = sqlite3.connect(os.path.join(BASE, "data", "pnl_ledger.db"))
    sim = con.execute("SELECT realized_pnl FROM trades WHERE id=1756").fetchone()[0]
    live = con.execute("SELECT realized_pnl FROM trades WHERE id=1750").fetchone()[0]
    n_null = con.execute("SELECT COUNT(*) FROM trades WHERE symbol IN ('NZDPLN','CHFMXN') "
                         "AND status='closed' AND realized_pnl IS NULL").fetchone()[0]
    con.close()
    assert abs(sim - (-8.55)) < 0.01, sim               # SIM MXNUSD: loss, not +8.23
    assert live < 0, live                               # LIVE MXNUSD untouched, still a loss
    assert n_null >= 20                                 # thin-exotic loop nulled

    cards = os.path.join(BASE, "data", "trade_observation_cards.jsonl")
    suspect = recomputed = 0
    for ln in open(cards, encoding="utf-8"):
        d = json.loads(ln)
        if d.get("pnl_suspect"):
            suspect += 1
            assert d.get("net_pnl_eur") is None
        if d.get("net_pnl_reconstructed") and not d.get("pnl_suspect"):
            recomputed += 1
    assert suspect >= 20 and recomputed >= 1


def test_modules_parse():
    for m in (fr, fx_notify):
        ast.parse(inspect.getsource(m))


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
