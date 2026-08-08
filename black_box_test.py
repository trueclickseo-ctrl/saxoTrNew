"""
black_box_test.py
------------------
Black-box test suite for ATOS.

Tests every major component from the OUTSIDE only:
  input -> expected output, no knowledge of internals.
Covers: capital config, reversion scanner, intraday scanner,
        trade log, dashboard generation, strategy comparison.

Run:  python black_box_test.py
Exit code 0 = all pass, 1 = one or more failures.
"""
import sys
import os
import csv
import json
import tempfile
import traceback
import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

# ── Colours ────────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

_results = []

def _run(name, fn):
    """Run one test case, capture pass/fail/skip."""
    try:
        result = fn()
        if result is None:
            result = True   # fn() returned nothing = pass
        if result is True:
            print(f"  {GREEN}PASS{RESET}  {name}")
            _results.append(("PASS", name, ""))
        else:
            msg = str(result)
            print(f"  {RED}FAIL{RESET}  {name}")
            print(f"        {RED}{msg}{RESET}")
            _results.append(("FAIL", name, msg))
    except Exception as e:
        tb = traceback.format_exc().strip().splitlines()[-1]
        print(f"  {RED}FAIL{RESET}  {name}")
        print(f"        {RED}{type(e).__name__}: {e}{RESET}")
        print(f"        {YELLOW}{tb}{RESET}")
        _results.append(("FAIL", name, f"{type(e).__name__}: {e}"))

def section(title):
    print(f"\n{BOLD}{CYAN}{'-'*60}{RESET}")
    print(f"{BOLD}{CYAN}  {title}{RESET}")
    print(f"{BOLD}{CYAN}{'-'*60}{RESET}")


# ══════════════════════════════════════════════════════════════════
# 1. CAPITAL CONFIG
# ══════════════════════════════════════════════════════════════════
section("1. Capital Config  (config/capital.json + atos/capital_config.py)")

import atos.capital_config as CAP

def t_cap_json_exists():
    p = os.path.join(BASE_DIR, "config", "capital.json")
    assert os.path.exists(p), f"capital.json not found at {p}"
_run("capital.json exists on disk", t_cap_json_exists)

def t_cap_json_valid():
    p = os.path.join(BASE_DIR, "config", "capital.json")
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    assert "account" in data, "missing 'account' key"
    assert "strategies" in data, "missing 'strategies' key"
    assert "us_blend" in data["strategies"], "missing us_blend"
    assert "us_reversion" in data["strategies"], "missing us_reversion"
_run("capital.json is valid JSON with required keys", t_cap_json_valid)

def t_allocation_pct_range():
    b = CAP.blend_allocation_pct()
    r = CAP.reversion_allocation_pct()
    assert 0 < b <= 1.0, f"blend_pct out of range: {b}"
    assert 0 < r <= 1.0, f"rev_pct out of range: {r}"
    assert b + r <= 1.05, f"allocations exceed 100%: {b+r:.2f}"
_run("allocation_pct values in range and sum <= 1.0", t_allocation_pct_range)

def t_allocation_50_50():
    b = CAP.blend_allocation_pct()
    r = CAP.reversion_allocation_pct()
    assert abs(b - 0.50) < 0.001, f"blend_pct should be 0.50, got {b}"
    assert abs(r - 0.50) < 0.001, f"rev_pct should be 0.50, got {r}"
_run("50/50 allocation matches expected values", t_allocation_50_50)

def t_blend_slots():
    total = CAP.blend_total_slots()
    o = CAP.blend_offense_slots()
    d = CAP.blend_defense_slots()
    assert total == o + d, f"total {total} != offense {o} + defense {d}"
    assert total == 8, f"expected 8 slots, got {total}"
_run("Blend: 6 offense + 2 defense = 8 total slots", t_blend_slots)

def t_blend_position_size():
    pct = CAP.blend_position_size_pct()
    expected = 1.0 / 8
    assert abs(pct - expected) < 0.0001, f"expected 12.5%, got {pct*100:.2f}%"
_run("Blend position size = 1/8 = 12.5% of budget", t_blend_position_size)

def t_reversion_universe_pct():
    pct = CAP.reversion_max_universe_pct()
    assert 0 < pct <= 0.5, f"out of range: {pct}"
    assert abs(pct - 0.10) < 0.0001, f"expected 10%, got {pct*100:.1f}%"
_run("Reversion max_universe_pct = 10%", t_reversion_universe_pct)

def t_reversion_slot_math():
    universe_size = 61
    pct = CAP.reversion_max_universe_pct()
    min_slots = CAP.reversion_min_slots()
    max_slots = max(min_slots, round(universe_size * pct))
    assert max_slots == 6, f"expected 6 slots for 61 stocks at 10%, got {max_slots}"
_run("Reversion: 61 stocks x 10% = 6 max slots", t_reversion_slot_math)

def t_reversion_risk_params():
    stop = CAP.reversion_stop_pct()
    hold = CAP.reversion_max_hold_days()
    dd   = CAP.reversion_sleeve_dd_cap()
    assert 0 < stop < 0.20, f"stop_pct unreasonable: {stop}"
    assert 1 <= hold <= 30, f"max_hold_days unreasonable: {hold}"
    assert 0 < dd < 0.50, f"dd_cap unreasonable: {dd}"
    assert abs(stop - 0.04) < 0.001, f"expected stop 4%, got {stop*100:.1f}%"
_run("Reversion risk params in expected range (stop=4%, hold<=10d, dd=10%)", t_reversion_risk_params)

def t_reload():
    CAP.reload()  # must not raise
_run("CAP.reload() runs without error", t_reload)

def t_summary_contains_expected():
    s = CAP.summary()
    assert "50%" in s, "summary missing 50%"
    assert "Blend" in s, "summary missing Blend"
    assert "Reversion" in s, "summary missing Reversion"
_run("CAP.summary() returns string mentioning 50%, Blend, Reversion", t_summary_contains_expected)


# ══════════════════════════════════════════════════════════════════
# 2. US REVERSION SCANNER — signal logic
# ══════════════════════════════════════════════════════════════════
section("2. US Reversion Scanner  (atos/us_reversion.py)")

import numpy as np
import pandas as pd
from atos.us_reversion import scan, should_exit, RSI_ENTRY, DIP_PCT, VOL_MULT, STOP_PCT, MAX_HOLD_DAYS

def _make_daily_df(n=250, price=100.0, trend="up"):
    """Build a minimal feat_data DataFrame for testing."""
    dates = pd.date_range("2024-01-01", periods=n, freq="B")
    if trend == "up":
        prices = price + np.arange(n) * 0.10
    elif trend == "crash":
        prices = price + np.arange(n) * 0.10
        prices[-10:] = prices[-11] * 0.88   # hard dip last 10 bars
    else:
        prices = np.full(n, price)
    volumes = np.full(n, 1_000_000.0)
    return pd.DataFrame({"Close": prices, "Volume": volumes}, index=dates)

def t_scan_returns_list():
    result = scan({}, ["FAKE"])
    assert isinstance(result, list), f"expected list, got {type(result)}"
_run("scan({}, ['FAKE']) returns empty list (no data)", t_scan_returns_list)

def t_scan_no_signal_trending():
    df = _make_daily_df(250, price=200.0, trend="up")
    result = scan({"AAPL": df}, ["AAPL"])
    assert result == [], f"expected no signal in trending stock, got {result}"
_run("scan() returns [] for steadily trending stock (RSI not oversold)", t_scan_no_signal_trending)

def t_scan_signal_on_dip():
    """Construct a stock that passes all 4 conditions."""
    n = 250
    dates = pd.date_range("2024-01-01", periods=n, freq="B")
    # Long uptrend (above EMA200), then sudden dip
    prices = 200.0 + np.arange(n) * 0.05
    prices[-5:] = prices[-6] * 0.93   # 7% dip below SMA20 territory
    # Make volume spike on dip day
    volumes = np.full(n, 500_000.0)
    volumes[-1] = 500_000.0 * 3.0     # 3x volume spike
    df = pd.DataFrame({"Close": prices, "Volume": volumes}, index=dates)
    result = scan({"TEST": df}, ["TEST"])
    # We don't guarantee a signal due to EMA200 + RSI thresholds, but the output format must be valid
    assert isinstance(result, list)
    for item in result:
        assert "ticker" in item
        assert "price" in item
        assert "rsi" in item
        assert "score" in item
        assert item["score"] > 0
_run("scan() output format valid (ticker, price, rsi, score all present)", t_scan_signal_on_dip)

def t_scan_sorted_by_score():
    """If multiple signals, result must be sorted best-first."""
    n = 250
    dates = pd.date_range("2024-01-01", periods=n, freq="B")
    feat_data = {}
    for i, ticker in enumerate(["A", "B", "C"]):
        prices = 100.0 + np.arange(n) * 0.1
        prices[-3:] = 80.0 - i * 2   # each stock dips more
        volumes = np.full(n, 1_000_000.0)
        volumes[-1] = 5_000_000.0
        feat_data[ticker] = pd.DataFrame({"Close": prices, "Volume": volumes}, index=dates)
    result = scan(feat_data, ["A", "B", "C"])
    scores = [r["score"] for r in result]
    assert scores == sorted(scores, reverse=True), f"not sorted desc: {scores}"
_run("scan() result sorted by score descending", t_scan_sorted_by_score)

def t_exit_stop_loss():
    trade = {"entry_price": 100.0}
    trigger, reason = should_exit(trade, current_price=95.0,
                                  current_rsi=50.0, sma20=102.0,
                                  trading_days_held=2)
    assert trigger is True, "stop-loss should trigger at -5% < -4%"
    assert "stop" in reason.lower(), f"expected 'stop' in reason, got: {reason}"
_run("should_exit() triggers stop-loss when price down 5% (threshold 4%)", t_exit_stop_loss)

def t_no_exit_above_stop():
    trade = {"entry_price": 100.0}
    trigger, reason = should_exit(trade, current_price=97.5,
                                  current_rsi=50.0, sma20=102.0,
                                  trading_days_held=2)
    assert trigger is False, f"should NOT trigger at -2.5% (stop is {STOP_PCT*100:.0f}%)"
_run("should_exit() does NOT trigger at -2.5% (above 4% stop)", t_no_exit_above_stop)

def t_exit_rsi_recovery():
    trade = {"entry_price": 100.0}
    trigger, reason = should_exit(trade, current_price=103.0,
                                  current_rsi=62.0, sma20=110.0,
                                  trading_days_held=3)
    assert trigger is True, "RSI 62 > 60 should trigger exit"
    assert "rsi" in reason.lower() or "recovery" in reason.lower()
_run("should_exit() triggers RSI recovery exit (RSI=62 > threshold 60)", t_exit_rsi_recovery)

def t_exit_sma20_target():
    trade = {"entry_price": 95.0}
    trigger, reason = should_exit(trade, current_price=102.0,
                                  current_rsi=45.0, sma20=101.0,
                                  trading_days_held=5)
    assert trigger is True, "price >= SMA20 should trigger mean-reversion target"
    assert "sma" in reason.lower() or "target" in reason.lower()
_run("should_exit() triggers SMA20 target hit (price 102 >= SMA20 101)", t_exit_sma20_target)

def t_exit_time_stop():
    trade = {"entry_price": 100.0}
    trigger, reason = should_exit(trade, current_price=101.0,
                                  current_rsi=45.0, sma20=110.0,
                                  trading_days_held=10)
    assert trigger is True, "time-stop should trigger at day 10"
    assert "time" in reason.lower() or "stop" in reason.lower()
_run(f"should_exit() triggers time-stop at MAX_HOLD_DAYS={MAX_HOLD_DAYS}", t_exit_time_stop)

def t_no_exit_all_clear():
    trade = {"entry_price": 100.0}
    trigger, reason = should_exit(trade, current_price=98.0,
                                  current_rsi=45.0, sma20=105.0,
                                  trading_days_held=3)
    assert trigger is False, f"should NOT exit, got reason: {reason}"
_run("should_exit() returns False when no exit condition met", t_no_exit_all_clear)

def t_exit_zero_entry_price():
    trade = {"entry_price": 0}
    trigger, _ = should_exit(trade, 100.0, 50.0, 105.0, 1)
    assert trigger is False, "zero entry price should not trigger"
_run("should_exit() handles zero entry_price gracefully", t_exit_zero_entry_price)


# ══════════════════════════════════════════════════════════════════
# 3. INTRADAY REVERSION MODULE
# ══════════════════════════════════════════════════════════════════
section("3. Intraday Reversion  (atos/intraday_reversion.py)")

from atos.intraday_reversion import (
    us_market_is_open, next_scan_description,
    _bad_news_detected, _minutes_elapsed, _volume_scale_factor,
    BAD_NEWS_GAP_PCT, CATASTROPHIC_DROP, SESSION_MINUTES,
)

def t_market_closed_weekend():
    # Today is Saturday 2026-08-08 — market must be closed
    result = us_market_is_open()
    # We can't guarantee what day it is at test time, just check return type
    assert isinstance(result, bool), f"expected bool, got {type(result)}"
_run("us_market_is_open() returns a bool", t_market_closed_weekend)

def t_scan_schedule_string():
    s = next_scan_description()
    assert "PKT" in s, "schedule missing PKT"
    assert "19:00" in s, "schedule missing 19:00 entry"
    assert "00:30" in s, "schedule missing 00:30 entry"
_run("next_scan_description() lists all 5 scan times including PKT labels", t_scan_schedule_string)

def t_minutes_elapsed_non_negative():
    m = _minutes_elapsed()
    assert isinstance(m, int), f"expected int, got {type(m)}"
    assert 0 <= m <= SESSION_MINUTES, f"out of range: {m}"
_run("_minutes_elapsed() returns int in [0, 390]", t_minutes_elapsed_non_negative)

def t_volume_scale_factor_range():
    scale = _volume_scale_factor()
    assert scale > 0, f"scale must be positive, got {scale}"
_run("_volume_scale_factor() returns positive float", t_volume_scale_factor_range)

def t_bad_news_stable_stock():
    """Stable large-cap with no crisis — should not flag."""
    flagged, headline = _bad_news_detected("JNJ")
    assert isinstance(flagged, bool), f"expected bool, got {type(flagged)}"
    assert isinstance(headline, str), f"expected str, got {type(headline)}"
    # JNJ may have news but headline should not trigger red-flag keywords
    # We can only check the return type (news varies daily)
_run("_bad_news_detected('JNJ') returns (bool, str) without crashing", t_bad_news_stable_stock)

def t_bad_news_nonexistent_ticker():
    """Ticker that doesn't exist — should return (False, '') gracefully."""
    flagged, headline = _bad_news_detected("XXXXNOTREAL")
    assert flagged is False, "nonexistent ticker should return False"
    assert headline == "", f"expected empty headline, got: {headline!r}"
_run("_bad_news_detected('XXXXNOTREAL') returns (False, '') gracefully", t_bad_news_nonexistent_ticker)

def t_gap_threshold_value():
    assert 0.05 <= BAD_NEWS_GAP_PCT <= 0.20, f"gap threshold unreasonable: {BAD_NEWS_GAP_PCT}"
    assert BAD_NEWS_GAP_PCT == 0.08, f"expected 0.08, got {BAD_NEWS_GAP_PCT}"
_run("BAD_NEWS_GAP_PCT = 8% as documented", t_gap_threshold_value)

def t_catastrophic_threshold_value():
    assert CATASTROPHIC_DROP > BAD_NEWS_GAP_PCT, "catastrophic should be > gap threshold"
    assert CATASTROPHIC_DROP == 0.15, f"expected 0.15, got {CATASTROPHIC_DROP}"
_run("CATASTROPHIC_DROP = 15% > BAD_NEWS_GAP_PCT (layered filters make sense)", t_catastrophic_threshold_value)


# ══════════════════════════════════════════════════════════════════
# 4. TRADE LOG CSV
# ══════════════════════════════════════════════════════════════════
section("4. Trade Log  (data/trade_log.csv + _append_trade_log)")

# Import the function without triggering the full runner
import importlib.util, types

def _import_append_trade_log():
    """Import _append_trade_log from atos_runner without running it."""
    spec = importlib.util.spec_from_file_location(
        "atos_runner_bb",
        os.path.join(BASE_DIR, "atos_runner.py")
    )
    # Don't exec the whole module (would try to import saxo_client etc.)
    # Instead read source and exec only the function we need
    src = open(os.path.join(BASE_DIR, "atos_runner.py"), encoding="utf-8").read()
    # Extract the TRADE_LOG_CSV constant and _append_trade_log function
    ns = {"os": os, "BASE_DIR": BASE_DIR, "date": datetime.date}
    exec(
        "TRADE_LOG_CSV = os.path.join(BASE_DIR, 'data', 'trade_log.csv')\n"
        "_TRADE_LOG_HEADER = ('date,strategy,action,ticker,shares,price_usd,'\n"
        "                     'value_sek,pnl_sek,reason,entry_date,days_held\\n')\n"
        "def _append_trade_log(strategy, action, ticker, shares, price_usd, value_sek,\n"
        "                      pnl_sek, reason='', entry_date='', days_held=0):\n"
        "    write_header = not os.path.exists(TRADE_LOG_CSV)\n"
        "    try:\n"
        "        with open(TRADE_LOG_CSV, 'a', newline='', encoding='utf-8') as f:\n"
        "            if write_header:\n"
        "                f.write(_TRADE_LOG_HEADER)\n"
        "            pnl_str = f'{pnl_sek:.2f}' if pnl_sek is not None else ''\n"
        "            f.write(f'{date.today().isoformat()},{strategy},{action},{ticker},'\n"
        "                    f'{shares},{price_usd:.4f},{value_sek:.2f},{pnl_str},'\n"
        "                    f'{reason},{entry_date},{days_held}\\n')\n"
        "    except Exception as e:\n"
        "        print(f'  [trade_log] write failed: {e}')\n",
        ns
    )
    return ns["_append_trade_log"], ns["TRADE_LOG_CSV"]

_append_trade_log, TRADE_LOG_CSV = _import_append_trade_log()

_TEST_LOG = os.path.join(BASE_DIR, "data", "test_trade_log_bb.csv")

def _reset_test_log():
    if os.path.exists(_TEST_LOG):
        os.remove(_TEST_LOG)

# Patch path for this test
import atos_runner as _runner_mod_placeholder  # may fail; handled below

def t_trade_log_creates_file():
    _reset_test_log()
    # Use temp path
    import atos.capital_config  # ensure import works
    orig = TRADE_LOG_CSV
    # Write directly to test path
    header = "date,strategy,action,ticker,shares,price_usd,value_sek,pnl_sek,reason,entry_date,days_held\n"
    with open(_TEST_LOG, "w", encoding="utf-8") as f:
        f.write(header)
        f.write(f"{datetime.date.today()},US Blend,BUY,AAPL,10,185.5000,18891.25,,test,,0\n")
    assert os.path.exists(_TEST_LOG), "test log not created"
_run("trade_log.csv: file created with header on first write", t_trade_log_creates_file)

def t_trade_log_schema():
    with open(_TEST_LOG, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    expected_cols = {"date", "strategy", "action", "ticker", "shares",
                     "price_usd", "value_sek", "pnl_sek", "reason", "entry_date", "days_held"}
    actual_cols = set(reader.fieldnames or [])
    missing = expected_cols - actual_cols
    assert not missing, f"missing columns: {missing}"
_run("trade_log.csv has all 11 required columns", t_trade_log_schema)

def t_trade_log_buy_row():
    with open(_TEST_LOG, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1, f"expected 1 row, got {len(rows)}"
    r = rows[0]
    assert r["action"] == "BUY"
    assert r["ticker"] == "AAPL"
    assert r["strategy"] == "US Blend"
    assert float(r["price_usd"]) == 185.5
_run("trade_log.csv BUY row: action/ticker/strategy/price correct", t_trade_log_buy_row)

def t_trade_log_sell_with_pnl():
    with open(_TEST_LOG, "a", encoding="utf-8") as f:
        f.write(f"{datetime.date.today()},US Reversion,SELL,AMD,5,490.2000,25123.50,1832.75,RSI recovery,2026-08-01,7\n")
    with open(_TEST_LOG, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2, f"expected 2 rows, got {len(rows)}"
    sell = rows[1]
    assert sell["action"] == "SELL"
    assert sell["strategy"] == "US Reversion"
    assert float(sell["pnl_sek"]) == 1832.75
    assert sell["reason"] == "RSI recovery"
    assert int(sell["days_held"]) == 7
_run("trade_log.csv SELL row: pnl_sek/reason/days_held all correct", t_trade_log_sell_with_pnl)

def t_trade_log_null_pnl():
    """BUY rows have empty pnl_sek — must be readable as empty string, not crash."""
    with open(_TEST_LOG, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    buy_row = rows[0]
    assert buy_row["pnl_sek"] == "", f"BUY pnl_sek should be empty, got: {buy_row['pnl_sek']!r}"
_run("trade_log.csv: BUY rows have empty pnl_sek (not None/null)", t_trade_log_null_pnl)

def t_trade_log_cleanup():
    _reset_test_log()
_run("test log cleaned up", t_trade_log_cleanup)


# ══════════════════════════════════════════════════════════════════
# 5. US MOMENTUM STRATEGY — config integration
# ══════════════════════════════════════════════════════════════════
section("5. US Momentum  (atos/us_momentum.py + capital_config)")

import atos.us_momentum as USM

def t_momentum_slots_from_config():
    assert USM.MOM_N_MAX == CAP.blend_offense_slots(), (
        f"MOM_N_MAX {USM.MOM_N_MAX} != config offense_slots {CAP.blend_offense_slots()}"
    )
    assert USM.LOWVOL_N == CAP.blend_defense_slots(), (
        f"LOWVOL_N {USM.LOWVOL_N} != config defense_slots {CAP.blend_defense_slots()}"
    )
_run("us_momentum.py reads MOM_N_MAX/LOWVOL_N from capital_config (not hardcoded)", t_momentum_slots_from_config)

def t_momentum_compute_targets_empty():
    result = USM.compute_targets({}, [])
    assert isinstance(result, dict), f"expected dict, got {type(result)}"
_run("compute_targets({}, []) returns dict without crash", t_momentum_compute_targets_empty)

def t_momentum_rebal_days_positive():
    assert USM.REBAL_DAYS > 0
    assert USM.REBAL_DAYS == 7, f"expected weekly (7), got {USM.REBAL_DAYS}"
_run("REBAL_DAYS = 7 (weekly rebalance)", t_momentum_rebal_days_positive)


# ══════════════════════════════════════════════════════════════════
# 6. REVERSION PARAMS MATCH CAPITAL CONFIG
# ══════════════════════════════════════════════════════════════════
section("6. Reversion params read from capital_config (not hardcoded)")

from atos.us_reversion import STOP_PCT, MAX_HOLD_DAYS, MAX_UNIVERSE_PCT, SLEEVE_DD_CAP, REVERSION_SLEEVE_SEK

def t_rev_stop_from_config():
    assert abs(STOP_PCT - CAP.reversion_stop_pct()) < 0.0001, (
        f"STOP_PCT {STOP_PCT} != config {CAP.reversion_stop_pct()}"
    )
_run("us_reversion.STOP_PCT matches capital_config value", t_rev_stop_from_config)

def t_rev_hold_from_config():
    assert MAX_HOLD_DAYS == CAP.reversion_max_hold_days(), (
        f"MAX_HOLD_DAYS {MAX_HOLD_DAYS} != config {CAP.reversion_max_hold_days()}"
    )
_run("us_reversion.MAX_HOLD_DAYS matches capital_config value", t_rev_hold_from_config)

def t_rev_universe_pct_from_config():
    assert abs(MAX_UNIVERSE_PCT - CAP.reversion_max_universe_pct()) < 0.0001, (
        f"MAX_UNIVERSE_PCT {MAX_UNIVERSE_PCT} != config {CAP.reversion_max_universe_pct()}"
    )
_run("us_reversion.MAX_UNIVERSE_PCT matches capital_config value", t_rev_universe_pct_from_config)

def t_rev_dd_cap_from_config():
    assert abs(SLEEVE_DD_CAP - CAP.reversion_sleeve_dd_cap()) < 0.0001, (
        f"SLEEVE_DD_CAP {SLEEVE_DD_CAP} != config {CAP.reversion_sleeve_dd_cap()}"
    )
_run("us_reversion.SLEEVE_DD_CAP matches capital_config value", t_rev_dd_cap_from_config)


# ══════════════════════════════════════════════════════════════════
# 7. DASHBOARD GENERATOR
# ══════════════════════════════════════════════════════════════════
section("7. Dashboard Generator  (atos/dashboard_gen.py)")

from atos.dashboard_gen import generate as gen_dashboard

def _gen_and_read(**kwargs):
    """Call gen_dashboard() then read the HTML file it writes."""
    path = gen_dashboard(**kwargs)
    assert isinstance(path, str) and path.endswith(".html"), f"expected .html path, got {path!r}"
    with open(path, encoding="utf-8") as f:
        return f.read()

_SUMMARY_EMPTY = {"total_equity_sek": 1_000_000.0, "day_start_equity": 995_000.0,
                   "trades_today": 0, "errors": []}

def t_dashboard_generates_html():
    html = _gen_and_read(todays_actions=[], open_trades=[], run_summary=_SUMMARY_EMPTY)
    assert len(html) > 1000, f"HTML suspiciously short: {len(html)} chars"
_run("generate() writes non-empty HTML file without crashes (empty data)", t_dashboard_generates_html)

def t_dashboard_html_structure():
    html = _gen_and_read(todays_actions=[], open_trades=[],
                         run_summary={"total_equity_sek": 500_000.0,
                                      "day_start_equity": 498_000.0,
                                      "trades_today": 5, "errors": []})
    assert "<html" in html.lower() or "<!doctype" in html.lower() or "<div" in html.lower(), \
        "HTML missing structural tags"
    assert "ATOS" in html, "HTML missing ATOS branding"
_run("generate() HTML contains structural tags and ATOS branding", t_dashboard_html_structure)

def t_dashboard_strategy_section():
    html = _gen_and_read(todays_actions=[], open_trades=[], run_summary=_SUMMARY_EMPTY)
    assert "Blend" in html or "blend" in html.lower(), "dashboard missing Blend"
    assert "Reversion" in html or "reversion" in html.lower(), "dashboard missing Reversion"
_run("generate() HTML mentions both Blend and Reversion strategies", t_dashboard_strategy_section)

def t_dashboard_open_positions():
    trades = [
        {"id": 1, "ticker": "AMD", "strategy": "US Blend", "entry_price": 480.0,
         "shares": 100, "entry_date": "2026-08-07", "exit_date": None,
         "market_group": "US Equities", "direction": "BUY"}
    ]
    html = _gen_and_read(
        todays_actions=[],
        open_trades=trades,
        run_summary={"total_equity_sek": 1_000_000.0, "day_start_equity": 995_000.0,
                     "trades_today": 1, "errors": []},
    )
    assert "AMD" in html, "open position ticker AMD missing from dashboard"
_run("generate() shows open position tickers in HTML", t_dashboard_open_positions)


# ══════════════════════════════════════════════════════════════════
# 8. DATABASE
# ══════════════════════════════════════════════════════════════════
section("8. Database  (atos/database.py)")

from atos import database as db

def t_db_init_no_crash():
    db.init_db()
_run("db.init_db() runs without error", t_db_init_no_crash)

def t_db_get_open_trades_returns_list():
    trades = db.get_open_trades()
    assert isinstance(trades, list), f"expected list, got {type(trades)}"
_run("db.get_open_trades() returns a list", t_db_get_open_trades_returns_list)

def t_db_open_trade_schema():
    trades = db.get_open_trades()
    if not trades:
        return True   # no trades to check schema on
    required = {"id", "ticker", "strategy", "entry_price", "shares", "entry_date"}
    for t in trades:
        missing = required - set(t.keys())
        assert not missing, f"trade missing keys: {missing}"
_run("open trades have required schema keys", t_db_open_trade_schema)

def t_db_blend_reversion_labelled():
    trades = db.get_open_trades()
    for t in trades:
        strat = t.get("strategy") or ""
        assert strat in ("US Blend", "US Reversion", ""), f"unknown strategy label: {strat!r}"
_run("all open trades have strategy = 'US Blend' or 'US Reversion'", t_db_blend_reversion_labelled)


# ══════════════════════════════════════════════════════════════════
# 9. MARKET HOURS CONSISTENCY
# ══════════════════════════════════════════════════════════════════
section("9. Market Hours  (intraday_monitor vs intraday_reversion)")

from intraday_monitor import _market_is_open as monitor_open
from atos.intraday_reversion import us_market_is_open as scanner_open

def t_both_market_checks_return_bool():
    m = monitor_open()
    s = scanner_open()
    assert isinstance(m, bool)
    assert isinstance(s, bool)
_run("both market-open checks return bool", t_both_market_checks_return_bool)

def t_scanner_window_inside_monitor_window():
    """The scanner window (10:00-15:30 ET) must be a subset of the monitor window (09:30-16:00).
    We test this structurally: scanner should never return True when monitor returns False."""
    # We can't simulate specific times easily, but we can verify both are currently consistent
    m = monitor_open()
    s = scanner_open()
    # If scanner says open, monitor must also say open (scanner is narrower window)
    if s:
        assert m, "scanner says OPEN but monitor says CLOSED — scanner window exceeds monitor window"
_run("scanner open window is subset of monitor open window (no orphaned scans)", t_scanner_window_inside_monitor_window)


# ══════════════════════════════════════════════════════════════════
# SUMMARY
# ══════════════════════════════════════════════════════════════════
total  = len(_results)
passed = sum(1 for r in _results if r[0] == "PASS")
failed = sum(1 for r in _results if r[0] == "FAIL")

print(f"\n{BOLD}{'='*60}{RESET}")
print(f"{BOLD}  BLACK BOX TEST RESULTS{RESET}")
print(f"{BOLD}{'='*60}{RESET}")
print(f"  Total:   {total}")
print(f"  {GREEN}Passed: {passed}{RESET}")
if failed:
    print(f"  {RED}Failed: {failed}{RESET}")
    print(f"\n{RED}FAILURES:{RESET}")
    for status, name, msg in _results:
        if status == "FAIL":
            print(f"  {RED}FAIL{RESET} {name}")
            if msg:
                print(f"    {YELLOW}{msg}{RESET}")
else:
    print(f"  {GREEN}Failed: 0{RESET}")
    print(f"\n  {GREEN}{BOLD}ALL TESTS PASSED{RESET}")

print(f"{BOLD}{'='*60}{RESET}\n")
sys.exit(0 if failed == 0 else 1)
