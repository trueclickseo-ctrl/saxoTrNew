"""
Avanza module tests -- 2026-09-04

Covers the pure-logic parts of the Avanza sleeve without touching the live API
or the real SQLite database.

Tests:
  avanza_client._mv()                        -- nested monetary value extraction
  avanza_client._extract_ticker_from_title() -- parse "Dell (DELL)" -> "DELL"
  avanza_client.search_stocks()              -- correct field mapping from API response
  avanza_client.confirm_fill()               -- filled path + timeout/cancel path
  avanza_instrument_cache._score_hit()       -- scoring: exact ticker > US > USD > market
  avanza_instrument_cache.lookup()           -- cache hit, cache miss, NOT_FOUND sentinel
  avanza_state (in-memory DB)                -- record_order, mark_filled, mark_cancelled,
                                                update_stop, get_open_buy_positions
  avanza_executor.compute_actions()          -- BUY / SELL / HOLD / SKIP logic
"""

import os
import sys
import types
import tempfile
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

G, R, Y, X, B = "\033[92m", "\033[91m", "\033[93m", "\033[0m", "\033[1m"
_res = []


def _run(name, fn):
    try:
        fn()
        _res.append((name, True, None))
    except Exception as e:
        import traceback
        _res.append((name, False, f"{type(e).__name__}: {e}\n{traceback.format_exc()}"))


# ── _mv: monetary value extraction ───────────────────────────────────────────

from avanza_module.avanza_client import _mv


def test_mv_nested_dict():
    assert _mv({"value": 123.45, "unit": "SEK"}) == 123.45

def test_mv_plain_float():
    assert _mv(99.9) == 99.9

def test_mv_plain_int():
    assert _mv(42) == 42.0

def test_mv_none_uses_default():
    assert _mv(None, 7.0) == 7.0

def test_mv_dict_missing_value_key():
    assert _mv({"unit": "SEK"}, 5.0) == 5.0

def test_mv_string_number():
    assert _mv("3.14") == 3.14

def test_mv_garbage_returns_default():
    assert _mv("not-a-number", 0.0) == 0.0

_run("_mv: nested dict",       test_mv_nested_dict)
_run("_mv: plain float",       test_mv_plain_float)
_run("_mv: plain int",         test_mv_plain_int)
_run("_mv: None → default",   test_mv_none_uses_default)
_run("_mv: dict no value key", test_mv_dict_missing_value_key)
_run("_mv: string number",     test_mv_string_number)
_run("_mv: garbage → default", test_mv_garbage_returns_default)


# ── _extract_ticker_from_title ────────────────────────────────────────────────

from avanza_module.avanza_client import _extract_ticker_from_title


def test_extract_ticker_normal():
    assert _extract_ticker_from_title("Dell Technologies C (DELL)") == "DELL"

def test_extract_ticker_short():
    assert _extract_ticker_from_title("Humana (HUM)") == "HUM"

def test_extract_ticker_no_parens():
    assert _extract_ticker_from_title("AES Corporation") == ""

def test_extract_ticker_empty():
    assert _extract_ticker_from_title("") == ""

def test_extract_ticker_open_paren_no_close():
    assert _extract_ticker_from_title("Something (NO_CLOSE") == ""

_run("extract_ticker: normal",         test_extract_ticker_normal)
_run("extract_ticker: short name",     test_extract_ticker_short)
_run("extract_ticker: no parens",      test_extract_ticker_no_parens)
_run("extract_ticker: empty",          test_extract_ticker_empty)
_run("extract_ticker: unclosed paren", test_extract_ticker_open_paren_no_close)


# ── search_stocks: field mapping ──────────────────────────────────────────────

from avanza_module import avanza_client as ac


def _make_fake_client(hits):
    """Minimal fake Avanza client whose search_for_stock() returns hits."""
    class FakeClient:
        def search_for_stock(self, query, limit=10):
            return hits
    return FakeClient()


def test_search_stocks_id_from_orderBookId():
    hits = [{"orderBookId": "918953", "title": "Dell Technologies C (DELL)",
              "flagCode": "US", "marketPlaceName": "NYSE",
              "price": {"currency": "USD"}}]
    result = ac.search_stocks(_make_fake_client(hits), "DELL")
    assert result[0]["id"] == "918953"

def test_search_stocks_ticker_from_title():
    hits = [{"orderBookId": "3691", "title": "Humana (HUM)",
              "flagCode": "US", "marketPlaceName": "NYSE",
              "price": {"currency": "USD"}}]
    result = ac.search_stocks(_make_fake_client(hits), "HUM")
    assert result[0]["ticker"] == "HUM"

def test_search_stocks_name_stripped():
    hits = [{"orderBookId": "1", "title": "Dell Technologies C (DELL)",
              "flagCode": "US", "marketPlaceName": "NYSE", "price": {}}]
    result = ac.search_stocks(_make_fake_client(hits), "DELL")
    assert result[0]["name"] == "Dell Technologies C"

def test_search_stocks_currency_from_price():
    hits = [{"orderBookId": "1", "title": "Foo (BAR)", "flagCode": "US",
              "marketPlaceName": "NYSE", "price": {"currency": "USD"}}]
    result = ac.search_stocks(_make_fake_client(hits), "BAR")
    assert result[0]["currency"] == "USD"

def test_search_stocks_market_from_marketPlaceName():
    hits = [{"orderBookId": "1", "title": "Fortinet (FTNT)", "flagCode": "US",
              "marketPlaceName": "NASDAQ", "price": {"currency": "USD"}}]
    result = ac.search_stocks(_make_fake_client(hits), "FTNT")
    assert result[0]["market"] == "NASDAQ"

def test_search_stocks_empty_hits():
    result = ac.search_stocks(_make_fake_client([]), "NONE")
    assert result == []

def test_search_stocks_exception_returns_empty():
    class BrokenClient:
        def search_for_stock(self, *a, **k):
            raise RuntimeError("network error")
    result = ac.search_stocks(BrokenClient(), "X")
    assert result == []

_run("search_stocks: id from orderBookId",      test_search_stocks_id_from_orderBookId)
_run("search_stocks: ticker from title",        test_search_stocks_ticker_from_title)
_run("search_stocks: name stripped",            test_search_stocks_name_stripped)
_run("search_stocks: currency from price dict", test_search_stocks_currency_from_price)
_run("search_stocks: market from marketPlaceName", test_search_stocks_market_from_marketPlaceName)
_run("search_stocks: empty hits → []",          test_search_stocks_empty_hits)
_run("search_stocks: exception → []",           test_search_stocks_exception_returns_empty)


# ── avanza_instrument_cache._score_hit ───────────────────────────────────────

from avanza_module.avanza_instrument_cache import _score_hit


def test_score_exact_ticker_wins():
    h_match    = {"ticker": "DELL",  "country": "US", "currency": "USD", "market": "NYSE"}
    h_no_match = {"ticker": "DELL2", "country": "US", "currency": "USD", "market": "NYSE"}
    assert _score_hit(h_match, "DELL") > _score_hit(h_no_match, "DELL")

def test_score_us_country_boost():
    us  = {"ticker": "X", "country": "US",  "currency": "USD", "market": "NYSE"}
    non = {"ticker": "X", "country": "DE",  "currency": "EUR", "market": "XETRA"}
    assert _score_hit(us, "X") > _score_hit(non, "X")

def test_score_nasdaq_gets_market_boost():
    nasdaq = {"ticker": "X", "country": "US", "currency": "USD", "market": "NASDAQ"}
    other  = {"ticker": "X", "country": "US", "currency": "USD", "market": "UNKNOWN"}
    assert _score_hit(nasdaq, "X") > _score_hit(other, "X")

def test_score_case_insensitive():
    h = {"ticker": "dell", "country": "us", "currency": "usd", "market": "nyse"}
    assert _score_hit(h, "DELL") == _score_hit(
        {"ticker": "DELL", "country": "US", "currency": "USD", "market": "NYSE"}, "DELL"
    )

_run("score_hit: exact ticker wins",      test_score_exact_ticker_wins)
_run("score_hit: US country boost",       test_score_us_country_boost)
_run("score_hit: NASDAQ market boost",    test_score_nasdaq_gets_market_boost)
_run("score_hit: case insensitive",       test_score_case_insensitive)


# ── avanza_instrument_cache.lookup ───────────────────────────────────────────

from avanza_module import avanza_instrument_cache as ic


def _fake_client_with_search(results):
    """search_stocks result injected via monkeypatching in the test."""
    class FC:
        pass
    return FC()


def test_lookup_cache_hit():
    cache = {"AAPL": {"id": "42", "name": "Apple", "ticker": "AAPL",
                       "currency": "USD", "country": "US", "market": "NASDAQ"}}
    result = ic.lookup(None, "AAPL", cache)
    assert result == "42"


def test_lookup_not_found_sentinel_returns_none():
    cache = {"AAPL": {"id": ic._NOT_FOUND_SENTINEL}}
    result = ic.lookup(None, "AAPL", cache)
    assert result is None


def test_lookup_miss_calls_search(monkeypatch_style=None):
    """When ticker not in cache, search_stocks is called and result cached."""
    cache = {}
    hits = [{"id": "999", "ticker": "HPE", "name": "Hewlett Packard Enterprise",
              "currency": "USD", "country": "US", "market": "NYSE"}]
    original_search = ic.__dict__.get("search_stocks")
    try:
        # Patch search_stocks in the avanza_instrument_cache module namespace
        import avanza_module.avanza_instrument_cache as _ic_mod
        import avanza_module.avanza_client as _ac_mod
        orig = _ac_mod.search_stocks
        _ac_mod.search_stocks = lambda client, query, limit=10: hits
        result = ic.lookup(object(), "HPE", cache)
        assert result == "999", f"expected '999', got {result!r}"
        assert cache["HPE"]["id"] == "999"
    finally:
        _ac_mod.search_stocks = orig


def test_lookup_no_hits_writes_sentinel():
    cache = {}
    import avanza_module.avanza_client as _ac_mod
    orig = _ac_mod.search_stocks
    try:
        _ac_mod.search_stocks = lambda *a, **k: []
        result = ic.lookup(object(), "ZZZZ", cache)
        assert result is None
        assert cache["ZZZZ"]["id"] == ic._NOT_FOUND_SENTINEL
    finally:
        _ac_mod.search_stocks = orig


_run("lookup: cache hit",                 test_lookup_cache_hit)
_run("lookup: NOT_FOUND sentinel → None", test_lookup_not_found_sentinel_returns_none)
_run("lookup: miss calls search, caches", test_lookup_miss_calls_search)
_run("lookup: no hits → sentinel",        test_lookup_no_hits_writes_sentinel)


# ── avanza_state (in-memory / temp DB) ───────────────────────────────────────

import avanza_module.avanza_state as st_mod


def _with_temp_db(fn):
    """Run fn with avanza_state pointing at a temp DB file."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        tmp_path = f.name
    orig = st_mod.DB_PATH
    st_mod.DB_PATH = tmp_path
    try:
        fn()
    finally:
        st_mod.DB_PATH = orig
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def test_state_record_and_read():
    def _inner():
        trade_id = st_mod.record_order("DELL", "918953", "BUY", 1, 515.0, "ORD-1",
                                        currency="USD", value_sek=5400.0)
        assert isinstance(trade_id, int) and trade_id > 0
        rows = st_mod.get_open_buy_positions()
        assert len(rows) == 1
        assert rows[0]["ticker"] == "DELL"
        assert rows[0]["status"] == "OPEN"
    _with_temp_db(_inner)


def test_state_mark_filled():
    def _inner():
        st_mod.record_order("HUM", "3691", "BUY", 1, 406.0, "ORD-2")
        st_mod.mark_filled("ORD-2", fill_price=407.5)
        rows = st_mod.get_open_buy_positions()
        assert rows[0]["status"] == "FILLED"
        assert abs(rows[0]["entry_price"] - 407.5) < 0.01
    _with_temp_db(_inner)


def test_state_mark_cancelled():
    def _inner():
        st_mod.record_order("STT", "4471", "BUY", 2, 194.0, "ORD-3")
        st_mod.mark_cancelled("ORD-3")
        rows = st_mod.get_open_buy_positions()
        # CANCELLED should not appear in open buy positions
        assert all(r["status"] != "CANCELLED" for r in rows)
    _with_temp_db(_inner)


def test_state_update_stop():
    def _inner():
        trade_id = st_mod.record_order("FTNT", "242850", "BUY", 2, 156.0, "ORD-4")
        st_mod.update_stop(trade_id, "SL-99", 143.52, 156.0)
        rows = st_mod.get_open_buy_positions()
        row = rows[0]
        assert row["stop_order_id"] == "SL-99"
        assert abs(row["stop_price"] - 143.52) < 0.01
        assert abs(row["trailing_stop_high"] - 156.0) < 0.01
    _with_temp_db(_inner)


def test_state_multiple_orders_only_open_returned():
    def _inner():
        st_mod.record_order("A", "1", "BUY", 1, 100.0, "ORD-A")
        st_mod.record_order("B", "2", "BUY", 1, 200.0, "ORD-B")
        st_mod.mark_cancelled("ORD-A")
        rows = st_mod.get_open_buy_positions()
        tickers = [r["ticker"] for r in rows]
        assert "B" in tickers
        assert "A" not in tickers
    _with_temp_db(_inner)


_run("state: record and read open buys",          test_state_record_and_read)
_run("state: mark_filled updates entry_price",    test_state_mark_filled)
_run("state: mark_cancelled hides from open",     test_state_mark_cancelled)
_run("state: update_stop persists stop fields",   test_state_update_stop)
_run("state: only OPEN/FILLED returned",          test_state_multiple_orders_only_open_returned)


# ── avanza_executor.compute_actions ──────────────────────────────────────────

from avanza_module import avanza_executor as ex


def _mock_cache(tickers_to_ids: dict):
    return {t: {"id": oid, "ticker": t, "name": t, "currency": "USD",
                "country": "US", "market": "NYSE"}
            for t, oid in tickers_to_ids.items()}


class FakePriceClient:
    """Fake Avanza client that returns a fixed price for any order_book_id."""
    def __init__(self, price=100.0):
        self._price = price

    def get_stock_info(self, ob_id):
        return {"quote": {"last": self._price, "buy": self._price},
                "listing": {"currency": "USD"}, "name": ob_id}

    def search_for_stock(self, q, limit=10):
        return []


def _patch_ic_lookup(mapping):
    """Patch ic.lookup to return from mapping without API calls."""
    import avanza_module.avanza_instrument_cache as _ic
    orig = _ic.lookup
    _ic.lookup = lambda client, ticker, cache, **kw: mapping.get(ticker.upper())
    return lambda: setattr(_ic, "lookup", orig)


def test_compute_actions_all_new_creates_buys():
    restore = _patch_ic_lookup({"DELL": "918953", "HUM": "3691"})
    try:
        actions = ex.compute_actions(
            target_tickers=["DELL", "HUM"],
            current_positions=[],
            budget_sek=10000.0,
            max_positions=10,
            instrument_cache={},
            client=FakePriceClient(100.0),
        )
    finally:
        restore()
    buys = [a for a in actions if a["action"] == "BUY"]
    assert len(buys) == 2
    tickers = {b["ticker"] for b in buys}
    assert "DELL" in tickers and "HUM" in tickers


def test_compute_actions_held_creates_hold():
    restore = _patch_ic_lookup({"DELL": "918953"})
    try:
        actions = ex.compute_actions(
            target_tickers=["DELL"],
            current_positions=[{
                "ticker": "DELL", "order_book_id": "918953",
                "qty": 1, "avg_price": 500.0, "current_price": 520.0,
                "value_sek": 5460.0, "gain_pct": 4.0,
            }],
            budget_sek=10000.0,
            max_positions=10,
            instrument_cache={},
            client=FakePriceClient(520.0),
        )
    finally:
        restore()
    holds = [a for a in actions if a["action"] == "HOLD"]
    buys  = [a for a in actions if a["action"] == "BUY"]
    assert len(holds) == 1 and holds[0]["ticker"] == "DELL"
    assert len(buys) == 0


def test_compute_actions_dropped_ticker_creates_sell():
    restore = _patch_ic_lookup({"HUM": "3691"})
    try:
        actions = ex.compute_actions(
            target_tickers=["HUM"],          # DELL dropped from basket
            current_positions=[
                {"ticker": "DELL", "order_book_id": "918953", "qty": 1,
                 "avg_price": 500.0, "current_price": 510.0,
                 "value_sek": 5355.0, "gain_pct": 2.0},
            ],
            budget_sek=0.0,                  # no new buys needed
            max_positions=10,
            instrument_cache={},
            client=FakePriceClient(510.0),
        )
    finally:
        restore()
    sells = [a for a in actions if a["action"] == "SELL"]
    assert len(sells) == 1
    assert sells[0]["ticker"] == "DELL"


def test_compute_actions_not_found_on_avanza_creates_skip():
    restore = _patch_ic_lookup({})  # nothing resolves
    try:
        actions = ex.compute_actions(
            target_tickers=["UNKNOWN"],
            current_positions=[],
            budget_sek=10000.0,
            max_positions=10,
            instrument_cache={},
            client=FakePriceClient(),
        )
    finally:
        restore()
    skips = [a for a in actions if a["action"] == "SKIP"]
    assert len(skips) == 1


def test_compute_actions_zero_budget_no_buys():
    restore = _patch_ic_lookup({"FTNT": "242850"})
    try:
        actions = ex.compute_actions(
            target_tickers=["FTNT"],
            current_positions=[],
            budget_sek=0.0,
            max_positions=10,
            instrument_cache={},
            client=FakePriceClient(),
        )
    finally:
        restore()
    buys = [a for a in actions if a["action"] == "BUY"]
    assert len(buys) == 0


_run("compute_actions: all new → BUYs",              test_compute_actions_all_new_creates_buys)
_run("compute_actions: already held → HOLD",         test_compute_actions_held_creates_hold)
_run("compute_actions: dropped ticker → SELL",       test_compute_actions_dropped_ticker_creates_sell)
_run("compute_actions: not on Avanza → SKIP",        test_compute_actions_not_found_on_avanza_creates_skip)
_run("compute_actions: zero budget → no buys",       test_compute_actions_zero_budget_no_buys)


# ── confirm_fill: filled path + timeout path ──────────────────────────────────

def test_confirm_fill_detects_fill():
    """Order disappears from open_orders on second poll → returns fill price from position."""
    import avanza_module.avanza_client as _ac

    call_count = [0]

    class FillClient:
        def get_orders(self):
            call_count[0] += 1
            if call_count[0] == 1:
                # First poll: order still there
                return {"orders": [{"orderId": "ORD-99", "orderbook": {"id": "918953",
                                    "tickerSymbol": "DELL"}, "orderType": "BUY",
                                    "volume": 1, "price": 515.0}]}
            # Second poll: order gone (filled)
            return {"orders": []}

        def get_accounts_positions(self):
            return {"withOrderbook": [{
                "account":    {"id": "5834714"},
                "instrument": {"orderbook": {"id": "918953", "quote": {"last": 516.1}},
                               "name": "Dell"},
                "volume": {"value": 1}, "value": {"value": 5400.0},
                "averageAcquiredPrice": {"value": 516.1},
                "acquiredValue": {"value": 5400.0},
            }]}

    orig_sleep = _ac.time.sleep
    _ac.time.sleep = lambda s: None   # skip actual waits
    try:
        fill = _ac.confirm_fill(FillClient(), "5834714", "ORD-99", "918953",
                                timeout_s=60, poll_s=1)
        assert fill is not None and fill > 0, f"expected fill price > 0, got {fill}"
    finally:
        _ac.time.sleep = orig_sleep


def test_confirm_fill_timeout_cancels():
    """Order never fills → confirm_fill calls cancel_order and returns None."""
    import avanza_module.avanza_client as _ac

    cancelled = [False]

    class StuckClient:
        def get_orders(self):
            return {"orders": [{"orderId": "ORD-STUCK",
                                "orderbook": {"id": "X"}, "orderType": "BUY",
                                "volume": 1, "price": 100.0}]}

        def get_accounts_positions(self):
            return {"withOrderbook": []}

        def delete_order(self, account_id, order_id):
            cancelled[0] = True

    orig_sleep    = _ac.time.sleep
    orig_monotonic = _ac.time.monotonic
    tick = [0]
    # Make monotonic advance by 2s each call so we blow through the timeout fast
    def fake_monotonic():
        tick[0] += 2
        return tick[0]
    _ac.time.sleep    = lambda s: None
    _ac.time.monotonic = fake_monotonic
    try:
        fill = _ac.confirm_fill(StuckClient(), "5834714", "ORD-STUCK", "X",
                                timeout_s=5, poll_s=1)
        assert fill is None, f"expected None on timeout, got {fill}"
        assert cancelled[0], "cancel_order was not called on timeout"
    finally:
        _ac.time.sleep    = orig_sleep
        _ac.time.monotonic = orig_monotonic


_run("confirm_fill: order fills → price returned", test_confirm_fill_detects_fill)
_run("confirm_fill: timeout → cancel, None",       test_confirm_fill_timeout_cancels)


# ── Summary ───────────────────────────────────────────────────────────────────

print()
passed = sum(1 for _, ok, _ in _res if ok)
failed = sum(1 for _, ok, _ in _res if not ok)
print(f"{B}{'─'*60}{X}")
for name, ok, err in _res:
    icon = f"{G}PASS{X}" if ok else f"{R}FAIL{X}"
    print(f"  {icon}  {name}")
    if err:
        for line in err.strip().split("\n"):
            print(f"        {Y}{line}{X}")
print(f"{B}{'─'*60}{X}")
print(f"  {G}{passed} passed{X}  {(R if failed else G)}{failed} failed{X}")
if failed:
    sys.exit(1)
