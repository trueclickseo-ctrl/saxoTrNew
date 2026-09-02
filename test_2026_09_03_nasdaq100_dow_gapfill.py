"""
2026-09-03 -- US equity universe extended to span all 3 major US indices.

User: "add the missing nasdaq-100 names plus DOW". atos/universe.py gains
NASDAQ100_DOW_TICKERS (17 names) folded into US_TICKERS. Verifies the list,
its instrument-map resolution, the deliberate exclusions (GOOG dual-class,
EA gone private), full Dow-30 coverage, and no dupes.
"""

import csv
import os
import sys

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


EXPECTED = {"ADP", "CTAS", "MNST", "ROP", "PCAR", "PAYX", "FAST", "ODFL", "CPRT",
            "CSGP", "VRSK", "CCEP", "FANG", "CDW", "GFS", "TRI", "DOW"}


def test_list_is_exactly_the_17_expected():
    from atos.universe import NASDAQ100_DOW_TICKERS
    assert set(NASDAQ100_DOW_TICKERS) == EXPECTED, set(NASDAQ100_DOW_TICKERS) ^ EXPECTED
    assert len(NASDAQ100_DOW_TICKERS) == 17            # no dupes within the list


def test_all_in_us_tickers_and_market_group():
    from atos.universe import US_TICKERS, MARKET_GROUPS, market_of
    for t in EXPECTED:
        assert t in US_TICKERS
        assert t in MARKET_GROUPS["US Equities"]
        assert market_of(t) == "US Equities"


def test_no_duplicate_in_us_tickers():
    from atos.universe import US_TICKERS
    assert len(US_TICKERS) == len(set(US_TICKERS))


def test_deliberate_exclusions():
    from atos.universe import US_TICKERS, NASDAQ100_DOW_TICKERS
    # GOOG: dual-class dup of GOOGL (which IS carried) -- never add the B/C share
    assert "GOOG" not in NASDAQ100_DOW_TICKERS
    assert "GOOGL" in US_TICKERS
    # EA: went private 2026-08-04 ($55B LBO) -- delisted, not a Nasdaq-100 member
    assert "EA" not in US_TICKERS


def test_full_dow30_now_covered():
    from atos.universe import US_TICKERS
    dow30 = {"AAPL", "AMGN", "AXP", "BA", "CAT", "CRM", "CSCO", "CVX", "DIS", "DOW",
             "GS", "HD", "HON", "IBM", "JNJ", "JPM", "KO", "MCD", "MMM", "MRK",
             "MSFT", "NKE", "NVDA", "PG", "SHW", "TRV", "UNH", "V", "VZ", "WMT"}
    missing = dow30 - set(US_TICKERS)
    assert not missing, f"Dow-30 names still missing: {missing}"


def _map():
    with open(os.path.join(BASE, "data", "instrument_map.csv"), encoding="utf-8") as f:
        return {r["yahoo_ticker"]: r for r in csv.DictReader(f)}


def test_every_new_ticker_resolved_usd_us_exchange_no_review():
    m = _map()
    bad = []
    for t in EXPECTED:
        row = m.get(t)
        if (row is None or not row.get("uic")
                or (row.get("currency") or "").upper() != "USD"
                or row.get("needs_review")):
            bad.append((t, row))
    assert not bad, f"unclean instrument-map rows: {bad}"


def test_dow_is_nyse_the_rest_nasdaq():
    m = _map()
    assert m["DOW"]["exchange"] in ("NYSE", "XNYS", "NYSE American")
    for t in EXPECTED - {"DOW"}:
        assert m[t]["exchange"] in ("NASDAQ", "XNAS"), (t, m[t]["exchange"])


def test_no_duplicate_rows_in_instrument_map():
    with open(os.path.join(BASE, "data", "instrument_map.csv"), encoding="utf-8") as f:
        rows = [r["yahoo_ticker"] for r in csv.DictReader(f)]
    dupes = {t for t in EXPECTED if rows.count(t) > 1}
    assert not dupes, f"duplicate instrument_map rows for: {dupes}"


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
