"""
test_gap_functional.py
----------------------
Functional tests for the gap strategy — both weekly and session gaps.

Tests every public function in forex/strategy_gap.py and the two
gap-related helpers in forex/runner.py (_detect_gap_session).

Run:  python -m pytest test_gap_functional.py -v
"""

import math
import sys
import types
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pandas as pd
import pytest

import forex.strategy_gap as gap


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _daily_df(friday_close: float, rows: int = 10) -> pd.DataFrame:
    """Minimal daily bar DataFrame with friday_close as the last bar."""
    closes = [friday_close * (1 + i * 0.001) for i in range(rows)]
    closes[-1] = friday_close
    return pd.DataFrame({
        "Open":  closes,
        "High":  [c * 1.001 for c in closes],
        "Low":   [c * 0.999 for c in closes],
        "Close": closes,
    })


def _h1_df(ref_hour: int, ref_close: float, n_bars: int = 24) -> pd.DataFrame:
    """H1 DataFrame with one bar at ref_hour having Close = ref_close."""
    rows = []
    for i in range(n_bars):
        h = (ref_hour - n_bars + 1 + i) % 24
        c = ref_close if h == ref_hour else ref_close * (1 + (i - n_bars // 2) * 0.0002)
        rows.append({"Open": c, "High": c * 1.0005, "Low": c * 0.9995,
                     "Close": c, "HourUTC": h})
    return pd.DataFrame(rows)


def _pos(direction="Buy", entry=1.2000, stop=1.1900, target=1.2100,
         gap_type="weekly", entry_datetime=None, days_ago=0):
    p = {
        "direction":   direction,
        "entry_price": entry,
        "stop_price":  stop,
        "gap_target":  target,
        "gap_type":    gap_type,
    }
    if entry_datetime:
        p["entry_datetime"] = entry_datetime
    elif gap_type in gap.SESSION_GAPS:
        # Default: entered now (not yet expired)
        p["entry_datetime"] = datetime.now().isoformat()
    return p


def _bar(high: float, low: float, close: float = None) -> pd.DataFrame:
    c = close or (high + low) / 2
    return pd.DataFrame([{"Open": c, "High": high, "Low": low, "Close": c}])


# ─────────────────────────────────────────────────────────────────────────────
# 1. Weekly gap — generate_signals
# ─────────────────────────────────────────────────────────────────────────────

class TestWeeklyGenerateSignals:

    def test_gap_up_produces_sell(self):
        """Sunday open above Friday close → SHORT to fade back down."""
        df = _daily_df(1.2000)
        sigs = gap.generate_signals({"EURUSD": df}, live_prices={"EURUSD": 1.2030})
        assert len(sigs) == 1
        s = sigs[0]
        assert s["direction"] == "Sell"
        assert s["gap_target"] == pytest.approx(1.2000)   # fill = Friday close
        assert s["gap_type"] == "weekly"

    def test_gap_down_produces_buy(self):
        """Sunday open below Friday close → LONG to fade back up."""
        df = _daily_df(1.2000)
        sigs = gap.generate_signals({"EURUSD": df}, live_prices={"EURUSD": 1.1970})
        assert len(sigs) == 1
        assert sigs[0]["direction"] == "Buy"
        assert sigs[0]["gap_target"] == pytest.approx(1.2000)

    def test_stop_price_sell(self):
        """Sell stop = sunday_open + 1.5 × gap_size."""
        friday = 1.2000
        sunday = 1.2030      # gap = 0.003
        df = _daily_df(friday)
        sigs = gap.generate_signals({"EURUSD": df}, live_prices={"EURUSD": sunday})
        s = sigs[0]
        expected_stop = sunday + 1.5 * abs(sunday - friday)
        assert s["stop_price"] == pytest.approx(expected_stop)

    def test_stop_price_buy(self):
        """Buy stop = sunday_open - 1.5 × gap_size."""
        friday = 1.2000
        sunday = 1.1960      # gap = -0.004
        df = _daily_df(friday)
        sigs = gap.generate_signals({"EURUSD": df}, live_prices={"EURUSD": sunday})
        s = sigs[0]
        expected_stop = sunday - 1.5 * abs(sunday - friday)
        assert s["stop_price"] == pytest.approx(expected_stop)

    def test_gap_too_small_filtered(self):
        """Gap < 0.10% is spread noise — no signal."""
        df = _daily_df(1.2000)
        # 0.05% gap
        sigs = gap.generate_signals({"EURUSD": df}, live_prices={"EURUSD": 1.2006})
        assert sigs == []

    def test_gap_too_large_filtered(self):
        """Gap > 2.00% is extreme event risk — no signal."""
        df = _daily_df(1.2000)
        # 2.5% gap
        sigs = gap.generate_signals({"EURUSD": df}, live_prices={"EURUSD": 1.2300})
        assert sigs == []

    def test_exactly_at_min_threshold_passes(self):
        """Gap at 0.11% (just above 0.10% min) should pass.
        Note: exactly 0.10% can fail due to float precision (0.0999...98%).
        """
        friday = 1.0000
        sunday = friday + 0.0011   # 0.11% — clear of float boundary
        df = _daily_df(friday)
        sigs = gap.generate_signals({"EURUSD": df}, live_prices={"EURUSD": sunday})
        assert len(sigs) == 1

    def test_no_live_price_skipped(self):
        df = _daily_df(1.2000)
        sigs = gap.generate_signals({"EURUSD": df}, live_prices={})
        assert sigs == []

    def test_live_price_zero_skipped(self):
        df = _daily_df(1.2000)
        sigs = gap.generate_signals({"EURUSD": df}, live_prices={"EURUSD": 0})
        assert sigs == []

    def test_none_df_skipped(self):
        sigs = gap.generate_signals({"EURUSD": None}, live_prices={"EURUSD": 1.2030})
        assert sigs == []

    def test_short_history_skipped(self):
        """DataFrame shorter than MIN_BARS — skip."""
        df = pd.DataFrame([{"Open": 1.2, "High": 1.2, "Low": 1.2, "Close": 1.2}])
        sigs = gap.generate_signals({"EURUSD": df}, live_prices={"EURUSD": 1.2020})
        assert sigs == []

    def test_open_symbol_skipped(self):
        """Already holding this pair — no new entry."""
        df = _daily_df(1.2000)
        sigs = gap.generate_signals({"EURUSD": df},
                                    open_symbols={"EURUSD"},
                                    live_prices={"EURUSD": 1.2030})
        assert sigs == []

    def test_sorted_by_gap_pct_descending(self):
        """Largest gap comes first."""
        dfs = {
            "EURUSD": _daily_df(1.2000),
            "GBPUSD": _daily_df(1.3000),
            "USDJPY": _daily_df(150.00),
        }
        live = {
            "EURUSD": 1.2030,    # 0.25%
            "GBPUSD": 1.3200,    # 1.54%
            "USDJPY": 150.30,    # 0.20%
        }
        sigs = gap.generate_signals(dfs, live_prices=live)
        assert len(sigs) == 3
        pcts = [s["gap_pct"] for s in sigs]
        assert pcts == sorted(pcts, reverse=True)

    def test_signal_fields_complete(self):
        """All required signal fields present."""
        df = _daily_df(1.2000)
        sigs = gap.generate_signals({"EURUSD": df}, live_prices={"EURUSD": 1.2030})
        s = sigs[0]
        for field in ["symbol", "direction", "score", "atr", "close",
                      "stop_price", "gap_target", "gap_pct", "gap_size",
                      "friday_close", "sunday_open", "gap_type"]:
            assert field in s, f"missing field: {field}"

    def test_multiple_pairs_independent(self):
        """Two pairs with gaps both produce signals independently."""
        dfs = {"EURUSD": _daily_df(1.2000), "GBPUSD": _daily_df(1.3000)}
        live = {"EURUSD": 1.2020, "GBPUSD": 1.3030}
        sigs = gap.generate_signals(dfs, live_prices=live)
        assert len(sigs) == 2
        syms = {s["symbol"] for s in sigs}
        assert syms == {"EURUSD", "GBPUSD"}

    def test_gap_type_weekly_in_signal(self):
        df = _daily_df(1.2000)
        sigs = gap.generate_signals({"EURUSD": df}, live_prices={"EURUSD": 1.2030})
        assert sigs[0]["gap_type"] == "weekly"


# ─────────────────────────────────────────────────────────────────────────────
# 2. _find_ref_bar_close
# ─────────────────────────────────────────────────────────────────────────────

class TestFindRefBarClose:

    def test_finds_correct_hour_with_column(self):
        df = _h1_df(ref_hour=6, ref_close=1.2000)
        result = gap._find_ref_bar_close(df, 6)
        assert result == pytest.approx(1.2000)

    def test_returns_none_when_hour_missing(self):
        # Build a sparse df containing only hours 5, 6, 7 — hour 15 genuinely absent
        rows = [{"Open": 1.2, "High": 1.2, "Low": 1.2, "Close": 1.2, "HourUTC": h}
                for h in (5, 6, 7)]
        df = pd.DataFrame(rows)
        result = gap._find_ref_bar_close(df, 15)   # hour 15 not in df
        assert result is None

    def test_returns_last_occurrence_when_multiple(self):
        """If two bars with same hour exist, return the last one."""
        rows = [
            {"Open": 1.2, "High": 1.2, "Low": 1.2, "Close": 1.2000, "HourUTC": 6},
            {"Open": 1.3, "High": 1.3, "Low": 1.3, "Close": 1.3000, "HourUTC": 6},
        ]
        df = pd.DataFrame(rows)
        result = gap._find_ref_bar_close(df, 6)
        assert result == pytest.approx(1.3000)

    def test_fallback_without_hourutc_column(self):
        """DataFrame without HourUTC column — still returns None (no match)."""
        df = pd.DataFrame([{"Open": 1.2, "High": 1.2, "Low": 1.2, "Close": 1.2}])
        result = gap._find_ref_bar_close(df, 6)
        assert result is None

    def test_hour_23_wraparound(self):
        """ref_hour=23 (Tokyo) correctly retrieved."""
        df = _h1_df(ref_hour=23, ref_close=150.50)
        result = gap._find_ref_bar_close(df, 23)
        assert result == pytest.approx(150.50)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Session gap — generate_session_signals
# ─────────────────────────────────────────────────────────────────────────────

class TestSessionGenerateSignals:

    def _session_market(self, sym: str, ref_close: float,
                        session: str = "london") -> dict:
        ref_hour = gap.SESSION_GAPS[session]["ref_hour_utc"]
        return {sym: _h1_df(ref_hour, ref_close)}

    def _live(self, sym: str, price: float) -> dict:
        return {sym: price}

    # ── London ──────────────────────────────────────────────────────────────

    def test_london_gap_up_sell(self):
        md = self._session_market("EURUSD", 1.2000)
        lp = self._live("EURUSD", 1.2015)   # 0.125% gap up
        sigs = gap.generate_session_signals("london", md, live_prices=lp)
        assert len(sigs) == 1
        assert sigs[0]["direction"] == "Sell"
        assert sigs[0]["gap_type"] == "london"

    def test_london_gap_down_buy(self):
        md = self._session_market("EURUSD", 1.2000)
        lp = self._live("EURUSD", 1.1993)   # 0.058% gap down
        sigs = gap.generate_session_signals("london", md, live_prices=lp)
        assert len(sigs) == 1
        assert sigs[0]["direction"] == "Buy"

    def test_london_gap_too_small(self):
        """Gap < 0.05% for London — filtered."""
        md = self._session_market("EURUSD", 1.2000)
        lp = self._live("EURUSD", 1.20005)  # 0.004% — below 0.05%
        sigs = gap.generate_session_signals("london", md, live_prices=lp)
        assert sigs == []

    def test_london_gap_too_large(self):
        """Gap > 0.40% for London — filtered (extreme move)."""
        md = self._session_market("EURUSD", 1.2000)
        lp = self._live("EURUSD", 1.2060)   # 0.50% — above 0.40%
        sigs = gap.generate_session_signals("london", md, live_prices=lp)
        assert sigs == []

    def test_london_stop_price_sell(self):
        """Sell stop = session_open + 2.0 × gap_size."""
        ref = 1.2000
        so  = 1.2015
        md  = self._session_market("EURUSD", ref)
        lp  = self._live("EURUSD", so)
        sigs = gap.generate_session_signals("london", md, live_prices=lp)
        s = sigs[0]
        expected = so + 2.0 * abs(so - ref)
        assert s["stop_price"] == pytest.approx(expected)

    def test_london_risk_pct_override_in_signal(self):
        md = self._session_market("EURUSD", 1.2000)
        lp = self._live("EURUSD", 1.2015)
        sigs = gap.generate_session_signals("london", md, live_prices=lp)
        assert sigs[0]["risk_pct_override"] == pytest.approx(0.005)

    # ── New York ─────────────────────────────────────────────────────────────

    def test_newyork_gap_up_sell(self):
        ref_hour = gap.SESSION_GAPS["newyork"]["ref_hour_utc"]   # 11
        md = {"GBPUSD": _h1_df(ref_hour, 1.3000)}
        lp = {"GBPUSD": 1.3015}   # 0.115% gap up
        sigs = gap.generate_session_signals("newyork", md, live_prices=lp)
        assert len(sigs) == 1
        assert sigs[0]["direction"] == "Sell"
        assert sigs[0]["gap_type"] == "newyork"

    def test_newyork_gap_too_large(self):
        ref_hour = gap.SESSION_GAPS["newyork"]["ref_hour_utc"]
        md = {"GBPUSD": _h1_df(ref_hour, 1.3000)}
        lp = {"GBPUSD": 1.3060}   # 0.46% — above 0.40%
        sigs = gap.generate_session_signals("newyork", md, live_prices=lp)
        assert sigs == []

    # ── Tokyo ────────────────────────────────────────────────────────────────

    def test_tokyo_gap_down_buy(self):
        ref_hour = gap.SESSION_GAPS["tokyo"]["ref_hour_utc"]   # 23
        md = {"USDJPY": _h1_df(ref_hour, 150.00)}
        lp = {"USDJPY": 149.94}   # −0.04% gap down
        sigs = gap.generate_session_signals("tokyo", md, live_prices=lp)
        assert len(sigs) == 1
        assert sigs[0]["direction"] == "Buy"
        assert sigs[0]["gap_type"] == "tokyo"

    def test_tokyo_min_gap_is_004_pct(self):
        """Tokyo min_gap_pct = 0.04%; 0.03% should be filtered."""
        ref_hour = gap.SESSION_GAPS["tokyo"]["ref_hour_utc"]
        ref = 150.00
        # 0.03% = 0.045 price units
        md = {"USDJPY": _h1_df(ref_hour, ref)}
        lp = {"USDJPY": ref - 0.03 * ref / 100}
        sigs = gap.generate_session_signals("tokyo", md, live_prices=lp)
        assert sigs == []

    # ── Common ───────────────────────────────────────────────────────────────

    def test_invalid_session_returns_empty(self):
        sigs = gap.generate_session_signals("sydney", {}, live_prices={})
        assert sigs == []

    def test_open_symbol_skipped(self):
        md = self._session_market("EURUSD", 1.2000)
        lp = self._live("EURUSD", 1.2015)
        sigs = gap.generate_session_signals("london", md,
                                            open_symbols={"EURUSD"},
                                            live_prices=lp)
        assert sigs == []

    def test_no_live_price_skipped(self):
        md = self._session_market("EURUSD", 1.2000)
        sigs = gap.generate_session_signals("london", md, live_prices={})
        assert sigs == []

    def test_fallback_to_last_bar_when_ref_hour_not_found(self):
        """If ref_hour bar is missing, uses last bar close as fallback — still works."""
        # Build df without the ref_hour bar
        rows = [{"Open": 1.2, "High": 1.2, "Low": 1.2,
                 "Close": 1.2000, "HourUTC": h} for h in range(4)]
        df = pd.DataFrame(rows)  # hours 0-3 only, no hour 6 (London ref)
        md = {"EURUSD": df}
        lp = {"EURUSD": 1.2015}
        sigs = gap.generate_session_signals("london", md, live_prices=lp)
        # Should still produce a signal using the fallback close
        assert len(sigs) == 1

    def test_sorted_largest_gap_first(self):
        ref_hour = gap.SESSION_GAPS["london"]["ref_hour_utc"]
        md = {
            "EURUSD": _h1_df(ref_hour, 1.2000),
            "GBPUSD": _h1_df(ref_hour, 1.3000),
        }
        lp = {
            "EURUSD": 1.2010,   # 0.083%
            "GBPUSD": 1.3020,   # 0.154%  ← larger
        }
        sigs = gap.generate_session_signals("london", md, live_prices=lp)
        assert len(sigs) == 2
        assert sigs[0]["symbol"] == "GBPUSD"

    def test_ref_close_stored_in_signal(self):
        md = self._session_market("EURUSD", 1.2000)
        lp = self._live("EURUSD", 1.2015)
        sigs = gap.generate_session_signals("london", md, live_prices=lp)
        assert "ref_close" in sigs[0]
        assert sigs[0]["ref_close"] == pytest.approx(1.2000)


# ─────────────────────────────────────────────────────────────────────────────
# 4. should_exit — weekly (day-based)
# ─────────────────────────────────────────────────────────────────────────────

class TestShouldExitWeekly:

    def test_gap_filled_buy(self):
        """Long position: high >= gap_target → gap_filled exit."""
        pos = _pos("Buy", entry=1.1970, stop=1.1925, target=1.2000)
        df  = _bar(high=1.2001, low=1.1980)
        ok, reason = gap.should_exit(pos, df, 1)
        assert ok
        assert "gap_filled" in reason

    def test_gap_filled_sell(self):
        """Short position: low <= gap_target → gap_filled exit."""
        pos = _pos("Sell", entry=1.2030, stop=1.2075, target=1.2000)
        df  = _bar(high=1.2025, low=1.1999)
        ok, reason = gap.should_exit(pos, df, 1)
        assert ok
        assert "gap_filled" in reason

    def test_hard_stop_buy(self):
        """Long position: low <= stop_price → hard_stop exit."""
        pos = _pos("Buy", entry=1.1970, stop=1.1940, target=1.2000)
        df  = _bar(high=1.1960, low=1.1938)
        ok, reason = gap.should_exit(pos, df, 1)
        assert ok
        assert "hard_stop" in reason

    def test_hard_stop_sell(self):
        """Short position: high >= stop_price → hard_stop exit."""
        pos = _pos("Sell", entry=1.2030, stop=1.2060, target=1.2000)
        df  = _bar(high=1.2061, low=1.2025)
        ok, reason = gap.should_exit(pos, df, 1)
        assert ok
        assert "hard_stop" in reason

    def test_time_stop_7_days(self):
        """Weekly: exit when calendar_days_held >= 7."""
        pos = _pos("Buy", entry=1.1970, stop=1.1900, target=1.2000)
        df  = _bar(high=1.1990, low=1.1975)   # not filled, not stopped
        ok, reason = gap.should_exit(pos, df, 7)
        assert ok
        assert "time_stop" in reason

    def test_no_exit_before_7_days(self):
        pos = _pos("Buy", entry=1.1970, stop=1.1900, target=1.2000)
        df  = _bar(high=1.1990, low=1.1975)
        ok, _ = gap.should_exit(pos, df, 6)
        assert not ok

    def test_none_df_no_exit(self):
        pos = _pos()
        ok, _ = gap.should_exit(pos, None, 3)
        assert not ok

    def test_empty_df_no_exit(self):
        pos = _pos()
        ok, _ = gap.should_exit(pos, pd.DataFrame(), 3)
        assert not ok

    def test_not_yet_stopped_or_filled(self):
        """Price in the middle — no exit triggered."""
        pos = _pos("Buy", entry=1.1970, stop=1.1900, target=1.2000)
        df  = _bar(high=1.1990, low=1.1960)
        ok, _ = gap.should_exit(pos, df, 3)
        assert not ok

    def test_gap_target_uses_entry_if_missing(self):
        """If gap_target absent, falls back to entry_price (won't trigger fill)."""
        pos = {"direction": "Buy", "entry_price": 1.2, "stop_price": 1.19, "gap_type": "weekly"}
        df  = _bar(high=1.21, low=1.195)
        # entry_price becomes target; high=1.21 > entry=1.2 → gap_filled
        ok, reason = gap.should_exit(pos, df, 1)
        assert ok


# ─────────────────────────────────────────────────────────────────────────────
# 5. should_exit — session gaps (hour-based time stop)
# ─────────────────────────────────────────────────────────────────────────────

class TestShouldExitSession:

    def test_london_time_stop_after_8h(self):
        entry_dt = (datetime.now() - timedelta(hours=8, minutes=5)).isoformat()
        pos = _pos("Buy", entry=1.1970, stop=1.1900, target=1.2000,
                   gap_type="london", entry_datetime=entry_dt)
        df  = _bar(high=1.1990, low=1.1975)   # not filled, not stopped
        ok, reason = gap.should_exit(pos, df, 0)
        assert ok
        assert "time_stop" in reason
        assert "london" in reason

    def test_london_no_exit_before_8h(self):
        entry_dt = (datetime.now() - timedelta(hours=7)).isoformat()
        pos = _pos("Buy", entry=1.1970, stop=1.1900, target=1.2000,
                   gap_type="london", entry_datetime=entry_dt)
        df  = _bar(high=1.1990, low=1.1975)
        ok, _ = gap.should_exit(pos, df, 0)
        assert not ok

    def test_newyork_time_stop_after_6h(self):
        entry_dt = (datetime.now() - timedelta(hours=6, minutes=1)).isoformat()
        pos = _pos("Sell", entry=1.3050, stop=1.3100, target=1.3000,
                   gap_type="newyork", entry_datetime=entry_dt)
        df  = _bar(high=1.3040, low=1.3010)
        ok, reason = gap.should_exit(pos, df, 0)
        assert ok
        assert "newyork" in reason

    def test_tokyo_time_stop_after_7h(self):
        entry_dt = (datetime.now() - timedelta(hours=7, minutes=30)).isoformat()
        pos = _pos("Buy", entry=149.80, stop=149.50, target=150.00,
                   gap_type="tokyo", entry_datetime=entry_dt)
        df  = _bar(high=149.95, low=149.82)
        ok, reason = gap.should_exit(pos, df, 0)
        assert ok
        assert "tokyo" in reason

    def test_session_gap_filled_exits_before_time_stop(self):
        """Gap fill takes priority over time stop."""
        entry_dt = (datetime.now() - timedelta(hours=2)).isoformat()
        pos = _pos("Buy", entry=1.1970, stop=1.1900, target=1.2000,
                   gap_type="london", entry_datetime=entry_dt)
        df  = _bar(high=1.2002, low=1.1975)
        ok, reason = gap.should_exit(pos, df, 0)
        assert ok
        assert "gap_filled" in reason

    def test_session_hard_stop_exits_before_time_stop(self):
        entry_dt = (datetime.now() - timedelta(hours=1)).isoformat()
        pos = _pos("Buy", entry=1.1970, stop=1.1940, target=1.2000,
                   gap_type="newyork", entry_datetime=entry_dt)
        df  = _bar(high=1.1960, low=1.1938)
        ok, reason = gap.should_exit(pos, df, 0)
        assert ok
        assert "hard_stop" in reason

    def test_invalid_entry_datetime_falls_through(self):
        """Bad timestamp string → time stop skipped, check gap/stop instead."""
        pos = _pos("Buy", entry=1.1970, stop=1.1900, target=1.2000,
                   gap_type="london", entry_datetime="not-a-date")
        df  = _bar(high=1.1990, low=1.1975)
        ok, _ = gap.should_exit(pos, df, 0)
        assert not ok   # no hard_stop, no gap_fill, bad datetime → no exit


# ─────────────────────────────────────────────────────────────────────────────
# 6. size_position
# ─────────────────────────────────────────────────────────────────────────────

class TestSizePosition:

    def test_basic_sizing(self):
        """units = floor(equity × 1% / (1.5 × gap_size)) rounded to 1000."""
        equity   = 100_000
        gap_size = 0.003   # 30 pips on EURUSD
        # risk = 1000, stop = 0.0045 → 222_222 → floor to 222_000
        qty = gap.size_position(equity, gap_size)
        assert qty % 1_000 == 0
        assert qty > 0

    def test_risk_pct_override_smaller(self):
        """0.5% risk produces half the units of 1%."""
        equity   = 100_000
        gap_size = 0.003
        qty_full = gap.size_position(equity, gap_size, risk_pct=0.01)
        qty_half = gap.size_position(equity, gap_size, risk_pct=0.005)
        assert qty_half < qty_full

    def test_minimum_lot_enforced(self):
        """Even with tiny equity, returns at least 1 lot (1000 units)."""
        qty = gap.size_position(10, 0.003)
        assert qty >= 1_000

    def test_zero_gap_size_returns_minimum(self):
        """Zero gap size → stop_distance = 0 → return minimum lot."""
        qty = gap.size_position(100_000, 0)
        assert qty == 1_000

    def test_negative_gap_size_returns_minimum(self):
        qty = gap.size_position(100_000, -0.001)
        assert qty == 1_000

    def test_multiple_of_lot_round(self):
        qty = gap.size_position(250_000, 0.002)
        assert qty % gap.LOT_ROUND == 0

    def test_larger_equity_more_units(self):
        q1 = gap.size_position(50_000, 0.003)
        q2 = gap.size_position(100_000, 0.003)
        assert q2 > q1


# ─────────────────────────────────────────────────────────────────────────────
# 7. _detect_gap_session (runner.py helper — tested via mock patch)
# ─────────────────────────────────────────────────────────────────────────────

def _utc(isoweekday: int, hour: int, minute: int = 0) -> datetime:
    """Build a UTC datetime with the given isoweekday and time."""
    # Start from a known Monday and shift
    base = datetime(2026, 8, 17, hour, minute, tzinfo=timezone.utc)  # 2026-08-17 = Monday
    delta_days = isoweekday - 1   # Mon=1 → +0, Sun=7 → +6
    return base + timedelta(days=delta_days)


class TestDetectGapSession:

    def _detect(self, dt: datetime) -> str | None:
        import forex.runner as runner
        with patch("forex.runner.datetime") as mock_dt:
            mock_dt.now.return_value = dt
            mock_dt.fromisoformat = datetime.fromisoformat
            return runner._detect_gap_session()

    # ── Weekly ───────────────────────────────────────────────────────────────

    def test_sunday_22utc_weekly(self):
        """FX reopens Sunday 22:00 UTC → weekly."""
        assert self._detect(_utc(7, 22)) == "weekly"

    def test_sunday_23utc_weekly(self):
        assert self._detect(_utc(7, 23)) == "weekly"

    def test_monday_01utc_weekly(self):
        """Monday 01:20 UTC → still in weekly window."""
        assert self._detect(_utc(1, 1, 20)) == "weekly"

    def test_monday_05utc_weekly(self):
        """Monday 05:59 UTC → still weekly."""
        assert self._detect(_utc(1, 5, 59)) == "weekly"

    def test_monday_06utc_not_weekly(self):
        """Monday 06:00 UTC → weekly window closed, no gap session."""
        result = self._detect(_utc(1, 6, 0))
        assert result != "weekly"

    # ── London ───────────────────────────────────────────────────────────────

    def test_monday_07utc_london(self):
        assert self._detect(_utc(1, 7, 0)) == "london"

    def test_wednesday_07utc_london(self):
        assert self._detect(_utc(3, 7, 30)) == "london"

    def test_friday_08utc_london(self):
        assert self._detect(_utc(5, 8, 15)) == "london"

    def test_london_window_closed_at_0830(self):
        result = self._detect(_utc(3, 8, 30))
        assert result != "london"

    def test_saturday_07utc_not_london(self):
        """Weekend — no session gap."""
        assert self._detect(_utc(6, 7)) is None

    # ── New York ─────────────────────────────────────────────────────────────

    def test_tuesday_12utc_newyork(self):
        assert self._detect(_utc(2, 12, 0)) == "newyork"

    def test_thursday_13utc_newyork(self):
        assert self._detect(_utc(4, 13, 15)) == "newyork"

    def test_newyork_window_closed_at_1330(self):
        result = self._detect(_utc(3, 13, 30))
        assert result != "newyork"

    # ── Tokyo ────────────────────────────────────────────────────────────────

    def test_tuesday_00utc_tokyo(self):
        assert self._detect(_utc(2, 0, 0)) == "tokyo"

    def test_thursday_01utc_tokyo(self):
        assert self._detect(_utc(4, 1, 15)) == "tokyo"

    def test_monday_01utc_not_tokyo(self):
        """Monday 01:00 UTC → weekly (not tokyo, covered by weekly window)."""
        assert self._detect(_utc(1, 1)) == "weekly"

    def test_tokyo_window_closed_at_0130(self):
        result = self._detect(_utc(2, 1, 30))
        assert result != "tokyo"

    # ── Dead zones ───────────────────────────────────────────────────────────

    def test_midday_wednesday_none(self):
        """Wednesday 10:00 UTC — no gap session."""
        assert self._detect(_utc(3, 10)) is None

    def test_saturday_none(self):
        """Market closed Saturday."""
        assert self._detect(_utc(6, 12)) is None

    def test_sunday_before_22utc_none(self):
        """Sunday 20:00 UTC — market not yet open."""
        assert self._detect(_utc(7, 20)) is None


# ─────────────────────────────────────────────────────────────────────────────
# 8. scan_summary
# ─────────────────────────────────────────────────────────────────────────────

class TestScanSummary:

    def test_gap_up_signal(self):
        df = _daily_df(1.2000)
        rows = gap.scan_summary({"EURUSD": df}, live_prices={"EURUSD": 1.2030})
        assert any("GAP UP" in r["signal"] for r in rows if r["symbol"] == "EURUSD")

    def test_gap_down_signal(self):
        df = _daily_df(1.2000)
        rows = gap.scan_summary({"EURUSD": df}, live_prices={"EURUSD": 1.1965})
        assert any("GAP DOWN" in r["signal"] for r in rows if r["symbol"] == "EURUSD")

    def test_no_gap_signal(self):
        df = _daily_df(1.2000)
        rows = gap.scan_summary({"EURUSD": df}, live_prices={"EURUSD": 1.2001})
        assert any(r["signal"] == "no gap" for r in rows if r["symbol"] == "EURUSD")

    def test_no_live_price_shows_proxy(self):
        df = _daily_df(1.2000)
        rows = gap.scan_summary({"EURUSD": df})
        assert any(r["symbol"] == "EURUSD" for r in rows)

    def test_no_data_returns_status(self):
        rows = gap.scan_summary({"EURUSD": None})
        assert rows[0]["status"] == "no_data"


# ─────────────────────────────────────────────────────────────────────────────
# 9. SESSION_GAPS config integrity
# ─────────────────────────────────────────────────────────────────────────────

class TestSessionGapsConfig:

    def test_all_required_keys_present(self):
        required = {"open_hour_utc", "ref_hour_utc", "min_gap_pct",
                    "max_gap_pct", "stop_mult", "time_stop_hours", "risk_pct"}
        for name, cfg in gap.SESSION_GAPS.items():
            missing = required - set(cfg.keys())
            assert not missing, f"{name} missing keys: {missing}"

    def test_min_gap_less_than_max_gap(self):
        for name, cfg in gap.SESSION_GAPS.items():
            assert cfg["min_gap_pct"] < cfg["max_gap_pct"], name

    def test_stop_mult_positive(self):
        for name, cfg in gap.SESSION_GAPS.items():
            assert cfg["stop_mult"] > 0, name

    def test_time_stop_reasonable(self):
        for name, cfg in gap.SESSION_GAPS.items():
            assert 1 <= cfg["time_stop_hours"] <= 24, name

    def test_risk_pct_half_of_weekly(self):
        """Session gaps use 0.5% risk — half of weekly 1%."""
        for name, cfg in gap.SESSION_GAPS.items():
            assert cfg["risk_pct"] == pytest.approx(0.005), name

    def test_ref_hour_is_one_before_open(self):
        """Reference bar is always the hour before the session open."""
        for name, cfg in gap.SESSION_GAPS.items():
            ref = cfg["ref_hour_utc"]
            opn = cfg["open_hour_utc"]
            expected_ref = (opn - 1) % 24
            assert ref == expected_ref, (
                f"{name}: ref_hour {ref} ≠ open_hour-1 ({expected_ref})")


if __name__ == "__main__":
    import pytest as _pt
    _pt.main([__file__, "-v"])
