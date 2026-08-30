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


def test_chfaud_present_and_any_cadchf_is_correctly_labelled():
    # 2026-08-28's Saxo currencypairs cross-check re-added CADCHF as a
    # GENUINE separate pair (CAD/CHF) with its own verified uic (5, not 7).
    # So "CADCHF exists" is no longer a bug -- what must never happen is
    # CADCHF resolving to uic 7 (the AUD-quoted instrument) again.
    from forex.universe import PAIRS
    by_sym = {p["symbol"]: p for p in PAIRS}
    assert "CHFAUD" in by_sym
    if "CADCHF" in by_sym:
        c = by_sym["CADCHF"]
        assert c["uic"] != 7, "CADCHF must not point at uic 7 (that is CHFAUD)"
        assert c["base"] == "CAD" and c["quote"] == "CHF", (
            f"CADCHF mislabelled: base={c['base']} quote={c['quote']}")
_run("forex.universe: CHFAUD present; CADCHF (if present) is a real CAD/CHF "
     "pair on its own uic, never uic 7", test_chfaud_present_and_any_cadchf_is_correctly_labelled)


def test_chfaud_in_core_symbols():
    from forex.universe import CORE_SYMBOLS
    assert "CHFAUD" in CORE_SYMBOLS
_run("forex.universe: CORE_SYMBOLS contains CHFAUD",
     test_chfaud_in_core_symbols)


def test_session_pairs_are_valid_universe_symbols_with_chfaud_in_london():
    # Originally asserted SESSION_PAIRS exactly partitioned CORE_SYMBOLS.
    # CORE_SYMBOLS was later deliberately expanded (17 -> 49, commit 5cc5bb6,
    # for the LIVE_EUR rsi universe) without widening the day-trade session
    # sets, so that exact-partition invariant no longer holds by design.
    # What still must hold: every session pair is a real universe symbol,
    # CHFAUD sits in london, and no session set carries the old wrong name.
    import forex.runner as r
    from forex.universe import PAIRS
    valid = {p["symbol"] for p in PAIRS}
    union = r.SESSION_PAIRS["asian"] | r.SESSION_PAIRS["london"]
    unknown = union - valid
    assert not unknown, f"SESSION_PAIRS references non-universe symbols: {unknown}"
    assert union <= set(r.CORE_SYMBOLS) if hasattr(r, "CORE_SYMBOLS") else True
    assert "CHFAUD" in r.SESSION_PAIRS["london"]
_run("forex/runner: SESSION_PAIRS are all valid universe symbols, CHFAUD in "
     "london", test_session_pairs_are_valid_universe_symbols_with_chfaud_in_london)


def test_london_breakout_pairs_uses_chfaud():
    import forex.strategy_london_breakout as lbo
    assert "CHFAUD" in lbo.PAIRS
    assert "CADCHF" not in lbo.PAIRS
    assert len(lbo.PAIRS) == 28, "the rename must not change the pair count"
_run("forex.strategy_london_breakout: PAIRS uses CHFAUD, still 28 pairs total",
     test_london_breakout_pairs_uses_chfaud)


def test_no_pair_list_maps_a_name_to_the_wrong_uic_7():
    # The real anti-regression: whatever a caller looks up, the *only* symbol
    # that may resolve to uic 7 is CHFAUD. (CADCHF as a legit CAD/CHF pair on
    # its own uic is fine -- see the test above.)
    import forex.universe as u
    by_uic7 = [p["symbol"] for p in u.PAIRS if p["uic"] == 7]
    assert by_uic7 == ["CHFAUD"], f"uic 7 resolves to {by_uic7}, expected only CHFAUD"
_run("forex: uic 7 resolves to CHFAUD and nothing else",
     test_no_pair_list_maps_a_name_to_the_wrong_uic_7)


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
