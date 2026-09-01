"""
2026-09-01 -- the ONE "ATOS needs a human" alert channel (attention.py)
and its wiring.

User: "set up ATOS like we have minimum human interaction; if human
interaction is needed we should get email."

  * attention.raise_attention / clear_attention / flush -- an item emails
    once it has persisted past its grace period, then nags once a day
    until it clears; a caller that stops raising it => auto-expires.
  * SIM `fully_untracked` positions are FLAT-CLOSED by safeguard.py (paper
    money, unmanaged) -- no email.
  * LIVE `fully_untracked` positions are NEVER auto-closed -- safeguard_
    live[_eur].py escalate via attention.
  * forex/runner._note_operational_blocks routes the 50% margin cap and
    the venue circuit breaker into the same channel.
"""
import ast
import inspect
import json
import os
import sys
from datetime import datetime, timedelta

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


import attention
import housekeeping
import safeguard
import safeguard_live
import safeguard_live_eur


_seq = 0


def _fresh_state():
    global _seq
    _seq += 1
    p = os.path.join(BASE, "data", f"_test_attention_{_seq}.json")
    try:
        os.remove(p)
    except OSError:
        pass
    attention.STATE_PATH = p
    # no config/email.json path in tests -> _send_digest logs, returns False
    attention._EMAIL_CFG = os.path.join(BASE, "data", "_test_attention_no_such_email.json")
    return p


def _cleanup(p):
    for q in (p, p + ".tmp"):
        try:
            os.remove(q)
        except OSError:
            pass


# ── attention.py core ────────────────────────────────────────────────────
def test_raise_then_clear():
    p = _fresh_state()
    try:
        attention.raise_attention("k:1", title="T", detail="d", source="s")
        assert [i["key"] for i in attention.open_items()] == ["k:1"]
        attention.clear_attention("k:1")
        assert attention.open_items() == []
    finally:
        _cleanup(p)


def test_grace_period_gates_the_email():
    p = _fresh_state()
    try:
        attention.raise_attention("k:2", title="T", grace_minutes=999)
        r = attention.flush()
        assert r["open"] == 1 and r["escalated"] == 0 and r["emailed"] == 0
        # zero grace -> escalates on the next flush
        attention.raise_attention("k:3", title="T2", grace_minutes=0)
        r2 = attention.flush()
        assert r2["escalated"] >= 1
    finally:
        _cleanup(p)


def test_caller_stops_raising_then_item_auto_expires():
    p = _fresh_state()
    try:
        attention.raise_attention("k:4", title="T", grace_minutes=0, recheck_minutes=60)
        attention.flush()
        # rewind last_seen well past recheck_minutes
        st = json.load(open(p))
        st["k:4"]["last_seen"] = (datetime.now() - timedelta(minutes=120)).isoformat()
        json.dump(st, open(p, "w"))
        r = attention.flush()
        assert "k:4" in r["resolved"]
        assert attention.open_items() == []
    finally:
        _cleanup(p)


def test_daily_renag_not_more_often():
    p = _fresh_state()
    try:
        attention.raise_attention("k:5", title="T", grace_minutes=0)
        r1 = attention.flush()
        assert r1["emailed"] == 1 or r1["escalated"] == 1   # first escalation
        # immediate re-flush: still open, but NOT re-emailed
        attention.raise_attention("k:5", title="T")
        st = json.load(open(p))
        first_email = st["k:5"]["last_emailed"]
        attention.flush()
        st2 = json.load(open(p))
        assert st2["k:5"]["last_emailed"] == first_email     # unchanged within 24h
        # age last_emailed past the re-nag window
        st2["k:5"]["last_emailed"] = (datetime.now() - timedelta(hours=25)).isoformat()
        json.dump(st2, open(p, "w"))
        attention.raise_attention("k:5", title="T")
        attention.flush()
        st3 = json.load(open(p))
        assert st3["k:5"]["last_emailed"] != first_email     # re-nagged
    finally:
        _cleanup(p)


def test_never_raises_on_garbage():
    p = _fresh_state()
    try:
        open(p, "w").write("not json{{{")
        attention.raise_attention("k:6", title="T")        # must not raise
        attention.clear_attention("k:6")
        attention.flush()
    finally:
        _cleanup(p)


# ── Finding carries what SIM needs to auto-close ──────────────────────────
def test_finding_carries_uic_net_assettype():
    f = housekeeping.Finding("forex", housekeeping.KIND_FULLY_UNTRACKED, "EURCAD", "d",
                             uic=13, net_amount=-46000, asset_type="FxSpot")
    assert f.uic == 13 and f.net_amount == -46000 and f.asset_type == "FxSpot"
    src = inspect.getsource(housekeeping._scan_fully_untracked)
    assert "uic=uic, net_amount=net, asset_type=asset_type" in src


# ── SIM: fully_untracked -> flat-close, no email ─────────────────────────
def test_sim_untracked_is_auto_closed_short_and_long():
    calls = []
    real_post = safeguard.saxo_client.post
    real_key = safeguard.saxo_client.get_account_key
    safeguard.saxo_client.post = lambda path, body: (calls.append((path, body)) or {"OrderId": "SIMX1"})
    safeguard.saxo_client.get_account_key = lambda: "AK"
    try:
        f_long = housekeeping.Finding("forex", housekeeping.KIND_FULLY_UNTRACKED, "EURCAD", "d",
                                      uic=13, net_amount=46000, asset_type="FxSpot")
        o = safeguard._close_untracked_sim(f_long)
        assert o.fixed and o.auto_resolved and calls[-1][1]["BuySell"] == "Sell"
        assert calls[-1][1]["Amount"] == 46000 and calls[-1][1]["OrderType"] == "Market"

        f_short = housekeeping.Finding("forex", housekeeping.KIND_FULLY_UNTRACKED, "EURCAD", "d",
                                       uic=13, net_amount=-42000, asset_type="FxSpot")
        o2 = safeguard._close_untracked_sim(f_short)
        assert o2.fixed and calls[-1][1]["BuySell"] == "Buy" and calls[-1][1]["Amount"] == 42000
    finally:
        safeguard.saxo_client.post = real_post
        safeguard.saxo_client.get_account_key = real_key


def test_sim_untracked_close_rejection_is_not_fixed():
    real_post = safeguard.saxo_client.post
    real_key = safeguard.saxo_client.get_account_key
    def _boom(path, body):
        raise RuntimeError("WouldExceedMargin")
    safeguard.saxo_client.post = _boom
    safeguard.saxo_client.get_account_key = lambda: "AK"
    try:
        f = housekeeping.Finding("forex", housekeeping.KIND_FULLY_UNTRACKED, "EURCAD", "d",
                                 uic=13, net_amount=46000, asset_type="FxSpot")
        o = safeguard._close_untracked_sim(f)
        assert not o.fixed and not o.auto_resolved and "rejected it" in o.detail
    finally:
        safeguard.saxo_client.post = real_post
        safeguard.saxo_client.get_account_key = real_key


def test_sim_mismatch_branch_calls_the_closer():
    src = inspect.getsource(safeguard._fix_mismatches)
    assert "KIND_FULLY_UNTRACKED" in src and "_close_untracked_sim(f)" in src
    assert "no_local_record_needs_human_review" not in src   # the old mislabel is gone


# ── LIVE: fully_untracked -> needs_human, never closed, escalated ────────
def test_live_untracked_is_needs_human_not_fixed():
    for mod in (safeguard_live, safeguard_live_eur):
        src = inspect.getsource(mod)
        assert 'f.kind == "fully_untracked"' in src
        assert "needs_human=True" in src and '"needs_human_review", False' in src
        assert "attention.raise_attention" in src and "attention.flush()" in src
        assert "auto_close" not in src.lower()   # LIVE never auto-closes
        fo = mod.FixOutcomeLive if mod is safeguard_live else mod.FixOutcomeLiveEur
        assert "needs_human" in inspect.getsource(fo)


def test_live_escalation_routes_to_attention():
    raised = []
    real = attention.raise_attention
    attention.raise_attention = lambda key, **kw: raised.append((key, kw))
    try:
        o = safeguard_live.FixOutcomeLive("EURCAD", "needs_human_review", False, "d",
                                          uic=13, needs_human=True)
        safeguard_live._escalate_live([o])
        assert raised and raised[0][0] == "safeguard-live:EURCAD:needs_human_review"
        assert raised[0][1].get("severity") == "critical"
    finally:
        attention.raise_attention = real


# ── operational blocks in the runner ────────────────────────────────────
def test_runner_has_operational_block_hook():
    import forex.runner as fr
    assert hasattr(fr, "_note_operational_blocks")
    src = inspect.getsource(fr.run_daily)
    assert "_note_operational_blocks()" in src
    assert 'ACCOUNT_ENV in ("live", "live_eur") and attention is not None' in src
    hook = inspect.getsource(fr._note_operational_blocks)
    assert "margin-block" in hook and "venue-circuit-open" in hook
    assert "raise_attention" in hook and "clear_attention" in hook


def test_runner_margin_block_raises_and_clears():
    import forex.runner as fr
    if fr.attention is None:
        return
    raised, cleared = [], []
    r_r, r_c = fr.attention.raise_attention, fr.attention.clear_attention
    fr.attention.raise_attention = lambda key, **kw: raised.append(key)
    fr.attention.clear_attention = lambda key, **kw: cleared.append(key)
    real_env = fr.ACCOUNT_ENV
    real_get = fr._get
    try:
        fr.ACCOUNT_ENV = "live"
        fr._margin_cache["utilization"] = 71.0
        fr._note_operational_blocks()
        assert any("margin-block" in k for k in raised)
        raised.clear(); cleared.clear()
        fr._margin_cache["utilization"] = 20.0
        fr._note_operational_blocks()
        assert any("margin-block" in k for k in cleared)
    finally:
        fr.attention.raise_attention, fr.attention.clear_attention = r_r, r_c
        fr.ACCOUNT_ENV = real_env
        fr._get = real_get
        fr._margin_cache["utilization"] = None


def test_modules_parse():
    for m in (attention, safeguard, safeguard_live, safeguard_live_eur):
        ast.parse(inspect.getsource(m))


for _n, _f in list(globals().items()):
    if _n.startswith("test_") and callable(_f):
        _run(_n, _f)

print(f"\n{B}{'=' * 70}{X}")
bad = [(n, e) for n, ok, e in _res if not ok]
for n, ok, e in _res:
    print(f"  [{G}PASS{X}]" if ok else f"  [{R}FAIL{X}]", n)
    if e:
        print(f"      {Y}{e}{X}")
print(f"{B}{'=' * 70}{X}")
if bad:
    print(f"{R}{B}  {len(bad)} / {len(_res)} FAILED{X}")
    sys.exit(1)
print(f"{G}{B}  ALL {len(_res)} TESTS PASSED{X}")
sys.exit(0)
