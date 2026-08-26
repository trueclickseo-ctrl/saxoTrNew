"""
Regression test — 2026-08-26 CADCHF/CHFAUD UIC mismatch.

Root cause: forex/universe.py's uic=7 entry was hand-guessed as "CADCHF"
("sequential gap between CADJPY=6 and CHFJPY=8") and never verified against
Saxo's own reference data. Confirmed live via
GET /ref/v1/instruments/details/7/FxSpot: uic=7 is actually "CHFAUD"
(Swiss Franc/Australian Dollar, CurrencyCode=AUD), not "CADCHF"
(Canadian Dollar/Swiss Franc). A full 149-pair audit against Saxo's live
reference data found this to be the ONLY mismatch in the universe.

User found this by noticing a LIVE-SEK account "CHFAUD" order in Saxo's own
web trader that looked like a duplicate of an open AUDCHF position -- it
wasn't a duplicate at all, it was ATOS's own "CADCHF" position, mislabeled.
The wrong label corrupted every quote-currency-derived calculation for this
pair (EUR conversion via _eur_per_unit(sym[3:6], ...) used "CHF" instead of
the real "AUD"), and this pair's CORE_SYMBOLS/SESSION_PAIRS membership
determined it was eligible for LIVE-SEK trading in the first place.

Fixed: universe.py's uic=7 entry, CORE_SYMBOLS, forex/runner.py's
SESSION_PAIRS["london"], forex/strategy_london_breakout.py's PAIRS, plus
renamed every "CADCHF" reference in local state (forex_state.json,
forex_live_state.json) and pnl_ledger.db to "CHFAUD".
"""

import os
import sys

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


def test_uic_7_is_chfaud_not_cadchf():
    from forex.universe import PAIRS
    p = next(x for x in PAIRS if x["uic"] == 7)
    assert p["symbol"] == "CHFAUD", f"uic=7 must be CHFAUD, got {p['symbol']}"
    assert p["base"] == "CHF" and p["quote"] == "AUD"
_run("forex.universe: uic=7 is labeled CHFAUD (base=CHF, quote=AUD), not "
     "the wrong CADCHF", test_uic_7_is_chfaud_not_cadchf)


def test_cadchf_no_longer_in_universe():
    from forex.universe import PAIRS
    syms = {p["symbol"] for p in PAIRS}
    assert "CADCHF" not in syms, "the wrong symbol must not linger anywhere in PAIRS"
    assert "CHFAUD" in syms
_run("forex.universe: CADCHF fully replaced by CHFAUD, not left as a "
     "second (also wrong) entry", test_cadchf_no_longer_in_universe)


def test_chfaud_in_core_symbols_not_cadchf():
    from forex.universe import CORE_SYMBOLS
    assert "CHFAUD" in CORE_SYMBOLS
    assert "CADCHF" not in CORE_SYMBOLS
_run("forex.universe: CORE_SYMBOLS uses CHFAUD, not CADCHF",
     test_chfaud_in_core_symbols_not_cadchf)


def test_session_pairs_asian_london_still_equals_core_symbols():
    import forex.runner as r
    from forex.universe import CORE_SYMBOLS
    union = r.SESSION_PAIRS["asian"] | r.SESSION_PAIRS["london"]
    assert union == CORE_SYMBOLS, (
        f"SESSION_PAIRS must still exactly partition CORE_SYMBOLS after the "
        f"rename -- diff: {union.symmetric_difference(CORE_SYMBOLS)}")
    assert "CHFAUD" in r.SESSION_PAIRS["london"]
_run("forex/runner: SESSION_PAIRS['asian']+['london'] still exactly equals "
     "CORE_SYMBOLS after the CHFAUD rename",
     test_session_pairs_asian_london_still_equals_core_symbols)


def test_london_breakout_pairs_uses_chfaud():
    import forex.strategy_london_breakout as lbo
    assert "CHFAUD" in lbo.PAIRS
    assert "CADCHF" not in lbo.PAIRS
    assert len(lbo.PAIRS) == 28, "the rename must not change the pair count"
_run("forex.strategy_london_breakout: PAIRS uses CHFAUD, still 28 pairs total",
     test_london_breakout_pairs_uses_chfaud)


def test_no_cadchf_in_active_pair_lists():
    # The old wrong symbol is intentionally still mentioned in universe.py's
    # explanatory comment on uic=7 (documents why the fix happened) -- that's
    # fine. What must NOT happen is CADCHF appearing in any of the actual
    # data structures a caller could look up.
    import forex.universe as u
    import forex.runner as r
    import forex.strategy_london_breakout as lbo
    all_symbols = {p["symbol"] for p in u.PAIRS}
    assert "CADCHF" not in all_symbols
    assert "CADCHF" not in u.CORE_SYMBOLS
    assert "CADCHF" not in u.SCANDI_SYMBOLS
    assert "CADCHF" not in r.SESSION_PAIRS["asian"]
    assert "CADCHF" not in r.SESSION_PAIRS["london"]
    assert "CADCHF" not in lbo.PAIRS
_run("forex: CADCHF doesn't appear in any actual pair set/list a caller "
     "could look up (PAIRS, CORE_SYMBOLS, SESSION_PAIRS, LBO PAIRS)",
     test_no_cadchf_in_active_pair_lists)


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
