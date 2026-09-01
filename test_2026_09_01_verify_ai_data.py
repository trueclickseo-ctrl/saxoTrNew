"""
Regression test -- 2026-09-01 verify_ai_data.py + the MAE/MFE write-time
sanity gate it surfaced.

verify_ai_data.py is the repeatable read-only audit of the AI substrate
(ledger + observation cards). Its first run found 59 sim:gap:* cards with
MAE/MFE up to 170R -- values accumulated into pos["mae_eur"] before the
2026-09-01 holding-window fix deployed, which the per-cycle reject
(only skips the CURRENT reading) let ride through to the exit card.

Fix: forex/runner._run_exits nulls mae_eur/mfe_eur at exit-card write
time when |value| > _MAE_MFE_SANE_R x risk_eur_at_entry, stamping
mae_mfe_invalidated="accumulated-over-cap" so report_giveback / the
journal skip it. forward_observation.log_trade_exit_card gains the param.
"""

import ast
import inspect
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


import verify_ai_data as v
import forex.runner as fr
import forex.forward_observation as fo


# ── verify_ai_data checks ──────────────────────────────────────────────
def test_impossible_commission_flags_negative_only():
    good = [{"event": "exit", "card_id": "a", "commission_eur": 5.18}]
    bad = [{"event": "exit", "card_id": "b", "commission_eur": -5.18}]
    assert v.check_impossible_commission(good) == []
    assert len(v.check_impossible_commission(bad)) == 1


def test_pnl_sign_mismatch_catches_win_booked_on_a_clearly_losing_move():
    led = [{
        "status": "closed", "id": 1, "module": "forex", "strategy": "rsi",
        "symbol": "EURUSD", "direction": "Buy", "quantity": 100000,
        "entry_price": 1.1600, "exit_price": 1.1590, "realized_pnl": 40.0,  # -100 move, +40 booked
    }]
    hits = v.check_pnl_sign_mismatch(None, led)
    assert len(hits) == 1 and "40.00" in hits[0]
    led[0]["realized_pnl"] = -105.0                      # a loss + cost -> clean
    assert v.check_pnl_sign_mismatch(None, led) == []


def test_mae_mfe_bounds_uses_entry_card_risk():
    cards = [
        {"event": "entry", "card_id": "x", "risk_eur": 80.0},
        {"event": "exit", "card_id": "x", "mae_eur": -4000.0},          # 50R -> flag
        {"event": "entry", "card_id": "y", "risk_eur": 80.0},
        {"event": "exit", "card_id": "y", "mae_eur": -120.0},           # 1.5R -> fine
        {"event": "entry", "card_id": "z", "risk_eur": 80.0},
        {"event": "exit", "card_id": "z", "mae_eur": -9e9,
         "mae_mfe_invalidated": "x"},                                    # already flagged -> skip
    ]
    hits = v.check_mae_mfe_bounds(cards)
    assert len(hits) == 1 and "card_id" not in hits[0].lower()[:3]
    assert "x" in hits[0]


def test_duplicate_open_rows():
    led = [
        {"status": "open", "id": 1, "module": "forex", "strategy": "ml", "symbol": "EURUSD"},
        {"status": "open", "id": 2, "module": "forex", "strategy": "ml", "symbol": "EURUSD"},
        {"status": "open", "id": 3, "module": "forex", "strategy": "rsi", "symbol": "GBPUSD"},
        {"status": "closed", "id": 4, "module": "forex", "strategy": "ml", "symbol": "EURUSD"},
    ]
    hits = v.check_duplicate_open_rows(None, led)
    assert len(hits) == 1 and "[1, 2]" in hits[0]


def test_unpaired_cards():
    cards = [
        {"event": "entry", "card_id": "a"}, {"event": "exit", "card_id": "a"},
        {"event": "exit", "card_id": "orphan"},
    ]
    hits = v.check_unpaired_cards(cards)
    assert hits == ["orphan  exit card with NO entry card"]


def test_verify_module_parses_and_is_read_only():
    src = inspect.getsource(v)
    ast.parse(src)
    for banned in ("UPDATE ", "INSERT ", "DELETE ", "_save_state", "place_", "cancel_order"):
        assert banned not in src, banned


# ── the MAE/MFE write-time gate ────────────────────────────────────────
def test_run_exits_nulls_over_cap_mae_mfe_at_write_time():
    src = inspect.getsource(fr._run_exits)
    assert "_MAE_MFE_SANE_R * risk_at_entry" in src
    assert "_mae = _mfe = None" in src
    assert 'mae_mfe_invalidated=("accumulated-over-cap"' in src


def test_exit_card_accepts_and_writes_the_invalidated_marker():
    sig = inspect.signature(fo.log_trade_exit_card)
    assert "mae_mfe_invalidated" in sig.parameters
    src = inspect.getsource(fo.log_trade_exit_card)
    assert '"mae_mfe_invalidated": mae_mfe_invalidated' in src


def test_giveback_and_journal_already_skip_invalidated():
    import report_giveback
    assert 'x.get("mae_mfe_invalidated")' in inspect.getsource(report_giveback._load_trades)


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
