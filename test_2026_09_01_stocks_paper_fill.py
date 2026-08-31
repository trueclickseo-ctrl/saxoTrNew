"""
Regression test -- 2026-09-01 stocks SIM paper-fill fallback.

Saxo SIM's order engine has rejected ~every order since ~2026-08-28
("CouldNotCompleteRequest (90)"). Forex rides it out with a local
paper-fill; the stocks module had none, so a valid signal (PYPL US
Reversion, 2026-08-31: RSI 33, -10.5% dip, 3.3x vol -- ~18 rejected
attempts, then the window closed) was simply missed.

This ports forex's SIM_PAPER_FILL_ON_REJECT to atos_runner.py:
  * a rejected SIM stock BUY is booked in trades with paper=1 at the scan
    price and managed by ATOS's own should_exit() / rebalance logic;
  * housekeeping.StocksAdapter skips paper rows (no Saxo counterpart);
  * the US-momentum reconcile skips paper rows (won't close them as
    "not owned");
  * paper exits close the DB row without a Saxo sell;
  * LIVE can never paper-fill (_STOCKS_ENV gate);
  * also fixes a latent bug: _place_us (US Blend) discarded the
    place_with_stop return -> a rejected order recorded a PHANTOM row.
"""

import ast
import inspect
import os
import sqlite3
import sys
import tempfile

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

GREEN, RED, YELLOW, RESET, BOLD = "\033[92m", "\033[91m", "\033[93m", "\033[0m", "\033[1m"
_results = []


def _run(name, fn):
    try:
        fn()
        _results.append((name, True, None))
    except Exception as e:
        import traceback
        _results.append((name, False, f"{type(e).__name__}: {e}\n{traceback.format_exc()}"))


import atos.database as adb
import atos_runner as ar
import housekeeping as hk


# ── SIM-only gate ────────────────────────────────────────────────────────
def test_paper_fill_enabled_on_sim_only():
    assert ar._STOCKS_ENV == "sim"
    assert ar._stocks_paper_fill_enabled() is True
    old = ar._STOCKS_ENV
    try:
        ar._STOCKS_ENV = "live"
        assert ar._stocks_paper_fill_enabled() is False
        ar._STOCKS_ENV = "sim"
        ar.STOCKS_SIM_PAPER_FILL_ON_REJECT = False
        assert ar._stocks_paper_fill_enabled() is False
    finally:
        ar._STOCKS_ENV = old
        ar.STOCKS_SIM_PAPER_FILL_ON_REJECT = True


# ── DB migration + insert ────────────────────────────────────────────────
def test_migration_adds_paper_column_and_insert_defaults():
    import atos.database as d
    orig = d.DB_PATH
    tmp = os.path.join(BASE_DIR, "data", "_test_paper_mig.db")
    for p in (tmp, tmp + "-wal", tmp + "-shm"):
        if os.path.exists(p):
            os.remove(p)
    d.DB_PATH = tmp
    try:
        d.init_db()
        con = sqlite3.connect(tmp)
        try:
            cols = [r[1] for r in con.execute("PRAGMA table_info(trades)")]
            assert "paper" in cols, cols
        finally:
            con.close()
        base = dict(strategy="US Reversion", market_group="US Equities", ticker="AAA",
                    direction="BUY", entry_date="2026-09-01", entry_price=10.0, shares=5,
                    commission_sek=1.0, entry_score=0, d1_trend=0, d2_momentum=0, d3_breakout=0,
                    d4_mean_revert=0, d5_volume=0, d6_smart_money=0, d7_mom_quality=0, d8_regime=0,
                    trailing_stop_high=10.0, regime_at_entry="reversion", stop_price=9.0)
        d.insert_trade(base)                              # no paper key -> default 0
        d.insert_trade({**base, "ticker": "BBB", "paper": 1})
        got = {t["ticker"]: t["paper"] for t in d.get_open_trades()}
        assert got == {"AAA": 0, "BBB": 1}, got
    finally:
        d.DB_PATH = orig
        for p in (tmp, tmp + "-wal", tmp + "-shm"):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass


def test_migration_idempotent_on_real_db():
    adb.init_db()
    adb.init_db()   # must not raise (ADD COLUMN is caught)


# ── housekeeping skips paper rows ────────────────────────────────────────
def test_stocks_adapter_skips_paper_rows():
    src = inspect.getsource(hk.StocksAdapter.load)
    assert "COALESCE(paper, 0) = 0" in src
    assert "exit_date IS NULL" in src


# ── source: every buy path handles rejection with paper-fill ─────────────
def test_all_active_buy_paths_paper_fill_on_reject():
    src = inspect.getsource(ar)
    # US Reversion (daily) + intraday both write "paper": paper into insert_trade
    assert src.count('"paper": paper') >= 2, "reversion daily + intraday must tag the row"
    assert '"paper": paper,' in src  # US Blend _place_us
    # each guarded by the SIM gate
    assert src.count("_stocks_paper_fill_enabled()") >= 4


def test_place_us_now_checks_the_order_return():
    src = inspect.getsource(ar._place_us)
    # the latent phantom bug: place_with_stop return was discarded entirely.
    # Now the entry id AND the stop id are both captured + used.
    assert "entry_oid, stop_oid, _ = saxo_order.place_with_stop(" in src
    assert "if entry_oid is None:" in src
    # rejected + no paper-fill -> return False BEFORE db.insert_trade
    i_reject = src.index("if entry_oid is None:")
    i_insert = src.index("db.insert_trade(")
    assert i_reject < i_insert
    assert "return False" in src[i_reject:i_insert]


def test_reconcile_skips_paper_positions():
    src = inspect.getsource(ar.run_us_momentum)
    i_loop = src.index("for tk in list(us_open.keys()):")
    i_close = src.index('"reconciled_not_owned"')
    block = src[i_loop:i_close]
    assert 'us_open[tk].get("paper")' in block and "continue" in block


def test_paper_exits_skip_saxo_sell():
    rev = inspect.getsource(ar.run_us_reversion)
    assert 'trade.get("paper")' in rev
    assert "if not is_paper:" in rev and "place_market_order" in rev
    blend = inspect.getsource(ar._place_us)
    assert 'cur_trade and cur_trade.get("paper")' in blend
    assert "if not cur_is_paper:" in blend


# ── the module still imports & is internally consistent ──────────────────
def test_atos_runner_imports_and_ast_parses():
    ast.parse(inspect.getsource(ar))
    assert callable(ar.run_us_reversion) and callable(ar._place_us)


for _n, _f in list(globals().items()):
    if _n.startswith("test_") and callable(_f):
        _run(_n, _f)

print(f"\n{BOLD}{'='*66}{RESET}")
failed = [(n, e) for n, ok, e in _results if not ok]
for name, ok, err in _results:
    print(f"  [{GREEN}PASS{RESET}]" if ok else f"  [{RED}FAIL{RESET}]", name)
    if err:
        print(f"      {YELLOW}{err}{RESET}")
print(f"{BOLD}{'='*66}{RESET}")
if failed:
    print(f"{RED}{BOLD}  {len(failed)} / {len(_results)} FAILED{RESET}")
    sys.exit(1)
print(f"{GREEN}{BOLD}  ALL {len(_results)} TESTS PASSED{RESET}")
sys.exit(0)
