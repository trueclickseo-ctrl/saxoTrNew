"""
2026-09-03 -- forex P&L VIEW reset to the 5-strategy roster era.

data/pnl_reset.json maps a module -> a "YYYY-MM-DD" cutoff. pnl_tracker's
closed-trade AGGREGATION reads then only count trades closed on/after that
date. Rows are NEVER deleted -- the full history stays in pnl_ledger.db.

Verifies: the cutoff is read + applied to the aggregation functions, other
modules are untouched, open positions are unaffected, and get_closed_trades
(raw rows for the learner / reports) still returns everything.
"""

import json
import os
import sqlite3
import sys
import tempfile

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


def _fresh_db(rows):
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE trades (id INTEGER PRIMARY KEY AUTOINCREMENT, module TEXT, strategy TEXT,
            symbol TEXT, direction TEXT, quantity REAL, entry_price REAL, exit_price REAL,
            stop_price REAL DEFAULT 0, realized_pnl REAL, commission REAL DEFAULT 0,
            currency TEXT DEFAULT 'USD', order_id TEXT, exit_reason TEXT, status TEXT DEFAULT 'open',
            timestamp_open TEXT, timestamp_close TEXT, source_ref TEXT, created_at TEXT);
    """)
    for r in rows:
        cols = ",".join(r); ph = ",".join("?" * len(r))
        c.execute(f"INSERT INTO trades ({cols}) VALUES ({ph})", tuple(r.values()))
    c.commit(); c.close()
    return path


def _with(reset_json, db_rows, fn):
    import importlib
    import pnl_tracker as pt
    importlib.reload(pt)
    rp = os.path.join(BASE, "data", "pnl_reset.json")
    real_rp_exists = os.path.exists(rp)
    real_rp = open(rp).read() if real_rp_exists else None
    real_db = pt.DB_PATH
    tmp_db = _fresh_db(db_rows)
    try:
        if reset_json is None:
            if os.path.exists(rp):
                os.remove(rp)
        else:
            json.dump(reset_json, open(rp, "w"))
        pt.DB_PATH = tmp_db
        return fn(pt)
    finally:
        pt.DB_PATH = real_db
        # restore the real reset file FIRST (a Windows unlink failure below
        # must not leave the committed pnl_reset.json overwritten)
        if real_rp is not None:
            open(rp, "w").write(real_rp)
        elif os.path.exists(rp):
            os.remove(rp)
        import gc; gc.collect()
        try:
            os.unlink(tmp_db)               # Windows may still hold a handle; harmless if it lingers in %TEMP%
        except OSError:
            pass


_ROWS = [
    dict(module="forex", strategy="rsi", symbol="EURUSD", status="closed",
         realized_pnl=100.0, timestamp_open="2026-08-01", timestamp_close="2026-08-15T10:00:00"),
    dict(module="forex", strategy="rsi", symbol="EURUSD", status="closed",
         realized_pnl=-10.0, timestamp_open="2026-09-03", timestamp_close="2026-09-03T14:00:00"),
    dict(module="forex", strategy="rsi", symbol="GBPUSD", status="open",
         realized_pnl=None, timestamp_open="2026-07-01", timestamp_close=None),
    dict(module="futures", strategy="donchian", symbol="NQ", status="closed",
         realized_pnl=500.0, timestamp_open="2026-08-01", timestamp_close="2026-08-10T10:00:00"),
]


def test_reset_since_reads_the_config():
    assert _with({"forex": "2026-09-01"}, [], lambda pt: pt.reset_since("forex")) == "2026-09-01"
    assert _with({"forex": "2026-09-01"}, [], lambda pt: pt.reset_since("stock")) is None
    assert _with(None, [], lambda pt: pt.reset_since("forex")) is None


def test_strategy_summary_excludes_pre_reset_forex_trades():
    def check(pt):
        rows = {r["strategy"]: r for r in pt.get_strategy_summary("forex")}
        return rows
    rows = _with({"forex": "2026-09-01"}, _ROWS, check)
    assert rows["rsi"]["total_pnl"] == -10.0        # only the 2026-09-03 trade, not the +100
    assert rows["rsi"]["trades"] == 1
    assert rows["rsi"]["open"] == 1                  # open count is NOT date-filtered


def test_no_reset_shows_all_time():
    rows = _with(None, _ROWS, lambda pt: {r["strategy"]: r for r in pt.get_strategy_summary("forex")})
    assert rows["rsi"]["total_pnl"] == 90.0         # 100 - 10
    assert rows["rsi"]["trades"] == 2


def test_other_modules_untouched_by_a_forex_reset():
    fut = _with({"forex": "2026-09-01"}, _ROWS,
                lambda pt: {r["strategy"]: r for r in pt.get_strategy_summary("futures")})
    assert fut["donchian"]["total_pnl"] == 500.0    # 2026-08-10 close, still counted


def test_get_summary_respects_the_reset_for_forex_only():
    def check(pt):
        return (pt.get_summary("forex")["forex"]["realized_pnl"],
                pt.get_summary("futures")["futures"]["realized_pnl"])
    fx, fut = _with({"forex": "2026-09-01"}, _ROWS, check)
    assert fx == -10.0 and fut == 500.0


def test_summary_since_never_shows_older_than_the_reset():
    # caller asks for "since 2026-08-01" but the reset is 2026-09-01 -> reset wins
    rows = _with({"forex": "2026-09-01"}, _ROWS,
                 lambda pt: {r["strategy"]: r for r in pt.get_strategy_summary_since("forex", "2026-08-01")})
    assert rows["rsi"]["total_pnl"] == -10.0


def test_raw_closed_trades_still_returns_full_history():
    # the learner / reports read raw rows -- the reset must NOT hide history from them
    n = _with({"forex": "2026-09-01"}, _ROWS, lambda pt: len(pt.get_closed_trades()))
    assert n == 3                                   # 2 forex + 1 futures, both pre and post reset


def test_committed_reset_file_is_wellformed_if_present():
    rp = os.path.join(BASE, "data", "pnl_reset.json")
    if not os.path.exists(rp):
        return
    d = json.load(open(rp, encoding="utf-8"))
    assert isinstance(d, dict)
    for k, v in d.items():
        if k.startswith("_"):
            continue
        assert isinstance(v, str) and len(v) == 10 and v[4] == "-" and v[7] == "-", (k, v)


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
