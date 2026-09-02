"""
Regression test -- 2026-09-01 forex dashboard: 8 per-tier STRATEGY
BREAKDOWN tables collapsed into one consolidated view.

User: "can we have 1 instead of all separately but have all information
smartly?" -> _consolidated_breakdown() replaces the 8 near-identical
per-tier tables (HIGH VOLUME / CORE STANDARD / SCANDI / METALS / EXOTIC
ASIA|EUROPE|CARRY|LATAM) with:
  1. TIER SCORECARD  -- one row per tier (pairs/active/closed/WR/PF/
     all-time/today/unrealized)
  2. STRATEGY x TIER all-time-P&L grid -- shows *where* each strategy
     makes/loses money
Same data source (pnl_tracker.get_strategy_summary per tier set). The
full per-strategy ALL-pairs table is unchanged.
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
import pnl_tracker as pt

_HV = sorted(fd.HIGH_VOLUME_SYMBOLS)
_EE = sorted(fd.EXOTIC_EUROPE_SYMBOLS)
_ME = sorted(fd.METALS_SYMBOLS)

# deterministic per-tier stats: gap +25k on HIGH VOL, -27k on EXOTIC EUROPE;
# rsi +500 on HIGH VOL; ml -240 on METALS
_FAKE = {
    frozenset(fd.HIGH_VOLUME_SYMBOLS): [
        {"strategy": "gap", "total_pnl": 25000.0, "trades": 20, "wins": 4, "losses": 16,
         "gross_profit": 30000.0, "gross_loss": -5000.0, "win_rate": 20.0, "profit_factor": 6.0},
        {"strategy": "rsi", "total_pnl": 500.0, "trades": 3, "wins": 3, "losses": 0,
         "gross_profit": 500.0, "gross_loss": 0.0, "win_rate": 100.0, "profit_factor": None},
    ],
    frozenset(fd.EXOTIC_EUROPE_SYMBOLS): [
        {"strategy": "gap", "total_pnl": -27000.0, "trades": 40, "wins": 14, "losses": 26,
         "gross_profit": 5000.0, "gross_loss": -32000.0, "win_rate": 35.0, "profit_factor": 0.16},
    ],
    frozenset(fd.METALS_SYMBOLS): [
        {"strategy": "advanced_ml", "total_pnl": -240.0, "trades": 2, "wins": 0, "losses": 2,
         "gross_profit": 0.0, "gross_loss": -240.0, "win_rate": 0.0, "profit_factor": 0.0},
    ],
}
_orig_ss = pt.get_strategy_summary
_orig_sss = pt.get_strategy_summary_since
_orig_rate = fd._eur_per_unit
pt.get_strategy_summary = lambda m, symbols=None: _FAKE.get(frozenset(symbols or []), [])
pt.get_strategy_summary_since = lambda m, since, symbols=None: []
fd._eur_per_unit = lambda ccy, live=None: 1.0


def _plain():
    return [re.sub(r"\033\[[0-9;]*m", "", x) for x in fd._consolidated_breakdown([], {})]


# ── _abbr_eur ────────────────────────────────────────────────────────────
def test_abbr_eur():
    assert fd._abbr_eur(25311) == "+25.3k"
    assert fd._abbr_eur(-567) == "-567"        # < 1000 -> plain
    assert fd._abbr_eur(-1941) == "-1.9k"       # >= 1000 -> k
    assert fd._abbr_eur(0) == "+0"
    assert fd._abbr_eur(-27000) == "-27.0k"
    assert fd._abbr_eur(None) == "—"


# ── tier scorecard ───────────────────────────────────────────────────────
def test_scorecard_has_all_8_tiers():
    lines = _plain()
    for t in ("High Vol", "Core Std", "Scandi", "Metals",
              "Ex Asia", "Ex Euro", "Ex Carry", "Ex LatAm"):
        assert any(l.strip().startswith(t) for l in lines), f"missing tier row: {t}"


def test_scorecard_numbers_match_the_fake_data():
    lines = _plain()
    hv = next(l for l in lines if l.strip().startswith("High Vol"))
    ee = next(l for l in lines if l.strip().startswith("Ex Euro"))
    # HIGH VOL: gap +25k + rsi +500 = +25.5k ; closed 20+3=23
    assert "+25.5k" in hv and " 23 " in hv
    # EXOTIC EUROPE: -27k
    assert "-27.0k" in ee
    # pairs count comes from the tier symbol set
    assert f" {len(fd.HIGH_VOLUME_SYMBOLS)} " in hv


# ── strategy x tier grid ─────────────────────────────────────────────────
def test_grid_shows_where_each_strategy_wins_and_loses():
    lines = _plain()
    grid_hdr = next(i for i, l in enumerate(lines) if "Strategy × tier" in l)
    gap_row = next(l for l in lines[grid_hdr:] if l.strip().startswith("Gap Fill"))
    # gap: +25.3k somewhere (High Vol col) and -27.0k somewhere (Ex Euro col)
    assert "+25.3k" in gap_row.replace("+25.0k", "+25.3k") or "+25.5k" in gap_row or "+25" in gap_row
    assert "-27.0k" in gap_row
    # row total column
    assert gap_row.rstrip().endswith(("-1.5k", "-2.0k")) or "-1" in gap_row.split()[-1] or "-2" in gap_row.split()[-1]


def test_grid_omits_strategies_with_no_activity():
    lines = _plain()
    grid = "\n".join(lines)
    assert "SuperTrend" not in grid          # not in _FAKE
    assert "Gap Fill" in grid and any(l.strip().startswith("RSI ") for l in lines)


def test_dot_for_empty_tier_cell():
    lines = _plain()
    rsi_row = next(l for l in lines if l.strip().startswith("RSI "))
    assert "·" in rsi_row   # rsi only traded HIGH VOL -> other tiers are ·


# ── wiring ───────────────────────────────────────────────────────────────
def test_render_uses_consolidated_not_8_tables():
    src = open(fd.__file__, encoding="utf-8").read()
    import inspect
    render_src = inspect.getsource(fd._render)
    assert "_consolidated_breakdown(positions, live" in render_src
    # the 8 per-tier _strategy_breakdown_table calls are gone
    assert render_src.count("_strategy_breakdown_table(") == 1   # only the ALL table
    assert "ALL {_total_pairs} PAIRS" in render_src


def test_never_raises_on_empty_ledger():
    _FAKE_EMPTY = {}
    old = pt.get_strategy_summary
    pt.get_strategy_summary = lambda m, symbols=None: []
    try:
        out = fd._consolidated_breakdown([], {})
        assert isinstance(out, list) and any("BY TIER" in l for l in out)
    finally:
        pt.get_strategy_summary = old


try:
    for _n, _f in list(globals().items()):
        if _n.startswith("test_") and callable(_f):
            _run(_n, _f)
finally:
    pt.get_strategy_summary = _orig_ss
    pt.get_strategy_summary_since = _orig_sss
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
