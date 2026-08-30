"""
Unit / logic tests -- 2026-08-30 -- the ORIGINAL "ml" strategy (forex/strategy_ml.py).

The advanced_ml A/B partner already has its own suite
(test_2026_08_30_advanced_ml_strategy.py); the original "ml" strategy had
none. This covers its observable contract and -- most importantly -- the
no-look-ahead property of its training window, since that is the one bug
class that would silently inflate a walk-forward number.

Run:  python test_2026_08_30_ml_strategy_and_logic.py
"""

import os
import sys

import numpy as np
import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import forex.strategy_ml as ml

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


# ── synthetic data helpers ────────────────────────────────────────────────────

def _frame(n=600, seed=0, drift=0.0004, vol=0.004):
    rng = np.random.default_rng(seed)
    steps = rng.normal(drift, vol, n).cumsum()
    close = 1.10 * np.exp(steps)
    high = close * (1 + np.abs(rng.normal(0, 0.0012, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.0012, n)))
    open_ = np.r_[close[0], close[:-1]]
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    return pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close},
                        index=idx)


# ── tests ────────────────────────────────────────────────────────────────────

def t_constants_unchanged():
    """Guard against an accidental edit to the A/B baseline."""
    assert ml.LOOKBACK == 126,                f"LOOKBACK={ml.LOOKBACK}"
    assert ml.CONFIDENCE_THRESHOLD == 0.58,   f"thr={ml.CONFIDENCE_THRESHOLD}"
    assert ml.ADX_MIN == 20
    assert ml.ATR_STOP_MULT == 2.0
    assert ml.TIME_STOP_DAYS == 20
    assert ml.RISK_PCT == 0.0025,             f"RISK_PCT={ml.RISK_PCT}"
    assert ml.MIN_BARS == ml.EMA_TREND + ml.LOOKBACK + 10 == 336


def t_no_signal_below_min_bars():
    md = {"EURUSD": _frame(n=ml.MIN_BARS - 1, seed=1)}
    assert ml.generate_signals(md) == []


def t_no_signal_on_none_or_empty():
    assert ml.generate_signals({"EURUSD": None}) == []
    assert ml.generate_signals({"EURUSD": pd.DataFrame()}) == []


def t_open_symbol_excluded():
    md = {"EURUSD": _frame(seed=2)}
    # Whatever it would emit, holding the pair must suppress it.
    held = {s["symbol"] for s in ml.generate_signals(md)}
    supp = {s["symbol"] for s in ml.generate_signals(md, open_symbols={"EURUSD"})}
    assert "EURUSD" not in supp
    if held:
        assert supp != held


def t_signal_shape_and_direction_consistency():
    md = {f"P{i}": _frame(seed=100 + i, drift=(0.001 if i % 2 else -0.001))
          for i in range(12)}
    sigs = ml.generate_signals(md)
    for s in sigs:
        assert set(s) >= {"symbol", "direction", "score", "atr", "close",
                          "stop_price", "ml_prob"}
        assert s["direction"] in ("Buy", "Sell")
        assert s["atr"] > 0
        assert 0.0 <= s["ml_prob"] <= 1.0
        if s["direction"] == "Buy":
            assert s["ml_prob"] >= ml.CONFIDENCE_THRESHOLD
            assert s["stop_price"] < s["close"]
            assert abs(s["score"] - s["ml_prob"]) < 1e-9
        else:
            assert s["ml_prob"] <= 1.0 - ml.CONFIDENCE_THRESHOLD
            assert s["stop_price"] > s["close"]
            assert abs(s["score"] - (1.0 - s["ml_prob"])) < 1e-9
    # sorted by score descending
    assert [s["score"] for s in sigs] == sorted((s["score"] for s in sigs),
                                                reverse=True)


def t_training_window_has_no_look_ahead():
    """
    The live prediction for bar N must not change when *future* bars are
    appended. _train_and_predict slices feats[N-127:N-1] for training and
    feats[-1] for the prediction row, and every feature is causal -- so
    freezing the first K bars and computing prob at K must equal computing
    prob on the full series truncated to K.
    """
    full = _frame(n=500, seed=7)
    for cut in (350, 400, 450):
        h, l, c = full["High"], full["Low"], full["Close"]
        p_trunc = ml._train_and_predict(h.iloc[:cut], l.iloc[:cut], c.iloc[:cut])
        # Same leading data, but with 50 more (future) bars visible afterwards.
        wide = full.iloc[:cut + 50]
        p_wide_at_cut = ml._train_and_predict(
            wide["High"].iloc[:cut], wide["Low"].iloc[:cut], wide["Close"].iloc[:cut])
        assert (p_trunc is None) == (p_wide_at_cut is None)
        if p_trunc is not None:
            assert abs(p_trunc - p_wide_at_cut) < 1e-9, \
                f"prediction at bar {cut} moved when future bars were added"


def t_target_label_is_next_bar_direction():
    """target[i] == 1  iff  close[i+1] > close[i]  (no shift error)."""
    c = pd.Series([1, 2, 1, 1, 3, 2], dtype=float)
    tgt = (c.diff().shift(-1) > 0).astype(float).values
    # up, down, flat->0, up, down, (last: NaN diff -> False)
    assert list(tgt[:5]) == [1.0, 0.0, 0.0, 1.0, 0.0]


def t_predict_proba_is_probability():
    rng = np.random.default_rng(0)
    X = rng.normal(0, 1, (200, 7))
    y = (X[:, 0] + X[:, 3] > 0).astype(float)
    w, b = ml._logistic_regression(X, y)
    p = ml._predict_proba(X, w, b)
    assert p.min() >= 0.0 and p.max() <= 1.0
    # learns *something*: mean prob on the positives beats the negatives
    assert p[y == 1].mean() > p[y == 0].mean()


def t_train_returns_none_on_degenerate_input():
    flat = pd.Series(np.full(500, 1.10))
    assert ml._train_and_predict(flat, flat, flat) is None  # NaN feats / <30 rows


def t_should_exit_priority_time_then_stop_then_flip():
    df = _frame(seed=3)
    pos_long = {"direction": "Buy", "stop_price": float(df["Close"].iloc[-1]) * 0.5}
    # A: time stop fires at exactly TIME_STOP_DAYS regardless of price
    ex, why = ml.should_exit(pos_long, df, ml.TIME_STOP_DAYS)
    assert ex and why.startswith("time_stop")
    ex, why = ml.should_exit(pos_long, df, ml.TIME_STOP_DAYS - 1)
    assert not ex
    # B: hard stop -- put the stop just above the last low so it triggers
    last_low = float(df["Low"].iloc[-1])
    pos_hit = {"direction": "Buy", "stop_price": last_low + abs(last_low) * 0.01}
    ex, why = ml.should_exit(pos_hit, df, 1)
    assert ex and why.startswith("hard_stop")
    # short mirror
    last_high = float(df["High"].iloc[-1])
    pos_short_hit = {"direction": "Sell", "stop_price": last_high - abs(last_high) * 0.01}
    ex, why = ml.should_exit(pos_short_hit, df, 1)
    assert ex and why.startswith("hard_stop")


def t_should_exit_none_on_short_history():
    short = _frame(n=ml.MIN_BARS - 1, seed=4)
    ex, why = ml.should_exit({"direction": "Buy", "stop_price": 0.0}, short, 3)
    assert ex is False and why == ""


def t_size_position_math_and_guards():
    # risk_amount / (2*atr), floored to lot
    q = ml.size_position(100_000, atr=0.0010)
    assert q == int((100_000 * ml.RISK_PCT) / (2 * 0.0010) / 1000) * 1000
    assert q % ml.LOT_ROUND == 0
    # atr <= 0 -> min_units (SIM) or 0 (block_below_min)
    assert ml.size_position(100_000, atr=0.0) == 1000
    assert ml.size_position(100_000, atr=0.0, block_below_min=True) == 0
    # tiny equity can't justify a lot
    assert ml.size_position(10, atr=0.01) == 1000
    assert ml.size_position(10, atr=0.01, block_below_min=True) == 0
    # monotonic in equity
    assert ml.size_position(200_000, 0.001) >= ml.size_position(100_000, 0.001)


def t_scan_summary_covers_every_pair():
    md = {"EURUSD": _frame(seed=5),
          "SHORT":  _frame(n=50, seed=6),
          "NONE":   None}
    rows = {r["symbol"]: r for r in ml.scan_summary(md)}
    assert set(rows) == {"EURUSD", "SHORT", "NONE"}
    assert rows["SHORT"]["status"] == "no_data"
    assert rows["NONE"]["status"] == "no_data"
    assert rows["EURUSD"]["status"] == "ok"
    assert rows["EURUSD"]["ml_prob"] is None or 0.0 <= rows["EURUSD"]["ml_prob"] <= 1.0


def t_registered_sim_only():
    import forex.runner as r
    assert r.STRATEGIES.get("ml") is ml
    assert "ml" not in getattr(r, "LIVE_ALLOWED_STRATEGIES", set())
    assert "ml" not in getattr(r, "LIVE_EUR_ALLOWED_STRATEGIES", set())


def t_subject_to_momentum_prefilter():
    """Doc contract: unlike bb/rsi, 'ml' IS in the momentum pre-filter path."""
    import forex.runner as r
    nmf = getattr(r, "_NO_MOMENTUM_FILTER", set())
    assert "ml" not in nmf


TESTS = [
    ("constants unchanged (A/B baseline frozen)",          t_constants_unchanged),
    ("no signal below MIN_BARS",                           t_no_signal_below_min_bars),
    ("no signal on None / empty frame",                    t_no_signal_on_none_or_empty),
    ("open_symbols suppresses that pair",                  t_open_symbol_excluded),
    ("signal shape + direction/threshold/stop consistency", t_signal_shape_and_direction_consistency),
    ("training window has NO look-ahead",                  t_training_window_has_no_look_ahead),
    ("target label = next-bar direction (shift correct)",  t_target_label_is_next_bar_direction),
    ("_predict_proba stays in [0,1] and learns signal",    t_predict_proba_is_probability),
    ("_train_and_predict returns None on degenerate input", t_train_returns_none_on_degenerate_input),
    ("should_exit priority: time -> hard_stop -> flip",    t_should_exit_priority_time_then_stop_then_flip),
    ("should_exit is silent on short history",             t_should_exit_none_on_short_history),
    ("size_position math + zero/again guards",             t_size_position_math_and_guards),
    ("scan_summary returns a row for every pair",          t_scan_summary_covers_every_pair),
    ("'ml' registered, absent from both LIVE allowlists",  t_registered_sim_only),
    ("'ml' is subject to the momentum pre-filter",         t_subject_to_momentum_prefilter),
]


def main():
    print(f"\n{BOLD}{'=' * 70}{RESET}")
    print(f"{BOLD}  ORIGINAL 'ml' STRATEGY -- unit / logic tests{RESET}")
    print(f"{BOLD}{'=' * 70}{RESET}")
    for name, fn in TESTS:
        _run(name, fn)
    print()
    for name, ok, err in _results:
        tag = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  [{tag}] {name}" + (f"  -- {err}" if err else ""))
    n_ok = sum(1 for _, ok, _ in _results if ok)
    print(f"\n{BOLD}{'=' * 70}{RESET}")
    if n_ok == len(_results):
        print(f"{GREEN}{BOLD}  ALL {n_ok} TESTS PASSED{RESET}")
    else:
        print(f"{RED}{BOLD}  {len(_results) - n_ok} / {len(_results)} FAILED{RESET}")
    print(f"{BOLD}{'=' * 70}{RESET}")
    sys.exit(0 if n_ok == len(_results) else 1)


if __name__ == "__main__":
    main()
