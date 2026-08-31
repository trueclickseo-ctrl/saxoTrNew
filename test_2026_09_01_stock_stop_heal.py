"""
Regression test -- 2026-09-01 stock broker-stop heal.

On 2026-09-01 Saxo SIM started accepting stock ENTRY orders again (after
the ~Aug-28 outage) but rejected the protective stop placed microseconds
later with "NotOwned" (settlement race). place_with_stop returned
(entry_oid, None): position real, broker stop missing. WSM/MTB/GEV were
bought this way. Fix:
  * atos.database: trades.stop_order_id column + set_stop_order_id();
  * saxo_order.place_stop_only(): a standalone protective stop, no entry;
  * atos_runner._heal_missing_stock_stops(): each cycle, for every open
    non-paper stock with stop_order_id NULL and a live-settled Saxo
    position, place the stop and record its id (or note an existing one).
  * all 3 stock buy paths now persist the stop_oid at entry.
Software-side exits (should_exit / trailing) protect the position meanwhile.
"""

import inspect
import os
import sqlite3
import sys

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
import saxo_order
import saxo_client
import instrument_map


# ── DB column + helper ───────────────────────────────────────────────────
def test_db_column_and_helper():
    import atos.database as d
    orig = d.DB_PATH
    tmp = os.path.join(BASE_DIR, "data", "_test_stopheal.db")
    for p in (tmp, tmp + "-wal", tmp + "-shm"):
        if os.path.exists(p):
            os.remove(p)
    d.DB_PATH = tmp
    try:
        d.init_db()
        con = sqlite3.connect(tmp)
        cols = [r[1] for r in con.execute("PRAGMA table_info(trades)")]
        con.close()
        assert "stop_order_id" in cols
        base = dict(strategy="US Reversion", market_group="US Equities", ticker="WSM",
                    direction="BUY", entry_date="2026-09-01", entry_price=228.25, shares=6,
                    commission_sek=5, entry_score=1, d1_trend=0, d2_momentum=0, d3_breakout=0,
                    d4_mean_revert=20, d5_volume=1.6, d6_smart_money=0, d7_mom_quality=0,
                    d8_regime=0, trailing_stop_high=228.25, regime_at_entry="reversion",
                    stop_price=219.12)
        tid = d.insert_trade(base)                         # no stop_order_id -> NULL
        got = {t["ticker"]: t["stop_order_id"] for t in d.get_open_trades()}
        assert got == {"WSM": None}
        d.set_stop_order_id(tid, "STOP-123")
        assert {t["ticker"]: t["stop_order_id"] for t in d.get_open_trades()} == {"WSM": "STOP-123"}
        # insert with an explicit id
        d.insert_trade({**base, "ticker": "MTB", "stop_order_id": "S9"})
        assert {t["ticker"]: t["stop_order_id"] for t in d.get_open_trades()}["MTB"] == "S9"
    finally:
        d.DB_PATH = orig
        for p in (tmp, tmp + "-wal", tmp + "-shm"):
            try:
                os.path.exists(p) and os.remove(p)
            except OSError:
                pass


# ── saxo_order.place_stop_only ───────────────────────────────────────────
def test_place_stop_only_builds_a_stock_stoplimit_sell():
    captured = {}

    def fake_post(path, body):
        captured["path"] = path
        captured["body"] = body
        return {"OrderId": "STOP-XYZ"}

    oid = saxo_order.place_stop_only(
        post_fn=fake_post, account_key="AK", uic=12345, asset_type="Stock",
        amount=6, entry_side="Buy", stop_price=219.12, symbol="WSM")
    assert oid == "STOP-XYZ"
    b = captured["body"]
    assert b["BuySell"] == "Sell" and b["Uic"] == 12345 and b["Amount"] == 6
    assert b["OrderType"] == "StopLimit" and "StopLimitPrice" in b
    assert b["OrderDuration"]["DurationType"] == "GoodTillCancel"


def test_place_stop_only_returns_none_on_failure():
    def boom(path, body):
        raise RuntimeError("Saxo said no")
    assert saxo_order.place_stop_only(boom, "AK", 1, "Stock", 1, "Buy", 10.0, "X") is None


# ── _heal_missing_stock_stops ────────────────────────────────────────────
class _Stub:
    def __init__(self):
        self.positions = {"Data": []}
        self.orders = {"Data": []}
        self.placed = []
        self.recorded = []

    def install(self):
        self._sp = saxo_client.get_positions
        self._so = saxo_client.get_orders
        self._sa = saxo_client.get_account_key
        self._pp = saxo_order.place_stop_only
        self._im = instrument_map.load_instrument_map
        self._db = ar.db.set_stop_order_id
        saxo_client.get_positions = lambda env="sim": self.positions
        saxo_client.get_orders = lambda at=None, env="sim": self.orders
        saxo_client.get_account_key = lambda env="sim": "AK"
        instrument_map.load_instrument_map = lambda: {
            "WSM": {"uic": 111}, "MTB": {"uic": 222}, "GEV": {"uic": 333}}
        saxo_order.place_stop_only = lambda **kw: (self.placed.append(kw) or "NEW-STOP")
        ar.db.set_stop_order_id = lambda tid, oid: self.recorded.append((tid, oid))

    def restore(self):
        saxo_client.get_positions = self._sp
        saxo_client.get_orders = self._so
        saxo_client.get_account_key = self._sa
        saxo_order.place_stop_only = self._pp
        instrument_map.load_instrument_map = self._im
        ar.db.set_stop_order_id = self._db


def _trade(ticker, tid, *, stop_order_id=None, paper=0, stop_price=100.0, shares=6):
    return dict(id=tid, ticker=ticker, direction="BUY", shares=shares,
                stop_price=stop_price, stop_order_id=stop_order_id, paper=paper)


def test_heal_places_stop_for_a_settled_naked_position():
    s = _Stub()
    s.positions = {"Data": [{"PositionBase": {"Uic": 111, "Amount": 6}}]}
    s.install()
    try:
        ar._heal_missing_stock_stops([_trade("WSM", 1)])
        assert len(s.placed) == 1 and s.placed[0]["uic"] == 111 and s.placed[0]["amount"] == 6
        assert s.recorded == [(1, "NEW-STOP")]
    finally:
        s.restore()


def test_heal_skips_position_not_yet_settled():
    s = _Stub()
    s.positions = {"Data": []}          # WSM not held at Saxo yet
    s.install()
    try:
        ar._heal_missing_stock_stops([_trade("WSM", 1)])
        assert s.placed == [] and s.recorded == []
    finally:
        s.restore()


def test_heal_marks_existing_when_a_stop_already_covers_it():
    s = _Stub()
    s.positions = {"Data": [{"PositionBase": {"Uic": 111, "Amount": 6}}]}
    s.orders = {"Data": [{"Uic": 111, "BuySell": "Sell", "OpenOrderType": "StopLimit"}]}
    s.install()
    try:
        ar._heal_missing_stock_stops([_trade("WSM", 1)])
        assert s.placed == []
        assert s.recorded == [(1, "EXISTING")]
    finally:
        s.restore()


def test_heal_ignores_paper_and_already_healed_and_no_stop_price():
    s = _Stub()
    s.positions = {"Data": [{"PositionBase": {"Uic": 111, "Amount": 6}},
                            {"PositionBase": {"Uic": 222, "Amount": 6}},
                            {"PositionBase": {"Uic": 333, "Amount": 6}}]}
    s.install()
    try:
        ar._heal_missing_stock_stops([
            _trade("WSM", 1, paper=1),                      # paper -> skip
            _trade("MTB", 2, stop_order_id="S-OLD"),        # already healed -> skip
            _trade("GEV", 3, stop_price=0),                 # no stop level -> skip
        ])
        assert s.placed == [] and s.recorded == []
    finally:
        s.restore()


def test_heal_partial_fill_not_healed():
    s = _Stub()
    s.positions = {"Data": [{"PositionBase": {"Uic": 111, "Amount": 3}}]}   # only 3 of 6
    s.install()
    try:
        ar._heal_missing_stock_stops([_trade("WSM", 1, shares=6)])
        assert s.placed == []
    finally:
        s.restore()


def test_heal_never_raises_on_api_failure():
    s = _Stub()
    s.install()
    try:
        saxo_client.get_positions = lambda env="sim": (_ for _ in ()).throw(RuntimeError("down"))
        ar._heal_missing_stock_stops([_trade("WSM", 1)])   # must not raise
    finally:
        s.restore()


# ── wiring ───────────────────────────────────────────────────────────────
def test_run_cycle_calls_the_heal():
    src = inspect.getsource(ar.run_cycle)
    assert "_heal_missing_stock_stops(" in src
    # before new entries (6b) so a healed position is covered ASAP
    assert src.index("_heal_missing_stock_stops(") < src.index("6b. New entries")


def test_all_three_buy_paths_persist_stop_oid():
    src = inspect.getsource(ar)
    assert src.count('"stop_order_id": (stop_oid if not paper else None)') == 3
    # _place_us captures the return now (was `entry_oid, _, _`)
    assert "entry_oid, stop_oid, _ = saxo_order.place_with_stop(" in inspect.getsource(ar._place_us)


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
