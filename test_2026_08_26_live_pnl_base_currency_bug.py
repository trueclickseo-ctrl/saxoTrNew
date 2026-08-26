"""
Regression tests — 2026-08-26 LIVE forex P&L base-currency bug.

Real-money incident: the live_eur account's first closed trade (EURPLN,
RSI Pullback, rsi_recovery exit) showed as a WIN (+18 PLN / +0.41%) in
ATOS's own instant email, but Saxo's own web trader recorded a real net
loss of -1.29 EUR once the ~5 EUR round-trip commission was included.
The local ledger (data/pnl_ledger.db) stored realized_pnl=-14.19 for the
same trade — direction correct (a loss) but magnitude wrong by ~11x.

Root cause, confirmed against Saxo's live API (GET /port/v1/closedpositions/me
under env="live_eur"): forex/runner.py's old _position_pnl_base_ccy() read
PositionView.ProfitLossOnTradeInBaseCurrency + TradeCostsTotalInBaseCurrency
and stored that number directly as EUR. Those "...InBaseCurrency" fields are
NOT denominated in the live_eur sub-account's own currency (EUR) -- they use
Saxo's Client-level base currency, SEK, regardless of which AccountKey the
request runs under (this Saxo login's primary sub-account is SEK). A real
-1.29 EUR loss came back as -14.19 in that field -- almost exactly the
EUR/SEK rate, because it was SEK being read as if it were EUR.

Fix: forex/runner.py's _position_net_pnl_quote_ccy() now reads the
quote-currency fields (ProfitLossOnTrade + TradeCostsTotal, e.g. PLN for
EURPLN) instead, and the caller converts to EUR itself via the codebase's
own already-correct _eur_per_unit() (Saxo-quote-based). The instant email
(forex/notifier.py's send_trade_closed()) now takes this same net figure
via net_pnl_native and bases its WIN/LOSS badge on it, instead of on raw
price movement alone. The [LIVE] email tag also now covers live_eur, which
it previously did not (ACCOUNT_ENV == "live" excluded "live_eur").
"""

import os
import sys
from unittest.mock import patch

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
section("1. forex/runner.py — quote-currency fields, not '...InBaseCurrency'")
# ═══════════════════════════════════════════════════════════════════════

def test_position_net_pnl_reads_quote_ccy_fields_not_base_currency_ones():
    import forex.runner as r
    fake_resp = {"Data": [{
        "PositionBase": {"Uic": 1343, "AssetType": "FxSpot", "Amount": 1000.0,
                          "OpenPrice": 4.295985},
        "PositionView": {
            "ProfitLossOnTrade": 16.74,               # real PLN gain
            "ProfitLossOnTradeInBaseCurrency": 42.998,  # SEK -- must NOT be used
            "TradeCostsTotal": -22.16,                # real PLN cost
            "TradeCostsTotalInBaseCurrency": -57.18,    # SEK -- must NOT be used
        },
    }]}
    with patch.object(r, "_get", return_value=fake_resp):
        net = r._position_net_pnl_quote_ccy(1343, 1000.0, "Buy", 4.295985)
    assert net is not None
    # Must be the quote-currency (PLN) net figure: 16.74 - 22.16 = -5.42
    assert abs(net - (-5.42)) < 1e-6, f"expected quote-ccy net -5.42, got {net}"
_run("forex/runner: _position_net_pnl_quote_ccy() uses ProfitLossOnTrade/"
     "TradeCostsTotal (quote ccy), never the '...InBaseCurrency' variants",
     test_position_net_pnl_reads_quote_ccy_fields_not_base_currency_ones)


def test_position_net_pnl_matches_closest_open_price_when_multiple_candidates():
    import forex.runner as r
    fake_resp = {"Data": [
        {"PositionBase": {"Uic": 1343, "AssetType": "FxSpot", "Amount": 1000.0,
                           "OpenPrice": 4.10000},
         "PositionView": {"ProfitLossOnTrade": 999.0, "TradeCostsTotal": 0.0}},
        {"PositionBase": {"Uic": 1343, "AssetType": "FxSpot", "Amount": 1000.0,
                           "OpenPrice": 4.29600},
         "PositionView": {"ProfitLossOnTrade": 16.74, "TradeCostsTotal": -22.16}},
    ]}
    with patch.object(r, "_get", return_value=fake_resp):
        net = r._position_net_pnl_quote_ccy(1343, 1000.0, "Buy", 4.295985)
    assert abs(net - (-5.42)) < 1e-6, (
        f"must match the position whose OpenPrice is closest to our entry_price, got {net}")
_run("forex/runner: _position_net_pnl_quote_ccy() picks the closest-OpenPrice "
     "match when multiple same-UIC positions are pooled in the response",
     test_position_net_pnl_matches_closest_open_price_when_multiple_candidates)


def test_position_net_pnl_returns_none_on_lookup_failure():
    import forex.runner as r
    with patch.object(r, "_get", side_effect=Exception("network")):
        net = r._position_net_pnl_quote_ccy(1343, 1000.0, "Buy", 4.295985)
    assert net is None
_run("forex/runner: _position_net_pnl_quote_ccy() returns None (never guesses) "
     "on a lookup failure", test_position_net_pnl_returns_none_on_lookup_failure)


def test_old_base_currency_function_name_is_gone():
    import forex.runner as r
    assert not hasattr(r, "_position_pnl_base_ccy"), (
        "the old ambiguous-currency function must be fully replaced, not left "
        "around alongside the fixed one where something could still call it")
_run("forex/runner: old _position_pnl_base_ccy() (ambiguous currency) no "
     "longer exists", test_old_base_currency_function_name_is_gone)


# ═══════════════════════════════════════════════════════════════════════
section("2. forex/runner.py — the exit path converts quote ccy -> EUR itself")
# ═══════════════════════════════════════════════════════════════════════

def test_run_exits_source_converts_via_eur_per_unit_not_saxo_base_currency():
    import inspect
    import forex.runner as r
    src = inspect.getsource(r)
    assert "net_pnl_quote * fx_rate" in src, (
        "the EUR figure stored in the ledger must be net_pnl_quote (quote ccy) "
        "explicitly multiplied by our own _eur_per_unit()-derived fx_rate -- "
        "not a Saxo '...InBaseCurrency' field taken at face value")
    assert "gross_pnl_base_override=saxo_pnl_eur" in src
_run("forex/runner: the ledger's EUR figure is net_pnl_quote converted via "
     "our own fx_rate, not trusted directly from Saxo",
     test_run_exits_source_converts_via_eur_per_unit_not_saxo_base_currency)


def test_ml_label_outcome_prefers_net_pnl_over_raw_price_pnl():
    import inspect
    import forex.runner as r
    src = inspect.getsource(r)
    assert "won_for_ml = (net_pnl_quote > 0) if net_pnl_quote is not None else (raw_pnl > 0)" in src, (
        "ML training labels must prefer the true net (price+cost) outcome when "
        "available, so a signal that lost to broker cost isn't labeled a win")
_run("forex/runner: signal_filter ML label uses net P&L (price+cost) when "
     "available, not raw price movement alone",
     test_ml_label_outcome_prefers_net_pnl_over_raw_price_pnl)


# ═══════════════════════════════════════════════════════════════════════
section("3. forex/notifier.py — email WIN/LOSS reflects net P&L, LIVE tag covers live_eur")
# ═══════════════════════════════════════════════════════════════════════

def test_send_trade_closed_win_loss_uses_net_pnl_when_provided():
    import forex.notifier as n
    captured = {}
    with patch.object(n, "_send", side_effect=lambda subj, body: captured.update(subject=subj, body=body)):
        n.send_trade_closed(
            strategy="rsi", symbol="EURPLN", direction="Buy",
            entry=4.295985, exit_px=4.3138, pnl_pct=0.41, units=1000,
            reason="rsi_recovery (72.6>=55)", live=True,
            net_pnl_native=-5.42,   # real net loss in PLN, despite positive pnl_pct
        )
    assert "LOSS" in captured["subject"], (
        f"a real net loss must show LOSS in the subject even though raw pnl_pct "
        f"was positive -- got: {captured['subject']}")
    assert "WIN" not in captured["subject"] or "LOSS" in captured["subject"]
_run("forex/notifier: send_trade_closed() bases WIN/LOSS on net_pnl_native "
     "(price+cost) when provided, not on raw pnl_pct alone",
     test_send_trade_closed_win_loss_uses_net_pnl_when_provided)


def test_send_trade_closed_falls_back_to_pnl_pct_without_net_figure():
    import forex.notifier as n
    captured = {}
    with patch.object(n, "_send", side_effect=lambda subj, body: captured.update(subject=subj, body=body)):
        n.send_trade_closed(
            strategy="ema", symbol="EURUSD", direction="Buy",
            entry=1.0800, exit_px=1.0850, pnl_pct=0.46, units=1000,
            reason="ema_cross", live=False,
        )
    assert "WIN" in captured["subject"], (
        "without a net_pnl_native override, a positive pnl_pct must still show WIN "
        "(backward-compatible default for every existing caller)")
_run("forex/notifier: send_trade_closed() without net_pnl_native behaves "
     "exactly as before (raw pnl_pct drives WIN/LOSS)",
     test_send_trade_closed_falls_back_to_pnl_pct_without_net_figure)


def test_send_trade_closed_live_tag_present_when_live_true():
    import forex.notifier as n
    captured = {}
    with patch.object(n, "_send", side_effect=lambda subj, body: captured.update(subject=subj, body=body)):
        n.send_trade_closed(
            strategy="rsi", symbol="EURPLN", direction="Buy",
            entry=4.295985, exit_px=4.3138, pnl_pct=0.41, units=1000,
            reason="rsi_recovery", live=True,
        )
    assert captured["subject"].startswith("[LIVE] "), (
        f"live=True must tag the subject with [LIVE] -- got: {captured['subject']}")
_run("forex/notifier: send_trade_closed(live=True) tags the email [LIVE]",
     test_send_trade_closed_live_tag_present_when_live_true)


def test_run_exits_source_tags_live_eur_as_live_too():
    import inspect
    import forex.runner as r
    src = inspect.getsource(r._run_exits)
    assert 'live=(ACCOUNT_ENV in ("live", "live_eur"))' in src, (
        "the live_eur account's trade-closed emails must be tagged [LIVE] too -- "
        "the old ACCOUNT_ENV == \"live\" check silently excluded live_eur, which "
        "is exactly what the user reported: a real live_eur trade's email did not "
        "show it was a LIVE trade")
_run("forex/runner: _run_exits() tags live_eur trade-closed emails as LIVE, "
     "not just the SEK 'live' account",
     test_run_exits_source_tags_live_eur_as_live_too)


print(f"\n{BOLD}{'='*70}{RESET}")
passed = sum(1 for _, ok, _ in _results)
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
