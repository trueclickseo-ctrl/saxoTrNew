"""
Regression test -- 2026-09-01 MAE/MFE holding-window fix.

forex/runner.py `_run_exits` measured a position's MAE/MFE over the FULL
~350-bar daily chart window (`since_entry = df`) instead of its holding
period, inflating "worst unrealised excursion" to tens of thousands of EUR
against a ~EUR80 risk (the AI Trading Journal caught it). Fix:
_bars_for_excursion() bounds the window to calendar days held (+buffer),
intraday strategies get a single daily bar (flagged coarse), and an
excursion > _MAE_MFE_SANE_R x entry-risk is rejected rather than logged.

fix_observation_card_mae_mfe.py nulls the corrupted historical values.
"""

import json
import os
import subprocess
import sys
from datetime import date, timedelta

import pandas as pd

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


import forex.runner as r
import forex.forward_observation as fo


def _df(n=120):
    return pd.DataFrame({"Open": range(n), "High": [x + 3 for x in range(n)],
                         "Low": [x - 3 for x in range(n)], "Close": range(n)})


# ── _bars_for_excursion ──────────────────────────────────────────────────
def test_swing_window_is_days_held_plus_buffer():
    df = _df(120)
    pos = {"entry_date": (date.today() - timedelta(days=4)).isoformat(), "direction": "Buy"}
    w = r._bars_for_excursion(df, pos, "donchian")
    assert len(w) == 6                 # 4 held + 2 buffer
    # it's the TAIL, not the whole frame -> extremes come from recent bars only
    assert w["Low"].min() == df["Low"].iloc[-6]


def test_intraday_strategies_get_one_bar():
    df = _df(120)
    pos = {"entry_date": date.today().isoformat(), "direction": "Buy"}
    for strat in ("gap", "gap_weekend", "london_breakout", "london_breakout_v2"):
        assert len(r._bars_for_excursion(df, pos, strat)) == 1, strat


def test_zero_days_held_swing_gets_two_bars():
    pos = {"entry_date": date.today().isoformat(), "direction": "Buy"}
    assert len(r._bars_for_excursion(_df(120), pos, "rsi")) == 2


def test_bad_or_missing_entry_date_falls_back_small():
    for ed in ("", None, "garbage", "not-a-date"):
        w = r._bars_for_excursion(_df(120), {"entry_date": ed}, "rsi")
        assert 1 <= len(w) <= 5, ed


def test_none_and_empty_df_pass_through():
    assert r._bars_for_excursion(None, {}, "rsi") is None
    empty = pd.DataFrame({"Open": [], "High": [], "Low": [], "Close": []})
    assert len(r._bars_for_excursion(empty, {}, "rsi")) == 0


def test_window_never_exceeds_frame():
    pos = {"entry_date": (date.today() - timedelta(days=999)).isoformat()}
    assert len(r._bars_for_excursion(_df(30), pos, "donchian")) == 30


# ── constants / wiring ───────────────────────────────────────────────────
def test_intraday_set_and_sane_cap():
    assert {"gap", "gap_weekend", "london_breakout", "london_breakout_v2"} <= r._INTRADAY_STRATEGIES
    assert "rsi" not in r._INTRADAY_STRATEGIES and "donchian" not in r._INTRADAY_STRATEGIES
    assert 10 <= r._MAE_MFE_SANE_R <= 100


def test_old_unbounded_line_is_gone():
    src = open(r.__file__, encoding="utf-8").read()
    assert "since_entry = df" not in src, "the unbounded-window bug line is still there"
    assert "_bars_for_excursion(df, pos, strat_name)" in src
    # the sanity reject must gate the update
    exits_src = src[src.index("def _run_exits"):]
    blk = exits_src[exits_src.index("_bars_for_excursion"): exits_src.index("Exit advisor")]
    assert "_MAE_MFE_SANE_R" in blk and "risk_eur_at_entry" in blk


def test_exit_card_accepts_coarse_flag():
    p = os.path.join(BASE_DIR, "data", "_test_mae_card.jsonl")
    old = fo.TRADE_CARDS_LOG
    fo.TRADE_CARDS_LOG = p
    try:
        if os.path.exists(p):
            os.remove(p)
        fo.log_trade_exit_card(card_id="x", exit_price=1.0, exit_reason="tp",
                               gross_pnl_eur=1.0, commission_eur=None, net_pnl_eur=1.0,
                               r_multiple=0.1, mae_eur=-5.0, mfe_eur=10.0,
                               holding_hours=3.0, mae_mfe_coarse=True)
        row = json.loads(open(p, encoding="utf-8").readline())
        assert row["mae_mfe_coarse"] is True
        # default is False
        fo.log_trade_exit_card(card_id="y", exit_price=1.0, exit_reason="tp",
                               gross_pnl_eur=1.0, commission_eur=None, net_pnl_eur=1.0,
                               r_multiple=0.1, mae_eur=-5.0, mfe_eur=10.0, holding_hours=3.0)
        rows = [json.loads(l) for l in open(p, encoding="utf-8")]
        assert rows[1]["mae_mfe_coarse"] is False
    finally:
        fo.TRADE_CARDS_LOG = old
        if os.path.exists(p):
            os.remove(p)


# ── fix_observation_card_mae_mfe.py ──────────────────────────────────────
def test_invalidation_script_dry_run_and_apply(tmp=None):
    work = os.path.join(BASE_DIR, "data", "_test_mae_fix_cards.jsonl")
    entry = {"event": "entry", "card_id": "c1", "strategy": "gap", "symbol": "ZARJPY",
             "risk_eur": 80.0}
    # pre-fix card: NO mae_mfe_coarse key -> gets invalidated
    ex_bad = {"event": "exit", "card_id": "c1", "mae_eur": -24000.0, "mfe_eur": 1100.0,
              "net_pnl_eur": 120.0, "r_multiple": 1.5}
    # post-fix card: HAS mae_mfe_coarse key -> must be LEFT ALONE
    entry2 = {"event": "entry", "card_id": "c2", "strategy": "rsi", "symbol": "EURUSD",
              "risk_eur": 80.0}
    ex_clean = {"event": "exit", "card_id": "c2", "mae_eur": -60.0, "mfe_eur": 25.0,
                "net_pnl_eur": -10.0, "r_multiple": -0.13, "mae_mfe_coarse": False}
    with open(work, "w", encoding="utf-8") as f:
        for row in (entry, ex_bad, entry2, ex_clean):
            f.write(json.dumps(row) + "\n")

    import importlib
    mod = importlib.import_module("fix_observation_card_mae_mfe")
    mod.CARDS = work
    try:
        # dry run: file unchanged
        before = open(work, encoding="utf-8").read()
        mod.main.__globals__["sys"].argv = ["x"]
        mod.main()
        assert open(work, encoding="utf-8").read() == before

        # apply: pre-fix card nulled (raw kept, marker added); post-fix card untouched
        mod.main.__globals__["sys"].argv = ["x", "--apply"]
        mod.main()
        rows = [json.loads(l) for l in open(work, encoding="utf-8")]
        bad = next(r_ for r_ in rows if r_["card_id"] == "c1" and r_["event"] == "exit")
        clean = next(r_ for r_ in rows if r_["card_id"] == "c2" and r_["event"] == "exit")
        assert bad["mae_eur"] is None and bad["mfe_eur"] is None
        assert bad["mae_eur_raw"] == -24000.0 and bad["mfe_eur_raw"] == 1100.0
        assert "2026-09-01" in bad["mae_mfe_invalidated"]
        assert bad["net_pnl_eur"] == 120.0 and bad["r_multiple"] == 1.5   # untouched
        # the clean post-fix card is left EXACTLY as-is
        assert clean["mae_eur"] == -60.0 and clean["mfe_eur"] == 25.0
        assert "mae_mfe_invalidated" not in clean and "mae_eur_raw" not in clean
        assert any(p.startswith(os.path.basename(work) + ".bak_") for p in os.listdir(os.path.dirname(work)))

        # safe to re-run: second apply invalidates nothing new
        mod.main()
        rows2 = [json.loads(l) for l in open(work, encoding="utf-8")]
        assert sum(1 for r_ in rows2 if r_.get("mae_mfe_invalidated")) == 1
        assert next(r_ for r_ in rows2 if r_["card_id"] == "c2" and r_["event"] == "exit")["mae_eur"] == -60.0
    finally:
        d = os.path.dirname(work)
        for p in os.listdir(d):
            if p.startswith("_test_mae_fix_cards"):
                os.remove(os.path.join(d, p))
        mod.main.__globals__["sys"].argv = ["x"]


for _n, _f in list(globals().items()):
    if _n.startswith("test_") and callable(_f):
        _run(_n, _f)

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
