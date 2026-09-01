"""
Regression test -- 2026-09-01 re-entry hardening + stacked-row cleanup.

A state/ledger race stacked 4 SIM RSI EURCAD longs (and 7 other
strategy/pair combos) in one afternoon: a scan's entry was written to
pnl_ledger.db immediately, but the state file (which `open_symbols` is
built from) only checkpointed at the END of that strategy's pass. If the
process was killed / the watchdog restarted it in between, the next scan's
`positions` didn't know about the just-placed entry and re-signalled the
same pair.

Three fixes:
  1. `_run_entries` / `_run_exits` now accept a `state` kwarg and
     checkpoint (`_save_state`) immediately after every single entry/exit,
     not just once at the end of the strategy's pass.
  2. `_run_entries` widens `open_symbols` with every symbol this strategy
     already has an OPEN row for in the pnl ledger -- a second, independent
     line of defense against exactly this state/ledger divergence.
  3. `dedup_stacked_reentries_2026-09-01.py` -- one-time cleanup of the
     already-stacked rows.
No signal/entry/exit logic changed -- this only prevents/repairs duplicate
bookkeeping.
"""
import ast
import inspect
import os
import sqlite3
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
import pnl_tracker


_tmp_seq = 0


def _tmp_path(suffix):
    # A real path under data/, not tempfile -- an open sqlite3 connection
    # keeps a Windows file lock past the `with` block (sqlite3's context
    # manager only commits/rolls back, it doesn't close), so cleanup must
    # be best-effort (see _rm below), same convention as
    # test_2026_09_01_verify_ai_data.py's orphan-ledger test.
    global _tmp_seq
    _tmp_seq += 1
    path = os.path.join(BASE, "data", f"_test_reentry_dedup_{_tmp_seq}{suffix}")
    _rm(path)
    return path


def _rm(path):
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def test_run_functions_accept_state_param():
    sig_e = inspect.signature(fr._run_entries)
    sig_x = inspect.signature(fr._run_exits)
    assert "state" in sig_e.parameters and sig_e.parameters["state"].default is None
    assert "state" in sig_x.parameters and sig_x.parameters["state"].default is None


def test_every_call_site_passes_state():
    src = inspect.getsource(fr)
    # every actual CALL (not the two `def` signatures) of _run_entries/
    # _run_exits threads state=state through -- 5 call sites as of
    # 2026-09-01 (2x _run_exits in run_exits_only, 2x legacy _run_exits,
    # 1x _run_entries in run_daily).
    n_calls = src.count("_run_exits(") + src.count("_run_entries(") - 2  # minus the 2 defs
    n_with_state = src.count("dry_run, today_str, state=state)") + src.count("regime_data=market_data, state=state)")
    assert n_calls == 5, f"expected 5 call sites, source has {n_calls}"
    assert n_with_state == n_calls, f"only {n_with_state}/{n_calls} call sites pass state=state"


def test_entries_checkpoint_immediately_in_source():
    src = inspect.getsource(fr._run_entries)
    i = src.index("entries += 1")
    seg = src[max(0, i - 400):i]
    assert "if state is not None and not dry_run:" in seg
    assert "_save_state(state)" in seg


def test_exits_checkpoint_immediately_in_source():
    src = inspect.getsource(fr._run_exits)
    i = src.index("del positions[key]")
    seg = src[i:i + 1000]
    assert "if state is not None and not dry_run:" in seg
    assert "_save_state(state)" in seg


def test_ledger_reentry_guard_in_source():
    src = inspect.getsource(fr._run_entries)
    assert "pnl_tracker.get_open_positions(module=_pnl_module())" in src
    i = src.index("_ledger_open_syms")
    j = src.index("open_syms = open_syms | _ledger_open_syms")
    assert i < j
    # merged BEFORE signal generation, not after
    k = src.index("generate_signals(")
    assert j < k


def test_modules_parse():
    ast.parse(inspect.getsource(fr))


# ── behavioural: the ledger query itself, on a real temp sqlite db ─────────
def test_get_open_positions_filters_by_strategy_and_module():
    real_db = pnl_tracker.DB_PATH
    tmp = _tmp_path(".db")
    pnl_tracker.DB_PATH = tmp
    try:
        pnl_tracker.log_open("forex", "rsi", "EURCAD", "Buy", 8000, 1.609, stop_price=1.600)
        pnl_tracker.log_open("forex", "ml", "EURCAD", "Sell", 5000, 1.609, stop_price=1.615)
        pnl_tracker.log_open("forex_live", "rsi", "EURCAD", "Buy", 8000, 1.609, stop_price=1.600)
        rows = pnl_tracker.get_open_positions(module="forex")
        by_strat = {r["strategy"] for r in rows if r["symbol"] == "EURCAD"}
        assert by_strat == {"rsi", "ml"}    # forex_live row excluded by module filter
        rsi_only = {r["symbol"] for r in rows if r["strategy"] == "rsi"}
        assert rsi_only == {"EURCAD"}
    finally:
        pnl_tracker.DB_PATH = real_db
        _rm(tmp)


def test_stacked_rows_reproduce_the_incident():
    # exact reproduction: two opens on the same (module, strategy, symbol)
    # with no close in between -- get_open_positions() must surface BOTH,
    # proving the merge into open_symbols would have included the symbol
    # even though state only ever holds one key for it.
    real_db = pnl_tracker.DB_PATH
    tmp = _tmp_path(".db")
    pnl_tracker.DB_PATH = tmp
    try:
        pnl_tracker.log_open("forex", "rsi", "EURCAD", "Buy", 10000, 1.6084, stop_price=1.600)
        pnl_tracker.log_open("forex", "rsi", "EURCAD", "Buy", 10000, 1.6088, stop_price=1.600)
        rows = [r for r in pnl_tracker.get_open_positions(module="forex")
               if r["strategy"] == "rsi" and r["symbol"] == "EURCAD"]
        assert len(rows) == 2, "both stacked rows must be visible to the guard"
    finally:
        pnl_tracker.DB_PATH = real_db
        _rm(tmp)


# ── the one-time cleanup script ─────────────────────────────────────────────
def test_dedup_script_dry_run_matches_state_and_closes_the_rest():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "dedup_stacked", os.path.join(BASE, "dedup_stacked_reentries_2026-09-01.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    tmp_db = _tmp_path(".db")
    tmp_state = _tmp_path(".json")
    real_db, real_state = mod.DB_PATH, mod.STATE_PATH
    mod.DB_PATH, mod.STATE_PATH = tmp_db, tmp_state
    try:
        import json
        with open(tmp_state, "w") as f:
            json.dump({"positions": {"rsi:EURCAD": {"direction": "Buy", "quantity": 10000,
                                                     "entry_price": 1.6088}}}, f)
        pnl_tracker.DB_PATH = tmp_db
        pnl_tracker.log_open("forex", "rsi", "EURCAD", "Buy", 10000, 1.6084, stop_price=1.600)
        pnl_tracker.log_open("forex", "rsi", "EURCAD", "Buy", 10000, 1.6088, stop_price=1.600)  # matches state
        # two stacked rows, no state key at all -> both are orphans
        pnl_tracker.log_open("forex", "rsi", "PLNHUF", "Sell", 50000, 84.8, stop_price=86.0)
        pnl_tracker.log_open("forex", "rsi", "PLNHUF", "Sell", 50000, 84.7, stop_price=86.0)

        actions = mod.plan()
        by_sym = {}
        for a in actions:
            by_sym.setdefault(a["symbol"], []).append(a)

        eurcad = by_sym["EURCAD"]
        kept = [a for a in eurcad if a["keep"]]
        assert len(kept) == 1 and abs(kept[0]["entry_price"] - 1.6088) < 1e-9

        plnhuf = by_sym["PLNHUF"]
        assert all(not a["keep"] for a in plnhuf), "no state key -> close ALL rows, keep none"

        # dry run must not write anything
        con = sqlite3.connect(tmp_db)
        n_open_before = con.execute("SELECT COUNT(*) FROM trades WHERE status='open'").fetchone()[0]
        con.close()
        assert n_open_before == 4

        # apply, then check the db
        sys.argv = ["dedup_stacked_reentries_2026-09-01.py", "--apply"]
        mod.main()
        con = sqlite3.connect(tmp_db)
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT * FROM trades").fetchall()
        con.close()
        open_now = [dict(r) for r in rows if r["status"] == "open"]
        closed = [dict(r) for r in rows if r["status"] == "closed"]
        assert len(open_now) == 1 and abs(open_now[0]["entry_price"] - 1.6088) < 1e-9
        assert len(closed) == 3
        # never fabricates a P&L
        assert all(r["realized_pnl"] is None for r in closed)
        # the in-state duplicate and the no-state orphan get distinct reasons
        reasons = {r["symbol"]: r["exit_reason"] for r in closed}
        assert reasons["EURCAD"] == "dedup_stacked_reentry_2026-09-01"
        assert reasons["PLNHUF"] == "reconciled_no_state"
    finally:
        mod.DB_PATH, mod.STATE_PATH = real_db, real_state
        pnl_tracker.DB_PATH = real_db
        _rm(tmp_db)
        _rm(tmp_state)
        tmp_dir = os.path.dirname(tmp_db)
        for p in os.listdir(tmp_dir):
            if p.startswith(os.path.basename(tmp_db) + ".bak_"):
                _rm(os.path.join(tmp_dir, p))


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
