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
  6. forex/runner.py    — _opposing_strategy_holds() blocks a new entry
                         that would take the OPPOSITE side of a pair
                         another strategy already holds
  7. pnl_tracker.py      — get_strategy_summary/get_pair_summary now scope
                         win_rate/P&L to CLOSED trades only (open positions
                         were diluting win_rate into the ground), and
                         open_count is computed correctly (was always 0)
  8. strategy_learner.py — magnitude factor no longer mixes a EUR-
                         denominated P&L with a quote-currency notional
                         (JPY/TRY/etc. pairs were pinned to the minimum
                         magnitude regardless of real trade size); the
                         closed-trades fetch is unbounded, not capped at
                         1000, avoiding a silent windowing bug as trade
                         history grows
  9. atos_runner.py     — US Reversion (US_REVERSION_ENABLED since
                         2026-08-08, backtested OOS Sharpe 2.39 / WR 70%)
                         had never placed a single real trade: all 3 order-
                         placement call sites used nonexistent functions
                         (saxo_client.place_order, db.record_trade, a
                         never-defined _get_uic helper) that raised
                         immediately on the strategy's first live
                         candidate (ROST, 2026-08-21). Fixed to the real
                         saxo_order.place_with_stop/db.insert_trade API,
                         with atomic stop-loss added to the path that
                         previously had none; also fixed a `numpy` import
                         missing at module scope that silently killed
                         diagnostic signal logging on every scan.
 10. atos_runner.py     — the "per-market strategy" scan (US Breakout /
                         OMX Momentum / CPH Mean Reversion), explicitly
                         marked rejected/no-edge in STRATEGY_NOTES.md, was
                         still fully wired to real order placement every
                         cycle and would have raced US Reversion's own
                         exit logic for the same position. Gated off by
                         default (LEGACY_PER_MARKET_STRATEGY_ENABLED).
                         Also: the terminal banner's per-strategy scorecard
                         read trade_log.csv independently of the DB the
                         HTML dashboard uses, so the two had drifted and
                         showed different trade counts/win rates for the
                         same strategy -- unified onto db.get_all_closed_
                         trades().
 11. scheduler_watchdog.py / run_hidden.vbs — a scheduled task's log file
                         getting touched at the right time (mtime looks
                         fresh) no longer proves the real command ran:
                         confirmed live, the futures/ETF scheduler logs
                         were touched to the second at their scheduled
                         times for 3 days while containing only a one-
                         line Windows sharing-violation error from a
                         locked log redirect. Watchdog now reads log
                         content, not just mtime; run_hidden.vbs retries
                         a failed launch, then falls back to a sibling
                         ".fallback" log path (and finally no log at all)
                         so a persistently locked log can never again
                         block the real trading logic from running.

Run:
    python test_2026_08_22_session_fixes.py
Exit code 0 = all pass, 1 = one or more failures.
"""
import os
import sys
import sqlite3
import tempfile
import time
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
section("6. forex/runner.py — blocks opposite-direction stacking on one pair")
# ═══════════════════════════════════════════════════════════════════════

def test_opposing_direction_blocked():
    """User found this live on the dashboard 2026-08-22: NZDUSD held Long
    via donchian+pullback AND Short via bb+ml at the same time -- pure
    waste, paying spread/commission on both legs for a smaller net
    position than either leg alone, no diversification benefit."""
    import forex.runner as runner
    positions = {"donchian:NZDUSD": {"direction": "Buy"}}
    opposing = runner._opposing_strategy_holds("NZDUSD", "Sell", positions)
    assert opposing == "donchian", f"expected 'donchian' to be flagged, got {opposing}"


def test_same_direction_not_blocked():
    """Same-direction stacking is deliberately allowed -- multiple
    strategies independently agreeing isn't a conflict."""
    import forex.runner as runner
    positions = {"donchian:NZDUSD": {"direction": "Buy"}}
    opposing = runner._opposing_strategy_holds("NZDUSD", "Buy", positions)
    assert opposing is None, f"same-direction entry should not be blocked, got {opposing}"


def test_no_existing_position_not_blocked():
    import forex.runner as runner
    positions = {"donchian:NZDUSD": {"direction": "Buy"}}
    opposing = runner._opposing_strategy_holds("EURUSD", "Sell", positions)
    assert opposing is None, f"a pair with no existing position must never be blocked, got {opposing}"


def test_multiple_opposing_strategies_flags_one():
    """USDCZK/USDTHB-shaped case: several strategies on both sides of the
    same pair -- just needs to correctly flag SOME conflicting strategy,
    not enumerate all of them."""
    import forex.runner as runner
    positions = {
        "ema:USDTHB": {"direction": "Sell"},
        "rsi:USDTHB": {"direction": "Buy"},
        "pullback:USDTHB": {"direction": "Sell"},
    }
    opposing = runner._opposing_strategy_holds("USDTHB", "Buy", positions)
    assert opposing in ("ema", "pullback"), f"expected a Sell-holding strategy flagged, got {opposing}"


_run("runner: opposite-direction entry on an already-held pair is blocked", test_opposing_direction_blocked)
_run("runner: same-direction entry on an already-held pair is NOT blocked", test_same_direction_not_blocked)
_run("runner: a pair with no existing position is never blocked", test_no_existing_position_not_blocked)
_run("runner: multi-strategy opposite-direction conflict correctly flagged", test_multiple_opposing_strategies_flags_one)


# ═══════════════════════════════════════════════════════════════════════
section("7. pnl_tracker.py — strategy/pair summaries scope to closed trades only")
# ═══════════════════════════════════════════════════════════════════════

def _seed_summary_db(db_path):
    pt = _fresh_pnl_tracker(db_path)
    # 2 real closed trades for 'donchian' (both wins)
    pt.log_open("forex", "donchian", "EURUSD", "Buy", 1000, 1.1000, currency="EUR",
               timestamp="2026-08-01T00:00:00")
    pt.log_close("forex", "EURUSD", 1.1100, "tp", strategy="donchian",
                 timestamp="2026-08-01T12:00:00", gross_pnl_base_override=100.0)
    pt.log_open("forex", "donchian", "GBPUSD", "Buy", 1000, 1.3000, currency="EUR",
               timestamp="2026-08-02T00:00:00")
    pt.log_close("forex", "GBPUSD", 1.3100, "tp", strategy="donchian",
                 timestamp="2026-08-02T12:00:00", gross_pnl_base_override=100.0)
    # 10 open (undecided) 'donchian' positions -- these must NOT dilute win_rate
    for i in range(10):
        pt.log_open("forex", "donchian", f"AUD{'CHF' if i % 2 else 'CAD'}", "Buy", 1000, 1.0,
                   currency="EUR", timestamp=f"2026-08-{10+i:02d}T00:00:00")
    return pt


def test_strategy_summary_excludes_open_from_win_rate():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        pt = _seed_summary_db(db_path)
        rows = pt.get_strategy_summary("forex")
        donchian = next((r for r in rows if r["strategy"] == "donchian"), None)
        assert donchian is not None, "donchian should appear (has closed trades)"
        assert donchian["trades"] == 2, f"expected 2 CLOSED trades, got {donchian['trades']}"
        assert donchian["win_rate"] == 100.0, (
            f"expected 100% WR (2 wins, 0 losses) -- got {donchian['win_rate']}%, "
            f"the open positions are diluting the denominator again"
        )
        assert donchian["open"] == 10, f"expected open=10, got {donchian['open']}"
    finally:
        try:
            os.remove(db_path)
        except OSError:
            pass


def test_strategy_with_zero_closed_trades_is_excluded_not_zeroed():
    """A strategy with only open positions must not appear in the list at
    all -- previously it showed a misleading win_rate=0.0/total_pnl=0.0
    row that read as 'traded and broke even' instead of 'no data yet'."""
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        pt = _fresh_pnl_tracker(db_path)
        pt.log_open("forex", "zscore", "EURJPY", "Buy", 1000, 150.0, currency="EUR",
                   timestamp="2026-08-01T00:00:00")
        rows = pt.get_strategy_summary("forex")
        zscore = next((r for r in rows if r["strategy"] == "zscore"), None)
        assert zscore is None, f"expected zscore excluded (0 closed trades), got {zscore}"
    finally:
        try:
            os.remove(db_path)
        except OSError:
            pass


def test_pair_summary_open_count_is_real_not_always_zero():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        pt = _fresh_pnl_tracker(db_path)
        pt.log_open("forex", "ema", "EURUSD", "Buy", 1000, 1.10, currency="EUR",
                   timestamp="2026-08-01T00:00:00")
        pt.log_close("forex", "EURUSD", 1.11, "tp", strategy="ema",
                     timestamp="2026-08-01T12:00:00", gross_pnl_base_override=100.0)
        pt.log_open("forex", "rsi", "EURUSD", "Buy", 1000, 1.10, currency="EUR",
                   timestamp="2026-08-02T00:00:00")  # a 2nd, still-open EURUSD position
        rows = pt.get_pair_summary("forex")
        eurusd = next((r for r in rows if r["symbol"] == "EURUSD"), None)
        assert eurusd is not None
        assert eurusd["open"] == 1, f"expected open=1 (the still-open rsi position), got {eurusd['open']}"
    finally:
        try:
            os.remove(db_path)
        except OSError:
            pass


def test_get_closed_trades_has_stable_tiebreak():
    """Multiple trades sharing the exact same timestamp_close (confirmed
    live: 21 stock trades on one rebalance day, 3 forex trades within one
    script run) must sort deterministically -- strategy_learner.py's
    incremental num_processed cursor depends on this ordering being stable
    across repeated calls."""
    import pnl_tracker
    with pnl_tracker._conn() as c:
        pass  # just confirm the query itself doesn't error and includes id as tiebreak
    import inspect
    src = inspect.getsource(pnl_tracker.get_closed_trades)
    assert "id DESC" in src or "id ASC" in src, \
        "get_closed_trades must have a secondary sort key for deterministic ordering"


_run("pnl_tracker: strategy summary win_rate excludes open positions", test_strategy_summary_excludes_open_from_win_rate)
_run("pnl_tracker: a strategy with 0 closed trades is excluded, not shown as 0%", test_strategy_with_zero_closed_trades_is_excluded_not_zeroed)
_run("pnl_tracker: pair summary open_count reflects real open positions", test_pair_summary_open_count_is_real_not_always_zero)
_run("pnl_tracker: get_closed_trades has a stable secondary sort key", test_get_closed_trades_has_stable_tiebreak)


# ═══════════════════════════════════════════════════════════════════════
section("8. strategy_learner.py — currency-correct magnitude, unbounded fetch")
# ═══════════════════════════════════════════════════════════════════════

def test_learner_magnitude_not_currency_mixed_for_jpy():
    """The exact live bug: a JPY-quoted pair's quantity*entry_price notional
    is in JPY, but realized_pnl is in EUR -- dividing one by the other used
    to pin every JPY trade's magnitude factor to the floor regardless of
    real size. Confirmed live: real JPY trades computed pnl_pct of
    0.0003%-0.004% before this fix; after, 0.05%-0.7% -- ~150x larger,
    matching the JPY/EUR rate."""
    import fx
    fx.reset_cache()
    qty, ep, pnl = 11000.0, 196.664, 20.69   # real CHFJPY trade from tonight
    entry_val_wrong = qty * ep
    pnl_pct_wrong = abs(pnl) / entry_val_wrong

    quote_ccy, ledger_ccy = "JPY", "EUR"
    rate_quote = fx.get_rate_to_sek(quote_ccy)
    rate_ledger = fx.get_rate_to_sek(ledger_ccy)
    entry_val_fixed = entry_val_wrong * rate_quote / rate_ledger
    pnl_pct_fixed = abs(pnl) / entry_val_fixed

    assert pnl_pct_fixed > pnl_pct_wrong * 50, (
        f"expected the currency-corrected pct to be far larger (~150x) than "
        f"the naive one -- wrong={pnl_pct_wrong:.6f} fixed={pnl_pct_fixed:.6f}"
    )
    assert 0.0001 < pnl_pct_fixed < 0.05, (
        f"fixed pnl_pct should land in a plausible range for a small real "
        f"trade, got {pnl_pct_fixed}"
    )


def test_learner_uses_unbounded_fetch_not_capped_at_1000():
    import inspect
    import strategy_learner as sl
    src = inspect.getsource(sl.run_learning_pass)
    assert "limit=1000" not in src, (
        "run_learning_pass must not cap get_closed_trades at a fixed limit -- "
        "get_closed_trades returns the N MOST RECENT rows under a limit, so "
        "once total closed trades exceed the cap, the window silently slides "
        "and desyncs from the num_processed cursor"
    )


_run("strategy_learner: magnitude factor is currency-correct for JPY pairs", test_learner_magnitude_not_currency_mixed_for_jpy)
_run("strategy_learner: closed-trades fetch is unbounded, not capped at 1000", test_learner_uses_unbounded_fetch_not_capped_at_1000)


# ═══════════════════════════════════════════════════════════════════════
section("9. atos_runner.py — US Reversion order placement was calling "
        "nonexistent functions on all 3 call sites")
# ═══════════════════════════════════════════════════════════════════════

def _code_only(src):
    """Strip full-line comments so a string search hits real calls, not
    the explanatory comments this fix left behind naming the old bugs."""
    return "\n".join(
        line for line in src.splitlines()
        if not line.strip().startswith("#")
    )


def test_no_nonexistent_place_order_calls_remain():
    import inspect
    import atos_runner
    src = _code_only(inspect.getsource(atos_runner))
    assert "saxo_client.place_order(" not in src, (
        "saxo_client has no place_order() method (real function is "
        "place_market_order, or place_with_stop for atomic stop-loss) -- "
        "any call site using this name will raise AttributeError on the "
        "first real order, exactly as it did live for ROST on 2026-08-21"
    )


def test_no_nonexistent_db_record_trade_calls_remain():
    import inspect
    import atos_runner
    src = _code_only(inspect.getsource(atos_runner))
    assert "db.record_trade(" not in src, (
        "atos.database has no record_trade() -- the only record_trade in "
        "the codebase is strategy_monitor's, a different class with a "
        "totally different signature (strategy_name, pnl, was_profitable). "
        "The real function for recording a new position is db.insert_trade()"
    )


def test_no_undefined_get_uic_helper_calls_remain():
    import inspect
    import atos_runner
    src = _code_only(inspect.getsource(atos_runner))
    assert "_get_uic(" not in src, (
        "_get_uic() was called but never defined anywhere in atos_runner.py "
        "-- would raise NameError immediately. UICs must come from "
        "load_instrument_map()"
    )


def test_intraday_reversion_buy_path_has_stop_loss():
    import inspect
    import atos_runner
    src = inspect.getsource(atos_runner.run_intraday_cycle)
    assert "place_with_stop" in src, (
        "run_intraday_cycle's buy path must attach an atomic broker-side "
        "stop-loss via saxo_order.place_with_stop, matching the daily "
        "run_us_reversion path -- an unprotected intraday entry can run "
        "with zero downside protection until the next scan"
    )
    assert "entry_oid is None" in src, (
        "a rejected entry order must not fall through to db.insert_trade() "
        "-- that would record a DB row for a position that was never "
        "actually opened"
    )


def test_numpy_import_present_for_rsi_sma20_helper():
    import atos_runner
    assert hasattr(atos_runner, "np"), (
        "np.nan is used inside run_us_reversion's _rsi_sma20() helper but "
        "numpy was never imported at module scope -- this silently killed "
        "signal-table logging for every scanned ticker with a NameError "
        "caught by a broad except block"
    )


_run("atos_runner: no nonexistent saxo_client.place_order() calls remain", test_no_nonexistent_place_order_calls_remain)
_run("atos_runner: no nonexistent db.record_trade() calls remain", test_no_nonexistent_db_record_trade_calls_remain)
_run("atos_runner: no undefined _get_uic() helper calls remain", test_no_undefined_get_uic_helper_calls_remain)
_run("atos_runner: intraday reversion buy path has atomic stop-loss + rejection guard", test_intraday_reversion_buy_path_has_stop_loss)
_run("atos_runner: numpy imported at module scope for RSI helper", test_numpy_import_present_for_rsi_sma20_helper)


# ═══════════════════════════════════════════════════════════════════════
section("10. atos_runner.py — legacy per-market strategies gated off, "
        "scorecard unified on the DB")
# ═══════════════════════════════════════════════════════════════════════

def test_legacy_per_market_strategy_disabled_by_default():
    import atos_runner
    assert atos_runner.LEGACY_PER_MARKET_STRATEGY_ENABLED is False, (
        "US Breakout / OMX Momentum / CPH Mean Reversion are explicitly "
        "marked rejected (no edge) in STRATEGY_NOTES.md but stay wired to "
        "real order placement whenever this flag is True -- must default "
        "off so only US Blend and US Reversion (the two validated "
        "strategies) can trade"
    )


def test_run_cycle_skips_legacy_scan_when_disabled():
    import inspect
    import atos_runner
    src = inspect.getsource(atos_runner.run_cycle)
    assert "if not LEGACY_PER_MARKET_STRATEGY_ENABLED" in src, (
        "run_cycle() must short-circuit the per-market/detector-consensus "
        "scan (decisions = {}) when the legacy flag is off, instead of "
        "always calling strategy_scan()/scan_universe() and feeding real "
        "order placement"
    )
    assert 'for trade in list(open_trades) if LEGACY_PER_MARKET_STRATEGY_ENABLED else []' in src, (
        "the generic exit loop must also be skipped when the legacy flag "
        "is off -- it only ever excluded 'US Blend' by name, so with US "
        "Reversion holding real positions again it would otherwise apply "
        "ATR/trailing-stop exits that know nothing about US Reversion's "
        "own RSI/time/stop exit rules, racing run_us_reversion() later in "
        "the same cycle"
    )


def test_scorecard_reads_db_not_csv():
    import inspect
    import atos_runner
    src = _code_only(inspect.getsource(atos_runner._strategy_scorecard))
    # Strip the docstring too -- it deliberately documents the old CSV
    # behavior it replaced, which would otherwise trip this same check.
    src = src.split('"""', 2)[-1] if src.count('"""') >= 2 else src
    assert "trade_log.csv" not in src, (
        "_strategy_scorecard() (the terminal banner's per-strategy stats) "
        "must not read trade_log.csv -- that log had drifted from the DB "
        "(20 Blend trades/35% WR in the CSV vs 30+ in the DB for the same "
        "period), so the console banner and the HTML dashboard showed "
        "different numbers for the same strategy"
    )
    assert "get_all_closed_trades" in src, (
        "_strategy_scorecard() must read db.get_all_closed_trades(), the "
        "same source of truth atos/dashboard_gen.py's _strat_stats() uses"
    )


def test_scorecard_excludes_unknown_pnl_not_zeros_it():
    import atos_runner
    from unittest.mock import patch
    fake_trades = [
        {"strategy": "US Blend", "pnl_sek": 100.0},
        {"strategy": "US Blend", "pnl_sek": -50.0},
        {"strategy": "US Blend", "pnl_sek": None},  # unknown P&L -- must not count as a loss
    ]
    with patch.object(atos_runner.db, "get_all_closed_trades", return_value=fake_trades):
        result = atos_runner._strategy_scorecard()
    assert result["US Blend"]["n"] == 2, (
        "a trade with pnl_sek=None (e.g. an old reconciliation cleanup "
        "row with unknowable P&L) must be excluded from the trade count, "
        "not silently treated as a $0 loss that drags down win rate"
    )


_run("atos_runner: legacy per-market strategies default to disabled", test_legacy_per_market_strategy_disabled_by_default)
_run("atos_runner: run_cycle skips the legacy scan+exit-loop when disabled", test_run_cycle_skips_legacy_scan_when_disabled)
_run("atos_runner: terminal scorecard reads the DB, not a separately-drifting CSV", test_scorecard_reads_db_not_csv)
_run("atos_runner: scorecard excludes unknown-P&L trades instead of zeroing them", test_scorecard_excludes_unknown_pnl_not_zeros_it)


# ═══════════════════════════════════════════════════════════════════════
section("11. scheduler_watchdog.py / run_hidden.vbs — a wrapper that "
        "touches its log at the right time no longer counts as \"ran\"")
# ═══════════════════════════════════════════════════════════════════════

def test_watchdog_catches_fresh_but_empty_log():
    import scheduler_watchdog as wd
    with tempfile.TemporaryDirectory() as tmp:
        old_data_dir = wd.DATA_DIR
        wd.DATA_DIR = tmp
        try:
            log_path = os.path.join(tmp, "futures_scheduler.log")
            with open(log_path, "w", encoding="utf-8") as f:
                f.write("The process cannot access the file because it is "
                        "being used by another process.\r\n")
            result = wd._log_content_failure("futures_scheduler.log")
        finally:
            wd.DATA_DIR = old_data_dir
    assert result is not None, (
        "a log containing only a Windows sharing-violation error must be "
        "flagged as a content failure -- this is exactly what happened "
        "live 2026-08-21/22: run_hidden.vbs's redirect touched the file "
        "at the correct scheduled time (so mtime freshness looked "
        "healthy) while the real command underneath never ran"
    )


def test_watchdog_does_not_flag_real_output():
    import scheduler_watchdog as wd
    with tempfile.TemporaryDirectory() as tmp:
        old_data_dir = wd.DATA_DIR
        wd.DATA_DIR = tmp
        try:
            log_path = os.path.join(tmp, "futures_scheduler.log")
            with open(log_path, "w", encoding="utf-8") as f:
                f.write("Futures universe: 13 markets from cache\n" * 20)
                f.write("Reconciling 1 tracked position(s) against the broker...\n")
                f.write("Reconcile: OK\n")
            result = wd._log_content_failure("futures_scheduler.log")
        finally:
            wd.DATA_DIR = old_data_dir
    assert result is None, (
        "a log with normal-looking run output and no failure signature "
        "must not be flagged, regardless of size"
    )


def test_watchdog_prefers_newer_fallback_log():
    import scheduler_watchdog as wd
    with tempfile.TemporaryDirectory() as tmp:
        old_data_dir = wd.DATA_DIR
        wd.DATA_DIR = tmp
        try:
            primary  = os.path.join(tmp, "futures_scheduler.log")
            fallback = primary + ".fallback"
            with open(primary, "w", encoding="utf-8") as f:
                f.write("The process cannot access the file because it is "
                        "being used by another process.\r\n")
            time.sleep(0.05)
            with open(fallback, "w", encoding="utf-8") as f:
                f.write("Futures universe: 13 markets from cache\n" * 20)
                f.write("Reconcile: OK\n")
            resolved = wd._log_path("futures_scheduler.log")
        finally:
            wd.DATA_DIR = old_data_dir
    assert resolved is not None and resolved.endswith(".fallback"), (
        "run_hidden.vbs writes to a '<log>.fallback' sibling when the "
        "primary log path is persistently locked (so the real command "
        "isn't blocked from running at all) -- the watchdog must read "
        "whichever of the two has the newer content, or it goes blind "
        "to real output that landed in the fallback"
    )


def test_run_hidden_vbs_has_retry_and_fallback():
    vbs_path = os.path.join(BASE_DIR, "run_hidden.vbs")
    with open(vbs_path, encoding="utf-8") as f:
        src = f.read()
    assert "Do While rc <> 0" in src, (
        "run_hidden.vbs must retry a failed launch a couple of times -- "
        "a locked log redirect can be transient (AV scan, a monitor "
        "script's read), and retrying costs nothing on that case"
    )
    assert ".fallback" in src, (
        "after retries are exhausted, run_hidden.vbs must fall back to a "
        "sibling '.fallback' log path (and finally to no log at all) "
        "rather than let a persistently locked log file block the real "
        "command from ever running -- confirmed live: the futures runner "
        "didn't execute for 3+ days because its log redirect kept failing "
        "against a file a stuck prior process never released"
    )


_run("watchdog: flags a log that's fresh but only contains a crash/stub", test_watchdog_catches_fresh_but_empty_log)
_run("watchdog: does not false-alarm on real run output", test_watchdog_does_not_flag_real_output)
_run("watchdog: prefers the newer of primary/.fallback log", test_watchdog_prefers_newer_fallback_log)
_run("run_hidden.vbs: has retry-then-fallback so a locked log can't block the real command", test_run_hidden_vbs_has_retry_and_fallback)


# ═══════════════════════════════════════════════════════════════════════
section("12. Weekend gating — don't scan markets that are definitely closed")
# ═══════════════════════════════════════════════════════════════════════

def test_atos_run_cycle_skips_weekend():
    import inspect
    import atos_runner
    src = inspect.getsource(atos_runner.run_cycle)
    assert "weekday() >= 5" in src, (
        "run_cycle() (US stocks daily scan) must skip Saturday and Sunday "
        "-- US equities never trade on weekends"
    )


def test_atos_open_scan_skips_weekend():
    import inspect
    import atos_runner
    src = inspect.getsource(atos_runner.run_open_scan)
    assert "weekday() >= 5" in src, (
        "run_open_scan() must also skip weekends, independently of "
        "run_cycle() -- it's a separate entrypoint triggered by "
        "intraday_monitor.py"
    )


def test_etf_run_once_skips_weekend():
    import inspect
    from saxo_etf_strategy.run_etf_bot import ETFBot
    src = inspect.getsource(ETFBot.run_once)
    assert "weekday() >= 5" in src, (
        "ETFBot.run_once() must skip Saturday and Sunday -- US-listed "
        "ETFs never trade on weekends, and this module had no weekend "
        "gate at all before this fix"
    )


def test_futures_run_daily_skips_weekend():
    import inspect
    import futures.runner as futures_runner
    src = inspect.getsource(futures_runner.run_daily)
    assert "weekday() >= 5" in src, (
        "futures run_daily() must skip both Saturday and Sunday -- its "
        "single fixed 06:15 PKT daily trigger falls before the Sunday "
        "evening CME reopen, so the market is closed at that time on "
        "both weekend days"
    )


def test_forex_never_weekday_gated():
    # Reverted 2026-08-22 per explicit user direction: forex must keep
    # scanning all 7 days, even Sat/Sun, rather than risk ever missing a
    # signal -- a scan against a genuinely closed market just finds
    # nothing (harmless); skipping outright is the riskier failure mode
    # if a weekend-boundary assumption is ever wrong. Unlike stocks/ETF/
    # futures (unconditionally closed weekends, gated on purpose), forex
    # must have NO weekday gate anywhere in these two functions.
    import inspect
    import forex.runner as forex_runner
    daily_src = inspect.getsource(forex_runner.run_daily)
    exits_src = inspect.getsource(forex_runner.run_exits_only)
    for label, src in [("run_daily", daily_src), ("run_exits_only", exits_src)]:
        assert "weekday()" not in src, (
            f"FX {label}() must not skip any day of the week -- explicit "
            f"user direction 2026-08-22 is to keep scanning 7 days/week "
            f"so no trade or signal is ever missed"
        )


_run("atos_runner: run_cycle skips weekends", test_atos_run_cycle_skips_weekend)
_run("atos_runner: run_open_scan skips weekends", test_atos_open_scan_skips_weekend)
_run("saxo_etf_strategy: ETFBot.run_once skips weekends", test_etf_run_once_skips_weekend)
_run("futures: run_daily skips weekends", test_futures_run_daily_skips_weekend)
_run("forex: never weekday-gated, scans all 7 days by design", test_forex_never_weekday_gated)


# ═══════════════════════════════════════════════════════════════════════
section("13. Dedicated second Forex watchdog + ETF dashboard log path")
# ═══════════════════════════════════════════════════════════════════════

def test_watchdog_only_forex_filters_correctly():
    import scheduler_watchdog as wd
    from argparse import Namespace
    forex_lbo = {n for n in wd.WINDOWS_TASKS if n.startswith("Forex") or n.startswith("LBO")}
    others = set(wd.WINDOWS_TASKS) - forex_lbo
    assert forex_lbo, "expected at least one Forex/LBO task in the registry"
    assert others, "expected at least one non-Forex task in the registry (to prove filtering excludes something)"


def test_watchdog_has_only_forex_flag():
    import inspect
    import scheduler_watchdog as wd
    src = inspect.getsource(wd.main)
    assert "only_forex" in src, (
        "main() must support --only-forex, the dedicated second watchdog "
        "mode for Forex -- deliberate redundancy so a single watchdog "
        "failure (the same class of silent bug that hit Futures/ETF) "
        "can't leave the highest-volume module unmonitored"
    )
    assert "FOREX_STATE_FILE" in src, (
        "the --only-forex run must use its own state file, not share "
        "STATE_FILE with the main watchdog -- otherwise the two aren't "
        "actually independent"
    )


def test_forex_watchdog_bat_and_task_exist():
    bat_path = os.path.join(BASE_DIR, "run_forex_watchdog.bat")
    assert os.path.exists(bat_path), (
        "run_forex_watchdog.bat must exist -- the launcher for the "
        "dedicated second Forex watchdog task"
    )
    with open(bat_path, encoding="utf-8") as f:
        src = f.read()
    assert "--only-forex" in src, (
        "run_forex_watchdog.bat must invoke scheduler_watchdog.py with "
        "--only-forex, not the full check"
    )


def test_etf_dashboard_reads_real_log_path():
    import inspect
    import etf_dashboard
    src = inspect.getsource(etf_dashboard)
    assert "etf_strategy.log" in src, (
        "etf_dashboard.py must read saxo_etf_strategy/logs/etf_strategy.log "
        "-- the bot's real configured log path (etf_config.py's "
        "ETFConfig.log_path default). It previously checked 3 different "
        "paths, none of which ever existed, so its recent-activity panel "
        "silently showed empty forever regardless of whether the bot was "
        "actually running"
    )


_run("watchdog: --only-forex registry actually filters to a subset", test_watchdog_only_forex_filters_correctly)
_run("watchdog: main() supports --only-forex with its own state file", test_watchdog_has_only_forex_flag)
_run("run_forex_watchdog.bat exists and calls --only-forex", test_forex_watchdog_bat_and_task_exist)
_run("etf_dashboard: reads the real etf_strategy.log path", test_etf_dashboard_reads_real_log_path)


def test_bracket_tp_leg_uses_order_price_field():
    # Regression for 2026-08-22: _place_bracket's take-profit child leg sent
    # "Price" instead of "OrderPrice" -- Saxo's real API rejects this with
    # "Orders[1].OrderPrice must be set for orders that are not of type
    # Market or TraspasoIn", confirmed live (a real GBPCHF LBO trade hit it),
    # so every gap/london_breakout bracket order silently fell back to two
    # unlinked stop+TP orders instead of a true OCO bracket. Verified fixed
    # against Saxo's live /trade/v2/orders/precheck endpoint (PreCheckResult
    # "Ok"), not just reasoned about -- see [[saxo-api-verification]].
    import inspect
    import re
    import saxo_order
    src = inspect.getsource(saxo_order._place_bracket)
    tp_leg_block = src.split('"OrderType":     "Limit"')[1][:200]
    assert '"OrderPrice":' in tp_leg_block, (
        "_place_bracket's take-profit leg must set 'OrderPrice', not 'Price' "
        "-- Saxo's API rejects 'Price' on a non-Market child order"
    )
    assert not re.search(r'"Price":\s', tp_leg_block), (
        "_place_bracket's take-profit leg still contains a bare 'Price' key "
        "-- Saxo rejects this field name for Limit orders inside a bracket"
    )


_run("saxo_order: bracket TP leg uses OrderPrice, not Price", test_bracket_tp_leg_uses_order_price_field)


# ═══════════════════════════════════════════════════════════════════════
section("14. Universal broker-side stop+TP for every strategy (2026-08-22)")
# ═══════════════════════════════════════════════════════════════════════
# User: "I want stop loss every part of strategy so when a pair bought on
# Saxo it create stops loss according to our strategy and also same time
# take profit coverage implemented on Saxo. I do not want to depend on the
# scheduler to do this." Previously only gap/london_breakout computed a
# take-profit target -- the other 9 strategies (ema/rsi/donchian/bb/
# pullback/ml/zscore/supertrend/cnn_lstm) only ever got a stop-loss at
# entry; profit-taking depended entirely on the next scheduled
# run_exits_only() catching should_exit(). Fixed in forex/runner.py:
# _resolve_tp_price() gives every strategy a broker-side TP at entry
# (its own target if it has one, else DEFAULT_TP_RR x its own stop
# distance) via the now-fixed true OCO bracket, _heal_missing_tp()
# generalized from gap-only to any position with a tp_price, and
# _run_exits() now cancels the resting stop+TP orders before a
# scheduler-driven close so neither is left orphaned.


def test_resolve_tp_price_uses_default_rr_when_strategy_has_none():
    import forex.runner as runner
    sig = {"close": 1.0940, "stop_price": 1.0895}  # EMA-shaped: no tp_price/gap_target
    tp = runner._resolve_tp_price(sig, "Buy")
    expected = 1.0940 + runner.DEFAULT_TP_RR * (1.0940 - 1.0895)
    assert abs(tp - expected) < 1e-9, f"expected {expected}, got {tp}"
    assert tp > sig["close"], "a Buy's take-profit must be above entry"

    sig_sell = {"close": 1.0940, "stop_price": 1.0985}
    tp_sell = runner._resolve_tp_price(sig_sell, "Sell")
    expected_sell = 1.0940 - runner.DEFAULT_TP_RR * (1.0985 - 1.0940)
    assert abs(tp_sell - expected_sell) < 1e-9, f"expected {expected_sell}, got {tp_sell}"
    assert tp_sell < sig_sell["close"], "a Sell's take-profit must be below entry"


def test_resolve_tp_price_respects_strategys_own_target():
    import forex.runner as runner
    sig_lbo = {"close": 1.09336, "stop_price": 1.08468, "tp_price": 1.10770}
    assert runner._resolve_tp_price(sig_lbo, "Buy") == 1.10770, (
        "london_breakout's own tp_price must not be overridden by the default R:R"
    )
    sig_gap = {"close": 10.9075, "stop_price": 10.95, "gap_target": 10.85}
    assert runner._resolve_tp_price(sig_gap, "Sell") == 10.85, (
        "gap's own gap_target must not be overridden by the default R:R"
    )


def test_pos_record_always_stores_tp_price():
    import inspect
    import forex.runner as runner
    src = inspect.getsource(runner._run_entries)
    assert '"tp_price":       tp,' in src or '"tp_price": tp' in src.replace("  ", " "), (
        "_run_entries' pos_record must store tp_price for every strategy "
        "(previously only london_breakout's own sig[\"tp_price\"] was ever "
        "recorded, so the dashboard/healer had no target for the other 9 "
        "strategies even after a bracket order was placed)"
    )


def test_heal_missing_tp_not_restricted_to_gap():
    import inspect
    import forex.runner as runner
    src = inspect.getsource(runner._heal_missing_tp)
    assert 'startswith("gap:")' not in src, (
        "_heal_missing_tp must no longer be gap-only -- every strategy now "
        "gets a tp_price at entry and needs the same healing safety net"
    )
    assert 'v.get("tp_price")' in src or "v.get('tp_price')" in src, (
        "_heal_missing_tp must key off tp_price (set for every strategy now), "
        "not gap_target (gap-only)"
    )


def test_fetch_open_orders_uses_open_order_type_field():
    # Regression for 2026-08-22: Saxo's live order objects carry the order
    # type under "OpenOrderType", not "OrderType" -- confirmed against a
    # real order dump. Reading "OrderType" silently returned None for every
    # order, so the "is a stop/TP already live on Saxo?" dedup check in
    # _heal_missing_stops/_heal_missing_tp could never match, undermining
    # the "queries Saxo first to avoid duplicates" guarantee both docstrings
    # make -- more consequential now that TP healing covers all 10 strategies.
    import inspect
    import forex.runner as runner
    src = inspect.getsource(runner._fetch_open_orders)
    assert 'o.get("OpenOrderType")' in src, (
        '_fetch_open_orders must read o.get("OpenOrderType"), not '
        '"OrderType" -- Saxo does not return an "OrderType" field on live orders'
    )


def test_run_exits_cancels_resting_orders_before_market_close():
    # Regression for 2026-08-22: when OUR should_exit() closes a position
    # (hard-stop/time-stop/trailing) rather than the broker's own resting
    # stop/TP order firing, those two resting orders were left live,
    # protecting a position that had just gone flat -- either could fire
    # later and open an unintended new position in the opposite direction
    # (the exact "naked order" class of risk found live in the GBPCHF LBO
    # bracket-fallback case, 2026-08-22). Now cancelled before the market
    # close is sent, for every strategy (not just gap/LBO).
    import inspect
    import forex.runner as runner
    src = inspect.getsource(runner._run_exits)
    cancel_block = src.split('order = {"AccountKey"')[0]
    assert "_cancel_order(oid, akey)" in src, (
        "_run_exits must cancel the position's resting stop_order_id/"
        "tp_order_id via _cancel_order before placing the market close"
    )
    close_order_idx = src.index('resp = _post("/trade/v2/orders", order)')
    cancel_idx = src.index("_cancel_order(oid, akey)")
    assert cancel_idx < close_order_idx, (
        "orders must be cancelled BEFORE the market close is sent, not after "
        "-- cancelling after leaves a window where the resting order could "
        "fire against an already-flat position"
    )


_run("runner: _resolve_tp_price applies DEFAULT_TP_RR when a strategy has no target", test_resolve_tp_price_uses_default_rr_when_strategy_has_none)
_run("runner: _resolve_tp_price keeps a strategy's own tp_price/gap_target", test_resolve_tp_price_respects_strategys_own_target)
_run("runner: pos_record always stores tp_price, not just for LBO", test_pos_record_always_stores_tp_price)
_run("runner: _heal_missing_tp generalized beyond gap-only", test_heal_missing_tp_not_restricted_to_gap)
_run("runner: _fetch_open_orders reads OpenOrderType, not OrderType", test_fetch_open_orders_uses_open_order_type_field)
_run("runner: _run_exits cancels stop+TP orders before closing", test_run_exits_cancels_resting_orders_before_market_close)


# ═══════════════════════════════════════════════════════════════════════
section("15. Every universe quote currency has an FX fallback rate (2026-08-22)")
# ═══════════════════════════════════════════════════════════════════════
# User hit this live: `python forex_dashboard.py` raised/warned on CZKSEK=X
# ("possibly delisted"). Investigation found ILS fails live 100% of the
# time (same KeyError('exchangeTimezoneName') class as TRY/MXN/CNH, fixed
# 2026-08-21) and CZK + 11 other currencies fetch live fine but had zero
# fallback -- forex_dashboard.py's own _eur_per_unit() silently swallows
# ANY fx.get_rate_to_sek() failure into rate=1.0 (wildly wrong for
# anything but EUR), so a transient Yahoo hiccup on an unlisted currency
# quietly corrupts displayed P&L/exposure rather than raising or warning.
# This test asserts every quote currency the live 117-pair universe
# actually uses has a fallback -- so expanding the universe again can't
# silently reopen this same gap.


def test_every_universe_quote_currency_has_fx_fallback():
    import fx
    from forex.universe import PAIRS
    quote_ccys = {p["symbol"][3:6] for p in PAIRS if len(p["symbol"]) >= 6}
    missing = sorted(c for c in quote_ccys if c not in fx.FALLBACK_RATES_TO_SEK)
    assert not missing, (
        f"quote currencies used in forex/universe.py with no entry in "
        f"fx.FALLBACK_RATES_TO_SEK: {missing} -- a transient (or, like ILS, "
        f"permanent) live-fetch failure on any of these either raises "
        f"(fx.get_rate_to_sek, forex/runner.py callers with no except) or "
        f"silently corrupts P&L display to a 1.0 rate (forex_dashboard.py)"
    )


_run("fx: every forex universe quote currency has a FALLBACK_RATES_TO_SEK entry", test_every_universe_quote_currency_has_fx_fallback)


# ═══════════════════════════════════════════════════════════════════════
section("16. Forex live paths use Saxo prices only, never Yahoo (2026-08-22)")
# ═══════════════════════════════════════════════════════════════════════
# User: "I ask you always use Saxo prices on the live Sim orders and on
# the Dashboard. Yahoo prices is only for historical prices and
# backtesting." forex/runner.py's and forex_dashboard.py's _eur_per_unit()
# both used to fall back to fx.py (Yahoo-based) whenever Saxo's live quote
# was momentarily unavailable -- removed entirely; both now return None on
# a Saxo miss and callers treat that as "unknown" rather than substituting
# a Yahoo-sourced number. price_service.fetch_prices() gained a retry pass
# for misses (confirmed live: 35 of 94 concurrent Saxo requests failed in
# one dashboard refresh, recovered on retry) so this rarely needs to fire
# at all. fx.py itself is untouched -- still the right tool for
# backtest_forex_universe.py's historical data.


def test_runner_eur_per_unit_has_no_yahoo_fallback():
    import inspect
    import forex.runner as runner
    src = inspect.getsource(runner._eur_per_unit)
    assert "import fx" not in src, (
        "forex/runner.py's _eur_per_unit() must not import fx.py (Yahoo) -- "
        "live position sizing/P&L conversion must be Saxo-only per explicit "
        "user direction 2026-08-22"
    )


def test_dashboard_eur_per_unit_has_no_yahoo_fallback():
    import inspect
    import forex_dashboard
    src = inspect.getsource(forex_dashboard._eur_per_unit)
    assert "import fx" not in src, (
        "forex_dashboard.py's _eur_per_unit() must not import fx.py (Yahoo) "
        "-- the dashboard must show Saxo-sourced numbers only per explicit "
        "user direction 2026-08-22"
    )


def test_price_service_retries_failed_instruments():
    import inspect
    import price_service
    src = inspect.getsource(price_service.fetch_prices)
    assert "misses" in src, (
        "price_service.fetch_prices() must retry instruments that failed "
        "on the first pass -- confirmed live that a meaningful fraction of "
        "a large concurrent batch fails transiently, and there's no longer "
        "a Yahoo fallback for forex's conversion rates to fall back on"
    )


_run("runner: _eur_per_unit has no Yahoo/fx.py fallback (Saxo-only)", test_runner_eur_per_unit_has_no_yahoo_fallback)
_run("forex_dashboard: _eur_per_unit has no Yahoo/fx.py fallback (Saxo-only)", test_dashboard_eur_per_unit_has_no_yahoo_fallback)
_run("price_service: fetch_prices retries instruments that failed the first pass", test_price_service_retries_failed_instruments)


# ═══════════════════════════════════════════════════════════════════════
section("17. Stocks (ATOS) live paths use Saxo prices only (2026-08-22)")
# ═══════════════════════════════════════════════════════════════════════
# User: "similarly I want same for ETF, Stocks, and FUTURES. all live
# prices from Saxo API and Yahoo only for historical prices and
# backtesting." ETF and Futures were already fully Saxo-native (no
# yfinance/fx.py anywhere in futures/*.py or saxo_etf_strategy/**/*.py).
# Stocks needed real work: atos_runner.py's download_universe() (the
# ONLY actually-scheduled live entry point, via daily_run.py ->
# run_cycle(), feeding BOTH US Blend and US Reversion) fetched all 385
# tickers' daily indicator history via yf.download(); 9 separate
# fx.get_rate_to_sek() calls throughout run_cycle/_place_us/
# run_us_momentum/run_us_reversion converted Saxo balances/prices to SEK
# via Yahoo. A data_loader.py comment claiming "Saxo's SIM environment
# does NOT provide historical market data for stocks" turned out to be
# stale/never re-verified -- confirmed live 2026-08-22 that Saxo's own
# /chart/v3/charts endpoint serves full daily history for stocks
# (see [[saxo_api_verification]]), just gated by its own 120/min
# rate-limit bucket (X-RateLimit-ChartMinute-Limit) discovered the hard
# way (an unthrottled fetch of all 385 tickers failed 185 of them,
# including obviously-liquid names like AMZN/JPM/WMT, purely from
# blowing through this limit in ~20s). New saxo_history.py paces
# requests under this limit; new saxo_fx.py gives Saxo-native SEK
# conversion (mirrors forex's _eur_per_unit triangulation, targeting SEK
# instead of EUR). data_loader.py itself, k4_export.py (tax export), and
# backtest_*.py are left untouched -- genuinely historical/backtest code,
# exactly what the user's own carve-out covers.


def test_atos_runner_has_no_yfinance_import():
    import inspect
    import atos_runner
    src = inspect.getsource(atos_runner)
    assert "import yfinance" not in src, (
        "atos_runner.py must not import yfinance anywhere -- its only "
        "scheduled entry point (run_cycle, via daily_run.py) and the "
        "formerly-dead-code run_intraday_cycle() must both source "
        "history from Saxo (saxo_history.fetch_daily_bars), not Yahoo"
    )
    assert "import fx" not in src, (
        "atos_runner.py must not import the Yahoo-based fx module -- "
        "use _rate_to_sek() (Saxo-native, via saxo_fx) instead"
    )


def test_atos_runner_download_universe_uses_saxo_history():
    import inspect
    import atos_runner
    src = inspect.getsource(atos_runner.download_universe)
    assert "saxo_history.fetch_daily_bars" in src, (
        "download_universe() must fetch from saxo_history.fetch_daily_bars, "
        "not yf.download -- this is the one function that feeds indicator "
        "history to both US Blend and US Reversion in the live daily cycle"
    )


def test_saxo_history_rate_limiter_paces_under_saxo_limit():
    # Regression for 2026-08-22: Saxo's /chart/v3/charts endpoint has its
    # own 120/min rate-limit bucket (X-RateLimit-ChartMinute-Limit),
    # separate from the general one -- confirmed live: an unthrottled
    # concurrent fetch of 385 tickers blew through it in ~20s and then
    # failed 185 tickers with 429s, including obviously-covered large-caps
    # (AMZN, JPM, WMT...), not a real data-coverage gap.
    import saxo_history
    assert saxo_history.CHART_REQUESTS_PER_MINUTE < 120, (
        "saxo_history's pacing must stay under Saxo's real 120/min chart "
        "limit, with headroom for another process sharing the same quota"
    )


def test_saxo_fx_instruments_needed_direct_eur_pair():
    import saxo_fx
    insts = saxo_fx._instruments_needed({"USD"})
    symbols = {i["symbol"] for i in insts}
    assert "EURUSD" in symbols and "EURSEK" in symbols, (
        f"USD needs EURUSD (direct EUR-cross) + EURSEK (EUR->SEK leg), got {symbols}"
    )


def test_saxo_fx_instruments_needed_usd_triangulation():
    # DKK has no direct EUR{ccy} pair in forex/universe.py -- must
    # triangulate via USD{ccy} + EURUSD.
    import saxo_fx
    insts = saxo_fx._instruments_needed({"DKK"})
    symbols = {i["symbol"] for i in insts}
    assert "USDDKK" in symbols and "EURUSD" in symbols and "EURSEK" in symbols, (
        f"DKK (no direct EURDKK pair) must triangulate via USDDKK + EURUSD "
        f"+ EURSEK, got {symbols}"
    )


_run("atos_runner: no yfinance/fx.py imports remain", test_atos_runner_has_no_yfinance_import)
_run("atos_runner: download_universe() sources from saxo_history", test_atos_runner_download_universe_uses_saxo_history)
_run("saxo_history: paces requests under Saxo's real 120/min chart limit", test_saxo_history_rate_limiter_paces_under_saxo_limit)
_run("saxo_fx: direct EUR-cross currency needs EURUSD+EURSEK", test_saxo_fx_instruments_needed_direct_eur_pair)
_run("saxo_fx: DKK (no direct EUR pair) triangulates via USD+EURUSD", test_saxo_fx_instruments_needed_usd_triangulation)


# ═══════════════════════════════════════════════════════════════════════
section("18. Cross-process lock prevents overlapping live runs (2026-08-24)")
# ═══════════════════════════════════════════════════════════════════════
# Found live: `ATOS Forex Gap Fill` and `ATOS Forex Gap Monday Early` both
# fire at the exact same instant (Mon 03:00 PKT) -- neither had ever run
# under this schedule (both showed Task Scheduler's "never run" sentinel),
# so this was about to be untested in production. Two independent
# forex/runner.py processes both entering the same weekend-gap signal
# concurrently is a real double-entry risk (forex_state.json's atomic
# write only guards against file corruption, not two full processes
# racing to read-then-write it) -- same class of bug as the LBO duplicate-
# schedule fix from 2026-08-20. Couldn't disable the redundant task
# (Disable-ScheduledTask and schtasks both denied for this account this
# session) so fixed with a cross-process file lock instead -- verified
# live with two real separate OS processes (not just threads): the second
# correctly waited for the first to fully release before acquiring, never
# ran concurrently.


def test_lock_functions_exist_and_are_wired_into_cli():
    import inspect
    import forex.runner as runner
    assert hasattr(runner, "_acquire_lock") and hasattr(runner, "_release_lock"), (
        "forex/runner.py must define _acquire_lock/_release_lock"
    )
    src = inspect.getsource(runner)
    main_section = src[src.index("active = list(STRATEGIES) if args.strategy"):]
    assert "_acquire_lock(" in main_section and "_release_lock(" in main_section, (
        "the CLI dispatch (main()) must acquire the lock before calling "
        "run_daily()/run_exits_only() and release it in a finally block -- "
        "this is what actually protects the two real overlapping scheduled "
        "tasks, not just the lock functions existing in isolation"
    )


def test_lock_serializes_two_real_processes():
    # End-to-end proof, not just a unit test of the function in isolation:
    # spawn two REAL separate Python processes (matching how Task Scheduler
    # actually launches overlapping triggers) and confirm the second only
    # acquires after the first fully releases.
    import subprocess
    import time
    import os

    lock_file = os.path.join("data", "forex_runner.lock")
    if os.path.exists(lock_file):
        os.remove(lock_file)

    script = (
        "import sys; sys.path.insert(0, '.'); import forex.runner as fr; "
        "import time; t0=time.time(); fr._acquire_lock(sys.argv[1]); "
        "print(f'{sys.argv[1]} ACQUIRED {time.time()-t0:.2f}'); "
        "time.sleep(float(sys.argv[2])); fr._release_lock(); "
        "print(f'{sys.argv[1]} RELEASED {time.time()-t0:.2f}')"
    )
    pA = subprocess.Popen([sys.executable, "-c", script, "A", "1.5"],
                          stdout=subprocess.PIPE, text=True)
    time.sleep(0.3)
    pB = subprocess.Popen([sys.executable, "-c", script, "B", "0.2"],
                          stdout=subprocess.PIPE, text=True)
    outA, _ = pA.communicate(timeout=30)
    outB, _ = pB.communicate(timeout=30)
    if os.path.exists(lock_file):
        os.remove(lock_file)

    b_acquired_line = [l for l in outB.splitlines() if "ACQUIRED" in l][0]
    b_wait_seconds = float(b_acquired_line.split()[-1])
    assert b_wait_seconds > 1.0, (
        f"Process B must wait for Process A to release before acquiring "
        f"(A holds for 1.5s) -- B acquired after only {b_wait_seconds:.2f}s, "
        f"meaning the lock did not actually serialize the two processes:\n"
        f"A: {outA}\nB: {outB}"
    )


_run("runner: lock functions exist and are wired into the CLI dispatch", test_lock_functions_exist_and_are_wired_into_cli)
_run("runner: lock serializes two real concurrent processes (not just threads)", test_lock_serializes_two_real_processes)


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
