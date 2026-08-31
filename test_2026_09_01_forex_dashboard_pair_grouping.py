"""
Regression test -- 2026-09-01 forex dashboard: OPEN POSITIONS grouped by PAIR.

Was grouped by strategy (each pair scattered across 2-3 strategy blocks).
Now grouped by pair with strategies nested + a per-pair mini-subtotal
(strategy count / units / P&L / closest stop), so multi-strategy
concentration on one instrument (e.g. XAUEUR held by rsi + gap +
advanced_ml) is visible at a glance. User request; applies to every tier's
positions table. Row P&L / subtotal math is unchanged.
"""

import os
import re
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

GREEN, RED, YELLOW, RESET, BOLD = "\033[92m", "\033[91m", "\033[93m", "\033[0m", "\033[1m"
_results = []


def _run(name, fn):
    try:
        fn()
        _results.append((name, True, None))
    except Exception as e:
        import traceback
        _results.append((name, False, f"{type(e).__name__}: {e}\n{traceback.format_exc()}"))


import forex_dashboard as fd

# deterministic FX: 1 unit of any quote ccy = 1 EUR
_orig_rate = fd._eur_per_unit
fd._eur_per_unit = lambda ccy, live=None: 1.0


def _pos(strat, sym, qty, entry, stop, ed="2026-08-30", uic=1):
    return dict(strategy=strat, symbol=sym, direction="Buy", qty=qty,
                entry=entry, stop=stop, atr=1.0, entry_date=ed, uic=uic)


def _plain(sym_positions, live):
    lines, tot_pnl, tot_cost, tot_costs = fd._positions_section(
        "OPEN POSITIONS — TEST", sym_positions, live, {}, 120, "-" * 120, color="")
    return [re.sub(r"\033\[[0-9;]*m", "", x) for x in lines], tot_pnl, tot_cost


def test_rows_are_grouped_by_pair_not_strategy():
    pos = [
        _pos("rsi", "AAAEUR", 1, 100, 90),
        _pos("gap", "BBBEUR", 1, 100, 95),
        _pos("advanced_ml", "AAAEUR", 1, 100, 80),
        _pos("gap", "AAAEUR", 3, 100, 98),
    ]
    live = {"AAAEUR": 99.0, "BBBEUR": 99.0}
    lines, *_ = _plain(pos, live)
    body = "\n".join(lines)
    # AAAEUR header appears once, and its 3 strategy rows are contiguous
    assert body.count("AAAEUR   3 strategies") == 1
    i_aaa = next(i for i, l in enumerate(lines) if l.strip().startswith("AAAEUR "))
    trio = lines[i_aaa + 1:i_aaa + 4]
    assert [l.split()[0] for l in trio] == ["rsi", "gap", "advanced_ml"]  # strat_order


def test_pairs_sorted_by_concentration_then_pnl():
    pos = [
        _pos("rsi", "ONE", 1, 100, 90),                 # 1 strat,  -1
        _pos("rsi", "TWOA", 1, 100, 90),                # 2 strat,  -2 total
        _pos("gap", "TWOA", 1, 100, 90),
        _pos("rsi", "TWOB", 1, 100, 90),                # 2 strat,  -10 total (worse)
        _pos("gap", "TWOB", 1, 100, 90),
    ]
    live = {"ONE": 99.0, "TWOA": 99.0, "TWOB": 90.0}
    lines, *_ = _plain(pos, live)
    order = [l.strip().split()[0] for l in lines
             if re.match(r"^(ONE|TWOA|TWOB)\s+\d+ strateg", l.strip())]
    # 2-strategy pairs first (TWOB before TWOA -- worse P&L), then the 1-strategy pair
    assert order == ["TWOB", "TWOA", "ONE"], order


def test_pair_header_fields():
    pos = [_pos("rsi", "XAUEUR", 1, 100, 90), _pos("gap", "XAUEUR", 3, 100, 98)]
    live = {"XAUEUR": 99.0}
    lines, *_ = _plain(pos, live)
    hdr = next(l.strip() for l in lines if l.strip().startswith("XAUEUR "))
    assert "2 strategies" in hdr
    assert "4 units" in hdr                       # 1 + 3
    assert "-4 EUR" in hdr                        # (99-100)*1 + (99-100)*3, EUR rate 1.0
    assert "closest stop" in hdr


def test_singular_grammar():
    pos = [_pos("rsi", "SOLO", 1, 100, 90)]
    lines, *_ = _plain(pos, {"SOLO": 99.0})
    hdr = next(l.strip() for l in lines if l.strip().startswith("SOLO "))
    assert "1 strategy  " in hdr and "1 unit  " in hdr
    assert "strategies" not in hdr and "units" not in hdr


def test_subtotal_math_unchanged():
    pos = [_pos("rsi", "AAA", 2, 100, 90), _pos("gap", "BBB", 5, 200, 190),
           _pos("ml", "AAA", 1, 100, 95)]
    live = {"AAA": 110.0, "BBB": 180.0}
    lines, tot_pnl, tot_cost = _plain(pos, live)
    # AAA: (110-100)*2 + (110-100)*1 = +30 ; BBB: (180-200)*5 = -100  -> -70
    assert round(tot_pnl) == -70
    # cost: 100*2 + 100*1 + 200*5 = 1300
    assert round(tot_cost) == 1300
    sub = next(l for l in lines if "SUBTOTAL" in l)
    assert "3 positions" in sub


def test_column_header_has_no_pair_label():
    lines, *_ = _plain([_pos("rsi", "AAA", 1, 100, 90)], {"AAA": 99.0})
    hdr = next(l for l in lines if "Strategy" in l and "Side" in l)
    assert "Pair" not in hdr


def test_empty_subset():
    lines, tot_pnl, tot_cost = _plain([], {})
    assert any("No open positions in this tier." in l for l in lines)
    assert tot_pnl == 0.0 and tot_cost == 0.0


def test_unpriced_pair_shows_dash_not_crash():
    pos = [_pos("rsi", "NOP", 1, 100, 90)]
    lines, tot_pnl, _ = _plain(pos, {})   # no live price
    hdr = next(l.strip() for l in lines if l.strip().startswith("NOP "))
    assert "—" in hdr
    assert tot_pnl == 0.0


try:
    for _n, _f in list(globals().items()):
        if _n.startswith("test_") and callable(_f):
            _run(_n, _f)
finally:
    fd._eur_per_unit = _orig_rate

print(f"\n{BOLD}{'='*66}{RESET}")
failed = [(n, e) for n, ok, e in _results if not ok]
for name, ok, err in _results:
    print(f"  [{GREEN}PASS{RESET}]" if ok else f"  [{RED}FAIL{RESET}]", name)
    if err:
        print(f"      {YELLOW}{err}{RESET}")
print(f"{BOLD}{'='*66}{RESET}")
if failed:
    print(f"{RED}{BOLD}  {len(failed)} / {len(_results)} FAILED{RESET}")
    sys.exit(1)
print(f"{GREEN}{BOLD}  ALL {len(_results)} TESTS PASSED{RESET}")
sys.exit(0)
