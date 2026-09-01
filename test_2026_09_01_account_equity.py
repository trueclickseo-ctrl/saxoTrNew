"""
2026-09-01 -- account_equity.py: the one honest real-money account view.

Spike finding: Saxo's OpenAPI does NOT split /balances/me per sub-account
when they share a margin group -- it returns the pooled AccountGroup total
in SEK regardless of AccountKey/ClientKey. That pooled TotalValue is the
real equity and is what constrains trading, so it's what we track. The old
*_peak_equity.json stored the CAPPED sizing number as "peak" -- meaningless.

Reporting only: snapshot() appends a curve row, stats() derives peak /
drawdown% / return / 7-day give-back, render()/render_html() are the
dashboard + email blocks. Nothing here gates a trade.
"""
import ast
import inspect
import json
import os
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


import account_equity as ae

_seq = 0


def _fresh():
    global _seq
    _seq += 1
    cp = os.path.join(BASE, "data", f"_test_ae_curve_{_seq}.jsonl")
    dp = os.path.join(BASE, "data", f"_test_ae_deps_{_seq}.json")
    for p in (cp, dp):
        try:
            os.remove(p)
        except OSError:
            pass
    ae.CURVE_PATH, ae.DEPOSITS_PATH = cp, dp
    return cp, dp


def _rm(*paths):
    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass


def _row(ts_days_ago, tv, pnl=0.0):
    return {"ts": (datetime.now(timezone.utc) - timedelta(days=ts_days_ago)).isoformat(),
            "total_value_sek": tv, "total_value_eur": round(tv / 11.14, 0),
            "cash_sek": tv * 0.4, "unrealized_pnl_sek": pnl, "open_positions": 5,
            "per_account_unrealized_sek": {"SEK": pnl}, "margin_util_pct": 30.0, "eursek": 11.14}


# ── stats ────────────────────────────────────────────────────────────────
def test_empty_curve():
    cp, dp = _fresh()
    try:
        assert ae.stats() == {"empty": True}
        assert "no snapshots" in ae.render(color=False)
    finally:
        _rm(cp, dp)


def test_peak_drawdown_return_from_curve():
    cp, dp = _fresh()
    try:
        with open(cp, "w") as f:
            for r in (_row(6, 30000), _row(4, 34000), _row(2, 36000), _row(0, 33000)):
                f.write(json.dumps(r) + "\n")
        json.dump({"entries": [{"date": "2026-08-21", "sek": 30000}]}, open(dp, "w"))
        s = ae.stats()
        assert s["equity_sek"] == 33000
        assert s["peak_sek"] == 36000
        assert s["drawdown_pct"] == round((36000 - 33000) / 36000 * 100, 2)   # 8.33
        assert s["return_pct"] == round((33000 - 30000) / 30000 * 100, 2)     # +10.0
        assert "inception" in s["return_basis"]
        assert s["week_peak_sek"] == 36000 and s["week_giveback_sek"] == 3000
    finally:
        _rm(cp, dp)


def test_return_basis_falls_back_to_first_row_without_deposits():
    cp, dp = _fresh()
    try:
        with open(cp, "w") as f:
            f.write(json.dumps(_row(3, 10000)) + "\n")
            f.write(json.dumps(_row(0, 10500)) + "\n")
        s = ae.stats()
        assert s["return_pct"] == 5.0 and "tracking started" in s["return_basis"]
    finally:
        _rm(cp, dp)


def test_net_deposits():
    cp, dp = _fresh()
    try:
        assert ae.net_deposits_sek() is None            # no file
        json.dump({"entries": [{"sek": 6000}, {"sek": 20000}]}, open(dp, "w"))
        assert ae.net_deposits_sek() == 26000
    finally:
        _rm(cp, dp)


# ── snapshot: fetch failure + deposit detection ─────────────────────────
def test_snapshot_fetch_failure_leaves_curve_untouched():
    cp, dp = _fresh()
    real = ae._fetch
    ae._fetch = lambda: None
    try:
        with open(cp, "w") as f:
            f.write(json.dumps(_row(1, 10000)) + "\n")
        assert ae.snapshot() is None
        assert len(open(cp).read().strip().splitlines()) == 1   # unchanged
    finally:
        ae._fetch = real
        _rm(cp, dp)


def test_snapshot_flags_a_suspected_transfer():
    cp, dp = _fresh()
    real = ae._fetch
    # prev row: 10,000 / +0 pnl.  new: 30,000 / +50 pnl -> +20,000 TV, +50 pnl
    # -> unexplained ~+19,950 -> flagged.
    ae._fetch = lambda: {"ts": datetime.now(timezone.utc).isoformat(),
                         "total_value_sek": 30000.0, "unrealized_pnl_sek": 50.0,
                         "open_positions": 5, "per_account_unrealized_sek": {}}
    try:
        with open(cp, "w") as f:
            f.write(json.dumps({"ts": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
                                "total_value_sek": 10000.0, "unrealized_pnl_sek": 0.0}) + "\n")
        row = ae.snapshot()
        assert row is not None and row.get("suspected_transfer_sek")
        assert abs(row["suspected_transfer_sek"] - 19950) < 100
    finally:
        ae._fetch = real
        _rm(cp, dp)


def test_normal_pnl_move_is_not_flagged():
    cp, dp = _fresh()
    real = ae._fetch
    ae._fetch = lambda: {"ts": datetime.now(timezone.utc).isoformat(),
                         "total_value_sek": 10120.0, "unrealized_pnl_sek": 120.0,
                         "open_positions": 5, "per_account_unrealized_sek": {}}
    try:
        with open(cp, "w") as f:
            f.write(json.dumps({"ts": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
                                "total_value_sek": 10000.0, "unrealized_pnl_sek": 0.0}) + "\n")
        row = ae.snapshot()
        assert "suspected_transfer_sek" not in row     # +120 TV explained by +120 pnl
    finally:
        ae._fetch = real
        _rm(cp, dp)


# ── render never crashes ────────────────────────────────────────────────
def test_render_and_html_on_real_shape():
    cp, dp = _fresh()
    try:
        with open(cp, "w") as f:
            for r in (_row(5, 35000, 100), _row(0, 34000, -50)):
                f.write(json.dumps(r) + "\n")
        txt = ae.render(color=False)
        assert "Real equity" in txt and "Drawdown from peak" in txt
        html = ae.render_html()
        assert "<table" in html and "Real equity" in html
    finally:
        _rm(cp, dp)


# ── wiring / report-only ────────────────────────────────────────────────
def test_runner_snapshots_on_live_only_and_never_gates():
    import forex.runner as fr
    src = inspect.getsource(fr.run_daily)
    assert "account_equity.snapshot()" in src
    assert 'ACCOUNT_ENV == "live"' in src[src.index("account_equity.snapshot()") - 200:
                                          src.index("account_equity.snapshot()")]
    # module contains no order / gate calls
    m = inspect.getsource(ae)
    for bad in ("place_order", "_run_entries", "_run_exits", "allows_entry", "raise SystemExit"):
        assert bad not in m, f"account_equity must be reporting-only, found {bad!r}"


def test_daily_summary_and_dashboard_wired():
    import daily_summary as ds
    assert "_account_equity_section()" in inspect.getsource(ds.build_summary) if hasattr(ds, "build_summary") \
        else "_account_equity_section()" in inspect.getsource(ds)
    assert hasattr(ds, "_account_equity_section")
    import forex_live_dashboard as fld
    assert "account_equity" in inspect.getsource(fld)


def test_modules_parse():
    ast.parse(inspect.getsource(ae))


for _n, _f in list(globals().items()):
    if _n.startswith("test_") and callable(_f):
        _run(_n, _f)

# tidy any leftovers
for p in os.listdir(os.path.join(BASE, "data")):
    if p.startswith("_test_ae_"):
        _rm(os.path.join(BASE, "data", p))

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
