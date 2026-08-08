"""
saxo_client_engine_black_box_test.py
--------------------------------------
Black-box coverage for saxo_client.py (HTTP layer talking to Saxo's API)
and saxo_live_engine.py (the daily decision-cycle orchestrator).

EVERYTHING here is mocked — no real HTTP call ever leaves this process,
and no Saxo token (live or expired) is required to run this file. That's
deliberate: these are the two files that actually place orders, so we
verify their behavior against controlled fake responses rather than a
live SIM account.

Run:  python saxo_client_engine_black_box_test.py
Exit code 0 = all pass, 1 = one or more failures.
"""
import sys, os, traceback
from unittest.mock import patch, MagicMock, call
import pandas as pd
import numpy as np

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


def _mock_response(status_code=200, json_data=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data if json_data is not None else {}
    resp.text = text
    if status_code >= 400:
        import requests
        http_err = requests.exceptions.HTTPError(f"{status_code} Client Error")
        http_err.response = resp
        resp.raise_for_status.side_effect = http_err
    else:
        resp.raise_for_status.return_value = None
    return resp


# ══════════════════════════════════════════════════════════════════
# 1. saxo_client.py — auth token resolution
# ══════════════════════════════════════════════════════════════════
section("1. saxo_client.py — token resolution (get_token)")

import saxo_client

def t_prefers_pkce_token_when_available():
    with patch("saxo_client.saxo_auth.get_valid_access_token", return_value="pkce_tok_123"):
        tok = saxo_client.get_token()
    assert tok == "pkce_tok_123"
_run("get_token() prefers self-refreshing PKCE login when available", t_prefers_pkce_token_when_available)

def t_falls_back_to_env_var_when_pkce_unavailable():
    with patch("saxo_client.saxo_auth.get_valid_access_token", side_effect=RuntimeError("no login")):
        with patch.dict(os.environ, {"SAXO_TOKEN": "manual_tok_456"}):
            tok = saxo_client.get_token()
    assert tok == "manual_tok_456"
_run("get_token() falls back to SAXO_TOKEN env var when PKCE unavailable", t_falls_back_to_env_var_when_pkce_unavailable)

def t_raises_clear_error_when_neither_available():
    with patch("saxo_client.saxo_auth.get_valid_access_token", side_effect=RuntimeError("no login")):
        env_without_token = {k: v for k, v in os.environ.items() if k != "SAXO_TOKEN"}
        with patch.dict(os.environ, env_without_token, clear=True):
            try:
                saxo_client.get_token()
                return "expected RuntimeError when no token source available, none raised"
            except RuntimeError as e:
                assert "SAXO_TOKEN" in str(e) or "login" in str(e).lower(), \
                    f"error message should guide the user, got: {e}"
                return True
_run("get_token() raises a clear, actionable RuntimeError when neither token source exists (this is exactly Monday's expired-token scenario)", t_raises_clear_error_when_neither_available)

def t_headers_include_bearer_prefix():
    with patch("saxo_client.get_token", return_value="abc123"):
        headers = saxo_client._headers()
    assert headers == {"Authorization": "Bearer abc123"}, f"got {headers}"
_run("_headers() formats Authorization as 'Bearer <token>'", t_headers_include_bearer_prefix)


# ══════════════════════════════════════════════════════════════════
# 2. saxo_client.py — retry logic for read endpoints
# ══════════════════════════════════════════════════════════════════
section("2. saxo_client.py — retry logic (_request_with_retry)")

def t_no_retry_needed_on_first_success():
    with patch("saxo_client.requests.request", return_value=_mock_response(200)) as mock_req:
        resp = saxo_client._request_with_retry("GET", "https://fake/url")
    assert mock_req.call_count == 1
    assert resp.status_code == 200
_run("_request_with_retry() makes exactly 1 call when the first attempt succeeds", t_no_retry_needed_on_first_success)

def t_retries_on_connection_error_then_succeeds():
    import requests as real_requests
    call_sequence = [real_requests.exceptions.ConnectionError("net down"), _mock_response(200)]
    with patch("saxo_client.requests.request", side_effect=call_sequence) as mock_req:
        with patch("saxo_client.time.sleep"):  # don't actually wait in tests
            resp = saxo_client._request_with_retry("GET", "https://fake/url")
    assert mock_req.call_count == 2, f"expected 2 attempts (1 fail + 1 success), got {mock_req.call_count}"
    assert resp.status_code == 200
_run("_request_with_retry() retries once on ConnectionError, then succeeds", t_retries_on_connection_error_then_succeeds)

def t_gives_up_after_max_retries():
    import requests as real_requests
    with patch("saxo_client.requests.request", side_effect=real_requests.exceptions.Timeout("slow")) as mock_req:
        with patch("saxo_client.time.sleep"):
            try:
                saxo_client._request_with_retry("GET", "https://fake/url")
                return "expected the final Timeout to be raised after exhausting retries"
            except real_requests.exceptions.Timeout:
                pass
    assert mock_req.call_count == saxo_client.MAX_RETRIES, \
        f"expected exactly MAX_RETRIES={saxo_client.MAX_RETRIES} attempts, got {mock_req.call_count}"
_run("_request_with_retry() gives up after exactly MAX_RETRIES attempts and re-raises", t_gives_up_after_max_retries)

def t_does_not_retry_on_http_error_status():
    """A 400/401 response object (not an exception) should NOT trigger the
    retry loop — retries are only for transient network errors, not for
    the server actively rejecting the request."""
    with patch("saxo_client.requests.request", return_value=_mock_response(400)) as mock_req:
        resp = saxo_client._request_with_retry("GET", "https://fake/url")
    assert mock_req.call_count == 1, "a 4xx HTTP response should not be retried like a network error"
    assert resp.status_code == 400
_run("_request_with_retry() does NOT retry on a 4xx response (only on network exceptions)", t_does_not_retry_on_http_error_status)


# ══════════════════════════════════════════════════════════════════
# 3. saxo_client.py — read endpoints raise_for_status + parsing
# ══════════════════════════════════════════════════════════════════
section("3. saxo_client.py — account/position/balance endpoints")

def t_test_connection_returns_parsed_json():
    with patch("saxo_client._headers", return_value={"Authorization": "Bearer fake"}):
        with patch("saxo_client._request_with_retry", return_value=_mock_response(200, {"UserId": "u1"})):
            result = saxo_client.test_connection()
    assert result == {"UserId": "u1"}
_run("test_connection() returns parsed JSON on success", t_test_connection_returns_parsed_json)

def t_test_connection_raises_on_401():
    with patch("saxo_client._headers", return_value={"Authorization": "Bearer fake"}):
        with patch("saxo_client._request_with_retry", return_value=_mock_response(401)):
            try:
                saxo_client.test_connection()
                return "expected HTTPError for 401 (expired/invalid token), none raised"
            except Exception:
                return True
_run("test_connection() raises on 401 (this is what Monday's expired token will produce)", t_test_connection_raises_on_401)

def t_get_balances_returns_parsed_json():
    fake = {"TotalValue": 12345.67, "CashBalance": 5000.0, "Currency": "EUR"}
    with patch("saxo_client._headers", return_value={"Authorization": "Bearer fake"}):
        with patch("saxo_client._request_with_retry", return_value=_mock_response(200, fake)):
            result = saxo_client.get_balances()
    assert result == fake
_run("get_balances() returns parsed JSON with TotalValue/CashBalance/Currency", t_get_balances_returns_parsed_json)

def t_get_positions_returns_parsed_json():
    fake = {"Data": [{"PositionBase": {"Uic": 111, "Amount": 10, "OpenPrice": 50.0}}]}
    with patch("saxo_client._headers", return_value={"Authorization": "Bearer fake"}):
        with patch("saxo_client._request_with_retry", return_value=_mock_response(200, fake)):
            result = saxo_client.get_positions()
    assert result == fake
_run("get_positions() returns parsed JSON", t_get_positions_returns_parsed_json)


# ══════════════════════════════════════════════════════════════════
# 4. saxo_client.py — get_account_key (env override + API fallback + cache)
# ══════════════════════════════════════════════════════════════════
section("4. saxo_client.py — get_account_key()")

def t_account_key_prefers_env_var():
    saxo_client._account_key_cache = None
    with patch.dict(os.environ, {"SAXO_ACCOUNT_KEY": "env_key_789"}):
        with patch("saxo_client.get_account_info") as mock_info:
            key = saxo_client.get_account_key()
    assert key == "env_key_789"
    mock_info.assert_not_called()
_run("get_account_key() uses SAXO_ACCOUNT_KEY env var without hitting the API", t_account_key_prefers_env_var)

def t_account_key_fetched_from_api_when_no_env():
    saxo_client._account_key_cache = None
    env_without_key = {k: v for k, v in os.environ.items() if k != "SAXO_ACCOUNT_KEY"}
    fake_info = {"Data": [{"AccountKey": "api_key_abc"}]}
    with patch.dict(os.environ, env_without_key, clear=True):
        with patch("saxo_client.get_account_info", return_value=fake_info):
            key = saxo_client.get_account_key()
    assert key == "api_key_abc"
    saxo_client._account_key_cache = None  # reset for other tests
_run("get_account_key() fetches from API and unwraps list-shaped Data when no env var set", t_account_key_fetched_from_api_when_no_env)

def t_account_key_cached_after_first_api_fetch():
    saxo_client._account_key_cache = None
    env_without_key = {k: v for k, v in os.environ.items() if k != "SAXO_ACCOUNT_KEY"}
    fake_info = {"Data": [{"AccountKey": "cached_key"}]}
    with patch.dict(os.environ, env_without_key, clear=True):
        with patch("saxo_client.get_account_info", return_value=fake_info) as mock_info:
            saxo_client.get_account_key()
            saxo_client.get_account_key()
            saxo_client.get_account_key()
    assert mock_info.call_count == 1, f"expected 1 API call (then cached), got {mock_info.call_count}"
    saxo_client._account_key_cache = None
_run("get_account_key() caches the AccountKey after the first API fetch", t_account_key_cached_after_first_api_fetch)

def t_account_key_raises_when_missing_from_response():
    saxo_client._account_key_cache = None
    env_without_key = {k: v for k, v in os.environ.items() if k != "SAXO_ACCOUNT_KEY"}
    fake_info = {"Data": [{"SomeOtherField": "x"}]}  # no AccountKey at all
    with patch.dict(os.environ, env_without_key, clear=True):
        with patch("saxo_client.get_account_info", return_value=fake_info):
            try:
                saxo_client.get_account_key()
                return "expected RuntimeError when AccountKey missing from response"
            except RuntimeError:
                pass
    saxo_client._account_key_cache = None
_run("get_account_key() raises RuntimeError (not silently None) when AccountKey missing from API response", t_account_key_raises_when_missing_from_response)


# ══════════════════════════════════════════════════════════════════
# 5. saxo_client.py — place_market_order (the highest-risk function)
# ══════════════════════════════════════════════════════════════════
section("5. saxo_client.py — place_market_order()")

def t_place_order_sends_correct_payload_shape():
    with patch("saxo_client.get_account_key", return_value="acct_1"):
        with patch("saxo_client._headers", return_value={"Authorization": "Bearer x"}):
            with patch("saxo_client.requests.post", return_value=_mock_response(200, {"OrderId": "999"})) as mock_post:
                result = saxo_client.place_market_order(uic=12345, asset_type="Stock", buy_sell="Buy", amount=10)
    assert result == {"OrderId": "999"}
    sent_json = mock_post.call_args.kwargs["json"]
    assert sent_json["AccountKey"] == "acct_1"
    assert sent_json["Uic"] == 12345
    assert sent_json["AssetType"] == "Stock"
    assert sent_json["BuySell"] == "Buy"
    assert sent_json["Amount"] == 10
    assert sent_json["OrderType"] == "Market"
    assert sent_json["ManualOrder"] is False, "ManualOrder must be False for algorithmic orders"
_run("place_market_order() sends the correct order payload shape to Saxo", t_place_order_sends_correct_payload_shape)

def t_place_order_400_includes_saxo_response_body():
    """Regression test for the original BUY-FAILED bug: the error message
    MUST include Saxo's actual rejection reason from the response body,
    not just requests' generic '400 Client Error'."""
    with patch("saxo_client.get_account_key", return_value="acct_1"):
        with patch("saxo_client._headers", return_value={"Authorization": "Bearer x"}):
            bad_resp = _mock_response(400, text='{"ErrorCode":"InvalidAmount","Message":"Amount too large"}')
            with patch("saxo_client.requests.post", return_value=bad_resp):
                try:
                    saxo_client.place_market_order(uic=1, asset_type="Stock", buy_sell="Buy", amount=999999999)
                    return "expected HTTPError to be raised on 400"
                except Exception as e:
                    msg = str(e)
                    assert "InvalidAmount" in msg or "Amount too large" in msg, \
                        f"error must surface Saxo's response body, got: {msg}"
                    return True
_run("place_market_order() surfaces Saxo's actual error body on 400 (the exact bug this project already hit)", t_place_order_400_includes_saxo_response_body)

def t_place_order_is_never_retried():
    """Deliberate design choice in the source: retrying an order after a
    timeout risks a duplicate fill. Verify place_market_order calls
    requests.post directly, NOT the retrying wrapper."""
    with patch("saxo_client.get_account_key", return_value="acct_1"):
        with patch("saxo_client._headers", return_value={"Authorization": "Bearer x"}):
            with patch("saxo_client._request_with_retry") as mock_retry_wrapper:
                with patch("saxo_client.requests.post", return_value=_mock_response(200, {"OrderId": "1"})):
                    saxo_client.place_market_order(uic=1, asset_type="Stock", buy_sell="Sell", amount=5)
    mock_retry_wrapper.assert_not_called()
_run("place_market_order() never uses the retry wrapper (avoids duplicate-fill risk on timeout)", t_place_order_is_never_retried)

def t_place_order_buy_and_sell_both_supported():
    with patch("saxo_client.get_account_key", return_value="acct_1"):
        with patch("saxo_client._headers", return_value={"Authorization": "Bearer x"}):
            with patch("saxo_client.requests.post", return_value=_mock_response(200, {"OrderId": "2"})) as mock_post:
                saxo_client.place_market_order(uic=1, asset_type="Stock", buy_sell="Sell", amount=3)
    assert mock_post.call_args.kwargs["json"]["BuySell"] == "Sell"
_run("place_market_order() correctly passes through 'Sell' as well as 'Buy'", t_place_order_buy_and_sell_both_supported)


# ══════════════════════════════════════════════════════════════════
# 6. saxo_live_engine.py — full daily cycle, all dependencies mocked
# ══════════════════════════════════════════════════════════════════
section("6. saxo_live_engine.py — run_cycle() orchestration")

import saxo_live_engine as engine
import config as cfg

def _make_df(cross_up=False, cross_down=False, close=100.0, low=99.0, atr=2.0):
    return pd.DataFrame([{
        "Close": close, "Low": low, "atr": atr,
        "cross_up": cross_up, "cross_down": cross_down,
    }])

def t_kill_switch_halts_cycle_before_any_api_call():
    with patch("saxo_live_engine.kill_switch_active", return_value=True):
        with patch("saxo_live_engine.saxo_client") as mock_client:
            with patch("saxo_live_engine._log") as mock_log:
                engine.run_cycle()
    mock_client.get_balances.assert_not_called()
    mock_client.get_positions.assert_not_called()
    mock_client.place_market_order.assert_not_called()
    mock_log.assert_called_once()
    assert mock_log.call_args[0][1] == "HALTED"
_run("run_cycle() exits immediately when kill switch is active — makes ZERO Saxo API calls", t_kill_switch_halts_cycle_before_any_api_call)

def t_daily_loss_cap_halts_before_any_orders():
    with patch("saxo_live_engine.kill_switch_active", return_value=False):
        with patch("saxo_live_engine.saxo_client") as mock_client:
            mock_client.get_balances.return_value = {"TotalValue": 9000, "CashBalance": 1000, "Currency": "EUR"}
            with patch("saxo_live_engine.fx.get_rate_to_sek", return_value=11.0):
                with patch("saxo_live_engine.get_day_start_equity", return_value=10000):
                    with patch("saxo_live_engine.daily_loss_cap_breached", return_value=True):
                        with patch("saxo_live_engine._log") as mock_log:
                            engine.run_cycle()
    mock_client.get_positions.assert_not_called()
    mock_client.place_market_order.assert_not_called()
    assert any(c[0][1] == "HALTED" for c in mock_log.call_args_list)
_run("run_cycle() halts on daily loss cap breach BEFORE fetching positions or placing orders", t_daily_loss_cap_halts_before_any_orders)

def t_exit_signal_places_sell_and_credits_risk_capital():
    ticker = "TEST.ST"
    uic = 111
    with patch("saxo_live_engine.kill_switch_active", return_value=False), \
         patch("saxo_live_engine.saxo_client") as mock_client, \
         patch("saxo_live_engine.fx.get_rate_to_sek", return_value=1.0), \
         patch("saxo_live_engine.get_day_start_equity", return_value=10000), \
         patch("saxo_live_engine.daily_loss_cap_breached", return_value=False), \
         patch("saxo_live_engine.get_risk_capital", return_value=10000), \
         patch("saxo_live_engine.record_fill") as mock_record_fill, \
         patch("saxo_live_engine.load_instrument_map", return_value={ticker: {"uic": uic, "currency": "SEK"}}), \
         patch("saxo_live_engine.get_latest_universe_data", return_value={ticker: pd.DataFrame()}), \
         patch("saxo_live_engine.add_indicators", return_value=_make_df(cross_down=True, close=95.0, low=94.0, atr=2.0)), \
         patch("saxo_live_engine._log") as mock_log:

        mock_client.get_balances.return_value = {"TotalValue": 10000, "CashBalance": 5000, "Currency": "SEK"}
        mock_client.get_positions.return_value = {
            "Data": [{"PositionBase": {"Uic": uic, "Amount": 20, "OpenPrice": 100.0}}]
        }
        mock_client.place_market_order.return_value = {"OrderId": "sell-1"}

        engine.run_cycle()

    mock_client.place_market_order.assert_called_once_with(uic=uic, asset_type="Stock", buy_sell="Sell", amount=20)
    mock_record_fill.assert_called_once()
    assert mock_record_fill.call_args[0][0] > 0, "SELL proceeds should be credited as a POSITIVE delta to risk capital"
_run("run_cycle() SELLS on trend-reversal exit signal and credits proceeds back to risk capital (positive delta)", t_exit_signal_places_sell_and_credits_risk_capital)

def t_stop_loss_exit_triggers_even_without_trend_break():
    ticker = "TEST2.ST"
    uic = 222
    # cross_down False, but Low <= stop_price (entry 100 - 2.5*ATR(2.0) = 95) -> Low 94 triggers stop
    with patch("saxo_live_engine.kill_switch_active", return_value=False), \
         patch("saxo_live_engine.saxo_client") as mock_client, \
         patch("saxo_live_engine.fx.get_rate_to_sek", return_value=1.0), \
         patch("saxo_live_engine.get_day_start_equity", return_value=10000), \
         patch("saxo_live_engine.daily_loss_cap_breached", return_value=False), \
         patch("saxo_live_engine.get_risk_capital", return_value=10000), \
         patch("saxo_live_engine.record_fill"), \
         patch("saxo_live_engine.load_instrument_map", return_value={ticker: {"uic": uic, "currency": "SEK"}}), \
         patch("saxo_live_engine.get_latest_universe_data", return_value={ticker: pd.DataFrame()}), \
         patch("saxo_live_engine.add_indicators", return_value=_make_df(cross_down=False, close=94.0, low=94.0, atr=2.0)), \
         patch("saxo_live_engine._log") as mock_log:

        mock_client.get_balances.return_value = {"TotalValue": 10000, "CashBalance": 5000, "Currency": "SEK"}
        mock_client.get_positions.return_value = {
            "Data": [{"PositionBase": {"Uic": uic, "Amount": 15, "OpenPrice": 100.0}}]
        }
        mock_client.place_market_order.return_value = {"OrderId": "stop-1"}

        engine.run_cycle()

    mock_client.place_market_order.assert_called_once_with(uic=uic, asset_type="Stock", buy_sell="Sell", amount=15)
    logged_reason = [c for c in mock_log.call_args_list if c[0][1] == "SELL"][0][0][4]
    assert logged_reason == "stop_loss", f"expected reason='stop_loss', got {logged_reason}"
_run("run_cycle() exits on ATR stop-loss hit even when trend hasn't reversed (cross_down=False)", t_stop_loss_exit_triggers_even_without_trend_break)

def t_failed_sell_order_is_logged_and_does_not_crash_cycle():
    ticker = "TEST3.ST"
    uic = 333
    with patch("saxo_live_engine.kill_switch_active", return_value=False), \
         patch("saxo_live_engine.saxo_client") as mock_client, \
         patch("saxo_live_engine.fx.get_rate_to_sek", return_value=1.0), \
         patch("saxo_live_engine.get_day_start_equity", return_value=10000), \
         patch("saxo_live_engine.daily_loss_cap_breached", return_value=False), \
         patch("saxo_live_engine.get_risk_capital", return_value=10000), \
         patch("saxo_live_engine.record_fill") as mock_record_fill, \
         patch("saxo_live_engine.load_instrument_map", return_value={ticker: {"uic": uic, "currency": "SEK"}}), \
         patch("saxo_live_engine.get_latest_universe_data", return_value={ticker: pd.DataFrame()}), \
         patch("saxo_live_engine.add_indicators", return_value=_make_df(cross_down=True, close=90.0, low=89.0, atr=2.0)), \
         patch("saxo_live_engine._log") as mock_log:

        mock_client.get_balances.return_value = {"TotalValue": 10000, "CashBalance": 5000, "Currency": "SEK"}
        mock_client.get_positions.return_value = {
            "Data": [{"PositionBase": {"Uic": uic, "Amount": 10, "OpenPrice": 100.0}}]
        }
        mock_client.place_market_order.side_effect = Exception("Saxo 401 Unauthorized (expired token)")

        engine.run_cycle()  # must NOT raise — should catch and log

    mock_record_fill.assert_not_called(), "risk capital must not be credited for a FAILED sell"
    assert any(c[0][1] == "SELL-FAILED" for c in mock_log.call_args_list)
_run("run_cycle() catches a failed SELL (e.g. expired token) — logs SELL-FAILED, doesn't crash, doesn't credit capital", t_failed_sell_order_is_logged_and_does_not_crash_cycle)

def t_new_entry_buy_debits_risk_capital():
    ticker = "TEST4.ST"
    uic = 444
    with patch("saxo_live_engine.kill_switch_active", return_value=False), \
         patch("saxo_live_engine.saxo_client") as mock_client, \
         patch("saxo_live_engine.fx.get_rate_to_sek", return_value=1.0), \
         patch("saxo_live_engine.get_day_start_equity", return_value=10000), \
         patch("saxo_live_engine.daily_loss_cap_breached", return_value=False), \
         patch("saxo_live_engine.get_risk_capital", return_value=10000), \
         patch("saxo_live_engine.record_fill", return_value=9500) as mock_record_fill, \
         patch("saxo_live_engine.load_instrument_map", return_value={ticker: {"uic": uic, "currency": "SEK"}}), \
         patch("saxo_live_engine.get_latest_universe_data", return_value={ticker: pd.DataFrame()}), \
         patch("saxo_live_engine.add_indicators", return_value=_make_df(cross_up=True, close=100.0, atr=2.0)), \
         patch("saxo_live_engine._log") as mock_log:

        mock_client.get_balances.return_value = {"TotalValue": 10000, "CashBalance": 5000, "Currency": "SEK"}
        mock_client.get_positions.return_value = {"Data": []}  # no open positions
        mock_client.place_market_order.return_value = {"OrderId": "buy-1"}

        engine.run_cycle()

    mock_client.place_market_order.assert_called_once()
    call_kwargs = mock_client.place_market_order.call_args.kwargs
    assert call_kwargs["buy_sell"] == "Buy"
    assert call_kwargs["uic"] == uic
    mock_record_fill.assert_called_once()
    assert mock_record_fill.call_args[0][0] < 0, "BUY cost should be debited as a NEGATIVE delta to risk capital"
_run("run_cycle() BUYS on cross_up entry signal and debits cost from risk capital (negative delta)", t_new_entry_buy_debits_risk_capital)

def t_entry_blocked_when_cash_insufficient():
    """This is the exact bug class this project already hit: sizing must
    never place an order Saxo can't afford to fill."""
    ticker = "TEST5.ST"
    uic = 555
    with patch("saxo_live_engine.kill_switch_active", return_value=False), \
         patch("saxo_live_engine.saxo_client") as mock_client, \
         patch("saxo_live_engine.fx.get_rate_to_sek", return_value=1.0), \
         patch("saxo_live_engine.get_day_start_equity", return_value=10000), \
         patch("saxo_live_engine.daily_loss_cap_breached", return_value=False), \
         patch("saxo_live_engine.get_risk_capital", return_value=1_000_000), \
         patch("saxo_live_engine.record_fill") as mock_record_fill, \
         patch("saxo_live_engine.load_instrument_map", return_value={ticker: {"uic": uic, "currency": "SEK"}}), \
         patch("saxo_live_engine.get_latest_universe_data", return_value={ticker: pd.DataFrame()}), \
         patch("saxo_live_engine.add_indicators", return_value=_make_df(cross_up=True, close=100.0, atr=2.0)), \
         patch("saxo_live_engine._log") as mock_log:

        # Huge risk_capital -> position_size() wants a lot of shares, but
        # cash_available is tiny -> must be BLOCKED, not sent to Saxo anyway.
        mock_client.get_balances.return_value = {"TotalValue": 10000, "CashBalance": 10, "Currency": "SEK"}
        mock_client.get_positions.return_value = {"Data": []}

        engine.run_cycle()

    mock_client.place_market_order.assert_not_called()
    mock_record_fill.assert_not_called()
    assert any(c[0][1] == "BUY-BLOCKED" for c in mock_log.call_args_list)
_run("run_cycle() BLOCKS an entry (never calls Saxo) when cost estimate exceeds cash available", t_entry_blocked_when_cash_insufficient)

def t_max_open_positions_stops_new_entries():
    tickers = {f"T{i}.ST": {"uic": i, "currency": "SEK"} for i in range(cfg.MAX_OPEN_POSITIONS + 2)}
    # Simulate already having MAX_OPEN_POSITIONS open positions
    open_positions_data = [
        {"PositionBase": {"Uic": i, "Amount": 1, "OpenPrice": 100.0}}
        for i in range(cfg.MAX_OPEN_POSITIONS)
    ]
    with patch("saxo_live_engine.kill_switch_active", return_value=False), \
         patch("saxo_live_engine.saxo_client") as mock_client, \
         patch("saxo_live_engine.fx.get_rate_to_sek", return_value=1.0), \
         patch("saxo_live_engine.get_day_start_equity", return_value=10000), \
         patch("saxo_live_engine.daily_loss_cap_breached", return_value=False), \
         patch("saxo_live_engine.get_risk_capital", return_value=10000), \
         patch("saxo_live_engine.record_fill"), \
         patch("saxo_live_engine.load_instrument_map", return_value=tickers), \
         patch("saxo_live_engine.get_latest_universe_data",
               return_value={t: pd.DataFrame() for t in tickers}), \
         patch("saxo_live_engine.add_indicators", return_value=_make_df(cross_up=False)), \
         patch("saxo_live_engine._log") as mock_log:

        mock_client.get_balances.return_value = {"TotalValue": 10000, "CashBalance": 5000, "Currency": "SEK"}
        mock_client.get_positions.return_value = {"Data": open_positions_data}

        engine.run_cycle()

    mock_client.place_market_order.assert_not_called()
_run(f"run_cycle() skips ALL new entries once open positions == MAX_OPEN_POSITIONS ({cfg.MAX_OPEN_POSITIONS})", t_max_open_positions_stops_new_entries)

def t_empty_dataframe_for_a_ticker_is_skipped_not_crashed():
    ticker = "EMPTY.ST"
    uic = 666
    with patch("saxo_live_engine.kill_switch_active", return_value=False), \
         patch("saxo_live_engine.saxo_client") as mock_client, \
         patch("saxo_live_engine.fx.get_rate_to_sek", return_value=1.0), \
         patch("saxo_live_engine.get_day_start_equity", return_value=10000), \
         patch("saxo_live_engine.daily_loss_cap_breached", return_value=False), \
         patch("saxo_live_engine.get_risk_capital", return_value=10000), \
         patch("saxo_live_engine.record_fill"), \
         patch("saxo_live_engine.load_instrument_map", return_value={ticker: {"uic": uic, "currency": "SEK"}}), \
         patch("saxo_live_engine.get_latest_universe_data", return_value={ticker: pd.DataFrame()}), \
         patch("saxo_live_engine.add_indicators", return_value=pd.DataFrame()), \
         patch("saxo_live_engine._log"):

        mock_client.get_balances.return_value = {"TotalValue": 10000, "CashBalance": 5000, "Currency": "SEK"}
        mock_client.get_positions.return_value = {"Data": []}

        engine.run_cycle()  # must not raise on empty df

    mock_client.place_market_order.assert_not_called()
_run("run_cycle() skips a ticker with an empty DataFrame (e.g. bad data fetch) instead of crashing", t_empty_dataframe_for_a_ticker_is_skipped_not_crashed)


# ══════════════════════════════════════════════════════════════════
print(f"\n{BOLD}{'='*60}{RESET}\n{BOLD}  SAXO CLIENT + LIVE ENGINE BLACK BOX RESULTS{RESET}\n{BOLD}{'='*60}{RESET}")
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
    print(f"  {GREEN}Failed: 0{RESET}")
    print(f"\n{GREEN}{BOLD}ALL TESTS PASSED{RESET}\n{BOLD}{'='*60}{RESET}")
    sys.exit(0)
