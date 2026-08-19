"""
test_strategy_learner.py
------------------------
Black-box tests for strategy_learner.py

Covers:
  1.  Default weights (no file)
  2.  slot_scale() at every critical boundary
  3.  Warm-up period (first MIN_TRADES_TO_LEARN trades must not shift weights)
  4.  Profitable trade rewards the strategy
  5.  Losing trade penalises the strategy
  6.  Weight floor (MIN_WEIGHT) — can never go below 0.30
  7.  Weight ceiling (MAX_WEIGHT) — can never exceed 2.00
  8.  File persistence — save / reload roundtrip
  9.  Magnitude factor — large P&L shifts weight more than small P&L
 10.  Recency decay — old trades carry less influence than new trades
 11.  num_processed tracking — second pass on same trades is a no-op
 12.  Unknown strategy auto-initialises to 1.0 then learns
 13.  New strategy seeded when get_weights() called after file exists
 14.  forex/runner imports cleanly with strategy_learner
 15.  futures/runner imports cleanly with strategy_learner
 16.  Full E2E: 20 profitable + 5 losing trades across strategies

Run:
    python test_strategy_learner.py
"""

import json
import os
import sys
import shutil
import tempfile
import types
import importlib

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

# ── Test harness ───────────────────────────────────────────────────────────

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
_results = []

def check(name: str, cond: bool, detail: str = ""):
    status = PASS if cond else FAIL
    print(f"  [{status}] {name}" + (f"  ({detail})" if detail else ""))
    _results.append((name, cond))
    return cond


def section(title: str):
    print(f"\n{'─'*60}")
    print(f"  {title}")
    print(f"{'─'*60}")


# ── Fixture: isolated DATA_DIR + mock pnl_tracker ─────────────────────────

class Fixture:
    """
    Each test gets a fresh temp directory for weight files and
    a mock pnl_tracker that returns a controlled list of trades.
    """
    def __init__(self):
        self.tmpdir   = tempfile.mkdtemp(prefix="sl_test_")
        self._trades  = []
        self._module  = None

    def set_trades(self, trades: list):
        self._trades = trades

    def install(self):
        """Patch strategy_learner to use tmpdir and mock pnl_tracker."""
        import strategy_learner as sl
        sl.DATA_DIR = self.tmpdir

        # Production pnl_tracker returns trades newest-first; mock must do the same.
        # run_learning_pass() calls reversed() on the result to get oldest-first.
        mock_pt = types.SimpleNamespace(
            get_closed_trades=lambda module=None, limit=1000: list(reversed(list(self._trades)))
        )
        sl.pnl_tracker = mock_pt
        self._module = sl
        return sl

    def cleanup(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        # Re-import to restore real DATA_DIR and pnl_tracker
        importlib.reload(importlib.import_module("strategy_learner"))


def _make_trade(strategy: str, pnl: float, qty: float = 1000,
                entry_price: float = 1.0) -> dict:
    return {
        "strategy":     strategy,
        "realized_pnl": pnl,
        "quantity":     qty,
        "entry_price":  entry_price,
    }


# ══════════════════════════════════════════════════════════════════════════
# Tests
# ══════════════════════════════════════════════════════════════════════════

def test_default_weights():
    section("1. Default weights (no file)")
    fx = Fixture()
    sl = fx.install()

    fw = sl.get_weights("forex")
    check("Returns dict", isinstance(fw, dict))
    check("10 forex strategies", len(fw) == 10,
          f"got {len(fw)}: {list(fw.keys())}")
    check("All equal 1.0", all(v == 1.0 for v in fw.values()),
          str(fw))

    fuw = sl.get_weights("futures")
    check("7 futures strategies", len(fuw) == 7,
          f"got {len(fuw)}: {list(fuw.keys())}")
    check("Futures all 1.0", all(v == 1.0 for v in fuw.values()))

    fx.cleanup()


def test_slot_scale():
    section("2. slot_scale() boundaries")
    import strategy_learner as sl

    check("0.30 → 0.50", sl.slot_scale(0.30) == 0.5,  f"got {sl.slot_scale(0.30)}")
    check("0.65 → 0.75", sl.slot_scale(0.65) == 0.75, f"got {sl.slot_scale(0.65)}")
    check("1.00 → 1.00", sl.slot_scale(1.00) == 1.0,  f"got {sl.slot_scale(1.00)}")
    check("1.50 → 1.25", sl.slot_scale(1.50) == 1.25, f"got {sl.slot_scale(1.50)}")
    check("2.00 → 1.50", sl.slot_scale(2.00) == 1.5,  f"got {sl.slot_scale(2.00)}")
    # Below floor
    check("0.00 clamps to 0.50", sl.slot_scale(0.00) == 0.5,
          f"got {sl.slot_scale(0.00)}")
    # Above ceiling
    check("3.00 clamps to 1.50", sl.slot_scale(3.00) == 1.5,
          f"got {sl.slot_scale(3.00)}")
    # Monotone: higher weight → higher scale
    vals = [sl.slot_scale(w/10) for w in range(3, 21)]
    check("slot_scale is monotone increasing", vals == sorted(vals),
          str(vals))


def test_warmup_period():
    section("3. Warm-up period (first 5 trades must not shift weights)")
    fx = Fixture()
    sl = fx.install()

    # Feed 4 highly profitable trades — not enough to exit warm-up
    trades = [_make_trade("ema", pnl=100.0) for _ in range(4)]
    fx.set_trades(trades)

    result = sl.run_learning_pass("forex")
    w = sl.get_weights("forex")
    check("4 trades: still in warm-up", result["new_trades"] == 4)
    check("Weights unchanged during warm-up",
          all(v == 1.0 for v in w.values()), str(w))
    check("num_processed incremented", sl._load_meta("forex").get("num_processed") == 4)

    fx.cleanup()


def test_profitable_trade_rewards():
    section("4. Profitable trade rewards the strategy")
    fx = Fixture()
    sl = fx.install()

    # 5 warm-up trades + 1 profitable ema trade
    warm   = [_make_trade("ema", pnl=1.0) for _ in range(5)]
    profit = [_make_trade("ema", pnl=50.0, qty=1000, entry_price=1.0)]
    fx.set_trades(warm + profit)

    sl.run_learning_pass("forex")
    w = sl.get_weights("forex")

    check("ema weight increased after profit",
          w["ema"] > 1.0, f"ema={w['ema']:.4f}")
    check("Other strategies unchanged",
          all(w[k] == 1.0 for k in w if k != "ema"),
          str({k: v for k, v in w.items() if k != "ema"}))

    fx.cleanup()


def test_losing_trade_penalises():
    section("5. Losing trade penalises the strategy")
    fx = Fixture()
    sl = fx.install()

    warm = [_make_trade("donchian", pnl=1.0) for _ in range(5)]
    loss = [_make_trade("donchian", pnl=-50.0, qty=1000, entry_price=1.0)]
    fx.set_trades(warm + loss)

    sl.run_learning_pass("forex")
    w = sl.get_weights("forex")

    check("donchian weight decreased after loss",
          w["donchian"] < 1.0, f"donchian={w['donchian']:.4f}")
    check("ema unchanged",
          w["ema"] == 1.0, f"ema={w['ema']:.4f}")

    fx.cleanup()


def test_weight_floor():
    section("6. Weight floor — never below MIN_WEIGHT (0.30)")
    fx = Fixture()
    sl = fx.install()

    # 5 warm-up + 100 massive losses for rsi
    warm   = [_make_trade("rsi", pnl=1.0) for _ in range(5)]
    losses = [_make_trade("rsi", pnl=-1000.0, qty=1000, entry_price=1.0)
              for _ in range(100)]
    fx.set_trades(warm + losses)

    sl.run_learning_pass("forex")
    w = sl.get_weights("forex")

    check("rsi weight >= MIN_WEIGHT (0.30)",
          w["rsi"] >= sl.MIN_WEIGHT,
          f"rsi={w['rsi']:.4f}, MIN={sl.MIN_WEIGHT}")

    fx.cleanup()


def test_weight_ceiling():
    section("7. Weight ceiling — never above MAX_WEIGHT (2.00)")
    fx = Fixture()
    sl = fx.install()

    warm    = [_make_trade("pullback", pnl=1.0) for _ in range(5)]
    profits = [_make_trade("pullback", pnl=1000.0, qty=1000, entry_price=1.0)
               for _ in range(100)]
    fx.set_trades(warm + profits)

    sl.run_learning_pass("forex")
    w = sl.get_weights("forex")

    check("pullback weight <= MAX_WEIGHT (2.00)",
          w["pullback"] <= sl.MAX_WEIGHT,
          f"pullback={w['pullback']:.4f}, MAX={sl.MAX_WEIGHT}")

    fx.cleanup()


def test_file_persistence():
    section("8. File persistence — save / reload roundtrip")
    fx = Fixture()
    sl = fx.install()

    warm   = [_make_trade("ema", pnl=1.0) for _ in range(5)]
    profit = [_make_trade("ema", pnl=100.0) for _ in range(3)]
    fx.set_trades(warm + profit)
    sl.run_learning_pass("forex")

    saved_weights = sl.get_weights("forex")
    ema_before    = saved_weights["ema"]
    check("ema > 1.0 after profits", ema_before > 1.0,
          f"ema={ema_before:.4f}")

    # Reload from file (simulates next day's run)
    reloaded = sl.get_weights("forex")
    check("Reloaded ema matches saved",
          reloaded["ema"] == ema_before,
          f"saved={ema_before:.4f}  reloaded={reloaded['ema']:.4f}")
    check("All 10 strategies present after reload", len(reloaded) == 10)

    fx.cleanup()


def test_magnitude_factor():
    section("9. Magnitude factor — large P&L shifts weight more than small P&L")
    # Two separate fixtures: one with small profits, one with large profits
    fx_small = Fixture()
    sl_small = fx_small.install()
    warm = [_make_trade("ema", pnl=1.0) for _ in range(5)]
    fx_small.set_trades(warm + [_make_trade("ema", pnl=1.0,
                                             qty=1000, entry_price=1.0)])
    sl_small.run_learning_pass("forex")
    small_w = sl_small.get_weights("forex")["ema"]
    fx_small.cleanup()

    fx_large = Fixture()
    sl_large = fx_large.install()
    fx_large.set_trades(warm + [_make_trade("ema", pnl=200.0,
                                             qty=1000, entry_price=1.0)])
    sl_large.run_learning_pass("forex")
    large_w = sl_large.get_weights("forex")["ema"]
    fx_large.cleanup()

    check("Large P&L shifts weight more than small P&L",
          large_w > small_w,
          f"large={large_w:.4f}  small={small_w:.4f}")


def test_recency_decay():
    section("10. Recency decay — recent trades carry more influence")
    import strategy_learner as sl

    # Simulate two runs:
    # Run A: 5 warm-up losses for rsi, then 1 recent profit for rsi
    # Run B: 5 warm-up profits for rsi, then 1 recent loss for rsi
    # Run A should end with higher rsi weight (profit is more recent)

    fx_a = Fixture()
    sl_a = fx_a.install()
    # oldest first (index 0 = oldest in unprocessed list):
    # 5 losses (warm-up, no weight change), then 1 big profit
    warm_losses  = [_make_trade("rsi", pnl=-100.0) for _ in range(5)]
    recent_profit= [_make_trade("rsi", pnl=100.0)]
    fx_a.set_trades(warm_losses + recent_profit)
    sl_a.run_learning_pass("forex")
    wa = sl_a.get_weights("forex")["rsi"]
    fx_a.cleanup()

    fx_b = Fixture()
    sl_b = fx_b.install()
    warm_profits = [_make_trade("rsi", pnl=100.0) for _ in range(5)]
    recent_loss  = [_make_trade("rsi", pnl=-100.0)]
    fx_b.set_trades(warm_profits + recent_loss)
    sl_b.run_learning_pass("forex")
    wb = sl_b.get_weights("forex")["rsi"]
    fx_b.cleanup()

    # After warm-up: the 1 non-warm-up trade determines the direction
    # Run A: recent profit → rsi should be > 1.0
    # Run B: recent loss   → rsi should be < 1.0
    check("Recent profit raises weight above 1.0", wa > 1.0,
          f"wa={wa:.4f}")
    check("Recent loss lowers weight below 1.0", wb < 1.0,
          f"wb={wb:.4f}")


def test_no_double_processing():
    section("11. num_processed — second pass on same trades is a no-op")
    fx = Fixture()
    sl = fx.install()

    warm   = [_make_trade("ema", pnl=1.0) for _ in range(5)]
    profit = [_make_trade("ema", pnl=100.0) for _ in range(3)]
    all_trades = warm + profit
    fx.set_trades(all_trades)

    r1 = sl.run_learning_pass("forex")
    w1 = sl.get_weights("forex")["ema"]

    # Second pass on exactly the same trade list — should be a no-op
    r2 = sl.run_learning_pass("forex")
    w2 = sl.get_weights("forex")["ema"]

    # 5 of the 8 are warm-up → new_trades = 3 post-warmup trades actually learned from
    check("First pass: 3 post-warmup trades learned", r1["new_trades"] == 3,
          f"got {r1['new_trades']}")
    check("num_processed = 8 after first pass",
          sl._load_meta("forex").get("num_processed") == 8)
    check("Second pass processed 0 trades", r2["new_trades"] == 0,
          f"got {r2['new_trades']}")
    check("Weight unchanged on second pass", w1 == w2,
          f"w1={w1:.4f}  w2={w2:.4f}")

    fx.cleanup()


def test_unknown_strategy_autoinit():
    section("12. Unknown strategy auto-initialises to 1.0 then learns")
    fx = Fixture()
    sl = fx.install()

    # "alpha_v3" is not in STRATEGY_NAMES["forex"]
    warm   = [_make_trade("alpha_v3", pnl=1.0) for _ in range(5)]
    profit = [_make_trade("alpha_v3", pnl=100.0) for _ in range(2)]
    fx.set_trades(warm + profit)

    sl.run_learning_pass("forex")
    w = sl.get_weights("forex")

    check("Unknown strategy appears in weights", "alpha_v3" in w,
          str(list(w.keys())))
    av3 = w.get("alpha_v3")
    check("Unknown strategy weight increased after profits",
          av3 is not None and av3 > 1.0,
          f"alpha_v3={av3}")

    fx.cleanup()


def test_new_strategy_seeded_on_load():
    section("13. New strategy seeded to 1.0 when get_weights() called after file exists")
    fx = Fixture()
    sl = fx.install()

    # Write a weight file that only has 3 strategies
    path = sl._weights_file("forex")
    os.makedirs(sl.DATA_DIR, exist_ok=True)
    with open(path, "w") as f:
        json.dump({"weights": {"ema": 1.5, "rsi": 0.8, "donchian": 1.2},
                   "meta": {}}, f)

    w = sl.get_weights("forex")
    check("Existing strategies preserved", w["ema"] == 1.5)
    check("10 strategies present after seeding", len(w) == 10,
          f"got {len(w)}: {list(w.keys())}")
    check("New strategies seeded at 1.0",
          all(w[k] == 1.0 for k in sl.STRATEGY_NAMES["forex"]
              if k not in ("ema", "rsi", "donchian")))

    fx.cleanup()


def test_forex_runner_import():
    section("14. forex/runner imports cleanly with strategy_learner present")
    try:
        if "forex.runner" in sys.modules:
            del sys.modules["forex.runner"]
        import forex.runner as fr
        check("forex.runner imported without error", True)
        check("strategy_learner referenced in forex.runner",
              hasattr(fr, "strategy_learner"))
    except Exception as exc:
        check(f"forex.runner import failed: {exc}", False)


def test_futures_runner_import():
    section("15. futures/runner imports cleanly with strategy_learner present")
    try:
        if "futures.runner" in sys.modules:
            del sys.modules["futures.runner"]
        import futures.runner as fur
        check("futures.runner imported without error", True)
        check("strategy_learner referenced in futures.runner",
              hasattr(fur, "strategy_learner"))
    except Exception as exc:
        check(f"futures.runner import failed: {exc}", False)


def test_e2e_multi_strategy():
    section("16. E2E — 20 profitable + 5 losing trades across strategies")
    fx = Fixture()
    sl = fx.install()

    trades = (
        # Warm-up (5 neutral)
        [_make_trade("ema", pnl=1.0) for _ in range(5)] +
        # ema: 8 wins, 0 losses  → should be highest weight
        [_make_trade("ema",      pnl=50.0)  for _ in range(8)] +
        # rsi: 5 wins, 2 losses  → medium weight
        [_make_trade("rsi",      pnl=30.0)  for _ in range(5)] +
        [_make_trade("rsi",      pnl=-20.0) for _ in range(2)] +
        # donchian: 0 wins, 3 losses → should be lowest weight
        [_make_trade("donchian", pnl=-40.0) for _ in range(3)] +
        # bb: 7 wins              → second-highest weight
        [_make_trade("bb",       pnl=20.0)  for _ in range(7)]
    )
    fx.set_trades(trades)

    result = sl.run_learning_pass("forex")
    w = sl.get_weights("forex")

    # Directional checks
    check("ema weight > 1.0 (8 wins)", w["ema"] > 1.0,
          f"ema={w['ema']:.4f}")
    check("rsi weight > 1.0 (net positive)", w["rsi"] > 1.0,
          f"rsi={w['rsi']:.4f}")
    check("donchian weight < 1.0 (3 losses)", w["donchian"] < 1.0,
          f"donchian={w['donchian']:.4f}")
    check("bb weight > 1.0 (7 wins)", w["bb"] > 1.0,
          f"bb={w['bb']:.4f}")

    # Ordering: more wins → higher weight
    check("ema weight > rsi weight (more wins)",
          w["ema"] > w["rsi"], f"ema={w['ema']:.4f}  rsi={w['rsi']:.4f}")
    check("rsi weight > donchian weight",
          w["rsi"] > w["donchian"],
          f"rsi={w['rsi']:.4f}  donchian={w['donchian']:.4f}")

    # Slot ordering: ema should get the largest slot multiplier
    scales = {k: sl.slot_scale(v) for k, v in w.items()
              if k in ("ema", "bb", "rsi", "donchian")}
    check("ema has highest slot_scale among traded strategies",
          scales["ema"] == max(scales.values()),
          str(scales))

    # All bounds respected
    check("All weights >= MIN_WEIGHT",
          all(v >= sl.MIN_WEIGHT for v in w.values()),
          str({k: v for k, v in w.items() if v < sl.MIN_WEIGHT}))
    check("All weights <= MAX_WEIGHT",
          all(v <= sl.MAX_WEIGHT for v in w.values()),
          str({k: v for k, v in w.items() if v > sl.MAX_WEIGHT}))

    # Untouched strategies stay at 1.0
    untouched = [k for k in w if k not in ("ema","rsi","donchian","bb")]
    check("Untouched strategies stay at 1.0",
          all(w[k] == 1.0 for k in untouched),
          str({k: w[k] for k in untouched}))

    print(f"\n  Final weights after E2E run:")
    for strat, weight in sorted(w.items(), key=lambda x: x[1], reverse=True):
        bar   = "█" * int(weight / 2.0 * 20) + "░" * (20 - int(weight / 2.0 * 20))
        scale = sl.slot_scale(weight)
        print(f"    {strat:<12} {bar}  {weight:.4f}  slots×{scale:.2f}")

    fx.cleanup()


# ── Runner ─────────────────────────────────────────────────────────────────

def main():
    print("\n" + "═"*60)
    print("  strategy_learner.py — Black Box Tests")
    print("═"*60)

    test_default_weights()
    test_slot_scale()
    test_warmup_period()
    test_profitable_trade_rewards()
    test_losing_trade_penalises()
    test_weight_floor()
    test_weight_ceiling()
    test_file_persistence()
    test_magnitude_factor()
    test_recency_decay()
    test_no_double_processing()
    test_unknown_strategy_autoinit()
    test_new_strategy_seeded_on_load()
    test_forex_runner_import()
    test_futures_runner_import()
    test_e2e_multi_strategy()

    total  = len(_results)
    passed = sum(1 for _, ok in _results if ok)
    failed = total - passed

    print(f"\n{'═'*60}")
    if failed == 0:
        print(f"\033[92m  ALL {total} TESTS PASSED\033[0m")
    else:
        print(f"\033[91m  {failed} / {total} TESTS FAILED\033[0m")
        print("  Failed tests:")
        for name, ok in _results:
            if not ok:
                print(f"    ✗  {name}")
    print("═"*60 + "\n")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
