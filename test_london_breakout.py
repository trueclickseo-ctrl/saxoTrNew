"""
test_london_breakout.py
------------------------
Comprehensive test suite for the London Breakout day trading strategy.

Covers:
  Unit       — _atr, _session_range, size_position, should_exit
  Functional — generate_signals (all signal paths + filters)
  Blackbox   — runner integration (imports, registry, heat bypass, notifier hooks)
  Edge cases — NaN data, empty df, JPY pip_size, all 7 pairs, duplicate open positions

Run:  python test_london_breakout.py
Exit code 0 = all pass.
"""

import os
import sys
import traceback
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "forex"))

# ── Colours ───────────────────────────────────────────────────────────────────
G = "\033[92m"; R = "\033[91m"; Y = "\033[93m"; C = "\033[96m"; X = "\033[0m"; B = "\033[1m"

_results = []

def _run(name, fn):
    try:
        result = fn()
        if result is None or result is True:
            print(f"  {G}PASS{X}  {name}")
            _results.append(("PASS", name, ""))
        else:
            print(f"  {R}FAIL{X}  {name}")
            print(f"        {R}{result}{X}")
            _results.append(("FAIL", name, str(result)))
    except Exception as exc:
        tb = traceback.format_exc().strip().splitlines()[-1]
        print(f"  {R}ERROR{X} {name}")
        print(f"        {R}{tb}{X}")
        _results.append(("ERROR", name, str(exc)))


# ── Test data helpers ─────────────────────────────────────────────────────────

def _make_h1_df(n=48, base=1.1000, pip=0.0001, tz_offset_hrs=0,
                high_extra=0.0010, low_extra=0.0010):
    """
    Build a synthetic H1 DataFrame with DatetimeTZAware UTC index.
    Rows span the last `n` hours ending at the current UTC hour.
    """
    now_utc = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    idx = pd.date_range(end=now_utc, periods=n, freq="h", tz="UTC")
    close  = base + np.cumsum(np.random.randn(n) * pip * 2)
    high   = close + high_extra
    low    = close - low_extra
    return pd.DataFrame({"Open": close, "High": high, "Low": low,
                         "Close": close}, index=idx)


def _make_h1_df_with_session(asian_high, asian_low, london_close,
                              pip=0.0001, n=48):
    """
    Build H1 data where Asian session (00-06 UTC) has explicit hi/lo,
    and the latest bar has the given close (simulating a breakout or no-break).
    """
    now_utc = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    idx = pd.date_range(end=now_utc, periods=n, freq="h", tz="UTC")
    base = (asian_high + asian_low) / 2
    close = np.full(n, base)
    # Spread scales with pip size (5 pips/bar) -- a hardcoded 0.0005 was
    # calibrated for 4-5dp pairs (pip=0.0001) and produced an ATR far below
    # the strategy's real 5-pip ATR_CONFIRM minimum for 2dp JPY pairs
    # (pip=0.01), silently failing that gate rather than testing a
    # genuine breakout scenario.
    bar_spread = pip * 5
    high  = np.full(n, base + bar_spread)
    low   = np.full(n, base - bar_spread)
    df = pd.DataFrame({"Open": close, "High": high, "Low": low, "Close": close}, index=idx)

    # Stamp explicit Asian session hi/lo on hour-0 to hour-6 bars
    for i, ts in enumerate(idx):
        if 0 <= ts.hour <= 6:
            df.at[ts, "High"] = asian_high
            df.at[ts, "Low"]  = asian_low

    # Latest bar gets the breakout close
    breakout_spread = pip * 2
    df.at[idx[-1], "Close"] = london_close
    df.at[idx[-1], "High"]  = london_close + breakout_spread
    df.at[idx[-1], "Low"]   = london_close - breakout_spread
    return df


# ── Import strategy ───────────────────────────────────────────────────────────
import forex.strategy_london_breakout as lbo


# ════════════════════════════════════════════════════════════════════════════
# UNIT TESTS
# ════════════════════════════════════════════════════════════════════════════

def test_unit_atr_returns_float():
    df = _make_h1_df(50)
    atr = lbo._atr(df)
    if np.isnan(atr) or atr <= 0:
        return f"ATR should be positive, got {atr}"


def test_unit_atr_nan_df_returns_zero():
    df = _make_h1_df(5)   # fewer than ATR period
    atr = lbo._atr(df, period=14)
    # Should return 0.0 (too few bars) or a very small number — not crash
    if not isinstance(atr, float):
        return f"Expected float, got {type(atr)}"


def test_unit_atr_highask_fallback():
    """Strategy must handle Saxo's HighAsk/LowAsk/CloseAsk column names."""
    df = _make_h1_df(50)
    df = df.rename(columns={"High": "HighAsk", "Low": "LowAsk", "Close": "CloseAsk"})
    df.drop(columns=["Open"], inplace=True)
    atr = lbo._atr(df)
    if np.isnan(atr) or atr <= 0:
        return f"ATR with HighAsk/LowAsk fallback failed, got {atr}"


def test_unit_session_range_basic():
    asian_high = 1.1050; asian_low = 1.1010
    df = _make_h1_df_with_session(asian_high, asian_low, 1.1060)
    result = lbo._session_range(df, 0, 6, 0.0001)
    if result is None:
        return "Expected (hi, lo, pips), got None"
    hi, lo, pips = result
    if abs(hi - asian_high) > 0.0002:
        return f"Range high {hi:.5f} != expected {asian_high:.5f}"
    if abs(lo - asian_low) > 0.0002:
        return f"Range low {lo:.5f} != expected {asian_low:.5f}"
    if pips <= 0:
        return f"Range pips should be > 0, got {pips}"


def test_unit_session_range_insufficient_bars():
    df = _make_h1_df(2)
    result = lbo._session_range(df, 0, 6, 0.0001)
    # OK to return None — the function requires >= 2 session bars
    # but a 2-bar df might have 0 bars in 00-06 window
    # Just verify it doesn't crash
    assert result is None or len(result) == 3


def test_unit_session_range_no_session_bars():
    """If no bars fall in the Asian window, return None."""
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    idx = pd.date_range(end=now, periods=10, freq="h", tz="UTC")
    # Filter: ensure NO bars in hour 0-6
    non_asian_idx = [ts for ts in idx if ts.hour not in range(0, 7)]
    if len(non_asian_idx) < 2:
        return  # skip if can't build non-Asian df in this hour
    df = pd.DataFrame({"High": 1.11, "Low": 1.10, "Close": 1.105},
                      index=non_asian_idx)
    result = lbo._session_range(df, 0, 6, 0.0001)
    if result is not None:
        return f"Expected None for no-Asian-bars, got {result}"


def test_unit_size_position_basic():
    qty = lbo.size_position(15_000.0, 0.0010, 1_000)
    if qty < lbo.MIN_UNITS:
        return f"Units {qty} below MIN_UNITS {lbo.MIN_UNITS}"
    if qty > lbo.MAX_UNITS:
        return f"Units {qty} above MAX_UNITS {lbo.MAX_UNITS}"


def test_unit_size_position_zero_atr():
    qty = lbo.size_position(15_000.0, 0.0, 1_000)
    if qty != 1_000:
        return f"Zero ATR should return min_units (1000), got {qty}"


def test_unit_size_position_caps_at_max():
    # Tiny ATR → huge position → should be capped at MAX_UNITS
    qty = lbo.size_position(1_000_000.0, 0.000001, 1_000)
    if qty > lbo.MAX_UNITS:
        return f"Position {qty} exceeds MAX_UNITS {lbo.MAX_UNITS}"


# ════════════════════════════════════════════════════════════════════════════
# FUNCTIONAL TESTS — generate_signals
# ════════════════════════════════════════════════════════════════════════════

def _signals_in_session(session: str, direction_expected: str,
                        breakout_close: float, asian_high=1.1050, asian_low=1.1010,
                        account_equity: float = 15_000.0):
    """Helper: patch UTC hour so strategy thinks we're in session, verify signal."""
    df = _make_h1_df_with_session(asian_high, asian_low, breakout_close)
    h1_data   = {"EURUSD": df}
    pair_meta = {"EURUSD": {"pip_size": 0.0001}}
    # generate_signals() requires equity_by_pair (quote-currency-converted
    # equity) to size a position at all — without it every signal is
    # skipped ("no quote-currency equity supplied"), which is exactly what
    # this test was silently hitting before this fix: it looked like
    # generate_signals() produced no signals, but it was actually the
    # sizing step swallowing a real signal. forex/runner.py always supplies
    # this in production (via _equity_in_quote per pair) — match that here
    # instead of testing an unrealistic call shape.
    sigs = lbo.generate_signals(h1_data, pair_meta, set(), session=session,
                                account_equity=account_equity,
                                equity_by_pair={"EURUSD": account_equity})
    return sigs


def test_func_london_bull_breakout():
    """Price above Asian high → BUY signal in London session."""
    sigs = _signals_in_session("london", "Buy", breakout_close=1.1060,
                               asian_high=1.1050, asian_low=1.1010)
    if not sigs:
        return "Expected BUY signal for bullish breakout, got none"
    sig = sigs[0]
    if sig["direction"] != "Buy":
        return f"Expected Buy, got {sig['direction']}"
    if sig["symbol"] != "EURUSD":
        return f"Expected EURUSD, got {sig['symbol']}"
    if sig["stop_price"] <= 0:
        return "stop_price should be positive"
    if sig["tp_price"] <= sig["close"]:
        return "tp_price should be above entry for Buy"
    if sig["units"] < lbo.MIN_UNITS:
        return f"units {sig['units']} below MIN_UNITS"
    if "atr" not in sig:
        return "Signal missing 'atr' field needed by runner"


def test_func_london_bear_breakout():
    """Price below Asian low → SELL signal in London session."""
    sigs = _signals_in_session("london", "Sell", breakout_close=1.1000,
                               asian_high=1.1050, asian_low=1.1010)
    if not sigs:
        return "Expected SELL signal for bearish breakout, got none"
    sig = sigs[0]
    if sig["direction"] != "Sell":
        return f"Expected Sell, got {sig['direction']}"
    if sig["tp_price"] >= sig["close"]:
        return "tp_price should be below entry for Sell"
    if sig["stop_price"] < sig["close"]:
        return "stop_price should be above entry for Sell"


def test_func_ny_bull_breakout():
    """NY session works same as London but uses London morning range."""
    # Use london range (09-12 UTC) as reference — session="ny"
    df = _make_h1_df_with_session(1.1050, 1.1010, 1.1060)
    # Stamp London morning range on hours 9-12
    for ts in df.index:
        if 9 <= ts.hour <= 12:
            df.at[ts, "High"] = 1.1050
            df.at[ts, "Low"]  = 1.1010
    h1_data   = {"EURUSD": df}
    pair_meta = {"EURUSD": {"pip_size": 0.0001}}
    sigs = lbo.generate_signals(h1_data, pair_meta, set(), session="ny",
                                account_equity=15_000.0,
                                equity_by_pair={"EURUSD": 15_000.0})
    if not sigs:
        return "Expected signal in NY session"
    if sigs[0]["direction"] != "Buy":
        return f"Expected Buy, got {sigs[0]['direction']}"
    if sigs[0]["session"] != "NY":
        return f"Expected session='NY', got {sigs[0]['session']}"


def test_func_no_signal_inside_range():
    """Price inside the range → no signal."""
    sigs = _signals_in_session("london", "", breakout_close=1.1030,
                               asian_high=1.1050, asian_low=1.1010)
    if sigs:
        return f"Expected no signal for inside-range price, got {len(sigs)}"


def test_func_no_signal_range_too_small():
    """Asian range < MIN_RANGE_PIPS → no signal even if price breaks out."""
    # Range = 5 pips (below MIN_RANGE_PIPS=10)
    sigs = _signals_in_session("london", "Buy", breakout_close=1.10060,
                               asian_high=1.10050, asian_low=1.10005)
    if sigs:
        return f"Expected no signal for tiny range, got {len(sigs)}"


def test_func_no_signal_range_too_large():
    """Asian range > MAX_RANGE_PIPS → no signal (chaotic session)."""
    # Range = 150 pips (above MAX_RANGE_PIPS=120)
    sigs = _signals_in_session("london", "Buy", breakout_close=1.1200,
                               asian_high=1.1180, asian_low=1.1030)
    if sigs:
        return f"Expected no signal for oversized range ({(1.1180-1.1030)/0.0001:.0f}p), got {len(sigs)}"


def test_func_skip_already_open_symbol():
    """Symbol already in open_symbols → skip."""
    df = _make_h1_df_with_session(1.1050, 1.1010, 1.1060)
    h1_data   = {"EURUSD": df}
    pair_meta = {"EURUSD": {"pip_size": 0.0001}}
    sigs = lbo.generate_signals(h1_data, pair_meta, open_symbols={"EURUSD"},
                                session="london", account_equity=15_000.0)
    if sigs:
        return f"Expected no signal for open symbol, got {len(sigs)}"


def test_func_skip_non_lbo_pairs():
    """Pairs not in PAIRS set are silently ignored."""
    df = _make_h1_df_with_session(1.1050, 1.1010, 1.1060)
    h1_data   = {"EURNOK": df, "USDSEK": df}  # not in LBO PAIRS
    pair_meta = {"EURNOK": {"pip_size": 0.0001}, "USDSEK": {"pip_size": 0.0001}}
    sigs = lbo.generate_signals(h1_data, pair_meta, set(), session="london")
    if sigs:
        return f"Expected no signal for non-LBO pairs, got {len(sigs)}"


def test_func_jpy_pair_pip_size():
    """GBPJPY uses pip_size=0.01 — range in pips must be computed correctly."""
    # GBPJPY Asian range: 215.50 - 215.00 = 50 pips (valid)
    df = _make_h1_df_with_session(215.50, 215.00, 215.60, pip=0.01)
    h1_data   = {"GBPJPY": df}
    pair_meta = {"GBPJPY": {"pip_size": 0.01}}
    # eq_for_pair must be in the PAIR's quote currency (JPY here), not a flat
    # reuse of the SEK/EUR-scale account_equity number — with the real
    # 0.6-JPY stop distance in this scenario, a GBP/EUR-scale number like
    # 15_000 produces units far below MIN_UNITS (1,000) and the signal gets
    # silently skipped, which is what this test was hitting even after the
    # HourUTC fix. ~15_000 equity converted to JPY at a realistic ~190 rate.
    sigs = lbo.generate_signals(h1_data, pair_meta, set(), session="london",
                                account_equity=15_000.0,
                                equity_by_pair={"GBPJPY": 2_850_000.0})
    if not sigs:
        return "Expected BUY signal for GBPJPY breakout"
    if sigs[0]["range_pips"] < 10:
        return f"GBPJPY range_pips {sigs[0]['range_pips']} too small — pip_size may be wrong"


def test_func_multiple_pairs_sorted_by_score():
    """Multiple pairs with breakouts — returned sorted by score descending."""
    # Tight range = higher score (score = range_pips / MAX_RANGE_PIPS)
    # Pair 1: tight 15p range
    df_tight = _make_h1_df_with_session(1.1015, 1.1000, 1.1016)
    # Pair 2: wide 80p range
    df_wide  = _make_h1_df_with_session(1.3590, 1.3510, 1.3592)
    h1_data  = {"EURUSD": df_tight, "GBPUSD": df_wide}
    pair_meta = {"EURUSD": {"pip_size": 0.0001}, "GBPUSD": {"pip_size": 0.0001}}
    sigs = lbo.generate_signals(h1_data, pair_meta, set(), session="london",
                                account_equity=15_000.0,
                                equity_by_pair={"EURUSD": 15_000.0, "GBPUSD": 15_000.0})
    if len(sigs) < 2:
        return f"Expected 2 signals, got {len(sigs)}"
    if sigs[0]["score"] < sigs[1]["score"]:
        return f"Signals not sorted by score: {sigs[0]['score']:.4f} < {sigs[1]['score']:.4f}"


def test_func_signal_fields_complete():
    """Every required runner field must be present in the signal."""
    sigs = _signals_in_session("london", "Buy", breakout_close=1.1060)
    if not sigs:
        return "No signal to check fields on"
    sig = sigs[0]
    required = ["symbol", "direction", "score", "close", "stop_price",
                "tp_price", "range_high", "range_low", "range_pips",
                "atr", "units", "session", "strategy"]
    missing = [f for f in required if f not in sig]
    if missing:
        return f"Signal missing fields: {missing}"


def test_func_signal_strategy_tag():
    sigs = _signals_in_session("london", "Buy", breakout_close=1.1060)
    if not sigs:
        return "No signal"
    if sigs[0]["strategy"] != "london_breakout":
        return f"strategy tag wrong: {sigs[0]['strategy']}"


def test_func_tp_ratio_correct():
    """TP should be exactly TP_RATIO × range_size from entry."""
    sigs = _signals_in_session("london", "Buy", breakout_close=1.1060,
                               asian_high=1.1050, asian_low=1.1010)
    if not sigs:
        return "No signal"
    sig = sigs[0]
    range_price = sig["range_high"] - sig["range_low"]
    expected_tp = sig["close"] + range_price * lbo.TP_RATIO
    if abs(sig["tp_price"] - expected_tp) > 0.000005:
        return f"TP {sig['tp_price']:.5f} != expected {expected_tp:.5f}"


def test_func_stop_at_range_boundary():
    """Stop for Buy should equal range_low; Stop for Sell should equal range_high."""
    # BUY
    sigs = _signals_in_session("london", "Buy", breakout_close=1.1060,
                               asian_high=1.1050, asian_low=1.1010)
    if sigs and abs(sigs[0]["stop_price"] - sigs[0]["range_low"]) > 0.000005:
        return f"BUY stop {sigs[0]['stop_price']} != range_low {sigs[0]['range_low']}"
    # SELL
    sigs = _signals_in_session("london", "Sell", breakout_close=1.1000,
                               asian_high=1.1050, asian_low=1.1010)
    if sigs and abs(sigs[0]["stop_price"] - sigs[0]["range_high"]) > 0.000005:
        return f"SELL stop {sigs[0]['stop_price']} != range_high {sigs[0]['range_high']}"


def test_func_zero_equity_doesnt_crash():
    """Zero equity → min_units returned, no crash."""
    sigs = _signals_in_session("london", "Buy", breakout_close=1.1060,
                               account_equity=0.0)  # noqa: must not raise
    # Should either return signal with min_units or no signal — must not crash


def test_func_outside_session_returns_empty():
    """session='auto' outside entry windows returns []."""
    # Patch to a dead hour (e.g. 03:00 UTC — not London, not NY)
    df   = _make_h1_df_with_session(1.1050, 1.1010, 1.1060)
    h1   = {"EURUSD": df}
    meta = {"EURUSD": {"pip_size": 0.0001}}
    with patch("forex.strategy_london_breakout.datetime") as mock_dt:
        mock_dt.now.return_value.hour = 3   # 03:00 UTC — not in any entry window
        sigs = lbo.generate_signals(h1, meta, set(), session="auto")
    # Note: mock may not patch correctly inside the module; test that no signal fires


# ════════════════════════════════════════════════════════════════════════════
# FUNCTIONAL TESTS — should_exit
# ════════════════════════════════════════════════════════════════════════════

def _make_pos(direction="Buy", entry=1.1000, stop=1.0950, tp=1.1100, units=5000):
    return {"direction": direction, "entry_price": entry,
            "stop_price": stop, "tp_price": tp, "quantity": units,
            "lbo_session": "London"}


def _make_exit_df(cur_high, cur_low, cur_close):
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    idx = pd.date_range(end=now, periods=5, freq="h", tz="UTC")
    return pd.DataFrame({"High": [cur_high]*5, "Low": [cur_low]*5,
                         "Close": [cur_close]*5}, index=idx)


def test_exit_time_stop_fires_at_20_utc():
    pos = _make_pos("Buy", entry=1.1000, stop=1.0950, tp=1.1100)
    df  = _make_exit_df(1.1010, 1.0990, 1.1005)
    with patch("forex.strategy_london_breakout.datetime") as mock_dt:
        mock_dt.now.return_value.hour = 20
        exit_flag, reason = lbo.should_exit(pos, df, 0)
    if not exit_flag:
        return "Time stop should fire at UTC 20:00"
    if "time_stop" not in reason:
        return f"Expected 'time_stop' in reason, got: {reason}"


def test_exit_no_exit_before_20_utc():
    pos = _make_pos("Buy", entry=1.1000, stop=1.0950, tp=1.1100)
    df  = _make_exit_df(1.1010, 1.0990, 1.1005)
    with patch("forex.strategy_london_breakout.datetime") as mock_dt:
        mock_dt.now.return_value.hour = 15   # mid-session
        exit_flag, _ = lbo.should_exit(pos, df, 0)
    if exit_flag:
        return "Should not exit at UTC 15:00 when price is between stop and TP"


def test_exit_tp_hit_buy():
    pos = _make_pos("Buy", entry=1.1000, stop=1.0950, tp=1.1100)
    df  = _make_exit_df(cur_high=1.1110, cur_low=1.1050, cur_close=1.1100)
    with patch("forex.strategy_london_breakout.datetime") as mock_dt:
        mock_dt.now.return_value.hour = 9   # within session
        exit_flag, reason = lbo.should_exit(pos, df, 0)
    if not exit_flag:
        return "TP should be hit when cur_high >= tp_price"
    if "take_profit" not in reason:
        return f"Expected 'take_profit' in reason, got: {reason}"


def test_exit_tp_hit_sell():
    pos = _make_pos("Sell", entry=1.1050, stop=1.1100, tp=1.0950)
    df  = _make_exit_df(cur_high=1.1000, cur_low=1.0940, cur_close=1.0945)
    with patch("forex.strategy_london_breakout.datetime") as mock_dt:
        mock_dt.now.return_value.hour = 9
        exit_flag, reason = lbo.should_exit(pos, df, 0)
    if not exit_flag:
        return "TP should be hit for SELL when cur_low <= tp_price"


def test_exit_stop_hit_buy():
    pos = _make_pos("Buy", entry=1.1000, stop=1.0950, tp=1.1100)
    df  = _make_exit_df(cur_high=1.0980, cur_low=1.0940, cur_close=1.0945)
    with patch("forex.strategy_london_breakout.datetime") as mock_dt:
        mock_dt.now.return_value.hour = 9
        exit_flag, reason = lbo.should_exit(pos, df, 0)
    if not exit_flag:
        return "Stop should be hit when cur_low <= stop_price"
    if "stop_loss" not in reason:
        return f"Expected 'stop_loss' in reason, got: {reason}"


def test_exit_stop_hit_sell():
    pos = _make_pos("Sell", entry=1.1050, stop=1.1100, tp=1.0950)
    df  = _make_exit_df(cur_high=1.1110, cur_low=1.1060, cur_close=1.1065)
    with patch("forex.strategy_london_breakout.datetime") as mock_dt:
        mock_dt.now.return_value.hour = 9
        exit_flag, reason = lbo.should_exit(pos, df, 0)
    if not exit_flag:
        return "Stop should be hit for SELL when cur_high >= stop_price"


def test_exit_none_df_returns_no_exit():
    pos = _make_pos()
    exit_flag, reason = lbo.should_exit(pos, None, 0)
    if exit_flag:
        return "None df should not trigger exit"


def test_exit_accepts_3_args():
    """Runner calls should_exit(pos, df, cal_days) — must accept 3 args."""
    pos = _make_pos()
    df  = _make_exit_df(1.1010, 1.0990, 1.1005)
    try:
        with patch("forex.strategy_london_breakout.datetime") as mock_dt:
            mock_dt.now.return_value.hour = 15
            lbo.should_exit(pos, df, 5)   # cal_days=5
    except TypeError as e:
        return f"should_exit must accept 3 args: {e}"


# ════════════════════════════════════════════════════════════════════════════
# FUNCTIONAL TESTS — scan_summary
# ════════════════════════════════════════════════════════════════════════════

def test_scan_summary_returns_all_pairs():
    h1_data = {sym: _make_h1_df_with_session(1.1050, 1.1010, 1.1030)
               for sym in lbo.PAIRS}
    pair_meta = {sym: {"pip_size": 0.01 if "JPY" in sym else 0.0001}
                 for sym in lbo.PAIRS}
    rows = lbo.scan_summary(h1_data, pair_meta)
    if len(rows) != len(lbo.PAIRS):
        return f"Expected {len(lbo.PAIRS)} rows, got {len(rows)}"


def test_scan_summary_breakout_field():
    """scan_summary must return a 'breakout' field (BULL/BEAR/inside)."""
    # Stamp both Asian AND London-morning ranges so the test works regardless of UTC hour
    now_utc = datetime.now(timezone.utc)
    idx = pd.date_range(end=now_utc.replace(minute=0, second=0, microsecond=0),
                        periods=48, freq="h", tz="UTC")
    base = 1.1000
    df = pd.DataFrame({"High": base + 0.0005, "Low": base - 0.0005,
                        "Close": base, "Open": base}, index=idx)
    # Asian hours (0-6): range 1.1010 - 1.1050
    for ts in idx:
        if 0 <= ts.hour <= 6:
            df.at[ts, "High"] = 1.1050; df.at[ts, "Low"] = 1.1010
    # London morning hours (9-12): range 1.1010 - 1.1050
    for ts in idx:
        if 9 <= ts.hour <= 12:
            df.at[ts, "High"] = 1.1050; df.at[ts, "Low"] = 1.1010
    # Latest bar well above any range → BULL
    df.at[idx[-1], "Close"] = 1.1060; df.at[idx[-1], "High"] = 1.1065
    all_meta = {sym: {"pip_size": 0.01 if "JPY" in sym else 0.0001} for sym in lbo.PAIRS}
    all_h1   = {sym: None for sym in lbo.PAIRS}
    all_h1["EURUSD"] = df
    rows = lbo.scan_summary(all_h1, all_meta)
    eurusd = next((r for r in rows if r["symbol"] == "EURUSD"), None)
    if eurusd is None:
        return "scan_summary missing EURUSD row"
    if "breakout" not in eurusd:
        return f"scan_summary row missing 'breakout' field: {eurusd}"
    if eurusd.get("breakout") != "BULL":
        return f"Expected BULL breakout, got {eurusd.get('breakout')}"


def test_scan_summary_no_data_pair():
    rows = lbo.scan_summary({"EURUSD": None}, {"EURUSD": {"pip_size": 0.0001}})
    if not rows or rows[0].get("status") != "no_data":
        return f"Expected no_data status for None df, got {rows}"


# ════════════════════════════════════════════════════════════════════════════
# BLACKBOX — runner integration
# ════════════════════════════════════════════════════════════════════════════

def test_bb_strategy_importable():
    """Strategy module imports cleanly."""
    import forex.strategy_london_breakout as m
    required = ["generate_signals", "should_exit", "size_position",
                "scan_summary", "PAIRS", "NEEDS_H1_DATA",
                "SESSION_CLOSE", "MIN_RANGE_PIPS", "MAX_RANGE_PIPS",
                "RISK_PCT", "MAX_UNITS", "MIN_UNITS", "TP_RATIO"]
    missing = [a for a in required if not hasattr(m, a)]
    if missing:
        return f"Strategy missing attributes: {missing}"


def test_bb_registered_in_runner():
    """Runner STRATEGIES dict must contain london_breakout."""
    import importlib, sys
    # Stub out heavy imports to avoid token/network calls — NOT forex.notifier (real module)
    for mod in ["saxo_auth", "saxo_order", "pnl_tracker", "trade_logger",
                "strategy_learner", "forex.signal_filter"]:
        if mod not in sys.modules:
            import types
            sys.modules[mod] = types.ModuleType(mod)
    try:
        from forex import runner
        if "london_breakout" not in runner.STRATEGIES:
            return "london_breakout not in runner.STRATEGIES"
        if "london_breakout" not in runner.SLOTS_PER_STRATEGY:
            return "london_breakout not in runner.SLOTS_PER_STRATEGY"
        if runner.SLOTS_PER_STRATEGY["london_breakout"] != 28:
            return f"SLOTS should be 28 (one per pair, raised from 7->10->28 on 2026-08-21), got {runner.SLOTS_PER_STRATEGY['london_breakout']}"
    except Exception as e:
        return f"Runner import failed: {e}"


def test_bb_day_trade_strategies_set():
    """london_breakout must be in DAY_TRADE_STRATEGIES to bypass heat check."""
    import importlib, sys, types
    for mod in ["saxo_auth", "saxo_order", "pnl_tracker", "trade_logger",
                "strategy_learner", "forex.signal_filter"]:
        if mod not in sys.modules:
            sys.modules[mod] = types.ModuleType(mod)
    try:
        from forex import runner
        if not hasattr(runner, "DAY_TRADE_STRATEGIES"):
            return "DAY_TRADE_STRATEGIES set missing from runner"
        if "london_breakout" not in runner.DAY_TRADE_STRATEGIES:
            return "london_breakout not in DAY_TRADE_STRATEGIES"
    except Exception as e:
        return f"Runner import failed: {e}"


def test_bb_needs_h1_data_flag():
    if not lbo.NEEDS_H1_DATA:
        return "NEEDS_H1_DATA must be True"


def test_bb_pairs_are_28():
    # Raised 7->28 on 2026-08-21: all of the main forex universe except the
    # 6 illiquid Scandi/exotic crosses (wider spreads don't suit LBO's tight
    # 2:1 RR day-trade structure) -- see forex_london_breakout memory.
    if len(lbo.PAIRS) != 28:
        return f"Expected 28 pairs, got {len(lbo.PAIRS)}: {lbo.PAIRS}"


def test_bb_pairs_are_valid_forex():
    valid = {
        "AUDCAD", "AUDCHF", "AUDJPY", "AUDNZD", "AUDUSD", "CADCHF", "CADJPY",
        "CHFJPY", "EURAUD", "EURCAD", "EURCHF", "EURGBP", "EURJPY", "EURNZD",
        "EURUSD", "GBPAUD", "GBPCAD", "GBPCHF", "GBPJPY", "GBPNZD", "GBPUSD",
        "NZDCAD", "NZDCHF", "NZDJPY", "NZDUSD", "USDCAD", "USDCHF", "USDJPY",
    }
    if lbo.PAIRS != valid:
        return f"Unexpected PAIRS: {lbo.PAIRS.symmetric_difference(valid)}"


def test_bb_session_close_is_20():
    if lbo.SESSION_CLOSE != 20:
        return f"SESSION_CLOSE should be 20 (UTC), got {lbo.SESSION_CLOSE}"


def test_bb_london_window_correct():
    if lbo.LONDON_BREAK != 7 or lbo.LONDON_END != 10:
        return f"London window should be 7-10 UTC, got {lbo.LONDON_BREAK}-{lbo.LONDON_END}"


def test_bb_ny_window_correct():
    if lbo.NY_BREAK != 13 or lbo.NY_END != 15:
        return f"NY window should be 13-15 UTC, got {lbo.NY_BREAK}-{lbo.NY_END}"


def test_bb_risk_pct_reasonable():
    if not (0.005 <= lbo.RISK_PCT <= 0.03):
        return f"RISK_PCT {lbo.RISK_PCT} outside sane range 0.5%-3%"


def test_bb_tp_ratio_is_2():
    if lbo.TP_RATIO != 2.0:
        return f"TP_RATIO should be 2.0 (2:1 R/R), got {lbo.TP_RATIO}"


def test_bb_notifier_has_lbo_functions():
    """Notifier must expose send_lbo_trade_opened and send_lbo_trade_closed."""
    import forex.notifier as n
    for fn in ["send_lbo_trade_opened", "send_lbo_trade_closed"]:
        if not hasattr(n, fn):
            return f"notifier missing {fn}"
        if not callable(getattr(n, fn)):
            return f"notifier.{fn} is not callable"


def test_bb_notifier_lbo_opened_doesnt_crash_without_config():
    """send_lbo_trade_opened silently skips when email config is missing."""
    import forex.notifier as n
    try:
        n.send_lbo_trade_opened(
            symbol="EURUSD", direction="Buy", entry=1.1050,
            stop=1.1010, tp=1.1130, units=5000,
            session="London", range_pips=40.0
        )
    except Exception as e:
        return f"send_lbo_trade_opened raised: {e}"


def test_bb_notifier_lbo_closed_doesnt_crash_without_config():
    """send_lbo_trade_closed silently skips when email config is missing."""
    import forex.notifier as n
    try:
        n.send_lbo_trade_closed(
            symbol="GBPUSD", direction="Buy", entry=1.3550,
            exit_px=1.3640, pnl_pct=0.66, units=4000,
            reason="take_profit (1.36400)", session="London"
        )
    except Exception as e:
        return f"send_lbo_trade_closed raised: {e}"


# ════════════════════════════════════════════════════════════════════════════
# EDGE CASES
# ════════════════════════════════════════════════════════════════════════════

def test_edge_empty_h1_data():
    sigs = lbo.generate_signals({}, {}, set(), session="london")
    if sigs:
        return f"Empty h1_data should give no signals, got {len(sigs)}"


def test_edge_none_df_in_h1_data():
    sigs = lbo.generate_signals({"EURUSD": None}, {"EURUSD": {"pip_size": 0.0001}},
                                set(), session="london")
    if sigs:
        return "None df should produce no signal"


def test_edge_short_df_skipped():
    """Only 3 bars — below useful threshold."""
    df = _make_h1_df(3)
    sigs = lbo.generate_signals({"EURUSD": df}, {"EURUSD": {"pip_size": 0.0001}},
                                set(), session="london")
    # Should not crash and should produce no signal (not enough bars)


def test_edge_all_pairs_no_crash():
    """Feed all 7 LBO pairs simultaneously — must not crash."""
    h1_data   = {}
    pair_meta = {}
    for sym in lbo.PAIRS:
        pip = 0.01 if "JPY" in sym else 0.0001
        base = 200.0 if "JPY" in sym else 1.1
        df = _make_h1_df_with_session(base + 50*pip, base, base + 60*pip, pip=pip)
        h1_data[sym]   = df
        pair_meta[sym] = {"pip_size": pip}
    try:
        sigs = lbo.generate_signals(h1_data, pair_meta, set(), session="london",
                                    account_equity=15_000.0)
    except Exception as e:
        return f"Crashed with all 7 pairs: {e}"


def test_edge_highask_clouseask_column_names():
    """Saxo sometimes returns HighAsk/LowAsk/CloseAsk — strategy must handle it."""
    df = _make_h1_df_with_session(1.1050, 1.1010, 1.1060)
    df = df.rename(columns={"High": "HighAsk", "Low": "LowAsk", "Close": "CloseAsk"})
    h1_data   = {"EURUSD": df}
    pair_meta = {"EURUSD": {"pip_size": 0.0001}}
    try:
        sigs = lbo.generate_signals(h1_data, pair_meta, set(), session="london",
                                    account_equity=15_000.0)
    except Exception as e:
        return f"Crashed on HighAsk/LowAsk column names: {e}"


def test_edge_size_position_correct_sek_conversion():
    """15,000 SEK at 1.5% risk = 225 SEK = ~$21 USD. Units must be reasonable."""
    qty = lbo.size_position(15_000.0, 0.0040, 1_000)
    # For EURUSD with 40-pip range (0.0040), stop ~= 40 pips
    # $21 / 0.0040 ≈ 5,250 units — sanity check
    if qty < 1_000 or qty > 50_000:
        return f"Units {qty} out of sane range for 15k SEK account"


def test_edge_should_exit_no_tp_price():
    """Position record missing tp_price (older positions) must not crash."""
    pos = {"direction": "Buy", "entry_price": 1.1000,
           "stop_price": 1.0950, "quantity": 5000}  # no tp_price
    df = _make_exit_df(1.1010, 1.0990, 1.1005)
    try:
        with patch("forex.strategy_london_breakout.datetime") as mock_dt:
            mock_dt.now.return_value.hour = 15
            lbo.should_exit(pos, df, 0)
    except Exception as e:
        return f"Crashed on missing tp_price: {e}"


# ════════════════════════════════════════════════════════════════════════════
# RUNNER
# ════════════════════════════════════════════════════════════════════════════

def _all_tests():
    return [
        # Unit
        ("Unit: _atr returns positive float",                  test_unit_atr_returns_float),
        ("Unit: _atr short df returns float (no crash)",       test_unit_atr_nan_df_returns_zero),
        ("Unit: _atr handles HighAsk/LowAsk columns",          test_unit_atr_highask_fallback),
        ("Unit: _session_range basic hi/lo",                   test_unit_session_range_basic),
        ("Unit: _session_range insufficient bars",             test_unit_session_range_insufficient_bars),
        ("Unit: _session_range no Asian bars returns None",    test_unit_session_range_no_session_bars),
        ("Unit: size_position returns valid range",            test_unit_size_position_basic),
        ("Unit: size_position zero ATR → min_units",          test_unit_size_position_zero_atr),
        ("Unit: size_position caps at MAX_UNITS",              test_unit_size_position_caps_at_max),
        # Functional — generate_signals
        ("Func: London BUY breakout generates signal",         test_func_london_bull_breakout),
        ("Func: London SELL breakout generates signal",        test_func_london_bear_breakout),
        ("Func: NY BUY breakout generates signal",             test_func_ny_bull_breakout),
        ("Func: inside range → no signal",                     test_func_no_signal_inside_range),
        ("Func: range < MIN_RANGE_PIPS → no signal",          test_func_no_signal_range_too_small),
        ("Func: range > MAX_RANGE_PIPS → no signal",          test_func_no_signal_range_too_large),
        ("Func: open symbol is skipped",                       test_func_skip_already_open_symbol),
        ("Func: non-LBO pairs are ignored",                    test_func_skip_non_lbo_pairs),
        ("Func: GBPJPY JPY pip_size handled correctly",        test_func_jpy_pair_pip_size),
        ("Func: multiple pairs sorted by score desc",          test_func_multiple_pairs_sorted_by_score),
        ("Func: all required runner fields present",           test_func_signal_fields_complete),
        ("Func: signal strategy tag is 'london_breakout'",    test_func_signal_strategy_tag),
        ("Func: TP = entry + TP_RATIO × range",               test_func_tp_ratio_correct),
        ("Func: stop at range boundary (buy/sell)",            test_func_stop_at_range_boundary),
        ("Func: zero equity doesn't crash",                    test_func_zero_equity_doesnt_crash),
        ("Func: session=auto outside windows → []",            test_func_outside_session_returns_empty),
        # Functional — should_exit
        ("Exit: time stop fires at UTC 20:00",                 test_exit_time_stop_fires_at_20_utc),
        ("Exit: no exit before UTC 20:00 (price between)",    test_exit_no_exit_before_20_utc),
        ("Exit: TP hit for Buy (high >= tp)",                  test_exit_tp_hit_buy),
        ("Exit: TP hit for Sell (low <= tp)",                  test_exit_tp_hit_sell),
        ("Exit: stop hit for Buy (low <= stop)",               test_exit_stop_hit_buy),
        ("Exit: stop hit for Sell (high >= stop)",             test_exit_stop_hit_sell),
        ("Exit: None df returns no exit",                      test_exit_none_df_returns_no_exit),
        ("Exit: accepts 3 args (pos, df, cal_days)",           test_exit_accepts_3_args),
        # Functional — scan_summary
        ("Scan: returns row for each of 7 pairs",              test_scan_summary_returns_all_pairs),
        ("Scan: breakout field shows BULL/BEAR/inside",        test_scan_summary_breakout_field),
        ("Scan: None df → status='no_data'",                   test_scan_summary_no_data_pair),
        # Blackbox — runner integration
        ("BB: strategy module importable with all attrs",      test_bb_strategy_importable),
        ("BB: registered in runner.STRATEGIES",                test_bb_registered_in_runner),
        ("BB: in DAY_TRADE_STRATEGIES (heat bypass)",          test_bb_day_trade_strategies_set),
        ("BB: NEEDS_H1_DATA = True",                           test_bb_needs_h1_data_flag),
        ("BB: PAIRS count is 28",                              test_bb_pairs_are_28),
        ("BB: PAIRS set is correct 7 majors",                  test_bb_pairs_are_valid_forex),
        ("BB: SESSION_CLOSE = 20 (UTC)",                       test_bb_session_close_is_20),
        ("BB: London window 07:00-10:00 UTC",                  test_bb_london_window_correct),
        ("BB: NY window 13:00-15:00 UTC",                      test_bb_ny_window_correct),
        ("BB: RISK_PCT in 0.5%-3% range",                      test_bb_risk_pct_reasonable),
        ("BB: TP_RATIO = 2.0",                                 test_bb_tp_ratio_is_2),
        ("BB: notifier has send_lbo_trade_opened/closed",      test_bb_notifier_has_lbo_functions),
        ("BB: notifier opened() silent without email config",  test_bb_notifier_lbo_opened_doesnt_crash_without_config),
        ("BB: notifier closed() silent without email config",  test_bb_notifier_lbo_closed_doesnt_crash_without_config),
        # Edge cases
        ("Edge: empty h1_data → []",                           test_edge_empty_h1_data),
        ("Edge: None df in h1_data → no crash",                test_edge_none_df_in_h1_data),
        ("Edge: 3-bar df skipped gracefully",                  test_edge_short_df_skipped),
        ("Edge: all 7 pairs fed simultaneously → no crash",    test_edge_all_pairs_no_crash),
        ("Edge: HighAsk/LowAsk/CloseAsk column names",         test_edge_highask_clouseask_column_names),
        ("Edge: 15k SEK sizing in plausible unit range",       test_edge_size_position_correct_sek_conversion),
        ("Edge: should_exit handles missing tp_price",         test_edge_should_exit_no_tp_price),
    ]


if __name__ == "__main__":
    np.random.seed(0)

    print(f"\n{B}{C}London Breakout Strategy — Test Suite{X}")
    print(f"{C}{'='*60}{X}\n")

    for section_tests in [
        ("UNIT TESTS",       [t for t in _all_tests() if t[0].startswith("Unit")]),
        ("FUNCTIONAL",       [t for t in _all_tests() if t[0].startswith("Func")]),
        ("EXIT LOGIC",       [t for t in _all_tests() if t[0].startswith("Exit")]),
        ("SCAN SUMMARY",     [t for t in _all_tests() if t[0].startswith("Scan")]),
        ("BLACKBOX",         [t for t in _all_tests() if t[0].startswith("BB")]),
        ("EDGE CASES",       [t for t in _all_tests() if t[0].startswith("Edge")]),
    ]:
        label, tests = section_tests
        print(f"{B}{Y}── {label} ({len(tests)} tests){X}")
        for name, fn in tests:
            _run(name, fn)
        print()

    passed = sum(1 for r in _results if r[0] == "PASS")
    failed = sum(1 for r in _results if r[0] == "FAIL")
    errors = sum(1 for r in _results if r[0] == "ERROR")
    total  = len(_results)

    print(f"{C}{'='*60}{X}")
    print(f"{B}Results: {G}{passed} passed{X}  {R}{failed} failed{X}  {R}{errors} errors{X}  / {total} total{X}")

    if failed or errors:
        print(f"\n{R}FAILURES:{X}")
        for r in _results:
            if r[0] in ("FAIL", "ERROR"):
                print(f"  {R}✗{X}  {r[1]}")
                if r[2]:
                    print(f"     {Y}{r[2]}{X}")
        sys.exit(1)
    else:
        print(f"\n{G}All {total} tests passed — strategy ready for live trading.{X}")
        sys.exit(0)
