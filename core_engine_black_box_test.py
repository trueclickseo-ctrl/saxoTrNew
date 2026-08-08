"""
core_engine_black_box_test.py
------------------------------
Black-box coverage for the ORIGINAL SaxoTrader/OMX30 engine — fx.py,
kill_switch.py, instrument_map.py, strategy.py — none of which are
touched by black_box_test.py (that file only covers the newer ATOS
US-stocks subsystem: capital_config, us_reversion, us_momentum, etc.)

This is the highest-risk, order-placing part of the codebase, so it
gets tested even though it's not the newest code.

Run:  python core_engine_black_box_test.py
Exit code 0 = all pass, 1 = one or more failures.
"""
import sys, os, json, tempfile, shutil, traceback
from unittest.mock import patch, MagicMock

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
        if result is True:
            print(f"  {GREEN}PASS{RESET}  {name}")
            _results.append(("PASS", name, ""))
        else:
            print(f"  {RED}FAIL{RESET}  {name}\n        {RED}{result}{RESET}")
            _results.append(("FAIL", name, str(result)))
    except Exception as e:
        tb = traceback.format_exc().strip().splitlines()[-1]
        print(f"  {RED}FAIL{RESET}  {name}\n        {RED}{type(e).__name__}: {e}{RESET}\n        {YELLOW}{tb}{RESET}")
        _results.append(("FAIL", name, f"{type(e).__name__}: {e}"))

def section(title):
    print(f"\n{BOLD}{CYAN}{'-'*60}{RESET}\n{BOLD}{CYAN}  {title}{RESET}\n{BOLD}{CYAN}{'-'*60}{RESET}")


# ══════════════════════════════════════════════════════════════════
# 1. fx.py — currency conversion (regression coverage for the bug
#    that originally caused BUY-FAILED 400s: missing/mis-applied FX)
# ══════════════════════════════════════════════════════════════════
section("1. FX conversion (fx.py)")

import fx

def t_sek_is_always_1to1():
    fx.reset_cache()
    assert fx.get_rate_to_sek("SEK") == 1.0
    assert fx.get_rate_to_sek("sek") == 1.0  # lowercase input
_run("get_rate_to_sek('SEK') == 1.0 regardless of case", t_sek_is_always_1to1)

def t_empty_currency_raises():
    fx.reset_cache()
    try:
        fx.get_rate_to_sek("")
        return "expected ValueError for empty currency, none raised"
    except ValueError:
        return True
_run("get_rate_to_sek('') raises ValueError (never silently sizes as SEK)", t_empty_currency_raises)

def t_none_currency_raises():
    fx.reset_cache()
    try:
        fx.get_rate_to_sek(None)
        return "expected ValueError for None currency, none raised"
    except ValueError:
        return True
_run("get_rate_to_sek(None) raises ValueError", t_none_currency_raises)

def t_live_fetch_used_when_available():
    fx.reset_cache()
    mock_fast_info = {"last_price": 12.34}
    with patch("fx.yf.Ticker") as mock_ticker:
        mock_ticker.return_value.fast_info = mock_fast_info
        rate = fx.get_rate_to_sek("EUR")
    assert rate == 12.34, f"expected live rate 12.34, got {rate}"
_run("live yfinance rate is used when fetch succeeds", t_live_fetch_used_when_available)

def t_falls_back_when_live_fetch_fails():
    fx.reset_cache()
    with patch("fx.yf.Ticker", side_effect=ConnectionError("network down")):
        rate = fx.get_rate_to_sek("USD")
    assert rate == fx.FALLBACK_RATES_TO_SEK["USD"], f"expected fallback {fx.FALLBACK_RATES_TO_SEK['USD']}, got {rate}"
_run("falls back to FALLBACK_RATES_TO_SEK when live fetch throws", t_falls_back_when_live_fetch_fails)

def t_falls_back_on_implausible_negative_rate():
    fx.reset_cache()
    with patch("fx.yf.Ticker") as mock_ticker:
        mock_ticker.return_value.fast_info = {"last_price": -5.0}  # implausible
        rate = fx.get_rate_to_sek("GBP")
    assert rate == fx.FALLBACK_RATES_TO_SEK["GBP"], f"expected fallback for implausible rate, got {rate}"
_run("negative/implausible live rate triggers fallback, not silently used", t_falls_back_on_implausible_negative_rate)

def t_unknown_currency_with_no_fallback_raises():
    fx.reset_cache()
    with patch("fx.yf.Ticker", side_effect=ConnectionError("network down")):
        try:
            fx.get_rate_to_sek("XYZ")  # not in FALLBACK_RATES_TO_SEK
            return "expected RuntimeError for unmapped currency, none raised"
        except RuntimeError:
            return True
_run("unmapped currency with failed fetch raises RuntimeError (never guesses)", t_unknown_currency_with_no_fallback_raises)

def t_rate_is_cached_after_first_call():
    fx.reset_cache()
    with patch("fx.yf.Ticker") as mock_ticker:
        mock_ticker.return_value.fast_info = {"last_price": 10.0}
        fx.get_rate_to_sek("USD")
        fx.get_rate_to_sek("USD")
        fx.get_rate_to_sek("USD")
    assert mock_ticker.call_count == 1, f"expected 1 live fetch (cached after), got {mock_ticker.call_count}"
_run("rate is fetched once and cached, not refetched every call", t_rate_is_cached_after_first_call)

def t_reset_cache_forces_refetch():
    with patch("fx.yf.Ticker") as mock_ticker:
        mock_ticker.return_value.fast_info = {"last_price": 10.0}
        fx.reset_cache()
        fx.get_rate_to_sek("USD")
        fx.reset_cache()
        fx.get_rate_to_sek("USD")
    assert mock_ticker.call_count == 2, f"expected 2 fetches across two cache resets, got {mock_ticker.call_count}"
_run("reset_cache() forces a fresh fetch on next call", t_reset_cache_forces_refetch)

def t_eur_sek_backward_compat_alias():
    fx.reset_cache()
    with patch("fx.yf.Ticker") as mock_ticker:
        mock_ticker.return_value.fast_info = {"last_price": 11.5}
        assert fx.get_eur_sek_rate() == 11.5
_run("get_eur_sek_rate() alias matches get_rate_to_sek('EUR')", t_eur_sek_backward_compat_alias)


# ══════════════════════════════════════════════════════════════════
# 2. instrument_map.py — never silently assume SEK for unmapped currency
# ══════════════════════════════════════════════════════════════════
section("2. Instrument map (instrument_map.py)")

import instrument_map

def _write_temp_map(rows_csv_text):
    """Point instrument_map.MAP_FILE at a temp CSV for the duration of a test."""
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, newline="")
    tmp.write(rows_csv_text)
    tmp.close()
    return tmp.name

def t_missing_map_file_raises():
    orig = instrument_map.MAP_FILE
    instrument_map.MAP_FILE = "/tmp/definitely_does_not_exist_12345.csv"
    try:
        instrument_map.load_instrument_map()
        return "expected FileNotFoundError, none raised"
    except FileNotFoundError:
        return True
    finally:
        instrument_map.MAP_FILE = orig
_run("load_instrument_map() raises FileNotFoundError if CSV missing", t_missing_map_file_raises)

def t_row_without_currency_is_skipped_not_defaulted():
    csv_text = "yahoo_ticker,uic,symbol,currency\nAAPL,123,AAPL:xnas,\nMSFT,456,MSFT:xnas,USD\n"
    path = _write_temp_map(csv_text)
    orig = instrument_map.MAP_FILE
    instrument_map.MAP_FILE = path
    try:
        mapping = instrument_map.load_instrument_map()
        assert "AAPL" not in mapping, "row with blank currency should be skipped, not defaulted"
        assert mapping["MSFT"]["currency"] == "USD"
    finally:
        instrument_map.MAP_FILE = orig
        os.unlink(path)
_run("row with no currency is skipped entirely (never assumed SEK)", t_row_without_currency_is_skipped_not_defaulted)

def t_row_without_uic_is_skipped():
    csv_text = "yahoo_ticker,uic,symbol,currency\nAAPL,,AAPL:xnas,USD\nMSFT,456,MSFT:xnas,USD\n"
    path = _write_temp_map(csv_text)
    orig = instrument_map.MAP_FILE
    instrument_map.MAP_FILE = path
    try:
        mapping = instrument_map.load_instrument_map()
        assert "AAPL" not in mapping, "row with no uic (needs_review/unmapped) should be skipped"
        assert "MSFT" in mapping
    finally:
        instrument_map.MAP_FILE = orig
        os.unlink(path)
_run("row with no uic is skipped (unmapped/needs_review ticker)", t_row_without_uic_is_skipped)

def t_currency_is_uppercased():
    csv_text = "yahoo_ticker,uic,symbol,currency\nAAPL,123,AAPL:xnas,usd\n"
    path = _write_temp_map(csv_text)
    orig = instrument_map.MAP_FILE
    instrument_map.MAP_FILE = path
    try:
        mapping = instrument_map.load_instrument_map()
        assert mapping["AAPL"]["currency"] == "USD", f"expected uppercased USD, got {mapping['AAPL']['currency']}"
    finally:
        instrument_map.MAP_FILE = orig
        os.unlink(path)
_run("currency codes are normalized to uppercase", t_currency_is_uppercased)


# ══════════════════════════════════════════════════════════════════
# 3. kill_switch.py — trading halt + daily loss circuit breaker +
#    risk-capital tracker (the sizing-bug fix lives here)
# ══════════════════════════════════════════════════════════════════
section("3. Kill switch & risk capital (kill_switch.py)")

# Redirect kill_switch's file paths into a temp dir for the whole section
# so tests never touch the real project's live state files.
_tmp_dir = tempfile.mkdtemp(prefix="ks_test_")
import kill_switch as ks
_orig_paths = (ks.KILL_SWITCH_FILE, ks.DAILY_STATE_FILE, ks.RISK_CAPITAL_FILE)
ks.KILL_SWITCH_FILE = os.path.join(_tmp_dir, "STOP_TRADING")
ks.DAILY_STATE_FILE = os.path.join(_tmp_dir, "data", "daily_state.json")
ks.RISK_CAPITAL_FILE = os.path.join(_tmp_dir, "data", "risk_capital.json")

def t_kill_switch_inactive_by_default():
    assert ks.kill_switch_active() is False
_run("kill_switch_active() is False when STOP_TRADING file absent", t_kill_switch_inactive_by_default)

def t_kill_switch_active_when_file_present():
    os.makedirs(_tmp_dir, exist_ok=True)
    open(ks.KILL_SWITCH_FILE, "w").close()
    try:
        assert ks.kill_switch_active() is True
    finally:
        os.remove(ks.KILL_SWITCH_FILE)
_run("kill_switch_active() is True once STOP_TRADING file exists", t_kill_switch_active_when_file_present)

def t_day_start_equity_initializes_once():
    if os.path.exists(ks.DAILY_STATE_FILE):
        os.remove(ks.DAILY_STATE_FILE)
    first = ks.get_day_start_equity(current_equity=15000)
    second = ks.get_day_start_equity(current_equity=99999)  # different value, same day
    assert first == 15000
    assert second == 15000, f"day_start_equity should stay pinned to first call's value, got {second}"
_run("get_day_start_equity() locks in the FIRST value seen that day", t_day_start_equity_initializes_once)

def t_daily_loss_cap_not_breached_under_threshold():
    breached = ks.daily_loss_cap_breached(day_start_equity=10000, current_equity=9800, max_daily_loss_pct=0.03)
    assert breached is False, "2% drawdown should not breach a 3% cap"
_run("daily_loss_cap_breached() False when drawdown below threshold", t_daily_loss_cap_not_breached_under_threshold)

def t_daily_loss_cap_breached_over_threshold():
    breached = ks.daily_loss_cap_breached(day_start_equity=10000, current_equity=9600, max_daily_loss_pct=0.03)
    assert breached is True, "4% drawdown should breach a 3% cap"
_run("daily_loss_cap_breached() True when drawdown exceeds threshold", t_daily_loss_cap_breached_over_threshold)

def t_daily_loss_cap_exact_boundary():
    breached = ks.daily_loss_cap_breached(day_start_equity=10000, current_equity=9700, max_daily_loss_pct=0.03)
    assert breached is True, "exactly 3% drawdown should breach a >= 3% cap (boundary inclusive)"
_run("daily_loss_cap_breached() treats exact threshold as breached (>=)", t_daily_loss_cap_exact_boundary)

def t_daily_loss_cap_zero_start_equity_safe():
    breached = ks.daily_loss_cap_breached(day_start_equity=0, current_equity=100, max_daily_loss_pct=0.03)
    assert breached is False, "zero start equity must not divide-by-zero or false-trigger"
_run("daily_loss_cap_breached() handles zero day_start_equity without crashing", t_daily_loss_cap_zero_start_equity_safe)

def t_risk_capital_initializes_to_starting_capital():
    if os.path.exists(ks.RISK_CAPITAL_FILE):
        os.remove(ks.RISK_CAPITAL_FILE)
    import config
    val = ks.get_risk_capital()
    assert val == config.STARTING_CAPITAL, f"expected {config.STARTING_CAPITAL}, got {val}"
_run("get_risk_capital() initializes from config.STARTING_CAPITAL (THE core sizing-bug fix)", t_risk_capital_initializes_to_starting_capital)

def t_record_fill_buy_decreases_risk_capital():
    if os.path.exists(ks.RISK_CAPITAL_FILE):
        os.remove(ks.RISK_CAPITAL_FILE)
    start = ks.get_risk_capital()
    new_balance = ks.record_fill(-1500)  # a buy costs 1500 SEK
    assert new_balance == start - 1500, f"expected {start - 1500}, got {new_balance}"
_run("record_fill() with negative delta (BUY) decreases risk capital correctly", t_record_fill_buy_decreases_risk_capital)

def t_record_fill_sell_increases_risk_capital():
    before = ks.get_risk_capital()
    new_balance = ks.record_fill(2000)  # a sell returns 2000 SEK
    assert new_balance == before + 2000, f"expected {before + 2000}, got {new_balance}"
_run("record_fill() with positive delta (SELL) increases risk capital correctly", t_record_fill_sell_increases_risk_capital)

def t_risk_capital_never_touches_saxo_reported_equity():
    """The whole point of this module: risk capital is LOCAL and independent
    of whatever inflated balance the Saxo SIM account reports."""
    import config
    if os.path.exists(ks.RISK_CAPITAL_FILE):
        os.remove(ks.RISK_CAPITAL_FILE)
    val = ks.get_risk_capital()
    # Simulate Saxo reporting a wildly different (inflated demo) balance —
    # get_risk_capital() takes no such argument, so it CAN'T be swayed by it.
    fake_saxo_equity = 250_000
    assert val != fake_saxo_equity
    assert val == config.STARTING_CAPITAL
_run("risk capital is fully decoupled from Saxo's reported account equity", t_risk_capital_never_touches_saxo_reported_equity)

# cleanup
ks.KILL_SWITCH_FILE, ks.DAILY_STATE_FILE, ks.RISK_CAPITAL_FILE = _orig_paths
shutil.rmtree(_tmp_dir, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════
# 4. strategy.py — signal generation, commission, position sizing
# ══════════════════════════════════════════════════════════════════
section("4. Strategy logic (strategy.py)")

import pandas as pd
import strategy
import config as cfg

def _flat_then_rising_df(n_flat=55, n_rise=10):
    import numpy as np
    flat = [100.0] * n_flat
    rise = [100 + i * 3 for i in range(1, n_rise + 1)]
    close = flat + rise
    df = pd.DataFrame({
        "Close": close,
        "High": [c * 1.01 for c in close],
        "Low": [c * 0.99 for c in close],
    })
    return df

def t_add_indicators_produces_expected_columns():
    df = _flat_then_rising_df()
    out = strategy.add_indicators(df)
    for col in ["fast_ma", "slow_ma", "atr", "trend_up", "cross_up", "cross_down"]:
        assert col in out.columns, f"missing column {col}"
_run("add_indicators() produces all expected columns", t_add_indicators_produces_expected_columns)

def t_cross_up_detected_on_golden_cross():
    df = _flat_then_rising_df()
    out = strategy.add_indicators(df)
    assert out["cross_up"].any(), "expected at least one cross_up=True after a sustained rally"
_run("cross_up fires when fast MA crosses above slow MA", t_cross_up_detected_on_golden_cross)

def t_no_cross_in_perfectly_flat_market():
    df = _flat_then_rising_df(n_flat=80, n_rise=0)
    out = strategy.add_indicators(df)
    assert not out["cross_up"].any() and not out["cross_down"].any(), "flat prices should never cross"
_run("no cross_up/cross_down signals fire in a perfectly flat market", t_no_cross_in_perfectly_flat_market)

def t_commission_uses_minimum_for_small_trades():
    # tiny trade: value * rate would be below the $1 minimum
    fee = strategy.commission(shares=1, price=10)  # $10 trade value
    assert fee == cfg.MIN_COMMISSION_USD, f"expected minimum {cfg.MIN_COMMISSION_USD}, got {fee}"
_run("commission() applies the $1 minimum ticket fee for small trades", t_commission_uses_minimum_for_small_trades)

def t_commission_uses_pct_for_large_trades():
    shares, price = 1000, 500  # $500,000 trade value
    fee = strategy.commission(shares, price)
    expected = shares * price * cfg.COMMISSION_RATE_PCT
    assert fee == expected, f"expected {expected}, got {fee}"
    assert fee > cfg.MIN_COMMISSION_USD
_run("commission() switches to percentage-based fee once it exceeds the minimum", t_commission_uses_pct_for_large_trades)

def t_position_size_respects_risk_pct():
    capital, entry, stop = 10_000, 100, 95  # risk 1% = 100 SEK, per-share risk = 5
    shares = strategy.position_size(capital, entry, stop)
    expected = int((capital * cfg.RISK_PER_TRADE_PCT) / (entry - stop))
    assert shares == expected, f"expected {expected}, got {shares}"
_run("position_size() sizes correctly off RISK_PER_TRADE_PCT and stop distance", t_position_size_respects_risk_pct)

def t_position_size_zero_when_stop_above_entry():
    """Invalid stop (at or above entry) must never produce a position, not a negative/huge one."""
    shares = strategy.position_size(capital=10_000, entry_price=100, stop_price=105)
    assert shares == 0, f"expected 0 shares for invalid stop above entry, got {shares}"
_run("position_size() returns 0 when stop_price >= entry_price (invalid stop)", t_position_size_zero_when_stop_above_entry)

def t_position_size_zero_stop_equals_entry():
    shares = strategy.position_size(capital=10_000, entry_price=100, stop_price=100)
    assert shares == 0
_run("position_size() returns 0 when stop_price == entry_price (would divide by zero)", t_position_size_zero_stop_equals_entry)

def t_position_size_cfd_takes_the_smaller_of_risk_and_margin_limits():
    # Risk-based sizing would allow a lot of contracts, but margin should cap it lower
    capital = 10_000
    entry, stop = 1000, 990  # per-unit risk = 10 -> risk-based = 10 contracts (1% of 10000/10)
    contracts = strategy.position_size_cfd(capital, entry, stop, margin_rate=0.5)  # 50% margin -> expensive
    margin_per_contract = entry * 0.5
    margin_based = int(capital / margin_per_contract)  # = 20
    risk_based = int((capital * cfg.RISK_PER_TRADE_PCT) / (entry - stop))  # = 10
    assert contracts == min(risk_based, margin_based), f"expected {min(risk_based, margin_based)}, got {contracts}"
_run("position_size_cfd() returns the SMALLER of risk-based and margin-based contract counts", t_position_size_cfd_takes_the_smaller_of_risk_and_margin_limits)

def t_position_size_cfd_zero_margin_rate_returns_zero():
    contracts = strategy.position_size_cfd(10_000, 1000, 990, margin_rate=0)
    assert contracts == 0, "zero margin_rate must not divide-by-zero or return an unbounded count"
_run("position_size_cfd() returns 0 (not a crash) when margin_rate is 0", t_position_size_cfd_zero_margin_rate_returns_zero)


# ══════════════════════════════════════════════════════════════════
print(f"\n{BOLD}{'='*60}{RESET}\n{BOLD}  CORE ENGINE BLACK BOX RESULTS{RESET}\n{BOLD}{'='*60}{RESET}")
passed = sum(1 for r in _results if r[0] == "PASS")
failed = sum(1 for r in _results if r[0] == "FAIL")
print(f"  Total:   {len(_results)}")
print(f"  {GREEN}Passed: {passed}{RESET}")
if failed:
    print(f"  {RED}Failed: {failed}{RESET}")
    for status, name, msg in _results:
        if status == "FAIL":
            print(f"    {RED}- {name}: {msg}{RESET}")
    print(f"\n{BOLD}{RED}SOME TESTS FAILED{RESET}\n{BOLD}{'='*60}{RESET}")
    sys.exit(1)
else:
    print(f"  {RED}Failed: 0{RESET}".replace(RED, GREEN))
    print(f"\n{GREEN}{BOLD}ALL TESTS PASSED{RESET}\n{BOLD}{'='*60}{RESET}")
    sys.exit(0)
