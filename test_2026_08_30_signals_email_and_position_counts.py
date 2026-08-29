"""
Regression tests -- 2026-08-30.

(1) LIVE signals email: forex/notifier.send_signals_detected renders for
    both the normal case and the FX-weekend-closed case, and the runner
    calls it on live/live_eur only.
(2) Position-count vs pair-count reconciliation: the run-summary email and
    the forex dashboard header now BOTH show open-position count AND
    distinct-pair count, side by side, so "119" (email) and "81/184"
    (dashboard) no longer look contradictory. Also de-hardcodes the email
    subtitle (was a stale literal "9 Strategies . 34 Pairs . Saxo SIM").
"""

import inspect
import os
import re
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

GREEN, RED, YELLOW, CYAN, RESET, BOLD = (
    "\033[92m", "\033[91m", "\033[93m", "\033[96m", "\033[0m", "\033[1m"
)
_results = []


def _run(name, fn):
    try:
        fn()
        _results.append((name, True, None))
    except Exception as e:
        _results.append((name, False, f"{type(e).__name__}: {e}"))


def _capture_sends(mod):
    sent = []
    mod._send = lambda subj, html: sent.append((subj, html))
    return sent


def test_signals_email_renders_normal_and_closed():
    import forex.notifier as n
    sent = _capture_sends(n)
    n.send_signals_detected(
        "rsi",
        [{"symbol": "GBPUSD", "direction": "Buy",  "rsi": 7.2,  "stop_price": 1.3438},
         {"symbol": "USDMXN", "direction": "Sell", "rsi": 96.7, "stop_price": 17.16}],
        entered=["GBPUSD"], account_env="live_eur", market_closed=False)
    n.send_signals_detected(
        "rsi",
        [{"symbol": "EURUSD", "direction": "Buy", "rsi": 6.1, "stop_price": 1.151}],
        entered=[], account_env="live", market_closed=True)
    assert len(sent) == 2
    s0, h0 = sent[0]
    assert "RSI" in s0 and "2 signal(s)" in s0 and "1 entered" in s0
    assert "GBPUSD" in h0 and "USDMXN" in h0 and "ENTERED" in h0
    s1, h1 = sent[1]
    assert "market closed" in s1.lower()
    assert "FX MARKET CLOSED" in h1 and "EURUSD" in h1
    # empty signal list -> no email
    sent.clear()
    n.send_signals_detected("rsi", [], entered=[], account_env="live", market_closed=True)
    assert sent == []
_run("notifier.send_signals_detected renders (normal + weekend-closed), skips empty", test_signals_email_renders_normal_and_closed)


def test_runner_calls_signals_email_live_only():
    import forex.runner as r
    src = inspect.getsource(r._run_entries)
    calls = [m.start() for m in re.finditer(r"fx_notify\.send_signals_detected\(", src)]
    assert len(calls) == 2, f"expected 2 call sites, found {len(calls)}"
    # normal (open-market) call site is guarded on live/live_eur
    assert 'if ACCOUNT_ENV in ("live", "live_eur") and signals and not dry_run:' in src
    # weekend call site sits inside the _weekend_entry_block branch, which is
    # itself live/live_eur-only (asserted in the market-hours-gate test)
    wk = src[src.index("if _weekend_entry_block:"):]
    assert "send_signals_detected" in wk[:wk.index("return 0")]
_run("forex/runner: send_signals_detected is called on live/live_eur only", test_runner_calls_signals_email_live_only)


def test_run_summary_shows_positions_and_pairs():
    import forex.notifier as n
    sent = _capture_sends(n)
    n.send_run_summary(session="all", entries=4, exits=0, holdings=119, equity=27800,
                       today_trades=[], strategy_stats=[], pairs_trading=81,
                       strategy_count=9, pair_count=184, venue="Saxo SIM")
    subj, html = sent[0]
    # subject carries BOTH numbers
    assert "119 pos" in subj and "81 pairs" in subj
    # body metric relabelled and shows the pair count
    assert "Positions" in html and ">119<" in html and "in 81 pairs" in html
    # subtitle is now the real counts, not the old hardcoded literal
    assert "9 Strategies" in html and "184 Pairs" in html and "Saxo SIM" in html
    assert "34 Pairs" not in html
_run("notifier.send_run_summary: subject + body show positions AND pairs; subtitle de-hardcoded", test_run_summary_shows_positions_and_pairs)


def test_dashboard_header_shows_both_counts():
    import forex_dashboard as d
    src = inspect.getsource(d._render)
    # the header f-string now prints both the position count and pairs/total
    assert "{len(positions)} positions in {pairs_trading}/{_total_pairs} pairs" in src
    # the old ambiguous "N/total trading now" phrasing is gone from real output
    assert "trading now'" not in src and "trading now  |" not in src
_run("forex_dashboard: header shows '<N> positions in <P>/<total> pairs'", test_dashboard_header_shows_both_counts)


def test_wrap_subtitle_is_parameterised():
    import forex.notifier as n
    src = inspect.getsource(n._wrap)
    assert "subtitle: str | None = None" in src
    # the sub div renders the parameter, not a hardcoded strategy/pair string
    assert '<div class="sub">{sub}' in src
_run("notifier._wrap: subtitle is a parameter, stale hardcoded literal removed", test_wrap_subtitle_is_parameterised)


print(f"\n{BOLD}{'='*70}{RESET}")
passed = sum(1 for _, ok, _ in _results)
failed = [(nm, e) for nm, ok, e in _results if not ok]
for nm, ok, err in _results:
    icon = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
    print(f"  [{icon}] {nm}")
    if err:
        print(f"         {YELLOW}{err}{RESET}")
print(f"{BOLD}{'='*70}{RESET}")
if failed:
    print(f"{RED}{BOLD}  {len(failed)} / {len(_results)} TESTS FAILED{RESET}")
    sys.exit(1)
else:
    print(f"{GREEN}{BOLD}  ALL {len(_results)} TESTS PASSED{RESET}")
    sys.exit(0)
