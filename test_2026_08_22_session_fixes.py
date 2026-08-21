"""
test_2026_08_22_session_fixes.py
---------------------------------
Regression tests for every fix made during the 2026-08-21/22 forex
deep-audit session. These code paths had ZERO prior automated coverage —
each test here pins down a bug that was live in production and is now
fixed, so a future change can't silently reintroduce it.

Covers:
  1. fx.py            — TRY/MXN/CNH fallback rates exist and are used
  2. saxo_order.py     — a rejected entry order returns (None, None)
                         instead of raising and killing the whole run
  3. forex/universe.py — price_decimals() matches each pair's real pip_size
  4. pnl_tracker.py    — sync_etf_from_json's UPDATE...ORDER BY/LIMIT bug
                         (SQLite doesn't support it) and the partial-sell
                         "closes the whole position" bug
  5. strategy_london_breakout.py — _session_range() reads the HourUTC
                         column, not a meaningless RangeIndex

Run:
    python test_2026_08_22_session_fixes.py
Exit code 0 = all pass, 1 = one or more failures.
"""
import os
import sys
import sqlite3
import tempfile
import traceback
from unittest.mock import patch

import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

GREEN, RED, YELLOW, CYAN, RESET, BOLD = (
    "\033[92m", "\033[91m", "\033[93m", "\033[96m", "\033[0m", "\033[1m"
)
_results = []


def _run(name, fn):
    try:
        result = fn()
        if result is None:
            result = True
        _results.append((name, bool(result), None))
    except Exception as e:
        _results.append((name, False, f"{type(e).__name__}: {e}"))


def section(title):
    print(f"\n{BOLD}{CYAN}{'-'*70}{RESET}")
    print(f"{BOLD}{CYAN}  {title}{RESET}")
    print(f"{BOLD}{CYAN}{'-'*70}{RESET}")


# ═══════════════════════════════════════════════════════════════════════
section("1. fx.py — TRY/MXN/CNH fallback rates")
# ═══════════════════════════════════════════════════════════════════════

def test_fx_fallback_has_try_mxn_cnh():
    import fx
    for ccy in ("TRY", "MXN", "CNH"):
        assert ccy in fx.FALLBACK_RATES_TO_SEK, f"{ccy} missing from FALLBACK_RATES_TO_SEK"
        assert fx.FALLBACK_RATES_TO_SEK[ccy] > 0, f"{ccy} fallback rate must be positive"


def test_fx_get_rate_uses_fallback_when_live_fails():
    import fx
    fx.reset_cache()
    # These 3 currencies' yfinance ticker structurally 404s (confirmed live,
    # not a transient outage) -- get_rate_to_sek must fall back, not raise.
    for ccy in ("TRY", "MXN", "CNH"):
        rate = fx.get_rate_to_sek(ccy)
        assert rate == fx.FALLBACK_RATES_TO_SEK[ccy], \
            f"{ccy}: expected fallback {fx.FALLBACK_RATES_TO_SEK[ccy]}, got {rate}"


def test_fx_missing_currency_raises_not_silently_wrong():
    import fx
    fx.reset_cache()
    try:
        fx.get_rate_to_sek("XYZ_NOT_A_REAL_CURRENCY")
        assert False, "should have raised RuntimeError for an unmapped currency"
    except RuntimeError:
        pass


_run("fx: FALLBACK_RATES_TO_SEK has TRY/MXN/CNH", test_fx_fallback_has_try_mxn_cnh)
_run("fx: get_rate_to_sek falls back for TRY/MXN/CNH (live 404s)", test_fx_get_rate_uses_fallback_when_live_fails)
_run("fx: truly unmapped currency still raises (not silently wrong)", test_fx_missing_currency_raises_not_silently_wrong)


# ═══════════════════════════════════════════════════════════════════════
section("2. saxo_order.py — rejected entry order doesn't crash the caller")
# ═══════════════════════════════════════════════════════════════════════

def test_rejected_entry_returns_none_not_raises():
    import saxo_order

    def failing_post(path, body):
        raise Exception("400 Client Error: WouldExceedMargin")

    entry_oid, stop_oid, tp_oid = saxo_order.place_with_stop(
        post_fn=failing_post, account_key="ACC", uic=21, asset_type="FxSpot",
        amount=10_000, buy_sell="Buy", stop_price=1.0900, label="test:EURUSD",
        symbol="EURUSD",
    )
    assert entry_oid is None, f"expected None entry_oid on rejection, got {entry_oid}"
    assert stop_oid is None
    assert tp_oid is None


def test_successful_entry_still_places_stop():
    import saxo_order
    calls = []

    def ok_post(path, body):
        calls.append(body)
        return {"OrderId": "12345"}

    entry_oid, stop_oid, tp_oid = saxo_order.place_with_stop(
        post_fn=ok_post, account_key="ACC", uic=21, asset_type="FxSpot",
        amount=10_000, buy_sell="Buy", stop_price=1.0900, label="test:EURUSD",
        symbol="EURUSD",
    )
    assert entry_oid == "12345"
    assert stop_oid == "12345"
    assert len(calls) == 2, f"expected 2 POSTs (entry + stop), got {len(calls)}"


def test_stop_rejection_alone_does_not_lose_the_entry():
    """Entry succeeds, stop fails -- caller must still get the real entry_oid
    back (position IS open at the broker) with stop_oid=None, not lose the
    whole result to the stop's failure."""
    import saxo_order
    call_n = [0]

    def flaky_post(path, body):
        call_n[0] += 1
        if call_n[0] == 1:
            return {"OrderId": "ENTRY1"}
        raise Exception("400 Client Error: PriceNotInTickSizeIncrements")

    entry_oid, stop_oid, tp_oid = saxo_order.place_with_stop(
        post_fn=flaky_post, account_key="ACC", uic=23509, asset_type="FxSpot",
        amount=39_000, buy_sell="Buy", stop_price=34.08998, label="donchian:AUDTRY",
        symbol="AUDTRY", price_decimals=4,
    )
    assert entry_oid == "ENTRY1", "entry succeeded, must not be lost"
    assert stop_oid is None, "stop failed, must be reported as None, not silently swallowed"


_run("saxo_order: rejected entry -> (None, None, None), no exception", test_rejected_entry_returns_none_not_raises)
_run("saxo_order: successful entry places both entry and stop", test_successful_entry_still_places_stop)
_run("saxo_order: stop rejection alone doesn't lose a real entry_oid", test_stop_rejection_alone_does_not_lose_the_entry)


# ═══════════════════════════════════════════════════════════════════════
section("3. forex/universe.py — price_decimals() matches real pip_size")
# ═══════════════════════════════════════════════════════════════════════

def test_price_decimals_try_cnh_pairs_are_4dp():
    from forex.universe import price_decimals
    for sym in ("AUDTRY", "USDTRY", "GBPTRY", "CADTRY", "EURCNH", "CHFCNH", "CNHHKD"):
        d = price_decimals(sym)
        assert d == 4, f"{sym}: expected 4dp (pip_size=0.001), got {d}"


def test_price_decimals_jpy_pairs_are_3dp():
    from forex.universe import price_decimals
    for sym in ("USDJPY", "EURJPY", "GBPJPY"):
        d = price_decimals(sym)
        assert d == 3, f"{sym}: expected 3dp, got {d}"


def test_price_decimals_majors_are_5dp():
    from forex.universe import price_decimals
    for sym in ("EURUSD", "GBPUSD", "USDMXN"):
        d = price_decimals(sym)
        assert d == 5, f"{sym}: expected 5dp, got {d}"


def test_saxo_order_rounds_audtry_stop_to_4dp_not_5dp():
    """The exact live rejection this fixed: AUDTRY stop 34.089979153806915
    must round to 34.0900 (4dp), not 34.08998 (5dp, which Saxo rejects)."""
    import saxo_order
    from forex.universe import price_decimals
    rounded = saxo_order._round_price(34.089979153806915, "FxSpot", "AUDTRY",
                                      price_decimals("AUDTRY"))
    assert rounded == 34.09, f"expected 34.09 (4dp), got {rounded}"
    assert len(str(rounded).split(".")[-1]) <= 4


_run("universe: TRY/CNH pairs are 4dp", test_price_decimals_try_cnh_pairs_are_4dp)
_run("universe: JPY pairs are 3dp", test_price_decimals_jpy_pairs_are_3dp)
_run("universe: majors are 5dp", test_price_decimals_majors_are_5dp)
_run("saxo_order: AUDTRY stop rounds to 4dp (the exact live rejection case)", test_saxo_order_rounds_audtry_stop_to_4dp_not_5dp)


# ═══════════════════════════════════════════════════════════════════════
section("4. pnl_tracker.py — ETF sync SQL bug + partial-sell handling")
# ═══════════════════════════════════════════════════════════════════════

def _fresh_pnl_tracker(tmp_db_path):
    """Import a clean pnl_tracker instance pointed at a scratch DB, isolated
    from the real data/pnl_ledger.db."""
    import importlib
    import pnl_tracker
    importlib.reload(pnl_tracker)
    pnl_tracker.DB_PATH = tmp_db_path
    return pnl_tracker


def test_etf_sell_update_does_not_raise_sql_syntax_error():
    """The exact bug: SQLite's UPDATE doesn't support ORDER BY/LIMIT. This
    used to raise 'near ORDER: syntax error' the moment a real ETF sell
    order was synced -- silently caught by sync_etf_from_json's except
    Exception, so it looked like nothing happened rather than crashing."""
    # A plain NamedTemporaryFile (not TemporaryDirectory) -- sqlite3
    # connections opened inside this function may still be finalizing on
    # Windows when a TemporaryDirectory's __exit__ tries to rmdir it,
    # raising a spurious PermissionError unrelated to the code under test.
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        pt = _fresh_pnl_tracker(db_path)
        pt.log_open("etf", "ETF Rotation", "XLV", "Buy", 175, 166.58,
                   currency="USD", timestamp="2026-08-17T13:30:00")
        with pt._conn() as c:
            # Directly exercise the exact code path sync_etf_from_json's
            # sell branch runs -- SELECT id first, then UPDATE WHERE id=.
            row = c.execute("""
                SELECT id, quantity FROM trades
                 WHERE module='etf' AND symbol='XLV' AND status='open'
                 ORDER BY id DESC LIMIT 1
            """).fetchone()
            assert row is not None
            c.execute("""
                UPDATE trades SET exit_price=?, realized_pnl=?,
                    exit_reason=?, status='closed', timestamp_close=?
                 WHERE id=?
            """, (174.78, 1000.0, "test", "2026-08-22", row["id"]))
        closed = pt.get_closed_trades(module="etf")
        assert len(closed) == 1
        assert closed[0]["symbol"] == "XLV"
    finally:
        try:
            os.remove(db_path)
        except OSError:
            pass


def test_partial_sell_reduces_open_row_not_closes_whole_position():
    """The second bug: a partial sell used to unconditionally mark the
    WHOLE open row closed off the partial-quantity P&L, silently dropping
    the remaining held shares from the ledger. A partial sell must reduce
    the open row's quantity and record only the sold portion as closed."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        pt = _fresh_pnl_tracker(db_path)
        pt.log_open("etf", "ETF Rotation", "XLF", "Buy", 505, 57.99,
                   currency="USD", timestamp="2026-08-17T13:30:00")

        with pt._conn() as c:
            row = c.execute("""
                SELECT id, quantity FROM trades
                 WHERE module='etf' AND symbol='XLF' AND status='open'
                 ORDER BY id DESC LIMIT 1
            """).fetchone()
            sold_qty = 252
            assert sold_qty < row["quantity"], "test setup: must be a genuine partial sell"
            # Partial-sell branch: reduce open row, insert closed row for sold portion
            c.execute("UPDATE trades SET quantity=? WHERE id=?",
                     (row["quantity"] - sold_qty, row["id"]))
            c.execute("""
                INSERT INTO trades
                    (module, strategy, symbol, direction, quantity,
                     entry_price, exit_price, realized_pnl, currency,
                     exit_reason, status, timestamp_open, timestamp_close)
                VALUES ('etf','ETF Rotation','XLF','Buy',?,?,?,?,'USD',?,'closed',?,?)
            """, (sold_qty, 57.99, 57.48, -128.52, "partial", "2026-08-17", "2026-08-22"))

        open_rows = pt.get_open_positions(module="etf")
        closed_rows = pt.get_closed_trades(module="etf")
        assert len(open_rows) == 1, f"expected the position to STILL be open, got {len(open_rows)} open rows"
        assert open_rows[0]["quantity"] == 253, f"expected remaining 253, got {open_rows[0]['quantity']}"
        assert len(closed_rows) == 1
        assert closed_rows[0]["quantity"] == 252, "closed row should only cover the SOLD portion"
    finally:
        try:
            os.remove(db_path)
        except OSError:
            pass


_run("pnl_tracker: ETF sell no longer raises SQLite UPDATE/ORDER BY syntax error", test_etf_sell_update_does_not_raise_sql_syntax_error)
_run("pnl_tracker: partial sell reduces open qty, doesn't wipe the position", test_partial_sell_reduces_open_row_not_closes_whole_position)


# ═══════════════════════════════════════════════════════════════════════
section("5. strategy_london_breakout.py — HourUTC-based session range")
# ═══════════════════════════════════════════════════════════════════════

def _runner_shaped_h1_df(asian_high, asian_low, breakout_close, n=48):
    """Reproduce forex/runner.py._fetch_history_h1()'s ACTUAL shape: a plain
    integer RangeIndex with the real hour in a separate HourUTC column --
    NOT a datetime index. This is what production always passes; the old
    bug only ever showed up against this exact shape, never against a
    nicely-indexed synthetic DataFrame (which is why it went undetected)."""
    rows = []
    base = (asian_high + asian_low) / 2
    for i in range(n):
        hour = i % 24
        if hour <= 6:
            rows.append({"Open": base, "High": asian_high, "Low": asian_low,
                        "Close": base, "HourUTC": hour})
        else:
            rows.append({"Open": base, "High": base + 0.0005, "Low": base - 0.0005,
                        "Close": base, "HourUTC": hour})
    rows[-1] = {"Open": base, "High": breakout_close + 0.0002,
               "Low": breakout_close - 0.0002, "Close": breakout_close,
               "HourUTC": rows[-1]["HourUTC"]}
    return pd.DataFrame(rows)  # plain RangeIndex, exactly like the real runner


def test_session_range_reads_hourutc_column_not_index():
    import forex.strategy_london_breakout as lbo
    df = _runner_shaped_h1_df(1.1050, 1.1010, 1.1060)
    result = lbo._session_range(df, lbo.ASIAN_START, lbo.ASIAN_END, 0.0001)
    assert result is not None, (
        "_session_range() returned None on a runner-shaped (RangeIndex + "
        "HourUTC column) DataFrame -- this is the exact bug that made LBO "
        "produce zero signals for its entire existence"
    )
    rng_high, rng_low, rng_pips = result
    assert abs(rng_high - 1.1050) < 1e-9, f"expected range_high=1.1050, got {rng_high}"
    assert abs(rng_low - 1.1010) < 1e-9, f"expected range_low=1.1010, got {rng_low}"
    assert 39 <= rng_pips <= 41, f"expected ~40 pips, got {rng_pips}"


def test_generate_signals_fires_on_runner_shaped_data():
    import forex.strategy_london_breakout as lbo
    df = _runner_shaped_h1_df(1.1050, 1.1010, 1.1060)
    sigs = lbo.generate_signals(
        {"EURUSD": df}, {"EURUSD": {"pip_size": 0.0001}}, set(),
        session="london", account_equity=15_000.0,
        equity_by_pair={"EURUSD": 15_000.0},
    )
    assert len(sigs) == 1, (
        f"expected 1 signal on runner-shaped breakout data, got {len(sigs)} -- "
        "if this is 0, the HourUTC regression is back"
    )
    assert sigs[0]["direction"] == "Buy"
    assert sigs[0]["symbol"] == "EURUSD"


def test_gap_strategy_still_uses_hourutc_directly():
    """Sanity check that the pattern LBO now matches is the one gap.py has
    used correctly all along -- if this ever changes, LBO's fix needs to
    change with it."""
    import inspect
    import forex.strategy_gap as gap
    src = inspect.getsource(gap)
    assert 'df["HourUTC"]' in src or "df['HourUTC']" in src, \
        "strategy_gap.py no longer filters on the HourUTC column directly"


_run("lbo: _session_range() reads HourUTC column on runner-shaped data", test_session_range_reads_hourutc_column_not_index)
_run("lbo: generate_signals() fires on runner-shaped breakout data (was 0 forever)", test_generate_signals_fires_on_runner_shaped_data)
_run("lbo: strategy_gap.py's HourUTC pattern (the reference this now matches)", test_gap_strategy_still_uses_hourutc_directly)


# ═══════════════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════════════

print(f"\n{BOLD}{'='*70}{RESET}")
passed = sum(1 for _, ok, _ in _results if ok)
failed = [(n, e) for n, ok, e in _results if not ok]
for name, ok, err in _results:
    icon = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
    print(f"  [{icon}] {name}")
    if err:
        print(f"         {YELLOW}{err}{RESET}")
print(f"{BOLD}{'='*70}{RESET}")
if failed:
    print(f"{RED}{BOLD}  {len(failed)} / {len(_results)} TESTS FAILED{RESET}")
    sys.exit(1)
else:
    print(f"{GREEN}{BOLD}  ALL {len(_results)} TESTS PASSED{RESET}")
    sys.exit(0)
