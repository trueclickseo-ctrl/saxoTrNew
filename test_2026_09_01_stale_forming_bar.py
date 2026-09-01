"""
Regression test -- 2026-09-01 stale forming-bar / stale-price entry guard.

Root cause of the NZDPLN re-entry loop: Saxo's SIM /chart/v3/charts daily
feed left the still-forming last bar frozen far from the live tradable
quote on a thin instrument. Confirmed: the NZDPLN 2026-08-31 bar had
OpenAsk 2.2481 / OpenBid 2.2078 (a 1.8% spread), giving a mid Open of
exactly 2.22795 -- ~1% above the real market. rsi/pullback then entered
at 2.22795 every scan, the position was born underwater vs the live
quote, hit its hard_stop (2.19931) straight away, and -- the chart still
frozen -- the same signal re-fired 30 minutes later. 23 fictional losses.

Fixes in forex/runner.py:
  * _fetch_history: the Ask/Bid spread sanity check now covers Open/High/
    Low too, not just Close -> that bar falls back to Bid-only OHLC.
  * _repair_stale_forming_bars: overwrite the forming bar's Close with the
    live mid (clamping High/Low) when they diverge > _STALE_FORMING_BAR_TOL
    (0.4%). Run in run_daily and run_exits_only.
  * _run_entries: a final guard -- skip any signal whose `close` still
    disagrees with the live quote by > the tolerance.
"""

import ast
import inspect
import os
import sys

import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

G, R, Y, X, B = "\033[92m", "\033[91m", "\033[93m", "\033[0m", "\033[1m"
_res = []


def _run(n, f):
    try:
        f()
        _res.append((n, True, None))
    except Exception as e:
        import traceback
        _res.append((n, False, f"{type(e).__name__}: {e}\n{traceback.format_exc()}"))


import forex.runner as fr


# ── _fetch_history: spread check covers all OHLC ──────────────────────
def test_fetch_history_checks_open_high_low_on_the_forming_bar_only():
    src = inspect.getsource(fr._fetch_history)
    assert "_is_last" in src and "OpenAsk" in src

    _normal = {"OpenAsk": 1.1002, "OpenBid": 1.1000, "HighAsk": 1.1012, "HighBid": 1.1010,
               "LowAsk": 1.0992, "LowBid": 1.0990, "CloseAsk": 1.1006, "CloseBid": 1.1004}
    # a COMPLETED bar with a wide intraday High spread -- must be LEFT as mid
    _wide_hist = {**_normal, "HighAsk": 1.140, "HighBid": 1.1010}
    # the FORMING (last) bar with a bogus wide Ask on the Open -> Bid-only
    _wide_last = {**_normal, "OpenAsk": 1.130, "OpenBid": 1.1000}
    data = [_normal, _wide_hist] * 2 + [_normal, _wide_last]

    o = fr._get
    fr._get = lambda path, params=None: type("R", (), {"get": staticmethod(
        lambda k, d=None: data if k == "Data" else d)})()
    try:
        df = fr._fetch_history(999)
        # completed wide-High bar kept its mid High (~1.12), NOT clamped to Bid
        assert any(h > 1.115 for h in df["High"][:-1]), list(df["High"])
        # forming bar's Open used Bid (1.1000), not mid (~1.115)
        assert df["Open"].iloc[-1] < 1.105, df["Open"].iloc[-1]
    finally:
        fr._get = o


# ── _repair_stale_forming_bars ───────────────────────────────────────
def _df(close, hi=None, lo=None):
    return pd.DataFrame([{"Open": close, "High": hi or close + 0.01,
                          "Low": lo or close - 0.01, "Close": close}])


def test_repair_overwrites_a_stale_forming_bar():
    md = {"NZDPLN": _df(2.22795, hi=2.229, lo=2.226)}
    n = fr._repair_stale_forming_bars(md, {"NZDPLN": 2.2040})
    assert n == 1
    row = md["NZDPLN"].iloc[-1]
    assert abs(row["Close"] - 2.2040) < 1e-9
    assert row["Low"] <= 2.2040 <= row["High"]      # clamped consistent


def test_repair_leaves_a_fresh_bar_alone():
    md = {"EURUSD": _df(1.16050)}
    assert fr._repair_stale_forming_bars(md, {"EURUSD": 1.16060}) == 0   # 0.009% -> fine
    assert md["EURUSD"]["Close"].iloc[-1] == 1.16050


def test_repair_ignores_missing_or_none():
    md = {"X": None, "Y": _df(1.0), "Z": pd.DataFrame()}
    assert fr._repair_stale_forming_bars(md, {}) == 0          # no live prices
    assert fr._repair_stale_forming_bars(md, {"X": 1.0, "Z": 1.0}) == 0


def test_tolerance_is_tight_enough_to_catch_the_incident():
    # NZDPLN was ~1.09% off -- must trip a 0.4% tol comfortably
    assert fr._STALE_FORMING_BAR_TOL <= 0.005
    md = {"NZDPLN": _df(2.22795)}
    assert fr._repair_stale_forming_bars(md, {"NZDPLN": 2.20400}) == 1


# ── wiring: both cycles repair; _run_entries has the guard ────────────
def test_run_daily_and_run_exits_only_repair():
    for fn in (fr.run_daily, fr.run_exits_only):
        src = inspect.getsource(fn)
        assert "_repair_stale_forming_bars(" in src, fn.__name__
    # run_daily now always fetches live prices (was gap-strategy-only)
    rd = inspect.getsource(fr.run_daily)
    assert "_fetch_live_prices(active_pairs)" in rd
    assert "if needs_live" not in rd                # the old gate is gone


def test_run_entries_skips_a_signal_that_is_still_off_the_live_quote():
    src = inspect.getsource(fr._run_entries)
    assert "_STALE_FORMING_BAR_TOL" in src
    i_guard = src.index("stale chart bar")
    i_filter = src.index("signal_filter.evaluate(")
    assert i_guard < i_filter                       # guard runs before anything else
    assert "continue" in src[src.index("Stale-price guard"):i_filter]


def test_module_parses():
    ast.parse(inspect.getsource(fr))


for _n, _f in list(globals().items()):
    if _n.startswith("test_") and callable(_f):
        _run(_n, _f)

print(f"\n{B}{'=' * 66}{X}")
bad = [(n, e) for n, ok, e in _res if not ok]
for n, ok, e in _res:
    print(f"  [{G}PASS{X}]" if ok else f"  [{R}FAIL{X}]", n)
    if e:
        print(f"      {Y}{e}{X}")
print(f"{B}{'=' * 66}{X}")
if bad:
    print(f"{R}{B}  {len(bad)} / {len(_res)} FAILED{X}")
    sys.exit(1)
print(f"{G}{B}  ALL {len(_res)} TESTS PASSED{X}")
sys.exit(0)
